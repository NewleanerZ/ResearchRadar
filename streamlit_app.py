from datetime import date, timedelta
import html
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import altair as alt
import httpx
import pandas as pd
from pydantic import ValidationError
import streamlit as st

from research_radar.chat_ui import assistant_page, memory_editor, research_card, route, timeline_view
from research_radar.clustering import CLUSTER_EVENT_LABELS, discover_clusters, ensure_cluster_snapshots, latest_dynamic_clusters
from research_radar.conversation import research_status
from research_radar.deep_read import deep_read_paper, translate_and_summarize_abstract
from research_radar.delivery import md, retry_notification
from research_radar.events import EVENT_LABELS, list_events, scan_library_events
from research_radar.feedback import record_feedback, reset_preference, set_preference_frozen, topic_preference_profile
from research_radar.gap_workflow import gap_history, run_gap_analysis
from research_radar.models import (
    CORE_SOURCES, SOURCE_LABELS, Settings, Topic, Publication, backfill_dates, backfill_venues, paper_publication,
    publication_label, utc_now,
)
from research_radar.period_reports import generate_period_report, period_report_history
from research_radar.projects import (
    ProjectInput, create_project, link_project_papers, list_projects, project_links, protocol_comparison_matrix,
)
from research_radar.rag import hybrid_search
from research_radar.ranking import best_record
from research_radar.settings_ui import embedding_diagnostics, settings_page
from research_radar.sources import catalog_applies
from research_radar.service import (
    RUN_LABELS, daily_overview, draft_topic, enqueue_backfill, enqueue_daily, find_card, library, open_store,
    report_history, run_history, sample_topics, trend_history,
)
from research_radar.store import norm, work_date
from research_radar.ui_theme import brand_header_html, paper_card_html, status_pill
from research_radar.ui_theme import theme_css
from research_radar.zotero import pull_zotero_collection, push_favorites_to_zotero

st.set_page_config(page_title="ResearchRadar", layout="wide")
ASSETS = Path(__file__).resolve().parent / "research_radar"


@st.cache_resource
def connect(data_dir):
    return open_store(Settings(data_dir=data_dir))


def lines(value):
    return [item.strip() for item in value.splitlines() if item.strip()]


def notice(message):
    st.session_state["notice"] = message
    st.rerun()


def navigate(page, topic=None):
    st.session_state["radar_page"] = page
    st.query_params["page"] = page
    if topic is not None:
        st.query_params["topic"] = topic


def open_topic_creation():
    st.session_state["add_topic"] = True
    navigate("topics")


def select_topic(topics, widget_key, options=None, empty_label=""):
    choices = list(topics) if options is None else options
    linked = st.query_params.get("topic")
    return st.selectbox("主题", choices, index=choices.index(linked) if linked in choices else 0,
                        format_func=lambda key: topics[key].name if key else empty_label, key=widget_key)


PUB_TONES = {
    "published": "success",
    "accepted": "primary",
    "submitted": "accent",
    "preprint": "neutral",
    "unknown": "warning",
}


def overview_page(store, topics):
    with st.container(key="radar_overview_intro"):
        content, mascot = st.columns([5, 2], vertical_alignment="center")
        content.title("研究概览")
        if topics:
            content.caption(f"已关注 {sum(topic.enabled for topic in topics.values())} 个研究主题 · 持续追踪论文、事件与趋势演化")
        mascot.image(ASSETS / "pet-mascot.png", width=108)
    if not topics:
        st.info("添加研究主题，即可检索论文并生成第一份日报。")
        create, example = st.columns(2)
        create.button("新建主题", on_click=open_topic_creation, type="primary")
        if example.button("载入示例主题"):
            existing = {topic.id for topic in store.topics()}
            missing = [topic for topic in sample_topics() if topic.id not in existing]
            if missing:
                for topic in missing:
                    store.save_topic(topic)
                notice("已载入示例主题")
            else:
                st.info("可在「主题管理 → 已删除」恢复示例主题")
        return
    key = select_topic(topics, "overview_topic")
    topic = topics[key]
    run, report = daily_overview(store, key)
    if topic.description:
        st.caption(topic.description)
    status, date_col, papers = st.columns(3)
    status.metric("最近一次检索", RUN_LABELS[run["status"]] if run else "尚未运行")
    date_col.metric("最新日报", report["local_day"] if report else "待生成")
    papers.metric("重点论文", len(report["papers"]) if report else "—")
    run_col, report_col, chat_col, refresh_col = st.columns(4)
    busy = bool(run and run["status"] in {"queued", "running"})
    if run_col.button("检索一次", type="primary", disabled=busy):
        try:
            enqueue_daily(store, key)
            current, _ = daily_overview(store, key)
            notice(f"今日检索状态 · {RUN_LABELS[current['status']]}")
        except ValueError as error:
            st.error(str(error))
    report_col.button("查看日报", on_click=navigate, args=("reports", key), disabled=report is None)
    chat_col.button("打开对话助手", on_click=navigate, args=("assistant", key))
    refresh_col.button("刷新状态")

    left_col, right_col = st.columns([3, 2], gap="large")
    with left_col:
        st.subheader("最新日报的重点论文")
        if report and report["papers"]:
            for index, paper in enumerate(report["papers"]):
                links = paper.get("links") or []
                pub = Publication(status=paper["status"], venue=paper.get("venue"))
                card = paper.get("card") or {}
                analysis = card.get("analysis") or {}
                summary = (analysis.get("summary") or {}).get("statement", "")
                inference = analysis.get("inference", "")
                rel = (paper.get("components") or {}).get("relevance")
                chip_links = [{"label": SOURCE_LABELS.get(lk["source"], lk["source"]), "url": lk["url"]} for lk in links]
                st.html(paper_card_html(
                    title=paper["title"],
                    url=links[0]["url"] if links else None,
                    authors="、".join(paper.get("authors") or [])[:90],
                    date_label=paper.get("published") or "",
                    pub_label=publication_label(pub),
                    pub_tone=PUB_TONES.get(pub.status, "neutral"),
                    relevance=rel,
                    summary=summary,
                    inference=inference,
                    links=chip_links,
                ))
        elif report:
            st.info("最新日报没有重点推荐。")
        else:
            st.info("点击上方「检索一次」生成该主题的首份重点论文推荐。")

    with right_col:
        st.subheader("近期科研事件与任务")
        events = list_events(store, key, limit=6)
        if events:
            for ev in events:
                payload = ev["payload"]
                ev_label = EVENT_LABELS.get(ev["event_type"], ev["event_type"])
                title = payload.get("title") or payload.get("label") or ""
                with st.container(border=True):
                    st.html(status_pill(ev_label, "accent", "spark"))
                    st.markdown(f"**{md(title)}**")
                    st.caption(ev["created_at"][:19])
        else:
            st.caption("暂无新科研事件（代码开源、顶会录用、热点升温会自动汇总在此）。")
        active_jobs = [j for j in research_status(store) if j["status"] in {"queued", "running"}]
        if active_jobs:
            st.markdown("**进行中的专题研究**")
            for job in active_jobs[:3]:
                q_text = json.loads(job["spec"]).get("question", "")[:70]
                st.html(f'<div class="rr-pill-row">{status_pill(RUN_LABELS.get(job["status"], job["status"]), "primary", "clock")}</div>')
                st.caption(q_text)


def advanced_topic_fields(topic):
    with st.expander("高级设置"):
        keywords = st.text_area("核心检索词，每行一个", value="\n".join(topic.keywords))
        excluded = st.text_area("排除词，每行一个", value="\n".join(topic.excluded))
        expansions = st.text_area("补充检索词，每行一个", value="\n".join(topic.expansions))
        categories = st.text_input("arXiv 分类，用逗号分隔", value=", ".join(topic.categories))
        subtopics = st.text_area("子主题，每行写为「名称 = 词1, 词2」", value="\n".join(
            label + " = " + ", ".join(terms) for label, terms in topic.subtopics.items()))
        cols = st.columns(2)
        timezone = cols[0].text_input("时区", value=topic.timezone)
        schedule = cols[1].time_input("每日检索时间", value=topic.schedule)
        notify = st.checkbox("允许飞书推送", value=topic.notify)
    return keywords, excluded, expansions, categories, subtopics, timezone, schedule, notify


def topic_from_fields(topic, name, description, enabled, fields):
    keywords, excluded, expansions, categories, subtopics, timezone, schedule, notify = fields
    groups = {}
    for entry in lines(subtopics):
        label, equals, terms = entry.partition("=")
        if not equals:
            raise ValueError("子主题请使用「名称 = 词1, 词2」格式")
        groups[label.strip()] = [term.strip() for term in terms.split(",") if term.strip()]
    return Topic.model_validate({**topic.model_dump(), "name": name, "description": description,
                                 "enabled": enabled, "keywords": lines(keywords), "excluded": lines(excluded),
                                 "expansions": lines(expansions), "categories": [
                                     term.strip() for term in categories.split(",") if term.strip()],
                                 "subtopics": groups, "timezone": timezone, "schedule": schedule, "notify": notify})


def topic_creation(store):
    st.subheader("添加主题")
    description = st.text_area("你想追踪什么研究方向？", max_chars=1500, key="new_topic_description",
                               placeholder="例如：跟踪工业工件的三维点云异常检测，关注缺陷定位，排除医疗影像。")
    if st.button("生成检索设置", type="primary", disabled=not description.strip()):
        try:
            with st.spinner("正在整理主题和检索词"), httpx.Client() as client:
                topic = draft_topic(description, Settings.from_env(), client)
            st.session_state["topic_draft"] = topic.model_dump()
        except ValueError as error:
            st.warning(str(error))
    saved = st.session_state.get("topic_draft")
    if saved and saved["description"] == description.strip():
        draft = Topic.model_validate(saved)
        draft = draft.model_copy(update={"expansions": draft.proposed_expansions, "proposed_expansions": []})
        st.subheader(draft.name)
        st.caption("核心检索词 · " + "、".join(draft.keywords))
        if draft.excluded:
            st.caption("排除词 · " + "、".join(draft.excluded))
        if draft.expansions:
            st.caption("补充检索词 · " + "、".join(draft.expansions))
        with st.form(f"confirm_topic_{draft.id}"):
            enabled = st.checkbox("关注此主题，每天自动检索", value=True)
            fields = advanced_topic_fields(draft)
            if st.form_submit_button("确认添加", type="primary"):
                try:
                    store.save_topic(topic_from_fields(draft, draft.name, draft.description, enabled, fields))
                    st.session_state.pop("topic_draft", None)
                    st.session_state["add_topic"] = False
                    notice(f"已添加「{draft.name}」")
                except ValueError as error:
                    st.error(str(error))
        return
    with st.expander("手动填写"):
        with st.form("create_manual_topic"):
            name = st.text_input("主题名称", max_chars=100)
            keywords = st.text_area("核心检索词，每行一个")
            follow = st.checkbox("关注此主题，每天自动检索", value=True)
            if st.form_submit_button("添加主题"):
                try:
                    store.save_topic(Topic(id=f"topic_{uuid4().hex[:12]}", name=name.strip(),
                                           description=description.strip(), keywords=lines(keywords), enabled=follow))
                    st.session_state["add_topic"] = False
                    notice(f"已添加「{name.strip()}」")
                except ValueError as error:
                    st.error(str(error))


def edit_topic(store, topic, revision):
    st.subheader(f"编辑「{topic.name}」")
    with st.form(f"edit_topic_{topic.id}_{revision}"):
        name = st.text_input("主题名称", topic.name, max_chars=100)
        description = st.text_area("研究方向", topic.description, max_chars=1500)
        fields = advanced_topic_fields(topic)
        if st.form_submit_button("保存修改", type="primary"):
            try:
                store.save_topic(topic_from_fields(topic, name.strip(), description.strip(), topic.enabled, fields),
                                 revision)
                st.session_state.pop("topic_edit_id", None)
                notice("主题已保存")
            except ValueError as error:
                st.error(str(error))
    memory_editor(store, topic)
    if topic.proposed_expansions:
        st.caption("待确认的补充检索词 · " + "、".join(topic.proposed_expansions))
        if st.button("确认补充检索词"):
            try:
                words = list(dict.fromkeys(topic.expansions + topic.proposed_expansions))
                store.save_topic(Topic.model_validate({**topic.model_dump(), "expansions": words,
                                                       "proposed_expansions": []}), revision)
                notice("补充检索词已保存")
            except ValueError as error:
                st.error(str(error))
    with st.expander("删除主题"):
        st.caption("主题会移到「已删除」，可在那里恢复并查看原有论文。")
        if st.button("确认删除主题"):
            try:
                store.save_topic(topic.model_copy(update={"enabled": False, "archived": True}), revision)
                st.session_state.pop("topic_edit_id", None)
                notice(f"已删除「{topic.name}」")
            except ValueError as error:
                st.error(str(error))


def topic_page(store, _topics):
    st.title("研究主题")
    all_topics = store.topics()
    linked = st.query_params.get("edit")
    if linked and linked in _topics and st.session_state.get("linked_topic_edit") != linked:
        st.session_state["linked_topic_edit"] = linked
        st.session_state["topic_edit_id"] = linked
    adding = st.session_state.get("add_topic", not all_topics)
    if st.button("返回主题管理" if adding else "添加主题", type="secondary" if adding else "primary"):
        st.session_state["add_topic"] = not adding
        st.rerun()
    if st.session_state.get("add_topic", not all_topics):
        with st.container(key="radar_card_topic_creation"):
            topic_creation(store)
        return
    if not all_topics:
        return
    search, state = st.columns([3, 2])
    query = search.text_input("搜索主题", placeholder="搜索名称或研究方向").strip().casefold()
    selection = state.selectbox("关注状态", ["全部", "已关注", "未关注", "已删除"])
    matching = []
    for topic in all_topics:
        label = "已删除" if topic.archived else "已关注" if topic.enabled else "未关注"
        if (selection == label or (selection == "全部" and not topic.archived)) and query in (
                topic.name + " " + topic.description).casefold():
            matching.append((topic, label))
    if not matching:
        st.info("没有匹配的主题。")
    for topic, label in matching:
        details, action, options = st.container(key=f"radar_card_topic_{topic.id}").columns(
            [4, 2, 1], vertical_alignment="center")
        details.markdown(f"**{md(topic.name)}**")
        details.caption(f"{label} · 每天 {topic.schedule:%H:%M}" if topic.enabled and not topic.archived else label)
        action_label = "恢复主题" if topic.archived else "取消关注" if topic.enabled else "关注"
        if action.button(action_label, key=f"follow_{topic.id}"):
            try:
                current, revision = store.topic(topic.id)
                if current == topic:
                    store.save_topic(current.model_copy(update={
                        "enabled": not topic.enabled and not topic.archived, "archived": False}), revision)
                    notice(f"{action_label}「{topic.name}」成功")
                st.rerun()
            except ValueError as error:
                st.error(str(error))
        if not topic.archived and options.button("编辑", key=f"edit_{topic.id}"):
            st.session_state["topic_edit_id"] = topic.id
    editing = st.session_state.get("topic_edit_id")
    if editing and any(topic.id == editing and not topic.archived for topic, _ in matching):
        edit_topic(store, *store.topic(editing))


def backfill_form(store, topic):
    with st.expander("按日期补录论文"):
        if topic.archived:
            st.info("恢复主题后可以补录论文。")
            return
        today = utc_now().astimezone(ZoneInfo(topic.timezone)).date()
        with st.form(f"date_backfill_{topic.id}"):
            first, last = st.columns(2)
            start = first.date_input("补录开始日期", today - timedelta(days=29), min_value=date.min, max_value=today)
            through = last.date_input("补录结束日期", today, min_value=date.min, max_value=today)
            st.caption("包含起止两天。按所选主题的关键词检索，论文去重后入库。")
            sources = (*CORE_SOURCES, *(key for key in Settings.from_env().catalog_sources()
                                        if catalog_applies(key, topic)))
            st.caption("检索来源 · " + "、".join(SOURCE_LABELS[key] for key in sources))
            st.caption("来源只给出月份或年份时，补录范围须包含整个对应月份或年份。")
            if st.form_submit_button("开始补录", type="primary"):
                try:
                    run_id = enqueue_backfill(store, topic.id, start, through, settings=Settings.from_env())
                    saved = store.one("SELECT status FROM runs WHERE id=:id", id=run_id)
                    notice(f"{start} 至 {through} 的补录 · {RUN_LABELS[saved['status']]}")
                except ValueError as error:
                    st.error(str(error))
        st.button("查看补录进度", on_click=navigate, args=("runs", topic.id))


def library_page(store, topics):
    st.title("论文库")
    if not topics:
        st.info("在「研究主题」中建立主题")
        return
    key = select_topic(topics, "library_topic")
    settings = Settings.from_env()
    backfill_form(store, topics[key])
    with st.expander("个性化兴趣画像"):
        profile = topic_preference_profile(store, key)
        st.caption(
            f"正向样本 {profile.get('positive_count', 0)} 篇 · 负向样本 {profile.get('negative_count', 0)} 篇 · "
            + ("已冻结" if profile.get("frozen") else "自动学习中")
        )
        if profile.get("positive_terms"):
            st.caption("偏好词 · " + "、".join(profile["positive_terms"][:8]))
        p_cols = st.columns(2)
        if p_cols[0].button("解除冻结" if profile.get("frozen") else "冻结画像", key=f"freeze_pref_{key}"):
            set_preference_frozen(store, key, not profile.get("frozen"))
            st.rerun()
        if p_cols[1].button("重置画像", key=f"reset_pref_{key}"):
            reset_preference(store, key)
            notice("已重置个性化画像")
    q_col, mode_col = st.columns([4, 1], vertical_alignment="bottom")
    query = q_col.text_input("搜索标题、摘要、作者或全文")
    rag_mode = mode_col.checkbox("全文 RAG 检索")
    if rag_mode and query.strip():
        hits = hybrid_search(store, settings, query.strip(), topic_id=key, limit=10)
        st.caption(f"全文片段命中 {len(hits)} 条")
        for hit in hits:
            with st.container(border=True):
                st.markdown(f"**[{md(hit['title'])}]({hit['url']})** · {md(hit['section'])} · 第 {hit['page_start']} 页")
                st.text(hit["text"][:500])
        return
    cols = st.columns(5)
    start = cols[0].date_input("起始日期", None)
    end = cols[1].date_input("结束日期", None, min_value=start)
    favorite = cols[2].checkbox("只看收藏")
    unread = cols[3].checkbox("只看未读")
    pdf_only = cols[4].checkbox("只看可获取 PDF")
    if st.button("预检测本主题 PDF 可用性", key=f"precheck_{key}"):
        from research_radar.pdf_explorer import precheck_library_pdfs
        with st.spinner("正在轻量预检测本主题论文是否可获取 PDF..."):
            res = precheck_library_pdfs(store, settings, key, limit=120)
        notice(f"预检测完成：可获取 PDF {res['available']} 篇，无公开 PDF {res['unavailable']} 篇")
    papers = library(store, topics[key], query, start, end, favorite, unread, pdf_only)
    available = [source for source in SOURCE_LABELS
                 if any(record.source == source for item in papers for record in item["records"])]
    if available:
        chosen = st.multiselect("来源", available, format_func=SOURCE_LABELS.get, placeholder="所有来源")
        if chosen:
            papers = [item for item in papers if any(record.source in chosen for record in item["records"])]
    st.caption(f"{len(papers)} 篇")
    if not papers:
        return
    page = st.number_input("页码", min_value=1, max_value=(len(papers) + 24) // 25, step=1)
    for item in papers[(page - 1) * 25:page * 25]:
        record = best_record(item)
        paper_id = item["paper_id"]
        with st.expander(f"{record.title} · {item['date_label'] or '日期未确认'}"):
            st.caption(" · ".join(record.authors[:8]) or "作者未确认")
            pdf_badge = ("✅ 可获取 PDF" + (f" ({item['pdf_strategy']})" if item.get("pdf_strategy") else "")
                         if item.get("pdf_status") == "available"
                         else "❌ 无公开 PDF" if item.get("pdf_status") == "unavailable" else "⏳ PDF 待检测")
            st.markdown(f"**发表状态** · {md(publication_label(paper_publication(item['records'])))} · {pdf_badge}")
            for source in item["records"]:
                st.link_button(SOURCE_LABELS[source.source], source.url, key=f"source_{paper_id}_{source.source}_{source.external_id}")
            card = find_card(store, key, paper_id)
            if card and card["analysis"]:
                st.text(card["analysis"]["summary"]["statement"])
                if card["analysis"]["inference"]:
                    st.caption("主题关联 · " + card["analysis"]["inference"])
                for evidence in card["evidence"]:
                    st.caption(evidence["text"])
            elif record.abstract:
                st.text(record.abstract)
            note_row = store.one(
                "SELECT translation, summary, model FROM paper_abstract_notes WHERE paper_id=:p",
                p=paper_id,
            )
            if note_row:
                st.markdown(f"**AI 摘要总结**：{note_row['summary']}")
                st.caption(f"摘要中文翻译（{note_row['model'] or 'LLM'}）：{note_row['translation']}")
            elif record.abstract and st.button("生成摘要中文翻译与总结", key=f"abs_note_{paper_id}"):
                with st.spinner("正在翻译并总结摘要"), httpx.Client() as client:
                    res = translate_and_summarize_abstract(store, settings, client, paper_id, item["title"], item["records"])
                if res.get("available"):
                    notice("摘要中文翻译与总结已完成")
                else:
                    st.warning(res.get("reason", "摘要翻译/总结未完成"))
            deep = store.one(
                "SELECT level, result_json FROM deep_analyses WHERE topic_id=:t AND paper_id=:p ORDER BY created_at DESC LIMIT 1",
                t=key, p=paper_id,
            )
            if deep:
                d_res = json.loads(deep["result_json"])
                st.info(f"精读（{deep['level']}）：{d_res.get('summary', '')}")
            cols = st.columns(6)
            if cols[0].button("取消收藏" if item["favorite"] else "收藏", key=f"fav_{paper_id}"):
                store.set_reading_state(key, paper_id, not item["favorite"], None)
                st.rerun()
            if cols[1].button("标记未读" if item["is_read"] else "标记已读", key=f"read_{paper_id}"):
                store.set_reading_state(key, paper_id, None, not item["is_read"])
                st.rerun()
            if cols[2].button("喜欢", key=f"like_{paper_id}"):
                record_feedback(store, key, paper_id, "like", settings=settings)
                notice("已记录喜欢并更新兴趣画像")
            if cols[3].button("忽略", key=f"ignore_{paper_id}"):
                record_feedback(store, key, paper_id, "ignore", settings=settings)
                notice("已记录忽略")
            if cols[4].button("摘要/网页精读", key=f"deep_l2_{paper_id}"):
                with st.spinner("正在基于摘要和网页信息总结"), httpx.Client() as client:
                    res = deep_read_paper(store, settings, client, topics[key], paper_id, item["records"], level="L2", fetch_pdf=False)
                if res.get("analysis"):
                    notice("摘要/网页精读完成")
                else:
                    st.warning(res.get("reason", "精读未完成"))
            if cols[5].button("获取 PDF 精读", key=f"deep_{paper_id}"):
                with st.spinner("正在探索获取 PDF 并精读"), httpx.Client() as client:
                    res = deep_read_paper(store, settings, client, topics[key], paper_id, item["records"], level="L3", fetch_pdf=True)
                if res.get("analysis"):
                    notice("PDF 精读完成")
                else:
                    st.warning(res.get("reason", "精读未完成"))


def _library_trend_data(store, topic: Topic, days: int, today: date):
    zone = ZoneInfo(topic.timezone)
    start = today - timedelta(days=days - 1)
    prior_start = start - timedelta(days=days)
    all_papers = store.papers(topic.id)
    subtopic_terms = {
        label: [f" {norm(term)} " for term in terms if norm(term)]
        for label, terms in topic.subtopics.items()
    }
    bucket_counts: dict[tuple[str, str], int] = {}
    cur_hits: dict[str, list[dict]] = {"全部": [], **{lbl: [] for lbl in subtopic_terms}}
    prev_counts: dict[str, int] = {"全部": 0, **{lbl: 0 for lbl in subtopic_terms}}
    for paper in all_papers:
        when = work_date(paper["records"], zone)
        if not when:
            continue
        content = " " + norm(" ".join(f"{r.title} {r.abstract or ''}" for r in paper["records"])) + " "
        matched_labels = [lbl for lbl, terms in subtopic_terms.items() if any(t in content for t in terms)]
        if start <= when <= today:
            bucket = (when - timedelta(days=when.weekday())).isoformat() if days > 30 else when.isoformat()
            cur_hits["全部"].append(paper)
            if not subtopic_terms:
                bucket_counts[(bucket, "全部")] = bucket_counts.get((bucket, "全部"), 0) + 1
            for lbl in matched_labels:
                cur_hits[lbl].append(paper)
                bucket_counts[(bucket, lbl)] = bucket_counts.get((bucket, lbl), 0) + 1
            if subtopic_terms and not matched_labels:
                bucket_counts[(bucket, "未归类")] = bucket_counts.get((bucket, "未归类"), 0) + 1
        elif prior_start <= when < start:
            prev_counts["全部"] += 1
            for lbl in matched_labels:
                prev_counts[lbl] += 1
    series = [
        {"日期": day_str, "子主题": lbl, "篇数": cnt}
        for (day_str, lbl), cnt in sorted(bucket_counts.items())
    ]
    bars = [
        {"子主题": lbl, "篇数": len(cur_hits[lbl])}
        for lbl in subtopic_terms
    ]
    return series, bars, cur_hits, prev_counts


def reports_page(store, topics):
    st.title("日报与趋势")
    if not topics:
        st.info("在「研究主题」中建立主题")
        return
    key = select_topic(topics, "reports_topic")
    topic = topics[key]
    settings = Settings.from_env()
    reports = report_history(store, key)
    today_local = utc_now().astimezone(ZoneInfo(topic.timezone)).date()
    report_tab, period_tab, trend_tab, dynamic_tab = st.tabs(["日报", "周报与月报", "固定趋势", "动态聚类与事件"])
    with report_tab:
        if reports:
            report_id = st.query_params.get("report")
            default = next((i for i, report in enumerate(reports) if report["id"] == report_id), 0)
            day = st.selectbox("报告日期", [report["local_day"] for report in reports], index=default)
            report = next(item for item in reports if item["local_day"] == day)
            st.download_button("下载 Markdown", report["markdown"], file_name=f"{key}-{day}.md", mime="text/markdown")
            st.markdown(report["markdown"])
        else:
            st.info("主题完成首次日报后，将在这里显示")
    with period_tab:
        periods = period_report_history(store, key)
        w_cnt = sum(1 for p in periods if p["report_type"] == "weekly")
        m_cnt = sum(1 for p in periods if p["report_type"] == "monthly")
        cur_w = len(library(store, topic, after=today_local - timedelta(days=6), before=today_local))
        cur_m = len(library(store, topic, after=today_local - timedelta(days=29), before=today_local))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("已生成周报", f"{w_cnt} 份")
        m2.metric("已生成月报", f"{m_cnt} 份")
        m3.metric("近 7 天论文", f"{cur_w} 篇")
        m4.metric("近 30 天论文", f"{cur_m} 篇")
        force_regen = st.checkbox("覆盖重新生成（含最新论文、BGE 聚类与科研事件）", value=True)
        p_cols = st.columns(3)
        if p_cols[0].button("生成本周周报"):
            generate_period_report(store, settings, topic, today_local, "weekly", force=force_regen)
            notice("本周周报已就绪")
        if p_cols[1].button("生成本月月报"):
            generate_period_report(store, settings, topic, today_local, "monthly", force=force_regen)
            notice("本月月报已就绪")
        if p_cols[2].button("一键生成周报与月报", type="primary"):
            generate_period_report(store, settings, topic, today_local, "weekly", force=True)
            generate_period_report(store, settings, topic, today_local, "monthly", force=True)
            notice("周报与月报已全部刷新")
        periods = period_report_history(store, key)
        if periods:
            chosen = st.selectbox(
                "选择周期报告",
                [f"{'周报' if p['report_type'] == 'weekly' else '月报'} · {p['period_key']}" for p in periods],
            )
            picked = next(
                p for p in periods
                if f"{'周报' if p['report_type'] == 'weekly' else '月报'} · {p['period_key']}" == chosen
            )
            st.download_button("下载周期报告 Markdown", picked["markdown"],
                               file_name=f"{key}-{picked['report_type']}-{picked['period_key']}.md", mime="text/markdown")
            st.markdown(picked["markdown"])
        else:
            st.info("点击上方「一键生成周报与月报」即可立即生成。")
    with trend_tab:
        st.caption("趋势覆盖按 arXiv 和 OpenAlex 历史扫描与库内已收录论文日期综合计算。")
        snapshots = trend_history(store, key)
        days = st.selectbox("统计窗口（天）", [7, 30, 90, 180], index=2)
        lib_series, lib_bars, cur_hits, prev_counts = _library_trend_data(store, topic, days, today_local)
        available_labels = ["全部", *topic.subtopics.keys()]
        if snapshots and days in (7, 30, 90):
            version = st.selectbox("快照版本", list(dict.fromkeys(item["version"] for item in snapshots)))
            ver_snaps = [item for item in snapshots if item["version"] == version]
            latest = ver_snaps[0]
            available_labels = list(dict.fromkeys(item["label"] for item in latest["windows"]))
        else:
            latest = None
            ver_snaps = []
        label = st.selectbox("查看子主题", available_labels)

        series_rows = [
            {"日期": snap["as_of"], "子主题": item["label"], "篇数": item["count"]}
            for snap in ver_snaps for item in snap["windows"]
            if item["days"] == days and item["coverage_complete"] and item["label"] != "全部"
        ]
        using_live_fallback = len({r["日期"] for r in series_rows}) < 2
        chart_rows = lib_series if using_live_fallback else series_rows
        if using_live_fallback and lib_series:
            st.caption("当前历史数据源尚在分批补录或运行天数较少，下图按库内已收录论文的实际日期展示演化曲线与子主题分布。")

        c_left, c_right = st.columns([3, 2], gap="large") if lib_bars else (st.container(), None)
        with c_left:
            if chart_rows:
                chart_df = pd.DataFrame(chart_rows)
                chart = (
                    alt.Chart(chart_df)
                    .mark_line(point=True, strokeWidth=2.4)
                    .encode(
                        x=alt.X("日期:T", title="时间"),
                        y=alt.Y("篇数:Q", title="论文数"),
                        color=alt.Color("子主题:N", scale=alt.Scale(
                            range=["#7C3AED", "#0891B2", "#059669", "#D97706", "#DC2626", "#6366F1"]
                        )),
                        tooltip=["日期:T", "子主题:N", "篇数:Q"],
                    )
                    .properties(height=280, title=f"近 {days} 天按时间分布")
                    .interactive()
                )
                st.altair_chart(chart, use_container_width=True)
            else:
                st.info(f"近 {days} 天暂未检索到带日期的论文记录，可增大统计窗口或运行一次补录。")
        if c_right is not None and lib_bars:
            with c_right:
                bar_df = pd.DataFrame(lib_bars)
                bar_chart = (
                    alt.Chart(bar_df)
                    .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
                    .encode(
                        y=alt.Y("子主题:N", sort="-x", title=""),
                        x=alt.X("篇数:Q", title="论文数"),
                        color=alt.Color("子主题:N", legend=None, scale=alt.Scale(
                            range=["#7C3AED", "#0891B2", "#059669", "#D97706", "#DC2626", "#6366F1"]
                        )),
                        tooltip=["子主题:N", "篇数:Q"],
                    )
                    .properties(height=280, title="子主题篇数对比")
                )
                st.altair_chart(bar_chart, use_container_width=True)

        if chart_rows:
            with st.expander("查看趋势明细表"):
                st.dataframe(pd.DataFrame(chart_rows), hide_index=True, use_container_width=True)

        snap_win = (
            next((item for item in latest["windows"] if item["days"] == days and item["label"] == label), None)
            if latest else None
        )
        live_list = cur_hits.get(label, [])
        obs_count = snap_win["count"] if (snap_win and snap_win["coverage_complete"]) else len(live_list)
        prior_count = (snap_win["prior"] if (snap_win and snap_win["comparable"])
                       else prev_counts.get(label, 0))
        cur_total = max(1, len(cur_hits.get("全部", [])))
        prev_total = max(1, prev_counts.get("全部", 0))
        share_delta = (snap_win["delta"] if (snap_win and snap_win["comparable"] and snap_win["delta"] is not None)
                       else (len(live_list) / cur_total - prev_counts.get(label, 0) / prev_total) if prev_counts.get("全部", 0) else None)
        cols = st.columns(3)
        cols[0].metric("当前窗口篇数", obs_count)
        cols[1].metric("上一同等窗口", prior_count if prev_counts.get("全部", 0) or (snap_win and snap_win["comparable"]) else "暂无")
        cols[2].metric("份额变化", f"{share_delta:+.1%}" if share_delta is not None else "暂无对比")
        if snap_win and snap_win["heating"]:
            st.success("该子主题连续两次周度评估升温")
        if latest:
            st.caption("数据源扫描记录 · " + " · ".join(
                f"{SOURCE_LABELS.get(source, source)} ({data['current_scan']})" for source, data in latest["coverage"].items()
            ))
        rep_papers = sorted(
            live_list,
            key=lambda p: (
                max((r.citation_count for r in p["records"] if r.citation_count is not None), default=-1),
                work_date(p["records"], ZoneInfo(topic.timezone)) or date.min,
            ),
            reverse=True,
        )[:8]
        if rep_papers:
            st.markdown(f"**「{md(label)}」代表论文**")
            for paper in rep_papers:
                record = best_record(paper)
                pub = publication_label(paper_publication(paper["records"]))
                cites = max((r.citation_count for r in paper["records"] if r.citation_count is not None), default=None)
                when = work_date(paper["records"], ZoneInfo(topic.timezone))
                meta = " · ".join(x for x in [when.isoformat() if when else None, pub, f"引用 {cites}" if cites is not None else None] if x)
                st.markdown(f"- [{md(record.title)}]({record.url})（{md(meta)}）")
    with dynamic_tab:
        if st.button("刷新动态语义聚类与事件", type="primary"):
            with st.spinner("正在执行 BGE 语义聚类与全库事件扫描..."):
                res = ensure_cluster_snapshots(store, settings, topic, today_local + timedelta(days=1))
                ev_added = scan_library_events(store, topic, limit=200)
            if res["mode"] != "dynamic":
                st.info(res.get("reason", "暂无法聚类"))
            else:
                st.success(f"动态聚类已更新（共 {len(res['clusters'])} 个方向，新增扫描 {len(ev_added)} 条科研事件）")
        dyn = latest_dynamic_clusters(store, key)
        if dyn["clusters"]:
            st.caption(f"聚类快照截止日期：{dyn['as_of']}（近 30 天窗口）")
            ev_tones = {"emerging": "primary", "growing": "success", "declining": "warning",
                        "merged": "accent", "split": "accent", "stable": "neutral"}
            papers_map = {p["paper_id"]: p for p in store.papers(key)}
            for c in dyn["clusters"]:
                m = c["metrics"]
                h_str = f"{m['hotspot_index']:.2f}" if m["hotspot_index"] is not None else m["note"]
                ev_cn = CLUSTER_EVENT_LABELS.get(c["event_type"], c["event_type"])
                with st.container(border=True):
                    c_head, c_stat = st.columns([3, 1])
                    c_head.markdown(f"**{md(c['label'])}**")
                    c_stat.html(status_pill(ev_cn, ev_tones.get(c["event_type"], "neutral"), "spark"))
                    st.caption(f"篇数 {m['count']} · 增长 {m['growth']:+.2f} · 份额 {m['share']:.1%} · 热点指数 H：{h_str}")
                    for pid in c.get("representative_ids", [])[:3]:
                        if pid in papers_map:
                            rec = best_record(papers_map[pid])
                            st.markdown(f"  - [{md(rec.title)}]({rec.url})")
        else:
            st.info("暂无动态聚类结果，点击上方「刷新动态语义聚类与事件」即可立即生成（需近 30 日语料 >= 15 篇且启用 BGE 模型）。")
        events = list_events(store, key, limit=25)
        if events:
            st.subheader(f"近期科研事件（共 {len(events)} 条）")
            st.dataframe(
                [{"类型": EVENT_LABELS.get(e["event_type"], e["event_type"]),
                  "论文/方向": e["payload"].get("title") or e["payload"].get("label") or "",
                  "详情": e["payload"].get("venue") or e["payload"].get("url") or e["payload"].get("version") or "",
                  "时间": e["created_at"][:19]} for e in events],
                hide_index=True,
                use_container_width=True,
            )


def projects_page(store, topics):
    st.title("科研项目与空白")
    if not topics:
        st.info("请先在「研究主题」中创建主题。")
        return
    key = select_topic(topics, "projects_topic")
    settings = Settings.from_env()
    proj_tab, gap_tab, zot_tab = st.tabs(["项目档案与方法对比", "Research Gap 与实验设计", "Zotero 同步"])
    with proj_tab:
        with st.expander("新建科研项目档案"):
            with st.form(f"create_project_{key}"):
                p_name = st.text_input("项目名称")
                p_q = st.text_area("研究问题")
                p_method = st.text_area("当前方法与思路")
                p_ds = st.text_input("数据集（用逗号分隔）")
                p_ms = st.text_input("评价指标（用逗号分隔）")
                if st.form_submit_button("保存项目", type="primary"):
                    try:
                        create_project(store, key, ProjectInput(
                            name=p_name, question=p_q, current_method=p_method,
                            datasets=[x.strip() for x in p_ds.split(",") if x.strip()],
                            metrics=[x.strip() for x in p_ms.split(",") if x.strip()],
                        ))
                        notice("科研项目已保存")
                    except (ValueError, ValidationError) as err:
                        st.error(str(err))
        projects = list_projects(store, key)
        if not projects:
            st.info("当前主题下暂无科研项目档案。")
        for proj in projects:
            with st.expander(f"{proj['name']} · 第 {proj['revision']} 版", expanded=True):
                st.write(proj["question"])
                if proj["datasets"]:
                    st.caption("数据集 · " + "、".join(proj["datasets"]))
                if st.button("关联相关论文", key=f"link_{proj['id']}"):
                    linked = link_project_papers(store, settings, proj["id"])
                    notice(f"关联完成，共匹配 {len(linked)} 篇")
                links = project_links(store, proj["id"])
                if links:
                    st.dataframe([{"论文": l["title"], "关系": l["relation_type"], "说明": l["notes"]} for l in links[:10]],
                                 hide_index=True)
                matrix = protocol_comparison_matrix(store, proj["id"])
                if matrix:
                    st.markdown("**同协议方法对比矩阵**")
                    for grp in matrix:
                        st.caption(f"{grp['dataset']} · {grp['split']} · {grp['metric']} · {grp['protocol']}")
                        st.dataframe(grp["entries"], hide_index=True)
    with gap_tab:
        projects = list_projects(store, key)
        choices = {"": "仅基于主题分析", **{p["id"]: p["name"] for p in projects}}
        picked = st.selectbox("关联项目", list(choices), format_func=choices.get)
        if st.button("运行 Research Gap 反向查证", type="primary"):
            with st.spinner("正在收集证据并反向查证"):
                run_gap_analysis(store, settings, key, project_id=picked or None)
            notice("分析已完成")
        for g in gap_history(store, key):
            with st.expander(f"{g['created_at'][:19]} · {g['status']}"):
                if g["markdown"]:
                    st.download_button("下载分析报告", g["markdown"], file_name=f"{g['id']}.md",
                                       mime="text/markdown", key=f"dl_{g['id']}")
                    st.markdown(g["markdown"])
    with zot_tab:
        col = st.text_input("Zotero Collection Key（可留空同步全库）")
        z_cols = st.columns(2)
        if z_cols[0].button("从 Zotero 导入"):
            try:
                res = pull_zotero_collection(store, settings, key, collection_key=col)
                notice(f"Zotero 导入 {res['imported']} 篇（版本 {res['version']}）")
            except Exception as err:
                st.error(str(err))
        if z_cols[1].button("推送收藏到 Zotero"):
            try:
                res = push_favorites_to_zotero(store, settings, key, collection_key=col)
                notice(f"已导出 {res['exported']} 篇收藏")
            except Exception as err:
                st.error(str(err))


def runs_page(store, topics):
    st.title("运行状态")
    key = select_topic(topics, "runs_topic", ["", *topics], "全部主题")
    st.button("刷新运行记录")
    linked = st.query_params.get("run")
    if linked and st.button("显示全部运行"):
        del st.query_params["run"]
        st.rerun()
    research = [job for job in research_status(store, run_id=linked)
                if not key or json.loads(job["spec"]).get("topic") == key]
    if research:
        st.subheader("专题任务")
        for job in research[:20]:
            with st.expander(json.loads(job["spec"])["question"][:100], expanded=bool(linked)):
                st.link_button("打开原对话", route("assistant", conversation=job["conversation_id"]),
                               key="chat_" + job["id"])
                research_card(store, job["conversation_id"], job["id"])
    jobs = run_history(store, key, linked)
    for run in jobs:
        dates = backfill_dates(run["kind"])
        job_label = f"按日期补录 {dates[0]} 至 {dates[1]}" if dates else run["kind"]
        with st.expander(f"{run['local_day']} · {run['topic_id']} · {job_label} · {RUN_LABELS[run['status']]}",
                         expanded=bool(linked)):
            st.caption(run["id"])
            timeline_view(store, "run", run["id"])
            if venues := backfill_venues(run["kind"]):
                st.caption("会议范围 · " + "、".join(venues))
            if run["error"]:
                st.warning(run["error"])
            scans = store.all("""SELECT source,stream,range_start,range_end,status,pages,received,detail FROM scans
                                 WHERE run_id=:id ORDER BY source,stream""", id=run["id"])
            if scans:
                st.dataframe(scans, hide_index=True, width="stretch")
            usage = json.loads(run["usage"])
            if usage:
                st.json(usage)
            if run["notification"]:
                st.caption(f"飞书 · {run['notification']} · 已尝试 {run['attempts']} 次")
                if run["delivery_detail"]:
                    st.caption(run["delivery_detail"])
                if run["notification"] in {"uncertain", "failed"}:
                    checked = st.checkbox("已核对目标群，确认需要重发", key=f"check_{run['id']}")
                    if st.button("重新投递", disabled=not checked, key=f"resend_{run['id']}"):
                        retry_notification(store, run["id"])
                        notice("已加入通知队列")
            if run["status"] in {"failed", "partial"}:
                if st.button("重试任务", key=f"retry_{run['id']}"):
                    try:
                        store.retry(run["id"])
                        notice("已重新加入运行队列")
                    except ValueError as error:
                        st.error(str(error))


db = connect(Settings.from_env().data_dir.resolve())
topics_by_id = {topic.id: topic for topic in db.topics() if not topic.archived}
st.sidebar.html(brand_header_html())

t_col1, t_col2 = st.sidebar.columns(2)
dark_mode = t_col1.toggle("深色模式", key="rr_dark_mode", value=False)
compact_mode = t_col2.toggle("紧凑视图", key="rr_compact_mode", value=False)
st.html(theme_css("dark" if dark_mode else "light", "compact" if compact_mode else "comfortable"))

diag = embedding_diagnostics(Settings.from_env())
st.sidebar.html(
    '<div class="rr-pill-row">'
    + status_pill("BGE 就绪" if diag["ready"] else "BGE 未下载", "success" if diag["ready"] else "warning",
                  "check" if diag["ready"] else "alert")
    + status_pill(f"{len(topics_by_id)} 个主题", "primary", "topics")
    + "</div>"
)

pages = {
    "overview": ("研究概览", overview_page),
    "assistant": ("对话助手", assistant_page),
    "library": ("论文库", library_page),
    "reports": ("日报与趋势", reports_page),
    "projects": ("科研项目与空白", projects_page),
    "topics": ("主题管理", topic_page),
    "runs": ("运行状态", runs_page),
    "settings": ("系统配置", settings_page),
}
if "radar_page" not in st.session_state:
    linked_page = st.query_params.get("page")
    st.session_state["radar_page"] = linked_page if linked_page in pages else "overview"
page = st.sidebar.radio("导航", list(pages), format_func=lambda route: pages[route][0], key="radar_page",
                        label_visibility="collapsed")
message = st.session_state.pop("notice", None)
if message:
    st.success(message)
pages[page][1](db, topics_by_id)
