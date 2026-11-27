from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE TABLE topics (
        id TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL
    )""",
    """CREATE TABLE topic_queries (
        topic_id TEXT NOT NULL REFERENCES topics(id), version TEXT NOT NULL,
        config TEXT NOT NULL, PRIMARY KEY (topic_id, version)
    )""",
    """CREATE TABLE papers (
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, title_norm TEXT NOT NULL,
        author_norm TEXT NOT NULL, work_year INTEGER, merged_into INTEGER REFERENCES papers(id)
    )""",
    "CREATE INDEX paper_match ON papers(title_norm, author_norm, work_year)",
    """CREATE TABLE identifiers (
        kind TEXT NOT NULL, value TEXT NOT NULL, paper_id INTEGER NOT NULL REFERENCES papers(id),
        PRIMARY KEY (kind, value)
    )""",
    """CREATE TABLE paper_sources (
        source TEXT NOT NULL, external_id TEXT NOT NULL, paper_id INTEGER NOT NULL REFERENCES papers(id),
        record TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (source, external_id)
    )""",
    "CREATE INDEX sources_by_paper ON paper_sources(paper_id)",
    """CREATE TABLE paper_versions (
        source TEXT NOT NULL, external_id TEXT NOT NULL, version TEXT NOT NULL,
        content_hash TEXT NOT NULL, record TEXT NOT NULL, first_seen TEXT NOT NULL,
        PRIMARY KEY(source, external_id, version, content_hash)
    )""",
    """CREATE TABLE possible_matches (
        paper_id INTEGER NOT NULL REFERENCES papers(id), other_id INTEGER NOT NULL REFERENCES papers(id),
        reason TEXT NOT NULL, PRIMARY KEY (paper_id, other_id)
    )""",
    """CREATE TABLE topic_papers (
        topic_id TEXT NOT NULL REFERENCES topics(id), paper_id INTEGER NOT NULL REFERENCES papers(id),
        first_seen TEXT NOT NULL, discovered_on TEXT, favorite INTEGER NOT NULL DEFAULT 0,
        is_read INTEGER NOT NULL DEFAULT 0, reported_on TEXT, discovery_version TEXT,
        PRIMARY KEY (topic_id, paper_id)
    )""",
    """CREATE TABLE corpus (
        topic_id TEXT NOT NULL, version TEXT NOT NULL, paper_id INTEGER NOT NULL REFERENCES papers(id),
        PRIMARY KEY (topic_id, version, paper_id),
        FOREIGN KEY (topic_id, version) REFERENCES topic_queries(topic_id, version)
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id), local_day TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, topic_snapshot TEXT NOT NULL, version TEXT NOT NULL,
        as_of TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        error TEXT, result TEXT, usage TEXT NOT NULL DEFAULT '{}',
        UNIQUE (topic_id, kind, local_day, version)
    )""",
    "CREATE UNIQUE INDEX daily_once ON runs(topic_id, local_day) WHERE kind='daily'",
    """CREATE TABLE scans (
        run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL, stream TEXT NOT NULL,
        range_start TEXT NOT NULL, range_end TEXT NOT NULL, cursor TEXT,
        pages INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL, detail TEXT, PRIMARY KEY (run_id, source, stream)
    )""",
    """CREATE TABLE source_sync (
        topic_id TEXT NOT NULL, version TEXT NOT NULL, source TEXT NOT NULL, watermark TEXT NOT NULL,
        PRIMARY KEY (topic_id, version, source)
    )""",
    """CREATE TABLE analysis_cache (
        cache_key TEXT PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id),
        analysis TEXT NOT NULL, evidence TEXT NOT NULL
    )""",
    """CREATE TABLE embeddings (
        cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL
    )""",
    """CREATE TABLE trends (
        topic_id TEXT NOT NULL, version TEXT NOT NULL, local_day TEXT NOT NULL, snapshot TEXT NOT NULL,
        PRIMARY KEY(topic_id, version, local_day)
    )""",
    """CREATE TABLE reports (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), path TEXT NOT NULL, markdown TEXT NOT NULL
    )""",
    """CREATE TABLE notifications (
        run_id TEXT NOT NULL REFERENCES reports(run_id), channel TEXT NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_at TEXT NOT NULL, detail TEXT, payload TEXT NOT NULL,
        PRIMARY KEY (run_id, channel)
    )""",
)


def upgrade():
    for sql in SCHEMA:
        op.execute(sql)


def downgrade():
    for table in ("notifications", "reports", "trends", "embeddings", "analysis_cache",
                  "source_sync", "scans", "runs", "corpus", "topic_papers", "possible_matches",
                  "paper_versions", "paper_sources", "identifiers", "papers", "topic_queries", "topics"):
        op.drop_table(table)
