"""抽取层：Schema 驱动地填字段。

这一层是「规则 + 模型」两种能力的分工点，也是整个项目最核心的设计：

    规则优先 —— 标签行匹配（置信度最高）、
                表格列匹配（结构化程度最高）、
                正则匹配（格式最确定）
    模型补漏 —— 只在前三招都没抽到某个字段时才调用，
                并且**它的结果置信度天花板只有 0.6**，
                会被校验层自动标成「可信度低」

为什么模型结果要给低置信度？
因为它**不可复现**：同一个提示词换一次调用，措辞可能就从「某某科技有限公司」
变成「某某科技公司」。规则层抽出来的值来自原文（可以在界面上核对位置），
模型「归纳」出来的值可能原文里根本没有。把两者标成同样的可信度，
用户就没法判断哪个该信。

与之配套的一条策略：**模型不推翻规则**。冲突时以规则为准，
但把分歧记录在字段的 ``messages`` 里 —— 让用户看到「模型说是 A，规则抽出 B」，
而不是悄悄丢掉一方。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from dataclasses import field as dc_field

from . import safe_eval, validators
from .config import ENGINE_AUTO, ENGINE_LLM, ENGINE_RULE, Settings
from .ir import Document, TableData
from .llm import LLMClient, LLMError, LLMNotConfigured, encode_image, extract_json
from .loaders import summarize_document
from .loaders.image_loader import prepare_for_vision
from .schemas import (
    CrossRule,
    DocumentSummary,
    ExtractedField,
    ExtractionResult,
    FieldSource,
    FieldSpec,
    TablePreview,
    Template,
)
from .textutil import collapse_ws, match_score, normalize_key, resolve_column

logger = logging.getLogger(__name__)

#: 各抽取策略的置信度基准。数值不是拍脑袋来的，含义是「这个来源本身有多可靠」：
#: 表格列 > 标签行 > 正则 > 模型。
BASE_CONFIDENCE: dict[str, float] = {
    "column": 0.95,
    "label": 0.92,
    #: 由结构推出来的值（目前只有「明细行数 = 明细表行数」）。
    #: 数值本身是数出来的、不含糊；扣掉的分数反映的是**语义假设**：
    #: 我们假设「明细行数」指的就是这张明细表的行数。
    "derived": 0.88,
    "regex": 0.80,
    "llm": 0.60,
}

#: 低于这个分数就不认为匹配成功（避免「备注」匹配到「备注说明人」这类误配）
MIN_MATCH_SCORE = 0.55


@dataclass
class RowPick:
    """一次「明细表挑选」的结果。

    把警告一起带出来，是因为「挑到的表缺了某些列」本身就是一条必须让用户看到的
    降级信息 —— 用多个并列的返回值传递它，调用方很容易只处理其中一个。
    """

    rows: list[dict[str, str]] = dc_field(default_factory=list)
    columns: list[str] = dc_field(default_factory=list)
    table: TableData | None = None
    warnings: list[str] = dc_field(default_factory=list)


def _wanted_detail_columns(template: Template) -> list[tuple[str, list[str]]]:
    """从模板的规则里推导「明细表必须提供哪些列」。

    返回 ``[(规范列名, 该列的别名), ...]``。只有两个来源，且必须区分清楚
    （``schemas.CrossRule`` 里写明了两者的语义差异）：

    - ``rule.column``：``sum_column`` / ``row_expr`` 明确指向的列（如「金额」）。
    - ``row_expr`` 表达式里的变量：这类表达式**在每一行上求值**，
      所以它的变量就是明细表的列名（``数量 * 单价`` → 「数量」「单价」）。

    刻意**不**收集 ``expr`` 的表达式变量，也不收集 ``rule.fields``：
    那两个位置放的是**字段 key**（amount / tax / order_date…），不是列名。
    把字段 key 当列名去找，会让所有表的覆盖度一起被稀释，
    反而掩盖掉真正「凑齐了所需列」的那张表。
    """
    wanted: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    for rule in template.rules:
        if rule.column:
            name = rule.column.strip()
            if name and name not in seen:
                seen.add(name)
                wanted.append((name, list(rule.column_aliases.get(name, []))))
        if rule.kind == "row_expr" and rule.expression:
            for token in safe_eval.variables_in(rule.expression):
                if token in seen:
                    continue
                seen.add(token)
                wanted.append((token, list(rule.column_aliases.get(token, []))))
    return wanted

#: 表格预览最多返回多少行
PREVIEW_ROWS = 15

#: 送给模型的正文长度上限
LLM_TEXT_LIMIT = 12000


@dataclass
class Candidate:
    """一个字段的候选值（来自某一种抽取策略）。"""

    value: str
    strategy: str
    locator: str = ""
    evidence: str = ""
    matched_name: str = ""
    page: int = 0
    matched_score: float = 1.0

    @property
    def confidence(self) -> float:
        base = BASE_CONFIDENCE.get(self.strategy, 0.5)
        if self.strategy in {"column", "label"}:
            score = max(0.0, min(1.0, self.matched_score))
            return round(base * (0.85 + 0.15 * score), 3)
        return base

    def to_source(self) -> FieldSource:
        return FieldSource(
            strategy=self.strategy,  # type: ignore[arg-type]
            locator=self.locator,
            evidence=self.evidence,
            matched_name=self.matched_name,
            page=self.page,
        )


@dataclass
class _Rules:
    fields: dict[str, list[Candidate]] = dc_field(default_factory=dict)

    def add(self, key: str, candidate: Candidate) -> None:
        self.fields.setdefault(key, []).append(candidate)


class Extractor:
    """抽取调度器。"""

    def __init__(self, settings: Settings, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.llm = llm

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def extract(
        self,
        doc: Document,
        template: Template,
        *,
        engine: str | None = None,
        image: tuple[bytes, str] | None = None,
    ) -> ExtractionResult:
        """完整抽取：规则层 → 模型层 → 校验层。

        ``image`` 是 ``(原始字节, mime)``，只有图片类文档才需要传。
        """
        started = time.perf_counter()
        mode = self._resolve_mode(engine)
        result = ExtractionResult(
            doc=summarize_document_as_model(doc),
            template=template.key,
            template_label=template.label,
        )
        # 先记下**请求**的引擎，等真的走完再改成**实际干活**的那个。
        #
        # 这两个值必须分开：界面上顶栏的徽标读 /api/config 的生效引擎，
        # 结果卡片读这里的 engine。以前这里直接写 mode，于是同一屏上
        # 顶栏写着「引擎 rule」、结果卡片写着「引擎 auto」——
        # auto 是"允许用模型"，不是"用了模型"。用户看到两个不一样的答案，
        # 就再也不会相信其中任何一个。
        result.engine = mode
        result.engine_mode = mode
        result.warnings.extend(doc.warnings)

        # 用户**明确点名**要用大模型时，没配 Key 就在这里失败，而不是「碰运气」。
        #
        # 之前是延后到「有字段没抽到」才去调模型，于是出现一个很坑的组合：
        # 一份规则引擎恰好能全覆盖的单据 + engine=llm + 没配 Key，
        # 会返回 200、engine 字段还写着 "llm"，用户以为大模型跑通了 ——
        # 实际上一次模型调用都没发生。配置错误必须在动手之前就喊出来。
        if mode == ENGINE_LLM:
            if self.llm is None:
                raise LLMNotConfigured()
            if doc.kind == "image" or doc.needs_ocr:
                self.llm.ensure_ready(vision=True)
            else:
                self.llm.ensure_ready()

        # ---------- 1. 规则层 ----------
        rules = _Rules()
        if template.key == "generic":
            self._collect_generic(doc, rules)
        else:
            self._collect_columns(doc, template, rules)
            self._collect_labels(doc, template, rules)
            self._collect_regex(doc, template, rules)

        candidates, disagreements = self._pick(rules, template)

        # ---------- 2. 模型层（补漏） ----------
        llm_used = False
        #: 「为什么最后没用上模型」的一句话。请求的引擎与实际不符时由后端给出，
        #: 前端只负责显示 —— 界面自己猜原因（"大概是没配 Key 吧"）在
        #: 「配了 Key 但规则已全覆盖」这种情况上一定是错的。
        engine_note = ""
        if mode in {ENGINE_AUTO, ENGINE_LLM}:
            missing = [spec for spec in template.fields if spec.key not in candidates]
            need_image = doc.kind == "image"
            if need_image or missing:
                try:
                    llm_candidates = await self._collect_llm(
                        doc, template, missing or template.fields, image=image
                    )
                except LLMNotConfigured as exc:
                    if mode == ENGINE_LLM:
                        raise
                    engine_note = f"未启用大模型补漏（{exc}）"
                    result.warnings.append(f"{engine_note}，本次结果全部来自规则引擎。")
                except LLMError as exc:
                    if mode == ENGINE_LLM:
                        raise
                    engine_note = f"大模型补漏失败：{exc}"
                    result.warnings.append(f"大模型补漏失败，已降级为纯规则结果：{exc}")
                else:
                    llm_used = True
                    for spec in template.fields:
                        candidate = llm_candidates.get(spec.key)
                        if candidate is None:
                            continue
                        existing = candidates.get(spec.key)
                        if existing is None:
                            candidates[spec.key] = candidate
                        else:
                            disagreements.setdefault(spec.key, []).append(candidate)
            else:
                engine_note = "规则层已覆盖全部字段，没有需要模型补漏的地方"
        elif mode == ENGINE_RULE and doc.kind == "image":
            result.warnings.append(
                "当前为规则引擎模式，图片内容无法被读取（图片需要视觉模型）。"
                "请把 EXTRACT_ENGINE 设为 auto 或 llm 并配置 VISION_MODEL。"
            )

        # 到这里才能真正回答「这次是谁干的活」：
        # 只有真的调用过模型才算 llm，其余一律 rule。
        #
        # 注意「点名叫 llm 但规则层已全覆盖」这种情况：模型一次都没调，
        # 结果 100% 来自规则，所以这里如实写 rule（并配一条 warning 说明原因）。
        # 写 llm 会让用户以为模型读懂了这份文档 —— 而他花的钱一分没花。
        result.engine = ENGINE_LLM if llm_used else ENGINE_RULE

        # generic 模板下，模型补漏的字段也要能落进来（模型可能发现了新的键）
        if template.key == "generic":
            specs = _specs_from_candidates(candidates, template)
        else:
            specs = template.fields

        # ---------- 3. 明细行 ----------
        pick = self._collect_rows(doc, template)
        rows = pick.rows
        result.rows = rows
        result.row_columns = pick.columns
        result.warnings.extend(pick.warnings)

        # 3.5 由结构推出的字段（目前只有「明细行数」）
        self._derive_from_rows(specs, candidates, rows)

        # ---------- 4. 校验层 ----------
        result.fields = self._build_fields(specs, candidates, disagreements)
        field_map = {f.key: f for f in result.fields}
        rules_to_run = self._effective_rules(template, result.fields)
        result.rules = validators.run_rules(rules_to_run, field_map, rows)
        # 规则失败时把原因回填到对应字段，用户不用两处对照着看
        self._attach_rule_messages(result.fields, result.rules, rules_to_run)

        result.completeness = validators.compute_completeness(result.fields, result.rules)
        result.tables = [self._preview_table(t) for t in doc.tables]
        if llm_used:
            result.warnings.append("部分字段由大模型补漏得到（置信度上限 0.6），已标记为「可信度低」。")
        elif mode == ENGINE_LLM:
            # 用户**点名**要用大模型，结果一次都没调 —— 这件事必须说出来，
            # 否则界面上 engine 写着 llm，用户会以为模型读懂了这份文档。
            result.warnings.append(f"{engine_note or '未调用大模型'}，本次结果 100% 来自规则引擎。")
        result.engine_note = engine_note
        if doc.needs_ocr and not (self.llm and self.llm.vision_enabled):
            result.warnings.append(
                "这份文档没有文字层（扫描件或图片），未配置视觉模型时无法抽取字段。"
            )

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ------------------------------------------------------------------
    # 模式
    # ------------------------------------------------------------------

    def _resolve_mode(self, engine: str | None) -> str:
        if engine in {ENGINE_AUTO, ENGINE_RULE, ENGINE_LLM}:
            return engine
        return self.settings.active_engine

    # ------------------------------------------------------------------
    # 规则层
    # ------------------------------------------------------------------

    def _collect_columns(self, doc: Document, template: Template, rules: _Rules) -> None:
        """从表格列里取值。

        只对「记录型表格」生效 —— 即这张表的数据行里，目标列的值**全都是同一个**
        （例如一张只有一行数据的表，或每行都重复写着同一个订单号）。
        明细表（每行金额都不同）的列不会被当成标量字段，
        因为它们进的是 ``rows``，不是某个字段。
        """
        for table in doc.tables:
            if not table.matrix:
                continue
            data_rows = table.body
            if not data_rows:
                continue
            for spec in template.fields:
                names = spec.all_names()
                for col_index, column in enumerate(table.columns):
                    matched, score = match_score(column, names)
                    if score < MIN_MATCH_SCORE:
                        continue
                    values = [
                        collapse_ws(row[col_index]) for row in data_rows if col_index < len(row) and collapse_ws(row[col_index])
                    ]
                    if not values:
                        continue
                    unique = set(values)
                    if len(unique) > 1:
                        # 多行不同值 → 这是明细列，不属于标量字段
                        continue
                    locator = f"{table.locator} → 列「{column}」"
                    if len(data_rows) > 1:
                        locator += f"（{len(data_rows)} 行取值一致）"
                    rules.add(
                        spec.key,
                        Candidate(
                            value=values[0],
                            strategy="column",
                            locator=locator,
                            evidence=f"{column} = {values[0]}",
                            matched_name=matched,
                            page=table.page,
                            matched_score=score,
                        ),
                    )
                    break

    def _collect_labels(self, doc: Document, template: Template, rules: _Rules) -> None:
        """从「标签: 值」行里取值。

        支持值在下一行的情况（PDF 里排版换行很常见）：
        ``发票号码：`` / 换行 / ``25317...``
        """
        kv_blocks = [(i, b) for i, b in enumerate(doc.blocks) if b.kind == "kv"]
        # 普通段落也可能是「标签 值」形式（没有冒号），一并纳入候选
        all_blocks = doc.blocks

        for spec in template.fields:
            names = spec.all_names()
            best: Candidate | None = None
            for index, block in kv_blocks:
                label, value = _split_label(block.text)
                if not label:
                    continue
                matched, score = match_score(label, names)
                if score < MIN_MATCH_SCORE:
                    continue
                text = value
                locator = f"第 {index + 1} 行「{collapse_ws(block.text)}」"
                if not text and index + 1 < len(all_blocks):
                    nxt = all_blocks[index + 1]
                    if nxt.kind in {"paragraph", "kv", "list_item"} and nxt.text.strip():
                        text = collapse_ws(nxt.text)
                        locator += f"（值在下一行）"
                if not text:
                    continue
                candidate = Candidate(
                    value=text,
                    strategy="label",
                    locator=locator,
                    evidence=collapse_ws(block.text),
                    matched_name=matched,
                    page=block.page,
                    matched_score=score,
                )
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
            if best is not None:
                rules.add(spec.key, best)

    def _collect_regex(self, doc: Document, template: Template, rules: _Rules) -> None:
        """用字段自带的正则在全文里搜索。"""
        text = doc.text
        for spec in template.fields:
            if not spec.pattern:
                continue
            try:
                pattern = re.compile(spec.pattern)
            except re.error as exc:
                logger.warning("字段 %s 的正则非法：%s", spec.key, exc)
                continue
            match = pattern.search(text)
            if not match:
                continue
            value = _group_value(match)
            if not value:
                continue
            rules.add(
                spec.key,
                Candidate(
                    value=value,
                    strategy="regex",
                    locator=f"正则匹配 {spec.pattern}",
                    evidence=collapse_ws(match.group(0)),
                    matched_name="pattern",
                    matched_score=1.0,
                ),
            )

    def _collect_generic(self, doc: Document, rules: _Rules) -> None:
        """通用模板：把所有「标签: 值」都收进来。

        键名直接用标签本身（去重），这样用户第一眼就能看到文档里到底有什么字段，
        再决定用哪个专用模板。
        """
        seen: dict[str, int] = {}
        for index, block in enumerate(doc.blocks):
            if block.kind != "kv":
                continue
            label, value = _split_label(block.text)
            if not label:
                continue
            key = normalize_key(label) or f"字段{index + 1}"
            if key in seen:
                seen[key] += 1
                key = f"{key}{seen[key] + 1}"
            else:
                seen[key] = 0
            rules.add(
                key,
                Candidate(
                    value=value or "(空)",
                    strategy="label",
                    locator=f"第 {index + 1} 行「{collapse_ws(block.text)}」",
                    evidence=collapse_ws(block.text),
                    matched_name=label,
                    page=block.page,
                    matched_score=1.0,
                ),
            )

    # ------------------------------------------------------------------
    # 候选挑选
    # ------------------------------------------------------------------

    def _pick(
        self, rules: _Rules, template: Template
    ) -> tuple[dict[str, Candidate], dict[str, list[Candidate]]]:
        """每种策略选一个最优候选，并记录不同策略之间的分歧。"""
        chosen: dict[str, Candidate] = {}
        disagreements: dict[str, list[Candidate]] = {}
        for key, candidates in rules.fields.items():
            winner = max(candidates, key=lambda c: c.confidence)
            chosen[key] = winner
            others = [c for c in candidates if c is not winner and collapse_ws(c.value) != collapse_ws(winner.value)]
            if others:
                disagreements[key] = others
        return chosen, disagreements

    # ------------------------------------------------------------------
    # 模型层
    # ------------------------------------------------------------------

    async def _collect_llm(
        self,
        doc: Document,
        template: Template,
        wanted: list[FieldSpec],
        *,
        image: tuple[bytes, str] | None,
    ) -> dict[str, Candidate]:
        """让模型补漏。图片类文档走视觉接口。"""
        if self.llm is None:
            raise LLMNotConfigured()

        prompt = build_prompt(template, wanted, doc)
        if doc.kind == "image":
            if image is None:
                raise LLMError("图片内容缺失，无法调用视觉模型")
            raw, mime = image
            prepared, mime = prepare_for_vision(raw)
            text = await self.llm.vision(encode_image(prepared), mime, prompt, json_mode=True)
        else:
            text = await self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是文档结构化抽取引擎。只输出 JSON，不要解释。"
                            "只填写你能在原文中确证的内容，找不到就留空字符串，不要编造。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                json_mode=True,
            )

        payload = extract_json(text)
        return _llm_payload_to_candidates(payload, doc)

    # ------------------------------------------------------------------
    # 明细行
    # ------------------------------------------------------------------

    def _collect_rows(
        self, doc: Document, template: Template
    ) -> RowPick:
        """挑选「明细表」并转成字典行。

        **挑选依据是「规则需要的列，这张表有没有」，而不是「这张表有几列撞上了关键词」。**

        这个区别非常关键。早先的写法是统计每张表命中 wanted_columns 的列数，
        结果在一张「订单头 + 明细 + 季度销量」的工作簿里选错了表：
        季度销量表有「一季度_金额」「二季度_金额」两列都含「金额」，
        按命中数算它得了 4.0 分，而真正的明细表只有一列「金额」，只得 2.3 分。
        更糟的是它**不会报错** —— 规则拿着一张无关的表去校验，只会安静地全部跳过，
        用户看到的是「校验：跳过 3 条」，根本不会怀疑是选表错了。

        所以现在改成两条硬规则：

        1. **按覆盖度评分**：对每一个规则需要的列名，取该表里最像的那一列的匹配强度，
           求和。表越宽不等于分越高 —— 只有真能凑齐规则所需列的表才会赢。
        2. **缺列就明说**：选中的表缺了哪些列，逐条写进 warnings。若一张表都没凑上，
           就返回空行并说明「没找到明细表」。宁可告诉用户「这张表没有数量列」，
           也不要塞一张无关的表让校验静默装死。
        """
        tables = [t for t in doc.tables if t.body]
        if not tables or not template.extract_rows:
            return RowPick()

        wanted = _wanted_detail_columns(template)
        if wanted:
            # 排序依据（依次比较）：
            #   1. 命中列数最多 —— 能凑齐规则所需列的表优先；
            #   2. 表越窄越好 —— 命中列数相同时，「每列都有用」的小表更像专用明细表，
            #      而不是顺带含了这些列的大宽表；
            #   3. 数据行越多越好 —— 仅作为最后的确定性兜底。
            best: TableData | None = None
            best_key: tuple[int, int, int] | None = None
            covered: set[str] = set()
            for table in tables:
                hit = {
                    canonical
                    for canonical, aliases in wanted
                    if resolve_column(canonical, list(table.columns), aliases)
                }
                key = (len(hit), -len(table.columns), len(table.body))
                if best_key is None or key > best_key:
                    best, best_key, covered = table, key, hit

            if best is None or not covered:
                return RowPick(
                    warnings=[
                        f"没有找到包含「{'、'.join(name for name, _ in wanted)}」列的明细表，"
                        f"明细行与依赖这些列的校验规则已跳过。"
                        f"当前文档的表列：{'；'.join('、'.join(t.columns) for t in tables)}"
                    ]
                )
            pick = RowPick(rows=best.dict_rows(), columns=list(best.columns), table=best)
            missing = [name for name, _ in wanted if name not in covered]
            if missing:
                # 部分命中时**照常使用**这张表（它已经是最贴近的了），
                # 但必须说清哪条规则会因为缺列而跑不起来。
                pick.warnings.append(
                    f"明细表「{best.locator}」没有「{'、'.join(missing)}」列，"
                    f"依赖这些列的校验规则已跳过。该表列为：{'、'.join(best.columns)}"
                )
        else:
            # 模板没有列级规则（如名片、通用模板）：退回「数据行最多的表」，
            # 这是唯一可用的启发式，所以不额外声明警告。
            best = max(tables, key=lambda t: len(t.body))
            pick = RowPick(rows=best.dict_rows(), columns=list(best.columns), table=best)

        if pick.table is not None and pick.table.truncated:
            pick.warnings.append(
                f"明细表「{pick.table.locator}」原有 {pick.table.original_row_count} 行，"
                f"已按上限截断到 {pick.table.row_count} 行。"
            )
        return pick

    def _derive_from_rows(
        self,
        specs: list[FieldSpec],
        candidates: dict[str, Candidate],
        rows: list[dict[str, str]],
    ) -> None:
        """把「值 = 明细行数」的字段用数出来的行数补上。

        只在两个条件下生效：

        - 模板里**显式**声明了 ``from_row_count=True``（见 :class:`FieldSpec`）；
        - 规则层和模型层都没给出值 —— 文档里**真的写了**行数时，
          读出来的值优先。这时如果两者对不上（写着 5 行、表里只有 3 行），
          那是一条需要被看见的矛盾，不该被数出来的数字悄悄盖掉。
        """
        if not rows:
            return
        count = len(rows)
        for spec in specs:
            if not spec.from_row_count or spec.key in candidates:
                continue
            candidates[spec.key] = Candidate(
                value=str(count),
                strategy="derived",
                locator=f"明细表共 {count} 行",
                evidence=f"按明细表行数推出：{count}",
                matched_name=spec.display(),
            )

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def _build_fields(
        self,
        specs: list[FieldSpec],
        candidates: dict[str, Candidate],
        disagreements: dict[str, list[Candidate]],
    ) -> list[ExtractedField]:
        """把候选值装配成字段并跑单字段校验。"""
        out: list[ExtractedField] = []
        for spec in specs:
            candidate = candidates.get(spec.key)
            if candidate is None:
                field = ExtractedField(
                    key=spec.key,
                    label=spec.display(),
                    type=spec.type,
                    required=spec.required,
                    status="missing",
                    source=FieldSource(strategy="none"),
                )
                if spec.required:
                    field.messages.append("必填字段未在文档中找到")
                else:
                    field.messages.append("未在文档中找到该字段")
                out.append(validators.validate_field(spec, field))
                continue

            field = ExtractedField(
                key=spec.key,
                label=spec.display(),
                type=spec.type,
                raw=candidate.value,
                confidence=candidate.confidence,
                required=spec.required,
                source=candidate.to_source(),
            )
            if spec.description and candidate.strategy == "llm":
                field.messages.append(f"由模型补漏得到（{spec.description}）")
            for other in disagreements.get(spec.key, []):
                field.messages.append(
                    f"注意：{_strategy_label(other.strategy)}给出的是「{other.value}」，"
                    f"与最终采用的「{candidate.value}」不一致（保留可信度更高的来源）"
                )
            out.append(validators.validate_field(spec, field))
        return out

    def _effective_rules(self, template: Template, fields: list[ExtractedField]) -> list[CrossRule]:
        """按实际抽到的字段过滤规则：参与字段全缺的规则不跑（避免刷屏的「跳过」）。"""
        present = {f.key for f in fields if f.value not in (None, "", [])}
        out: list[CrossRule] = []
        for rule in template.rules:
            if rule.kind in {"row_expr", "row_count"}:
                out.append(rule)
                continue
            involved = set(rule.fields) | ({rule.target} if rule.target else set())
            if involved and not (involved & present):
                continue
            out.append(rule)
        return out

    def _attach_rule_messages(
        self, fields: list[ExtractedField], results: list, rules: list[CrossRule]
    ) -> None:
        """把校验失败的原因回填到字段上，用户不用两处对照。"""
        by_key = {f.key: f for f in fields}
        for rule, result in zip(rules, results):
            if result.passed or result.skipped:
                continue
            for key in rule.fields:
                if key in by_key:
                    by_key[key].messages.append(f"一致性校验未通过：{result.detail}")
            if rule.target and rule.target in by_key:
                by_key[rule.target].messages.append(f"一致性校验未通过：{result.detail}")

    @staticmethod
    def _preview_table(table: TableData) -> TablePreview:
        return TablePreview(
            locator=table.locator,
            caption=table.caption,
            columns=list(table.columns),
            header_rows=table.header_rows,
            row_count=table.row_count,
            col_count=table.col_count,
            preview=table.to_preview(PREVIEW_ROWS),
            merged_cells=len(table.merges),
            truncated=table.truncated,
            page=table.page,
        )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _split_label(text: str) -> tuple[str, str]:
    """把「标签: 值」拆开。没有冒号返回 ``("", "")``。"""
    cleaned = collapse_ws(text)
    for sep in ("：", ":"):
        if sep in cleaned:
            label, _, value = cleaned.partition(sep)
            return collapse_ws(label), collapse_ws(value)
    return "", ""


def _group_value(match: re.Match[str]) -> str:
    """从正则匹配里取值：优先命名组 value，其次第一个分组，最后整个匹配。"""
    groups = match.groupdict()
    if "value" in groups and groups["value"]:
        return collapse_ws(groups["value"])
    if match.groups():
        first = match.group(1)
        if first:
            return collapse_ws(first)
    return collapse_ws(match.group(0))


def _strategy_label(strategy: str) -> str:
    return {
        "column": "表格列匹配",
        "label": "标签行匹配",
        "regex": "正则匹配",
        "llm": "大模型",
        "none": "无",
    }.get(strategy, strategy)


def summarize_document_as_model(doc: Document) -> DocumentSummary:
    """``Document`` → 接口用的 ``DocumentSummary``。"""
    data = summarize_document(doc)
    return DocumentSummary(
        name=str(data["name"]),
        kind=str(data["kind"]),
        detail=str(data["detail"]),
        pages=int(data["pages"]),  # type: ignore[arg-type]
        char_count=int(data["char_count"]),  # type: ignore[arg-type]
        table_count=int(data["table_count"]),  # type: ignore[arg-type]
        block_count=int(data["block_count"]),  # type: ignore[arg-type]
        needs_ocr=bool(data["needs_ocr"]),
        encoding=str(data["encoding"]),
        warnings=list(data["warnings"]),  # type: ignore[arg-type]
    )


def _specs_from_candidates(specs_map: dict[str, Candidate], template: Template) -> list[FieldSpec]:
    """通用模板下，把抽到的键动态变成字段定义。"""
    if template.fields:
        return template.fields
    out: list[FieldSpec] = []
    for key, candidate in specs_map.items():
        out.append(
            FieldSpec(
                key=key,
                label=candidate.matched_name or key,
                type="string",
            )
        )
    return out


def build_prompt(template: Template, wanted: list[FieldSpec], doc: Document) -> str:
    """构造抽取提示词。

    要点：
    - 把字段的别名一并给出 —— 别名表是规则层的资产，对模型同样有效；
    - 明确「找不到就留空，不要编造」，并在系统提示里再强调一次；
    - 带页码标注，让模型能给出 ``page``，便于用户回溯。
    """
    lines: list[str] = []
    lines.append(f"任务：从下面的文档中抽取字段（模板：{template.label}）。")
    if template.description:
        lines.append(f"说明：{template.description}")
    lines.append("")
    lines.append("需要抽取的字段：")
    for spec in wanted:
        parts = [f"- {spec.key}（{spec.display()}，类型 {spec.type}"]
        if spec.required:
            parts.append("，必填")
        if spec.aliases:
            parts.append(f"，别名：{'、'.join(spec.aliases[:6])}")
        parts.append("）")
        if spec.description:
            parts.append(f"　说明：{spec.description}")
        lines.append("".join(parts))

    lines.append("")
    lines.append('输出 JSON，格式为：{"fields": {"字段key": "值"}, "rows": [{"列名": "值"}]}')
    lines.append("规则：")
    lines.append("1. 只填你能在原文里确证的值；找不到就留空字符串，**绝对不要推测或编造**。")
    lines.append("2. 金额/数量等数值只写数字，不要带货币符号与千分位。")
    lines.append("3. 日期统一写成 YYYY-MM-DD。")
    lines.append("4. rows 只放明细行（如商品明细、订单行项目），没有就留空数组。")
    lines.append("")
    lines.append("文档内容：")
    lines.append(doc.text_with_pages(max_chars=LLM_TEXT_LIMIT))
    return "\n".join(lines)


def _llm_payload_to_candidates(payload: object, doc: Document) -> dict[str, Candidate]:
    """把模型返回的 JSON 转成候选值。"""
    out: dict[str, Candidate] = {}
    if not isinstance(payload, dict):
        return out

    fields = payload.get("fields")
    if isinstance(fields, dict):
        for key, value in fields.items():
            if value is None:
                continue
            text = collapse_ws(str(value)) if not isinstance(value, list) else "、".join(
                collapse_ws(str(v)) for v in value if v not in (None, "")
            )
            if not text:
                continue
            out[str(key)] = Candidate(
                value=text,
                strategy="llm",
                locator="大模型补漏",
                evidence=text,
                matched_name=str(key),
                page=0,
            )

    # 少数模型会把明细行写成 rows 里的 dict，这里只用于补漏标量字段：
    # 若某列在所有行里取值一致，说明它其实是表级字段（如订单号）
    rows = payload.get("rows")
    if isinstance(rows, list) and rows:
        keys = [k for k in rows[0].keys()] if isinstance(rows[0], dict) else []
        for key in keys:
            values = {
                collapse_ws(str(row.get(key, "")))
                for row in rows
                if isinstance(row, dict) and collapse_ws(str(row.get(key, "")))
            }
            if len(values) == 1:
                value = values.pop()
                out.setdefault(
                    normalize_key(str(key)),
                    Candidate(
                        value=value,
                        strategy="llm",
                        locator=f"大模型：明细列「{key}」各行取值一致",
                        evidence=value,
                        matched_name=str(key),
                    ),
                )
    return out


__all__ = [
    "Candidate",
    "Extractor",
    "build_prompt",
    "summarize_document_as_model",
    "BASE_CONFIDENCE",
    "MIN_MATCH_SCORE",
    "PREVIEW_ROWS",
]
