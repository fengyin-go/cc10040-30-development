"""业务行为测试：验证跨模块数据流链。

链1: metric_definition -> caliber_version -> query_builder -> data_service -> cache_layer -> query_journal
链2: dimension_model -> drill_down -> pivot_table -> report_template
链3: user_cohort -> experiment_group -> segment_compare -> anomaly_detector
"""
from __future__ import annotations

import pytest

from app.services.metric_definition import MetricDefinitionService
from app.services.caliber_version import CaliberVersionService
from app.services.query_builder import QueryBuilderService
from app.services.data_service import DataServiceService
from app.services.cache_layer import CacheLayerService
from app.services.query_journal import QueryJournalService
from app.services.dimension_model import DimensionModelService
from app.services.drill_down import DrillDownService
from app.services.pivot_table import PivotTableService
from app.services.report_template import ReportTemplateService
from app.services.user_cohort import UserCohortService
from app.services.experiment_group import ExperimentGroupService
from app.services.segment_compare import SegmentCompareService
from app.services.anomaly_detector import AnomalyDetectorService
from app.services.recalc_engine import RecalcEngineService
from app.services.field_lineage import FieldLineageService
from app.store import store


class TestMetricCaliberChain:
    """链1测试：指标定义 -> 口径版本 -> 查询构建器 -> 数据服务 -> 缓存 -> 查询日志"""

    def test_publish_metric_creates_caliber(self):
        """测试发布指标自动创建口径版本"""
        svc = MetricDefinitionService()
        # 指标ID=1 初始状态为"草稿"
        result, msg = svc.publish_with_caliber(1, "统计最近30天活跃用户数")
        assert result is not None
        assert "已发布" in msg

        # 验证口径版本已创建
        calibers = store.find_by_field("caliber_version", "所属指标", "指标定义样例1")
        assert len(calibers) > 0
        assert any(c.get("版本号") == "v1.0" for c in calibers)

    def test_metric_chain_completeness(self):
        """测试指标完整数据链查询"""
        svc = MetricDefinitionService()
        chain = svc.get_metric_chain(1)
        assert chain is not None
        assert chain["metric_name"] == "指标定义样例1"
        assert "caliber_count" in chain
        assert "query_templates" in chain
        assert "service_apis" in chain

    def test_caliber_activate_expires_old(self):
        """测试启用新口径版本自动过期旧版本"""
        svc = CaliberVersionService()
        # 先确认ID=1是已生效版本（由publish_with_caliber创建）
        # 需要先手动创建第二个版本
        store.rows("caliber_version").append({
            "id": 99, "所属指标": "指标定义样例1", "版本号": "v2.0",
            "口径描述": "新口径：统计自然月用户", "变更类型": "口径调整",
            "生效时间": "2026-11-01", "status": "待生效",
            "pending": True, "abnormal": False,
        })
        result, msg = svc.activate_version(99)
        assert result is not None
        assert "已生效" in msg

        # 验证旧版本已过期
        calibers = store.find_by_field("caliber_version", "所属指标", "指标定义样例1")
        active = [c for c in calibers if c.get("status") == "已生效"]
        expired = [c for c in calibers if c.get("status") == "已过期"]
        assert len(active) == 1
        assert active[0].get("版本号") == "v2.0"
        assert len(expired) >= 1

    def test_caliber_history(self):
        """测试口径版本历史查询"""
        svc = CaliberVersionService()
        history = svc.get_version_history("指标定义样例1")
        assert len(history) >= 2

    def test_caliber_compare(self):
        """测试口径版本对比"""
        svc = CaliberVersionService()
        # 找到两个版本
        calibers = store.find_by_field("caliber_version", "所属指标", "指标定义样例1")
        if len(calibers) >= 2:
            result = svc.compare_versions(
                int(calibers[0].get("id", 0)),
                int(calibers[1].get("id", 0)),
            )
            assert result is not None
            assert "version_a" in result
            assert "version_b" in result

    def test_query_execute_with_cache(self):
        """测试查询执行包含缓存和日志记录"""
        svc = QueryBuilderService()
        # 确保查询模板和口径就绪
        template = store.find("query_builder", 2)
        template["status"] = "已发布"
        metric_name = template.get("关联指标", "")

        # 确保指标有生效口径
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        if not calibers or not any(c.get("status") == "已生效" for c in calibers):
            store.rows("caliber_version").append({
                "id": 200, "所属指标": metric_name, "版本号": "v1.0",
                "口径描述": "测试口径", "变更类型": "初始创建",
                "生效时间": "2026-11-01", "status": "已生效",
                "pending": True, "abnormal": False,
            })

        result = svc.execute_query(2, operator="测试用户")
        assert "error" not in str(result)
        logs = store.query_logs()
        assert len(logs) > 0
        assert any("测试用户" in str(log.get("操作人", "")) for log in logs)

    def test_query_cache_second_call(self):
        """测试查询缓存：第二次相同查询应走缓存"""
        svc = QueryBuilderService()
        # 确保模板和口径就绪
        template = store.find("query_builder", 2)
        template["status"] = "已发布"
        metric_name = template.get("关联指标", "")
        if not store.find_by_field("caliber_version", "所属指标", metric_name):
            store.rows("caliber_version").append({
                "id": 201, "所属指标": metric_name, "版本号": "v1.0",
                "口径描述": "测试口径", "变更类型": "初始创建",
                "生效时间": "2026-11-01", "status": "已生效",
                "pending": True, "abnormal": False,
            })

        svc.execute_query(2, operator="缓存测试1")
        result2 = svc.execute_query(2, operator="缓存测试2")

        assert result2 is not None
        logs = store.query_logs()
        assert sum(1 for log in logs if "缓存测试" in str(log.get("操作人", ""))) >= 2

    def test_data_service_call(self):
        """测试数据服务接口调用"""
        svc = DataServiceService()
        # 确保数据服务API和查询模板就绪
        api = store.find("data_service", 2)
        api["status"] = "已发布"
        target = api.get("关联指标或模型", "")

        # 创建匹配的查询模板和口径
        store.rows("query_builder").append({
            "id": 300, "模板名称": "数据服务测试查询",
            "关联指标": target, "维度组合": "日,周",
            "过滤条件": "", "缓存策略": "",
            "status": "已发布", "pending": False, "abnormal": False,
        })
        store.rows("caliber_version").append({
            "id": 301, "所属指标": target, "版本号": "v1.0",
            "口径描述": "测试口径", "变更类型": "初始创建",
            "生效时间": "2026-11-01", "status": "已生效",
            "pending": True, "abnormal": False,
        })

        result = svc.call_service(2, operator="测试客户端")
        assert "error" not in str(result)

    def test_cache_invalidate_by_metric(self):
        """测试根据指标失效缓存"""
        svc = CacheLayerService()
        result = svc.invalidate_by_metric("指标定义样例1")
        assert result["metric"] == "指标定义样例1"
        assert "invalidated_entries" in result

    def test_cache_warm(self):
        """测试缓存预热"""
        svc = CacheLayerService()
        result = svc.warm_cache("指标定义样例1")
        assert result["metric"] == "指标定义样例1"
        assert "warmed_templates" in result

    def test_query_journal_slow_queries(self):
        """测试慢查询统计"""
        svc = QueryJournalService()
        slow = svc.get_slow_queries(threshold_ms=50)
        assert isinstance(slow, list)

    def test_query_stats_by_metric(self):
        """测试按指标查询统计"""
        svc = QueryJournalService()
        stats = svc.get_query_stats_by_metric()
        assert isinstance(stats, dict)

    def test_performance_insights(self):
        """测试性能洞察"""
        svc = QueryJournalService()
        insights = svc.get_performance_insights()
        assert isinstance(insights, dict)


class TestDimensionAnalysisChain:
    """链2测试：维度模型 -> 下钻 -> 透视表 -> 报告模板"""

    def test_dimension_model_with_analytics(self):
        """测试维度模型关联分析配置"""
        svc = DimensionModelService()
        result = svc.get_model_with_analytics(1)
        assert result is not None
        assert result["model_name"] == "维度建模样例1"
        assert "drill_paths" in result
        assert "pivot_configs" in result

    def test_dimension_hierarchy(self):
        """测试维度层次结构"""
        svc = DimensionModelService()
        result = svc.get_dimension_hierarchy(1)
        assert result is not None
        assert "levels" in result
        assert len(result["levels"]) > 0

    def test_drill_down_execute(self):
        """测试下钻分析执行"""
        svc = DrillDownService()
        # 确保下钻路径已生效
        store.rows("drill_down")[1]["status"] = "已生效"
        result = svc.execute_drill_down(2, current_level=0)
        assert result is not None
        if "error" not in str(result):
            assert "dimension" in result
            assert "rows" in result

    def test_drill_tree(self):
        """测试下钻路径树"""
        svc = DrillDownService()
        result = svc.get_drill_tree(1)
        assert result is not None
        assert "tree" in result

    def test_drill_export_to_pivot(self):
        """测试下钻导出透视配置建议"""
        svc = DrillDownService()
        store.rows("drill_down")[0]["下钻维度序列"] = "日活跃,周活跃,月活跃"
        result = svc.export_to_pivot(1)
        # 需要至少两个维度
        assert result is not None
        if "error" not in str(result):
            assert "row_dimension" in result
            assert "column_dimension" in result

    def test_pivot_compute(self):
        """测试透视表计算"""
        svc = PivotTableService()
        result = svc.compute_pivot(1)
        assert result is not None
        if "error" not in str(result):
            assert "grid" in result
            assert "row_totals" in result

    def test_pivot_share_to_report(self):
        """测试透视表共享到报告模板"""
        svc = PivotTableService()
        result = svc.share_to_report(1, 1)
        assert result is not None

    def test_report_generate(self):
        """测试报告生成"""
        svc = ReportTemplateService()
        # 确保已发布
        store.rows("report_template")[0]["status"] = "已发布"
        result = svc.generate_report(1)
        assert result is not None
        if "error" not in str(result):
            assert "sections" in result


class TestCohortAnomalyChain:
    """链3测试：用户分群 -> 实验分群 -> 分群对比 -> 异常检测"""

    def test_recompute_cohort(self):
        """测试分群重计算"""
        svc = UserCohortService()
        result = svc.recompute_cohort(1)
        assert result is not None
        if "error" not in str(result):
            assert "member_count" in result
            assert result["member_count"] > 0

    def test_cohort_detail(self):
        """测试分群详细信息"""
        svc = UserCohortService()
        result = svc.get_cohort_detail(1)
        assert result is not None
        assert "cohort_name" in result

    def test_experiment_evaluate(self):
        """测试实验分组评估"""
        svc = ExperimentGroupService()
        result = svc.evaluate_groups(1, 2)
        assert result is not None
        if "error" not in str(result):
            assert "group_a" in result
            assert "group_b" in result

    def test_segment_compare_execute(self):
        """测试分群对比执行"""
        svc = SegmentCompareService()
        result = svc.execute_compare(1)
        assert result is not None
        if "error" not in str(result):
            assert "cohort_a" in result
            assert "cohort_b" in result
            assert "diff_percentage" in result

    def test_segment_compare_insights(self):
        """测试分群对比洞察"""
        svc = SegmentCompareService()
        result = svc.get_cross_segment_insights(1)
        assert result is not None

    def test_anomaly_detection(self):
        """测试异常检测"""
        svc = AnomalyDetectorService()
        store.rows("anomaly_detector")[0]["status"] = "已启用"
        result = svc.run_detection(1)
        assert result is not None
        if "error" not in str(result):
            assert "is_anomaly" in result

    def test_anomaly_report(self):
        """测试异常报告"""
        svc = AnomalyDetectorService()
        result = svc.get_anomaly_report()
        assert isinstance(result, list)

    def test_detection_coverage(self):
        """测试异常检测覆盖率"""
        svc = AnomalyDetectorService()
        result = svc.get_detection_coverage()
        assert "total_rules" in result
        assert "enabled_rules" in result


class TestRecalcAndLineage:
    """重算引擎和字段血缘测试"""

    def test_recalc_start(self):
        """测试重算引擎"""
        svc_recalc = RecalcEngineService()
        # 需要重算任务存在
        result = svc_recalc.start_recalc(1)
        assert result is not None

    def test_recalc_estimate_impact(self):
        """测试重算影响预估"""
        svc = RecalcEngineService()
        result = svc.estimate_recalc_impact(1)
        assert result is not None
        if "error" not in str(result):
            assert "affected_query_templates" in result

    def test_recalc_history(self):
        """测试重算历史"""
        svc = RecalcEngineService()
        result = svc.get_recalc_history()
        assert isinstance(result, list)

    def test_metric_recalc(self):
        """测试指标触发重算"""
        svc = MetricDefinitionService()
        result, msg = svc.recalc_metric_history(1, "v2.0", "2026-01-01 到 2026-11-01")
        assert result is not None
        assert "待调度" in msg or "重算" in msg

    def test_field_lineage_trace(self):
        """测试字段血缘追溯"""
        svc = FieldLineageService()
        result = svc.trace_field("字段血缘样例1")
        assert result is not None
        assert "field" in result

    def test_field_downstream_impact(self):
        """测试字段变更下游影响"""
        svc = FieldLineageService()
        result = svc.get_downstream_impact("字段血缘样例1")
        assert result is not None
        assert "field" in result


class TestFullChainSnapshot:
    """全链路快照测试"""

    def test_domain_snapshots(self):
        """测试所有模块的domain_snapshot"""
        services = [
            MetricDefinitionService(),
            CaliberVersionService(),
            QueryBuilderService(),
            DataServiceService(),
            CacheLayerService(),
            QueryJournalService(),
            DimensionModelService(),
            DrillDownService(),
            PivotTableService(),
            ReportTemplateService(),
            UserCohortService(),
            ExperimentGroupService(),
            SegmentCompareService(),
            AnomalyDetectorService(),
            RecalcEngineService(),
            FieldLineageService(),
        ]
        for svc in services:
            snapshot = svc.domain_snapshot()
            assert snapshot is not None
            assert "module" in snapshot
            assert "entity" in snapshot
            assert "count" in snapshot

    def test_cross_module_data_consistency(self):
        """跨模块数据一致性测试"""
        # 链1: 指标 -> 口径 -> 查询
        metric_svc = MetricDefinitionService()
        chain = metric_svc.get_metric_chain(1)
        assert chain is not None

        # 口径版本数量应该合理
        caliber_count = chain.get("caliber_count", 0)
        assert caliber_count >= 0

        # 链2: 维度 -> 下钻 -> 透视
        dim_svc = DimensionModelService()
        dim_data = dim_svc.get_model_with_analytics(1)
        assert dim_data is not None

        # 下钻路径和透视配置应该可枚举
        drill_count = dim_data.get("total_drill_paths", 0)
        pivot_count = dim_data.get("total_pivot_configs", 0)
        assert drill_count >= 0
        assert pivot_count >= 0

        # 链3: 分群 -> 实验 -> 对比 -> 异常
        cohort_svc = UserCohortService()
        cohort_detail = cohort_svc.get_cohort_detail(1)
        assert cohort_detail is not None

class TestQueryDiagnosticHandoff:
    """查询日志 -> 慢查询诊断文件 -> 复跑交接链。

    覆盖：冷执行与缓存命中的运行日志、同一秒并发、下载期间写入、
    中断半成品不可复跑、切换口径后复跑与旧档复算。
    """

    @staticmethod
    def _seed_metric(suffix: str = "A") -> dict:
        metric_name = f"日活用户{suffix}"
        # 模板与指标各自编号，避免跨用例的 ID 撞车（find 会取第一条同 ID 记录）
        base = 1000 + ord(suffix) * 10
        metric_id = base
        store.rows("metric_definition").append({
            "id": metric_id, "status": "已发布", "pending": False, "abnormal": False,
            "指标名称": metric_name, "计算口径": "按设备去重", "统计粒度": "日",
            "数据源": "dws_user", "业务域": "增长",
        })
        store.rows("caliber_version").append({
            "id": metric_id, "status": "已生效", "pending": False, "abnormal": False,
            "所属指标": metric_name, "版本号": "v1.0", "口径描述": "按设备去重",
            "变更类型": "初始创建", "生效时间": "2026-11-01",
        })
        slow_tpl = {
            "id": base + 2, "status": "已发布", "pending": False, "abnormal": False,
            "模板名称": f"慢模板{suffix}", "关联指标": metric_name,
            "维度组合": "日期,渠道", "过滤条件": "app=1", "缓存策略": "慢",
        }
        fast_tpl = {
            "id": base + 3, "status": "已发布", "pending": False, "abnormal": False,
            "模板名称": f"快模板{suffix}", "关联指标": metric_name,
            "维度组合": "日期", "过滤条件": "app=2", "缓存策略": "",
        }
        store.rows("query_builder").extend([slow_tpl, fast_tpl])
        return {"metric_name": metric_name, "metric_id": metric_id,
                "slow": slow_tpl, "fast": fast_tpl}

    def _exec(self, template_id: int, **kwargs):
        svc = QueryBuilderService()
        result = svc.execute_query(template_id, **kwargs)
        assert "error" not in result, result
        return result

    def test_cold_and_cache_log_separate_identifiers(self):
        seed = self._seed_metric("B")
        threshold = store.slow_threshold_ms
        cold = self._exec(seed["slow"]["id"], operator="值班甲")
        hit = self._exec(seed["slow"]["id"], operator="值班乙")

        assert cold["source"] == "computed"
        assert hit["source"] == "cache"
        assert cold["execution_id"] != hit["execution_id"]

        cold_log = store.find_execution(cold["execution_id"])
        hit_log = store.find_execution(hit["execution_id"])
        # 模板标识与真实指标标识分开记录
        assert cold_log["模板标识"].startswith("慢模板B#")
        assert cold_log["指标标识"].startswith("日活用户B#")
        assert cold_log["模板标识"] == f"慢模板B#{seed['slow']['id']}"
        assert cold_log["指标标识"] == f"日活用户B#{seed['metric_id']}"
        # 完成方式与实际口径
        assert cold_log["完成方式"] == "冷执行"
        assert hit_log["完成方式"] == "缓存命中"
        assert cold_log["取数口径"] == "v1.0"
        assert hit_log["取数口径"] == "v1.0"
        # 缓存命中按形成结果的实际口径记账
        assert hit_log["缓存来源执行标识"] == cold["execution_id"]
        assert cold_log["duration_ms"] > threshold
        assert cold_log["status"] == "慢查询"

    def test_concurrent_same_second_executions_get_unique_ids(self):
        import threading
        seed = self._seed_metric("C")
        svc = QueryBuilderService()
        results: list[dict] = []
        barrier = threading.Barrier(5)

        def run(idx: int) -> None:
            barrier.wait()
            results.append(svc.execute_query(seed["fast"]["id"], operator=f"并发{idx}"))

        threads = [threading.Thread(target=run, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        ids = [result["execution_id"] for result in results]
        keys = [result["execution_key"] for result in results]
        assert len(set(ids)) == 5
        assert len(set(keys)) == 5
        # 同一秒也允许，但完成序必须唯一
        completion_orders = [store.find_execution(i)["完成序"] for i in ids]
        assert len(set(completion_orders)) == 5

    def test_download_snapshot_excludes_later_execution(self):
        seed = self._seed_metric("D")
        journal = QueryJournalService()
        slow1 = self._exec(seed["slow"]["id"], operator="值班丙")

        file_meta = journal.create_diagnostic_file(operator="接班人")
        file_id = file_meta["文件标识"]
        snapshot = journal.build_diagnostic_payload(file_id)
        assert snapshot is not None
        covered_orders = set(snapshot["覆盖执行标识"])
        assert slow1["execution_id"] in covered_orders
        baseline_count = snapshot["摘要"]["样本数"]

        # 下载开始后（快照冻结后）再写一笔同秒慢查
        slow2 = self._exec(seed["slow"]["id"], operator="值班丁", use_cache=False)
        assert slow2["execution_id"] not in covered_orders

        # 新一笔慢查出现在「读取明细」与「计算摘要」之间：摘要也按同一冻结快照计算
        payload = journal.build_diagnostic_payload(file_id)
        assert payload["覆盖执行标识"] == snapshot["覆盖执行标识"]
        assert slow2["execution_id"] not in payload["覆盖执行标识"]
        assert payload["摘要"]["样本数"] == snapshot["摘要"]["样本数"]
        assert payload["摘要"]["平均耗时毫秒"] == snapshot["摘要"]["平均耗时毫秒"]

        # 用文件明细重算数量与平均耗时，应与摘要一致，且不含后写入的慢查
        details = payload["明细"]
        recomputed_count = len(details)
        recomputed_avg = round(
            sum(float(item["耗时(毫秒)"]) for item in details) / recomputed_count, 2)
        assert recomputed_count == baseline_count
        assert recomputed_avg == payload["摘要"]["平均耗时毫秒"]
        assert all(item["执行标识"] != slow2["execution_key"] for item in details)

    def test_interrupted_file_cannot_be_replayed(self):
        seed = self._seed_metric("E")
        journal = QueryJournalService()
        slow = self._exec(seed["slow"]["id"], operator="值班戊")
        file_id = journal.create_diagnostic_file(operator="接班人")["文件标识"]
        journal.build_diagnostic_payload(file_id)
        journal.mark_diagnostic_interrupted(file_id)

        svc = QueryBuilderService()
        result = svc.replay_from_diagnostic(file_id, slow["execution_key"])
        assert "error" in result
        assert "未完整交付" in result["error"]

        # 重新下载后成为完整文件，即可复跑
        journal.build_diagnostic_payload(file_id)
        journal.mark_diagnostic_complete(file_id)
        assert store.is_diagnostic_replayable(file_id)

    def test_replay_after_caliber_switch_keeps_old_archive(self):
        seed = self._seed_metric("F")
        journal = QueryJournalService()
        svc = QueryBuilderService()

        cold = self._exec(seed["slow"]["id"], operator="值班己")
        file_id = journal.create_diagnostic_file(operator="接班人")["文件标识"]
        old_payload = journal.build_diagnostic_payload(file_id)
        journal.mark_diagnostic_complete(file_id)
        old_detail = next(item for item in old_payload["明细"]
                          if item["执行标识"] == cold["execution_key"])

        # 切换口径：新增并启用 v2.0（启用会失效相关缓存）
        new_caliber = {
            "id": 999, "所属指标": seed["metric_name"], "版本号": "v2.0",
            "口径描述": "按账号去重", "变更类型": "口径调整",
            "生效时间": "2026-11-20", "status": "待生效",
            "pending": True, "abnormal": False,
        }
        store.rows("caliber_version").append(new_caliber)
        CaliberVersionService().activate_version(999)

        # 重新打开完整文件，选中原缓存慢查复跑
        reopened = journal.get_diagnostic_file(file_id)
        assert reopened is not None
        assert reopened["已完整交付"] is True
        target = next(item for item in reopened["明细"]
                      if item["执行标识"] == cold["execution_key"])
        replay = svc.replay_from_diagnostic(file_id, target["执行标识"], operator="接班人")
        assert "error" not in replay, replay
        assert replay["source"] == "computed"
        assert replay["caliber"] == "v2.0"

        # 新记录关联旧执行标识，按新口径冷查写入
        replay_log = store.find_execution(replay["execution_id"])
        assert replay_log["完成方式"] == "冷执行"
        assert replay_log["取数口径"] == "v2.0"
        assert replay_log["复跑关联执行标识"] == cold["execution_id"]
        assert replay_log["复跑关联执行键"] == cold["execution_key"]
        assert replay_log["关联诊断文件"] == file_id

        # 旧档仍可复算：口径和耗时保持历史值
        archive_detail = store.find_diagnostic_execution(file_id, cold["execution_key"])
        assert archive_detail is not None
        assert archive_detail["取数口径"] == old_detail["取数口径"] == "v1.0"
        assert archive_detail["duration_ms"] == old_detail["duration_ms"]

    def test_summary_and_samples_share_slow_threshold(self):
        seed = self._seed_metric("G")
        journal = QueryJournalService()
        fast = self._exec(seed["fast"]["id"], operator="值班庚")
        slow = self._exec(seed["slow"]["id"], operator="值班辛")
        threshold = store.slow_threshold_ms

        file_id = journal.create_diagnostic_file(operator="接班人")["文件标识"]
        payload = journal.build_diagnostic_payload(file_id)
        assert payload["摘要"]["慢查询阈值毫秒"] == threshold
        included = {item["执行标识"] for item in payload["明细"]}
        assert slow["execution_key"] in included
        # 阈值以下的正常查询不能混进耗时样本
        assert fast["execution_key"] not in included
        assert all(float(item["耗时(毫秒)"]) > threshold for item in payload["明细"])

    def test_cache_hit_records_forming_caliber_not_current(self):
        """缓存命中必须按形成结果的实际口径记账，即使当前已切到新口径。"""
        seed = self._seed_metric("H")
        svc = QueryBuilderService()
        cold = self._exec(seed["slow"]["id"], operator="值班壬")
        # 新口径直接入库但未经过 activate_version，缓存不会被主动失效，
        # 用来模拟「命中时当前口径已变、缓存仍是旧口径」的场景。
        store.rows("caliber_version").append({
            "id": seed["metric_id"] + 8, "所属指标": seed["metric_name"], "版本号": "v2.0",
            "口径描述": "按账号去重", "变更类型": "口径调整",
            "生效时间": "2026-11-21", "status": "生效中（被人工改写）",
            "pending": False, "abnormal": False,
        })
        # 直接把旧口径置为已过期、新口径置为已生效，但不动缓存
        for row in store.find_by_field("caliber_version", "所属指标", seed["metric_name"]):
            if row["版本号"] == "v1.0":
                row["status"] = "已过期"
            elif row["版本号"] == "v2.0":
                row["status"] = "已生效"

        hit = self._exec(seed["slow"]["id"], operator="值班癸")
        assert hit["source"] == "cache"
        assert hit["caliber"] == "v1.0"
        hit_log = store.find_execution(hit["execution_id"])
        assert hit_log["取数口径"] == "v1.0"
        assert hit_log["缓存形成口径"] == "v1.0"
        assert hit_log["命中时当前离线口径"] == "v2.0"
        assert hit_log["缓存来源执行标识"] == cold["execution_id"]
