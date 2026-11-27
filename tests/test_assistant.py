from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from research_radar.activity import timeline
from research_radar.conversation import actions, cancel_job, confirm_action, expire_turn, new_conversation, queue_turn, research_status, submit_turn, turns
from research_radar.delivery import retry_notification, send_one
from research_radar.models import Page, Publication, Settings, backfill_venues, utc_now
from research_radar.project_memory import ProjectMemory, project_memory, save_project_memory
from research_radar.research import tick_research
from research_radar.research_control import ResearchAdjustment, adjustments, queue_adjustment
from research_radar.service import sample_topics
from test_radar import NOW, FixtureSources, preprint, published, radar, worker


def call(name, arguments, key=None):
    return {"id": key or name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def complete(text=None, calls=()):
    return httpx.Response(200, json={"choices": [{"message": {"content": text, "tool_calls": list(calls)}}],
                                    "usage": {"prompt_tokens": 180, "completion_tokens": 45}})


def model(store):
    return Settings(data_dir=store.directory, llm_model="deepseek-flash", llm_key="fixture",
                    llm_base_url="https://api.deepseek.com")


def respond(store, key, prompt, handler):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        turn = submit_turn(store, model(store), key, prompt, client=client)
    return next(item for item in turns(store, key) if item["id"] == turn)


def test_native_calls_ground_ordinal_and_isolate_conversations(radar):
    store, topic = radar
    run_id = store.enqueue(topic, NOW)
    run = store.one("SELECT * FROM runs WHERE id=:id", id=run_id)
    store.start_scan(run_id, "arxiv", "main", NOW.date(), NOW.date())
    store.save_page(run, topic, "arxiv", "main", [preprint(1), preprint(2)], None, NOW.date())
    key = new_conversation(store, topic.id)

    def read(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["tool_choice"] == "auto"
        assert "response_format" not in body
        if body["messages"][-1]["role"] == "tool":
            assert body["messages"][-2]["tool_call_id"] == "topics"
            assert body["messages"][-1]["tool_call_id"] == "papers"
            return complete("已找到论文")
        return complete(calls=[call("list_topics", {}, "topics"), call("search_library", {}, "papers")])

    previous = respond(store, key, "查一下当前库的论文", read)
    assert previous["status"] == "done"
    cards = json.loads(previous["events"])[-1]["result"]["data"]["papers"]

    def selected(reference):
        return lambda request: complete("查看执行回执") if json.loads(request.content)["messages"][-1]["role"] == "tool" else complete(
            calls=[call("set_paper_state", {"paper": reference, "favorite": True})])

    mismatched = respond(store, key, "收藏第二篇", selected(cards[0]["reference"]))
    assert json.loads(mismatched["events"])[0]["result"]["error_code"] == "invalid_target"
    draft = respond(store, key, "我收藏的第二篇是什么", selected(cards[1]["reference"]))
    assert json.loads(draft["events"])[0]["result"]["status"] == "error"
    success = respond(store, key, "把第二篇收藏", selected("第二篇"))
    saved = json.loads(success["events"])[0]["result"]
    assert saved["data"]["reference"] == cards[1]["reference"] and saved["data"]["favorite"]
    other = respond(store, new_conversation(store, topic.id), "收藏第二篇", selected("第二篇"))
    assert json.loads(other["events"])[0]["result"]["status"] == "error"


def test_confirmation_receipt_and_stale_topic_revision(radar):
    store, topic = radar
    key = new_conversation(store, topic.id)

    def action(args):
        return lambda request: complete("请确认") if json.loads(request.content)["messages"][-1]["role"] == "tool" else complete(
            calls=[call("propose_action", args)])

    turn = respond(store, key, "暂停关注这个主题", action({"action": "unfollow"}))
    card = actions(store, turn["id"])[0]
    assert card["status"] == "pending" and store.topic(topic.id)[0].enabled
    receipt = confirm_action(store, key, card["id"])
    assert receipt == confirm_action(store, key, card["id"])
    saved, revision = store.topic(topic.id)
    assert not saved.enabled and revision == 2
    assert confirm_action(store, key, card["id"], approve=False) == receipt
    with pytest.raises(ValueError, match="确认卡"):
        confirm_action(store, new_conversation(store), card["id"])

    day = utc_now().date()
    turn = respond(store, key, "给这个主题补录两天的论文", action({
        "action": "backfill", "after": (day - timedelta(days=3)).isoformat(),
        "before": (day - timedelta(days=1)).isoformat()}))
    card = actions(store, turn["id"])[0]
    store.save_topic(saved.model_copy(update={"name": "编辑后的主题"}), revision)
    with pytest.raises(ValueError, match="已变化"):
        confirm_action(store, key, card["id"])
    assert store.one("SELECT id FROM runs") is None
    turn = respond(store, key, "再补录这两天的论文", action({
        "action": "backfill", "after": (day - timedelta(days=3)).isoformat(),
        "before": (day - timedelta(days=1)).isoformat()}))
    accepted = confirm_action(store, key, actions(store, turn["id"])[0]["id"])
    assert store.one("SELECT id FROM runs")["id"] == accepted["run_id"]


def test_create_and_import_selected_conferences_uses_one_confirmation(radar, monkeypatch):
    from research_radar import conversation
    from research_radar.models import Topic

    store, _ = radar
    chat = new_conversation(store)
    start, end = "2026-05-27", "2026-11-27"
    payloads = []

    def handle(request):
        body = json.loads(request.content)
        payloads.append(body)
        assert "response_format" not in body
        if len(payloads) == 1:
            return complete(calls=[call("list_topics", {"query": "NAS"}), call("search_library", {
                "query": "neural architecture search", "after": start, "before": end})])
        if len(payloads) == 2:
            return complete(calls=[call("propose_action", {
                "action": "create_topic", "draft": {"name": "神经网络架构搜索（NAS）",
                "keywords": ["neural architecture search", "differentiable architecture search"],
                "excluded": ["neural network verification"]},
                "description": "追踪 NAS 方法和评测。",
                "after": start, "before": end, "venues": ["ICML", "NeurIPS"]})])
        return complete("请确认创建和补录")

    message = "查询神经网络架构搜索（NAS）近6个月的相关研究并创建这个主题，将这个方向的顶会论文入库"
    turn = respond(store, chat, message, handle)
    assert turn["status"] == "done" and len(payloads) == 3
    proposed = actions(store, turn["id"])[0]
    assert json.loads(proposed["preview"])["venues"] == ["ICML", "NEURIPS"]
    assert store.one("SELECT id FROM runs") is None and len(store.topics()) == 1

    class ReloadedTopic(Topic):
        pass

    # Streamlit may use a reloaded model while retaining the cached store.
    monkeypatch.setattr(conversation, "Topic", ReloadedTopic)
    receipt = confirm_action(store, chat, proposed["id"])
    assert receipt == confirm_action(store, chat, proposed["id"])
    created, _ = store.topic(receipt["topic"])
    assert not created.enabled and not created.excluded
    run = store.one("SELECT * FROM runs WHERE id=:id", id=receipt["run_id"])
    assert backfill_venues(run["kind"]) == ["ICML", "NEURIPS"]

    qualifying = published(1, arxiv=False).model_copy(update={
        "publication": Publication(status="published", venue="Proceedings of the International Conference on Machine Learning")})
    workshop = published(2, arxiv=False).model_copy(update={
        "publication": Publication(status="published", venue="NeurIPS Workshop on AutoML")})
    unrelated = published(3, arxiv=False).model_copy(update={
        "publication": Publication(status="published", venue="Journal of Machine Learning Research")})
    assert worker(store, FixtureSources([preprint(4)], [qualifying, workshop, unrelated])).tick(
        schedule=False, run_id=run["id"], notify=False)
    papers = store.papers(created.id)
    assert len(papers) == 1 and papers[0]["records"][0].external_id == qualifying.external_id
    assert not store.papers(created.id, created.query_version())


def test_manual_search_reuses_receipt_and_notification_scope(radar):
    store, topic = radar
    chat = new_conversation(store, topic.id)

    def handle(request):
        body = json.loads(request.content)
        if body["messages"][-1]["role"] == "tool":
            return complete("请查看运行卡")
        return complete(calls=[call("enqueue_daily", {})])

    initial = respond(store, chat, "检索一次当前主题", handle)
    duplicate = respond(store, chat, "检索一次当前主题", handle)
    first_run = json.loads(initial["events"])[0]["result"]["data"]
    second_run = json.loads(duplicate["events"])[0]["result"]["data"]
    assert first_run["id"] == second_run["id"] and not first_run["reused"] and second_run["reused"]
    first = store.one("SELECT * FROM runs WHERE id=:id", id=first_run["id"])
    second_id = store.enqueue(topic, utc_now(), local_day=utc_now().date() - timedelta(days=2))
    second = store.one("SELECT * FROM runs WHERE id=:id", id=second_id)
    store.finish(first, "succeeded", {"recommendations": []}, "日报 A", "A")
    store.finish(second, "succeeded", {"recommendations": []}, "日报 B", "B")
    store.execute("UPDATE notifications SET status='uncertain' WHERE run_id=:id", id=second_id)
    retry_notification(store, second_id)
    sent = []
    def capture(request):
        sent.append(json.loads(request.content)["content"]["text"])
        return httpx.Response(200, json={"code": 0})
    settings = model(store).model_copy(update={"feishu_webhook": Settings(
        feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/fixture").feishu_webhook})
    with httpx.Client(transport=httpx.MockTransport(capture)) as client:
        assert send_one(store, settings, client, manual_only=True)
        assert not send_one(store, settings, client, manual_only=True)
    assert sent == ["B"]


def fixture_pdf():
    writer = PdfWriter()
    for text in ("Introduction", "Evaluation protocol: split A. AUROC: 93.5 on Dataset X."):
        page = writer.add_blank_page(180, 100)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        })})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 60 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    binary = BytesIO()
    writer.write(binary)
    return binary.getvalue()


@pytest.mark.parametrize("topic_index", [0, 1])
def test_research_resumes_supplements_and_verifies_real_pdf_pages(radar, topic_index):
    store, _ = radar
    topic = sample_topics()[topic_index]
    store.save_topic(topic)
    chat = new_conversation(store, topic.id)

    def schedule(request):
        return complete("专题已提交") if json.loads(request.content)["messages"][-1]["role"] == "tool" else complete(
            calls=[call("start_research", {"question": topic.name})])

    turn = respond(store, chat, f"做个专题研究，查{topic.name}公开全文", schedule)
    assert turn["status"] == "done"
    job = research_status(store, chat)[0]
    source = preprint(title=f"{topic.name} evidence")
    matching = published(title=source.title)

    class Sources(FixtureSources):
        def page(self, source_name, topic, terms, start, end, cursor, deadline, **options):
            self.calls.append((source_name, terms))
            return Page(records=[source if source_name == "arxiv" else matching], cursor=None)

    sources = Sources()
    pdf = fixture_pdf()
    packet = {}

    def investigate(request):
        if request.url.host == "arxiv.org":
            return httpx.Response(200, content=pdf)
        body = json.loads(request.content)
        latest = json.loads(store.one("SELECT checkpoint FROM research_jobs WHERE id=:id", id=job["id"])["checkpoint"])
        if "tools" not in body and not latest.get("candidates"):
            return complete(json.dumps({"terms": ["evaluation protocol"]}))
        if "tools" in body:
            if len(latest["searches"]) == 1:
                paper = next(iter(latest["candidates"]))
                return complete(calls=[call("read_open_pdf", {"paper": paper, "query": "evaluation AUROC"}),
                                       call("search_scholarly", {"terms": ["Dataset X"], "reason": "补查评测设置"})])
            return complete("完成")
        evidence = next(item for item in latest["evidence"].values() if item["page"] == 2)
        packet.update(evidence)
        return complete(json.dumps({"claims": [
            {"text": "可核对的分割和指标", "evidence_ids": [evidence["id"]]},
            {"text": "伪造的结论", "evidence_ids": ["unknown"]},
        ], "comparisons": [{"paper": evidence["paper"], "value": "93.5", "dataset": "Dataset X",
            "split": "A", "metric": "AUROC", "protocol": "Evaluation protocol", "evidence_ids": [evidence["id"]]}]}))

    for _ in range(8):
        with httpx.Client(transport=httpx.MockTransport(investigate)) as client:
            tick_research(store, model(store), client, sources)
        job = research_status(store, chat)[0]
        if job["status"] not in {"queued", "running"}:
            break
    assert job["status"] == "partial" and job["markdown"]
    assert len(sources.calls) == 4 and len(json.loads(job["checkpoint"])["candidates"]) == 1
    assert "PDF 第 2 页" in job["markdown"] and "#page=2" in job["markdown"]
    assert "93.5" in job["markdown"] and "伪造的结论" not in job["markdown"]
    assert packet["sha256"] and not store.papers(topic.id)


def test_streamlit_conversation_and_restored_history(radar, monkeypatch):
    from streamlit.testing.v1 import AppTest

    store, topic = radar
    monkeypatch.setenv("RADAR_DATA_DIR", str(store.directory))
    chat = new_conversation(store, topic.id)
    respond(store, chat, "解释点级 AUROC", lambda _request: complete("这是一个点级指标。"))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=8)
    app.query_params["page"] = "assistant"
    app.run()
    assert not app.exception and app.chat_input
    assert any("点级指标" in item.value for item in app.markdown)
    assert any(item.label == "任务时间线" for item in app.expander)
    next(button for button in app.button if button.label == "新对话").click().run()
    assert not app.exception and len(app.selectbox[0].options) == 2
    memory_page = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=8)
    memory_page.query_params["page"] = "topics"
    memory_page.query_params["edit"] = topic.id
    memory_page.run()
    assert any(item.label == "项目记忆" for item in memory_page.expander)
    next(item for item in memory_page.text_area if item.label == "常用数据集（每行一项）").set_value("Dataset X\nDataset Y")
    next(button for button in memory_page.button if button.label == "保存项目记忆").click().run()
    assert not memory_page.exception and project_memory(store, topic.id)[1].datasets == ["Dataset X", "Dataset Y"]


def test_research_deadline_and_cancel_keep_private_jobs_separate(radar):
    store, topic = radar
    chat = new_conversation(store, topic.id)

    def schedule(request):
        if json.loads(request.content)["messages"][-1]["role"] == "tool":
            return complete("已提交")
        return complete(calls=[call("start_research", {"question": "industrial inspection"})])

    respond(store, chat, "做个专题研究，查一下公开论文", schedule)
    waiting = research_status(store, chat)[0]
    queue_adjustment(store, chat, waiting["id"], ResearchAdjustment(main_only=True))
    assert cancel_job(store, chat, waiting["id"])["status"] == "cancelled"
    assert adjustments(store, waiting["id"])[0]["status"] == "cancelled"
    with pytest.raises(ValueError, match="当前会话"):
        cancel_job(store, new_conversation(store), waiting["id"])

    respond(store, chat, "做个专题研究，查另外一批公开论文", schedule)
    expired = research_status(store, chat)[0]
    store.execute("UPDATE research_jobs SET started_at=:then WHERE id=:id",
                  then=(utc_now() - timedelta(minutes=6)).isoformat(), id=expired["id"])
    with httpx.Client(transport=httpx.MockTransport(
            lambda _request: pytest.fail("expired work must not make network requests"))) as client:
        assert tick_research(store, model(store), client, FixtureSources())
    completed = research_status(store, chat, expired["id"])[0]
    assert completed["status"] == "failed" and "五分钟时限" in completed["markdown"]


def test_project_memory_across_chats_and_timeline(radar):
    store, topic = radar
    initial = ProjectMemory(venues=["icml"], excluded=["workshop"], datasets=["Dataset X"],
                            research_protocol="只比较使用同一数据划分的结果")
    assert save_project_memory(store, topic.id, 0, initial) == 1
    assert save_project_memory(store, topic.id, 1, initial) == 1
    first, second = (new_conversation(store, topic.id) for _ in range(2))
    context = []

    def report_memory(request):
        messages = json.loads(request.content)["messages"]
        request_context = json.loads(messages[-1]["content"])["app_context"]
        context.append(request_context["project_memory"])
        return complete("已读取项目记忆。")

    original = respond(store, first, "这个项目用什么数据集？", report_memory)
    assert context[-1]["revision"] == 1 and context[-1]["venues"] == ["ICML"]
    assert save_project_memory(store, topic.id, 1, initial.model_copy(update={"datasets": ["Dataset Y"]})) == 2
    with pytest.raises(ValueError, match="项目记忆已更新"):
        save_project_memory(store, topic.id, 1, initial)
    latest = respond(store, second, "当前项目记忆是什么？", report_memory)
    assert project_memory(store, topic.id)[0] == 2
    assert context[-1]["revision"] == 2 and context[-1]["datasets"] == ["Dataset Y"]
    for turn, revision in ((original, 1), (latest, 2)):
        entries = timeline(store, "chat", turn["id"])
        assert json.loads(next(event["detail"] for event in entries if event["stage"] == "project_memory"))["revision"] == revision
        assert any(event["stage"] == "model" and event["elapsed_ms"] is not None for event in entries)
        assert entries[-1]["status"] == "done"


def test_expired_chat_turn_unlocks_conversation(radar):
    store, topic = radar
    settings = model(store)
    chat = new_conversation(store, topic.id)
    turn = queue_turn(store, settings, chat, "第一条消息")
    store.execute("UPDATE conversation_turns SET status='running',created_at=:then WHERE id=:id",
                  id=turn, then=(utc_now() - timedelta(minutes=10)).isoformat())
    assert expire_turn(store, settings, turn)
    assert not expire_turn(store, settings, turn)
    assert queue_turn(store, settings, chat, "下一条消息") != turn
    assert timeline(store, "chat", turn)[-1]["status"] == "error"


def test_research_steering_persists_and_filters_evidence(radar):
    store, topic = radar
    chat = new_conversation(store, topic.id)
    save_project_memory(store, topic.id, 0, ProjectMemory(datasets=["Dataset X"]))
    preferred = preprint(7, title="Main track dataset measurement")
    workshop = preprint(8, title="Workshop dataset measurement")

    def schedule(request):
        if json.loads(request.content)["messages"][-1]["role"] == "tool":
            return complete("已提交")
        return complete(calls=[call("start_research", {"question": "Compare evaluation protocols"})])

    respond(store, chat, "做个专题研究，比对评测协议", schedule)
    job = research_status(store, chat)[0]["id"]
    class ResearchSources(FixtureSources):
        def page(self, source, topic, terms, start, end, cursor, deadline, **_options):
            return super().page(source, topic, terms, start, end, cursor, deadline)

    sources = ResearchSources(arxiv=[preferred, workshop], openalex=[
        published(7, title=preferred.title).model_copy(update={"publication": Publication(status="published", venue="ICML 2026")}),
        published(8, title=workshop.title).model_copy(update={"publication": Publication(status="published", venue="ICML Workshop")}),
    ])
    observed = []

    def state():
        return json.loads(research_status(store, chat, job)[0]["checkpoint"])

    def backend(request):
        if request.url.host == "arxiv.org":
            observed.append(request.url.path)
            return httpx.Response(200, content=fixture_pdf())
        messages = json.loads(request.content)
        if "tools" in messages:
            scope = json.loads(messages["messages"][1]["content"])
            assert scope["scope"] == {"main_only": True, "venues": ["ICML"]}
            assert scope["focus"] == "Dataset X 的测试划分"
            assert [item["title"] for item in scope["candidates"]] == [preferred.title]
            return complete("已找到证据")
        if not state()["candidates"]:
            return complete('{"terms":["evaluation protocols"]}')
        evidence = next(item for item in state()["evidence"].values() if item["page"] == 2)
        return complete(json.dumps({"claims": [{"text": "Evaluation protocol: split A.", "evidence_ids": [evidence["id"]]}]}))

    def tick():
        with httpx.Client(transport=httpx.MockTransport(backend)) as client:
            assert tick_research(store, model(store), client, sources)

    tick()
    tick()
    assert research_status(store, chat, job)[0]["stage"] == "investigate"

    def steer(request):
        if json.loads(request.content)["messages"][-1]["role"] == "tool":
            return complete("已提交调整")
        return complete(calls=[call("steer_research", {"adjustment": {"venues": ["ICML"], "main_only": True,
                               "focus": "Dataset X 的测试划分", "paper": preferred.arxiv_id}})])

    denied = respond(store, chat, "优先查评测设置", steer)
    assert json.loads(denied["events"])[0]["result"]["status"] == "error"
    updated = respond(store, chat, f"只看 ICML 主会，优先查 Dataset X 的测试划分，并读 {preferred.arxiv_id}", steer)
    assert json.loads(updated["events"])[0]["result"]["status"] == "ok"
    assert adjustments(store, job)[0]["status"] == "pending"
    save_project_memory(store, topic.id, 1, ProjectMemory(datasets=["Dataset Y"]))
    for _ in range(5):
        tick()
        completed = research_status(store, chat, job)[0]
        if completed["status"] not in {"running", "queued"}:
            break
    assert completed["markdown"] and "PDF 第 2 页" in completed["markdown"]
    assert "项目记忆 · 第 1 版" in completed["markdown"] and workshop.title not in completed["markdown"]
    assert observed and all(preferred.arxiv_id in path for path in observed)
    assert adjustments(store, job)[0]["status"] == "applied"
    assert [event["status"] for event in timeline(store, "research", job) if event["stage"] == "adjustment"] == ["queued", "applied"]
    assert len(research_status(store, chat)) == 1
