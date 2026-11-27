"""Weekly and monthly research synthesis reports."""

from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sklearn.feature_extraction.text import TfidfVectorizer

from .clustering import CLUSTER_EVENT_LABELS, _CLUSTER_STOP_WORDS, enrich_clusters_with_papers, ensure_cluster_snapshots
from .deep_read import _strip_json_fences
from .delivery import md
from .events import EVENT_LABELS, scan_library_events
from .models import (
    PUBLICATION_LABELS, Settings, Topic, dump, fingerprint, paper_publication, publication_label, utc_now,
)
from .ranking import best_record, request_json_completion
from .service import find_card, library
from .store import Store, norm, work_date, write


def period_key(day: date, report_type: Literal["weekly", "monthly"]) -> str:
    if report_type == "weekly":
        iso = day.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return f"{day.year}-{day.month:02d}"


def _fixed_subtopic_rows(topic: Topic, papers: list[dict]) -> list[tuple[str, int, float, str]]:
    total = max(1, len(papers))
    rows_out = []
    indexed = []
    for item in papers:
        content = " " + norm(" ".join(f"{r.title} {r.abstract or ''}" for r in item["records"])) + " "
        indexed.append((item, content))
    for label, terms in topic.subtopics.items():
        norm_terms = [f" {norm(t)} " for t in terms if norm(t)]
        matched = [item for item, content in indexed if any(t in content for t in norm_terms)]
        rep = best_record(matched[0]).title[:60] if matched else "—"
        rows_out.append((label, len(matched), len(matched) / total, rep))
    rows_out.sort(key=lambda r: r[1], reverse=True)
    return rows_out


def generate_period_report(store: Store, settings: Settings, topic: Topic, day: date,
                           report_type: Literal["weekly", "monthly"] = "weekly",
                           *, force: bool = False, include_ai: bool = False) -> dict:
    """Generate and persist an idempotent weekly or monthly Markdown report."""
    key = period_key(day, report_type)
    existing = store.one(
        "SELECT * FROM period_reports WHERE topic_id=:topic AND report_type=:rtype AND period_key=:pkey",
        topic=topic.id, rtype=report_type, pkey=key,
    )
    if existing and not force:
        return existing

    days = 7 if report_type == "weekly" else 30
    start = day - timedelta(days=days - 1)
    papers = library(store, topic, after=start, before=day)

    def _paper_sort_key(item):
        cites = max((r.citation_count for r in item["records"] if r.citation_count is not None), default=-1)
        return (bool(item["favorite"]), cites, item["date"] or date.min, item["paper_id"])

    papers.sort(key=_paper_sort_key, reverse=True)

    min_cluster_papers = 8 if report_type == "weekly" else 15
    clusters_info = ensure_cluster_snapshots(
        store, settings, topic, day + timedelta(days=1), window_days=days, min_papers=min_cluster_papers,
    )
    scan_library_events(store, topic, limit=120)
    period_paper_ids = {item["paper_id"] for item in papers}
    all_events = store.all(
        """SELECT * FROM research_events WHERE topic_id=:topic
           ORDER BY created_at DESC LIMIT 60""",
        topic=topic.id,
    )
    events = [
        ev for ev in all_events
        if (ev["paper_id"] in period_paper_ids) or ev["created_at"][:10] >= start.isoformat()
    ][:15]

    pub_counts = Counter(paper_publication(item["records"]).status for item in papers)
    pub_summary = " · ".join(
        f"{PUBLICATION_LABELS[st_key]} {cnt} 篇" for st_key, cnt in pub_counts.most_common() if cnt
    ) or "暂无"

    ai_synthesis = None
    if include_ai and papers and settings.llm_model and settings.llm_key.get_secret_value():
        with httpx.Client() as client:
            ai_res = synthesize_topic_landscape(
                store, settings, client, topic, day, window_days=days,
                clusters_info=clusters_info, force=force,
            )
        if ai_res.get("available"):
            ai_synthesis = ai_res.get("synthesis")

    label_cn = "科研周报" if report_type == "weekly" else "科研月报"
    lines = [
        f"# {md(topic.name)} · {label_cn}（{key}）",
        "",
        f"统计区间：{start.isoformat()} 至 {day.isoformat()}（共收录去重论文 **{len(papers)}** 篇 · {pub_summary}）。",
        "",
    ]

    if ai_synthesis:
        lines.extend([
            "## 本期研究态势与跨文献综述",
            "",
            f"> **{md(ai_synthesis.get('headline', ''))}**",
            "",
            md(ai_synthesis.get("overview", "")),
            "",
        ])
        for idx, direction in enumerate(ai_synthesis.get("directions") or [], start=1):
            lines.append(f"### {idx}. {md(direction.get('name', ''))}")
            lines.append(f"- **聚焦问题与思路**：{md(direction.get('summary', ''))}")
            if direction.get("key_methods"):
                lines.append(f"- **典型方法**：{md('、'.join(direction['key_methods']))}")
            if direction.get("challenges"):
                lines.append(f"- **局限/待突破**：{md(direction['challenges'])}")
            lines.append("")
        if ai_synthesis.get("datasets_and_eval"):
            lines.append(f"- **评测与数据集动向**：{md(ai_synthesis['datasets_and_eval'])}")
        for tip in ai_synthesis.get("research_advice") or []:
            lines.append(f"- **研究建议**：{md(tip)}")
        lines.append("")

    lines.extend(["## 重点论文", ""])
    if not papers:
        lines.append("本周期内未收录满足条件的新论文。")
    for item in papers[:10]:
        rec = best_record(item)
        pub = publication_label(paper_publication(item["records"]))
        cites = max((r.citation_count for r in item["records"] if r.citation_count is not None), default=None)
        cite_str = f" · 引用 {cites}" if cites is not None else ""
        tag = "★ 已收藏 · " if item["favorite"] else ""
        lines.append(f"- {tag}[{md(rec.title)}]({rec.url}) · {item['date_label'] or '日期未确认'} · {md(pub)}{cite_str}")
        card = find_card(store, topic.id, item["paper_id"])
        stmt = ((card or {}).get("analysis") or {}).get("summary", {}).get("statement")
        if stmt:
            lines.append(f"  - 要点：{md(stmt)}")

    lines.extend(["", "## 主题趋势与演化", ""])
    if clusters_info["mode"] == "dynamic" and clusters_info["clusters"]:
        lines.extend([
            "| 热点子方向 | 篇数 | 占比 | 较上期 | 状态 | 热度评分 |",
            "| --- | --- | --- | --- | --- | --- |",
        ])
        for c in clusters_info["clusters"]:
            m = c["metrics"]
            score = f"{m['heat_score']}/100" if m.get("heat_score") is not None else (
                f"{round(m['hotspot_index'] * 100)}/100" if m.get("hotspot_index") is not None else m["note"]
            )
            delta = m.get("count_delta")
            delta_str = f"{delta:+d} 篇" if delta is not None else "新发现"
            ev_cn = CLUSTER_EVENT_LABELS.get(c["event_type"], c["event_type"])
            lines.append(f"| {md(c['label'])} | {m['count']} | {m.get('share', 0):.1%} | {delta_str} | {ev_cn} | {score} |")
    else:
        lines.append(clusters_info.get("reason", "当前阶段沿用固定子主题统计。"))
        lines.append("")

    if topic.subtopics and papers:
        sub_rows = _fixed_subtopic_rows(topic, papers)
        lines.extend([
            "",
            "### 固定子主题分布",
            "",
            "| 固定子主题 | 篇数 | 占比 | 代表论文 |",
            "| --- | --- | --- | --- |",
        ])
        for label, cnt, share, rep in sub_rows:
            lines.append(f"| {md(label)} | {cnt} | {share:.1%} | {md(rep)} |")

    lines.extend(["", "## 科研事件汇总", ""])
    if not events:
        lines.append("本周期暂无高优先级科研事件。")
    for ev in events:
        payload = json.loads(ev["payload_json"])
        ev_label = EVENT_LABELS.get(ev["event_type"], ev["event_type"])
        title = payload.get("title") or payload.get("label") or ""
        extra = payload.get("venue") or payload.get("url") or payload.get("version") or ""
        suffix = f"（{extra}）" if extra else ""
        lines.append(f"- **{ev_label}**：{md(title)}{suffix}")

    markdown = "\n".join(lines) + "\n"
    relative = (Path("reports") / topic.id / f"{report_type}-{key}.md").as_posix()
    full_path = store.directory / relative
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(markdown, encoding="utf-8")

    rid = uuid4().hex
    now = utc_now().isoformat()
    meta = {"paper_count": len(papers), "cluster_mode": clusters_info["mode"], "event_count": len(events)}
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO period_reports(id, topic_id, report_type, period_key, path, markdown, meta_json, created_at)
               VALUES (:id, :topic, :rtype, :pkey, :path, :md, :meta, :now)
               ON CONFLICT(topic_id, report_type, period_key) DO UPDATE SET
               path=excluded.path, markdown=excluded.markdown, meta_json=excluded.meta_json, created_at=excluded.created_at""",
            id=rid, topic=topic.id, rtype=report_type, pkey=key, path=relative,
            md=markdown, meta=dump(meta), now=now,
        )
    return store.one(
        "SELECT * FROM period_reports WHERE topic_id=:topic AND report_type=:rtype AND period_key=:pkey",
        topic=topic.id, rtype=report_type, pkey=key,
    )


def period_report_history(store: Store, topic_id: str, report_type: str = "") -> list[dict]:
    return store.all(
        """SELECT * FROM period_reports
           WHERE topic_id=:topic AND (:rtype='' OR report_type=:rtype)
           ORDER BY period_key DESC LIMIT 36""",
        topic=topic_id, rtype=report_type,
    )


_DATASET_STOPWORDS = frozenset({
    "LLM", "LLMs", "GPT", "API", "GPU", "CPU", "NLP", "CNN", "RNN", "GAN", "VAE", "SVM",
    "ViT", "RGB", "RGBD", "MLP", "SGD", "RL", "PCA", "PDF", "DOI", "URL", "III", "IEEE",
    "CVPR", "ICCV", "ECCV", "ICLR", "ICML", "NEURIPS", "AAAI", "ACL", "EMNLP", "THE", "AND",
    "FOR", "WITH", "FROM", "THIS", "THAT", "OUR", "NEW", "TWO", "ONE",
})
_DATASET_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9\-]{2,15}(?:\s+(?:3D|AD|QA|Bench|Net))?)\b")


def build_topic_insights(
    store: Store,
    topic: Topic,
    as_of: date,
    window_days: int = 30,
    clusters_info: dict | None = None,
) -> dict:
    """Compute zero-LLM-token research pulse: growth, venue maturity, hot terms/datasets, and plain Chinese summary."""
    zone = ZoneInfo(topic.timezone)
    start = as_of - timedelta(days=window_days - 1)
    prev_start = start - timedelta(days=window_days)

    cur_papers: list[dict] = []
    prev_papers: list[dict] = []
    for paper in store.papers(topic.id):
        when = work_date(paper["records"], zone)
        if not when:
            continue
        if start <= when <= as_of:
            cur_papers.append(paper)
        elif prev_start <= when < start:
            prev_papers.append(paper)

    total = len(cur_papers)
    prev_total = len(prev_papers)
    delta = total - prev_total
    growth_pct = round(delta / prev_total * 100) if prev_total > 0 else None

    status_counts = Counter()
    venue_counts: Counter[str] = Counter()
    docs_cur: list[str] = []
    dataset_counts: Counter[str] = Counter()

    for paper in cur_papers:
        pub = paper_publication(paper["records"])
        status_counts[pub.status] += 1
        if pub.venue:
            venue_counts[pub.venue] += 1
        rec = best_record(paper)
        text = f"{rec.title}\n{rec.abstract or ''}"
        docs_cur.append(text)
        seen_ds: set[str] = set()
        for m in _DATASET_TOKEN_RE.finditer(text):
            tok = m.group(1).strip()
            if tok.upper() in _DATASET_STOPWORDS or tok in _DATASET_STOPWORDS:
                continue
            if any(hint in tok for hint in ("-", "3D", "Bench", "QA", "AD", "MVTec", "VisA", "ImageNet", "CIFAR", "ScanNet", "ShapeNet")):
                seen_ds.add(tok)
        for ds in seen_ds:
            dataset_counts[ds] += 1

    formal_count = status_counts["published"] + status_counts["accepted"]
    preprint_count = status_counts["preprint"]
    formal_share = round(formal_count / max(1, total) * 100) if total else 0

    top_terms: list[dict] = []
    if len(docs_cur) >= 2:
        try:
            vec = TfidfVectorizer(
                ngram_range=(1, 2),
                stop_words=_CLUSTER_STOP_WORDS,
                token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9\-]{1,}\b",
                max_features=400,
            )
            mat = vec.fit_transform(docs_cur)
            names = np.array(vec.get_feature_names_out())
            scores = np.asarray(mat.mean(axis=0)).ravel()
            ranked_idx = scores.argsort()[::-1][:24]
            picked_terms: list[tuple[str, int]] = []
            norm_docs = [f" {norm(d)} " for d in docs_cur]
            for idx in ranked_idx:
                term = str(names[idx]).strip()
                if not term or scores[idx] <= 0:
                    continue
                words = set(term.split())
                if any(words <= set(existing.split()) for existing, _ in picked_terms):
                    continue
                cnt = sum(1 for d in norm_docs if f" {term} " in d)
                if cnt >= 2:
                    picked_terms.append((term, cnt))
                if len(picked_terms) >= 8:
                    break
            top_terms = [
                {"term": term, "count": cnt, "share": round(cnt / max(1, total) * 100)}
                for term, cnt in picked_terms
            ]
        except ValueError:
            top_terms = []

    top_venues = [{"venue": v, "count": c} for v, c in venue_counts.most_common(6)]
    top_datasets = [{"name": ds, "count": c} for ds, c in dataset_counts.most_common(8) if c >= 2]

    event_rows = store.all(
        "SELECT event_type, COUNT(*) AS c FROM research_events WHERE topic_id=:t AND created_at >= :since GROUP BY event_type",
        t=topic.id, since=start.isoformat(),
    )
    event_counts = {r["event_type"]: r["c"] for r in event_rows}
    code_count = event_counts.get("code_released", 0)
    accepted_ev_count = event_counts.get("venue_accepted", 0)

    clusters = (clusters_info or {}).get("clusters") or []
    top_cluster = next((c for c in clusters if not c.get("metrics", {}).get("low_sample")), clusters[0] if clusters else None)

    parts: list[str] = []
    if total == 0:
        headline = f"近 {window_days} 天内库内暂无该主题文献记录，可尝试补录近 90–180 天顶会或切换更宽时间窗口。"
    else:
        delta_txt = ""
        if growth_pct is not None:
            delta_txt = f"，较前 {window_days} 天{'上升' if delta >= 0 else '回落'} {abs(growth_pct)}%（{delta:+d} 篇）"
        elif prev_total == 0:
            delta_txt = "（当前为新增积累期，暂无前序基线）"
        parts.append(f"近 {window_days} 天收录去重文献 {total} 篇{delta_txt}")
        parts.append(f"顶会/期刊正式发表及录用占 {formal_share}%（{formal_count} 篇），预印本 {preprint_count} 篇")
        if top_cluster:
            parts.append(f"最活跃方向为「{top_cluster['label']}」（占 {round(top_cluster['metrics'].get('share', 0) * 100)}%）")
        if code_count:
            parts.append(f"含代码开源动态 {code_count} 项")
        headline = "；".join(parts) + "。"

    return {
        "window_days": window_days,
        "start": start.isoformat(),
        "as_of": as_of.isoformat(),
        "total": total,
        "prev_total": prev_total,
        "delta": delta,
        "growth_pct": growth_pct,
        "formal_count": formal_count,
        "formal_share": formal_share,
        "preprint_count": preprint_count,
        "code_released_count": code_count,
        "venue_accepted_count": accepted_ev_count,
        "top_cluster": {
            "label": top_cluster["label"],
            "count": top_cluster["metrics"]["count"],
            "share": round(top_cluster["metrics"].get("share", 0) * 100),
        } if top_cluster else None,
        "top_venues": top_venues,
        "top_terms": top_terms,
        "top_datasets": top_datasets,
        "headline": headline,
    }


SYNTHESIS_VERSION = "topic-synthesis-v1"


class SynthesisDirection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=900)
    key_methods: list[str] = Field(default_factory=list, max_length=8)
    challenges: str | None = Field(default=None, max_length=600)
    paper_ids: list[int] = Field(default_factory=list, max_length=10)


class TopicSynthesis(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headline: str = Field(min_length=1, max_length=400)
    overview: str = Field(min_length=1, max_length=1600)
    directions: list[SynthesisDirection] = Field(default_factory=list, max_length=6)
    datasets_and_eval: str | None = Field(default=None, max_length=800)
    research_advice: list[str] = Field(default_factory=list, max_length=6)


def _attach_synthesis_papers(store: Store, topic_id: str, synthesis: dict, allowed_ids: set[int] | None = None) -> dict:
    papers_by_id = {p["paper_id"]: p for p in store.papers(topic_id)}
    enriched_dirs = []
    for d in synthesis.get("directions") or []:
        valid_ids = [
            int(pid) for pid in (d.get("paper_ids") or [])
            if int(pid) in papers_by_id and (allowed_ids is None or int(pid) in allowed_ids)
        ]
        rep_papers = []
        for pid in valid_ids[:5]:
            p = papers_by_id[pid]
            rec = best_record(p)
            pub = paper_publication(p["records"])
            rep_papers.append({
                "paper_id": pid,
                "title": rec.title,
                "date": p.get("date_label"),
                "publication_label": publication_label(pub),
            })
        enriched_dirs.append({**d, "paper_ids": valid_ids, "papers": rep_papers})
    return {**synthesis, "directions": enriched_dirs}


def latest_topic_synthesis(store: Store, topic_id: str, window_days: int | None = None) -> dict | None:
    if window_days is not None:
        row = store.one(
            """SELECT * FROM topic_syntheses WHERE topic_id=:t AND window_days=:w
               ORDER BY created_at DESC LIMIT 1""",
            t=topic_id, w=window_days,
        )
    else:
        row = store.one(
            "SELECT * FROM topic_syntheses WHERE topic_id=:t ORDER BY created_at DESC LIMIT 1",
            t=topic_id,
        )
    if not row:
        return None
    synthesis = _attach_synthesis_papers(store, topic_id, json.loads(row["result_json"]))
    return {
        "id": row["id"],
        "topic_id": row["topic_id"],
        "window_days": row["window_days"],
        "model": row.get("model"),
        "created_at": row["created_at"],
        "synthesis": synthesis,
        "cached": True,
    }


def synthesize_topic_landscape(
    store: Store,
    settings: Settings,
    client,
    topic: Topic,
    as_of: date,
    *,
    window_days: int = 30,
    clusters_info: dict | None = None,
    force: bool = False,
) -> dict:
    """Single-call LLM cross-paper literature & hotspot synthesis with persistent cache."""
    start = as_of - timedelta(days=window_days - 1)
    papers = library(store, topic, after=start, before=as_of)
    if not papers:
        return {"available": False, "reason": f"近 {window_days} 天内暂无论文，无法生成整理总结。"}

    def _sort_key(item):
        cites = max((r.citation_count for r in item["records"] if r.citation_count is not None), default=-1)
        return (bool(item["favorite"]), cites, item["date"] or date.min, item["paper_id"])

    papers.sort(key=_sort_key, reverse=True)

    # Pick balanced representatives across clusters first, then fill by citation/recency up to 24 papers
    chosen: list[dict] = []
    seen_ids: set[int] = set()
    papers_by_id = {p["paper_id"]: p for p in papers}
    for c in (clusters_info or {}).get("clusters") or []:
        for pid in (c.get("representative_ids") or [])[:4]:
            if pid in papers_by_id and pid not in seen_ids:
                chosen.append(papers_by_id[pid])
                seen_ids.add(pid)
    for p in papers:
        if len(chosen) >= 24:
            break
        if p["paper_id"] not in seen_ids:
            chosen.append(p)
            seen_ids.add(p["paper_id"])

    cache_key = fingerprint([
        SYNTHESIS_VERSION,
        topic.id,
        str(window_days),
        ",".join(str(p["paper_id"]) for p in chosen),
        settings.llm_model or "",
    ])
    existing = store.one("SELECT * FROM topic_syntheses WHERE cache_key=:k", k=cache_key)
    if existing and not force:
        return {
            "available": True,
            "id": existing["id"],
            "topic_id": topic.id,
            "window_days": window_days,
            "model": existing.get("model"),
            "created_at": existing["created_at"],
            "synthesis": _attach_synthesis_papers(store, topic.id, json.loads(existing["result_json"]), seen_ids),
            "cached": True,
        }

    if not settings.llm_model or not settings.llm_key.get_secret_value():
        return {"available": False, "reason": "尚未配置摘要大模型（LLM），请先在设置中填写模型与密钥。"}

    digest_items = []
    for p in chosen:
        rec = best_record(p)
        pub = publication_label(paper_publication(p["records"]))
        digest_items.append({
            "paper_id": p["paper_id"],
            "title": rec.title[:160],
            "venue": pub,
            "date": p.get("date_label") or "",
            "abstract": (" ".join((rec.abstract or "").split())[:260]),
        })

    cluster_hints = [
        {"label": c["label"], "count": c["metrics"]["count"], "rep_ids": (c.get("representative_ids") or [])[:4]}
        for c in ((clusters_info or {}).get("clusters") or [])[:6]
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "你是资深科研文献综述专家。请根据给定研究主题、聚类提示和近期代表论文摘要，用清晰易懂的中文对当前研究内容、热点流派与趋势做整理总结。\n"
                "严格返回如下结构的单个 JSON 对象（不要额外输出 type 字段，不要输出 Markdown 代码块）：\n"
                '{"headline":"一句话概括本阶段研究核心进展","overview":"3-5句概括研究聚焦的问题与演进趋势",'
                '"directions":[{"name":"中文热点方向名称","summary":"2-3句说明该方向解决什么问题及主思路",'
                '"key_methods":["方法1","方法2"],"challenges":"主要瓶颈或未解问题","paper_ids":[123,456]}],'
                '"datasets_and_eval":"1-2句总结主流数据集与评价方式变化","research_advice":["建议1","建议2"]}\n'
                "要求：directions 给 3-5 个清晰不重叠的研究子方向；paper_ids 只能填写输入中真实存在的 paper_id 整数。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic.name,
                    "description": topic.description,
                    "window_days": window_days,
                    "total_papers_in_window": len(papers),
                    "cluster_hints": cluster_hints,
                    "papers": digest_items,
                },
                ensure_ascii=False,
            ),
        },
    ]

    parsed: TopicSynthesis | None = None
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            attempt_msgs = messages if attempt == 0 else [
                *messages,
                {"role": "user", "content": "请严格按指定 JSON 键名重新输出，确保 headline、overview 和 directions 非空且无额外字段。"},
            ]
            reply = request_json_completion(client, settings, attempt_msgs, max_tokens=2200, timeout=45.0)
            cleaned = _strip_json_fences(reply["choices"][0]["message"]["content"])
            raw_obj = json.loads(cleaned)
            if isinstance(raw_obj.get("research_advice"), str):
                raw_obj["research_advice"] = [raw_obj["research_advice"]]
            for d in raw_obj.get("directions") or []:
                if isinstance(d, dict):
                    if isinstance(d.get("challenges"), list):
                        d["challenges"] = "；".join(str(x) for x in d["challenges"] if x)
                    if isinstance(d.get("key_methods"), str):
                        d["key_methods"] = [d["key_methods"]]
                    d["paper_ids"] = [int(x) for x in (d.get("paper_ids") or []) if str(x).isdigit()]
            parsed = TopicSynthesis.model_validate(raw_obj)
            break
        except (ValueError, KeyError, IndexError, ValidationError, Exception) as err:
            last_err = err
    if parsed is None:
        return {"available": False, "reason": f"AI 整理总结生成失败（{type(last_err).__name__}）"}

    syn_dict = _attach_synthesis_papers(store, topic.id, parsed.model_dump(), seen_ids)
    sid = uuid4().hex
    now = utc_now().isoformat()
    model_name = settings.llm_model or None
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO topic_syntheses(id, topic_id, window_days, cache_key, result_json, model, created_at)
               VALUES (:id, :topic, :win, :key, :res, :model, :now)
               ON CONFLICT(cache_key) DO UPDATE SET
               id=excluded.id, result_json=excluded.result_json, model=excluded.model, created_at=excluded.created_at""",
            id=sid, topic=topic.id, win=window_days, key=cache_key,
            res=dump(parsed.model_dump()), model=model_name, now=now,
        )
    return {
        "available": True,
        "id": sid,
        "topic_id": topic.id,
        "window_days": window_days,
        "model": model_name,
        "created_at": now,
        "synthesis": syn_dict,
        "cached": False,
    }
