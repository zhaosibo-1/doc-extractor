"""校验层：类型强转 + 单字段校验 + 跨字段一致性。

为什么要把校验单独拎出来做一层
------------------------------
「抽取」和「校验」是两种完全不同的失效模式：

- 抽取错了 → 值根本不对（拿到「￥2,38.00」这种）；
- 抽取对了但**不合逻辑** → 每个字段单看都正常，但「数量×单价 ≠ 金额」
  「明细合计 ≠ 总额」「合同截止日早于起始日」。

第二类错误单字段校验永远发现不了，而它恰恰是财务/合同类文档里最要命的问题。
所以这一层是独立可测的：给它一组字段值，它给出「哪里对不上」。

另一条设计原则：**校验失败不丢值**。默认保留原值并标记 ``invalid``，
让用户自己判断 —— 直接丢弃会让人以为「文档里根本没有这个字段」。
"""

from __future__ import annotations

import re
from typing import Any

from . import safe_eval
from .schemas import CrossRule, ExtractedField, FieldSpec, RuleResult
from .textutil import collapse_ws, normalize_key, resolve_column, to_bool, to_date, to_int, to_number

#: 字段得分权重：必填字段更重要
WEIGHT_REQUIRED = 3.0
WEIGHT_OPTIONAL = 1.0

#: 各状态在完成度里的得分
STATUS_SCORE: dict[str, float] = {
    "ok": 1.0,
    "low_confidence": 0.85,
    "invalid": 0.4,
    "missing": 0.0,
}

#: 每条失败的非 soft 规则扣多少分，以及总扣分上限
RULE_PENALTY = 8.0
RULE_PENALTY_CAP = 30.0

#: 校验阶段认为「可信度偏低」的阈值
LOW_CONFIDENCE = 0.65


# ---------------------------------------------------------------------------
# 类型强转
# ---------------------------------------------------------------------------


def coerce_value(spec: FieldSpec, raw: str) -> tuple[Any, str | None]:
    """按字段类型强转，返回 ``(值, 错误信息)``。错误信息为 ``None`` 表示成功。"""
    text = collapse_ws(raw)
    if not text:
        return None, None

    kind = spec.type
    if kind == "number":
        value = to_number(text)
        return (value, None) if value is not None else (text, f"「{text}」不是合法数值")
    if kind == "integer":
        value = to_int(text)
        return (value, None) if value is not None else (text, f"「{text}」不是合法整数")
    if kind == "date":
        value = to_date(text)
        return (value, None) if value is not None else (text, f"「{text}」不是可识别的日期")
    if kind == "boolean":
        value = to_bool(text)
        return (value, None) if value is not None else (text, f"「{text}」不是可识别的布尔值")
    if kind == "list":
        return split_list(text), None
    return text, None


#: 列表型字段的分隔符（中英文都要覆盖，「、」在中文文档里最常见）
_LIST_SEPARATORS = re.compile(r"[、,，;；\n]+")


def split_list(text: str) -> list[str]:
    """把一个单元格里的多项内容拆成列表。

    同时处理「1. 甲 2. 乙」这类编号列表：去掉行首序号。
    """
    parts = [collapse_ws(p) for p in _LIST_SEPARATORS.split(text)]
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        cleaned = re.sub(r"^\s*(?:\d+|[一二三四五六七八九十]+)\s*[.、)．]\s*", "", part)
        if cleaned:
            out.append(cleaned)
    return out or ([collapse_ws(text)] if collapse_ws(text) else [])


# ---------------------------------------------------------------------------
# 单字段校验
# ---------------------------------------------------------------------------


def validate_field(spec: FieldSpec, field: ExtractedField, *, tolerance: float = 0.0) -> ExtractedField:
    """就地校验一个字段，更新 ``status`` 与 ``messages``。

    顺序：空值 → 类型强转 → 枚举 → 范围 → 正则。
    一旦在某一步判定失败就**停止后续校验**（后面的检查都建立在前面的结果上）。
    """
    field.label = field.label or spec.display()
    field.type = spec.type
    field.required = spec.required

    raw = collapse_ws(field.raw) if field.raw else ("" if field.value is None else collapse_ws(str(field.value)))
    if not raw and field.value is None:
        field.status = "missing"
        if spec.required:
            field.messages.append("必填字段未在文档中找到")
        return field

    # ---- 类型强转 ----
    value, error = coerce_value(spec, raw)
    if error:
        field.messages.append(error)
        field.status = "invalid"
        field.value = value if spec.keep_on_invalid else None
        return field
    field.value = value

    # ---- 枚举 ----
    if spec.enum and value is not None:
        if not _enum_hit(value, spec.enum):
            field.messages.append(
                f"「{value}」不在允许值范围内（{'、'.join(spec.enum)}）"
            )
            field.status = "invalid"
            if not spec.keep_on_invalid:
                field.value = None
            return field

    # ---- 数值范围 ----
    if spec.type in {"number", "integer"} and isinstance(value, (int, float)):
        if spec.minimum is not None and float(value) < float(spec.minimum) - tolerance:
            field.messages.append(f"值 {value} 小于下限 {spec.minimum}")
            field.status = "invalid"
            return field
        if spec.maximum is not None and float(value) > float(spec.maximum) + tolerance:
            field.messages.append(f"值 {value} 大于上限 {spec.maximum}")
            field.status = "invalid"
            return field

    # ---- 校验用正则 ----
    if spec.must_match:
        try:
            pattern = re.compile(spec.must_match)
        except re.error as exc:
            field.messages.append(f"字段定义的校验正则非法：{exc}")
        else:
            if not pattern.fullmatch(raw):
                field.messages.append(f"「{raw}」不符合格式要求 {spec.must_match}")
                field.status = "invalid"
                return field

    # ---- 非空但类型是列表却解析出空列表 ----
    if spec.type == "list" and not value:
        field.status = "missing"
        if spec.required:
            field.messages.append("必填字段未在文档中找到")
        return field

    field.status = "low_confidence" if field.confidence < LOW_CONFIDENCE else "ok"
    return field


def _enum_hit(value: Any, enum: list[str]) -> bool:
    """枚举匹配：归一化后比较（大小写、空格、全角半角都不敏感）。"""
    normalized = normalize_key(str(value))
    return any(normalize_key(item) == normalized for item in enum)


# ---------------------------------------------------------------------------
# 跨字段规则
# ---------------------------------------------------------------------------


def run_rules(
    rules: list[CrossRule],
    fields: dict[str, ExtractedField],
    rows: list[dict[str, str]],
    *,
    tolerance: float | None = None,
) -> list[RuleResult]:
    """逐条执行跨字段规则。

    ``tolerance`` 是**全局覆盖**，不是兜底默认值：

    - ``None``（默认）→ 每条规则各用自己的 ``rule.tolerance``。
      模板作者把「金额 = 数量 × 单价」写成 0.02、「明细合计 = 总额」写成 1.0，
      这些是**刻意的**，不能被动过。
    - 传了数值 → 覆盖**所有**规则的容差。

    这里曾经写成 ``tol = rule.tolerance if rule.tolerance else tolerance``，
    看起来像「规则优先、否则用全局」，但因为 :class:`CrossRule` 的 ``tolerance``
    有默认值 0.02（恒为真），全局参数永远轮不到生效 —— 接口上挂着一个
    完全不起作用的旋钮。容差是用户唯一能在界面上调的参数，
    它必须真的能调。
    """
    return [_run_one(rule, fields, rows, tolerance) for rule in rules]


def _run_one(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tolerance: float | None
) -> RuleResult:
    tol = rule.tolerance if tolerance is None else tolerance
    result = RuleResult(key=rule.key, kind=rule.kind, passed=True, description=rule.describe(), soft=rule.soft)

    handler = {
        "equals": _rule_equals,
        "expr": _rule_expr,
        "row_expr": _rule_row_expr,
        "sum_column": _rule_sum_column,
        "row_count": _rule_row_count,
        "date_order": _rule_date_order,
        "required_together": _rule_required_together,
    }.get(rule.kind)

    if handler is None:  # 理论上被 Literal 挡住，留个兜底更安全
        result.passed = True
        result.skipped = True
        result.detail = f"未知规则类型：{rule.kind}"
        return result

    try:
        passed, detail, skipped = handler(rule, fields, rows, tol)
    except Exception as exc:  # 单条规则出错不该让整次校验崩掉
        passed, detail, skipped = True, f"规则执行出错，已跳过：{exc}", True

    result.passed = passed
    result.detail = detail
    result.skipped = skipped
    return result


def _numeric(field: ExtractedField | None) -> float | None:
    """从字段里取数值（不管声明类型，能用就用）。"""
    if field is None or field.value is None:
        return None
    if isinstance(field.value, bool):
        return None
    if isinstance(field.value, (int, float)):
        return float(field.value)
    if isinstance(field.value, list):
        return None
    return to_number(str(field.value))


def _missing(rule: CrossRule, fields: dict[str, ExtractedField], names: list[str]) -> list[str]:
    """返回参与字段里「没值」的那些名字。"""
    out: list[str] = []
    for name in names:
        field = fields.get(name)
        if field is None or field.value is None or field.value == "":
            out.append(name)
    return out


def _variables(rule: CrossRule, fields: dict[str, ExtractedField]) -> dict[str, object]:
    variables: dict[str, object] = {}
    for name in rule.fields:
        field = fields.get(name)
        if field is None:
            continue
        if isinstance(field.value, (int, float)) and not isinstance(field.value, bool):
            variables[name] = float(field.value)
        elif isinstance(field.value, str):
            number = to_number(field.value)
            if number is not None:
                variables[name] = number
    return variables


def _rule_equals(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    if len(rule.fields) < 2:
        return True, "规则需要至少两个字段", True
    missing = _missing(rule, fields, rule.fields)
    if missing:
        return True, f"参与字段未填齐（{'、'.join(missing)}），已跳过", True

    values = [fields[name].value for name in rule.fields]
    left, right = values[0], values[1]
    left_num, right_num = _numeric(fields[rule.fields[0]]), _numeric(fields[rule.fields[1]])
    if left_num is not None and right_num is not None:
        ok = safe_eval.close_enough(left_num, right_num, tolerance=tol)
        detail = f"{left} 与 {right} {'一致' if ok else '不一致'}（容差 {tol}）"
        return ok, detail, False

    ok = normalize_key(str(left)) == normalize_key(str(right))
    return ok, f"「{left}」与「{right}」{'一致' if ok else '不一致'}", False


def _rule_expr(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    if not rule.expression:
        return True, "未提供表达式", True

    variables = _variables(rule, fields)
    missing = [name for name in rule.fields if name not in variables]
    if missing:
        return True, f"表达式变量缺少数值（{'、'.join(missing)}），已跳过", True

    try:
        computed = safe_eval.evaluate(rule.expression, variables)
    except safe_eval.ExpressionError as exc:
        return True, f"表达式无法求值（{exc}），已跳过", True

    if not rule.target:
        ok = bool(computed)
        return ok, f"{rule.expression} → {computed}", False

    target = fields.get(rule.target)
    target_num = _numeric(target)
    if target_num is None:
        return True, f"目标字段「{rule.target}」没有数值，已跳过", True

    diff = abs(target_num - computed)
    # 容差是**绝对值**：0.02 就是 2 分钱，跟金额多大无关。后面那一项只是给
    # 浮点误差留的极小余量（1e-6 相对量，100 万元也才 1 元），量级上不可能
    # 盖过绝对容差。
    #
    # 这里曾经写成 `max(tol, tol * abs(computed))` —— 把相对系数也写成了 tol。
    # 后果是只要金额大于 1 元，`tol * |value|` 就必然大于 `tol`，绝对容差被
    # 静默丢掉，实际容差变成「2% × 金额」：一份 2388 元的发票被允许差 47.76 元，
    # 发票勾稽形同虚设，而界面上还老老实实写着「容差 0.02」。
    # 同一个函数里 _rule_equals / _rule_sum_column 用的都是绝对容差，
    # 只有这里不一致 —— 一致性本身就是发现它的线索。
    ok = safe_eval.close_enough(target_num, computed, tolerance=max(tol, abs(computed) * 1e-6))
    detail = (
        f"按 {rule.expression} 计算得 {computed:.2f}，"
        f"文档里「{target.label}」为 {target_num:.2f}，"
        f"差 {diff:.2f}（容差 {tol}）"
    )
    return ok, detail, False


def _resolve_row_columns(rule: CrossRule, columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """解析规则用到的所有列，返回 ``(规范名 → 真实列名, 解析失败的名字)``。

    ``row_expr`` 的表达式变量本身就是列名（``数量 * 单价``），所以它们和
    ``column`` 走同一套解析。这样只要模板里声明了别名，一份英文列头的报表
    就能和中文列头的报表跑同一套校验 —— 而不是各自写一套规则。

    这里调的是 :func:`~app.textutil.resolve_column`，与选明细表层**同一个函数**：
    两层对「这张表有没有这一列」必须给出同一个答案。
    """
    names: list[str] = []
    if rule.column:
        names.append(rule.column)
    if rule.kind == "row_expr" and rule.expression:
        for token in safe_eval.variables_in(rule.expression):
            if token not in names:
                names.append(token)

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        column = resolve_column(name, columns, rule.column_aliases.get(name, []))
        if column:
            resolved[name] = column
        else:
            missing.append(name)
    return resolved, missing


def _rule_row_expr(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    """逐行算术校验：每行都满足表达式与目标列一致。

    典型用法：``数量 * 单价`` 应等于 ``金额`` 列。
    汇总加起来对得上、但单行算错的情况在真实报表里很常见
    （比如某一行手工改过金额），只做合计校验是发现不了的。
    """
    if not rule.expression or not rule.column:
        return True, "规则需要同时指定 expression 与 column", True
    if not rows:
        return True, "没有明细数据，已跳过", True

    columns = list(rows[0].keys())
    # 表达式变量（列）与目标列一起解析；解析不出来的名字会明确报出来
    mapping, missing = _resolve_row_columns(rule, columns)
    if missing:
        # 说清「缺哪一列」。含糊地说「没有可校验的数据行」，会把「选错了明细表」
        # 这种真问题伪装成「这份文档本来就没有数据」。
        return (
            True,
            f"明细表里找不到列 {'、'.join(missing)}（现有列：{'、'.join(columns)}），已跳过",
            True,
        )

    target_column = mapping[rule.column]
    # 表达式仍然按**规范列名**书写，求值时把规范名绑到真实列的值上。
    # （不重写表达式字符串：英文列名常带空格，如「Unit Price」，
    #   重写成表达式后不再是合法标识符，根本没法解析。）
    expression_vars = safe_eval.variables_in(rule.expression)

    bad: list[str] = []
    checked = 0
    skipped_rows = 0
    for index, row in enumerate(rows):
        variables: dict[str, object] = {}
        usable = True
        for name in expression_vars:
            number = to_number(row.get(mapping[name], ""))
            if number is None:
                usable = False
                break
            variables[name] = number
        if not usable:
            skipped_rows += 1
            continue

        try:
            expected = safe_eval.evaluate(rule.expression, variables)
        except safe_eval.ExpressionError:
            skipped_rows += 1
            continue

        actual = to_number(row.get(target_column, ""))
        if actual is None:
            skipped_rows += 1
            continue

        checked += 1
        if abs(actual - expected) > max(tol, abs(expected) * 1e-6):
            bad.append(f"第 {index + 1} 行：{rule.expression}={expected:.2f}，{rule.column}={actual:.2f}")

    if checked == 0:
        return True, f"列都在，但 {skipped_rows} 行都没有可用数值，已跳过", True

    if bad:
        detail = f"{checked} 行中有 {len(bad)} 行对不上：" + "；".join(bad[:3])
        if skipped_rows:
            detail += f"（另有 {skipped_rows} 行缺少数值，未参与校验）"
        return False, detail, False

    detail = f"{checked} 行全部满足 {rule.expression} = {rule.column}"
    if skipped_rows:
        detail += f"（{skipped_rows} 行缺少数值，未参与校验）"
    return True, detail, False


def _rule_sum_column(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    if not rule.column or not rule.target:
        return True, "规则需要指定 column 与 target", True
    if not rows:
        return True, "没有明细数据，已跳过", True
    if rule.target not in fields:
        return True, f"目标字段「{rule.target}」不存在，已跳过", True

    columns = list(rows[0].keys())
    column = resolve_column(rule.column, columns, rule.column_aliases.get(rule.column, []))
    if not column:
        return (
            True,
            f"明细表里找不到列「{rule.column}」（现有列：{'、'.join(columns)}），已跳过",
            True,
        )

    total = 0.0
    counted = 0
    unparsable: list[str] = []
    for row in rows:
        cell = row.get(column)
        if cell is None:
            continue
        number = to_number(cell)
        if number is None:
            if collapse_ws(str(cell)):
                unparsable.append(collapse_ws(str(cell)))
            continue
        total += number
        counted += 1

    if counted == 0:
        return True, f"明细列「{rule.column}」没有可解析的数值，已跳过", True

    target_num = _numeric(fields[rule.target])
    if target_num is None:
        return True, f"目标字段「{rule.target}」没有数值，已跳过", True

    ok = abs(target_num - total) <= max(tol, abs(total) * 1e-6)
    detail = (
        f"明细列「{rule.column}」{counted} 行合计 {total:.2f}，"
        f"「{fields[rule.target].label}」为 {target_num:.2f}，差 {abs(target_num - total):.2f}"
    )
    if unparsable:
        detail += f"；另有 {len(unparsable)} 个格子无法解析为数值（如「{unparsable[0]}」），可能影响结果"
    return ok, detail, False


def _rule_row_count(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    if rule.expected is None:
        return True, "未指定期望行数", True
    actual = len(rows)
    ok = actual == rule.expected
    return ok, f"明细共 {actual} 行，期望 {rule.expected} 行", False


def _rule_date_order(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    missing = _missing(rule, fields, rule.fields)
    if missing:
        return True, f"参与字段未填齐（{'、'.join(missing)}），已跳过", True

    dates: list[str] = []
    for name in rule.fields:
        normalized = to_date(fields[name].value)
        if normalized is None:
            return True, f"字段「{name}」不是可比较的日期，已跳过", True
        dates.append(normalized)

    ok = all(dates[i] <= dates[i + 1] for i in range(len(dates) - 1))
    return ok, f"{' ≤ '.join(dates)}（{'顺序正确' if ok else '顺序颠倒'}）", False


def _rule_required_together(
    rule: CrossRule, fields: dict[str, ExtractedField], rows: list[dict[str, str]], tol: float
) -> tuple[bool, str, bool]:
    missing = _missing(rule, fields, rule.fields)
    if not missing:
        return True, "相关字段均已填写", False
    if len(missing) == len(rule.fields):
        return True, "相关字段均未填写，视为不涉及", True
    present = [n for n in rule.fields if n not in missing]
    return False, f"已填写 {'、'.join(present)}，但缺少 {'、'.join(missing)}", False


# ---------------------------------------------------------------------------
# 完成度
# ---------------------------------------------------------------------------


def compute_completeness(fields: list[ExtractedField], rules: list[RuleResult]) -> float:
    """计算完成度（0~100）。

    公式是显式的、可以手算的，这样用户看到「完成度 72」时能自己验证：

        基础分 = Σ(字段权重 × 状态得分) / Σ字段权重 × 100
            字段权重：必填 3，选填 1
            状态得分：正常 1.0 / 可信度低 0.85 / 校验失败 0.4 / 缺失 0
        规则惩罚：每条失败的非 soft 规则扣 8 分，最多扣 30 分

    「缺失」拿 0 分而不是「不计入分母」，是因为一份字段全丢光的抽取结果
    不该显示 100 分。
    """
    if not fields:
        rules_penalty = min(RULE_PENALTY * sum(1 for r in rules if not r.passed and not r.soft), RULE_PENALTY_CAP)
        return max(0.0, 100.0 - rules_penalty)

    total_weight = 0.0
    earned = 0.0
    for field in fields:
        weight = WEIGHT_REQUIRED if field.required else WEIGHT_OPTIONAL
        total_weight += weight
        earned += weight * STATUS_SCORE.get(field.status, 0.0)

    base = (earned / total_weight * 100.0) if total_weight else 0.0
    failed = sum(1 for rule in rules if not rule.passed and not rule.soft and not rule.skipped)
    penalty = min(RULE_PENALTY * failed, RULE_PENALTY_CAP)
    return max(0.0, round(base - penalty, 1))


__all__ = [
    "WEIGHT_REQUIRED",
    "WEIGHT_OPTIONAL",
    "STATUS_SCORE",
    "RULE_PENALTY",
    "RULE_PENALTY_CAP",
    "LOW_CONFIDENCE",
    "coerce_value",
    "split_list",
    "validate_field",
    "run_rules",
    "compute_completeness",
]
