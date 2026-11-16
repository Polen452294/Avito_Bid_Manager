import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.analytics.collect import StatisticsCollector
from app.analytics.engine import AnalyticsEngine
from app.analytics.import_data import StatisticsImport, import_statistics
from app.analytics.store import DailyMetric, StatisticsStore, day_bounds
from app.avito.bidding import MockBidProvider
from app.bidding.allocator import BudgetAllocator
from app.bidding.backtest import ReplayFrame, backtest
from app.bidding.budget import BudgetController
from app.bidding.engine import BiddingEngine
from app.bidding.optimizer import PerformanceOptimizer
from app.database.models import (
    DecisionRow,
    OperationRow,
    PerformanceRow,
    RecommendationRow,
    RuntimeRow,
)
from app.domain.enums import Mode, PromotionModel
from app.domain.models import Advertisement, AppliedChange
from app.services.analytics_service import AnalyticsService
from app.services.bidding_service import BiddingService

NOW = datetime(2026, 1, 20, 12, tzinfo=UTC)
MIN = timedelta(minutes=1)
DAY = timedelta(days=1)
ZONE = ZoneInfo("Europe/Moscow")
TODAY = NOW.astimezone(ZONE).date()


@pytest.fixture
def finance(case):
    settings, item, info, spending, _, positions = case
    info = info.model_copy(
        update={
            "checked_at": NOW,
            "capabilities": info.capabilities.model_copy(update={"expires_at": NOW + DAY}),
        }
    )
    spending = spending.model_copy(
        update={
            "checked_at": NOW,
            "data_as_of": NOW - MIN,
            "period_date": TODAY.isoformat(),
            "data_type": "test",
            "source": "finance_fixture",
        }
    )

    def samples(values=(12, 12), *, now=NOW, item_id=1, provider="mock"):
        return [
            p.model_copy(
                update={
                    "checked_at": now - (len(values) - 1 - i) * MIN,
                    "item_id": item_id,
                    "provider": provider,
                }
            )
            for i, p in enumerate(positions(values))
        ]

    return settings, item, info, spending, samples


@pytest.mark.parametrize(
    ("item_spent", "total", "reason", "warnings"),
    [
        (7999, 79999, None, {}),
        (8000, 8000, "ITEM_BUDGET_GUARD", {"item": 80}),
        (0, 80000, "ACCOUNT_BUDGET_GUARD", {"account": 80}),
        (9500, 95000, "ITEM_BUDGET_GUARD", {"item": 95, "account": 95}),
        (10333, 120000, "ITEM_BUDGET_GUARD", {"item": 95, "account": 95}),
    ],
)
def test_budget_boundaries(finance, item_spent, total, reason, warnings):
    settings, item, _, spending, _ = finance
    controller = BudgetController(settings)
    spending = spending.model_copy(update={"item_spent": item_spent, "account_spent": total})
    assert controller.check(item, spending, NOW) == reason
    assert controller.warnings(item, spending, NOW) == warnings


@pytest.mark.parametrize(
    "patch",
    [
        {"account_spent": None},
        {"reliable": False},
        {"data_as_of": NOW - 31 * MIN},
        {"period_date": "2026-01-19"},
        {"data_as_of": NOW + MIN},
        {"data_type": "preliminary"},
    ],
)
def test_financial_freshness(finance, patch):
    settings, item, info, spending, samples = finance
    engine = BiddingEngine(settings)
    decision = engine.decide(item, info, samples(), spending.model_copy(update=patch), NOW)
    assert decision.reason_code == "BUDGET_DATA_UNAVAILABLE"
    assert decision.new_bid is None


@pytest.mark.parametrize("mode", [Mode.DRY_RUN, Mode.ASSISTED])
async def test_five_simultaneous_decisions_share_one_financial_cut(finance, database, mode):
    settings, item, info, spending, samples = finance
    _, sessions, repository = database
    items = [item.model_copy(update={"item_id": i}) for i in range(1, 6)]
    for item in items:
        await repository.save_item(item)
    provider = MockBidProvider(
        {i.item_id: info.model_copy(update={"item_id": i.item_id}) for i in items},
        clock=lambda: NOW,
    )
    store = StatisticsStore(sessions)
    await store.append([], [spending.model_copy(update={"item_id": i.item_id}) for i in items])
    outcomes = await asyncio.gather(
        *(
            BiddingService(
                sessions, provider, BiddingEngine(settings), clock=lambda: NOW
            ).evaluate_history(
                i,
                SimpleNamespace(
                    history=lambda item_id: asyncio.sleep(0, result=samples(item_id=item_id))
                ),
            )
            if mode == Mode.DRY_RUN
            else BiddingService(
                sessions, provider, BiddingEngine(settings), clock=lambda: NOW
            ).evaluate(
                i,
                samples(item_id=i.item_id),
                spending.model_copy(update={"item_id": i.item_id}),
                mode=mode,
                owner_confirmed=True,
            )
            for i in items
        )
    )
    assert sum(d.new_bid is not None for d, _ in outcomes) == 1
    assert sum(d.reason_code == "ACCOUNT_BUDGET_SNAPSHOT_CLAIMED" for d, _ in outcomes) == 4
    assert provider.write_count == (1 if mode == Mode.ASSISTED else 0)


async def test_expense_reloaded_before_execution(finance, database):
    settings, item, info, spending, samples = finance
    _, sessions, repository = database
    provider = MockBidProvider({1: info}, clock=lambda: NOW)
    service = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: NOW)
    decision, status = await service.evaluate(item, samples(), spending, mode=Mode.ASSISTED)
    assert status == "AWAITING_CONFIRMATION"
    await StatisticsStore(sessions).append(
        [], [spending.model_copy(update={"account_spent": 100000})]
    )
    async with sessions() as session:
        operation_id = await session.scalar(select(OperationRow.id))
    assert (
        await service.execution.execute(operation_id, item, decision, spending, Mode.ASSISTED, True)
        == "PREFLIGHT_BLOCKED"
    )
    saved = (await repository.decisions(item.item_id))[0]
    assert (saved["action"], saved["reason_code"]) == ("HOLD", "ACCOUNT_BUDGET_GUARD")
    assert provider.write_count == 0


def metric(item, day, kind, value, **update):
    start, end = day_bounds(day, ZONE)
    data = {
        "item_id": item.item_id,
        "metric": kind,
        "value": value,
        "period_start": start,
        "period_end": end,
        "received_at": NOW - MIN,
        "source": "verified_export",
        "data_type": "actual",
        "quality": "verified",
        "promotion_model": item.promotion_model,
        "listing_revision": "rev-1",
        "demand_context": "matched weekdays",
    }
    return DailyMetric(**(data | update))


def live_item(item, **updates):
    return Advertisement.model_validate(
        {
            **item.model_dump(),
            "promotion_model": "CPX",
            "bid_unit": None,
            **updates,
        }
    )


async def test_idempotence_corrections_and_as_of_history(finance, database):
    _, item, _, _, _ = finance
    _, sessions, _ = database
    store = StatisticsStore(sessions)
    amount = metric(item, TODAY - DAY, "spend", 10001)
    assert await store.append([amount]) == 1
    assert await store.append([amount]) == 0
    assert await store.append([amount.model_copy(update={"received_at": NOW})]) == 0
    update = amount.model_copy(update={"value": 9500, "received_at": NOW})
    assert await store.append([update]) == 1
    assert await store.append([amount]) == 0
    start, end = day_bounds(TODAY - DAY, ZONE)
    assert (await store.latest_metrics(item.item_id, start, end, NOW - MIN))[0].value == 10001
    assert (await store.latest_metrics(item.item_id, start, end, NOW))[0].value == 9500
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PerformanceRow)) == 2


@pytest.mark.parametrize(
    ("contacts", "cpl", "conversion"),
    [
        (0, None, Decimal(0)),
        (2, Decimal("0.015"), Decimal(50)),
        (None, None, None),
    ],
)
def test_decimal_ratios_and_contact_overlap(finance, contacts, cpl, conversion):
    _, item, _, _, _ = finance
    item = live_item(item)
    rows = [
        metric(item, TODAY - DAY, kind, value)
        for kind, value in (
            ("spend", 3),
            ("views", 4),
            ("contacts", contacts),
            ("phone_reveals", 2),
            ("messenger_contacts", 2),
        )
        if value is not None
    ]
    report = AnalyticsEngine().summarize(item, TODAY - DAY, TODAY, ZONE, rows, [], [])
    assert (report.cpl, report.cpv, report.conversion) == (cpl, Decimal("0.0075"), conversion)


def period(
    item,
    info,
    samples,
    days,
    end,
    bid,
    spend,
    *,
    context="matched weekdays",
    leads=3,
    provider="manual",
    missing_day=False,
    rank=5,
):
    start = end - days * DAY
    rows, bids, positions = [], [], []
    for i in range(days):
        day = start + i * DAY
        if missing_day and i == 0:
            continue
        seen = day_bounds(day, ZONE)[0] + timedelta(hours=12)
        rows.extend(
            metric(item, day, kind, value, demand_context=context)
            for kind, value in (("spend", spend), ("contacts", leads), ("views", 20))
        )
        bids.append(
            info.model_copy(
                update={
                    "item_id": item.item_id,
                    "bid": bid,
                    "checked_at": seen,
                    "data_type": "actual",
                    "capabilities": info.capabilities.model_copy(
                        update={"model": item.promotion_model}
                    ),
                }
            )
        )
        positions.extend(samples((rank,), now=seen, item_id=item.item_id, provider=provider))
    return AnalyticsEngine().summarize(item, start, end, ZONE, rows, bids, positions)


@pytest.mark.parametrize(
    ("days", "patch", "expected"),
    [
        (7, {}, "LOWER_BID"),
        (14, {}, "LOWER_BID"),
        (7, {"context": "demand changed"}, "INSUFFICIENT_DATA"),
        (7, {"leads": 1}, "INSUFFICIENT_DATA"),
        (7, {"missing_day": True}, "INSUFFICIENT_DATA"),
        (7, {"rank": 9}, "KEEP_BID"),
    ],
)
def test_optimizer_matched_periods(finance, days, patch, expected):
    _, item, info, _, samples = finance
    item = live_item(item)
    earlier = period(item, info, samples, days, TODAY - days * DAY, 2400, 8000, **patch)
    recent = period(item, info, samples, days, TODAY, 3000, 12000)
    recommendation = PerformanceOptimizer(10).recommend(item, 3000, recent, earlier, NOW)
    assert recommendation.recommendation_type == expected
    assert recommendation.analysis_period == (TODAY - days * 2 * DAY, TODAY)
    if expected == "LOWER_BID":
        assert recommendation.suggested_value == 2400
        assert "требуют проверки" in recommendation.expected_effect


def test_allocator_preserves_total_and_bounds(finance):
    _, item, info, _, samples = finance
    receiver = live_item(item, maximum_daily_budget=10400)
    donor = live_item(item, item_id=2, minimum_daily_budget=9700)
    entries = [
        (
            article,
            period(article, info, samples, 7, TODAY, 2400, amount),
            period(article, info, samples, 7, TODAY - 7 * DAY, 2400, amount),
        )
        for article, amount in ((receiver, 6000), (donor, 16000))
    ]
    allocator = BudgetAllocator(10)
    assert allocator.allocate(entries, 19999, NOW) == []
    assert {r.item_id: r.suggested_value for r in allocator.allocate(entries, 20000, NOW)} == {
        1: 10300,
        2: 9700,
    }


async def test_optimizer_conflict_is_hold_and_budget_has_priority(finance, database):
    settings, item, info, spending, samples = finance
    _, sessions, repository = database
    item = live_item(item)
    await repository.save_item(item)
    info = info.model_copy(
        update={"capabilities": info.capabilities.model_copy(update={"model": PromotionModel.CPX})}
    )
    provider = MockBidProvider({item.item_id: info}, clock=lambda: NOW)
    service = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: NOW)
    spending = spending.model_copy(update={"data_type": "actual"})
    async with sessions.begin() as session:
        session.add(
            RecommendationRow(
                item_id=item.item_id,
                payload={
                    "group": "bid",
                    "period_key": "current",
                    "analysis_period": [(TODAY - 14 * DAY).isoformat(), TODAY.isoformat()],
                    "recommendation_type": "LOWER_BID",
                    "data_quality": "COMPARABLE",
                    "current_value": info.bid,
                    "reason_code": "LOWER_CPL",
                    "item_config": item.model_dump(mode="json", exclude={"title"}),
                },
            )
        )
    observations = samples(provider="manual")
    decision, status = await service.evaluate(item, observations, spending)
    assert (decision.reason_code, status) == ("STRATEGY_CONFLICT", "HOLD")
    assert decision.limits["performance_reason"] == "LOWER_CPL"
    spent = spending.model_copy(update={"account_spent": 100000})
    denied, _ = await service.evaluate(item, observations, spent)
    assert denied.reason == "ACCOUNT_BUDGET_GUARD"
    assert provider.write_count == 0


def test_floor_review_only_in_dry_run_with_fresh_evidence(finance):
    settings, item, info, spending, samples = finance
    restored = AppliedChange(
        applied_at=NOW - 15 * DAY, old_bid=1900, new_bid=2000, restore_bid=2000
    )
    engine = BiddingEngine(settings)
    strong = samples((2, 2, 2, 2, 2))
    for mode, rows, spend, expected in (
        (Mode.ASSISTED, strong, spending, "EFFECTIVE_FLOOR_REACHED"),
        (Mode.AUTO, strong, spending, "EFFECTIVE_FLOOR_REACHED"),
        (Mode.DRY_RUN, strong[-3:], spending, "EFFECTIVE_FLOOR_REACHED"),
        (Mode.DRY_RUN, strong, None, "BUDGET_DATA_UNAVAILABLE"),
        (Mode.DRY_RUN, strong, spending, "FLOOR_REVIEW_PROBE"),
    ):
        decision = engine.decide(item, info, rows, spend, NOW, applied=(restored,), mode=mode)
        assert decision.reason_code == expected
    recent = restored.model_copy(update={"applied_at": NOW - 2 * DAY})
    assert (
        engine.decide(
            item,
            info,
            strong,
            spending,
            NOW,
            applied=(recent,),
            mode=Mode.DRY_RUN,
        ).reason_code
        == "EFFECTIVE_FLOOR_REACHED"
    )


async def test_recommendations_and_reports_persist_without_applying(finance, database):
    settings, item, _, spending, _ = finance
    _, sessions, repository = database
    item = live_item(item)
    await repository.save_item(item)
    analytics = AnalyticsService(repository, settings, clock=lambda: NOW)
    # Validation of whole-account observed spending is independent of historical metrics.
    with pytest.raises(ValueError, match="one fresh account cut"):
        await import_statistics(analytics, StatisticsImport(spendings=(spending,)), now=NOW)
    trusted = spending.model_copy(update={"data_type": "actual", "item_spent": 123})
    trusted = trusted.model_copy(update={"account_spent": 133})
    rows = [metric(item, TODAY - DAY, "spend", 123)]
    await import_statistics(
        analytics, StatisticsImport(metrics=tuple(rows), spendings=(trusted,)), now=NOW
    )
    await analytics.refresh()
    await AnalyticsService(repository, settings, clock=lambda: NOW).refresh()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RecommendationRow)) == 2
        summary = await session.get(RuntimeRow, "phase5_daily_report")
        assert summary.payload["items"][0]["verified_spend"] == 123
        assert await session.scalar(select(func.count()).select_from(OperationRow)) == 0


async def test_collection_keeps_mock_separate_and_missing_values_unknown(finance, database):
    settings, item, _, _, _ = finance
    _, sessions, repository = database

    class FakeAPI:
        client = SimpleNamespace(offline=True)

        async def daily_item_metrics(self, account, ids, day):
            assert (account, ids) == (7, [1])
            return {1: {"views": 100, "spend": 1234}}

    store = StatisticsStore(sessions)
    collector = StatisticsCollector(FakeAPI(), 7, store, settings, clock=lambda: NOW)
    await collector.collect([item])
    await collector.collect([item])
    start, end = day_bounds(TODAY, ZONE)
    raw = await store.latest_metrics(1, start, end, NOW)
    assert {r.data_type for r in raw} == {"test"}
    assert next(r for r in raw if r.metric == "contacts").value is None
    (report,) = await AnalyticsService(repository, settings, clock=lambda: NOW).reports(0)
    assert report["actual"].spend is None
    assert report["provisional"].spend is None
    assert report["spending"] is None


async def test_statistics_normalization_uses_account_total_and_paginates(monkeypatch):
    from app.avito.statistics import StatisticsAPI

    monkeypatch.setattr(
        "app.avito.statistics.dates",
        lambda start, end: {"dateFrom": start.isoformat(), "dateTo": end.isoformat()},
    )
    calls = []

    class Client:
        async def operation(self, name, *, path_values, body):
            assert path_values == {"user_id": 7}
            assert body["dateFrom"] == body["dateTo"] == TODAY.isoformat()
            calls.append((name, body))
            if name == "itemAnalytics":
                item_id = 9 if body["offset"] == 0 else 1
                return {
                    "result": {
                        "dataTotalCount": 2,
                        "timestamp": "2026-01-20T10:00:00Z",
                        "groupings": [
                            {
                                "id": item_id,
                                "type": "item",
                                "metrics": [
                                    {"slug": "spending", "value": 10009},
                                    {"slug": "promoSpending", "value": 1000},
                                    {"slug": "contactsShowPhone", "value": 2},
                                ],
                            }
                        ],
                    }
                }
            assert name == "accountSpendings"
            return {
                "result": {
                    "timestamp": 1768903200,
                    "groupings": [
                        {
                            "date": TODAY.isoformat(),
                            "type": "day",
                            "spendings": [
                                {"slug": "all", "value": 31309},
                                {"slug": "promotion", "value": 4000},
                            ],
                        }
                    ],
                }
            }

    adapter = StatisticsAPI(Client())
    assert await adapter.daily_item_metrics(7, [1], TODAY) == {
        1: {"spend": 10009, "promotion_spend": 1000, "phone_reveals": 2}
    }
    assert await adapter.daily_account_spend(7, TODAY) == 31309
    assert [body["offset"] for name, body in calls if name == "itemAnalytics"] == [0, 1]
    assert "filter" not in calls[-1][1]


async def test_collector_only_revisits_recent_days(finance, database):
    settings, item, _, _, _ = finance
    _, sessions, _ = database
    settings = settings.model_copy(update={"statistics_backfill_days": 4})
    calls = []

    class FakeAPI:
        client = SimpleNamespace(offline=True)

        async def daily_item_metrics(self, account, ids, day):
            calls.append(day)
            return {1: {"spend": 100}}

    collector = StatisticsCollector(
        FakeAPI(),
        7,
        StatisticsStore(sessions),
        settings,
        clock=lambda: NOW,
    )
    await collector.collect([item])
    assert calls == [TODAY - offset * DAY for offset in range(5)]
    calls.clear()
    await collector.collect([item])
    assert calls == [TODAY - offset * DAY for offset in range(3)]


def test_strategies_replay_identical_input_without_financial_counterfactual(finance):
    settings, item, info, spending, samples = finance
    data = [
        ReplayFrame(
            now=now,
            observations=samples(positions, now=now),
            bid_info=info.model_copy(update={"checked_at": now}),
            spending=spending.model_copy(
                update={
                    "checked_at": now,
                    "data_as_of": now - MIN,
                    "item_spent": amount,
                    "account_spent": amount,
                }
            ),
        )
        for now, positions, amount in (
            (NOW, (12, 12), 6500),
            (NOW + 10 * MIN, (12, 12), 7500),
            (NOW + 20 * MIN, (5, 5, 5), 7600),
        )
    ]
    baseline = backtest(BiddingEngine(settings), item, info, data, position_origin="fixture")
    conservative = backtest(
        BiddingEngine(settings.model_copy(update={"budget_reserve_percent": 30})),
        item,
        info,
        data,
        position_origin="fixture",
    )
    assert [row["action"] for row in baseline] == ["INCREASE", "INCREASE", "DECREASE"]
    assert [row["action"] for row in conservative] == ["INCREASE", "HOLD", "HOLD"]
    for result in (baseline, conservative):
        assert all(row["simulated"] and row["position_origin"] == "fixture" for row in result)
        assert all("savings" not in row for row in result)


async def test_floor_review_throttle_survives_service_restart(finance, database):
    settings, item, info, spending, samples = finance
    _, sessions, repository = database
    failure_time = (NOW - 15 * DAY).isoformat()
    async with sessions.begin() as session:
        failed = DecisionRow(
            item_id=item.item_id,
            created_at=failure_time,
            schema_version=2,
            payload={"old_bid": 1900, "restore_bid": 2000},
        )
        session.add(failed)
        await session.flush()
        session.add(
            OperationRow(
                id="historical-recovery",
                item_id=item.item_id,
                decision_id=failed.id,
                requested_bid=2000,
                confirmed_bid=2000,
                status="SIMULATED",
                created_at=failure_time,
                applied_at=failure_time,
            )
        )
    provider = MockBidProvider({1: info}, clock=lambda: NOW)
    rows = samples((2, 2, 2, 2, 2))
    first = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: NOW)
    decision, status = await first.evaluate(item, rows, spending)
    assert (decision.reason_code, status) == ("FLOOR_REVIEW_PROBE", "DRY_RUN")
    restarted = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: NOW)
    repeated, status = await restarted.evaluate(item, rows, spending)
    assert (repeated.reason_code, status) == ("FLOOR_REVIEW_ALREADY_PROPOSED", "HOLD")
    assert provider.write_count == 0
    assert any(row.get("restore_bid") == 2000 for row in await repository.decisions(item.item_id))


async def test_telegram_reports_and_budget_settings(finance, database, monkeypatch):
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from app.telegram.bot import make_dispatcher

    settings, item, _, spending, _ = finance
    _, sessions, repository = database
    settings = settings.model_copy(update={"telegram_admin_ids": [44]})
    item = live_item(item)
    await repository.save_item(item)
    store = StatisticsStore(sessions)
    manual = spending.model_copy(
        update={
            "item_spent": 8123,
            "account_spent": 9321,
            "data_type": "actual",
        }
    )
    await store.append(
        [
            metric(item, TODAY - i * DAY, kind, value)
            for i in range(1, 8)
            for kind, value in (("spend", 1000), ("views", 20), ("contacts", 2))
        ]
        + [
            metric(
                item,
                TODAY - DAY,
                "spend",
                394556,
                data_type="test",
                source="test-source",
            )
        ],
        [manual],
    )
    service = AnalyticsService(repository, settings, clock=lambda: NOW)
    dispatcher = make_dispatcher(settings, repository, analytics=service)
    answers = []
    keyboards = []

    async def capture(message, text, **kwargs):
        answers.append(text)
        keyboards.append(kwargs.get("reply_markup"))

    monkeypatch.setattr(Message, "answer", capture)

    async def acknowledge(*args, **kwargs):
        return None

    monkeypatch.setattr(CallbackQuery, "answer", acknowledge)
    monkeypatch.setattr(Message, "edit_text", capture)
    async with Bot("42:TEST") as bot:

        async def send(text, actor=44):
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=Message(
                        message_id=1,
                        date=NOW,
                        text=text,
                        chat=Chat(id=actor, type="private"),
                        from_user=User(id=actor, is_bot=False, first_name="Test"),
                    ),
                ),
            )

        async def confirm():
            user = User(id=44, is_bot=False, first_name="Test")
            callback = CallbackQuery(
                id="confirmation",
                from_user=user,
                chat_instance="tests",
                data=keyboards[-1].inline_keyboard[0][0].callback_data,
                message=Message(
                    message_id=1,
                    date=NOW,
                    chat=Chat(id=44, type="private"),
                    from_user=user,
                ),
            )
            await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))

        await send("/analytics 7 1", actor=3)
        assert not answers
        await send("/analytics 7 1")
        assert "81.23 ₽" in answers[-1] and "CPL: 5.00 ₽" in answers[-1]
        assert "80%" in answers[-1] and "3945.56" not in answers[-1]
        await send("/compare 7")
        assert "расходы 70.00 ₽" in answers[-2]
        await send("/analytics 14 1")
        assert "CPL: нет данных" in answers[-1]
        await send("/budget 1 12500")
        assert (await repository.item(1)).daily_budget == 10000
        await confirm()
        assert (await repository.item(1)).daily_budget == 12500
        await send("/pause 1")
        assert "кабинете Авито" in answers[-1]
        assert (await repository.item(1)).active
        await confirm()
        assert not (await repository.item(1)).active
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OperationRow)) == 0
