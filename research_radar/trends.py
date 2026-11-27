from datetime import date, timedelta
from math import log
from zoneinfo import ZoneInfo

from .store import Store, norm, work_date


def covered(intervals, start: date, end: date) -> bool:
    current = start.isoformat()
    for low, high in sorted(intervals):
        if low > current:
            break
        current = max(current, high)
        if current >= end.isoformat():
            return True
    return current >= end.isoformat()


def trend_snapshot(store: Store, run, topic, daily_status):
    day = date.fromisoformat(run["local_day"])
    intervals = {}
    coverage = {}
    for source in ("arxiv", "openalex"):
        scans = store.all("""SELECT s.range_start,s.range_end,r.kind,r.local_day FROM scans s JOIN runs r ON r.id=s.run_id
            WHERE r.topic_id=:topic AND r.version=:version AND s.source=:source AND s.status='complete'
            AND (s.stream='main' OR s.stream LIKE 'main:%') AND r.local_day<=:day""",
            topic=topic.id, version=run["version"], source=source, day=day.isoformat())
        intervals[source] = [(scan["range_start"], min(scan["range_end"], scan["local_day"])
                              if scan["kind"] == "daily" else scan["range_end"]) for scan in scans]
        coverage[source] = {"current_scan": daily_status.get(source, "unknown"),
                            "ranges": [[low, high] for low, high in sorted(set(intervals[source]))]}
    dated = []
    for paper in store.papers(topic.id, run["version"]):
        when = work_date(paper["records"], ZoneInfo(topic.timezone))
        if when and day - timedelta(days=180) <= when < day:
            content = " ".join(f"{record.title} {record.abstract or ''}" for record in paper["records"])
            dated.append((when, f" {norm(content)} ", paper["paper_id"]))
    groups = [("全部", [])] + [(label, [f" {norm(term)} " for term in terms])
                             for label, terms in topic.subtopics.items()]
    snapshots = []
    for days in (7, 30, 90):
        start, older = day - timedelta(days=days), day - timedelta(days=2 * days)
        current_complete = all(covered(ranges, start, day) and daily_status.get(source) == "complete"
                               for source, ranges in intervals.items())
        comparable = current_complete and all(covered(ranges, older, start) for ranges in intervals.values())
        current = [(content, key) for when, content, key in dated if start <= when < day]
        previous = [(content, key) for when, content, key in dated if older <= when < start]
        for label, terms in groups:
            current_hits = [key for content, key in current if not terms or any(term in content for term in terms)]
            previous_hits = [key for content, key in previous if not terms or any(term in content for term in terms)]
            count, prior = len(current_hits), len(previous_hits)
            share, old_share = count / max(1, len(current)), prior / max(1, len(previous))
            growth = log((count + 1) / (prior + 1)) if comparable else None
            delta = share - old_share if comparable else None
            rising = (days == 30 and bool(terms) and comparable and count >= 5 and growth > 0 and delta > 0)
            snapshots.append({
                "label": label, "days": days, "count": count, "prior": prior if comparable else None,
                "growth": growth, "share": share if current_complete else None, "delta": delta,
                "coverage_complete": current_complete, "comparable": comparable, "representative_ids": current_hits[:5],
                "weekly_candidate": rising, "heating": False,
            })
    if day.weekday() == 0:
        import json
        last = store.one("SELECT snapshot FROM trends WHERE topic_id=:topic AND version=:version AND local_day=:day",
                         topic=topic.id, version=run["version"], day=str(day - timedelta(days=7)))
        prior_labels = {item["label"] for item in json.loads(last["snapshot"])["windows"]
                        if item["weekly_candidate"]} if last else set()
        for item in snapshots:
            item["heating"] = item["weekly_candidate"] and item["label"] in prior_labels
    return {"version": run["version"], "as_of": str(day), "coverage": coverage, "windows": snapshots}
