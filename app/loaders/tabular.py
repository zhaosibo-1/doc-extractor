"""XLSX 加载器。

Excel 是所有格式里「看起来最简单、坑最多」的一个：

- **合并单元格**：只有左上角有值，不填充就会让表头错位（已在 table_utils 处理，这里负责取到 merges）。
- **公式格**：``data_only=True`` 读到的是缓存值；如果这个文件从没被 Excel 打开过，
  缓存值是 ``None``，整列会变成空的 —— 必须显式警告，而不是当成「这列没数据」。
- **日期格**：openpyxl 返回的是 ``datetime`` 对象，直接 ``str()`` 会得到
  ``2024-01-05 00:00:00``，与用户看到的 ``2024-01-05`` 不一致，要统一裁掉时间部分。
- **多工作表**：一张表就是一「页」，全部解析并在定位信息里带表名。
"""

from __future__ import annotations

import datetime as _dt
import io

from ..ir import Block, Cell, Document, TableData
from ..textutil import clean_cell
from .table_utils import build_table, build_tables

#: 单个工作簿最多解析多少个工作表（防止异常文件拖垮服务）
MAX_SHEETS = 20


class XlsxDependencyError(RuntimeError):
    """缺少 openpyxl。消息里给出安装指引而不是让 ImportError 冒到接口层。"""


def _require_openpyxl():
    try:
        import openpyxl  # noqa: PLC0415 - 延迟导入，让缺失依赖变成可读错误
    except ImportError as exc:  # pragma: no cover - 正常环境不会触发
        raise XlsxDependencyError(
            "解析 xlsx 需要 openpyxl，请执行：pip install -r requirements.txt"
        ) from exc
    return openpyxl


def format_cell_value(value: object) -> str:
    """把 openpyxl 读出来的值格式化成展示用字符串。

    日期只保留日期部分；浮点数去掉无意义的 ``.0``；
    布尔值转成 是/否（Excel 里勾选项常是 TRUE/FALSE）。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, _dt.datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        # 1.0 -> "1"，1.5 -> "1.5"：整数型浮点去掉小数点，报表里更可读
        if value.is_integer():
            return str(int(value))
        return repr(value).rstrip("0").rstrip(".")
    return clean_cell(str(value))


def load_xlsx(name: str, data: bytes, *, max_rows: int = 500, max_header_rows: int = 3) -> Document:
    """解析 XLSX，每个工作表变成一张表。"""
    openpyxl = _require_openpyxl()

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    except Exception as exc:  # openpyxl 的异常类型比较多，统一包装成可读消息
        raise XlsxDependencyError(f"无法打开 xlsx 文件：{exc}") from exc

    doc = Document(name=name, kind="xlsx", pages=0)
    sheets = [ws for ws in workbook.worksheets if getattr(ws, "sheet_state", "visible") != "hidden"]
    hidden = len(workbook.worksheets) - len(sheets)
    if hidden:
        doc.add_warning(f"已跳过 {hidden} 个隐藏工作表。")

    if len(sheets) > MAX_SHEETS:
        doc.add_warning(f"工作簿含 {len(sheets)} 个可见工作表，仅解析前 {MAX_SHEETS} 个。")
        sheets = sheets[:MAX_SHEETS]

    table_index = 0
    for sheet_index, ws in enumerate(sheets):
        rows_raw: list[list[object]] = []
        for row in ws.iter_rows(values_only=True):
            rows_raw.append([format_cell_value(cell) for cell in row])

        if not any(any(cell for cell in row) for row in rows_raw):
            doc.add_warning(f"工作表「{ws.title}」是空的，已跳过。")
            continue

        merges = _collect_merges(ws)
        locator = f"{ws.title}!{ws.dimensions or 'A1'}"
        built = build_tables(
            rows_raw,
            merges=merges,
            locator=locator,
            page=sheet_index + 1,
            max_rows=max_rows,
            max_header_rows=max_header_rows,
        )
        if len(built) > 1:
            doc.add_warning(
                f"工作表「{ws.title}」里检测到 {len(built)} 张上下堆叠的表格，已分别解析。"
            )
        for table in built:
            if table.row_count >= max_rows:
                doc.add_warning(f"工作表「{ws.title}」行数达到上限 {max_rows}，后续行已截断。")
            doc.blocks.append(Block(kind="table", table=table, page=sheet_index + 1))
            table_index += 1

    doc.pages = max(1, table_index)
    doc.meta["sheet_count"] = table_index
    doc.meta["formula_cache_missing"] = _has_missing_formula_cache(workbook)
    if doc.meta["formula_cache_missing"]:
        doc.add_warning(
            "文件中存在公式单元格且没有缓存值（通常是文件由程序生成、未用 Excel 打开过），"
            "这些格子会是空的；请在 Excel / WPS 中打开并保存一次后重传。"
        )
    return doc


def _collect_merges(ws) -> list[Cell]:
    """收集合并单元格信息，转成 0 基的 :class:`Cell`。"""
    out: list[Cell] = []
    try:
        ranges = list(ws.merged_cells.ranges)
    except Exception:  # 只读模式下可能拿不到，退化为「没有合并」
        return out
    for rng in ranges:
        out.append(
            Cell(
                text="",
                row=rng.min_row - 1,
                col=rng.min_col - 1,
                row_span=rng.max_row - rng.min_row + 1,
                col_span=rng.max_col - rng.min_col + 1,
            )
        )
    return out


def _has_missing_formula_cache(workbook) -> bool:
    """检测「有公式但取不到值」的情况。

    ``data_only=True`` 下，若单元格的 ``value`` 是 ``None`` 而它本身有公式，
    说明这份文件从未被 Excel 计算过。
    """
    for ws in workbook.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None and isinstance(cell.data_type, str) and cell.data_type == "f":
                    return True
    return False


def load_table_data(
    rows: list[list[object]],
    *,
    locator: str,
    index: int = 0,
    page: int = 1,
    max_rows: int = 500,
    max_header_rows: int = 3,
) -> TableData:
    """给 docx / pdf 用的便捷入口：直接由二维数据构造一张规范化表格。"""
    return build_table(
        rows,
        locator=locator,
        index=index,
        page=page,
        max_rows=max_rows,
        max_header_rows=max_header_rows,
    )


__all__ = [
    "XlsxDependencyError",
    "format_cell_value",
    "load_xlsx",
    "load_table_data",
    "MAX_SHEETS",
]
