"""安全算术求值（用于跨字段一致性校验）。

校验规则里要表达「金额 = 数量 × 单价」「合计 = 各行之和」这类关系，
最省事的写法是 ``eval("qty * price")`` —— 但**这个项目的输入是用户上传的文档**，
文档里的内容会被填进表达式，用 ``eval`` 等于把任意代码执行权交给上传者。

所以这里走 AST 白名单：先解析成语法树，再只放行允许的节点类型。
``__import__('os').system('rm -rf /')`` 连语法层都过不去，
因为 ``Call``/``Attribute``/``Name`` 里的非白名单标识符都会被拒绝。

同样的实现思路在 Agent 项目（计算器工具）里也用过 —— 这类「安全边界」是真实项目必须有的，
而不是面试八股。
"""

from __future__ import annotations

import ast
import math
import operator
import re

#: 允许的二元运算符
_BIN_OPS: dict[type, object] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

#: 允许的一元运算符
_UNARY_OPS: dict[type, object] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

#: 允许的比较运算符
_CMP_OPS: dict[type, object] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

#: 允许调用的函数（白名单，不是黑名单 —— 黑名单永远漏）
_FUNCS: dict[str, object] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sum": sum,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
}

#: 允许的常量
_CONSTS: dict[str, object] = {
    "pi": math.pi,
    "e": math.e,
    "True": True,
    "False": False,
    "None": None,
}

#: 幂运算的指数上限（``2 ** 10**9`` 能把进程内存吃光）
MAX_POWER = 1000

#: 表达式里的标识符分三类：变量、函数名、常量名。后两类不是「数据来源」，
#: 所以推导「这张明细表需要哪些列」时必须把它们排除，否则 min/max/pi
#: 会被当成列名去找，白白拉低所有表的匹配度。
NON_VARIABLES: frozenset[str] = frozenset({*_FUNCS, *_CONSTS})

#: 标识符词法规则：首字符是字母 / 下划线 / 中文，后续可含数字与下划线。
#: 「一季度_销量」因此被识别为**一个**变量，而不是「一季度」「销量」两个 ——
#: 这一点很关键，中文列名里带分隔符是常态。
_IDENTIFIER_RE = re.compile(r"[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*")


def variables_in(expression: str) -> list[str]:
    """列出表达式引用到的变量名（按首次出现顺序去重）。

    与 :func:`evaluate` 共用同一套词法规则。之前校验层和选表层各写了一份
    ``re.findall``，改一处忘一处就会让两边对同一个表达式的理解悄悄分叉 ——
    现在只有一份定义。
    """
    out: list[str] = []
    seen: set[str] = set()
    for token in _IDENTIFIER_RE.findall(expression or ""):
        if token in NON_VARIABLES or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


class ExpressionError(ValueError):
    """表达式非法（语法错误、包含不允许的节点、变量缺失、结果非数值）。"""


def evaluate(expression: str, variables: dict[str, object]) -> float:
    """在当前变量环境下求值，返回浮点数。

    :raises ExpressionError: 表达式非法或求值失败
    """
    if not expression or not expression.strip():
        raise ExpressionError("表达式为空")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误：{exc.msg}") from exc
    return float(_eval(tree.body, variables))


def _eval(node: ast.AST, variables: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)) or node.value is None:
            return node.value
        raise ExpressionError(f"不支持的常量类型：{type(node.value).__name__}")

    if isinstance(node, ast.Name):
        name = node.id
        if name in variables:
            return variables[name]
        if name in _CONSTS:
            return _CONSTS[name]
        raise ExpressionError(f"未知变量：{name}")

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的运算符：{type(node.op).__name__}")
        left = _eval(node.left, variables)
        right = _eval(node.right, variables)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > MAX_POWER:
            raise ExpressionError(f"幂指数过大（上限 {MAX_POWER}）")
        try:
            return op(left, right)  # type: ignore[operator]
        except ZeroDivisionError as exc:
            raise ExpressionError("除数为零") from exc
        except TypeError as exc:
            raise ExpressionError(f"运算类型不匹配：{exc}") from exc

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的一元运算符：{type(node.op).__name__}")
        return op(_eval(node.operand, variables))  # type: ignore[operator]

    if isinstance(node, ast.Compare):
        left = _eval(node.left, variables)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise ExpressionError(f"不支持的比较运算符：{type(op_node).__name__}")
            right = _eval(comparator, variables)
            if not op(left, right):  # type: ignore[operator]
                return False
            left = right
        return True

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("只允许调用白名单里的函数")
        func = _FUNCS.get(node.func.id)
        if func is None:
            raise ExpressionError(f"不允许调用的函数：{node.func.id}")
        if node.keywords:
            raise ExpressionError("函数调用不支持关键字参数")
        args = [_eval(arg, variables) for arg in node.args]
        try:
            return func(*args)  # type: ignore[operator]
        except Exception as exc:
            raise ExpressionError(f"函数 {node.func.id} 求值失败：{exc}") from exc

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(item, variables) for item in node.elts]

    raise ExpressionError(f"不支持的语法节点：{type(node).__name__}")


def check(expression: str, variables: dict[str, object]) -> bool:
    """把表达式当成断言求值（返回布尔）。"""
    return bool(evaluate(expression, variables))


def close_enough(left: float, right: float, *, tolerance: float = 0.01) -> bool:
    """两个数是否在容差内相等。

    会计校验必须带容差：``0.1 + 0.2 != 0.3`` 在浮点世界里是真的，
    不加容差的「金额对不上」告警会把用户逼疯。
    """
    return abs(float(left) - float(right)) <= abs(float(tolerance))


def relative_close(left: float, right: float, *, relative: float = 1e-6, absolute: float = 0.01) -> bool:
    """相对容差比较，适合金额大小差异很大的场景（1.5 与 1500000 都适用）。"""
    left_f, right_f = float(left), float(right)
    diff = abs(left_f - right_f)
    if diff <= absolute:
        return True
    scale = max(abs(left_f), abs(right_f), 1.0)
    return diff / scale <= relative


__all__ = [
    "ExpressionError",
    "evaluate",
    "check",
    "close_enough",
    "relative_close",
    "MAX_POWER",
]
