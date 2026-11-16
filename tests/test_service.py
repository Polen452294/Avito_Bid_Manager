import pytest
from sqlalchemy import select

from app.avito.bidding import MockBidProvider
from app.bidding.engine import BiddingEngine
from app.database.models import OperationRow
from app.domain.enums import Mode
from app.services.bidding_service import BiddingService


@pytest.mark.parametrize("mode", [Mode.DRY_RUN, Mode.ASSISTED, Mode.AUTO])
async def test_no_write_without_confirmation(case, database, mode):
    s, item, info, spent, _, make = case
    provider = MockBidProvider({1: info})
    service = BiddingService(database[1], provider, BiddingEngine(s))
    await service.evaluate(item, make(), spent, mode=mode)
    assert provider.write_count == 0
    async with database[1]() as session:
        row = await session.scalar(select(OperationRow))
        assert row.confirmed_bid is None


async def test_dry_run_even_with_confirmation(case, database):
    s, item, info, spent, _, make = case
    provider = MockBidProvider({1: info})
    await BiddingService(database[1], provider, BiddingEngine(s)).evaluate(
        item, make(), spent, owner_confirmed=True
    )
    assert provider.write_count == 0


async def test_timeout_reconciles_without_resend(case, database):
    class TimeoutAfterWrite(MockBidProvider):
        async def set_bid(self, *args):
            await super().set_bid(*args)
            raise TimeoutError()

    s, item, info, spent, _, make = case
    provider = TimeoutAfterWrite({1: info})
    _, status = await BiddingService(database[1], provider, BiddingEngine(s)).evaluate(
        item, make(), spent, mode=Mode.ASSISTED, owner_confirmed=True
    )
    assert status == "SIMULATED_RECONCILED"
    assert provider.write_count == 1


async def test_five_item_limit_and_independent_pause(case, database):
    repo = database[2]
    for item_id in range(2, 6):
        await repo.save_item(case[1].model_copy(update={"item_id": item_id}))
    with pytest.raises(ValueError):
        await repo.save_item(case[1].model_copy(update={"item_id": 6}))
    await repo.save_item(case[1].model_copy(update={"active": False}))
    items = await repo.items()
    assert not items[0].active
    assert all(i.active for i in items[1:])


async def test_recovery_blocks_uncertain_item(case, database):
    s, item, info, spent, _, make = case
    provider = MockBidProvider({1: info})
    service = BiddingService(database[1], provider, BiddingEngine(s))
    await service.evaluate(item, make(), spent)
    async with database[1].begin() as session:
        row = await session.scalar(select(OperationRow))
        row.status = "PENDING"
    assert await database[2].recover() == 1
    decision, _ = await service.evaluate(item, make(), spent)
    assert decision.reason == "FAIL_SAFE"
    assert provider.write_count == 0


async def test_mock_idempotence(case):
    provider = MockBidProvider({1: case[2]})
    first = await provider.set_bid(1, 2200, "id")
    assert await provider.set_bid(1, 2200, "id") == first
    assert provider.write_count == 1
    with pytest.raises(ValueError):
        await provider.set_bid(1, 2300, "id")


async def test_confirmed_history_survives_service_restart(case, database):
    s, item, info, spent, _, make = case
    provider = MockBidProvider({1: info})
    first = BiddingService(database[1], provider, BiddingEngine(s))
    await first.evaluate(item, make(), spent, mode=Mode.ASSISTED, owner_confirmed=True)
    restarted = BiddingService(database[1], provider, BiddingEngine(s))
    decision, _ = await restarted.evaluate(
        item, make(), spent, mode=Mode.ASSISTED, owner_confirmed=True
    )
    assert decision.reason == "COOLDOWN"
    assert provider.write_count == 1
