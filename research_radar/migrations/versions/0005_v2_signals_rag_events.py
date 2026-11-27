"""Add V2 tables for feedback, full-text RAG, deep reading, dynamic clusters, events, and period reports."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE TABLE user_feedback (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES topics(id),
        paper_id INTEGER NOT NULL REFERENCES papers(id),
        action TEXT NOT NULL,
        rating INTEGER,
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX feedback_by_topic ON user_feedback(topic_id, paper_id, created_at)",
    """CREATE TABLE topic_preferences (
        topic_id TEXT PRIMARY KEY REFERENCES topics(id),
        profile_json TEXT NOT NULL,
        frozen INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE paper_documents (
        paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
        source_url TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        page_count INTEGER NOT NULL,
        status TEXT NOT NULL,
        parsed_at TEXT NOT NULL
    )""",
    """CREATE TABLE paper_chunks (
        id TEXT PRIMARY KEY,
        paper_id INTEGER NOT NULL REFERENCES papers(id),
        chunk_index INTEGER NOT NULL,
        section TEXT NOT NULL,
        page_start INTEGER NOT NULL,
        page_end INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding_key TEXT
    )""",
    "CREATE INDEX chunks_by_paper ON paper_chunks(paper_id, chunk_index)",
    """CREATE VIRTUAL TABLE paper_chunks_fts USING fts5(
        chunk_id UNINDEXED,
        paper_id UNINDEXED,
        section,
        text,
        tokenize='unicode61'
    )""",
    """CREATE TABLE deep_analyses (
        cache_key TEXT PRIMARY KEY,
        paper_id INTEGER NOT NULL REFERENCES papers(id),
        topic_id TEXT NOT NULL REFERENCES topics(id),
        level TEXT NOT NULL,
        result_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX deep_analyses_by_paper ON deep_analyses(topic_id, paper_id, level)",
    """CREATE TABLE dynamic_clusters (
        topic_id TEXT NOT NULL REFERENCES topics(id),
        version TEXT NOT NULL,
        as_of TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        label TEXT NOT NULL,
        terms_json TEXT NOT NULL,
        centroid_json TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        event_type TEXT NOT NULL,
        representative_ids_json TEXT NOT NULL,
        PRIMARY KEY (topic_id, version, as_of, cluster_id)
    )""",
    """CREATE TABLE research_events (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES topics(id),
        paper_id INTEGER REFERENCES papers(id),
        event_type TEXT NOT NULL,
        event_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        notified_at TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX events_by_topic ON research_events(topic_id, event_type, created_at)",
    """CREATE TABLE period_reports (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES topics(id),
        report_type TEXT NOT NULL,
        period_key TEXT NOT NULL,
        path TEXT NOT NULL,
        markdown TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE (topic_id, report_type, period_key)
    )""",
    """CREATE TABLE event_notifications (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES topics(id),
        channel TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at TEXT NOT NULL,
        detail TEXT,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
)


def upgrade():
    for sql in SCHEMA:
        op.execute(sql)


def downgrade():
    for table in (
        "event_notifications", "period_reports", "research_events", "dynamic_clusters", "deep_analyses",
        "paper_chunks_fts", "paper_chunks", "paper_documents", "topic_preferences", "user_feedback",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
