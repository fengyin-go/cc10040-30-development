"""查询缓存层领域服务：管理查询缓存的TTL、失效策略和缓存命中率。

跨模块数据流: data_service/query_builder -> cache_layer -> query_journal
当口径版本变更或数据更新时触发缓存失效。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class CacheLayerService(BaseModuleService):
    MODULE = "cache_layer"
    ENTITY = "缓存策略"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["策略名称", "关联查询模板", "缓存时长", "失效触发条件", "命中率"]
    REQUIRED_FIELDS = ["策略名称", "关联查询模板", "缓存时长"]
    STATUS_ORDER = ["待生效", "已生效", "已停用"]
    ACTION_RULES = {"启用策略": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def _init_metrics(self) -> dict[str, int]:
        return {"hits": 0, "misses": 0, "invalidations": 0}

    def apply_cache_strategy(self, query_template_id: int, ttl_seconds: int) -> str:
        """为查询模板应用缓存策略。

        跨模块数据流: cache_layer -> query_builder
        """
        template = store.find("query_builder", query_template_id)
        if template is None:
            return f"查询模板 {query_template_id} 不存在"

        cache_key_prefix = f"query:{template.get('关联指标','')}"
        # 设置 TTL - 持久化到策略记录中
        cache_key = f"strategy:{query_template_id}"
        store.cache_set(cache_key, {"template_id": query_template_id,
                                     "ttl": ttl_seconds}, ttl_seconds * 2)
        return f"缓存策略已应用到模板 #{query_template_id}，TTL={ttl_seconds}秒"

    def invalidate_by_metric(self, metric_name: str) -> dict[str, Any]:
        """根据指标名称失效所有关联缓存。

        跨模块数据流: caliber_version/query_builder -> cache_layer
        口径版本变更或指标数据更新时触发。
        """
        count = store.cache_invalidate_by_prefix(f"query:{metric_name}:")
        return {"metric": metric_name, "invalidated_entries": count}

    def invalidate_by_template(self, template_id: int) -> dict[str, Any]:
        """根据查询模板失效缓存。"""
        template = store.find("query_builder", template_id)
        if template is None:
            return {"error": f"查询模板 {template_id} 不存在"}
        count = store.cache_invalidate_by_prefix(
            f"query:{template.get('关联指标','')}:")
        return {"template_id": template_id, "invalidated_entries": count}

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计信息。"""
        cache_entry_count = len(store._cache)
        return {
            "cached_entries": cache_entry_count,
            "strategy_count": len(store.rows(self.MODULE)),
        }

    def warm_cache(self, metric_name: str) -> dict[str, Any]:
        """预热缓存：提前计算并缓存指标查询结果。

        跨模块数据流: cache_layer -> query_builder (pre-execute and cache)
        预热条目也要记录实际口径，缓存命中时才能按形成结果的口径记账。
        """
        templates = store.find_by_field("query_builder", "关联指标", metric_name)
        calibers = store.find_by_field("caliber_version", "所属指标", metric_name)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)
        caliber_version = str(active_caliber.get("版本号", "")) if active_caliber else ""
        warmed = 0
        for tmpl in templates:
            if tmpl.get("status") == "已发布":
                cache_key = (
                    f"query:{metric_name}:{tmpl.get('维度组合','')}:"
                    f"{tmpl.get('过滤条件','')}:tpl={tmpl.get('id', 0)}"
                )
                # 生成预热数据
                metric_rows = store.find_by_field("metric_definition", "指标名称", metric_name)
                pre_data = [{"序号": i+1, "指标": metric_name,
                             "口径版本": caliber_version,
                             "预热": True} for i in range(len(metric_rows))]
                cache_ttl = 300
                if "长期" in str(tmpl.get("缓存策略", "")):
                    cache_ttl = 3600
                store.cache_set(
                    cache_key, pre_data, cache_ttl,
                    metadata={
                        "caliber_version": caliber_version,
                        "caliber_desc": str(active_caliber.get("口径描述", "")) if active_caliber else "",
                        "source_execution_id": None,
                        "template_id": tmpl.get("id"),
                        "metric_name": metric_name,
                        "warmed": True,
                    },
                )
                warmed += 1
        return {"metric": metric_name, "warmed_templates": warmed}