"""批次追踪领域服务 — 追踪采集批次状态，输出 domain_snapshot。

数据工程核心：数据入湖存储与分区管理 — 跨模块数据产物
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.metrics import metric_engine
from app.metrics.engine import BATCH_TERMINAL_STATES
from app.services.base import BaseModuleService
from app.store import store


class BatchTrackerService(BaseModuleService):
    MODULE = "batch_tracker"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次编号", "触发流程", "采集时间", "目标条数", "成功条数", "遗漏条数", "校验状态"]
    REQUIRED_FIELDS = ["批次编号", "触发流程", "采集时间"]
    STATUS_ORDER = ["收集中", "已收集", "校验中", "已完成", "有遗漏"]
    ACTION_RULES = {"发起校验": "已收集", "确认完成": "有遗漏"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """批次状态流转；进入终态即登记修订，触发受影响时间窗重算。"""
        entry, message = super().run_action(entry_id, action)
        if entry is not None and entry.get("status") in BATCH_TERMINAL_STATES:
            metric_engine.register_revision(
                self.MODULE, entry_id, reason=f"动作「{action}」进入终态")
        return entry, message

    def track_batch_progress(self, batch_ref: str) -> dict[str, Any]:
        """追踪指定批次的进度（跨模块聚合）。"""
        # 采集记录统计
        collect_records = store.collect_records.list_by_batch(batch_ref)
        # 脏数据统计
        dirty_records = store.dirty_vault.list_by_batch(batch_ref)
        # 校验事件
        validation_events = [
            e for e in store.channel.snapshot(topic="rpa.validation_complete")
            if e.get("payload", {}).get("batch_ref") == batch_ref
        ]
        return {
            "batch_ref": batch_ref,
            "collected": len(collect_records),
            "dirty": len(dirty_records),
            "validated_events": len(validation_events),
            "completion_rate": round(
                len(collect_records) / max(len(collect_records) + len(dirty_records), 1), 2),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        return {
            **base,
            "completed_batches": sum(1 for r in rows if r.get("status") == "已完成"),
            "missed_batches": sum(1 for r in rows if r.get("status") == "有遗漏"),
            "in_progress": sum(1 for r in rows if r.get("status") in ("收集中", "已收集", "校验中")),
        }