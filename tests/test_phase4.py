import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.avito.bidding import MockBidProvider
from app.bidding.backtest import ReplayFrame, backtest
from app.bidding.engine import BiddingEngine
from app.database.models import BidSnapshotRow, DecisionRow, OperationRow, SpendingRow
from app.domain.enums import Mode, PositionStatus, PositionType
from app.domain.models import AppliedChange, SearchQuery
from app.positions.history import PositionHistory
from app.positions.monitor import PositionMonitor
from app.services.bidding_service import BiddingService

T0 = datetime(2026, 1, 5, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


@pytest.fixture
def phase(case):
    settings, item, info, spending, _, make = case
    info = info.model_copy(
        update={
            "checked_at": T0,
            "capabilities": info.capabilities.model_copy(
                update={"expires_at": T0 + 8 * MINUTE * 60}
            ),
        }
    )
    spending = spending.model_copy(update={"checked_at": T0, "period_date": "2026-01-05"})

    def samples(values=(12, 12), now=T0, **update):
        return [
            row.model_copy(update={"checked_at": now - (len(values) - i - 1) * MINUTE, **update})
            for i, row in enumerate(make(values))
        ]

    return settings, item, info, spending, samples


@pytest.mark.parametrize(
    ("values", "action", "code"),
    [
        ((8, 8), "INCREASE", "TARGET_BELOW_RANGE"),
        ((7, 7, 7), "HOLD", "TARGET_STABLE"),
        ((6, 6, 6), "DECREASE", "TARGET_STABLE"),
        ((3, 3, 3), "DECREASE", "TARGET_STABLE"),
        ((2, 2, 2), "DECREASE", "POSITION_ABOVE_TARGET"),
        ((15, 3, 15, 1), "HOLD", "INSUFFICIENT_CONFIRMATIONS"),
        ((), "HOLD", "POSITION_DATA_MISSING"),
    ],
)
def test_position_scenarios(phase, values, action, code):
    settings, item, info, spending, samples = phase
    decision = BiddingEngine(settings).decide(item, info, samples(values), spending, T0)
    assert decision.action == action
    assert decision.reason_code == code
    assert decision.authorization == (
        "NOT_APPLICABLE" if action == "HOLD" else "REQUIRES_PREFLIGHT"
    )


@pytest.mark.parametrize(
    ("changes", "age", "code"),
    [
        ({"confidence": Decimal("0.79")}, 1, "POSITION_DATA_UNRELIABLE"),
        ({"position": None, "status": PositionStatus.UNAVAILABLE}, 1, "POSITION_DATA_UNRELIABLE"),
        ({"provider": "unreviewed"}, 1, "SOURCE_NOT_VERIFIED"),
        ({"position_type": PositionType.UNVERIFIED}, 1, "POSITION_DATA_UNRELIABLE"),
        ({}, 31, "POSITION_DATA_STALE"),
    ],
)
def test_invalid_and_lost_source(phase, changes, age, code):
    settings, item, info, spending, samples = phase
    rows = samples(now=T0 - age * MINUTE, **changes)
    decision = BiddingEngine(settings).decide(item, info, rows, spending, T0)
    assert decision.reason_code == code
    assert decision.new_bid is None
    assert decision.observations


def test_coverage_matches_primary_weight_and_conflict(phase):
    settings, item, info, spending, samples = phase
    first = item.queries[0].model_copy(update={"weight": Decimal(3)})
    second = SearchQuery(text="secondary", region="r")
    item = item.model_copy(
        update={
            "queries": (first, second),
            "min_position_coverage": Decimal("0.75"),
        }
    )
    engine = BiddingEngine(settings)

    def calculate(rows):
        return engine.decide(item, info, rows, spending, T0)

    primary = samples()
    missing_secondary = calculate(primary)
    assert missing_secondary.coverage == Decimal("0.75")
    assert missing_secondary.new_bid == 2200
    assert calculate(samples(search_query="secondary")).reason_code == "POSITION_DATA_MISSING"
    secondary = samples((8, 8), search_query="secondary")
    assert calculate(primary + secondary).position_score == Decimal(11)
    assert calculate(primary + samples((2, 2, 2), search_query="secondary")).reason_code == (
        "CONFLICTING_QUERIES"
    )
    assert calculate(primary + samples((2,), search_query="secondary")).new_bid is None
    assert calculate(
        primary + samples(search_query="secondary", provider="manual")
    ).reason_code == ("INCOMPARABLE_OBSERVATIONS")


def test_step_caps_budget_and_api_values(phase):
    settings, item, info, spending, samples = phase
    caps = info.capabilities.model_copy(update={"increment": 1})
    info = info.model_copy(update={"capabilities": caps})
    engine = BiddingEngine(settings)

    def calculate(item, spending):
        return engine.decide(item, info, samples(), spending, T0)

    tiny = item.model_copy(update={"max_step_kopecks": 37, "max_step_percent": 1})
    assert calculate(tiny, spending).new_bid == 2020
    assert calculate(item.model_copy(update={"max_step_kopecks": 37}), spending).new_bid == 2037
    assert calculate(item.model_copy(update={"max_bid": 2001}), spending).new_bid == 2001
    assert calculate(item.model_copy(update={"max_bid": 2000}), spending).reason_code == (
        "MAX_BID_REACHED"
    )
    assert calculate(item, None).reason_code == "BUDGET_DATA_UNAVAILABLE"
    assert (
        calculate(item, spending.model_copy(update={"checked_at": T0 - 31 * MINUTE})).reason_code
        == "BUDGET_DATA_UNAVAILABLE"
    )
    for budget in ({"account_spent": 80000}, {"item_spent": 8000, "account_spent": 8000}):
        assert calculate(item, spending.model_copy(update=budget)).reason_code == "BUDGET_LIMIT"
    discrete = info.model_copy(
        update={"capabilities": caps.model_copy(update={"allowed_bids": (2000, 2500)})}
    )
    assert engine.decide(item, discrete, samples(), spending, T0).reason_code == (
        "UNSUPPORTED_BID_VALUE"
    )


def test_stabilization_requires_observation_period_after_change(phase):
    settings, item, info, spending, samples = phase
    item = item.model_copy(update={"cooldown_minutes": 2, "stabilization_minutes": 5})
    engine = BiddingEngine(settings)
    now = T0 + 10 * MINUTE
    rows = samples(now=now, period_start=T0 + MINUTE, period_end=T0 + 4 * MINUTE)
    decision = engine.decide(item, info, rows, spending, now, last_change=T0)
    assert decision.reason_code == "AWAITING_POST_STABILIZATION_DATA"
    assert (
        engine.decide(item, info, samples(now=now), spending, now, last_change=T0).new_bid == 2200
    )


def test_increase_interval_and_daily_count(phase):
    settings, item, info, spending, samples = phase
    engine = BiddingEngine(settings)
    item = item.model_copy(update={"cooldown_minutes": 1, "max_changes_per_day": 2})
    prior = AppliedChange(applied_at=T0 - 4 * MINUTE, old_bid=1900, new_bid=2000)
    decision = engine.decide(item, info, samples(), spending, T0, applied=(prior,))
    assert decision.reason_code == "INCREASE_INTERVAL_ACTIVE"
    second = AppliedChange(applied_at=T0 - 10 * MINUTE, old_bid=1800, new_bid=1900)
    assert (
        engine.decide(item, info, samples(), spending, T0, applied=(second, prior)).reason_code
        == "CHANGE_LIMIT"
    )


async def test_probe_restart_restoration_and_audit(phase, database):
    settings, item, info, spending, samples = phase
    _, sessions, repository = database
    item = item.model_copy(update={"cooldown_minutes": 2, "increase_interval_minutes": 2})
    await repository.save_item(item)
    now = T0
    clock = lambda: now  # noqa: E731
    provider = MockBidProvider({1: info}, clock=clock)

    async def calculate(values):
        provider.bids[1] = provider.bids[1].model_copy(update={"checked_at": now})
        service = BiddingService(sessions, provider, BiddingEngine(settings), clock=clock)
        return await service.evaluate(
            item,
            samples(values, now),
            spending.model_copy(update={"checked_at": now}),
            mode=Mode.ASSISTED,
            owner_confirmed=True,
        )

    probe, status = await calculate((5, 5, 5))
    assert (probe.new_bid, status) == (1900, "SIMULATED")
    now += 2 * MINUTE
    delayed, status = await calculate((12, 12))
    assert (delayed.reason_code, status) == ("AWAITING_POST_STABILIZATION_DATA", "HOLD")
    now += 5 * MINUTE
    restoration, status = await calculate((12, 12))
    assert (restoration.new_bid, restoration.restore_bid, status) == (2000, 2000, "SIMULATED")
    now += 7 * MINUTE
    hold, status = await calculate((4, 4, 4))
    assert (hold.reason_code, status) == ("EFFECTIVE_FLOOR_REACHED", "HOLD")
    assert provider.write_count == 2
    async with sessions() as session:
        operations = list(
            await session.scalars(select(OperationRow).order_by(OperationRow.created_at))
        )
        assert [datetime.fromisoformat(op.applied_at) for op in operations] == [T0, T0 + 7 * MINUTE]
        audits = list(await session.scalars(select(DecisionRow).order_by(DecisionRow.id)))
        assert audits[2].payload["authorization"] == "SIMULATED"
        assert len(audits[2].payload["observations"]) == 2
        assert audits[2].payload["expected_result"]
        assert audits[3].payload["reason_code"] == "EFFECTIVE_FLOOR_REACHED"
        assert {row.schema_version for row in audits} == {2}


async def test_concurrent_execution_and_auto_block(phase, database):
    settings, item, info, spending, samples = phase
    _, sessions, _ = database
    provider = MockBidProvider({1: info}, clock=lambda: T0)
    services = [
        BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: T0)
        for _ in range(2)
    ]
    _, status = await services[0].evaluate(
        item, samples(), spending, mode=Mode.AUTO, owner_confirmed=True
    )
    assert status == "AUTO_DISABLED"
    assert provider.write_count == 0

    results = await asyncio.gather(
        *(
            service.evaluate(item, samples(), spending, mode=Mode.ASSISTED, owner_confirmed=True)
            for service in services
        )
    )
    assert [status for _, status in results].count("SIMULATED") == 1
    assert provider.write_count == 1


async def test_unfinished_dispatch_reconciles_without_repeat(phase, database):
    settings, item, info, spending, samples = phase
    _, sessions, repository = database

    class LostConfirmation(MockBidProvider):
        offline = False

        async def set_bid(self, *args):
            await super().set_bid(*args)
            self.offline = True
            raise TimeoutError

        async def get_bid_info(self, item_id):
            if self.offline:
                raise TimeoutError
            return await super().get_bid_info(item_id)

    provider = LostConfirmation({1: info}, clock=lambda: T0)
    first = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: T0)
    _, status = await first.evaluate(
        item, samples(), spending, mode=Mode.ASSISTED, owner_confirmed=True
    )
    assert status == "UNKNOWN"
    assert await repository.recover() == 1
    provider.offline = False
    next_service = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: T0)
    decision, status = await next_service.evaluate(item, samples(), spending)
    assert decision.reason_code == "COOLDOWN_ACTIVE"
    assert status == "HOLD"
    assert provider.write_count == 1
    assert (await repository.decisions(1))[-1]["authorization"] == "SIMULATED_RECONCILED"


async def test_monitor_handles_five_items_only_once_and_honors_account_guard(phase, database):
    settings, item, info, spending, samples = phase
    _, sessions, repository = database
    history = PositionHistory(sessions)
    for item_id in range(1, 6):
        await repository.save_item(item.model_copy(update={"item_id": item_id}))
        await history.append(samples(item_id=item_id))
        async with sessions.begin() as session:
            session.add(
                BidSnapshotRow(
                    item_id=item_id,
                    payload=info.model_copy(update={"item_id": item_id}).model_dump(mode="json"),
                )
            )
            session.add(
                SpendingRow(
                    item_id=item_id,
                    payload=spending.model_copy(
                        update={"item_id": item_id, "account_spent": 80000}
                    ).model_dump(mode="json"),
                )
            )
    service = BiddingService(sessions, None, BiddingEngine(settings), clock=lambda: T0)

    async def evaluate(item):
        await service.evaluate_new_history(item, history)

    monitor = PositionMonitor(repository, history, (), evaluate=evaluate)
    await asyncio.gather(monitor.tick(), monitor.tick())
    await monitor.tick()
    for item_id in range(1, 6):
        rows = await repository.decisions(item_id)
        assert len(rows) == 1
        assert rows[0]["reason_code"] == "BUDGET_LIMIT"


def test_replay_distinguishes_recorded_and_hypothetical_bids(phase):
    settings, item, info, spending, samples = phase
    item = item.model_copy(update={"cooldown_minutes": 2})
    engine = BiddingEngine(settings)
    later = T0 + 8 * MINUTE
    frames = [
        ReplayFrame(now=T0, observations=samples((4, 4, 4)), spending=spending),
        ReplayFrame(now=later, observations=samples(now=later), spending=spending),
    ]
    historical = backtest(engine, item, info, frames)
    model = backtest(engine, item, info, frames, assume_applied=True, position_origin="fixture")
    assert historical[1]["old_bid"] == 2000
    assert historical[1]["bid_origin"] == "recorded"
    assert model[1]["old_bid"] == 1900
    assert model[1]["position_origin"] == "fixture"
    assert model[1]["bid_origin"] == "hypothetical_chain"
    assert model[1]["new_bid"] == 2000
    assert model == backtest(
        engine, item, info, frames, assume_applied=True, position_origin="fixture"
    )


def test_migrations_keep_legacy_audit_and_positions(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    path = tmp_path / "upgrade.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    monkeypatch.setenv("MODE", "DRY_RUN")
    alembic = Config("alembic.ini")
    alembic.set_main_option("path_separator", "os")
    command.upgrade(alembic, "0001")
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("INSERT INTO accounts (id) VALUES (1)")
            connection.execute(
                "INSERT INTO advertisements (item_id, account_id, config) VALUES (1, 1, '{}')"
            )
            connection.execute(
                "INSERT INTO search_queries (item_id, query, region, weight) "
                "VALUES (1, 'q', 'r', '1')"
            )
            for table in (
                "bid_decisions",
                "position_snapshots",
                "spending_snapshots",
                "performance_snapshots",
            ):
                connection.execute(
                    f"INSERT INTO {table} (item_id, created_at, payload) VALUES (1, ?, ?)",
                    (T0.isoformat(), '{"legacy":true}'),
                )
        command.upgrade(alembic, "0002")
        assert connection.execute("SELECT filters_key FROM search_queries").fetchone() == ("{}",)
        assert connection.execute(
            "SELECT payload, observation_key FROM position_snapshots"
        ).fetchone() == ('{"legacy":true}', None)
        command.upgrade(alembic, "0003")
        assert connection.execute(
            "SELECT schema_version, payload FROM bid_decisions"
        ).fetchone() == (1, '{"legacy":true}')
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0003",)
        command.upgrade(alembic, "0004")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0004",)
        assert connection.execute(
            "SELECT schema_version, payload FROM bid_decisions"
        ).fetchone() == (1, '{"legacy":true}')
        assert connection.execute("SELECT payload FROM position_snapshots").fetchone() == (
            '{"legacy":true}',
        )
        for table in ("spending_snapshots", "performance_snapshots"):
            assert connection.execute(f"SELECT payload FROM {table}").fetchone() == (
                '{"legacy":true}',
            )
        command.check(alembic)
    finally:
        connection.close()


async def test_telegram_strategy_history_budget_and_recommendation(phase, database, monkeypatch):
    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from app.telegram.bot import make_dispatcher

    settings, item, info, spending, samples = phase
    _, sessions, repository = database
    provider = MockBidProvider({1: info}, clock=lambda: T0)
    service = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: T0)
    dispatcher = make_dispatcher(
        settings.model_copy(update={"telegram_admin_ids": [44]}), repository, service
    )
    await PositionHistory(sessions).append(samples())
    async with sessions.begin() as session:
        session.add(SpendingRow(item_id=1, payload=spending.model_dump(mode="json")))
    answers = []
    keyboards = []

    async def answer(message, text, **kwargs):
        answers.append(text)
        keyboards.append(kwargs.get("reply_markup"))

    monkeypatch.setattr(Message, "answer", answer)

    async def acknowledge(*args, **kwargs):
        return None

    monkeypatch.setattr(CallbackQuery, "answer", acknowledge)
    monkeypatch.setattr(Message, "edit_text", Message.answer)
    async with Bot("42:TEST") as bot:

        async def send(text, user_id=44):
            update = Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=T0,
                    text=text,
                    chat=Chat(id=user_id, type="private"),
                    from_user=User(id=user_id, is_bot=False, first_name="Test"),
                ),
            )
            await dispatcher.feed_update(bot, update)

        async def confirm():
            user = User(id=44, is_bot=False, first_name="Test")
            callback = CallbackQuery(
                id="confirmation",
                from_user=user,
                chat_instance="tests",
                data=keyboards[-1].inline_keyboard[0][0].callback_data,
                message=Message(
                    message_id=1,
                    date=T0,
                    chat=Chat(id=44, type="private"),
                    from_user=user,
                ),
            )
            await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))

        await send('/strategy 1 {"target_min":4,"target_max":8,"max_step_kopecks":150}')
        assert (await repository.item(item.item_id)).max_step_kopecks == 1000
        assert "3 → 4" in answers[-1]
        await confirm()
        assert (await repository.item(item.item_id)).max_step_kopecks == 150
        await send("/recommend 1")
        assert "DRY_RUN · INCREASE · TARGET_BELOW_RANGE" in answers[-1]
        assert "2100 коп." in answers[-1]
        await send("/history 1")
        assert "TARGET_BELOW_RANGE" in answers[-1]
        await send("/budget 1")
        assert "100000 коп." in answers[-1]
        await send('/strategy 1 {"target_max":2}', user_id=3)
        assert len(answers) == 5
    assert provider.write_count == 0


@pytest.mark.parametrize("change", ["bid", "time"])
async def test_preflight_rechecks_current_bid_and_age(phase, database, change):
    settings, item, info, spending, samples = phase
    _, sessions, repository = database
    now = T0

    def clock():
        return now

    class ChangedBeforeExecution(MockBidProvider):
        reads = 0

        async def get_bid_info(self, item_id):
            nonlocal now
            self.reads += 1
            current = await super().get_bid_info(item_id)
            if self.reads == 2:
                if change == "bid":
                    return current.model_copy(update={"bid": 2100})
                now += 2 * MINUTE
            return current

    provider = ChangedBeforeExecution({1: info}, clock=clock)
    service = BiddingService(sessions, provider, BiddingEngine(settings), clock=clock)
    decision, status = await service.evaluate(
        item, samples(), spending, mode=Mode.ASSISTED, owner_confirmed=True
    )
    assert (decision.authorization, status) == ("PREFLIGHT_BLOCKED", "PREFLIGHT_BLOCKED")
    assert provider.write_count == 0
    assert (await repository.decisions(1))[0]["authorization"] == "PREFLIGHT_BLOCKED"


def test_accepted_probe_permits_a_second_small_reduction(phase):
    settings, item, info, spending, samples = phase
    item = item.model_copy(update={"cooldown_minutes": 2})
    engine = BiddingEngine(settings)
    now = T0 + 8 * MINUTE
    result = backtest(
        engine,
        item,
        info,
        [
            (T0, samples((5, 5, 5)), spending),
            (now, samples((5, 5, 5), now), spending),
        ],
        assume_applied=True,
        position_origin="fixture",
    )
    assert [(row["old_bid"], row["new_bid"]) for row in result] == [(2000, 1900), (1900, 1800)]
