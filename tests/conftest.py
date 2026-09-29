"""pytest 公共夹具。

只放夹具与全局配置；供测试模块 import 的静态数据在 ``tests/helpers.py``。

测试用到的示例文件直接读仓库里的 ``examples/``（它们是提交进版本库的产物，
同时被 README、端到端验收脚本和 CI 使用）。这样测试验证的是**用户真正会点开的
那几份文件**，而不是测试自己临时造的一份理想数据 —— 后者往往过于干净，
反而漏掉真实缺陷。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

from .helpers import EXAMPLE_CASES, EXAMPLES, ROOT, read_example


@pytest.fixture
def settings() -> Settings:
    """默认测试配置：不落盘、限制放宽。"""
    return Settings(store_persist=False, pdf_max_pages=200, table_max_rows=10000)


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """起一个真实的 FastAPI 应用实例（走 lifespan，但不监听端口）。

    环境变量必须在进入上下文**之前**设好 —— ``app.state`` 是在 lifespan 里
    根据当时的配置构造的，晚一步设就等于没设。这也是为什么这里用 monkeypatch
    而不是在模块顶层改 ``os.environ``：测试之间必须互不污染。
    """
    monkeypatch.setenv("STORE_PERSIST", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("VISION_MODEL", "")
    monkeypatch.setenv("EXTRACT_ENGINE", "auto")

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


__all__ = ["EXAMPLE_CASES", "EXAMPLES", "ROOT", "read_example", "settings", "client"]
