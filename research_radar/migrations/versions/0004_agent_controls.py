"""Add task events, research adjustments and versioned project memory."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE task_events (
        seq INTEGER PRIMARY KEY, scope TEXT NOT NULL, target TEXT NOT NULL, stage TEXT NOT NULL,
        status TEXT NOT NULL, detail TEXT NOT NULL, elapsed_ms INTEGER, created_at TEXT NOT NULL
    )""")
    op.execute("CREATE INDEX task_events_by_target ON task_events(scope,target,seq)")
    op.execute("""CREATE TABLE research_adjustments (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id),
        kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, applied_at TEXT, detail TEXT
    )""")
    op.execute("CREATE INDEX adjustments_by_job ON research_adjustments(job_id,status,created_at)")
    op.execute("""CREATE TABLE project_memories (
        topic_id TEXT NOT NULL REFERENCES topics(id), revision INTEGER NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(topic_id,revision)
    )""")


def downgrade():
    for table in ("project_memories", "research_adjustments", "task_events"):
        op.drop_table(table)
