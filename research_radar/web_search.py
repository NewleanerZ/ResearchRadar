"""Model-agnostic open-source web search (SearXNG / DuckDuckGo / GitHub / Hugging Face) and SSRF-safe webpage reader."""

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import ipaddress
import re
import time
from typing import Literal
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .pdf_explorer import USER_AGENT, _request_with_redirects
from .sources import tidy

MAX_WEB_BYTES = 768 * 1024


def safe_public_web_url(url: str) -> str | None:
    """Validate an HTTPS public URL (allowing root '/' paths) and block loopback/private targets."""
    if not isinstance(url, str) or not url:
        return None
    clean = url.strip()
    if any(ch in clean for ch in ("\r", "\n", "\t", " ", "<", ">", "\\")):
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
    if ".." in parsed.path.split("/"):
        return None
    return clean


def safe_searxng_url(url: str) -> str | None:
    """Allow local or HTTPS SearXNG base URLs configured explicitly by the operator."""
    clean = (url or "").strip().rstrip("/")
    if not clean:
        return None
    parsed = urlsplit(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return clean


class _DdgHtmlParser(HTMLParser):
    """Parse DuckDuckGo HTML search results without extra external dependencies."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._in_title = False
        self._in_snippet = False
        self._cur_title: list[str] = []
        self._cur_href: str = ""
        self._cur_snippet: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
        cls = attr_map.get("class", "")
        if tag.lower() == "a" and ("result__a" in cls or "result-link" in cls):
            self._in_title = True
            self._cur_title = []
            self._cur_href = attr_map.get("href", "")
        elif "result__snippet" in cls or "result-snippet" in cls:
            self._in_snippet = True
            self._cur_snippet = []

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._in_title:
            self._in_title = False
            title = tidy(" ".join(self._cur_title))
            href = self._cur_href
            if href.startswith("//"):
                href = "https:" + href
            if title and href.startswith("https://"):
                self.results.append({"title": title, "url": href, "snippet": "", "source": "duckduckgo"})
        elif self._in_snippet and tag.lower() in {"a", "div", "span", "td"}:
            self._in_snippet = False
            snippet = tidy(" ".join(self._cur_snippet))
            if snippet and self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet

    def handle_data(self, data):
        if self._in_title:
            self._cur_title.append(data)
        elif self._in_snippet:
            self._cur_snippet.append(data)


class _WebpageParser(HTMLParser):
    """Extract clean readable text, title, meta description, and key links from HTML."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside"}
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "pre", "blockquote", "tr", "section", "article"}

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.title_chunks: list[str] = []
        self.meta: dict[str, str] = {}
        self.blocks: list[str] = []
        self.links: list[dict] = []
        self._skip_depth = 0
        self._in_title = False
        self._current_block: list[str] = []
        self._block_tag = ""
        self._in_a = False
        self._a_href = ""
        self._a_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
        if t in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t == "title":
            self._in_title = True
        elif t == "meta":
            key = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content") or ""
            if key in {"description", "og:description", "og:title"} and content and key not in self.meta:
                self.meta[key] = tidy(content)
        elif t in self.BLOCK_TAGS:
            self._flush_block()
            self._block_tag = t
        elif t == "a":
            href = attr_map.get("href", "")
            if href:
                self._in_a = True
                self._a_href = urljoin(self.base_url, href)
                self._a_text = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t == "title":
            self._in_title = False
        elif t in self.BLOCK_TAGS:
            self._flush_block()
            self._block_tag = ""
        elif t == "a" and self._in_a:
            self._in_a = False
            label = tidy(" ".join(self._a_text))
            if label and self._a_href.startswith("https://") and len(self.links) < 25:
                if not any(x["url"] == self._a_href for x in self.links):
                    self.links.append({"label": label[:100], "url": self._a_href})

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_chunks.append(data)
            return
        clean = tidy(data)
        if not clean:
            return
        self._current_block.append(clean)
        if self._in_a:
            self._a_text.append(clean)

    def _flush_block(self):
        text = tidy(" ".join(self._current_block))
        self._current_block = []
        if not text:
            return
        if self._block_tag in {"h1", "h2", "h3", "h4"}:
            level = int(self._block_tag[1])
            self.blocks.append(f"{'#' * level} {text}")
        elif self._block_tag == "li":
            self.blocks.append(f"- {text}")
        else:
            self.blocks.append(text)

    def render(self, max_chars: int = 12000) -> dict:
        self._flush_block()
        title = tidy(" ".join(self.title_chunks)) or self.meta.get("og:title", "")
        content = "\n\n".join(self.blocks)
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(" ", 1)[0] + "\n\n...(网页内容已截断)"
        return {
            "title": title[:240],
            "description": self.meta.get("description") or self.meta.get("og:description") or "",
            "content": content,
            "links": self.links[:15],
        }


def _search_searxng(client: httpx.Client, base_url: str, query: str, focus: str, limit: int, timeout: float) -> list[dict]:
    cats = {"code": "it", "models": "it,science", "blogs": "general,it", "general": "general,it"}.get(focus, "general")
    resp = client.get(
        f"{base_url}/search",
        params={"q": query, "format": "json", "categories": cats},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for item in (data.get("results") or [])[:limit]:
        url = item.get("url") or ""
        if not url.startswith(("https://", "http://")):
            continue
        out.append({
            "title": tidy(item.get("title") or url),
            "url": url,
            "snippet": tidy(item.get("content") or "")[:360],
            "source": f"searxng:{item.get('engine') or 'web'}",
            "published_at": item.get("publishedDate"),
        })
    return out


def _search_duckduckgo(client: httpx.Client, query: str, limit: int, timeout: float) -> list[dict]:
    resp = client.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    parser = _DdgHtmlParser()
    parser.feed(resp.text)
    return parser.results[:limit]


def _search_github(client: httpx.Client, settings, query: str, limit: int, timeout: float) -> list[dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = settings.github_token.get_secret_value().strip() if getattr(settings, "github_token", None) else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.get(
        "https://api.github.com/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc", "per_page": min(limit, 10)},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("items") or []
    return [
        {
            "title": f"{it.get('full_name')} (★ {it.get('stargazers_count', 0)})",
            "url": it.get("html_url"),
            "snippet": tidy(it.get("description") or "")[:300],
            "source": "github",
            "language": it.get("language"),
            "updated_at": (it.get("updated_at") or "")[:10] or None,
        }
        for it in items
        if it.get("html_url")
    ]


def _search_huggingface(client: httpx.Client, settings, query: str, limit: int, timeout: float) -> list[dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    token = settings.hf_token.get_secret_value().strip() if getattr(settings, "hf_token", None) else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.get(
        "https://huggingface.co/api/models",
        params={"search": query, "sort": "downloads", "direction": "-1", "limit": min(limit, 8)},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    items = resp.json() or []
    return [
        {
            "title": f"HF Model: {it.get('id')} (↓ {it.get('downloads', 0)})",
            "url": f"https://huggingface.co/{it.get('id')}",
            "snippet": ", ".join((it.get("tags") or [])[:8]),
            "source": "huggingface",
        }
        for it in items
        if it.get("id")
    ]


def web_search(
    settings,
    query: str,
    *,
    focus: Literal["general", "code", "models", "blogs"] = "general",
    limit: int = 8,
    deadline: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Search non-paper web info (GitHub repos, Hugging Face models, technical blogs, benchmarks)."""
    clean_q = tidy(query)
    if not clean_q:
        raise ValueError("请提供有效检索词")
    deadline = deadline or (time.monotonic() + 18.0)
    limit = max(1, min(limit, 15))
    searx_url = safe_searxng_url(getattr(settings, "searxng_url", ""))
    coverage: dict[str, dict] = {}
    results: list[dict] = []
    seen_urls: set[str] = set()

    def _add_items(items: list[dict]):
        for it in items:
            u = (it.get("url") or "").strip()
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            results.append(it)

    if searx_url:
        try:
            with httpx.Client(transport=transport) as client:
                hits = _search_searxng(client, searx_url, clean_q, focus, limit, min(10.0, max(1.0, deadline - time.monotonic())))
            _add_items(hits)
            coverage["searxng"] = {"status": "ok", "count": len(hits)}
            return {
                "query": clean_q,
                "focus": focus,
                "provider": "searxng",
                "coverage": coverage,
                "total": len(results),
                "results": results[:limit],
            }
        except Exception as err:
            coverage["searxng"] = {"status": "error", "error": type(err).__name__}

    providers = ["duckduckgo"]
    if focus in {"code", "general"}:
        providers.append("github")
    if focus in {"models", "general"}:
        providers.append("huggingface")

    def _run_provider(prov: str):
        rem = max(1.5, min(9.0, deadline - time.monotonic()))
        try:
            with httpx.Client(transport=transport) as client:
                if prov == "duckduckgo":
                    return prov, {"status": "ok"}, _search_duckduckgo(client, clean_q, limit, rem)
                if prov == "github":
                    return prov, {"status": "ok"}, _search_github(client, settings, clean_q, min(5, limit), rem)
                return prov, {"status": "ok"}, _search_huggingface(client, settings, clean_q, min(4, limit), rem)
        except Exception as err:
            return prov, {"status": "error", "error": type(err).__name__}, []

    per_provider: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix="radar-web") as pool:
        for prov, cov, items in pool.map(_run_provider, providers):
            coverage[prov] = {**cov, "count": len(items)}
            per_provider[prov] = items

    # Interleave GitHub/HF highlights with web results based on focus
    order = (
        ("github", "duckduckgo", "huggingface")
        if focus == "code"
        else ("huggingface", "duckduckgo", "github")
        if focus == "models"
        else ("duckduckgo", "github", "huggingface")
    )
    for prov in order:
        _add_items(per_provider.get(prov, []))

    return {
        "query": clean_q,
        "focus": focus,
        "provider": "opensource_hybrid",
        "coverage": coverage,
        "total": len(results),
        "results": results[:limit],
    }


def read_webpage(
    settings,
    url: str,
    *,
    deadline: float | None = None,
    max_chars: int = 12000,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Fetch and extract readable content from a public HTTPS webpage (with SSRF protection)."""
    checked = safe_public_web_url(url)
    if not checked:
        raise ValueError("仅支持访问安全的公开 HTTPS 网页链接")
    deadline = deadline or (time.monotonic() + 18.0)

    with httpx.Client(transport=transport) as client:
        if getattr(settings, "jina_reader_enabled", False):
            try:
                jina_url = f"https://r.jina.ai/{checked}"
                resp = client.get(
                    jina_url,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/plain, text/markdown"},
                    timeout=min(12.0, max(1.0, deadline - time.monotonic())),
                )
                if resp.status_code == 200 and resp.text.strip():
                    text = resp.text.strip()[:max_chars]
                    return {
                        "url": checked,
                        "title": "",
                        "description": "",
                        "extractor": "jina_reader",
                        "content": text,
                        "links": [],
                        "char_count": len(text),
                    }
            except Exception:
                pass

        final_url, headers, raw = _request_with_redirects(
            client,
            checked,
            deadline,
            accept="text/html,application/xhtml+xml,text/plain,text/markdown",
            max_bytes=MAX_WEB_BYTES,
            url_validator=safe_public_web_url,
        )
    ctype = (headers.get("content-type") or "").lower()
    decoded = raw.decode("utf-8", errors="replace")
    if "html" not in ctype and "<html" not in decoded[:500].lower():
        text = decoded.strip()[:max_chars]
        return {
            "url": final_url,
            "title": "",
            "description": "",
            "extractor": "plain",
            "content": text,
            "links": [],
            "char_count": len(text),
        }
    parser = _WebpageParser(final_url)
    parser.feed(decoded)
    parsed = parser.render(max_chars=max_chars)
    return {
        "url": final_url,
        **parsed,
        "extractor": "html",
        "char_count": len(parsed["content"]),
    }
