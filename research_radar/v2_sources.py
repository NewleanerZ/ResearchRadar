"""V2 academic & code-signal adapters: Semantic Scholar, CVF Open Access, GitHub, and Hugging Face."""

from datetime import date, timedelta
from html import unescape
import re
from urllib.parse import quote

from .cs_sources import doi_value, matches, plain, serial_page, within
from .models import Page, Publication, Record, arxiv_identifier

SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/search"
CVF_BASE = "https://openaccess.thecvf.com"
GITHUB_API = "https://api.github.com/repos"
HF_API = "https://huggingface.co/api"

S2_FIELDS = (
    "paperId,externalIds,url,title,abstract,venue,publicationVenue,year,"
    "publicationDate,authors,isOpenAccess,openAccessPdf,citationCount,publicationTypes"
)

GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
HF_RE = re.compile(r"https?://(?:www\.)?huggingface\.co/(?:(datasets|spaces)/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


def extract_code_and_hf_refs(text: str | None) -> dict[str, list[str]]:
    """Extract normalized GitHub repo slugs and Hugging Face dataset/model slugs from text."""
    if not text:
        return {"github": [], "huggingface_datasets": [], "huggingface_models": []}
    github_repos = []
    for owner, repo in GITHUB_RE.findall(text):
        repo = repo.rstrip(".,;:)]}>").removesuffix(".git")
        slug = f"{owner}/{repo}"
        if repo and owner.lower() not in {"topics", "orgs", "features", "marketplace", "settings"}:
            github_repos.append(slug)
    hf_datasets, hf_models = [], []
    for kind, slug in HF_RE.findall(text):
        clean = slug.rstrip(".,;:)]}>")
        if kind == "datasets":
            hf_datasets.append(clean)
        elif not kind:
            hf_models.append(clean)
    return {
        "github": list(dict.fromkeys(github_repos))[:6],
        "huggingface_datasets": list(dict.fromkeys(hf_datasets))[:6],
        "huggingface_models": list(dict.fromkeys(hf_models))[:6],
    }


def parse_semantic_scholar(item: dict) -> Record | None:
    paper_id = (item.get("paperId") or "").strip()
    title = plain(item.get("title"))
    if not paper_id or not title or not re.fullmatch(r"[A-Za-z0-9_-]+", paper_id):
        return None
    ext = item.get("externalIds") or {}
    doi = doi_value(ext.get("DOI"))
    arxiv_id, _ = arxiv_identifier(str(ext.get("ArXiv") or ""))
    pub_date = None
    raw_date = item.get("publicationDate")
    if isinstance(raw_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        pub_date = date.fromisoformat(raw_date)
    year = item.get("year")
    if not (isinstance(year, int) and 1900 <= year <= 2200):
        year = pub_date.year if pub_date else None
    venue_info = item.get("publicationVenue") or {}
    venue = plain(venue_info.get("name") or item.get("venue")) or None
    pub_types = set(item.get("publicationTypes") or [])
    if venue and venue.lower() != "arxiv" and (pub_types & {"JournalArticle", "Conference"} or not arxiv_id):
        publication = Publication(status="published", venue=venue)
    elif arxiv_id:
        publication = Publication(status="preprint", venue="arXiv")
    else:
        publication = Publication(status="unknown", venue=venue)
    pdfs = []
    oa_pdf = (item.get("openAccessPdf") or {}).get("url")
    if isinstance(oa_pdf, str) and oa_pdf.startswith("https://"):
        pdfs.append(oa_pdf)
    authors = [plain(a.get("name")) for a in (item.get("authors") or []) if plain(a.get("name"))]
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
    cited = item.get("citationCount")
    return Record(
        source="semantic_scholar",
        external_id=paper_id,
        title=title,
        url=url,
        authors=authors,
        abstract=plain(item.get("abstract")) or None,
        doi=doi,
        arxiv_id=arxiv_id,
        open_pdf_urls=pdfs[:4],
        published_on=pub_date,
        published_year=None if pub_date else year,
        status=publication.status,
        publication=publication,
        citation_count=cited if isinstance(cited, int) and cited >= 0 else None,
    )


CVF_MONTHS = {"WACV": 1, "CVPR": 6, "ICCV": 10}


def parse_cvf_listing(html_text: str, conference: str, year: int) -> list[Record]:
    """Parse CVF Open Access proceedings HTML listing into Record entries."""
    records = []
    entries = re.findall(
        r'<dt class="ptitle">\s*(?:<br\s*/?>\s*)?<a href="([^"]+)">(.*?)</a>\s*</dt>\s*'
        r'<dd>(.*?)</dd>\s*<dd>(.*?)</dd>',
        html_text,
        re.S | re.I,
    )
    conf_upper = conference.upper()
    venue = f"{conf_upper} {year}"
    pub_month = CVF_MONTHS.get(conf_upper)
    for rel_path, raw_title, raw_authors, raw_links in entries:
        title = plain(unescape(raw_title))
        if not title:
            continue
        clean_path = "/" + rel_path.lstrip("/")
        if not clean_path.startswith("/content"):
            continue
        pdf_match = re.search(r'href="([^"]+\.pdf)"', raw_links, re.I)
        pdfs = []
        if pdf_match:
            pdf_rel = "/" + pdf_match[1].lstrip("/")
            pdfs.append(f"{CVF_BASE}{pdf_rel}")
        authors = [plain(unescape(name)) for name in re.findall(r"<a [^>]*>(.*?)</a>", raw_authors, re.S) if plain(name)]
        if not authors:
            authors = [plain(part) for part in plain(raw_authors).split(",") if plain(part)]
        ext_id = f"{conference.lower()}-{year}-{clean_path.rsplit('/', 1)[-1].removesuffix('.html')}"
        records.append(Record(
            source="cvf",
            external_id=ext_id[:120],
            title=title,
            url=f"{CVF_BASE}{clean_path}",
            authors=authors,
            published_year=year,
            published_month=pub_month,
            open_pdf_urls=pdfs,
            status="published",
            publication=Publication(status="published", venue=venue),
        ))
    return records


def fetch_github_signal(client, slug: str, deadline: float, token: str = "") -> dict | None:
    """Fetch public GitHub repository metadata & latest release tag within budget."""
    import time

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        return None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ResearchRadar/0.2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = client.get(f"{GITHUB_API}/{quote(slug, safe='/')}", headers=headers, timeout=min(8.0, remaining))
        if resp.status_code != 200:
            return None
        data = resp.json()
        latest_tag = None
        rel_rem = deadline - time.monotonic()
        if rel_rem > 0.5:
            rel = client.get(
                f"{GITHUB_API}/{quote(slug, safe='/')}/releases/latest",
                headers=headers,
                timeout=min(5.0, rel_rem),
            )
            if rel.status_code == 200:
                latest_tag = rel.json().get("tag_name")
        return {
            "repo": slug,
            "url": f"https://github.com/{slug}",
            "stars": data.get("stargazers_count", 0),
            "pushed_at": data.get("pushed_at"),
            "latest_release": latest_tag,
            "archived": bool(data.get("archived")),
        }
    except Exception:
        return None


def fetch_hf_signal(client, kind: str, slug: str, deadline: float, token: str = "") -> dict | None:
    """Fetch public Hugging Face model or dataset metadata within budget."""
    import time

    if kind not in {"models", "datasets"} or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        return None
    headers = {"User-Agent": "ResearchRadar/0.2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = client.get(f"{HF_API}/{kind}/{quote(slug, safe='/')}", headers=headers, timeout=min(8.0, remaining))
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "kind": kind,
            "id": slug,
            "url": f"https://huggingface.co/{'datasets/' if kind == 'datasets' else ''}{slug}",
            "downloads": data.get("downloads", 0),
            "likes": data.get("likes", 0),
            "last_modified": data.get("lastModified"),
        }
    except Exception:
        return None


class V2Catalogs:
    """Adapters for Semantic Scholar and CVF Open Access."""

    def __init__(self, request_fn, s2_api_key: str = ""):
        self.request = request_fn
        self.s2_api_key = s2_api_key
        self.cvf_cache: dict[str, list[Record]] = {}

    def semantic_scholar(self, terms: list[str], start: date, end: date, cursor: str | None,
                         deadline: float, limit: int, zone) -> Page:
        headers = {"x-api-key": self.s2_api_key} if self.s2_api_key else {}
        pub_range = f"{start.isoformat()}:{(end - timedelta(days=1)).isoformat()}"

        def query(index, offset, size):
            offset = offset or 0
            params = {
                "query": terms[index],
                "offset": offset,
                "limit": min(size, 100),
                "fields": S2_FIELDS,
                "publicationDateOrYear": pub_range,
            }
            data = self.request(SEMANTIC_SCHOLAR, params, deadline, headers=headers).json()
            items = data.get("data") or []
            records = [
                record for item in items
                if (record := parse_semantic_scholar(item))
                and matches(record, terms)
                and within(record, start, end, zone)
            ]
            next_offset = data.get("next")
            more = next_offset if isinstance(next_offset, int) and next_offset > offset and items else None
            return records, more

        return serial_page(cursor, len(terms), query, min(limit, 50))

    def cvf(self, terms: list[str], start: date, end: date, cursor: str | None,
            deadline: float, limit: int, zone) -> Page:
        import json
        import time

        years = list(range((end - timedelta(days=1)).year, start.year - 1, -1))
        archives = []
        for year in years:
            for conf in ("CVPR", "ICCV", "WACV"):
                if conf == "ICCV" and year % 2 == 0:
                    continue
                month = CVF_MONTHS[conf]
                low = date(year, month, 1)
                high = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
                if start <= low and high <= end:
                    archives.append((year, conf))
        if not archives:
            return Page(records=[], cursor=None)
        index, offset = json.loads(cursor) if cursor else (0, 0)
        collected: list[Record] = []
        next_page = None
        while index < len(archives):
            year, conf = archives[index]
            cache_key = f"{conf}{year}"
            if cache_key not in self.cvf_cache:
                resp = self.request(f"{CVF_BASE}/{cache_key}", {"day": "all"}, deadline, allowed_statuses=(404,))
                self.cvf_cache[cache_key] = parse_cvf_listing(resp.text, conf, year) if resp.status_code == 200 else []
                if len(self.cvf_cache) > 6:
                    self.cvf_cache.pop(next(iter(self.cvf_cache)))
            eligible = [
                record for record in self.cvf_cache[cache_key]
                if within(record, start, end, zone) and matches(record, terms)
            ]
            need = max(1, limit - len(collected))
            chunk = eligible[offset:offset + need]
            collected.extend(chunk)
            at = offset + len(chunk)
            if at < len(eligible):
                next_page = [index, at]
                break
            index += 1
            offset = 0
            if collected or time.monotonic() >= deadline - 1.0:
                if index < len(archives):
                    next_page = [index, 0]
                break
        return Page(records=collected, cursor=json.dumps(next_page) if next_page is not None else None)
