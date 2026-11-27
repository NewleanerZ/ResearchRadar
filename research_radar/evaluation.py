"""Freeze a candidate set, then compare ranking variants against human labels."""

import argparse
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import time as clock
from zoneinfo import ZoneInfo

import httpx

from .models import Record, Settings, Topic, dump, fingerprint, utc_now
from .ranking import MODEL, ModelGateway, Ranker, best_record, keyword_similarity
from .service import open_store
from .store import Store, work_date


def freeze(store, topic, start, end, limit, seed):
    candidates = []
    zone = ZoneInfo(topic.timezone)
    for item in store.papers(topic.id, topic.query_version()):
        published = work_date(item["records"], zone)
        if published and start <= published <= end:
            candidates.append({**item, "date": published})
    candidates.sort(key=lambda item: item["paper_id"])
    sample = random.Random(seed).sample(candidates, min(limit, len(candidates)))
    return {
        "frozen_at": utc_now().isoformat(), "seed": seed,
        "window": {"from": str(start), "through": str(end)},
        "topic": topic.model_dump(mode="json"),
        "scans": store.all("""SELECT s.source,s.stream,s.range_start,s.range_end,s.status,s.detail
                               FROM scans s JOIN runs r ON r.id=s.run_id
                               WHERE r.topic_id=:topic AND r.version=:version ORDER BY r.created_at DESC""",
                            topic=topic.id, version=topic.query_version()),
        "candidates": [
            {"key": str(item["paper_id"]), "date": str(item["date"]), "retrieved": True, "relevant": None,
             "records": [entry.model_dump(mode="json") for entry in item["records"]]}
            for item in sample
        ],
    }


def precision(keys, labels, k=10):
    chosen = keys[:k]
    if not chosen or any(type(labels[key]) is not bool for key in chosen):
        return None
    return sum(labels[key] for key in chosen) / len(chosen)


def compare(dataset, settings, llm=False):
    topic = Topic.model_validate(dataset["topic"])
    candidates = [{**item, "records": [Record.model_validate(entry) for entry in item["records"]]}
                  for item in dataset["candidates"] if item["retrieved"]]
    labels = {item["key"]: item["relevant"] for item in dataset["candidates"]}
    candidate_identity = fingerprint([topic.model_dump(mode="json"), dataset["window"],
                                      [[item["key"], [record.model_dump(mode="json") for record in item["records"]]]
                                       for item in candidates]])
    store = Store(settings.data_dir / "evaluation" / candidate_identity[:16])
    store.initialize()
    base = sorted(candidates, key=lambda item: (-keyword_similarity(topic, best_record(item)),
                                               -date.fromisoformat(item["date"]).toordinal(), item["key"]))
    rankings = {"B0_keywords": [item["key"] for item in base]}
    usage, a1_status = None, "disabled"
    if candidates:
        if settings.keyword_only:
            raise ValueError("比较语义排序需要启用 Embedding")
        ranker = Ranker(store, settings)
        similarity = dict(zip((item["key"] for item in candidates), ranker.similarities(topic, candidates)))
        ordered = sorted(candidates, key=lambda item: (-similarity[item["key"]], item["key"]))
        rankings["B1_embedding"] = [item["key"] for item in ordered]
        if settings.reranker_enabled:
            rerank_items = [{"paper_id": item["key"], "record": best_record(item),
                             "components": {"relevance": similarity[item["key"]]}} for item in ordered[:30]]
            rscores = ranker.rerank_scores(topic, rerank_items)
            if rscores is not None:
                b2_head = [x["paper_id"] for x in sorted(
                    [{"paper_id": item["key"], "score": 0.5 * similarity[item["key"]] + 0.5 * rs}
                     for item, rs in zip(ordered[:30], rscores)],
                    key=lambda row: (-row["score"], row["paper_id"]),
                )]
                rankings["B2_rerank"] = b2_head + rankings["B1_embedding"][30:]
        if llm:
            if not settings.llm_key.get_secret_value() or not settings.llm_model:
                raise ValueError("A1 需要配置摘要模型")
            eval_topic = topic.model_copy(update={"enabled": True, "archived": False})
            if not store.topics():
                store.save_topic(eval_topic)
            as_of = datetime.combine(date.fromisoformat(dataset["window"]["through"]), time(), timezone.utc)
            job_id = store.enqueue(eval_topic, as_of, kind="evaluation")
            job = store.one("SELECT * FROM runs WHERE id=:id", id=job_id)
            with httpx.Client() as client:
                gateway = ModelGateway(store, settings, client)
                scored = {}
                deadline = clock.monotonic() + topic.max_minutes * 60
                for item in ordered[:15]:
                    record = best_record(item)
                    with store.transaction() as connection:
                        paper_id = store._ingest(connection, record, utc_now().isoformat())
                    review = gateway.analyze(job, topic, {"record": record, "paper_id": paper_id}, deadline)
                    relevance = review["analysis"]["relevance"] if review["analysis"] else None
                    if relevance is not None:
                        scored[item["key"]] = .75 * similarity[item["key"]] + .25 * relevance
                keys = rankings["B1_embedding"]
                head = sorted(keys[:15], key=lambda key: (-scored.get(key, similarity[key]), key))
                rankings["A1_embedding_llm"] = head + keys[15:]
                a1_status = f"{len(scored)}/{len(head)} cards verified"
            usage = json.loads(store.one("SELECT usage FROM runs WHERE id=:id", id=job_id)["usage"])
    metrics = {method: {"precision_at_10": precision(keys, labels), "top_count": min(10, len(keys))}
               for method, keys in rankings.items()}
    gold = sum(value is True for value in labels.values())
    has_independent_gold = any(not item["retrieved"] for item in dataset["candidates"])
    recall = (sum(labels[item["key"]] is True for item in candidates) / gold
              if gold and has_independent_gold and all(type(value) is bool for value in labels.values()) else None)
    return {"candidate_set": candidate_identity, "candidates": len(candidates),
            "human_labels": sum(type(value) is bool for value in labels.values()),
            "retrieval_recall": recall, "embedding": {"model": MODEL, "revision": settings.embedding_revision},
            "a1_status": a1_status, "llm_usage": usage, "metrics": metrics, "rankings": rankings}


def replay_nas():
    """Replay the same chat-to-backfill path in a disposable database."""
    from .activity import timeline
    from .conversation import actions, confirm_action, new_conversation, submit_turn
    from .models import Page, Publication, backfill_dates, backfill_venues
    from .workflow import Worker

    requests = []
    text = "查询神经网络架构搜索（NAS）近6个月的相关研究并创建这个主题，将这个方向的顶会论文入库"
    steps = [
        [("list_topics", {"query": "NAS"}), ("search_library", {"query": "neural architecture search"})],
        [("propose_action", {"action": "create_topic", "draft": {"name": "神经网络架构搜索 NAS",
          "keywords": ["neural architecture search"]}, "after": "2025-05-27", "before": "2025-11-27",
          "venues": ["ICML", "NEURIPS"]})],
        [],
    ]

    def response(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(index)
        if index >= len(steps) or (index and payload["messages"][-1]["role"] != "tool"):
            raise ValueError("回放顺序发生变化")
        call_list = [{"id": f"{index}-{name}", "type": "function", "function": {"name": name, "arguments": dump(args)}}
                     for name, args in steps[index]]
        return httpx.Response(200, json={"choices": [{"message": {"content": None if call_list else "请确认。",
                              "tool_calls": call_list}}], "usage": {"prompt_tokens": 120, "completion_tokens": 25}})

    articles = [Record(source="openalex", external_id=f"W{number}", title=title, url=f"https://openalex.org/W{number}",
                       published_on=date(2025, 7, 8), publication=Publication(status="published", venue=venue))
                for number, title, venue in [(1, "Neural architecture search for edge devices", "ICML 2025"),
                    (2, "Efficient neural architecture search", "NeurIPS Workshop on AutoML"),
                    (3, "Neural architecture search review", "Journal of Example Methods")]]

    class Fixture:
        def page(self, source, _topic, _terms, start, end, _cursor, _deadline):
            return Page(records=[paper for paper in articles if source == "openalex" and start <= paper.published_on < end])

    with TemporaryDirectory(prefix="research-radar-replay-") as directory:
        settings = Settings(data_dir=Path(directory), keyword_only=True, llm_key="fixture", llm_model="fixture",
                            extra_sources="")
        store = open_store(settings)
        chat = new_conversation(store)
        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            turn = submit_turn(store, settings, chat, text, client=client)
            pending = actions(store, turn)
            if len(pending) != 1 or pending[0]["status"] != "pending" or store.topics():
                raise ValueError("NAS 确认卡回放失败")
            action = pending[0]["id"]
            receipt = confirm_action(store, chat, action)
            if confirm_action(store, chat, action) != receipt:
                raise ValueError("重复确认未复用回执")
            key = receipt["run_id"]
            Worker(store, settings, client, Fixture()).tick(schedule=False, run_id=key, notify=False)
            run = store.one("SELECT status,kind,result FROM runs WHERE id=:id", id=key)
            papers = [record.external_id for item in store.papers(receipt["topic"]) for record in item["records"]]
            events = [event["stage"] for event in timeline(store, "chat", turn)]
            run_events = {event["stage"] for event in timeline(store, "run", key)}
            if (run["status"] != "succeeded" or papers != ["W1"] or len(requests) != 3
                    or not {"model", "tool", "confirmation"}.issubset(events)
                    or not {"confirmation", "source", "job"}.issubset(run_events)):
                raise ValueError("NAS 回放结果发生变化")
            return {"status": "passed", "model_calls": len(requests), "confirmation_cards": len(pending),
                    "imported": json.loads(run["result"])["imported_count"],
                    "dates": [value.isoformat() for value in backfill_dates(run["kind"])],
                    "venues": backfill_venues(run["kind"]), "recorded_events": len(events),
                    "duplicate_confirmation_reused": True}


def audit_frozen_reviews(review_path):
    """Score stored rankings and locate the annotated excerpts in their original records."""
    from .research import Claim, Findings, verified_findings

    reviewed = json.loads(review_path.read_text(encoding="utf-8"))
    results = {}
    for key, group in reviewed.items():
        dataset = json.loads((review_path.parent / group["dataset"]).read_text(encoding="utf-8"))
        rankings = json.loads((review_path.parent / group["rankings"]).read_text(encoding="utf-8"))["rankings"]
        by_id = {entry["key"]: entry for entry in dataset["candidates"]}
        evaluations = {name: precision(order, group["relevance"]) for name, order in rankings.items()}
        if any(value is None for value in evaluations.values()):
            raise ValueError("前十名的评测标注不完整")
        located, supported = 0, 0
        for item in group["evidence"]:
            paper = by_id[item["paper"]]
            record = next((record for record in paper["records"] if item["quote"] in (record["abstract"] or "")), None)
            if not record:
                raise ValueError(f"原文中找不到证据：{item['paper']}")
            evidence = {"E1": {"paper": item["paper"], "quote": item["quote"], "source_text": record["abstract"]}}
            claims, _, _ = verified_findings(Findings(claims=[Claim(text=item["claim"], evidence_ids=["E1"])]), evidence)
            located += bool(claims)
            supported += item["supported"]
        results[key] = {"candidate_count": len(by_id), "reviewed_count": len(group["relevance"]),
                        "precision_at_10": evaluations, "retrieval_recall": None, "evidence_examples": len(group["evidence"]),
                        "quotes_located": located, "claims_supported_per_review": supported,
                        "scope": "已冻结候选和已保存排序，未包含独立召回对照集"}
    return results


def evaluate_hotspots(store: Store, settings: Settings, topic: Topic, as_of: date) -> dict:
    """Compare H0 (fixed subtopic growth) vs H1/H2 (dynamic semantic clustering & composite H) at a split date."""
    from .clustering import discover_clusters
    from .trends import trend_snapshot

    fake_run = {"local_day": as_of.isoformat(), "version": topic.query_version()}
    h0 = trend_snapshot(store, fake_run, topic, {"arxiv": "complete", "openalex": "complete"})
    h1_h2 = discover_clusters(store, settings, topic, as_of, window_days=30)
    return {
        "as_of": as_of.isoformat(),
        "topic_id": topic.id,
        "H0_fixed_windows": [w for w in h0["windows"] if w["days"] == 30],
        "H1_H2_dynamic": h1_h2,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("freeze")
    export.add_argument("topic")
    export.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    export.add_argument("--through", required=True, type=date.fromisoformat)
    export.add_argument("--limit", type=int, choices=range(50, 101), default=80, metavar="50–100")
    export.add_argument("--seed", type=int, default=2026)
    export.add_argument("output", type=Path)
    review = commands.add_parser("compare")
    review.add_argument("dataset", type=Path)
    review.add_argument("output", type=Path)
    review.add_argument("--llm", action="store_true")
    commands.add_parser("replay-nas")
    audit = commands.add_parser("audit")
    audit.add_argument("reviews", type=Path, nargs="?", default=Path("data/acceptance/agent-controls-review.json"))
    hot = commands.add_parser("evaluate-hotspots")
    hot.add_argument("topic")
    hot.add_argument("--as-of", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.command == "replay-nas":
        print(dump(replay_nas()))
        return
    if args.command == "audit":
        print(dump(audit_frozen_reviews(args.reviews)))
        return
    settings = Settings.from_env()
    if args.command == "evaluate-hotspots":
        store = open_store(settings)
        print(dump(evaluate_hotspots(store, settings, store.topic(args.topic)[0], args.as_of)))
        return
    if args.output.exists():
        parser.error("输出文件已存在，请选择新路径")
    if args.command == "freeze":
        if args.start > args.through:
            parser.error("起始日期晚于结束日期")
        store = open_store(settings)
        value = freeze(store, store.topic(args.topic)[0], args.start, args.through, args.limit, args.seed)
    else:
        value = compare(json.loads(args.dataset.read_text(encoding="utf-8-sig")), settings, args.llm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        target.write(dump(value) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
