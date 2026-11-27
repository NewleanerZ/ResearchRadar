"""User feedback collection and topic-level preference profiles with freeze/reset controls."""

from collections import Counter
import json
from typing import Literal
from uuid import uuid4

from .models import Settings, dump, fingerprint, utc_now
from .store import Store, norm, row, write

FeedbackAction = Literal["like", "ignore", "hide", "rating"]


def _paper_text(store: Store, paper_id: int) -> str:
    records = store.all("SELECT record FROM paper_sources WHERE paper_id=:pid", pid=paper_id)
    parts = []
    for item in records:
        data = json.loads(item["record"])
        parts.append(f"{data.get('title', '')} {data.get('abstract') or ''}")
    return " ".join(parts).strip()


def _cached_vector(store: Store, settings: Settings, text: str) -> list[float] | None:
    if settings.keyword_only or not text.strip():
        return None
    from .ranking import MODEL

    key = fingerprint([MODEL, settings.embedding_revision, text])
    saved = store.one("SELECT vector FROM embeddings WHERE cache_key=:key", key=key)
    if saved:
        return json.loads(saved["vector"])
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(str(settings.embedding_path()), device="cpu", local_files_only=True)
        vector = model.encode([text], batch_size=1, normalize_embeddings=True).tolist()[0]
        store.execute("INSERT OR IGNORE INTO embeddings VALUES (:key,:vector)", key=key, vector=dump(vector))
        return vector
    except (ImportError, OSError, RuntimeError, ValueError):
        return None


def _average_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    summed = [sum(vec[i] for vec in vectors) for i in range(dim)]
    norm_val = sum(x * x for x in summed) ** 0.5
    if norm_val == 0:
        return []
    return [x / norm_val for x in summed]


def _top_terms(texts: list[str], limit: int = 12) -> list[str]:
    counts: Counter[str] = Counter()
    stop = {"the", "and", "for", "with", "that", "this", "from", "are", "using", "based", "paper", "method", "model"}
    for text in texts:
        tokens = [tok for tok in norm(text).split() if len(tok) > 3 and tok not in stop]
        counts.update(set(tokens))
    return [term for term, _ in counts.most_common(limit)]


def topic_preference_profile(store: Store, topic_id: str) -> dict:
    saved = store.one("SELECT profile_json, frozen, updated_at FROM topic_preferences WHERE topic_id=:topic",
                      topic=topic_id)
    if not saved:
        return {"positive_vector": [], "negative_vector": [], "positive_terms": [], "negative_terms": [],
                "frozen": False, "updated_at": None}
    profile = json.loads(saved["profile_json"])
    profile["frozen"] = bool(saved["frozen"])
    profile["updated_at"] = saved["updated_at"]
    return profile


def hidden_paper_ids(store: Store, topic_id: str) -> set[int]:
    rows_found = store.all(
        """SELECT paper_id, action FROM user_feedback
           WHERE topic_id=:topic ORDER BY created_at ASC, rowid ASC""",
        topic=topic_id,
    )
    hidden = set()
    for item in rows_found:
        if item["action"] == "hide":
            hidden.add(item["paper_id"])
        elif item["action"] == "like":
            hidden.discard(item["paper_id"])
    return hidden


def rebuild_preference_profile(store: Store, topic_id: str, settings: Settings | None = None,
                               *, force: bool = False) -> dict:
    existing = store.one("SELECT frozen FROM topic_preferences WHERE topic_id=:topic", topic=topic_id)
    if existing and existing["frozen"] and not force:
        return topic_preference_profile(store, topic_id)
    settings = settings or Settings.from_env()
    events = store.all(
        """SELECT paper_id, action, rating FROM user_feedback
           WHERE topic_id=:topic ORDER BY created_at ASC, rowid ASC""",
        topic=topic_id,
    )
    favorites = {item["paper_id"] for item in store.all(
        "SELECT paper_id FROM topic_papers WHERE topic_id=:topic AND favorite=1", topic=topic_id
    )}
    state_by_paper: dict[int, str] = {pid: "like" for pid in favorites}
    for item in events:
        if item["action"] in {"like", "ignore", "hide"}:
            state_by_paper[item["paper_id"]] = item["action"]
        elif item["action"] == "rating" and item["rating"] is not None:
            state_by_paper[item["paper_id"]] = "like" if item["rating"] >= 4 else (
                "ignore" if item["rating"] <= 2 else state_by_paper.get(item["paper_id"], "neutral")
            )
    pos_texts, neg_texts = [], []
    pos_vecs, neg_vecs = [], []
    for pid, label in state_by_paper.items():
        text = _paper_text(store, pid)
        if not text:
            continue
        vec = _cached_vector(store, settings, text)
        if label == "like":
            pos_texts.append(text)
            if vec:
                pos_vecs.append(vec)
        elif label in {"ignore", "hide"}:
            neg_texts.append(text)
            if vec:
                neg_vecs.append(vec)
    profile = {
        "positive_vector": _average_vectors(pos_vecs),
        "negative_vector": _average_vectors(neg_vecs),
        "positive_terms": _top_terms(pos_texts),
        "negative_terms": _top_terms(neg_texts),
        "positive_count": len(pos_texts),
        "negative_count": len(neg_texts),
    }
    now = utc_now().isoformat()
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO topic_preferences(topic_id, profile_json, frozen, updated_at)
               VALUES (:topic, :profile, 0, :now)
               ON CONFLICT(topic_id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
            topic=topic_id, profile=dump(profile), now=now,
        )
    return {**profile, "frozen": bool(existing and existing["frozen"]), "updated_at": now}


def record_feedback(store: Store, topic_id: str, paper_id: int, action: FeedbackAction,
                    *, rating: int | None = None, reason: str = "", settings: Settings | None = None) -> dict:
    if action not in {"like", "ignore", "hide", "rating"}:
        raise ValueError("不支持的反馈类型")
    if action == "rating" and (rating is None or not 1 <= rating <= 5):
        raise ValueError("评分必须在 1 到 5 之间")
    if not store.one("SELECT 1 FROM topic_papers WHERE topic_id=:topic AND paper_id=:paper",
                     topic=topic_id, paper=paper_id):
        raise ValueError("当前主题没有这篇论文")
    fid = uuid4().hex
    now = utc_now().isoformat()
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO user_feedback(id, topic_id, paper_id, action, rating, reason, created_at)
               VALUES (:id, :topic, :paper, :action, :rating, :reason, :now)""",
            id=fid, topic=topic_id, paper=paper_id, action=action, rating=rating,
            reason=reason.strip()[:300], now=now,
        )
        if action == "like":
            write(con, "UPDATE topic_papers SET favorite=1 WHERE topic_id=:topic AND paper_id=:paper",
                  topic=topic_id, paper=paper_id)
    profile = rebuild_preference_profile(store, topic_id, settings=settings)
    return {"id": fid, "action": action, "rating": rating, "profile": profile}


def set_preference_frozen(store: Store, topic_id: str, frozen: bool) -> dict:
    now = utc_now().isoformat()
    empty = dump({"positive_vector": [], "negative_vector": [], "positive_terms": [], "negative_terms": [],
                  "positive_count": 0, "negative_count": 0})
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO topic_preferences(topic_id, profile_json, frozen, updated_at)
               VALUES (:topic, :profile, :frozen, :now)
               ON CONFLICT(topic_id) DO UPDATE SET frozen=excluded.frozen, updated_at=excluded.updated_at""",
            topic=topic_id, profile=empty, frozen=int(frozen), now=now,
        )
    return topic_preference_profile(store, topic_id)


def reset_preference(store: Store, topic_id: str) -> dict:
    now = utc_now().isoformat()
    empty = dump({"positive_vector": [], "negative_vector": [], "positive_terms": [], "negative_terms": [],
                  "positive_count": 0, "negative_count": 0})
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO topic_preferences(topic_id, profile_json, frozen, updated_at)
               VALUES (:topic, :profile, 0, :now)
               ON CONFLICT(topic_id) DO UPDATE SET profile_json=excluded.profile_json, frozen=0, updated_at=excluded.updated_at""",
            topic=topic_id, profile=empty, now=now,
        )
    return topic_preference_profile(store, topic_id)


def preference_match_score(profile: dict, text: str, vector: list[float] | None) -> float | None:
    """Compute a [0, 1] personalization score from vector centroids or term overlap."""
    pos_vec = profile.get("positive_vector") or []
    neg_vec = profile.get("negative_vector") or []
    if vector and (pos_vec or neg_vec):
        pos = max(0.0, sum(a * b for a, b in zip(vector, pos_vec))) if pos_vec else 0.5
        neg = max(0.0, sum(a * b for a, b in zip(vector, neg_vec))) if neg_vec else 0.0
        return max(0.0, min(1.0, pos - 0.5 * neg))
    pos_terms = profile.get("positive_terms") or []
    neg_terms = profile.get("negative_terms") or []
    if not pos_terms and not neg_terms:
        return None
    words = set(norm(text).split())
    pos_hit = len(words & set(pos_terms)) / max(1, len(pos_terms)) if pos_terms else 0.5
    neg_hit = len(words & set(neg_terms)) / max(1, len(neg_terms)) if neg_terms else 0.0
    return max(0.0, min(1.0, 0.5 + pos_hit - 0.6 * neg_hit))
