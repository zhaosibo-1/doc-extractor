"""配置层测试。

配置看起来「没什么逻辑」，但这个项目里有一类问题专门藏在配置里：
**路径与引擎都依赖环境**，而环境在开发机、容器、CI 上各不相同。
「在我这儿好好的」几乎总是这一类。所以这里重点守两件事：

1. 所有默认路径都锚定到项目根，不跟着进程的当前工作目录跑；
2. 引擎的实际生效值必须能被预期 —— 降级要降得明明白白。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    ENGINE_AUTO,
    ENGINE_LLM,
    ENGINE_RULE,
    PROJECT_ROOT,
    Settings,
    load_settings,
    mask_secret,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    """清掉可能从开发机继承来的环境变量，否则测试结果取决于谁的机器。"""
    for name in ("DATA_DIR", "LLM_API_KEY", "LLM_PRESET", "LLM_BASE_URL", "LLM_MODEL",
                 "VISION_MODEL", "EXTRACT_ENGINE"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 路径锚定
# ---------------------------------------------------------------------------


class TestPathAnchoring:
    def test_默认data目录锚定项目根(self) -> None:
        assert load_settings().data_dir == PROJECT_ROOT / "data"

    def test_换掉工作目录不影响默认data目录(self, tmp_path, monkeypatch) -> None:
        """``Path("data")`` 这种写法会让上传的文件落到「当前目录」，而没人知道当前目录是哪儿。"""
        monkeypatch.chdir(tmp_path)
        assert load_settings().data_dir == PROJECT_ROOT / "data"

    def test_绝对DATA_DIR原样使用(self, tmp_path, monkeypatch) -> None:
        """容器里给的就是绝对路径 ``/app/data``，必须原样使用。

        这里用 ``tmp_path`` 而不是写字面量 ``/app/data`` —— 后者在 Windows 上
        不算绝对路径（没有盘符），会走「按项目根解析」那条分支，
        于是测试测的是另一件事，在 Windows 上假失败、在 Linux 上假通过。
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "container-data"))
        assert load_settings().data_dir == tmp_path / "container-data"

    def test_相对DATA_DIR按项目根解析(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DATA_DIR", "mydata")
        assert load_settings().data_dir == PROJECT_ROOT / "mydata"

    def test_uploads目录在data之下(self) -> None:
        assert Settings().uploads_dir == Settings().data_dir / "uploads"


# ---------------------------------------------------------------------------
# 引擎生效值
# ---------------------------------------------------------------------------


class TestActiveEngine:
    def test_auto无Key降级为rule(self) -> None:
        settings = Settings(engine_mode=ENGINE_AUTO, llm_api_key="")
        assert settings.llm_enabled is False
        assert settings.active_engine == ENGINE_RULE

    def test_auto有Key用llm(self, monkeypatch) -> None:
        monkeypatch.setenv("LLM_PRESET", "deepseek")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-key-1234567890")
        settings = load_settings()
        assert settings.llm_enabled is True
        assert settings.active_engine == ENGINE_LLM

    def test_显式rule不被Key改变(self, monkeypatch) -> None:
        monkeypatch.setenv("LLM_PRESET", "deepseek")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-key-1234567890")
        monkeypatch.setenv("EXTRACT_ENGINE", "rule")
        settings = load_settings()
        assert settings.active_engine == ENGINE_RULE

    def test_显式llm不会被静默改成rule(self) -> None:
        """``llm`` 是「点名要用模型」。没配 Key 时 active_engine 照样报 llm，
        由抽取层抛错 —— 换成静默降级就等于骗人。"""
        settings = Settings(engine_mode=ENGINE_LLM, llm_api_key="")
        assert settings.active_engine == ENGINE_LLM

    def test_非法引擎名回落到auto(self, monkeypatch) -> None:
        monkeypatch.setenv("EXTRACT_ENGINE", "gpt")
        assert load_settings().engine_mode == ENGINE_AUTO


# ---------------------------------------------------------------------------
# 密钥脱敏
# ---------------------------------------------------------------------------


class TestMaskSecret:
    def test_空值给出明确的未配置(self) -> None:
        # 空串在界面上是一片空白，用户分不清「没配」和「配了但读不到」
        assert mask_secret("") == "(未配置)"

    def test_过短一律打满星(self) -> None:
        assert mask_secret("sk-123") == "****"

    def test_保留头尾便于核对(self) -> None:
        masked = mask_secret("sk-abcdefghijklmnopqrstuvwxyz")
        assert masked.startswith("sk-")
        assert masked.endswith("wxyz")
        assert "****" in masked

    def test_脱敏结果不包含原密钥(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        assert secret not in mask_secret(secret)

    def test_config导出里绝不出现明文密钥(self, monkeypatch) -> None:
        monkeypatch.setenv("LLM_PRESET", "deepseek")
        monkeypatch.setenv("LLM_API_KEY", "sk-abcdefghijklmnopqrstuvwxyz")
        dumped = repr(load_settings().describe())
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in dumped
        assert "(未配置)" not in dumped
