import asyncio
from datetime import UTC, datetime, timedelta

from app.avito.accounts import positive_id
from app.avito.exceptions import (
    AccessDenied,
    AvitoError,
    CapabilityBlocked,
    InvalidRequest,
    InvalidResponse,
    ModelUnavailable,
    UncertainWrite,
    VerificationFailed,
)
from app.avito.schemas import AuctionBids, CpxBids, parse
from app.domain.enums import PromotionModel
from app.domain.models import BidCapabilities, BidInfo, BidResult


class AuditedProvider:
    """Actual wire adapter; network writes stay disabled. Mock-only wire execution is audited."""

    def __init__(self, client, journal=None):
        self.client, self.journal = client, journal
        self.simulated = client.offline
        self._lock = asyncio.Lock()

    async def _apply(self, item_id, bid, operation_id):
        positive_id(item_id)
        if type(bid) is not int or bid < 0:
            raise InvalidRequest("Bid must be a non-negative integer number of kopecks")
        if not self.client.offline:
            raise CapabilityBlocked("Paid network operations are disabled; read-only release")
        if self.journal is None:
            raise CapabilityBlocked("Durable operation journal is required")
        async with self._lock:
            previous = await self.journal.lookup(operation_id, item_id, bid)
            if previous:
                _, saved = await self.journal.details(operation_id)
                if saved.get("model") != self.model.value:
                    raise InvalidRequest("Operation ID reused across promotion models")
                if previous.status == "SIMULATED_VERIFIED":
                    return self._result(
                        previous.item_id,
                        previous.requested_bid,
                        previous.confirmed_bid,
                        "SIMULATED_VERIFIED",
                    )
                raise UncertainWrite(
                    "Operation already exists; explicit read reconciliation required"
                )
            info = await self.get_bid_info(item_id)
            if not info.capabilities.allows(bid, datetime.now(UTC)):
                raise InvalidRequest("Bid is outside current API-supported values")
            payload = await self._payload(item_id, bid)
            await self.journal.intent(
                operation_id, item_id, bid, {"model": self.model.value, "body": payload}
            )
            try:
                await self.client.operation(self.write_operation, body=payload)
            except UncertainWrite:
                return await self.reconcile(operation_id)
            except (InvalidRequest, AccessDenied, CapabilityBlocked) as exc:
                await self.journal.finish(operation_id, "REJECTED")
                raise exc
            except AvitoError:
                await self.journal.finish(operation_id, "UNKNOWN")
                raise UncertainWrite("Write outcome requires reconciliation") from None
            return await self.reconcile(operation_id)

    def _result(self, item_id, bid, confirmed, status):
        return BidResult(
            item_id=item_id,
            requested_bid=bid,
            confirmed_bid=confirmed,
            status=status,
            simulated=self.simulated,
        )

    async def reconcile(self, operation_id):
        if self.journal is None:
            raise CapabilityBlocked("Journal required for reconciliation")
        row, payload = await self.journal.details(operation_id)
        if payload.get("model") != self.model.value:
            raise InvalidRequest("Wrong promotion adapter for operation")
        try:
            matches = await self._matches(row.item_id, payload["body"])
        except AvitoError:
            matches = False
        if not matches:
            await self.journal.finish(operation_id, "UNKNOWN")
            raise VerificationFailed("Requested bid state not confirmed; item in FAIL_SAFE")
        await self.journal.finish(operation_id, "SIMULATED_VERIFIED", row.requested_bid)
        return self._result(row.item_id, row.requested_bid, row.requested_bid, "SIMULATED_VERIFIED")

    async def set_bid(self, item_id, bid, operation_id):
        return await self._apply(item_id, bid, operation_id)


class CpxBidProvider(AuditedProvider):
    model = PromotionModel.CPX
    write_operation = "saveManualBid"

    async def current(self, item_id):
        data = parse(
            CpxBids,
            await self.client.operation("getBids", path_values={"itemId": positive_id(item_id)}),
        )
        return data

    async def get_bid_info(self, item_id):
        data = await self.current(item_id)
        if data.selectedType != "manual" or data.manual is None or data.manual.bidPenny is None:
            raise ModelUnavailable("CPX manual bidding is not active; auto mode is not converted")
        manual, now = data.manual, datetime.now(UTC)
        if manual.minBidPenny >= manual.maxBidPenny:
            raise InvalidResponse("Invalid CPX bid range")
        # Text specifies strictly above minimum and whole rubles, not an arbitrary list.
        minimum = (manual.minBidPenny // 100 + 1) * 100
        maximum = manual.maxBidPenny // 100 * 100
        if minimum > maximum:
            raise ModelUnavailable("No whole-ruble manual bid within API bounds")
        return BidInfo(
            item_id=item_id,
            bid=manual.bidPenny,
            checked_at=now,
            data_type="test" if self.client.offline else "actual",
            source="mock-transport" if self.client.offline else "avito:cpx",
            capabilities=BidCapabilities(
                model=self.model,
                verified=True,
                can_write=self.client.offline,
                minimum=minimum,
                maximum=maximum,
                increment=100,
                expires_at=now + timedelta(minutes=5),
            ),
        )

    async def _payload(self, item_id, bid):
        data = await self.current(item_id)
        if data.selectedType != "manual" or data.manual is None:
            raise ModelUnavailable("Manual CPX no longer active")
        manual = data.manual
        if not manual.minBidPenny < bid <= manual.maxBidPenny or bid % 100:
            raise InvalidRequest("CPX bid violates current bounds or whole-ruble rule")
        if manual.limitPenny is None:
            raise CapabilityBlocked("No existing CPX spending limit; write preparation blocked")
        if (
            manual.limitPenny % 100
            or not manual.minLimitPenny <= manual.limitPenny <= manual.maxLimitPenny
        ):
            raise InvalidResponse("Existing CPX limit cannot be safely preserved")
        return {
            "itemID": item_id,
            "actionTypeID": data.actionTypeID,
            "bidPenny": bid,
            "limitPenny": manual.limitPenny,
        }

    async def _matches(self, item_id, payload):
        data = await self.current(item_id)
        return (
            data.selectedType == "manual"
            and data.manual is not None
            and data.actionTypeID == payload["actionTypeID"]
            and data.manual.bidPenny == payload["bidPenny"]
            and data.manual.limitPenny == payload["limitPenny"]
        )


class AuctionBidProvider(AuditedProvider):
    model = PromotionModel.CPA_AUCTION
    write_operation = "saveItemBids"

    async def page(self, from_item_id=0, batch_size=200):
        if (
            type(from_item_id) is not int
            or from_item_id < 0
            or type(batch_size) is not int
            or not 1 <= batch_size <= 200
        ):
            raise InvalidRequest("Invalid auction cursor or batch size")
        result = parse(
            AuctionBids,
            await self.client.operation(
                "getUserBids", params={"fromItemID": from_item_id, "batchSize": batch_size}
            ),
        )
        ids = [i.itemID for i in result.items]
        if (
            len(ids) > batch_size
            or len(set(ids)) != len(ids)
            or any(i <= from_item_id for i in ids)
        ):
            raise InvalidResponse("Auction cursor did not advance")
        return result

    async def current(self, item_id):
        positive_id(item_id)
        cursor = 0
        for _ in range(20):
            batch = await self.page(cursor)
            for item in batch.items:
                if item.itemID == item_id:
                    return item
            if len(batch.items) < 200:
                raise ModelUnavailable("Item not returned by CPA auction; availability unconfirmed")
            cursor = batch.items[-1].itemID
        raise InvalidResponse("Auction page bound reached; availability unknown")

    async def get_bid_info(self, item_id):
        item, now = await self.current(item_id), datetime.now(UTC)
        allowed = tuple(p.pricePenny for p in item.availablePrices)
        if len(set(allowed)) != len(allowed):
            raise InvalidResponse("Duplicate auction prices")
        return BidInfo(
            item_id=item_id,
            bid=item.pricePenny,
            checked_at=now,
            data_type="test" if self.client.offline else "actual",
            source="mock-transport" if self.client.offline else "avito:auction",
            capabilities=BidCapabilities(
                model=self.model,
                verified=True,
                can_write=self.client.offline,
                allowed_bids=allowed,
                expires_at=now + timedelta(minutes=5),
            ),
        )

    async def _payload(self, item_id, bid):
        item = await self.current(item_id)
        if bid not in {p.pricePenny for p in item.availablePrices}:
            raise InvalidRequest("CPA bid is not in current availablePrices")
        # Preserve existing duration verbatim; this release does not change expiration policy.
        return {
            "items": [
                {
                    "itemID": item_id,
                    "pricePenny": bid,
                    "expirationTime": item.expirationTime or None,
                }
            ]
        }

    async def _matches(self, item_id, payload):
        current = await self.current(item_id)
        expected = payload["items"][0]
        return (
            current.pricePenny == expected["pricePenny"]
            and (current.expirationTime or None) == expected["expirationTime"]
        )
