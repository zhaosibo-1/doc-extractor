"""加载器：把任意格式的文件转成统一的 :class:`~app.ir.Document`。

对外只有一个入口 :func:`load_document`，内部按文件**真实类型**（文件头，
不是扩展名）分发。抽取层因此完全不需要知道输入是 PDF 还是 Excel。

分发之后还会统一跑一遍 :func:`~app.loaders.normalize.normalize_document` ——
「跨格式必须一致」的整理一律放在那一步，而不是让每个加载器各写一遍。
"""

from __future__ import annotations

from ..config import Settings
from ..ir import Document
from .detect import UnsupportedFormat, decode_text, sniff_kind
from .docx_loader import DocxDependencyError, load_docx
from .image_loader import ImageDependencyError, InvalidImage, load_image
from .normalize import normalize_document
from .pdf_loader import PdfDependencyError, PdfNeedsPassword, load_pdf
from .tabular import XlsxDependencyError, load_xlsx
from .text_loader import load_csv, load_text


class LoadError(RuntimeError):
    """加载失败（依赖缺失 / 格式不支持 / 文件损坏 / 需要密码）。"""


#: 各类可预期的失败都归一成 LoadError，让接口层只需要处理一种异常。
_EXPECTED = (
    UnsupportedFormat,
    XlsxDependencyError,
    DocxDependencyError,
    PdfDependencyError,
    PdfNeedsPassword,
    ImageDependencyError,
    InvalidImage,
)


def load_document(name: str, data: bytes, settings: Settings, *, kind_hint: str = "") -> Document:
    """解析文件，返回统一的 :class:`Document`。

    ``kind_hint`` 允许调用方强制指定类型（例如用户手动选择了「这是图片」），
    但**仍然会走一次文件头校验** —— 强行指定只是给误判的扩展名一个补救机会，
    而不是绕过安全检查。
    """
    if not data:
        raise LoadError("文件内容为空，无法解析。")

    try:
        kind, detail = sniff_kind(name, data)
    except UnsupportedFormat as exc:
        raise LoadError(str(exc)) from exc

    if kind_hint and kind_hint != kind:
        # 提示类型与真实类型不一致时以**真实类型**为准，但把差异告知调用方
        pass
    doc: Document
    try:
        if kind == "pdf":
            doc = load_pdf(
                name,
                data,
                max_pages=settings.pdf_max_pages,
                max_rows=settings.table_max_rows,
                max_header_rows=settings.header_max_rows,
            )
        elif kind == "xlsx":
            doc = load_xlsx(
                name,
                data,
                max_rows=settings.table_max_rows,
                max_header_rows=settings.header_max_rows,
            )
        elif kind == "docx":
            doc = load_docx(
                name,
                data,
                max_rows=settings.table_max_rows,
                max_header_rows=settings.header_max_rows,
            )
        elif kind == "csv":
            doc = load_csv(name, data)
        elif kind == "image":
            doc = load_image(name, data)
        else:
            doc = load_text(name, data)
    except _EXPECTED as exc:
        raise LoadError(str(exc)) from exc
    except RecursionError as exc:  # 恶意构造的嵌套文档
        raise LoadError("文档结构异常（嵌套过深），无法解析。") from exc

    # 跨格式归一化：把所有格式都应该一致的东西（如键值行判定）在这里一次做完。
    # 放在分发之后、返回之前，是这条链路上唯一的必经之处。
    normalize_document(doc)

    doc.meta.setdefault("detected_kind", kind)
    doc.meta["detected_detail"] = detail
    doc.meta["bytes"] = len(data)
    doc.meta["name"] = name
    return doc


def summarize_document(doc: Document) -> dict[str, object]:
    """给接口层用的文档摘要（不含正文，避免响应过大）。"""
    return {
        "name": doc.name,
        "kind": doc.kind,
        "detail": doc.meta.get("detected_detail", doc.kind),
        "pages": doc.pages,
        "char_count": doc.char_count,
        "table_count": len(doc.tables),
        "block_count": len(doc.blocks),
        "needs_ocr": doc.needs_ocr,
        "encoding": doc.meta.get("encoding", ""),
        "warnings": list(doc.warnings),
    }


__all__ = [
    "LoadError",
    "load_document",
    "summarize_document",
    "normalize_document",
    "sniff_kind",
    "decode_text",
    "UnsupportedFormat",
    # 各加载器的专属异常，便于测试与上层精确捕获
    "XlsxDependencyError",
    "DocxDependencyError",
    "PdfDependencyError",
    "PdfNeedsPassword",
    "ImageDependencyError",
    "InvalidImage",
]
