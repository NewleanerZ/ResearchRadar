"""Add persistent per-paper PDF availability precheck table."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """CREATE TABLE paper_pdf_checks (
            paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
            status TEXT NOT NULL,
            pdf_url TEXT,
            strategy TEXT,
            reason TEXT,
            checked_at TEXT NOT NULL
        )"""
    )
    op.execute("CREATE INDEX pdf_checks_by_status ON paper_pdf_checks(status, checked_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS pdf_checks_by_status")
    op.execute("DROP TABLE IF EXISTS paper_pdf_checks")
