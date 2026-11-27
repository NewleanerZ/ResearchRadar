from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
import time
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring
import httpx

from .cs_sources import DBLP, ComputerScienceCatalogs
from .models import CORE_SOURCES, Page, Publication, Record, Settings, Topic, arxiv_identifier, utc_now
from .v2_sources import V2Catalogs

ARXIV = "https://export.arxiv.org/api/query"
OPENALEX = "https://api.openalex.org/works"
OPENALEX_PUBLICATION_FIELDS = "id,primary_location,locations,type"
ATOM = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom",
        "os": "http://a9.com/-/spec/opensearch/1.1/"}
DOI = re.compile(r"10\.\d{4,9}/\S+", re.I)


def normalize_doi(value: str | None) -> str | None:
    clean = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (value or "").strip(), flags=re.I)
    clean = unquote(clean).strip().lower()
    return clean if DOI.fullmatch(clean) else None


def tidy(value: str | None) -> str:
    return " ".join((value or "").split())


def phrase(value: str) -> str:
    return '"' + re.sub(r'["\\\x00-\x1f]', " ", value) + '"'


class SourceError(Exception):
    """A sanitized failure message safe to persist."""


def retry_delay(response: httpx.Response, fallback: float) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - utc_now()).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return fallback


class Sources:
    def __init__(self, client: httpx.Client, openalex_key: str, arxiv_interval: float = 3.0, *,
                 extra=(), crossref_mailto="", s2_api_key=""):
        self.client = client
        self.openalex_key = openalex_key
        self.arxiv_interval = arxiv_interval
        self.arxiv_next = 0.0
        self.catalogs = ComputerScienceCatalogs(self.request, crossref_mailto)
        self.v2_catalogs = V2Catalogs(self.request, s2_api_key)
        self.daily_sources = (*CORE_SOURCES, *(key for key in extra if key in {"crossref", "openreview", "semantic_scholar"}))
        self.historical_sources = (*CORE_SOURCES, *extra)

    @classmethod
    def configured(cls, client: httpx.Client, settings: Settings):
        return cls(client, settings.openalex_key.get_secret_value(), extra=settings.catalog_sources(),
                   crossref_mailto=settings.crossref_mailto, s2_api_key=settings.s2_api_key.get_secret_value())

    def request(self, url, params, deadline, *, allowed_statuses=(), headers=None):
        delay = 0.0
        for attempt in range(3):
            if url == ARXIV:
                delay = max(delay, self.arxiv_next - time.monotonic())
            if time.monotonic() + delay >= deadline:
                raise SourceError("time_budget")
            if delay > 0:
                time.sleep(delay)
            try:
                response = self.client.get(url, params=params, headers=headers or {},
                                           timeout=min(25, max(.1, deadline - time.monotonic())))
            except httpx.RequestError:
                if attempt == 2:
                    raise SourceError("network_error") from None
                delay = 2 ** attempt
            else:
                if response.status_code == 200 or response.status_code in allowed_statuses:
                    return response
                if url == DBLP and response.status_code == 202:
                    raise SourceError("dblp_access_challenge")
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt < 2:
                        delay = retry_delay(response, 2 ** attempt)
                        continue
                raise SourceError(f"http_{response.status_code}")
            finally:
                if url == ARXIV:
                    self.arxiv_next = time.monotonic() + self.arxiv_interval

    def page(self, source: str, topic: Topic, terms: list[str], start: date, end: date,
             cursor: str | None, deadline: float, *, limit=100, newest=False, sort: str | None = None) -> Page:
        try:
            if source not in CORE_SOURCES:
                if source not in self.historical_sources or not terms or start >= end:
                    raise SourceError("unsupported_source_or_query")
                if not catalog_applies(source, topic, terms):
                    return Page(records=[], cursor=None)
                if source in {"semantic_scholar", "cvf"}:
                    adapter = getattr(self.v2_catalogs, source)
                else:
                    adapter = getattr(self.catalogs, source)
                return adapter(terms, start, end, cursor, deadline, limit, ZoneInfo(topic.timezone))
            if source == "arxiv":
                zone = ZoneInfo(topic.timezone)
                start_at = datetime.combine(start, datetime.min.time(), zone).astimezone(timezone.utc)
                end_at = datetime.combine(end, datetime.min.time(), zone).astimezone(timezone.utc) - timedelta(minutes=1)
                query = "(" + " OR ".join(f"ti:{phrase(term)} OR abs:{phrase(term)}" for term in terms) + ")"
                if topic.categories:
                    categories = [value for value in topic.categories if re.fullmatch(r"[\w.-]+", value)]
                    if len(categories) != len(topic.categories):
                        raise SourceError("invalid_arxiv_category")
                    query += " AND (" + " OR ".join(f"cat:{cat}" for cat in categories) + ")"
                query += f" AND submittedDate:[{start_at:%Y%m%d%H%M} TO {end_at:%Y%m%d%H%M}]"
                offset = int(cursor or 0)
                reply = self.request(ARXIV, {"search_query": query, "start": offset, "max_results": limit,
                                             "sortBy": "submittedDate",
                                             "sortOrder": "descending" if newest else "ascending"}, deadline)
                items, count = parse_arxiv(reply.content)
                if not items and offset < count:
                    raise SourceError("arxiv_pagination_gap")
                next_cursor = str(offset + len(items)) if offset + len(items) < count else None
                return Page(records=items, cursor=next_cursor)
            if not self.openalex_key:
                raise SourceError("openalex_key_missing")
            params = {
                "api_key": self.openalex_key, "cursor": cursor or "*", "per_page": limit,
                "search": " OR ".join(phrase(term) for term in terms),
                "filter": f"from_publication_date:{start},to_publication_date:{end - timedelta(days=1)}",
                "select": OPENALEX_PUBLICATION_FIELDS + ",doi,title,publication_date,authorships,abstract_inverted_index,updated_date,cited_by_count",
            }
            if sort == "citations":
                params["sort"] = "cited_by_count:desc"
            elif newest:
                params["sort"] = "publication_date:desc"
            reply = self.request(OPENALEX, params, deadline).json()
            items = reply["results"]
            next_cursor = reply["meta"].get("next_cursor") if items else None
            if next_cursor == cursor and cursor is not None:
                raise SourceError("openalex_cursor_repeated")
            # OpenAlex can return a work with no title. Keep the source cursor for the raw page.
            records = [parse_openalex(item) for item in items if tidy(item.get("title"))]
            return Page(records=records, cursor=next_cursor)
        except SourceError:
            raise
        except Exception as error:
            raise SourceError(f"invalid_{source}_response_{type(error).__name__}") from None

    def arxiv_versions(self, ids: list[str], deadline: float) -> list[Record]:
        if not ids:
            return []
        reply = self.request(ARXIV, {"id_list": ",".join(ids), "max_results": len(ids)}, deadline)
        try:
            return parse_arxiv(reply.content)[0]
        except Exception:
            raise SourceError("invalid_arxiv_versions") from None

    def openalex_publications(self, ids: list[str], deadline: float) -> dict[str, Publication]:
        if not ids:
            return {}
        if not self.openalex_key:
            raise SourceError("openalex_key_missing")
        reply = self.request(OPENALEX, {
            "api_key": self.openalex_key, "per_page": len(ids),
            "filter": "openalex:" + "|".join(ids), "select": OPENALEX_PUBLICATION_FIELDS,
        }, deadline)
        try:
            return {item["id"].rsplit("/", 1)[-1]: openalex_publication(item) for item in reply.json()["results"]}
        except (ValueError, TypeError, KeyError):
            raise SourceError("invalid_openalex_publications") from None


def arxiv_publication(journal_ref: str, comment: str) -> Publication:
    markers = (
        ("published", r"published\s+(?:in|at|by)\s+(.+)"),
        ("accepted", r"(?:accepted(?:\s+for publication)?|(?:to|will) appear)(?:\s+(?:in|at|to|by)\s+(.+))?"),
        ("submitted", r"(?:submitted|under review)(?:\s+(?:to|at|by)\s+(.+))?"),
    )
    parts = re.split(r"[;\n]|\.\s+(?=(?:published|accepted|submitted|under review|to appear|will appear)\b)",
                     comment, flags=re.I)
    reference = tidy(journal_ref)
    candidate = None
    for part in [reference, *parts]:
        for status, pattern in markers:
            match = re.fullmatch(pattern, tidy(part).rstrip("."), re.I)
            if match:
                if part == reference:
                    reference = ""
                venue = match[1] or None
                if venue and venue.lower().startswith("arxiv"):
                    break
                flagged = Publication(status=status, venue=venue)
                if status == "published":
                    return flagged
                if candidate is None or candidate.status == "submitted":
                    candidate = flagged
                break
    if reference:
        return Publication(status="published", venue=reference)
    return candidate or Publication(status="preprint", venue="arXiv")


def openalex_publication(item: dict) -> Publication:
    accepted = None
    preprint = item.get("type") == "preprint"
    arxiv = False
    published_without_venue = False
    for location in [item.get("primary_location"), *(item.get("locations") or [])]:
        if not location:
            continue
        source = location.get("source") or {}
        source_type = source.get("type")
        is_arxiv = arxiv_identifier(location.get("landing_page_url") or "")[0] is not None
        arxiv = arxiv or is_arxiv
        venue = (tidy(source.get("display_name") or location.get("raw_source_name")) or None
                 if source_type in {"journal", "conference", "book series", "ebook platform"} else None)
        version = location.get("version")
        published = version == "publishedVersion" or location.get("is_published") is True
        if published and not is_arxiv and source_type != "repository" and (venue or not preprint):
            if venue:
                return Publication(status="published", venue=venue)
            published_without_venue = True
        elif not is_arxiv and (version == "acceptedVersion" or (not published and location.get("is_accepted") is True)):
            if accepted is None or (accepted.venue is None and venue):
                accepted = Publication(status="accepted", venue=venue)
    if published_without_venue:
        return Publication(status="published")
    if accepted:
        return accepted
    if arxiv or preprint:
        return Publication(status="preprint", venue="arXiv" if arxiv else None)
    return Publication(status="unknown")


def parse_arxiv(content: bytes) -> tuple[list[Record], int]:
    root = fromstring(content)
    entries = root.findall("a:entry", ATOM)
    records = []
    for entry in entries:
        arxiv_id, version = arxiv_identifier(entry.findtext("a:id", "", ATOM))
        if not arxiv_id:
            raise SourceError("arxiv_invalid_entry")
        identifier = arxiv_id + (version or "")
        publication = arxiv_publication(entry.findtext("ar:journal_ref", "", ATOM),
                                        entry.findtext("ar:comment", "", ATOM))
        records.append(Record(
            source="arxiv", external_id=arxiv_id, title=tidy(entry.findtext("a:title", "", ATOM)),
            url=f"https://arxiv.org/abs/{identifier}", arxiv_id=arxiv_id, version=version,
            abstract=tidy(entry.findtext("a:summary", "", ATOM)) or None,
            authors=[tidy(author.text) for author in entry.findall("a:author/a:name", ATOM) if author.text],
            doi=normalize_doi(entry.findtext("ar:doi", "", ATOM)),
            preprint_at=entry.findtext("a:published", None, ATOM),
            updated_at=entry.findtext("a:updated", None, ATOM), status="preprint", publication=publication,
        ))
    return records, int(root.findtext("os:totalResults", str(len(records)), ATOM))


def parse_openalex(item: dict) -> Record:
    abstract = item.get("abstract_inverted_index") or {}
    tokens = sorted((position, word) for word, positions in abstract.items() for position in positions)
    doi = normalize_doi(item.get("doi"))
    external = item["id"].rsplit("/", 1)[-1]
    if not re.fullmatch(r"W\d+", external):
        raise ValueError("Invalid OpenAlex work ID")
    arxiv_id = None
    for location in item.get("locations") or []:
        arxiv_id, _ = arxiv_identifier(location.get("landing_page_url") or "")
        if arxiv_id:
            break
    if doi and doi.startswith("10.48550/arxiv."):
        arxiv_id, _ = arxiv_identifier(doi.split("arxiv.", 1)[1])
    publication = openalex_publication(item)
    open_pdf_urls = list(dict.fromkeys(location["pdf_url"] for location in item.get("locations") or []
        if location.get("is_oa") is True and isinstance(location.get("pdf_url"), str)))[:8]
    cited = item.get("cited_by_count")
    return Record(
        source="openalex", external_id=external, url=f"https://openalex.org/{external}",
        title=tidy(item.get("title")), doi=doi, arxiv_id=arxiv_id, open_pdf_urls=open_pdf_urls,
        abstract=" ".join(word for _, word in tokens) or None,
        authors=[author["author"]["display_name"] for author in (item.get("authorships") or [])
                 if (author.get("author") or {}).get("display_name")],
        published_on=item.get("publication_date"), updated_at=item.get("updated_date"),
        status="preprint" if item.get("type") == "preprint" else publication.status,
        publication=publication,
        citation_count=cited if isinstance(cited, int) and cited >= 0 else None,
    )


def catalog_applies(source, topic, terms=None):
    text = " ".join([topic.name, *(topic.keywords if terms is None else terms)])
    if source == "acl":
        return "cs.CL" in topic.categories or bool(re.search(
            r"\b(?:nlp|llms?|acl|emnlp|naacl|eacl|coling|language models?|computational linguistics|machine translation|"
            r"natural language|text generation|question answering)\b", text, re.I))
    if source == "pmlr":
        return bool({"cs.AI", "cs.LG", "cs.CV", "stat.ML"} & set(topic.categories) or re.search(
            r"\b(?:learning|neural|nas|anomaly detection|vision|llms?|language models?|agent|optimization|classifier|classification)\b",
            text, re.I))
    if source == "cvf":
        return bool("cs.CV" in topic.categories or re.search(
            r"\b(?:vision|image|3d|point cloud|segmentation|detection|tracking|video|depth|slam|nerf|gaussian)\b",
            text, re.I))
    return True
