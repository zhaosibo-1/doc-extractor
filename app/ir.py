"""统一中间表示（IR）。

整个项目最重要的一条分层决策：**所有格式先落到同一套数据结构，抽取层完全不关心来源**。

    pdf ─┐
    docx ┤
    xlsx ┼─► Document(blocks + tables) ─► 抽取层 ─► 结构化 JSON
    csv  ┤
    图片 ─┘

好处有两个：
1. 抽取逻辑只写一遍，新增格式只是在 loaders 里加一个转换器；
2. 每种格式的「坑」（合并单元格、多级表头、扫描件没有文字层）都在这一层被处理掉，
   不会漏进抽取层变成莫名其妙的空字段。

另一条同样重要的约定：**任何降级都要写进 ``Document.warnings``**。
PDF 页数超限被截断、扫描件没有文字层、表格行数超限……都必须显式记录，
而不是悄悄少给数据 —— 悄无声息的降级是这类工具最容易被用户误信的失效模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: 块的类型。抽取层靠它决定用哪种策略（表格取单元格、段落取标签同行……）
BlockKind = Literal["heading", "paragraph", "list_item", "table", "image", "kv"]

#: 支持的文档类型
DocKind = Literal["pdf", "docx", "xlsx", "csv", "text", "image"]


@dataclass
class Cell:
    """表格里的一个单元格，保留原始坐标与合并信息。"""

    text: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1

    @property
    def is_merged(self) -> bool:
        return self.row_span > 1 or self.col_span > 1

    @property
    def ref(self) -> str:
        """Excel 风格的坐标，如 ``B3``。用于给前端做「来源定位」高亮。"""
        return f"{col_to_letters(self.col)}{self.row + 1}"


def col_to_letters(col: int) -> str:
    """把 0 基的列号转成 Excel 风格的字母（0 -> A，26 -> AA）。"""
    if col < 0:
        return "?"
    letters = ""
    n = col
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


@dataclass
class TableData:
    """一张表。

    ``matrix`` 是**归一化之后**的二维数组：
    - 合并单元格已经把值填满整个区域（不会只留左上角一个值、其余是空）；
    - 每一行长度一致（短的补空串），这样按列取值不会 IndexError。

    归一化放在这里做，是因为「合并单元格」和「参差不齐的行」是表格相关 bug 的两大来源。
    """

    matrix: list[list[str]] = field(default_factory=list)
    #: 表头占前几行（0 表示没有识别到表头）
    header_rows: int = 0
    #: 多级表头合并之后的列名（长度 = 列数）
    columns: list[str] = field(default_factory=list)
    #: 表头上方的标题行文字（如「销售明细表」），没有则为空
    caption: str = ""
    #: 来源定位，如 ``Sheet1!A1:D12`` / ``page 2 / table 1``
    locator: str = ""
    #: 页码（Excel/CSV 固定为 1）
    page: int = 1
    #: 表在文档中的序号
    index: int = 0
    #: 合并单元格的原始信息（(row, col, row_span, col_span)），用于前端展示
    merges: list[Cell] = field(default_factory=list)
    #: 是否因为行数超限被截断过
    truncated: bool = False
    #: 截断前的行数（未截断时等于 row_count）
    original_row_count: int = 0

    @property
    def row_count(self) -> int:
        return len(self.matrix)

    @property
    def col_count(self) -> int:
        return len(self.matrix[0]) if self.matrix else 0

    @property
    def body(self) -> list[list[str]]:
        """去掉表头之后的数据行。"""
        return self.matrix[self.header_rows :]

    def dict_rows(self) -> list[dict[str, str]]:
        """把数据行按列名转成字典列表；没有列名时用 ``列1 / 列2`` 兜底。

        列名重复是真实存在的（合并表头拆完之后尤其常见），
        所以这里对重复列名做去重后缀，避免后面的列把前面的覆盖掉。
        """
        if not self.matrix:
            return []
        names = list(self.columns)
        if len(names) != self.col_count:
            names = [f"列{i + 1}" for i in range(self.col_count)]

        seen: dict[str, int] = {}
        unique: list[str] = []
        for name in names:
            key = name or "列"
            if key in seen:
                seen[key] += 1
                unique.append(f"{key}_{seen[key]}")
            else:
                seen[key] = 0
                unique.append(key)

        out: list[dict[str, str]] = []
        for row in self.body:
            # 全空行要丢掉：它们在「按空行切分堆叠表格」之后通常不会出现，
            # 但手工构造的表格与未切分的输入里仍可能有，留着会变成一行空记录
            if not any(cell.strip() for cell in row):
                continue
            out.append({unique[i]: (row[i] if i < len(row) else "") for i in range(len(unique))})
        return out

    def to_preview(self, limit: int = 20) -> list[list[str]]:
        """截断后的预览数据，用于接口返回（避免把整张表塞进 JSON）。"""
        return [row[:] for row in self.matrix[:limit]]


@dataclass
class Block:
    """文档里的一个内容块（标题 / 段落 / 列表项 / 表格 / 图片）。"""

    kind: BlockKind
    text: str = ""
    level: int = 0
    page: int = 1
    #: 表格块才会用到
    table: TableData | None = None
    #: 图片块：图片在文档里的序号（0 基）
    image_index: int = -1

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and self.table is None


@dataclass
class Document:
    """一份解析完成的文档。"""

    name: str
    kind: DocKind
    pages: int = 1
    blocks: list[Block] = field(default_factory=list)
    #: 降级 / 截断 / 异常情况的显式记录（**必须展示给用户**）
    warnings: list[str] = field(default_factory=list)
    #: 元信息：文件大小、字数、是否扫描件、编码……
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def tables(self) -> list[TableData]:
        return [b.table for b in self.blocks if b.table is not None]  # type: ignore[misc]

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks)

    @property
    def needs_ocr(self) -> bool:
        """是否是「有页面但没有文字层」的文档（典型：扫描件 PDF）。

        这个标记很关键：如果不说，用户只会看到一堆空字段，以为工具坏了。
        """
        return bool(self.meta.get("needs_ocr"))

    @property
    def text(self) -> str:
        """把所有块的文本拼起来，作为送给大模型的纯文本视图。"""
        parts: list[str] = []
        for block in self.blocks:
            if block.kind == "table" and block.table is not None:
                parts.append(_table_to_text(block.table))
            elif block.text.strip():
                parts.append(block.text)
        return "\n".join(parts)

    def text_with_pages(self, *, max_chars: int = 12000) -> str:
        """带页码标注的文本视图，便于模型输出 ``page`` 字段做来源定位。"""
        parts: list[str] = []
        used = 0
        current_page = -1
        for block in self.blocks:
            chunk = block.text.strip()
            if block.kind == "table" and block.table is not None:
                chunk = _table_to_text(block.table)
            if not chunk:
                continue
            if block.page != current_page:
                head = f"\n===== 第 {block.page} 页 ====="
                parts.append(head)
                used += len(head)
                current_page = block.page
            if used + len(chunk) > max_chars:
                parts.append("……（后续内容因长度限制已省略）")
                break
            parts.append(chunk)
            used += len(chunk) + 1
        return "\n".join(parts)

    def add_warning(self, message: str) -> None:
        """记录一条降级说明（去重）。"""
        if message not in self.warnings:
            self.warnings.append(message)


def _table_to_text(table: TableData) -> str:
    """把表格渲染成 Markdown 风格文本。

    Markdown 表格是模型最容易正确理解的行式表示，比直接塞 JSON 数组稳定得多。
    没有列名时退回「用 ``|`` 连接的原始行」。
    """
    if not table.matrix:
        return ""
    lines: list[str] = []
    if table.columns and len(table.columns) == table.col_count:
        lines.append("| " + " | ".join(table.columns) + " |")
        lines.append("| " + " | ".join("---" for _ in table.columns) + " |")
        rows = table.body
    else:
        rows = table.matrix
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
    return "\n".join(lines)


__all__ = [
    "BlockKind",
    "DocKind",
    "Cell",
    "TableData",
    "Block",
    "Document",
    "col_to_letters",
]
