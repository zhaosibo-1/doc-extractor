"""生成 examples/ 里的演示文件。

为什么用脚本生成而不是直接把文件丢进仓库？
- 内容可读、可 diff、可复现（除了图片）；
- 二进制示例（xlsx / docx / pdf / png）的**生成过程**本身就是文档，
  看代码就知道那份 Excel 里到底有什么结构（合并标题行、订单头 + 明细两段表等）。

用法::

    python scripts/make_examples.py

PDF 由 ``scripts/minipdf.py`` 生成（只支持 Latin-1，原因见该模块说明）；
中文示例使用 txt / docx / xlsx / png，这些都原生支持 UTF-8。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.minipdf import MiniPdf  # noqa: E402

OUT = ROOT / "examples"

# ---------------------------------------------------------------------------
# 文本示例
# ---------------------------------------------------------------------------

INVOICE_TXT = """增值税电子普通发票

发票代码：011002000311
发票号码：25317000000012345678
开票日期：2026年3月12日
发票类型：电子普通发票
校验码：0231 4567 8901 2345

购买方名称：北京某某咨询有限公司
购买方纳税人识别号：91110108MA01ABCD2X

销售方名称：深圳某某数码科技有限公司
销售方纳税人识别号：91440300MA5EFGH34Y

金额：2104.42
税额：283.58
价税合计（小写）：￥2,388.00
价税合计（大写）：贰仟叁佰捌拾捌元整

商品明细
1. 无线键盘 * 12 套 * 175.37 元
2. 显示器支架 * 3 个 * 12.41 元
"""

BUSINESS_CARD_TXT = """某某智能科技有限公司

姓名：王小明
职位：算法工程师
部门：AI 研究院
手机：13812345678
邮箱：wangxiaoming@example.com
地址：辽宁省沈阳市浑南区创新路 128 号 A 座 15 层
网址：https://www.example.com
微信：wangxm_ai

"让机器理解世界"
"""

CONTRACT_TXT = """采购合同

合同编号：HT-2026-0417
签订日期：2026年4月17日

甲方：某某科技有限公司
乙方：某某信息技术有限公司

合同标的：服务器、交换机与存储阵列采购及配套维保服务
合同金额：267400.00

生效日期：2026年5月1日
到期日期：2027年4月30日

服务内容
1. 提供 2U 机架式服务器 4 台，含 3 年原厂质保；
2. 提供 48 口千兆交换机 6 台；
3. 提供 24 盘位存储阵列 2 套；
4. 服务期内提供 7x24 小时远程支持，故障响应不超过 30 分钟。

付款方式
合同签订后 15 个工作日内支付 30% 预付款，验收合格后支付剩余 70%。
"""

# ---------------------------------------------------------------------------
# Excel 示例：刻意做成「订单头 + 明细」两段堆叠 + 合并标题行
# ---------------------------------------------------------------------------


def build_order_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "销售订单"

    ws["A1"] = "销售订单明细表"
    ws.merge_cells("A1:F1")

    # 上半段：订单头（只有一行记录）
    ws.append(["订单号", "客户名称", "下单日期", "交货日期", "币种", "订单金额"])
    ws.append(["SO-2026-001", "阿尔法科技（北京）有限公司", "2026-01-05", "2026-01-20", "CNY", 8782.50])

    ws.append([])  # 空行分隔两段表格

    # 下半段：商品明细（多行）
    ws.append(["序号", "商品名称", "数量", "单价", "金额", "备注"])
    ws.append([1, "无线键盘", 12, 199.00, 2388.00, ""])
    ws.append([2, "机械键盘", 5, 499.50, 2497.50, "含定制键帽"])
    ws.append([3, "显示器", 3, 1299.00, 3897.00, ""])

    # 多级表头示例放在第二个工作表
    ws2 = wb.create_sheet("季度销量")
    ws2.append(["产品", "一季度", "", "二季度", ""])
    ws2.merge_cells("A1:A2")
    ws2.merge_cells("B1:C1")
    ws2.merge_cells("D1:E1")
    ws2.append(["", "销量", "金额", "销量", "金额"])
    ws2.append(["无线键盘", 120, 23880.00, 150, 29850.00])
    ws2.append(["机械键盘", 45, 22477.50, 60, 29970.00])
    ws2.append(["显示器", 30, 38970.00, 42, 54558.00])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PDF 示例：单个订单 + 明细行（英文，带表格线）
# ---------------------------------------------------------------------------
#
# 这一份刻意做成**英文**，用来验证一件常被忽略的事：
# 模板里的规则写的是规范列名（数量 / 单价 / 金额），而真实单据的列名由
# 开票 / ERP 系统决定（Qty / Unit Price / Amount）。两者靠模板里的
# ``column_aliases`` 对上，规则不必为每种语言各写一套。
#
# 语义上必须是「一个订单 + 它的明细行」——这和 sales_order 模板的模型一致。
# 早先这份示例是「每行一个订单」的报表（4 张不同订单），字段一个都抽不到：
# 每行的订单号、客户都不同，无法归纳出**这一个订单**的属性。
# 示例形态与模板语义不匹配时，界面上只会显示「0%」，看不出原因，
# 所以这里宁可把示例改对，也不留一个会误导人的演示。

#: 订单头（标签行）——PDF 只支持 Latin-1，所以用英文标签
ORDER_PDF_HEADER = [
    "Sales Order",
    "",
    "Order No.: SO-2026-001",
    "Customer: Alpha Technology Ltd",
    "Order Date: 2026-01-05",
    "Delivery Date: 2026-01-20",
    "Currency: CNY",
    "",
    "Order Items",
]

#: 明细表：列名用英文，靠模板的 column_aliases 映射到规范列名
ORDER_PDF_ROWS = [
    ["Item", "Qty", "Unit Price", "Amount"],
    ["Wireless Keyboard", "12", "199.00", "2388.00"],
    ["Mechanical Keyboard", "5", "499.50", "2497.50"],
    ["Monitor", "3", "1299.00", "3897.00"],
]

#: 合计与行数放在表格下方，作为普通标签行出现
ORDER_PDF_FOOTER = [
    "",
    "Items: 3",
    "Total Amount: 8782.50",
]


def build_order_pdf() -> bytes:
    """手写 PDF：标签行 + 带边框线的明细表 + 合计行。

    表格线是 pdfplumber 能识别出表格的唯一依据，所以这里必须真的画框线，
    不能只把文字对齐成三列 —— 没有线的「表格」在 PDF 里只是散落的文字。
    """
    pdf = MiniPdf()
    page = pdf.add_page()
    y = page.height - 60.0

    page.text(50, y, "Sales Order Sheet", size=15)
    y -= 34

    for line in ORDER_PDF_HEADER[1:]:
        if line:
            page.text(50, y, line, size=11)
        y -= 19

    y -= 6
    y = page.table(50, y, [140, 60, 85, 85], ORDER_PDF_ROWS, row_height=21, font_size=10)

    y -= 26
    for line in ORDER_PDF_FOOTER[1:]:
        page.text(50, y, line, size=11)
        y -= 19

    return pdf.to_bytes()


# ---------------------------------------------------------------------------
# Word 示例：段落与表格交错
# ---------------------------------------------------------------------------


def build_contract_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("采购合同摘要", level=1)
    document.add_paragraph("合同编号：HT-2026-0417")
    document.add_paragraph("签订日期：2026年4月17日")
    document.add_paragraph("甲方：某某科技有限公司")
    document.add_paragraph("乙方：某某信息技术有限公司")

    document.add_heading("采购清单", level=2)
    table = document.add_table(rows=4, cols=4)
    rows = [
        ["产品", "规格", "数量", "金额"],
        ["服务器", "2U 机架式", "4", "156000.00"],
        ["交换机", "48 口千兆", "6", "23400.00"],
        ["存储阵列", "24 盘位", "2", "88000.00"],
    ]
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value

    document.add_paragraph("合同金额：267400.00")
    document.add_paragraph("生效日期：2026年5月1日")
    document.add_paragraph("到期日期：2027年4月30日")
    document.add_paragraph("合同标的：服务器、交换机与存储阵列采购及配套维保服务")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 名片图片（用于演示视觉模型抽取）
# ---------------------------------------------------------------------------

#: 常见中文字体候选（Windows / macOS / Linux）
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

CARD_LINES_CN = [
    ("王小明", 34),
    ("算法工程师 · AI 研究院", 20),
    ("某某智能科技有限公司", 22),
    ("手机：13812345678", 19),
    ("邮箱：wangxiaoming@example.com", 19),
    ("地址：辽宁省沈阳市浑南区创新路 128 号 A 座 15 层", 17),
    ("网址：https://www.example.com", 19),
]

CARD_LINES_ASCII = [
    ("WANG XIAOMING", 34),
    ("Algorithm Engineer / AI Lab", 20),
    ("Example Intelligent Technology Co., Ltd.", 22),
    ("Mobile: 13812345678", 19),
    ("Email: wangxiaoming@example.com", 19),
    ("Website: https://www.example.com", 19),
]


def build_card_png() -> tuple[bytes, str]:
    """生成名片图片，返回 ``(字节, 使用的字体说明)``。"""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1000, 620
    image = Image.new("RGB", (width, height), (250, 251, 253))
    draw = ImageDraw.Draw(image)

    # 左侧竖条做点缀
    draw.rectangle([0, 0, 14, height], fill=(28, 110, 242))

    font_path = ""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            font_path = candidate
            break

    lines = CARD_LINES_CN
    note = f"使用字体：{font_path}"
    if not font_path:
        lines = CARD_LINES_ASCII
        note = "未找到中文字体，已回退为英文名片（仅影响本示例图片）"

    y = 70
    for text, size in lines:
        try:
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        color = (24, 32, 48) if size >= 20 else (86, 96, 112)
        draw.text((70, y), text, font=font, fill=color)
        y += size + 22
    draw.line([70, y + 6, width - 90, y + 6], fill=(210, 216, 226), width=2)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), note


# ---------------------------------------------------------------------------


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    text_files = {
        "invoice_cn.txt": INVOICE_TXT,
        "business_card_cn.txt": BUSINESS_CARD_TXT,
        "contract_cn.txt": CONTRACT_TXT,
    }
    for name, content in text_files.items():
        (OUT / name).write_text(content, encoding="utf-8")
        print(f"  ✓ {name}  ({len(content)} 字符)")

    binary_files: dict[str, bytes] = {
        "order.xlsx": build_order_xlsx(),
        "order.pdf": build_order_pdf(),
        "contract.docx": build_contract_docx(),
    }
    for name, data in binary_files.items():
        (OUT / name).write_bytes(data)
        print(f"  ✓ {name}  ({len(data)} 字节)")

    card, note = build_card_png()
    (OUT / "business_card.png").write_bytes(card)
    print(f"  ✓ business_card.png  ({len(card)} 字节)  {note}")

    return 0


if __name__ == "__main__":
    print(f"生成示例文件到 {OUT}：")
    raise SystemExit(main())
