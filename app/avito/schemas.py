"""Wire schemas from the pinned snapshot; intentionally stricter on critical fields."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.avito.exceptions import InvalidResponse

PositiveID = Annotated[int, Field(strict=True, gt=0)]
Count = Annotated[int, Field(strict=True, ge=0)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def parse(model, data):
    try:
        return model.model_validate(data)
    except (ValidationError, ValueError, TypeError):
        raise InvalidResponse(f"Invalid or incomplete {model.__name__} response") from None


class Account(WireModel):
    id: PositiveID
    name: str | None = None


class Listing(WireModel):
    id: PositiveID
    title: str
    status: Literal["active", "removed", "old", "blocked", "rejected"]
    category: dict | None = None


class PageMeta(WireModel):
    page: PositiveID
    per_page: PositiveID


class ListingsPage(WireModel):
    resources: list[Listing]
    meta: PageMeta


class ItemDetail(WireModel):
    status: Literal["active", "removed", "old", "blocked", "rejected", "not_found", "another_user"]
    vas: list[dict] | None = None


class ManualCpx(WireModel):
    bidPenny: Count | None = None
    limitPenny: Count | None = None
    minBidPenny: Count
    maxBidPenny: Count
    minLimitPenny: Count
    maxLimitPenny: Count


class CpxBids(WireModel):
    actionTypeID: PositiveID
    selectedType: str
    manual: ManualCpx | None = None
    auto: dict | None = None


class CpxPromotion(WireModel):
    itemID: PositiveID
    actionTypeID: PositiveID
    manualPromotion: dict | None = None
    autoPromotion: dict | None = None


class CpxPromotions(WireModel):
    items: list[CpxPromotion]


class AvailablePrice(WireModel):
    pricePenny: Count
    goodness: int = Field(strict=True)


class AuctionBid(WireModel):
    itemID: PositiveID
    pricePenny: Count
    expirationTime: str
    availablePrices: list[AvailablePrice]


class AuctionBids(WireModel):
    items: list[AuctionBid]


class ServiceItem(WireModel):
    itemId: PositiveID
    services: list[dict]


class ServiceError(WireModel):
    itemId: PositiveID
    errorCode: int = Field(strict=True)
    # errorText intentionally omitted from reports/logs.


class Services(WireModel):
    degraded: bool = Field(strict=True)
    items: list[ServiceItem] | None = None
    errors: list[ServiceError] | None = None


class DailyCounters(WireModel):
    date: date
    uniqViews: Count | None = None
    uniqContacts: Count | None = None
    uniqFavorites: Count | None = None


class ItemCounters(WireModel):
    itemId: PositiveID
    stats: list[DailyCounters]


class CounterResult(WireModel):
    items: list[ItemCounters]


class Counters(WireModel):
    result: CounterResult
    errors: dict | None = None


class Metric(WireModel):
    slug: str
    value: Decimal = Field(allow_inf_nan=False)


class MetricGrouping(WireModel):
    id: int = Field(strict=True)
    type: Literal["day", "week", "month", "item", "totals"]
    metrics: list[Metric]


class AnalyticsResult(WireModel):
    dataTotalCount: Count
    groupings: list[MetricGrouping]
    timestamp: str


class Analytics(WireModel):
    result: AnalyticsResult


class SpendingValue(Metric):
    services: list[Metric] | None = None


class SpendingGrouping(WireModel):
    date: str
    type: Literal["day", "week", "month"]
    spendings: list[SpendingValue]


class SpendingsResult(WireModel):
    groupings: list[SpendingGrouping]
    timestamp: int = Field(strict=True)


class Spendings(WireModel):
    result: SpendingsResult
