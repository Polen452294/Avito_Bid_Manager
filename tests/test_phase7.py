import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from argparse import Namespace
from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Update
from sqlalchemy import select

from app.analytics.store import StatisticsStore
from app.avito.diagnostics import diagnose
from app.bidding.engine import BiddingEngine
from app.cli import check_account
from app.database.models import BudgetRow, DecisionRow, OperationRow, RuntimeRow
from app.database.session import backup_database
from app.main import run
from app.positions.history import PositionHistory
from app.services.bidding_service import BiddingService
from app.services.readiness import ReadinessService
from app.telegram.bot import make_dispatcher


async def test_crash_intent_and_wal_restore(tmp_path, database):
    _, sessions, repository = database
    source = tmp_path / "test.db"
    child = """
import asyncio
import os
import sys
from app.avito.journal import OperationJournal
from app.database.session import make_database

async def crash():
    _, sessions = make_database(sys.argv[1])
    await OperationJournal(sessions).intent("crash", 1, 1234, {"model": "CPX"})
    os._exit(73)

asyncio.run(crash())
"""
    result = subprocess.run(
        [sys.executable, "-c", child, f"sqlite+aiosqlite:///{source.as_posix()}"],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 73, result.stderr
    async with sessions.begin() as session:
        session.add(RuntimeRow(key="backup:test", payload={"daily_limit_kopecks": 12345}))
    backup = tmp_path / "snapshot.db"
    restored = tmp_path / "restored.db"
    await backup_database(source, backup)
    await backup_database(backup, restored)
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT status, requested_bid FROM bid_change_operations").fetchone() == (
            "PENDING",
            1234,
        )
        assert db.execute("SELECT count(*) FROM bid_decisions").fetchone() == (1,)
        assert db.execute(
            "SELECT json_extract(payload, '$.daily_limit_kopecks') "
            "FROM runtime_states WHERE key='backup:test'"
        ).fetchone() == (12345,)
    assert await repository.recover() == 1
    assert await repository.recover() == 1
    async with sessions() as session:
        assert (await session.get(OperationRow, "crash")).status == "UNKNOWN"
        assert await session.get(RuntimeRow, "fail_safe:1")


async def test_bad_source_preserves_previous_backup(tmp_path, database):
    corrupted = tmp_path / "invalid.db"
    corrupted.write_bytes(b"invalid")
    good = tmp_path / "good.db"
    await backup_database(tmp_path / "test.db", good)
    before = good.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        await backup_database(corrupted, good)
    assert good.read_bytes() == before


async def test_readiness_uses_migrated_database_and_live_evidence(tmp_path, case):
    settings = case[0].model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'real.db').as_posix()}",
            "avito_client_id": None,
            "avito_client_secret": None,
        }
    )
    absent = await ReadinessService(settings).evaluate()
    assert not absent["levels"]["SIMULATION_READY"]["ready"]
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": settings.database_url, "MODE": "DRY_RUN"},
        check=True,
        timeout=20,
        capture_output=True,
    )
    evidence = await diagnose(settings)
    assert evidence["capabilities"]["bid_write"] == "UNVERIFIED"
    assert evidence["last_successful_check_at"] is None
    evidence.update(
        simulated=True, capabilities=dict.fromkeys(evidence["capabilities"], "AVAILABLE")
    )
    report = await ReadinessService(settings).evaluate(evidence)
    assert report["runtime"]["database"] == "AVAILABLE"
    assert report["levels"]["SIMULATION_READY"]["ready"]
    assert not any(
        level["ready"] for name, level in report["levels"].items() if name != "SIMULATION_READY"
    )


async def test_five_item_virtual_days_share_spending_cuts(case, database):
    from app.avito.bidding import MockBidProvider

    settings, item, bid, spending, _, samples = case
    _, sessions, repository = database
    history = PositionHistory(sessions)
    store = StatisticsStore(sessions)
    items = [item.model_copy(update={"item_id": number}) for number in range(1, 6)]
    for listing in items:
        await repository.save_item(listing)
    with pytest.raises(ValueError):
        await repository.save_item(item.model_copy(update={"item_id": 6}))
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    now = start
    provider = MockBidProvider({}, clock=lambda: now)
    services = [
        BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: now)
        for _ in items
    ]
    for tick in range(12):
        now = start + timedelta(hours=tick * 6)
        period = now.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
        for listing in items:
            item_id = listing.item_id
            await history.append(
                [
                    row.model_copy(
                        update={"item_id": item_id, "checked_at": now - timedelta(minutes=10 * n)}
                    )
                    for n, row in enumerate(reversed(samples()))
                ]
            )
            provider.bids[item_id] = bid.model_copy(
                update={
                    "item_id": item_id,
                    "checked_at": now,
                    "capabilities": bid.capabilities.model_copy(
                        update={"expires_at": now + timedelta(hours=1)}
                    ),
                }
            )
        await store.append(
            [],
            [
                spending.model_copy(
                    update={
                        "item_id": listing.item_id,
                        "checked_at": now,
                        "period_date": period,
                        "reliable": tick % 2 == 0,
                    }
                )
                for listing in items
            ],
        )
        results = await asyncio.gather(
            *(
                service.evaluate_new_history(listing, history, managed=True)
                for service, listing in zip(services, items, strict=True)
            ),
            services[0].evaluate_new_history(items[0], history, managed=True),
        )
        decisions = [decision for decision, _ in filter(None, results)]
        assert len(decisions) == 5
        proposed = [decision for decision in decisions if decision.new_bid is not None]
        assert len(proposed) == (1 if tick % 2 == 0 else 0)
        assert all(item.min_bid <= decision.new_bid <= item.max_bid for decision in proposed)
    assert provider.write_count == 0
    async with sessions() as session:
        assert len(list(await session.scalars(select(DecisionRow)))) == 60


async def test_migration_failure_rolls_back_and_keeps_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    monkeypatch.setenv("MODE", "DRY_RUN")
    monkeypatch.setenv("BACKUP_RETENTION", "1")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE legacy (value TEXT)")
        db.execute("INSERT INTO legacy VALUES ('retained')")

    def failed_upgrade(config, revision):
        config.attributes["connection"].exec_driver_sql("CREATE TABLE partial (id INTEGER)")
        raise RuntimeError("injected migration error")

    monkeypatch.setattr("app.main.command.upgrade", failed_upgrade)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="injected migration error"):
            await run(Namespace(command="serve", migrate=True))
    snapshots = list((tmp_path / "backups").glob("before-migration-*.db"))
    assert len(snapshots) == 1
    for database_path in [path, *snapshots]:
        with closing(sqlite3.connect(database_path)) as db:
            assert db.execute("SELECT name FROM sqlite_master").fetchall() == [("legacy",)]
            assert db.execute("SELECT value FROM legacy").fetchone() == ("retained",)


async def test_diagnostic_history_retains_timestamp_without_confirming_current_access(
    tmp_path, case, database, monkeypatch
):
    settings = case[0].model_copy(
        update={"database_url": (f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")}
    )
    from pydantic import SecretStr

    settings.avito_client_id = SecretStr("mock-client")
    stamp = "2026-01-01T08:00:00+00:00"
    reports = iter(
        [
            {"last_successful_check_at": stamp, "status": "READ_CHECKS_PASSED"},
            {"last_successful_check_at": None, "status": "BLOCKED"},
        ]
    )

    async def probe(settings):
        return next(reports)

    monkeypatch.setattr("app.cli.diagnose", probe)
    assert (await check_account(settings))["last_successful_check_at"] == stamp
    current = await check_account(settings)
    assert current == {"last_successful_check_at": stamp, "status": "BLOCKED"}


async def test_managed_proposal_checks_the_persisted_budget(case, database):
    from app.avito.bidding import MockBidProvider

    settings, item, bid, spending, now, samples = case
    _, sessions, _ = database
    async with sessions.begin() as session:
        session.add(BudgetRow(key="account", limit_kopecks=10))
    provider = MockBidProvider({item.item_id: bid}, clock=lambda: now)
    service = BiddingService(sessions, provider, BiddingEngine(settings), clock=lambda: now)
    decision, status = await service.evaluate(item, samples(), spending, managed=True)
    assert status == "HOLD"
    assert decision.reason_code == "ACCOUNT_BUDGET_CHANGED"
    assert provider.write_count == 0


async def test_telegram_dispatcher_sends_only_to_admin_through_mock_api(case, database):
    class TelegramAPI(BaseSession):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def make_request(self, bot, method, timeout=None):
            assert isinstance(method, SendMessage)
            self.requests.append(method)
            return self.check_response(
                bot,
                method,
                200,
                json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "message_id": 2,
                            "date": 1767254400,
                            "chat": {"id": method.chat_id, "type": "private"},
                            "text": method.text,
                        },
                    }
                ),
            ).result

        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            raise AssertionError("Unexpected Telegram download")

    settings = case[0].model_copy(update={"telegram_admin_ids": [44]})
    _, _, repository = database
    dispatcher = make_dispatcher(settings, repository)
    api = TelegramAPI()
    async with Bot("123456:mock-token", session=api) as bot:
        for chat_id, chat_type, actor in [
            (55, "private", 55),
            (-1, "group", 44),
            (44, "private", 44),
        ]:
            update = Update.model_validate(
                {
                    "update_id": chat_id,
                    "message": {
                        "message_id": 1,
                        "date": 1767254400,
                        "chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": actor, "is_bot": False, "first_name": "Test"},
                        "text": "/start",
                    },
                }
            )
            await dispatcher.feed_update(bot, update)
    assert [request.chat_id for request in api.requests] == [44]
    assert api.requests[0].reply_markup is not None
