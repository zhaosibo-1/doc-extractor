"""纯文本类加载器：TXT / MD / CSV / TSV。

文本格式看起来最简单，实际有两个坑：
1. **编码**：中文环境里 GBK/GB18030 与 UTF-8 混杂，还有带 BOM 的 UTF-8；
2. **结构**：纯文本没有显式结构，但「标题行」「标签: 值」「列表项」这三类
   是可以高置信度识别出来的，识别出来之后抽取层的准确率会明显提升。
"""

from __future__ import annotations

import csv
import io
import re

from ..ir import Block, Document, TableData
from ..textutil import clean_cell, collapse_ws, split_label_value
from .detect import decode_text
from .table_utils import build_tables

#: 常见章节标题关键词（命中即认为是标题行）
SECTION_WORDS = (
    "任职要求", "岗位职责", "岗位要求", "工作职责", "职位描述", "加分项", "福利待遇",
    "求职意向", "教育背景", "工作经历", "实习经历", "项目经历", "科研经历",
    "专业技能", "技能特长", "荣誉奖项", "自我评价", "基本信息", "个人简介",
    "发票信息", "购买方", "销售方", "商品明细", "合计", "备注", "摘要", "说明",
    # 合同 / 订单类文档的常见章节词。
    # 漏掉这些词的后果不是「少识别一个标题」，而是这一行会被当成普通正文，
    # 进而可能被「标签行的值在下一行」这条规则当成某个字段的值抓走。
    "服务内容", "付款方式", "结算方式", "采购清单", "合同条款", "订单信息",
    "交付方式", "验收标准", "其他约定", "违约责任", "争议解决",
)

#: 标题的几种常见形态
_HEADING_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[一二三四五六七八九十]+[、.．]\s*\S"),
    re.compile(r"^第[一二三四五六七八九十百]+[章节部分条]\s*\S"),
    re.compile(r"^\d+(?:\.\d+)*[、.．]\s*\S"),
    re.compile(r"^[A-Z][A-Za-z0-9 _\-]{2,40}$"),
)

#: Markdown 标题
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")

#: 列表项
_LIST_RE = re.compile(r"^\s*(?:[-*+•·]|\d+[.、)])\s+")


def load_text(name: str, data: bytes) -> Document:
    """解析 TXT / MD。"""
    text, encoding = decode_text(data)
    doc = Document(name=name, kind="text", pages=1)
    doc.meta["encoding"] = encoding

    blocks = _split_text_blocks(text)
    doc.blocks = blocks
    doc.pages = max(1, len(blocks) // 40 + 1)  # 粗略的「页数」，仅用于展示
    return doc


def _split_text_blocks(text: str) -> list[Block]:
    """把纯文本切成块。

    这里**不再**判定「键值行」—— 那件事由 :func:`~app.loaders.normalize.normalize_document`
    在分发之后统一做。之前这段逻辑同时存在于本文件和 docx / pdf 加载器之外，
    结果是同一份合同存成 ``.txt`` 能抽到字段、存成 ``.docx`` 完全抽不到：
    两份文件文本一样，却因为「谁负责标 kind」不同而结果不同。

    顺序上仍然先问标题再问列表项：``looks_like_heading`` 内部已经把
    「带值的冒号行」排除在标题之外，所以「价税合计（小写）：￥2,388.00」
    不会被误判成标题。
    """
    blocks: list[Block] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue

        md = _MD_HEADING_RE.match(line)
        if md:
            blocks.append(Block(kind="heading", text=md.group(2).strip(), level=len(md.group(1))))
            continue

        if looks_like_heading(line):
            blocks.append(Block(kind="heading", text=collapse_ws(line), level=1))
            continue

        # 列表项先剥掉记号（「- 甲方: 某某公司」→「甲方: 某某公司」），
        # 这样后续的键值行判定拿到的是干净的标签，而不是带横杠的标签。
        if _LIST_RE.match(line):
            blocks.append(Block(kind="list_item", text=collapse_ws(_LIST_RE.sub("", line))))
            continue

        blocks.append(Block(kind="paragraph", text=collapse_ws(line)))
    return blocks


def looks_like_heading(line: str) -> bool:
    """判断一行像不像标题。

    关键的一条：**带值的冒号行不是标题**。
    ``价税合计（小写）：￥2,388.00`` 这种行虽然含有章节词「合计」，
    但它的冒号后面有值，说明它是键值行 —— 早期版本把章节词判断放在冒号判断
    之前，结果这一行被当成标题，导致「价税合计」这个字段彻底抽不到，
    还顺带让票面勾稽规则报出「金额+税额 ≠ 价税合计」的假告警。
    """
    text = collapse_ws(line)
    if not text or len(text) > 40:
        return False
    # 标题不以句末标点结尾
    if text.endswith(("。", ".", "！", "!", "？", "?", ";", "；", ",", "，")):
        return False

    label, value = split_label_value(text)
    if label and value:
        # 「标签: 有值」→ 键值行，绝不可能是标题
        return False
    if label and not value:
        # 「任职要求：」这种只有标签没有值的行，是章节标题
        return True
    if "：" in text or ":" in text:
        return False

    if any(word in text for word in SECTION_WORDS) and len(text) <= 20:
        return True
    return any(pattern.match(text) for pattern in _HEADING_RES)


def load_csv(name: str, data: bytes) -> Document:
    """解析 CSV / TSV，作为「单表文档」。"""
    text, encoding = decode_text(data)
    rows = _parse_csv(text, name)
    tables = build_tables(rows, locator="表格", max_rows=100000)

    doc = Document(name=name, kind="csv", pages=1)
    doc.meta["encoding"] = encoding
    doc.meta["row_count"] = sum(t.row_count for t in tables)
    doc.blocks = [Block(kind="table", table=t) for t in tables]
    return doc


def _parse_csv(text: str, name: str) -> list[list[str]]:
    """解析 CSV 文本成二维数组。

    先尝试 ``csv.Sniffer`` 自动识别分隔符；识别失败（单列文件、
    或分隔符不统一）时按扩展名在逗号与制表符之间选。
    """
    sample = text[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        if name.lower().endswith(".tsv") or "\t" in sample:
            delimiter = "\t"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        cells = [clean_cell(cell) for cell in row]
        # 跳过完全空的行（末尾空行很常见）
        if any(cells):
            rows.append(cells)
    return rows


__all__ = ["load_text", "load_csv", "looks_like_heading", "SECTION_WORDS"]
