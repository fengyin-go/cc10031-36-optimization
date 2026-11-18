"""执行记录领域服务 — 记录和查询 RPA 执行日志，输出 domain_snapshot。

数据工程核心：数据校验与去重策略执行 — 跨模块数据产物
"""
from __future__ import annotations

from typing import Any

from app.metrics import metric_engine
from app.metrics.engine import EXEC_TERMINAL_STATES
from app.services.base import BaseModuleService
from app.store import store


class ExecutionLogService(BaseModuleService):
    MODULE = "execution_log"
    ENTITY = "执行日志"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["执行编号", "所属流程", "开始时间", "结束时间", "执行状态", "采集行数", "错误摘要"]
    REQUIRED_FIELDS = ["执行编号", "所属流程", "开始时间"]
    STATUS_ORDER = ["执行中", "执行成功", "执行失败", "已超时", "已取消"]
    ACTION_RULES = {"归档日志": "执行成功"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行状态流转；进入终态即登记修订，触发受影响时间窗重算。"""
        entry, message = super().run_action(entry_id, action)
        if entry is not None and entry.get("status") in EXEC_TERMINAL_STATES:
            metric_engine.register_revision(
                self.MODULE, entry_id, reason=f"动作「{action}」进入终态")
        return entry, message


    def query_execution_history(self,
                                 bot_name: str | None = None,
                                 days: int = 7) -> list[dict[str, Any]]:
        """查询执行历史记录。"""
        rows = store.rows(self.MODULE)
        if bot_name:
            rows = [
                r for r in rows if bot_name in r.get("所属流程", "")
            ]
        return [
            {
                "id": r.get("id"), "exec_no": r.get("执行编号"),
                "bot": r.get("所属流程"), "status": r.get("status"),
                "start": r.get("开始时间"), "end": r.get("结束时间"),
                "error": r.get("错误摘要"),
            }
            for r in rows
        ]

    def get_execution_stats(self) -> dict[str, Any]:
        """获取执行统计概览。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        success = sum(1 for r in rows if r.get("status") == "执行成功")
        failed = sum(1 for r in rows if r.get("status") == "执行失败")
        return {
            "total_executions": total,
            "success_rate": round(success / max(total, 1), 2),
            "success": success,
            "failed": failed,
            "in_progress": sum(1 for r in rows if r.get("status") == "执行中"),
            "timeout": sum(1 for r in rows if r.get("status") == "已超时"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        stats = self.get_execution_stats()
        return {**base, **stats}