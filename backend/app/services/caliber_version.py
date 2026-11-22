"""口径版本领域服务：管理指标计算口径的版本发布、历史留档和回退。

口径版本是连接指标定义与查询构建器的桥：
  metric_definition -> caliber_version -> query_builder
口径变更会触发缓存失效和重算通知。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class CaliberVersionService(BaseModuleService):
    MODULE = "caliber_version"
    ENTITY = "口径版本"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["所属指标", "版本号", "口径描述", "变更类型", "生效时间"]
    REQUIRED_FIELDS = ["所属指标", "版本号", "口径描述"]
    STATUS_ORDER = ["待生效", "已生效", "已过期"]
    ACTION_RULES = {"启用版本": "已生效", "过期版本": "已过期"}
    NEGATIVE_ACTIONS = []

    def activate_version(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """启用指定口径版本，同时将同一指标的旧版本标为过期。

        同指标下只有一个已生效口径版本。
        启用后触发相关缓存失效（跨模块: caliber_version -> cache_layer）。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] == "已过期":
            return None, f"{self.ENTITY} {entry_id} 已过期，不能重新启用"

        metric_name = entry.get("所属指标", "")

        # 将同指标所有旧版本过期
        for row in store.rows(self.MODULE):
            if (row.get("所属指标") == metric_name
                    and row.get("id") != entry_id
                    and row.get("status") == "已生效"):
                row["status"] = "已过期"
                row["pending"] = False
                row["abnormal"] = False

        entry["status"] = "已生效"
        entry["pending"] = True
        entry["abnormal"] = False

        # 失效相关缓存（前缀带冒号，避免指标名互为前缀时误伤）
        cache_key_prefix = f"query:{metric_name}:"
        invalidated = store.cache_invalidate_by_prefix(cache_key_prefix)

        return entry, f"口径版本 {entry.get('版本号')} 已生效（旧版本已过期，缓存失效 {invalidated} 条）"

    def get_version_history(self, metric_name: str) -> list[dict[str, Any]]:
        """获取指标的完整版本变更历史。"""
        entries = store.find_by_field(self.MODULE, "所属指标", metric_name)
        return sorted(entries, key=lambda e: e.get("生效时间", ""))

    def compare_versions(self, version_id_a: int, version_id_b: int) -> dict[str, Any] | None:
        """比较两个口径版本差异。"""
        a = store.find(self.MODULE, version_id_a)
        b = store.find(self.MODULE, version_id_b)
        if a is None or b is None:
            return None
        return {
            "version_a": {"id": a["id"], "version": a.get("版本号"), "desc": a.get("口径描述"),
                          "change_type": a.get("变更类型"), "effective": a.get("生效时间")},
            "version_b": {"id": b["id"], "version": b.get("版本号"), "desc": b.get("口径描述"),
                          "change_type": b.get("变更类型"), "effective": b.get("生效时间")},
        }

    def get_current_version(self, metric_name: str) -> dict[str, Any] | None:
        """获取指标当前已生效的口径版本。"""
        entries = store.find_by_field(self.MODULE, "所属指标", metric_name)
        active = [e for e in entries if e.get("status") == "已生效"]
        return active[0] if active else None