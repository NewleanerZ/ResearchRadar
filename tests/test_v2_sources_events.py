from datetime import date, timedelta
import json
import time

import httpx

from research_radar.delivery import send_event_alerts
from research_radar.events import detect_daily_events, list_events, queue_event_notifications, record_event
from research_radar.models import Settings, Topic, utc_now
from research_radar.sources import Sources
from research_radar.v2_sources import CVF_BASE, SEMANTIC_SCHOLAR, extract_code_and_hf_refs, parse_cvf_listing, parse_semantic_scholar
from test_radar import DAY, FixtureSources, NOW, preprint, radar, run_daily, worker


def test_semantic_scholar_and_cvf_parsers_and_paging():
    s2_item = {
        "paperId": "abc123def",
        "title": "Point Cloud Anomaly Detection via Geometric Priors",
        "abstract": "Code at https://github.com/example/pc-ad and dataset https://huggingface.co/datasets/ex/mvtec3d.",
        "venue": "CVPR",
        "publicationTypes": ["Conference"],
        "publicationDate": "2026-05-18",
        "externalIds": {"DOI": "10.1109/CVPR.2026.0001", "ArXiv": "2605.00123"},
        "openAccessPdf": {"url": "https://openaccess.thecvf.com/content/CVPR2026/papers/test.pdf"},
        "authors": [{"name": "Ada Lee"}],
    }
    rec = parse_semantic_scholar(s2_item)
    assert rec is not None and rec.source == "semantic_scholar" and rec.doi == "10.1109/cvpr.2026.0001"
    assert rec.arxiv_id == "2605.00123" and rec.publication.status == "published"

    refs = extract_code_and_hf_refs(s2_item["abstract"])
    assert refs["github"] == ["example/pc-ad"]
    assert refs["huggingface_datasets"] == ["ex/mvtec3d"]

    cvf_html = """
    <dt class="ptitle"><br><a href="/content/CVPR2026/html/Lee_Point_CVPR_2026_paper.html">Point Cloud Anomaly Detection</a></dt>
    <dd><a href="#">Ada Lee</a>, <a href="#">Lin Chen</a></dd>
    <dd>[<a href="/content/CVPR2026/papers/Lee_Point_CVPR_2026_paper.pdf">pdf</a>]</dd>
    """
    cvf_records = parse_cvf_listing(cvf_html, "CVPR", 2026)
    assert len(cvf_records) == 1
    assert cvf_records[0].open_pdf_urls == ["https://openaccess.thecvf.com/content/CVPR2026/papers/Lee_Point_CVPR_2026_paper.pdf"]

    def respond(request):
        url = str(request.url)
        if url.startswith(SEMANTIC_SCHOLAR):
            return httpx.Response(200, json={"data": [s2_item], "next": None})
        if url.startswith(CVF_BASE):
            return httpx.Response(200, text=cvf_html)
        return httpx.Response(404)

    topic = Topic(id="cv_test", name="Vision", keywords=["point cloud anomaly detection"], categories=["cs.CV"])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        srcs = Sources(client, "", extra=("semantic_scholar", "cvf"), s2_api_key="test-s2")
        s2_page = srcs.page("semantic_scholar", topic, topic.keywords, date(2026, 5, 1), date(2026, 6, 1), None, time.monotonic() + 5)
        cvf_page = srcs.page("cvf", topic, topic.keywords, date(2026, 1, 1), date(2027, 1, 1), None, time.monotonic() + 5)
    assert len(s2_page.records) == 1 and s2_page.cursor is None
    assert len(cvf_page.records) == 1 and cvf_page.records[0].source == "cvf"
    assert cvf_page.records[0].published_month == 6

    # Partial current-year range (e.g. Jan 1 through Nov 27) still matches CVPR 2026 (June) and skips 404 ICCV/WACV
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        srcs2 = Sources(client, "", extra=("cvf",))
        partial_year_page = srcs2.page("cvf", topic, topic.keywords, date(2026, 1, 1), date(2026, 11, 28), None, time.monotonic() + 5)
    assert len(partial_year_page.records) == 1 and partial_year_page.records[0].title == "Point Cloud Anomaly Detection"


def test_event_detection_cooldown_daily_cap_and_delivery(radar):
    store, topic = radar
    abstract = "We release code at https://github.com/lab/ad3d and dataset https://huggingface.co/datasets/lab/ad3d-bench."
    job = worker(store, FixtureSources([preprint(1, abstract=abstract)]),
                 feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/events")
    run_daily(store, topic, job)
    events = list_events(store, topic.id)
    types = {e["event_type"] for e in events}
    assert {"code_released", "dataset_released"}.issubset(types)

    # run_daily already queued event alert automatically when feishu_webhook is configured
    assert store.one("SELECT COUNT(*) AS n FROM event_notifications WHERE status='pending'")["n"] == 1
    # Daily cap blocks additional event alert today
    record_event(store, topic.id, events[0]["paper_id"], events[0]["event_type"], "dup-key-2", {"title": "Dup"})
    assert queue_event_notifications(store, job.settings, topic, daily_cap=1, cooldown_days=7) is None

    sent = []
    with httpx.Client(transport=httpx.MockTransport(
            lambda req: (sent.append(json.loads(req.content)["content"]["text"]), httpx.Response(200, json={"code": 0}))[1]
    )) as client:
        assert send_event_alerts(store, job.settings, client) == 1
        assert send_event_alerts(store, job.settings, client) == 0
    assert len(sent) == 1 and "科研事件提醒" in sent[0]
