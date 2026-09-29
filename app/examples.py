"""内置示例清单。

放在代码里而不是读目录，是为了让每个示例都带上「它想演示什么」和「该用哪个模板」——
用户点一下就明白这个功能解决什么问题，而不是面对一堆文件名猜。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: 示例文件所在目录（相对项目根）
EXAMPLES_DIR = Path("examples")


@dataclass(frozen=True)
class ExampleSpec:
    key: str
    filename: str
    label: str
    description: str
    template: str
    #: 是否是可复制的纯文本（二进制示例要走「载入到工作区」而非直接填文本框）
    text_based: bool = True


EXAMPLES: tuple[ExampleSpec, ...] = (
    ExampleSpec(
        key="invoice",
        filename="invoice_cn.txt",
        label="增值税电子发票（文本）",
        description="演示键值抽取 + 票面勾稽：金额 + 税额 必须等于价税合计。",
        template="invoice",
    ),
    ExampleSpec(
        key="business_card",
        filename="business_card_cn.txt",
        label="名片（文本）",
        description="演示散落标签抽取 + 格式校验：手机号与邮箱会被正则校验。",
        template="business_card",
    ),
    ExampleSpec(
        key="contract",
        filename="contract_cn.txt",
        label="采购合同关键信息（文本）",
        description="演示日期先后校验与甲乙方齐全性检查。",
        template="contract",
    ),
    ExampleSpec(
        key="order_xlsx",
        filename="order.xlsx",
        label="销售订单（Excel，含订单头 + 明细两段表）",
        description="演示上下堆叠表格切分、金额逐行校验与合计校验。",
        template="sales_order",
        text_based=False,
    ),
    ExampleSpec(
        key="order_pdf",
        filename="order.pdf",
        label="销售订单报表（PDF 表格）",
        description="演示 PDF 表格线识别、合并单元格还原与多级表头。",
        template="sales_order",
        text_based=False,
    ),
    ExampleSpec(
        key="contract_tables",
        filename="contract.docx",
        label="采购合同（Word，含正文与表格）",
        description="演示 Word 段落与表格按真实顺序解析。",
        template="contract",
        text_based=False,
    ),
    ExampleSpec(
        key="card_image",
        filename="business_card.png",
        label="名片照片（图片）",
        description="演示图片结构化抽取：需要配置视觉模型（VISION_MODEL）才能读取内容。",
        template="business_card",
        text_based=False,
    ),
)


def all_examples() -> tuple[ExampleSpec, ...]:
    return EXAMPLES


def get_example(key: str) -> ExampleSpec | None:
    for spec in EXAMPLES:
        if spec.key == key:
            return spec
    return None


def example_path(spec: ExampleSpec, base: Path | None = None) -> Path:
    root = base if base is not None else Path(__file__).resolve().parent.parent
    return root / EXAMPLES_DIR / spec.filename


__all__ = ["EXAMPLES_DIR", "ExampleSpec", "EXAMPLES", "all_examples", "get_example", "example_path"]
