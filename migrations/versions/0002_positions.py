"""Add indexed identity without changing or deleting historical observations."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("position_snapshots") as batch:
        batch.add_column(sa.Column("observation_key", sa.String, nullable=True))
        batch.add_column(sa.Column("context_key", sa.String, nullable=True))
        batch.create_unique_constraint("uq_position_identity", ["observation_key"])
        batch.create_index("ix_position_snapshots_context_key", ["context_key"])
    with op.batch_alter_table(
        "search_queries",
        naming_convention={
            "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s_%(column_2_name)s"
        },
    ) as batch:
        batch.add_column(sa.Column("filters_key", sa.Text, nullable=False, server_default="{}"))
        batch.drop_constraint("uq_search_queries_item_id_query_region", type_="unique")
        batch.create_unique_constraint(
            "uq_query_context", ["item_id", "query", "region", "filters_key"]
        )


def downgrade():
    raise RuntimeError("Forward-only migration: preserve position history")
