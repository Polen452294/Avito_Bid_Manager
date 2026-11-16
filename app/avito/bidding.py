from datetime import UTC, datetime

from app.avito.exceptions import CapabilityBlocked
from app.domain.models import BidInfo, BidResult


class BlockedBidProvider:
    simulated = False

    async def get_bid_info(self, item_id: int) -> BidInfo:
        raise CapabilityBlocked("Official contract and account access are not verified")

    async def set_bid(self, item_id: int, bid: int, operation_id: str) -> BidResult:
        raise CapabilityBlocked("Live writes are disabled in this release")


class MockBidProvider:
    """Explicit local simulator, never connects to Avito."""

    simulated = True

    def __init__(self, bids: dict[int, BidInfo], *, clock=None):
        self.bids = dict(bids)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.operations: dict[str, BidResult] = {}
        self.write_count = 0

    async def get_bid_info(self, item_id: int) -> BidInfo:
        return self.bids[item_id]

    async def set_bid(self, item_id: int, bid: int, operation_id: str) -> BidResult:
        if operation_id in self.operations:
            result = self.operations[operation_id]
            if (result.item_id, result.requested_bid) != (item_id, bid):
                raise ValueError("Operation id reused with different payload")
            return result
        info = self.bids[item_id]
        now = self.clock()
        if not info.capabilities.can_write or not info.capabilities.allows(bid, now):
            raise CapabilityBlocked("Unsupported bid")
        result = BidResult(
            item_id=item_id,
            requested_bid=bid,
            confirmed_bid=bid,
            status="SIMULATED",
            simulated=True,
        )
        self.bids[item_id] = info.model_copy(update={"bid": bid, "checked_at": now})
        self.operations[operation_id] = result
        self.write_count += 1
        return result
