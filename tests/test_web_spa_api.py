import json
from pathlib import Path

from fastapi.testclient import TestClient

from research_radar.api import create_app
from research_radar.models import Settings
from test_radar import FixtureSources, preprint, radar, run_daily, worker


def test_fastapi_loopback_auth_and_remote_guard(radar):
    store, _ = radar
    local_app = create_app(store, Settings(data_dir=store.directory, api_host="127.0.0.1", api_token=""))
    loopback_client = TestClient(local_app, client=("127.0.0.1", 51234))
    assert loopback_client.get("/api/auth/status").json()["loopback_bypass"] is True
    assert loopback_client.get("/api/topics").status_code == 200

    remote_app = create_app(store, Settings(data_dir=store.directory, api_host="0.0.0.0", api_token=""))
    remote_client = TestClient(remote_app, client=("10.0.0.5", 51234))
    assert remote_client.get("/api/topics").status_code == 401


def test_fastapi_workbench_settings_conversations_and_spa(radar, tmp_path):
    store, topic = radar
    run_daily(store, topic, worker(store, FixtureSources([preprint(101)])))
    paper_id = store.papers(topic.id)[0]["paper_id"]

    env_file = tmp_path / ".env"
    env_file.write_text("RADAR_LLM_MODEL=old-model\n", encoding="utf-8")

    fake_dist = tmp_path / "dist"
    (fake_dist / "assets").mkdir(parents=True)
    (fake_dist / "index.html").write_text("<!doctype html><html><body>SPA OK</body></html>", encoding="utf-8")

    app = create_app(
        store,
        Settings(data_dir=store.directory, api_host="127.0.0.1", api_token=""),
        env_path=env_file,
        web_dist=fake_dist,
    )
    client = TestClient(app, client=("127.0.0.1", 51234))

    # Overview, papers, reading state, memory
    ov = client.get(f"/api/topics/{topic.id}/overview")
    assert ov.status_code == 200 and ov.json()["topic"]["id"] == topic.id

    papers = client.get(f"/api/papers?topic_id={topic.id}")
    assert papers.status_code == 200 and papers.json()["total"] == 1

    st = client.patch(f"/api/papers/{paper_id}/state", json={"topic_id": topic.id, "favorite": True, "is_read": True})
    assert st.status_code == 200 and st.json()["favorite"] is True and st.json()["is_read"] is True

    mem_put = client.put(
        f"/api/topics/{topic.id}/memory",
        json={"revision": 0, "venues": ["CVPR"], "excluded": ["medical"], "datasets": ["MVTec 3D-AD"], "research_protocol": "AUROC", "main_only": True},
    )
    assert mem_put.status_code == 200 and mem_put.json()["revision"] == 1

    # Conversations: empty chats stay hidden until they have at least one turn
    from research_radar.conversation import queue_turn

    conv = client.post("/api/conversations", json={"topic_id": topic.id})
    assert conv.status_code == 200
    chat_id = conv.json()["id"]
    assert all(c["id"] != chat_id for c in client.get("/api/conversations").json()["conversations"])
    assert client.get(f"/api/conversations/{chat_id}/turns").status_code == 200
    queue_turn(store, Settings(data_dir=store.directory), chat_id, "测试首条消息")
    listed = client.get("/api/conversations").json()["conversations"]
    assert any(c["id"] == chat_id for c in listed)

    # Settings & Env allowlist
    diag = client.get("/api/settings/diagnostics")
    assert diag.status_code == 200 and "embedding" in diag.json() and "secrets" in diag.json()
    assert client.put("/api/settings/env", json={"updates": {"RADAR_LLM_MODEL": "new-model"}}).status_code == 200
    assert "RADAR_LLM_MODEL=new-model" in env_file.read_text(encoding="utf-8")
    assert client.put("/api/settings/env", json={"updates": {"PATH": "/bad"}}).status_code == 400

    # MCP CRUD
    mcp_add = client.post("/api/mcp-servers", json={"name": "local_test", "command": "python", "args": ["-V"]})
    assert mcp_add.status_code == 200
    srv_id = mcp_add.json()["id"]
    assert client.patch(f"/api/mcp-servers/{srv_id}", json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/mcp-servers/{srv_id}").status_code == 200

    # SPA fallback & API 404 separation
    spa_res = client.get("/workbench")
    assert spa_res.status_code == 200 and "SPA OK" in spa_res.text
    assert client.get("/api/does-not-exist").status_code == 404


def test_pdf_precheck_marks_available_and_unavailable_and_filters(radar, monkeypatch):
    from datetime import timedelta
    from research_radar.models import Publication, Record
    from research_radar.pdf_explorer import precheck_library_pdfs
    from test_radar import DAY

    store, topic = radar
    run_daily(store, topic, worker(store, FixtureSources([preprint(201)])))
    # Add a second non-OA closed paper
    run_id = store.enqueue(topic, store.topics()[0] and __import__("research_radar.models").models.utc_now())
    run = store.one("SELECT * FROM runs WHERE id=:id", id=run_id)
    store.save_page(run, topic, "openalex", "main", [
        Record(
            source="openalex", external_id="W888", url="https://closed-publisher.org/paper888",
            title="Closed Journal Paper", doi="10.1000/closed888",
            authors=["A. Author"], published_on=DAY - timedelta(days=1),
            status="published", publication=Publication(status="published", venue="Closed Journal"),
        ),
    ], None, DAY)

    def fake_probe(client, settings, *, title="", records=(), **kwargs):
        if any(r.arxiv_id for r in records):
            return {"status": "available", "pdf_url": "https://arxiv.org/pdf/2611.00201", "strategy": "direct", "reason": None}
        return {"status": "unavailable", "pdf_url": None, "strategy": None, "reason": "HTTP 403"}

    monkeypatch.setattr("research_radar.pdf_explorer.probe_pdf_availability", fake_probe)
    settings = Settings(data_dir=store.directory, api_host="127.0.0.1", api_token="")
    app = create_app(store, settings)
    client = TestClient(app, client=("127.0.0.1", 51234))

    res = client.post("/api/papers/precheck", json={"topic_id": topic.id, "limit": 50})
    assert res.status_code == 200
    assert res.json()["available"] == 1 and res.json()["unavailable"] == 1

    # Re-running without force checks 0 new papers
    res2 = precheck_library_pdfs(store, settings, topic.id)
    assert res2["checked"] == 0

    all_p = client.get(f"/api/papers?topic_id={topic.id}").json()
    assert all_p["pdf_counts"]["available"] == 1 and all_p["pdf_counts"]["unavailable"] == 1

    only_avail = client.get(f"/api/papers?topic_id={topic.id}&pdf_available=true").json()
    assert only_avail["total"] == 1 and only_avail["papers"][0]["pdf_status"] == "available"


def test_cached_pdf_fast_path_preserves_available_on_transient_failure_and_abstract_note(radar, monkeypatch):
    from fastapi.testclient import TestClient

    from research_radar.api import create_app
    from research_radar.models import Publication, Record, Settings
    from research_radar.pdf_explorer import save_pdf_check
    from test_radar import FixtureSources, preprint, run_daily, worker

    store, topic = radar
    run_daily(store, topic, worker(store, FixtureSources([preprint(301)])))
    paper_id = store.papers(topic.id)[0]["paper_id"]
    cached_url = "https://proceedings.mlr.press/v202/test301/test301.pdf"
    save_pdf_check(store, paper_id, "available", pdf_url=cached_url, strategy="landing_page")

    seen_preferred = []

    def fake_explore_transient(client, cfg, **kwargs):
        seen_preferred.append(kwargs.get("preferred_urls"))
        return {
            "found": False,
            "attempts": [{"strategy": "cached", "url": cached_url, "status": "failed", "reason": "网络请求失败(ConnectError)"}],
            "transient": True,
            "reason": "网络请求失败(ConnectError)",
        }

    monkeypatch.setattr("research_radar.pdf_explorer.explore_pdf_sources", fake_explore_transient)
    settings = Settings(
        data_dir=store.directory, api_host="127.0.0.1", api_token="",
        llm_model="gpt-4o-mini", llm_key="fixture",
    )
    app = create_app(store, settings)
    client = TestClient(app, client=("127.0.0.1", 51234))

    pdf_res = client.get(f"/api/papers/{paper_id}/pdf?topic_id={topic.id}")
    assert pdf_res.status_code == 404
    assert seen_preferred and cached_url in (seen_preferred[0] or [])
    # Transient ConnectError must NOT flip pre-checked 'available' back to 'unavailable'
    after_row = store.one("SELECT status, pdf_url FROM paper_pdf_checks WHERE paper_id=:pid", pid=paper_id)
    assert after_row["status"] == "available" and after_row["pdf_url"] == cached_url

    # Permanent 404 (transient=False) DOES update status to unavailable
    def fake_explore_permanent(client, cfg, **kwargs):
        return {"found": False, "attempts": [], "transient": False, "reason": "无公开 PDF"}

    monkeypatch.setattr("research_radar.pdf_explorer.explore_pdf_sources", fake_explore_permanent)
    pdf_res2 = client.get(f"/api/papers/{paper_id}/pdf?topic_id={topic.id}")
    assert pdf_res2.status_code == 404
    assert store.one("SELECT status FROM paper_pdf_checks WHERE paper_id=:pid", pid=paper_id)["status"] == "unavailable"

    # Abstract translation + LLM summary generation and caching (no PDF download required)
    llm_calls = []

    def fake_completion(http_client, cfg, messages, **kwargs):
        llm_calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps({
            "translation": "本文提出了一种用于视觉异常检测的跨模态特征融合框架。",
            "summary": "针对工业场景小样本异常检测问题，本文设计了跨模态特征对齐模块，在标准基准上显著提升了检出率。",
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr("research_radar.deep_read.request_json_completion", fake_completion)
    note_res = client.post(f"/api/papers/{paper_id}/abstract-note", json={"topic_id": topic.id})
    assert note_res.status_code == 200
    note_data = note_res.json()
    assert note_data["available"] is True and note_data["cached"] is False
    assert "跨模态特征融合" in note_data["translation"] and "小样本异常检测" in note_data["summary"]

    # Second call uses persisted cache; paper detail includes abstract_note
    note_cached = client.post(f"/api/papers/{paper_id}/abstract-note", json={"topic_id": topic.id}).json()
    assert note_cached["cached"] is True and len(llm_calls) == 1
    detail = client.get(f"/api/papers/{paper_id}?topic_id={topic.id}").json()
    assert detail["abstract_note"] and "跨模态特征融合" in detail["abstract_note"]["translation"]


def test_trends_insights_zero_llm_and_synthesis_endpoint(radar, monkeypatch):
    from fastapi.testclient import TestClient

    from research_radar.api import create_app
    from research_radar.models import Settings
    from test_radar import FixtureSources, preprint, run_daily, worker

    store, topic = radar
    no_sub_topic = topic.model_copy(update={"subtopics": {}})
    store.save_topic(no_sub_topic, store.topic(topic.id)[1])
    run_daily(store, no_sub_topic, worker(store, FixtureSources([
        preprint(401, title="Voxel memory 3D anomaly", abstract="voxel memory bank benchmark MVTec-3D"),
        preprint(402, title="Voxel memory industrial defect", abstract="voxel memory bank defect localization"),
    ])))

    # Ensure GET /api/topics/{id}/trends NEVER triggers LLM calls
    monkeypatch.setattr(
        "research_radar.period_reports.request_json_completion",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("GET trends must not call LLM")),
    )
    settings = Settings(
        data_dir=store.directory, api_host="127.0.0.1", api_token="",
        llm_model="gpt-4o-mini", llm_key="fixture",
    )
    app = create_app(store, settings)
    client = TestClient(app, client=("127.0.0.1", 51234))
    res = client.get(f"/api/topics/{no_sub_topic.id}/trends?window_days=30")
    assert res.status_code == 200
    body = res.json()
    assert body["insights"]["total"] == 2
    assert body["latest_synthesis"] is None
    assert body["live_trends"]["30"]["bars"]

    # Explicit POST /synthesis calls LLM once and persists
    monkeypatch.setattr(
        "research_radar.period_reports.request_json_completion",
        lambda *a, **kw: {"choices": [{"message": {"content": json.dumps({
            "headline": "聚焦体素记忆库的三维异常定位",
            "overview": "当前集中于体素特征匹配方法。",
            "directions": [{"name": "体素记忆匹配", "summary": "利用局部记忆特征检测缺陷", "key_methods": ["Voxel Memory"], "challenges": None, "paper_ids": []}],
            "datasets_and_eval": "MVTec-3D",
            "research_advice": ["探索跨类别迁移"],
        }, ensure_ascii=False)}}]},
    )
    syn = client.post(f"/api/topics/{no_sub_topic.id}/synthesis", json={"window_days": 30}).json()
    assert syn["available"] is True and syn["synthesis"]["headline"].startswith("聚焦")
