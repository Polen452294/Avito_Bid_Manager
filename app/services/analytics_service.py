import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.analytics.engine import AnalyticsEngine
from app.analytics.metrics import ratio
from app.analytics.store import StatisticsStore, day_bounds, latest_spending
from app.bidding.allocator import BudgetAllocator
from app.bidding.budget import BudgetController
from app.bidding.optimizer import PerformanceOptimizer
from app.database.models import BidSnapshotRow, OperationRow, RecommendationRow, RuntimeRow
from app.domain.models import BidInfo
from app.positions.history import PositionHistory
from app.services.execution_service import APPLIED


class AnalyticsService:
    def __init__(self, repository, settings, *, clock=None):
        self.repository = repository
        self.sessions = repository.sessions
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.zone = ZoneInfo(settings.timezone)
        self.store = StatisticsStore(self.sessions)
        self.history = PositionHistory(self.sessions)
        self.engine = AnalyticsEngine()
        self.budget = BudgetController(settings)
        self.optimizer = PerformanceOptimizer(settings.optimization_min_contacts)
        self.allocator = BudgetAllocator(settings.optimization_min_contacts)
        self.refresh_lock = asyncio.Lock()

    async def reports(self, days, *, now=None, items=None):
        if days not in (0, 1, 7, 14):
            raise ValueError("Use today, 1, 7 or 14 days")
        now = now or self.clock()
        today = now.astimezone(self.zone).date()
        start = today - timedelta(days=days)
        end = today if days else today + timedelta(days=1)
        earlier = start - timedelta(days=days)
        begin = day_bounds(earlier, self.zone)[0]
        period_start = day_bounds(start, self.zone)[0]
        until = day_bounds(end, self.zone)[0]
        result = []
        items = await self.repository.items() if items is None else items
        for item in items:
            rows = await self.store.latest_metrics(item.item_id, begin, until, now)
            positions = [
                row
                for row in await self.history.history(item.item_id)
                if begin <= row.checked_at <= now
            ]
            async with self.sessions() as session:
                bid_payloads = await session.scalars(
                    select(BidSnapshotRow.payload).where(BidSnapshotRow.item_id == item.item_id)
                )
                bids = [
                    bid
                    for payload in bid_payloads
                    if (bid := BidInfo.model_validate(payload)).checked_at <= now
                ]
                spending = await latest_spending(session, item.item_id)
                simulated_changes, last_simulated_change = (
                    await session.execute(
                        select(func.count(), func.max(OperationRow.applied_at)).where(
                            OperationRow.item_id == item.item_id,
                            OperationRow.status.in_(APPLIED),
                            OperationRow.applied_at >= period_start.astimezone(UTC).isoformat(),
                            OperationRow.applied_at < until.astimezone(UTC).isoformat(),
                            OperationRow.applied_at <= now.astimezone(UTC).isoformat(),
                        )
                    )
                ).one()
            current = max(
                (
                    bid
                    for bid in bids
                    if bid.data_type == "actual"
                    and bid.capabilities.model == item.promotion_model
                    and now - bid.checked_at <= timedelta(minutes=15)
                    and bid.capabilities.allows(bid.bid, now)
                ),
                key=lambda bid: bid.checked_at,
                default=None,
            )
            actual = self.engine.summarize(item, start, end, self.zone, rows, bids, positions)
            previous = (
                self.engine.summarize(item, earlier, start, self.zone, rows, bids, positions)
                if days
                else None
            )
            provisional = self.engine.summarize(
                item, start, end, self.zone, rows, [], [], data_type="preliminary"
            )
            result.append(
                {
                    "item": item,
                    "actual": actual,
                    "latest_metric_received": max(
                        (
                            row.received_at
                            for row in rows
                            if row.period_start >= day_bounds(start, self.zone)[0]
                            and row.data_type != "test"
                            and row.promotion_model == item.promotion_model
                        ),
                        default=None,
                    ),
                    "previous": previous,
                    "provisional": provisional,
                    "current_bid": current.bid if current else None,
                    "current_bid_info": current,
                    "simulated_changes": simulated_changes,
                    "last_simulated_change": (
                        datetime.fromisoformat(last_simulated_change)
                        if last_simulated_change
                        else None
                    ),
                    "budget_status": self.budget.check(item, spending, now),
                    "spending": spending
                    if spending
                    and spending.period_date == today.isoformat()
                    and spending.checked_at <= now
                    and spending.data_type != "test"
                    and item.promotion_model.value != "MOCK"
                    else None,
                    "warnings": self.budget.warnings(item, spending, now)
                    if spending
                    and spending.data_type != "test"
                    and item.promotion_model.value != "MOCK"
                    else {},
                    "account_limit": self.settings.account_daily_limit_kopecks,
                    "timezone": self.zone,
                    "budget_share": ratio(
                        item.daily_budget * 100, self.settings.account_daily_limit_kopecks
                    ),
                    "recommendation": self.optimizer.recommend(
                        item, current.bid if current else None, actual, previous, now
                    )
                    if previous
                    else None,
                }
            )
        return result

    async def refresh(self):
        async with self.refresh_lock:
            return await self._refresh()

    async def _refresh(self):
        now = self.clock()
        items = await self.repository.items()
        weekly = await self.reports(7, now=now, items=items)
        fortnightly = await self.reports(14, now=now, items=items)
        today = await self.reports(0, now=now, items=items)
        allocations = self.allocator.allocate(
            [(r["item"], r["actual"], r["previous"]) for r in weekly],
            self.settings.account_daily_limit_kopecks,
            now,
        )
        recommendations = [r["recommendation"] for r in [*weekly, *fortnightly]] + allocations
        configs = {item.item_id: item.model_dump(mode="json", exclude={"title"}) for item in items}
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            for recommendation in recommendations:
                payload = recommendation.model_dump(mode="json")
                table = RecommendationRow
                group = "allocation" if recommendation in allocations else "bid"
                period = "|".join(payload["analysis_period"])
                previous = await session.scalar(
                    select(table.payload)
                    .where(
                        table.item_id == recommendation.item_id,
                        table.payload["group"].as_string() == group,
                        table.payload["period_key"].as_string() == period,
                    )
                    .order_by(table.id.desc())
                    .limit(1)
                )
                state = {
                    **payload,
                    "group": group,
                    "period_key": period,
                    "item_config": configs[recommendation.item_id],
                }
                if previous and all(
                    value == previous.get(key)
                    for key, value in state.items()
                    if key != "calculated_at"
                ):
                    continue
                session.add(
                    RecommendationRow(
                        item_id=recommendation.item_id,
                        created_at=now.isoformat(),
                        payload=state,
                    )
                )
            await session.merge(
                RuntimeRow(
                    key="phase5_daily_report",
                    payload={
                        "calculated_at": now.isoformat(),
                        "items": [
                            {
                                "item_id": r["item"].item_id,
                                "verified_spend": r["spending"].item_spent
                                if (self.budget.reliable(r["item"], r["spending"], now))
                                else None,
                                "provisional_spend": r["provisional"].spend,
                                "daily_budget": r["item"].daily_budget,
                                "warnings": r["warnings"],
                                "cpl_7d_rubles": str(w["actual"].cpl)
                                if w["actual"].cpl is not None
                                else None,
                                "cpl_14d_rubles": str(f["actual"].cpl)
                                if f["actual"].cpl is not None
                                else None,
                            }
                            for r, w, f in zip(today, weekly, fortnightly, strict=True)
                        ],
                    },
                )
            )
        return today

    def budget_recommendations(self, weekly):
        return self.allocator.allocate(
            [(r["item"], r["actual"], r["previous"]) for r in weekly],
            self.settings.account_daily_limit_kopecks,
            self.clock(),
        )
