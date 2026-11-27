"""Full-text PDF section chunking and SQLite FTS5 + BGE hybrid RAG retrieval."""

import json
import re
from uuid import uuid4

from .models import Record, Settings, dump, fingerprint, utc_now
from .ranking import MODEL, Ranker
from .store import Store, norm, row, write

PARSER_VERSION = "pypdf-chunk-v1"
SECTION_HEADING = re.compile(
    r"(?:^|\s)(?:\d+(?:\.\d+)?\.?\s+)?(Abstract|Introduction|Related Work|Background|Method|Methodology|"
    r"Approach|Experiments?|Evaluation|Results?|Discussion|Limitations?|Conclusion|Future Work)\b",
    re.I,
)


def _detect_section(text: str, current: str) -> str:
    match = SECTION_HEADING.search(text[:240])
    return match[1].strip().title() if match else current


def chunk_pages(pages: list[str], *, max_chars: int = 2200, overlap: int = 240) -> list[dict]:
    """Split extracted PDF pages into overlapping section-aware chunks."""
    chunks = []
    section = "Body"
    buffer = ""
    page_start = 1
    page_end = 1
    for page_num, raw in enumerate(pages, start=1):
        clean = " ".join((raw or "").split())
        if not clean:
            continue
        section = _detect_section(clean, section)
        if not buffer:
            page_start = page_num
        buffer = f"{buffer} {clean}".strip() if buffer else clean
        page_end = page_num
        while len(buffer) >= max_chars:
            cut = buffer.rfind(" ", overlap, max_chars)
            if cut <= overlap:
                cut = max_chars
            piece = buffer[:cut].strip()
            chunks.append({
                "chunk_index": len(chunks),
                "section": section,
                "page_start": page_start,
                "page_end": page_end,
                "text": piece,
            })
            tail = buffer[max(0, cut - overlap):].strip()
            buffer = tail
            page_start = page_num
    if buffer.strip():
        chunks.append({
            "chunk_index": len(chunks),
            "section": section,
            "page_start": page_start,
            "page_end": page_end,
            "text": buffer.strip(),
        })
    return chunks


def index_chunk_records(store: Store, settings: Settings, paper_id: int, source_url: str,
                        pages: list[str], content_hash: str) -> dict:
    """Persist chunked PDF pages into SQLite + FTS5 and compute BGE embeddings when available."""
    existing = store.one(
        "SELECT content_hash, parser_version, page_count FROM paper_documents WHERE paper_id=:pid",
        pid=paper_id,
    )
    if existing and existing["content_hash"] == content_hash and existing["parser_version"] == PARSER_VERSION:
        count = store.one("SELECT COUNT(*) AS n FROM paper_chunks WHERE paper_id=:pid", pid=paper_id)["n"]
        return {"paper_id": paper_id, "chunks": count, "cached": True, "url": source_url}

    chunks = chunk_pages(pages)
    if not chunks:
        raise ValueError("PDF 未提取出可索引文本")
    keys: list[str | None] = [None] * len(chunks)
    if not settings.keyword_only:
        try:
            ranker = Ranker(store, settings)
            texts = [chunk["text"] for chunk in chunks]
            ranker.encode_texts(texts)
            keys = [fingerprint([MODEL, settings.embedding_revision, t]) for t in texts]
        except (ImportError, OSError, RuntimeError, ValueError):
            keys = [None] * len(chunks)

    now = utc_now().isoformat()
    with store.transaction() as con:
        write(con, "DELETE FROM paper_chunks_fts WHERE paper_id=:pid", pid=paper_id)
        write(con, "DELETE FROM paper_chunks WHERE paper_id=:pid", pid=paper_id)
        write(
            con,
            """INSERT INTO paper_documents(paper_id, source_url, content_hash, parser_version, page_count, status, parsed_at)
               VALUES (:pid, :url, :hash, :ver, :pages, 'indexed', :now)
               ON CONFLICT(paper_id) DO UPDATE SET source_url=excluded.source_url,
               content_hash=excluded.content_hash, parser_version=excluded.parser_version,
               page_count=excluded.page_count, status='indexed', parsed_at=excluded.parsed_at""",
            pid=paper_id, url=source_url, hash=content_hash, ver=PARSER_VERSION, pages=len(pages), now=now,
        )
        for chunk, emb_key in zip(chunks, keys):
            cid = f"C{paper_id}_{chunk['chunk_index']}_{uuid4().hex[:8]}"
            write(
                con,
                """INSERT INTO paper_chunks(id, paper_id, chunk_index, section, page_start, page_end, text, embedding_key)
                   VALUES (:id, :pid, :idx, :sec, :ps, :pe, :text, :ekey)""",
                id=cid, pid=paper_id, idx=chunk["chunk_index"], sec=chunk["section"],
                ps=chunk["page_start"], pe=chunk["page_end"], text=chunk["text"], ekey=emb_key,
            )
            write(
                con,
                "INSERT INTO paper_chunks_fts(chunk_id, paper_id, section, text) VALUES (:cid, :pid, :sec, :text)",
                cid=cid, pid=paper_id, sec=chunk["section"], text=chunk["text"],
            )
    return {"paper_id": paper_id, "chunks": len(chunks), "cached": False, "url": source_url}


def index_paper_pdf(store: Store, settings: Settings, client, paper_id: int,
                    records: list[Record], deadline: float) -> dict:
    """Explore multiple open PDF sources for a paper, parse transiently, and index sections into hybrid RAG."""
    from .pdf_explorer import (
        cached_pdf_urls, explore_pdf_sources, mark_pdf_unavailable_if_confirmed, save_pdf_check,
    )

    preferred = cached_pdf_urls(store, paper_id)
    explored = explore_pdf_sources(
        client, settings, records=records, preferred_urls=preferred, deadline=deadline,
    )
    if not explored.get("found"):
        reason = explored.get("reason", "没有来自受支持学术站点的开放 PDF")
        mark_pdf_unavailable_if_confirmed(store, paper_id, explored, fallback_reason=reason)
        raise ValueError(reason)
    save_pdf_check(store, paper_id, "available", pdf_url=explored["pdf_url"], strategy=explored.get("strategy"))
    res = index_chunk_records(
        store, settings, paper_id, explored["pdf_url"], explored["pages"], explored["content_hash"],
    )
    return {**res, "strategy": explored.get("strategy"), "attempts": explored.get("attempts", [])}


def _fts_query(query: str) -> str:
    tokens = [tok for tok in norm(query).split() if len(tok) >= 2]
    return " OR ".join(f'"{tok}"' for tok in dict.fromkeys(tokens[:12]))


def hybrid_search(store: Store, settings: Settings, query: str, *, topic_id: str | None = None,
                  paper_ids: list[int] | None = None, limit: int = 8) -> list[dict]:
    """Hybrid FTS5 BM25 + BGE cosine RRF retrieval over indexed paper chunks."""
    if not query.strip():
        return []
    scope_filters = []
    params: dict = {}
    if topic_id:
        scope_filters.append("EXISTS (SELECT 1 FROM topic_papers tp WHERE tp.topic_id=:topic AND tp.paper_id=c.paper_id)")
        params["topic"] = topic_id
    if paper_ids:
        placeholders = ", ".join(f":p{i}" for i in range(len(paper_ids)))
        scope_filters.append(f"c.paper_id IN ({placeholders})")
        for i, pid in enumerate(paper_ids):
            params[f"p{i}"] = pid
    where_sql = ("WHERE " + " AND ".join(scope_filters)) if scope_filters else ""

    fts_expr = _fts_query(query)
    fts_ranks: dict[str, int] = {}
    if fts_expr:
        fts_rows = store.all(
            f"""SELECT f.chunk_id, bm25(paper_chunks_fts) AS rank
                FROM paper_chunks_fts f
                JOIN paper_chunks c ON c.id=f.chunk_id
                {"WHERE " + " AND ".join(["paper_chunks_fts MATCH :expr", *scope_filters]) if scope_filters else "WHERE paper_chunks_fts MATCH :expr"}
                ORDER BY rank LIMIT 60""",
            expr=fts_expr, **params,
        )
        fts_ranks = {item["chunk_id"]: idx + 1 for idx, item in enumerate(fts_rows)}

    all_chunks = store.all(
        f"""SELECT c.*, p.title, d.source_url
            FROM paper_chunks c
            JOIN papers p ON p.id=c.paper_id
            LEFT JOIN paper_documents d ON d.paper_id=c.paper_id
            {where_sql}
            ORDER BY c.paper_id, c.chunk_index LIMIT 400""",
        **params,
    )
    if not all_chunks:
        return []

    vec_scores: dict[str, float] = {}
    if not settings.keyword_only:
        try:
            ranker = Ranker(store, settings)
            q_vec = ranker.encode_texts(["Represent this sentence for searching relevant passages: " + query])[0]
            missing = [c for c in all_chunks if not c["embedding_key"]]
            if missing:
                ranker.encode_texts([c["text"] for c in missing])
            for c in all_chunks:
                key = c["embedding_key"] or fingerprint([MODEL, settings.embedding_revision, c["text"]])
                saved = store.one("SELECT vector FROM embeddings WHERE cache_key=:key", key=key)
                if saved:
                    vec = json.loads(saved["vector"])
                    vec_scores[c["id"]] = max(0.0, sum(a * b for a, b in zip(q_vec, vec)))
        except (ImportError, OSError, RuntimeError, ValueError):
            vec_scores = {}

    if not vec_scores:
        q_tokens = set(norm(query).split())
        for c in all_chunks:
            c_tokens = set(norm(c["text"]).split())
            vec_scores[c["id"]] = len(q_tokens & c_tokens) / max(1, len(q_tokens))

    vec_ordered = sorted(all_chunks, key=lambda c: vec_scores.get(c["id"], 0.0), reverse=True)
    vec_ranks = {c["id"]: idx + 1 for idx, c in enumerate(vec_ordered)}

    scored = []
    for c in all_chunks:
        cid = c["id"]
        if cid not in fts_ranks and vec_scores.get(cid, 0.0) <= 0.0:
            continue
        rrf = (1.0 / (60 + fts_ranks[cid]) if cid in fts_ranks else 0.0) + (1.0 / (60 + vec_ranks[cid]))
        url = c["source_url"] or ""
        if not url:
            src = store.one("SELECT record FROM paper_sources WHERE paper_id=:pid LIMIT 1", pid=c["paper_id"])
            url = json.loads(src["record"])["url"] if src else ""
        scored.append({
            "chunk_id": cid,
            "paper_id": c["paper_id"],
            "title": c["title"],
            "section": c["section"],
            "page_start": c["page_start"],
            "page_end": c["page_end"],
            "text": c["text"],
            "url": f"{url}#page={c['page_start']}" if url and c["page_start"] else url,
            "score": round(rrf, 6),
        })
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def rebuild_rag_index(store: Store, settings: Settings) -> dict:
    """Rebuild FTS5 index and missing chunk embeddings from persisted paper_chunks."""
    chunks = store.all("SELECT id, paper_id, section, text, embedding_key FROM paper_chunks ORDER BY paper_id, chunk_index")
    with store.transaction() as con:
        write(con, "DELETE FROM paper_chunks_fts")
        for c in chunks:
            write(con, "INSERT INTO paper_chunks_fts(chunk_id, paper_id, section, text) VALUES (:cid, :pid, :sec, :text)",
                  cid=c["id"], pid=c["paper_id"], sec=c["section"], text=c["text"])
    encoded = 0
    if not settings.keyword_only and chunks:
        try:
            ranker = Ranker(store, settings)
            texts = [c["text"] for c in chunks]
            ranker.encode_texts(texts)
            encoded = len(texts)
        except (ImportError, OSError, RuntimeError, ValueError):
            encoded = 0
    return {"chunks_reindexed": len(chunks), "embeddings_warmed": encoded}
