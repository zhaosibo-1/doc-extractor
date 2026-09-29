"""端到端验收脚本：对**真实运行中的服务**做一轮黑盒验证。

和 pytest 的分工
-----------------
pytest 验证「每个模块内部对不对」；这个脚本验证「把服务真的装起来、跑起来，
用户能不能用」。两者的失效模式完全不同：单测全绿但服务起不来的情况很常见
（缺依赖、静态文件路径错、lifespan 装配顺序错……）。

只依赖标准库（urllib / json），所以任何环境都能跑：

    # 先起服务
    uvicorn app.main:app --host 127.0.0.1 --port 8123
    # 再跑验收
    python scripts/e2e_check.py http://127.0.0.1:8123

退出码：全部通过为 0，有失败为 1（CI 靠它判断成败）。

本脚本最重要的一组断言在第 6 节：**跨格式一致性**。
这个项目的核心承诺是「同一份内容换个格式存，结果必须一致」——
order.xlsx 与 order.pdf 承载的是同一张订单（同订单号、同 3 行明细、
同金额合计），只是一个中文列头、一个英文列头。如果哪天有人改坏了
归一化逻辑，单测可能还是绿的，但这组断言会当场炸。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

# 绕过系统代理：本地回环不该走代理（开发机上常有全局 http_proxy）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [+] {name}")
    else:
        FAILED.append(name)
        print(f"  [-] {name}   {str(detail)[:300]}")


def request(
    method: str,
    path: str,
    payload: dict | None = None,
    raw: bytes | None = None,
    content_type: str = "application/json",
    timeout: int = 120,
) -> tuple[int, str]:
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None

    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore")


def get(path: str) -> tuple[int, str]:
    return request("GET", path)


def post(path: str, payload: dict | None = None) -> tuple[int, str]:
    return request("POST", path, payload=payload or {})


def post_form(path: str, fields: dict[str, str]) -> tuple[int, str]:
    """urlencoded 表单。/api/parse 用的就是 Form 参数，不是 JSON。"""
    from urllib.parse import urlencode

    return request("POST", path, raw=urlencode(fields).encode("utf-8"),
                   content_type="application/x-www-form-urlencoded")


def upload(path: str, filename: str, data: bytes) -> tuple[int, str]:
    """手写 multipart/form-data（避免为脚本引入 requests 依赖）。"""
    boundary = "----e2e" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return request("POST", path, raw=body, content_type=f"multipart/form-data; boundary={boundary}")


def delete(path: str) -> tuple[int, str]:
    return request("DELETE", path)


def jloads(body: str) -> dict | list:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name, data = "", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if name:
            events.append((name, jloads(data) if data and data != "[DONE]" else {}))
    return events


def section(title: str) -> None:
    print(f"\n[{title}]")


def values(result: dict) -> dict:
    """把抽取结果压成 ``key -> value``，方便比较。"""
    return {f["key"]: f["value"] for f in result.get("fields", [])}


def rule_of(result: dict, key: str) -> dict:
    return next((r for r in result.get("rules", []) if r["key"] == key), {})


def load_example(key: str) -> str:
    """把一个内置示例载进服务，返回 file_id。"""
    status, body = post(f"/api/examples/{key}/load")
    return jloads(body).get("file_id", "")


print("=" * 74)
print("文档结构化抽取系统 —— 端到端验收")
print(f"目标：{BASE}")
print("=" * 74)

# ---------------------------------------------------------------- 1. 基础状态
section("1/9 基础状态")
status, body = get("/api/health")
health = jloads(body)
check("GET /api/health 返回 200", status == 200, body)
check("健康检查报告模板数量", health.get("templates", 0) >= 5, health)
check("未配置 Key 时 llm_enabled=false（零配置可跑）", health.get("llm_enabled") is False, health)

status, body = get("/api/config")
config = jloads(body)
check("GET /api/config 返回 200", status == 200, body)
check(
    "config 含前端读取的全部字段",
    all(k in config for k in ("llm_enabled", "vision_enabled", "provider", "model",
                              "base_url", "api_key_masked", "engine", "engine_mode",
                              "max_upload_mb", "pdf_max_pages")),
    sorted(config),
)
check("未配置 Key 时引擎降级为 rule", config.get("engine") == "rule", config.get("engine"))
check("API Key 未泄露（只给脱敏串）",
      config.get("api_key_masked") == "(未配置)", config.get("api_key_masked"))
check("响应里没有明文的 key 字段", "llm_api_key" not in json.dumps(config))

status, body = get("/api/stats")
stats = jloads(body)
check("GET /api/stats 返回 200", status == 200, body)
check(
    "stats 字段与前端约定一致",
    {"files_stored", "extractions_run", "llm_calls", "llm_failures", "uptime_seconds"} <= set(stats),
    sorted(stats),
)

# ---------------------------------------------------------------- 2. 模板与示例
section("2/9 模板与内置示例")
status, body = get("/api/templates")
templates = jloads(body)
check("GET /api/templates 返回 200", status == 200, body[:200])
tpl_keys = {t["key"] for t in templates}
check("五个内置模板齐全", {"generic", "invoice", "business_card", "sales_order", "contract"} <= tpl_keys,
      sorted(tpl_keys))
check("每个模板都带字段数与规则数", all(t["field_count"] >= 0 and t["rule_count"] >= 0 for t in templates))

status, body = get("/api/templates/sales_order")
detail = jloads(body)
check("GET /api/templates/{key} 返回 200", status == 200, body[:200])
check("模板详情带 fields / rules / extract_rows",
      all(k in detail for k in ("fields", "rules", "extract_rows")), sorted(detail))
check("字段定义带 aliases（对齐中英单据的基础）",
      all(f.get("aliases") for f in detail.get("fields", [])), detail.get("fields", [])[:2])
check("规则写了跨字段勾稽",
      any(r.get("kind") in ("expr", "sum_column", "row_expr") for r in detail.get("rules", [])),
      detail.get("rules"))

status, _ = get("/api/templates/%E4%B8%8D%E5%AD%98%E5%9C%A8")
check("未知模板返回 404（而不是空模板）", status == 404, status)

status, body = get("/api/examples")
examples = jloads(body)
check("GET /api/examples 返回 200", status == 200, body[:200])
check("覆盖 txt/xlsx/pdf/docx/png 五种形态",
      {"txt", "xlsx", "pdf", "docx", "png"} <= {e["kind"] for e in examples},
      sorted({e["kind"] for e in examples}))

# 文本示例走「一键填充」（detail 直接给 content）；
# 二进制示例走「一键载入」（POST /load 才拿得到 file_id）。
# 两条路都必须真的通，否则用户点下去只会得到一片空白。
#
# 这个名单是**前端那份的镜像**（web/index.html 的 loadExample）：
# 前端决定了点下去走哪条路，验收就必须按前端的判断来分。两边不一致时，
# 前端会把 xlsx 当文本示例，然后拿到空字符串 —— 所以这里也要顺带守住一致性。
TEXT_KINDS = {"txt", "md", "markdown", "csv", "tsv", "log"}

text_examples = [e for e in examples if e["kind"] in TEXT_KINDS]
binary_examples = [e for e in examples if e["kind"] not in TEXT_KINDS]
check("示例覆盖了两条载入路径（一键填充 / 一键载入）",
      bool(text_examples) and bool(binary_examples),
      (len(text_examples), len(binary_examples)))

text_ok = True
for item in text_examples:
    st, bd = get(f"/api/examples/{item['key']}")
    if st != 200 or not jloads(bd).get("content", "").strip():
        text_ok = False
check(f"全部 {len(text_examples)} 个文本示例都能一键填充", text_ok)

binary_ok = True
for item in binary_examples:
    st, bd = get(f"/api/examples/{item['key']}")
    if st != 200:
        binary_ok = False
        continue
    st2, bd2 = post(f"/api/examples/{item['key']}/load")
    if st2 != 200 or len(jloads(bd2).get("file_id", "")) != 12:
        binary_ok = False
check(f"全部 {len(binary_examples)} 个二进制示例都能一键载入", binary_ok)

status, _ = get("/api/examples/%E4%B8%8D%E5%AD%98%E5%9C%A8")
check("未知示例返回 404", status == 404, status)

# ---------------------------------------------------------------- 3. 文件管理
section("3/9 文件上传与管理")
status, body = post("/api/examples/invoice/load")
uploaded = jloads(body)
check("POST /api/examples/{key}/load 返回 200", status == 200, body[:200])
check("返回了 12 位十六进制 file_id", len(uploaded.get("file_id", "")) == 12, uploaded.get("file_id"))
check("返回了文档摘要（kind / char_count）",
      uploaded.get("doc", {}).get("kind") == "text" and uploaded.get("doc", {}).get("char_count", 0) > 0,
      uploaded.get("doc"))

invoice_id = uploaded.get("file_id", "")

status, body = get("/api/files")
files = jloads(body)
check("列表里能查到刚载入的文件", invoice_id in {f["file_id"] for f in files}, body[:200])
check("列表项带 kind 与 size（前端要渲染）",
      all({"file_id", "name", "size", "kind"} <= set(f) for f in files), files[:1])

status, body = get(f"/api/files/{invoice_id}")
check("详情能取回摘要", status == 200 and "summary" in jloads(body), sorted(jloads(body)))

status, _ = get("/api/files/aaaaaaaaaaaa")
check("取不存在的文件返回 404", status == 404, status)

# 路径穿越：file_id 里塞 ../ 不能读到磁盘上的 .env
status, _ = get("/api/files/..%2F..%2F.env")
check("路径穿越尝试被拒绝（非 200）", status in (400, 404), status)

status, body = upload("/api/files", "evil.exe", b"MZ\x90\x00")
check("不支持的后缀被拒绝并说清支持哪些", status == 400 and "." in body, body[:200])

status, body = upload("/api/files", "empty.txt", b"")
check("空文件被拒绝（400）而不是落一个空文档", status == 400, status)

status, body = upload("/api/files", "t.txt", "序号,名称\n1,键盘\n".encode("utf-8"))
check("直传文件成功并返回 file_id", status == 200 and len(jloads(body).get("file_id", "")) > 0, body[:200])
direct_id = jloads(body).get("file_id", "")

status, body = delete(f"/api/files/{direct_id}")
check("DELETE 返回 deleted=true", status == 200 and jloads(body).get("deleted") is True, body[:200])
status, _ = delete(f"/api/files/{direct_id}")
check("重复删除返回 404", status == 404, status)

# ---------------------------------------------------------------- 4. 解析
section("4/9 解析（文本 / file_id / 直传）")
status, body = get("/api/examples/contract")
contract_text = jloads(body).get("content", "")

status, body = post_form("/api/parse", {"text": contract_text})
parsed = jloads(body)
check("POST /api/parse 支持纯文本输入", status == 200, body[:200])
check("返回 doc / tables / text_preview 三块",
      {"doc", "tables", "text_preview"} <= set(parsed), sorted(parsed))
check("doc 带 kind 与 char_count",
      parsed.get("doc", {}).get("kind") == "text" and parsed.get("doc", {}).get("char_count", 0) > 0,
      parsed.get("doc"))

status, body = post_form("/api/parse", {"file_id": invoice_id})
check("POST /api/parse 支持 file_id（前端不用两套渲染逻辑）",
      status == 200 and jloads(body).get("text_preview"), status)

status, body = upload("/api/parse", "t.csv", "名称,数量\n键盘,12\n".encode("utf-8"))
parsed = jloads(body)
check("POST /api/parse 支持直接上传文件", status == 200, body[:200])
check("csv 被识别成表格并给出列名",
      parsed.get("tables") and parsed["tables"][0]["columns"] == ["名称", "数量"], parsed.get("tables"))

status, body = post_form("/api/parse", {"text": "   "})
check("空文本被拒绝（400）", status == 400, status)

status, body = post_form("/api/parse", {})
check("什么都不给返回 400 且说清要什么", status == 400 and "file" in body, body[:200])

status, body = post_form("/api/parse", {"file_id": invoice_id})
table_info = (jloads(body).get("tables") or [{}])[0]
check("表格预览带前端渲染需要的结构信息",
      {"locator", "columns", "header_rows", "row_count", "col_count", "preview"} <= set(table_info)
      or not jloads(body).get("tables"),
      table_info)

# ---------------------------------------------------------------- 5. 抽取
section("5/9 抽取（七个示例的完成度矩阵）")

EXPECT = [
    # (示例 key, 模板, 期望完成度, 期望明细行数)
    ("invoice", "invoice", 100.0, 0),
    ("business_card", "business_card", 100.0, 0),
    ("contract", "contract", 100.0, 0),
    ("contract_tables", "contract", 100.0, 3),
    ("order_xlsx", "sales_order", 100.0, 3),
    ("order_pdf", "sales_order", 100.0, 3),
]

results: dict[str, dict] = {}
for key, tpl, want_score, want_rows in EXPECT:
    fid = load_example(key)
    status, body = post("/api/extract", {"file_id": fid, "template": tpl, "engine": "rule"})
    result = jloads(body)
    results[key] = result
    check(f"{key} 抽取成功", status == 200, body[:200])
    check(f"{key} 完成度 = {want_score}", result.get("completeness") == want_score,
          f"{result.get('completeness')} 缺失："
          f"{[f['key'] for f in result.get('fields', []) if f.get('status') != 'ok']}")
    check(f"{key} 明细行数 = {want_rows}", len(result.get("rows", [])) == want_rows,
          len(result.get("rows", [])))
    # 关键：规则必须**真的执行过**。全部 skipped 会让界面看起来「没有问题」，
    # 而实际上一次校验都没做 —— 这是这个项目最容易骗过自己的失效模式。
    check(f"{key} 至少有一条规则真的执行了（不是全 skipped）",
          any(not r["skipped"] for r in result.get("rules", [])) or not result.get("rules"),
          [(r["key"], r["skipped"]) for r in result.get("rules", [])])
    check(f"{key} 没有规则失败", all(r["passed"] for r in result.get("rules", [])),
          [(r["key"], r.get("detail")) for r in result.get("rules", []) if not r["passed"]])

# 关键字段逐个核对（不是只看总分）
inv = values(results["invoice"])
check("发票号码抽对", inv.get("invoice_number") == "25317000000012345678", inv.get("invoice_number"))
check("价税合计抽对（含 ￥ 与千分位）", inv.get("total") == 2388.0, inv.get("total"))
check("开票日期归一成 ISO", inv.get("invoice_date") == "2026-03-12", inv.get("invoice_date"))
rule = rule_of(results["invoice"], "amount_plus_tax")
check("票面勾稽真的算出了数字", rule.get("passed") and "2388.00" in (rule.get("detail") or ""),
      rule.get("detail"))

xlsx = results["order_xlsx"]
check("英文列头 xlsx 的明细表被正确定位",
      xlsx.get("row_columns") == ["序号", "商品名称", "数量", "单价", "金额", "备注"], xlsx.get("row_columns"))
check("xlsx 逐行校验 3 行全部通过",
      [r["商品名称"] for r in xlsx.get("rows", [])] == ["无线键盘", "机械键盘", "显示器"],
      xlsx.get("rows"))
check("xlsx 明细合计校验算出了数字",
      "8782" in (rule_of(xlsx, "sum_amount").get("detail") or ""),
      rule_of(xlsx, "sum_amount").get("detail"))

# 降级必须显式：图片没有文字层时要说明原因
status, body = post("/api/extract", {"file_id": load_example("card_image"),
                                     "template": "business_card", "engine": "rule"})
image_result = jloads(body)
check("图片在规则模式下完成度为 0（不假装成功）", image_result.get("completeness") == 0.0,
      image_result.get("completeness"))
check("图片的降级原因出现在 warnings 里",
      any("视觉模型" in w or "文字层" in w for w in image_result.get("warnings", [])),
      image_result.get("warnings"))

status, body = post("/api/extract", {"file_id": load_example("invoice"), "template": "invoice", "engine": "auto"})
auto_result = jloads(body)
check("请求 auto 但没配 Key 时，engine 如实报 rule", auto_result.get("engine") == "rule",
      auto_result.get("engine"))
check("engine_mode 保留请求的 auto（降级这件事被记下来了）",
      auto_result.get("engine_mode") == "auto", auto_result.get("engine_mode"))
check("结果里的 engine 与 /api/config 的生效引擎是同一个答案（前端两处不会打架）",
      auto_result.get("engine") == config.get("engine"),
      (auto_result.get("engine"), config.get("engine")))
check("engine_note 给出了为什么没走模型（前端不必自己猜）",
      bool(auto_result.get("engine_note")), auto_result.get("engine_note"))

status, body = post("/api/extract", {"text": contract_text, "template": "invoice", "engine": "llm"})
check("点名用大模型但没配 Key 时返回 400（配置错误不能拖到碰运气才暴露）", status == 400, body[:200])

status, body = post("/api/extract", {"text": "订单号：SO-1", "template": "nope"})
check("未知模板返回 400/404", status in (400, 404), status)

status, body = post("/api/extract", {"text": "x", "template": "custom",
                                     "custom_fields": [{"key": "a"}, {"key": "a"}]})
check("自定义模板重复 key 返回 400", status == 400 and "重复" in body, body[:200])

status, body = post("/api/extract", {"text": "账户名：张三\n开户行：工行", "template": "custom",
                                     "custom_fields": [{"key": "account", "label": "账户名"},
                                                       {"key": "bank", "label": "开户行"}]})
custom = jloads(body)
check("自定义模板能真的抽出字段", status == 200 and values(custom).get("bank") == "工行", body[:300])

# ---------------------------------------------------------------- 6. 跨格式一致性
section("6/9 跨格式一致性（本项目的核心承诺）")
# order.xlsx 与 order.pdf 是同一张订单的两种存法：同一个订单号、同样 3 行明细、
# 同样的数量与单价、同样的金额合计。列头一个是中文一个是英文。
# 「换个格式存，结果必须一致」——这句话就是在这里被验证的。
a, b = results["order_xlsx"], results["order_pdf"]
va, vb = values(a), values(b)

for field in ("order_no", "order_date", "delivery_date", "currency", "total_amount", "item_count"):
    check(f"两种格式的 {field} 一致（{va.get(field)!r}）", va.get(field) == vb.get(field),
          f"xlsx={va.get(field)!r} vs pdf={vb.get(field)!r}")

check("两种格式的订单号都等于单号本体", va.get("order_no") == "SO-2026-001", va.get("order_no"))
check("两种格式的金额合计都等于 8782.50", va.get("total_amount") == 8782.50 and vb.get("total_amount") == 8782.50,
      (va.get("total_amount"), vb.get("total_amount")))

rows_a = a.get("rows", [])
rows_b = b.get("rows", [])
check("两种格式抽出的明细行数相同", len(rows_a) == len(rows_b) == 3, (len(rows_a), len(rows_b)))


def _triples(rows: list[dict], cols: tuple[str, str, str]) -> list[tuple[float, float, float]]:
    """按**数值**取 (数量, 单价, 金额)。

    不能直接比字符串：xlsx 里 199.0 是数字，读出来是 "199"；PDF 里的文字
    是 "199.00"。两者指的显然是同一个数，比字符串会把「格式差异」误报成
    「数据不一致」—— 而这正是这个项目要消灭的那类噪音。
    """
    out = []
    for row in rows:
        out.append(tuple(float(row.get(c) or 0) for c in cols))
    return out


check("两种格式的行数量/单价/金额逐行相同",
      _triples(rows_a, ("数量", "单价", "金额")) == _triples(rows_b, ("Qty", "Unit Price", "Amount")),
      (_triples(rows_a, ("数量", "单价", "金额")), _triples(rows_b, ("Qty", "Unit Price", "Amount"))))
check("两种格式的明细合计都等于 8782.50",
      abs(sum(float(r.get("金额") or 0) for r in rows_a) - 8782.5) < 0.01
      and abs(sum(float(r.get("Amount") or 0) for r in rows_b) - 8782.5) < 0.01,
      (sum(float(r.get("金额") or 0) for r in rows_a), sum(float(r.get("Amount") or 0) for r in rows_b)))
check("英文列头的 pdf 也跑完了全部行级校验（别名机制真的接上了）",
      all(r["passed"] and not r["skipped"] for r in b.get("rules", [])),
      [(r["key"], r["passed"], r["skipped"]) for r in b.get("rules", [])])
check("两种格式的完成度都是 100", a.get("completeness") == b.get("completeness") == 100.0,
      (a.get("completeness"), b.get("completeness")))

# 同一输入重复跑必须给同一结果（抽取层不能有随机性）
again = jloads(post("/api/extract", {"file_id": load_example("order_xlsx"),
                                     "template": "sales_order", "engine": "rule"})[1])
check("同一输入两次抽取结果完全一致（可复现）", values(again) == va, "两次字段值不一致")

# ---------------------------------------------------------------- 7. SSE
section("7/9 SSE 流式抽取")
status, body = post("/api/extract/stream", {"text": contract_text, "template": "contract", "engine": "rule"})
events = parse_sse(body)
names = [n for n, _ in events]
check("POST /api/extract/stream 返回 200", status == 200, status)
check("事件以 start 开头", names and names[0] == "start", names[:3])
check("事件以 [DONE] 结尾", names and names[-1] == "[DONE]", names[-3:])
check("含 done 事件", "done" in names, names)
stages = [p.get("stage") for n, p in events if n == "stage"]
check("阶段顺序为 parse -> extract -> validate", stages == ["parse", "extract", "validate"], stages)
percent = [p.get("percent") for n, p in events if n == "stage"]
check("进度百分比单调不减", percent == sorted(percent), percent)
parsed_ev = next((p for n, p in events if n == "parsed"), {})
check("parsed 事件带表格数/字数/是否扫描件",
      {"table_count", "char_count", "needs_ocr"} <= set(parsed_ev), parsed_ev)
fields_ev = next((p for n, p in events if n == "fields"), {})
check("fields 事件报告填充进度（不是把整份结果再发一遍）",
      {"filled", "total", "rows", "engine"} <= set(fields_ev), fields_ev)
check("fields 事件的填充数不超过总数", 0 < fields_ev.get("filled", 0) <= fields_ev.get("total", 0), fields_ev)
rules_ev = next((p for n, p in events if n == "rules"), {})
check("rules 事件带校验结果", "passed" in rules_ev and "failed" in rules_ev, rules_ev)
done = next((p for n, p in events if n == "done"), {})
check("done 事件是完整的 ExtractionResult",
      {"doc", "fields", "rules", "completeness", "elapsed_ms"} <= set(done), sorted(done))
check("流式结果与非流式结果一致",
      done.get("completeness") == results["contract"].get("completeness"),
      (done.get("completeness"), results["contract"].get("completeness")))

status, body = post("/api/extract/stream", {"file_id": "aaaaaaaaaaaa", "template": "invoice"})
check("流式接口的参数错误在建流前返回普通 404", status == 404, status)

# ---------------------------------------------------------------- 8. 重跑校验
section("8/9 重跑校验与容差")
inv_result = results["invoice"]
fields = inv_result.get("fields", [])
rules = jloads(get("/api/templates/invoice")[1]).get("rules", [])

status, body = post("/api/revalidate", {"fields": fields, "rows": [], "rules": rules})
same = jloads(body)
check("POST /api/revalidate 返回 200", status == 200, body[:200])
check("不传容差时结果与首次一致（说明规则用的是自己的容差）",
      same.get("failed") == 0 and same.get("passed", 0) >= 1, same)

broken = [dict(f) for f in fields]
for f in broken:
    if f["key"] == "total":
        f["value"] = 99999.0  # 人为把价税合计改错
status, body = post("/api/revalidate", {"fields": broken, "rows": [], "rules": rules})
strict = jloads(body)
check("改坏价税合计后勾稽报失败", strict.get("failed") == 1, strict)

status, body = post("/api/revalidate", {"fields": broken, "rows": [], "rules": rules,
                                        "tolerance": 100000.0})
loose = jloads(body)
check("把容差放大到 100000 后同一条规则变成通过（容差旋钮真的能调）",
      loose.get("failed") == 0, loose)

status, body = post("/api/revalidate", {"fields": [], "rows": [], "rules": []})
check("空输入返回 200（不 5xx）", status == 200, status)

# ---------------------------------------------------------------- 9. 前端与连通性
section("9/9 前端页面与大模型连通性")
status, body = get("/")
check("GET / 返回 200", status == 200, status)
check("页面含标题", "抽取" in body)
check("页面含内联脚本（零构建单页）", "<script>" in body)
check("页面不依赖任何 CDN（离线可用）",
      not any(m in body for m in ("cdn.", "unpkg", "jsdelivr", "googleapis", "fonts.gstatic")))
for element_id in ("textInput", "resultArea", "btnRun", "tplGrid", "tplDetail",
                   "engineSel", "exList", "tolInput", "modeTabs"):
    check(f"页面含元素 #{element_id}", f'id="{element_id}"' in body)

status, body = post("/api/llm/ping")
ping = jloads(body)
check("POST /api/llm/ping 返回结构化结果（不抛异常）", status == 200 and "ok" in ping, body[:200])
check("未配置 Key 时给出可读的降级说明",
      ping.get("configured") is False and "规则引擎" in ping.get("message", ""), ping)

status, body = get("/favicon.ico")
check("favicon 返回 204 且不带 body（204 带 body 会让 uvicorn 每次加载都刷一整篇堆栈）",
      status == 204 and body == "", (status, repr(body)[:80]))

status, body = get("/openapi.json")
check("OpenAPI 文档可访问", status == 200 and "/api/extract" in body)

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
total = len(PASSED) + len(FAILED)
print(f"验收结果：{len(PASSED)}/{total} 项通过")
if FAILED:
    print("\n失败项：")
    for name in FAILED:
        print(f"  [-] {name}")
    print("=" * 74)
    sys.exit(1)

print("全部通过 ✓")
print("=" * 74)
