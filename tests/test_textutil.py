"""文本工具层测试。

这一层是整条链路的地基：列名归一化、数值解析、标签拆分、列名解析。
它们出错的表现通常是「某个字段悄悄抽错」，比抛异常难发现得多，
所以边界条件要一条条钉住。
"""

from __future__ import annotations

import pytest

from app.textutil import (
    collapse_ws,
    is_data_like_cell,
    is_numeric_cell,
    match_score,
    normalize_key,
    resolve_column,
    split_label_value,
    to_date,
    to_number,
)


class TestNormalizeKey:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("销售金额（元）", "销售金额"),
            ("Total Amount (USD)", "totalamount"),
            ("unit_price", "unitprice"),
            ("Unit Price", "unitprice"),
            ("UnitPrice", "unitprice"),
            ("  前后空格  ", "前后空格"),
            ("小写金额 / 价税合计", "小写金额价税合计"),
        ],
    )
    def test_归一化会抹平大小写空格与括号(self, raw: str, expected: str) -> None:
        assert normalize_key(raw) == expected


class TestResolveColumn:
    """``resolve_column`` 是**确认**（精确相等），必须与模糊的 ``match_score`` 分工明确。"""

    def test_三种英文写法视为同一列(self) -> None:
        columns = ["Unit Price", "Qty", "Amount"]
        assert resolve_column("单价", columns, ["UnitPrice", "unit_price", "Unit Price"]) == "Unit Price"

    def test_中文规范名走别名命中英文列(self) -> None:
        columns = ["Item", "Qty", "Unit Price", "Amount"]
        assert resolve_column("数量", columns, ["Qty", "Quantity"]) == "Qty"
        assert resolve_column("金额", columns, ["Amount", "Line Total"]) == "Amount"

    def test_找不到就返回空串而不是近似列(self) -> None:
        columns = ["一季度_金额", "二季度_金额"]
        # 「一季度_金额」**含有**「金额」两个字，但它们是不同的列。
        # 这里必须拒绝 —— 接受它会让规则拿错误的列算出一个「通过」。
        assert resolve_column("金额", columns, []) == ""

    def test_别名也不能放宽成包含匹配(self) -> None:
        assert resolve_column("金额", ["订单金额"], ["Amount"]) == ""

    def test_空名字不会误命中(self) -> None:
        assert resolve_column("", ["金额"], []) == ""


class TestMatchScore:
    """``match_score`` 是**猜测**：宽松但方向必须正确。"""

    def test_完全相等得满分(self) -> None:
        assert match_score("金额", ["金额"])[1] == 1.0

    def test_别名是标签的子串时给高分(self) -> None:
        # 「本月销售金额」里确实含「销售金额」，这是可靠证据
        assert match_score("本月销售金额", ["销售金额"])[1] == pytest.approx(0.9)

    def test_标签比别名更短时不给高分(self) -> None:
        # 回归：名片模板里 address 的别名有「公司地址」，
        # 于是一张只写「公司：某某科技」的名片把公司名抽进了地址字段。
        # 「公司」是「公司地址」的子串，但方向反了 —— 标签更短说明它更可能是另一个字段。
        assert match_score("公司", ["公司地址"])[1] < 0.55

    def test_空输入不会崩(self) -> None:
        assert match_score("", ["金额"]) == ("", 0.0)
        assert match_score("金额", []) == ("", 0.0)


class TestSplitLabelValue:
    @pytest.mark.parametrize(
        ("line", "label", "value"),
        [
            ("发票号码：25317000000012345678", "发票号码", "25317000000012345678"),
            ("Order No.: SO-2026-001", "Order No.", "SO-2026-001"),
            # 全角字符会被 NFKC 归一化成半角 —— 这是刻意的：
            # 同一份文档里「（小写）」和「(小写)」必须被当成同一个标签，
            # 否则模板要为每种写法各列一条别名。
            ("价税合计（小写）：￥2,388.00", "价税合计(小写)", "¥2,388.00"),
            ("任职要求：", "任职要求", ""),
            ("这是一段没有冒号的正文", "", ""),
        ],
    )
    def test_拆分标签与值(self, line: str, label: str, value: str) -> None:
        assert split_label_value(line) == (label, value)


class TestToNumber:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,234.50", 1234.50),
            ("￥2,388.00", 2388.0),
            ("(1,200.00)", -1200.0),  # 会计负数：括号表示负
            ("1.2万", 12000.0),
            ("3.5亿", 350000000.0),
            ("1.2千万", 12000000.0),  # 多字后缀要优先于单字「千」/「万」
            ("３００", 300.0),  # 全角数字
            ("-45", -45.0),
            ("", None),
            ("不是数字", None),
        ],
    )
    def test_数值解析兼顾中文单据的真实写法(self, raw: str, expected: float | None) -> None:
        assert to_number(raw) == expected

    def test_小写m被当作未知尾缀而不是数量级(self) -> None:
        # 「5m」在真实单据里更可能是「5 米」。to_number 对「1,234.5元」这类
        # 「数字 + 未知单位」的处理是保留数字、丢掉单位，所以这里是 5.0。
        # 关键是不把它读成 5000000 —— 猜错数量级的代价比不识别更大。
        assert to_number("5m") == 5.0

    def test_数字加未知单位保留数字(self) -> None:
        assert to_number("1,234.5元") == 1234.5


class TestCellJudgements:
    def test_严格数值判定排除中文与字母(self) -> None:
        assert is_numeric_cell("120") is True
        assert is_numeric_cell("120.50") is True
        assert is_numeric_cell("数量") is False
        assert is_numeric_cell("12 套") is False
        assert is_numeric_cell("2023年") is False

    @pytest.mark.parametrize("raw", ["1.2万", "3.5亿", "3k", "4.5M", "３００", "100%", "¥1,234.50"])
    def test_带数量级或符号的数值仍算数据(self, raw: str) -> None:
        # 回归：CJK 判定曾经排在「剥数量级后缀」之前，于是「1.2万」里那个「万」
        # 直接把它判成非数值 —— 剥后缀的代码成了永远走不到的死代码，
        # 和这个函数的文档说明正好相反。
        assert is_numeric_cell(raw) is True

    def test_数据行判定把日期与编码也算作数据(self) -> None:
        # 这一条是「记录型数据行被当成表头吃掉」那个 bug 的刹车信号
        assert is_data_like_cell("SO-2026-001") is True
        assert is_data_like_cell("2026-01-05") is True
        assert is_data_like_cell("199.00") is True
        # 带数量级的数值也必须算数据
        assert is_data_like_cell("1.2万") is True

    def test_中文日期算数据但中文表头不算(self) -> None:
        # 回归：「含中文就排除」曾经排在数值/日期判定之前，
        # 结果「2026年1月5日」这种最典型的中文数据被当成表头。
        assert is_data_like_cell("2026年1月5日") is True
        assert is_data_like_cell("2026-01-05") is True
        # 但中文列名与不完整年份必须继续被排除
        assert is_data_like_cell("商品名称") is False
        assert is_data_like_cell("2023年") is False
        assert is_data_like_cell("一季度") is False


class TestToDate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026年3月12日", "2026-03-12"),
            ("2026-01-05", "2026-01-05"),
            ("2026/1/5", "2026-01-05"),
            ("2026年4月17日", "2026-04-17"),
        ],
    )
    def test_中文日期与本格日期都能解析(self, raw: str, expected: str) -> None:
        assert to_date(raw) == expected


def test_collapse_ws_统一空白与全角标点() -> None:
    # 全角冒号 → 半角同样是 NFKC 归一化的结果，不是偶然
    assert collapse_ws("  甲   方 ：  某某  ") == "甲 方 : 某某"
