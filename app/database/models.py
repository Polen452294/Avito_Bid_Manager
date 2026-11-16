from datetime import UTC, datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    avito_id: Mapped[int | None] = mapped_column(unique=True)


class AdvertisementRow(Base):
    __tablename__ = "advertisements"
    item_id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), default=1)
    config: Mapped[dict] = mapped_column(JSON)


class SearchQueryRow(Base):
    __tablename__ = "search_queries"
    __table_args__ = (UniqueConstraint("item_id", "query", "region", "filters_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("advertisements.item_id"), index=True)
    query: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(String)
    filters_key: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    weight: Mapped[str] = mapped_column(String)


class RecordMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("advertisements.item_id"), index=True)
    created_at: Mapped[str] = mapped_column(String, default=utc_now, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class PositionRow(RecordMixin, Base):
    __tablename__ = "position_snapshots"
    observation_key: Mapped[str | None] = mapped_column(String, unique=True)
    context_key: Mapped[str | None] = mapped_column(String, index=True)


class BidSnapshotRow(RecordMixin, Base):
    __tablename__ = "bid_snapshots"


class DecisionRow(RecordMixin, Base):
    __tablename__ = "bid_decisions"
    schema_version: Mapped[int] = mapped_column(Integer, server_default="1")


class SpendingRow(RecordMixin, Base):
    __tablename__ = "spending_snapshots"


class PerformanceRow(RecordMixin, Base):
    __tablename__ = "performance_snapshots"


class RecommendationRow(RecordMixin, Base):
    __tablename__ = "performance_recommendations"


class OperationRow(Base):
    __tablename__ = "bid_change_operations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("advertisements.item_id"))
    decision_id: Mapped[int] = mapped_column(ForeignKey("bid_decisions.id"), unique=True)
    requested_bid: Mapped[int] = mapped_column(Integer)
    confirmed_bid: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=utc_now)
    __table_args__ = (Index("ix_operation_item_time", "item_id", "created_at"),)


class BudgetRow(Base):
    __tablename__ = "budget_configs"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    limit_kopecks: Mapped[int] = mapped_column(Integer)


class RuntimeRow(Base):
    __tablename__ = "runtime_states"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class NotificationRow(Base):
    __tablename__ = "notification_history"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)
    sent_at: Mapped[str] = mapped_column(String, default=utc_now)
