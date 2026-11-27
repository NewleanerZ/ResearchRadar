"""Persist conversations, action receipts and research checkpoints."""

from alembic import op
from sqlalchemy import Boolean, Column, false

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, context TEXT NOT NULL,
        created_at TEXT NOT NULL, opened_at TEXT NOT NULL
    )""")
    op.execute("""CREATE TABLE conversation_turns (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        prompt TEXT NOT NULL, answer TEXT, status TEXT NOT NULL, events TEXT NOT NULL DEFAULT '[]',
        usage TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    )""")
    op.execute("CREATE INDEX turns_by_conversation ON conversation_turns(conversation_id, created_at)")
    op.execute("""CREATE TABLE conversation_actions (
        id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES conversation_turns(id),
        kind TEXT NOT NULL, arguments TEXT NOT NULL, preview TEXT NOT NULL,
        status TEXT NOT NULL, receipt TEXT
    )""")
    op.execute("""CREATE TABLE research_jobs (
        id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES conversation_turns(id),
        status TEXT NOT NULL, stage TEXT NOT NULL, spec TEXT NOT NULL, checkpoint TEXT NOT NULL,
        created_at TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL,
        cancel_requested INTEGER NOT NULL DEFAULT 0, markdown TEXT
    )""")
    op.add_column("notifications", Column("manual_retry", Boolean(), nullable=False, server_default=false()))


def downgrade():
    op.drop_column("notifications", "manual_retry")
    for table in ("research_jobs", "conversation_actions", "conversation_turns", "conversations"):
        op.drop_table(table)
