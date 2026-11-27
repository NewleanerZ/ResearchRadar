"""Exploratory multi-path open PDF discovery and webpage-first paper summarization."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .models import Record, Settings, paper_publication, publication_label, utc_now
from .ranking import best_record
from .sources import OPENALEX, arxiv_identifier, normalize_doi, tidy
from .store import Store, write
from .v2_sources import SEMANTIC_SCHOLAR

MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_HTML_BYTES = 512 * 1024
MAX_PDF_PAGES = 40
MAX_PDF_TEXT_PER_PAGE = 18000
_MLRESEARCH_RAW_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/mlresearch/(v\d+)/main/assets/(.+\.pdf)$",
    re.IGNORECASE,
)


def _expand_pdf_candidates(urls: list[str] | tuple[str, ...]) -> list[str]:
    expanded: list[str] = []
    for item in urls:
        if not item:
            continue
        expanded.append(item)
        match = _MLRESEARCH_RAW_RE.match(item.strip())
        if match:
            expanded.append(f"https://proceedings.mlr.press/{match.group(1)}/{match.group(2)}")
    return list(dict.fromkeys(expanded))


def _has_transient_failure(attempts: list[dict], deadline: float) -> bool:
    if time.monotonic() >= deadline - 1.2:
        return True
    for item in attempts:
        reason = str(item.get("reason") or "")
        if any(tok in reason for tok in ("超时", "网络请求失败", "剩余超时时间不足", "HTTP 429")):
            return True
        if re.search(r"HTTP 5\d\d", reason):
            return True
    return False
USER_AGENT = "ResearchRadar/1.0 (+https://researchradar.local)"

PDF_HOSTS = {
    "arxiv.org", "www.arxiv.org", "export.arxiv.org", "ar5iv.labs.arxiv.org",
    "openreview.net", "openaccess.thecvf.com", "aclanthology.org",
    "proceedings.mlr.press", "proceedings.neurips.cc", "ojs.aaai.org",
    "jmlr.org", "www.jmlr.org", "ceur-ws.org",
    "pmc.ncbi.nlm.nih.gov", "europepmc.org", "www.ebi.ac.uk",
    "biorxiv.org", "www.biorxiv.org", "medrxiv.org", "www.medrxiv.org",
    "hal.science", "zenodo.org", "link.springer.com",
    "mdpi.com", "www.mdpi.com", "frontiersin.org", "www.frontiersin.org",
    "nature.com", "www.nature.com", "journals.plos.org", "pdfs.semanticscholar.org",
    "raw.githubusercontent.com",
}

PDF_PROGRAM = """
import json
import sys
from pypdf import PdfReader
path, page_limit, text_limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
reader = PdfReader(path)
if reader.is_encrypted:
    raise ValueError("encrypted")
count = len(reader.pages)
pages = []
for index in range(min(count, page_limit)):
    page = reader.pages[index]
    stream = page.get_contents()
    if stream is not None and len(stream.get_data()) > 4 * 1024 * 1024:
        pages.append("")
        continue
    pages.append(" ".join((page.extract_text() or "").split())[:text_limit])
print(json.dumps({"count": count, "pages": pages}, ensure_ascii=True))
"""


def is_safe_public_https_url(url: str) -> str | None:
    """Validate an HTTPS URL and reject private/loopback/internal hosts or unsafe characters."""
    if not isinstance(url, str) or not url:
        return None
    clean = url.strip()
    if any(ch in clean for ch in ("\r", "\n", "\t", " ", "(", ")", "\\")):
        return None
    try:
        parsed = urlsplit(clean)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.casefold()
    if host in {"localhost", "0.0.0.0"} or host.endswith((".localhost", ".local", ".internal", ".arpa")):
        return None
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return None
    except ValueError:
        pass
    if ".." in parsed.path.split("/") or parsed.path in {"", "/"}:
        return None
    if host == "raw.githubusercontent.com" and not re.fullmatch(
        r"/mlresearch/v\d+/[A-Za-z0-9_./-]+\.pdf", parsed.path
    ):
        return None
    return clean


def safe_pdf_url(url: str) -> str | None:
    """Validate URL against allowed scholarly PDF hosts while permitting legitimate query IDs."""
    checked = is_safe_public_https_url(url)
    if not checked:
        return None
    host = (urlsplit(checked).hostname or "").casefold()
    return checked if host in PDF_HOSTS else None


class _PaperHtmlParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.pdf_links: list[str] = []
        self.meta: dict[str, str] = {}
        self.title_chunks: list[str] = []
        self.body_chunks: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._in_para = False

    def handle_starttag(self, tag, attrs):
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
        if tag_l in {"script", "style", "noscript", "nav", "header", "footer", "svg"}:
            self._skip_depth += 1
            return
        if tag_l == "title":
            self._in_title = True
        elif tag_l in {"p", "section", "article"} and self._skip_depth == 0:
            self._in_para = True
        elif tag_l == "meta":
            key = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content") or ""
            if key and content:
                if key in {"citation_pdf_url", "wkhtmltopdf_pdf_url"}:
                    self.pdf_links.append(urljoin(self.base_url, content))
                elif key not in self.meta:
                    self.meta[key] = tidy(content)
        elif tag_l == "link":
            rel = attr_map.get("rel", "").lower()
            href = attr_map.get("href", "")
            mime = attr_map.get("type", "").lower()
            if href and ("pdf" in mime or ("alternate" in rel and href.lower().endswith(".pdf"))):
                self.pdf_links.append(urljoin(self.base_url, href))
        elif tag_l == "a":
            href = attr_map.get("href", "")
            if href and (".pdf" in href.lower() or "/pdf" in href.lower()):
                self.pdf_links.append(urljoin(self.base_url, href))

    def handle_endtag(self, tag):
        tag_l = tag.lower()
        if tag_l in {"script", "style", "noscript", "nav", "header", "footer", "svg"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag_l == "title":
            self._in_title = False
        elif tag_l in {"p", "section", "article"}:
            self._in_para = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        clean = tidy(data)
        if not clean:
            return
        if self._in_title:
            self.title_chunks.append(clean)
        elif self._in_para and sum(len(x) for x in self.body_chunks) < 3500:
            self.body_chunks.append(clean)


def _request_with_redirects(
    client: httpx.Client,
    url: str,
    deadline: float,
    *,
    accept: str,
    max_bytes: int,
    url_validator=is_safe_public_https_url,
    max_redirects: int = 5,
) -> tuple[str, dict[str, str], bytes]:
    current = url_validator(url)
    if not current:
        raise ValueError("来源链接不符合安全校验要求")
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    for _ in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            raise ValueError("剩余超时时间不足")
        try:
            with client.stream("GET", current, headers=headers, follow_redirects=False,
                               timeout=min(15.0, remaining)) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("重定向缺少目标地址")
                    next_url = url_validator(urljoin(current, location))
                    if not next_url:
                        raise ValueError("重定向目标不在允许的安全范围内")
                    current = next_url
                    continue
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                cl = response.headers.get("Content-Length", "")
                if cl.isdigit() and int(cl) > max_bytes:
                    raise ValueError("响应体积超出下载上限")
                buf = bytearray()
                for block in response.iter_bytes(chunk_size=65536):
                    buf.extend(block)
                    if len(buf) > max_bytes or time.monotonic() >= deadline:
                        raise ValueError("下载体积或时间超出上限")
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                return current, resp_headers, bytes(buf)
        except httpx.HTTPError as err:
            raise ValueError(f"网络请求失败({type(err).__name__})") from None
    raise ValueError("重定向次数过多")


def parse_pdf_bytes_ephemeral(raw_bytes: bytes, deadline: float) -> tuple[dict, str]:
    """Parse PDF bytes in a temporary directory without retaining PDF files on disk."""
    if not raw_bytes[:8].startswith(b"%PDF-"):
        raise ValueError("返回内容不是有效的 PDF 文件")
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        raise ValueError("没有剩余 PDF 解析时间")
    digest = sha256(raw_bytes).hexdigest()
    with TemporaryDirectory(prefix="research-radar-pdf-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(raw_bytes)
        try:
            process = subprocess.run(
                [sys.executable, "-B", "-I", "-c", PDF_PROGRAM, str(path), str(MAX_PDF_PAGES), str(MAX_PDF_TEXT_PER_PAGE)],
                timeout=min(18.0, remaining), check=True, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            parsed = json.loads(process.stdout)
        except (ValueError, OSError, subprocess.SubprocessError):
            raise ValueError("PDF 解析失败或超时") from None
        if not any(parsed["pages"]):
            raise ValueError("PDF 没有可提取的文本层")
    return parsed, digest


def download_pdf_bytes(client: httpx.Client, url: str, deadline: float, *, strict_hosts: bool = False) -> tuple[str, bytes]:
    validator = safe_pdf_url if strict_hosts else is_safe_public_https_url
    final_url, _headers, raw = _request_with_redirects(
        client, url, deadline, accept="application/pdf,*/*;q=0.8", max_bytes=MAX_PDF_BYTES, url_validator=validator,
    )
    if not raw[:8].startswith(b"%PDF-"):
        raise ValueError("目标未返回 PDF 文件")
    return final_url, raw


def _json_get(client: httpx.Client, url: str, deadline: float, params: dict | None = None,
              headers: dict | None = None) -> dict | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return None
    try:
        req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        resp = client.get(url, params=params, headers=req_headers, follow_redirects=True, timeout=min(10.0, remaining))
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _reconstruct_openalex_abstract(inverted: dict | None) -> str | None:
    if not inverted:
        return None
    tokens = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return tidy(" ".join(word for _, word in tokens)) or None


def _title_close(query_title: str, candidate_title: str) -> bool:
    if not query_title or not candidate_title:
        return True
    q_words = {w for w in re.findall(r"[a-z0-9]+", query_title.casefold()) if len(w) >= 3}
    c_words = {w for w in re.findall(r"[a-z0-9]+", candidate_title.casefold()) if len(w) >= 3}
    if not q_words or not c_words:
        return True
    overlap = len(q_words & c_words) / max(1, min(len(q_words), len(c_words)))
    return overlap >= 0.6


def explore_pdf_sources(
    client: httpx.Client,
    settings: Settings,
    *,
    title: str = "",
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
    records: list[Record] | tuple[Record, ...] = (),
    preferred_urls: list[str] | tuple[str, ...] = (),
    deadline: float | None = None,
    return_bytes: bool = False,
    strict_direct_only: bool = False,
) -> dict:
    """Try multiple scholarly APIs and landing pages to locate and parse an open PDF."""
    deadline = deadline or (time.monotonic() + 35.0)
    clean_doi = normalize_doi(doi) or next((normalize_doi(r.doi) for r in records if r.doi), None)
    clean_arxiv = None
    arxiv_ver = ""
    for candidate in [arxiv_id or "", *(r.arxiv_id or "" for r in records), url or ""]:
        k, v = arxiv_identifier(candidate)
        if k:
            clean_arxiv, arxiv_ver = k, v
            break
    clean_title = tidy(title) or (best_record({"records": list(records)}).title if records else "")
    landing_urls: list[str] = []
    for candidate in [url, *(r.url for r in records)]:
        if candidate and is_safe_public_https_url(candidate):
            landing_urls.append(candidate)
    if clean_doi:
        landing_urls.append(f"https://doi.org/{clean_doi}")

    attempts: list[dict] = []
    tried_urls: set[str] = set()

    def try_pdf(strategy: str, candidate_url: str, *, strict_hosts: bool = False) -> dict | None:
        checked = (safe_pdf_url if strict_hosts else is_safe_public_https_url)(candidate_url)
        if not checked or checked in tried_urls or time.monotonic() >= deadline - 0.5:
            return None
        tried_urls.add(checked)
        try:
            final_url, raw = download_pdf_bytes(client, checked, deadline, strict_hosts=strict_hosts)
            if return_bytes:
                attempts.append({"strategy": strategy, "url": final_url, "status": "ok", "bytes": len(raw)})
                return {"found": True, "pdf_url": final_url, "strategy": strategy, "pdf_bytes": raw, "attempts": attempts}
            parsed, digest = parse_pdf_bytes_ephemeral(raw, deadline)
            attempts.append({"strategy": strategy, "url": final_url, "status": "ok", "pages": parsed["count"]})
            return {
                "found": True,
                "pdf_url": final_url,
                "strategy": strategy,
                "pages": parsed["pages"],
                "page_count": parsed["count"],
                "content_hash": digest,
                "attempts": attempts,
            }
        except ValueError as err:
            attempts.append({"strategy": strategy, "url": checked, "status": "failed", "reason": str(err)[:140]})
            return None

    # Strategy 0: Cached/prechecked or already indexed URL fast-path (avoids slow external lookups)
    for item_url in _expand_pdf_candidates(preferred_urls):
        hit = try_pdf("cached", item_url, strict_hosts=strict_direct_only)
        if hit:
            return hit

    # Strategy 1: Direct URLs (arXiv mirrors + record open_pdf_urls + explicit PDF url)
    direct_urls: list[str] = []
    if clean_arxiv:
        suffix = arxiv_ver if re.fullmatch(r"v\d+", arxiv_ver or "") else ""
        direct_urls.append(f"https://arxiv.org/pdf/{clean_arxiv}{suffix}")
        direct_urls.append(f"https://export.arxiv.org/pdf/{clean_arxiv}{suffix}")
    for rec in records:
        direct_urls.extend(rec.open_pdf_urls)
    if url and (".pdf" in url.lower() or "/pdf" in url.lower()):
        direct_urls.append(url)
    for item_url in _expand_pdf_candidates(direct_urls):
        hit = try_pdf("direct", item_url, strict_hosts=strict_direct_only)
        if hit:
            return hit

    if strict_direct_only:
        return {
            "found": False,
            "attempts": attempts,
            "transient": _has_transient_failure(attempts, deadline),
            "reason": "未在受支持直链获取到可用 PDF",
        }

    # Strategy 2: OpenAlex OA lookup (DOI or title)
    oa_key = settings.openalex_key.get_secret_value()
    oa_work = None
    if clean_doi and time.monotonic() < deadline - 2.0:
        params = {"api_key": oa_key} if oa_key else None
        oa_work = _json_get(client, f"{OPENALEX}/https://doi.org/{quote(clean_doi, safe='/')}", deadline, params=params)
    if not oa_work and clean_title and time.monotonic() < deadline - 2.0:
        params = {"search": clean_title, "per-page": 3}
        if oa_key:
            params["api_key"] = oa_key
        data = _json_get(client, OPENALEX, deadline, params=params)
        for cand in (data or {}).get("results", []):
            if _title_close(clean_title, cand.get("title") or ""):
                oa_work = cand
                break
    if oa_work:
        if not clean_doi and oa_work.get("doi"):
            clean_doi = normalize_doi(oa_work["doi"])
        oa_pdfs = []
        for loc in [oa_work.get("best_oa_location"), oa_work.get("primary_location"), *(oa_work.get("locations") or [])]:
            if not loc:
                continue
            if loc.get("pdf_url"):
                oa_pdfs.append(loc["pdf_url"])
            if loc.get("landing_page_url"):
                landing_urls.append(loc["landing_page_url"])
        for pdf_u in _expand_pdf_candidates(oa_pdfs):
            hit = try_pdf("openalex", pdf_u)
            if hit:
                return hit

    # Strategy 3: Unpaywall OA lookup by DOI
    if clean_doi and time.monotonic() < deadline - 2.0:
        email = settings.crossref_mailto or "researchradar@example.org"
        unpaywall = _json_get(
            client, f"https://api.unpaywall.org/v2/{quote(clean_doi, safe='')}", deadline, params={"email": email},
        )
        if unpaywall:
            up_pdfs = []
            for loc in [unpaywall.get("best_oa_location"), *(unpaywall.get("oa_locations") or [])]:
                if not loc:
                    continue
                if loc.get("url_for_pdf"):
                    up_pdfs.append(loc["url_for_pdf"])
                if loc.get("url_for_landing_page"):
                    landing_urls.append(loc["url_for_landing_page"])
        for pdf_u in _expand_pdf_candidates(up_pdfs):
            hit = try_pdf("unpaywall", pdf_u)
            if hit:
                return hit

    # Strategy 4: Semantic Scholar OA lookup
    if (clean_doi or clean_arxiv or clean_title) and time.monotonic() < deadline - 2.0:
        s2_key = settings.s2_api_key.get_secret_value()
        s2_headers = {"x-api-key": s2_key} if s2_key else {}
        s2_paper = None
        fields = "title,openAccessPdf,url,externalIds"
        if clean_doi:
            s2_paper = _json_get(client, f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(clean_doi, safe='')}",
                                 deadline, params={"fields": fields}, headers=s2_headers)
        elif clean_arxiv:
            s2_paper = _json_get(client, f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{clean_arxiv}",
                                 deadline, params={"fields": fields}, headers=s2_headers)
        elif clean_title:
            s2_search = _json_get(client, SEMANTIC_SCHOLAR, deadline,
                                  params={"query": clean_title, "limit": 2, "fields": fields}, headers=s2_headers)
            for cand in (s2_search or {}).get("data", []):
                if _title_close(clean_title, cand.get("title") or ""):
                    s2_paper = cand
                    break
        if s2_paper:
            if s2_paper.get("url"):
                landing_urls.append(s2_paper["url"])
            new_arxiv = (s2_paper.get("externalIds") or {}).get("ArXiv")
            if new_arxiv and not clean_arxiv:
                k, _ = arxiv_identifier(new_arxiv)
                if k:
                    hit = try_pdf("semantic_scholar_arxiv", f"https://arxiv.org/pdf/{k}")
                    if hit:
                        return hit
            oa_pdf = (s2_paper.get("openAccessPdf") or {}).get("url")
            if oa_pdf:
                hit = try_pdf("semantic_scholar", oa_pdf)
                if hit:
                    return hit

    # Strategy 5: Europe PMC / PMC lookup (strong biomedical / medical imaging coverage)
    if (clean_doi or clean_title) and time.monotonic() < deadline - 2.0:
        epmc_q = f'DOI:"{clean_doi}"' if clean_doi else f'TITLE:"{clean_title}"'
        epmc = _json_get(
            client,
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            deadline,
            params={"query": epmc_q, "format": "json", "resultType": "core", "pageSize": 2},
        )
        epmc_pdfs = []
        for item in ((epmc or {}).get("resultList") or {}).get("result", []):
            if not clean_doi and not _title_close(clean_title, item.get("title") or ""):
                continue
            for entry in ((item.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
                if (entry.get("documentStyle") or "").lower() == "pdf" and entry.get("url"):
                    epmc_pdfs.append(entry["url"])
            pmcid = item.get("pmcid")
            if pmcid and re.fullmatch(r"PMC\d+", pmcid):
                epmc_pdfs.append(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
        for pdf_u in _expand_pdf_candidates(epmc_pdfs):
            hit = try_pdf("europepmc", pdf_u)
            if hit:
                return hit

    # Strategy 6: Landing page HTML citation_pdf_url & PDF link extraction
    tried_pages: set[str] = set()
    for page_url in dict.fromkeys(landing_urls):
        safe_page = is_safe_public_https_url(page_url)
        if not safe_page or safe_page in tried_pages or len(tried_pages) >= 3 or time.monotonic() >= deadline - 1.5:
            continue
        tried_pages.add(safe_page)
        try:
            final_page, headers, raw_html = _request_with_redirects(
                client, safe_page, deadline, accept="text/html,application/xhtml+xml,*/*;q=0.8", max_bytes=MAX_HTML_BYTES,
            )
        except ValueError as err:
            attempts.append({"strategy": "landing_page", "url": safe_page, "status": "failed", "reason": str(err)[:140]})
            continue
        content_type = headers.get("content-type", "").lower()
        if "application/pdf" in content_type or raw_html[:8].startswith(b"%PDF-"):
            if return_bytes:
                return {"found": True, "pdf_url": final_page, "strategy": "landing_page", "pdf_bytes": raw_html, "attempts": attempts}
            try:
                parsed, digest = parse_pdf_bytes_ephemeral(raw_html, deadline)
                return {"found": True, "pdf_url": final_page, "strategy": "landing_page",
                        "pages": parsed["pages"], "page_count": parsed["count"], "content_hash": digest, "attempts": attempts}
            except ValueError:
                continue
        parser = _PaperHtmlParser(final_page)
        parser.feed(raw_html.decode("utf-8", errors="ignore"))
        for discovered_pdf in _expand_pdf_candidates(parser.pdf_links[:5]):
            hit = try_pdf("landing_page", discovered_pdf)
            if hit:
                return hit

    return {
        "found": False,
        "attempts": attempts,
        "landing_urls": list(dict.fromkeys(landing_urls))[:4],
        "transient": _has_transient_failure(attempts, deadline),
        "reason": "已尝试所有可用开放检索路径，未能获取公开可解析的原始 PDF（可能仅限机构订阅或反爬拦截）。",
    }


def fetch_paper_webpage_info(
    client: httpx.Client,
    settings: Settings,
    *,
    title: str = "",
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
    records: list[Record] | tuple[Record, ...] = (),
    deadline: float | None = None,
) -> dict:
    """Collect paper summary from abstracts, scholarly APIs, and public webpages without downloading PDFs."""
    deadline = deadline or (time.monotonic() + 20.0)
    primary = best_record({"records": list(records)}) if records else None
    clean_doi = normalize_doi(doi) or (primary.doi if primary else None)
    clean_arxiv = arxiv_id or (primary.arxiv_id if primary else None)
    clean_title = tidy(title) or (primary.title if primary else "")
    abstract = tidy(primary.abstract) if primary and primary.abstract else ""
    authors = primary.authors[:6] if primary else []
    pub_label = publication_label(paper_publication(list(records))) if records else None
    citations = max((r.citation_count for r in records if r.citation_count is not None), default=None) if records else None
    landing = url or (primary.url if primary else None)
    sources_used: list[str] = ["local_record"] if primary else []
    webpage_excerpt = ""

    # Enrich via OpenAlex JSON if missing abstract or metadata
    if (len(abstract) < 180 or not clean_title or citations is None) and time.monotonic() < deadline - 2.0:
        oa_key = settings.openalex_key.get_secret_value()
        oa_work = None
        if clean_doi:
            params = {"api_key": oa_key} if oa_key else None
            oa_work = _json_get(client, f"{OPENALEX}/https://doi.org/{quote(clean_doi, safe='/')}", deadline, params=params)
        elif clean_title:
            params = {"search": clean_title, "per-page": 3}
            if oa_key:
                params["api_key"] = oa_key
            data = _json_get(client, OPENALEX, deadline, params=params)
            for cand in (data or {}).get("results", []):
                if _title_close(clean_title, cand.get("title") or ""):
                    oa_work = cand
                    break
        if oa_work:
            sources_used.append("openalex")
            clean_title = clean_title or tidy(oa_work.get("title"))
            clean_doi = clean_doi or normalize_doi(oa_work.get("doi"))
            if not abstract:
                abstract = _reconstruct_openalex_abstract(oa_work.get("abstract_inverted_index")) or ""
            if citations is None and isinstance(oa_work.get("cited_by_count"), int):
                citations = oa_work["cited_by_count"]
            if not authors:
                authors = [a["author"]["display_name"] for a in (oa_work.get("authorships") or [])
                           if (a.get("author") or {}).get("display_name")][:6]
            loc = oa_work.get("primary_location") or oa_work.get("best_oa_location") or {}
            if not landing and loc.get("landing_page_url"):
                landing = loc["landing_page_url"]
            if not pub_label and (loc.get("source") or {}).get("display_name"):
                pub_label = loc["source"]["display_name"]

    # Enrich via EuropePMC / Semantic Scholar when abstract is still short
    if len(abstract) < 180 and (clean_doi or clean_title) and time.monotonic() < deadline - 2.0:
        epmc_q = f'DOI:"{clean_doi}"' if clean_doi else f'TITLE:"{clean_title}"'
        epmc = _json_get(
            client,
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            deadline,
            params={"query": epmc_q, "format": "json", "resultType": "core", "pageSize": 2},
        )
        for item in ((epmc or {}).get("resultList") or {}).get("result", []):
            if clean_doi or _title_close(clean_title, item.get("title") or ""):
                if item.get("abstractText"):
                    abstract = re.sub(r"<[^>]+>", " ", item["abstractText"])
                    abstract = tidy(abstract)
                    sources_used.append("europepmc")
                if not pub_label and item.get("journalTitle"):
                    pub_label = item["journalTitle"]
                break

    # Fetch public HTML webpage metadata & excerpt (without downloading PDFs)
    candidate_page = landing or (f"https://arxiv.org/abs/{clean_arxiv}" if clean_arxiv else f"https://doi.org/{clean_doi}" if clean_doi else None)
    if candidate_page and time.monotonic() < deadline - 1.5:
        safe_page = is_safe_public_https_url(candidate_page)
        if safe_page:
            try:
                final_page, headers, raw_html = _request_with_redirects(
                    client, safe_page, deadline, accept="text/html,application/xhtml+xml", max_bytes=MAX_HTML_BYTES,
                )
                if "pdf" not in headers.get("content-type", "").lower() and not raw_html[:8].startswith(b"%PDF-"):
                    parser = _PaperHtmlParser(final_page)
                    parser.feed(raw_html.decode("utf-8", errors="ignore"))
                    sources_used.append("webpage")
                    landing = final_page
                    clean_title = clean_title or parser.meta.get("citation_title") or tidy(" ".join(parser.title_chunks))
                    meta_abs = (parser.meta.get("citation_abstract") or parser.meta.get("description")
                                or parser.meta.get("og:description") or "")
                    if len(meta_abs) > len(abstract):
                        abstract = meta_abs
                    webpage_excerpt = " ".join(parser.body_chunks)[:2200]
            except ValueError:
                pass

    return {
        "title": clean_title,
        "authors": authors,
        "publication": pub_label,
        "citation_count": citations,
        "doi": clean_doi,
        "arxiv_id": clean_arxiv,
        "url": landing,
        "abstract": abstract or None,
        "webpage_excerpt": webpage_excerpt or None,
        "sources_used": list(dict.fromkeys(sources_used)),
        "fetched_pdf": False,
    }


def _verify_pdf_url_lightweight(client: httpx.Client, url: str, deadline: float) -> str:
    """Verify via safe redirect + byte-range/header peek that a URL serves a real PDF without downloading the full file."""
    current = is_safe_public_https_url(url)
    if not current:
        raise ValueError("来源链接不符合安全校验要求")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,*/*;q=0.8",
        "Range": "bytes=0-63",
    }
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            raise ValueError("检测超时")
        try:
            with client.stream("GET", current, headers=headers, follow_redirects=False,
                               timeout=min(10.0, remaining)) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("重定向缺少目标地址")
                    next_url = is_safe_public_https_url(urljoin(current, location))
                    if not next_url:
                        raise ValueError("重定向目标不在安全范围内")
                    current = next_url
                    continue
                if response.status_code in {200, 206}:
                    ctype = response.headers.get("content-type", "").lower()
                    total_bytes: int | None = None
                    cr = response.headers.get("Content-Range", "")
                    if m_cr := re.search(r"/(\d+)\s*$", cr):
                        total_bytes = int(m_cr.group(1))
                    elif (cl := response.headers.get("Content-Length", "")).isdigit() and response.status_code == 200:
                        total_bytes = int(cl)
                    if total_bytes is not None and total_bytes > MAX_PDF_BYTES:
                        raise ValueError("响应体积超出下载上限")
                    head = b""
                    for chunk in response.iter_bytes(chunk_size=128):
                        head += chunk
                        if len(head) >= 16:
                            break
                    if head[:5] == b"%PDF-" or ("application/pdf" in ctype and not head.lstrip().startswith(b"<")):
                        return current
                    raise ValueError("目标未返回 PDF 文件头")
                if response.status_code == 416 and "application/pdf" in response.headers.get("content-type", "").lower():
                    return current
                raise ValueError(f"HTTP {response.status_code}")
        except httpx.HTTPError as err:
            raise ValueError(f"网络失败({type(err).__name__})") from None
    raise ValueError("重定向次数过多")


def probe_pdf_availability(
    client: httpx.Client,
    settings: Settings,
    *,
    title: str = "",
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
    records: list[Record] | tuple[Record, ...] = (),
    deadline: float | None = None,
    include_slow_apis: bool = False,
) -> dict:
    """Lightweight exploratory check for whether a paper's original PDF is obtainable without full download."""
    deadline = deadline or (time.monotonic() + 18.0)
    clean_doi = normalize_doi(doi) or next((normalize_doi(r.doi) for r in records if r.doi), None)
    clean_arxiv, arxiv_ver = None, ""
    for candidate in [arxiv_id or "", *(r.arxiv_id or "" for r in records), url or ""]:
        k, v = arxiv_identifier(candidate)
        if k:
            clean_arxiv, arxiv_ver = k, v
            break
    clean_title = tidy(title) or (best_record({"records": list(records)}).title if records else "")
    landing_urls: list[str] = []
    for candidate in [url, *(r.url for r in records)]:
        if candidate and is_safe_public_https_url(candidate):
            landing_urls.append(candidate)
    if clean_doi:
        landing_urls.append(f"https://doi.org/{clean_doi}")

    tried: set[str] = set()
    last_reason = "无可用公开 PDF 来源"

    def try_probe(strategy: str, pdf_u: str) -> dict | None:
        nonlocal last_reason
        checked = is_safe_public_https_url(pdf_u)
        if not checked or checked in tried or time.monotonic() >= deadline - 0.3:
            return None
        tried.add(checked)
        try:
            final_u = _verify_pdf_url_lightweight(client, checked, deadline)
            return {"status": "available", "pdf_url": final_u, "strategy": strategy, "reason": None}
        except ValueError as err:
            last_reason = f"{strategy}: {err}"
            return None

    # 1. Direct links (arXiv / open_pdf_urls / explicit .pdf)
    direct_urls: list[str] = []
    if clean_arxiv:
        suffix = arxiv_ver if re.fullmatch(r"v\d+", arxiv_ver or "") else ""
        direct_urls.extend([f"https://arxiv.org/pdf/{clean_arxiv}{suffix}", f"https://export.arxiv.org/pdf/{clean_arxiv}{suffix}"])
    for rec in records:
        direct_urls.extend(rec.open_pdf_urls)
    if url and (".pdf" in url.lower() or "/pdf" in url.lower()):
        direct_urls.append(url)
    for item_u in _expand_pdf_candidates(direct_urls):
        if hit := try_probe("direct", item_u):
            return hit

    # 2. OpenAlex OA lookup
    oa_key = settings.openalex_key.get_secret_value()
    oa_work = None
    if clean_doi and time.monotonic() < deadline - 1.5:
        oa_work = _json_get(client, f"{OPENALEX}/https://doi.org/{quote(clean_doi, safe='/')}",
                             deadline, params={"api_key": oa_key} if oa_key else None)
    if not oa_work and clean_title and not clean_doi and time.monotonic() < deadline - 1.5:
        params = {"search": clean_title, "per-page": 3}
        if oa_key:
            params["api_key"] = oa_key
        data = _json_get(client, OPENALEX, deadline, params=params)
        for cand in (data or {}).get("results", []):
            if _title_close(clean_title, cand.get("title") or ""):
                oa_work = cand
                break
    if oa_work:
        clean_doi = clean_doi or normalize_doi(oa_work.get("doi"))
        oa_pdfs = []
        for loc in [oa_work.get("best_oa_location"), oa_work.get("primary_location"), *(oa_work.get("locations") or [])]:
            if not loc:
                continue
            if loc.get("pdf_url"):
                oa_pdfs.append(loc["pdf_url"])
            if loc.get("landing_page_url"):
                landing_urls.append(loc["landing_page_url"])
        for pdf_u in dict.fromkeys(oa_pdfs):
            if hit := try_probe("openalex", pdf_u):
                return hit

    # 3. Unpaywall OA lookup by DOI
    if clean_doi and time.monotonic() < deadline - 1.5:
        email = settings.crossref_mailto or "researchradar@example.org"
        up = _json_get(client, f"https://api.unpaywall.org/v2/{quote(clean_doi, safe='')}",
                       deadline, params={"email": email})
        if up:
            up_pdfs = []
            for loc in [up.get("best_oa_location"), *(up.get("oa_locations") or [])]:
                if not loc:
                    continue
                if loc.get("url_for_pdf"):
                    up_pdfs.append(loc["url_for_pdf"])
                if loc.get("url_for_landing_page"):
                    landing_urls.append(loc["url_for_landing_page"])
            for pdf_u in dict.fromkeys(up_pdfs):
                if hit := try_probe("unpaywall", pdf_u):
                    return hit

    # 4. Optional S2 lookup when API key present or explicitly enabled
    if (include_slow_apis or settings.s2_api_key.get_secret_value()) and clean_doi and time.monotonic() < deadline - 1.5:
        s2_key = settings.s2_api_key.get_secret_value()
        s2 = _json_get(
            client, f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(clean_doi, safe='')}",
            deadline, params={"fields": "openAccessPdf,url"}, headers={"x-api-key": s2_key} if s2_key else {},
        )
        oa_pdf = ((s2 or {}).get("openAccessPdf") or {}).get("url")
        if oa_pdf:
            if hit := try_probe("semantic_scholar", oa_pdf):
                return hit

    # 5. Landing page <meta name="citation_pdf_url"> / .pdf link extraction (at most 2 pages)
    tried_pages: set[str] = set()
    for page_url in dict.fromkeys(landing_urls):
        safe_page = is_safe_public_https_url(page_url)
        if not safe_page or safe_page in tried_pages or len(tried_pages) >= 2 or time.monotonic() >= deadline - 1.0:
            continue
        tried_pages.add(safe_page)
        try:
            final_page, headers, raw_html = _request_with_redirects(
                client, safe_page, deadline, accept="text/html,application/xhtml+xml,*/*;q=0.8", max_bytes=256 * 1024,
            )
        except ValueError as err:
            last_reason = f"landing_page: {err}"
            continue
        if "application/pdf" in headers.get("content-type", "").lower() or raw_html[:5] == b"%PDF-":
            return {"status": "available", "pdf_url": final_page, "strategy": "landing_page", "reason": None}
        parser = _PaperHtmlParser(final_page)
        parser.feed(raw_html.decode("utf-8", errors="ignore"))
        for discovered in dict.fromkeys(parser.pdf_links[:3]):
            if hit := try_probe("landing_page", discovered):
                return hit

    return {"status": "unavailable", "pdf_url": None, "strategy": None, "reason": last_reason[:180]}


def save_pdf_check(store: Store, paper_id: int, status: str, *, pdf_url: str | None = None,
                   strategy: str | None = None, reason: str | None = None, connection=None) -> None:
    now = utc_now().isoformat()
    sql = """INSERT INTO paper_pdf_checks(paper_id, status, pdf_url, strategy, reason, checked_at)
             VALUES (:pid, :status, :url, :strategy, :reason, :now)
             ON CONFLICT(paper_id) DO UPDATE SET
             status=excluded.status, pdf_url=excluded.pdf_url, strategy=excluded.strategy,
             reason=excluded.reason, checked_at=excluded.checked_at"""
    params = {"pid": paper_id, "status": status, "url": pdf_url, "strategy": strategy, "reason": reason, "now": now}
    if connection is not None:
        write(connection, sql, **params)
    else:
        store.execute(sql, **params)


def precheck_library_pdfs(store: Store, settings: Settings, topic_id: str | None = None,
                          *, force: bool = False, limit: int = 120, deadline_seconds: float = 90.0) -> dict:
    """Pre-check PDF obtainability once per library paper and persist status without storing PDFs."""
    topics = [store.topic(topic_id)[0]] if topic_id else [t for t in store.topics() if not t.archived]
    existing_checks = {
        r["paper_id"]: r for r in store.all("SELECT * FROM paper_pdf_checks")
    }
    indexed_docs = {
        r["paper_id"]: r for r in store.all("SELECT paper_id, source_url FROM paper_documents WHERE status='indexed'")
    }
    seen_ids: set[int] = set()
    to_check: list[tuple[int, str, list[Record]]] = []
    auto_marked = 0
    for topic in topics:
        for paper in store.papers(topic.id):
            pid = paper["paper_id"]
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            if pid in indexed_docs and (force or pid not in existing_checks):
                save_pdf_check(store, pid, "available", pdf_url=indexed_docs[pid]["source_url"], strategy="indexed")
                auto_marked += 1
                continue
            if not force and pid in existing_checks:
                continue
            to_check.append((pid, paper["title"], paper["records"]))
    if limit > 0:
        to_check = to_check[:limit]

    deadline = time.monotonic() + deadline_seconds
    available_count = auto_marked
    unavailable_count = 0

    def _worker(entry: tuple[int, str, list[Record]]) -> tuple[int, dict]:
        pid, title, recs = entry
        with httpx.Client(timeout=12.0) as client:
            res = probe_pdf_availability(
                client, settings, title=title, records=recs,
                deadline=min(deadline, time.monotonic() + 14.0),
            )
        return pid, res

    if to_check:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_worker, item) for item in to_check]
            for fut in as_completed(futures):
                if time.monotonic() >= deadline:
                    break
                try:
                    pid, res = fut.result()
                except Exception as err:
                    continue
                save_pdf_check(
                    store, pid, res["status"],
                    pdf_url=res.get("pdf_url"), strategy=res.get("strategy"), reason=res.get("reason"),
                )
                if res["status"] == "available":
                    available_count += 1
                else:
                    unavailable_count += 1

    return {
        "checked": available_count + unavailable_count,
        "available": available_count,
        "unavailable": unavailable_count,
        "remaining": max(0, len(seen_ids) - len(store.all("SELECT paper_id FROM paper_pdf_checks"))),
    }


def cached_pdf_urls(store: Store, paper_id: int) -> list[str]:
    """Return previously verified or indexed PDF URLs for fast-path fetching."""
    urls: list[str] = []
    check_row = store.one("SELECT pdf_url FROM paper_pdf_checks WHERE paper_id=:pid", pid=paper_id)
    if check_row and check_row["pdf_url"]:
        urls.append(check_row["pdf_url"])
    doc_row = store.one(
        "SELECT source_url FROM paper_documents WHERE paper_id=:pid AND status='indexed'",
        pid=paper_id,
    )
    if doc_row and doc_row["source_url"]:
        urls.append(doc_row["source_url"])
    return list(dict.fromkeys(urls))


def mark_pdf_unavailable_if_confirmed(
    store: Store,
    paper_id: int,
    explored: dict,
    *,
    fallback_reason: str = "未能获取公开 PDF",
) -> None:
    """Only mark a paper unavailable when failure is non-transient; keep existing available badges on timeouts/blips."""
    if explored.get("transient"):
        return
    reason = str(explored.get("reason") or fallback_reason)[:180]
    save_pdf_check(store, paper_id, "unavailable", reason=reason)

