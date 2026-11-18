"""增量指标水位引擎测试。

覆盖：
- 首次全量纳入终态、再次刷新不重复累计
- 水位与纳入版本持久化（跨引擎实例/模拟重启）
- 修订记录只重算受影响时间窗、漂移检测
- 指标流程升级 continue / rebuild 双策略均不重复累计历史执行
- 并发刷新只接受首个成功结果，其余返回 36 号冲突终态
- 迁移幂等、日期范围查询、环境切换重置
"""
from __future__ import annotations

import copy
import threading
import time

import pytest

from app.metrics.engine import (
    BATCH_TERMINAL_STATES,
    CONFLICT_TERMINAL_CODE,
    EXEC_TERMINAL_STATES,
    METRIC_VERSION_V1,
    METRIC_VERSION_V2,
    MetricWatermarkEngine,
)
from app.store import store

WATCHED_TABLES = ("execution_log", "batch_tracker")


@pytest.fixture
def engine():
    """每个用例使用独立内存库，并在结束后还原全局 store 的原始表。"""
    snapshot = {
        name: copy.deepcopy(store.rows(name)) for name in WATCHED_TABLES
    }
    eng = MetricWatermarkEngine(":memory:")
    yield eng
    for name in WATCHED_TABLES:
        table = store.rows(name)
        table.clear()
        table.extend(copy.deepcopy(snapshot[name]))
    eng.close()


def _window_totals(eng: MetricWatermarkEngine) -> dict[str, int]:
    windows = eng.query_windows()
    return {
        "total_exec": sum(w["total_exec"] for w in windows),
        "success_exec": sum(w["success_exec"] for w in windows),
        "failed_exec": sum(w["failed_exec"] for w in windows),
        "windows": len(windows),
    }


# ---------------------------------------------------------------------------
# 纳入规则与增量刷新
# ---------------------------------------------------------------------------

class TestIncrementalRefresh:
    def test_first_refresh_includes_only_terminal(self, engine: MetricWatermarkEngine) -> None:
        result = engine.refresh(force=True)
        # 种子数据中 id=1 为「执行中」（非终态），id=2/3 为终态
        assert result["status"] == "success"
        assert result["new_executions"] == 2
        totals = _window_totals(engine)
        assert totals["total_exec"] == 2
        assert totals["success_exec"] == 1
        assert totals["failed_exec"] == 1

    def test_second_refresh_is_noop_without_double_counting(
        self, engine: MetricWatermarkEngine
    ) -> None:
        engine.refresh(force=True)
        before = _window_totals(engine)
        result = engine.refresh()
        assert result["status"] == "noop"
        assert result["new_executions"] == 0
        assert _window_totals(engine) == before

    def test_repeated_refresh_never_double_counts(self, engine: MetricWatermarkEngine) -> None:
        for _ in range(5):
            engine.refresh(force=True)
        totals = _window_totals(engine)
        # 无论刷新多少次，终态执行始终只有 2 条
        assert totals["total_exec"] == 2
        ledger = engine.watermark_info()["included_counts"]
        assert ledger == {"execution_log": 2}

    def test_new_terminal_execution_processed_incrementally(
        self, engine: MetricWatermarkEngine
    ) -> None:
        engine.refresh(force=True)
        rows = store.rows("execution_log")
        new_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": new_id,
            "status": "执行成功",
            "pending": False,
            "abnormal": False,
            "执行编号": f"EXEC-{new_id:04d}",
            "所属流程": "增量流程",
            "开始时间": "2026-10-01",
            "结束时间": "2026-10-01",
            "执行状态": "执行成功",
            "采集行数": 12,
            "错误摘要": "",
        })
        result = engine.refresh()
        assert result["new_executions"] == 1
        assert "2026-10-01" in result["rebuilt_windows"]
        # 历史窗口不受影响，仅新增窗口
        assert _window_totals(engine)["total_exec"] == 3
        oct_window = next(w for w in engine.query_windows() if w["window_date"] == "2026-10-01")
        assert oct_window["total_exec"] == 1
        assert oct_window["collected_rows"] == 12

    def test_non_terminal_rows_never_included(self, engine: MetricWatermarkEngine) -> None:
        engine.refresh(force=True)
        ledger_ids = engine._ledger_ids("execution_log")  # noqa: SLF001
        assert 1 not in ledger_ids  # 执行中
        assert 2 in ledger_ids
        assert 3 in ledger_ids


# ---------------------------------------------------------------------------
# 水位与版本持久化
# ---------------------------------------------------------------------------

class TestWatermarkPersistence:
    def test_watermark_and_version_survive_restart(self, tmp_path) -> None:
        db_path = str(tmp_path / "metric.sqlite3")
        first = MetricWatermarkEngine(db_path)
        first.refresh(force=True)
        first.stamp_legacy_watermark(METRIC_VERSION_V1)
        first.upgrade_metric_flow("continue", METRIC_VERSION_V2)
        first.close()

        # 重新打开同一文件：模拟服务重启 / 页面再次刷新
        second = MetricWatermarkEngine(db_path)
        info = second.watermark_info()
        assert info["metric_version"] == METRIC_VERSION_V2
        assert info["window_count"] == 2
        assert info["included_counts"] == {"execution_log": 2}
        result = second.refresh()
        assert result["status"] == "noop"
        assert _window_totals(second)["total_exec"] == 2
        second.close()

    def test_migrations_are_idempotent(self, engine: MetricWatermarkEngine) -> None:
        from app.metrics.migrations import migration_state, run_migrations

        first = run_migrations(engine._conn)  # noqa: SLF001
        second = run_migrations(engine._conn)  # noqa: SLF001
        assert first == []  # 构造时已应用
        assert second == []
        states = migration_state(engine._conn)  # noqa: SLF001
        assert len(states) == 1
        assert states[0]["version"] == "0001_init_metric_watermark"


# ---------------------------------------------------------------------------
# 修订记录与受影响窗口重算
# ---------------------------------------------------------------------------

class TestRevisionWindowRebuild:
    def test_status_drift_registers_revision_boundary(
        self, engine: MetricWatermarkEngine
    ) -> None:
        engine.refresh(force=True)
        row = store.find("execution_log", 3)
        row["status"] = "执行成功"  # 执行失败 → 执行成功
        drift = engine._detect_drift_revisions()  # noqa: SLF001
        assert len(drift) == 1
        assert drift[0]["window_date"] == "2026-09-03"

        result = engine.refresh()
        assert result["revision_windows"] == ["2026-09-03"]
        window = next(
            w for w in engine.query_windows() if w["window_date"] == "2026-09-03"
        )
        assert window["success_exec"] == 1
        assert window["failed_exec"] == 0
        # 修订记录已结算，不会重复触发
        assert engine.refresh()["status"] == "noop"

    def test_revision_only_rebuilds_affected_window(
        self, engine: MetricWatermarkEngine
    ) -> None:
        engine.refresh(force=True)
        untouched_before = next(
            w for w in engine.query_windows() if w["window_date"] == "2026-09-02"
        )
        engine.register_revision("execution_log", 3, "手工修订")
        result = engine.refresh()
        assert result["rebuilt_windows"] == ["2026-09-03"]
        untouched_after = next(
            w for w in engine.query_windows() if w["window_date"] == "2026-09-02"
        )
        assert untouched_before["updated_at"] == untouched_after["updated_at"]

    def test_non_terminal_revision_rejected(self, engine: MetricWatermarkEngine) -> None:
        # id=1 仍为执行中，不能登记修订
        assert engine.register_revision("execution_log", 1) is None

    def test_manual_rebuild_windows(self, engine: MetricWatermarkEngine) -> None:
        engine.refresh(force=True)
        result = engine.rebuild_windows(["2026-09-02"])
        assert result["rebuilt_windows"] == ["2026-09-02"]
        assert _window_totals(engine)["total_exec"] == 2  # 重建不重复累计


# ---------------------------------------------------------------------------
# 指标流程升级：双策略
# ---------------------------------------------------------------------------

class TestMetricFlowUpgrade:
    def _legacy_engine(self, engine: MetricWatermarkEngine) -> MetricWatermarkEngine:
        engine.refresh(force=True)
        engine.stamp_legacy_watermark(METRIC_VERSION_V1)
        return engine

    def test_continue_strategy_keeps_legacy_watermark(
        self, engine: MetricWatermarkEngine
    ) -> None:
        self._legacy_engine(engine)
        before = _window_totals(engine)
        upgrade = engine.upgrade_metric_flow("continue", METRIC_VERSION_V2)
        assert upgrade["strategy"] == "continue"
        assert upgrade["from_version"] == METRIC_VERSION_V1
        assert upgrade["to_version"] == METRIC_VERSION_V2
        assert upgrade["windows_touched"] == 0
        assert engine.watermark_info()["metric_version"] == METRIC_VERSION_V2
        # 历史窗口结果沿用，不重算
        assert _window_totals(engine) == before

    def test_rebuild_strategy_recomputes_history_windows(
        self, engine: MetricWatermarkEngine
    ) -> None:
        self._legacy_engine(engine)
        upgrade = engine.upgrade_metric_flow("rebuild", METRIC_VERSION_V2)
        assert upgrade["windows_touched"] == 2
        windows = engine.query_windows()
        assert all(w["metric_version"] == METRIC_VERSION_V2 for w in windows)

    def test_both_strategies_never_double_count_history(
        self, engine: MetricWatermarkEngine
    ) -> None:
        for strategy in ("continue", "rebuild"):
            fresh = MetricWatermarkEngine(":memory:")
            fresh.refresh(force=True)
            fresh.stamp_legacy_watermark(METRIC_VERSION_V1)
            fresh.upgrade_metric_flow(strategy, METRIC_VERSION_V2)
            # 升级后再刷新：新增数据为 0，累计总量不变
            fresh.refresh()
            assert _window_totals(fresh)["total_exec"] == 2
            assert fresh.watermark_info()["included_counts"] == {"execution_log": 2}
            fresh.close()

    def test_upgrade_audit_trail(self, engine: MetricWatermarkEngine) -> None:
        self._legacy_engine(engine)
        engine.upgrade_metric_flow("rebuild", METRIC_VERSION_V2)
        upgrades = engine.list_upgrades()
        assert len(upgrades) == 1
        assert upgrades[0]["strategy"] == "rebuild"

    def test_invalid_strategy_rejected(self, engine: MetricWatermarkEngine) -> None:
        with pytest.raises(ValueError):
            engine.upgrade_metric_flow("rollback")


# ---------------------------------------------------------------------------
# 并发首胜：36 号冲突终态
# ---------------------------------------------------------------------------

class TestConcurrentRefresh:
    def test_only_first_succeeds_others_get_conflict_36(
        self, engine: MetricWatermarkEngine
    ) -> None:
        original = engine._rebuild_window  # noqa: SLF001

        def slow_rebuild(window_date, *, force_version=None):
            time.sleep(0.2)
            return original(window_date, force_version=force_version)

        engine._rebuild_window = slow_rebuild  # type: ignore[method-assign]
        barrier = threading.Barrier(4)
        results: list[dict] = []

        def worker() -> None:
            barrier.wait()
            results.append(engine.refresh(force=True))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [r for r in results if r["status"] == "success"]
        conflicts = [r for r in results if r["status"] == "conflict"]
        assert len(successes) == 1
        assert len(conflicts) == 3
        assert all(r["conflict_code"] == CONFLICT_TERMINAL_CODE for r in conflicts)
        assert all("36号处理终态" in r["conflict_terminal"] for r in conflicts)
        # 冲突没有改变累计结果
        assert _window_totals(engine)["total_exec"] == 2


# ---------------------------------------------------------------------------
# 查询、重置与路由装配
# ---------------------------------------------------------------------------

class TestQueryAndRoutes:
    def test_date_range_query(self, engine: MetricWatermarkEngine) -> None:
        engine.refresh(force=True)
        all_windows = engine.query_windows()
        assert {w["window_date"] for w in all_windows} == {"2026-09-02", "2026-09-03"}
        single = engine.query_windows(start="2026-09-03", end="2026-09-03")
        assert [w["window_date"] for w in single] == ["2026-09-03"]
        # 展示别名与运行概览字段对齐
        assert "统计日期" in single[0]
        assert "总执行数" in single[0]

    def test_reset_clears_ledger_and_watermark(self, engine: MetricWatermarkEngine) -> None:
        engine.refresh(force=True)
        engine.reset()
        info = engine.watermark_info()
        assert info["window_count"] == 0
        assert info["included_counts"] == {}
        assert info["watermark"]["last_exec_id"] == 0
        # 重置后可重新纳入且不重复
        engine.refresh(force=True)
        assert _window_totals(engine)["total_exec"] == 2

    def test_metric_routes_registered_before_resource_router(self) -> None:
        from app.main import app

        # FastAPI 0.163 以 _IncludedRouter 封装子路由，路径以 OpenAPI 为准
        paths = set(app.openapi()["paths"].keys())
        expected = {
            "/api/report-dashboard/watermark",
            "/api/report-dashboard/metrics",
            "/api/report-dashboard/revisions",
            "/api/report-dashboard/upgrades",
            "/api/report-dashboard/refresh",
            "/api/report-dashboard/rebuild",
            "/api/report-dashboard/upgrade",
        }
        assert expected.issubset(paths)

        def _flatten(route: object) -> list[str]:
            # 普通路由直接取 path；_IncludedRouter 从 include_context 取子路由
            direct = getattr(route, "path", None)
            if isinstance(direct, str):
                return [direct]
            context = getattr(route, "include_context", None)
            inner = getattr(context, "included_router", None)
            if inner is not None:
                return [getattr(r, "path", "") for r in inner.routes]
            return []

        ordered = [path for route in app.routes for path in _flatten(route)]
        metric_index = ordered.index("/api/report-dashboard/watermark")
        dynamic_index = ordered.index("/api/{module}")
        # 固定指标路由必须排在通用动态资源路由之前，
        # 否则 /api/report-dashboard/watermark 会被当作模块名匹配
        assert metric_index < dynamic_index

    def test_execution_action_feeds_revision(
        self, engine: MetricWatermarkEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import execution_log as exec_module
        from app.services import batch_tracker as batch_module

        # 服务模块在导入时绑定了全局单例，测试中替换为用例独立引擎
        monkeypatch.setattr(exec_module, "metric_engine", engine)
        monkeypatch.setattr(batch_module, "metric_engine", engine)

        service = exec_module.ExecutionLogService()
        # id=1 由「执行中」归档为「执行成功」终态
        entry, message = service.run_action(1, "归档日志")
        assert entry is not None and "已归档日志" in message
        revisions = engine.list_revisions()
        assert any(
            r["source"] == "execution_log" and r["source_id"] == 1
            for r in revisions
        )
        result = engine.refresh()
        assert "2026-09-01" in result["rebuilt_windows"]
        assert _window_totals(engine)["total_exec"] == 3

    def test_batch_action_feeds_revision(
        self, engine: MetricWatermarkEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import batch_tracker as batch_module

        monkeypatch.setattr(batch_module, "metric_engine", engine)
        service = batch_module.BatchTrackerService()
        # 种子批次 id=3 为「校验中」，确认完成 → 有遗漏（终态）
        entry, message = service.run_action(3, "确认完成")
        assert entry is not None and "已确认完成" in message
        revisions = engine.list_revisions()
        assert any(
            r["source"] == "batch_tracker" and r["source_id"] == 3
            for r in revisions
        )
        engine.refresh()
        window = next(
            w for w in engine.query_windows() if w["window_date"] == "2026-09-03"
        )
        assert window["total_batches"] == 1
        assert window["missed_batches"] == 1

    def test_terminal_constants_match_domain(self) -> None:
        assert "执行成功" in EXEC_TERMINAL_STATES
        assert "已完成" in BATCH_TERMINAL_STATES
        assert "执行中" not in EXEC_TERMINAL_STATES
