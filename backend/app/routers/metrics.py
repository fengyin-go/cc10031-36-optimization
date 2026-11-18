"""运行概览指标接口：增量水位、时间窗数据集、修订与升级管理。

这些固定路径必须在通用资源路由 ``/api/{module}`` 之前注册，
否则会被动态模块路由抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.metrics.engine import CONFLICT_TERMINAL_CODE
from app.services import SERVICES

router = APIRouter(prefix="/api/report-dashboard", tags=["运行概览指标"])


def _dashboard_service():
    service = SERVICES.get("report_dashboard")
    if service is None:  # pragma: no cover - 服务注册表始终包含该模块
        raise HTTPException(status_code=404, detail="运行概览模块未注册")
    return service


class RevisionPayload(BaseModel):
    source: str = Field(..., description="execution_log 或 batch_tracker")
    source_id: int
    reason: str = "手动登记修订"


class RebuildPayload(BaseModel):
    window_dates: list[str]


class UpgradePayload(BaseModel):
    strategy: str = Field(..., description="continue 或 rebuild")


class LegacyBoundaryPayload(BaseModel):
    version: str | None = Field(default=None, description="旧指标流程版本，默认 metric-flow-v1")


@router.get("/watermark")
def get_watermark() -> dict[str, Any]:
    """查看指标水位、已纳入版本、最近刷新耗时与迁移状态。"""
    return _dashboard_service().watermark()


@router.get("/metrics")
def get_metrics(
    start: str | None = Query(default=None, description="起始日期 YYYY-MM-DD"),
    end: str | None = Query(default=None, description="结束日期 YYYY-MM-DD"),
) -> dict[str, Any]:
    """按日期范围查询时间窗运行指标数据集。"""
    service = _dashboard_service()
    windows = service.metric_windows(start=start, end=end)
    return {
        "items": windows,
        "total": len(windows),
        "start": start,
        "end": end,
        "watermark": service.watermark(),
    }


@router.get("/revisions")
def get_revisions(
    status: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询修订记录及其重算状态。"""
    items = _dashboard_service().revisions(status=status, start=start, end=end)
    return {"items": items, "total": len(items)}


@router.get("/upgrades")
def get_upgrades() -> dict[str, Any]:
    """查询指标流程升级审计记录。"""
    items = _dashboard_service().upgrades()
    return {"items": items, "total": len(items)}


@router.post("/refresh")
def post_refresh(force: bool = Query(default=False)) -> dict[str, Any]:
    """增量刷新运行指标；并发时只接受首个成功结果。

    冲突时 HTTP 层仍返回 200，业务终态携带 36 号冲突码，
    由前端按 conflict_terminal 提示，不改变已累计指标。
    """
    result = _dashboard_service().refresh_overview(force=force)
    if result.get("status") == "conflict":
        result["http_hint"] = f"HTTP 200 + 业务码 {CONFLICT_TERMINAL_CODE}"
    return result


@router.post("/revisions")
def post_revision(payload: RevisionPayload) -> dict[str, Any]:
    """登记修订记录，下次刷新只重算受影响时间窗。"""
    if payload.source not in ("execution_log", "batch_tracker"):
        raise HTTPException(status_code=400, detail="source 只能是 execution_log 或 batch_tracker")
    revision = _dashboard_service().register_revision(
        payload.source, payload.source_id, payload.reason)
    if revision is None:
        raise HTTPException(
            status_code=409,
            detail="仅终态记录可登记修订，或原始记录不存在/缺少可识别的时间窗",
        )
    return {"ok": True, "revision": revision}


@router.post("/rebuild")
def post_rebuild(payload: RebuildPayload) -> dict[str, Any]:
    """对指定受影响时间窗重建全部指标。"""
    if not payload.window_dates:
        raise HTTPException(status_code=400, detail="window_dates 不能为空")
    return _dashboard_service().rebuild_windows(payload.window_dates)


@router.post("/upgrade")
def post_upgrade(payload: UpgradePayload) -> dict[str, Any]:
    """升级指标流程：continue 沿用旧水位，rebuild 重建历史窗口。"""
    if payload.strategy not in ("continue", "rebuild"):
        raise HTTPException(status_code=400, detail="strategy 只能是 continue 或 rebuild")
    return _dashboard_service().upgrade_metric_flow(payload.strategy)


@router.post("/legacy-boundary")
def post_legacy_boundary(payload: LegacyBoundaryPayload) -> dict[str, Any]:
    """把当前历史聚合标记为旧版本边界（兼容既有版本，供升级核对）。"""
    return _dashboard_service().stamp_legacy_boundary(payload.version)
