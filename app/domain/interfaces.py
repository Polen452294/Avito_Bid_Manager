from typing import Protocol

from app.domain.models import BidInfo, BidResult, PositionSnapshot, SearchQuery, Spending


class BidProvider(Protocol):
    simulated: bool

    async def get_bid_info(self, item_id: int) -> BidInfo: ...
    async def set_bid(self, item_id: int, bid: int, operation_id: str) -> BidResult: ...


class PositionProvider(Protocol):
    async def observe(self, item_id: int, query: SearchQuery) -> PositionSnapshot: ...


class StatisticsProvider(Protocol):
    async def spending(self, item_id: int) -> Spending: ...
