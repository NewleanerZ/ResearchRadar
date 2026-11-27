"""Dynamic semantic topic clustering, cross-window lifecycle matching, and hotspot index."""

from datetime import date, timedelta
import json
from math import log
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.cluster import AgglomerativeClustering, KMeans
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics import silhouette_score

from .models import Settings, Topic, dump, paper_publication, publication_label
from .ranking import Ranker, best_record
from .store import Store, work_date, write

_EXTRA_TERM_STOPS = frozenset({
    "paper", "method", "methods", "approach", "approaches", "propose", "proposed", "based",
    "using", "use", "used", "result", "results", "experiment", "experiments", "experimental",
    "performance", "model", "models", "framework", "study", "analysis", "novel", "existing",
    "show", "shows", "demonstrate", "demonstrates", "evaluate", "evaluation", "task", "tasks",
    "data", "work", "improve", "improved", "achieve", "achieves", "high", "effective",
})
_CLUSTER_STOP_WORDS = sorted(ENGLISH_STOP_WORDS | _EXTRA_TERM_STOPS)


def _normalize_vec(vec: np.ndarray) -> list[float]:
    norm_val = float(np.linalg.norm(vec))
    return (vec / norm_val).tolist() if norm_val > 0 else vec.tolist()


def _cluster_terms(docs: list[str], labels: np.ndarray, cluster_id: int, top_k: int = 4) -> list[str]:
    if not docs:
        return []
    try:
        vec = TfidfVectorizer(
            ngram_range=(1, 2),
            stop_words=_CLUSTER_STOP_WORDS,
            token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9\-]{1,}\b",
            max_features=600,
        )
        mat = vec.fit_transform(docs)
        names = np.array(vec.get_feature_names_out())
        mask = labels == cluster_id
        mean_scores = np.asarray(mat[mask].mean(axis=0)).ravel()
        top_idx = mean_scores.argsort()[::-1][: top_k * 4]
        raw_terms = [str(names[i]).strip() for i in top_idx if mean_scores[i] > 0]
        # Prefer bigrams / distinct phrases and drop unigrams subsumed by an already-chosen bigram
        picked: list[str] = []
        ordered = [t for _, t in sorted(enumerate(raw_terms), key=lambda kv: (" " in kv[1], -kv[0]), reverse=True)]
        for term in ordered:
            words = set(term.split())
            if any(words <= set(existing.split()) for existing in picked):
                continue
            picked.append(term)
            if len(picked) >= top_k:
                break
        return picked
    except ValueError:
        return []


def _choose_k(reduced: np.ndarray, n_samples: int) -> np.ndarray:
    max_k = min(8, max(2, n_samples // 5))
    best_labels = None
    best_score = -1.0
    for k in range(2, max_k + 1):
        model = AgglomerativeClustering(n_clusters=k)
        labels = model.fit_predict(reduced)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(reduced, labels))
        if score > best_score:
            best_score = score
            best_labels = labels
    if best_labels is None:
        return np.zeros(n_samples, dtype=int)
    return _rebalance_labels(reduced, best_labels)


def _rebalance_labels(reduced: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Subdivide overly broad mega-clusters and fold isolated 1-2 paper outliers into nearest major clusters."""
    n_samples = len(labels)
    if n_samples < 12:
        return labels
    out = labels.copy()
    next_id = int(out.max()) + 1

    # 1. Subdivide any single cluster holding >45% of a >=12 corpus when enough distinct points exist
    unique_ids, counts = np.unique(out, return_counts=True)
    for cid, cnt in zip(unique_ids, counts):
        if cnt >= 12 and cnt > 0.45 * n_samples:
            idxs = np.where(out == cid)[0]
            sub_vecs = reduced[idxs]
            distinct = len(np.unique(np.round(sub_vecs, 4), axis=0))
            target_k = min(5, max(3, int(cnt) // 25)) if cnt >= 45 else (3 if cnt >= 15 else 2)
            if distinct >= target_k + 1:
                sub_labels = KMeans(n_clusters=target_k, n_init=10, random_state=2026).fit_predict(sub_vecs)
                if len(set(sub_labels)) >= 2:
                    for local_i, global_i in enumerate(idxs):
                        out[global_i] = next_id + int(sub_labels[local_i])
                    next_id += target_k

    # 2. Fold tiny 1-2 paper outlier clusters into nearest larger cluster when at least 2 healthy clusters remain
    unique_ids, counts = np.unique(out, return_counts=True)
    major_ids = [int(cid) for cid, cnt in zip(unique_ids, counts) if cnt >= 3]
    if len(major_ids) >= 2:
        centroids = {
            cid: reduced[out == cid].mean(axis=0)
            for cid in major_ids
        }
        for cid, cnt in zip(unique_ids, counts):
            if int(cid) not in centroids and cnt <= 2:
                for idx in np.where(out == cid)[0]:
                    pt = reduced[idx]
                    best_cid = min(major_ids, key=lambda mcid: float(np.linalg.norm(pt - centroids[mcid])))
                    out[idx] = best_cid

    # Re-number 0..k-1 deterministically by size descending
    unique_ids, counts = np.unique(out, return_counts=True)
    order = [int(cid) for cid, _ in sorted(zip(unique_ids, counts), key=lambda kv: kv[1], reverse=True)]
    remap = {old: new for new, old in enumerate(order)}
    return np.array([remap[int(x)] for x in out], dtype=int)


def discover_clusters(store: Store, settings: Settings, topic: Topic, as_of: date,
                      *, window_days: int = 30, min_papers: int = 15) -> dict:
    """Cluster stable corpus embeddings using PCA + AgglomerativeClustering and match prior snapshots."""
    if settings.keyword_only:
        return {"mode": "fallback_fixed", "reason": "未启用语义向量，沿用固定子主题统计", "clusters": []}
    version = topic.query_version()
    zone = ZoneInfo(topic.timezone)
    start = as_of - timedelta(days=window_days)
    prev_start = start - timedelta(days=window_days)

    current_papers = []
    prior_paper_ids: set[int] = set()
    for paper in store.papers(topic.id, version):
        when = work_date(paper["records"], zone)
        if not when:
            continue
        if start <= when < as_of:
            current_papers.append(paper)
        elif prev_start <= when < start:
            prior_paper_ids.add(paper["paper_id"])

    if len(current_papers) < min_papers:
        return {"mode": "fallback_fixed", "reason": f"样本量不足（{len(current_papers)} < {min_papers}），沿用固定子主题", "clusters": []}

    docs = []
    author_sets = []
    for paper in current_papers:
        rec = best_record(paper)
        docs.append(f"{rec.title}\n{rec.abstract or ''}")
        author_sets.append(set(rec.authors[:3]))

    try:
        vectors = np.array(Ranker(store, settings).encode_texts(docs), dtype=float)
    except (ImportError, OSError, RuntimeError, ValueError):
        return {"mode": "fallback_fixed", "reason": "语义模型不可用，沿用固定子主题", "clusters": []}

    if len(current_papers) > 20 and vectors.shape[1] > 16:
        n_comp = min(16, len(current_papers) - 1)
        reduced = PCA(n_components=n_comp, random_state=2026).fit_transform(vectors)
    else:
        reduced = vectors

    labels = _choose_k(reduced, len(current_papers))
    unique_ids = sorted(set(int(x) for x in labels))

    # Load previous snapshot for lifecycle matching
    prev_snap = store.all(
        """SELECT * FROM dynamic_clusters
           WHERE topic_id=:topic AND version=:ver AND as_of<:day
           ORDER BY as_of DESC LIMIT 20""",
        topic=topic.id, ver=version, day=as_of.isoformat(),
    )
    prev_by_day: list[dict] = []
    if prev_snap:
        latest_prev_day = prev_snap[0]["as_of"]
        prev_by_day = [row for row in prev_snap if row["as_of"] == latest_prev_day]

    raw_clusters = []
    total_cur = len(current_papers)
    total_prev = max(1, sum(json.loads(p["metrics_json"]).get("count", 0) for p in prev_by_day)) if prev_by_day else max(1, len(prior_paper_ids))

    for cid in unique_ids:
        idxs = [i for i, lbl in enumerate(labels) if int(lbl) == cid]
        centroid = _normalize_vec(vectors[idxs].mean(axis=0))
        terms = _cluster_terms(docs, labels, cid, top_k=4)
        label = " · ".join(terms[:3]) or f"方向 {cid + 1}"

        # Rank representative papers by favorite -> citations -> recency
        def _rep_rank(i: int):
            p = current_papers[i]
            cites = max((r.citation_count for r in p["records"] if r.citation_count is not None), default=-1)
            dt = work_date(p["records"], zone) or date.min
            return (bool(p.get("favorite")), cites, dt, p["paper_id"])

        ranked_idxs = sorted(idxs, key=_rep_rank, reverse=True)
        rep_ids = [current_papers[i]["paper_id"] for i in ranked_idxs[:8]]
        diversity_authors = len({a for i in idxs for a in author_sets[i]})
        pub_count = 0
        venue_counts: dict[str, int] = {}
        for i in idxs:
            pub = paper_publication(current_papers[i]["records"])
            if pub.status in {"published", "accepted"}:
                pub_count += 1
            if pub.venue:
                venue_counts[pub.venue] = venue_counts.get(pub.venue, 0) + 1
        top_venues = [v for v, _ in sorted(venue_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]]

        raw_clusters.append({
            "cluster_id": f"c{cid}",
            "label": label,
            "terms": terms,
            "centroid": centroid,
            "count": len(idxs),
            "share": len(idxs) / max(1, total_cur),
            "author_diversity": diversity_authors / max(1, len(idxs)),
            "published_share": pub_count / max(1, len(idxs)),
            "top_venues": top_venues,
            "representative_ids": rep_ids,
        })

    # Match against previous clusters
    prior_matches: dict[str, list[str]] = {}
    matched_priors_for_current: dict[str, list[dict]] = {}
    for cur in raw_clusters:
        matches = []
        for prev in prev_by_day:
            pvec = json.loads(prev["centroid_json"])
            sim = sum(a * b for a, b in zip(cur["centroid"], pvec))
            if sim >= 0.72:
                matches.append({**prev, "sim": sim})
                prior_matches.setdefault(prev["cluster_id"], []).append(cur["cluster_id"])
        matched_priors_for_current[cur["cluster_id"]] = sorted(matches, key=lambda x: x["sim"], reverse=True)

    clusters_out = []
    for cur in raw_clusters:
        matched = matched_priors_for_current[cur["cluster_id"]]
        prior_count = sum(json.loads(m["metrics_json"]).get("count", 0) for m in matched)
        prior_share = prior_count / total_prev if matched else 0.0
        growth = log((cur["count"] + 1) / (prior_count + 1))
        share_delta = cur["share"] - prior_share
        emerging = 1.0 if not matched else 0.2
        persistence = min(1.0, len(matched) * 0.5 + (0.5 if cur["count"] >= 5 and growth > 0 else 0.0))

        # Normalized components in [0, 1]
        g_norm = max(0.0, min(1.0, (growth + 1.0) / 2.0))
        v_norm = max(0.0, min(1.0, (share_delta + 0.25) / 0.5))
        e_norm = emerging
        p_norm = persistence
        d_norm = max(0.0, min(1.0, cur["author_diversity"] / 2.0))
        hotspot_h = 0.30 * g_norm + 0.20 * v_norm + 0.20 * e_norm + 0.15 * p_norm + 0.15 * d_norm

        if len(matched) > 1:
            event_type = "merged"
        elif matched and len(prior_matches.get(matched[0]["cluster_id"], [])) > 1:
            event_type = "split"
        elif not matched:
            event_type = "emerging"
        elif growth > 0.15 and share_delta > 0:
            event_type = "growing"
        elif growth < -0.15:
            event_type = "declining"
        else:
            event_type = "stable"

        insufficient = cur["count"] < 5
        metrics = {
            "count": cur["count"],
            "prior_count": prior_count if matched else None,
            "prev_count": prior_count if matched else None,
            "count_delta": (cur["count"] - prior_count) if matched else None,
            "growth": round(growth, 4),
            "share": round(cur["share"], 4),
            "share_delta": round(share_delta, 4),
            "hotspot_index": None if insufficient else round(hotspot_h, 4),
            "heat_score": None if insufficient else round(hotspot_h * 100),
            "published_share": round(cur["published_share"], 4),
            "top_venues": cur["top_venues"],
            "low_sample": insufficient,
            "note": "证据不足" if insufficient else "可评估",
        }
        clusters_out.append({
            "cluster_id": cur["cluster_id"],
            "label": cur["label"],
            "terms": cur["terms"],
            "event_type": event_type,
            "metrics": metrics,
            "representative_ids": cur["representative_ids"],
            "_centroid": cur["centroid"],
        })

    with store.transaction() as con:
        write(con, "DELETE FROM dynamic_clusters WHERE topic_id=:topic AND version=:ver AND as_of=:day",
              topic=topic.id, ver=version, day=as_of.isoformat())
        for item in clusters_out:
            write(
                con,
                """INSERT INTO dynamic_clusters
                   (topic_id, version, as_of, cluster_id, label, terms_json, centroid_json, metrics_json, event_type, representative_ids_json)
                   VALUES (:topic, :ver, :day, :cid, :label, :terms, :centroid, :metrics, :etype, :reps)""",
                topic=topic.id, ver=version, day=as_of.isoformat(), cid=item["cluster_id"],
                label=item["label"], terms=dump(item["terms"]), centroid=dump(item["_centroid"]),
                metrics=dump(item["metrics"]), etype=item["event_type"], reps=dump(item["representative_ids"]),
            )
    for item in clusters_out:
        item.pop("_centroid", None)
    clusters_out.sort(key=lambda c: (c["metrics"]["hotspot_index"] or -1.0, c["metrics"]["count"]), reverse=True)
    return {"mode": "dynamic", "as_of": as_of.isoformat(), "window_days": window_days,
            "total_papers": total_cur, "clusters": clusters_out}


CLUSTER_EVENT_LABELS = {
    "emerging": "新兴方向",
    "growing": "持续增长",
    "declining": "热度回落",
    "merged": "方向融合",
    "split": "方向分化",
    "stable": "稳定活跃",
}


def ensure_cluster_snapshots(store: Store, settings: Settings, topic: Topic, as_of: date,
                             *, window_days: int = 30, min_papers: int = 15) -> dict:
    """Ensure a prior baseline snapshot exists when older papers allow lifecycle comparison."""
    version = topic.query_version()
    prev_day = as_of - timedelta(days=window_days)
    has_prev = store.one(
        """SELECT 1 FROM dynamic_clusters
           WHERE topic_id=:topic AND version=:ver AND as_of<:day LIMIT 1""",
        topic=topic.id, ver=version, day=as_of.isoformat(),
    )
    if not has_prev:
        discover_clusters(store, settings, topic, prev_day, window_days=window_days, min_papers=min_papers)
    return discover_clusters(store, settings, topic, as_of, window_days=window_days, min_papers=min_papers)


def latest_dynamic_clusters(store: Store, topic_id: str) -> dict:
    rows_found = store.all(
        """SELECT * FROM dynamic_clusters WHERE topic_id=:topic
           AND as_of=(SELECT MAX(as_of) FROM dynamic_clusters WHERE topic_id=:topic)
           ORDER BY cluster_id""",
        topic=topic_id,
    )
    if not rows_found:
        return {"mode": "none", "clusters": []}
    clusters = [
        {
            "cluster_id": r["cluster_id"],
            "label": r["label"],
            "terms": json.loads(r["terms_json"]),
            "event_type": r["event_type"],
            "metrics": json.loads(r["metrics_json"]),
            "representative_ids": json.loads(r["representative_ids_json"]),
        }
        for r in rows_found
    ]
    clusters.sort(key=lambda c: (c["metrics"].get("hotspot_index") or -1.0, c["metrics"].get("count", 0)), reverse=True)
    return {
        "mode": "dynamic",
        "as_of": rows_found[0]["as_of"],
        "version": rows_found[0]["version"],
        "clusters": clusters,
    }


def enrich_clusters_with_papers(store: Store, topic_id: str, cluster_result: dict) -> dict:
    """Attach representative paper metadata (title, date, venue, citations, pdf status) without calling any LLM."""
    clusters = cluster_result.get("clusters") or []
    if not clusters:
        return cluster_result
    needed_ids = {pid for c in clusters for pid in (c.get("representative_ids") or [])}
    if not needed_ids:
        return cluster_result
    from .service import find_card

    papers_by_id = {p["paper_id"]: p for p in store.papers(topic_id) if p["paper_id"] in needed_ids}
    pdf_rows = {
        r["paper_id"]: r["status"]
        for r in store.all("SELECT paper_id, status FROM paper_pdf_checks")
        if r["paper_id"] in needed_ids
    }
    card_rows = {}
    for pid in needed_ids:
        card = find_card(store, topic_id, pid)
        stmt = ((card or {}).get("analysis") or {}).get("summary", {}).get("statement")
        if stmt:
            card_rows[pid] = stmt
    enriched = []
    for c in clusters:
        reps = []
        for pid in c.get("representative_ids") or []:
            p = papers_by_id.get(pid)
            if not p:
                continue
            rec = best_record(p)
            pub = paper_publication(p["records"])
            cites = max((r.citation_count for r in p["records"] if r.citation_count is not None), default=None)
            reps.append({
                "paper_id": pid,
                "title": rec.title,
                "date": p.get("date_label"),
                "publication_status": pub.status,
                "publication_label": publication_label(pub),
                "venue": pub.venue,
                "citation_count": cites,
                "has_pdf": pdf_rows.get(pid) == "available",
                "pdf_status": pdf_rows.get(pid, "unchecked"),
                "summary_statement": card_rows.get(pid),
                "abstract_snippet": (" ".join((rec.abstract or "").split())[:220]) if rec.abstract else None,
            })
        enriched.append({**c, "representative_papers": reps})
    return {**cluster_result, "clusters": enriched}
