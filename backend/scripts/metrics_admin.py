#!/usr/bin/env python3
"""指标水位库运维 CLI — 流水线数据库迁移部署与本地开发切换入口。

用法（在 backend/ 目录下）::

    python -m scripts.metrics_admin migrate   # 执行/校验数据库迁移（幂等）
    python -m scripts.metrics_admin info      # 查看水位、纳入版本、迁移状态
    python -m scripts.metrics_admin refresh   # 触发一次增量刷新
    python -m scripts.metrics_admin reset     # 清空指标数据并重置水位（保留表结构）

数据库路径由环境变量 METRICS_DB 控制；不设置时使用默认持久化文件。
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import settings
from app.metrics.engine import MetricWatermarkEngine


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="指标水位库运维工具")
    parser.add_argument(
        "command",
        choices=["migrate", "info", "refresh", "reset"],
        help="migrate=执行迁移；info=查看水位；refresh=增量刷新；reset=重置指标",
    )
    args = parser.parse_args(argv)

    engine = MetricWatermarkEngine(settings.metric_db_path)
    try:
        if args.command == "migrate":
            from app.metrics.migrations import migration_state, run_migrations

            applied = run_migrations(engine._conn)  # noqa: SLF001 - 运维脚本直接驱动迁移
            _print({
                "ok": True,
                "db_path": settings.metric_db_path,
                "newly_applied": applied,
                "state": migration_state(engine._conn),
            })
        elif args.command == "info":
            _print(engine.watermark_info())
        elif args.command == "refresh":
            _print(engine.refresh(force=True))
        elif args.command == "reset":
            engine.reset()
            _print({"ok": True, "message": "指标数据已清空，水位已重置"})
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
