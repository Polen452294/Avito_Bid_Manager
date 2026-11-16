from datetime import date, timedelta
from decimal import Decimal

from app.avito.accounts import item_ids, positive_id
from app.avito.exceptions import InvalidRequest, InvalidResponse
from app.avito.schemas import Analytics, Counters, Spendings, parse


def dates(start: date, end: date):
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise InvalidRequest("Invalid statistics date range")
    if (end - start).days >= 270:
        raise InvalidRequest("Statistics range exceeds 270 days")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    if start < today - timedelta(days=269) or end > today:
        raise InvalidRequest("Statistics dates outside published history depth")
    return {"dateFrom": start.isoformat(), "dateTo": end.isoformat()}


def exact_kopecks(value: Decimal) -> int:
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise InvalidResponse("Expected non-negative whole kopecks")
    return int(value)


class StatisticsAPI:
    def __init__(self, client):
        self.client = client

    async def counters(self, account_id, ids, start, end):
        requested = item_ids(ids)
        body = {
            **dates(start, end),
            "itemIds": requested,
            "periodGrouping": "day",
            "fields": ["uniqViews", "uniqContacts", "uniqFavorites"],
        }
        result = parse(
            Counters,
            await self.client.operation(
                "itemStatsShallow", path_values={"user_id": positive_id(account_id)}, body=body
            ),
        )
        returned = [i.itemId for i in result.result.items]
        if len(set(returned)) != len(returned) or not set(returned) <= set(requested):
            raise InvalidResponse("Unexpected statistics item IDs")
        for item in result.result.items:
            days = [s.date for s in item.stats]
            if len(set(days)) != len(days) or any(not start <= day <= end for day in days):
                raise InvalidResponse("Unexpected statistics dates")
        return result

    async def analytics(self, account_id, start, end, *, grouping="item", offset=0, limit=1000):
        if grouping not in {"item", "totals", "day", "week", "month"}:
            raise InvalidRequest("Invalid analytics grouping")
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise InvalidRequest("Invalid analytics pagination")
        body = {
            **dates(start, end),
            "grouping": grouping,
            "offset": offset,
            "limit": limit,
            "metrics": [
                "spending",
                "promoSpending",
                "views",
                "contacts",
                "contactsShowPhone",
                "contactsMessenger",
            ],
        }
        result = parse(
            Analytics,
            await self.client.operation(
                "itemAnalytics", path_values={"user_id": positive_id(account_id)}, body=body
            ),
        )
        for group in result.result.groupings:
            if group.type != grouping or len({m.slug for m in group.metrics}) != len(group.metrics):
                raise InvalidResponse("Invalid analytics grouping or duplicate metrics")
            for metric in group.metrics:
                if metric.slug in {"spending", "promoSpending"}:
                    exact_kopecks(metric.value)
        return result

    async def daily_item_metrics(self, account_id, ids, day):
        requested = set(item_ids(ids))
        names = {
            "spending": "spend",
            "promoSpending": "promotion_spend",
            "views": "views",
            "contacts": "contacts",
            "contactsMessenger": "messenger_contacts",
            "contactsShowPhone": "phone_reveals",
        }
        found = {}
        seen = set()
        total = None
        for _ in range(5):
            result = (await self.analytics(account_id, day, day, offset=len(seen))).result
            groups = result.groupings
            page = {group.id for group in groups}
            if total is None:
                total = result.dataTotalCount
            if (
                total != result.dataTotalCount
                or len(groups) != len(page)
                or seen & page
                or len(seen) + len(page) > total
            ):
                raise InvalidResponse("Inconsistent item analytics pagination")
            seen.update(page)
            found.update(
                {
                    g.id: {
                        name: exact_kopecks(m.value)
                        for m in g.metrics
                        if (name := names.get(m.slug))
                    }
                    for g in groups
                    if g.id in requested
                }
            )
            if len(seen) == total or found.keys() == requested:
                return found
            if not page:
                break
        raise InvalidResponse("Incomplete item analytics pagination")

    async def daily_account_spend(self, account_id, day):
        result = await self.spendings(account_id, day, day)
        groups = result.result.groupings
        if len(groups) > 1 or any(g.type != "day" or g.date != day.isoformat() for g in groups):
            raise InvalidResponse("Unexpected daily spending grouping")
        return next(
            (exact_kopecks(m.value) for g in groups for m in g.spendings if m.slug == "all"),
            None,
        )

    async def spendings(self, account_id, start, end, *, ids=None):
        body = {**dates(start, end), "grouping": "day", "spendingTypes": ["all", "promotion"]}
        if ids is not None:
            body["filter"] = {"itemIDs": item_ids(ids)}
        result = parse(
            Spendings,
            await self.client.operation(
                "accountSpendings", path_values={"user_id": positive_id(account_id)}, body=body
            ),
        )
        for group in result.result.groupings:
            if len({s.slug for s in group.spendings}) != len(group.spendings):
                raise InvalidResponse("Duplicate spending totals")
            for spending in group.spendings:
                exact_kopecks(spending.value)
        # `all` already includes promotion: never add them. Raw timestamp kept as published;
        # request time is NOT a guaranteed update time, so no reliable BudgetController input.
        return result
