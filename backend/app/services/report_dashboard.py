"""运行概览领域服务 — 聚合展示 RPA 平台运行统计，输出 domain_snapshot。

数据工程核心：数据标准化与字段映射转换 — 跨模块数据产物

增量改造要点：
- 运行指标不再每次全量重算，改由 ``MetricWatermarkEngine`` 持久化
  处理水位与已纳入版本，按时间窗增量累计执行日志与采集批次；
- 修订记录只触发受影响时间窗重算，指标流程升级支持 continue / rebuild，
  台账去重保证同一执行不会被重复累计。
"""
from __future__ import annotations

from typing import Any

from app.metrics import metric_engine
from app.services.base import BaseModuleService
from app.store import store


class ReportDashboardService(BaseModuleService):
    MODULE = "report_dashboard"
    ENTITY = "运行数据"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["统计日期", "总执行数", "成功数", "失败数", "平均耗时", "采集总量", "可用率"]
    REQUIRED_FIELDS = ["统计日期", "总执行数", "成功数"]
    STATUS_ORDER = ["正常", "下降", "异常"]
    ACTION_RULES = {"刷新概览": "正常"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 增量指标（委托指标水位引擎）
    # ------------------------------------------------------------------

    def refresh_overview(self, *, force: bool = False) -> dict[str, Any]:
        """增量刷新概览指标；并发时只接受首个成功结果，否则返回 36 号终态。"""
        return metric_engine.refresh(force=force)

    def metric_windows(self, start: str | None = None,
                       end: str | None = None) -> list[dict[str, Any]]:
        """按日期范围查询时间窗指标数据集。"""
        return metric_engine.query_windows(start=start, end=end)

    def watermark(self) -> dict[str, Any]:
        """查询指标水位、纳入版本与最近刷新耗时。"""
        return metric_engine.watermark_info()

    def register_revision(self, source: str, source_id: int,
                          reason: str = "手动登记修订") -> dict[str, Any] | None:
        """手动登记修订记录，只重算受影响时间窗。"""
        return metric_engine.register_revision(source, source_id, reason)

    def revisions(self, status: str | None = None,
                  start: str | None = None,
                  end: str | None = None) -> list[dict[str, Any]]:
        return metric_engine.list_revisions(status=status, start=start, end=end)

    def rebuild_windows(self, window_dates: list[str]) -> dict[str, Any]:
        """对受影响时间窗重建全部指标（升级 rebuild / 对账入口）。"""
        return metric_engine.rebuild_windows(window_dates, reason="概览手动重建窗口")

    def upgrade_metric_flow(self, strategy: str) -> dict[str, Any]:
        """指标流程升级：continue 沿用旧水位只处理新增，rebuild 重建历史窗口。"""
        return metric_engine.upgrade_metric_flow(strategy)

    def stamp_legacy_boundary(self, version: str | None = None) -> dict[str, Any]:
        """把当前聚合结果标记为既有版本边界，用于演示/核对升级前的历史水位。"""
        from app.metrics.engine import METRIC_VERSION_V1

        return metric_engine.stamp_legacy_watermark(version or METRIC_VERSION_V1)

    def upgrades(self) -> list[dict[str, Any]]:
        return metric_engine.list_upgrades()

    # ------------------------------------------------------------------
    # 跨模块平台聚合（保留原有能力，供 domain_snapshot 使用）
    # ------------------------------------------------------------------

    def aggregate_platform_stats(self) -> dict[str, Any]:
        """跨模块聚合平台运行统计。

        从多个模块的数据汇总生成运行概览指标：
        - 采集记录统计（collect_records）
        - 任务队列状态（task_queue）
        - 异常日志（anomaly_log）
        - 脏数据统计（dirty_vault）
        - 通道消息流量（channel）
        """
        collect = store.collect_records.snapshot()
        dirty = store.dirty_vault.snapshot()
        recovery = store.recovery.snapshot()

        # 任务队列状态
        task_rows = store.rows("task_queue")
        task_completed = sum(1 for r in task_rows if r.get("status") == "已完成")
        task_failed = sum(1 for r in task_rows if r.get("status") == "执行失败")

        # 执行日志
        exec_rows = store.rows("execution_log")
        exec_success = sum(1 for r in exec_rows if r.get("status") == "执行成功")

        # 通道流量
        total_msgs = store.channel.total_messages
        unconfirmed = store.channel.uncomfirmed_count()

        return {
            "collect_records": collect["total"],
            "validated_records": collect["validated"],
            "dirty_records": dirty["total"],
            "recovery_pending": recovery["pending"],
            "tasks_completed": task_completed,
            "tasks_failed": task_failed,
            "exec_success": exec_success,
            "channel_messages": total_msgs,
            "channel_unconfirmed": unconfirmed,
            "platform_health": "正常" if task_failed < task_completed / 2 else "下降",
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """「刷新概览」动作映射为增量刷新，不再全量重算。"""
        if action == "刷新概览":
            result = self.refresh_overview()
            if result.get("status") == "conflict":
                return None, result.get("conflict_terminal", "刷新冲突")
            return {"id": entry_id, "status": "正常", "action_result": result}, "运行概览已增量刷新"
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        agg = self.aggregate_platform_stats()
        watermark = metric_engine.watermark_info()
        return {
            **base,
            **agg,
            "metric_watermark": watermark["watermark"],
            "metric_version": watermark["metric_version"],
            "metric_window_count": watermark["window_count"],
            "included_counts": watermark["included_counts"],
            "last_refresh_ms": watermark["last_elapsed_ms"],
        }
