from collections import defaultdict
from contextlib import closing, contextmanager, nullcontext
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from uuid import uuid4
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from filelock import FileLock
from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.pool import NullPool

from .models import Record, Topic, backfill_venues, dump, fingerprint, utc_now, venue_matches
from .runtime_paths import bundle_dir
from .sources import normalize_doi


def norm(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def rows(connection, sql, **params) -> list[dict]:
    return [dict(row) for row in connection.execute(text(sql), params).mappings()]


def row(connection, sql, **params) -> dict | None:
    result = connection.execute(text(sql), params).mappings().first()
    return dict(result) if result is not None else None


def write(connection, sql, **params):
    return connection.execute(text(sql), params)


def record_date(record: Record, zone: ZoneInfo) -> date | None:
    if record.preprint_at:
        return record.preprint_at.astimezone(zone).date()
    return record.published_on


def work_date(records: list[Record], zone: ZoneInfo) -> date | None:
    preprints = [record_date(item, zone) for item in records if item.preprint_at or item.status == "preprint"]
    return min((day for day in preprints if day), default=None) or min(
        (item.published_on for item in records if item.published_on), default=None)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "radar.sqlite3"
        self.worker_lock = FileLock(self.directory / "worker.lock", timeout=0)
        self.engine = create_engine(URL.create("sqlite", database=str(self.path)),
                                    connect_args={"timeout": 15}, poolclass=NullPool)
        event.listen(self.engine, "connect", self._prepare)

    @staticmethod
    def _prepare(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA busy_timeout = 15000")

    def initialize(self):
        with FileLock(self.directory / "migration.lock", timeout=30):
            with self.engine.connect() as con:
                con.exec_driver_sql("PRAGMA journal_mode = WAL")
            config = Config()
            migrations_dir = bundle_dir() / "research_radar" / "migrations"
            if not migrations_dir.exists():
                migrations_dir = Path(__file__).parent / "migrations"
            config.set_main_option("script_location", str(migrations_dir).replace("%", "%%"))
            config.attributes["engine"] = self.engine
            command.upgrade(config, "head")

    @contextmanager
    def transaction(self):
        with self.engine.connect() as con:
            con.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def one(self, sql, **params):
        with self.engine.connect() as con:
            return row(con, sql, **params)

    def all(self, sql, **params):
        with self.engine.connect() as con:
            return rows(con, sql, **params)

    def execute(self, sql, **params):
        with self.transaction() as con:
            return write(con, sql, **params).rowcount

    def topic(self, key: str) -> tuple[Topic, int]:
        saved = self.one("SELECT config, revision FROM topics WHERE id=:key", key=key)
        if not saved:
            raise ValueError("找不到主题")
        return Topic.model_validate_json(saved["config"]), saved["revision"]

    def topics(self) -> list[Topic]:
        return [Topic.model_validate_json(item["config"]) for item in self.all("SELECT config FROM topics ORDER BY id")]

    def save_topic(self, topic: Topic, expected_revision: int = 0, *, connection=None):
        config = topic.model_dump_json()
        with (self.transaction() if connection is None else nullcontext(connection)) as con:
            previous = row(con, "SELECT revision FROM topics WHERE id=:key", key=topic.id)
            if (previous["revision"] if previous else 0) != expected_revision:
                raise ValueError("主题已存在或刚刚被修改，请重新读取")
            write(con, """INSERT INTO topics VALUES (:key,:config,1)
                ON CONFLICT(id) DO UPDATE SET config=excluded.config, revision=topics.revision+1""",
                key=topic.id, config=config)
            write(con, "INSERT OR IGNORE INTO topic_queries VALUES (:key,:version,:config)",
                  key=topic.id, version=topic.query_version(), config=config)
            if topic.archived or not topic.enabled:
                write(con, """UPDATE runs SET status='cancelled',updated_at=:now
                    WHERE topic_id=:key AND status='queued' AND (:archive OR automatic)""",
                      key=topic.id, now=utc_now().isoformat(), archive=topic.archived)

    def enqueue(self, topic: Topic, now: datetime, kind: str = "daily", local_day: date | None = None,
                *, automatic: bool = False, connection=None) -> str:
        day = (local_day or now.astimezone(ZoneInfo(topic.timezone)).date()).isoformat()
        stamp = now.isoformat()
        with (self.transaction() if connection is None else nullcontext(connection)) as con:
            active = row(con, "SELECT config FROM topics WHERE id=:key", key=topic.id)
            current = Topic.model_validate_json(active["config"]).model_dump(mode="json") if active else None
            # Accept validated topic configurations from callers outside this module.
            if current != topic.model_dump(mode="json"):
                raise ValueError("主题刚刚被修改，请重新读取")
            if topic.archived or (automatic and not topic.enabled):
                raise ValueError("主题已删除或未开启自动检索")
            write(con, """INSERT OR IGNORE INTO runs
                (id,topic_id,local_day,kind,status,topic_snapshot,version,as_of,created_at,updated_at,automatic)
                VALUES (:id,:key,:day,:kind,'queued',:config,:version,:stamp,:stamp,:stamp,:automatic)""",
                id=uuid4().hex, key=topic.id, day=day, kind=kind, config=topic.model_dump_json(),
                version=topic.query_version(), stamp=stamp, automatic=automatic)
            existing = row(con, """SELECT * FROM runs WHERE topic_id=:key AND kind=:kind AND local_day=:day
                           AND (kind='daily' OR version=:version)""",
                           key=topic.id, kind=kind, day=day, version=topic.query_version())
            if existing["status"] == "cancelled":
                write(con, """UPDATE runs SET status='queued',topic_snapshot=:config,version=:version,
                    updated_at=:stamp,as_of=:stamp,automatic=:automatic WHERE id=:id""", id=existing["id"],
                      config=topic.model_dump_json(), version=topic.query_version(), stamp=stamp, automatic=automatic)
            elif not automatic and existing["automatic"] and existing["status"] == "queued":
                write(con, "UPDATE runs SET automatic=0 WHERE id=:id", id=existing["id"])
            return existing["id"]

    def retry(self, run_id: str, *, connection=None):
        with (self.transaction() if connection is None else nullcontext(connection)) as con:
            saved = row(con, """SELECT r.status,r.kind,t.config FROM runs r JOIN topics t ON r.topic_id=t.id
                WHERE r.id=:id""", id=run_id)
            if not saved:
                raise ValueError("找不到运行")
            topic = Topic.model_validate_json(saved["config"])
            if topic.archived:
                raise ValueError("主题已删除")
            if saved["status"] not in {"partial", "failed"}:
                raise ValueError("可重试的任务为失败或部分完成的任务")
            write(con, "UPDATE runs SET status='queued',error=NULL,automatic=0,updated_at=:now WHERE id=:id",
                  id=run_id, now=utc_now().isoformat())

    def claim(self, run_id: str | None = None):
        with self.transaction() as con:
            saved = row(con, """SELECT * FROM runs WHERE status IN ('queued','running')
                AND (:id IS NULL OR id=:id) ORDER BY status='running' DESC, kind='daily' DESC, created_at, rowid LIMIT 1""",
                id=run_id)
            if saved:
                write(con, "UPDATE runs SET status='running',updated_at=:now WHERE id=:id",
                      id=saved["id"], now=utc_now().isoformat())
            return saved

    def start_scan(self, run_id, source, stream, start, end):
        with self.transaction() as con:
            previous = row(con, """SELECT * FROM scans WHERE run_id=:run AND source=:source AND stream=:stream""",
                           run=run_id, source=source, stream=stream)
            if previous:
                return previous
            write(con, """INSERT INTO scans (run_id,source,stream,range_start,range_end,status)
                VALUES (:run,:source,:stream,:start,:end,'running')""",
                run=run_id, source=source, stream=stream, start=start.isoformat(), end=end.isoformat())
            return {"status": "running", "cursor": None, "range_start": start.isoformat(),
                    "range_end": end.isoformat(), "received": 0}

    def save_page(self, run, topic, source, stream, records, cursor, end):
        stamp = utc_now().isoformat()
        as_of = datetime.fromisoformat(run["as_of"])
        venues = backfill_venues(run["kind"])
        with self.transaction() as con:
            imported = 0
            for record in records:
                if stream != "versions" and record.preprint_at and record.preprint_at > as_of:
                    continue
                if venues and not venue_matches(record.publication, venues):
                    continue
                paper_id = self._ingest(con, record, stamp)
                content = norm(f"{record.title} {record.abstract or ''}")
                padded_content = f" {content} "
                if stream == "versions" or any(f" {norm(term)} " in padded_content for term in topic.excluded):
                    continue
                imported += self._associate(con, run, topic, paper_id, stream, stamp)
            if imported:
                write(con, """UPDATE runs SET result=json_set(coalesce(result,'{}'),'$.imported_count',
                    coalesce(json_extract(result,'$.imported_count'),0)+:count) WHERE id=:id""",
                      count=imported, id=run["id"])
            write(con, """UPDATE scans SET cursor=:cursor,pages=pages+1,received=received+:count,
                status=:status,detail=NULL WHERE run_id=:run AND source=:source AND stream=:stream""",
                run=run["id"], source=source, stream=stream, cursor=cursor, count=len(records),
                status="complete" if cursor is None else "running")
            if cursor is None and run["kind"] == "daily" and stream in {"main", "supplemental"}:
                watermark = min(str(end), run["local_day"])
                write(con, """INSERT INTO source_sync VALUES (:topic,:version,:source,:mark)
                    ON CONFLICT(topic_id,version,source) DO UPDATE SET watermark=max(watermark,excluded.watermark)""",
                    topic=topic.id, version=run["version"], source=source, mark=watermark)

    def _associate(self, con, run, topic, paper_id, stream, stamp):
        discovery = run["local_day"] if run["kind"] == "daily" or run["kind"] == "reconcile" else None
        created = write(con, """INSERT OR IGNORE INTO topic_papers
              (topic_id,paper_id,first_seen,discovered_on,discovery_version)
              VALUES (:topic,:paper,:stamp,:discovery,:version)""",
              topic=topic.id, paper=paper_id, stamp=stamp, discovery=discovery,
              version=run["version"] if discovery else None)
        if stream == "main" or stream.startswith("main:"):
            write(con, "INSERT OR IGNORE INTO corpus VALUES (:topic,:version,:paper)",
                  topic=topic.id, version=run["version"], paper=paper_id)
        return created.rowcount

    def _ingest(self, con, record: Record, stamp: str) -> int:
        title = norm(record.title)
        author = norm(record.authors[0]) if record.authors else ""
        year = record.preprint_at.year if record.preprint_at else (
            record.published_on.year if record.published_on else record.published_year)
        ids = [(kind, value) for kind, value in (
            (record.source, record.external_id), ("doi", normalize_doi(record.doi)), ("arxiv", record.arxiv_id)
        ) if value]
        exact = set()
        for kind, value in ids:
            saved = row(con, "SELECT paper_id FROM identifiers WHERE kind=:kind AND value=:value",
                        kind=kind, value=value)
            if saved:
                exact.add(saved["paper_id"])
        similar = rows(con, """SELECT id,author_norm,work_year FROM papers WHERE title_norm=:title
            AND merged_into IS NULL""", title=title)
        close = [item["id"] for item in similar if author and author == item["author_norm"] and year is not None
                 and item["work_year"] is not None and abs(year - item["work_year"]) <= 1]
        if exact:
            paper_id = min(exact)
            for other in exact - {paper_id}:
                self._merge(con, paper_id, other)
        elif len(close) == 1 and not self._conflicting_identifiers(con, close[0], ids):
            paper_id = close[0]
        else:
            paper_id = write(con, """INSERT INTO papers (title,title_norm,author_norm,work_year)
                VALUES (:name,:title,:author,:year)""", name=record.title, title=title,
                             author=author, year=year).lastrowid
            for item in similar:
                write(con, "INSERT OR IGNORE INTO possible_matches VALUES (:one,:two,:reason)",
                      one=min(paper_id, item["id"]), two=max(paper_id, item["id"]), reason="title_match")
        for kind, value in ids:
            write(con, "INSERT OR IGNORE INTO identifiers VALUES (:kind,:value,:paper)",
                  kind=kind, value=value, paper=paper_id)
        data = record.model_dump_json()
        ver = record.version or "metadata"
        optional = {"open_pdf_urls", "published_year", "published_month", "citation_count"}
        fields = record.model_dump(mode="json", exclude={name for name in optional if not getattr(record, name)} | {"citation_count"})
        write(con, "INSERT OR IGNORE INTO paper_versions VALUES (:src,:key,:ver,:hash,:record,:stamp)",
              src=record.source, key=record.external_id, ver=ver,
              hash=fingerprint(fields), record=data, stamp=stamp)
        existing = row(con, "SELECT record FROM paper_sources WHERE source=:src AND external_id=:key",
                       src=record.source, key=record.external_id)
        old = Record.model_validate_json(existing["record"]) if existing else None
        if old and old.updated_at and record.updated_at and old.updated_at > record.updated_at:
            data = existing["record"]
        write(con, """INSERT INTO paper_sources VALUES (:src,:key,:paper,:record,:stamp)
           ON CONFLICT(source,external_id) DO UPDATE SET paper_id=excluded.paper_id,
             record=excluded.record,fetched_at=excluded.fetched_at""",
              src=record.source, key=record.external_id, paper=paper_id, record=data, stamp=stamp)
        return paper_id

    @staticmethod
    def _conflicting_identifiers(con, paper_id, incoming):
        known = defaultdict(set)
        for item in rows(con, "SELECT kind,value FROM identifiers WHERE paper_id=:paper", paper=paper_id):
            if item["kind"] not in {"openalex", "openreview", "dblp", "acl", "crossref", "pmlr"}:
                known[item["kind"]].add(item["value"])
        return any(known[kind] and value not in known[kind] for kind, value in incoming)

    @staticmethod
    def _merge(con, canonical, other):
        for table in ("identifiers", "paper_sources", "analysis_cache"):
            write(con, f"UPDATE {table} SET paper_id=:keep WHERE paper_id=:old", keep=canonical, old=other)
        for item in rows(con, "SELECT * FROM topic_papers WHERE paper_id=:old", old=other):
            write(con, """INSERT INTO topic_papers VALUES (:topic,:keep,:seen,:found,:fav,:read,:reported,:version)
                ON CONFLICT(topic_id,paper_id) DO UPDATE SET
                    first_seen=min(first_seen,excluded.first_seen),
                    discovery_version=CASE
                        WHEN discovered_on IS NULL OR excluded.discovered_on<discovered_on
                        THEN excluded.discovery_version ELSE discovery_version END,
                    discovered_on=coalesce(min(discovered_on,excluded.discovered_on),discovered_on,excluded.discovered_on),
                    favorite=max(favorite,excluded.favorite),is_read=max(is_read,excluded.is_read),
                    reported_on=coalesce(min(reported_on,excluded.reported_on),reported_on,excluded.reported_on)""",
                  topic=item["topic_id"], keep=canonical, seen=item["first_seen"], found=item["discovered_on"],
                  fav=item["favorite"], read=item["is_read"], reported=item["reported_on"],
                  version=item["discovery_version"])
        write(con, """INSERT OR IGNORE INTO corpus SELECT topic_id,version,:keep FROM corpus WHERE paper_id=:old""",
              keep=canonical, old=other)
        write(con, "DELETE FROM corpus WHERE paper_id=:old", old=other)
        write(con, "DELETE FROM topic_papers WHERE paper_id=:old", old=other)
        write(con, "UPDATE papers SET merged_into=:keep WHERE id=:old OR merged_into=:old", keep=canonical, old=other)

    def papers(self, topic_id: str, corpus_version: str | None = None, discovered_through: str | None = None,
               discovery_version: str | None = None):
        filters = ["tp.topic_id=:topic"]
        if corpus_version:
            filters.append("""EXISTS (SELECT 1 FROM corpus c WHERE c.topic_id=tp.topic_id AND
                               c.paper_id=tp.paper_id AND c.version=:version)""")
        if discovered_through:
            filters.append("""(tp.reported_on IS NULL OR tp.reported_on=:day)
                           AND tp.discovered_on<=:day AND tp.discovered_on>=:since
                           AND tp.discovery_version=:discovery_version""")
        result = {}
        for saved in self.all(
            """SELECT tp.*,p.title,s.record FROM topic_papers tp JOIN papers p ON p.id=tp.paper_id
               JOIN paper_sources s ON s.paper_id=tp.paper_id WHERE """ + " AND ".join(filters)
            + " ORDER BY tp.first_seen DESC, tp.paper_id, s.source,s.external_id",
            topic=topic_id, version=corpus_version, day=discovered_through, discovery_version=discovery_version,
            since=str(date.fromisoformat(discovered_through) - timedelta(days=3)) if discovered_through else None,
        ):
            paper_id = saved["paper_id"]
            result.setdefault(paper_id, {key: value for key, value in saved.items() if key != "record"})
            result[paper_id].setdefault("records", []).append(Record.model_validate_json(saved["record"]))
        return list(result.values())

    def set_reading_state(self, topic_id, paper_id, favorite, is_read, *, connection=None):
        with (self.transaction() if connection is None else nullcontext(connection)) as con:
            count = write(con, """UPDATE topic_papers SET favorite=coalesce(:fav,favorite),
                is_read=coalesce(:read,is_read) WHERE topic_id=:topic AND paper_id=:paper""",
                topic=topic_id, paper=paper_id, fav=favorite, read=is_read).rowcount
            if not count:
                raise ValueError("当前主题没有这篇论文")

    def finish(self, run, status, result, markdown=None, notification=None):
        with self.transaction() as con:
            previous = row(con, "SELECT result FROM runs WHERE id=:id", id=run["id"])
            imported = json.loads(previous["result"] or "{}").get("imported_count")
            if imported is not None:
                result = {**result, "imported_count": imported}
            if markdown is not None:
                relative = Path("reports") / run["topic_id"] / (run["local_day"] + ".md")
                self._write_report(con, run["id"], relative.as_posix(), markdown)
                for paper in result.get("recommendations", []):
                    write(con, """UPDATE topic_papers SET reported_on=:day WHERE topic_id=:topic AND paper_id=:paper""",
                          day=run["local_day"], topic=run["topic_id"], paper=paper["paper_id"])
                if notification is not None:
                    write(con, """INSERT INTO notifications
                        (run_id,channel,status,next_at,payload) VALUES (:id,'feishu','pending',:now,:body)
                        ON CONFLICT(run_id,channel) DO UPDATE SET payload=excluded.payload
                        WHERE notifications.status='pending' AND notifications.attempts=0""",
                        id=run["id"], now=utc_now().isoformat(), body=notification)
            write(con, "UPDATE runs SET status=:status,result=:result,error=:error,updated_at=:now WHERE id=:id",
                  id=run["id"], status=status, result=dump(result), error=result.get("error"), now=utc_now().isoformat())

    def _write_report(self, con, run_id, relative, markdown):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(markdown, encoding="utf-8")
        temporary.replace(path)
        write(con, """INSERT INTO reports VALUES (:id,:path,:md)
            ON CONFLICT(run_id) DO UPDATE SET markdown=excluded.markdown""",
              id=run_id, path=relative, md=markdown)

    def update_report_publications(self, run_id, relative, result, markdown):
        with self.transaction() as con:
            self._write_report(con, run_id, relative, markdown)
            write(con, "UPDATE runs SET result=:result WHERE id=:id", id=run_id, result=dump(result))

    def backup(self, destination: Path):
        target = destination.resolve()
        if target == self.path or target.exists():
            raise ValueError("备份目标必须是新文件")
        with self.worker_lock:
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(target)) as copy:
                source.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("备份校验失败")

    def restore_markdown(self):
        for report in self.all("SELECT path,markdown FROM reports"):
            path = self.directory / report["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report["markdown"], encoding="utf-8")
