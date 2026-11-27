"""Attach steering instructions to a running research job."""

import json
from uuid import uuid4

from pydantic import Field, model_validator

from .activity import log_event
from .models import dump, utc_now
from .project_memory import ProjectMemory
from .sources import arxiv_identifier, normalize_doi
from .store import row, write
from .tool_calling import Arguments


class ResearchAdjustment(Arguments):
    focus: str | None = Field(None, min_length=1, max_length=800)
    venues: list[str] | None = Field(None, max_length=12)
    main_only: bool | None = None
    paper: str | None = Field(None, max_length=250)

    @model_validator(mode="after")
    def normalize(self):
        if all(value is None for value in (self.focus, self.venues, self.main_only, self.paper)):
            raise ValueError("请填写一项专题调整")
        if self.venues is not None:
            self.venues = ProjectMemory(venues=self.venues).venues
        if self.paper is not None:
            arxiv_id, version = arxiv_identifier(self.paper)
            doi = normalize_doi(self.paper)
            if not arxiv_id and not doi:
                raise ValueError("请提供 DOI、arXiv ID 或 arXiv 原文链接")
            self.paper = doi or arxiv_id + (version or "")
        return self


def queue_adjustment(store, chat_id, job_id, adjustment: ResearchAdjustment):
    saved = adjustment.model_dump(exclude_none=True)
    body = dump(saved)
    with store.transaction() as con:
        job = row(con, """SELECT j.status,j.cancel_requested FROM research_jobs j
                          JOIN conversation_turns t ON t.id=j.turn_id
                          WHERE j.id=:id AND t.conversation_id=:chat""", id=job_id, chat=chat_id)
        if not job or job["status"] not in {"queued", "running"} or job["cancel_requested"]:
            raise ValueError("当前会话没有这个可调整的专题任务")
        pending = row(con, """SELECT id,status FROM research_adjustments
                              WHERE job_id=:job AND body=:body AND status='pending'""", job=job_id, body=body)
        if pending:
            return {"id": pending["id"], "status": pending["status"], "job": job_id}
        key = uuid4().hex
        write(con, """INSERT INTO research_adjustments VALUES
                      (:id,:job,'scope',:body,'pending',:now,NULL,NULL)""",
              id=key, job=job_id, body=body, now=utc_now().isoformat())
        log_event(store, "research", job_id, "adjustment", "queued",
                  detail={"fields": sorted(saved), "adjustment_id": key}, connection=con)
        return {"id": key, "status": "pending", "job": job_id}


def adjustments(store, job_id, *, pending=False):
    results = store.all("""SELECT id,body,status,detail FROM research_adjustments
        WHERE job_id=:id AND (NOT :pending OR status='pending') ORDER BY rowid""", id=job_id, pending=pending)
    return [{**item, "body": json.loads(item["body"])} for item in results]


def cancel_pending_adjustments(store, job_id, connection):
    updated = write(connection, """UPDATE research_adjustments SET status='cancelled'
        WHERE job_id=:job AND status='pending'""", job=job_id)
    if updated.rowcount:
        log_event(store, "research", job_id, "adjustment", "cancelled",
                  detail={"count": updated.rowcount}, connection=connection)
