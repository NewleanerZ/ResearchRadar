"""Add turn reasoning/streaming columns and external MCP server registry."""

from alembic import op
from sqlalchemy import Column, Text

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversation_turns", Column("reasoning", Text(), nullable=True))
    op.add_column("conversation_turns", Column("partial_answer", Text(), nullable=True))
    op.execute("""CREATE TABLE mcp_servers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        command TEXT NOT NULL,
        args_json TEXT NOT NULL DEFAULT '[]',
        env_keys_json TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""")


def downgrade():
    op.execute("DROP TABLE IF EXISTS mcp_servers")
    op.drop_column("conversation_turns", "partial_answer")
    op.drop_column("conversation_turns", "reasoning")
