"""pytest 全局配置：测试期间把指标水位库隔离到内存，避免污染本地持久化文件。"""
from __future__ import annotations

import os

# 必须在 app.* 被测试模块导入前生效；conftest 由 pytest 最先加载。
os.environ.setdefault("METRICS_DB", ":memory:")
