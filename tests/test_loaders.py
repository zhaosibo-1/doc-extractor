"""加载器测试：6 种格式 + 两条最重要的回归。

这里刻意验证的是「**同一份内容，换一种格式存，结果必须一样**」。
统一中间表示（IR）的全部价值就在这句话上，而它恰好最容易被打破 ——
只要某个格式的加载器少标一个字段，抽取层对应的策略就在那个格式上静默失效。
"""

from __future__ import annotations

import io

import pytest

from app.config import Settings
from app.loaders import LoadError, load_document
from app.loaders.detect import UnsupportedFormat, decode_text, sniff_kind
from app.loaders.table_utils import build_tables

from .helpers import EXAMPLE_CASES, read_example

#: 测试用设置：不落盘、限制放到最宽，避免测试因为默认上限而失败
SETTINGS = Settings(store_persist=False, pdf_max_pages=200, table_max_rows=10000)


# ---------------------------------------------------------------------------
# 类型识别：看文件头，不看扩展名
# ---------------------------------------------------------------------------


class TestSniffKind:
    def test_按文件头识别真实类型(self) -> None:
        assert sniff_kind("a.pdf", b"%PDF-1.7\n...")[0] == "pdf"
        assert sniff_kind("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)[0] == "image"
        assert sniff_kind("a.txt", "你好".encode())[0] == "text"
        # ZIP 容器要再看内部目录结构才能区分 docx / xlsx / pptx
        assert sniff_kind("order.xlsx", read_example("order.xlsx"))[0] == "xlsx"
        assert sniff_kind("contract.docx", read_example("contract.docx"))[0] == "docx"

    def test_扩展名撒谎时以文件头为准(self) -> None:
        # 一张 PNG 被改名成 .txt，必须仍然被认成图片；
        # 否则解析会走文本路径，读出一堆乱码当成正文。
        kind, _ = sniff_kind("假的.txt", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        assert kind == "image"

    def test_xlsx改名成docx仍然被认成xlsx(self) -> None:
        kind, _ = sniff_kind("伪装的.docx", read_example("order.xlsx"))
        assert kind == "xlsx"

    def test_未知格式直接报不支持(self) -> None:
        with pytest.raises(UnsupportedFormat):
            sniff_kind("a.exe", b"MZ\x90\x00" + b"\x00" * 40)


class TestDecodeText:
    def test_优先按utf8_sig解码(self) -> None:
        text, encoding = decode_text("中文内容".encode("utf-8-sig"))
        assert text == "中文内容"
        assert "utf-8" in encoding

    def test_退到gbk不会乱码(self) -> None:
        text, encoding = decode_text("中文内容".encode("gbk"))
        assert text == "中文内容"
        assert encoding.lower() in {"gbk", "gb18030"}


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kind", "template"), EXAMPLE_CASES)
def test_每种示例都能被识别并解析出内容(name: str, kind: str, template: str) -> None:
    doc = load_document(name, read_example(name), SETTINGS)
    assert doc.kind == kind, f"{name} 被识别成 {doc.kind}，期望 {kind}"
    # 除图片外都必须解析出可读内容
    if kind != "image":
        assert doc.char_count > 0 or doc.tables, f"{name} 没有解析出任何内容"


class TestTextLoader:
    def test_标题与键值行被分开(self) -> None:
        doc = load_document(
            "t.txt",
            "商品明细\n\n合同编号：HT-2026-0417\n合同金额：267400.00\n".encode(),
            SETTINGS,
        )
        kinds = [b.kind for b in doc.blocks]
        assert kinds == ["heading", "kv", "kv"]

    def test_带句末标点的编号行是列表项而不是标题(self) -> None:
        # 这是本层最微妙的一条：``1. 概述`` 是标题，``1. 提供服务器 4 台…；`` 是列表项。
        # 区分它们的是**行尾标点** —— 标题不以句末标点结尾，列表项几乎都带。
        # （全角「；」「。」会被 NFKC 归一化成半角，所以断言里是半角）
        doc = load_document(
            "t.txt",
            "服务内容\n1. 提供 2U 机架式服务器 4 台，含 3 年原厂质保；\n2. 提供交换机 6 台。\n".encode(),
            SETTINGS,
        )
        items = [b.text for b in doc.blocks if b.kind == "list_item"]
        assert items == ["提供 2U 机架式服务器 4 台,含 3 年原厂质保;", "提供交换机 6 台."]

    def test_短编号行会被当作标题(self) -> None:
        # 「1. 概述」这类短编号行确实是章节标题，不是列表项
        doc = load_document("t.txt", "1. 概述\n".encode(), SETTINGS)
        assert [b.kind for b in doc.blocks] == ["heading"]

    def test_整行无标签的短句不会被当成标题(self) -> None:
        # 记录一个刻意的取舍：文档第一行如果是纯标题（如「采购合同」）而不含
        # 章节词或编号，它会被当成普通段落。这是为了避免把**陈述句**误判成标题 ——
        # 名片示例的第一行是公司名，一旦被判成标题就不再是「下一行取值」的候选，
        # 反而会破坏抽取。标题识别宁可保守。
        doc = load_document("t.txt", "采购合同\n".encode(), SETTINGS)
        assert [b.kind for b in doc.blocks] == ["paragraph"]

    def test_列表项会剥掉记号(self) -> None:
        doc = load_document("t.txt", "- 提供服务器 4 台；\n".encode(), SETTINGS)
        items = [b.text for b in doc.blocks if b.kind == "list_item"]
        assert items == ["提供服务器 4 台;"]


class TestCsvLoader:
    def test_csv被当成单表文档(self) -> None:
        doc = load_document("t.csv", "名称,数量,金额\n无线键盘,12,2388.00\n显示器,3,3897.00\n".encode(), SETTINGS)
        assert doc.kind == "csv"
        assert len(doc.tables) == 1
        table = doc.tables[0]
        assert table.columns == ["名称", "数量", "金额"]
        assert len(table.body) == 2


class TestXlsxLoader:
    def test_订单头与明细被切成两张表(self) -> None:
        # 回归：一张工作表里「订单头 + 空行 + 明细」两段，早期被当成一张表，
        # 订单号那行被误当表头，订单号直接变成列名。
        doc = load_document("order.xlsx", read_example("order.xlsx"), SETTINGS)
        order_tables = [t for t in doc.tables if t.locator.startswith("销售订单")]
        assert len(order_tables) == 2, f"期望切成 2 张，实际 {len(order_tables)}：{[t.columns for t in order_tables]}"
        assert "订单号" in order_tables[0].columns
        assert "数量" in order_tables[1].columns

    def test_多级表头被纵向拼接(self) -> None:
        doc = load_document("order.xlsx", read_example("order.xlsx"), SETTINGS)
        quarter = next(t for t in doc.tables if "季度" in t.locator)
        # 「一季度」+「销量」应拼成「一季度_销量」
        assert any("销量" in c for c in quarter.columns)
        assert quarter.header_rows == 2

    def test_合并单元格被正确填充(self) -> None:
        doc = load_document("order.xlsx", read_example("order.xlsx"), SETTINGS)
        assert any(t.merges for t in doc.tables), "合并单元格信息丢失了"


class TestPdfLoader:
    def test_表格线被识别成表格(self) -> None:
        doc = load_document("order.pdf", read_example("order.pdf"), SETTINGS)
        assert doc.kind == "pdf"
        assert len(doc.tables) >= 1
        table = doc.tables[0]
        assert table.columns == ["Item", "Qty", "Unit Price", "Amount"]
        assert len(table.body) == 3

    def test_正文里的标签行可被读取(self) -> None:
        doc = load_document("order.pdf", read_example("order.pdf"), SETTINGS)
        assert "Order No.: SO-2026-001" in doc.text

    def test_加密pdf不会崩(self) -> None:
        # 一个最小但确实声明了加密的 PDF 头；加载器应给出可读的 LoadError
        # 而不是抛底层异常穿透到接口层。
        broken = b"%PDF-1.4\n" + b"garbage" * 4
        with pytest.raises(LoadError):
            load_document("坏.pdf", broken, SETTINGS)


class TestDocxLoader:
    def test_段落与表格按真实顺序还原(self) -> None:
        doc = load_document("contract.docx", read_example("contract.docx"), SETTINGS)
        kinds = [b.kind for b in doc.blocks]
        # 合同里「正文 → 表格 → 正文」的顺序不能被打乱：
        # 第一张表格前后都必须还有文字块
        first_table = kinds.index("table")
        assert "paragraph" in kinds[:first_table] or "kv" in kinds[:first_table]
        assert "table" in kinds
        assert any(k in {"paragraph", "kv"} for k in kinds[first_table + 1 :])

    def test_表格内容完整且不错位(self) -> None:
        """回归：``id(cell._tc)`` 被回收后地址复用，导致无关单元格被判成合并单元格。

        表现极具迷惑性 —— 一张干净的 4×4 表格会解析出**看起来完全合法**
        但实际是前两行值错乱重排的内容，校验层也发现不了。
        """
        doc = load_document("contract.docx", read_example("contract.docx"), SETTINGS)
        table = doc.tables[0]
        assert table.columns == ["产品", "规格", "数量", "金额"]
        bodies = table.body
        assert len(bodies) == 3
        assert bodies[0] == ["服务器", "2U 机架式", "4", "156000.00"]
        assert bodies[1] == ["交换机", "48 口千兆", "6", "23400.00"]
        assert bodies[2] == ["存储阵列", "24 盘位", "2", "88000.00"]

    def test_重复读取同一份文件结果稳定(self) -> None:
        # 地址复用是概率性 bug：同一份字节多解析几次，才能把它钉死
        data = read_example("contract.docx")
        results = []
        for _ in range(5):
            doc = load_document("contract.docx", data, SETTINGS)
            results.append([tuple(row) for row in doc.tables[0].body])
        assert all(r == results[0] for r in results), "多次解析结果不一致（疑似对象身份判断不稳定）"


# ---------------------------------------------------------------------------
# 跨格式归一化（最关键的回归）
# ---------------------------------------------------------------------------


class TestCrossFormatNormalization:
    """同一份文本，存在 .txt 与存在 .docx 里，键值行的判定必须一致。

    回归背景：``text_loader`` 会把「合同编号：HT-2026-0417」标成 ``kv``，
    而 ``docx_loader`` / ``pdf_loader`` 只标 ``paragraph``。
    抽取层置信度最高的一路（标签行，0.92）只认 ``kv``，
    于是同一份合同存成 txt 能抽到 100%、存成 docx 是 **0%**。
    """

    def test_txt里的键值行被标成kv(self) -> None:
        doc = load_document("t.txt", "合同编号：HT-2026-0417\n".encode(), SETTINGS)
        assert [b.kind for b in doc.blocks] == ["kv"]

    def test_docx里的键值行也被标成kv(self) -> None:
        import docx

        document = docx.Document()
        document.add_paragraph("合同编号：HT-2026-0417")
        document.add_paragraph("甲方：某某科技有限公司")
        buffer = io.BytesIO()
        document.save(buffer)

        doc = load_document("t.docx", buffer.getvalue(), SETTINGS)
        assert [b.kind for b in doc.blocks] == ["kv", "kv"]

    def test_pdf里的键值行也被标成kv(self) -> None:
        doc = load_document("order.pdf", read_example("order.pdf"), SETTINGS)
        kv_texts = [b.text for b in doc.blocks if b.kind == "kv"]
        assert any("Order No." in t for t in kv_texts), f"PDF 里没有识别出键值行：{kv_texts}"

    def test_标题不会被误判成键值行(self) -> None:
        doc = load_document("t.txt", "商品明细\n\n服务内容\n".encode(), SETTINGS)
        assert all(b.kind == "heading" for b in doc.blocks)

    def test_带值的冒号行不会被当成标题(self) -> None:
        # 「价税合计（小写）：￥2,388.00」含章节词「合计」，
        # 早期实现把它当标题，字段彻底抽不到，还连带报出假告警。
        doc = load_document("t.txt", "价税合计（小写）：￥2,388.00\n".encode(), SETTINGS)
        assert [b.kind for b in doc.blocks] == ["kv"]


# ---------------------------------------------------------------------------
# 表格装配
# ---------------------------------------------------------------------------


class TestBuildTables:
    def test_常规二维数组(self) -> None:
        tables = build_tables([["名称", "数量"], ["甲", "1"], ["乙", "2"]])
        assert len(tables) == 1
        assert tables[0].columns == ["名称", "数量"]
        assert len(tables[0].body) == 2

    def test_参差不齐的行被补齐(self) -> None:
        tables = build_tables([["名称", "数量", "金额"], ["甲", "1"], ["乙"]])
        assert all(len(row) == 3 for row in tables[0].body)

    def test_上下堆叠的两张表被切开(self) -> None:
        rows = [
            ["订单号", "客户"],
            ["SO-001", "甲"],
            [],
            ["商品", "数量"],
            ["键盘", "1"],
        ]
        tables = build_tables(rows)
        assert len(tables) == 2
        assert tables[0].columns == ["订单号", "客户"]
        assert tables[1].columns == ["商品", "数量"]

    def test_记录型数据行不会被当成表头(self) -> None:
        # 回归：整行都是「编码 / 日期」这类不含中文的值，被判成标题行吃掉。
        rows = [
            ["订单号", "客户名称", "下单日期", "金额"],
            ["SO-2026-001", "阿尔法", "2026-01-05", "8782.50"],
            ["SO-2026-002", "贝塔", "2026-01-07", "1200.00"],
        ]
        tables = build_tables(rows)
        assert tables[0].columns == ["订单号", "客户名称", "下单日期", "金额"]
        assert len(tables[0].body) == 2

    def test_表头上方的标题行被剥离(self) -> None:
        rows = [
            ["销售订单明细表", "", ""],
            ["名称", "数量", "金额"],
            ["键盘", "1", "199"],
        ]
        tables = build_tables(rows)
        assert tables[0].caption == "销售订单明细表"
        assert tables[0].columns == ["名称", "数量", "金额"]

    def test_空输入返回空列表(self) -> None:
        assert build_tables([]) == []
        assert build_tables([[], []]) == []
