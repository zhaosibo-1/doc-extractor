"""内置抽取模板。

一个模板就是「字段定义 + 跨字段校验规则」。内置 5 套覆盖最常见的三类场景：

- 票据类（增值税发票）：字段少、格式死、**必须做金额勾稽**（金额+税额=价税合计）
- 卡片类（名片）：字段散落在多行标签里，正则校验价值高（邮箱、电话）
- 明细表类（销售订单）：核心是**逐行算术校验**（数量×单价=金额）和**合计校验**
- 合同类：核心是**日期先后**与**甲乙方同时存在**
- 通用类（generic）：没有 schema 时把所有「标签: 值」自动抽出来，先让用户看到有什么

设计原则：字段的 ``aliases`` 尽量把真实文档里出现过的写法都列全。
**别名表是这个项目最「脏」也最有价值的部分** —— 它决定了规则层能覆盖多少真实文档。
"""

from __future__ import annotations

from .schemas import CrossRule, FieldSpec, Template, TemplateSummary

#: 邮箱正则（用于名片模板的字段校验）
EMAIL_PATTERN = r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}"

#: 纳税人识别号：15 / 18 / 20 位，字母数字混合
TAX_ID_PATTERN = r"[0-9A-Z]{15,20}"

#: 发票号码：8 位或 20 位（数电票）
INVOICE_NO_PATTERN = r"[0-9]{8}|[0-9]{20}"

#: 「整行就是一个公司名」。
#:
#: 真实的中文名片（尤其是纸质名片）**不会**把公司名写成「公司：某某科技」——
#: 它就是一行独立的大字。所以按标签行匹配的常规路径抽不到它，
#: 而 company 又恰好是名片模板里的必填字段，缺了会让完成度明显掉下来。
#:
#: 用一条正则解决，而不是新加一类抽取策略：正则本来就是三层策略中的一层
#: （置信度 0.80，**低于**标签行的 0.92），所以文档里一旦出现显式的
#: 「公司名称：X」，标签行仍然会赢 —— 只是多了一条兜底路径。
#: 锚定 ``^...$`` 保证它只认「整行都是公司名」的情况，不会在正文里瞎抓。
COMPANY_LINE_PATTERN = (
    r"(?m)^[ \t]*(?P<value>[\u4e00-\u9fffA-Za-z0-9()（）·&\-]{2,30}"
    r"(?:股份有限公司|有限责任公司|有限公司|集团公司|集团|公司|研究院|研究所|事务所))[ \t]*$"
)


def _templates() -> dict[str, Template]:
    """构造所有内置模板（每次调用新构造，避免被调用方就地改坏）。"""
    return {
        # ------------------------------------------------------------------
        "generic": Template(
            key="generic",
            label="通用键值抽取",
            description="没有固定 schema 时使用：自动把所有「标签: 值」与表格抽出来。",
            hint="适合先摸清一份陌生文档里有什么字段，再决定用哪个专用模板。",
            fields=[],
            rules=[],
            extract_rows=True,
        ),
        # ------------------------------------------------------------------
        "invoice": Template(
            key="invoice",
            label="增值税发票",
            description="抽取发票代码/号码、开票日期、购销双方、金额、税额、价税合计与商品明细。",
            hint="同时校验 金额 + 税额 = 价税合计，以及明细金额之和是否等于票面金额。",
            fields=[
                FieldSpec(
                    key="invoice_code",
                    label="发票代码",
                    aliases=["发票代码", "代码", "Invoice Code"],
                    pattern=r"发票代码[：:\s]*([0-9]{10,12})",
                    must_match=r"[0-9]{10,12}",
                    description="10 或 12 位数字；数电票没有发票代码，可以留空",
                ),
                FieldSpec(
                    key="invoice_number",
                    label="发票号码",
                    aliases=["发票号码", "号码", "发票号", "No.", "Invoice No"],
                    pattern=r"发票号码[：:\s]*([0-9]{8,20})",
                    required=True,
                    description="8 位（传统发票）或 20 位（数电票）",
                ),
                FieldSpec(
                    key="invoice_date",
                    label="开票日期",
                    type="date",
                    aliases=["开票日期", "开具日期", "日期", "Date"],
                    required=True,
                ),
                FieldSpec(
                    key="buyer_name",
                    label="购买方名称",
                    aliases=["购买方名称", "购方名称", "购买方", "购方", "买方"],
                ),
                FieldSpec(
                    key="buyer_tax_id",
                    label="购买方纳税人识别号",
                    aliases=["购买方纳税人识别号", "购方税号", "购买方税号", "购方纳税人识别号"],
                    must_match=TAX_ID_PATTERN,
                ),
                FieldSpec(
                    key="seller_name",
                    label="销售方名称",
                    aliases=["销售方名称", "销方名称", "销售方", "销方", "卖方"],
                ),
                FieldSpec(
                    key="seller_tax_id",
                    label="销售方纳税人识别号",
                    aliases=["销售方纳税人识别号", "销方税号", "销售方税号"],
                    must_match=TAX_ID_PATTERN,
                ),
                FieldSpec(
                    key="amount",
                    label="金额（不含税）",
                    type="number",
                    aliases=["金额", "不含税金额", "合计金额", "金额(不含税)", "Amount"],
                    pattern=r"(?:金额|不含税金额)[^\d\-]{0,6}([0-9,]+\.?\d{0,2})",
                ),
                FieldSpec(
                    key="tax",
                    label="税额",
                    type="number",
                    aliases=["税额", "合计税额", "增值税额", "Tax"],
                    pattern=r"税额[^\d\-]{0,6}([0-9,]+\.?\d{0,2})",
                ),
                FieldSpec(
                    key="total",
                    label="价税合计",
                    type="number",
                    aliases=[
                        "价税合计", "价税合计(小写)", "价税合计（小写）", "小写", "合计",
                        "总金额", "价税合计小写", "Total",
                    ],
                    required=True,
                    pattern=r"(?:价税合计|小写|总金额)[^\d\-]{0,8}([¥￥]?[0-9,]+\.?\d{0,2})",
                ),
                FieldSpec(
                    key="invoice_type",
                    label="发票类型",
                    aliases=["发票类型", "票据类型", "类型"],
                    enum=[
                        "增值税专用发票", "增值税普通发票", "电子普通发票",
                        "电子专用发票", "数电票", "全面数字化的电子发票",
                    ],
                    keep_on_invalid=True,
                ),
                FieldSpec(
                    key="check_code",
                    label="校验码",
                    aliases=["校验码", "校验码后6位"],
                    pattern=r"校验码[：:\s]*([0-9 ]{6,25})",
                ),
            ],
            rules=[
                CrossRule(
                    key="amount_plus_tax",
                    kind="expr",
                    fields=["amount", "tax"],
                    target="total",
                    expression="amount + tax",
                    tolerance=0.02,
                    message="票面勾稽：金额（不含税）+ 税额 应等于价税合计",
                ),
                CrossRule(
                    key="detail_sum",
                    kind="sum_column",
                    fields=[],
                    target="amount",
                    column="金额",
                    tolerance=0.05,
                    message="明细金额之和应等于票面金额",
                    soft=True,
                ),
            ],
            extract_rows=True,
        ),
        # ------------------------------------------------------------------
        "business_card": Template(
            key="business_card",
            label="名片",
            description="抽取姓名、公司、职位、电话、邮箱、地址、网址等联系信息。",
            hint="邮箱与手机号会做格式校验，抽到错字会直接标出来。",
            fields=[
                FieldSpec(
                    key="name",
                    label="姓名",
                    aliases=["姓名", "名字", "Name", "联系人"],
                    required=True,
                ),
                FieldSpec(
                    key="company",
                    label="公司",
                    aliases=["公司", "公司名称", "单位", "工作单位", "Company"],
                    pattern=COMPANY_LINE_PATTERN,
                    required=True,
                    description="真实名片上公司名常是一行独立的大字，没有「公司：」这样的标签",
                ),
                FieldSpec(
                    key="title",
                    label="职位",
                    aliases=["职位", "职务", "岗位", "Title", "Position"],
                ),
                FieldSpec(
                    key="department",
                    label="部门",
                    aliases=["部门", "所属部门", "Department"],
                ),
                FieldSpec(
                    key="phone",
                    label="电话",
                    aliases=["电话", "手机", "手机号", "联系方式", "Tel", "Mobile", "Phone"],
                    pattern=r"(?:(?:\+?86[-\s]?)?1[3-9]\d{9})|(?:0\d{2,3}[-\s]?\d{7,8})",
                    must_match=r"(?:(?:\+?86[-\s]?)?1[3-9]\d{9})|(?:0\d{2,3}[-\s]?\d{7,8})",
                ),
                FieldSpec(
                    key="email",
                    label="邮箱",
                    aliases=["邮箱", "电子邮箱", "E-mail", "Email", "Mail"],
                    pattern=EMAIL_PATTERN,
                    must_match=EMAIL_PATTERN,
                ),
                FieldSpec(
                    key="address",
                    label="地址",
                    aliases=["地址", "公司地址", "通讯地址", "Address"],
                ),
                FieldSpec(
                    key="website",
                    label="网址",
                    aliases=["网址", "官网", "Web", "Website", "主页"],
                    pattern=r"(?:https?://)?(?:www\.)?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+(?:/[^\s]*)?",
                ),
                FieldSpec(
                    key="wechat",
                    label="微信 / QQ",
                    aliases=["微信", "WeChat", "QQ", "微信号"],
                ),
            ],
            rules=[
                CrossRule(
                    key="contact_together",
                    kind="required_together",
                    fields=["phone", "email"],
                    message="名片通常至少要有电话或邮箱之一",
                    soft=True,
                ),
            ],
            extract_rows=False,
        ),
        # ------------------------------------------------------------------
        "sales_order": Template(
            key="sales_order",
            label="销售订单",
            description="抽取订单号、客户、下单日期、订单金额，以及逐行明细（含单价数量金额）。",
            hint="会逐行校验 数量 × 单价 = 金额，并校验明细金额之和是否等于订单金额。",
            fields=[
                FieldSpec(
                    key="order_no",
                    label="订单号",
                    aliases=["订单号", "订单编号", "单号", "Order No", "OrderNo", "合同号"],
                    required=True,
                ),
                FieldSpec(
                    key="customer",
                    label="客户名称",
                    aliases=["客户名称", "客户", "购方", "Customer", "客户单位"],
                    required=True,
                ),
                FieldSpec(
                    key="order_date",
                    label="下单日期",
                    type="date",
                    aliases=["下单日期", "订单日期", "日期", "Date", "Order Date"],
                ),
                FieldSpec(
                    key="delivery_date",
                    label="交货日期",
                    type="date",
                    aliases=["交货日期", "发货日期", "Delivery Date"],
                ),
                FieldSpec(
                    key="currency",
                    label="币种",
                    aliases=["币种", "货币", "Currency"],
                    enum=["CNY", "USD", "EUR", "JPY", "HKD", "人民币", "美元", "欧元", "港币"],
                ),
                FieldSpec(
                    key="total_amount",
                    label="订单金额",
                    type="number",
                    required=True,
                    aliases=["订单金额", "总金额", "合计金额", "金额合计", "金额", "Total", "Total Amount"],
                ),
                FieldSpec(
                    key="item_count",
                    label="明细行数",
                    type="integer",
                    aliases=["行数", "明细行数", "Items", "条数"],
                    # 很多订单只在表下方写一句「Items: 3」，Excel 导出的更是
                    # 一个字都不写 —— 但明细表里明明有 3 行。文档没写就用数出来的。
                    from_row_count=True,
                ),
            ],
            rules=[
                CrossRule(
                    key="line_total",
                    kind="row_expr",
                    expression="数量 * 单价",
                    column="金额",
                    tolerance=0.02,
                    # 明细列的**真实名字由开票 / ERP 系统决定**，模板只声明规范名。
                    # 没有这张表，「订单报表（PDF，英文列头）」这类文档会让所有
                    # 行级校验静默跳过 —— 而「跳过」在界面上看起来跟「没问题」几乎一样。
                    column_aliases={
                        "数量": ["Qty", "Quantity", "Sales Qty"],
                        "单价": ["Unit Price", "UnitPrice", "Price"],
                        "金额": ["Amount", "Line Total", "小计", "金额合计"],
                    },
                    message="逐行校验：数量 × 单价 = 金额",
                ),
                CrossRule(
                    key="sum_amount",
                    kind="sum_column",
                    fields=[],
                    target="total_amount",
                    column="金额",
                    tolerance=0.05,
                    column_aliases={
                        "金额": ["Amount", "Line Total", "小计", "金额合计"],
                    },
                    message="明细金额之和应等于订单金额",
                ),
                CrossRule(
                    key="date_order",
                    kind="date_order",
                    fields=["order_date", "delivery_date"],
                    message="下单日期不应晚于交货日期",
                    soft=True,
                ),
            ],
            extract_rows=True,
        ),
        # ------------------------------------------------------------------
        "contract": Template(
            key="contract",
            label="合同关键信息",
            description="抽取合同编号、签订日期、甲乙方、合同金额与生效/到期日期。",
            hint="会校验生效日期不晚于到期日期，并提醒甲乙方是否齐全。",
            fields=[
                FieldSpec(
                    key="contract_no",
                    label="合同编号",
                    aliases=["合同编号", "合同号", "协议编号", "编号", "Contract No"],
                    required=True,
                ),
                FieldSpec(
                    key="sign_date",
                    label="签订日期",
                    type="date",
                    aliases=["签订日期", "签署日期", "签约日期", "日期"],
                ),
                FieldSpec(
                    key="party_a",
                    label="甲方",
                    aliases=["甲方", "委托方", "采购方", "发包方", "Party A"],
                ),
                FieldSpec(
                    key="party_b",
                    label="乙方",
                    aliases=["乙方", "受托方", "供应方", "承包方", "Party B"],
                ),
                FieldSpec(
                    key="amount",
                    label="合同金额",
                    type="number",
                    aliases=["合同金额", "合同总金额", "总金额", "金额", "合同价款", "Amount"],
                ),
                FieldSpec(
                    key="start_date",
                    label="生效日期",
                    type="date",
                    aliases=["生效日期", "起始日期", "开始日期", "服务起始日"],
                ),
                FieldSpec(
                    key="end_date",
                    label="到期日期",
                    type="date",
                    aliases=["到期日期", "截止日期", "结束日期", "服务截止日"],
                ),
                FieldSpec(
                    key="subject",
                    label="合同标的",
                    aliases=["合同标的", "标的", "项目名称", "服务内容", "采购内容"],
                ),
            ],
            rules=[
                CrossRule(
                    key="term_order",
                    kind="date_order",
                    fields=["start_date", "end_date"],
                    message="生效日期不应晚于到期日期",
                ),
                CrossRule(
                    key="parties",
                    kind="required_together",
                    fields=["party_a", "party_b"],
                    message="合同应有甲乙双方",
                ),
                CrossRule(
                    key="sign_before_start",
                    kind="date_order",
                    fields=["sign_date", "start_date"],
                    message="签订日期不应晚于生效日期",
                    soft=True,
                ),
            ],
            extract_rows=True,
        ),
    }


def all_templates() -> dict[str, Template]:
    """所有内置模板（新构造，调用方可以放心修改）。"""
    return _templates()


def get_template(key: str) -> Template | None:
    """按 key 取模板；不存在返回 ``None``（由调用方决定怎么报错）。"""
    return _templates().get((key or "").strip())


def list_summaries() -> list[TemplateSummary]:
    """模板清单（供 ``/api/templates`` 使用）。"""
    return [
        TemplateSummary(
            key=t.key,
            label=t.label,
            description=t.description,
            hint=t.hint,
            field_count=len(t.fields),
            rule_count=len(t.rules),
            builtin=t.builtin,
        )
        for t in _templates().values()
    ]


__all__ = [
    "EMAIL_PATTERN",
    "TAX_ID_PATTERN",
    "INVOICE_NO_PATTERN",
    "all_templates",
    "get_template",
    "list_summaries",
]
