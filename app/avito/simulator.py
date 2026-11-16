"""SQLite-backed, explicitly configured simulator for the long-running service."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.avito.bidding import MockBidProvider
from app.avito.exceptions import CapabilityBlocked
from app.database.models import AdvertisementRow, RuntimeRow
from app.domain.enums import PromotionModel
from app.domain.models import Advertisement, BidCapabilities, BidInfo, BidResult


class LocalSimulator(MockBidProvider):
    def __init__(self, sessions):
        super().__init__({})
        self.sessions = sessions

    async def _info(self, session, item_id):
        row = await session.get(AdvertisementRow, item_id)
        if row is None or row.account_id != 1:
            raise CapabilityBlocked("Simulator item missing")
        item = Advertisement.model_validate(row.config)
        if item.promotion_model != PromotionModel.MOCK or item.confirmed_bid is None:
            raise CapabilityBlocked("Simulator needs an explicit starting bid")
        saved = await session.get(RuntimeRow, f"simulator:bid:{item_id}")
        now = datetime.now(UTC)
        return BidInfo(
            item_id=item_id,
            bid=saved.payload["bid"] if saved else item.confirmed_bid,
            checked_at=now,
            source="local-simulator",
            capabilities=BidCapabilities(
                model=PromotionModel.MOCK,
                verified=True,
                can_write=True,
                minimum=item.min_bid,
                maximum=item.max_bid,
                increment=1,
                expires_at=now + timedelta(minutes=15),
            ),
        )

    async def get_bid_info(self, item_id, *, session=None):
        if session is not None:
            return await self._info(session, item_id)
        async with self.sessions() as session:
            return await self._info(session, item_id)

    async def apply_in_session(self, session, item_id, bid, operation_id):
        key = f"simulator:operation:{operation_id}"
        saved = await session.get(RuntimeRow, key)
        if saved:
            result = BidResult.model_validate(saved.payload)
            if (result.item_id, result.requested_bid) != (item_id, bid):
                raise CapabilityBlocked("Operation payload mismatch")
            return result
        info = await self._info(session, item_id)
        if not info.capabilities.allows(bid, info.checked_at):
            raise CapabilityBlocked("Simulator bid outside limits")
        result = BidResult(
            item_id=item_id,
            requested_bid=bid,
            confirmed_bid=bid,
            status="SIMULATED",
            simulated=True,
        )
        session.add(RuntimeRow(key=key, payload=result.model_dump(mode="json")))
        await session.merge(RuntimeRow(key=f"simulator:bid:{item_id}", payload={"bid": bid}))
        return result

    async def set_bid(self, item_id, bid, operation_id):
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            return await self.apply_in_session(session, item_id, bid, operation_id)

    async def has_receipt(self, operation_id):
        async with self.sessions() as session:
            return await session.get(RuntimeRow, f"simulator:operation:{operation_id}") is not None
