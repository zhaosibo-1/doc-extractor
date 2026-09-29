"""表格装配测试：表头识别、堆叠切分、合并单元格。

这一层是「看起来最简单、实际上最容易毁数据」的一层：判定错了不会报错，
只会给出一个**看起来完全合理**的列名集合，然后把整表内容吃掉。
"""

from __future__ import annotations

import pytest

from app.ir import Cell
from app.loaders.table_utils import (
    apply_merges,
    build_tables,
    detect_header_rows,
    looks_like_header_row,
    split_stacked,
)


class TestDetectHeaderRows:
    def test_普通中文表(self) -> None:
        assert detect_header_rows([["名称", "数量"], ["键盘", "12"]]) == 1

    def test_纯数字表没有表头(self) -> None:
        assert detect_header_rows([["1", "2"], ["3", "4"]]) == 0

    @pytest.mark.parametrize(
        "rows",
        [
            [["产品", "地区"], ["键盘", "北京"]],
            [["产品", "地区"], ["键盘", "北京"], ["显示器", "上海"]],
            [["产品", "地区"], ["键盘", "北京"], ["显示器", "上海"], ["鼠标", "广州"]],
            [
                ["产品", "地区"],
                ["键盘", "北京"],
                ["显示器", "上海"],
                ["鼠标", "广州"],
                ["音箱", "深圳"],
            ],
        ],
    )
    def test_全文字数据表不能把整表当表头(self, rows: list[list[str]]) -> None:
        """回归（最严重的一个缺陷）：整张文字表格被拼成列名，数据一行不剩。

        判据曾经写成「扫描数是否走到了表格末尾」，但扫描本身受 ``max_header_rows``
        限制 —— 4 行以上的全文字表格扫到第 3 行就停了，``count`` 永远到不了末尾，
        于是安全网从未触发。修复前的实际表现是：

            输入: [['产品','地区'], ['键盘','北京'], ['显示器','上海'], ['鼠标','广州']]
            列名: ['产品_键盘_显示器', '地区_北京_上海']
            数据: 1 行（只剩最后一行）

        「名称 / 类别 / 地址 / 状态」这些列天然都是文字，所以这种表在真实文档里
        非常常见。整表被毁比「第一个列名不太准」严重得多。
        """
        header_rows = detect_header_rows(rows)
        assert header_rows == 1, f"期望只把第一行当表头，实际 {header_rows}"

        table = build_tables(rows)[0]
        assert table.columns == ["产品", "地区"]
        assert len(table.body) == len(rows) - 1

    def test_竖排键值表的记录不会被吃掉(self) -> None:
        """额外表头行必须有「结构依据」：上一行有空格子或相邻重复。

        这张表里前两行都是纯文字，逐行判定无法区分；靠「上一行排满且互不相同
        ⇒ 它自己就是完整的一行」把第一条记录保住。
        """
        rows = [["字段", "值"], ["产品", "服务器"], ["数量", "4"], ["金额", "156000.00"]]
        assert detect_header_rows(rows) == 1
        table = build_tables(rows)[0]
        assert table.columns == ["字段", "值"]
        assert ["产品", "服务器"] in table.body

    def test_多级表头被识别成两行(self) -> None:
        rows = [
            ["产品", "一季度", "一季度", "二季度", "二季度"],
            ["产品", "销量", "金额", "销量", "金额"],
            ["无线键盘", "120", "23880", "150", "29850"],
            ["机械键盘", "45", "22477", "60", "29970"],
        ]
        assert detect_header_rows(rows) == 2

    def test_表头行里出现成片数据时立即停止(self) -> None:
        # 记录型数据行（编码 / 日期）曾经被当成表头吃掉
        rows = [
            ["订单号", "客户名称", "下单日期"],
            ["SO-2026-001", "阿尔法", "2026-01-05"],
            ["SO-2026-002", "贝塔", "2026-01-07"],
        ]
        assert detect_header_rows(rows) == 1

    def test_表头不超过配置上限(self) -> None:
        # 每行都带相邻重复（横向合并被填充后的样子），所以逐行都满足「继续当表头」，
        # 此时必须由 max_rows 兜住，不能无限往下吞。
        rows = [["甲", "甲"], ["乙", "乙"], ["丙", "丙"], ["丁", "丁"], ["1", "2"]]
        assert detect_header_rows(rows, max_rows=3) == 3
        assert detect_header_rows(rows, max_rows=1) == 1

    def test_排满且互不相同的行不构成多行表头(self) -> None:
        # 「a / b」这种排满且互不相同的行自己是完整的一行，
        # 下面重复出现说明那是新记录，不是表头的第二层
        assert detect_header_rows([["a", "b"], ["a", "b"], ["a", "b"], ["1", "2"]]) == 1

    def test_空表返回0(self) -> None:
        assert detect_header_rows([]) == 0


class TestLooksLikeHeaderRow:
    def test_纯文字行像表头(self) -> None:
        assert looks_like_header_row(["名称", "数量"]) is True

    def test_含数值的行不像表头(self) -> None:
        assert looks_like_header_row(["键盘", "12"]) is False

    def test_一半是数值就够判成数据行了(self) -> None:
        # DATA_ROW_NUMERIC_RATIO = 0.4
        assert looks_like_header_row(["键盘", "12", "2388", "备注"]) is False

    def test_太空的行不算表头(self) -> None:
        assert looks_like_header_row(["名称", "", "", ""]) is False

    def test_空行不算表头(self) -> None:
        assert looks_like_header_row(["", ""]) is False


class TestSplitStacked:
    def test_空行后跟着表头就切分(self) -> None:
        rows = [["订单号", "客户"], ["SO-001", "甲"], [], ["商品", "数量"], ["键盘", "1"]]
        pieces = split_stacked(rows)
        assert len(pieces) == 2

    def test_单张表不切分(self) -> None:
        rows = [["名称", "数量"], ["键盘", "1"], ["显示器", "2"]]
        assert len(split_stacked(rows)) == 1


class TestApplyMerges:
    def test_横向合并被填充(self) -> None:
        matrix = [["标题", "", ""], ["a", "b", "c"]]
        merged = apply_merges(matrix, [Cell(text="标题", row=0, col=0, row_span=1, col_span=3)])
        assert merged[0] == ["标题", "标题", "标题"]

    def test_纵向合并被填充(self) -> None:
        matrix = [["甲", "1"], ["", "2"]]
        merged = apply_merges(matrix, [Cell(text="甲", row=0, col=0, row_span=2, col_span=1)])
        assert merged[1][0] == "甲"

    def test_越界的合并信息不会崩(self) -> None:
        matrix = [["a", "b"]]
        merged = apply_merges(matrix, [Cell(text="a", row=0, col=0, row_span=9, col_span=9)])
        assert merged == [["a", "b"]]


class TestBuildTablesIntegration:
    def test_参差不齐的行被补齐到同宽(self) -> None:
        table = build_tables([["名称", "数量", "金额"], ["键盘", "1"], ["显示器"]])[0]
        assert all(len(row) == 3 for row in table.body)

    def test_标题孤行并入下一段并被识别为caption(self) -> None:
        table = build_tables([["销售订单明细表", "", ""], ["名称", "数量", "金额"], ["键盘", "1", "199"]])[0]
        assert table.caption == "销售订单明细表"
        assert table.columns == ["名称", "数量", "金额"]
        assert len(table.body) == 1

    def test_重复列名被去重(self) -> None:
        # 多级表头拼接后可能撞名（「一季度_金额」出现两次），必须保证列名唯一，
        # 否则 dict_rows() 会静默丢列
        table = build_tables([["金额", "金额"], ["1", "2"]])[0]
        assert len(set(table.columns)) == len(table.columns)

    def test_dict_rows跳过全空行(self) -> None:
        table = build_tables([["名称", "数量"], ["键盘", "1"], ["", ""]])[0]
        assert table.dict_rows() == [{"名称": "键盘", "数量": "1"}]

    def test_截断会记录原始行数(self) -> None:
        rows = [["名称", "数量"]] + [[f"商品{i}", str(i)] for i in range(20)]
        table = build_tables(rows, max_rows=5)[0]
        assert table.truncated is True
        assert table.original_row_count == 21
        assert table.row_count == 5
