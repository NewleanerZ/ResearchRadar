"""Token-budgeted context assembly, full tool trajectory reconstruction, and structured compaction."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import dump, utc_now
from .ranking import request_completion
from .store import write
from .tool_calling import estimate_tokens


class CompactionState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    goal: str = Field(default="", max_length=600)
    topic_and_projects: list[str] = Field(default_factory=list, max_length=12)
    referenced_papers: list[str] = Field(default_factory=list, max_length=25)
    verified_conclusions: list[str] = Field(default_factory=list, max_length=15)
    pending_items: list[str] = Field(default_factory=list, max_length=10)
    summary_text: str = Field(default="", max_length=2400)


def _turn_tool_summary(turn: dict) -> str:
    events = json.loads(turn.get("events") or "[]")
    names = list(dict.fromkeys(ev.get("name", "") for ev in events if ev.get("name")))
    return f"\n[已调用工具: {', '.join(names)}]" if names else ""


def _sanitize_history_messages(raw_messages: list[dict]) -> list[dict]:
    """Keep valid user/assistant/tool sequence from a past turn while dropping past reasoning_content."""
    cleaned: list[dict] = []
    for msg in raw_messages:
        role = msg.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        copy = {k: v for k, v in msg.items() if k != "reasoning_content"}
        if role == "tool":
            content = copy.get("content") or ""
            if isinstance(content, str) and len(content) > 6000:
                copy["content"] = content[:6000] + "...[truncated]"
        cleaned.append(copy)
    return cleaned


def turn_to_messages(turn: dict) -> list[dict]:
    """Convert a persisted completed/partial turn into chat messages, preserving tool trajectories."""
    raw_json = turn.get("messages_json")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list) and parsed:
                sanitized = _sanitize_history_messages(parsed)
                if sanitized:
                    return sanitized
        except (ValueError, TypeError):
            pass
    prompt = (turn.get("prompt") or "")[:4000]
    answer = (turn.get("answer") or "")[:4000]
    tool_note = _turn_tool_summary(turn)
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": (answer + tool_note)[:4500]},
    ]


def format_compaction_summary(compaction: CompactionState) -> str:
    parts = []
    if compaction.goal:
        parts.append(f"研究目标：{compaction.goal}")
    if compaction.topic_and_projects:
        parts.append("涉及主题/项目：" + "；".join(compaction.topic_and_projects))
    if compaction.referenced_papers:
        parts.append("关键论文/ID：" + "；".join(compaction.referenced_papers))
    if compaction.verified_conclusions:
        parts.append("已核实结论：" + "；".join(compaction.verified_conclusions))
    if compaction.pending_items:
        parts.append("待跟进事项：" + "；".join(compaction.pending_items))
    if compaction.summary_text:
        parts.append("要点补充：" + compaction.summary_text)
    return "\n".join(parts)[:2400]


def _extract_entities(turns: list[dict]) -> list[str]:
    found: list[str] = []
    for t in turns:
        blob = f"{t.get('prompt', '')}\n{t.get('answer', '')}\n{t.get('events', '')}"
        for match in re.findall(r"\b(?:10\.\d{4,9}/[-._;()/:A-Za-z0-9]+|\d{4}\.\d{4,5}(?:v\d+)?|art_[a-f0-9]{12}|topic_[a-f0-9]{12})\b", blob):
            if match not in found:
                found.append(match)
            if len(found) >= 20:
                return found
    return found


def compact_history_structured(
    history: list[dict],
    context: dict,
    settings,
    client=None,
    *,
    store=None,
    conversation_id: str | None = None,
    force: bool = False,
) -> dict | None:
    """Compact older turns into a structured summary when turn count or token budget grows."""
    completed = [t for t in history if t["status"] in {"done", "partial"} and (t.get("answer") or t.get("messages_json"))]
    if not force and len(completed) <= 4:
        return None

    keep_tail = 4 if not force else min(2, len(completed))
    older = completed[:-keep_tail] if keep_tail > 0 else completed
    if not older:
        return None
    if not force and context.get("compacted_count", 0) >= len(older):
        return None

    bullets = []
    for turn in older[-16:]:
        events = json.loads(turn.get("events") or "[]")
        tools_used = [ev.get("name") for ev in events if ev.get("name")]
        tag = f"[工具:{','.join(dict.fromkeys(tools_used))}] " if tools_used else ""
        p_txt = (turn.get("prompt") or "").strip()[:500]
        a_txt = (turn.get("answer") or "").strip()[:700]
        bullets.append(f"- 问：{p_txt} | {tag}答：{a_txt}")
    fallback_summary = "\n".join(bullets)
    entities = _extract_entities(older[-16:])
    compaction = CompactionState(
        goal=(older[-1].get("prompt") or "")[:200],
        referenced_papers=entities,
        summary_text=fallback_summary[:2000],
    )

    if settings.llm_model and settings.llm_key.get_secret_value() and client is not None and (force or len(older) >= 4):
        prior_text = context.get("compacted_summary") or ""
        prompt_payload = {
            "previous_compaction": prior_text[:1200],
            "turns_to_compact": fallback_summary[:10000],
        }
        try:
            reply = request_completion(
                client,
                settings,
                [
                    {
                        "role": "system",
                        "content": (
                            "将历史科研对话压缩为结构化 JSON 交接摘要，保留真实主题 ID、论文标题/DOI/arXiv ID、"
                            "产物 ID (art_*)、项目名与已核实结论。"
                            '输出 JSON：{"goal":"...","topic_and_projects":[],"referenced_papers":[],'
                            '"verified_conclusions":[],"pending_items":[],"summary_text":"300字以内中文要点"}'
                        ),
                    },
                    {"role": "user", "content": dump(prompt_payload)},
                ],
                structured=True,
                max_tokens=700,
                timeout=12.0,
                thinking_level="off",
            )
            raw_content = (reply["choices"][0]["message"].get("content") or "").strip()
            parsed = CompactionState.model_validate_json(raw_content)
            compaction = parsed
        except (Exception, ValidationError):
            pass

    formatted = format_compaction_summary(compaction)
    if not formatted:
        formatted = fallback_summary[:2400]
    context["compacted_summary"] = formatted[:2400]
    context["compacted_count"] = len(older)
    structured_payload = {
        **compaction.model_dump(mode="json"),
        "compacted_count": len(older),
        "updated_at": utc_now().isoformat(),
    }
    context["compaction"] = structured_payload
    if store is not None and conversation_id:
        store.execute(
            """INSERT INTO conversation_compactions(conversation_id, compaction_json, updated_at)
               VALUES (:cid, :comp, :now)
               ON CONFLICT(conversation_id) DO UPDATE SET compaction_json=excluded.compaction_json,
               updated_at=excluded.updated_at""",
            comp=dump(structured_payload),
            cid=conversation_id,
            now=utc_now().isoformat(),
        )
    return structured_payload


def build_context_messages(
    system_prompt: str,
    history: list[dict],
    context: dict,
    prompt: str,
    *,
    token_budget: int = 48000,
) -> list[dict]:
    """Assemble system prompt, bounded recent turn trajectories, and current user context."""
    facts = {
        key: value
        for key, value in context.items()
        if key in {
            "topic", "papers", "paper_lists", "paper", "run", "research", "draft",
            "local_date", "timezone", "project_memory", "compacted_summary",
            "latest_research_report",
        }
    }
    current = {"role": "user", "content": dump({"user_message": prompt, "app_context": facts})}
    base_tokens = estimate_tokens([{"role": "system", "content": system_prompt}, current])
    remaining = max(2000, token_budget - base_tokens)

    compacted_count = int(context.get("compacted_count") or 0)
    completed = [t for t in history if t["status"] in {"done", "partial"} and (t.get("answer") or t.get("messages_json"))]
    uncompacted = completed[compacted_count:] if compacted_count > 0 and compacted_count < len(completed) else completed
    if not uncompacted and completed:
        uncompacted = completed[-4:]

    selected_blocks: list[list[dict]] = []
    used = 0
    for turn in reversed(uncompacted):
        block = turn_to_messages(turn)
        block_tokens = estimate_tokens(block)
        if selected_blocks and used + block_tokens > remaining:
            break
        selected_blocks.append(block)
        used += block_tokens

    tail = [msg for block in reversed(selected_blocks) for msg in block]
    return [{"role": "system", "content": system_prompt}, *tail, current]
