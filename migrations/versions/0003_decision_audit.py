"""Version decision payloads and record confirmed application time."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bid_decisions",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
    )
    op.add_column("bid_change_operations", sa.Column("applied_at", sa.String, nullable=True))


def downgrade():
    raise RuntimeError("Forward-only migration: preserve decision audit")
