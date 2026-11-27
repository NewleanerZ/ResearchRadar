"""Add V3 tables for research projects, gap analyses, and Zotero sync."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE TABLE research_projects (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL REFERENCES topics(id),
        name TEXT NOT NULL,
        question TEXT NOT NULL,
        current_method TEXT NOT NULL DEFAULT '',
        datasets_json TEXT NOT NULL DEFAULT '[]',
        metrics_json TEXT NOT NULL DEFAULT '[]',
        experiments_json TEXT NOT NULL DEFAULT '[]',
        expected_contributions TEXT NOT NULL DEFAULT '',
        representative_paper_ids_json TEXT NOT NULL DEFAULT '[]',
        revision INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX projects_by_topic ON research_projects(topic_id, updated_at)",
    """CREATE TABLE project_paper_links (
        project_id TEXT NOT NULL REFERENCES research_projects(id),
        paper_id INTEGER NOT NULL REFERENCES papers(id),
        relation_type TEXT NOT NULL,
        score REAL NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (project_id, paper_id)
    )""",
    """CREATE TABLE gap_analyses (
        id TEXT PRIMARY KEY,
        project_id TEXT REFERENCES research_projects(id),
        topic_id TEXT NOT NULL REFERENCES topics(id),
        status TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        state_json TEXT NOT NULL DEFAULT '{}',
        markdown TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE zotero_sync (
        topic_id TEXT PRIMARY KEY REFERENCES topics(id),
        collection_key TEXT NOT NULL DEFAULT '',
        last_version INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE zotero_items (
        item_key TEXT PRIMARY KEY,
        paper_id INTEGER NOT NULL REFERENCES papers(id),
        version INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
)


def upgrade():
    for sql in SCHEMA:
        op.execute(sql)


def downgrade():
    for table in ("zotero_items", "zotero_sync", "gap_analyses", "project_paper_links", "research_projects"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
