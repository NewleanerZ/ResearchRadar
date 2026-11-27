"""Streamlit view for conversations and operation receipts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from threading import RLock
from urllib.parse import quote, urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from .activity import timeline
from .conversation import (
    actions, cancel_job, cancel_turn, compact_run, confirm_action, conversations, job_card, new_conversation,
    research_status, expire_turn, queue_turn, run_turn, turns,
)
from .delivery import md
from .models import SOURCE_LABELS, Settings, utc_now
from .project_memory import ProjectMemory, project_memory, save_project_memory
from .research_control import ResearchAdjustment, adjustments, queue_adjustment
from .ranking import best_record  # noqa: F401
from .service import RUN_LABELS, report_history, run_history
from .ui_theme import status_pill


TOOL_LABELS = {
    "list_topics": "读取主题", "search_library": "查询论文库", "search_external_papers": "外部学术检索",
    "fetch_paper_summary": "获取网页与摘要", "fetch_paper_pdf": "探索获取原始 PDF",
    "rag_search": "全文混合检索",
    "deep_read_paper": "精读论文", "get_paper": "读取论文",
    "get_report": "读取日报", "get_trend": "读取趋势", "get_run": "查询任务",
    "list_projects": "查询科研项目", "compare_methods": "同协议方法对比",
    "list_research_events": "查询科研事件", "run_gap_analysis": "Research Gap 分析",
    "list_external_mcp": "查看外部 MCP 工具", "call_external_mcp": "调用外部 MCP 工具",
    "set_paper_state": "更新阅读状态", "enqueue_daily": "提交检索", "draft_topic": "生成主题草案",
    "propose_action": "准备确认卡", "start_research": "提交专题", "cancel_research": "取消专题",
    "steer_research": "调整专题", "get_project_memory": "读取项目记忆",
}
STAGES = {"plan": "规划检索", "collect": "检索候选论文", "investigate": "阅读证据与补查",
          "report": "核对引用并生成报告", "finished": "已结束", "cancelled": "已取消"}
FIELD_NAMES = {"name": "名称", "description": "研究方向", "keywords": "核心检索词", "excluded": "排除词",
               "expansions": "已确认扩展词", "proposed_expansions": "待确认扩展词", "categories": "arXiv 分类",
               "subtopics": "子主题", "timezone": "时区", "schedule": "检索时间", "enabled": "关注",
               "archived": "已删除", "notify": "飞书通知"}
EVENT_LABELS = {"turn": "对话", "job": "任务", "tool": "工具", "model": "模型调用", "source": "数据源",
                "action": "操作", "confirmation": "确认", "project_memory": "项目记忆", "adjustment": "专题调整",
                "stage": "研究阶段", "report": "专题报告", "target_paper": "查指定论文"}
EVENT_STATUS = {**RUN_LABELS, "running": "进行中", "ok": "完成", "completed": "完成", "complete": "完成",
                "needs_confirmation": "待确认", "queued": "已排队", "loaded": "已采用", "applied": "已应用",
                "ready": "下一步", "error": "失败", "truncated": "截断", "budget": "触及上限",
                "done": "完成", "stopping": "等待当前步骤结束", "cancelled": "已取消", "outside_scope": "超出范围"}


class ChatWorker:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-radar-chat")
        self.active = {}
        self.lock = RLock()

    def submit(self, store, settings, key):
        with self.lock:
            if key not in self.active:
                task = self.pool.submit(run_turn, store, settings, key)
                self.active[key] = task
                task.add_done_callback(lambda _task: self.finished(key))

    def finished(self, key):
        with self.lock:
            self.active.pop(key, None)


@st.cache_resource
def chat_worker():
    return ChatWorker()


def route(page, **values):
    return "?" + urlencode({"page": page, **values})


def source_link(url):
    return quote(url, safe=":/?&=%#@+;~")


def paper_results(items):
    for index, item in enumerate(items, start=1):
        title = md(item["title"])
        st.markdown(f"{index}. [{title}]({source_link(item['url'])})" if item.get("url") else f"{index}. {title}")
        citations = f"引用 {item['citation_count']}" if item.get("citation_count") is not None else None
        pdf_tag = ("可获取 PDF" if item.get("pdf_status") == "available"
                   else "无公开 PDF" if item.get("pdf_status") == "unavailable" else None)
        detail = [item.get("topic_name"), item.get("date"), item.get("publication"), citations, pdf_tag, item.get("source")]
        if item.get("favorite"):
            detail.append("已收藏")
        if item.get("is_read"):
            detail.append("已读")
        if any(detail):
            st.caption(" · ".join(value for value in detail if value))


def run_summary(run):
    label = RUN_LABELS.get(run["status"], run["status"])
    tone = {"succeeded": "success", "partial": "warning", "failed": "danger", "running": "primary"}.get(
        run["status"], "accent"
    )
    icon = {"succeeded": "check", "failed": "error", "partial": "alert"}.get(run["status"], "clock")
    st.html(f'<div class="rr-pill-row">{status_pill(label, tone, icon)}<span class="rr-chip">{run["id"]}</span></div>')
    if run.get("scans"):
        st.dataframe(run["scans"], hide_index=True)
    if run.get("imported") is not None:
        st.caption(f"新增到主题库 {run['imported']} 篇")
    if run.get("notification"):
        st.caption("飞书 · " + {"pending": "排队", "sending": "发送中", "sent": "已送达",
            "failed": "发送失败", "uncertain": "送达待确认"}.get(run["notification"], run["notification"]))
    if run.get("usage"):
        st.caption(f"模型请求 {run['usage'].get('calls', 0)} 次 · {run['usage'].get('tokens', 0)} Token")


def timeline_view(store, scope, target, *, live=False):
    events = timeline(store, scope, target, 8 if live else 100)
    if not events:
        return
    items = []
    for event in events:
        data = json.loads(event["detail"])
        name = TOOL_LABELS.get(data.get("tool"), data.get("tool")) or SOURCE_LABELS.get(data.get("source"), data.get("source"))
        stage = name or EVENT_LABELS.get(event["stage"], event["stage"])
        bits = []
        for key, label in (("received", "记录"), ("total", "命中"), ("imported", "入库"), ("candidates", "候选"),
                           ("evidence", "证据"), ("citations", "引用"), ("revision", "版本")):
            if data.get(key) is not None:
                bits.append(f"{label} {data[key]}")
        for key in ("step", "next"):
            if data.get(key):
                bits.append(STAGES.get(data[key], data[key]))
        if data.get("paper"):
            bits.append(data["paper"])
        if data.get("http_status"):
            bits.append(f"HTTP {data['http_status']}")
        if data.get("input_tokens") is not None and data.get("output_tokens") is not None:
            bits.append(f"Token {data['input_tokens']} / {data['output_tokens']}")
        if reason := data.get("error") or data.get("reason"):
            bits.append(str(reason))
        items.append({"时间（本地）": datetime.fromisoformat(event["created_at"]).astimezone(
                          ZoneInfo("Asia/Hong_Kong")).strftime("%H:%M:%S"),
                      "阶段": stage, "状态": EVENT_STATUS.get(event["status"], event["status"]),
                      "耗时（毫秒）": event["elapsed_ms"], "结果": " · ".join(bits)})
    if live:
        last = items[-1]
        st.caption(f"{last['阶段']} · {last['状态']}" + (f" · {last['结果']}" if last["结果"] else ""))
    with st.expander("任务时间线", expanded=live):
        st.dataframe(items, hide_index=True, width="stretch")


@st.fragment(run_every=3)
def live_run(store, run_id, widget_key):
    matching = run_history(store, run_id=run_id)
    if matching:
        run = matching[0]
        run_summary(compact_run(store, run))
        timeline_view(store, "run", run_id, live=run["status"] in {"queued", "running"})
        st.link_button("查看运行记录", route("runs", run=run_id), key="run_link_" + widget_key)


def action_card(store, conversation_id, item):
    preview = json.loads(item["preview"])
    receipt = json.loads(item["receipt"] or "{}")
    with st.container(border=True, key="radar_card_action_" + item["id"]):
        st.markdown(f"**{md(preview['label'])}**")
        if preview.get("topic"):
            st.text(preview["topic"])
        if preview.get("after"):
            changes = [{"设置": FIELD_NAMES.get(key, key), "修改前": _display(preview.get("before", {}).get(key)),
                        "修改后": _display(value)} for key, value in preview["after"].items() if key in FIELD_NAMES]
            if changes:
                st.dataframe(changes, hide_index=True)
        if preview.get("start"):
            sources = " / ".join(preview.get("sources") or ["arXiv", "OpenAlex"])
            st.write(f"{preview['start']} 至 {preview['end']} · {sources}")
        if preview.get("venues"):
            st.write("仅收录已核实发表或录用的会议论文 · " + "、".join(preview["venues"]))
        if preview.get("date"):
            st.write(f"{preview['date']} · {preview['current']} · {preview['run_id']}")
        if item["status"] == "pending":
            left, right = st.columns(2)
            approve = left.button("确认执行", key="confirm_" + item["id"], type="primary")
            dismiss = right.button("取消", key="dismiss_" + item["id"])
            if approve or dismiss:
                try:
                    confirm_action(store, conversation_id, item["id"], approve=approve)
                except ValueError as error:
                    st.warning(str(error))
                else:
                    st.rerun()
        elif item["status"] == "dismissed":
            st.caption("已取消")
        elif receipt.get("run_id"):
            st.success("已提交")
            live_run(store, receipt["run_id"], item["id"])
        else:
            st.success("已完成")


def _display(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(map(str, value))
    return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)


def memory_editor(store, topic):
    revision, memory = project_memory(store, topic.id)
    with st.expander("项目记忆"):
        st.caption(f"第 {revision} 版 · 供此主题的对话和专题研究使用")
        with st.form(f"project_memory_{topic.id}_{revision}"):
            venues = st.text_input("会议范围（用逗号分隔）", ", ".join(memory.venues))
            excluded = st.text_area("排除条件（每行一项）", "\n".join(memory.excluded))
            datasets = st.text_area("常用数据集（每行一项）", "\n".join(memory.datasets))
            protocol = st.text_area("研究口径", memory.research_protocol, max_chars=1500)
            main = st.checkbox("专题默认只看主会", memory.main_only)
            if st.form_submit_button("保存项目记忆"):
                try:
                    updated = ProjectMemory(venues=venues.replace("，", ",").split(","),
                                            excluded=excluded.splitlines(), datasets=datasets.splitlines(),
                                            research_protocol=protocol, main_only=main)
                    save_project_memory(store, topic.id, revision, updated)
                except ValueError as error:
                    st.warning(str(error))
                else:
                    st.rerun()


def adjustment_controls(store, conversation_id, key):
    with st.expander("调整正在进行的专题"):
        with st.form("research_adjust_" + key, clear_on_submit=True):
            focus = st.text_input("优先查证的问题", max_chars=800)
            venues = st.text_input("新的会议范围（用逗号分隔）")
            reset_venues = st.checkbox("清除会议范围限制")
            main = st.selectbox("主会范围", ["沿用当前范围", "仅主会", "取消仅主会限制"])
            paper = st.text_input("优先读取的 DOI 或 arXiv ID", max_chars=250)
            if st.form_submit_button("提交调整"):
                try:
                    scope = [] if reset_venues else venues.replace("，", ",").split(",") if venues.strip() else None
                    payload = ResearchAdjustment(focus=focus.strip() or None, venues=scope,
                        main_only=None if main == "沿用当前范围" else main == "仅主会", paper=paper.strip() or None)
                    queue_adjustment(store, conversation_id, key, payload)
                except ValueError as error:
                    st.warning(str(error))
                else:
                    st.rerun()


@st.fragment(run_every=3)
def research_card(store, conversation_id, key):
    matches = research_status(store, conversation_id, key)
    if not matches:
        return
    job = matches[0]
    view = job_card(job)
    spec = json.loads(job["spec"])
    state = json.loads(job["checkpoint"])
    with st.container(border=True, key="radar_card_research_" + key):
        st.markdown("**专题研究**")
        st.text(view["question"])
        st.caption(f"{spec['after']} 至 {spec['before']} · {RUN_LABELS.get(job['status'], job['status'])} · "
                   f"{STAGES.get(job['stage'], job['stage'])}")
        st.caption(f"`{key}` · {len(state.get('candidates', {}))} 篇候选 · {len(state.get('pdfs', {}))} 篇尝试读取全文")
        memory = spec.get("project_memory") or {}
        if memory.get("revision"):
            st.caption(f"项目记忆 · 第 {memory['revision']} 版")
        scope = spec.get("scope") or {}
        selected = "、".join(scope.get("venues") or [])
        if selected or scope.get("main_only"):
            st.caption("当前范围 · " + " · ".join(filter(None, (selected, "仅主会" if scope.get("main_only") else ""))))
        for change in adjustments(store, key)[-4:]:
            st.caption(("待应用" if change["status"] == "pending" else EVENT_STATUS.get(change["status"], change["status"]))
                       + " · " + "；".join(_display(value) for value in change["body"].values()))
        timeline_view(store, "research", key, live=job["status"] in {"queued", "running"})
        if job["status"] in {"queued", "running"}:
            if job["cancel_requested"]:
                st.caption("正在停止，当前步骤结束后生效")
            elif st.button("取消专题", key="cancel_" + key):
                cancel_job(store, conversation_id, key)
                st.rerun()
            else:
                adjustment_controls(store, conversation_id, key)
        if job["markdown"]:
            st.markdown(job["markdown"])
            st.download_button("下载专题 Markdown", job["markdown"], file_name=f"research-{key[:10]}.md",
                               mime="text/markdown", key="download_" + key)


def render_event(store, event, turn_id, index):
    name, outcome = event["name"], event["result"]
    data = outcome["data"]
    if name in {"propose_action", "start_research", "cancel_research"} and outcome["status"] != "error":
        return
    if outcome["status"] == "error":
        with st.expander(TOOL_LABELS.get(name, "工具") + " · 未完成"):
            st.text(str(data))
        return
    if name in {"search_library", "search_external_papers", "get_report"}:
        if name == "search_library":
            note = f"共找到 {data['total']} 篇"
            if data.get("partial_match"):
                note += "（按关键词部分匹配）"
            st.caption(note)
        elif name == "search_external_papers":
            coverage = data.get("coverage") or {}
            ok_sources = [SOURCE_LABELS.get(src, src) for src, info in coverage.items()
                          if isinstance(info, dict) and info.get("status") == "ok"]
            source_note = f" · 来源：{'、'.join(ok_sources)}" if ok_sources else ""
            st.caption(f"外部检索命中 {data['total']} 篇{source_note}")
        else:
            st.link_button(f"查看 {data['date']} 日报", route("reports", topic=data["topic"], report=data["id"]),
                           key=f"report_link_{turn_id}_{index}")
            entries = report_history(store, data["topic"], run_id=data["id"])
            if entries:
                st.download_button("下载日报 Markdown", entries[0]["markdown"], mime="text/markdown",
                                   file_name=f"{data['topic']}-{data['date']}.md", key=f"report_{turn_id}_{index}")
        paper_results(data["papers"])
    elif name == "fetch_paper_summary":
        st.caption("来源 · " + "、".join(data.get("sources_used") or ["摘要"]) + "（未下载 PDF）")
        if data.get("abstract"):
            with st.expander("查看摘要与网页信息"):
                st.text(data["abstract"])
                if data.get("webpage_excerpt"):
                    st.caption("网页摘录")
                    st.text(data["webpage_excerpt"][:800])
    elif name == "fetch_paper_pdf":
        if data.get("found"):
            st.caption(f"探索命中：{data.get('strategy')} · 共 {data.get('page_count')} 页（临时解析，未永久保存 PDF 原件）")
        else:
            st.warning(data.get("reason", "未获取到可用 PDF"))
        if data.get("attempts"):
            with st.expander(f"查看探索路径（共尝试 {len(data['attempts'])} 次）"):
                for att in data["attempts"]:
                    st.caption(f"[{att.get('strategy')}] {att.get('status')} · {att.get('url')} {att.get('reason', '')}")
    elif name == "rag_search":
        st.caption(f"全文片段命中 {data['total']} 条")
        for chunk in data.get("chunks", []):
            title = md(chunk["title"])
            page_info = f"PDF 第 {chunk['page_start']} 页" if chunk.get("page_start") else "全文"
            st.markdown(f"- [{title}]({source_link(chunk['url'])}) · {md(chunk['section'])} · {page_info}")
            st.caption(chunk["text"][:260] + ("…" if len(chunk["text"]) > 260 else ""))
    elif name == "deep_read_paper":
        analysis = data.get("analysis")
        if analysis:
            st.caption(f"精读等级 {data.get('level', 'L3')}")
            st.text(analysis.get("summary", ""))
            if analysis.get("datasets"):
                st.caption("数据集 · " + "、".join(analysis["datasets"]))
            if analysis.get("limitations"):
                st.caption("局限性 · " + analysis["limitations"])
        else:
            st.caption(data.get("reason", "暂未生成精读结果"))
    elif name == "get_paper":
        paper_results([data])
        if data["abstract"]:
            with st.expander("查看摘要"):
                st.text(data["abstract"])
        if data.get("card"):
            with st.expander("查看已有中文卡片"):
                card = data["card"]
                for field, label in (("summary", "内容总结"), ("problem", "研究问题"), ("method", "方法"),
                                     ("inference", "主题关联")):
                    value = card.get(field)
                    if value:
                        st.text(f"{label} · {value.get('statement', '') if isinstance(value, dict) else value}")
                for claim in card.get("author_claims", []):
                    st.text("作者主张 · " + claim["statement"])
    elif name == "set_paper_state":
        st.success(f"《{data['title']}》 · {'已收藏' if data['favorite'] else '未收藏'} · {'已读' if data['is_read'] else '未读'}")
    elif name == "enqueue_daily":
        st.caption("已复用当天任务" if data["reused"] else "已提交检索")
        live_run(store, data["id"], f"{turn_id}_{index}")
    elif name == "get_run":
        if data.get("kind") == "research":
            st.link_button("查看专题运行", route("runs", run=data["id"]), key=f"research_link_{turn_id}_{index}")
        else:
            run_summary(data)
    elif name == "get_trend":
        window = data["window"]
        st.caption(f"{data['as_of']} · {window['days']} 日 · {window['count']} 篇"
                   + (" · 覆盖不足" if not window["comparable"] else ""))
    elif name == "list_topics" and data:
        labels = {"followed": "已关注", "unfollowed": "未关注", "archived": "已删除"}
        with st.expander("主题查询结果"):
            for topic in data:
                st.text(f"{topic['name']} · {labels[topic['state']]} · {topic['schedule']}")
    elif name == "get_project_memory":
        st.caption(f"项目记忆 · 第 {data['revision']} 版")
        st.link_button("查看项目记忆", route("topics", edit=data["topic_id"]), key=f"memory_{turn_id}_{index}")
    elif name == "steer_research" and data.get("job"):
        records = adjustments(store, data["job"])
        status = next((item["status"] for item in records if item["id"] == data["id"]), data["status"])
        st.caption({"pending": "已提交调整，等待当前步骤结束", "applied": "专题已应用调整",
                    "cancelled": "专题调整已取消"}.get(status, status))
        st.link_button("查看专题进度", route("runs", run=data["job"]), key=f"steer_{turn_id}_{index}")
    elif name == "list_projects" and isinstance(data, list):
        for proj in data:
            st.caption(f"科研项目 · {proj['name']}（第 {proj['revision']} 版）")
    elif name == "compare_methods" and isinstance(data, list):
        for grp in data:
            st.caption(f"{grp['dataset']} · {grp['split']} · {grp['metric']}")
            st.dataframe(grp["entries"], hide_index=True)
    elif name == "list_research_events" and isinstance(data, list):
        st.caption(f"近期科研事件 {len(data)} 条")
    elif name == "run_gap_analysis" and data.get("markdown"):
        with st.expander("查看 Research Gap 分析报告"):
            st.markdown(data["markdown"])
    elif name in {"list_external_mcp", "call_external_mcp"}:
        with st.expander(TOOL_LABELS[name]):
            st.json(data)


@st.fragment(run_every=1.0)
def live_turn(store, settings, conversation_id, key):
    row_turn = store.one("SELECT status, reasoning, partial_answer FROM conversation_turns WHERE id=:id", id=key)
    status = row_turn["status"]
    if status == "queued":
        chat_worker().submit(store, settings, key)
    elif status != "running" or expire_turn(store, settings, key):
        st.rerun()
    if row_turn.get("reasoning"):
        with st.expander("思考过程（Thinking）", expanded=True):
            st.text(row_turn["reasoning"])
    if row_turn.get("partial_answer"):
        st.markdown(row_turn["partial_answer"] + " ▌")
    if status in {"queued", "running"} and st.button("停止生成", key="stop_turn_" + key):
        cancel_turn(store, conversation_id, key)
        st.rerun()
    timeline_view(store, "chat", key, live=True)


def assistant_page(store, topics):
    st.title("对话助手")
    initial_topic = st.query_params.get("topic")
    initial_topic = initial_topic if initial_topic in topics else None
    history = conversations(store, include_empty=True)
    if not history:
        new_conversation(store, initial_topic)
        history = conversations(store, include_empty=True)
    ids = [item["id"] for item in history]
    keyed = {item["id"]: item["title"] for item in history}
    linked = st.query_params.get("conversation")
    wanted = st.session_state.pop("new_chat", None) or linked
    if wanted not in ids:
        wanted = st.session_state.get("current_chat", ids[0])
    header, create = st.columns([5, 1], vertical_alignment="bottom")
    if create.button("新对话"):
        st.session_state["new_chat"] = new_conversation(store, initial_topic)
        st.rerun()
    current = header.selectbox("会话", ids, index=ids.index(wanted) if wanted in ids else 0,
                               format_func=keyed.get, key="chat_picker_" + (wanted if wanted in ids else ids[0]))
    if current != st.session_state.get("current_chat"):
        store.execute("UPDATE conversations SET opened_at=:now WHERE id=:id", now=utc_now().isoformat(), id=current)
        st.session_state["current_chat"] = current
    if current != linked:
        st.query_params["conversation"] = current
    history = turns(store, current)
    settings = Settings.from_env()
    if not history:
        st.markdown("查论文和日报、管理主题，或发起带原文证据的专题研究。")
    grouped = {}
    for job in research_status(store, current):
        grouped.setdefault(job["turn_id"], []).append(job["id"])
    for turn in history:
        is_callback = turn["prompt"] == "[后台专题完成回传]"
        with st.chat_message("assistant" if is_callback else "user"):
            if is_callback:
                st.html(status_pill("后台专题研究完成回传", "success", "spark"))
            else:
                st.markdown(md(turn["prompt"]))
        if is_callback:
            with st.chat_message("assistant"):
                if turn["answer"]:
                    st.markdown(turn["answer"])
            continue
        with st.chat_message("assistant"):
            if turn.get("reasoning") and turn["status"] not in {"queued", "running"}:
                with st.expander("思考过程（Thinking）"):
                    st.text(turn["reasoning"])
            if turn["answer"]:
                st.markdown(turn["answer"])
            elif turn["status"] in {"queued", "running"}:
                st.caption("正在处理这一轮")
                live_turn(store, settings, current, turn["id"])
            if turn["status"] not in {"queued", "running"}:
                timeline_view(store, "chat", turn["id"])
            for index, event in enumerate(json.loads(turn["events"])):
                render_event(store, event, turn["id"], index)
            for item in actions(store, turn["id"]):
                action_card(store, current, item)
            for key in grouped.get(turn["id"], []):
                research_card(store, current, key)
    busy = any(turn["status"] in {"queued", "running"} for turn in history)
    if prompt := st.chat_input("询问论文、管理主题或发起专题", max_chars=4500, disabled=busy):
        try:
            key = queue_turn(store, settings, current, prompt, turn_id=uuid4().hex)
            chat_worker().submit(store, settings, key)
        except ValueError as error:
            st.warning(str(error))
        else:
            st.rerun()
