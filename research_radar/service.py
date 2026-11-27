from datetime import date, timedelta
import json
import re
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from .cs_sources import within
from .delivery import render_report
from .models import DATE_BACKFILL_PREFIX, EXTRA_SOURCES, Settings, Topic, TopicDraft, dump, paper_publication, utc_now
from .ranking import request_json_completion
from .sources import SourceError, Sources, catalog_applies
from .store import Store, norm, work_date, write

RUN_LABELS = {
    "queued": "等待检索", "running": "正在检索", "succeeded": "已完成",
    "partial": "部分完成", "failed": "运行失败", "cancelled": "已取消",
}


def open_store(settings: Settings):
    store = Store(settings.data_dir)
    store.initialize()
    return store


def sample_topics():
    return [
        Topic(id="industrial_3d_ad", name="三维工业异常检测",
              description="工业工件的点云异常检测、表面缺陷定位和几何建模。",
              keywords=["3D anomaly detection", "point cloud anomaly detection", "industrial anomaly detection"],
              excluded=["medical imaging"], categories=["cs.CV", "cs.LG"],
              proposed_expansions=["point cloud anomaly segmentation", "industrial point cloud inspection"],
              subtopics={"点云异常检测": ["point cloud anomaly detection", "point cloud"],
                         "异常分割与定位": ["anomaly segmentation", "anomaly localization"],
                         "工业检测": ["industrial anomaly detection", "industrial inspection"]}),
        Topic(id="llm_agent_evaluation", name="LLM Agent 评估",
              description="关注大语言模型智能体的基准、工具使用与可靠性评估。",
              keywords=["LLM agent evaluation", "agent benchmark", "tool use benchmark"],
              categories=["cs.AI", "cs.CL", "cs.LG"],
              proposed_expansions=["agentic benchmark", "language model agent evaluation"],
              subtopics={"工具使用": ["tool use", "tool calling"], "智能体基准": ["benchmark", "evaluation"]}),
    ]


def make_topic_draft(description: str, draft: TopicDraft, *, topic_id=None) -> Topic:
    reserved = {"ai", "ml", "model", "benchmark", "research"}
    if any(term.strip().casefold() in reserved for term in draft.keywords + draft.proposed_expansions):
        raise ValueError("检索词过于宽泛")
    return Topic(id=topic_id or f"topic_{uuid4().hex[:12]}", description=description, **draft.model_dump())


def draft_topic(description: str, settings: Settings, client: httpx.Client) -> Topic:
    description = description.strip()
    if not description or len(description) > 1500:
        raise ValueError("请用 1 至 1500 字描述想关注的方向")
    if not settings.llm_model or not settings.llm_key.get_secret_value():
        raise ValueError("摘要模型尚未配置，可以手动填写主题和检索词")
    messages = [
        {"role": "system", "content": (
            "把用户的科研方向整理成文献检索配置，以 JSON 对象返回。"
            "每个 keywords 元素会作为独立的短语参加 OR 搜索，因此每个词组都要单独准确覆盖主题。"
            "优先保留用户明确给出的英文术语、数据集和约束，将中文术语转为学界常用英文。"
            "用 1 至 6 个具体英文核心短语，避免 AI、model、benchmark 等单独出现的宽泛词。"
            "同义短语可放 proposed_expansions，只用于需要时的补充检索。"
            "excluded 只放用户明确要求排除的内容。subtopics 用简短中文标签和相关英文短语。"
            "以下是 JSON 格式示例，按用户实际主题替换内容："
            '{"name":"三维工业异常检测","keywords":["3D industrial anomaly detection","point cloud defect detection"],'
            '"excluded":[],"proposed_expansions":["industrial 3D defect inspection"],'
            '"subtopics":{"点云表面检测":["point cloud defect detection"]}}'
        )},
        {"role": "user", "content": dump({"研究方向": description})},
    ]
    try:
        reply = request_json_completion(client, settings, messages, max_tokens=1000, timeout=30)
        draft = TopicDraft.model_validate_json(reply["choices"][0]["message"]["content"])
        return make_topic_draft(description, draft)
    except httpx.HTTPStatusError as error:
        raise ValueError(f"模型请求失败（HTTP {error.response.status_code}），可重试或手动填写") from None
    except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError, KeyError, IndexError):
        raise ValueError("模型没有返回可用的检索设置，请重试或手动填写") from None


def enqueue_daily(store, topic_id):
    return store.enqueue(store.topic(topic_id)[0], utc_now())


def backfill_kind(topic, start, through, now, *, venues=(), catalogs=()):
    today = now.astimezone(ZoneInfo(topic.timezone)).date()
    if start > through:
        raise ValueError("开始日期须早于或等于结束日期")
    if through > today:
        raise ValueError("结束日期须为今天或更早")
    # Use the date bounds as part of the run kind so different requests can share the same topic and day.
    names = sorted({venue.strip().upper() for venue in venues})
    if any(not re.fullmatch(r"[A-Z][A-Z0-9-]{1,19}", name) for name in names):
        raise ValueError("请使用会议简称，例如 NeurIPS、ICML 或 ICLR")
    scope = "@" + ",".join(names) if names else ""
    expanded = tuple(key for key in EXTRA_SOURCES if key in catalogs)
    profile = "~" + ",".join(expanded) if expanded else ""
    return f"{DATE_BACKFILL_PREFIX}{start.isoformat()}:{through.isoformat()}{scope}{profile}"


def enqueue_backfill(store, topic_id, start, through, *, settings=None):
    topic, _ = store.topic(topic_id)
    now = utc_now()
    catalogs = tuple(key for key in settings.catalog_sources() if catalog_applies(key, topic)) if settings else ()
    return store.enqueue(topic, now, kind=backfill_kind(topic, start, through, now, catalogs=catalogs))


def report_history(store, topic_id, *, day=None, run_id=None):
    return store.all("""SELECT r.id,r.local_day,r.result,p.markdown FROM reports p JOIN runs r ON r.id=p.run_id
        WHERE r.topic_id=:topic AND (:day IS NULL OR r.local_day=:day) AND (:id IS NULL OR r.id=:id)
        ORDER BY r.local_day DESC LIMIT 90""", topic=topic_id, day=day.isoformat() if day else None, id=run_id)


def trend_history(store, topic_id):
    return [json.loads(item["snapshot"]) for item in store.all(
        "SELECT snapshot FROM trends WHERE topic_id=:topic ORDER BY local_day DESC LIMIT 90", topic=topic_id)]


def run_history(store, topic_id="", run_id=None):
    return store.all("""SELECT r.id,r.topic_id,r.local_day,r.kind,r.status,r.error,r.usage,r.result,
        n.status AS notification,n.detail AS delivery_detail,n.attempts
        FROM runs r LEFT JOIN notifications n ON r.id=n.run_id WHERE (:topic='' OR r.topic_id=:topic)
        AND (:id IS NULL OR r.id=:id) ORDER BY r.created_at DESC LIMIT 100""", topic=topic_id, id=run_id)


def daily_overview(store, topic_id):
    latest_run = store.one(
        """SELECT id,local_day,status FROM runs
           WHERE topic_id=:topic AND kind='daily' ORDER BY local_day DESC LIMIT 1""",
        topic=topic_id,
    )
    latest_report = store.one(
        """SELECT r.id,r.local_day,r.result FROM reports p JOIN runs r ON r.id=p.run_id
           WHERE r.topic_id=:topic ORDER BY r.local_day DESC LIMIT 1""",
        topic=topic_id,
    )
    if latest_report:
        result = json.loads(latest_report.pop("result") or "{}")
        latest_report["papers"] = result.get("recommendations", [])
    return latest_run, latest_report


def refresh_publications(store, settings):
    counts = {"arxiv": 0, "openalex": 0, "reports": 0}
    failures = {}
    with store.worker_lock, httpx.Client() as client:
        sources = Sources(client, settings.openalex_key.get_secret_value())
        deadline = time.monotonic() + 180
        for source in ("arxiv", "openalex"):
            ids = [item["external_id"] for item in store.all(
                "SELECT external_id FROM paper_sources WHERE source=:source ORDER BY external_id", source=source)]
            for offset in range(0, len(ids), 50):
                batch = ids[offset:offset + 50]
                try:
                    if source == "arxiv":
                        publications = {record.external_id: record.publication
                                        for record in sources.arxiv_versions(batch, deadline)}
                    else:
                        publications = sources.openalex_publications(batch, deadline)
                except SourceError as error:
                    failures[source] = str(error)
                    break
                with store.transaction() as con:
                    for external_id, publication in publications.items():
                        if publication is not None:
                            counts[source] += write(con, """UPDATE paper_sources
                                SET record=json_set(record, '$.publication', json(:publication))
                                WHERE source=:source AND external_id=:id""",
                                publication=publication.model_dump_json(), source=source, id=external_id).rowcount

        runs = store.all("""SELECT r.*, p.path, t.snapshot FROM reports p JOIN runs r ON r.id=p.run_id
            JOIN trends t ON t.topic_id=r.topic_id AND t.version=r.version AND t.local_day=r.local_day""")
        topics = {}
        for run in runs:
            key = run["topic_id"]
            if key not in topics:
                topics[key] = {paper["paper_id"]: paper_publication(paper["records"])
                               for paper in store.papers(key)}
            result = json.loads(run["result"])
            for recommendation in result["recommendations"]:
                publication = topics[key].get(recommendation["paper_id"])
                if publication:
                    recommendation.update(publication.model_dump())
            topic = Topic.model_validate_json(run["topic_snapshot"])
            markdown = render_report(topic, run, result, json.loads(run["snapshot"]))
            store.update_report_publications(run["id"], run["path"], result, markdown)
            counts["reports"] += 1
    return {**counts, "errors": failures}


def library(store, topic: Topic, query="", after=None, before=None, favorite=False, unread=False, pdf_available=False,
            *, paper_id: int | None = None):
    papers = store.papers(topic.id, paper_id=paper_id)
    if not papers:
        return []
    zone = ZoneInfo(topic.timezone)
    checks_sql = "SELECT paper_id, status, pdf_url, strategy, reason, checked_at FROM paper_pdf_checks"
    indexed_sql = "SELECT paper_id, source_url FROM paper_documents WHERE status='indexed'"
    if paper_id is not None:
        checks_sql += " WHERE paper_id=:paper_id"
        indexed_sql += " AND paper_id=:paper_id"
    checks = {item["paper_id"]: item for item in store.all(checks_sql, paper_id=paper_id)}
    indexed = {item["paper_id"]: item["source_url"] for item in store.all(indexed_sql, paper_id=paper_id)}
    matches = []
    for paper in papers:
        records = paper["records"]
        when = work_date(records, zone)
        text = norm(" ".join(" ".join([record.title, record.abstract or "", *record.authors]) for record in records))
        if not all(word in text.split() for word in norm(query).split()):
            continue
        coarse = next((record for record in records if record.published_year and record.published_month),
                      next((record for record in records if record.published_year), None))
        if after or before:
            if when is not None:
                if (after and when < after) or (before and when > before):
                    continue
            elif not any(within(record, after or date.min, before + timedelta(days=1) if before else date.max, zone)
                         for record in records):
                continue
        if (favorite and not paper["favorite"]) or (unread and paper["is_read"]):
            continue
        pid = paper["paper_id"]
        chk = checks.get(pid)
        if pid in indexed:
            pdf_status, pdf_url, pdf_strategy, pdf_reason, pdf_checked_at = (
                "available", (chk or {}).get("pdf_url") or indexed[pid], (chk or {}).get("strategy") or "indexed",
                None, (chk or {}).get("checked_at"),
            )
        elif chk:
            pdf_status, pdf_url, pdf_strategy, pdf_reason, pdf_checked_at = (
                chk["status"], chk["pdf_url"], chk["strategy"], chk["reason"], chk["checked_at"],
            )
        else:
            pdf_status, pdf_url, pdf_strategy, pdf_reason, pdf_checked_at = "unchecked", None, None, None, None
        if pdf_available and pdf_status != "available":
            continue
        month = f"-{coarse.published_month:02d}" if coarse and coarse.published_month else ""
        label = when.isoformat() if when else f"{coarse.published_year}{month}" if coarse else None
        matches.append({
            **paper,
            "date": when,
            "date_label": label,
            "pdf_status": pdf_status,
            "pdf_url": pdf_url,
            "pdf_strategy": pdf_strategy,
            "pdf_reason": pdf_reason,
            "pdf_checked_at": pdf_checked_at,
        })
    return matches


def find_card(store, topic_id, paper_id):
    for run in store.all("SELECT result FROM runs WHERE topic_id=:topic AND kind='daily' AND result IS NOT NULL "
                         "ORDER BY local_day DESC", topic=topic_id):
        for item in json.loads(run["result"]).get("recommendations", []):
            if item["paper_id"] == paper_id:
                return item["card"]
    return None


def print_json(value):
    print(json.dumps(value, ensure_ascii=(sys.stdout.encoding or "").lower() not in {"utf-8", "utf8"},
                     indent=2, default=str))
