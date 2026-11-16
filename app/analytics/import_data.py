from datetime import UTC, datetime

from app.analytics.store import DailyMetric, day_bounds
from app.domain.models import BidInfo, Model, Spending


class StatisticsImport(Model):
    metrics: tuple[DailyMetric, ...] = ()
    bids: tuple[BidInfo, ...] = ()
    spendings: tuple[Spending, ...] = ()


async def import_statistics(service, data, *, now=None):
    now = now or datetime.now(UTC)
    items = {item.item_id: item for item in await service.repository.items()}
    for row in data.metrics:
        item = items.get(row.item_id)
        day = row.period_start.astimezone(service.zone).date()
        if (
            item is None
            or item.promotion_model != row.promotion_model
            or row.received_at > now
            or day_bounds(day, service.zone) != (row.period_start, row.period_end)
            or (item.promotion_model.value == "MOCK" and row.data_type != "test")
        ):
            raise ValueError("Invalid item, model, receipt or daily boundaries in metrics")
    for bid in data.bids:
        item = items.get(bid.item_id)
        if (
            item is None
            or item.promotion_model != bid.capabilities.model
            or not bid.capabilities.allows(bid.bid, bid.checked_at)
            or bid.checked_at > now
            or (item.promotion_model.value == "MOCK" and bid.data_type != "test")
        ):
            raise ValueError("Bid history requires a verified observation and matching model")
    spending = data.spendings
    if spending:
        cuts = {
            (
                row.period_date,
                row.data_as_of,
                row.checked_at,
                row.source,
                row.account_spent,
                row.data_type,
            )
            for row in spending
        }
        if (
            {row.item_id for row in spending} != items.keys()
            or len(spending) != len(items)
            or len(cuts) != 1
            or any(
                row.source == "legacy"
                or row.data_as_of is None
                or not service.budget.reliable(items[row.item_id], row, now)
                for row in spending
            )
            or sum(row.item_spent for row in spending) > spending[0].account_spent
        ):
            raise ValueError("Current spending requires one fresh account cut covering every item")
    imported_bids = [bid.model_copy(update={"source": "manual-import"}) for bid in data.bids]
    return await service.store.append(data.metrics, spending, imported_bids)
