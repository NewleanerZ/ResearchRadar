from datetime import date, datetime, timedelta, timezone
import time

import httpx

from research_radar.cs_sources import ACL_DATA, CROSSREF, DBLP, OPENREVIEW, PMLR
from research_radar.models import Settings, Topic, backfill_venues
from research_radar.service import backfill_kind, enqueue_backfill, library
from research_radar.sources import Sources
from research_radar.store import Store
from research_radar.workflow import Worker


def crossref_work(doi, title, parts):
    return {"DOI": doi, "title": [title], "type": "proceedings-article",
            "author": [{"given": "Ada", "family": "Lee"}], "published": {"date-parts": [parts]},
            "event": {"name": "IEEE/CVF CVPR"}}


def test_crossref_rotates_queries_filters_dates_and_keeps_official_links():
    calls = []
    def respond(request):
        query = request.url.params
        calls.append((query["query.bibliographic"], query["cursor"]))
        assert "type:proceedings-article" in query["filter"] and query["mailto"] == "radar@example.com"
        assert query["sort"] == "score" and query["order"] == "desc"
        if len(calls) == 1:
            data = [crossref_work("10.1109/cvpr.42", "Neural architecture search", [2026, 5, 17])]
            return httpx.Response(200, json={"message": {"items": data, "total-results": 2, "next-cursor": "more"}})
        if len(calls) == 2:
            data = [crossref_work("10.18653/v1/2026.acl-long.1", "Efficient vision model", [2026, 5])]
        else:
            data = [crossref_work("10.1109/cvpr.43", "Unrelated paper", [2026, 5, 18])]
        return httpx.Response(200, json={"message": {"items": data, "total-results": len(data)}})

    terms = ["neural architecture search", "efficient vision model"]
    topic = Topic(id="test_source", name="Vision", keywords=terms)
    start, end = date(2026, 5, 15), date(2026, 6, 1)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = Sources(client, "", extra=("crossref",), crossref_mailto="radar@example.com")
        deadline = time.monotonic() + 5
        first = source.page("crossref", topic, terms, start, end, None, deadline, limit=1)
        second = source.page("crossref", topic, terms, start, end, first.cursor, deadline, limit=1)
        third = source.page("crossref", topic, terms, start, end, second.cursor, deadline, limit=1)
    assert calls == [(terms[0], "*"), (terms[1], "*"), (terms[0], "more")]
    assert first.records[0].doi == "10.1109/cvpr.42"
    assert first.records[0].publication.venue == "IEEE/CVF CVPR"
    assert second.records == third.records == [] and third.cursor is None


def note(key, venue, venue_id, *, public=True, accepted=True):
    when = int(datetime(2026, 5, 22, tzinfo=timezone.utc).timestamp() * 1000)
    return {"id": key, "forum": key, "readers": ["everyone"] if public else ["example"],
            "odate": when, "tmdate": when, "pdate": when if accepted else None,
            "content": {"title": {"value": "Neural architecture search"}, "venue": {"value": venue},
                        "venueid": {"value": venue_id}, "pdf": {"value": "/pdf/123abc.pdf"}}}


def test_openreview_distinguishes_public_and_accepted():
    taken = note("accepted", "ICLR 2026 Poster", "ICLR.cc/2026/Conference")
    submitted = note("submitted", "ICLR 2026 Submission", "ICLR.cc/2026/Conference/Submission")
    private = note("private", "ICLR 2026 Poster", "ICLR.cc/2026/Conference", public=False)
    taken["content"]["authors"] = {"value": [{"fullname": "Ada Lee", "username": "~Ada_Lee1"}, "Jo Park"]}
    def respond(request):
        assert str(request.url).startswith(OPENREVIEW)
        assert request.url.params["sort"] == "tmdate:desc"
        return httpx.Response(200, json={"notes": [taken, submitted, private], "count": 3})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        sources = Sources(client, "", extra=("openreview",))
        topic = Topic(id="nas_example", name="NAS", keywords=["neural architecture search"])
        page = sources.page("openreview", topic, topic.keywords, date(2026, 5, 21), date(2026, 5, 23), None,
                            time.monotonic() + 5)
    assert [(item.external_id, item.publication.status) for item in page.records] == [
        ("accepted", "accepted"), ("submitted", "preprint")]
    assert page.records[0].open_pdf_urls == ["https://openreview.net/pdf/123abc.pdf"]
    assert page.records[0].authors == ["Ada Lee", "Jo Park"]


def test_dblp_enriches_partial_year_only_with_matching_official_doi():
    def respond(request):
        if str(request.url).startswith(DBLP):
            info = {"key": "conf/icml/Lee26", "title": "Neural architecture search", "venue": "ICML",
                    "year": "2026", "type": "Conference and Workshop Papers",
                    "ee": "https://doi.org/10.1234/nas", "authors": {"author": {"text": "Ada Lee"}}}
            return httpx.Response(200, json={"result": {"hits": {"@total": "1", "hit": [{"info": info}]}}})
        assert str(request.url).startswith(CROSSREF)
        return httpx.Response(200, json={"message": {"items": [crossref_work("10.1234/nas", "Neural architecture search", [2026, 5, 17])]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        sources = Sources(client, "", extra=("dblp",))
        topic = Topic(id="nas_dblp", name="NAS", keywords=["neural architecture search"])
        page = sources.page("dblp", topic, topic.keywords, date(2026, 5, 1), date(2026, 5, 31), None,
                            time.monotonic() + 5)
    assert [(item.source, item.published_on) for item in page.records] == [("dblp", date(2026, 5, 17))]


def test_acl_pmlr_and_crossref_enter_library_without_duplicating_doi(tmp_path):
    acl = b"""<collection id="2026.acl"><volume id="long"><meta><year>2026</year><month>May</month>
    <booktitle>Proceedings of ACL</booktitle></meta><paper id="1"><title>Large language model evaluation</title>
    <author><first>Ada</first><last>Lee</last></author><doi>10.18653/v1/2026.acl-long.1</doi><pdf/>
    </paper></volume></collection>"""
    pmlr = b"""- id: lee26
  title: Large language model robustness
  container-title: Proceedings of ICML
  author: [{given: Ada, family: Lee}]
  issued: {date-parts: [2026, 5, 22]}
  PDF: https://raw.githubusercontent.com/mlresearch/v306/main/assets/lee26/lee26.pdf
"""

    def respond(request):
        address = str(request.url)
        if address.startswith(CROSSREF):
            item = crossref_work("10.18653/v1/2026.acl-long.1", "Large language model evaluation", [2026, 5, 20])
            return httpx.Response(200, json={"message": {"items": [item], "total-results": 1}})
        if address.startswith(ACL_DATA):
            return httpx.Response(200, content=acl) if "2026.acl.xml" in address else httpx.Response(404)
        if address == PMLR + "/":
            return httpx.Response(200, text='<li><a href="v306"><b>Volume 306</b></a> Proceedings of ICML 2026</li>')
        if address.startswith(PMLR):
            return httpx.Response(200, content=pmlr)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

    settings = Settings(data_dir=tmp_path, openalex_key="fixture", keyword_only=True, extra_sources="crossref,acl,pmlr")
    store = Store(tmp_path)
    store.initialize()
    topic = Topic(id="language_eval", name="LLM Evaluation", keywords=["large language model"])
    store.save_topic(topic)
    start, end = date(2026, 5, 1), date(2026, 5, 31)
    old = enqueue_backfill(store, topic.id, start, end)
    job = enqueue_backfill(store, topic.id, start, end, settings=settings)
    assert job != old and enqueue_backfill(store, topic.id, start, end, settings=settings) == job
    key = backfill_kind(topic, start, end, datetime(2026, 6, 1, tzinfo=timezone.utc),
                        venues=["ICML"], catalogs=["pmlr"])
    assert backfill_venues(key) == ["ICML"]
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        worker = Worker(store, settings, client, Sources(client, "fixture", extra=settings.catalog_sources()))
        assert worker.tick(schedule=False, run_id=job, notify=False)
    items = library(store, topic, after=start, before=end)
    kinds = {tuple(sorted(record.source for record in row["records"])) for row in items}
    assert kinds == {("acl", "crossref"), ("pmlr",)}
    assert not store.papers(topic.id, topic.query_version())
    assert store.one("SELECT status FROM runs WHERE id=:id", id=job)["status"] == "succeeded"
