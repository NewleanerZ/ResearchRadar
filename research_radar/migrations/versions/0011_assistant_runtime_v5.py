"""Add runtime suspend/resume, context compaction, artifacts, steer queue, and MCP cache tables."""

from alembic import op
from sqlalchemy import Column, Integer, Text

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mcp_servers", Column("tools_cache_json", Text(), nullable=False, server_default="[]"))
    op.add_column("mcp_servers", Column("auto_approve", Integer(), nullable=False, server_default="0"))

    op.execute(
        """CREATE TABLE conversation_compactions (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            compaction_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )

    op.execute(
        """CREATE TABLE conversation_turn_runtime (
            turn_id TEXT PRIMARY KEY REFERENCES conversation_turns(id) ON DELETE CASCADE,
            state_json TEXT,
            messages_json TEXT,
            parent_turn_id TEXT,
            updated_at TEXT NOT NULL
        )"""
    )

    op.execute(
        """CREATE TABLE conversation_artifacts (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            turn_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            title TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    op.execute("CREATE INDEX artifacts_by_conversation ON conversation_artifacts(conversation_id, created_at)")

    op.execute(
        """CREATE TABLE conversation_steer_queue (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            turn_id TEXT,
            mode TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    op.execute("CREATE INDEX steer_by_conversation ON conversation_steer_queue(conversation_id, status, created_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS steer_by_conversation")
    op.execute("DROP TABLE IF EXISTS conversation_steer_queue")
    op.execute("DROP INDEX IF EXISTS artifacts_by_conversation")
    op.execute("DROP TABLE IF EXISTS conversation_artifacts")
    op.execute("DROP TABLE IF EXISTS conversation_turn_runtime")
    op.execute("DROP TABLE IF EXISTS conversation_compactions")
    op.drop_column("mcp_servers", "auto_approve")
    op.drop_column("mcp_servers", "tools_cache_json")
