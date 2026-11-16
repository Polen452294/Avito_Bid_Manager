from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import Mode, PositionStatus, PositionType, PromotionModel

Money = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC) if isinstance(value, datetime) else value


class SearchQuery(Model):
    text: str = Field(min_length=1)
    region: str = Field(min_length=1)
    filters: dict[str, str] = Field(default_factory=dict)
    weight: Decimal = Field(default=Decimal(1), gt=0)


class Advertisement(Model):
    item_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    active: bool = True
    archived: bool = False
    avito_verified: bool = False
    management_mode: Literal[Mode.DRY_RUN, Mode.ASSISTED] = Mode.DRY_RUN
    trial_decreases: bool = True
    auto_enabled: bool = False
    promotion_model: PromotionModel = PromotionModel.UNKNOWN
    min_bid: Money = 0
    max_bid: Money
    confirmed_bid: Money | None = None
    target_min: int = Field(default=3, ge=1)
    target_max: int = Field(default=7, ge=1)
    queries: tuple[SearchQuery, ...] = Field(default=(), max_length=5)
    primary_query_index: int = Field(default=0, ge=0, le=4)
    min_position_coverage: Decimal = Field(default=Decimal(1), gt=0, le=1)
    max_query_skew_minutes: int = Field(default=15, strict=True, ge=1, le=2**31 - 1)
    position_sources: tuple[str, ...] = ("mock", "manual")
    daily_budget: Money
    minimum_daily_budget: Money = 0
    maximum_daily_budget: Money | None = None
    bid_unit: (
        Literal["kopecks_per_action", "kopecks_per_auction_bid", "kopecks_simulated", "unknown"]
        | None
    ) = None
    active_hours: tuple[int, ...] = tuple(range(24))
    monitoring_interval_minutes: int = Field(default=15, strict=True, ge=1, le=2**31 - 1)
    cooldown_minutes: int = Field(default=45, strict=True, ge=1, le=2**31 - 1)
    stabilization_minutes: int | None = Field(default=None, strict=True, ge=1, le=2**31 - 1)
    increase_interval_minutes: int = Field(default=45, strict=True, ge=1, le=2**31 - 1)
    increase_confirmations: int | None = Field(default=None, ge=2)
    decrease_confirmations: int | None = Field(default=None, ge=2)
    max_changes_per_day: int | None = Field(default=None, ge=1)
    max_step_kopecks: Money = 1000
    max_step_percent: int = Field(default=20, ge=1, le=100)
    increase_step_kopecks: int | None = Field(default=None, strict=True, ge=1)
    decrease_step_kopecks: int | None = Field(default=None, strict=True, ge=1)
    increase_step_percent: int | None = Field(default=None, ge=1, le=100)
    decrease_step_percent: int | None = Field(default=None, ge=1, le=100)
    hysteresis_positions: int = Field(default=1, ge=1)
    strategy: str = "target_position"

    @model_validator(mode="after")
    def consistent(self):
        units = {
            PromotionModel.CPX: "kopecks_per_action",
            PromotionModel.CPA_AUCTION: "kopecks_per_auction_bid",
            PromotionModel.MOCK: "kopecks_simulated",
        }
        unit = units.get(self.promotion_model, "unknown")
        if self.bid_unit is not None and self.bid_unit != unit:
            raise ValueError("Bid unit does not match promotion model")
        object.__setattr__(self, "bid_unit", unit)
        if self.daily_budget < self.minimum_daily_budget or (
            self.maximum_daily_budget is not None and self.daily_budget > self.maximum_daily_budget
        ):
            raise ValueError("Daily budget outside allocation bounds")
        if (
            self.promotion_model == PromotionModel.MOCK
            and self.confirmed_bid is not None
            and not self.min_bid <= self.confirmed_bid <= self.max_bid
        ):
            raise ValueError("Simulator starting bid outside bounds")
        if self.min_bid > self.max_bid or self.target_min > self.target_max:
            raise ValueError("Invalid bid/position bounds")
        if any(h < 0 or h > 23 for h in self.active_hours):
            raise ValueError("Hours must be 0..23")
        keys = [(q.text, q.region, tuple(sorted(q.filters.items()))) for q in self.queries]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate query/region")
        if self.primary_query_index >= max(1, len(self.queries)):
            raise ValueError("Primary query must be configured")
        return self


class BidCapabilities(Model):
    model: PromotionModel
    verified: bool = False
    can_write: bool = False
    allowed_bids: tuple[Money, ...] = ()
    minimum: Money = 0
    maximum: Money = 0
    increment: int = Field(default=100, strict=True, gt=0)
    expires_at: AwareDatetime

    def allows(self, bid: int, now: datetime) -> bool:
        if not self.verified or now >= self.expires_at:
            return False
        if self.model == PromotionModel.CPA_AUCTION or self.allowed_bids:
            return bid in self.allowed_bids
        return self.minimum <= bid <= self.maximum and bid % self.increment == 0


class BidInfo(Model):
    item_id: int = Field(gt=0)
    bid: Money
    checked_at: AwareDatetime
    capabilities: BidCapabilities
    data_type: Literal["actual", "test"] = "test"
    source: str = Field(default="unspecified", min_length=1)


class BidResult(Model):
    item_id: int
    requested_bid: Money
    confirmed_bid: Money | None = None
    status: str
    simulated: bool = False


class PositionSnapshot(Model):
    item_id: int = Field(gt=0)
    search_query: str
    region: str
    filters: dict[str, str] = Field(default_factory=dict)
    position: Decimal | None = Field(default=None, ge=1, allow_inf_nan=False)
    period_start: AwareDatetime | None = None
    period_end: AwareDatetime | None = None
    checked_range: int | None = Field(default=None, ge=1)
    error: str | None = None
    position_type: PositionType
    checked_at: AwareDatetime
    provider: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
    status: PositionStatus

    @model_validator(mode="after")
    def valid_position(self):
        if (self.status == PositionStatus.FOUND) != (self.position is not None):
            raise ValueError("Only FOUND must contain a position")
        if self.position_type != PositionType.AVERAGE and self.position is not None:
            if self.position != self.position.to_integral_value():
                raise ValueError("Exact position must be integral")
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("Both period boundaries required")
        if self.period_start and not self.period_start < self.period_end <= self.checked_at:
            raise ValueError("Invalid observation period")
        return self


class Spending(Model):
    item_id: int
    item_spent: Money | None
    account_spent: Money | None
    checked_at: AwareDatetime
    period_date: str
    reliable: bool = False
    data_as_of: AwareDatetime | None = None
    source: str = "legacy"
    data_type: Literal["actual", "preliminary", "test"] = "actual"


class Decision(Model):
    item_id: int
    old_bid: Money | None
    new_bid: Money | None = None
    reason: str
    created_at: AwareDatetime
    action: str = "HOLD"
    reason_code: str | None = None
    observations: tuple[PositionSnapshot, ...] = ()
    limits: dict = Field(default_factory=dict)
    confidence: Decimal | None = None
    position_score: Decimal | None = None
    coverage: Decimal = Decimal(0)
    expected_result: str | None = None
    authorization: str = "NOT_APPLICABLE"
    restore_bid: Money | None = None


class AppliedChange(Model):
    applied_at: AwareDatetime
    old_bid: Money
    new_bid: Money
    restore_bid: Money | None = None
    probe: bool = True
