from datetime import date, timedelta
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from research_radar.conversation import (
    Conversation, TOOLS, actions, cancel_turn, compact_history, confirm_action, new_conversation, recent_messages,
    research_status, submit_turn, turns,
)
from research_radar.tool_calling import estimate_tokens, execute_tool
from research_radar.cs_sources import parse_crossref
from research_radar.mcp_client import add_mcp_server, call_external_mcp_tool, discover_mcp_tools
from research_radar.models import Page, Publication, Record, Settings, Topic, utc_now
from research_radar.projects import list_projects
from research_radar.research import tick_research
from research_radar.sources import SourceError, Sources, parse_openalex
from research_radar.v2_sources import parse_semantic_scholar
from test_radar import DAY, FixtureSources, preprint, radar


def _call(name, arguments, key=None):
    return {"id": key or name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def test_thinking_streaming_reasoning_passthrough_and_retry(radar):
    store, topic = radar
    chat = new_conversation(store, topic.id)
    settings = Settings(
        data_dir=store.directory, llm_model="deepseek-reasoner", llm_key="fixture",
        llm_base_url="https://api.deepseek.com", thinking_level="medium", stream_chat=True,
    )

    attempts = []

    def sse_and_retry(request):
        body = json.loads(request.content)
        attempts.append(body)
        assert body["thinking"] == {"type": "enabled"}
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate_limit"})
        if len(attempts) == 2:
            sse = "\n".join([
                'data: {"choices":[{"delta":{"reasoning_content":"先检索项目"}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"list_projects","arguments":"{}"}}]}}]}',
                'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":100,"completion_tokens":20}}',
                'data: [DONE]',
            ])
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
        # Third call must preserve reasoning_content from the assistant tool-call message
        assistant_msgs = [m for m in body["messages"] if m.get("role") == "assistant"]
        assert assistant_msgs and assistant_msgs[0]["reasoning_content"] == "先检索项目"
        sse = "\n".join([
            'data: {"choices":[{"delta":{"reasoning_content":"整理结果"}}]}',
            'data: {"choices":[{"delta":{"content":"当前暂无科研项目。"}}]}',
            'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":120,"completion_tokens":15}}',
            'data: [DONE]',
        ])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    with httpx.Client(transport=httpx.MockTransport(sse_and_retry)) as client:
        tid = submit_turn(store, settings, chat, "列出我的科研项目", client=client)

    saved = next(t for t in turns(store, chat) if t["id"] == tid)
    assert saved["status"] == "done" and "当前暂无科研项目" in saved["answer"]
    assert "先检索项目" in saved["reasoning"] and "整理结果" in saved["reasoning"]
    assert len(attempts) == 3


def test_compaction_v3_tools_and_research_callback(radar):
    store, topic = radar
    chat = new_conversation(store, topic.id)
    settings = Settings(
        data_dir=store.directory, llm_model="gpt-4o-mini", llm_key="fixture",
        thinking_level="off", stream_chat=False,
    )

    # Seed 6 completed turns to trigger compaction
    with store.transaction() as con:
        from research_radar.store import write
        for i in range(6):
            write(con, "INSERT INTO conversation_turns VALUES (:id,:chat,:p,:a,'done','[]','{}',:ts,NULL,NULL)",
                  id=f"t{i}", chat=chat, p=f"问题 {i}", a=f"回答 {i}", ts=f"2026-11-27T00:00:0{i}+00:00")
    ctx = {"topic": topic.id}
    hist = turns(store, chat)
    compact_history(hist, ctx, settings)
    assert ctx["compacted_count"] == 2 and "问题 0" in ctx["compacted_summary"]
    msgs = recent_messages(hist, ctx, "帮我建个课题并做研究")
    assert "compacted_summary" in msgs[-1]["content"]

    # Bilingual/relaxed V3 tool: create_project confirmation card -> confirm_action
    def handle_proj(request):
        body = json.loads(request.content)
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(200, json={"choices": [{"message": {"content": "请确认创建项目"}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": None,
            "tool_calls": [_call("propose_action", {
                "action": "create_project",
                "project_input": {
                    "name": "3D 点云异常定位",
                    "question": "如何提升少样本精度？",
                    "datasets": ["MVTec 3D-AD"],
                    "metrics": ["AUROC"],
                },
            })],
        }}]})

    with httpx.Client(transport=httpx.MockTransport(handle_proj)) as client:
        t_proj = submit_turn(store, settings, chat, "Create a project for 3D anomaly detection", client=client)
    card = actions(store, t_proj)[0]
    receipt = confirm_action(store, chat, card["id"])
    assert receipt["project_id"] and len(list_projects(store, topic.id)) == 1

    # Research job completion triggers automatic callback turn into conversation
    def sched(request):
        body = json.loads(request.content)
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(200, json={"choices": [{"message": {"content": "专题已提交"}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": None,
            "tool_calls": [_call("start_research", {"question": "point cloud anomaly"})],
        }}]})

    with httpx.Client(transport=httpx.MockTransport(sched)) as client:
        submit_turn(store, settings, chat, "帮我做个专题研究点云异常检测", client=client)
    job = research_status(store, chat)[0]

    def backend(request):
        body = json.loads(request.content)
        if "tools" in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"terms":["point cloud anomaly detection"]}'}}]})

    sources = FixtureSources([preprint(1, title="Point cloud anomaly detection")])
    for _ in range(5):
        with httpx.Client(transport=httpx.MockTransport(backend)) as client:
            tick_research(store, settings, client, sources)
        if research_status(store, chat, job["id"])[0]["status"] not in {"queued", "running"}:
            break
    all_turns = turns(store, chat)
    assert any(t["prompt"] == "[后台专题完成回传]" for t in all_turns)


def test_external_mcp_client_discovery_and_call(radar, tmp_path):
    store, topic = radar
    script = tmp_path / "echo_mcp.py"
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('Echo')\n"
        "@mcp.tool()\n"
        "def ping(msg: str) -> str:\n"
        "    return 'pong:' + msg\n"
        "mcp.run()\n",
        encoding="utf-8",
    )
    srv = add_mcp_server(store, "echo_srv", sys.executable, args=[str(script)])
    tools = discover_mcp_tools(srv, timeout=15.0)
    assert [t["name"] for t in tools] == ["ping"]
    out = call_external_mcp_tool(store, "echo_srv", "ping", {"msg": "radar"}, timeout=15.0)
    assert not out["isError"] and "pong:radar" in out["content"][0]


def test_citation_count_extraction_and_openalex_sort():
    oa_rec = parse_openalex({
        "id": "https://openalex.org/W424242",
        "title": "Industrial Vision Foundation Model",
        "publication_date": "2026-10-01",
        "type": "article",
        "cited_by_count": 37,
        "primary_location": {"version": "publishedVersion", "source": {"type": "conference", "display_name": "CVPR 2026"}},
        "locations": [],
    })
    assert oa_rec.citation_count == 37
    cr_rec = parse_crossref({
        "DOI": "10.1145/1234.5678",
        "type": "proceedings-article",
        "title": ["Smart Factory Reasoning"],
        "is-referenced-by-count": 19,
        "published": {"date-parts": [[2026, 9, 15]]},
        "container-title": ["Proceedings of KDD"],
    })
    assert cr_rec.citation_count == 19
    s2_rec = parse_semantic_scholar({
        "paperId": "s2abc",
        "title": "Industrial Agents",
        "citationCount": 64,
        "publicationDate": "2026-10-10",
        "venue": "NeurIPS",
    })
    assert s2_rec.citation_count == 64

    captured = {}

    def oa_transport(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"meta": {"next_cursor": None}, "results": []})

    topic = Topic(id="t1", name="Test", description="Test", keywords=["industrial intelligence"])
    with httpx.Client(transport=httpx.MockTransport(oa_transport)) as client:
        sources = Sources(client, "test-key")
        sources.page("openalex", topic, ["industrial intelligence"], date(2026, 9, 1), date(2026, 11, 1), None, 1e12,
                     sort="citations")
    assert captured["params"].get("sort") == "cited_by_count:desc"
    assert "cited_by_count" in captured["params"].get("select", "")


def test_search_library_venues_citations_fallback_and_no_topic_pollution(radar):
    store, topic = radar
    settings = Settings(data_dir=store.directory)
    records = [
        Record(
            source="openalex", external_id="W101", url="https://openalex.org/W101",
            title="High Citation Journal Survey", doi="10.1000/j1",
            authors=["A. Author"], published_on=DAY - timedelta(days=5),
            abstract="industrial point cloud anomaly survey",
            status="published", publication=Publication(status="published", venue="Scientific Reports"),
            citation_count=120,
        ),
        Record(
            source="openalex", external_id="W102", url="https://openalex.org/W102",
            title="Top Venue Low Cite Paper", doi="10.1000/c1",
            authors=["B. Author"], published_on=DAY - timedelta(days=2),
            abstract="industrial point cloud defect inspection",
            status="published", publication=Publication(status="published", venue="CVPR 2026"),
            citation_count=5,
        ),
        Record(
            source="openalex", external_id="W103", url="https://openalex.org/W103",
            title="Top Venue High Cite Paper", doi="10.1000/c2",
            authors=["C. Author"], published_on=DAY - timedelta(days=10),
            abstract="industrial point cloud representation",
            status="published", publication=Publication(status="published", venue="ICLR 2026"),
            citation_count=45,
        ),
    ]
    run_id = store.enqueue(topic, utc_now())
    run = store.one("SELECT * FROM runs WHERE id=:id", id=run_id)
    store.save_page(run, topic, "openalex", "main", records, None, DAY)
    chat = new_conversation(store)
    with httpx.Client() as client:
        conv = Conversation(store, settings, client, chat, "turn-1", "查询工业智能的顶会论文", {}, lambda *_: None)
        mem = execute_tool(_call("get_project_memory", {"topic": topic.id}), TOOLS, conv.dispatch)
        assert mem["status"] == "ok"
        assert conv.context.get("topic") is None

        lib_res = execute_tool(_call("search_library", {
            "topic": topic.id,
            "query": "industrial nonexistent",
            "venues": ["CVPR", "ICLR"],
            "sort_by": "citations",
        }), TOOLS, conv.dispatch)
        assert lib_res["status"] == "ok"
        data = lib_res["data"]
        assert data["partial_match"] is True
        assert [p["title"] for p in data["papers"]] == ["Top Venue High Cite Paper", "Top Venue Low Cite Paper"]
        assert [p["citation_count"] for p in data["papers"]] == [45, 5]
        assert conv.context.get("topic") is None


def test_search_external_papers_citations_venues_and_dedup(radar, monkeypatch):
    store, _ = radar
    settings = Settings(data_dir=store.directory, openalex_key="k", extra_sources="crossref")
    oa_records = [
        Record(
            source="openalex", external_id="W201", url="https://openalex.org/W201",
            title="Industrial Intelligence at NeurIPS", doi="10.1000/neurips1",
            authors=["X. Author"], published_on=DAY - timedelta(days=20),
            status="published", publication=Publication(status="published", venue="NeurIPS 2026"),
            citation_count=55,
        ),
        Record(
            source="openalex", external_id="W202", url="https://openalex.org/W202",
            title="Industrial Anomaly Preprint", doi="10.1000/pre1",
            authors=["Y. Author"], published_on=DAY - timedelta(days=10),
            status="preprint", publication=Publication(status="preprint", venue="arXiv"),
            citation_count=99,
        ),
        Record(
            source="openalex", external_id="W203", url="https://openalex.org/W203",
            title="Industrial Vision in CVPR", doi="10.1000/cvpr1", arxiv_id="2609.00011",
            authors=["Z. Author"], published_on=DAY - timedelta(days=30),
            status="published", publication=Publication(status="published", venue="CVPR 2026"),
            citation_count=12,
        ),
    ]
    ax_records = [
        Record(
            source="arxiv", external_id="2609.00011", url="https://arxiv.org/abs/2609.00011v1",
            title="Industrial Vision in CVPR", arxiv_id="2609.00011",
            abstract="Full arXiv abstract for CVPR paper.",
            authors=["Z. Author"], preprint_at=preprint(11, day=DAY - timedelta(days=30)).preprint_at,
            status="preprint", publication=Publication(status="preprint", venue="arXiv"),
        ),
        Record(
            source="arxiv", external_id="2610.00022", url="https://arxiv.org/abs/2610.00022v1",
            title="Pure Arxiv Preprint", arxiv_id="2610.00022",
            authors=["W. Author"], preprint_at=preprint(22, day=DAY - timedelta(days=5)).preprint_at,
            status="preprint", publication=Publication(status="preprint", venue="arXiv"),
        ),
    ]

    class FakeExternalSources:
        historical_sources = ("openalex", "crossref", "arxiv")

        def page(self, source, topic, terms, start, end, cursor, deadline, *, limit=100, newest=False, sort=None):
            if source == "crossref":
                raise SourceError("http_429")
            if source == "openalex":
                return Page(records=oa_records, cursor=None)
            return Page(records=ax_records, cursor=None)

    monkeypatch.setattr(Sources, "configured", classmethod(lambda cls, client, cfg: FakeExternalSources()))
    chat = new_conversation(store)
    with httpx.Client() as client:
        conv = Conversation(
            store, settings, client, chat, "turn-ext", "检索工业智能顶会论文按引用量排序",
            {"local_date": DAY.isoformat(), "timezone": "Asia/Shanghai"}, lambda *_: None,
        )
        out = execute_tool(_call("search_external_papers", {
            "terms": ["industrial intelligence"],
            "venues": ["CVPR", "NeurIPS"],
            "sort_by": "citations",
        }), TOOLS, conv.dispatch)
    assert out["status"] == "ok"
    data = out["data"]
    assert data["coverage"]["crossref"] == {"status": "error", "error": "http_429"}
    assert data["coverage"]["openalex"]["status"] == "ok"
    assert [p["title"] for p in data["papers"]] == ["Industrial Intelligence at NeurIPS", "Industrial Vision in CVPR"]
    assert [p["citation_count"] for p in data["papers"]] == [55, 12]
    assert "arXiv" in data["papers"][1]["source"] and "OpenAlex" in data["papers"][1]["source"]
    assert conv.context.get("topic") is None
    assert "papers" not in conv.context
    assert len(conv.context.get("external_papers", [])) == 2


def test_estimate_tokens_and_partial_match_min_hits(radar):
    store, topic = radar
    chinese_payload = {"content": "中文科研助手论文检索与对比分析" * 1000}
    raw_bytes = len(json.dumps(chinese_payload, ensure_ascii=False).encode("utf-8"))
    est = estimate_tokens(chinese_payload)
    assert est < raw_bytes * 0.55

    run_id = store.enqueue(topic, utc_now())
    run = store.one("SELECT * FROM runs WHERE id=:id", id=run_id)
    store.save_page(run, topic, "openalex", "main", [
        Record(
            source="openalex", external_id="W999", url="https://openalex.org/W999",
            title="Industrial Point Cloud Segmentation", doi="10.1000/seg999",
            authors=["A. Author"], published_on=DAY - timedelta(days=2),
            abstract="industrial surface segmentation method",
            status="published", publication=Publication(status="published", venue="CVPR 2026"),
        ),
    ], None, DAY)
    chat = new_conversation(store)
    settings = Settings(data_dir=store.directory)
    with httpx.Client() as client:
        conv = Conversation(store, settings, client, chat, "turn-tok", "了解 PGH-UNet retinal vessel segmentation", {}, lambda *_: None)
    out = execute_tool(_call("search_library", {
        "topic": topic.id,
        "query": "PGH-UNet retinal vessel segmentation",
    }), TOOLS, conv.dispatch)
    assert out["status"] == "ok"
    assert out["data"]["total"] == 0


def test_exploratory_pdf_discovery_redirects_unpaywall_and_ssrf():
    from research_radar.pdf_explorer import explore_pdf_sources, is_safe_public_https_url
    from test_assistant import fixture_pdf

    assert is_safe_public_https_url("https://127.0.0.1/secret.pdf") is None
    assert is_safe_public_https_url("https://localhost/secret.pdf") is None
    assert is_safe_public_https_url("http://arxiv.org/pdf/2601.00001") is None
    assert is_safe_public_https_url("https://openreview.net/pdf?id=abc123") == "https://openreview.net/pdf?id=abc123"

    pdf_bytes = fixture_pdf()

    def handler(request):
        u = str(request.url)
        if "api.openalex.org" in u:
            return httpx.Response(200, json={"doi": "https://doi.org/10.1000/pgh"})
        if "api.unpaywall.org" in u:
            return httpx.Response(200, json={"best_oa_location": {"url_for_pdf": "https://publisher.org/redirect?id=99"}})
        if u == "https://publisher.org/redirect?id=99":
            return httpx.Response(302, headers={"Location": "https://cdn.publisher.org/paper.pdf?token=abc"})
        if u == "https://cdn.publisher.org/paper.pdf?token=abc":
            return httpx.Response(200, content=pdf_bytes, headers={"Content-Type": "application/pdf"})
        return httpx.Response(404)

    settings = Settings(openalex_key="", crossref_mailto="dev@example.org")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res = explore_pdf_sources(client, settings, title="PGH-UNet", doi="10.1000/pgh")
    assert res["found"] is True
    assert res["strategy"] == "unpaywall"
    assert res["pdf_url"] == "https://cdn.publisher.org/paper.pdf?token=abc"
    assert "93.5" in res["pages"][1]


def test_explicit_pdf_gating_and_webpage_summary_default(radar, monkeypatch):
    store, topic = radar
    settings = Settings(data_dir=store.directory, llm_model="deepseek-chat", llm_key="k")
    run_id = store.enqueue(topic, utc_now())
    run = store.one("SELECT * FROM runs WHERE id=:id", id=run_id)
    store.save_page(run, topic, "openalex", "main", [
        Record(
            source="openalex", external_id="W777", url="https://journal.org/pgh-unet",
            title="PGH-UNet Retinal Vessel Segmentation", doi="10.1000/pgh777",
            authors=["A. Researcher"], published_on=DAY - timedelta(days=5),
            abstract="Adaptive feature integration for retinal vessel segmentation.",
            status="published", publication=Publication(status="published", venue="Medical Image Analysis"),
        ),
    ], None, DAY)

    called = {"pdf_explore": 0, "webpage_info": 0}

    def fake_webpage(client, cfg, **kwargs):
        called["webpage_info"] += 1
        return {
            "title": "PGH-UNet Retinal Vessel Segmentation",
            "authors": ["A. Researcher"],
            "publication": "已发表 · Medical Image Analysis",
            "citation_count": 12,
            "doi": "10.1000/pgh777",
            "arxiv_id": None,
            "url": "https://journal.org/pgh-unet",
            "abstract": "Adaptive feature integration for retinal vessel segmentation.",
            "webpage_excerpt": "Method details on webpage.",
            "sources_used": ["local_record", "webpage"],
            "fetched_pdf": False,
        }

    def fake_explore(client, cfg, **kwargs):
        called["pdf_explore"] += 1
        return {
            "found": True,
            "pdf_url": "https://cdn.journal.org/pgh-unet.pdf",
            "strategy": "landing_page",
            "pages": ["Abstract PGH-UNet", "Method adaptive fusion"],
            "page_count": 2,
            "content_hash": "hashpgh",
            "attempts": [{"strategy": "landing_page", "url": "https://cdn.journal.org/pgh-unet.pdf", "status": "ok"}],
        }

    monkeypatch.setattr("research_radar.conversation.fetch_paper_webpage_info", fake_webpage)
    monkeypatch.setattr("research_radar.deep_read.fetch_paper_webpage_info", fake_webpage)
    monkeypatch.setattr("research_radar.conversation.explore_pdf_sources", fake_explore)
    monkeypatch.setattr("research_radar.rag.index_paper_pdf", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not fetch PDF")))

    chat = new_conversation(store)
    llm_resp = {"choices": [{"message": {"content": json.dumps({
        "level": "L2", "summary": "基于摘要和网页的总结。", "evidence_ids": ["E0"],
    })}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=llm_resp))) as client:
        # 1. Without explicit PDF request: fetch_paper_pdf blocked, deep_read downgrades fetch_pdf, webpage summary works
        conv_no_pdf = Conversation(store, settings, client, chat, "t-nopdf", "介绍一下 PGH-UNet 讲了什么", {}, lambda *_: None)
        sum_out = execute_tool(_call("fetch_paper_summary", {"paper": f"{topic.id}:1"}), TOOLS, conv_no_pdf.dispatch)
        assert sum_out["status"] == "ok" and sum_out["data"]["fetched_pdf"] is False

        denied_pdf = execute_tool(_call("fetch_paper_pdf", {"paper": f"{topic.id}:1"}), TOOLS, conv_no_pdf.dispatch)
        assert denied_pdf["status"] == "error"
        assert called["pdf_explore"] == 0

        deep_out = execute_tool(_call("deep_read_paper", {"paper": f"{topic.id}:1", "level": "L3", "fetch_pdf": True}), TOOLS, conv_no_pdf.dispatch)
        assert deep_out["status"] == "ok"
        assert "pdf_skipped_reason" in deep_out["data"]
        assert called["pdf_explore"] == 0

        # 2. With explicit PDF request: fetch_paper_pdf runs exploratory discovery and parses without saving PDF file
        conv_pdf = Conversation(store, settings, client, chat, "t-pdf", "请获取 PGH-UNet 的原始 PDF 并提取方法", {}, lambda *_: None)
        pdf_out = execute_tool(_call("fetch_paper_pdf", {"title": "PGH-UNet", "query": "method"}), TOOLS, conv_pdf.dispatch)
        assert pdf_out["status"] == "ok"
        assert pdf_out["data"]["found"] is True and pdf_out["data"]["stored_raw_pdf"] is False
        assert called["pdf_explore"] == 1


def test_explore_pdf_uses_preferred_cached_url_and_mlr_mirror(monkeypatch):
    from research_radar.pdf_explorer import explore_pdf_sources

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen_urls.append(url)
        if url.startswith("https://raw.githubusercontent.com/mlresearch/"):
            raise httpx.ConnectError("github raw connection reset", request=request)
        if url == "https://proceedings.mlr.press/v202/shao26b/shao26b.pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.5\n%fake\n")
        return httpx.Response(404)

    settings = Settings()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res = explore_pdf_sources(
            client, settings,
            title="Test PMLR Paper",
            preferred_urls=["https://raw.githubusercontent.com/mlresearch/v202/main/assets/shao26b/shao26b.pdf"],
            return_bytes=True,
        )
    assert res["found"] is True and res["strategy"] == "cached"
    assert res["pdf_url"] == "https://proceedings.mlr.press/v202/shao26b/shao26b.pdf"
    assert seen_urls == [
        "https://raw.githubusercontent.com/mlresearch/v202/main/assets/shao26b/shao26b.pdf",
        "https://proceedings.mlr.press/v202/shao26b/shao26b.pdf",
    ]
