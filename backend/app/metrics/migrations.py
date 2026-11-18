"""指标库迁移脚本注册表 — 流水线数据库迁移部署入口。

每条迁移带有序号、版本号和幂等 SQL（建表使用 IF NOT EXISTS），
``run_migrations`` 按序号顺序执行并把已应用版本写入
``metric_migrations``，重复部署不会重复建表或重复累计数据。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """单条数据库迁移。"""

    seq: int
    version: str
    statements: tuple[str, ...]


# 0001 初始化：指标水位引擎所需的全部表
MIGRATION_0001 = Migration(
    seq=1,
    version="0001_init_metric_watermark",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS metric_migrations (
            seq         INTEGER PRIMARY KEY,
            version     TEXT NOT NULL UNIQUE,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS metric_meta (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        )
        """,
        # 时间窗指标数据集（运行指标对账数据，按统计日期分区）
        """
        CREATE TABLE IF NOT EXISTS metric_windows (
            window_date      TEXT PRIMARY KEY,
            total_exec       INTEGER NOT NULL DEFAULT 0,
            success_exec     INTEGER NOT NULL DEFAULT 0,
            failed_exec      INTEGER NOT NULL DEFAULT 0,
            total_batches    INTEGER NOT NULL DEFAULT 0,
            missed_batches   INTEGER NOT NULL DEFAULT 0,
            collected_rows   INTEGER NOT NULL DEFAULT 0,
            avg_duration_ms  REAL NOT NULL DEFAULT 0,
            availability     REAL NOT NULL DEFAULT 0,
            metric_version   TEXT NOT NULL,
            exec_ids         TEXT NOT NULL DEFAULT '',
            batch_ids        TEXT NOT NULL DEFAULT '',
            updated_at       TEXT NOT NULL
        )
        """,
        # 终态纳入台账：同一条执行/批次只可能被纳入一次（防重复累计核心）
        """
        CREATE TABLE IF NOT EXISTS metric_processed (
            source         TEXT NOT NULL,
            source_id      INTEGER NOT NULL,
            window_date    TEXT NOT NULL,
            status_snapshot TEXT NOT NULL,
            metric_version TEXT NOT NULL,
            processed_at   TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )
        """,
        # 修订记录：只登记受影响的时间窗，刷新时只重算这些窗口
        """
        CREATE TABLE IF NOT EXISTS metric_revisions (
            revision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,
            source_id      INTEGER NOT NULL,
            window_date    TEXT NOT NULL,
            reason         TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT '待重算',
            applied_at     TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        # 指标流程升级审计：记录每次升级策略与版本边界
        """
        CREATE TABLE IF NOT EXISTS metric_upgrades (
            upgrade_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            from_version   TEXT NOT NULL,
            to_version     TEXT NOT NULL,
            strategy       TEXT NOT NULL,
            windows_touched INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_metric_processed_window ON metric_processed(window_date)",
        "CREATE INDEX IF NOT EXISTS idx_metric_revisions_window ON metric_revisions(window_date, status)",
    ),
)

MIGRATIONS: tuple[Migration, ...] = (MIGRATION_0001,)


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """按序号执行全部未应用的迁移，返回本次新应用的迁移版本列表。

    幂等：已登记在 ``metric_migrations`` 中的迁移会被跳过，
    因此流水线重复部署不会重建表，也不会影响已累计的指标。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_migrations (
            seq         INTEGER PRIMARY KEY,
            version     TEXT NOT NULL UNIQUE,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    applied = {
        int(row[0])
        for row in conn.execute("SELECT seq FROM metric_migrations")
    }
    newly_applied: list[str] = []
    for migration in sorted(MIGRATIONS, key=lambda item: item.seq):
        if migration.seq in applied:
            continue
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO metric_migrations(seq, version) VALUES (?, ?)",
            (migration.seq, migration.version),
        )
        newly_applied.append(migration.version)
    conn.commit()
    return newly_applied


def migration_state(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """返回迁移应用情况，供部署探针核对流水线迁移是否到位。"""
    try:
        rows = conn.execute(
            "SELECT seq, version, applied_at FROM metric_migrations ORDER BY seq"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"seq": int(seq), "version": version, "applied_at": applied_at}
        for seq, version, applied_at in rows
    ]
