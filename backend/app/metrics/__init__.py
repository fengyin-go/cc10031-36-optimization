"""增量指标水位引擎包。

- migrations: 数据库迁移脚本注册表（流水线迁移部署入口）
- engine: 指标水位引擎，持久化处理水位与已纳入版本，支持增量累计、
  修订窗口重算、指标流程升级双策略与并发首胜
"""
from __future__ import annotations

from app.config import settings
from app.metrics.engine import MetricWatermarkEngine

# 单例引擎：数据库路径由环境变量 METRICS_DB 控制，本地/测试/部署环境可切换
metric_engine = MetricWatermarkEngine(settings.metric_db_path)

__all__ = ["MetricWatermarkEngine", "metric_engine"]
