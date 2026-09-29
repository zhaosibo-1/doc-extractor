"""测试包的公共工具（示例文件路径与读取）。

放在独立模块而不是 ``conftest.py`` 里，是因为 ``conftest`` 应当只放夹具；
测试模块需要 import 的静态数据从 ``conftest`` 导入会形成反向依赖，
而且 ``pytest`` 对 ``conftest`` 的导入方式在不同版本间有过变化。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

#: 所有示例文件：文件名 → (期望的文档类型, 该用哪个模板)
EXAMPLE_CASES: list[tuple[str, str, str]] = [
    ("invoice_cn.txt", "text", "invoice"),
    ("business_card_cn.txt", "text", "business_card"),
    ("contract_cn.txt", "text", "contract"),
    ("order.xlsx", "xlsx", "sales_order"),
    ("order.pdf", "pdf", "sales_order"),
    ("contract.docx", "docx", "contract"),
    ("business_card.png", "image", "business_card"),
]

#: 只看文本类示例（用于抽取断言，图片需要视觉模型才能读）
TEXT_EXAMPLE_CASES: list[tuple[str, str, str]] = [
    case for case in EXAMPLE_CASES if case[1] != "image"
]


def read_example(name: str) -> bytes:
    """读一个示例文件；缺失时给出可执行的修复提示，而不是一句 FileNotFoundError。"""
    path = EXAMPLES / name
    if not path.exists():
        pytest.skip(f"示例文件 {name} 不存在。请先执行：python scripts/make_examples.py")
    return path.read_bytes()
