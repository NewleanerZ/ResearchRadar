"""Conversation artifact storage and bounded slice/search reader for large tool outputs."""

import json
from typing import Callable
from uuid import uuid4

from .models import dump, utc_now
from .store import Store, write


def save_artifact(
    store: Store,
    conversation_id: str,
    turn_id: str,
    tool_name: str,
    title: str,
    content: str | dict | list,
    *,
    mime_type: str = "application/json",
    metadata: dict | None = None,
) -> dict:
    """Persist full tool output as a conversation-scoped artifact."""
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
    aid = f"art_{uuid4().hex[:12]}"
    now = utc_now().isoformat()
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO conversation_artifacts
               (id, conversation_id, turn_id, tool_name, title, mime_type, char_count, content, metadata_json, created_at)
               VALUES (:id, :cid, :tid, :tool, :title, :mime, :chars, :content, :meta, :now)""",
            id=aid,
            cid=conversation_id,
            tid=turn_id,
            tool=tool_name,
            title=title[:160],
            mime=mime_type,
            chars=len(text),
            content=text,
            meta=dump(metadata or {}),
            now=now,
        )
    return {
        "id": aid,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "tool_name": tool_name,
        "title": title[:160],
        "mime_type": mime_type,
        "char_count": len(text),
        "created_at": now,
    }


def get_artifact(store: Store, conversation_id: str, artifact_id: str) -> dict | None:
    """Fetch a single artifact belonging to the specified conversation."""
    row = store.one(
        """SELECT id, conversation_id, turn_id, tool_name, title, mime_type, char_count, content, metadata_json, created_at
           FROM conversation_artifacts WHERE id=:id AND conversation_id=:cid""",
        id=artifact_id,
        cid=conversation_id,
    )
    if not row:
        return None
    row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
    return row


def list_artifacts(store: Store, conversation_id: str, *, limit: int = 50) -> list[dict]:
    """List conversation artifacts (metadata only, newest first)."""
    items = store.all(
        """SELECT id, conversation_id, turn_id, tool_name, title, mime_type, char_count, metadata_json, created_at
           FROM conversation_artifacts WHERE conversation_id=:cid
           ORDER BY created_at DESC, rowid DESC LIMIT :lim""",
        cid=conversation_id,
        lim=max(1, min(limit, 100)),
    )
    for item in items:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    return items


def read_artifact_slice(
    store: Store,
    conversation_id: str,
    artifact_id: str,
    *,
    offset: int = 0,
    limit: int = 3000,
    query: str = "",
) -> dict:
    """Read a bounded character slice or keyword-matched excerpts from an artifact."""
    art = get_artifact(store, conversation_id, artifact_id)
    if not art:
        raise ValueError(f"找不到当前会话的产物：{artifact_id}")
    content = art["content"]
    total = len(content)
    safe_limit = max(200, min(limit, 6000))
    clean_query = query.strip()
    if clean_query:
        lowered = content.casefold()
        q_lower = clean_query.casefold()
        matches = []
        pos = 0
        while len(matches) < 8:
            idx = lowered.find(q_lower, pos)
            if idx == -1:
                break
            win_start = max(0, idx - 220)
            win_end = min(total, idx + len(clean_query) + 420)
            matches.append({
                "offset": win_start,
                "end": win_end,
                "excerpt": content[win_start:win_end],
            })
            pos = max(idx + len(clean_query), win_end)
        return {
            "artifact_id": artifact_id,
            "title": art["title"],
            "tool_name": art["tool_name"],
            "query": clean_query,
            "total_chars": total,
            "matched_count": len(matches),
            "matches": matches,
        }
    safe_offset = max(0, min(offset, total))
    chunk = content[safe_offset : safe_offset + safe_limit]
    next_offset = safe_offset + len(chunk)
    return {
        "artifact_id": artifact_id,
        "title": art["title"],
        "tool_name": art["tool_name"],
        "mime_type": art["mime_type"],
        "offset": safe_offset,
        "limit": safe_limit,
        "total_chars": total,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "content": chunk,
    }


def maybe_offload_tool_output(
    store: Store,
    conversation_id: str,
    turn_id: str,
    tool_name: str,
    title: str,
    payload: dict | list | str,
    *,
    inline_chars: int = 2400,
    mime_type: str = "application/json",
    summary_builder: Callable[[dict | list | str], dict] | None = None,
) -> tuple[dict | list | str, dict | None]:
    """Store large tool payloads as artifacts and return a compact summary + handle."""
    serialized = payload if isinstance(payload, str) else dump(payload)
    if len(serialized) <= inline_chars:
        return payload, None
    art = save_artifact(
        store,
        conversation_id,
        turn_id,
        tool_name,
        title,
        payload,
        mime_type=mime_type,
        metadata={"inline_threshold": inline_chars},
    )
    if summary_builder is not None:
        compact = dict(summary_builder(payload))
    elif isinstance(payload, dict):
        compact = {k: v for k, v in payload.items() if k not in {"pages", "markdown", "content", "chunks"}}
    else:
        compact = {"preview": serialized[:1000]}
    compact.update({
        "artifact_id": art["id"],
        "artifact_chars": art["char_count"],
        "artifact_hint": f"完整结果（{art['char_count']} 字符）已存入产物 {art['id']}，如需查看更多细节请调用 read_artifact。",
    })
    return compact, art
