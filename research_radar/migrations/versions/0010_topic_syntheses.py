"""Add persistent per-topic cross-paper AI synthesis table and optional report synthesis column."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """CREATE TABLE topic_syntheses (
            id TEXT PRIMARY KEY,
            topic_id TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            cache_key TEXT NOT NULL,
            result_json TEXT NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    op.execute("CREATE INDEX topic_syntheses_by_topic ON topic_syntheses(topic_id, window_days, created_at)")
    op.execute("CREATE UNIQUE INDEX topic_syntheses_cache_key ON topic_syntheses(cache_key)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS topic_syntheses_cache_key")
    op.execute("DROP INDEX IF EXISTS topic_syntheses_by_topic")
    op.execute("DROP TABLE IF EXISTS topic_syntheses")
