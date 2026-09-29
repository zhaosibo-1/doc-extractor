"""安全求值测试。

这里的测试目标是「**恶意输入必须连语法层都过不去**」——
因为表达式的内容来自用户上传的文档，等价于把任意代码执行权交出去。
另外覆盖 ``variables_in``：它被选表层和校验层共用，
一旦它多认或少认一个变量，两边对同一个表达式的理解就会悄悄分叉。
"""

from __future__ import annotations

import pytest

from app import safe_eval


class TestEvaluate:
    def test_基本四则运算(self) -> None:
        assert safe_eval.evaluate("数量 * 单价", {"数量": 12, "单价": 199.0}) == pytest.approx(2388.0)
        assert safe_eval.evaluate("金额 + 税额", {"金额": 2104.42, "税额": 283.58}) == pytest.approx(2388.0)
        assert safe_eval.evaluate("(a - b) / 2", {"a": 10, "b": 4}) == pytest.approx(3.0)

    def test_白名单函数可用(self) -> None:
        assert safe_eval.evaluate("round(a / b, 2)", {"a": 10, "b": 3}) == pytest.approx(3.33)
        assert safe_eval.evaluate("max(a, b)", {"a": 1, "b": 9}) == 9
        assert safe_eval.evaluate("abs(a - b)", {"a": 1, "b": 9}) == 8

    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('echo pwned')",
            "open('/etc/passwd').read()",
            "(1).__class__.__bases__[0].__subclasses__()",
            "exec('x = 1')",
            "eval('1+1')",
            "globals()",
            "a.__class__",
            "lambda: 1",
            "[x for x in range(3)]",
        ],
    )
    def test_危险表达式一律拒绝(self, expression: str) -> None:
        with pytest.raises(safe_eval.ExpressionError):
            safe_eval.evaluate(expression, {"a": 1})

    def test_语法错误被归一成ExpressionError(self) -> None:
        with pytest.raises(safe_eval.ExpressionError):
            safe_eval.evaluate("1 +", {})

    def test_缺变量报错而不是当成0(self) -> None:
        # 把缺失变量当 0 会让「金额 = 数量 × 单价」在单价缺失时算出 0，
        # 然后与票面的非零金额比对，报出一个看起来真实的错误结论。
        with pytest.raises(safe_eval.ExpressionError):
            safe_eval.evaluate("数量 * 单价", {"数量": 3})

    def test_结果非数值时报错(self) -> None:
        with pytest.raises(safe_eval.ExpressionError):
            safe_eval.evaluate("'a' + 'b'", {})

    def test_超大幂运算被拦住(self) -> None:
        # 2 ** 10**9 能把进程内存吃光
        with pytest.raises(safe_eval.ExpressionError):
            safe_eval.evaluate("2 ** 100000000", {})


class TestVariablesIn:
    def test_中文列名是一个整体变量(self) -> None:
        # 「一季度_销量」必须被识别成一个变量，而不是「一季度」「销量」两个 ——
        # 中文列名里带分隔符是常态。
        assert safe_eval.variables_in("一季度_销量 * 4") == ["一季度_销量"]

    def test_英文列名与下划线(self) -> None:
        assert safe_eval.variables_in("unit_price * qty") == ["unit_price", "qty"]

    def test_函数名与常量不算变量(self) -> None:
        # 这是选明细表时的关键：min/max/pi 被当成列名去找会拉低所有表的匹配度
        assert safe_eval.variables_in("round(pi * r, 2)") == ["r"]

    def test_去重且保持出现顺序(self) -> None:
        assert safe_eval.variables_in("b + a + b") == ["b", "a"]

    def test_空表达式(self) -> None:
        assert safe_eval.variables_in("") == []
        assert safe_eval.variables_in("1 + 2") == []


class TestHelpers:
    def test_close_enough按绝对容差比较(self) -> None:
        assert safe_eval.close_enough(100.0, 100.01, tolerance=0.02) is True
        assert safe_eval.close_enough(100.0, 100.5, tolerance=0.02) is False

    def test_check返回布尔(self) -> None:
        assert safe_eval.check("a > b", {"a": 2, "b": 1}) is True
        assert safe_eval.check("a > b", {"a": 1, "b": 2}) is False
