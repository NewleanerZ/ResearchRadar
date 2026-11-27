"""Catalog adapters for proceedings, journals and public reviews."""

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from html import unescape
import json
import re
import time
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring
import yaml

from .models import Page, Publication, Record, dump


CROSSREF = "https://api.crossref.org/works"
OPENREVIEW = "https://api2.openreview.net/notes/search"
DBLP = "https://dblp.org/search/publ/api"
ACL_DATA = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml"
PMLR = "https://proceedings.mlr.press"
ACL_VENUES = ("acl", "emnlp", "naacl", "eacl", "findings", "coling")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december")
CROSSREF_TYPES = {"journal-article", "proceedings-article"}


def normalized(value):
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


def matches(record, terms):
    body = f" {normalized(record.title + ' ' + (record.abstract or ''))} "
    return any(name and f" {name} " in body for name in map(normalized, terms))


def plain(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def first(values):
    return values[0] if values else ""


def doi_value(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I).lower()
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s]+", value) else None


def exact_date(parts):
    return date(*parts) if len(parts) == 3 else None


def within(record, start, end, zone):
    when = record.preprint_at.astimezone(zone).date() if record.preprint_at else record.published_on
    if when:
        return start <= when < end
    if not record.published_year:
        return False
    year, month = record.published_year, record.published_month
    low = date(year, month or 1, 1)
    high = date(year, month + 1, 1) if month and month < 12 else date(year + 1, 1, 1)
    return start <= low and high <= end


def serial_page(cursor, size, request, limit, *, initial=None):
    # Rotate search phrases so a busy term cannot take every page in a bounded scan.
    pending = json.loads(cursor) if cursor else [[index, initial] for index in range(size)]
    index, current = pending.pop(0)
    records, more = request(index, current, limit)
    if more is not None:
        pending.append([index, more])
    return Page(records=records, cursor=dump(pending) if pending else None)


def parse_crossref(work):
    doi = doi_value(work.get("DOI"))
    title = plain(first(work.get("title")))
    if not doi or not title or work.get("type") not in CROSSREF_TYPES:
        return None
    candidates = [first((work.get(field) or {}).get("date-parts"))
                  for field in ("published", "published-online", "published-print", "issued")]
    dates = [parts for parts in candidates if parts and 1900 <= parts[0] <= 2200]
    exact = min((exact_date(parts) for parts in dates if len(parts) == 3), default=None)
    precision = max(dates, key=len, default=[])
    venue = plain((work.get("event") or {}).get("name") or first(work.get("container-title"))) or None
    authors = [" ".join(filter(None, (name.get("given"), name.get("family")))) or name.get("name", "")
               for name in work.get("author", [])]
    pdfs = []
    # ACL DOI names identify the official Anthology landing page and open PDF.
    if doi.startswith("10.18653/v1/") and re.fullmatch(r"[a-z0-9.-]+", doi.split("/")[-1]):
        paper = doi.split("/")[-1]
        url = f"https://aclanthology.org/{paper}/"
        pdfs = [f"https://aclanthology.org/{paper}.pdf"]
    else:
        url = "https://doi.org/" + quote(doi, safe="/")
    cited = work.get("is-referenced-by-count")
    return Record(source="crossref", external_id=doi, doi=doi, title=title, url=url,
                  abstract=plain(work.get("abstract")) or None, authors=[author for author in authors if author],
                  published_on=exact, published_year=precision[0] if not exact and precision else None,
                  published_month=precision[1] if not exact and len(precision) > 1 else None,
                  open_pdf_urls=pdfs, status="published", publication=Publication(status="published", venue=venue),
                  citation_count=cited if isinstance(cited, int) and cited >= 0 else None)


def parse_openreview(note):
    fields = note.get("content") or {}

    def value(key, default=None):
        entry = fields.get(key, {})
        return entry.get("value", default) if isinstance(entry, dict) else default

    key = note.get("id")
    title = plain(value("title"))
    if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key)
            or note.get("forum") != key or "everyone" not in note.get("readers", [])
            or not title):
        return None
    venue_id = value("venueid") or ""
    venue = plain(value("venue")) or None
    # OpenReview's pdate records acceptance by the venue, not publisher publication.
    accepted = bool(note.get("pdate") and venue and re.search(
        r"/\d{4}/(?:Conference|Workshop|Journal)(?:$|/)", venue_id, re.I)
        and not re.search(r"submission|withdraw|reject|desk", f"{venue} {venue_id}", re.I))
    timestamp = note.get("odate") or (note.get("pdate") if accepted else None)
    when = datetime.fromtimestamp(timestamp / 1000, timezone.utc) if timestamp else None
    changed = note.get("tmdate")
    updated = datetime.fromtimestamp(changed / 1000, timezone.utc) if changed else None
    pdf = value("pdf", "")
    files = ["https://openreview.net" + pdf] if re.fullmatch(r"/pdf/[A-Fa-f\d]+\.pdf", pdf or "") else []
    state = "accepted" if accepted else "preprint"
    source_authors = value("authors", []) or []
    names = [author.get("fullname") if isinstance(author, dict) else author for author in source_authors]
    return Record(source="openreview", external_id=key, title=title, url=f"https://openreview.net/forum?id={key}",
                  authors=[author for author in names if isinstance(author, str) and author],
                  abstract=plain(value("abstract")) or None,
                  doi=doi_value(value("doi")), preprint_at=when, updated_at=updated, open_pdf_urls=files,
                  status=state, publication=Publication(status=state, venue=venue if accepted else "OpenReview"))


def parse_dblp(info):
    key = info.get("key", "")
    title = plain(info.get("title"))
    year = info.get("year", "")
    venue = plain(info.get("venue"))
    if (not title or not re.fullmatch(r"(?:conf|journals)/[A-Za-z0-9_.:/-]+", key)
            or venue.casefold() in {"corr", "arxiv"} or not str(year).isdigit()
            or not 1900 <= int(year) <= 2200 or info.get("type") not in {"Conference and Workshop Papers", "Journal Articles"}):
        return None
    raw_authors = (info.get("authors") or {}).get("author", [])
    if not isinstance(raw_authors, list):
        raw_authors = [raw_authors]
    authors = [item.get("text", "") if isinstance(item, dict) else item for item in raw_authors]
    links = info.get("ee", [])
    if isinstance(links, str):
        links = [links]
    doi = doi_value(info.get("doi")) or next((value for url in links if (value := doi_value(url))), None)
    landing = next((url for url in links if isinstance(url, str) and url.startswith("https://") and
                    urlsplit(url).hostname in {"proceedings.mlr.press", "openaccess.thecvf.com", "www.usenix.org",
                                               "aclanthology.org", "dl.acm.org", "ieeexplore.ieee.org"}), None)
    pdfs = []
    if landing:
        host = urlsplit(landing).hostname
        if host == "aclanthology.org":
            pdfs.append(landing.rstrip("/") + ".pdf")
        elif host == "proceedings.mlr.press" and landing.endswith(".html"):
            stem = landing[:-5]
            pdfs.append(f"{stem}/{stem.rsplit('/', 1)[-1]}.pdf")
    return Record(source="dblp", external_id=key, title=title,
                  url=landing or f"https://dblp.org/rec/{quote(key, safe='/')}.html", doi=doi,
                  authors=[name for name in authors if name], published_year=int(year), open_pdf_urls=pdfs,
                  status="published", publication=Publication(status="published", venue=venue or None))


def element_text(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def parse_acl(content):
    root = fromstring(content)
    records = []
    for volume in root.findall("volume"):
        meta = volume.find("meta")
        if meta is None:
            continue
        year = int(meta.findtext("year", default="0"))
        if not 1900 <= year <= 2200:
            continue
        label = meta.findtext("month", default="").strip().casefold()
        month = MONTHS.index(label) + 1 if label in MONTHS else None
        venue = element_text(meta.find("booktitle"))
        if not volume.get("id"):
            continue
        for paper in volume.findall("paper"):
            title = element_text(paper.find("title"))
            if not title or not paper.get("id"):
                continue
            key = f"{root.get('id')}-{volume.get('id')}.{paper.get('id')}"
            if not re.fullmatch(r"[A-Za-z0-9.-]+", key):
                continue
            authors = [" ".join(filter(None, (item.findtext("first"), item.findtext("last"))))
                       for item in paper.findall("author")]
            url = f"https://aclanthology.org/{key}"
            records.append(Record(source="acl", external_id=key, title=title, url=url + "/",
                                  doi=doi_value(paper.findtext("doi")),
                                  authors=[name for name in authors if name], abstract=element_text(paper.find("abstract")) or None,
                                  published_year=year, published_month=month,
                                  open_pdf_urls=[url + ".pdf"] if paper.find("pdf") is not None else [],
                                  status="published", publication=Publication(status="published", venue=venue or None)))
    return records


def pmlr_volumes(index, first_year, last_year):
    linked = re.findall(r'<li>\s*<a href="/?(v\d+)/?">\s*<b>Volume \d+</b>\s*</a>(.*?)</li>', index, re.S | re.I)
    venues = r"\b(?:ICML|AISTATS|COLT|UAI|CoRL|MLSys|ACML|ALT|ECML|PKDD|machine learning systems)\b"
    volumes = []
    for volume, description in linked:
        title = plain(description)
        years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", title)]
        if any(first_year <= year <= last_year for year in years) and re.search(venues, title, re.I):
            volumes.append(volume)
    return list(dict.fromkeys(volumes))


def parse_pmlr(content, volume):
    export = yaml.load(content, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    records = []
    for paper in export or []:
        key = paper.get("id", "")
        title = plain(paper.get("title"))
        if not title or not re.fullmatch(r"[a-zA-Z0-9_.-]+", key) or key in {".", ".."}:
            continue
        dates = (paper.get("issued") or {}).get("date-parts", [])
        parts = first(dates) if dates and isinstance(dates[0], list) else dates
        when = exact_date(parts)
        if not when:
            continue
        pdf = paper.get("PDF") or ""
        safe_pdf = (re.fullmatch(rf"https://{re.escape('proceedings.mlr.press')}/{volume}/[a-zA-Z0-9_./-]+\.pdf", pdf)
                    or re.fullmatch(rf"https://raw\.githubusercontent\.com/mlresearch/{volume}/[a-zA-Z0-9_./-]+\.pdf", pdf))
        venue = plain(paper.get("container-title")) or None
        authors = [" ".join(filter(None, (item.get("given"), item.get("family"))))
                   for item in paper.get("author") or []]
        records.append(Record(source="pmlr", external_id=f"{volume}/{key}", title=title,
                              url=f"{PMLR}/{volume}/{key}.html", abstract=plain(paper.get("abstract")) or None,
                              authors=[name for name in authors if name], published_on=when, doi=doi_value(paper.get("DOI")),
                              open_pdf_urls=[pdf] if safe_pdf and "/../" not in pdf else [],
                              status="published", publication=Publication(status="published", venue=venue)))
    return records


class ComputerScienceCatalogs:
    def __init__(self, request, mailto):
        self.request = request
        self.mailto = {"mailto": mailto} if mailto else {}
        self.acl_cache = OrderedDict()
        self.pmlr_index = ("", 0.0)
        self.pmlr_cache = OrderedDict()

    def crossref(self, terms, start, end, cursor, deadline, limit, zone):
        def query(index, state, size):
            term = terms[index]
            token, consumed = state or ["*", 0]
            filters = ",".join(["type:journal-article", "type:proceedings-article",
                                f"from-pub-date:{start}", f"until-pub-date:{end - timedelta(days=1)}"])
            data = self.request(CROSSREF, {**self.mailto, "query.bibliographic": term, "filter": filters,
                                "sort": "score", "order": "desc", "rows": size, "cursor": token}, deadline).json()["message"]
            items = data["items"]
            count = consumed + len(items)
            more = [data["next-cursor"], count] if len(items) == size and count < data["total-results"] else None
            return [record for item in items if (record := parse_crossref(item))
                    and matches(record, terms) and within(record, start, end, zone)], more

        return serial_page(cursor, len(terms), query, limit)

    def openreview(self, terms, start, end, cursor, deadline, limit, zone):
        def query(index, offset, size):
            offset = offset or 0
            notes = self.request(OPENREVIEW, {"term": terms[index], "source": "forum", "content": "all",
                                 "sort": "tmdate:desc", "count": "true", "limit": size,
                                 "offset": offset}, deadline).json()
            raw = notes["notes"]
            records = [record for note in raw if (record := parse_openreview(note))
                       and matches(record, terms) and within(record, start, end, zone)]
            boundary = datetime.combine(start, datetime.min.time(), zone).timestamp() * 1000
            older = raw and all(note.get("tmdate") and max(note["tmdate"], note.get("odate") or 0,
                        note.get("pdate") or 0) < boundary for note in raw)
            count = offset + len(raw)
            more = count if len(raw) == size and count < notes.get("count", count + 1) and not older else None
            return records, more

        return serial_page(cursor, len(terms), query, limit)

    def dblp(self, terms, start, end, cursor, deadline, limit, zone):
        queries = [(term, year) for year in range(start.year, end.year + 1) for term in terms]

        def query(index, offset, size):
            offset = offset or 0
            term, year = queries[index]
            hits = self.request(DBLP, {"q": f"{term} year:{year}", "format": "json", "h": size,
                                "f": offset}, deadline).json()["result"]["hits"]
            raw = hits.get("hit", [])
            records = [record for item in raw if (record := parse_dblp(item["info"])) and matches(record, terms)]
            dois = {record.doi for record in records if record.doi and not within(record, start, end, zone)}
            if dois:
                dates = self.request(CROSSREF, {**self.mailto, "filter": ",".join(f"doi:{doi}" for doi in sorted(dois)),
                                     "rows": len(dois)}, deadline).json()["message"]["items"]
                exact = {value.doi: value.published_on for item in dates if (value := parse_crossref(item))
                         and value.published_on}
                records = [record.model_copy(update={"published_on": exact[record.doi],
                                                      "published_year": None}) if record.doi in exact
                           else record for record in records]
            count = offset + len(raw)
            return [record for record in records if within(record, start, end, zone)], (
                count if raw and count < int(hits["@total"]) else None)

        return serial_page(cursor, len(queries), query, min(limit, 25))

    def acl(self, terms, start, end, cursor, deadline, limit, zone):
        archives = [(year, venue) for year in range((end - timedelta(days=1)).year, start.year - 1, -1)
                    for venue in ACL_VENUES]
        index, offset = json.loads(cursor) if cursor else (0, 0)
        year, venue = archives[index]
        key = f"{year}.{venue}"
        if key in self.acl_cache:
            self.acl_cache.move_to_end(key)
        else:
            response = self.request(f"{ACL_DATA}/{key}.xml", {}, deadline, allowed_statuses=(404,))
            self.acl_cache[key] = parse_acl(response.content) if response.status_code == 200 else []
            if len(self.acl_cache) > 8:
                self.acl_cache.popitem(last=False)
        eligible = [record for record in self.acl_cache[key] if within(record, start, end, zone) and matches(record, terms)]
        result = eligible[offset:offset + limit]
        at = offset + len(result)
        next_page = (index, at) if at < len(eligible) else (index + 1, 0)
        return Page(records=result, cursor=dump(next_page) if next_page[0] < len(archives) else None)

    def pmlr(self, terms, start, end, cursor, deadline, limit, zone):
        if not cursor and (not self.pmlr_index[0] or self.pmlr_index[1] < time.monotonic()):
            response = self.request(f"{PMLR}/", {}, deadline)
            self.pmlr_index = (response.text, time.monotonic() + 3600)
        # Keep the initial volume plan in the cursor so site updates cannot shift offsets.
        volumes, index, offset = json.loads(cursor) if cursor else (
            pmlr_volumes(self.pmlr_index[0], start.year - 1, (end - timedelta(days=1)).year), 0, 0)
        if index >= len(volumes):
            return Page(records=[], cursor=None)
        volume = volumes[index]
        if volume in self.pmlr_cache:
            self.pmlr_cache.move_to_end(volume)
        else:
            response = self.request(f"{PMLR}/{volume}/assets/bib/citeproc.yaml", {}, deadline)
            self.pmlr_cache[volume] = parse_pmlr(response.content, volume)
            if len(self.pmlr_cache) > 4:
                self.pmlr_cache.popitem(last=False)
        records = [record for record in self.pmlr_cache[volume] if within(record, start, end, zone) and matches(record, terms)]
        result = records[offset:offset + limit]
        at = offset + len(result)
        next_index, next_offset = (index, at) if at < len(records) else (index + 1, 0)
        return Page(records=result, cursor=dump([volumes, next_index, next_offset]) if next_index < len(volumes) else None)
