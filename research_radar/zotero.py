"""Zotero Web API v3 synchronization for pulling collections and pushing favorites."""

from datetime import date
import re

import httpx

from .models import Publication, Record, Settings, utc_now
from .ranking import best_record
from .sources import arxiv_identifier, normalize_doi
from .store import Store, row, write

ZOTERO_API = "https://api.zotero.org"


def _zotero_headers(settings: Settings) -> dict[str, str]:
    key = settings.zotero_api_key.get_secret_value()
    user_id = settings.zotero_user_id.strip()
    if not key or not user_id:
        raise ValueError("请先配置 ZOTERO_API_KEY 与 ZOTERO_USER_ID")
    if not re.fullmatch(r"\d+", user_id):
        raise ValueError("ZOTERO_USER_ID 格式无效")
    return {"Zotero-API-Key": key, "Zotero-API-Version": "3"}


def _parse_zotero_item(item: dict) -> Record | None:
    data = item.get("data") or {}
    item_type = data.get("itemType")
    if item_type in {"attachment", "note"}:
        return None
    title = " ".join((data.get("title") or "").split())
    key = item.get("key")
    if not title or not key:
        return None
    doi = normalize_doi(data.get("DOI"))
    url = data.get("url") or ""
    arxiv_id, _ = arxiv_identifier(url)
    if not arxiv_id and data.get("extra"):
        m = re.search(r"arXiv:\s*(\S+)", data["extra"], re.I)
        if m:
            arxiv_id, _ = arxiv_identifier(m[1])
    if not isinstance(url, str) or not url.startswith("https://"):
        url = f"https://doi.org/{doi}" if doi else f"https://www.zotero.org/items/{key}"
    authors = [
        " ".join(filter(None, (c.get("firstName"), c.get("lastName")))) or c.get("name", "")
        for c in (data.get("creators") or [])
    ]
    pub_on = None
    pub_year = None
    raw_date = str(data.get("date") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        pub_on = date.fromisoformat(raw_date)
    elif m := re.search(r"\b(19|20)\d{2}\b", raw_date):
        pub_year = int(m[0])
    venue = data.get("proceedingsTitle") or data.get("publicationTitle") or None
    return Record(
        source="crossref" if doi else "openalex",
        external_id=f"zotero:{key}",
        title=title,
        url=url,
        authors=[a for a in authors if a],
        abstract=" ".join((data.get("abstractNote") or "").split()) or None,
        doi=doi,
        arxiv_id=arxiv_id,
        published_on=pub_on,
        published_year=pub_year,
        status="published" if venue else "unknown",
        publication=Publication(status="published" if venue else "unknown", venue=venue),
    )


def pull_zotero_collection(store: Store, settings: Settings, topic_id: str,
                           *, collection_key: str = "", client: httpx.Client | None = None) -> dict:
    """Incrementally import items from a Zotero user library or collection into a topic."""
    headers = _zotero_headers(settings)
    if not store.one("SELECT id FROM topics WHERE id=:id", id=topic_id):
        raise ValueError("找不到主题")
    sync_state = store.one("SELECT collection_key, last_version FROM zotero_sync WHERE topic_id=:t", t=topic_id)
    last_ver = sync_state["last_version"] if sync_state else 0
    target_col = collection_key.strip() or (sync_state["collection_key"] if sync_state else "")
    if target_col and not re.fullmatch(r"[A-Za-z0-9]{4,16}", target_col):
        raise ValueError("Zotero Collection Key 格式无效")
    uid = settings.zotero_user_id.strip()
    path = f"{ZOTERO_API}/users/{uid}/collections/{target_col}/items" if target_col else f"{ZOTERO_API}/users/{uid}/items"

    own = client is None
    client = client or httpx.Client()
    try:
        req_headers = {**headers, **({"If-Modified-Since-Version": str(last_ver)} if last_ver else {})}
        resp = client.get(path, params={"format": "json", "limit": 50}, headers=req_headers, timeout=15)
        if resp.status_code == 304:
            return {"imported": 0, "version": last_ver, "unchanged": True}
        resp.raise_for_status()
        items = resp.json()
        new_ver = int(resp.headers.get("Last-Modified-Version", last_ver) or last_ver)
    finally:
        if own:
            client.close()

    now = utc_now().isoformat()
    imported = 0
    with store.transaction() as con:
        for item in items:
            rec = _parse_zotero_item(item)
            if not rec:
                continue
            pid = store._ingest(con, rec, now)
            res = write(
                con,
                """INSERT OR IGNORE INTO topic_papers(topic_id, paper_id, first_seen, discovered_on, favorite, is_read, discovery_version)
                   VALUES (:topic, :pid, :now, NULL, 1, 0, NULL)""",
                topic=topic_id, pid=pid, now=now,
            )
            imported += res.rowcount
            write(
                con,
                """INSERT INTO zotero_items(item_key, paper_id, version, updated_at)
                   VALUES (:key, :pid, :ver, :now)
                   ON CONFLICT(item_key) DO UPDATE SET paper_id=excluded.paper_id, version=excluded.version, updated_at=excluded.updated_at""",
                key=item["key"], pid=pid, ver=int(item.get("version") or new_ver), now=now,
            )
        write(
            con,
            """INSERT INTO zotero_sync(topic_id, collection_key, last_version, updated_at)
               VALUES (:topic, :col, :ver, :now)
               ON CONFLICT(topic_id) DO UPDATE SET collection_key=excluded.collection_key,
               last_version=excluded.last_version, updated_at=excluded.updated_at""",
            topic=topic_id, col=target_col, ver=new_ver, now=now,
        )
    return {"imported": imported, "version": new_ver, "unchanged": False}


def push_favorites_to_zotero(store: Store, settings: Settings, topic_id: str,
                             *, collection_key: str = "", client: httpx.Client | None = None) -> dict:
    """Export favorited topic papers not yet linked in zotero_items to Zotero."""
    headers = _zotero_headers(settings)
    uid = settings.zotero_user_id.strip()
    target_col = collection_key.strip()
    if target_col and not re.fullmatch(r"[A-Za-z0-9]{4,16}", target_col):
        raise ValueError("Zotero Collection Key 格式无效")
    favorites = [p for p in store.papers(topic_id) if p["favorite"]]
    mapped_pids = {r["paper_id"] for r in store.all("SELECT paper_id FROM zotero_items")}
    to_push = [p for p in favorites if p["paper_id"] not in mapped_pids][:25]
    if not to_push:
        return {"exported": 0}

    payload = []
    for p in to_push:
        rec = best_record(p)
        entry = {
            "itemType": "journalArticle",
            "title": rec.title,
            "url": rec.url,
            "abstractNote": rec.abstract or "",
            "DOI": rec.doi or "",
            "creators": [{"creatorType": "author", "name": name} for name in rec.authors[:10]],
        }
        if target_col:
            entry["collections"] = [target_col]
        payload.append(entry)

    own = client is None
    client = client or httpx.Client()
    try:
        resp = client.post(f"{ZOTERO_API}/users/{uid}/items", headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        body = resp.json()
    finally:
        if own:
            client.close()

    successful = body.get("successful") or {}
    now = utc_now().isoformat()
    exported = 0
    with store.transaction() as con:
        for idx_str, created in successful.items():
            idx = int(idx_str)
            if 0 <= idx < len(to_push):
                write(
                    con,
                    """INSERT OR IGNORE INTO zotero_items(item_key, paper_id, version, updated_at)
                       VALUES (:key, :pid, :ver, :now)""",
                    key=created["key"], pid=to_push[idx]["paper_id"], ver=int(created.get("version") or 0), now=now,
                )
                exported += 1
    return {"exported": exported}
