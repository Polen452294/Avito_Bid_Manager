"""Persist advisory recommendations independently from bid decisions."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "performance_recommendations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("advertisements.item_id"), nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    for column in ("item_id", "created_at"):
        op.create_index(
            f"ix_performance_recommendations_{column}", "performance_recommendations", [column]
        )


def downgrade():
    op.drop_table("performance_recommendations")
