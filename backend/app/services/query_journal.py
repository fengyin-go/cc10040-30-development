"""查询日志领域服务：记录和审计所有分析查询的调用链路和性能。

跨模块数据流: query_builder/data_service -> query_journal（记录查询）
              cache_layer -> query_journal（区分缓存与实时查询）

慢查询诊断文件：
- 打包慢查询明细（执行标识、模板标识、真实指标标识、取数口径、完成方式、耗时）
  和按同一慢查阈值从快照中算出的摘要统计，支持下载。
- 摘要与明细来自下载开始时冻结的同一份快照，只取已完成执行；下载期间的新写入
  不会影响本次文件。
- 只有完整交付的文件可复跑；生成中断留下的是半成品，仅供识别、不可作为依据。
"""
from __future__ import annotations

import io
import json
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class QueryJournalService(BaseModuleService):
    MODULE = "query_journal"
    ENTITY = "查询日志"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["查询时间", "操作人", "涉及指标", "查询语句摘要", "返回行数"]
    REQUIRED_FIELDS = ["查询时间", "操作人", "涉及指标"]
    STATUS_ORDER = ["正常", "慢查询", "异常终止"]
    ACTION_RULES = {"标记慢查询": "慢查询", "忽略告警": "正常"}
    NEGATIVE_ACTIONS = ["忽略告警"]

    # 运行日志里的执行以「偏移量+执行ID」作为查询日志模块行 ID，避免与登记行冲突。
    EXECUTION_ROW_OFFSET = 1_000_000

    # ── 模块列表：示例日志 + 运行日志 ──

    def _execution_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for log in store.query_logs():
            rows.append({
                "id": self.EXECUTION_ROW_OFFSET + int(log.get("id", 0)),
                "查询时间": log.get("查询时间", ""),
                "操作人": log.get("操作人", ""),
                "涉及指标": log.get("涉及指标", ""),
                "查询语句摘要": log.get("查询语句摘要", ""),
                "返回行数": log.get("返回行数", ""),
                "status": log.get("status", "正常"),
                "pending": bool(log.get("pending")),
                "abnormal": bool(log.get("abnormal")),
                "执行标识": log.get("执行标识"),
                "指标标识": log.get("指标标识"),
                "模板标识": log.get("模板标识"),
                "取数口径": log.get("取数口径"),
                "完成方式": log.get("完成方式"),
                "耗时(毫秒)": log.get("耗时(毫秒)"),
                "完成状态": log.get("状态"),
                "completed": bool(log.get("completed")),
                "复跑关联执行标识": log.get("复跑关联执行标识"),
                "复跑关联执行键": log.get("复跑关联执行键"),
            })
        return rows

    def list_entries(self, *, filters=None, keyword=None, status=None,
                     page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE)) + self._execution_rows()
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [row for row in rows
                        if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows
                    if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        if entry_id >= self.EXECUTION_ROW_OFFSET:
            execution_id = entry_id - self.EXECUTION_ROW_OFFSET
            for row in self._execution_rows():
                if int(row["id"]) == entry_id:
                    return row
            return store.find_execution(execution_id)
        return store.find(self.MODULE, entry_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if entry_id >= self.EXECUTION_ROW_OFFSET:
            execution_id = entry_id - self.EXECUTION_ROW_OFFSET
            target = self.ACTION_RULES.get(action)
            if target is None:
                return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
            entry = store.find_execution(execution_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            # 仅调整告警状态，不改写已经记录的真实耗时和完成方式
            store.update_execution_fields(execution_id, {
                "status": target,
                "pending": target != "正常",
                "abnormal": target in ("慢查询", "异常终止"),
            })
            updated = store.find_execution(execution_id)
            return updated, f"{self.ENTITY}已{action}"
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        logs = store.query_logs()
        snapshot["executions"] = len(logs)
        snapshot["completed_executions"] = sum(1 for log in logs if log.get("completed"))
        return snapshot

    # ── 查询日志聚合 ──

    def _completed_logs(self) -> list[dict[str, Any]]:
        """只统计已完成的执行，执行中的记录还没有真实耗时。"""
        return [log for log in store.query_logs() if log.get("completed")]

    def get_slow_queries(self, threshold_ms: float | None = None) -> list[dict[str, Any]]:
        """获取慢查询列表（默认使用与诊断文件一致的慢查阈值）。"""
        limit = store.slow_threshold_ms if threshold_ms is None else float(threshold_ms)
        return [log for log in self._completed_logs()
                if float(log.get("duration_ms", 0.0)) > limit]

    def get_query_stats_by_metric(self) -> dict[str, Any]:
        """按指标统计查询情况。

        跨模块数据流: query_journal -> metric_definition（关联指标分析）
        """
        logs = self._completed_logs()
        by_metric: dict[str, dict[str, Any]] = {}
        for log in logs:
            metric_key = log.get("指标标识") or log.get("涉及指标", "未知")
            if metric_key not in by_metric:
                by_metric[metric_key] = {"count": 0, "slow": 0, "avg_duration_ms": 0.0,
                                          "total_duration_ms": 0.0}
            stats = by_metric[metric_key]
            stats["count"] += 1
            stats["total_duration_ms"] += log.get("duration_ms", 0)
            if log.get("status") == "慢查询":
                stats["slow"] += 1

        for metric_key, stats in by_metric.items():
            stats["avg_duration_ms"] = round(stats["total_duration_ms"] / max(1, stats["count"]), 2)
            del stats["total_duration_ms"]

        return by_metric

    def get_performance_insights(self) -> dict[str, Any]:
        """获取性能洞察：跨模块查询效率分析。"""
        logs = self._completed_logs()
        if not logs:
            return {"message": "暂无查询日志"}

        total = len(logs)
        slow = sum(1 for log in logs if log.get("status") == "慢查询")
        cached = sum(1 for log in logs if log.get("完成方式") == "缓存命中")
        errors = sum(1 for log in logs if log.get("status") == "异常终止")

        return {
            "total_queries": total,
            "slow_query_ratio": round(slow / max(1, total), 2),
            "cache_hit_ratio": round(cached / max(1, total), 2),
            "error_count": errors,
            "recommendations": [
                f"{slow} 条慢查询建议优化口径或增加缓存" if slow > 0 else None,
                f"{cached} 条缓存命中，减少重复计算" if cached > 0 else "暂无缓存命中" if total > 0 else None,
            ]
        }

    # ── 诊断文件 ──

    def create_diagnostic_file(self, threshold_ms: float | None = None,
                               operator: str = "") -> dict[str, Any]:
        """登记一个待下载的诊断文件；快照在下载开始时才冻结。"""
        return store.create_diagnostic_file(threshold_ms=threshold_ms, operator=operator)

    def list_diagnostic_files(self) -> list[dict[str, Any]]:
        return store.list_diagnostic_files()

    def get_diagnostic_file(self, file_id: str) -> dict[str, Any] | None:
        """读取诊断文件：未完整交付只返回登记信息，完整交付返回与下载一致的结构。"""
        meta = store.get_diagnostic_file(file_id)
        if meta is None:
            return None
        if not meta.get("已完整交付"):
            return meta
        # 完整文件的快照已冻结，这里只做字段整形，不会重新切片。
        payload = self.build_diagnostic_payload(file_id, reading=True)
        if payload is None:
            return meta
        payload.update({
            "状态": meta.get("状态"),
            "已完整交付": True,
            "文件状态": meta.get("状态"),
            "创建时间": meta.get("创建时间"),
            "最近交付时间": meta.get("最近交付时间"),
        })
        return payload

    def _execution_material(self, execution: dict[str, Any]) -> dict[str, Any]:
        """文件明细里保留可交接、可核对的字段，不塞整份结果集。"""
        return {
            "执行ID": execution.get("id"),
            "执行标识": execution.get("执行标识"),
            "执行序号": execution.get("执行序号"),
            "完成序": execution.get("完成序"),
            "查询时间": execution.get("查询时间"),
            "操作人": execution.get("操作人"),
            "指标标识": execution.get("指标标识"),
            "指标ID": execution.get("指标ID"),
            "指标名称": execution.get("指标名称"),
            "模板标识": execution.get("模板标识"),
            "模板ID": execution.get("模板ID"),
            "模板名称": execution.get("模板名称"),
            "取数口径": execution.get("取数口径"),
            "取数口径版本": execution.get("取数口径版本"),
            "取数口径描述": execution.get("取数口径描述"),
            "完成方式": execution.get("完成方式"),
            "duration_ms": execution.get("duration_ms"),
            "耗时(毫秒)": execution.get("耗时(毫秒)"),
            "返回行数": execution.get("返回行数"),
            "status": execution.get("status"),
            "慢查询阈值(毫秒)": execution.get("慢查询阈值(毫秒)"),
            "缓存来源执行标识": execution.get("缓存来源执行标识"),
            "复跑关联执行标识": execution.get("复跑关联执行标识"),
            "复跑关联执行键": execution.get("复跑关联执行键"),
            "查询语句摘要": execution.get("查询语句摘要"),
            "template_snapshot": execution.get("template_snapshot", {}),
        }

    def build_diagnostic_payload(self, file_id: str,
                                 *, reading: bool = False) -> dict[str, Any] | None:
        """下载开始时冻结快照并打包文件内容（明细 + 摘要统计）。

        打开明细走 reading=True：只读已冻结快照，不会刷新下载时间或触发重新生成。
        """
        if store.get_diagnostic_file(file_id) is None:
            return None
        snapshot = store.capture_diagnostic_snapshot(file_id, reading=reading)
        if snapshot is None:
            return None
        threshold = float(snapshot["摘要"].get("慢查询阈值毫秒", store.slow_threshold_ms))
        details = [self._execution_material(item) for item in snapshot.get("明细", [])]
        durations = [float(item.get("duration_ms", 0.0)) for item in details]
        summary = {
            "慢查询阈值毫秒": threshold,
            "样本数": len(details),
            "平均耗时毫秒": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "最大耗时毫秒": round(max(durations), 2) if durations else 0.0,
            "最小耗时毫秒": round(min(durations), 2) if durations else 0.0,
            "耗时合计毫秒": round(sum(durations), 2),
            "完成方式分布": snapshot["摘要"].get("完成方式分布", {}),
            "取数口径分布": snapshot["摘要"].get("取数口径分布", {}),
        }
        file_meta = store.get_diagnostic_file(file_id) or {}
        return {
            "文件标识": file_id,
            "生成时间": snapshot.get("快照时刻"),
            "慢查询阈值毫秒": threshold,
            "状态": file_meta.get("状态"),
            "已完整交付": bool(file_meta.get("已完整交付")),
            "覆盖完成序上界": snapshot.get("覆盖完成序上界"),
            "覆盖执行标识上界": snapshot.get("覆盖执行标识上界"),
            "已完成执行数": snapshot.get("已完成执行数"),
            "覆盖执行标识": snapshot.get("覆盖执行标识", []),
            "说明": (
                "明细与摘要均取自下载开始时已完成的执行；"
                "覆盖完成序上界表示本文件覆盖到哪一次已完成执行，"
                "覆盖执行标识上界为连续已完成的执行标识前缀。"
            ),
            "摘要": summary,
            "明细": details,
            "诊断文件状态": file_meta.get("状态"),
        }

    def render_diagnostic_json(self, file_id: str) -> str | None:
        payload = self.build_diagnostic_payload(file_id)
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def render_diagnostic_csv(self, file_id: str) -> str | None:
        """CSV 版明细：便于在表格里重算数量和平均耗时。"""
        payload = self.build_diagnostic_payload(file_id)
        if payload is None:
            return None
        columns = [
            "执行ID", "执行标识", "完成序", "查询时间", "操作人", "指标标识", "指标ID", "指标名称",
            "模板标识", "模板ID", "模板名称", "取数口径", "取数口径版本", "取数口径描述",
            "完成方式", "耗时(毫秒)", "返回行数", "status", "慢查询阈值(毫秒)",
            "缓存来源执行标识", "复跑关联执行标识", "复跑关联执行键",
        ]
        output = io.StringIO()
        output.write("﻿")
        output.write(",".join(columns) + "\n")
        for item in payload["明细"]:
            values = []
            for column in columns:
                value = item.get(column, "")
                text = "" if value is None else str(value)
                if any(ch in text for ch in [",", '"', "\n"]):
                    text = '"' + text.replace('"', '""') + '"'
                values.append(text)
            output.write(",".join(values) + "\n")
        return output.getvalue()

    def mark_diagnostic_complete(self, file_id: str) -> dict[str, Any] | None:
        return store.mark_diagnostic_complete(file_id)

    def mark_diagnostic_interrupted(self, file_id: str) -> dict[str, Any] | None:
        return store.mark_diagnostic_interrupted(file_id)

    # ── 新旧执行核对 ──

    def compare_executions(self, old_execution_id: int,
                           new_execution_id: int) -> dict[str, Any]:
        """并列对照新旧两笔执行的口径、取数方式和耗时。

        旧档执行从已交付诊断文件的冻结快照读取，因此旧文件保持历史口径和耗时；
        新执行从运行日志读取。
        """
        old = store.find_execution(old_execution_id)
        new = store.find_execution(new_execution_id)
        if old is None or new is None:
            return {
                "error": f"执行不存在：old={old_execution_id}, new={new_execution_id}",
            }

        def pick(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "执行标识": item.get("执行标识"),
                "模板标识": item.get("模板标识"),
                "指标标识": item.get("指标标识"),
                "取数口径": item.get("取数口径"),
                "取数口径描述": item.get("取数口径描述"),
                "完成方式": item.get("完成方式"),
                "耗时(毫秒)": item.get("耗时(毫秒)"),
                "返回行数": item.get("返回行数"),
                "查询时间": item.get("查询时间"),
            }

        old_view, new_view = pick(old), pick(new)
        caliber_changed = old_view["取数口径"] != new_view["取数口径"]
        method_changed = old_view["完成方式"] != new_view["完成方式"]
        return {
            "旧执行": old_view,
            "新执行": new_view,
            "口径变化": caliber_changed,
            "取数方式变化": method_changed,
            "耗时差(毫秒)": round(
                float(new_view["耗时(毫秒)"] or 0) - float(old_view["耗时(毫秒)"] or 0), 3),
            "复跑关联执行标识": new.get("复跑关联执行标识"),
            "复跑关联执行键": new.get("复跑关联执行键"),
            "关联诊断文件": new.get("关联诊断文件"),
        }
