"""配置层：环境变量 -> Settings 对象。

设计原则（与前两个项目保持一致）：
- **零配置可跑**。不填任何环境变量，服务也能启动并完成文档 / 表格的全流程抽取，
  只是抽取阶段走规则引擎而不是大模型。CI 因此不需要任何 secret。
- **Key 只从环境变量读**，``describe()`` 返回的永远是脱敏值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 大模型厂商预设（OpenAI 兼容协议）
# ---------------------------------------------------------------------------

LLM_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        # DeepSeek 目前没有视觉模型接入，图片类任务需要换成别的 preset
        "vision_model": "",
        "label": "DeepSeek",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "vision_model": "qwen-vl-plus",
        "label": "阿里通义千问",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "vision_model": "moonshot-v1-8k-vision-preview",
        "label": "月之暗面 Kimi",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "vision_model": "glm-4v-flash",
        "label": "智谱 GLM",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "vision_model": "Qwen/Qwen2.5-VL-7B-Instruct",
        "label": "硅基流动",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "label": "OpenAI",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "vision_model": "",
        "label": "自定义（需同时填 LLM_BASE_URL 与 LLM_MODEL）",
    },
}

#: 项目根目录（``app/`` 的上一级）。所有默认路径都以它为基准，
#: 不用进程的当前工作目录 —— 否则「换个目录起服务」会安静地读写另一份数据。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 抽取引擎模式
ENGINE_AUTO = "auto"  # 有 Key 用模型补漏，无 Key 自动降级到规则
ENGINE_RULE = "rule"  # 强制规则引擎（结果确定，便于对照调试）
ENGINE_LLM = "llm"    # 强制模型引擎（无 Key 直接报错，不静默降级）
ENGINES = (ENGINE_AUTO, ENGINE_RULE, ENGINE_LLM)

VERSION = "1.0.0"


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _env_str(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = _env_str(name)
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env_str(name)
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def mask_secret(secret: str, *, keep_head: int = 3, keep_tail: int = 4) -> str:
    """把密钥脱敏成 ``sk-xxx****abcd`` 形式。过短的密钥一律返回 ``****``。"""
    if not secret:
        return "(未配置)"
    if len(secret) < keep_head + keep_tail + 4:
        return "****"
    return f"{secret[:keep_head]}****{secret[-keep_tail:]}"


@dataclass
class Settings:
    """运行期配置。由 :func:`load_settings` 从环境变量装配。"""

    # ---- 文本大模型 ----
    llm_preset: str = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout: int = 60

    # ---- 视觉大模型（图片 / 扫描件抽取用）----
    vision_model: str = ""

    # ---- 抽取引擎 ----
    engine_mode: str = ENGINE_AUTO

    # ---- 服务 ----
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    max_upload_mb: int = 10
    #: 上传文件的落盘目录。
    #:
    #: 默认**锚定到包所在的项目根**，而不是进程的当前工作目录。
    #: ``Path("data")`` 这种写法在「从别的目录起服务」时会安静地
    #: 新建一个空的 data 目录，用户看到的是「我上传的文件全不见了」，
    #: 而实际原因是读写了另一个路径 —— 这类问题排查起来非常费劲。
    #: 显式设了 ``DATA_DIR`` 环境变量（容器里就是这么做的）则以环境变量为准。
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    # ---- 解析限制（防止超大文件拖垮服务）----
    #: PDF 最多解析多少页。超出的部分会被截断并明确告知，而不是静默丢弃。
    pdf_max_pages: int = 60
    #: 单个表格最多保留多少行
    table_max_rows: int = 500
    #: 单文档最多保留多少块
    block_max_count: int = 4000
    #: 表头最多识别几行
    header_max_rows: int = 3

    #: 上传的文件是否落盘。测试环境设为 False，避免仓库里留下测试残留。
    store_persist: bool = True

    #: 进程启动时间（用于 /api/stats 里的运行时长）
    started_at: float = 0.0

    # -- 派生属性 ----------------------------------------------------------

    @property
    def preset(self) -> dict[str, str]:
        return LLM_PRESETS.get(self.llm_preset, LLM_PRESETS["custom"])

    @property
    def resolved_base_url(self) -> str:
        return self.llm_base_url or self.preset.get("base_url", "")

    @property
    def resolved_model(self) -> str:
        return self.llm_model or self.preset.get("model", "")

    @property
    def resolved_vision_model(self) -> str:
        """实际生效的视觉模型：显式配置优先于 preset。"""
        return self.vision_model or self.preset.get("vision_model", "")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.resolved_base_url and self.resolved_model)

    @property
    def vision_enabled(self) -> bool:
        """图片 / 扫描件抽取的前提：有 Key、有 Base URL、且拿到了视觉模型名。"""
        return bool(self.llm_api_key and self.resolved_base_url and self.resolved_vision_model)

    @property
    def active_engine(self) -> str:
        """当前真正生效的抽取引擎。

        ``auto`` 模式下有 Key 就上模型，没 Key 就降级到规则 —— 但**明确告知**降级了。
        """
        if self.engine_mode == ENGINE_RULE:
            return ENGINE_RULE
        if self.engine_mode == ENGINE_LLM:
            return ENGINE_LLM
        return ENGINE_LLM if self.llm_enabled else ENGINE_RULE

    @property
    def uploads_dir(self) -> Path:
        """上传文件的落盘目录。"""
        return self.data_dir / "uploads"

    # -- 输出 --------------------------------------------------------------

    def describe(self) -> dict[str, object]:
        """导出给 ``/api/config`` 的脱敏配置。**绝不返回 Key 原文。**"""
        return {
            "llm_enabled": self.llm_enabled,
            "vision_enabled": self.vision_enabled,
            "provider": self.preset.get("label", self.llm_preset),
            "model": self.resolved_model or "(未配置)",
            "vision_model": self.resolved_vision_model or "(未配置，图片抽取不可用)",
            "base_url": self.resolved_base_url or "(未配置)",
            "api_key_masked": mask_secret(self.llm_api_key),
            "engine": self.active_engine,
            "engine_mode": self.engine_mode,
            "max_upload_mb": self.max_upload_mb,
            "pdf_max_pages": self.pdf_max_pages,
        }

    def ensure_dirs(self) -> None:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)


def _resolve_data_dir() -> Path:
    """解析 ``DATA_DIR``。

    - 没设 → ``<项目根>/data``
    - 设了绝对路径（容器里是 ``/app/data``）→ 原样使用
    - 设了相对路径 → 相对**项目根**解析，而不是相对进程的当前工作目录

    最后一条是刻意的：``DATA_DIR=data`` 加上从别的目录起服务，会让上传的文件
    落到一个谁都想不到的地方，而界面上看起来一切正常。
    """
    raw = _env_str("DATA_DIR")
    if not raw:
        return PROJECT_ROOT / "data"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings() -> Settings:
    """从环境变量装配 Settings。

    所有取值都有兜底：环境变量写错不会让服务起不来，只会回落到默认值 ——
    配置错误应该表现为 ``/api/config`` 里一眼可见的降级提示，而不是启动崩溃。
    """
    import time

    preset = _env_str("LLM_PRESET", "deepseek").lower()
    if preset not in LLM_PRESETS:
        preset = "custom"

    engine = _env_str("EXTRACT_ENGINE", ENGINE_AUTO).lower()
    if engine not in ENGINES:
        engine = ENGINE_AUTO

    return Settings(
        llm_preset=preset,
        llm_api_key=_env_str("LLM_API_KEY"),
        llm_base_url=_env_str("LLM_BASE_URL"),
        llm_model=_env_str("LLM_MODEL"),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.0, minimum=0.0, maximum=2.0),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 4096, minimum=256, maximum=32768),
        llm_timeout=_env_int("LLM_TIMEOUT", 60, minimum=5, maximum=600),
        vision_model=_env_str("VISION_MODEL"),
        engine_mode=engine,
        host=_env_str("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000, minimum=1, maximum=65535),
        cors_origins=_env_list("CORS_ORIGINS", ["*"]),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 10, minimum=1, maximum=200),
        data_dir=_resolve_data_dir(),
        pdf_max_pages=_env_int("PDF_MAX_PAGES", 60, minimum=1, maximum=2000),
        table_max_rows=_env_int("TABLE_MAX_ROWS", 500, minimum=1, maximum=100000),
        block_max_count=_env_int("BLOCK_MAX_COUNT", 4000, minimum=1, maximum=1000000),
        header_max_rows=_env_int("HEADER_MAX_ROWS", 3, minimum=1, maximum=10),
        store_persist=_env_bool("STORE_PERSIST", True),
        started_at=time.time(),
    )


__all__ = [
    "LLM_PRESETS",
    "ENGINE_AUTO",
    "ENGINE_RULE",
    "ENGINE_LLM",
    "ENGINES",
    "VERSION",
    "Settings",
    "load_settings",
    "mask_secret",
]
