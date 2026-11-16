from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.bidding.engine import BiddingEngine
from app.config import Settings
from app.domain.enums import PositionStatus, PromotionModel
from app.domain.models import Advertisement


@pytest.mark.parametrize(
    ("positions", "expected"), [((12, 12), 2200), ((5, 5, 5), 1900), ((12, 5), None), ((12,), None)]
)
def test_directions(case, positions, expected):
    s, item, info, spent, now, make = case
    assert BiddingEngine(s).decide(item, info, make(positions), spent, now).new_bid == expected


@pytest.mark.parametrize(
    "guard",
    [
        "cooldown",
        "limit",
        "stale",
        "budget",
        "account",
        "missing",
        "future",
        "duplicate",
        "paused",
        "fail_safe",
        "old_spending",
    ],
)
def test_guards(case, guard):
    s, item, info, spent, now, make = case
    rows, kwargs = make(), {}
    if guard == "cooldown":
        kwargs["last_change"] = now - timedelta(minutes=20)
    elif guard == "limit":
        kwargs["changes_today"] = 4
    elif guard == "stale":
        rows = [p.model_copy(update={"checked_at": now - timedelta(hours=1)}) for p in rows]
    elif guard == "budget":
        spent = spent.model_copy(update={"item_spent": 8000, "account_spent": 8000})
    elif guard == "account":
        spent = spent.model_copy(update={"account_spent": 80000})
    elif guard == "missing":
        rows[-1] = rows[-1].model_copy(
            update={"position": None, "status": PositionStatus.NOT_FOUND_IN_CHECKED_RANGE}
        )
    elif guard == "future":
        rows[-1] = rows[-1].model_copy(update={"checked_at": now + timedelta(minutes=1)})
    elif guard == "duplicate":
        rows = [rows[-1], rows[-1]]
    elif guard == "paused":
        item = item.model_copy(update={"active": False})
    elif guard == "fail_safe":
        kwargs["fail_safe"] = True
    else:
        spent = spent.model_copy(update={"checked_at": now - timedelta(hours=1)})
    assert BiddingEngine(s).decide(item, info, rows, spent, now, **kwargs).new_bid is None


def test_cpa_allowed_values_only(case):
    s, item, info, spent, now, make = case
    item = item.model_copy(update={"promotion_model": PromotionModel.CPA_AUCTION})
    caps = info.capabilities.model_copy(
        update={"model": PromotionModel.CPA_AUCTION, "allowed_bids": (2000, 2100, 2400)}
    )
    info = info.model_copy(update={"capabilities": caps})
    assert BiddingEngine(s).decide(item, info, make(), spent, now).new_bid == 2100


def test_conflicting_queries(case):
    s, item, info, spent, now, make = case
    from app.domain.models import SearchQuery

    item = item.model_copy(
        update={"queries": (*item.queries, SearchQuery(text="other", region="r"))}
    )
    rows = make() + [p.model_copy(update={"search_query": "other"}) for p in make((2, 2, 2))]
    assert BiddingEngine(s).decide(item, info, rows, spent, now).reason == "CONFLICTING_QUERIES"


@pytest.mark.parametrize("mode", ["AUTO", "ASSISTED"])
def test_no_automatic_live_start(mode):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode=mode)


@pytest.mark.parametrize("money", [1.1, "100", -1, True])
def test_money_is_integer(case, money):
    data = case[1].model_dump()
    data["max_bid"] = money
    with pytest.raises(ValidationError):
        Advertisement.model_validate(data)
