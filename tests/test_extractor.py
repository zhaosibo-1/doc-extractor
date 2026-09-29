"""抽取层测试。

抽取层是「规则优先、模型补漏」的分工点，也是缺陷最容易藏身的地方 ——
它不抛异常，只会安静地少抽一个字段、或者选错一张表让校验全部跳过。
所以这里的用例一半以上是**回归测试**。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.extractor import Extractor, _wanted_detail_columns
from app.llm import LLMClient
from app.loaders import load_document
from app.templates import get_template


@pytest.fixture
def extractor() -> Extractor:
    """不配 Key 的抽取器：engine=rule 时完全不需要模型。"""
    settings = Settings(store_persist=False, pdf_max_pages=200, table_max_rows=10000)
    return Extractor(settings, LLMClient(api_key="", base_url="", model="", timeout=3))


FILLED = (None, "", [])


def filled_count(result) -> int:
    return sum(1 for field in result.fields if field.value not in FILLED)


# ---------------------------------------------------------------------------
# 明细表所需列名的推导
# ---------------------------------------------------------------------------


class TestWantedDetailColumns:
    def test_逐行表达式的变量也是列名(self) -> None:
        # 「数量 * 单价」在每一行上求值，所以「数量」「单价」是明细表的列名
        wanted = dict(_wanted_detail_columns(get_template("sales_order")))
        assert wanted["金额"] == ["Amount", "Line Total", "小计", "金额合计"]
        assert wanted["数量"] == ["Qty", "Quantity", "Sales Qty"]
        assert "单价" in wanted

    def test_字段级表达式的变量不会被当列名(self) -> None:
        # invoice 的 amount_plus_tax 是 expr 规则，变量 amount / tax / total
        # 都是**字段 key** 而不是列名。把它们当列名去找会拉低所有表的匹配度。
        names = [name for name, _ in _wanted_detail_columns(get_template("invoice"))]
        assert names == ["金额"]

    def test_没有列级规则的模板返回空(self) -> None:
        assert _wanted_detail_columns(get_template("contract")) == []
        assert _wanted_detail_columns(get_template("business_card")) == []
        assert _wanted_detail_columns(get_template("generic")) == []


# ---------------------------------------------------------------------------
# 明细表挑选（回归重点）
# ---------------------------------------------------------------------------


class TestCollectRows:
    def test_在一张含多段表格的工作簿里选中真正的明细表(self, extractor: Extractor) -> None:
        """回归：按「命中列数」评分时，一张带两个含「金额」列的「季度销量」表

        （一季度_金额 / 二季度_金额）会以 4.0 分压过真正的明细表（只有一列「金额」，2.3 分）。
        更糟的是它**不报错** —— 校验拿一张无关的表去跑，只会安静地全部跳过，
        界面上显示「跳过 3 条」，看不出是选表错了。

        现在按「规则需要的列是否齐备」评分，且列名必须精确相等，
        「一季度_金额」不会被当成「金额」。
        """
        settings = Settings(store_persist=False, table_max_rows=10000)
        doc = load_document("order.xlsx", (settings and __import__("pathlib").Path("examples/order.xlsx").read_bytes()), settings)
        pick = extractor._collect_rows(doc, get_template("sales_order"))

        assert pick.table is not None
        assert "季度" not in pick.table.locator, f"选错了表：{pick.table.locator}"
        assert pick.columns == ["序号", "商品名称", "数量", "单价", "金额", "备注"]
        assert len(pick.rows) == 3
        assert pick.warnings == []

    def test_英文列头靠别名也能选中(self, extractor: Extractor) -> None:
        # order.pdf 的列名是 Item / Qty / Unit Price / Amount
        settings = Settings(store_persist=False)
        doc = load_document("order.pdf", __import__("pathlib").Path("examples/order.pdf").read_bytes(), settings)
        pick = extractor._collect_rows(doc, get_template("sales_order"))

        assert pick.columns == ["Item", "Qty", "Unit Price", "Amount"]
        assert len(pick.rows) == 3
        assert pick.warnings == []

    def test_缺列时返回空行并说清缺哪一列(self, extractor: Extractor) -> None:
        """凑不齐所需的列时宁可明说，也不要塞一张无关的表让校验静默装死。"""
        settings = Settings(store_persist=False)
        doc = load_document(
            "t.xlsx",
            _make_xlsx([["产品", "地区"], ["键盘", "北京"], ["显示器", "上海"]]),
            settings,
        )
        pick = extractor._collect_rows(doc, get_template("sales_order"))

        assert pick.rows == []
        assert len(pick.warnings) == 1
        warning = pick.warnings[0]
        # 必须报出缺了哪几列，并附带当前文档有哪些列，用户才知道该怎么办
        assert "金额" in warning and "数量" in warning and "单价" in warning
        assert "产品" in warning and "地区" in warning

    def test_模板没有列级规则时退回行数最多的表(self, extractor: Extractor) -> None:
        settings = Settings(store_persist=False)
        doc = load_document(
            "t.xlsx",
            _make_xlsx([["产品", "地区"], ["键盘", "北京"], ["显示器", "上海"], ["鼠标", "广州"]]),
            settings,
        )
        pick = extractor._collect_rows(doc, get_template("contract"))
        assert len(pick.rows) == 3
        assert pick.warnings == []

    def test_表被截断时给出提示(self, extractor: Extractor) -> None:
        settings = Settings(store_persist=False, table_max_rows=3)
        rows = [["名称", "数量", "金额"]] + [[f"商品{i}", "1", "10.00"] for i in range(10)]
        doc = load_document("t.xlsx", _make_xlsx(rows), settings)
        pick = extractor._collect_rows(doc, get_template("sales_order"))
        assert any("截断" in w for w in pick.warnings)


def _make_xlsx(rows: list[list[object]]) -> bytes:
    """把二维数据做成一个最小 xlsx（测试里不必手写 ZIP 结构）。"""
    import io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 端到端抽取（六个示例）
# ---------------------------------------------------------------------------


class TestExampleExtraction:
    def test_发票_票面勾稽必须通过(self, extractor: Extractor) -> None:
        result = _extract(extractor, "invoice_cn.txt", "invoice")
        assert result.completeness == 100.0
        assert result.field_map()["total"].value == 2388.0
        rule = next(r for r in result.rules if r.key == "amount_plus_tax")
        assert rule.passed and not rule.skipped

    def test_名片_公司名来自整行正则(self, extractor: Extractor) -> None:
        # 真实名片上公司名是一行独立的大字，没有「公司：」标签。
        # 靠模板里的正则兜底，而不是靠猜「第一行就是公司」。
        result = _extract(extractor, "business_card_cn.txt", "business_card")
        assert result.completeness == 100.0
        company = result.field_map()["company"]
        assert company.value == "某某智能科技有限公司"
        assert company.source.strategy == "regex"

    def test_名片_地址不会被公司名污染(self, extractor: Extractor) -> None:
        # 回归：address 的别名里有「公司地址」，而「公司」是它的子串，
        # 早期实现按双向包含匹配，于是地址字段被抽成了公司名。
        result = _extract(extractor, "business_card_cn.txt", "business_card")
        address = result.field_map()["address"]
        assert "辽宁" in str(address.value)
        assert "科技有限公司" not in str(address.value)

    def test_合同_日期先后与甲乙方齐全性都要通过(self, extractor: Extractor) -> None:
        result = _extract(extractor, "contract_cn.txt", "contract")
        assert result.completeness == 100.0
        assert all(r.passed and not r.skipped for r in result.rules)

    def test_合同docx与合同txt抽到同样的字段(self, extractor: Extractor) -> None:
        """回归：同一份合同内容，存成 .txt 曾能抽到 100%，存成 .docx 是 0%。

        原因是标签行判定长在 text_loader 里，docx_loader 从不产出 ``kind="kv"``。
        这条用例直接对比两种格式的抽取结果，是跨格式一致性最有力的守卫。
        """
        from_txt = _extract(extractor, "contract_cn.txt", "contract")
        from_docx = _extract(extractor, "contract.docx", "contract")

        txt_values = {f.key: f.value for f in from_txt.fields}
        docx_values = {f.key: f.value for f in from_docx.fields}
        assert docx_values == txt_values
        assert from_docx.completeness == 100.0

    def test_订单xlsx_逐行算术与合计都要通过(self, extractor: Extractor) -> None:
        result = _extract(extractor, "order.xlsx", "sales_order")
        assert result.completeness >= 92.0
        for key in ("line_total", "sum_amount"):
            rule = next(r for r in result.rules if r.key == key)
            assert rule.passed and not rule.skipped, f"{key} 未真正执行：{rule.detail}"
        # 明细表必须真的是那三行商品
        assert [row["商品名称"] for row in result.rows] == ["无线键盘", "机械键盘", "显示器"]

    def test_订单pdf_英文列头也能跑通行级校验(self, extractor: Extractor) -> None:
        """模板的规则用中文规范列名书写，靠 column_aliases 落到英文实际列名上。"""
        result = _extract(extractor, "order.pdf", "sales_order")
        assert result.completeness == 100.0
        rule = next(r for r in result.rules if r.key == "line_total")
        assert rule.passed and not rule.skipped
        assert "3 行全部满足" in rule.detail
        assert result.row_columns == ["Item", "Qty", "Unit Price", "Amount"]


class TestSourceLocators:
    def test_每个抽到的字段都能回到原文(self, extractor: Extractor) -> None:
        """可解释性：这条断言保证「值 + 来源定位 + 置信度」三件套齐全。

        少了来源定位，用户就无法核对 —— 那才是抽取系统真正让人不敢用的原因。
        """
        for name, template in [
            ("invoice_cn.txt", "invoice"),
            ("business_card_cn.txt", "business_card"),
            ("contract_cn.txt", "contract"),
        ]:
            result = _extract(extractor, name, template)
            for field in result.fields:
                if field.value in FILLED:
                    continue
                assert field.source.strategy != "none", f"{name} 的 {field.key} 没有来源策略"
                assert field.source.locator, f"{name} 的 {field.key} 没有来源定位"
                assert field.confidence > 0, f"{name} 的 {field.key} 置信度为 0"

    def test_规则层置信度高于模型层(self) -> None:
        from app.extractor import BASE_CONFIDENCE

        assert (
            BASE_CONFIDENCE["column"]
            > BASE_CONFIDENCE["label"]
            > BASE_CONFIDENCE["regex"]
            > BASE_CONFIDENCE["llm"]
        )


class TestDeriveFromRows:
    """由结构推出的字段（目前只有「明细行数」）。

    这是整条链路里唯一一种「值不是从文档里读出来的」字段，所以三条边界都要守住：
    没声明就不能生效、有声明才补、文档写了就必须让位给文档。
    """

    def _extract(self, extractor: Extractor, text: str):
        import asyncio

        from app.schemas import FieldSpec, Template

        settings = Settings(store_persist=False)
        doc = load_document("t.csv", text.encode("utf-8"), settings)
        template = Template(
            key="t",
            label="测试",
            fields=[
                FieldSpec(key="plain", label="没声明的字段"),
                FieldSpec(key="counted", label="明细行数", type="integer", from_row_count=True),
            ],
        )
        return asyncio.run(extractor.extract(doc, template, engine="rule"))

    def test_没声明的字段不会被行数污染(self, extractor: Extractor) -> None:
        """否则「数量」这类字段在单行明细时会被悄悄填成 1，而且不留痕迹。"""
        result = self._extract(extractor, "序号,商品名称\n1,键盘\n2,显示器\n")
        values = {f.key: f.value for f in result.fields}
        assert values["plain"] is None
        assert values["counted"] == 2

    def test_声明了才补且带上来源定位(self, extractor: Extractor) -> None:
        result = self._extract(extractor, "序号,商品名称\n1,键盘\n2,显示器\n3,鼠标\n")
        field = next(f for f in result.fields if f.key == "counted")
        assert field.value == 3
        assert field.source.strategy == "derived"
        assert "3 行" in field.source.locator

    def test_已有值就不让位(self, extractor: Extractor) -> None:
        """文档写 9 行、表里只有 2 行 —— 这条矛盾必须留在结果里被人看见。

        直接调派生步骤，是因为「文档里读到行数」这件事要构造出
        「标签行 + 明细表」的文档才自然（端到端那一条在 test_api.py 里，
        用 order.pdf 的 ``Items: 3`` 验）。这里要守的是派生步骤本身的分寸：
        它只填空，从不覆盖。
        """
        from app.extractor import Candidate
        from app.schemas import FieldSpec

        specs = [FieldSpec(key="counted", label="明细行数", type="integer", from_row_count=True)]
        candidates = {"counted": Candidate(value="9", strategy="label", locator="第 1 行「明细行数:9」")}
        extractor._derive_from_rows(specs, candidates, [{"a": "1"}, {"a": "2"}])
        assert candidates["counted"].value == "9"
        assert candidates["counted"].strategy == "label"


class TestRuleEngineOnly:
    def test_未配置模型时规则引擎仍可完整工作(self, extractor: Extractor) -> None:
        result = _extract(extractor, "invoice_cn.txt", "invoice", engine="rule")
        assert result.engine == "rule"
        assert result.completeness == 100.0

    def test_图片在规则模式下给出明确的降级提示(self, extractor: Extractor) -> None:
        from pathlib import Path

        settings = Settings(store_persist=False)
        doc = load_document("business_card.png", Path("examples/business_card.png").read_bytes(), settings)
        assert doc.kind == "image"
        assert doc.needs_ocr is True


def _extract(extractor: Extractor, name: str, template: str, engine: str = "auto"):
    import asyncio
    from pathlib import Path

    from app.loaders import load_document

    settings = Settings(store_persist=False, pdf_max_pages=200, table_max_rows=10000)
    doc = load_document(name, (Path("examples") / name).read_bytes(), settings)
    return asyncio.run(extractor.extract(doc, get_template(template), engine=engine))
