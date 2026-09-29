"""OpenAI 兼容协议客户端（文本 + 视觉）。

把所有厂商的差异收在这一层，上层只看到 ``chat()`` 和 ``vision()`` 两个方法。

三个值得讲清楚的实现细节：

1. **JSON 提取**：模型返回的「JSON」从来不是干净的 JSON —— 会带 ```json 围栏、
   会先来解释两句、会在结尾接一段总结。这里用三级策略逐级降级地把它捞出来，
   最后一级是**括号平衡扫描**（不是正则），因为正则处理不了嵌套与字符串里的花括号。
2. **错误分类**：401/403 是 Key 问题、429 是限流、5xx 是服务端问题 —— 它们的
   重试价值完全不同，统一抛「请求失败」等于把排障成本转嫁给了用户。
3. **视觉走同一个 Base URL**，只是把 content 换成 ``image_url`` 形式的多模态数组。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """大模型调用失败。``retriable`` 指示重试是否有意义。"""

    def __init__(self, message: str, *, retriable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.status = status


class LLMNotConfigured(LLMError):
    """没有配置 Key / Base URL / 模型名。"""

    def __init__(self, message: str = "未配置大模型，请在 .env 里填写 LLM_API_KEY") -> None:
        super().__init__(message, retriable=False, status=0)


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从模型输出里尽最大努力提取 JSON。

    三级策略：
        1. 直接 ``json.loads``（最理想的情况）；
        2. 剥掉 Markdown 代码围栏后再试；
        3. 从第一个 ``{`` / ``[`` 开始做**括号平衡扫描**，取第一个完整的对象/数组。

    第 3 级必须用括号平衡而不是「找最后一个 ``}``」：
    模型在 JSON 后面又写了一段解释、而解释里也带花括号的情况非常常见，
    用正则或 rfind 都会把后半段解释一起吞进来。
    """
    if not text or not text.strip():
        raise LLMError("模型返回了空内容")

    candidate = text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE_RE.search(candidate)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            candidate = inner

    balanced = _first_balanced(candidate)
    if balanced is not None:
        try:
            return json.loads(balanced)
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回的内容不是合法 JSON：{exc}") from exc

    raise LLMError("模型返回的内容里找不到 JSON 对象或数组")


def _first_balanced(text: str) -> str | None:
    """从第一个 ``{`` 或 ``[`` 开始，扫描出第一个括号平衡的片段。

    必须处理字符串字面量与转义引号 —— 否则 ``{"note": "}"}`` 会在引号里的
    那个 ``}`` 处提前闭合。
    """
    start = -1
    opener = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            opener = ch
            break
    if start < 0:
        return None

    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


def classify_http_error(status: int, body: str) -> LLMError:
    """把 HTTP 状态码翻译成「人能看懂 + 知道该不该重试」的错误。"""
    detail = _shorten(body)
    if status in (401, 403):
        return LLMError(
            f"鉴权失败（HTTP {status}）：API Key 无效、已过期或权限不足。{detail}",
            retriable=False,
            status=status,
        )
    if status == 402:
        return LLMError(f"账户余额不足（HTTP 402）。{detail}", retriable=False, status=status)
    if status == 404:
        return LLMError(
            f"接口地址或模型名不存在（HTTP 404）：请检查 LLM_BASE_URL 与 LLM_MODEL。{detail}",
            retriable=False,
            status=status,
        )
    if status == 429:
        return LLMError(f"触发限流（HTTP 429）：稍后重试或降低并发。{detail}", retriable=True, status=status)
    if status in (400, 422):
        # 很多兼容服务在「不支持 response_format」时返回 400，
        # 调用方会据此去掉 json_mode 重试，所以这里标成可重试
        return LLMError(
            f"请求被拒绝（HTTP {status}）：可能是模型不支持该参数。{detail}",
            retriable=True,
            status=status,
        )
    if status >= 500:
        return LLMError(f"服务端错误（HTTP {status}）。{detail}", retriable=True, status=status)
    return LLMError(f"请求失败（HTTP {status}）。{detail}", retriable=False, status=status)


def _shorten(body: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", (body or "").strip())
    return text[:limit] + ("…" if len(text) > limit else "")


#: 「不认识 response_format 参数」的典型报错特征
_UNSUPPORTED_JSON_MODE = (
    "response_format",
    "json_object",
    "json mode",
    "unsupported",
    "not support",
    "invalid_request_error",
)


def looks_like_json_mode_unsupported(body: str) -> bool:
    """判断 400 是不是因为模型不支持 ``response_format``。

    这个判断值得单独写一个函数：不同厂商的报错措辞差异极大，
    集中在一处便于测试，也便于后续补关键词。
    """
    lowered = (body or "").lower()
    return any(token in lowered for token in _UNSUPPORTED_JSON_MODE)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class LLMClient:
    """OpenAI 兼容客户端。未配置时所有方法都会抛 :class:`LLMNotConfigured`。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        vision_model: str = "",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.vision_model = vision_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._transport = transport
        self.calls = 0
        self.failures = 0

    # -- 能力判断 ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.api_key and self.base_url and (self.vision_model or self.model))

    def ensure_ready(self, *, vision: bool = False) -> None:
        """配置自检：没配就抛 :class:`LLMNotConfigured`。

        公开出来是给**调用方提前失败**用的：抽取器在「用户点名要用大模型」时
        会在动手之前先调它，避免出现「规则恰好全覆盖 → 一次模型都没调 →
        却回报 engine=llm」这种情况。内部的 ``_ensure`` 仍是私有实现。
        """
        self._ensure(vision=vision)

    def _ensure(self, *, vision: bool = False) -> None:
        if not self.enabled:
            raise LLMNotConfigured()
        if vision and not self.vision_enabled:
            raise LLMNotConfigured(
                "未配置视觉模型，无法读取图片内容。"
                "请在 .env 里设置 VISION_MODEL，或换用带视觉能力的厂商预设"
                "（如 LLM_PRESET=qwen 会自动带出 qwen-vl-plus）。"
            )

    # -- 请求 --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        self.calls += 1
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            self.failures += 1
            raise LLMError(f"请求超时（{self.timeout}s）：{exc}", retriable=True) from exc
        except httpx.HTTPError as exc:
            self.failures += 1
            raise LLMError(f"网络请求失败：{exc}", retriable=True) from exc

        if response.status_code >= 400:
            self.failures += 1
            raise classify_http_error(response.status_code, response.text)

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            self.failures += 1
            raise LLMError(f"服务端返回的不是 JSON：{exc}", retriable=True) from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """发起一次对话，返回文本内容。

        ``json_mode=True`` 时会带上 ``response_format``；服务端明确表示不支持时
        **自动去掉该参数重试一次** —— 大量自建/开源兼容服务在这一项上不一致，
        强制要求会白白失败。
        """
        self._ensure()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            data = await self._post(payload)
        except LLMError as exc:
            if not (json_mode and exc.retriable and exc.status in (400, 422)):
                raise
            if not looks_like_json_mode_unsupported(str(exc)):
                raise
            logger.info("服务端不支持 response_format，去掉该参数重试")
            payload.pop("response_format", None)
            data = await self._post(payload)

        return _first_message(data)

    async def vision(
        self,
        image_b64: str,
        mime: str,
        prompt: str,
        *,
        json_mode: bool = True,
    ) -> str:
        """把图片 + 提示词发给视觉模型。"""
        self._ensure(vision=True)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                    },
                ],
            }
        ]
        model = self.vision_model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            data = await self._post(payload)
        except LLMError as exc:
            if not (json_mode and exc.retriable and exc.status in (400, 422)):
                raise
            logger.info("视觉接口不支持 response_format，去掉该参数重试")
            payload.pop("response_format", None)
            data = await self._post(payload)

        return _first_message(data)

    async def ping(self) -> dict[str, Any]:
        """连通性自检：发一个最小请求，把结果翻译成可读状态。"""
        if not self.enabled:
            return {
                "ok": False,
                "configured": False,
                "message": "未配置大模型（缺 LLM_API_KEY / Base URL / 模型名），"
                           "当前运行在规则引擎模式，功能完整可用。",
            }
        try:
            text = await self.chat(
                [{"role": "user", "content": "只回复两个字：正常"}],
                max_tokens=16,
            )
        except LLMError as exc:
            return {"ok": False, "configured": True, "message": str(exc), "retriable": exc.retriable}
        return {
            "ok": True,
            "configured": True,
            "model": self.model,
            "reply": _shorten(text, 60),
            "message": f"连通正常，模型：{self.model}",
        }


def _first_message(data: dict[str, Any]) -> str:
    """从响应体里取第一条消息的文本内容，并处理各种非标准结构。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        error = data.get("error")
        if isinstance(error, dict):
            raise LLMError(f"服务端返回错误：{_shorten(str(error.get('message', error)))}")
        raise LLMError(f"响应里没有 choices 字段：{_shorten(json.dumps(data, ensure_ascii=False))}")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    # 少数实现返回分段内容数组
    if isinstance(content, list):
        parts = [seg.get("text", "") for seg in content if isinstance(seg, dict)]
        return "".join(parts)
    # 推理模型可能把内容放在 reasoning_content
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning
    return ""


def encode_image(data: bytes) -> str:
    """把图片字节编码成 base64（视觉接口需要）。"""
    return base64.b64encode(data).decode("ascii")


__all__ = [
    "LLMError",
    "LLMNotConfigured",
    "LLMClient",
    "extract_json",
    "classify_http_error",
    "looks_like_json_mode_unsupported",
    "encode_image",
]
