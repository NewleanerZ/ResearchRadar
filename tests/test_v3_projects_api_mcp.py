import asyncio
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from research_radar.api import create_app
from research_radar.gap_workflow import run_gap_analysis
from research_radar.mcp_server import create_mcp_server
from research_radar.models import Settings, dump, utc_now
from research_radar.projects import ProjectInput, create_project, link_project_papers, protocol_comparison_matrix, update_project
from research_radar.zotero import ZOTERO_API, pull_zotero_collection, push_favorites_to_zotero
from test_radar import FixtureSources, preprint, radar, run_daily, worker


def test_projects_protocol_matrix_and_gap_reverse_search(radar):
    store, topic = radar
    job = worker(store, FixtureSources([
        preprint(1, title="PointNet-AD for MVTec 3D-AD", abstract="We evaluate 3D point cloud anomaly detection on MVTec 3D-AD."),
        preprint(2, title="Reg3D-AD baseline", abstract="Geometric prior for industrial point cloud inspection."),
    ]))
    run_daily(store, topic, job)
    papers = store.papers(topic.id)

    settings = Settings(data_dir=store.directory, keyword_only=True, llm_key="mock", llm_model="fixture")
    proj = create_project(store, topic.id, ProjectInput(
        name="工业点云几何异常检测",
        question="如何在少样本下提升 MVTec 3D-AD 的像素级定位精度？",
        current_method="基于局部几何描述符的记忆库方法",
        datasets=["MVTec 3D-AD"],
        metrics=["AUROC", "PRO"],
    ))
    updated = update_project(store, proj["id"], 1, ProjectInput(
        name=proj["name"], question=proj["question"], current_method=proj["current_method"],
        datasets=["MVTec 3D-AD", "Real3D-AD"], metrics=["AUROC"],
    ))
    assert updated["revision"] == 2
    with pytest.raises(ValueError, match="已更新"):
        update_project(store, proj["id"], 1, ProjectInput(name="X", question="Y"))

    links = link_project_papers(store, settings, proj["id"], min_score=0.1)
    assert links

    # Seed two deep analyses with DIFFERENT protocols/splits and confirm they never mix
    now = utc_now().isoformat()
    with store.transaction() as con:
        from research_radar.store import write
        for idx, split in enumerate(("split-A", "split-B")):
            pid = papers[idx]["paper_id"]
            write(con, "INSERT INTO deep_analyses VALUES (:k, :p, :t, 'L3', :r, :e, :n)",
                  k=f"k{idx}", p=pid, t=topic.id,
                  r=dump({"level": "L3", "summary": "ok",
                          "metrics": [{"dataset": "MVTec 3D-AD", "split": split, "metric": "AUROC",
                                       "protocol": "standard", "value": f"9{idx}.5", "evidence_id": "E1"}]}),
                  e=dump([{"id": "E1", "page": 2, "url": "https://arxiv.org/pdf/test#page=2"}]),
                  n=now)

    matrix = protocol_comparison_matrix(store, proj["id"])
    assert len(matrix) == 2 and {g["split"] for g in matrix} == {"split-A", "split-B"}

    # Gap analysis with reverse search counter-examples
    def llm_mock(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "candidates": [{
                "title": "Few-shot 3D point cloud anomaly detection",
                "rationale": "Existing works require full normal sets.",
                "search_terms": ["point cloud anomaly detection"],
                "supporting_paper_ids": [papers[0]["paper_id"]],
            }]
        })}}]})

    ext_sources = FixtureSources([
        preprint(50, title="Few-shot point cloud anomaly detection"),
        preprint(51, title="Zero-shot point cloud anomaly detection"),
    ])
    with httpx.Client(transport=httpx.MockTransport(llm_mock)) as client:
        gap = run_gap_analysis(store, settings, topic.id, project_id=proj["id"], client=client, sources=ext_sources)
    assert gap["status"] == "succeeded" and "存在强相关先例" in gap["markdown"]


def test_zotero_sync_fastapi_auth_and_mcp(radar):
    store, topic = radar
    settings = Settings(
        data_dir=store.directory, keyword_only=True,
        zotero_api_key="zot-secret", zotero_user_id="123456", api_token="radar-secret",
    )

    def zotero_mock(request):
        assert request.headers["Zotero-API-Key"] == "zot-secret"
        if request.method == "GET":
            return httpx.Response(200, headers={"Last-Modified-Version": "42"}, json=[{
                "key": "ZITEM1", "version": 42,
                "data": {"itemType": "journalArticle", "title": "Zotero Imported 3D Paper",
                         "DOI": "10.1000/zot.1", "date": "2026-06-01",
                         "creators": [{"firstName": "Ada", "lastName": "Lee"}]},
            }])
        return httpx.Response(200, json={"successful": {"0": {"key": "ZNEW1", "version": 43}}})

    with httpx.Client(transport=httpx.MockTransport(zotero_mock)) as client:
        pulled = pull_zotero_collection(store, settings, topic.id, client=client)
        assert pulled == {"imported": 1, "version": 42, "unchanged": False}
        # Add another favorite not from zotero and push
        run_daily(store, topic, worker(store, FixtureSources([preprint(9)])))
        other_pid = max(p["paper_id"] for p in store.papers(topic.id))
        store.set_reading_state(topic.id, other_pid, True, False)
        pushed = push_favorites_to_zotero(store, settings, topic.id, client=client)
        assert pushed == {"exported": 1}

    # FastAPI authentication & endpoints
    app = create_app(store, settings)
    api_client = TestClient(app)
    assert api_client.get("/health").status_code == 200
    assert api_client.get("/api/topics").status_code == 401
    auth = {"Authorization": "Bearer radar-secret"}
    assert api_client.get("/api/topics", headers=auth).status_code == 200
    assert api_client.get(f"/api/papers?topic_id={topic.id}", headers=auth).json()["total"] == 2

    # MCP server read tools
    mcp = create_mcp_server(store, settings)
    tools = mcp._tool_manager.list_tools()
    tool_names = {t.name for t in tools}
    assert {"search_papers", "hybrid_rag_search", "get_trends", "compare_methods"}.issubset(tool_names)
    res = asyncio.run(mcp._tool_manager.call_tool("search_papers", {"topic_id": topic.id, "query": "Zotero"}))
    assert "Zotero" in str(res)


def test_configuration_persistence_and_embedding_diagnostics(radar, monkeypatch, tmp_path):
    from research_radar.configuration import embedding_diagnostics, mask_secret, save_env_updates

    monkeypatch.delenv("RADAR_LLM_MODEL", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nRADAR_LLM_MODEL=old\n", encoding="utf-8")
    save_env_updates(env_file, {"RADAR_LLM_MODEL": "deepseek-flash", "S2_API_KEY": "sec-123456"})
    saved_text = env_file.read_text(encoding="utf-8")
    assert "RADAR_LLM_MODEL=deepseek-flash" in saved_text and "S2_API_KEY=sec-123456" in saved_text
    assert mask_secret("sec-123456") == "已配置（••••3456）"
    save_env_updates(env_file, {}, clear_keys={"S2_API_KEY"})
    assert "S2_API_KEY" not in env_file.read_text(encoding="utf-8")

    store, _ = radar
    diag = embedding_diagnostics(Settings(data_dir=store.directory))
    assert not diag["ready"] and diag["reasons"]
