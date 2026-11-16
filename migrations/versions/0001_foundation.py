"""Initial schema, fixed independently from application metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("avito_id", sa.Integer, unique=True),
    )
    op.create_table(
        "advertisements",
        sa.Column("item_id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
    )
    op.create_table(
        "search_queries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("advertisements.item_id"), nullable=False),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("region", sa.String, nullable=False),
        sa.Column("weight", sa.String, nullable=False),
        sa.UniqueConstraint("item_id", "query", "region"),
    )
    op.create_index("ix_search_queries_item_id", "search_queries", ["item_id"])
    for name in (
        "position_snapshots",
        "bid_snapshots",
        "bid_decisions",
        "spending_snapshots",
        "performance_snapshots",
    ):
        op.create_table(
            name,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "item_id", sa.Integer, sa.ForeignKey("advertisements.item_id"), nullable=False
            ),
            sa.Column("created_at", sa.String, nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
        )
        op.create_index(f"ix_{name}_item_id", name, ["item_id"])
        op.create_index(f"ix_{name}_created_at", name, ["created_at"])
    op.create_table(
        "bid_change_operations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("advertisements.item_id"), nullable=False),
        sa.Column(
            "decision_id",
            sa.Integer,
            sa.ForeignKey("bid_decisions.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("requested_bid", sa.Integer, nullable=False),
        sa.Column("confirmed_bid", sa.Integer),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_index("ix_operation_item_time", "bid_change_operations", ["item_id", "created_at"])
    op.create_table(
        "budget_configs",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("limit_kopecks", sa.Integer, nullable=False),
    )
    op.create_table(
        "runtime_states",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_table(
        "notification_history",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("sent_at", sa.String, nullable=False),
    )


def downgrade():
    for name in (
        "notification_history",
        "runtime_states",
        "budget_configs",
        "bid_change_operations",
        "performance_snapshots",
        "spending_snapshots",
        "bid_decisions",
        "bid_snapshots",
        "position_snapshots",
        "search_queries",
        "advertisements",
        "accounts",
    ):
        op.drop_table(name)
