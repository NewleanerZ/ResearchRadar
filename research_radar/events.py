"""Scientific event detection, cooldown/cap enforcement, and alert queuing."""

from datetime import date, timedelta
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import Topic, dump, paper_publication, utc_now
from .store import Store, row, work_date, write
from .v2_sources import extract_code_and_hf_refs, fetch_github_signal, fetch_hf_signal

EVENT_LABELS = {
    "code_released": "代码开源",
    "dataset_released": "数据集发布",
    "venue_accepted": "会议/期刊录用发表",
    "version_updated": "论文版本更新",
    "hotspot_heating": "热点持续升温",
    "project_overlap": "科研项目高相关提醒",
}


def record_event(store: Store, topic_id: str, paper_id: int | None, event_type: str,
                 event_key: str, payload: dict, *, connection=None) -> str | None:
    """Persist a deduplicated scientific event; returns new event id or None if duplicate."""
    key = uuid4().hex
    now = utc_now().isoformat()
    sql = """INSERT OR IGNORE INTO research_events
             (id, topic_id, paper_id, event_type, event_key, payload_json, notified_at, created_at)
             VALUES (:id, :topic, :paper, :etype, :ekey, :payload, NULL, :now)"""
    params = {"id": key, "topic": topic_id, "paper": paper_id, "etype": event_type,
              "ekey": event_key, "payload": dump(payload), "now": now}
    if connection is not None:
        res = write(connection, sql, **params)
        return key if res.rowcount else None
    with store.transaction() as con:
        res = write(con, sql, **params)
        return key if res.rowcount else None


def detect_daily_events(store: Store, topic: Topic, run: dict, result: dict, trend: dict,
                        *, client=None, settings=None, deadline: float | None = None,
                        fetch_external: bool = False) -> list[dict]:
    """Detect research events from daily recommendations, versions, trends, and code/HF links."""
    created = []
    recommendations = result.get("recommendations", [])
    github_token = settings.github_token.get_secret_value() if settings else ""
    hf_token = settings.hf_token.get_secret_value() if settings else ""

    for item in recommendations:
        paper_id = item["paper_id"]
        title = item["title"]
        status = item.get("status")
        venue = item.get("venue")
        if status in {"published", "accepted"} and venue and venue.lower() != "arxiv":
            eid = record_event(
                store, topic.id, paper_id, "venue_accepted",
                f"venue:{topic.id}:{paper_id}:{status}:{venue}",
                {"title": title, "status": status, "venue": venue},
            )
            if eid:
                created.append({"id": eid, "event_type": "venue_accepted", "paper_id": paper_id, "title": title})

        records = store.all("SELECT record FROM paper_sources WHERE paper_id=:pid", pid=paper_id)
        combined_text = " ".join(
            (json.loads(r["record"]).get("abstract") or "") for r in records
        )
        refs = extract_code_and_hf_refs(combined_text)
        for slug in refs["github"][:2]:
            meta = (fetch_github_signal(client, slug, deadline, github_token)
                    if fetch_external and client and deadline else None)
            payload = {"title": title, "repo": slug, "url": f"https://github.com/{slug}"}
            if meta:
                payload.update(stars=meta.get("stars"), latest_release=meta.get("latest_release"))
            eid = record_event(
                store, topic.id, paper_id, "code_released",
                f"code:{topic.id}:{paper_id}:{slug.lower()}",
                payload,
            )
            if eid:
                created.append({"id": eid, "event_type": "code_released", "paper_id": paper_id, "title": title})

        for slug in refs["huggingface_datasets"][:2]:
            meta = (fetch_hf_signal(client, "datasets", slug, deadline, hf_token)
                    if fetch_external and client and deadline else None)
            payload = {"title": title, "dataset": slug, "url": f"https://huggingface.co/datasets/{slug}"}
            if meta:
                payload.update(downloads=meta.get("downloads"), likes=meta.get("likes"))
            eid = record_event(
                store, topic.id, paper_id, "dataset_released",
                f"hf_dataset:{topic.id}:{paper_id}:{slug.lower()}",
                payload,
            )
            if eid:
                created.append({"id": eid, "event_type": "dataset_released", "paper_id": paper_id, "title": title})

    # Version update events for arXiv papers updated after initial v1
    versions = store.all(
        """SELECT tp.paper_id, p.title, pv.external_id, pv.version
           FROM topic_papers tp
           JOIN papers p ON p.id=tp.paper_id
           JOIN paper_sources ps ON ps.paper_id=tp.paper_id AND ps.source='arxiv'
           JOIN paper_versions pv ON pv.source='arxiv' AND pv.external_id=ps.external_id
           WHERE tp.topic_id=:topic AND pv.version NOT IN ('v1', 'metadata')
           ORDER BY pv.first_seen DESC LIMIT 20""",
        topic=topic.id,
    )
    for ver in versions:
        eid = record_event(
            store, topic.id, ver["paper_id"], "version_updated",
            f"ver:{topic.id}:{ver['external_id']}:{ver['version']}",
            {"title": ver["title"], "arxiv_id": ver["external_id"], "version": ver["version"]},
        )
        if eid:
            created.append({"id": eid, "event_type": "version_updated", "paper_id": ver["paper_id"], "title": ver["title"]})

    # Hotspot heating events
    for win in trend.get("windows", []):
        if win.get("heating"):
            eid = record_event(
                store, topic.id, None, "hotspot_heating",
                f"heat:{topic.id}:{run['local_day']}:{win['label']}",
                {"label": win["label"], "count": win["count"], "delta": win.get("delta"), "as_of": run["local_day"]},
            )
            if eid:
                created.append({"id": eid, "event_type": "hotspot_heating", "paper_id": None, "label": win["label"]})
    return created


def scan_library_events(store: Store, topic: Topic, *, limit: int = 120) -> list[dict]:
    """Scan local ingested papers for code releases, datasets, accepted venues, and arXiv updates without network I/O."""
    zone = ZoneInfo(topic.timezone)
    papers = store.papers(topic.id)
    papers.sort(key=lambda item: (work_date(item["records"], zone) or date.min, item["paper_id"]), reverse=True)
    created = []
    for item in papers[:limit]:
        paper_id = item["paper_id"]
        records = item["records"]
        title = records[0].title
        pub = paper_publication(records)
        if pub.status in {"published", "accepted"} and pub.venue and pub.venue.lower() != "arxiv":
            eid = record_event(
                store, topic.id, paper_id, "venue_accepted",
                f"venue:{topic.id}:{paper_id}:{pub.status}:{pub.venue}",
                {"title": title, "status": pub.status, "venue": pub.venue},
            )
            if eid:
                created.append({"id": eid, "event_type": "venue_accepted", "paper_id": paper_id, "title": title})
        combined_text = " ".join((rec.abstract or "") for rec in records)
        refs = extract_code_and_hf_refs(combined_text)
        for slug in refs["github"][:2]:
            eid = record_event(
                store, topic.id, paper_id, "code_released",
                f"code:{topic.id}:{paper_id}:{slug.lower()}",
                {"title": title, "repo": slug, "url": f"https://github.com/{slug}"},
            )
            if eid:
                created.append({"id": eid, "event_type": "code_released", "paper_id": paper_id, "title": title})
        for slug in refs["huggingface_datasets"][:2]:
            eid = record_event(
                store, topic.id, paper_id, "dataset_released",
                f"hf_dataset:{topic.id}:{paper_id}:{slug.lower()}",
                {"title": title, "dataset": slug, "url": f"https://huggingface.co/datasets/{slug}"},
            )
            if eid:
                created.append({"id": eid, "event_type": "dataset_released", "paper_id": paper_id, "title": title})
    versions = store.all(
        """SELECT tp.paper_id, p.title, pv.external_id, pv.version
           FROM topic_papers tp
           JOIN papers p ON p.id=tp.paper_id
           JOIN paper_sources ps ON ps.paper_id=tp.paper_id AND ps.source='arxiv'
           JOIN paper_versions pv ON pv.source='arxiv' AND pv.external_id=ps.external_id
           WHERE tp.topic_id=:topic AND pv.version NOT IN ('v1', 'metadata')
           ORDER BY pv.first_seen DESC LIMIT 20""",
        topic=topic.id,
    )
    for ver in versions:
        eid = record_event(
            store, topic.id, ver["paper_id"], "version_updated",
            f"ver:{topic.id}:{ver['external_id']}:{ver['version']}",
            {"title": ver["title"], "arxiv_id": ver["external_id"], "version": ver["version"]},
        )
        if eid:
            created.append({"id": eid, "event_type": "version_updated", "paper_id": ver["paper_id"], "title": ver["title"]})
    return created


def queue_event_notifications(store: Store, settings, topic: Topic,
                              *, daily_cap: int = 5, cooldown_days: int = 7) -> str | None:
    """Queue a digest alert for unnotified events while enforcing daily caps and cooldown."""
    if not topic.notify or not settings.feishu_webhook.get_secret_value():
        return None
    now = utc_now()
    today_prefix = now.date().isoformat()
    cooldown_since = (now - timedelta(days=cooldown_days)).isoformat()
    with store.transaction() as con:
        sent_today = row(
            con,
            """SELECT COUNT(*) AS n FROM research_events
               WHERE topic_id=:topic AND notified_at IS NOT NULL AND notified_at>=:today""",
            topic=topic.id, today=today_prefix,
        )["n"]
        remaining = max(0, daily_cap - sent_today)
        if remaining == 0:
            return None
        pending = store.all(
            """SELECT * FROM research_events
               WHERE topic_id=:topic AND notified_at IS NULL
               ORDER BY created_at ASC LIMIT 25""",
            topic=topic.id,
        )
        selected = []
        suppressed = []
        for ev in pending:
            if len(selected) >= remaining:
                break
            recent = row(
                con,
                """SELECT 1 FROM research_events
                   WHERE topic_id=:topic AND event_type=:etype
                   AND ((:pid IS NULL AND paper_id IS NULL) OR paper_id=:pid)
                   AND notified_at IS NOT NULL AND notified_at>=:since LIMIT 1""",
                topic=topic.id, etype=ev["event_type"], pid=ev["paper_id"], since=cooldown_since,
            )
            if recent:
                suppressed.append(ev["id"])
            else:
                selected.append(ev)
        if suppressed:
            for eid in suppressed:
                write(con, "UPDATE research_events SET notified_at=:now WHERE id=:id",
                      now="suppressed:" + now.isoformat(), id=eid)
        if not selected:
            return None
        lines = [f"{topic.name} · 科研事件提醒"]
        for ev in selected:
            payload = json.loads(ev["payload_json"])
            label = EVENT_LABELS.get(ev["event_type"], ev["event_type"])
            title = payload.get("title") or payload.get("label") or ""
            extra = payload.get("venue") or payload.get("url") or payload.get("version") or ""
            lines.append(f"- [{label}] {title}" + (f" ({extra})" if extra else ""))
        alert_id = uuid4().hex
        write(
            con,
            """INSERT INTO event_notifications(id, topic_id, channel, status, attempts, next_at, detail, payload, created_at)
               VALUES (:id, :topic, 'feishu', 'pending', 0, :now, NULL, :payload, :now)""",
            id=alert_id, topic=topic.id, now=now.isoformat(), payload="\n".join(lines)[:3800],
        )
        for ev in selected:
            write(con, "UPDATE research_events SET notified_at=:now WHERE id=:id", now=now.isoformat(), id=ev["id"])
        return alert_id


def list_events(store: Store, topic_id: str = "", limit: int = 50) -> list[dict]:
    items = store.all(
        """SELECT * FROM research_events
           WHERE (:topic='' OR topic_id=:topic)
           ORDER BY created_at DESC LIMIT :limit""",
        topic=topic_id, limit=limit,
    )
    return [{**item, "payload": json.loads(item["payload_json"])} for item in items]
