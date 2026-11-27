"""Bounded LangGraph Research Gap discovery (reverse-search verification) & experiment design."""

from datetime import date, timedelta
import json
import time
from typing import TypedDict
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .delivery import md
from .models import CORE_SOURCES, Settings, Topic, dump, utc_now
from .projects import get_project, project_links, protocol_comparison_matrix
from .rag import hybrid_search
from .ranking import best_record, request_json_completion
from .sources import SourceError, Sources
from .store import Store, norm, write


class GapCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=600)
    search_terms: list[str] = Field(min_length=1, max_length=3)
    supporting_paper_ids: list[int] = Field(default_factory=list, max_length=8)


class GapDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[GapCandidate] = Field(min_length=1, max_length=4)


class GapState(TypedDict, total=False):
    project: dict | None
    topic: dict
    local_evidence: list[dict]
    candidates: list[dict]
    reverse_hits: dict[str, list[dict]]
    reviewed: list[dict]
    experiment_plan: dict
    warnings: list[str]
    markdown: str


def run_gap_analysis(store: Store, settings: Settings, topic_id: str, *, project_id: str | None = None,
                     client: httpx.Client | None = None, sources=None, deadline: float | None = None) -> dict:
    """Execute bounded LangGraph workflow: evidence -> candidates -> reverse search -> review -> report."""
    topic, _ = store.topic(topic_id)
    project = get_project(store, project_id) if project_id else None
    if project and project["topic_id"] != topic_id:
        raise ValueError("科研项目不属于所选主题")

    gid = f"gap_{uuid4().hex[:12]}"
    now = utc_now().isoformat()
    spec = {"topic_id": topic_id, "project_id": project_id, "as_of": utc_now().date().isoformat()}
    with store.transaction() as con:
        write(
            con,
            """INSERT INTO gap_analyses(id, project_id, topic_id, status, spec_json, state_json, markdown, created_at, updated_at)
               VALUES (:id, :pid, :topic, 'running', :spec, '{}', NULL, :now, :now)""",
            id=gid, pid=project_id, topic=topic_id, spec=dump(spec), now=now,
        )

    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": "ResearchRadar/0.2"})
    sources = sources or Sources.configured(client, settings)
    deadline = deadline or (time.monotonic() + 90.0)

    def collect_evidence(_state: GapState) -> dict:
        query = (project["question"] if project else topic.description) or " ".join(topic.keywords)
        rag_hits = hybrid_search(store, settings, query, topic_id=topic_id, limit=8)
        papers = store.papers(topic_id)[:15]
        local_items = [
            {
                "paper_id": p["paper_id"],
                "title": best_record(p).title,
                "abstract": (best_record(p).abstract or "")[:800],
                "url": best_record(p).url,
            }
            for p in papers
        ]
        return {"project": project, "topic": topic.model_dump(mode="json"),
                "local_evidence": local_items, "warnings": [], "reverse_hits": {},
                "experiment_plan": {"rag_chunks": len(rag_hits)}}

    def generate_candidates(state: GapState) -> dict:
        warnings = list(state.get("warnings", []))
        if settings.llm_model and settings.llm_key.get_secret_value() and time.monotonic() < deadline:
            prompt = (
                "你是科研问题分析助手。仅根据给出的项目信息与已有文献摘要，"
                "提出 2 到 3 个具体、可检验的候选研究空白（Research Gap）。"
                "返回 JSON 对象：{\"candidates\":[{\"title\":\"...\",\"rationale\":\"...\","
                "\"search_terms\":[\"english query\"],\"supporting_paper_ids\":[]}]}"
            )
            try:
                reply = request_json_completion(
                    client, settings,
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": dump({
                            "project": project, "topic": topic.name,
                            "keywords": topic.keywords, "papers": state["local_evidence"][:10],
                        })},
                    ],
                    max_tokens=1200,
                    timeout=min(30.0, max(1.0, deadline - time.monotonic())),
                )
                draft = GapDraft.model_validate_json(reply["choices"][0]["message"]["content"])
                return {"candidates": [c.model_dump() for c in draft.candidates], "warnings": warnings}
            except (httpx.HTTPError, ValueError, KeyError, IndexError, ValidationError):
                warnings.append("模型生成候选空白失败，已回退至基于主题词的规则候选。")
        base_terms = topic.keywords[:2]
        title = (project["question"][:80] if project else f"{topic.name} 现有方法在跨协议与泛化上的不足")
        return {
            "candidates": [{
                "title": title,
                "rationale": "基于项目目标与本地文献库的初步比对，需通过反向检索确认是否已有直接工作。",
                "search_terms": base_terms,
                "supporting_paper_ids": [item["paper_id"] for item in state["local_evidence"][:3]],
            }],
            "warnings": warnings,
        }

    def reverse_search(state: GapState) -> dict:
        hits_by_title: dict[str, list[dict]] = {}
        warnings = list(state.get("warnings", []))
        today = utc_now().date()
        start = today - timedelta(days=365)
        end = today + timedelta(days=1)
        search_sources = tuple(s for s in getattr(sources, "daily_sources", CORE_SOURCES) if s in {"arxiv", "openalex", "semantic_scholar"})
        for cand in state["candidates"]:
            found: list[dict] = []
            terms = cand["search_terms"][:2]
            for src in search_sources:
                if time.monotonic() >= deadline:
                    warnings.append("反向检索达到时间预算")
                    break
                try:
                    page = sources.page(src, topic, terms, start, end, None,
                                        min(deadline, time.monotonic() + 12.0))
                    for rec in page.records:
                        found.append({"source": rec.source, "title": rec.title, "url": rec.url,
                                      "abstract": (rec.abstract or "")[:400]})
                except SourceError as err:
                    warnings.append(f"反向检索 {src} 未完成（{err}）")
            hits_by_title[cand["title"]] = found
        return {"reverse_hits": hits_by_title, "warnings": warnings}

    def review_evidence(state: GapState) -> dict:
        reviewed = []
        local_by_id = {item["paper_id"]: item for item in state["local_evidence"]}
        for cand in state["candidates"]:
            ext_hits = state["reverse_hits"].get(cand["title"], [])
            terms = [norm(t) for t in cand["search_terms"]]
            counter_examples = []
            for hit in ext_hits:
                body = norm(f"{hit['title']} {hit['abstract']}")
                if any(all(w in body for w in t.split()[:2]) for t in terms if t.split()):
                    counter_examples.append(hit)
            support = [local_by_id[pid] for pid in cand["supporting_paper_ids"] if pid in local_by_id]
            verdict = (
                "存在强相关先例，疑似已被研究（建议收缩或调整方向）" if len(counter_examples) >= 2
                else "发现少量相近文献，需进一步核对实验设置" if counter_examples
                else "未在检索范围内发现直接反例（仍受来源覆盖限制）"
            )
            reviewed.append({
                **cand,
                "supporting_papers": support,
                "counter_examples": counter_examples[:5],
                "verdict": verdict,
                "confidence": "low" if counter_examples else "medium",
            })
        plan = design_experiment_plan(store, topic, project)
        return {"reviewed": reviewed, "experiment_plan": plan}

    def synthesize_report(state: GapState) -> dict:
        lines = [
            f"# Research Gap 与实验方案分析 · {md(project['name'] if project else topic.name)}",
            "",
            f"生成日期：{spec['as_of']} · 主题：{md(topic.name)}",
            "",
            "> 本分析仅基于当前本地文献库与有限外部反向检索结果，**不构成绝对创新性或投稿录用保证**。",
            "",
            "## 候选研究问题与反向查证",
            "",
        ]
        for idx, item in enumerate(state["reviewed"], start=1):
            lines.extend([
                f"### {idx}. {md(item['title'])}",
                "",
                f"- **提出依据**：{md(item['rationale'])}",
                f"- **反向查证结论**：{md(item['verdict'])}（置信度：{item['confidence']}）",
            ])
            if item["supporting_papers"]:
                lines.append("- **支持文献**：" + "；".join(
                    f"[{md(p['title'])}]({p['url']})" for p in item["supporting_papers"]))
            if item["counter_examples"]:
                lines.append("- **反向检索相近/反例文献**：")
                for ce in item["counter_examples"]:
                    lines.append(f"  - [{md(ce['title'])}]({ce['url']})（{ce['source']}）")
            lines.append("")

        plan = state["experiment_plan"]
        lines.extend(["## 实验方案建议", ""])
        lines.append("- **推荐数据集**：" + ("、".join(map(md, plan["datasets"])) or "待根据目标任务补充"))
        lines.append("- **评价指标**：" + ("、".join(map(md, plan["metrics"])) or "待确认"))
        if plan["baselines"]:
            lines.append("- **候选基线论文**：")
            for b in plan["baselines"]:
                lines.append(f"  - {md(b['title'])}（{md(b['notes'])}）")
        lines.append("- **消融实验清单**：")
        for ab in plan["ablations"]:
            lines.append(f"  - {md(ab)}")
        if state.get("warnings"):
            lines.extend(["", "## 范围限制与警告", "", *[f"- {md(w)}" for w in dict.fromkeys(state["warnings"])]])
        return {"markdown": "\n".join(lines) + "\n"}

    graph = StateGraph(GapState)
    for node in (collect_evidence, generate_candidates, reverse_search, review_evidence, synthesize_report):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "collect_evidence")
    graph.add_edge("collect_evidence", "generate_candidates")
    graph.add_edge("generate_candidates", "reverse_search")
    graph.add_edge("reverse_search", "review_evidence")
    graph.add_edge("review_evidence", "synthesize_report")
    graph.add_edge("synthesize_report", END)

    try:
        final = graph.compile().invoke({})
        status = "partial" if final.get("warnings") else "succeeded"
        with store.transaction() as con:
            write(
                con,
                "UPDATE gap_analyses SET status=:status, state_json=:st, markdown=:md, updated_at=:now WHERE id=:id",
                status=status,
                st=dump({"reviewed": final["reviewed"], "experiment_plan": final["experiment_plan"],
                         "warnings": final.get("warnings", [])}),
                md=final["markdown"], now=utc_now().isoformat(), id=gid,
            )
    except Exception as err:
        with store.transaction() as con:
            write(
                con,
                "UPDATE gap_analyses SET status='failed', state_json=:st, updated_at=:now WHERE id=:id",
                st=dump({"error": type(err).__name__}), now=utc_now().isoformat(), id=gid,
            )
    finally:
        if own_client:
            client.close()
    return store.one("SELECT * FROM gap_analyses WHERE id=:id", id=gid)


def design_experiment_plan(store: Store, topic: Topic, project: dict | None = None) -> dict:
    """Build structured experiment recommendations grounded in project archives & deep analyses."""
    datasets = list(project["datasets"]) if project else []
    metrics = list(project["metrics"]) if project else []
    baselines = []
    if project:
        for link in project_links(store, project["id"])[:5]:
            baselines.append({"paper_id": link["paper_id"], "title": link["title"], "notes": link["notes"]})
        for group in protocol_comparison_matrix(store, project["id"]):
            if group["dataset"] != "原文未确认" and group["dataset"] not in datasets:
                datasets.append(group["dataset"])
            if group["metric"] != "原文未确认" and group["metric"] not in metrics:
                metrics.append(group["metric"])
    ablations = [
        "核心模块消融：移除新增设计后的性能变化（保持数据划分与超参一致）",
        "跨数据集/域外泛化对比：分别报告各数据集指标，避免混合不同协议",
        "计算开销与延迟对比：记录推理耗时、显存/Token 消耗与基线对比",
    ]
    return {"datasets": datasets[:8], "metrics": metrics[:8], "baselines": baselines, "ablations": ablations}


def gap_history(store: Store, topic_id: str = "") -> list[dict]:
    return store.all(
        "SELECT * FROM gap_analyses WHERE (:topic='' OR topic_id=:topic) ORDER BY created_at DESC LIMIT 20",
        topic=topic_id,
    )
