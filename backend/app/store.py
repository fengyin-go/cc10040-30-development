"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
同时提供跨模块查询和数据流处理能力。
"""
from __future__ import annotations

import copy
import time
import threading
from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._id_counters: dict[str, int] = {}
        # Cache layer storage: key -> (expires_at, {"value": ..., "metadata": {...}})
        self._cache: dict[str, tuple[float, Any]] = {}
        # 查询运行日志：以 execution_id 单调递增，记录执行中/已完成两个阶段。
        self._query_log: list[dict[str, Any]] = []
        self._execution_counter = 0
        self._completion_counter = 0
        # 查询诊断文件登记表：file_id -> artifact 记录（内容在生成后冻结，不再跟随运行日志变化）
        self._diagnostic_files: dict[str, dict[str, Any]] = {}
        # 慢查询阈值（毫秒），运行日志判定与诊断文件默认取样使用同一口径。
        self._slow_threshold_ms = 100.0
        # Cohort snapshots
        self._cohort_snapshots: dict[str, list[dict[str, Any]]] = {}
        # Anomaly records
        self._anomaly_records: list[dict[str, Any]] = []
        # Recalc history
        self._recalc_history: list[dict[str, Any]] = []
        # Lock for thread safety (re-entrant：登记/完成/快照会组合调用)
        self._lock = threading.RLock()

    @property
    def lock(self):
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        if not rows:
            return 1
        return max(int(row.get("id", 0)) for row in rows) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ── Cross-module query helpers ──

    def find_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """查找模块中 field 等于 value 的所有记录。"""
        return [row for row in self.rows(module) if row.get(field) == value]

    def find_by_status(self, module: str, status: str) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get("status") == status]

    # ── Query execution journal ──
    #
    # 运行日志以「执行」为单位：开始执行时先登记（状态=执行中），结束时补齐耗时、
    # 完成方式、取数口径并分配「完成序」。执行标识与时间戳分离，同一秒内并发执行
    # 也各有唯一标识；诊断文件按完成序水位线取快照，能明确覆盖到哪一次已完成执行。

    @property
    def slow_threshold_ms(self) -> float:
        """慢查询阈值（毫秒）：运行日志判定、诊断文件取样和摘要统计共用。"""
        return self._slow_threshold_ms

    @slow_threshold_ms.setter
    def slow_threshold_ms(self, value: float) -> None:
        self._slow_threshold_ms = max(0.0, float(value))

    def begin_query_execution(
        self,
        *,
        operator: str,
        sql_summary: str,
        completion_method: str,
        metric_id: int = 0,
        metric_name: str = "",
        template_id: int = 0,
        template_name: str = "",
        caliber_version: str = "",
        caliber_desc: str = "",
        cache_source_execution_id: int | None = None,
        replay_of_execution_id: int | None = None,
        replay_of_execution_key: str | None = None,
        source_file_id: str | None = None,
        filter_condition: str = "",
        dimension_combo: str = "",
        template_snapshot: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一次正在执行的查询，返回带唯一执行标识的日志条目。"""
        with self._lock:
            self._execution_counter += 1
            execution_id = self._execution_counter
            started_at = datetime.now()
            entry: dict[str, Any] = {
                "id": execution_id,
                "执行标识": f"exec-{execution_id:06d}",
                "执行序号": execution_id,
                "查询时间": started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "开始时间": started_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "完成时间": None,
                "操作人": operator,
                # 真实指标标识：指标名 + 指标定义 ID（不是模板 ID）
                "指标标识": f"{metric_name}#{metric_id}" if metric_name else f"metric_{metric_id}",
                "指标ID": metric_id,
                "指标名称": metric_name,
                # 模板标识：模板名 + 查询模板 ID
                "模板标识": f"{template_name}#{template_id}" if template_name else f"template_{template_id}",
                "模板ID": template_id,
                "模板名称": template_name,
                "模板维度组合": dimension_combo,
                "模板过滤条件": filter_condition,
                "涉及指标": (f"{metric_name} (metric_{metric_id})" if metric_name
                             else f"metric_{metric_id}"),
                "查询语句摘要": sql_summary,
                "返回行数": "",
                "status": "正常",
                "状态": "执行中",
                "completed": False,
                "duration_ms": 0.0,
                "耗时(毫秒)": 0.0,
                "完成方式": completion_method,
                "取数口径": caliber_version,
                "取数口径版本": caliber_version,
                "取数口径描述": caliber_desc,
                "慢查询阈值(毫秒)": self._slow_threshold_ms,
                "缓存来源执行标识": cache_source_execution_id,
                "复跑关联执行标识": replay_of_execution_id,
                "复跑关联执行键": replay_of_execution_key,
                "关联诊断文件": source_file_id,
                "完成序": None,
                "pending": True,
                "abnormal": False,
                "template_snapshot": copy.deepcopy(template_snapshot or {}),
            }
            if extra:
                entry.update(copy.deepcopy(extra))
            self._query_log.append(entry)
            return copy.deepcopy(entry)

    def complete_query_execution(
        self,
        execution_id: int,
        *,
        row_count: int,
        duration_ms: float,
        status: str | None = None,
        threshold_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """完成一次查询：记录真实耗时，并按同一慢查阈值判定状态。"""
        with self._lock:
            entry = next(
                (item for item in self._query_log if item.get("id") == execution_id),
                None,
            )
            if entry is None:
                return None
            threshold = self._slow_threshold_ms if threshold_ms is None else float(threshold_ms)
            duration = round(max(0.0, float(duration_ms)), 3)
            if status is None:
                status = "慢查询" if duration > threshold else "正常"
            finished_at = datetime.now()
            self._completion_counter += 1
            entry.update({
                "查询时间": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
                "完成时间": finished_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "返回行数": str(row_count),
                "duration_ms": duration,
                "耗时(毫秒)": duration,
                "status": status,
                "状态": "已完成",
                "completed": True,
                "完成序": self._completion_counter,
                "慢查询阈值(毫秒)": threshold,
                "pending": status != "正常",
                "abnormal": status in ("慢查询", "异常终止"),
            })
            if extra:
                entry.update(copy.deepcopy(extra))
            return copy.deepcopy(entry)

    def log_query(self, metric_id: int, operator: str, sql_summary: str,
                  row_count: int, duration_ms: float, status: str = "正常",
                  completion_method: str = "冷执行") -> dict[str, Any]:
        """一次性写入一条已完成日志（兼容异常检测器等轻量调用方）。"""
        entry = self.begin_query_execution(
            operator=operator,
            sql_summary=sql_summary,
            completion_method=completion_method,
            metric_id=metric_id,
            metric_name="",
        )
        # 轻量调用方显式传入状态，不再按阈值改写
        return self.complete_query_execution(
            int(entry["id"]),
            row_count=row_count,
            duration_ms=duration_ms,
            status=status,
            threshold_ms=float("inf"),
        ) or entry

    def query_logs(self) -> list[dict[str, Any]]:
        return self._query_log

    def update_execution_fields(self, execution_id: int,
                                values: dict[str, Any]) -> dict[str, Any] | None:
        """只更新告警类字段，真实耗时、完成方式与口径保持原样。"""
        with self._lock:
            entry = next(
                (item for item in self._query_log if item.get("id") == execution_id),
                None,
            )
            if entry is None:
                return None
            for key, value in values.items():
                if key in ("duration_ms", "耗时(毫秒)", "完成方式", "取数口径",
                           "完成时间", "完成序"):
                    continue
                entry[key] = value
            return copy.deepcopy(entry)

    def find_execution(self, execution_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = next(
                (item for item in self._query_log if item.get("id") == execution_id),
                None,
            )
            return copy.deepcopy(entry) if entry else None

    # ── Query diagnostic files ──
    #
    # 文件只是在下载开始时对「已完成执行」做一次冻结切片：摘要和耗时样本都从同
    # 一份切片计算，切片之后新写入的执行不会改变文件。只有完整交付的文件才能
    # 作为复跑依据，中断的半成品可以在文件登记表里看到，但不能发起复跑。

    def create_diagnostic_file(self, *, threshold_ms: float | None = None,
                               operator: str = "") -> dict[str, Any]:
        with self._lock:
            file_id = f"diag-{len(self._diagnostic_files) + 1:06d}"
            record = {
                "文件标识": file_id,
                "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "创建人": operator,
                "慢查询阈值毫秒": (self._slow_threshold_ms if threshold_ms is None
                                   else max(0.0, float(threshold_ms))),
                "状态": "待下载",
                "已完整交付": False,
                "快照": None,
                "最近下载开始时间": None,
                "最近交付时间": None,
                "最近中断时间": None,
            }
            self._diagnostic_files[file_id] = record
            return self._public_diagnostic_file(record)

    def _public_diagnostic_file(self, record: dict[str, Any]) -> dict[str, Any]:
        snapshot = record.get("快照") or {}
        summary = snapshot.get("摘要") or {}
        return {
            "文件标识": record["文件标识"],
            "创建时间": record["创建时间"],
            "创建人": record.get("创建人", ""),
            "慢查询阈值毫秒": record["慢查询阈值毫秒"],
            "状态": record["状态"],
            "已完整交付": record["已完整交付"],
            "最近下载开始时间": record.get("最近下载开始时间"),
            "最近交付时间": record.get("最近交付时间"),
            "最近中断时间": record.get("最近中断时间"),
            "覆盖完成序上界": snapshot.get("覆盖完成序上界"),
            "覆盖执行标识上界": snapshot.get("覆盖执行标识上界"),
            "已完成执行数": snapshot.get("已完成执行数", 0),
            "样本数": summary.get("样本数", 0),
            "平均耗时毫秒": summary.get("平均耗时毫秒"),
        }

    def list_diagnostic_files(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public_diagnostic_file(record)
                    for record in self._diagnostic_files.values()]

    def get_diagnostic_file(self, file_id: str) -> dict[str, Any] | None:
        """读取诊断文件登记信息（不含明细）。"""
        with self._lock:
            record = self._diagnostic_files.get(file_id)
            if record is None:
                return None
            return self._public_diagnostic_file(record)

    def capture_diagnostic_snapshot(self, file_id: str,
                                    *, reading: bool = False) -> dict[str, Any] | None:
        """下载开始时冻结一次快照；只有生成中的文件会重新切片。

        reading=True 只读取已有快照（打开明细），不推进「下载开始」时间，
        也不把待下载文件改成生成中。
        """
        with self._lock:
            record = self._diagnostic_files.get(file_id)
            if record is None:
                return None
            # 已完整交付的文件保持自己的历史快照；重新生成（待下载/中断）才重新冻结。
            if record.get("快照") is None or record["状态"] in ("待下载", "中断"):
                completed = [
                    copy.deepcopy(item) for item in self._query_log
                    if item.get("completed") and item.get("完成序") is not None
                ]
                watermark = max((int(item["完成序"]) for item in completed), default=0)
                covered = [item for item in completed
                           if int(item["完成序"]) <= watermark]
                # 执行先登记后完成，完成顺序可能与执行标识顺序不一致。
                # 「覆盖执行标识上界」只取连续已完成的执行标识前缀，
                # 避免把仍在执行中的更低标识误报成已覆盖。
                completed_ids = {int(item["id"]) for item in covered}
                contiguous_watermark = 0
                while (contiguous_watermark + 1) in completed_ids:
                    contiguous_watermark += 1
                threshold = float(record["慢查询阈值毫秒"])
                samples = [item for item in covered
                           if float(item.get("duration_ms", 0.0)) > threshold]
                samples.sort(key=lambda item: (int(item["完成序"]), int(item["id"])))
                durations = [float(item.get("duration_ms", 0.0)) for item in samples]
                methods: dict[str, int] = {}
                calibers: dict[str, int] = {}
                for item in samples:
                    method = str(item.get("完成方式", ""))
                    methods[method] = methods.get(method, 0) + 1
                    caliber = str(item.get("取数口径", ""))
                    calibers[caliber] = calibers.get(caliber, 0) + 1
                summary = {
                    "样本数": len(samples),
                    "平均耗时毫秒": round(sum(durations) / len(durations), 2) if durations else 0.0,
                    "最大耗时毫秒": round(max(durations), 2) if durations else 0.0,
                    "最小耗时毫秒": round(min(durations), 2) if durations else 0.0,
                    "完成方式分布": methods,
                    "取数口径分布": calibers,
                    "慢查询阈值毫秒": threshold,
                }
                snapshot = {
                    "快照时刻": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "覆盖完成序上界": watermark,
                    "覆盖执行标识上界": contiguous_watermark,
                    "已完成执行数": len(covered),
                    "覆盖执行标识": [int(item["id"]) for item in covered],
                    "摘要": summary,
                    "明细": samples,
                }
                record["快照"] = snapshot
            if not reading:
                record["最近下载开始时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if record["状态"] in ("待下载", "中断"):
                    # 已完整交付的文件重下时保持「完整」，避免一次失败下载抹掉有效档
                    record["状态"] = "生成中"
            return copy.deepcopy(record["快照"])

    def mark_diagnostic_complete(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._diagnostic_files.get(file_id)
            if record is None:
                return None
            record["状态"] = "完整"
            record["已完整交付"] = True
            record["最近交付时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return self._public_diagnostic_file(record)

    def mark_diagnostic_interrupted(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._diagnostic_files.get(file_id)
            if record is None:
                return None
            record["最近中断时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not record["已完整交付"]:
                record["状态"] = "中断"
            return self._public_diagnostic_file(record)

    def is_diagnostic_replayable(self, file_id: str) -> bool:
        with self._lock:
            record = self._diagnostic_files.get(file_id)
            return bool(record and record["已完整交付"] and record["状态"] == "完整")

    def find_diagnostic_execution(self, file_id: str,
                                  execution_ref: str | int) -> dict[str, Any] | None:
        """按执行标识或来源执行ID在已冻结快照中定位一笔明细。

        旧档不会随运行日志或口径变化，定位始终落在文件的冻结切片里。
        """
        with self._lock:
            record = self._diagnostic_files.get(file_id)
            if record is None or not record["已完整交付"]:
                return None
            snapshot = record.get("快照") or {}
            ref_text = str(execution_ref)
            for item in snapshot.get("明细", []):
                matches = (
                    str(item.get("执行标识")) == ref_text
                    or str(item.get("id")) == ref_text
                    or str(item.get("执行ID")) == ref_text
                )
                if matches:
                    return copy.deepcopy(item)
            return None

    # ── Cache operations ──
    #
    # 缓存条目携带「形成该结果的实际口径」和来源执行标识；缓存命中时按形成结果
    # 的旧口径记账，而不是按命中时刻的当前口径改写历史。

    def cache_get(self, key: str) -> Any | None:
        """获取缓存值，过期返回 None。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.time() > expires_at:
            del self._cache[key]
            return None
        return payload.get("value") if isinstance(payload, dict) else payload

    def cache_get_entry(self, key: str) -> dict[str, Any] | None:
        """获取缓存值及其口径元数据。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.time() > expires_at:
            del self._cache[key]
            return None
        if isinstance(payload, dict):
            return copy.deepcopy(payload)
        return {"value": payload, "metadata": {}}

    def cache_set(self, key: str, value: Any, ttl: int,
                  metadata: dict[str, Any] | None = None) -> None:
        """设置缓存，ttl 单位为秒；metadata 记录形成结果的实际口径与来源执行。"""
        self._cache[key] = (
            time.time() + ttl,
            {"value": value, "metadata": copy.deepcopy(metadata or {})},
        )

    def cache_invalidate(self, key: str) -> bool:
        return self._cache.pop(key, None) is not None

    def cache_invalidate_by_prefix(self, prefix: str) -> int:
        count = 0
        for key in list(self._cache):
            if key.startswith(prefix):
                del self._cache[key]
                count += 1
        return count

    # ── Cohort operations ──

    def save_cohort_snapshot(self, cohort_id: int, members: list[dict[str, Any]]) -> None:
        self._cohort_snapshots[str(cohort_id)] = members

    def get_cohort_snapshot(self, cohort_id: int) -> list[dict[str, Any]] | None:
        return self._cohort_snapshots.get(str(cohort_id))

    # ── Anomaly detection operations ──

    def record_anomaly(self, metric_id: int, severity: str, description: str,
                       value: float, threshold: float) -> dict[str, Any]:
        record = {
            "id": len(self._anomaly_records) + 1,
            "指标ID": metric_id,
            "严重程度": severity,
            "描述": description,
            "当前值": value,
            "阈值": threshold,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._anomaly_records.append(record)
        return record

    def anomaly_records(self) -> list[dict[str, Any]]:
        return self._anomaly_records

    # ── Recalc operations ──

    def record_recalc(self, metric_id: int, old_version: str, new_version: str,
                      time_range: str, diff_summary: str) -> dict[str, Any]:
        record = {
            "id": len(self._recalc_history) + 1,
            "关联指标": f"metric_{metric_id}",
            "旧口径": old_version,
            "新口径": new_version,
            "历史时间范围": time_range,
            "产出差异": diff_summary,
            "重算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._recalc_history.append(record)
        return record

    def recalc_history(self) -> list[dict[str, Any]]:
        return self._recalc_history

    # ── Data artifacts for cross-module consumption ──

    def get_metric_data(self, metric_id: int) -> dict[str, Any] | None:
        """Get metric data with its calibers, queries, and services."""
        metric = self.find("metric_definition", metric_id)
        if metric is None:
            return None
        return {
            "metric": metric,
            "calibers": self.find_by_field("caliber_version", "所属指标",
                                           metric.get("指标名称", "")),
            "queries": self.find_by_field("query_builder", "关联指标",
                                          metric.get("指标名称", "")),
            "services": [row for row in self.rows("data_service")
                         if metric.get("指标名称", "") in str(row.get("关联指标或模型", ""))],
            "subscriptions": self.find_by_field("data_subscription", "关联指标",
                                                 metric.get("指标名称", "")),
        }

    def get_dimension_model_data(self, model_id: int) -> dict[str, Any] | None:
        """Get dimension model with its drill-down paths and pivot configs."""
        model = self.find("dimension_model", model_id)
        if model is None:
            return None
        return {
            "model": model,
            "drill_paths": self.find_by_field("drill_down", "起始指标",
                                              model.get("模型名称", "")),
            "pivot_configs": self.find_by_field("pivot_table", "数据源指标",
                                                 model.get("模型名称", "")),
        }

    def get_cohort_analysis(self, cohort_id: int) -> dict[str, Any] | None:
        """Get cohort with experiments and segment comparisons."""
        cohort = self.find("user_cohort", cohort_id)
        if cohort is None:
            return None
        cohort_name = cohort.get("分群名称", "")
        return {
            "cohort": cohort,
            "experiments": [row for row in self.rows("experiment_group")
                            if cohort_name in str(row.get("筛选条件", ""))],
            "comparisons": [row for row in self.rows("segment_compare")
                            if cohort_name in (str(row.get("分群A", "")) + str(row.get("分群B", "")))],
            "anomaly_rules": [row for row in self.rows("anomaly_detector")
                              if cohort_name in str(row.get("目标指标", ""))],
        }

    def get_field_lineage(self, field_name: str) -> dict[str, Any]:
        """Trace a field's complete lineage chain."""
        entries = self.find_by_field("field_lineage", "目标字段", field_name)
        if not entries:
            return {"field": field_name, "lineage": [], "depth": 0}

        lineage = [{
            "source_fields": entry.get("来源字段列表", ""),
            "transform": entry.get("转换逻辑", ""),
            "intermediate_table": entry.get("中间表", ""),
            "depth": entry.get("血缘深度", ""),
            "status": entry.get("status", ""),
        } for entry in entries]

        return {
            "field": field_name,
            "lineage": lineage,
            "depth": len(lineage),
        }


store = Store()