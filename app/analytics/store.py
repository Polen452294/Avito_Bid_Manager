from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, ValidationError, model_validator
from sqlalchemy import select, text

from app.database.models import BidSnapshotRow, PerformanceRow, SpendingRow
from app.domain.enums import PromotionModel
from app.domain.models import Model, Money, Spending

MetricKind = Literal[
    "spend",
    "promotion_spend",
    "views",
    "contacts",
    "favorites",
    "messenger_contacts",
    "phone_reveals",
]
DataType = Literal["actual", "preliminary", "test"]


def day_bounds(day: date, zone: ZoneInfo):
    return (
        datetime.combine(day, time.min, zone),
        datetime.combine(day + timedelta(days=1), time.min, zone),
    )


class DailyMetric(Model):
    item_id: int = Field(gt=0)
    metric: MetricKind
    value: Money | None
    period_start: AwareDatetime
    period_end: AwareDatetime
    received_at: AwareDatetime
    source: str = Field(min_length=1)
    data_type: DataType
    quality: Literal["verified", "unverified", "invalid"]
    promotion_model: PromotionModel
    listing_revision: str | None = None
    demand_context: str | None = None

    @model_validator(mode="after")
    def valid_period(self):
        if not self.period_start < self.period_end or self.received_at < self.period_start:
            raise ValueError("Invalid metric period")
        if self.data_type == "actual" and self.received_at < self.period_end:
            raise ValueError("Actual daily metrics require a closed period")
        if (self.value is None) != (self.quality == "invalid"):
            raise ValueError("Only invalid metrics represent an unavailable value")
        return self

    def key(self):
        return self.item_id, self.metric, self.period_start, self.period_end, self.source


async def latest_spending(session, item_id):
    payload = await session.scalar(
        select(SpendingRow.payload)
        .where(SpendingRow.item_id == item_id)
        .order_by(SpendingRow.id.desc())
        .limit(1)
    )
    try:
        return Spending.model_validate(payload) if payload is not None else None
    except ValidationError:
        return None


class StatisticsStore:
    def __init__(self, sessions):
        self.sessions = sessions

    async def append(self, metrics, spendings=(), bids=()):
        spendings = tuple(spendings)
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            inserted = 0
            for metric in metrics:
                payload = metric.model_dump(mode="json")
                table = PerformanceRow
                previous = await session.scalar(
                    select(table.payload)
                    .where(
                        table.item_id == metric.item_id,
                        *(
                            table.payload[key].as_string() == payload[key]
                            for key in ("metric", "period_start", "period_end", "source")
                        ),
                    )
                    .order_by(table.id.desc())
                    .limit(1)
                )
                if previous:
                    earlier = DailyMetric.model_validate(previous)
                    if metric.received_at < earlier.received_at:
                        continue
                    if earlier.model_dump(exclude={"received_at"}) == metric.model_dump(
                        exclude={"received_at"}
                    ):
                        continue
                    if metric.received_at == earlier.received_at:
                        raise ValueError("Conflicting metric versions with identical receipt time")
                session.add(
                    PerformanceRow(
                        item_id=metric.item_id,
                        created_at=metric.received_at.isoformat(),
                        payload=payload,
                    )
                )
                await session.flush()
                inserted += 1
            current = [await latest_spending(session, row.item_id) for row in spendings]
            batch = list(zip(spendings, current, strict=True))
            if not any(old and new.checked_at < old.checked_at for new, old in batch):
                for spending, previous in batch:
                    if previous == spending:
                        continue
                    session.add(
                        SpendingRow(
                            item_id=spending.item_id,
                            created_at=spending.checked_at.isoformat(),
                            payload=spending.model_dump(mode="json"),
                        )
                    )
            for bid in bids:
                payload = bid.model_dump(mode="json")
                existing = await session.scalar(
                    select(BidSnapshotRow.id)
                    .where(
                        BidSnapshotRow.item_id == bid.item_id,
                        BidSnapshotRow.payload == payload,
                    )
                    .limit(1)
                )
                if existing is None:
                    session.add(
                        BidSnapshotRow(
                            item_id=bid.item_id,
                            created_at=bid.checked_at.isoformat(),
                            payload=payload,
                        )
                    )
            return inserted

    async def latest_metrics(self, item_id, start, end, now):
        start = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self.sessions() as session:
            table = PerformanceRow
            rows = await session.scalars(
                select(table.payload)
                .where(
                    table.item_id == item_id,
                    table.payload["period_start"].as_string() >= start,
                    table.payload["period_end"].as_string() <= end,
                )
                .order_by(table.id)
            )
            versions = {}
            for payload in rows:
                row = DailyMetric.model_validate(payload)
                if row.received_at <= now:
                    versions[row.key()] = row
            return list(versions.values())

    async def collected_day(self, items, start, end, source):
        ids = {item.item_id for item in items}
        start = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with self.sessions() as session:
            table = PerformanceRow
            rows = await session.execute(
                select(table.item_id, table.payload["promotion_model"].as_string())
                .where(
                    table.item_id.in_(ids),
                    table.payload["period_start"].as_string() == start,
                    table.payload["period_end"].as_string() == end,
                    table.payload["metric"].as_string() == "spend",
                    table.payload["source"].as_string() == source,
                )
                .distinct()
            )
            return {(item.item_id, item.promotion_model.value) for item in items} <= set(rows)
