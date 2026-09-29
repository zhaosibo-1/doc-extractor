"""校验层测试。

校验层是整个项目里**最独立**的一层：它不依赖文档、不依赖模型，
只需要「字段值 + 明细行 + 规则」三样东西 —— 所以它可以被单独重跑
（``POST /api/revalidate``），也可以在这里被单独测试。
"""

from __future__ import annotations

import pytest

from app.schemas import CrossRule, ExtractedField, FieldSpec
from app.validators import (
    LOW_CONFIDENCE,
    RULE_PENALTY,
    RULE_PENALTY_CAP,
    STATUS_SCORE,
    coerce_value,
    compute_completeness,
    run_rules,
    validate_field,
)


def make_field(
    key: str,
    value=None,
    *,
    raw: str = "",
    confidence: float = 0.95,
    label: str = "",
    status: str = "ok",
) -> ExtractedField:
    """造一个**默认已通过校验**的字段。

    注意 ``ExtractedField`` 的默认 ``status`` 是 ``missing``（因为它是「还没校验」
    的初始态）。测试里若忘记显式设置状态，完成度算出来永远是 0，
    会让人误以为公式坏了。
    """
    return ExtractedField(
        key=key,
        label=label or key,
        value=value,
        raw=raw or str(value if value is not None else ""),
        confidence=confidence,
        status=status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 类型强转
# ---------------------------------------------------------------------------


class TestCoerceValue:
    def test_数值类型接受中文写法(self) -> None:
        spec = FieldSpec(key="amount", type="number")
        assert coerce_value(spec, "￥2,388.00")[0] == 2388.0
        assert coerce_value(spec, "1.2万")[0] == 12000.0

    def test_数值类型解析不了要报错(self) -> None:
        # 报错时保留原值（供用户核对），但必须带上错误说明
        value, error = coerce_value(FieldSpec(key="amount", type="number"), "两千")
        assert value == "两千"
        assert error == "「两千」不是合法数值"

    def test_数字后面带单位仍能解析(self) -> None:
        assert coerce_value(FieldSpec(key="a", type="number"), "12 套")[0] == 12.0
        assert coerce_value(FieldSpec(key="a", type="integer"), "12 套")[0] == 12

    def test_日期类型归一成ISO(self) -> None:
        spec = FieldSpec(key="d", type="date")
        assert coerce_value(spec, "2026年3月12日")[0] == "2026-03-12"

    def test_布尔类型(self) -> None:
        spec = FieldSpec(key="b", type="boolean")
        assert coerce_value(spec, "是")[0] is True
        assert coerce_value(spec, "否")[0] is False

    def test_列表类型按分隔符切分(self) -> None:
        spec = FieldSpec(key="l", type="list")
        assert coerce_value(spec, "服务器、交换机；存储阵列")[0] == ["服务器", "交换机", "存储阵列"]

    def test_整数类型(self) -> None:
        assert coerce_value(FieldSpec(key="i", type="integer"), "12 套")[0] == 12


# ---------------------------------------------------------------------------
# 单字段校验
# ---------------------------------------------------------------------------


class TestValidateField:
    def test_必填缺失会给出说明(self) -> None:
        spec = FieldSpec(key="total", label="价税合计", type="number", required=True)
        field = validate_field(spec, ExtractedField(key="total", label="价税合计"))
        assert field.status == "missing"
        assert field.required is True
        assert any("必填" in m for m in field.messages)

    def test_正则不匹配判为invalid但保留原值(self) -> None:
        # keep_on_invalid 默认 True：用户需要看到「抽到了什么」才能判断是规则太严
        # 还是文档本身有问题。直接丢掉值等于把线索也丢了。
        spec = FieldSpec(key="phone", label="电话", must_match=r"1[3-9]\d{9}")
        field = validate_field(spec, make_field("phone", "12345", raw="12345"))
        assert field.status == "invalid"
        assert field.value == "12345"
        assert any("格式" in m or "不匹配" in m for m in field.messages)

    def test_枚举不命中判为invalid(self) -> None:
        spec = FieldSpec(key="invoice_type", enum=["电子普通发票", "增值税专用发票"])
        field = validate_field(spec, make_field("invoice_type", "收据"))
        assert field.status == "invalid"

    def test_超范围判为invalid(self) -> None:
        spec = FieldSpec(key="amount", type="number", minimum=0.0)
        field = validate_field(spec, make_field("amount", "-5"))
        assert field.status == "invalid"

    def test_低置信度被标成low_confidence(self) -> None:
        spec = FieldSpec(key="x", type="string")
        field = validate_field(spec, make_field("x", "模型猜的", confidence=LOW_CONFIDENCE - 0.05))
        assert field.status == "low_confidence"

    def test_置信度足够就是ok(self) -> None:
        spec = FieldSpec(key="x", type="string")
        field = validate_field(spec, make_field("x", "规则抽的", confidence=0.95))
        assert field.status == "ok"


# ---------------------------------------------------------------------------
# 跨字段规则
# ---------------------------------------------------------------------------


class TestRunRules:
    def test_算术关系通过(self) -> None:
        rules = [CrossRule(key="t", kind="expr", fields=["amount", "tax"], expression="amount + tax", target="total")]
        fields = {
            "amount": make_field("amount", 2104.42),
            "tax": make_field("tax", 283.58),
            "total": make_field("total", 2388.0),
        }
        result = run_rules(rules, fields, [])
        assert result[0].passed and not result[0].skipped

    def test_不传容差时用规则自己的(self) -> None:
        """``tolerance=None`` 是「别动规则的容差」，不是「容差等于 0」。

        回归用：旧实现写的是 ``tol = rule.tolerance if rule.tolerance else tolerance``，
        而 ``CrossRule.tolerance`` 有默认值 0.02（恒为真），于是外部传进来的
        容差永远被忽略 —— 接口上挂着一个怎么调都没反应的旋钮。
        """
        rule = CrossRule(
            key="t", kind="expr", fields=["amount", "tax"],
            expression="amount + tax", target="total", tolerance=5.0,
        )
        fields = {
            "amount": make_field("amount", 100.0),
            "tax": make_field("tax", 13.0),
            "total": make_field("total", 116.0),  # 差 3.0
        }
        assert run_rules([rule], fields, [])[0].passed is True
        # 传了更严的全局容差 → 必须以全局为准（覆盖，而不是被规则挡住）
        assert run_rules([rule], fields, [], tolerance=1.0)[0].passed is False

    def test_全局容差覆盖规则自带容差(self) -> None:
        rule = CrossRule(
            key="t", kind="expr", fields=["amount", "tax"],
            expression="amount + tax", target="total", tolerance=0.02,
        )
        fields = {
            "amount": make_field("amount", 100.0),
            "tax": make_field("tax", 13.0),
            "total": make_field("total", 200.0),  # 差 87
        }
        assert run_rules([rule], fields, [])[0].passed is False
        assert run_rules([rule], fields, [], tolerance=100.0)[0].passed is True

    def test_大金额下的容差仍是绝对值(self) -> None:
        """2388 元的发票差 47.76 元必须报失败 —— 容差是 2 分钱，不是 2%。

        回归用：判定式一度写成 ``diff <= max(tol, tol * abs(computed))``，
        金额一大，``tol * |value|`` 就吞掉绝对容差，实际容差变成「2% × 金额」。
        47.76 元的税额差错（少算了一个税率档）会被判成「勾稽通过」。
        """
        rule = CrossRule(
            key="amt", kind="expr", fields=["amount", "tax"],
            expression="amount + tax", target="total", tolerance=0.02,
        )
        fields = {
            "amount": make_field("amount", 2104.42),
            "tax": make_field("tax", 235.82),      # 正确的应是 283.58
            "total": make_field("total", 2388.0),
        }
        result = run_rules([rule], fields, [])
        assert result[0].passed is False
        assert "47.76" in (result[0].detail or "")

    def test_算术关系失败会给出差额(self) -> None:
        rules = [CrossRule(key="t", kind="expr", fields=["amount", "tax"], expression="amount + tax", target="total")]
        fields = {
            "amount": make_field("amount", 100.0),
            "tax": make_field("tax", 13.0),
            "total": make_field("total", 200.0),
        }
        result = run_rules(rules, fields, [])
        assert result[0].passed is False
        assert "87" in result[0].detail  # 差额要报出来，否则用户还得自己算

    def test_逐行算术_数量乘单价(self) -> None:
        rules = [CrossRule(key="line", kind="row_expr", expression="数量 * 单价", column="金额")]
        rows = [
            {"数量": "12", "单价": "199.00", "金额": "2388.00"},
            {"数量": "5", "单价": "499.50", "金额": "2497.50"},
        ]
        result = run_rules(rules, {}, rows)
        assert result[0].passed and not result[0].skipped
        assert "2 行全部满足" in result[0].detail

    def test_逐行算术_能定位到具体哪一行错了(self) -> None:
        rules = [CrossRule(key="line", kind="row_expr", expression="数量 * 单价", column="金额")]
        rows = [
            {"数量": "12", "单价": "199.00", "金额": "2388.00"},
            {"数量": "5", "单价": "499.50", "金额": "9999.00"},  # 手工改过
        ]
        result = run_rules(rules, {}, rows)
        assert result[0].passed is False
        assert "第 2 行" in result[0].detail

    def test_逐行算术_列名对不上时明确报出缺哪列(self) -> None:
        """含糊地说「没有可校验的数据行」会把「选错了明细表」伪装成

        「这份文档本来就没有数据」，用户根本无从判断。
        """
        rules = [CrossRule(key="line", kind="row_expr", expression="数量 * 单价", column="金额")]
        rows = [{"产品": "键盘", "地区": "北京"}]
        result = run_rules(rules, {}, rows)
        assert result[0].skipped is True
        detail = result[0].detail
        assert "找不到列" in detail
        assert "数量" in detail and "单价" in detail and "金额" in detail
        # 现有列也要列出来，用户才知道系统看到了什么
        assert "产品" in detail

    def test_英文列名靠别名解析(self) -> None:
        """模板用中文规范列名书写，靠 column_aliases 落到英文实际列名上。

        没有这张别名表，一份英文列头的销售订单报表会让所有行级校验静默跳过 ——
        而「跳过」在界面上看起来跟「没有问题」几乎一样。
        """
        rules = [
            CrossRule(
                key="line",
                kind="row_expr",
                expression="数量 * 单价",
                column="金额",
                column_aliases={"数量": ["Qty"], "单价": ["Unit Price"], "金额": ["Amount"]},
            )
        ]
        rows = [{"Item": "Keyboard", "Qty": "12", "Unit Price": "199.00", "Amount": "2388.00"}]
        result = run_rules(rules, {}, rows)
        assert result[0].passed and not result[0].skipped
        assert "12 * 199" in result[0].detail or "全部满足" in result[0].detail

    def test_别名不会放宽成包含匹配(self) -> None:
        # 「一季度_金额」含「金额」两个字，但它是**另一个列**。
        # 接受它会让规则拿错误的列算出一个「通过」，比跳过更危险。
        rules = [CrossRule(key="s", kind="sum_column", target="total", column="金额", column_aliases={"金额": ["Amount"]})]
        rows = [{"一季度_金额": "100"}, {"一季度_金额": "200"}]
        fields = {"total": make_field("total", 300.0)}
        result = run_rules(rules, fields, rows)
        assert result[0].skipped is True
        assert "找不到列" in result[0].detail

    def test_明细合计(self) -> None:
        rules = [CrossRule(key="s", kind="sum_column", target="total", column="金额")]
        rows = [{"金额": "2388.00"}, {"金额": "2497.50"}, {"金额": "3897.00"}]
        fields = {"total": make_field("total", 8782.50, label="订单金额")}
        result = run_rules(rules, fields, rows)
        assert result[0].passed
        assert "8782.50" in result[0].detail

    def test_明细合计对不上要报差额(self) -> None:
        rules = [CrossRule(key="s", kind="sum_column", target="total", column="金额")]
        rows = [{"金额": "100.00"}]
        fields = {"total": make_field("total", 999.0, label="订单金额")}
        result = run_rules(rules, fields, rows)
        assert result[0].passed is False
        assert "899" in result[0].detail

    def test_日期先后(self) -> None:
        rules = [CrossRule(key="d", kind="date_order", fields=["start_date", "end_date"])]
        fields = {
            "start_date": make_field("start_date", "2026-05-01"),
            "end_date": make_field("end_date", "2027-04-30"),
        }
        assert run_rules(rules, fields, [])[0].passed is True
        fields["end_date"] = make_field("end_date", "2025-01-01")
        assert run_rules(rules, fields, [])[0].passed is False

    def test_同时必填(self) -> None:
        rules = [CrossRule(key="r", kind="required_together", fields=["phone", "email"])]
        both = {"phone": make_field("phone", "13800000000"), "email": make_field("email", "a@b.com")}
        assert run_rules(rules, both, [])[0].passed is True
        only_phone = {"phone": make_field("phone", "13800000000"), "email": make_field("email")}
        assert run_rules(rules, only_phone, [])[0].passed is False

    def test_参与字段没填齐时报跳过而不是失败(self) -> None:
        """「跳过」与「失败」必须分开：把无法判定说成失败会让用户去改一份没问题的文档。"""
        rules = [CrossRule(key="d", kind="date_order", fields=["a", "b"])]
        result = run_rules(rules, {"a": make_field("a", "2026-01-01")}, [])
        assert result[0].skipped is True
        assert result[0].passed is True

    def test_没有明细数据时报跳过(self) -> None:
        rules = [CrossRule(key="s", kind="sum_column", target="total", column="金额")]
        result = run_rules(rules, {"total": make_field("total", 100.0)}, [])
        assert result[0].skipped is True

    def test_单条规则出错不会让整次校验崩掉(self) -> None:
        rules = [CrossRule(key="bad", kind="expr", expression="a +", fields=["a"], target="a")]
        result = run_rules(rules, {"a": make_field("a", 1.0)}, [])
        assert result[0].skipped is True
        assert "出错" in result[0].detail or "跳过" in result[0].detail


# ---------------------------------------------------------------------------
# 完成度
# ---------------------------------------------------------------------------


class TestComputeCompleteness:
    def test_全通过是100(self) -> None:
        fields = [make_field("a", "x"), make_field("b", "y")]
        assert compute_completeness(fields, []) == 100.0

    def test_必填缺失扣分多于选填(self) -> None:
        # 用「一个填好了 + 一个缺失」来比较，才能真正看出权重差异：
        # 全缺失时两边都是 0 分，看不出任何区别。
        def score(*, required: bool) -> float:
            present = make_field("present", "值")
            absent = validate_field(FieldSpec(key="absent", required=required), make_field("absent", status="missing"))
            return compute_completeness([present, absent], [])

        assert score(required=True) == pytest.approx(25.0)  # 权重 1/4
        assert score(required=False) == pytest.approx(50.0)  # 权重 1/2
        assert score(required=True) < score(required=False)

    def test_校验失败扣分多于缺失(self) -> None:
        # 抽到了但值不合法，比根本没抽到更值得处理 —— 它是「有内容但不自洽」
        invalid = [make_field("a", "x")] 
        invalid[0].status = "invalid"
        missing = [make_field("a")]
        missing[0].status = "missing"
        assert compute_completeness(invalid, []) > compute_completeness(missing, [])

    def test_规则失败按条扣分且有上限(self) -> None:
        fields = [make_field(f"f{i}", "x") for i in range(10)]
        assert compute_completeness(fields, []) == 100.0

        from app.schemas import RuleResult

        one = [RuleResult(key="r", kind="expr", passed=False)]
        assert compute_completeness(fields, one) == 100.0 - RULE_PENALTY

        many = [RuleResult(key=f"r{i}", kind="expr", passed=False) for i in range(50)]
        assert compute_completeness(fields, many) == 100.0 - RULE_PENALTY_CAP

    def test_跳过的规则不扣分(self) -> None:
        from app.schemas import RuleResult

        fields = [make_field("a", "x")]
        skipped = [RuleResult(key="r", kind="expr", passed=True, skipped=True)]
        assert compute_completeness(fields, skipped) == 100.0

    def test_soft规则失败不扣分(self) -> None:
        from app.schemas import RuleResult

        fields = [make_field("a", "x")]
        soft = [RuleResult(key="r", kind="expr", passed=False, soft=True)]
        assert compute_completeness(fields, soft) == 100.0

    def test_完成度可以手算核对(self) -> None:
        """完成度必须能被人手工算出来，否则用户无法理解这个数字是怎么来的。

        公式：``Σ(权重 × 状态得分) / Σ权重 × 100 - 规则扣分``
        """
        fields = [
            make_field("required_ok", "x", status="ok", label="必填且正常"),
            make_field("required_missing", None, status="missing", label="必填但缺失"),
            make_field("optional_ok", "y", status="ok", label="选填且正常"),
        ]
        fields[0].required = True
        fields[1].required = True

        # 权重：必填 3 ×2，选填 1 ×1 → 分母 7
        # 得分：1.0 + 0.0 + 1.0 → 分子 4
        expected = (3 * STATUS_SCORE["ok"] + 3 * STATUS_SCORE["missing"] + 1 * STATUS_SCORE["ok"]) / 7 * 100
        assert expected == pytest.approx(4 / 7 * 100)
        assert compute_completeness(fields, []) == pytest.approx(round(expected, 1))

    def test_空字段列表只有规则扣分(self) -> None:
        # 刻意如此：没有字段可评价时不判 0 分，只扣规则分。
        # 判 0 会让「通用模板 + 一条规则失败」看起来像整次抽取都崩了。
        from app.schemas import RuleResult

        assert compute_completeness([], []) == 100.0
        failed = [RuleResult(key="r", kind="expr", passed=False)]
        assert compute_completeness([], failed) == 100.0 - RULE_PENALTY
