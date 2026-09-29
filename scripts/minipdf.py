"""极简 PDF 生成器（只为了造测试 / 演示用的 PDF）。

为什么不用 reportlab / fpdf？
----------------------------
PDF 相关测试需要的只是「一份有文字、有表格线、结构合法的 PDF」。
为了这个往依赖里塞一个完整排版引擎，会让 CI 安装变慢、也增加了供应链面。
PDF 的 1.4 基础语法其实很小：对象 + 交叉引用表 + 内容流，手写一个够用的版本大约 150 行。

能力边界（刻意画得很窄）
------------------------
- 只用**标准字体 Helvetica + WinAnsi 编码**，因此**只能写 Latin-1 字符**。
  写中文需要嵌入 TrueType 子集并生成 ToUnicode CMap，不是这个工具的目标。
  传入非 Latin-1 字符会直接抛 :class:`UnicodeEncodeError`，
  而不是悄悄替换成 `?` —— 静默改字会让测试断言变得毫无意义。
- 支持：文字、直线、矩形、带边框的文字表格。

中文 PDF 的测试改用真实 PDF（见 examples/ 与 tests 里的说明）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

PAGE_WIDTH = 595  # A4 @72dpi
PAGE_HEIGHT = 842
LINE_WIDTH = 0.8


class Page:
    """一页 PDF 的内容流构造器。坐标原点在**左下角**（PDF 原生约定）。"""

    def __init__(self, width: int = PAGE_WIDTH, height: int = PAGE_HEIGHT) -> None:
        self.width = width
        self.height = height
        self.ops: list[str] = []

    # -- 图元 -------------------------------------------------------------

    def text(self, x: float, y: float, content: str, size: float = 10.0) -> None:
        """在 ``(x, y)`` 写一段文字（y 为基线位置）。"""
        self.ops.append(f"BT /F1 {size:.2f} Tf {x:.2f} {y:.2f} Td {_pdf_string(content)} Tj ET")

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float = LINE_WIDTH) -> None:
        self.ops.append(f"{width:.2f} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S")

    def rect(self, x: float, y: float, w: float, h: float, width: float = LINE_WIDTH) -> None:
        self.ops.append(f"{width:.2f} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    # -- 复合图元 ---------------------------------------------------------

    def table(
        self,
        x: float,
        y_top: float,
        col_widths: list[float],
        rows: list[list[str]],
        *,
        row_height: float = 20.0,
        font_size: float = 9.0,
        padding: float = 4.0,
    ) -> float:
        """画一张带边框线的表格，返回最后一行的下边界 y 坐标。

        表格线是 pdfplumber 识别表格的依据 —— 没有线的表格在 PDF 里
        只是一堆散落的文字，任何解析器都只能猜。
        """
        total_width = sum(col_widths)
        height = row_height * len(rows)

        # 外框
        self.rect(x, y_top - height, total_width, height)
        # 横线
        for i in range(1, len(rows)):
            y = y_top - row_height * i
            self.line(x, y, x + total_width, y)
        # 竖线
        acc = x
        for width in col_widths[:-1]:
            acc += width
            self.line(acc, y_top, acc, y_top - height)

        # 单元格文字（垂直居中）
        for r, row in enumerate(rows):
            base_y = y_top - row_height * r - row_height + (row_height - font_size) / 2 + 1
            acc = x
            for c, cell in enumerate(row):
                if cell:
                    self.text(acc + padding, base_y, cell, size=font_size)
                acc += col_widths[c] if c < len(col_widths) else col_widths[-1]
        return y_top - height

    def render(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("latin-1")


def _pdf_string(content: str) -> str:
    """把字符串编码成 PDF 的 ``(...)`` 字面量。

    只支持 Latin-1：超出范围的字符直接报错，避免静默替换成 ``?``。
    """
    try:
        content.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise UnicodeEncodeError(
            exc.encoding,
            exc.object,
            exc.start,
            exc.end,
            "minipdf 只支持 Latin-1（标准 Helvetica 字体）。"
            "需要中文 PDF 请使用真实文件，或改走 docx / xlsx 格式。",
        ) from exc
    escaped = content.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return f"({escaped})"


@dataclass
class MiniPdf:
    """多页 PDF 组装器。"""

    pages: list[Page] = field(default_factory=list)
    width: int = PAGE_WIDTH
    height: int = PAGE_HEIGHT

    def add_page(self, width: int | None = None, height: int | None = None) -> Page:
        page = Page(width or self.width, height or self.height)
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        """输出完整的 PDF 字节流（含交叉引用表）。"""
        if not self.pages:
            self.add_page()

        objects: list[bytes] = []
        # 对象编号：1=Catalog, 2=Pages, 3..(2+n)=Page, 之后是 Contents 与 Font
        page_count = len(self.pages)
        first_page_obj = 3
        first_content_obj = first_page_obj + page_count
        font_obj = first_content_obj + page_count

        kids = " ".join(f"{first_page_obj + i} 0 R" for i in range(page_count))
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(
            f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("latin-1")
        )

        for i, page in enumerate(self.pages):
            content_ref = first_content_obj + i
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page.width} {page.height}] "
                    f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_ref} 0 R >>"
                ).encode("latin-1")
            )

        for page in self.pages:
            stream = page.render()
            objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream")

        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

        return _assemble(objects)


def _assemble(objects: list[bytes]) -> bytes:
    """拼出最终的 PDF：头部 + 对象 + 交叉引用表 + trailer。"""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def simple_text_pdf(lines: list[str], *, page_size: tuple[int, int] = (PAGE_WIDTH, PAGE_HEIGHT)) -> bytes:
    """最简用法：一页纯文字 PDF。"""
    pdf = MiniPdf(width=page_size[0], height=page_size[1])
    page = pdf.add_page()
    y = page.height - 60
    for line in lines:
        page.text(50, y, line, size=11)
        y -= 18
    return pdf.to_bytes()


def table_pdf(
    rows: list[list[str]],
    col_widths: list[float] | None = None,
    *,
    title: str = "",
    page_size: tuple[int, int] = (PAGE_WIDTH, PAGE_HEIGHT),
) -> bytes:
    """单页 PDF：一段标题 + 一张带边框的表格。"""
    width, height = page_size
    pdf = MiniPdf(width=width, height=height)
    page = pdf.add_page()
    y = height - 60
    if title:
        page.text(50, y, title, size=13)
        y -= 30
    if col_widths is None:
        col_widths = [80.0] * max(len(rows[0]) if rows else 1, 1)
    page.table(50, y, col_widths, rows)
    return pdf.to_bytes()


__all__ = [
    "PAGE_WIDTH",
    "PAGE_HEIGHT",
    "Page",
    "MiniPdf",
    "simple_text_pdf",
    "table_pdf",
]
