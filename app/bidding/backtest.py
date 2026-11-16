from collections.abc import Iterable
from datetime import datetime

from pydantic import AwareDatetime

from app.bidding.engine import BiddingEngine
from app.domain.models import (
    Advertisement,
    AppliedChange,
    BidInfo,
    Model,
    PositionSnapshot,
    Spending,
)


class ReplayFrame(Model):
    now: AwareDatetime
    observations: list[PositionSnapshot]
    spending: Spending | None = None
    bid_info: BidInfo | None = None


def backtest(
    engine: BiddingEngine,
    item: Advertisement,
    info: BidInfo,
    frames: Iterable[ReplayFrame | tuple[datetime, list[PositionSnapshot], Spending]],
    *,
    assume_applied: bool = False,
    position_origin: str = "historical",
) -> list[dict]:
    """Replay supplied positions. Optional simulated bid changes never predict a position."""
    if position_origin not in {"historical", "fixture"}:
        raise ValueError("position_origin must be historical or fixture")
    output = []
    applied = []
    previous = None
    for entry in frames:
        frame = (
            entry
            if isinstance(entry, ReplayFrame)
            else ReplayFrame(now=entry[0], observations=entry[1], spending=entry[2])
        )
        now = frame.now
        if previous is not None and now <= previous:
            raise ValueError("Replay frames must have strictly increasing timestamps")
        previous = now
        if not assume_applied and frame.bid_info is not None:
            if frame.bid_info.bid != info.bid:
                applied.append(
                    AppliedChange(
                        applied_at=now, old_bid=info.bid, new_bid=frame.bid_info.bid, probe=False
                    )
                )
            info = frame.bid_info
        current = info.model_copy(update={"checked_at": now}) if assume_applied else info
        decision = engine.decide(
            item, current, frame.observations, frame.spending, now, applied=tuple(applied)
        )
        output.append(
            {
                "simulated": True,
                "position_origin": position_origin,
                "bid_origin": "hypothetical_chain" if assume_applied else "recorded",
                **decision.model_dump(mode="json"),
            }
        )
        if assume_applied and decision.new_bid is not None:
            applied.append(
                AppliedChange(
                    applied_at=now,
                    old_bid=current.bid,
                    new_bid=decision.new_bid,
                    restore_bid=decision.restore_bid,
                )
            )
            info = current.model_copy(update={"bid": decision.new_bid})
    return output
