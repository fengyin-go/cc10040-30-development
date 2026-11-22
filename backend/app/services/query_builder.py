"""查询构建器领域服务：构建和缓存多维组合查询模板以加速分析。

消费口径版本的指标定义，生成查询模板供数据服务调用。
跨模块数据流: caliber_version -> query_builder -> data_service -> cache_layer -> query_journal

运行日志约定：
- 冷执行和缓存命中都会写日志，且都先登记「执行中」、结束时补齐耗时，因此同一秒
  内的并发执行各有唯一执行标识，诊断文件也能按完成序精确切片。
- 日志分开记录执行标识、真实指标标识、模板标识，以及实际取数口径、完成方式和耗时。
- 缓存命中按「形成该结果的旧口径」记账，命中本身不产生新的口径。
- 复跑（诊断回放）绕过缓存，以独立执行写入日志，并关联旧档里的执行标识。
"""
from __future__ import annotations

import copy
import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService

DEFAULT_SLOW_THRESHOLD_MS = 100.0


class QueryBuilderService(BaseModuleService):
    MODULE = "query_builder"
    ENTITY = "查询模板"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["模板名称", "关联指标", "维度组合", "过滤条件", "缓存策略"]
    REQUIRED_FIELDS = ["模板名称", "关联指标", "维度组合"]
    STATUS_ORDER = ["草稿", "已发布", "已过期"]
    ACTION_RULES = {"发布模板": "已发布", "过期模板": "已过期"}
    NEGATIVE_ACTIONS = []

    # ── helpers ──

    @staticmethod
    def slow_threshold_ms() -> float:
        return float(store.slow_threshold_ms or DEFAULT_SLOW_THRESHOLD_MS)

    def _cache_key(self, template: dict[str, Any], caliber_version: str = "") -> str:
        # 缓存键描述「哪个模板查什么」，不绑定口径：命中后按条目里记录的形成口径
        # 记账，口径切换由 caliber_version.activate_version 主动失效来处理。
        template_id = int(template.get("id", 0) or 0)
        return (
            f"query:{template.get('关联指标', '')}:{template.get('维度组合', '')}:"
            f"{template.get('过滤条件', '')}:tpl={template_id}"
        )

    def _cache_ttl(self, template: dict[str, Any]) -> int:
        policy = str(template.get("缓存策略", ""))
        if "长期" in policy:
            return 3600
        if "短期" in policy:
            return 60
        return 300

    @staticmethod
    def _active_caliber(metric_name: str) -> dict[str, Any] | None:
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        return next((c for c in calibers if c.get("status") == "已生效"), None)

    @staticmethod
    def _metric_entry(metric_name: str) -> dict[str, Any] | None:
        rows = store.find_by_field("metric_definition", "指标名称", metric_name)
        return rows[0] if rows else None

    def _compute_rows(
        self,
        *,
        metric_name: str,
        metric: dict[str, Any] | None,
        template: dict[str, Any],
        caliber_version: str,
        caliber_desc: str,
    ) -> list[dict[str, Any]]:
        """按给定口径实际取数。口径描述参与结果，复跑时能看出新旧口径差异。"""
        metric_id = int((metric or {}).get("id", 0))
        dimensions = [dim.strip() for dim in str(template.get("维度组合", "")).split(",")
                      if dim.strip()]
        base_rows = store.find_by_field("metric_definition", "指标名称", metric_name)
        rows: list[dict[str, Any]] = []
        for idx, source in enumerate(base_rows or [metric or {}]):
            row = {
                "序号": idx + 1,
                "指标": metric_name,
                "指标标识": f"{metric_name}#{metric_id}",
                "口径版本": caliber_version,
                "计算口径": caliber_desc or source.get("计算口径", ""),
                "统计粒度": source.get("统计粒度", ""),
            }
            for dim in dimensions:
                row[dim] = f"维度值_{idx + 1}"
            rows.append(row)
        return rows

    @staticmethod
    def _summary(template: dict[str, Any], *, suffix: str = "") -> str:
        base = (f"查询模板: {template.get('模板名称')}, 维度: {template.get('维度组合')}"
                f", 过滤: {template.get('过滤条件') or '无'}")
        return f"{base}, {suffix}" if suffix else base

    def _simulated_cold_duration_ms(self, template: dict[str, Any]) -> float:
        """模拟冷执行耗时。缓存策略为「慢」或维度多时变慢，用于稳定制造慢查询样本。"""
        policy = str(template.get("缓存策略", ""))
        dims = len([d for d in str(template.get("维度组合", "")).split(",") if d.strip()])
        duration = 8.0 + dims * 4.0
        if "慢" in policy:
            duration += 150.0
        return duration

    # ── main execution ──

    def execute_query(
        self,
        entry_id: int,
        operator: str = "系统",
        use_cache: bool = True,
        *,
        replay_of: dict[str, Any] | None = None,
        source_file_id: str | None = None,
        template_override: dict[str, Any] | None = None,
        duration_override_ms: float | None = None,
    ) -> dict[str, Any]:
        """执行查询模板并返回结果与日志。

        use_cache=True 时命中缓存直接按旧口径记账；use_cache=False 时必定冷执行。
        replay_of 用于诊断文件的复跑，template_override / caliber_override 允许以
        历史档中的模板与口径为入口，但复跑会按「当前生效口径」重新取数。
        """
        template: dict[str, Any] | None
        if template_override is not None:
            template = copy.deepcopy(template_override)
            template["id"] = template_override.get("模板ID") or template_override.get("id", 0)
        else:
            template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"查询模板 {entry_id} 不存在"}
        if template.get("status") != "已发布":
            return {"error": f"查询模板 {entry_id} 未发布"}

        metric_name = str(template.get("关联指标", ""))
        if not metric_name:
            return {"error": "查询模板未关联指标"}

        metric = self._metric_entry(metric_name)
        metric_id = int((metric or {}).get("id", 0))
        template_id = int(template.get("id", entry_id) or 0)
        template_name = str(template.get("模板名称", ""))

        active_caliber = self._active_caliber(metric_name)
        if active_caliber is None:
            return {"error": f"指标 {metric_name} 没有已生效的口径版本"}

        current_caliber_version = str(active_caliber.get("版本号", ""))
        current_caliber_desc = str(active_caliber.get("口径描述", ""))
        threshold = self.slow_threshold_ms()
        is_replay = replay_of is not None
        use_cache = bool(use_cache) and not is_replay

        cache_key = self._cache_key(template)
        template_snapshot = {
            "模板ID": template_id,
            "模板名称": template_name,
            "关联指标": metric_name,
            "维度组合": template.get("维度组合", ""),
            "过滤条件": template.get("过滤条件", ""),
            "缓存策略": template.get("缓存策略", ""),
            "status": template.get("status", ""),
        }

        def begin(completion_method: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
            return store.begin_query_execution(
                operator=operator,
                sql_summary="",
                completion_method=completion_method,
                metric_id=metric_id,
                metric_name=metric_name,
                template_id=template_id,
                template_name=template_name,
                # 登记时按当前生效口径占位；缓存命中会在完成时改写为形成结果的口径
                caliber_version=current_caliber_version,
                caliber_desc=current_caliber_desc,
                filter_condition=str(template.get("过滤条件", "")),
                dimension_combo=str(template.get("维度组合", "")),
                replay_of_execution_id=(int(replay_of["id"]) if is_replay else None),
                replay_of_execution_key=(replay_of.get("执行标识") if is_replay else None),
                source_file_id=source_file_id,
                template_snapshot=template_snapshot,
                extra=extra,
            )

        # ── 缓存命中 ──
        if use_cache:
            cached_entry = store.cache_get_entry(cache_key)
            if cached_entry is not None:
                metadata = cached_entry.get("metadata") or {}
                cached_rows = cached_entry.get("value") or []
                cached_caliber_version = str(
                    metadata.get("caliber_version", current_caliber_version))
                cached_caliber_desc = str(metadata.get("caliber_desc", current_caliber_desc))
                cached_source_execution = metadata.get("source_execution_id")
                execution = begin(
                    "缓存命中",
                    extra={
                        "缓存来源执行标识": cached_source_execution,
                        "复跑关联执行标识": int(replay_of["id"]) if is_replay else None,
                    },
                )
                t_start = time.perf_counter()
                row_count = len(cached_rows) if isinstance(cached_rows, list) else 1
                hit_duration = (time.perf_counter() - t_start) * 1000
                if duration_override_ms is not None:
                    hit_duration = float(duration_override_ms)
                # 命中按形成结果的旧口径记账，而不是当前生效口径
                completed = store.complete_query_execution(
                    int(execution["id"]),
                    row_count=row_count,
                    duration_ms=max(hit_duration, 0.05),
                    threshold_ms=threshold,
                    extra={
                        "查询语句摘要": self._summary(
                            template, suffix=f"缓存命中复用执行 {cached_source_execution}"),
                        "取数口径": cached_caliber_version,
                        "取数口径版本": cached_caliber_version,
                        "取数口径描述": cached_caliber_desc,
                        "缓存形成口径": cached_caliber_version,
                        "缓存来源执行标识": cached_source_execution,
                        "命中时当前离线口径": current_caliber_version,
                    },
                ) or execution
                return {
                    "source": "cache",
                    "template": template_name,
                    "template_id": template_id,
                    "metric": metric_name,
                    "metric_id": metric_id,
                    "caliber": cached_caliber_version,
                    "caliber_desc": cached_caliber_desc,
                    "execution_id": completed.get("id"),
                    "execution_key": completed.get("执行标识"),
                    "duration_ms": completed.get("duration_ms"),
                    "rows": copy.deepcopy(cached_rows),
                }

        # ── 冷执行 ──
        execution = begin("冷执行", extra={
            "复跑关联执行标识": int(replay_of["id"]) if is_replay else None,
        })
        started = time.perf_counter()
        computed_rows = self._compute_rows(
            metric_name=metric_name,
            metric=metric,
            template=template,
            caliber_version=current_caliber_version,
            caliber_desc=current_caliber_desc,
        )
        if duration_override_ms is not None:
            duration_ms = float(duration_override_ms)
        else:
            duration_ms = max(self._simulated_cold_duration_ms(template),
                              (time.perf_counter() - started) * 1000)

        completed = store.complete_query_execution(
            int(execution["id"]),
            row_count=len(computed_rows),
            duration_ms=duration_ms,
            threshold_ms=threshold,
            extra={
                "查询语句摘要": self._summary(
                    template, suffix="复跑" if is_replay else "冷执行"),
            },
        ) or execution

        cache_ttl = self._cache_ttl(template)
        if not is_replay and use_cache:
            store.cache_set(
                cache_key,
                computed_rows,
                cache_ttl,
                metadata={
                    "caliber_version": current_caliber_version,
                    "caliber_desc": current_caliber_desc,
                    "source_execution_id": completed.get("id"),
                    "source_execution_key": completed.get("执行标识"),
                    "template_id": template_id,
                    "metric_name": metric_name,
                },
            )

        return {
            "source": "computed",
            "replay": is_replay,
            "replay_of_execution_id": int(replay_of["id"]) if is_replay else None,
            "template": template_name,
            "template_id": template_id,
            "metric": metric_name,
            "metric_id": metric_id,
            "caliber": current_caliber_version,
            "caliber_desc": current_caliber_desc,
            "execution_id": completed.get("id"),
            "execution_key": completed.get("执行标识"),
            "duration_ms": completed.get("duration_ms"),
            "cached_for_seconds": None if is_replay else cache_ttl,
            "rows": computed_rows,
        }

    def replay_from_diagnostic(
        self,
        file_id: str,
        execution_ref: str | int,
        operator: str = "接班人",
    ) -> dict[str, Any]:
        """从查询诊断文件复跑一笔慢查：绕过缓存，按当前口径冷执行。

        - 只有完整交付的文件可以作为依据；
        - 复跑写回查询日志作为独立执行，并关联旧文件里的执行标识；
        - 旧文件内容保持历史口径和耗时，不因复跑而改写。

        execution_ref 支持执行标识（exec-000123）或来源执行ID。
        """
        if not store.is_diagnostic_replayable(file_id):
            return {"error": f"诊断文件 {file_id} 未完整交付，不能作为复跑依据"}
        source = store.find_diagnostic_execution(file_id, execution_ref)
        if source is None:
            return {"error": f"诊断文件 {file_id} 中没有执行 {execution_ref} 的明细"}
        template_id = int(source.get("模板ID", 0))
        if template_id <= 0:
            return {"error": "来源执行缺少模板标识，无法复跑"}
        template_snapshot = source.get("template_snapshot") or {}
        if not template_snapshot:
            return {"error": "来源执行缺少模板快照，无法复跑"}
        # 复跑从另一个查询执行入口进入，强制冷执行并跳过缓存。
        return self.execute_query(
            template_id,
            operator=operator,
            use_cache=False,
            replay_of=source,
            source_file_id=file_id,
            template_override=template_snapshot,
        )

    def invalidate_query_cache(self, metric_name: str) -> int:
        """当口径版本变更时，失效指标相关的所有查询缓存。

        跨模块数据流: caliber_version -> query_builder -> cache_layer
        """
        return store.cache_invalidate_by_prefix(f"query:{metric_name}:")
