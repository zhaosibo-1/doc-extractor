"""PDF 加载器。

PDF 是最难的一个，因为它根本不是为「结构化」设计的 —— 它只记录「在坐标 (x,y) 画这串字」。
这里用两个库分工，各取所长：

- **pypdf** 收文字：快、稳，适合大文件；
- **pdfplumber** 收表格：它能把线条与空白推断成单元格网格，还能拿到单元格的坐标，
  从而**反推出合并单元格**（同一个 bbox 出现在多个网格位置上）。

另外两件必须做的事：
1. **加密 PDF**：先试空密码解密；失败要明确报「需要密码」，不能给一个空结果。
2. **扫描件检测**：有页面但提不出文字 → 标记 ``needs_ocr`` 并给出可操作建议。
   这是 PDF 抽取里最常见的「工具看起来坏了」的场景，必须解释清楚。
"""

from __future__ import annotations

import io

from ..ir import Block, Cell, Document
from ..textutil import collapse_ws
from .table_utils import build_tables
from .text_loader import looks_like_heading


class PdfDependencyError(RuntimeError):
    """缺少 PDF 解析依赖。"""


class PdfNeedsPassword(RuntimeError):
    """PDF 被加密，无法在没有密码的情况下解析。"""


def _require_pypdf():
    try:
        from pypdf import PdfReader  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise PdfDependencyError(
            "解析 pdf 需要 pypdf，请执行：pip install -r requirements.txt"
        ) from exc
    return PdfReader


def load_pdf(name: str, data: bytes, *, max_pages: int = 60, max_rows: int = 500, max_header_rows: int = 3) -> Document:
    """解析 PDF：逐页产出文本块与表格块。"""
    PdfReader = _require_pypdf()

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise PdfDependencyError(f"无法打开 pdf 文件：{exc}") from exc

    if reader.is_encrypted:
        # 很多 PDF 的「权限密码」是空的，先试一下
        try:
            opened = reader.decrypt("")
        except Exception:
            opened = 0
        if not opened:
            raise PdfNeedsPassword(
                "该 PDF 已加密，需要密码才能解析。请提供未加密的版本，"
                "或在 PDF 阅读器里「另存为」去掉密码保护。"
            )

    try:
        total_pages = len(reader.pages)
    except Exception as exc:
        raise PdfDependencyError(f"无法读取 pdf 页数：{exc}") from exc

    doc = Document(name=name, kind="pdf", pages=total_pages)
    doc.meta["page_count"] = total_pages
    doc.meta["encrypted"] = bool(reader.is_encrypted)

    limit = min(total_pages, max_pages)
    if total_pages > max_pages:
        doc.add_warning(
            f"PDF 共 {total_pages} 页，超过单次解析上限 {max_pages} 页，仅解析前 {max_pages} 页。"
        )

    page_texts: list[str] = []
    for index in range(limit):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception as exc:  # 个别页损坏不应该让整个文件失败
            text = ""
            doc.add_warning(f"第 {index + 1} 页文字提取失败：{exc}")
        page_texts.append(text)
        for block in _text_to_blocks(text, page=index + 1):
            doc.blocks.append(block)

    tables, table_warnings = _extract_tables(data, limit=limit, max_rows=max_rows, max_header_rows=max_header_rows)
    doc.blocks.extend(tables)
    for message in table_warnings:
        doc.add_warning(message)

    # 按页码排序，让阅读顺序与原文一致（表格块插入后会乱序）
    doc.blocks.sort(key=lambda b: (b.page, 0 if b.kind != "table" else 1))

    total_chars = sum(len(t.strip()) for t in page_texts)
    doc.meta["char_count"] = total_chars
    if total_chars < 20 and limit > 0:
        doc.meta["needs_ocr"] = True
        doc.add_warning(
            "这份 PDF 没有提取到文字，通常是扫描件（图片型 PDF），需要 OCR 或视觉模型才能读取。"
            "可以改用图片上传，或先做 OCR 转成可复制文字的 PDF。"
        )
    elif total_chars < 200 and limit > 2:
        doc.add_warning(f"这份 PDF 只提取到 {total_chars} 个字符，可能是以图片为主的文件。")

    return doc


def _text_to_blocks(text: str, page: int) -> list[Block]:
    """把一页的文字切成块。

    PDF 的换行多数是**排版换行**而非语义换行，所以这里以「行」为最小单位，
    只做标题/列表的粗分类，不做句子合并 —— 合并错了会丢信息，不合并只是多几行。
    """
    blocks: list[Block] = []
    for raw in (text or "").split("\n"):
        line = collapse_ws(raw)
        if not line:
            continue
        if looks_like_heading(line):
            blocks.append(Block(kind="heading", text=line, level=1, page=page))
        elif len(line) <= 200:
            blocks.append(Block(kind="paragraph", text=line, page=page))
        else:
            blocks.append(Block(kind="paragraph", text=line, page=page))
    return blocks


def _extract_tables(
    data: bytes, *, limit: int, max_rows: int, max_header_rows: int
) -> tuple[list[Block], list[str]]:
    """用 pdfplumber 抽表格。缺库或失败时降级为「只收文字」，并明确告知。"""
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError:
        return [], [
            "未安装 pdfplumber，本次仅提取了 PDF 文字，表格未解析。"
            "执行 pip install -r requirements.txt 后即可解析 PDF 表格。"
        ]

    blocks: list[Block] = []
    warnings: list[str] = []
    failed_pages: list[int] = []

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for index in range(min(limit, len(pdf.pages))):
                page = pdf.pages[index]
                try:
                    found = page.find_tables()
                except Exception:
                    failed_pages.append(index + 1)
                    continue
                for t_index, table in enumerate(found):
                    rows = table.extract() or []
                    normalised = [["" if cell is None else str(cell) for cell in row] for row in rows]
                    if not any(any(cell.strip() for cell in row) for row in normalised):
                        continue
                    merges = _detect_merges(table)
                    built_list = build_tables(
                        normalised,
                        merges=merges,
                        locator=f"第 {index + 1} 页 / 表格 {t_index + 1}",
                        page=index + 1,
                        max_rows=max_rows,
                        max_header_rows=max_header_rows,
                    )
                    for built in built_list:
                        if built.row_count >= max_rows:
                            warnings.append(
                                f"第 {index + 1} 页的表格行数达到上限 {max_rows}，后续行已截断。"
                            )
                        blocks.append(Block(kind="table", table=built, page=index + 1))
    except Exception as exc:
        return blocks, warnings + [f"PDF 表格解析失败（{exc}），已降级为只提取文字。"]

    if failed_pages:
        warnings.append(
            "以下页面的表格解析失败，仅提取了文字：" + "、".join(str(p) for p in failed_pages[:10])
        )
    return blocks, warnings


def _detect_merges(table) -> list[Cell]:
    """从 pdfplumber 的网格结构反推合并单元格。

    思路：把每个网格位置对应的单元格 bbox 收集起来，**同一个 bbox 出现在多个位置**
    就说明这些位置是同一个被合并的单元格。这样横向与纵向合并能一起识别出来。

    pdfplumber 的内部结构在不同版本上有细微差异，所以整个过程是尽力而为：
    拿不到就返回空列表（不填充），不会让解析失败。
    """
    try:
        rows = list(getattr(table, "rows", []) or [])
    except Exception:
        return []
    if not rows:
        return []

    positions: dict[tuple, list[tuple[int, int]]] = {}
    for r, row in enumerate(rows):
        cells = getattr(row, "cells", None)
        if not cells:
            continue
        for c, bbox in enumerate(cells):
            if bbox is None:
                continue
            key = tuple(round(float(v), 2) for v in bbox)
            positions.setdefault(key, []).append((r, c))

    merges: list[Cell] = []
    for coords in positions.values():
        if len(coords) <= 1:
            continue
        rows_idx = [r for r, _ in coords]
        cols_idx = [c for _, c in coords]
        merges.append(
            Cell(
                text="",
                row=min(rows_idx),
                col=min(cols_idx),
                row_span=max(rows_idx) - min(rows_idx) + 1,
                col_span=max(cols_idx) - min(cols_idx) + 1,
            )
        )
    return merges


__all__ = [
    "PdfDependencyError",
    "PdfNeedsPassword",
    "load_pdf",
]
