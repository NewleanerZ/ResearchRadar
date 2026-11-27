"""Authenticated FastAPI REST/SSE service layer and SPA host for ResearchRadar."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import hmac
import json
import os
from pathlib import Path
import time
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from .activity import timeline
from .clustering import (
    CLUSTER_EVENT_LABELS, discover_clusters, enrich_clusters_with_papers, ensure_cluster_snapshots,
    latest_dynamic_clusters,
)
from .conversation import (
    actions, cancel_job, cancel_turn, compact_run, confirm_action, conversations, expire_turn, job_card,
    new_conversation, queue_turn, research_status, run_turn, submit_turn, turns,
)
from .deep_read import deep_read_paper, translate_and_summarize_abstract
from .delivery import retry_notification
from .events import EVENT_LABELS, list_events, scan_library_events
from .feedback import record_feedback, reset_preference, set_preference_frozen, topic_preference_profile
from .gap_workflow import gap_history, run_gap_analysis
from .mcp_client import add_mcp_server, discover_mcp_tools, list_mcp_servers, remove_mcp_server, set_mcp_server_enabled
from .models import (
    CORE_SOURCES, EXTRA_SOURCES, SOURCE_LABELS, Publication, Settings, Topic, paper_publication,
    publication_label, utc_now,
)
from .period_reports import (
    build_topic_insights, generate_period_report, latest_topic_synthesis, period_report_history,
    synthesize_topic_landscape,
)
from .project_memory import ProjectMemory, project_memory, save_project_memory
from .projects import (
    ProjectInput, create_project, link_project_papers, list_projects, project_links, protocol_comparison_matrix,
    update_project,
)
from .rag import hybrid_search
from .ranking import best_record
from .research import MAX_PDF_BYTES, pdf_candidates, safe_pdf_url
from .research_control import ResearchAdjustment, adjustments, queue_adjustment
from .service import (
    RUN_LABELS, daily_overview, draft_topic, enqueue_backfill, enqueue_daily, find_card, library, open_store,
    report_history, run_history, sample_topics, trend_history,
)
from .settings_ui import ENV_PATH, SECRET_SPECS, embedding_diagnostics, mask_secret, save_env_updates, warm_embedding_weights
from .sources import catalog_applies
from .store import Store, norm, work_date
from .zotero import pull_zotero_collection, push_favorites_to_zotero

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
NON_SECRET_ENV_KEYS = {
    "RADAR_LLM_BASE_URL", "RADAR_LLM_MODEL", "RADAR_THINKING_LEVEL", "RADAR_STREAM_CHAT",
    "RADAR_LLM_INPUT_PRICE", "RADAR_LLM_OUTPUT_PRICE", "RADAR_KEYWORD_ONLY", "RADAR_RERANKER_ENABLED",
    "RADAR_RERANKER_MODEL", "RADAR_CHAT_MAX_CALLS", "RADAR_CHAT_MAX_SECONDS", "RADAR_EXTRA_SOURCES",
    "RADAR_CROSSREF_MAILTO", "RADAR_PAGE_LIMIT", "ZOTERO_USER_ID", "RADAR_API_HOST", "RADAR_API_PORT",
    "HF_ENDPOINT",
}
ALLOWED_ENV_KEYS = NON_SECRET_ENV_KEYS | {key for key, _, _ in SECRET_SPECS}


class TopicPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    keywords: list[str] | None = None
    excluded: list[str] | None = None
    expansions: list[str] | None = None
    proposed_expansions: list[str] | None = None
    categories: list[str] | None = None
    subtopics: dict[str, list[str]] | None = None
    timezone: str | None = None
    schedule: str | None = None
    enabled: bool | None = None
    archived: bool | None = None
    notify: bool | None = None
    revision: int = Field(ge=0)


class TopicDraftPayload(BaseModel):
    description: str = Field(min_length=1, max_length=1500)


class MemoryPayload(BaseModel):
    revision: int = Field(ge=0)
    venues: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    research_protocol: str = ""
    main_only: bool = False


class BackfillPayload(BaseModel):
    start: date
    through: date


class ReadingStatePayload(BaseModel):
    topic_id: str
    favorite: bool | None = None
    is_read: bool | None = None


class FreezePayload(BaseModel):
    frozen: bool


class FeedbackPayload(BaseModel):
    topic_id: str
    action: Literal["like", "ignore", "hide", "rating"]
    rating: int | None = None
    reason: str = ""


class DeepReadPayload(BaseModel):
    topic_id: str
    level: Literal["L2", "L3"] = "L2"
    fetch_pdf: bool = False


class PrecheckPayload(BaseModel):
    topic_id: str = ""
    force: bool = False
    limit: int = Field(default=120, ge=1, le=500)


class AbstractNotePayload(BaseModel):
    topic_id: str
    force: bool = False


class PeriodReportPayload(BaseModel):
    topic_id: str
    report_type: Literal["weekly", "monthly"]
    force: bool = False
    include_ai: bool = False


class TopicSynthesisPayload(BaseModel):
    window_days: int = Field(default=30, ge=7, le=180)
    force: bool = False


class ZoteroSyncPayload(BaseModel):
    topic_id: str
    collection_key: str = ""


class ConversationCreatePayload(BaseModel):
    topic_id: str | None = None


class ChatPayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=4500)
    conversation_id: str | None = None
    topic_id: str | None = None


class ChatTurnQueuePayload(BaseModel):
    conversation_id: str
    prompt: str = Field(min_length=1, max_length=4500)


class ActionConfirmPayload(BaseModel):
    approve: bool = True


class ResearchPayload(BaseModel):
    topic_id: str | None = None
    question: str = Field(min_length=1, max_length=1500)


class ResearchAdjustPayload(BaseModel):
    conversation_id: str
    focus: str | None = None
    venues: list[str] | None = None
    main_only: bool | None = None
    paper: str | None = None


class ResearchCancelPayload(BaseModel):
    conversation_id: str


class GapPayload(BaseModel):
    topic_id: str
    project_id: str | None = None


class EnvUpdatePayload(BaseModel):
    updates: dict[str, str] = Field(default_factory=dict)
    clear_keys: list[str] = Field(default_factory=list)


class WarmEmbeddingPayload(BaseModel):
    hf_endpoint: str = ""


class McpServerCreatePayload(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)


class McpServerTogglePayload(BaseModel):
    enabled: bool


def _error_response(status_code: int, code: str, message: str, *, retryable: bool = False, request_id: str = ""):
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "retryable": retryable, "request_id": request_id or uuid4().hex},
    )


def _is_loopback_request(cfg: Settings, request: Request) -> bool:
    if cfg.api_host.strip().lower() not in LOOPBACK_HOSTS:
        return False
    client_host = (request.client.host if request.client else "") or ""
    return client_host.strip().lower() in LOOPBACK_HOSTS


def _serialize_turn(db: Store, turn: dict, jobs_by_turn: dict[str, list[dict]]) -> dict:
    turn_id = turn["id"]
    turn_actions = [
        {
            "id": a["id"],
            "kind": a["kind"],
            "status": a["status"],
            "preview": json.loads(a["preview"] or "{}"),
            "receipt": json.loads(a["receipt"] or "{}") if a["receipt"] else None,
        }
        for a in actions(db, turn_id)
    ]
    return {
        "id": turn_id,
        "conversation_id": turn["conversation_id"],
        "prompt": turn["prompt"],
        "answer": turn["answer"],
        "reasoning": turn.get("reasoning"),
        "partial_answer": turn.get("partial_answer"),
        "status": turn["status"],
        "events": json.loads(turn.get("events") or "[]"),
        "actions": turn_actions,
        "research_jobs": jobs_by_turn.get(turn_id, []),
        "timeline": timeline(db, "chat", turn_id, 100),
        "created_at": turn["created_at"],
    }


def _serialize_job(db: Store, job: dict) -> dict:
    spec = json.loads(job["spec"] or "{}")
    checkpoint = json.loads(job["checkpoint"] or "{}")
    return {
        **job_card(job),
        "conversation_id": job.get("conversation_id"),
        "turn_id": job.get("turn_id"),
        "topic": spec.get("topic"),
        "after": spec.get("after"),
        "before": spec.get("before"),
        "scope": spec.get("scope") or {},
        "project_memory": spec.get("project_memory") or {},
        "candidate_count": len(checkpoint.get("candidates") or {}),
        "pdf_count": len(checkpoint.get("pdfs") or {}),
        "markdown": job.get("markdown"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "adjustments": adjustments(db, job["id"]),
        "timeline": timeline(db, "research", job["id"], 100),
    }


def create_app(
    store: Store | None = None,
    settings: Settings | None = None,
    *,
    env_path: Path = ENV_PATH,
    web_dist: Path = WEB_DIST,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    db = store or open_store(cfg)
    app = FastAPI(title="ResearchRadar API", version="0.4.0")
    chat_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="radar-api-chat")
    runtime_cfg = [cfg]

    def live_settings() -> Settings:
        return runtime_cfg[0] if settings is not None else Settings.from_env()

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": f"http_{exc.status_code}", "message": str(exc.detail)}
        return _error_response(
            exc.status_code,
            detail.get("code", f"http_{exc.status_code}"),
            detail.get("message", str(exc.detail)),
            retryable=exc.status_code >= 500,
            request_id=getattr(request.state, "request_id", ""),
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return _error_response(400, "invalid_request", str(exc), request_id=getattr(request.state, "request_id", ""))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error_response(422, "validation_error", str(exc.errors()[:3]), request_id=getattr(request.state, "request_id", ""))

    def require_auth(request: Request, authorization: str | None = Header(default=None)):
        current_cfg = live_settings()
        expected = current_cfg.api_token.get_secret_value()
        if not expected:
            if _is_loopback_request(current_cfg, request):
                return
            raise HTTPException(status_code=401, detail={"code": "api_token_unconfigured", "message": "未配置 RADAR_API_TOKEN"})
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "无效的认证凭据"})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        current_cfg = live_settings()
        expected = current_cfg.api_token.get_secret_value()
        loopback = _is_loopback_request(current_cfg, request)
        return {
            "auth_required": bool(expected) or not loopback,
            "token_configured": bool(expected),
            "loopback_bypass": (not bool(expected)) and loopback,
        }

    # --- Topics & Project Memory ---
    @app.get("/api/topics", dependencies=[Depends(require_auth)])
    def api_list_topics():
        rows = db.all("SELECT id, config, revision FROM topics ORDER BY id")
        return {
            "topics": [
                {**Topic.model_validate_json(r["config"]).model_dump(mode="json"), "revision": r["revision"]}
                for r in rows
            ]
        }

    @app.post("/api/topics/sample", dependencies=[Depends(require_auth)])
    def api_seed_sample_topics():
        existing = {topic.id for topic in db.topics()}
        added = []
        for topic in sample_topics():
            if topic.id not in existing:
                db.save_topic(topic)
                added.append(topic.id)
        return {"added": added}

    @app.post("/api/topics/draft", dependencies=[Depends(require_auth)])
    def api_draft_topic(body: TopicDraftPayload):
        with httpx.Client() as client:
            topic = draft_topic(body.description, live_settings(), client)
        return {"topic": topic.model_dump(mode="json")}

    @app.post("/api/topics", dependencies=[Depends(require_auth)])
    def api_create_topic(topic: Topic):
        db.save_topic(topic, 0)
        saved, rev = db.topic(topic.id)
        return {"topic": saved.model_dump(mode="json"), "revision": rev}

    @app.patch("/api/topics/{topic_id}", dependencies=[Depends(require_auth)])
    def api_patch_topic(topic_id: str, patch: TopicPatch):
        current, _ = db.topic(topic_id)
        updates = patch.model_dump(exclude={"revision"}, exclude_none=True)
        updated = Topic.model_validate({**current.model_dump(), **updates})
        db.save_topic(updated, patch.revision)
        saved, rev = db.topic(topic_id)
        return {"topic": saved.model_dump(mode="json"), "revision": rev}

    @app.get("/api/topics/{topic_id}/memory", dependencies=[Depends(require_auth)])
    def api_get_memory(topic_id: str):
        db.topic(topic_id)
        rev, mem = project_memory(db, topic_id)
        return {"revision": rev, "memory": mem.model_dump()}

    @app.put("/api/topics/{topic_id}/memory", dependencies=[Depends(require_auth)])
    def api_save_memory(topic_id: str, body: MemoryPayload):
        mem = ProjectMemory(
            venues=body.venues,
            excluded=body.excluded,
            datasets=body.datasets,
            research_protocol=body.research_protocol,
            main_only=body.main_only,
        )
        new_rev = save_project_memory(db, topic_id, body.revision, mem)
        return {"revision": new_rev, "memory": mem.model_dump()}

    @app.get("/api/topics/{topic_id}/overview", dependencies=[Depends(require_auth)])
    def api_topic_overview(topic_id: str):
        topic, rev = db.topic(topic_id)
        run, report = daily_overview(db, topic_id)
        events = list_events(db, topic_id, limit=12)
        active_jobs = [
            _serialize_job(db, j)
            for j in research_status(db)
            if j["status"] in {"queued", "running"} and json.loads(j["spec"] or "{}").get("topic") == topic_id
        ]
        current_cfg = live_settings()
        available_sources = [
            {"id": key, "label": SOURCE_LABELS[key]}
            for key in (*CORE_SOURCES, *(k for k in current_cfg.catalog_sources() if catalog_applies(k, topic)))
        ]
        return {
            "topic": {**topic.model_dump(mode="json"), "revision": rev},
            "latest_run": run,
            "latest_report": report,
            "events": events,
            "event_labels": EVENT_LABELS,
            "active_jobs": active_jobs,
            "backfill_sources": available_sources,
        }

    @app.post("/api/topics/{topic_id}/runs", status_code=202, dependencies=[Depends(require_auth)])
    def api_enqueue_run(topic_id: str):
        run_id = enqueue_daily(db, topic_id)
        return {"run_id": run_id, "status": db.one("SELECT status FROM runs WHERE id=:id", id=run_id)["status"]}

    @app.post("/api/topics/{topic_id}/backfill", status_code=202, dependencies=[Depends(require_auth)])
    def api_enqueue_backfill(topic_id: str, body: BackfillPayload):
        run_id = enqueue_backfill(db, topic_id, body.start, body.through, settings=live_settings())
        return {"run_id": run_id, "status": db.one("SELECT status FROM runs WHERE id=:id", id=run_id)["status"]}

    # --- Runs, Events, Activity ---
    @app.get("/api/runs", dependencies=[Depends(require_auth)])
    def api_list_runs(topic_id: str = "", run_id: str | None = None):
        items = run_history(db, topic_id, run_id)
        enriched = []
        for item in items:
            scans = db.all(
                "SELECT source,stream,range_start,range_end,status,pages,received,detail FROM scans WHERE run_id=:id ORDER BY source,stream",
                id=item["id"],
            )
            enriched.append({
                **item,
                "usage": json.loads(item["usage"] or "{}"),
                "result": json.loads(item["result"] or "{}"),
                "scans": scans,
                "timeline": timeline(db, "run", item["id"], 60),
            })
        return {"runs": enriched, "labels": RUN_LABELS}

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_auth)])
    def api_get_run(run_id: str):
        items = run_history(db, run_id=run_id)
        if not items:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到运行记录"})
        item = items[0]
        scans = db.all(
            "SELECT source,stream,range_start,range_end,status,pages,received,detail FROM scans WHERE run_id=:id ORDER BY source,stream",
            id=item["id"],
        )
        return {
            **item,
            "usage": json.loads(item["usage"] or "{}"),
            "result": json.loads(item["result"] or "{}"),
            "scans": scans,
            "timeline": timeline(db, "run", item["id"], 100),
        }

    @app.post("/api/runs/{run_id}/retry", dependencies=[Depends(require_auth)])
    def api_retry_run(run_id: str):
        db.retry(run_id)
        return {"run_id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/retry-notification", dependencies=[Depends(require_auth)])
    def api_retry_notification(run_id: str):
        retry_notification(db, run_id)
        return {"run_id": run_id, "status": "pending"}

    @app.get("/api/topics/{topic_id}/events", dependencies=[Depends(require_auth)])
    def api_topic_events(topic_id: str, limit: int = 25):
        return {"events": list_events(db, topic_id, limit=min(100, max(1, limit))), "labels": EVENT_LABELS}

    @app.get("/api/activity/{scope}/{target}", dependencies=[Depends(require_auth)])
    def api_activity(scope: str, target: str, limit: int = 100):
        return {"events": timeline(db, scope, target, min(200, max(1, limit)))}

    # --- Papers, PDF, RAG & Preference ---
    @app.get("/api/papers", dependencies=[Depends(require_auth)])
    def api_papers(
        topic_id: str,
        q: str = "",
        from_date: date | None = Query(default=None, alias="from"),
        to_date: date | None = Query(default=None, alias="to"),
        favorite: bool = False,
        unread: bool = False,
        pdf_available: bool = False,
        source: str = "",
        limit: int = 50,
        offset: int = 0,
    ):
        topic, _ = db.topic(topic_id)
        all_items = library(db, topic, q, from_date, to_date, favorite, unread)
        pdf_counts = {
            "available": sum(1 for item in all_items if item.get("pdf_status") == "available"),
            "unavailable": sum(1 for item in all_items if item.get("pdf_status") == "unavailable"),
            "unchecked": sum(1 for item in all_items if item.get("pdf_status", "unchecked") == "unchecked"),
        }
        items = [item for item in all_items if not pdf_available or item.get("pdf_status") == "available"]
        if source:
            wanted_sources = {s.strip() for s in source.split(",") if s.strip()}
            items = [item for item in items if any(r.source in wanted_sources for r in item["records"])]
        available_sources = [
            {"id": src, "label": SOURCE_LABELS[src]}
            for src in SOURCE_LABELS
            if any(r.source == src for item in items for r in item["records"])
        ]
        deep_ids = {
            row["paper_id"]
            for row in db.all("SELECT DISTINCT paper_id FROM deep_analyses WHERE topic_id=:t", t=topic_id)
        }
        sliced = items[max(0, offset):max(0, offset) + min(100, max(1, limit))]
        return {
            "total": len(items),
            "pdf_counts": pdf_counts,
            "available_sources": available_sources,
            "papers": [
                {
                    "paper_id": p["paper_id"],
                    "title": p["title"],
                    "date": p["date_label"],
                    "favorite": bool(p["favorite"]),
                    "is_read": bool(p["is_read"]),
                    "publication": (pub := paper_publication(p["records"])).model_dump(),
                    "publication_label": publication_label(pub),
                    "citation_count": max((r.citation_count for r in p["records"] if r.citation_count is not None), default=None),
                    "has_deep_analysis": p["paper_id"] in deep_ids,
                    "has_pdf": p.get("pdf_status") == "available",
                    "pdf_status": p.get("pdf_status", "unchecked"),
                    "pdf_strategy": p.get("pdf_strategy"),
                    "pdf_reason": p.get("pdf_reason"),
                    "pdf_checked_at": p.get("pdf_checked_at"),
                    "authors": best_record(p).authors[:8],
                    "records": [r.model_dump(mode="json") for r in p["records"]],
                }
                for p in sliced
            ],
        }

    @app.post("/api/papers/precheck", dependencies=[Depends(require_auth)])
    def api_precheck_papers(body: PrecheckPayload):
        from .pdf_explorer import precheck_library_pdfs

        if body.topic_id:
            db.topic(body.topic_id)
        return precheck_library_pdfs(
            db, live_settings(), body.topic_id or None, force=body.force, limit=body.limit,
        )

    @app.get("/api/papers/{paper_id}", dependencies=[Depends(require_auth)])
    def api_paper_detail(paper_id: int, topic_id: str):
        topic, _ = db.topic(topic_id)
        match = next((p for p in library(db, topic) if p["paper_id"] == paper_id), None)
        if not match:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到论文"})
        deep = db.one(
            "SELECT level, result_json, evidence_json FROM deep_analyses WHERE topic_id=:t AND paper_id=:p ORDER BY created_at DESC LIMIT 1",
            t=topic_id, p=paper_id,
        )
        abstract_note = db.one(
            "SELECT translation, summary, model, created_at FROM paper_abstract_notes WHERE paper_id=:p",
            p=paper_id,
        )
        chunks = db.all(
            "SELECT id, section, page_start, page_end, text FROM paper_chunks WHERE paper_id=:p ORDER BY chunk_index LIMIT 20",
            p=paper_id,
        )
        pub = paper_publication(match["records"])
        return {
            "paper_id": paper_id,
            "title": match["title"],
            "date": match["date_label"],
            "favorite": bool(match["favorite"]),
            "is_read": bool(match["is_read"]),
            "publication": pub.model_dump(),
            "publication_label": publication_label(pub),
            "has_pdf": match.get("pdf_status") == "available",
            "pdf_status": match.get("pdf_status", "unchecked"),
            "pdf_strategy": match.get("pdf_strategy"),
            "pdf_reason": match.get("pdf_reason"),
            "pdf_checked_at": match.get("pdf_checked_at"),
            "pdf_urls": ([match["pdf_url"]] if match.get("pdf_url") else []) + pdf_candidates(match["records"]),
            "records": [r.model_dump(mode="json") for r in match["records"]],
            "card": find_card(db, topic_id, paper_id),
            "abstract_note": {
                "translation": abstract_note["translation"],
                "summary": abstract_note["summary"],
                "model": abstract_note["model"],
                "created_at": abstract_note["created_at"],
            } if abstract_note else None,
            "deep_analysis": {
                "level": deep["level"],
                "analysis": json.loads(deep["result_json"]),
                "evidence": json.loads(deep["evidence_json"]),
            } if deep else None,
            "rag_chunks": chunks,
        }

    @app.patch("/api/papers/{paper_id}/state", dependencies=[Depends(require_auth)])
    def api_paper_state(paper_id: int, body: ReadingStatePayload):
        db.set_reading_state(body.topic_id, paper_id, body.favorite, body.is_read)
        row = db.one("SELECT favorite, is_read FROM topic_papers WHERE topic_id=:t AND paper_id=:p",
                     t=body.topic_id, p=paper_id)
        return {"paper_id": paper_id, "favorite": bool(row["favorite"]), "is_read": bool(row["is_read"])}

    @app.get("/api/papers/{paper_id}/pdf", dependencies=[Depends(require_auth)])
    def api_paper_pdf(paper_id: int, topic_id: str):
        from .pdf_explorer import (
            cached_pdf_urls, explore_pdf_sources, mark_pdf_unavailable_if_confirmed, save_pdf_check,
        )

        topic, _ = db.topic(topic_id)
        match = next((p for p in library(db, topic) if p["paper_id"] == paper_id), None)
        if not match:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到论文"})
        preferred = cached_pdf_urls(db, paper_id)
        with httpx.Client() as client:
            explored = explore_pdf_sources(
                client, live_settings(), title=match["title"], records=match["records"],
                preferred_urls=preferred, return_bytes=True, deadline=time.monotonic() + 32.0,
            )
        if explored.get("found") and explored.get("pdf_bytes"):
            save_pdf_check(db, paper_id, "available", pdf_url=explored["pdf_url"], strategy=explored.get("strategy"))
            return Response(
                content=explored["pdf_bytes"],
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="paper-{paper_id}.pdf"'},
            )
        mark_pdf_unavailable_if_confirmed(db, paper_id, explored, fallback_reason="暂未找到公开 PDF")
        raise HTTPException(
            status_code=404,
            detail={"code": "pdf_unavailable", "message": explored.get("reason", "暂未找到公开 PDF")},
        )

    @app.post("/api/papers/{paper_id}/abstract-note", dependencies=[Depends(require_auth)])
    def api_abstract_note(paper_id: int, body: AbstractNotePayload):
        topic, _ = db.topic(body.topic_id)
        match = next((p for p in library(db, topic) if p["paper_id"] == paper_id), None)
        if not match:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到论文"})
        with httpx.Client() as client:
            return translate_and_summarize_abstract(
                db, live_settings(), client, paper_id, match["title"], match["records"], force=body.force,
            )

    @app.post("/api/papers/{paper_id}/feedback", dependencies=[Depends(require_auth)])
    def api_paper_feedback(paper_id: int, body: FeedbackPayload):
        return record_feedback(db, body.topic_id, paper_id, body.action, rating=body.rating,
                               reason=body.reason, settings=live_settings())

    @app.post("/api/papers/{paper_id}/deep-read", dependencies=[Depends(require_auth)])
    def api_deep_read(paper_id: int, body: DeepReadPayload):
        topic, _ = db.topic(body.topic_id)
        match = next((p for p in library(db, topic) if p["paper_id"] == paper_id), None)
        if not match:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到论文"})
        with httpx.Client() as client:
            return deep_read_paper(
                db, live_settings(), client, topic, paper_id, match["records"],
                level=body.level, fetch_pdf=body.fetch_pdf,
            )

    @app.get("/api/rag/search", dependencies=[Depends(require_auth)])
    def api_rag_search(q: str, topic_id: str = "", limit: int = 10):
        if not q.strip():
            return {"hits": []}
        hits = hybrid_search(db, live_settings(), q.strip(), topic_id=topic_id or None, limit=min(30, max(1, limit)))
        return {"hits": hits}

    @app.get("/api/topics/{topic_id}/preference", dependencies=[Depends(require_auth)])
    def api_get_preference(topic_id: str):
        db.topic(topic_id)
        return topic_preference_profile(db, topic_id)

    @app.post("/api/topics/{topic_id}/preference/freeze", dependencies=[Depends(require_auth)])
    def api_freeze_preference(topic_id: str, body: FreezePayload):
        db.topic(topic_id)
        return set_preference_frozen(db, topic_id, body.frozen)

    @app.delete("/api/topics/{topic_id}/preference", dependencies=[Depends(require_auth)])
    def api_reset_preference(topic_id: str):
        db.topic(topic_id)
        return reset_preference(db, topic_id)

    # --- Trends & Reports ---
    @app.get("/api/topics/{topic_id}/trends", dependencies=[Depends(require_auth)])
    def api_trends(topic_id: str, window_days: int = 30, refresh_clusters: bool = False):
        topic, _ = db.topic(topic_id)
        zone = ZoneInfo(topic.timezone)
        today_local = utc_now().astimezone(zone).date()
        fixed = trend_history(db, topic_id)
        if refresh_clusters:
            dynamic = ensure_cluster_snapshots(
                db, live_settings(), topic, today_local + timedelta(days=1), window_days=window_days,
            )
            scan_library_events(db, topic, limit=200)
        else:
            dynamic = latest_dynamic_clusters(db, topic_id)
        enriched_dynamic = enrich_clusters_with_papers(db, topic_id, dynamic)
        insights = build_topic_insights(db, topic, today_local, window_days=window_days, clusters_info=enriched_dynamic)

        all_papers = db.papers(topic_id)
        effective_subtopics = dict(topic.subtopics)
        if not effective_subtopics and insights.get("top_terms"):
            effective_subtopics = {t["term"]: [t["term"]] for t in insights["top_terms"][:5]}

        subtopic_terms = {
            label: [f" {norm(term)} " for term in terms if norm(term)]
            for label, terms in effective_subtopics.items()
        }
        dated_papers = []
        for paper in all_papers:
            when = work_date(paper["records"], zone)
            if not when:
                continue
            content = " " + norm(" ".join(f"{r.title} {r.abstract or ''}" for r in paper["records"])) + " "
            matched = [lbl for lbl, terms in subtopic_terms.items() if any(t in content for t in terms)]
            dated_papers.append((when, matched))

        live_trends = {}
        for days in (7, 30, 90, 180):
            start = today_local - timedelta(days=days - 1)
            buckets: dict[str, dict] = {}
            totals = {lbl: 0 for lbl in subtopic_terms}
            window_total = 0
            for when, matched in dated_papers:
                if not (start <= when <= today_local):
                    continue
                window_total += 1
                bkey = (when - timedelta(days=when.weekday())).isoformat() if days > 30 else when.isoformat()
                row = buckets.setdefault(bkey, {"as_of": bkey, **{lbl: 0 for lbl in subtopic_terms}})
                if not subtopic_terms:
                    row["全部"] = row.get("全部", 0) + 1
                for lbl in matched:
                    row[lbl] = row.get(lbl, 0) + 1
                    totals[lbl] += 1
                if subtopic_terms and not matched:
                    row["其他"] = row.get("其他", 0) + 1
            series_keys = list(subtopic_terms.keys()) or (["全部"] if window_total else [])
            if subtopic_terms and any("其他" in r for r in buckets.values()):
                series_keys = [*series_keys, "其他"]
            live_trends[str(days)] = {
                "total": window_total,
                "subtopics": series_keys,
                "series": [buckets[k] for k in sorted(buckets)],
                "bars": [{"label": lbl, "count": cnt} for lbl, cnt in totals.items() if cnt > 0]
                or [{"label": lbl, "count": cnt} for lbl, cnt in totals.items()],
            }

        return {
            "fixed_trends": fixed,
            "dynamic_clusters": enriched_dynamic,
            "live_trends": live_trends,
            "insights": insights,
            "latest_synthesis": latest_topic_synthesis(db, topic_id, window_days),
            "events": list_events(db, topic_id, limit=30),
            "event_labels": EVENT_LABELS,
            "cluster_labels": CLUSTER_EVENT_LABELS,
        }

    @app.post("/api/topics/{topic_id}/synthesis", dependencies=[Depends(require_auth)])
    def api_topic_synthesis(topic_id: str, body: TopicSynthesisPayload):
        topic, _ = db.topic(topic_id)
        today_local = utc_now().astimezone(ZoneInfo(topic.timezone)).date()
        dynamic = latest_dynamic_clusters(db, topic_id)
        with httpx.Client() as client:
            return synthesize_topic_landscape(
                db, live_settings(), client, topic, today_local,
                window_days=body.window_days, clusters_info=dynamic, force=body.force,
            )

    @app.get("/api/reports", dependencies=[Depends(require_auth)])
    def api_reports(topic_id: str, type: Literal["daily", "weekly", "monthly"] = "daily"):
        if type == "daily":
            rows = report_history(db, topic_id)
            return {
                "reports": [
                    {**r, "result": json.loads(r.get("result") or "{}")}
                    for r in rows
                ]
            }
        return {"reports": period_report_history(db, topic_id, type)}

    @app.post("/api/reports/period", dependencies=[Depends(require_auth)])
    def api_generate_period_report(body: PeriodReportPayload):
        topic, _ = db.topic(body.topic_id)
        today = utc_now().astimezone(ZoneInfo(topic.timezone)).date()
        return generate_period_report(
            db, live_settings(), topic, today, body.report_type,
            force=body.force, include_ai=body.include_ai,
        )

    @app.get("/api/reports/{report_id}", dependencies=[Depends(require_auth)])
    def api_report_detail(report_id: str, topic_id: str = ""):
        if topic_id:
            daily = report_history(db, topic_id, run_id=report_id)
            if daily:
                return daily[0]
        period = db.one("SELECT * FROM period_reports WHERE id=:id", id=report_id)
        if period:
            return period
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到报告"})

    # --- Projects, Gap & Zotero ---
    @app.get("/api/projects", dependencies=[Depends(require_auth)])
    def api_list_projects(topic_id: str = ""):
        return {"projects": list_projects(db, topic_id)}

    @app.post("/api/projects", dependencies=[Depends(require_auth)])
    def api_create_project(topic_id: str, body: ProjectInput):
        return create_project(db, topic_id, body)

    @app.patch("/api/projects/{project_id}", dependencies=[Depends(require_auth)])
    def api_update_project(project_id: str, revision: int, body: ProjectInput):
        return update_project(db, project_id, revision, body)

    @app.get("/api/projects/{project_id}/links", dependencies=[Depends(require_auth)])
    def api_project_links(project_id: str):
        return {"links": project_links(db, project_id), "comparison_matrix": protocol_comparison_matrix(db, project_id)}

    @app.post("/api/projects/{project_id}/compare", dependencies=[Depends(require_auth)])
    def api_project_compare(project_id: str, relink: bool = False):
        links = link_project_papers(db, live_settings(), project_id) if relink else None
        return {"links": links, "comparison_matrix": protocol_comparison_matrix(db, project_id)}

    @app.get("/api/gap", dependencies=[Depends(require_auth)])
    def api_list_gap(topic_id: str):
        return {"analyses": gap_history(db, topic_id)}

    @app.post("/api/gap", dependencies=[Depends(require_auth)])
    def api_gap(body: GapPayload):
        return run_gap_analysis(db, live_settings(), body.topic_id, project_id=body.project_id)

    @app.post("/api/zotero/pull", dependencies=[Depends(require_auth)])
    def api_zotero_pull(body: ZoteroSyncPayload):
        return pull_zotero_collection(db, live_settings(), body.topic_id, collection_key=body.collection_key)

    @app.post("/api/zotero/push", dependencies=[Depends(require_auth)])
    def api_zotero_push(body: ZoteroSyncPayload):
        return push_favorites_to_zotero(db, live_settings(), body.topic_id, collection_key=body.collection_key)

    # --- Conversations, Streaming Chat & Research Control ---
    @app.get("/api/conversations", dependencies=[Depends(require_auth)])
    def api_list_conversations(include_empty: bool = False):
        return {
            "conversations": [
                {"id": c["id"], "title": c["title"], "context": json.loads(c.get("context") or "{}")}
                for c in conversations(db, include_empty=include_empty)
            ]
        }

    @app.post("/api/conversations", dependencies=[Depends(require_auth)])
    def api_create_conversation(body: ConversationCreatePayload):
        chat_id = new_conversation(db, body.topic_id)
        return {"id": chat_id}

    @app.get("/api/conversations/{conversation_id}/turns", dependencies=[Depends(require_auth)])
    def api_list_turns(conversation_id: str):
        jobs_by_turn: dict[str, list[dict]] = {}
        for job in research_status(db, conversation_id=conversation_id):
            jobs_by_turn.setdefault(job["turn_id"], []).append(_serialize_job(db, job))
        return {"turns": [_serialize_turn(db, t, jobs_by_turn) for t in turns(db, conversation_id)]}

    @app.post("/api/chat/turns", status_code=202, dependencies=[Depends(require_auth)])
    def api_queue_chat_turn(body: ChatTurnQueuePayload):
        current_cfg = live_settings()
        turn_id = queue_turn(db, current_cfg, body.conversation_id, body.prompt, turn_id=uuid4().hex)
        chat_pool.submit(run_turn, db, current_cfg, turn_id)
        return {"conversation_id": body.conversation_id, "turn_id": turn_id, "status": "queued"}

    @app.get("/api/chat/turns/{turn_id}/stream", dependencies=[Depends(require_auth)])
    async def api_stream_chat_turn(turn_id: str):
        current_cfg = live_settings()

        async def event_generator():
            last_signature = None
            while True:
                row = db.one("SELECT * FROM conversation_turns WHERE id=:id", id=turn_id)
                if not row:
                    yield f"event: error\ndata: {json.dumps({'message': '找不到对话轮次'}, ensure_ascii=False)}\n\n"
                    break
                if row["status"] == "running":
                    expire_turn(db, current_cfg, turn_id)
                    row = db.one("SELECT * FROM conversation_turns WHERE id=:id", id=turn_id)
                jobs = [_serialize_job(db, j) for j in research_status(db, conversation_id=row["conversation_id"]) if j["turn_id"] == turn_id]
                payload = _serialize_turn(db, row, {turn_id: jobs})
                signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if signature != last_signature:
                    last_signature = signature
                    event_name = "complete" if row["status"] not in {"queued", "running"} else "update"
                    yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if row["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.25)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat/turns/{turn_id}/cancel", dependencies=[Depends(require_auth)])
    def api_cancel_chat_turn(turn_id: str, body: ConversationCreatePayload | None = None):
        row = db.one("SELECT conversation_id FROM conversation_turns WHERE id=:id", id=turn_id)
        if not row:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到对话轮次"})
        cancelled = cancel_turn(db, row["conversation_id"], turn_id)
        return {"turn_id": turn_id, "cancelled": cancelled}

    @app.post("/api/conversations/{conversation_id}/actions/{action_id}/confirm", dependencies=[Depends(require_auth)])
    def api_confirm_action(conversation_id: str, action_id: str, body: ActionConfirmPayload):
        receipt = confirm_action(db, conversation_id, action_id, approve=body.approve, settings=live_settings())
        return {"approved": body.approve, "receipt": receipt}

    @app.post("/api/chat", dependencies=[Depends(require_auth)])
    def api_chat(body: ChatPayload):
        current_cfg = live_settings()
        chat_id = body.conversation_id or new_conversation(db, body.topic_id)
        turn_id = submit_turn(db, current_cfg, chat_id, body.prompt)
        turn = next(item for item in turns(db, chat_id) if item["id"] == turn_id)
        return {"conversation_id": chat_id, "turn": turn}

    @app.get("/api/research/jobs", dependencies=[Depends(require_auth)])
    def api_list_research_jobs(conversation_id: str | None = None, run_id: str | None = None, topic_id: str = ""):
        jobs = research_status(db, conversation_id=conversation_id, run_id=run_id)
        serialized = [_serialize_job(db, j) for j in jobs]
        if topic_id:
            serialized = [j for j in serialized if j.get("topic") == topic_id]
        return {"jobs": serialized}

    @app.post("/api/research", status_code=202, dependencies=[Depends(require_auth)])
    def api_research(body: ResearchPayload):
        current_cfg = live_settings()
        chat_id = new_conversation(db, body.topic_id)
        prompt = f"做个专题研究：{body.question}"
        turn_id = submit_turn(db, current_cfg, chat_id, prompt)
        turn = next(item for item in turns(db, chat_id) if item["id"] == turn_id)
        return {"conversation_id": chat_id, "turn": turn}

    @app.post("/api/research/jobs/{job_id}/adjust", dependencies=[Depends(require_auth)])
    def api_adjust_research(job_id: str, body: ResearchAdjustPayload):
        payload = ResearchAdjustment(
            focus=body.focus.strip() if body.focus and body.focus.strip() else None,
            venues=body.venues,
            main_only=body.main_only,
            paper=body.paper.strip() if body.paper and body.paper.strip() else None,
        )
        return queue_adjustment(db, body.conversation_id, job_id, payload)

    @app.post("/api/research/jobs/{job_id}/cancel", dependencies=[Depends(require_auth)])
    def api_cancel_research(job_id: str, body: ResearchCancelPayload):
        return cancel_job(db, body.conversation_id, job_id)

    # --- System Settings & External MCP ---
    @app.get("/api/settings/diagnostics", dependencies=[Depends(require_auth)])
    def api_settings_diagnostics():
        current_cfg = live_settings()
        diag = embedding_diagnostics(current_cfg)
        return {
            "embedding": {
                **diag,
                "model_dir": str(diag["model_dir"]),
            },
            "config": {
                "llm_base_url": current_cfg.llm_base_url,
                "llm_model": current_cfg.llm_model,
                "thinking_level": current_cfg.thinking_level,
                "stream_chat": current_cfg.stream_chat,
                "input_price": current_cfg.input_price,
                "output_price": current_cfg.output_price,
                "keyword_only": current_cfg.keyword_only,
                "reranker_enabled": current_cfg.reranker_enabled,
                "reranker_model": current_cfg.reranker_model,
                "chat_max_calls": current_cfg.chat_max_calls,
                "chat_max_seconds": current_cfg.chat_max_seconds,
                "extra_sources": [s.strip() for s in current_cfg.extra_sources.split(",") if s.strip() in EXTRA_SOURCES],
                "available_extra_sources": [{"id": k, "label": SOURCE_LABELS[k]} for k in EXTRA_SOURCES],
                "crossref_mailto": current_cfg.crossref_mailto,
                "page_limit": current_cfg.page_limit,
                "zotero_user_id": current_cfg.zotero_user_id,
                "api_host": current_cfg.api_host,
                "api_port": current_cfg.api_port,
                "hf_endpoint": os.getenv("HF_ENDPOINT", ""),
            },
            "secrets": [
                {
                    "key": env_key,
                    "label": label,
                    "description": help_text,
                    "masked": mask_secret(os.getenv(env_key, "")),
                    "configured": bool(os.getenv(env_key, "").strip()),
                }
                for env_key, label, help_text in SECRET_SPECS
            ],
        }

    @app.put("/api/settings/env", dependencies=[Depends(require_auth)])
    def api_update_env(body: EnvUpdatePayload):
        invalid_updates = set(body.updates) - ALLOWED_ENV_KEYS
        invalid_clears = set(body.clear_keys) - ALLOWED_ENV_KEYS
        if invalid_updates or invalid_clears:
            raise ValueError("包含不允许修改的配置项")
        save_env_updates(env_path, body.updates, clear_keys=set(body.clear_keys))
        if settings is not None:
            runtime_cfg[0] = Settings.from_env().model_copy(
                update={"data_dir": cfg.data_dir, "api_host": cfg.api_host, "api_port": cfg.api_port}
            )
        return {"saved": True}

    @app.post("/api/settings/warm-embedding", dependencies=[Depends(require_auth)])
    def api_warm_embedding(body: WarmEmbeddingPayload):
        target = warm_embedding_weights(live_settings(), body.hf_endpoint)
        if body.hf_endpoint.strip():
            save_env_updates(env_path, {"HF_ENDPOINT": body.hf_endpoint.strip()})
        return {"target": target}

    @app.get("/api/mcp-servers", dependencies=[Depends(require_auth)])
    def api_list_mcp():
        return {"servers": list_mcp_servers(db)}

    @app.post("/api/mcp-servers", dependencies=[Depends(require_auth)])
    def api_add_mcp(body: McpServerCreatePayload):
        return add_mcp_server(db, body.name, body.command, args=body.args, env_keys=body.env_keys)

    @app.patch("/api/mcp-servers/{server_id}", dependencies=[Depends(require_auth)])
    def api_toggle_mcp(server_id: str, body: McpServerTogglePayload):
        set_mcp_server_enabled(db, server_id, body.enabled)
        return {"id": server_id, "enabled": body.enabled}

    @app.delete("/api/mcp-servers/{server_id}", dependencies=[Depends(require_auth)])
    def api_delete_mcp(server_id: str):
        remove_mcp_server(db, server_id)
        return {"deleted": True}

    @app.post("/api/mcp-servers/{server_id}/probe", dependencies=[Depends(require_auth)])
    def api_probe_mcp(server_id: str):
        server = next((s for s in list_mcp_servers(db) if s["id"] == server_id), None)
        if not server:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "找不到 MCP 服务"})
        return {"tools": discover_mcp_tools(server)}

    # --- Static SPA Hosting ---
    if web_dist.is_dir():
        assets_dir = web_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            if full_path.startswith("api/") or full_path == "health":
                raise HTTPException(status_code=404, detail={"code": "not_found", "message": "接口不存在"})
            resolved_root = web_dist.resolve()
            candidate = (resolved_root / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(resolved_root):
                return FileResponse(candidate)
            index_file = resolved_root / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)
            raise HTTPException(status_code=404, detail={"code": "spa_missing", "message": "前端资源尚未构建"})

    return app
