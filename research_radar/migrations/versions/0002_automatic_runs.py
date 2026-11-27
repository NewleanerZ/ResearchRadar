"""Distinguish scheduled jobs from jobs explicitly requested by the user."""

from alembic import op
from sqlalchemy import Boolean, Column, false

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", Column("automatic", Boolean(), nullable=False, server_default=false()))


def downgrade():
    op.drop_column("runs", "automatic")
