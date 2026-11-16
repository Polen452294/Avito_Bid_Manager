from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from app.analytics.metrics import ratio
from app.analytics.store import day_bounds
from app.positions.validation import context_key, usable


def mean(values):
    return sum(values, Decimal(0)) / len(values) if values else None


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    spend: int | None
    views: int | None
    contacts: int | None
    average_bid: Decimal | None
    bid_days: int
    position: Decimal | None
    position_context: tuple | None
    position_days: int
    context: tuple | None
    bid_changes: int | None
    quality: str
    bid_range: tuple | None
    metric_sources: tuple
    metric_received_at: tuple
    bid_sources: tuple[str, ...]
    bid_as_of: datetime | None
    position_as_of: datetime | None

    @property
    def days(self):
        return (self.end - self.start).days

    @property
    def cpl(self):
        if self.metric_sources[0] == self.metric_sources[2] and self.contacts:
            return ratio(self.spend, self.contacts * 100)
        return None

    @property
    def cpv(self):
        if self.metric_sources[0] == self.metric_sources[1] and self.views:
            return ratio(self.spend, self.views * 100)
        return None

    @property
    def conversion(self):
        if self.metric_sources[1] == self.metric_sources[2] and self.contacts is not None:
            return ratio(self.contacts * 100, self.views)
        return None

    @property
    def average_daily_spend(self):
        return ratio(self.spend, self.days)


class AnalyticsEngine:
    def summarize(self, item, start, end, zone, rows, bids, positions, *, data_type="actual"):
        days = [start + timedelta(days=i) for i in range((end - start).days)]
        if not days:
            raise ValueError("Analytics period must include a day")
        bounds = {day: day_bounds(day, zone) for day in days}
        selected = defaultdict(list)
        for row in rows:
            day = row.period_start.astimezone(zone).date()
            if (
                row.item_id == item.item_id
                and bounds.get(day) == (row.period_start, row.period_end)
                and row.promotion_model == item.promotion_model
                and row.value is not None
                and row.data_type == data_type
                and (data_type != "actual" or row.quality == "verified")
            ):
                selected[day, row.metric].append(row)

        def complete(metric):
            groups = [selected[day, metric] for day in days]
            if all(len(group) == 1 for group in groups):
                series = [group[0] for group in groups]
                if len({row.source for row in series}) == 1:
                    return series
            return []

        series = [complete(metric) for metric in ("spend", "views", "contacts")]
        spend, views, contacts = (
            sum(r.value for r in values) if values else None for values in series
        )
        context_keys = {
            (r.source, r.promotion_model, r.listing_revision, r.demand_context)
            for values in series
            for r in values
        }
        context = None
        if data_type == "actual" and all(series) and len(context_keys) == 1:
            key = next(iter(context_keys))
            if key[2] and key[3]:
                context = key
        start_time, end_time = bounds[days[0]][0], bounds[days[-1]][1]
        bid_samples = sorted(
            (
                bid
                for bid in bids
                if bid.item_id == item.item_id
                and bid.capabilities.model == item.promotion_model
                and bid.data_type == data_type
                and bid.capabilities.allows(bid.bid, bid.checked_at)
                and start_time <= bid.checked_at < end_time
            ),
            key=lambda bid: bid.checked_at,
        )
        bids_by_day = defaultdict(list)
        for bid in bid_samples:
            bids_by_day[bid.checked_at.astimezone(zone).date()].append(bid.bid)
        bid_values = [bid.bid for bid in bid_samples]
        position_context, position, position_days, position_as_of = self._positions(
            item, start_time, end_time, zone, positions, data_type
        )
        return Period(
            start,
            end,
            spend,
            views,
            contacts,
            mean([mean(values) for values in bids_by_day.values()]),
            len(bids_by_day),
            position,
            position_context,
            position_days,
            context,
            sum(a != b for a, b in pairwise(bid_values)) if bid_values else None,
            data_type.upper()
            if all(series) and len({v[0].source for v in series}) == 1
            else "INCOMPLETE",
            (min(bid_values), max(bid_values)) if bid_values else None,
            tuple(values[0].source if values else None for values in series),
            tuple(max((row.received_at for row in values), default=None) for values in series),
            tuple(sorted({bid.source for bid in bid_samples})),
            bid_samples[-1].checked_at if bid_samples else None,
            position_as_of,
        )

    @staticmethod
    def _positions(item, start, end, zone, observations, data_type):
        if not item.queries:
            return None, None, 0, None
        query = item.queries[item.primary_query_index]
        key = context_key(item.item_id, query)
        groups = defaultdict(lambda: defaultdict(dict))
        for row in observations:
            if (
                context_key(row.item_id, row.search_query, row.region, row.filters) == key
                and (row.provider == "mock") == (data_type == "test")
                and row.provider in item.position_sources
                and start <= row.checked_at < end
                and usable(row, row.checked_at)
            ):
                day = row.checked_at.astimezone(zone).date()
                groups[row.provider, row.position_type][day][row.checked_at] = row.position
        if len(groups) != 1:
            return None, None, 0, None
        (provider, kind), values = next(iter(groups.items()))
        return (
            (query.text, query.region, tuple(sorted(query.filters.items())), provider, kind),
            mean([mean(list(daily.values())) for daily in values.values()]),
            len(values),
            max(stamp for daily in values.values() for stamp in daily),
        )
