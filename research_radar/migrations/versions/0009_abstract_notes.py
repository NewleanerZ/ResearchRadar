"""Add persistent per-paper abstract translation and LLM summary cache table."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """CREATE TABLE paper_abstract_notes (
            paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
            cache_key TEXT NOT NULL,
            translation TEXT NOT NULL,
            summary TEXT NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    op.execute("CREATE INDEX abstract_notes_by_key ON paper_abstract_notes(cache_key)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS abstract_notes_by_key")
    op.execute("DROP TABLE IF EXISTS paper_abstract_notes")
