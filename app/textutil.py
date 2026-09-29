"""文本归一化与类型解析。

这个模块是整个项目里被依赖最广的一层，也是最容易被低估的一层。
「文档抽取」之所以在实际项目里效果忽好忽坏，多数时候不是模型不行，
而是**同一个值有几十种写法**没有先被归一化：

    金额：  ¥1,234.50   ￥1,234.50   1 234.50   １２３４．５   1,234.5元   (1,234.50)
    日期：  2024-01-05  2024/1/5     2024.1.5   2024年1月5日   20240105
    列名：  销售金额（元）  销售金额(元)  销售金额 / 万元   销售金额

所以这里的原则是：**先把「表示」收敛掉，再谈「语义」**。
归一化是纯函数、可穷举测试，比调模型提示词可靠得多。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# 一、字符级归一化
# ---------------------------------------------------------------------------

#: 需要整体替换的特殊空白与不可见字符。
#: NBSP / 窄空格 / 零宽字符在从 PDF、网页复制出来的文本里极常见，
#: 它们肉眼看不见，却会让「完全相等」的字符串比较失败。
_INVISIBLE = {
    "\u00a0": " ",  # NBSP
    "\u2007": " ",  # figure space
    "\u202f": " ",  # narrow no-break space
    "\u3000": " ",  # 全角空格
    "\u200b": "",   # zero-width space
    "\u200c": "",   # zero-width non-joiner
    "\u200d": "",   # zero-width joiner
    "\ufeff": "",   # BOM
}

#: 中文排版里常见的成对符号，统一成 ASCII，便于写正则和比较
_PUNCT_MAP = {
    "，": ",",
    "。": ".",
    "；": ";",
    "：": ":",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "《": "<",
    "》": ">",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "－": "-",
    "—": "-",
    "–": "-",
    "～": "~",
    "％": "%",
    "＃": "#",
    "＆": "&",
    "＊": "*",
    "＋": "+",
    "／": "/",
    "＝": "=",
    "｜": "|",
}


def normalize_text(text: str) -> str:
    """归一化一段文本：统一全角/半角、清掉不可见字符、统一换行。"""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    for src, dst in _INVISIBLE.items():
        if src in out:
            out = out.replace(src, dst)
    for src, dst in _PUNCT_MAP.items():
        if src in out:
            out = out.replace(src, dst)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    return out


def collapse_ws(text: str) -> str:
    """把连续空白压成一个空格并去掉首尾空白。

    表格单元格里换行会破坏「一行一条记录」的假设，所以单元格必须先过这一道。
    """
    return re.sub(r"[ \t]+", " ", normalize_text(text)).strip()


def clean_cell(text: str) -> str:
    """单元格专用清洗：换行也压成空格，并去掉首尾多余标点。"""
    out = collapse_ws(text.replace("\n", " "))
    return out.strip(" \t,;:") if out.strip(" \t,;:") else out.strip()


# ---------------------------------------------------------------------------
# 二、数值解析
# ---------------------------------------------------------------------------

_CURRENCY = "¥￥$€£₩"
#: 数量级后缀（真实财务报表里大量使用）
#:
#: 键的**顺序有意义**：判定时按顺序取第一个命中的后缀，
#: 所以「千万」必须排在「千」前面、末尾的「万」必须排在「千」后面才不会被截错。
#: 字典保持插入顺序，因此这里的书写顺序就是匹配优先级。
#:
#: 刻意**只收大写** ``M`` / ``B``：小写 ``m`` 在真实单据里更可能是「米」「分钟」
#: 这类计量单位，把它当百万会把「5m」读成 5000000 —— 猜错数量级的代价比不识别更大。
_SCALES: dict[str, float] = {
    "亿": 1e8,
    "千万": 1e7,
    "百万": 1e6,
    "万": 1e4,
    "千": 1e3,
    "k": 1e3,
    "K": 1e3,
    "w": 1e4,
    "W": 1e4,
    "M": 1e6,
    "B": 1e9,
}

_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def to_number(value: object) -> float | None:
    """尽最大努力把一个值解析成数字；解析不了返回 ``None``。

    覆盖的写法：
        ``1,234.50`` / ``¥1,234`` / ``(1,234.50)``（会计负数）/ ``12%`` /
        ``1.2万`` / ``3.5亿`` / ``1,234.5元`` / ``１２３４``（全角）

    括号包裹表示负数，这是财务报表的标准写法，不能当成「去掉括号的正数」。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    raw = collapse_ws(str(value))
    if not raw:
        return None

    negative = False
    # 会计负数：(1,234.50) 或 （1,234.50）
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1].strip()

    # 百分号
    is_percent = raw.endswith("%")
    if is_percent:
        raw = raw[:-1].strip()

    # 去掉货币符号与千分位
    raw = raw.strip(_CURRENCY + " ")
    raw = raw.replace(",", "").replace(" ", "")

    # 数量级后缀（1.2万）
    scale = 1.0
    for suffix, factor in _SCALES.items():
        if raw.endswith(suffix):
            scale = factor
            raw = raw[: -len(suffix)].strip()
            break
    # 去掉尾部单位（元 / 人民币 / 美元……），只留数字
    if not _NUM_RE.match(raw):
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)", raw)
        if not m:
            return None
        raw = m.group(1)

    if not _NUM_RE.match(raw):
        return None

    try:
        number = float(raw) * scale
    except ValueError:
        return None
    if is_percent:
        number /= 100.0
    return -number if negative else number


def to_int(value: object) -> int | None:
    """解析成整数；小数会被四舍五入。"""
    number = to_number(value)
    if number is None:
        return None
    # 直接用 int() 会把 2.9999999 截成 2，先 round 更符合直觉
    return int(round(number))


def to_decimal_str(value: object, places: int = 2) -> str | None:
    """把金额格式化成定点字符串。

    金额用字符串而不是 float 传递，是为了避免二进制浮点在 JSON 里
    变成 ``1234.5000000000002`` 这种读者一眼就皱眉的表示。
    """
    number = to_number(value)
    if number is None:
        return None
    return f"{number:.{places}f}"


# ---------------------------------------------------------------------------
# 三、日期解析
# ---------------------------------------------------------------------------

_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: 支持的分隔符：- / . 年月日
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 2024-01-05 / 2024/1/5 / 2024.1.5
    re.compile(r"(?P<y>\d{4})\s*[-/.]\s*(?P<m>\d{1,2})\s*[-/.]\s*(?P<d>\d{1,2})"),
    # 2024年1月5日
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"),
    # 2024年1月
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月(?!\s*\d)"),
    # 20240105
    re.compile(r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"),
    # 2024-01
    re.compile(r"(?P<y>\d{4})\s*[-/.]\s*(?P<m>\d{1,2})(?!\s*[-/.]\s*\d)"),
)


def to_date(value: object) -> str | None:
    """把各种日期写法归一成 ``YYYY-MM-DD``；解析不了返回 ``None``。

    只认**年月日齐全**或**年月**（日补 01）的形式。
    刻意不猜 ``05/01/2024`` 到底是 5 月 1 日还是 1 月 5 日 ——
    猜错会静默产生错误数据，比返回 ``None`` 危险得多。
    """
    if value is None:
        return None
    raw = collapse_ws(str(value))
    if not raw:
        return None

    for pattern in _DATE_PATTERNS:
        m = pattern.search(raw)
        if not m:
            continue
        groups = m.groupdict()
        year = int(groups["y"])
        month = int(groups["m"])
        day = int(groups.get("d") or 1)
        if not (1900 <= year <= 2999):
            continue
        if not (1 <= month <= 12):
            continue
        if not (1 <= day <= 31):
            continue
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def date_range(value: object) -> tuple[str | None, str | None]:
    """从「2023.09-2027.06」这类区间里取出起止日期。

    只看整条字符串里的第一段日期之前的部分，避免把区间当成单个日期。
    """
    raw = collapse_ws(str(value or ""))
    if not raw:
        return None, None
    parts = re.split(r"\s*(?:~|-|—|–|到|至)\s*", raw, maxsplit=1)
    if len(parts) == 1:
        return to_date(parts[0]), None
    left, right = parts
    return to_date(left), to_date(right)


# ---------------------------------------------------------------------------
# 四、列名 / 键名归一化与相似度
# ---------------------------------------------------------------------------

#: 括号里的内容常常是单位或备注，匹配列名时应先剥离
_BRACKET_RE = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")


def normalize_key(key: str) -> str:
    """归一化列名 / 键名，用于匹配。

    ``销售金额（元）`` → ``销售金额``；``Total Amount (USD)`` → ``total amount``
    """
    out = collapse_ws(key).lower()
    out = _BRACKET_RE.sub("", out)
    out = re.sub(r"[\s_\-/、]+", "", out)
    out = re.sub(r"[^\w\u4e00-\u9fff]", "", out)
    return out


def resolve_column(name: str, columns: Iterable[str], aliases: Iterable[str] = ()) -> str:
    """在 ``columns`` 里找出与 ``name``（或它的别名）相等的**真实列名**；找不到返回 ``""``。

    与 :func:`match_score` 的分工很明确，两者不能混用：

    - ``match_score`` 是**猜测** —— 用相似度，适合「从文档里找可能的字段」这种
      猜错了也只是少抽一个值的场景；
    - ``resolve_column`` 是**确认** —— 只认归一化后的精确相等，适合「规则要靠这一列
      算出通过 / 不通过」的场景。这里猜错的代价不是少一个值，而是给出一个**错误的结论**。

    正因为如此，选明细表和跑跨字段校验必须共用这一个函数：
    否则会出现「选表层说这列覆盖到了、校验层说找不到这一列」的分裂，
    用户看到一条自相矛盾的提示，却无从判断哪一层错了。

    两者都基于 :func:`normalize_key`，所以 ``Unit Price`` / ``unit_price`` /
    ``UnitPrice`` 在这里是同一个名字。
    """
    wanted = {normalize_key(n) for n in (name, *aliases) if n and n.strip()}
    if not wanted:
        return ""
    for column in columns:
        if normalize_key(column) in wanted:
            return column
    return ""


def _bigrams(text: str) -> set[str]:
    """取字符二元组。中文没有空格分词，二元组是最省事且效果稳定的粒度。"""
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def dice(a: str, b: str) -> float:
    """字符二元组的 Dice 相似度，返回 0~1。

    用来把表格列名匹配到字段别名：``销售额`` vs ``销售金额`` → 0.5，可以命中；
    ``销售额`` vs ``备注`` → 0，不会误配。
    """
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return 0.0
    inter = len(left & right)
    return 2 * inter / (len(left) + len(right))


def match_score(text: str, candidates: list[str]) -> tuple[str, float]:
    """在候选词里找最匹配的一个，返回 ``(最佳候选, 分数)``。

    匹配规则**刻意做成不对称的**：

    - 完全相等 → 1.0；
    - 别名是文档标签的一部分（``alias ⊆ label``）→ 0.9
      —— 「本月销售金额」里确实含「销售金额」，是可靠的证据；
    - 其余退回字符二元组 Dice 相似度。

    反过来「文档标签是别名的一部分」（``label ⊂ alias``）**不再给高分**。
    这一条是被真实 bug 逼出来的：名片模板里 ``address`` 的别名有「公司地址」，
    于是一份只写了「公司：某某科技」的名片，地址字段被抽成了公司名 ——
    因为「公司」是「公司地址」的子串。方向反了，证据强度完全不同：
    标签比别名**更短**时，它更可能是另一个字段。

    短别名（如「号码」对应「发票号码」）不要指望被推出来，
    直接写进模板的 ``aliases`` 里 —— 显式声明的准确率永远高于猜测。
    """
    target = normalize_key(text)
    if not target or not candidates:
        return "", 0.0
    best, best_score = "", 0.0
    for cand in candidates:
        key = normalize_key(cand)
        if not key:
            continue
        if key == target:
            return cand, 1.0
        if key in target:
            score = 0.9
        else:
            score = dice(key, target)
        if score > best_score:
            best, best_score = cand, score
    return best, best_score


# ---------------------------------------------------------------------------
# 五、小工具
# ---------------------------------------------------------------------------

_YES = {"是", "有", "y", "yes", "true", "1", "√", "✓", "是的有", "涉及"}
_NO = {"否", "无", "n", "no", "false", "0", "×", "x", "-", "不涉及", "未涉及"}


def to_bool(value: object) -> bool | None:
    """把「是/否」「有/无」「√/×」这类标记解析成布尔值。"""
    if isinstance(value, bool):
        return value
    raw = collapse_ws(str(value or "")).lower()
    if not raw:
        return None
    if raw in _YES:
        return True
    if raw in _NO:
        return False
    return None


#: 「标签: 值」行里标签的最大长度
LABEL_MAX_CHARS = 24

#: 匹配「标签: 值」的冒号（中英文都要）
_LABEL_RE = re.compile(r"^\s*(?P<label>[^：:]{1,%d}?)\s*[：:]\s*(?P<value>.*)$" % LABEL_MAX_CHARS)


def split_label_value(text: str) -> tuple[str, str]:
    """把「标签: 值」拆成 ``(标签, 值)``；不是键值行则返回 ``("", "")``。

    这是文档抽取里复现率最高的一种结构（「发票号码：12345」），
    所以放在公共层，让标题判定、块切分、字段抽取都用同一份实现 ——
    三处各写一遍必然会漂移。
    """
    cleaned = collapse_ws(text)
    match = _LABEL_RE.match(cleaned)
    if not match:
        return "", ""
    label = collapse_ws(match.group("label"))
    value = collapse_ws(match.group("value"))
    return label, value


def is_blank_row(row: list[str]) -> bool:
    """整行都是空的（合并单元格规范化之后，这类行很常见，必须能识别出来）。"""
    return all(not clean_cell(cell) for cell in row)


def is_numberish(text: str) -> bool:
    """判断一个字符串「开头像数字」（宽松）。用于给用户看的提示，不用于判定表头。"""
    return to_number(text) is not None


_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def is_numeric_cell(text: str) -> bool:
    """判断一个单元格是否是**纯数值**（严格）。

    与 :func:`is_numberish` 的区别很关键：
    ``is_numberish("2023年")`` 是 ``True``（匹配到了开头的 2023），
    但它显然是一列表头而不是数据；``is_numberish("SO-2026-001")`` 是 ``False``
    但它是订单号这类「带字母的编号」，也不该当数值。

    所以这里要求**整个字符串**在剥掉货币符号、千分位、百分号之后就是数字，
    并且不含任何中日韩字符或拉丁字母（``1.2万`` / ``3k`` 这类带数量级后缀的除外）。

    表头判定必须用这个严格版本，否则「2023年」这种表头会被误判成数据行，
    整个表格的列名就全错了。

    实现上先做 NFKC 归一化（全角 ``３００`` → 半角 ``300``、全角括号 → 半角），
    保证与 :func:`to_number` 对「什么算数字」的判断一致 —— 两个函数对同一个
    单元格给出相反答案，会让表头判定与数值校验互相打架。
    """
    raw = normalize_text(collapse_ws(text))
    if not raw:
        return False

    # 先剥数量级后缀，**再**判中日韩字符 —— 这个顺序不能反。
    # 「1.2万」里的「万」本身就在 CJK 区间内，先判 CJK 会把它直接判成非数值，
    # 让下面这段剥后缀的逻辑变成永远走不到的死代码，而这个函数的文档
    # 明确承诺「1.2万 是数值」。顺序反了会有什么实际后果？
    # 一列「销量」里写着 1.2万 / 3.5万 时，这些格子不算数据，
    # 表头判定会因此丢掉「有数字 ⇒ 是数据行」这个刹车信号。
    stripped = raw
    for suffix in _SCALES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break

    if _CJK_RE.search(stripped) or _LATIN_RE.search(stripped):
        return False

    stripped = stripped.strip(" \t").strip(_CURRENCY + " ")
    if stripped.endswith("%"):
        stripped = stripped[:-1].strip()
    stripped = stripped.replace(",", "").replace(" ", "")
    return bool(_NUM_RE.match(stripped))


#: 「编码型」单元格：订单号 / 合同号 / 发票号这类，含数字且含连字符或字母
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/#.]{3,23}$")


def is_data_like_cell(text: str) -> bool:
    """判断一个单元格更像「数据」还是更像「表头」。

    这是表头识别的核心判据（表头行里不该出现成片的数据）。三类都算数据：

    1. **纯数值** —— ``1,234.50`` / ``12`` / ``￥99`` / ``1.2万``
       （交给严格版 :func:`is_numeric_cell`，它内部已经处理了中文数量级后缀）
    2. **日期** —— ``2026-01-05`` / ``2026年1月5日``。日期是最常被漏掉的一类：
       早期的实现只认纯数字，结果一整行「订单号 / 客户 / 下单日期 / 交货日期 /
       币种 / 金额」被判定成表头，订单信息整块丢失。
       注意中文日期必须放行 —— 它含有中文字符，但它是数据而不是表头。
    3. **编码** —— ``SO-2026-001`` / ``HT-2026-0417``。同理，订单号不是表头。

    **判定顺序很关键**：数值与日期要先于「含中文就排除」这条规则。
    把中文排除放在最前面（这个函数早期就是这么写的），会让
    ``1.2万`` 和 ``2026年1月5日`` 一起被判成非数据 ——
    前者是因为「万」在 CJK 区间里，后者是因为「年」「月」「日」在 CJK 区间里，
    而它们恰恰是最典型的**数据**。

    剩下含中文的一律不算数据：中文表格里的表头几乎都带中文
    （``订单金额``、``商品名称``、``2023年``），把它们算成数据会让表头判定彻底失效。
    """
    raw = collapse_ws(text)
    if not raw:
        return False
    if is_numeric_cell(raw):
        return True
    if to_date(raw) is not None:
        return True
    # 走到这里说明既不是数值也不是日期。含中文的一律不算数据
    # （「2023年」这类不完整年份 to_date 会返回 None，正好被这条拦住）
    if _CJK_RE.search(raw):
        return False
    # 编码型：必须有数字，且只由字母数字与常见分隔符组成
    if any(ch.isdigit() for ch in raw) and _CODE_RE.match(raw):
        return True
    return False


def data_like_ratio(row: list[str]) -> float:
    """一行里「数据型」单元格的占比（非空格子为分母）。"""
    filled = [cell for cell in row if cell]
    if not filled:
        return 0.0
    return sum(1 for cell in filled if is_data_like_cell(cell)) / len(filled)


def truncate(text: str, limit: int = 80) -> str:
    """按字符截断并加省略号，用于日志与错误信息。"""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def dedupe_keep_order(items: list[str]) -> list[str]:
    """去重但保持首次出现的顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = [
    "normalize_text",
    "collapse_ws",
    "clean_cell",
    "to_number",
    "to_int",
    "to_decimal_str",
    "to_date",
    "date_range",
    "normalize_key",
    "dice",
    "match_score",
    "resolve_column",
    "to_bool",
    "is_blank_row",
    "split_label_value",
    "LABEL_MAX_CHARS",
    "is_numberish",
    "is_numeric_cell",
    "is_data_like_cell",
    "data_like_ratio",
    "truncate",
    "dedupe_keep_order",
]
