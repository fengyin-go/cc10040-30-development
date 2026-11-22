"""异常检测器领域服务：配置和执行异常检测规则，发现数据异常。

跨模块数据流: segment_compare -> anomaly_detector
用户分群的对比分析结果触发异常检测。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class AnomalyDetectorService(BaseModuleService):
    MODULE = "anomaly_detector"
    ENTITY = "检测规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "目标指标", "检测算法", "异常阈值", "生效时段"]
    REQUIRED_FIELDS = ["规则名称", "目标指标", "检测算法"]
    STATUS_ORDER = ["待启用", "已启用", "已关闭"]
    ACTION_RULES = {"启用检测": "已启用", "关闭检测": "已关闭"}
    NEGATIVE_ACTIONS = []

    def run_detection(self, entry_id: int) -> dict[str, Any] | None:
        """执行异常检测。

        跨模块数据流: anomaly_detector -> metric_definition（检测目标指标）
        """
        rule = store.find(self.MODULE, entry_id)
        if rule is None:
            return {"error": f"检测规则 {entry_id} 不存在"}
        if rule["status"] != "已启用":
            return {"error": f"检测规则 {entry_id} 未启用"}

        metric_name = rule.get("目标指标", "")
        try:
            threshold = float(str(rule.get("异常阈值", "10")).replace(">", "").replace("<", ""))
        except (ValueError, TypeError):
            threshold = 10.0
        algorithm = rule.get("检测算法", "threshold")

        # 查找关联指标
        metrics = store.find_by_field("metric_definition", "指标名称", metric_name)
        if not metrics:
            return {"error": f"未找到指标 {metric_name}"}

        # 模拟检测
        import random
        random.seed(entry_id)
        metric_id = int(metrics[0].get("id", 0))
        sample_value = random.uniform(threshold * 0.5, threshold * 1.5)

        is_anomaly = sample_value > threshold

        if is_anomaly:
            record = store.record_anomaly(
                metric_id=metric_id,
                severity="critical" if sample_value > threshold * 1.3 else "warning",
                description=f"指标 {metric_name} 检测到异常：{sample_value:.2f} > {threshold}",
                value=sample_value,
                threshold=threshold,
            )

        # 更新查询日志中相关的异常记录
        store.log_query(
            metric_id=metric_id,
            operator="异常检测器",
            sql_summary=f"异常检测: {rule.get('规则名称')}, 算法={algorithm}",
            row_count=1,
            duration_ms=10.0,
            status="正常" if not is_anomaly else "异常终止",
            completion_method="检测任务",
        )

        return {
            "rule_name": rule.get("规则名称"),
            "metric": metric_name,
            "algorithm": algorithm,
            "threshold": threshold,
            "sample_value": round(sample_value, 2),
            "is_anomaly": is_anomaly,
            "severity": "critical" if is_anomaly and sample_value > threshold * 1.3
                        else "warning" if is_anomaly else "normal",
            "detection_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_anomaly_report(self, metric_name: str | None = None) -> list[dict[str, Any]]:
        """获取异常报告。

        跨模块数据流: anomaly_detector -> query_journal（异常记录关联）
        """
        records = store.anomaly_records()
        if metric_name:
            records = [r for r in records if metric_name in str(r.get("描述", ""))]

        # 关联查询日志
        for record in records:
            metric_id = record.get("指标ID")
            related_logs = [log for log in store.query_logs()
                            if f"metric_{metric_id}" in str(log.get("涉及指标", ""))
                            and "异常" in str(log.get("查询语句摘要", ""))]
            record["related_queries"] = len(related_logs)

        return records

    def get_detection_coverage(self) -> dict[str, Any]:
        """获取异常检测覆盖率统计。"""
        rules = store.rows(self.MODULE)
        enabled = [r for r in rules if r.get("status") == "已启用"]
        anomalies = store.anomaly_records()

        return {
            "total_rules": len(rules),
            "enabled_rules": len(enabled),
            "disabled_rules": len(rules) - len(enabled),
            "total_anomalies_detected": len(anomalies),
            "coverage_ratio": round(len(enabled) / max(1, len(rules)), 2) if rules else 0,
        }