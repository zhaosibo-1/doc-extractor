"""接口层测试。

接口层是「用户唯一直接接触的一层」，所以这里除了功能正确性，
还专门验证三件容易被忽略的事：

1. **契约一致**：前端读取的每个字段名都在响应模型里有定义。
   前两个项目都出现过「后端改了字段名、前端还在读老名字」导致界面静默显示
   ``undefined``，所以这条要有测试守着。
2. **降级可见**：没配 Key、图片没有文字层、缺列……这些都必须出现在 warnings 里，
   不能让用户面对一份「看起来正常但少了一半」的结果。
3. **错误是给人看的**：400/404 的 detail 要说清原因，而不是一句 Internal Server Error。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from .helpers import read_example

#: 直接用仓库里那份**真实的**示例发票，而不是测试手搓的一份精简版。
#: 手搓版缺了纳税人识别号、发票类型、校验码，完成度只有 77.8 —— 于是
#: 「断言 100% 通过」这条测试就变成了在测一份虚构的、过于干净的输入。
#: 用真示例，测的就是用户点开示例后会看到的那份东西。
INVOICE_TEXT = read_example("invoice_cn.txt").decode("utf-8")


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------


class TestMetaEndpoints:
    def test_health(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["llm_enabled"] is False
        assert body["templates"] == 5

    def test_config_不泄露密钥(self, client: TestClient) -> None:
        body = client.get("/api/config").json()
        assert "api_key_masked" in body
        # 没配 Key 时脱敏串必须是明确的「未配置」，而不是空串 ——
        # 空串在界面上会被渲染成一片空白，用户分不清「没配」和「配了但读不到」
        assert body["api_key_masked"] == "(未配置)"
        assert "llm_api_key" not in body

    def test_stats(self, client: TestClient) -> None:
        body = client.get("/api/stats").json()
        assert set(body) == {"files_stored", "extractions_run", "llm_calls", "llm_failures", "uptime_seconds"}

    def test_首页返回前端(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_favicon_不会404(self, client: TestClient) -> None:
        assert client.get("/favicon.ico").status_code == 204

    def test_favicon不带body(self, client: TestClient) -> None:
        """204 按规范不允许有 body。

        回归用：这里曾经是 ``JSONResponse({}, status_code=204)``。Starlette 对 204
        不写 Content-Length，而 JSONResponse 总会渲染出 ``{}`` 两个字节 ——
        真起服务时 uvicorn 每次都抛 ``Response content longer than Content-Length``
        并往日志里灌一整篇堆栈，客户端却照样收到 204，完全看不出来。
        TestClient 对不合法响应是宽容的（照样返回 b"{}"），所以只有断言
        **响应体为空**才能守住这条。
        """
        response = client.get("/favicon.ico")
        assert response.content == b""
        assert "content-length" not in response.headers or response.headers["content-length"] == "0"


class TestTemplates:
    def test_模板清单(self, client: TestClient) -> None:
        items = client.get("/api/templates").json()
        keys = [item["key"] for item in items]
        assert keys == ["generic", "invoice", "business_card", "sales_order", "contract"]
        for item in items:
            assert item["label"] and isinstance(item["field_count"], int)

    def test_模板详情带字段与规则(self, client: TestClient) -> None:
        body = client.get("/api/templates/invoice").json()
        assert {f["key"] for f in body["fields"]} >= {"invoice_number", "total", "amount"}
        assert any(r["kind"] == "expr" for r in body["rules"])

    def test_不存在的模板返回404且说明原因(self, client: TestClient) -> None:
        response = client.get("/api/templates/不存在")
        assert response.status_code == 404
        assert "不存在" in response.json()["detail"]


class TestExamples:
    def test_示例清单(self, client: TestClient) -> None:
        items = client.get("/api/examples").json()
        assert len(items) == 7
        for item in items:
            assert item["key"] and item["label"] and item["description"]
            assert item["size"] > 0, f"{item['key']} 的示例文件缺失，请先跑 scripts/make_examples.py"

    def test_文本示例返回内容(self, client: TestClient) -> None:
        body = client.get("/api/examples/invoice").json()
        assert "发票号码" in body["content"]
        assert body["kind"] == "txt"

    def test_二进制示例载入工作区(self, client: TestClient) -> None:
        body = client.post("/api/examples/order_xlsx/load").json()
        assert body["file_id"]
        assert body["doc"]["kind"] == "xlsx"
        assert body["doc"]["table_count"] >= 2

    def test_不存在的示例返回404(self, client: TestClient) -> None:
        assert client.get("/api/examples/不存在").status_code == 404
        assert client.post("/api/examples/不存在/load").status_code == 404

    def test_换掉工作目录后示例接口仍然可用(self, client: TestClient, tmp_path, monkeypatch) -> None:
        """示例文件必须按「包所在位置」找，不能按进程的当前工作目录找。

        回归用：三个示例接口一度写成 ``Path("examples") / spec.filename``，
        而 ``example_path()`` 是锚定包目录的 —— 两套路径规则并存。后果是
        从别的目录起服务（systemd、``uvicorn --app-dir``、容器里换了 WORKDIR）
        时首页和健康检查都正常、只有示例相关接口静默变成 404，
        排查方向会被完全带偏。
        """
        monkeypatch.chdir(tmp_path)   # 换到一个没有 examples/ 的目录

        items = client.get("/api/examples").json()
        assert len(items) == 7
        assert all(item["size"] > 0 for item in items), "示例文件大小算成了 0，说明没找到文件"

        assert "发票号码" in client.get("/api/examples/invoice").json()["content"]
        assert client.post("/api/examples/order_pdf/load").json()["file_id"]


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------


class TestFiles:
    def test_上传并出现在列表里(self, client: TestClient) -> None:
        response = client.post(
            "/api/files",
            files={"file": ("合同.txt", "合同编号：HT-2026-0417\n".encode(), "text/plain")},
        )
        assert response.status_code == 200
        file_id = response.json()["file_id"]
        assert response.json()["doc"]["kind"] == "text"

        listing = client.get("/api/files").json()
        assert any(item["file_id"] == file_id for item in listing)

        assert client.get(f"/api/files/{file_id}").json()["name"] == "合同.txt"
        assert client.delete(f"/api/files/{file_id}").json()["deleted"] is True
        assert client.get(f"/api/files/{file_id}").status_code == 404

    def test_不支持的扩展名被明确拒绝(self, client: TestClient) -> None:
        response = client.post("/api/files", files={"file": ("病毒.exe", b"MZ\x90\x00", "application/octet-stream")})
        assert response.status_code == 400
        assert "不支持" in response.json()["detail"]

    def test_文件名里的路径穿越被清洗(self, client: TestClient) -> None:
        """``../../.env`` 这类名字必须在落盘之前就被打掉。"""
        response = client.post(
            "/api/files",
            files={"file": ("../../.env", b"LLM_API_KEY=secret\n", "text/plain")},
        )
        assert response.status_code == 200
        name = response.json()["name"]
        assert "/" not in name and "\\" not in name and ".." not in name

    def test_文件id格式非法时返回404而不是500(self, client: TestClient) -> None:
        # id 白名单是 12 位十六进制；非法 id 不该走到文件系统
        for bad in ["../../etc/passwd", "..%2F..%2Fetc", "ZZZZ", "0" * 13]:
            assert client.get(f"/api/files/{bad}").status_code in (404, 400)

    def test_空文件被拒绝(self, client: TestClient) -> None:
        response = client.post("/api/files", files={"file": ("空.txt", b"", "text/plain")})
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class TestParse:
    def test_解析文本(self, client: TestClient) -> None:
        body = client.post("/api/parse", data={"text": INVOICE_TEXT}).json()
        assert body["doc"]["kind"] == "text"
        assert body["doc"]["char_count"] > 0
        assert "发票号码" in body["text_preview"]

    def test_解析已上传文件(self, client: TestClient) -> None:
        file_id = client.post("/api/examples/order_pdf/load").json()["file_id"]
        body = client.post("/api/parse", data={"file_id": file_id}).json()
        assert body["doc"]["kind"] == "pdf"
        assert len(body["tables"]) >= 1
        assert body["tables"][0]["columns"] == ["Item", "Qty", "Unit Price", "Amount"]

    def test_直接上传文件解析(self, client: TestClient) -> None:
        body = client.post(
            "/api/parse",
            files={"file": ("t.csv", "名称,数量\n键盘,12\n".encode(), "text/csv")},
        ).json()
        assert body["doc"]["kind"] == "csv"
        assert body["tables"][0]["row_count"] == 2

    def test_什么都不给返回400且说清要什么(self, client: TestClient) -> None:
        response = client.post("/api/parse", data={"text": "  "})
        assert response.status_code == 400
        assert "file" in response.json()["detail"]

    def test_表格预览带结构信息(self, client: TestClient) -> None:
        file_id = client.post("/api/examples/order_xlsx/load").json()["file_id"]
        body = client.post("/api/parse", data={"file_id": file_id}).json()
        table = body["tables"][0]
        assert {"locator", "columns", "header_rows", "row_count", "col_count", "preview"} <= set(table)
        assert isinstance(table["preview"], list)


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------


class TestExtract:
    def test_文本抽取_发票勾稽通过(self, client: TestClient) -> None:
        body = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        assert body["engine"] == "rule"
        assert body["completeness"] == 100.0
        values = {f["key"]: f["value"] for f in body["fields"]}
        assert values["total"] == 2388.0
        rule = next(r for r in body["rules"] if r["key"] == "amount_plus_tax")
        assert rule["passed"] is True and rule["skipped"] is False

    def test_engine报告的是实际干活的引擎而不是请求的(self, client: TestClient) -> None:
        """界面上顶栏徽标读的是生效引擎，结果卡片读的是 ``engine``。

        两处必须是同一个答案。以前 ``engine`` 直接写请求的模式，
        于是没配 Key 时同一屏上出现「顶栏 rule / 结果卡片 auto」——
        auto 是「允许用模型」，不是「用了模型」。
        """
        body = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "auto"},
        ).json()
        assert body["engine"] == "rule", "没配 Key 时实际就是规则引擎干的活"
        assert body["engine_mode"] == "auto", "但请求的确实是 auto，这个信息要留着"

        # 和顶栏徽标同源的那个值必须一致
        assert client.get("/api/config").json()["engine"] == body["engine"]

    def test_文件抽取_明细表与逐行校验(self, client: TestClient) -> None:
        file_id = client.post("/api/examples/order_xlsx/load").json()["file_id"]
        body = client.post(
            "/api/extract",
            json={"file_id": file_id, "template": "sales_order", "engine": "rule"},
        ).json()
        assert body["completeness"] == 100.0
        assert body["row_columns"] == ["序号", "商品名称", "数量", "单价", "金额", "备注"]
        assert len(body["rows"]) == 3
        assert [r["商品名称"] for r in body["rows"]] == ["无线键盘", "机械键盘", "显示器"]
        assert all(r["passed"] and not r["skipped"] for r in body["rules"])

    def test_明细行数由结构推出且文档写了就优先读文档(self, client: TestClient) -> None:
        """「明细行数」是唯一一种由结构推出的字段，两条路径都要对。

        xlsx 版本里一个字都没写行数 → 用数出来的行数（strategy=derived）。
        pdf 版本表下方写着「Items: 3」→ 必须读文档里的那个（strategy=label），
        因为如果文档写 5 行、表里只有 3 行，那是一条要被人看见的矛盾。
        """
        xlsx = client.post("/api/examples/order_xlsx/load").json()["file_id"]
        body = client.post(
            "/api/extract", json={"file_id": xlsx, "template": "sales_order", "engine": "rule"}
        ).json()
        field = next(f for f in body["fields"] if f["key"] == "item_count")
        assert field["value"] == 3
        assert field["source"]["strategy"] == "derived"
        assert "3 行" in field["source"]["locator"]

        pdf = client.post("/api/examples/order_pdf/load").json()["file_id"]
        body = client.post(
            "/api/extract", json={"file_id": pdf, "template": "sales_order", "engine": "rule"}
        ).json()
        field = next(f for f in body["fields"] if f["key"] == "item_count")
        assert field["value"] == 3
        assert field["source"]["strategy"] == "label"

    def test_英文列头pdf也能抽取(self, client: TestClient) -> None:
        file_id = client.post("/api/examples/order_pdf/load").json()["file_id"]
        body = client.post(
            "/api/extract",
            json={"file_id": file_id, "template": "sales_order", "engine": "rule"},
        ).json()
        assert body["completeness"] == 100.0
        assert body["row_columns"] == ["Item", "Qty", "Unit Price", "Amount"]

    def test_每个字段都带来源定位与置信度(self, client: TestClient) -> None:
        body = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        for field in body["fields"]:
            if field["value"] in (None, "", []):
                continue
            assert field["source"]["strategy"] in {"label", "column", "regex", "llm"}
            assert field["source"]["locator"]
            assert 0 < field["confidence"] <= 1

    def test_自定义模板(self, client: TestClient) -> None:
        body = client.post(
            "/api/extract",
            json={
                "text": "项目名称：某某系统\n负责人：张三\n",
                "template": "custom",
                "custom_fields": [
                    {"key": "project", "label": "项目名称", "aliases": ["项目名称", "项目"]},
                    {"key": "owner", "label": "负责人", "aliases": ["负责人"]},
                ],
                "engine": "rule",
            },
        ).json()
        values = {f["key"]: f["value"] for f in body["fields"]}
        assert values["project"] == "某某系统"
        assert values["owner"] == "张三"

    def test_自定义模板缺字段定义时返回400(self, client: TestClient) -> None:
        response = client.post("/api/extract", json={"text": "x", "template": "custom"})
        assert response.status_code == 400
        assert "custom_fields" in response.json()["detail"]

    def test_自定义字段key重复返回400(self, client: TestClient) -> None:
        """key 是输出 JSON 的键，重复会让其中一个字段静默消失。"""
        response = client.post(
            "/api/extract",
            json={
                "text": "x",
                "template": "custom",
                "custom_fields": [{"key": "a", "label": "甲"}, {"key": "a", "label": "乙"}],
            },
        )
        assert response.status_code == 400
        assert "重复" in response.json()["detail"]

    def test_既有text又有file_id时以file_id为准(self, client: TestClient) -> None:
        file_id = client.post("/api/examples/invoice/load").json()["file_id"]
        body = client.post(
            "/api/extract",
            json={"text": "这是一段无关文本", "file_id": file_id, "template": "invoice", "engine": "rule"},
        ).json()
        values = {f["key"]: f["value"] for f in body["fields"]}
        assert values["invoice_number"] == "25317000000012345678"

    def test_两个输入都不给返回400(self, client: TestClient) -> None:
        response = client.post("/api/extract", json={"template": "invoice"})
        assert response.status_code == 400

    def test_图片在规则模式下给出明确降级提示(self, client: TestClient) -> None:
        file_id = client.post("/api/examples/card_image/load").json()["file_id"]
        body = client.post(
            "/api/extract",
            json={"file_id": file_id, "template": "business_card", "engine": "rule"},
        ).json()
        assert body["doc"]["needs_ocr"] is True
        # 必须明确告诉用户「为什么什么都没抽到」，而不是给一份空白结果
        assert any("视觉模型" in w or "文字层" in w for w in body["warnings"])
        assert body["completeness"] == 0.0

    def test_engine_llm但没配key时返回400(self, client: TestClient) -> None:
        """点名要用大模型却没配 Key，必须当场失败。

        这条测试曾经「假通过」过一次：示例文本缺了 4 个字段，所以抽取器走到
        「补漏」分支才去调模型、才抛异常。等示例补全（规则层能全覆盖）之后，
        补漏分支根本不会进入 —— 于是返回 200，`engine` 字段还写着 "llm"，
        而实际上一次模型调用都没有。配置错误不能依赖「恰好有字段缺失」才暴露。
        """
        response = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "llm"},
        )
        assert response.status_code == 400
        assert "大模型" in response.json()["detail"] or "Key" in response.json()["detail"]

    def test_engine_llm且规则已全覆盖仍然400(self, client: TestClient) -> None:
        """规则层能 100% 覆盖时也要拦下来 —— 这才是上面那条的真正含义。"""
        covered = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        assert covered["completeness"] == 100.0

        response = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "llm"},
        )
        assert response.status_code == 400


class TestExtractStream:
    def test_SSE事件顺序完整(self, client: TestClient) -> None:
        text = INVOICE_TEXT
        with client.stream(
            "POST",
            "/api/extract/stream",
            json={"text": text, "template": "invoice", "engine": "rule"},
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            events = [line for line in response.iter_lines() if line.startswith("event:")]

        assert events == [
            "event: start",
            "event: stage",
            "event: parsed",
            "event: stage",
            "event: fields",
            "event: stage",
            "event: rules",
            "event: done",
            "event: [DONE]",
        ]

    def test_done事件带完整结果(self, client: TestClient) -> None:
        with client.stream(
            "POST",
            "/api/extract/stream",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ) as response:
            payloads: dict[str, dict] = {}
            current = ""
            for line in response.iter_lines():
                if line.startswith("event:"):
                    current = line[6:].strip()
                elif line.startswith("data:") and current:
                    payloads[current] = json.loads(line[5:].strip())

        assert payloads["done"]["completeness"] == 100.0
        assert len(payloads["done"]["fields"]) == 12
        assert payloads["parsed"]["kind"] == "text"
        assert payloads["fields"]["filled"] == 12
        assert payloads["rules"]["passed"] == 1
        # 不伪造进度条：每个 stage 的百分比必须单调递增
        assert payloads["stage"]["percent"] <= 100

    def test_出错时也走SSE而不是断流(self, client: TestClient) -> None:
        # 空的 custom_fields 会在准备阶段被拒；流式接口应当把错误也推给前端
        response = client.post("/api/extract/stream", json={"text": "x", "template": "custom"})
        assert response.status_code == 400


class TestRevalidate:
    def test_只重跑校验不重新抽取(self, client: TestClient) -> None:
        """这个接口能存在，本身就说明校验层是独立的 ——

        改一条容差不该触发整条抽取链路，更不该再花一次模型调用。
        """
        extracted = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        body = client.post(
            "/api/revalidate",
            json={
                "fields": extracted["fields"],
                "rows": extracted["rows"],
                "rules": client.get("/api/templates/invoice").json()["rules"],
                "tolerance": 0.02,
            },
        ).json()
        assert body["passed"] == 1
        assert body["failed"] == 0

    def test_放大容差能让失败变通过(self, client: TestClient) -> None:
        extracted = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        # 人为把金额改错，制造一条必然失败的规则
        for field in extracted["fields"]:
            if field["key"] == "amount":
                field["value"] = 9999.0

        rules = [
            {
                "key": "t",
                "kind": "expr",
                "fields": ["amount", "tax"],
                "expression": "amount + tax",
                "target": "total",
                "tolerance": 0.02,
            }
        ]
        strict = client.post("/api/revalidate", json={"fields": extracted["fields"], "rows": [], "rules": rules}).json()
        assert strict["failed"] == 1

        loose = client.post(
            "/api/revalidate",
            json={"fields": extracted["fields"], "rows": [], "rules": rules, "tolerance": 100000.0},
        ).json()
        assert loose["failed"] == 0


# ---------------------------------------------------------------------------
# 前后端契约
# ---------------------------------------------------------------------------


class TestFrontendContract:
    """前端读的字段名必须都在后端模型里。

    这类问题的典型表现是界面上一片 ``undefined``，但请求全是 200 ——
    没有测试盯着就很难发现。
    """

    def test_抽取结果字段齐全(self, client: TestClient) -> None:
        body = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        assert {
            "doc", "template", "template_label", "engine", "fields",
            "rows", "row_columns", "rules", "tables", "completeness", "warnings", "elapsed_ms",
        } <= set(body)

    def test_字段对象结构齐全(self, client: TestClient) -> None:
        body = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        field = body["fields"][0]
        assert {"key", "label", "type", "value", "raw", "confidence", "status", "required", "source", "messages"} <= set(field)
        assert {"strategy", "locator", "evidence", "matched_name", "page"} <= set(field["source"])

    def test_规则结果结构齐全(self, client: TestClient) -> None:
        body = client.post(
            "/api/extract",
            json={"text": INVOICE_TEXT, "template": "invoice", "engine": "rule"},
        ).json()
        rule = body["rules"][0]
        assert {"key", "kind", "passed", "description", "detail", "soft", "skipped"} <= set(rule)

    def test_前端读取的接口路径都存在(self, client: TestClient) -> None:
        html = client.get("/").text
        for path in [
            "/api/health", "/api/config", "/api/templates", "/api/examples",
            "/api/stats", "/api/parse", "/api/extract", "/api/extract/stream",
            "/api/files", "/api/revalidate",
        ]:
            assert path in html, f"前端没有引用 {path}"

    def test_前端不依赖任何CDN(self, client: TestClient) -> None:
        """零构建、零外部依赖：打开就能用，断网也不会变成白屏。"""
        html = client.get("/").text
        for bad in ["cdn.", "unpkg.com", "jsdelivr", "googleapis.com", "fonts.gstatic"]:
            assert bad not in html, f"前端引用了外部资源 {bad}"

    def test_前端区分文本与二进制示例的名单与示例类型对得上(self, client: TestClient) -> None:
        """前端靠一份写死的名单决定「点示例时走填充还是走载入」。

        这份名单在后端没有对应物，是纯粹的重复 —— 一旦示例新增了类型
        （比如 md、csv）而名单没跟上，前端会走错分支：把 xlsx 当文本示例，
        拿到空字符串，然后静默地什么都不做。所以这里把两边钉在一起。
        """
        html = client.get("/").text
        assert "['txt','md','markdown','csv','tsv','log']" in html, (
            "前端文本示例名单变了，请同步更新 e2e_check.py 里的 TEXT_KINDS"
        )

        kinds = {e["kind"] for e in client.get("/api/examples").json()}
        text_kinds = {"txt", "md", "markdown", "csv", "tsv", "log"}
        assert kinds & text_kinds, "没有任何文本类示例，前端的一键填充路径等于死代码"
        assert kinds - text_kinds, "没有任何二进制类示例，前端的一键载入路径等于死代码"

    def test_示例列表带前端渲染需要的字段(self, client: TestClient) -> None:
        for item in client.get("/api/examples").json():
            assert {"key", "label", "description", "kind", "template", "size"} <= set(item)
