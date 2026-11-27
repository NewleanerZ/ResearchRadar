"""Structured research project archives, paper linking, and protocol-aware comparison matrices."""

from contextlib import nullcontext
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .events import record_event
from .models import Settings, dump, utc_now
from .ranking import Ranker, best_record
from .store import Store, norm, row, write


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1500)
    current_method: str = Field(default="", max_length=1500)
    datasets: list[str] = Field(default_factory=list, max_length=20)
    metrics: list[str] = Field(default_factory=list, max_length=20)
    experiments: list[str] = Field(default_factory=list, max_length=30)
    expected_contributions: str = Field(default="", max_length=1500)
    representative_paper_ids: list[int] = Field(default_factory=list, max_length=20)


def _format_project(saved: dict) -> dict:
    return {
        "id": saved["id"],
        "topic_id": saved["topic_id"],
        "name": saved["name"],
        "question": saved["question"],
        "current_method": saved["current_method"],
        "datasets": json.loads(saved["datasets_json"]),
        "metrics": json.loads(saved["metrics_json"]),
        "experiments": json.loads(saved["experiments_json"]),
        "expected_contributions": saved["expected_contributions"],
        "representative_paper_ids": json.loads(saved["representative_paper_ids_json"]),
        "revision": saved["revision"],
        "updated_at": saved["updated_at"],
    }


def create_project(store: Store, topic_id: str, data: ProjectInput, *, connection=None) -> dict:
    pid = f"proj_{uuid4().hex[:12]}"
    now = utc_now().isoformat()
    with (store.transaction() if connection is None else nullcontext(connection)) as con:
        if not row(con, "SELECT id FROM topics WHERE id=:id", id=topic_id):
            raise ValueError("找不到主题")
        write(
            con,
            """INSERT INTO research_projects
               (id, topic_id, name, question, current_method, datasets_json, metrics_json,
                experiments_json, expected_contributions, representative_paper_ids_json, revision, updated_at)
               VALUES (:id, :topic, :name, :q, :method, :ds, :ms, :exps, :contrib, :reps, 1, :now)""",
            id=pid, topic=topic_id, name=data.name, q=data.question, method=data.current_method,
            ds=dump(data.datasets), ms=dump(data.metrics), exps=dump(data.experiments),
            contrib=data.expected_contributions, reps=dump(data.representative_paper_ids), now=now,
        )
    if connection is not None:
        return _format_project(row(connection, "SELECT * FROM research_projects WHERE id=:id", id=pid))
    return get_project(store, pid)


def update_project(store: Store, project_id: str, expected_revision: int, data: ProjectInput) -> dict:
    now = utc_now().isoformat()
    with store.transaction() as con:
        saved = row(con, "SELECT revision FROM research_projects WHERE id=:id", id=project_id)
        if not saved:
            raise ValueError("找不到科研项目档案")
        if saved["revision"] != expected_revision:
            raise ValueError("科研项目档案已更新，请刷新后重试")
        write(
            con,
            """UPDATE research_projects SET name=:name, question=:q, current_method=:method,
               datasets_json=:ds, metrics_json=:ms, experiments_json=:exps,
               expected_contributions=:contrib, representative_paper_ids_json=:reps,
               revision=revision+1, updated_at=:now
               WHERE id=:id""",
            id=project_id, name=data.name, q=data.question, method=data.current_method,
            ds=dump(data.datasets), ms=dump(data.metrics), exps=dump(data.experiments),
            contrib=data.expected_contributions, reps=dump(data.representative_paper_ids), now=now,
        )
    return get_project(store, project_id)


def get_project(store: Store, project_id: str) -> dict:
    saved = store.one("SELECT * FROM research_projects WHERE id=:id", id=project_id)
    if not saved:
        raise ValueError("找不到科研项目档案")
    return _format_project(saved)


def list_projects(store: Store, topic_id: str = "") -> list[dict]:
    items = store.all(
        "SELECT * FROM research_projects WHERE (:topic='' OR topic_id=:topic) ORDER BY updated_at DESC",
        topic=topic_id,
    )
    return [_format_project(item) for item in items]


def link_project_papers(store: Store, settings: Settings, project_id: str, *, min_score: float = 0.45) -> list[dict]:
    """Match topic papers to the research project and record links + overlap alerts."""
    project = get_project(store, project_id)
    papers = store.papers(project["topic_id"])
    if not papers:
        return []
    proj_text = " ".join([
        project["name"], project["question"], project["current_method"],
        *project["datasets"], *project["metrics"],
    ]).strip()
    proj_tokens = set(norm(proj_text).split())
    ds_norms = [norm(d) for d in project["datasets"] if norm(d)]

    scores: list[float] = []
    if not settings.keyword_only:
        try:
            ranker = Ranker(store, settings)
            texts = [proj_text, *(f"{best_record(p).title}\n{best_record(p).abstract or ''}" for p in papers)]
            vecs = ranker.encode_texts(texts)
            p_vec = vecs[0]
            scores = [max(0.0, min(1.0, sum(a * b for a, b in zip(p_vec, v)))) for v in vecs[1:]]
        except (ImportError, OSError, RuntimeError, ValueError):
            scores = []
    if not scores:
        for p in papers:
            rec = best_record(p)
            toks = set(norm(f"{rec.title} {rec.abstract or ''}").split())
            scores.append(len(proj_tokens & toks) / max(1, min(len(proj_tokens), 12)))

    now = utc_now().isoformat()
    linked = []
    with store.transaction() as con:
        for paper, score in zip(papers, scores):
            rec = best_record(paper)
            body = norm(f"{rec.title} {rec.abstract or ''}")
            ds_hit = any(d in body for d in ds_norms)
            if score < min_score and not ds_hit:
                continue
            relation = (
                "overlapping_work" if score >= 0.68
                else "new_dataset" if (ds_hit and score < 0.55)
                else "potential_baseline"
            )
            notes = f"匹配度 {score:.2f}" + (" · 命中项目数据集" if ds_hit else "")
            write(
                con,
                """INSERT INTO project_paper_links(project_id, paper_id, relation_type, score, notes, created_at)
                   VALUES (:pid, :paper, :rel, :score, :notes, :now)
                   ON CONFLICT(project_id, paper_id) DO UPDATE SET
                   relation_type=excluded.relation_type, score=excluded.score, notes=excluded.notes""",
                pid=project_id, paper=paper["paper_id"], rel=relation, score=round(score, 4), notes=notes, now=now,
            )
            if score >= 0.72:
                record_event(
                    store, project["topic_id"], paper["paper_id"], "project_overlap",
                    f"proj:{project_id}:{paper['paper_id']}",
                    {"title": rec.title, "project": project["name"], "score": round(score, 3), "relation": relation},
                    connection=con,
                )
            linked.append({"paper_id": paper["paper_id"], "title": rec.title, "relation_type": relation,
                           "score": round(score, 4), "notes": notes})
    linked.sort(key=lambda item: item["score"], reverse=True)
    return linked


def project_links(store: Store, project_id: str) -> list[dict]:
    return store.all(
        """SELECT l.*, p.title FROM project_paper_links l
           JOIN papers p ON p.id=l.paper_id
           WHERE l.project_id=:pid ORDER BY l.score DESC""",
        pid=project_id,
    )


def protocol_comparison_matrix(store: Store, project_id: str) -> list[dict]:
    """Group verified deep-read metrics strictly by (dataset, split, metric, protocol)."""
    project = get_project(store, project_id)
    analyses = store.all(
        """SELECT da.paper_id, da.level, da.result_json, da.evidence_json, p.title
           FROM deep_analyses da
           JOIN papers p ON p.id=da.paper_id
           WHERE da.topic_id=:topic
           ORDER BY da.created_at DESC""",
        topic=project["topic_id"],
    )
    groups: dict[tuple, list[dict]] = {}
    seen_paper_protocol: set[tuple] = set()
    for row_item in analyses:
        res = json.loads(row_item["result_json"])
        ev_by_id = {ev["id"]: ev for ev in json.loads(row_item["evidence_json"])}
        for m in res.get("metrics", []):
            protocol_key = (
                (m.get("dataset") or "原文未确认").strip(),
                (m.get("split") or "原文未确认").strip(),
                (m.get("metric") or "原文未确认").strip(),
                (m.get("protocol") or "原文未确认").strip(),
            )
            dedup_key = (row_item["paper_id"], protocol_key)
            if dedup_key in seen_paper_protocol:
                continue
            seen_paper_protocol.add(dedup_key)
            # Only group together when all 4 experimental axes are explicitly confirmed.
            comparable = all(val != "原文未确认" and val != "" for val in protocol_key)
            group_key = (protocol_key, "" if comparable else f"unconfirmed:{row_item['paper_id']}")
            ev = ev_by_id.get(m.get("evidence_id"), {})
            groups.setdefault(group_key, []).append({
                "paper_id": row_item["paper_id"],
                "title": row_item["title"],
                "level": row_item["level"],
                "value": m["value"],
                "evidence_page": ev.get("page"),
                "evidence_url": ev.get("url"),
                "comparable": comparable,
            })
    return [
        {
            "dataset": key[0][0],
            "split": key[0][1],
            "metric": key[0][2],
            "protocol": key[0][3],
            "comparable": all(val != "原文未确认" and val != "" for val in key[0]),
            "entries": entries,
        }
        for key, entries in groups.items()
    ]
