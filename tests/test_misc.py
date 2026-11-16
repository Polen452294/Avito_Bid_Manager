from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analytics.metrics import metrics
from app.telegram.bot import AdminOnly


@pytest.mark.parametrize(
    ("user", "chat", "allowed"), [(1, "private", True), (2, "private", False), (1, "group", False)]
)
async def test_telegram_guard(user, chat, allowed):
    handler = AsyncMock()
    event = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(type=chat))
    await AdminOnly([1])(handler, event, {})
    assert handler.await_count == int(allowed)


def test_metrics_zero_and_unknown():
    assert metrics(100, 0, 0)["cpl_rubles"] is None
    assert metrics(100, 10, None)["conversion_percent"] is None
    assert metrics(100, 10, 2)["cpl_rubles"] == __import__("decimal").Decimal("0.5")


async def test_backup(database, tmp_path):
    import sqlite3

    from app.database.session import backup_database

    await backup_database(tmp_path / "test.db", tmp_path / "backup.db")
    with sqlite3.connect(tmp_path / "backup.db") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM advertisements").fetchone()[0] == 1


def test_backtest_has_no_network(case):
    from app.bidding.backtest import backtest
    from app.bidding.engine import BiddingEngine

    s, item, info, spending, now, positions = case
    result = backtest(BiddingEngine(s), item, info, [(now, positions(), spending)])
    assert result[0]["simulated"]
    assert result[0]["new_bid"] == 2200
