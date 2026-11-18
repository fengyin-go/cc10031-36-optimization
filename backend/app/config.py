"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _default_metric_db_path() -> str:
    """指标水位库路径。

    - 显式设置 ``METRICS_DB`` 时优先使用（流水线部署/本地切换都走这个变量）；
    - 默认落在 backend/var/metric_watermark.sqlite3，持久化处理水位与纳入版本；
    - 测试可传 ``:memory:`` 得到完全隔离的内存库。
    """
    explicit = os.getenv("METRICS_DB", "").strip()
    if explicit:
        return explicit
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_dir = os.path.join(base_dir, "var")
    os.makedirs(default_dir, exist_ok=True)
    return os.path.join(default_dir, "metric_watermark.sqlite3")


@dataclass(frozen=True)
class Settings:
    app_name: str = "RPA流程自动化中心"
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "local"))
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    metric_db_path: str = field(default_factory=_default_metric_db_path)


settings = Settings()
