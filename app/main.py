"""FastAPI 接口层。

设计要点：
- **零配置可跑**：没配 Key 时所有接口依然可用，只是抽取走规则引擎，
  并在响应里明确标注 ``engine=rule`` 与降级原因；
- **降级一定要说清楚**：任何「少给了内容」的情况都会出现在 ``warnings`` 里，
  绝不让用户面对一份「看起来正常但少了一半」的结果；
- **SSE 只推真实阶段**：先解析、再抽取、再校验，每步完成才推送，
  不做假的进度条。
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import __version__
from .config import Settings, load_settings
from .examples import all_examples, example_path, get_example
from .extractor import Extractor, summarize_document_as_model
from .ir import Document
from .llm import LLMClient, LLMError, LLMNotConfigured
from .loaders import LoadError, load_document
from .schemas import (
    ConfigResponse,
    ExampleDetail,
    ExampleItem,
    ExtractRequest,
    ExtractionResult,
    FieldSpec,
    HealthResponse,
    ParseResponse,
    RevalidateRequest,
    StatsResponse,
    TablePreview,
    Template,
    TemplateSummary,
    UploadResponse,
)
from .store import FileNotFoundInStore, FileStore
from .templates import get_template, list_summaries
from .validators import run_rules

logger = logging.getLogger(__name__)

#: 允许上传的扩展名（只是第一道门，真正的类型判断看文件头）
ALLOWED_SUFFIXES = {
    ".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".md", ".markdown", ".log",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}

#: 正文预览的字符上限
TEXT_PREVIEW_LIMIT = 1500

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    settings.ensure_dirs()
    app.state.settings = settings
    app.state.store = FileStore(settings.uploads_dir, persist=settings.store_persist)
    app.state.llm = LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.resolved_base_url,
        model=settings.resolved_model,
        vision_model=settings.resolved_vision_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_timeout,
    )
    app.state.extractor = Extractor(settings, app.state.llm)
    app.state.stats = {"extractions_run": 0, "revalidations_run": 0}
    app.state.started_at = time.time()
    logger.info(
        "服务启动：引擎=%s 大模型=%s 视觉=%s",
        settings.active_engine,
        "已启用" if settings.llm_enabled else "未配置",
        "已启用" if settings.vision_enabled else "未配置",
    )
    yield


app = FastAPI(
    title="文档 / 表格 / 图像结构化抽取",
    description=(
        "把 PDF、Word、Excel、CSV、纯文本与图片变成带来源定位、带置信度、"
        "经过校验的结构化 JSON。规则优先、模型补漏、校验兜底。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def _configure_cors() -> None:
    settings = load_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_configure_cors()


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> FileStore:
    return request.app.state.store


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_extractor(request: Request) -> Extractor:
    return request.app.state.extractor


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse, tags=["元信息"])
def health(request: Request) -> HealthResponse:
    settings = get_settings(request)
    return HealthResponse(
        status="ok",
        version=__version__,
        llm_enabled=settings.llm_enabled,
        vision_enabled=settings.vision_enabled,
        templates=len(list_summaries()),
    )


@app.get("/api/config", response_model=ConfigResponse, tags=["元信息"])
def config(request: Request) -> ConfigResponse:
    settings = get_settings(request)
    described = settings.describe()
    return ConfigResponse(**described)  # type: ignore[arg-type]


@app.get("/api/stats", response_model=StatsResponse, tags=["元信息"])
def stats(request: Request) -> StatsResponse:
    store = get_store(request)
    llm = get_llm(request)
    counters = request.app.state.stats
    return StatsResponse(
        files_stored=store.count(),
        extractions_run=counters.get("extractions_run", 0),
        llm_calls=llm.calls,
        llm_failures=llm.failures,
        uptime_seconds=round(time.time() - request.app.state.started_at, 2),
    )


@app.get("/api/templates", response_model=list[TemplateSummary], tags=["模板"])
def templates() -> list[TemplateSummary]:
    return list_summaries()


@app.get("/api/templates/{key}", response_model=Template, tags=["模板"])
def template_detail(key: str) -> Template:
    template = get_template(key)
    if template is None:
        raise HTTPException(status_code=404, detail=f"模板不存在：{key}")
    return template


# ---------------------------------------------------------------------------
# 示例
# ---------------------------------------------------------------------------


@app.get("/api/examples", response_model=list[ExampleItem], tags=["元信息"])
def examples() -> list[ExampleItem]:
    out: list[ExampleItem] = []
    for spec in all_examples():
        path = example_path(spec)
        out.append(
            ExampleItem(
                key=spec.key,
                label=spec.label,
                description=spec.description,
                kind=spec.filename.rsplit(".", 1)[-1],
                template=spec.template,
                size=path.stat().st_size if path.exists() else 0,
            )
        )
    return out


@app.get("/api/examples/{key}", response_model=ExampleDetail, tags=["元信息"])
def example_detail(key: str) -> ExampleDetail:
    spec = get_example(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"示例不存在：{key}")
    path = example_path(spec)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"示例文件 {spec.filename} 不存在，请先执行 python scripts/make_examples.py",
        )
    content = ""
    if spec.text_based:
        content = path.read_text(encoding="utf-8")
    kind = spec.filename.rsplit(".", 1)[-1]
    return ExampleDetail(
        key=spec.key,
        label=spec.label,
        kind=kind,
        template=spec.template,
        content=content,
    )


@app.post("/api/examples/{key}/load", response_model=UploadResponse, tags=["元信息"])
def load_example(request: Request, key: str) -> UploadResponse:
    """把示例文件载入工作区，返回 file_id（二进制示例必须走这条路）。"""
    spec = get_example(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"示例不存在：{key}")
    path = example_path(spec)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"示例文件 {spec.filename} 不存在，请先执行 python scripts/make_examples.py",
        )
    data = path.read_bytes()
    settings = get_settings(request)
    doc = _load_or_400(spec.filename, data, settings)
    record = get_store(request).save(
        spec.filename, data, kind=doc.kind, detail=str(doc.meta.get("detected_detail", "")),
        summary=summarize_document_as_model(doc).model_dump(),
    )
    return UploadResponse(
        file_id=record.file_id,
        name=record.name,
        size=record.size,
        doc=summarize_document_as_model(doc),
    )


# ---------------------------------------------------------------------------
# 文件管理
# ---------------------------------------------------------------------------


@app.post("/api/files", response_model=UploadResponse, tags=["文件"])
async def upload(request: Request, file: UploadFile = File(...)) -> UploadResponse:
    settings = get_settings(request)
    name = _safe_filename(file.filename or "upload")
    suffix = Path(name).suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {suffix}。支持：{'、'.join(sorted(ALLOWED_SUFFIXES))}",
        )

    data = await file.read()
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {len(data) / 1024 / 1024:.1f} MB 超过上限 {settings.max_upload_mb} MB",
        )

    doc = _load_or_400(name, data, settings)
    record = get_store(request).save(
        name, data, kind=doc.kind, detail=str(doc.meta.get("detected_detail", "")),
        summary=summarize_document_as_model(doc).model_dump(),
    )
    return UploadResponse(
        file_id=record.file_id, name=record.name, size=record.size,
        doc=summarize_document_as_model(doc),
    )


@app.get("/api/files", tags=["文件"])
def list_files(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "file_id": record.file_id,
            "name": record.name,
            "size": record.size,
            "kind": record.kind,
            "created_at": record.created_at,
        }
        for record in get_store(request).list_files()
    ]


@app.get("/api/files/{file_id}", tags=["文件"])
def file_detail(request: Request, file_id: str) -> dict[str, Any]:
    record = _get_record_or_404(request, file_id)
    return record.to_dict()


@app.delete("/api/files/{file_id}", tags=["文件"])
def delete_file(request: Request, file_id: str) -> dict[str, Any]:
    deleted = get_store(request).delete(file_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"deleted": True, "file_id": file_id}


# ---------------------------------------------------------------------------
# 解析（只看结构，不抽字段）
# ---------------------------------------------------------------------------


@app.post("/api/parse", response_model=ParseResponse, tags=["解析"])
async def parse(
    request: Request,
    file: UploadFile | None = File(None),
    text: str = Form(""),
    file_id: str = Form(""),
) -> ParseResponse:
    """只解析、不抽取。用来先看清「系统到底读到了什么」。

    三种输入方式（新上传的文件 / 已存在工作区的 ``file_id`` / 粘贴文本）
    共用同一条解析链路，返回的也永远是同一套结构 —— 前端因此不需要为
    「文件是不是刚上传的」写两套渲染逻辑。
    """
    settings = get_settings(request)
    if file is not None and (file.filename or ""):
        name = _safe_filename(file.filename or "upload")
        raw = await file.read()
        content = _load_or_400_bytes(name, raw, settings)
    elif file_id.strip():
        record = _get_record_or_404(request, file_id.strip())
        try:
            raw = get_store(request).get_bytes(file_id.strip())
        except FileNotFoundInStore as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        content = _load_or_400_bytes(record.name, raw, settings)
    elif text.strip():
        content = _wrap_text(text, settings)
    else:
        raise HTTPException(status_code=400, detail="请提供 file、file_id 或 text 其中之一")

    return ParseResponse(
        doc=summarize_document_as_model(content),
        text_preview=content.text[:TEXT_PREVIEW_LIMIT],
        tables=[_preview_table(t) for t in content.tables],
    )


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


@app.post("/api/extract", response_model=ExtractionResult, tags=["抽取"])
async def extract(request: Request, payload: ExtractRequest) -> ExtractionResult:
    doc, image, template = await _prepare(request, payload)
    extractor = get_extractor(request)
    try:
        result = await extractor.extract(doc, template, engine=payload.engine, image=image)
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.stats["extractions_run"] = request.app.state.stats.get("extractions_run", 0) + 1
    return result


@app.post("/api/extract/stream", tags=["抽取"])
async def extract_stream(request: Request, payload: ExtractRequest) -> StreamingResponse:
    """SSE 流式抽取。

    事件序列::

        start   -> 请求开始（引擎、模板、文档摘要）
        stage   -> 当前阶段与进度（parse / extract / validate）
        parsed  -> 解析完成（表格数量、是否扫描件、降级提示）
        fields  -> 字段抽取完成
        rules   -> 一致性校验完成
        done    -> 完整结果（ExtractionResult 的 JSON）
        error   -> 出错
        [DONE]  -> 流结束

    实现上没有引入「后台任务 + 队列」：这里的每个阶段都是**真的按顺序发生**的，
    直接在生成器里 yield 就够了。多引入一层队列只会让事件顺序变得难以推理。
    """
    doc, image, template = await _prepare(request, payload)
    settings = get_settings(request)
    extractor = get_extractor(request)
    engine = payload.engine or settings.active_engine

    def pack(event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield pack(
                "start",
                {
                    "engine": engine,
                    "engine_mode": engine,
                    "template": template.key,
                    "template_label": template.label,
                    "doc": summarize_document_as_model(doc).model_dump(),
                },
            )
            yield pack("stage", {"stage": "parse", "percent": 20, "message": "文档结构解析完成"})
            yield pack(
                "parsed",
                {
                    "kind": doc.kind,
                    "table_count": len(doc.tables),
                    "char_count": doc.char_count,
                    "needs_ocr": doc.needs_ocr,
                    "warnings": list(doc.warnings),
                },
            )

            yield pack("stage", {"stage": "extract", "percent": 55, "message": "字段抽取中"})
            result = await extractor.extract(doc, template, engine=payload.engine, image=image)
            request.app.state.stats["extractions_run"] = (
                request.app.state.stats.get("extractions_run", 0) + 1
            )
            yield pack(
                "fields",
                {
                    "filled": sum(1 for f in result.fields if f.value not in (None, "", [])),
                    "total": len(result.fields),
                    "rows": len(result.rows),
                    "engine": result.engine,
                },
            )

            yield pack("stage", {"stage": "validate", "percent": 90, "message": "一致性校验完成"})
            yield pack(
                "rules",
                {
                    "passed": sum(1 for r in result.rules if r.passed and not r.skipped),
                    "failed": sum(1 for r in result.rules if not r.passed and not r.skipped),
                    "skipped": sum(1 for r in result.rules if r.skipped),
                },
            )
            yield pack("done", result.model_dump())
        except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端拿到可读信息
            logger.exception("流式抽取失败")
            yield pack("error", {"error": str(exc), "type": type(exc).__name__})
        finally:
            yield "event: [DONE]\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/revalidate", tags=["校验"])
def revalidate(request: Request, payload: RevalidateRequest) -> dict[str, Any]:
    """只重跑跨字段校验：改容差、改规则时不用重新解析，也不用再调模型。

    这个接口能存在，本身就说明校验层是独立的 —— 它不依赖文档、不依赖模型，
    只依赖「字段值 + 明细行 + 规则」这三样东西。
    """
    field_map = {f.key: f for f in payload.fields}
    results = run_rules(payload.rules, field_map, payload.rows, tolerance=payload.tolerance)
    request.app.state.stats["revalidations_run"] = (
        request.app.state.stats.get("revalidations_run", 0) + 1
    )
    return {
        "rules": [r.model_dump() for r in results],
        "passed": sum(1 for r in results if r.passed and not r.skipped),
        "failed": sum(1 for r in results if not r.passed and not r.skipped),
        "skipped": sum(1 for r in results if r.skipped),
    }


@app.post("/api/llm/ping", tags=["元信息"])
async def llm_ping(request: Request) -> dict[str, Any]:
    return await get_llm(request).ping()


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>前端未构建</h1><p>缺少 web/index.html</p>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """没有 favicon，回 204 让浏览器安静下来。

    这里必须是 ``Response(status_code=204)`` 而不是 ``JSONResponse({}, status_code=204)``：
    204 按 HTTP 规范**不允许带 body**，而 ``JSONResponse`` 总会渲染出 ``{}``
    这 2 个字节。Starlette 对 204 不写 Content-Length，于是 uvicorn/httptools
    收到「body 比声明的 Content-Length 长」，在**每次页面加载**时都往日志里
    写一整篇 RuntimeError 堆栈 —— 而响应码还是 204，客户端完全看不出来。
    TestClient 对这种不合法响应是宽容的（照样返回 b"{}"），所以单测全绿、
    只有真的起服务才会发现。
    """
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

_UNSAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff.\-]+")


def _safe_filename(name: str) -> str:
    """清洗上传文件名：去掉路径分隔符与危险字符。

    ``../../.env`` 这种名字必须在这一步就被打掉，不能等到落盘时再说。
    """
    base = (name or "upload").replace("\\", "/").split("/")[-1]
    cleaned = _UNSAFE_NAME.sub("_", base).strip("._") or "upload"
    return cleaned[:120]


def _wrap_text(text: str, settings: Settings) -> Document:
    """把粘贴的文本包成 ``Document``（走与文件一致的解析链路）。"""
    return load_document("粘贴文本.txt", text.encode("utf-8"), settings)


def _load_or_400(name: str, data: bytes, settings: Settings) -> Document:
    try:
        return load_document(name, data, settings)
    except LoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _load_or_400_bytes(name: str, data: bytes, settings: Settings) -> Document:
    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {len(data) / 1024 / 1024:.1f} MB 超过上限 {settings.max_upload_mb} MB",
        )
    return _load_or_400(name, data, settings)


def _get_record_or_404(request: Request, file_id: str):
    try:
        return get_store(request).get_meta(file_id)
    except FileNotFoundInStore as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc


def _preview_table(table) -> TablePreview:
    return TablePreview(
        locator=table.locator,
        caption=table.caption,
        columns=list(table.columns),
        header_rows=table.header_rows,
        row_count=table.row_count,
        col_count=table.col_count,
        preview=table.to_preview(15),
        merged_cells=len(table.merges),
        truncated=table.truncated,
        page=table.page,
    )


async def _prepare(request: Request, payload: ExtractRequest) -> tuple[Document, tuple[bytes, str] | None, Template]:
    """把请求变成 ``(文档, 图片字节, 模板)``。三种输入方式共用这一条准备逻辑。"""
    settings = get_settings(request)
    image: tuple[bytes, str] | None = None

    if payload.file_id:
        store = get_store(request)
        record = _get_record_or_404(request, payload.file_id)
        try:
            data = store.get_bytes(payload.file_id)
        except FileNotFoundInStore as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        doc = _load_or_400_bytes(record.name, data, settings)
        if doc.kind == "image":
            mime = f"image/{(doc.meta.get('format') or 'png')}"
            if mime.endswith("jpg"):
                mime = "image/jpeg"
            image = (data, mime)
    elif payload.text.strip():
        doc = _wrap_text(payload.text, settings)
    else:
        raise HTTPException(status_code=400, detail="请提供 text 或 file_id 其中之一")

    return doc, image, _resolve_template(payload)


def _resolve_template(payload: ExtractRequest) -> Template:
    """取模板：内置模板，或由请求里的自定义字段现场拼一个。"""
    if payload.template == "custom":
        if not payload.custom_fields:
            raise HTTPException(status_code=400, detail="使用 custom 模板时必须提供 custom_fields")
        _check_duplicate_keys(payload.custom_fields)
        return Template(
            key="custom",
            label="自定义模板",
            description="由本次请求的字段定义临时构造。",
            fields=payload.custom_fields,
            rules=payload.custom_rules,
            builtin=False,
        )

    template = get_template(payload.template)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=f"模板不存在：{payload.template}。可用模板见 /api/templates",
        )
    if payload.custom_rules:
        template = template.model_copy(update={"rules": [*template.rules, *payload.custom_rules]})
    return template


def _check_duplicate_keys(fields: list[FieldSpec]) -> None:
    seen: set[str] = set()
    for spec in fields:
        if spec.key in seen:
            raise HTTPException(
                status_code=400, detail=f"自定义字段的 key 重复：{spec.key}（key 是输出 JSON 的键，必须唯一）"
            )
        seen.add(spec.key)


__all__ = ["app"]
