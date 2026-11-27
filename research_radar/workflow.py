from datetime import date, datetime, timedelta
import json
import logging
import time
from typing import TypedDict
from urllib.parse import quote
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from filelock import Timeout
import httpx
from langgraph.graph import END, START, StateGraph

from .activity import log_event
from .clustering import ensure_cluster_snapshots
from .delivery import notification_text, render_report, send_event_alerts, send_one
from .events import detect_daily_events, queue_event_notifications, scan_library_events
from .models import CORE_SOURCES, Topic, backfill_catalogs, backfill_dates, backfill_venues, dump, paper_publication, utc_now
from .pdf_explorer import precheck_library_pdfs
from .period_reports import generate_period_report
from .ranking import ModelGateway, Ranker, fallback_card, mmr_select, sort_scores
from .research import tick_research
from .sources import SourceError, Sources, catalog_applies
from .trends import trend_snapshot

BACKFILLS = {"backfill-recent": (90, 0), "backfill-older": (180, 90)}
logger = logging.getLogger(__name__)


class DailyState(TypedDict, total=False):
    sources: dict
    ranked: list
    warnings: list[str]
    candidates: int
    expand: bool
    result: dict


class Worker:
    def __init__(self, store, settings, client=None, sources=None):
        self.store = store
        self.settings = settings
        self.client = client or httpx.Client(headers={"User-Agent": "ResearchRadar/0.1"})
        self.sources = sources or Sources.configured(self.client, settings)
        self.daily_sources = getattr(self.sources, "daily_sources", CORE_SOURCES)
        self.historical_sources = getattr(self.sources, "historical_sources", CORE_SOURCES)
        self.ranker = Ranker(store, settings)
        self.gateway = ModelGateway(store, settings, self.client)

    def scan(self, run, topic, source, stream, terms, start, end, deadline):
        started = time.monotonic()
        saved = self.store.start_scan(run["id"], source, stream, start, end)
        start, end = date.fromisoformat(saved["range_start"]), date.fromisoformat(saved["range_end"])
        params = {"run": run["id"], "src": source, "stream": stream}
        if saved["status"] != "complete":
            log_event(self.store, "run", run["id"], "source", "running",
                      detail={"source": source, "stream": stream, "from": str(start), "until": str(end)})
            cursor = saved["cursor"]
            seen = {cursor}
            status, detail = "truncated", "page_budget"
            try:
                max_pages = self.settings.page_limit if source in CORE_SOURCES else min(self.settings.page_limit, 16)
                for _ in range(max_pages):
                    page = self.sources.page(source, topic, terms, start, end, cursor, deadline)
                    if page.cursor is not None and page.cursor in seen:
                        raise SourceError("cursor_repeated")
                    self.store.save_page(run, topic, source, stream, page.records, page.cursor, end)
                    if page.cursor is None:
                        status, detail = "complete", None
                        break
                    cursor = page.cursor
                    seen.add(cursor)
            except SourceError as error:
                detail = str(error)
                status = "truncated" if detail == "time_budget" else "failed"
            self.store.execute("""UPDATE scans SET status=:status,detail=:detail
                WHERE run_id=:run AND source=:src AND stream=:stream""", **params, status=status, detail=detail)
        final = self.store.one("""SELECT status,detail,received FROM scans
            WHERE run_id=:run AND source=:src AND stream=:stream""", **params)
        if saved["status"] != "complete":
            log_event(self.store, "run", run["id"], "source", final["status"],
                      detail={"source": source, "stream": stream, "received": final["received"], "reason": final["detail"]},
                      elapsed_ms=int((time.monotonic() - started) * 1000))
        return {**final, "start": str(start), "end": str(end)}

    def date_range(self, run, source):
        day = date.fromisoformat(run["local_day"])
        saved = self.store.one("""SELECT watermark FROM source_sync
            WHERE topic_id=:topic AND version=:version AND source=:source""",
            topic=run["topic_id"], version=run["version"], source=source)
        if saved:
            start = date.fromisoformat(saved["watermark"]) - timedelta(days=3)
        else:
            oldest = self.store.one("""SELECT min(range_start) AS oldest FROM scans s JOIN runs r ON r.id=s.run_id
                WHERE r.topic_id=:topic AND r.version=:version AND r.kind='daily' AND s.source=:source
                AND s.stream IN ('main','supplemental')""",
                topic=run["topic_id"], version=run["version"], source=source)
            start = date.fromisoformat(oldest["oldest"]) if oldest["oldest"] else day - timedelta(days=3)
        return min(start, day - timedelta(days=3)), day + timedelta(days=1)

    def daily(self, run, topic: Topic, deadline):
        day = date.fromisoformat(run["local_day"])

        def sync(_state):
            coverage = {}
            for source in self.daily_sources:
                start, end = self.date_range(run, source)
                primary = source in CORE_SOURCES
                coverage[source] = self.scan(run, topic, source, "main" if primary else "supplemental",
                                             topic.keywords, start, end, min(deadline, time.monotonic() + (300 if primary else 50)))
            return {"sources": coverage, "warnings": []}

        def rank(_state):
            papers = self.store.papers(topic.id, discovered_through=run["local_day"],
                                       discovery_version=run["version"])
            ranked, warning = self.ranker.rank(topic, papers, day)
            return {"ranked": ranked, "candidates": len(papers), "warnings": [warning] if warning else []}

        def needs_expansion(state):
            if (topic.expansions and state.get("candidates") and len(state["ranked"]) < 5
                    and time.monotonic() < deadline
                    and all(info["status"] == "complete" for info in state["sources"].values())
                    and any(info["received"] for info in state["sources"].values())):
                return "expand"
            return "analyze"

        def expand(_state):
            statuses = [self.scan(run, topic, name, "expansion", topic.expansions, day - timedelta(days=3),
                                  day + timedelta(days=1), min(deadline, time.monotonic() + 75)) for name in self.daily_sources]
            updates = rank(_state)
            if any(item["status"] != "complete" for item in statuses):
                updates["warnings"].append("补充检索覆盖不完整")
            return updates

        def analyze(state):
            ranked = state.get("ranked", [])
            warnings = list(state["warnings"])
            for item in ranked[:15]:
                card = self.gateway.analyze(run, topic, item, deadline)
                item["card"] = card
                if card["analysis"]:
                    parts = item["components"]
                    parts["llm_relevance"] = card["analysis"]["relevance"]
                    parts["relevance"] = .75 * parts["relevance"] + .25 * parts["llm_relevance"]
                elif card["reason"] not in warnings:
                    warnings.append(card["reason"])
            recommendations = []
            # LLM adjustments rerank the same shortlist so an unreviewed paper cannot displace it.
            for item in mmr_select(sort_scores(ranked[:15]), k=5):
                record = item["record"]
                publication = paper_publication(item["records"])
                links = [{"source": entry.source, "url": entry.url, "external_id": entry.external_id}
                         for entry in sorted(item["records"], key=lambda entry: entry != record)]
                for doi in sorted({entry.doi for entry in item["records"] if entry.doi}):
                    links.append({"source": "doi", "url": f"https://doi.org/{quote(doi, safe='/')}", "external_id": doi})
                recommendations.append({
                    "paper_id": item["paper_id"], "title": record.title, "authors": record.authors,
                    "published": item["published"], "status": publication.status, "venue": publication.venue,
                    "links": links,
                    "card": item.get("card") or fallback_card(record, "基础摘要"),
                    "components": item["components"], "score": item["score"],
                })
            return {"result": {"sources": state["sources"], "warnings": warnings,
                               "candidates": state.get("candidates", 0), "eligible": len(ranked),
                               "recommendations": recommendations,
                               "scores": [{"paper_id": item["paper_id"], "score": item["score"],
                                           "components": item["components"]} for item in ranked]}}

        def finalize(state):
            result = state["result"]
            source_states = {name: info["status"] for name, info in result["sources"].items()}
            trend = trend_snapshot(self.store, run, topic, source_states)
            self.store.execute("""INSERT INTO trends VALUES (:topic,:version,:day,:trend)
                ON CONFLICT(topic_id,version,local_day) DO UPDATE SET snapshot=excluded.snapshot""",
                topic=topic.id, version=run["version"], day=run["local_day"], trend=dump(trend))
            markdown = render_report(topic, run, result, trend)
            enabled = topic.notify and bool(self.settings.feishu_webhook.get_secret_value())
            degraded = bool(result["warnings"])
            status = "succeeded" if all(status == "complete" for status in source_states.values()) and not degraded else "partial"
            self.store.finish(run, status, result, markdown, notification_text(topic, run, result) if enabled else None)
            detect_daily_events(self.store, topic, run, result, trend, client=self.client,
                                settings=self.settings, deadline=deadline)
            scan_library_events(self.store, topic, limit=120)
            try:
                ensure_cluster_snapshots(self.store, self.settings, topic,
                                         date.fromisoformat(run["local_day"]) + timedelta(days=1))
            except Exception:
                pass
            try:
                if not isinstance(getattr(self.client, "_transport", None), httpx.MockTransport):
                    precheck_library_pdfs(self.store, self.settings, topic.id, limit=25, deadline_seconds=18.0)
            except Exception:
                pass
            if enabled:
                queue_event_notifications(self.store, self.settings, topic)
            return {}

        def sources_failed(state):
            if all(info["status"] == "failed" for info in state["sources"].values()):
                return "fail"
            return "rank"

        def fail(state):
            self.store.finish(run, "failed", {"error": "all_sources_failed", "sources": state["sources"]})
            return {}

        graph = StateGraph(DailyState)
        for node in (sync, rank, expand, analyze, finalize, fail):
            graph.add_node(node.__name__, node)
        graph.add_edge(START, "sync")
        graph.add_conditional_edges("sync", sources_failed, {"fail": "fail", "rank": "rank"})
        graph.add_conditional_edges("rank", needs_expansion, {"expand": "expand", "analyze": "analyze"})
        graph.add_edge("expand", "analyze")
        graph.add_edge("analyze", "finalize")
        graph.add_edge("finalize", END)
        graph.add_edge("fail", END)
        graph.compile().invoke({})

    def refresh_arxiv(self, run, topic, day, deadline):
        start = day - timedelta(days=90)
        saved = self.store.start_scan(run["id"], "arxiv", "versions", start, day)
        if saved["status"] == "complete":
            return "complete"
        # Reserve a stable list per run; rescans must not shift the boundaries of a saved batch.
        old = self.store.one("SELECT result FROM runs WHERE id=:id", id=run["id"])
        frozen = json.loads(old["result"] or "{}").get("version_ids")
        if frozen is None:
            current = set()
            for paper in self.store.papers(topic.id):
                current.update(record.arxiv_id for record in paper["records"]
                               if record.arxiv_id and record.preprint_at and record.preprint_at.date() >= start)
            frozen = sorted(current)
            self.store.execute("UPDATE runs SET result=:plan WHERE id=:id",
                               plan=dump({"version_ids": frozen}), id=run["id"])
        index = int(saved["cursor"] or 0)
        status = "complete"
        try:
            for offset in range(index, max(1, len(frozen)), 50):
                batch = frozen[offset:offset + 50]
                records = self.sources.arxiv_versions(batch, deadline) if batch else []
                if {record.arxiv_id for record in records} != set(batch):
                    raise SourceError("arxiv_version_ids_missing")
                cursor = str(offset + 50) if offset + 50 < len(frozen) else None
                self.store.save_page(run, topic, "arxiv", "versions", records, cursor, day)
        except SourceError as error:
            status = "truncated" if str(error) == "time_budget" else "failed"
            self.store.execute("""UPDATE scans SET status=:status,detail=:detail
                WHERE run_id=:id AND source='arxiv' AND stream='versions'""",
                status=status, detail=str(error), id=run["id"])
        return status

    def scan_backfill_range(self, run, topic, source, start, end, deadline):
        # Keep each slice's checkpoint so a long import can resume without re-fetching completed dates.
        current = start
        received = 0
        stream = "venues" if backfill_venues(run["kind"]) else "main"
        if source not in CORE_SOURCES:
            stream = "venues" if stream == "venues" else "catalog"
            return self.scan(run, topic, source, f"{stream}:{current}", topic.keywords, current, end, deadline)
        while True:
            stop = current + timedelta(days=min(30, (end - current).days))
            scan = self.scan(run, topic, source, f"{stream}:{current}", topic.keywords, current, stop, deadline)
            received += scan["received"]
            if scan["status"] != "complete" or stop == end:
                status = scan["status"]
                if status == "failed" and (current > start or received):
                    status = "partial"
                return {**scan, "status": status, "received": received, "start": str(start), "end": str(end)}
            current = stop

    def historical(self, run, topic, deadline):
        day = date.fromisoformat(run["local_day"])
        dates = backfill_dates(run["kind"])
        chosen = backfill_catalogs(run["kind"])
        sources = tuple(source for source in self.historical_sources
                        if (chosen is None or source in CORE_SOURCES or source in chosen) and catalog_applies(source, topic))
        if dates:
            start, through = dates
            end = through + timedelta(days=1)
            coverage = {source: self.scan_backfill_range(
                run, topic, source, start, end, min(deadline, time.monotonic() + (400 if source in CORE_SOURCES else 90)))
                for source in sources}
        elif run["kind"] == "reconcile":
            version_status = self.refresh_arxiv(run, topic, day, min(deadline, time.monotonic() + 240))
            coverage = {"arxiv_versions": {"status": version_status},
                        "openalex": self.scan(run, topic, "openalex", "main", topic.keywords,
                                              day - timedelta(days=30), day, deadline)}
        else:
            oldest, newest = BACKFILLS[run["kind"]]
            coverage = {source: self.scan(run, topic, source, "main" if source in CORE_SOURCES else "catalog",
                                          topic.keywords, day - timedelta(days=oldest), day - timedelta(days=newest),
                                          min(deadline, time.monotonic() + (400 if source in CORE_SOURCES else 90)))
                        for source in sources}
        states = {entry["status"] for entry in coverage.values()}
        status = "succeeded" if states == {"complete"} else ("failed" if states == {"failed"} else "partial")
        saved = self.store.one("SELECT result FROM runs WHERE id=:id", id=run["id"])
        self.store.finish(run, status, {**json.loads(saved["result"] or "{}"), "sources": coverage})
        scan_library_events(self.store, topic, limit=120)
        try:
            ensure_cluster_snapshots(self.store, self.settings, topic, day + timedelta(days=1))
        except Exception:
            pass
        try:
            if not isinstance(getattr(self.client, "_transport", None), httpx.MockTransport):
                precheck_library_pdfs(self.store, self.settings, topic.id, limit=40, deadline_seconds=25.0)
        except Exception:
            pass

    def run(self, job):
        started = time.monotonic()
        deadline = time.monotonic() + Topic.model_validate_json(job["topic_snapshot"]).max_minutes * 60
        log_event(self.store, "run", job["id"], "job", "running",
                  detail={"kind": job["kind"], "topic_id": job["topic_id"]})
        try:
            topic = Topic.model_validate_json(job["topic_snapshot"])
            if job["kind"] == "daily":
                self.daily(job, topic, deadline)
            else:
                self.historical(job, topic, deadline)
        except Exception as error:
            # Exception messages can include API URLs or tokens. Persist only the type.
            logger.error("run %s interrupted: %s", job["id"], type(error).__name__)
            self.store.execute("UPDATE runs SET status='failed',error=:error,updated_at=:now WHERE id=:id",
                               id=job["id"], error=type(error).__name__, now=utc_now().isoformat())
        final = self.store.one("SELECT status,error,result FROM runs WHERE id=:id", id=job["id"])
        if final:
            log_event(self.store, "run", job["id"], "job", final["status"],
                      detail={"imported": json.loads(final["result"] or "{}").get("imported_count", 0),
                              "error": final["error"]}, elapsed_ms=int((time.monotonic() - started) * 1000))

    def schedule_due(self, now: datetime, maintenance=True):
        for topic in self.store.topics():
            if not topic.enabled or topic.archived:
                continue
            local = now.astimezone(ZoneInfo(topic.timezone))
            try:
                if local.time() >= topic.schedule:
                    daily = self.store.one("""SELECT status FROM runs
                        WHERE topic_id=:topic AND kind='daily' AND local_day=:day""",
                        topic=topic.id, day=local.date().isoformat())
                    if daily is None or daily["status"] == "cancelled":
                        self.store.enqueue(topic, now, automatic=True)
                if not maintenance:
                    continue
                first = self.store.one("""SELECT local_day FROM runs
                    WHERE topic_id=:topic AND kind='daily' AND version=:version
                    ORDER BY created_at LIMIT 1""", topic=topic.id, version=topic.query_version())
                if not first:
                    continue
                anchor = date.fromisoformat(first["local_day"])
                for kind in BACKFILLS:
                    self.store.enqueue(topic, now, kind, anchor, automatic=True)
                weekly = local.date() - timedelta(days=local.weekday())
                if (weekly > anchor or weekly == anchor) and local.time() >= topic.schedule:
                    self.store.enqueue(topic, now, "reconcile", weekly, automatic=True)
                if local.weekday() == 0 and local.date() > anchor and local.time() >= topic.schedule:
                    generate_period_report(self.store, self.settings, topic, local.date() - timedelta(days=1), "weekly")
                if local.day == 1 and local.date() > anchor and local.time() >= topic.schedule:
                    generate_period_report(self.store, self.settings, topic, local.date() - timedelta(days=1), "monthly")
            except ValueError:
                # The UI can replace or pause the topic during this scheduler pass.
                continue

    def tick(self, schedule=True, run_id=None, notify=True, maintenance=True, research=False):
        try:
            with self.store.worker_lock:
                self.store.execute("""UPDATE notifications SET status='uncertain',detail='worker_interrupted'
                                      WHERE status='sending'""")
                if schedule:
                    self.schedule_due(utc_now(), maintenance=maintenance)
                job = self.store.claim(run_id)
                if job:
                    self.run(job)
                if notify:
                    try:
                        send_one(self.store, self.settings, self.client)
                        send_event_alerts(self.store, self.settings, self.client)
                    except ValueError as error:
                        logger.error("notification: %s", error)
                elif research:
                    try:
                        send_one(self.store, self.settings, self.client, manual_only=True)
                    except ValueError as error:
                        logger.error("manual notification: %s", error)
                if not job and research:
                    return tick_research(self.store, self.settings, self.client, self.sources)
                return bool(job)
        except Timeout:
            return False

    def serve(self, schedule=True, maintenance=True):
        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(self.tick, "interval", seconds=15 if schedule and maintenance else 3, max_instances=1,
                          coalesce=True, next_run_time=utc_now(),
                          kwargs={"schedule": schedule, "notify": schedule and maintenance, "maintenance": maintenance,
                                  "research": True})
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown(wait=True)
        finally:
            self.client.close()
