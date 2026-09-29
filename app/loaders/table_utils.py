"""表格归一化工具。

不管是 Excel、CSV、PDF 里的表格还是 Word 里的表格，最终都要先过这一层。
这里集中处理表格类 bug 最集中的四件事：

1. **参差不齐的行** —— 有的行 5 列有的行 3 列，按列取值就 IndexError。统一补齐成等长矩阵。
2. **合并单元格** —— 只有左上角有值、其余是空。不填充的话表头会整体错位，
   出现「第二列没名字、第三列名字是第二列的」这种静默错误。
3. **表头上方的标题行** —— 「销售明细表」占一整行（通常是 A1:E1 合并），
   它不是列名。不剥掉的话列名会变成「销售明细表_序号」这种四不像。
4. **多级表头** —— 「2023年 / 销量」占两行，必须纵向合并成一个列名，
   否则第一行数据会被当成第二级表头吃掉。

关于表头判定的一条重要经验
--------------------------
判定必须**逐行做，而且要用严格的数值判定**。

最初的实现是把前 N 行拼起来再判断「像不像表头」，结果「有数字 ⇒ 是数据行」
这个刹车信号被拼接符抹掉了 —— ``数量`` 拼上 ``10`` 变成 ``数量_10``，
不再像数字，于是表头一路吃到了 max_rows 行。测试直接把这条 bug 抓了出来。

现在改为：一行一行看，只要这一行里「数据型格子」（数值 / 日期 / 编码）
的占比达到 :data:`DATA_ROW_NUMERIC_RATIO` 就立刻停止 —— 表头行里不该出现成片的数据。
"""

from __future__ import annotations

from ..ir import Cell, TableData
from ..textutil import clean_cell, data_like_ratio, is_blank_row

#: 多级表头的拼接符
HEADER_JOINER = "_"

#: 判定「这行是数据行」的数据型单元格占比阈值。
#: 取 0.4 而不是 0.5：像「产品 / 规格 / 数量 / 金额」这类表，
#: 数据行里往往有一半是文字型字段（产品名、规格描述），能被识别为数据的只有一半。
DATA_ROW_NUMERIC_RATIO = 0.4

#: 判定「这行是表头候选」的最低填充率。表头行通常每格都有内容。
HEADER_FILL_RATIO = 0.6

#: 标题行最长字符数（超过这个长度更可能是正文段落）
CAPTION_MAX_CHARS = 60

#: 最多剥离几行标题 / 空行
MAX_LEADING_STRIP = 3


def pad_rows(rows: list[list[str]]) -> list[list[str]]:
    """把所有行补齐到同一长度（按最长行）。"""
    if not rows:
        return []
    width = max(len(row) for row in rows)
    return [list(row) + [""] * (width - len(row)) for row in rows]


def clean_matrix(rows: list[list[object]]) -> list[list[str]]:
    """把任意二维数据清洗成等长的字符串矩阵。"""
    cleaned = [[clean_cell("" if cell is None else str(cell)) for cell in row] for row in rows]
    return pad_rows(cleaned)


def trim_blank_edges(matrix: list[list[str]]) -> list[list[str]]:
    """去掉开头和结尾的全空行、全空列。

    导出工具常常在表格四周留空行空列，不去掉会让表头识别直接失手。
    """
    if not matrix:
        return []
    rows = [row[:] for row in matrix]
    while rows and is_blank_row(rows[0]):
        rows.pop(0)
    while rows and is_blank_row(rows[-1]):
        rows.pop()
    if not rows:
        return []

    width = len(rows[0])
    left, right = 0, width
    while left < right and all(not row[left] for row in rows):
        left += 1
    while right > left and all(not row[right - 1] for row in rows):
        right -= 1
    return [row[left:right] for row in rows]


def apply_merges(matrix: list[list[str]], merges: list[Cell]) -> list[list[str]]:
    """把合并单元格的值铺满整个合并区域（值取左上角格子）。"""
    if not merges or not matrix:
        return matrix
    out = [row[:] for row in matrix]
    height = len(out)
    for cell in merges:
        if cell.row >= height or cell.row < 0:
            continue
        source = out[cell.row][cell.col] if cell.col < len(out[cell.row]) else ""
        if not source:
            continue
        for r in range(cell.row, min(cell.row + cell.row_span, height)):
            for c in range(cell.col, min(cell.col + cell.col_span, len(out[r]))):
                if not out[r][c]:
                    out[r][c] = source
    return out


# ---------------------------------------------------------------------------
# 表头判定
# ---------------------------------------------------------------------------


def looks_like_header_row(row: list[str], next_row: list[str] | None = None) -> bool:
    """判断这一行像不像表头（只看这一行本身）。"""
    filled = [cell for cell in row if cell]
    if not filled:
        return False
    if len(filled) / len(row) < HEADER_FILL_RATIO:
        return False
    # 表头行里不该出现成片的「数据」（数值 / 日期 / 编码）
    if data_like_ratio(row) >= DATA_ROW_NUMERIC_RATIO:
        return False
    return True


def _continues_above(row: list[str]) -> bool:
    """判断下一行是否有资格继续当表头 —— 依据是**这一行本身带着「分层」的痕迹**。

    两行表头在视觉上通常是同一块的上下两层，于是上面那一层必然留下其中之一：

    - **空格子**：``['年度', '一季度', '', '二季度', '']`` —— 右边那格由下面一行来填；
    - **相邻重复**：``['产品', '一季度', '一季度', '二季度', '二季度']`` ——
      横向合并的单元格在 ``apply_merges`` 之后会被填成重复值。

    反过来，一行**排满且互不相同**的文字（``['字段', '值']``、
    ``['名称', '数量', '金额']``）说明它自己就是完整的一行，
    下面那行是新的一条记录，不该被并进表头。

    这条判据是为「竖排键值表」加的：

    .. code-block:: text

        字段      值
        产品      服务器
        数量      4

    这张表里前两行都是纯文字，逐行判定无法区分。加上「结构依据」之后
    只有第一行是表头，``产品 / 服务器`` 这条记录才不会被吃掉。
    """
    filled = [cell for cell in row if cell]
    if len(filled) < len(row):
        return True
    return any(row[i] and row[i] == row[i + 1] for i in range(len(row) - 1))


def detect_header_rows(matrix: list[list[str]], max_rows: int = 3) -> int:
    """判断表头占前几行。返回 0 表示「没识别到表头」。

    三条判据，缺一条都会出问题：

    1. **逐行判定**：只要某一行像数据行就停。不拼接后再判断 ——
       拼接符会把 ``数量_10`` 变成非数字字符串，抹掉「有数字 ⇒ 是数据行」
       这个唯一的刹车信号。
    2. **额外表头行必须有结构依据**（见 :func:`_continues_above`）。
    3. **必须留得下数据**：如果整张表里没有任何一行像数据，那「前几行是表头」
       这个结论就没有依据 —— 它只说明我们无法区分表头与纯文字型数据行。
       这时退回到「第一行是表头」。

    第 3 条不能写成「扫描数走到了表格末尾」。扫描本身受 ``max_rows`` 限制，
    一张 4 行的全文字表格扫到第 3 行就停了，``count`` 永远到不了 4，
    于是**这张表的全部内容会被拼成列名，一行数据都不剩**：

    .. code-block:: text

        产品      地区
        键盘      北京
        显示器    上海
        鼠标      广州

    这种表在实际文档里非常常见（名称 / 类别 / 地址 / 状态这些列天然都是文字），
    而整表被毁比「第一个字段名不太准」严重得多。所以判据必须回到
    「表里到底有没有数据行」这个事实上，而不是数循环数到了第几行。
    """
    if not matrix:
        return 0

    header_like = [looks_like_header_row(row, None) for row in matrix]
    if not any(not flag for flag in header_like):
        # 全表没有一行像数据：无法区分表头与文字型数据行，
        # 保守地只认第一行，把其余内容全部留给数据。
        return 1 if len(matrix) >= 2 else 0

    count = 0
    for index in range(min(max_rows, len(matrix))):
        if not header_like[index]:
            break
        if index > 0 and not _continues_above(matrix[index - 1]):
            break
        count += 1
    return count


def _merge_header_block(rows: list[list[str]]) -> list[str]:
    """把多行表头纵向拼接成一行（只拼非空格子，避免合并填充值被拼两次）。"""
    if not rows:
        return []
    width = len(rows[0])
    out: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for row in rows:
            value = row[col] if col < len(row) else ""
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        out.append(HEADER_JOINER.join(parts))
    return out


def build_columns(matrix: list[list[str]], header_rows: int) -> list[str]:
    """生成列名：多级表头拼接 + 重名去重 + 空名兜底。"""
    if not matrix:
        return []
    width = len(matrix[0])
    if header_rows <= 0:
        return [f"列{i + 1}" for i in range(width)]

    names = _merge_header_block(matrix[:header_rows])
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, name in enumerate(names):
        key = name or f"列{i + 1}"
        if key in seen:
            seen[key] += 1
            key = f"{key}{HEADER_JOINER}{seen[key] + 1}"
        else:
            seen[key] = 0
        out.append(key)
    return out


# ---------------------------------------------------------------------------
# 标题行剥离
# ---------------------------------------------------------------------------


def _is_caption_like(row: list[str]) -> bool:
    """整行只表达一个信息（标题行 / 合并铺满后的行）。"""
    values = [cell for cell in row if cell]
    return bool(values) and len(set(values)) == 1


def _has_full_width_merge(merges: list[Cell] | None, row_index: int, col_count: int) -> bool:
    """这一行是否存在「横跨整行」的合并单元格（标题行的强证据）。"""
    if not merges:
        return False
    for cell in merges:
        if cell.row == row_index and cell.col == 0 and cell.col_span >= col_count >= 2:
            return True
    return False


def strip_leading_noise(
    matrix: list[list[str]], merges: list[Cell] | None = None
) -> tuple[str, list[list[str]]]:
    """剥离表格顶部的空行与标题行，返回 ``(标题, 剩余矩阵)``。

    判定标题行需要**证据**，满足任意一条即可：
    - 存在横跨整行的合并单元格（Excel 里 A1:E1 合并是最典型的写法）；
    - 整行所有格子内容完全相同且非空（合并填充后的表现）；
    - 整行只有一个非空格子，且**下一行确实是表头**（CSV / PDF 里
      「标题,,,,」这种写法）。最后这条要求下一行像表头，
      避免把「合计,,,」这类汇总行误当成标题。
    """
    if not matrix:
        return "", []
    merges = merges or []
    caption = ""
    removed = 0
    rows = [row[:] for row in matrix]
    width = len(rows[0])

    while rows and removed < MAX_LEADING_STRIP:
        row = rows[0]
        if is_blank_row(row):
            rows.pop(0)
            removed += 1
            continue

        non_empty = [cell for cell in row if cell]
        merged_full = _has_full_width_merge(merges, removed, width)
        all_same = width >= 2 and len(set(row)) == 1 and bool(row[0])
        single_before_header = (
            len(non_empty) == 1
            and len(rows) > 1
            and looks_like_header_row(rows[1])
        )
        too_long = bool(non_empty) and max(len(c) for c in non_empty) > CAPTION_MAX_CHARS

        if not too_long and (merged_full or all_same or single_before_header):
            if not caption and non_empty:
                caption = non_empty[0]
            rows.pop(0)
            removed += 1
            continue
        break

    return caption, rows


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


#: 一张工作表里最多切分出多少张堆叠表（防止异常文件切出几百张）
MAX_STACKED_TABLES = 10


def split_stacked(
    matrix: list[list[str]], merges: list[Cell] | None = None
) -> list[tuple[list[list[str]], list[Cell]]]:
    """把「多张表上下堆叠」的矩阵按空行切开。

    真实场景：Excel 导出的订单通常在**上半部分**放一行订单头
    （订单号 / 客户 / 日期 / 金额），空一行，再放明细表。
    如果整块当成一张表解析，订单头那一行会被误当成表头，
    于是「订单号」变成了列名而不是字段值 —— 这正是一开始抽不出订单信息的原因。

    切分条件（必须同时满足，避免把「中间有零星空行的正常表格」切碎）：
    - 遇到全空行；
    - 空行之前已经有内容；
    - 空行**紧接着的下一行「像表头」**（每格有值、基本不含数字）。

    最后一条是关键：正常表格中间的空行后面跟的是数据行，不会触发切分。

    返回 ``[(子矩阵, 子合并信息), ...]``；空行统一丢弃。
    """
    if not matrix:
        return []
    merges = merges or []

    groups: list[list[tuple[int, list[str]]]] = []
    current: list[tuple[int, list[str]]] = []
    for index, row in enumerate(matrix):
        if is_blank_row(row):
            following = matrix[index + 1] if index + 1 < len(matrix) else None
            if current and following is not None and looks_like_header_row(following):
                groups.append(current)
                current = []
            continue
        current.append((index, row))
    if current:
        groups.append(current)

    if not groups:
        return []

    # 「标题行独占一段」的情况要并到下一段里去。
    # 例：Excel 里 A1:E1 合并写着表名、第 2 行是空的、第 3 行才是真表头 ——
    # 按空行切分会让表名自己成为一张只有标题的表，切完是空表。
    merged_groups: list[list[tuple[int, list[str]]]] = []
    carry: list[tuple[int, list[str]]] = []
    for group in groups:
        if len(group) == 1 and _is_caption_like(group[0][1]):
            carry.extend(group)
            continue
        if carry:
            group = carry + group
            carry = []
        merged_groups.append(group)
    # 末尾孤零零的标题行直接丢弃（它下面没有表）
    groups = merged_groups
    if not groups:
        return []

    # 切得太碎说明这不是「堆叠表格」（例如整张表每隔几行就有空行），
    # 退回成一张表，并顺手丢掉空行 —— 宁可少切，不可切错。
    if len(groups) > MAX_STACKED_TABLES:
        flattened = [row for group in groups for _, row in group]
        return [(flattened, [])]

    out: list[tuple[list[list[str]], list[Cell]]] = []
    for group in groups:
        sub_rows = [row[:] for _, row in group]
        row_map = {original: new for new, (original, _) in enumerate(group)}
        sub_merges: list[Cell] = []
        for cell in merges:
            if cell.row not in row_map:
                continue
            new_row = row_map[cell.row]
            # 合并区域不能跨出本子表
            span = min(cell.row_span, len(sub_rows) - new_row)
            if span < 1:
                continue
            sub_merges.append(
                Cell(
                    text=cell.text,
                    row=new_row,
                    col=cell.col,
                    row_span=span,
                    col_span=cell.col_span,
                )
            )
        out.append((sub_rows, sub_merges))
    return out


def build_tables(
    rows: list[list[object]],
    *,
    merges: list[Cell] | None = None,
    locator: str = "",
    page: int = 1,
    max_rows: int = 500,
    max_header_rows: int = 3,
    split: bool = True,
) -> list[TableData]:
    """把原始二维数据装配成**一张或多张**表（自动处理上下堆叠）。

    ``locator`` 会在切分出多张表时带上 ``#1`` ``#2`` 后缀，
    这样界面上的来源定位仍然能指向具体是哪一张。
    """
    matrix = clean_matrix(rows)
    if merges:
        matrix = apply_merges(matrix, merges)
    matrix = trim_blank_edges(matrix)
    if not matrix:
        return []

    pieces = split_stacked(matrix, merges) if split else [(matrix, list(merges or []))]
    if not pieces:
        pieces = [(matrix, list(merges or []))]
    multiple = len(pieces) > 1

    out: list[TableData] = []
    for index, (sub_rows, sub_merges) in enumerate(pieces):
        sub_locator = f"{locator} #{index + 1}" if multiple and locator else locator
        table = build_table(
            sub_rows,
            merges=sub_merges,
            locator=sub_locator,
            page=page,
            index=index,
            max_rows=max_rows,
            max_header_rows=max_header_rows,
        )
        # 空表（切分后只剩标题行的情况）不进结果，
        # 否则界面上会出现一张「0 行 0 列」的幽灵表格
        if not table.matrix:
            continue
        out.append(table)
    return out


def build_table(
    rows: list[list[object]],
    *,
    header_rows: int = -1,
    merges: list[Cell] | None = None,
    locator: str = "",
    page: int = 1,
    index: int = 0,
    max_rows: int = 500,
    max_header_rows: int = 3,
) -> TableData:
    """把原始二维数据装配成一张规范化的 :class:`TableData`。

    ``header_rows=-1`` 表示自动识别。
    """
    matrix = clean_matrix(rows)
    if merges:
        matrix = apply_merges(matrix, merges)
    matrix = trim_blank_edges(matrix)

    caption, matrix = strip_leading_noise(matrix, merges)

    if header_rows < 0:
        header_rows = detect_header_rows(matrix, max_rows=max_header_rows)
    header_rows = max(0, min(header_rows, len(matrix)))

    columns = build_columns(matrix, header_rows)
    # 截断前的行数必须**先记下来**，否则调用方没法告诉用户「还有多少行没解析」
    original_row_count = len(matrix)
    truncated = original_row_count > max_rows
    if truncated:
        matrix = matrix[:max_rows]

    return TableData(
        matrix=matrix,
        header_rows=header_rows,
        columns=columns,
        locator=locator,
        page=page,
        index=index,
        merges=list(merges or []),
        caption=caption,
        truncated=truncated,
        original_row_count=original_row_count,
    )


__all__ = [
    "HEADER_JOINER",
    "DATA_ROW_NUMERIC_RATIO",
    "HEADER_FILL_RATIO",
    "MAX_LEADING_STRIP",
    "MAX_STACKED_TABLES",
    "CAPTION_MAX_CHARS",
    "pad_rows",
    "clean_matrix",
    "trim_blank_edges",
    "apply_merges",
    "looks_like_header_row",
    "detect_header_rows",
    "build_columns",
    "strip_leading_noise",
    "split_stacked",
    "build_table",
    "build_tables",
]
