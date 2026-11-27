from datetime import timedelta
import numpy as np

from research_radar.clustering import discover_clusters, latest_dynamic_clusters
from research_radar.models import Settings, dump
from research_radar.period_reports import generate_period_report, period_report_history
from test_radar import DAY, FixtureSources, NOW, preprint, radar, run_daily, worker


def test_dynamic_clustering_and_weekly_monthly_reports(radar, monkeypatch):
    store, topic = radar
    papers = []
    for i in range(1, 19):
        if i <= 9:
            title = f"Voxel feature memory bank for 3D anomaly {i}"
            abs_text = "voxel memory bank point cloud feature matching industrial surface"
        else:
            title = f"Diffusion geometric prior defect localization {i}"
            abs_text = "diffusion model geometric prior defect segmentation benchmark"
        papers.append(preprint(i, day=DAY - timedelta(days=(i % 2) + 1), title=title, abstract=abs_text))

    job = worker(store, FixtureSources(papers))
    run_daily(store, topic, job)
    settings = Settings(data_dir=store.directory, keyword_only=False)

    # Low sample fallback
    low = discover_clusters(store, settings, topic, DAY, min_papers=30)
    assert low["mode"] == "fallback_fixed"

    # Deterministic embedding mock for scikit-learn clustering
    def fake_encode(_self, texts):
        vecs = []
        for t in texts:
            if "voxel" in t.lower():
                vec = np.array([1.0, 0.05, 0.0, 0.0])
            else:
                vec = np.array([0.05, 1.0, 0.0, 0.0])
            vecs.append((vec / np.linalg.norm(vec)).tolist())
        return vecs

    monkeypatch.setattr("research_radar.clustering.Ranker.encode_texts", fake_encode)

    # First snapshot -> emerging clusters
    snap1 = discover_clusters(store, settings, topic, DAY, window_days=30, min_papers=15)
    assert snap1["mode"] == "dynamic" and len(snap1["clusters"]) >= 2
    assert all(c["event_type"] == "emerging" for c in snap1["clusters"])

    # Second snapshot -> matches prior snapshot centroids
    snap2 = discover_clusters(store, settings, topic, DAY + timedelta(days=1), window_days=30, min_papers=15)
    assert snap2["mode"] == "dynamic" and latest_dynamic_clusters(store, topic.id)["clusters"]
    assert any(c["event_type"] != "emerging" for c in snap2["clusters"])

    # Weekly & monthly reports idempotent
    w1 = generate_period_report(store, settings, topic, DAY, "weekly")
    w2 = generate_period_report(store, settings, topic, DAY, "weekly")
    m1 = generate_period_report(store, settings, topic, DAY, "monthly")
    assert w1["id"] == w2["id"] and "科研周报" in w1["markdown"]
    assert "科研月报" in m1["markdown"]
    assert "固定子主题分布" in w1["markdown"]
    assert len(period_report_history(store, topic.id)) == 2


def test_weekly_low_sample_renders_fixed_subtopic_table_and_events(radar):
    store, topic = radar
    papers = [
        preprint(i, day=DAY - timedelta(days=1), title=f"Point cloud anomaly detection {i}",
                 abstract="point cloud anomaly detection https://github.com/org/pcad")
        for i in range(1, 5)
    ]
    job = worker(store, FixtureSources(papers))
    run_daily(store, topic, job)
    settings = Settings(data_dir=store.directory, keyword_only=True)
    report = generate_period_report(store, settings, topic, DAY, "weekly", force=True)
    assert "固定子主题分布" in report["markdown"]
    assert "点云" in report["markdown"]
    assert "科研事件汇总" in report["markdown"]
    assert "代码开源" in report["markdown"]


def test_balanced_cluster_subdivision_insights_and_on_demand_synthesis(radar, monkeypatch):
    import json
    import httpx
    from research_radar.clustering import enrich_clusters_with_papers
    from research_radar.period_reports import build_topic_insights, synthesize_topic_landscape

    store, topic = radar
    papers = []
    # 30 papers all close together initially, plus distinct directions inside that group
    for i in range(1, 31):
        sub = i % 3
        if sub == 0:
            title = f"Memory bank voxel anomaly localization {i}"
            abs_t = "voxel memory bank on MVTec-3D benchmark for industrial surface anomaly"
        elif sub == 1:
            title = f"Diffusion prior zero-shot defect segmentation {i}"
            abs_t = "diffusion prior zero-shot segmentation on VisA benchmark"
        else:
            title = f"Contrastive point cloud registration inspection {i}"
            abs_t = "contrastive point cloud registration and multi-view fusion"
        papers.append(preprint(i, day=DAY - timedelta(days=(i % 5) + 1), title=title, abstract=abs_t))

    run_daily(store, topic, worker(store, FixtureSources(papers)))
    settings = Settings(
        data_dir=store.directory, keyword_only=False, llm_model="gpt-4o-mini", llm_key="fixture",
    )

    # Skewed embeddings: all 30 are inside one wide cluster so initial Agglomerative picks 1 mega cluster + 1 outlier,
    # triggering balanced KMeans subdivision!
    def skewed_encode(_self, texts):
        vecs = []
        for idx, t in enumerate(texts):
            tl = t.lower()
            if idx == 0:
                v = np.array([-1.0, 0.0, 0.0, 0.0])  # single outlier
            elif "voxel" in tl:
                v = np.array([1.0, 0.15 + (idx % 4) * 0.01, 0.0, 0.0])
            elif "diffusion" in tl:
                v = np.array([1.0, 0.0, 0.15 + (idx % 4) * 0.01, 0.0])
            else:
                v = np.array([1.0, 0.0, 0.0, 0.15 + (idx % 4) * 0.01])
            vecs.append((v / np.linalg.norm(v)).tolist())
        return vecs

    monkeypatch.setattr("research_radar.clustering.Ranker.encode_texts", skewed_encode)
    snap = discover_clusters(store, settings, topic, DAY, window_days=30, min_papers=15)
    assert snap["mode"] == "dynamic"
    assert len(snap["clusters"]) >= 3
    assert max(c["metrics"]["share"] for c in snap["clusters"]) < 0.70

    enriched = enrich_clusters_with_papers(store, topic.id, snap)
    assert all(len(c["representative_papers"]) >= 1 for c in enriched["clusters"])

    # Zero-LLM insights computation
    insights = build_topic_insights(store, topic, DAY, window_days=30, clusters_info=enriched)
    assert insights["total"] >= 18 and insights["top_terms"] and "近 30 天" in insights["headline"]

    # On-demand single-call AI synthesis + cache hit
    llm_calls = []

    def fake_completion(http_client, cfg, messages, **kwargs):
        llm_calls.append(messages)
        first_pid = enriched["clusters"][0]["representative_ids"][0]
        return {"choices": [{"message": {"content": "```json\n" + json.dumps({
            "type": "json_object",
            "headline": "工业三维异常检测正从记忆库范式转向扩散先验与跨视角对比对齐。",
            "overview": "近 30 天研究主要集中在体素记忆库、零样本扩散先验分割和点云配准检测三条路径。",
            "directions": [
                {
                    "name": "体素记忆库异常定位",
                    "summary": "通过局部体素记忆特征比对定位表面异常。",
                    "key_methods": ["Voxel Memory Bank"],
                    "challenges": "计算显存占用偏高",
                    "paper_ids": [first_pid, 999999],
                }
            ],
            "datasets_and_eval": "MVTec-3D 与 VisA 仍为核心评价基准。",
            "research_advice": ["探索轻量化零样本扩散特征蒸馏"],
        }, ensure_ascii=False) + "\n```"}}]}

    monkeypatch.setattr("research_radar.period_reports.request_json_completion", fake_completion)
    with httpx.Client() as client:
        syn1 = synthesize_topic_landscape(store, settings, client, topic, DAY, window_days=30, clusters_info=enriched)
        syn2 = synthesize_topic_landscape(store, settings, client, topic, DAY, window_days=30, clusters_info=enriched)
    assert syn1["available"] is True and not syn1["cached"]
    assert syn2["cached"] is True and len(llm_calls) == 1
    assert syn1["synthesis"]["directions"][0]["paper_ids"] == [enriched["clusters"][0]["representative_ids"][0]]
