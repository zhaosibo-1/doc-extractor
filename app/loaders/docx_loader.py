"""DOCX 加载器。

Word 文档有两个必须处理好的点：

1. **顺序**：``document.paragraphs`` 与 ``document.tables`` 是两个独立列表，
   分别取会把「段落 → 表格 → 段落」的原始顺序打乱，
   而抽取层恰恰依赖顺序（比如「合计」通常出现在明细表之后）。
   这里改为遍历底层 XML 子元素，还原真实顺序。

2. **合并单元格**：``row.cells`` 对横向合并会返回重复的 ``_tc`` 引用。
   如果按引用去重，纵向合并的续行又会被误判成「同一个格子」。
   正确做法是把每个 ``_tc`` 的全部出现位置收集起来，
   还原出 ``(min_row, min_col, max_row, max_col)`` 再用统一的填充逻辑处理 ——
   这样横向、纵向合并都走同一条代码路径。
"""

from __future__ import annotations

import io
from collections import defaultdict

from ..ir import Block, Cell, Document
from ..textutil import clean_cell, collapse_ws
from .table_utils import build_tables


class DocxDependencyError(RuntimeError):
    """缺少 python-docx。"""


def _require_docx():
    try:
        import docx  # noqa: PLC0415
        from docx.oxml.ns import qn  # noqa: PLC0415
        from docx.table import Table  # noqa: PLC0415
        from docx.text.paragraph import Paragraph  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise DocxDependencyError(
            "解析 docx 需要 python-docx，请执行：pip install -r requirements.txt"
        ) from exc
    return docx, qn, Table, Paragraph


def load_docx(name: str, data: bytes, *, max_rows: int = 500, max_header_rows: int = 3) -> Document:
    """解析 DOCX：按真实顺序产出段落块与表格块。"""
    docx, qn, Table, Paragraph = _require_docx()

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise DocxDependencyError(f"无法打开 docx 文件：{exc}") from exc

    doc = Document(name=name, kind="docx", pages=1)
    _collect_props(document, doc)

    blocks: list[Block] = []
    table_index = 0
    nested_tables = 0

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = collapse_ws(paragraph.text)
            if not text:
                continue
            level = _heading_level(paragraph)
            blocks.append(Block(kind="heading" if level else "paragraph", text=text, level=level))
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows, merges, nested = _table_to_rows(table)
            nested_tables += nested
            if not any(any(cell for cell in row) for row in rows):
                continue
            built_list = build_tables(
                rows,
                merges=merges,
                locator=f"表格 {table_index + 1}",
                page=1,
                max_rows=max_rows,
                max_header_rows=max_header_rows,
            )
            if len(built_list) > 1:
                doc.add_warning(
                    f"第 {table_index + 1} 张 Word 表格里检测到 {len(built_list)} 张上下堆叠的表格，已分别解析。"
                )
            for built in built_list:
                blocks.append(Block(kind="table", table=built))
                table_index += 1

    doc.blocks = blocks
    doc.meta["table_count"] = table_index
    if nested_tables:
        doc.add_warning(
            f"检测到 {nested_tables} 处嵌套表格（表格里还有表格），"
            "已按外层表格解析，内层内容会合并进对应单元格。"
        )
    if not blocks:
        doc.add_warning("文档里没有解析到任何文字或表格内容。")
    return doc


def _heading_level(paragraph) -> int:
    """从段落样式名推断标题级别。

    ``Heading 1`` / ``标题 1`` / ``2. 数据`` 这类编号样式都算标题。
    """
    style_name = ""
    try:
        style_name = paragraph.style.name or ""
    except Exception:  # 样式表损坏时 python-docx 会抛异常
        style_name = ""

    lowered = style_name.lower()
    for prefix in ("heading", "标题", "title"):
        if lowered.startswith(prefix):
            digits = "".join(ch for ch in style_name if ch.isdigit())
            return int(digits) if digits else 1
    if style_name.lower() == "subtitle":
        return 2
    return 0


def _table_to_rows(table) -> tuple[list[list[str]], list[Cell], int]:
    """把 docx 表格转成 ``(二维数组, 合并信息, 嵌套表格数)``。

    这里有一个**必须踩过才知道的坑**：判断「两个格子是不是同一个合并单元格」
    不能直接用 ``id(cell._tc)``。

    ``cell._tc`` 拿到的是 lxml 的元素代理对象，而 lxml 只为**仍然存活**的对象
    维持「同一个节点 → 同一个代理对象」这一保证。一旦某个代理被垃圾回收，
    它的地址就可能被后面新建的代理复用 —— 于是 ``id()`` 会把两个毫不相干的
    单元格判成同一个格子，后者的值被前者的值覆盖掉。

    表现极具迷惑性：一张干干净净的 4×4 表格（产品 / 规格 / 数量 / 金额），
    解析出来第 3、4 行会变成第 1、2 行值的错乱重排，看起来像是「这份 Word 表格
    本身就是坏的」，实际是合并检测误判 —— 而错误的值**看起来完全合法**，
    校验层也发现不了。

    修法很小，但必须做对：用 ``keep_alive`` 列表持有每一个代理对象的强引用，
    保证它们在一轮解析期间都活着。只要代理不会被回收，``id()`` 就重新变成
    稳定的身份标识 —— 既不会误判合并，也仍然能正确识别真正的合并单元格
    （lxml 对同一个节点的多次访问返回的是同一个代理对象）。
    """
    grid: list[list[object]] = []
    positions: dict[int, list[tuple[int, int]]] = defaultdict(list)
    texts: dict[int, str] = {}
    #: 强引用容器 —— 没有它，下面每次 id() 都可能撞车（见 docstring）。
    #: 这里的 ``keep_alive`` 不参与任何计算，它的唯一作用就是「不让自己被回收」，
    #: 所以刻意不做成局部临时变量。
    keep_alive: list[object] = []
    nested = 0

    for r, row in enumerate(table.rows):
        row_cells = list(row.cells)
        grid.append([None] * len(row_cells))
        for c, cell in enumerate(row_cells):
            tc = cell._tc
            keep_alive.append(tc)
            key = id(tc)
            positions[key].append((r, c))
            if key not in texts:
                texts[key] = _cell_text(cell)
                nested += len(getattr(cell, "tables", []) or [])
            grid[r][c] = texts[key]

    merges: list[Cell] = []
    for key, coords in positions.items():
        if len(coords) <= 1:
            continue
        rows_idx = [r for r, _ in coords]
        cols_idx = [c for _, c in coords]
        merges.append(
            Cell(
                text=texts.get(key, ""),
                row=min(rows_idx),
                col=min(cols_idx),
                row_span=max(rows_idx) - min(rows_idx) + 1,
                col_span=max(cols_idx) - min(cols_idx) + 1,
            )
        )

    rows = [[("" if v is None else str(v)) for v in row] for row in grid]
    return rows, merges, nested


def _cell_text(cell) -> str:
    """单元格文本：多个段落用空格连接（换行会破坏「一行一条记录」的假设）。"""
    parts = [collapse_ws(p.text) for p in cell.paragraphs]
    return clean_cell(" ".join(p for p in parts if p))


def _collect_props(document, doc: Document) -> None:
    """把文档属性收进 meta（作者、创建时间等，有助于前端展示「这是什么文件」）。"""
    try:
        props = document.core_properties
    except Exception:
        return
    for key, value in (
        ("author", props.author),
        ("title", props.title),
        ("created", props.created),
        ("modified", props.modified),
    ):
        if value:
            doc.meta[key] = str(value)


__all__ = ["DocxDependencyError", "load_docx"]
