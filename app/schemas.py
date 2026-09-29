"""前后端唯一契约（Pydantic）。

前两个项目都出现过「后端改了字段名、前端还在读老名字」导致界面静默显示
``undefined`` 的问题，所以这里的契约**同时被 3 处使用**：
后端返回、前端渲染、契约测试静态校验。只有一份定义，就不可能对不上。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 字段与模板定义
# ---------------------------------------------------------------------------

#: 支持的字段类型。类型决定了校验阶段怎么强转、怎么比对。
FieldType = Literal["string", "number", "integer", "date", "boolean", "list"]

FIELD_TYPE_LABELS: dict[str, str] = {
    "string": "文本",
    "number": "数值",
    "integer": "整数",
    "date": "日期",
    "boolean": "布尔",
    "list": "列表",
}

#: 跨字段校验的种类
RuleKind = Literal[
    "equals", "expr", "row_expr", "sum_column", "row_count", "date_order", "required_together"
]

RULE_KIND_LABELS: dict[str, str] = {
    "equals": "字段相等",
    "expr": "算术关系",
    "row_expr": "逐行算术关系",
    "sum_column": "明细合计",
    "row_count": "行数检查",
    "date_order": "日期先后",
    "required_together": "同时必填",
}

#: 字段状态
FieldStatus = Literal["ok", "missing", "invalid", "low_confidence"]

STATUS_LABELS: dict[str, str] = {
    "ok": "正常",
    "missing": "缺失",
    "invalid": "校验失败",
    "low_confidence": "可信度低",
}


class FieldSpec(BaseModel):
    """一个待抽取字段的定义。"""

    key: str = Field(min_length=1, description="字段名，作为输出 JSON 的 key")
    label: str = Field(default="", description="中文标签；留空则用 key")
    type: FieldType = "string"
    required: bool = False
    description: str = Field(default="", description="给模型看的说明，规则层不使用")

    #: 别名列表：用于匹配「标签: 值」的标签部分，以及表格的列名
    aliases: list[str] = Field(default_factory=list)
    #: 直接指定表格列名（优先级高于 aliases）
    column: str = ""
    #: 正则表达式；用于**抽取**（在原文里搜索），含命名组 ``value`` 时只取该组
    pattern: str = ""
    #: 校验用正则；用于**校验**（要求整个值匹配）。与 pattern 分开，
    #: 是因为「在原文里搜到」和「值本身必须长这样」是两个不同的语义，
    #: 用同一个字段表达一定会有一边是错的。
    must_match: str = ""
    #: 允许值（大小写不敏感比较），命中即通过
    enum: list[str] = Field(default_factory=list)
    #: 数值范围
    minimum: float | None = None
    maximum: float | None = None
    #: 校验失败时是否仍然保留原值（默认保留，只是标记 invalid）
    keep_on_invalid: bool = True

    #: 「这个字段的值 = 明细表的行数」，文档里没写时用数出来的行数补上。
    #:
    #: 必须显式声明，不能对全部字段默认生效 —— 这是整个抽取链路里唯一一种
    #: 「值不是从文档里读出来的，而是从**结构**推出来的」字段。做默认行为的话，
    #: 「数量」这种字段在只有一行明细时会被悄悄填成 1，错误没有任何痕迹。
    #:
    #: 典型场景：销售订单模板要「明细行数」，而很多 ERP 导出的单据只在表下方
    #: 写一句「Items: 3」，甚至什么都不写，但表格里明明有 3 行。
    #: 声明了这个开关，就等于模板作者说：「数不清行数就当我没写」。
    from_row_count: bool = False

    def display(self) -> str:
        """给界面用的展示名。"""
        return self.label or self.key

    def all_names(self) -> list[str]:
        """所有可用于匹配的名字：别名 + 列名 + 标签 + key。"""
        names = [*self.aliases]
        if self.column:
            names.insert(0, self.column)
        if self.label:
            names.append(self.label)
        names.append(self.key)
        # 去重且保持顺序
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out


class CrossRule(BaseModel):
    """跨字段一致性校验规则。

    单字段校验（类型、范围、枚举）只能保证「这个值自己合法」，
    真正有价值的是**字段之间的关系**：金额 = 数量 × 单价、明细合计 = 总额、
    合同起始日 ≤ 截止日。这类错误单字段校验永远发现不了。
    """

    key: str
    kind: RuleKind
    #: 参与校验的字段名
    fields: list[str] = Field(default_factory=list)
    #: 目标字段（expr / sum_column 用来指明「结果放到哪里」）
    target: str = ""
    #: 明细表里的列名（sum_column 用）
    column: str = ""
    #: 「规范列名 → 真实单据里可能出现的其它写法」。
    #:
    #: 规则由模板作者书写，但明细列的**真实名字由开票 / ERP 系统决定**：
    #: 同一列可能叫「金额」「Amount」「小计」。没有这张别名表，规则就只能认
    #: 一种写法 —— 一份英文列头的销售订单报表会让所有行级校验静默跳过，
    #: 而「跳过」在界面上看起来跟「没有问题」几乎一样。
    #:
    #: 例：``{"数量": ["Qty", "Quantity"], "单价": ["Unit Price"], "金额": ["Amount"]}``
    #: 键是 ``column`` 或 ``expression`` 里出现的规范列名，值是它的其它写法。
    column_aliases: dict[str, list[str]] = Field(default_factory=dict)
    #: 算术表达式（expr / row_expr 用）
    #: - expr 的变量名是字段 key
    #: - row_expr 的变量名是**明细表的列名**
    expression: str = ""
    #: 数值容差
    tolerance: float = 0.02
    #: 期望行数（row_count 用）
    expected: int | None = None
    #: 失败时的说明；留空则用自动生成的描述
    message: str = ""
    #: 是否为「仅提示」级别（失败不降低完成度）
    soft: bool = False

    def describe(self) -> str:
        if self.message:
            return self.message
        kind = RULE_KIND_LABELS.get(self.kind, self.kind)
        if self.kind == "expr":
            return f"{kind}：{self.expression}"
        if self.kind == "row_expr":
            return f"{kind}：每行 {self.expression} 应等于「{self.column}」"
        if self.kind == "sum_column":
            return f"{kind}：明细列「{self.column}」之和应等于「{self.target}」"
        if self.kind == "row_count":
            return f"{kind}：明细应有 {self.expected} 行"
        if self.kind == "date_order":
            return f"{kind}：{' ≤ '.join(self.fields)}"
        if self.kind == "required_together":
            return f"{kind}：{'、'.join(self.fields)} 需同时存在"
        return f"{kind}：{' = '.join(self.fields)}"


class Template(BaseModel):
    """一套抽取模板 = 字段定义 + 跨字段校验规则。"""

    key: str
    label: str
    description: str = ""
    #: 适用场景提示（前端展示）
    hint: str = ""
    fields: list[FieldSpec] = Field(default_factory=list)
    rules: list[CrossRule] = Field(default_factory=list)
    #: 是否把明细表也抽出来（如发票的商品明细、订单的行项目）
    extract_rows: bool = True
    #: role: "resume" 之类需要特殊处理的场景；目前只有 invoice/card/order/contract/generic
    builtin: bool = True

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields}


class TemplateSummary(BaseModel):
    """模板清单项（不含字段细节，避免列表响应过大）。"""

    key: str
    label: str
    description: str = ""
    hint: str = ""
    field_count: int = 0
    rule_count: int = 0
    builtin: bool = True


# ---------------------------------------------------------------------------
# 抽取结果
# ---------------------------------------------------------------------------


class FieldSource(BaseModel):
    """字段的来源定位 —— 这是「可解释」的关键。

    同一个值从「标签行」抽到和从「表格 B3 单元格」抽到，可信度完全不同，
    而且用户需要能**回到原文核对**。
    """

    #: 抽取策略：label（标签行）/ column（表头列）/ regex（正则）/ llm（模型补漏）
    strategy: Literal["label", "column", "regex", "llm", "derived", "none"] = "none"
    #: 人可读的定位描述，如「第 3 行『发票号码：...』」「销售明细!B3」
    locator: str = ""
    #: 命中的原文片段
    evidence: str = ""
    #: 命中用的名字（哪个别名命中的），便于用户理解为什么抽到了
    matched_name: str = ""
    #: 页码（PDF）或工作表序号
    page: int = 0


class ExtractedField(BaseModel):
    """一个抽出来的字段（含置信度、来源、校验信息）。"""

    key: str
    label: str
    type: FieldType = "string"
    value: Any = None
    #: 抽取阶段的原始文本（未强转前），便于用户核对
    raw: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: FieldStatus = "missing"
    required: bool = False
    source: FieldSource = Field(default_factory=FieldSource)
    #: 校验阶段产生的说明（可能是通过时的提示，也可能是失败原因）
    messages: list[str] = Field(default_factory=list)

    @property
    def is_filled(self) -> bool:
        return self.status in {"ok", "low_confidence", "invalid"}

    def status_text(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class RuleResult(BaseModel):
    """一条跨字段规则的执行结果。"""

    key: str
    kind: RuleKind
    passed: bool
    description: str = ""
    detail: str = ""
    soft: bool = False
    #: 参与校验的字段没填齐时，规则无法判定 —— 这时报「失败」会误导用户。
    #: 明确区分「跳过」与「通过」。
    skipped: bool = False


class TablePreview(BaseModel):
    """表格预览（截断，避免响应体过大）。"""

    locator: str
    caption: str = ""
    columns: list[str] = Field(default_factory=list)
    header_rows: int = 0
    row_count: int = 0
    col_count: int = 0
    preview: list[list[str]] = Field(default_factory=list)
    merged_cells: int = 0
    truncated: bool = False
    page: int = 1


class DocumentSummary(BaseModel):
    """文档摘要（不含正文）。"""

    name: str
    kind: str
    detail: str = ""
    pages: int = 1
    char_count: int = 0
    table_count: int = 0
    block_count: int = 0
    needs_ocr: bool = False
    encoding: str = ""
    warnings: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """一次完整的抽取结果。"""

    doc: DocumentSummary
    template: str
    template_label: str = ""
    #: **实际干活**的引擎（``rule`` / ``llm``），不是请求的那个。
    #: 请求 auto 但没配 Key 时这里是 rule —— 界面顶栏的徽标读的也是生效引擎，
    #: 两处必须是同一个答案，否则用户会觉得两个都在骗他。
    engine: str = "rule"
    #: 本次**请求**的引擎模式（``auto`` / ``rule`` / ``llm``）。
    #: 与 engine 不同才知道发生过降级：请求 auto、实际 rule。
    engine_mode: str = ""
    #: 请求的引擎与实际不符时的**原因**，一句话，直接可显示。
    #: 由后端给出而不是让前端猜 —— 「配了 Key 但规则层已全覆盖」和
    #: 「根本没配 Key」在界面上长得一样，猜错会给出错误的操作建议。
    engine_note: str = ""
    fields: list[ExtractedField] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)
    row_columns: list[str] = Field(default_factory=list)
    rules: list[RuleResult] = Field(default_factory=list)
    tables: list[TablePreview] = Field(default_factory=list)
    #: 完成度：已填且校验通过的字段占比（required 字段缺失会额外扣分）
    completeness: float = Field(default=0.0, ge=0.0, le=100.0)
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0

    def field_map(self) -> dict[str, ExtractedField]:
        return {f.key: f for f in self.fields}


# ---------------------------------------------------------------------------
# 接口请求 / 响应
# ---------------------------------------------------------------------------

#: 引擎模式（与 config.ENGINES 保持一致）
EngineMode = Literal["auto", "rule", "llm"]


class ExtractRequest(BaseModel):
    """按文本 / 已上传文件做抽取。"""

    text: str = Field(default="", description="直接粘贴的文本内容")
    file_id: str = Field(default="", description="已上传文件的 id")
    template: str = Field(default="generic", description="模板 key，或 custom")
    custom_fields: list[FieldSpec] = Field(default_factory=list, description="自定义字段（template=custom 时使用）")
    custom_rules: list[CrossRule] = Field(default_factory=list)
    engine: EngineMode | None = None


class RevalidateRequest(BaseModel):
    """只重跑校验，不重新抽取。

    用户改了一条跨字段规则（比如把容差从 0.02 改成 1.0）不该触发整条抽取链路，
    更不该再花一次模型调用。
    """

    fields: list[ExtractedField] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)
    rules: list[CrossRule] = Field(default_factory=list)
    #: 容差覆盖值。
    #:
    #: ``None``（不传）→ 每条规则各用自己的 ``tolerance``，这是默认行为。
    #: 传数值 → 覆盖全部规则的容差，用于「差几分钱想先看看是不是整体对得上」。
    #:
    #: 之所以是 ``None`` 而不是 ``0.02`` 默认值：后者会让「没传」和
    #: 「传了 0.02」变成同一件事，从而静默覆盖掉模板作者精心设的宽容差规则。
    tolerance: float | None = None


class UploadResponse(BaseModel):
    file_id: str
    name: str
    size: int = 0
    doc: DocumentSummary


class ParseResponse(BaseModel):
    """只解析、不抽取的结果（「先看看抽出了什么」）。"""

    doc: DocumentSummary
    text_preview: str = ""
    tables: list[TablePreview] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = ""
    llm_enabled: bool = False
    vision_enabled: bool = False
    templates: int = 0


class ConfigResponse(BaseModel):
    """前端启动时拉取的配置（Key 一律脱敏）。"""

    llm_enabled: bool
    vision_enabled: bool
    provider: str
    model: str
    vision_model: str
    base_url: str
    api_key_masked: str
    engine: str
    engine_mode: str
    max_upload_mb: int
    pdf_max_pages: int


class StatsResponse(BaseModel):
    files_stored: int = 0
    extractions_run: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    uptime_seconds: float = 0.0


class ExampleItem(BaseModel):
    key: str
    label: str
    description: str
    kind: str
    template: str = ""
    size: int = 0


class ExampleDetail(BaseModel):
    key: str
    label: str
    kind: str
    template: str = ""
    content: str = ""
    #: 二进制示例（图片 / PDF / xlsx）不带 content，只给 file_id
    file_id: str = ""


__all__ = [
    "FieldType",
    "FIELD_TYPE_LABELS",
    "RuleKind",
    "RULE_KIND_LABELS",
    "FieldStatus",
    "STATUS_LABELS",
    "FieldSpec",
    "CrossRule",
    "Template",
    "TemplateSummary",
    "FieldSource",
    "ExtractedField",
    "RuleResult",
    "TablePreview",
    "DocumentSummary",
    "ExtractionResult",
    "EngineMode",
    "ExtractRequest",
    "RevalidateRequest",
    "UploadResponse",
    "ParseResponse",
    "HealthResponse",
    "ConfigResponse",
    "StatsResponse",
    "ExampleItem",
    "ExampleDetail",
]
