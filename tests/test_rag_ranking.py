import json
import time

import httpx

from research_radar.deep_read import deep_read_paper, verify_deep_analysis
from research_radar.feedback import hidden_paper_ids, record_feedback, reset_preference, set_preference_frozen, topic_preference_profile
from research_radar.models import Settings
from research_radar.rag import hybrid_search, index_chunk_records, rebuild_rag_index
from research_radar.ranking import Ranker, mmr_select
from test_radar import DAY, FixtureSources, NOW, preprint, radar, run_daily, worker


def test_feedback_freeze_reset_hide_and_mmr_diversity(radar):
    store, topic = radar
    papers = [
        preprint(1, title="Point cloud anomaly detection A", abstract="point cloud anomaly detection on MVTec 3D-AD"),
        preprint(2, title="Point cloud anomaly detection B", abstract="point cloud anomaly detection on MVTec 3D-AD variant"),
        preprint(3, title="Point cloud anomaly detection C", abstract="point cloud anomaly detection industrial inspection"),
        preprint(4, title="Point cloud anomaly detection D", abstract="point cloud anomaly detection geometric prior"),
        preprint(5, title="Point cloud anomaly detection E", abstract="point cloud anomaly detection surface reconstruction"),
        preprint(6, title="Point cloud anomaly detection F", abstract="point cloud anomaly detection zero-shot benchmark"),
    ]
    job = worker(store, FixtureSources(papers))
    run_daily(store, topic, job)
    stored = store.papers(topic.id)
    assert len(stored) == 6

    settings = Settings(data_dir=store.directory, keyword_only=True)
    res = record_feedback(store, topic.id, stored[0]["paper_id"], "like", settings=settings)
    assert res["profile"]["positive_count"] == 1

    # Freeze stops updates
    set_preference_frozen(store, topic.id, True)
    record_feedback(store, topic.id, stored[1]["paper_id"], "ignore", settings=settings)
    assert topic_preference_profile(store, topic.id)["negative_count"] == 0

    # Hide removes from Ranker.rank
    record_feedback(store, topic.id, stored[2]["paper_id"], "hide", settings=settings)
    assert stored[2]["paper_id"] in hidden_paper_ids(store, topic.id)
    ranked, _ = Ranker(store, settings).rank(topic, stored, DAY)
    assert all(item["paper_id"] != stored[2]["paper_id"] for item in ranked)

    # MMR selects 5 items with 1 exploratory slot when >= 6 candidates exist
    expanded = [*ranked, {**ranked[0], "paper_id": 999}]
    selected = mmr_select(expanded, k=5, reserve_exploratory=True)
    assert len(selected) == 5 and any(item.get("exploratory") for item in selected)

    reset_preference(store, topic.id)
    assert topic_preference_profile(store, topic.id)["positive_count"] == 0


def test_rag_chunking_hybrid_search_and_l3_verification(radar):
    store, topic = radar
    job = worker(store, FixtureSources([preprint(1, title="Geometric 3D Anomaly Paper")]))
    run_daily(store, topic, job)
    paper = store.papers(topic.id)[0]
    settings = Settings(data_dir=store.directory, keyword_only=True, llm_key="mock", llm_model="fixture")

    pages = [
        "Abstract We study 3D anomaly detection. Introduction Point clouds are noisy.",
        "Method We use voxel memory banks. Experiments Evaluation on MVTec 3D-AD split A gives 94.2 AUROC.",
        "Limitations Sensitive to heavy occlusion.",
    ]
    idx_res = index_chunk_records(store, settings, paper["paper_id"], "https://arxiv.org/pdf/2611.00001", pages, "hash123")
    assert idx_res["chunks"] >= 1 and not idx_res["cached"]
    assert index_chunk_records(store, settings, paper["paper_id"], "https://arxiv.org/pdf/2611.00001", pages, "hash123")["cached"]

    hits = hybrid_search(store, settings, "MVTec 94.2 AUROC", topic_id=topic.id, limit=5)
    assert hits and "94.2" in hits[0]["text"] and "#page=" in hits[0]["url"]

    assert rebuild_rag_index(store, settings)["chunks_reindexed"] >= 1

    # Verify numeric grounding check rejects unverified numbers
    ev_map = {"E1": {"id": "E1", "text": pages[1], "page": 2, "url": "https://arxiv.org/pdf/2611.00001#page=2"}}
    bad_payload = json.dumps({
        "level": "L3", "summary": "总结",
        "metrics": [
            {"dataset": "MVTec 3D-AD", "split": "A", "metric": "AUROC", "protocol": "default", "value": "94.2", "evidence_id": "E1"},
            {"dataset": "MVTec 3D-AD", "split": "A", "metric": "AUROC", "protocol": "default", "value": "99.9", "evidence_id": "E1"},
        ],
        "evidence_ids": ["E1"],
    })
    cleaned, _ = verify_deep_analysis(bad_payload, ev_map)
    assert [m["value"] for m in cleaned["metrics"]] == ["94.2"]

    def mock_llm(_request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "level": "L3",
            "summary": "在 MVTec 3D-AD 上取得 94.2 AUROC。",
            "problem": "点云异常定位",
            "method": "体素记忆库",
            "datasets": ["MVTec 3D-AD"],
            "training_protocol": "原文未确认",
            "baselines": ["BTF"],
            "metrics": [{"dataset": "MVTec 3D-AD", "split": "A", "metric": "AUROC", "protocol": "standard", "value": "94.2", "evidence_id": "E1"}],
            "code_urls": [],
            "limitations": "对严重遮挡敏感",
            "agent_inferences": ["与主题高度相关"],
            "evidence_ids": ["E1"],
        })}}]})

    with httpx.Client(transport=httpx.MockTransport(mock_llm)) as client:
        first = deep_read_paper(store, settings, client, topic, paper["paper_id"], paper["records"], level="L3",
                                deadline=time.monotonic() + 10)
        second = deep_read_paper(store, settings, client, topic, paper["paper_id"], paper["records"], level="L3",
                                 deadline=time.monotonic() + 10)
    assert first["analysis"]["datasets"] == ["MVTec 3D-AD"] and not first["cached"]
    assert second["cached"]

    # Normalizes DeepSeek wrapper keys ("type": "json_object"), null/str lists, list limitations, and null metric values
    quirky_payload = "```json\n" + json.dumps({
        "type": "json_object",
        "level": "L2",
        "summary": "提出基于特征对齐的异常检测方法。",
        "problem": None,
        "method": "特征对齐",
        "datasets": "MVTec 3D-AD",
        "training_protocol": None,
        "baselines": None,
        "metrics": [
            {"dataset": "MVTec 3D-AD", "split": None, "metric": "AUROC", "protocol": None, "value": None, "evidence_id": "E1"},
            {"dataset": "MVTec 3D-AD", "split": None, "metric": "AUROC", "protocol": None, "value": "94.2", "evidence_id": "E1"},
        ],
        "code_urls": None,
        "limitations": ["仅评估部分类别", "依赖高分辨率点云"],
        "agent_inferences": "可迁移至工业检测",
        "evidence_ids": ["E1"],
    }) + "\n```"
    norm_cleaned, _ = verify_deep_analysis(quirky_payload, ev_map)
    assert norm_cleaned["datasets"] == ["MVTec 3D-AD"]
    assert norm_cleaned["baselines"] == []
    assert "仅评估部分类别" in (norm_cleaned["limitations"] or "")
    assert [m["value"] for m in norm_cleaned["metrics"]] == ["94.2"]
    assert norm_cleaned["metrics"][0]["split"] == "原文未确认"
