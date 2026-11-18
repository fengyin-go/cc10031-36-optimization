"""指标水位引擎：持久化处理水位与已纳入版本，驱动运行概览增量累计。

设计目标（对应运行概览增量改造）：

1. 水位与版本同时持久化
   - ``metric_meta`` 保存处理水位（execution_log / batch_tracker 已扫描到的最大 id）
     与已纳入指标流程版本，刷新不再全量重算。
2. 终态才纳入、幂等不重复
   - ``metric_processed`` 台账以 (source, source_id) 为主键，同一条执行/批次
     无论刷新多少次只会被纳入一次，升级指标流程后不会重复累计历史执行。
3. 修订只重算受影响时间窗
   - ``metric_revisions`` 登记修订记录及其统计日期，刷新时仅对这些窗口
     做窗口级重建，其余窗口沿用已持久化结果。
4. 指标流程升级双策略
   - ``continue``：沿用旧水位、旧窗口结果，只让新增数据按新版本累计；
   - ``rebuild``：对受影响（历史）时间窗按新版本整体重建。
     两种策略都以台账去重，不会重复累计同一执行。
5. 并发首胜
   - 同一时刻只接受首个成功刷新，后续并发刷新直接返回 36 号冲突终态。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

from app.metrics.migrations import migration_state, run_migrations
from app.store import store

# ---------------------------------------------------------------------------
# 指标流程版本：v1 为升级前既有版本边界，v2 为升级后的指标流程
# ---------------------------------------------------------------------------
METRIC_VERSION_V1 = "metric-flow-v1"
METRIC_VERSION_V2 = "metric-flow-v2"
DEFAULT_METRIC_VERSION = METRIC_VERSION_V2

# 纳入指标的终态：执行中/收集中等非终态不纳入，待其进入终态后增量补入
EXEC_TERMINAL_STATES = {"执行成功", "执行失败", "已超时", "已取消"}
BATCH_TERMINAL_STATES = {"已完成", "有遗漏"}

# 并发刷新冲突的处理终态编号
CONFLICT_TERMINAL_CODE = 36
CONFLICT_TERMINAL_LABEL = "36号处理终态：并发冲突"

WATERMARK_META_KEY = "watermark"
LAST_REFRESH_META_KEY = "last_refresh"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _parse_date(value: Any) -> str | None:
    """把 开始时间/采集时间 归一成统计日期（YYYY-MM-DD）。

    支持 ISO 时间戳（含 T 或空格分隔）以及仅有日期的字符串；
    解析失败时退化为取前 10 位。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # 退化为取前 10 位（YYYY-MM-DD）
    return text[:10] if len(text) >= 10 else None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class MetricWatermarkEngine:
    """指标水位引擎：所有方法线程安全，状态持久化在 SQLite。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        # check_same_thread=False 配合内部锁支持并发首胜测试
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._gate_lock = threading.Lock()
        self._refresh_in_flight = False
        run_migrations(self._conn)
        self._ensure_watermark()

    # ------------------------------------------------------------------
    # 基础持久化
    # ------------------------------------------------------------------

    def _ensure_watermark(self) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metric_meta WHERE key = ?", (WATERMARK_META_KEY,)
            ).fetchone()
            if row is None:
                self._save_watermark({
                    "last_exec_id": 0,
                    "last_batch_id": 0,
                    "metric_version": DEFAULT_METRIC_VERSION,
                    "legacy": False,
                    "updated_at": _now_iso(),
                })

    def _load_watermark(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM metric_meta WHERE key = ?", (WATERMARK_META_KEY,)
        ).fetchone()
        if row is None:
            self._ensure_watermark()
            row = self._conn.execute(
                "SELECT value FROM metric_meta WHERE key = ?", (WATERMARK_META_KEY,)
            ).fetchone()
        return json.loads(row["value"])

    def _save_watermark(self, watermark: dict[str, Any]) -> None:
        watermark["updated_at"] = _now_iso()
        self._conn.execute(
            "INSERT INTO metric_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (WATERMARK_META_KEY, json.dumps(watermark, ensure_ascii=False)),
        )
        self._conn.commit()

    def _save_refresh_stats(self, stats: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO metric_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LAST_REFRESH_META_KEY, json.dumps(stats, ensure_ascii=False)),
        )

    def _load_refresh_stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT value FROM metric_meta WHERE key = ?", (LAST_REFRESH_META_KEY,)
        ).fetchone()
        return json.loads(row["value"]) if row else {}

    # ------------------------------------------------------------------
    # 原始数据读取（执行日志、采集批次）
    # ------------------------------------------------------------------

    @staticmethod
    def _exec_rows() -> list[dict[str, Any]]:
        return store.rows("execution_log")

    @staticmethod
    def _batch_rows() -> list[dict[str, Any]]:
        return store.rows("batch_tracker")

    @staticmethod
    def _window_of(row: dict[str, Any], time_field: str) -> str | None:
        return _parse_date(row.get(time_field))

    def _ledger_ids(self, source: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT source_id FROM metric_processed WHERE source = ?", (source,)
        ).fetchall()
        return {int(r["source_id"]) for r in rows}

    def _pending_terminal(self, source: str, rows: list[dict[str, Any]],
                          terminal_states: set[str]) -> list[dict[str, Any]]:
        """解析新增终态：已进入终态但尚未纳入台账的记录。"""
        included = self._ledger_ids(source)
        return [
            row for row in rows
            if row.get("status") in terminal_states and int(row.get("id", 0)) not in included
        ]

    # ------------------------------------------------------------------
    # 修订记录
    # ------------------------------------------------------------------

    def register_revision(self, source: str, source_id: int,
                          reason: str = "业务修订") -> dict[str, Any] | None:
        """登记一条修订记录；只有终态记录才会触发受影响窗口重算。"""
        with self._lock:
            if source == "execution_log":
                rows = self._exec_rows()
                time_field, terminal = "开始时间", EXEC_TERMINAL_STATES
            elif source == "batch_tracker":
                rows = self._batch_rows()
                time_field, terminal = "采集时间", BATCH_TERMINAL_STATES
            else:
                raise ValueError(f"未知修订来源：{source}")
            row = next((r for r in rows if int(r.get("id", 0)) == source_id), None)
            if row is None or row.get("status") not in terminal:
                return None
            window_date = self._window_of(row, time_field)
            if window_date is None:
                return None
            cur = self._conn.execute(
                "SELECT status_snapshot FROM metric_processed "
                "WHERE source = ? AND source_id = ?", (source, source_id)
            ).fetchone()
            cursor = self._conn.execute(
                "INSERT INTO metric_revisions(source, source_id, window_date, reason) "
                "VALUES(?, ?, ?, ?)",
                (source, source_id, window_date, reason),
            )
            self._conn.commit()
            return {
                "revision_id": cursor.lastrowid,
                "source": source,
                "source_id": source_id,
                "window_date": window_date,
                "reason": reason,
                "already_included": cur is not None,
                "status": "待重算",
            }

    def _detect_drift_revisions(self) -> list[dict[str, Any]]:
        """自动漂移检测：台账快照与当前终态不一致时登记修订边界。"""
        detected: list[dict[str, Any]] = []
        ledger = {
            (r["source"], int(r["source_id"])): r
            for r in self._conn.execute(
                "SELECT source, source_id, window_date, status_snapshot "
                "FROM metric_processed"
            ).fetchall()
        }
        live_index = {
            ("execution_log", int(r.get("id", 0))): r for r in self._exec_rows()
        }
        live_index.update({
            ("batch_tracker", int(r.get("id", 0))): r for r in self._batch_rows()
        })
        for (source, source_id), snap in ledger.items():
            live = live_index.get((source, source_id))
            if live is None:
                reason = "原始记录缺失"
                changed = True
            elif str(live.get("status")) != snap["status_snapshot"]:
                reason = f"状态修订：{snap['status_snapshot']} → {live.get('status')}"
                changed = True
            else:
                continue
            if not changed:
                continue
            cursor = self._conn.execute(
                "INSERT INTO metric_revisions(source, source_id, window_date, reason) "
                "VALUES(?, ?, ?, ?)",
                (source, source_id, snap["window_date"], reason),
            )
            detected.append({
                "revision_id": cursor.lastrowid,
                "source": source,
                "source_id": source_id,
                "window_date": snap["window_date"],
                "reason": reason,
            })
        self._conn.commit()
        return detected

    def list_revisions(self, status: str | None = None,
                       start: str | None = None,
                       end: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM metric_revisions WHERE 1 = 1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if start:
            sql += " AND window_date >= ?"
            params.append(start)
        if end:
            sql += " AND window_date <= ?"
            params.append(end)
        sql += " ORDER BY window_date DESC, revision_id DESC"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------
    # 窗口计算与重建
    # ------------------------------------------------------------------

    def _terminal_in_window(self, window_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        execs = [
            r for r in self._exec_rows()
            if r.get("status") in EXEC_TERMINAL_STATES
            and self._window_of(r, "开始时间") == window_date
        ]
        batches = [
            r for r in self._batch_rows()
            if r.get("status") in BATCH_TERMINAL_STATES
            and self._window_of(r, "采集时间") == window_date
        ]
        return execs, batches

    @staticmethod
    def _avg_duration_ms(execs: list[dict[str, Any]]) -> float:
        durations: list[float] = []
        for row in execs:
            start = _parse_date(row.get("开始时间"))
            end_raw = row.get("结束时间")
            end = _parse_date(end_raw)
            if not start or not end:
                continue
            try:
                start_dt = datetime.fromisoformat(str(row.get("开始时间")))
                end_dt = datetime.fromisoformat(str(end_raw))
            except ValueError:
                # 仅有日期粒度时按当天 0 点计算，同日起止耗时为 0
                start_dt = datetime.fromisoformat(start)
                end_dt = datetime.fromisoformat(end)
            delta_ms = (end_dt - start_dt).total_seconds() * 1000
            if delta_ms >= 0:
                durations.append(delta_ms)
        if not durations:
            return 0.0
        return round(sum(durations) / len(durations), 2)

    def _rebuild_window(self, window_date: str, *, force_version: str | None = None) -> None:
        """按当前原始终态重建单个时间窗，并对齐台账（修订窗口重算入口）。"""
        execs, batches = self._terminal_in_window(window_date)
        version = force_version or self._load_watermark()["metric_version"]

        # 1) 台账对齐：窗口内终态全部保证在台账中，且快照为最新
        live_keys: set[tuple[str, int]] = set()
        for source, rows in (("execution_log", execs), ("batch_tracker", batches)):
            for row in rows:
                source_id = int(row["id"])
                live_keys.add((source, source_id))
                existing = self._conn.execute(
                    "SELECT metric_version FROM metric_processed "
                    "WHERE source = ? AND source_id = ?", (source, source_id)
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        "INSERT INTO metric_processed"
                        "(source, source_id, window_date, status_snapshot, metric_version, processed_at) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (source, source_id, window_date, str(row.get("status")),
                         version, _now_iso()),
                    )
                else:
                    self._conn.execute(
                        "UPDATE metric_processed SET window_date = ?, status_snapshot = ?, "
                        "metric_version = ? WHERE source = ? AND source_id = ?",
                        (window_date, str(row.get("status")),
                         force_version or existing["metric_version"], source, source_id),
                    )

        # 2) 清理本窗口已不再是终态的陈旧台账项（状态回退场景）
        stale = [
            r for r in self._conn.execute(
                "SELECT source, source_id FROM metric_processed WHERE window_date = ?",
                (window_date,),
            ).fetchall()
            if (r["source"], int(r["source_id"])) not in live_keys
        ]
        for item in stale:
            self._conn.execute(
                "DELETE FROM metric_processed WHERE source = ? AND source_id = ?",
                (item["source"], item["source_id"]),
            )

        # 3) 重算指标数据集
        total_exec = len(execs)
        success = sum(1 for r in execs if r.get("status") == "执行成功")
        failed = sum(1 for r in execs if r.get("status") in ("执行失败", "已超时"))
        collected = sum(_to_int(r.get("采集行数")) for r in execs)
        missed = sum(1 for r in batches if r.get("status") == "有遗漏")
        availability = round(success / total_exec, 4) if total_exec else 0.0

        self._conn.execute(
            "INSERT INTO metric_windows("
            "window_date, total_exec, success_exec, failed_exec, total_batches, "
            "missed_batches, collected_rows, avg_duration_ms, availability, "
            "metric_version, exec_ids, batch_ids, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(window_date) DO UPDATE SET "
            "total_exec = excluded.total_exec, success_exec = excluded.success_exec, "
            "failed_exec = excluded.failed_exec, total_batches = excluded.total_batches, "
            "missed_batches = excluded.missed_batches, collected_rows = excluded.collected_rows, "
            "avg_duration_ms = excluded.avg_duration_ms, availability = excluded.availability, "
            "metric_version = excluded.metric_version, exec_ids = excluded.exec_ids, "
            "batch_ids = excluded.batch_ids, updated_at = excluded.updated_at",
            (
                window_date, total_exec, success, failed, len(batches), missed,
                collected, self._avg_duration_ms(execs), availability, version,
                ",".join(str(r["id"]) for r in execs),
                ",".join(str(r["id"]) for r in batches),
                _now_iso(),
            ),
        )
        self._conn.commit()

    def rebuild_windows(self, window_dates: list[str],
                        reason: str = "手动重建窗口") -> dict[str, Any]:
        """对指定时间窗重建全部指标（升级 rebuild 策略 / 手动对账入口）。"""
        with self._lock:
            touched: list[str] = []
            for window_date in sorted(set(window_dates)):
                self._rebuild_window(window_date)
                touched.append(window_date)
            return {
                "ok": True,
                "reason": reason,
                "rebuilt_windows": touched,
                "metric_version": self._load_watermark()["metric_version"],
            }

    # ------------------------------------------------------------------
    # 增量刷新（主入口）
    # ------------------------------------------------------------------

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """增量刷新运行指标。

        流程：漂移检测 → 修订窗口重算 → 新增终态纳入 → 推进水位。
        并发调用时只有首个刷新执行，其余返回 36 号冲突终态。
        """
        started = datetime.now()
        with self._gate_lock:
            if self._refresh_in_flight:
                return self._conflict_result()
            self._refresh_in_flight = True
        try:
            with self._lock:
                return self._do_refresh(force=force, started=started)
        finally:
            with self._gate_lock:
                self._refresh_in_flight = False

    def _conflict_result(self) -> dict[str, Any]:
        watermark = self._load_watermark()
        return {
            "status": "conflict",
            "conflict_code": CONFLICT_TERMINAL_CODE,
            "conflict_terminal": CONFLICT_TERMINAL_LABEL,
            "message": "已有刷新正在执行，并发请求只接受首个成功结果",
            "watermark": watermark,
            "metric_version": watermark["metric_version"],
        }

    def _do_refresh(self, *, force: bool, started: datetime) -> dict[str, Any]:
        watermark = self._load_watermark()
        before_version = watermark["metric_version"]

        # 1) 漂移检测：解析需要重算的修订边界
        drift = self._detect_drift_revisions()

        # 2) 待重算修订 → 受影响时间窗集合
        pending = self._conn.execute(
            "SELECT DISTINCT window_date FROM metric_revisions WHERE status = '待重算'"
        ).fetchall()
        revision_windows = [r["window_date"] for r in pending]

        # 3) 新增终态：执行日志 + 采集批次
        new_execs = self._pending_terminal(
            "execution_log", self._exec_rows(), EXEC_TERMINAL_STATES)
        new_batches = self._pending_terminal(
            "batch_tracker", self._batch_rows(), BATCH_TERMINAL_STATES)
        touched_windows = set(revision_windows)
        for row in new_execs:
            window = self._window_of(row, "开始时间")
            if window:
                touched_windows.add(window)
        for row in new_batches:
            window = self._window_of(row, "采集时间")
            if window:
                touched_windows.add(window)

        # 4) 窗口级重建（修订窗口 + 新增数据所在窗口），台账去重保证不重复累计
        for window_date in sorted(touched_windows):
            self._rebuild_window(window_date)
        if revision_windows:
            self._conn.execute(
                "UPDATE metric_revisions SET status = '已重算', applied_at = ? "
                "WHERE status = '待重算'",
                (_now_iso(),),
            )
            self._conn.commit()

        # 5) 推进水位（扫描到的最大 id，与是否终态无关）
        max_exec = max((int(r.get("id", 0)) for r in self._exec_rows()), default=0)
        max_batch = max((int(r.get("id", 0)) for r in self._batch_rows()), default=0)
        watermark["last_exec_id"] = max(watermark["last_exec_id"], max_exec)
        watermark["last_batch_id"] = max(watermark["last_batch_id"], max_batch)
        self._save_watermark(watermark)

        elapsed_ms = round((datetime.now() - started).total_seconds() * 1000, 2)
        has_change = bool(new_execs or new_batches or revision_windows or drift)
        status = "success" if (has_change or force) else "noop"
        stats = {
            "status": status,
            "refreshed_at": _now_iso(),
            "elapsed_ms": elapsed_ms,
            "new_executions": len(new_execs),
            "new_batches": len(new_batches),
            "revision_windows": revision_windows,
            "rebuilt_windows": sorted(touched_windows),
            "drift_detected": len(drift),
            "metric_version": watermark["metric_version"],
        }
        self._save_refresh_stats(stats)
        self._conn.commit()
        return {
            "status": status,
            "mode": "incremental",
            "watermark": watermark,
            "metric_version": watermark["metric_version"],
            "version_changed": before_version != watermark["metric_version"],
            "processed": {
                "execution_log": len(new_execs),
                "batch_tracker": len(new_batches),
            },
            "new_executions": len(new_execs),
            "new_batches": len(new_batches),
            "drift_detected": len(drift),
            "revision_windows": revision_windows,
            "rebuilt_windows": sorted(touched_windows),
            "refresh_ms": elapsed_ms,
            "refreshed_at": stats["refreshed_at"],
            "last_elapsed_ms": elapsed_ms,
        }

    # ------------------------------------------------------------------
    # 指标流程升级
    # ------------------------------------------------------------------

    def stamp_legacy_watermark(self, version: str = METRIC_VERSION_V1) -> dict[str, Any]:
        """把当前结果标记为既有版本边界的历史聚合（兼容升级前场景）。"""
        with self._lock:
            watermark = self._load_watermark()
            watermark["metric_version"] = version
            watermark["legacy"] = True
            self._save_watermark(watermark)
            self._conn.execute(
                "UPDATE metric_windows SET metric_version = ?", (version,))
            self._conn.execute(
                "UPDATE metric_processed SET metric_version = ?", (version,))
            self._conn.commit()
            return watermark

    def upgrade_metric_flow(self, strategy: str,
                            target_version: str = METRIC_VERSION_V2) -> dict[str, Any]:
        """升级指标流程，支持 continue / rebuild 两种策略，均不重复累计。"""
        if strategy not in ("continue", "rebuild"):
            raise ValueError("升级策略只能是 continue 或 rebuild")
        with self._lock:
            watermark = self._load_watermark()
            from_version = watermark["metric_version"]
            if from_version == target_version:
                return {
                    "ok": True,
                    "strategy": strategy,
                    "from_version": from_version,
                    "to_version": target_version,
                    "windows_touched": 0,
                    "message": "当前已是目标版本，无需升级",
                }

            touched = 0
            if strategy == "continue":
                # 沿用旧水位与历史窗口结果，仅切换版本；新增数据按新版本累计
                watermark["metric_version"] = target_version
                watermark["legacy"] = False
                self._save_watermark(watermark)
            else:
                # 对全部历史时间窗按新版本重建；台账主键保证同一执行不重复累计
                history_windows = [
                    r["window_date"]
                    for r in self._conn.execute(
                        "SELECT DISTINCT window_date FROM metric_processed"
                    ).fetchall()
                ]
                watermark["metric_version"] = target_version
                watermark["legacy"] = False
                self._save_watermark(watermark)
                for window_date in history_windows:
                    self._rebuild_window(window_date, force_version=target_version)
                touched = len(history_windows)

            cursor = self._conn.execute(
                "INSERT INTO metric_upgrades"
                "(from_version, to_version, strategy, windows_touched) "
                "VALUES(?, ?, ?, ?)",
                (from_version, target_version, strategy, touched),
            )
            self._conn.commit()
            return {
                "ok": True,
                "upgrade_id": cursor.lastrowid,
                "strategy": strategy,
                "from_version": from_version,
                "to_version": target_version,
                "windows_touched": touched,
                "watermark": self._load_watermark(),
            }

    def list_upgrades(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM metric_upgrades ORDER BY upgrade_id DESC"
            ).fetchall()
        ]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query_windows(self, start: str | None = None,
                      end: str | None = None) -> list[dict[str, Any]]:
        """按日期范围查看指标水位、纳入版本和窗口指标数据集。"""
        sql = "SELECT * FROM metric_windows WHERE 1 = 1"
        params: list[Any] = []
        if start:
            sql += " AND window_date >= ?"
            params.append(start)
        if end:
            sql += " AND window_date <= ?"
            params.append(end)
        sql += " ORDER BY window_date DESC"
        result: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params).fetchall():
            item = dict(row)
            item["exec_id_list"] = [
                int(x) for x in row["exec_ids"].split(",") if x
            ]
            item["batch_id_list"] = [
                int(x) for x in row["batch_ids"].split(",") if x
            ]
            # 与运行概览字段对齐的展示别名
            item["统计日期"] = row["window_date"]
            item["总执行数"] = row["total_exec"]
            item["成功数"] = row["success_exec"]
            item["失败数"] = row["failed_exec"]
            item["采集总量"] = row["collected_rows"]
            item["平均耗时"] = row["avg_duration_ms"]
            item["可用率"] = row["availability"]
            result.append(item)
        return result

    def watermark_info(self) -> dict[str, Any]:
        """指标水位、纳入版本与最近刷新耗时的综合视图。"""
        watermark = self._load_watermark()
        last_refresh = self._load_refresh_stats()
        included = self._conn.execute(
            "SELECT source, COUNT(*) AS c FROM metric_processed GROUP BY source"
        ).fetchall()
        window_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM metric_windows"
        ).fetchone()["c"]
        return {
            "watermark": watermark,
            "last_exec_id": watermark["last_exec_id"],
            "last_batch_id": watermark["last_batch_id"],
            "metric_version": watermark["metric_version"],
            "legacy": watermark.get("legacy", False),
            "last_refresh": last_refresh,
            "last_elapsed_ms": last_refresh.get("elapsed_ms"),
            "refreshed_at": last_refresh.get("refreshed_at"),
            "included_counts": {r["source"]: r["c"] for r in included},
            "window_count": window_count,
            "pending_revisions": len(self.list_revisions(status="待重算")),
            "migrations": migration_state(self._conn),
            "storage": "memory" if self.db_path == ":memory:" else "file",
            "db_path": self.db_path,
        }

    def reset(self) -> None:
        """清空指标数据（保留迁移记录），用于本地开发环境切换/重新对账。"""
        with self._lock:
            for table in (
                "metric_windows", "metric_processed",
                "metric_revisions", "metric_upgrades",
            ):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute(
                "DELETE FROM metric_meta WHERE key IN (?, ?)",
                (WATERMARK_META_KEY, LAST_REFRESH_META_KEY),
            )
            self._conn.commit()
            self._ensure_watermark()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
