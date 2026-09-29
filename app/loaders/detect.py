"""文件类型识别。

**不信任扩展名**：用户把 `.xlsx` 改成 `.txt`（或反过来）是家常便饭，
真正的类型必须看文件头（magic bytes）。

这同时也是一个安全边界：不做这一步的话，一个可执行文件被命名成 `.txt`
就会走进文本解析分支，把二进制内容当成字符串处理。
"""

from __future__ import annotations

import io
import zipfile

from ..ir import DocKind

#: 图片格式的文件头
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)

#: 文本类扩展名（只在内容也确实是文本时才会被采用）
_TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".json", ".xml", ".html", ".htm", ".yaml", ".yml"}
_CSV_EXTS = {".csv", ".tsv"}

#: 编码尝试顺序。UTF-8 BOM 必须**优先**处理：
#: 用普通 utf-8 解 BOM 文件会在第一个字段名前留一个不可见字符，
#: 那个字段将永远匹配不上任何东西（这个坑项目 1 已经踩过一次）。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")


class UnsupportedFormat(ValueError):
    """无法识别的文件格式。消息里会带上可操作的建议。"""


def sniff_kind(name: str, data: bytes) -> tuple[DocKind, str]:
    """识别文件类型，返回 ``(DocKind, 细节说明)``。

    细节说明会展示在界面上（例如 ``pdf`` / ``xlsx`` / ``png``），
    方便用户确认「系统认为这是什么」。
    """
    head = data[:16]
    if not head:
        raise UnsupportedFormat("文件是空的（0 字节），无法解析。")

    if head.startswith(b"%PDF-"):
        return "pdf", "pdf"

    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(data)

    for magic, label in _IMAGE_MAGIC:
        if head.startswith(magic):
            return "image", label
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "webp"

    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2 复合文档：.doc / .xls 等老格式
        raise UnsupportedFormat(
            "识别为旧版 Office 二进制格式（.doc / .xls）。"
            "请在 Office / WPS 里另存为 .docx / .xlsx 后重新上传。"
        )

    ext = _ext(name)
    if _looks_binary(data):
        raise UnsupportedFormat(
            f"文件 {name or '(未命名)'} 看起来是二进制内容，"
            "支持的格式为 PDF / DOCX / XLSX / CSV / TXT / MD / 图片。"
        )

    if ext in _CSV_EXTS:
        return "csv", "csv" if ext == ".csv" else "tsv"
    return "text", "text"


def _sniff_zip(data: bytes) -> tuple[DocKind, str]:
    """ZIP 容器（docx / xlsx / pptx）需要看内部目录结构才能区分。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat("文件看起来是 ZIP，但已损坏，无法读取。") from exc

    if "word/document.xml" in names:
        return "docx", "docx"
    if "xl/workbook.xml" in names:
        return "xlsx", "xlsx"
    if any(n.startswith("ppt/") for n in names):
        raise UnsupportedFormat(
            "识别为 PPTX。本项目专注文档 / 表格 / 图像抽取，暂不支持演示文稿；"
            "可先把要抽取的内容另存为 docx 或 xlsx。"
        )
    raise UnsupportedFormat("这是一个普通的 ZIP 压缩包，不是 Office 文档。")


def _ext(name: str) -> str:
    lowered = (name or "").lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot >= 0 else ""


def _looks_binary(data: bytes, sample: int = 4096) -> bool:
    """用 NUL 字节判断是不是二进制内容。

    真实文本文件（即使编码是 GBK）极少含 NUL。
    """
    chunk = data[:sample]
    return b"\x00" in chunk


def decode_text(data: bytes) -> tuple[str, str]:
    """按候选编码解码文本，返回 ``(文本, 实际使用的编码)``。

    最后一定会成功：``latin-1`` 能把任意字节序列解出来。
    它是兜底而非正解 —— 但如果前面几种都失败了，给出一份乱码
    也好过直接抛异常让用户拿不到任何结果。
    """
    # 先快速试 UTF-8 系列（含 BOM），命中率最高
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1(replace)"


def has_text_layer(pages: list[str], *, min_chars: int = 20) -> bool:
    """判断解析出来的 PDF 是否有文字层。

    扫描件 PDF 会「有 N 页」但一页文字都提取不出来。此时必须显式标记
    ``needs_ocr``，否则用户只会看到满屏空字段，以为工具坏了。
    """
    total = sum(len(page.strip()) for page in pages)
    return total >= min_chars


__all__ = [
    "UnsupportedFormat",
    "sniff_kind",
    "decode_text",
    "has_text_layer",
]
