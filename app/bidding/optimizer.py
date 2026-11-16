from datetime import date
from typing import Literal

from pydantic import AwareDatetime

from app.analytics.metrics import metrics
from app.domain.models import Model, Money


def recommendation(
    spent: int, views: int, contacts: int | None, days: int, minimum_contacts: int = 10
) -> dict:
    if days not in (7, 14):
        raise ValueError("Use a complete 7 or 14 day period")
    result = metrics(spent, views, contacts)
    result["recommendation"] = (
        "INSUFFICIENT_DATA" if contacts is None or contacts < minimum_contacts else "REVIEW_CPL"
    )
    result["automatic_change"] = False
    return result


class Recommendation(Model):
    item_id: int
    recommendation_type: Literal["INSUFFICIENT_DATA", "KEEP_BID", "LOWER_BID", "REALLOCATE_BUDGET"]
    current_value: Money | None
    suggested_value: Money | None
    reason_code: str
    calculated_at: AwareDatetime
    analysis_period: tuple[date, date]
    data_quality: str
    expected_effect: str


class PerformanceOptimizer:
    def __init__(self, minimum_contacts):
        self.minimum_contacts = minimum_contacts

    def recommend(self, item, current_bid, recent, earlier, now):
        comparable = (
            recent.days == earlier.days
            and recent.days in {7, 14}
            and earlier.end == recent.start
            and recent.context is not None
            and recent.context == earlier.context
            and recent.position_context is not None
            and recent.position_context == earlier.position_context
            and all(
                p.contacts is not None
                and p.contacts >= self.minimum_contacts
                and p.views
                and p.spend
                and p.bid_days == p.days
                and p.position_days == p.days
                and p.bid_range[0] == p.bid_range[1]
                for p in (recent, earlier)
            )
            and current_bid == recent.average_bid
        )
        action, reason, suggested, effect = (
            "INSUFFICIENT_DATA",
            "PERIODS_NOT_COMPARABLE",
            None,
            "Оценка эффекта от изменения ставки пока недоступна.",
        )
        if comparable:
            action, reason, suggested, effect = (
                "KEEP_BID",
                "NO_STRONG_COST_SIGNAL",
                current_bid,
                "История пока не подтверждает преимущество другой ставки.",
            )
            if (
                item.min_bid <= earlier.average_bid < recent.average_bid <= item.max_bid
                and earlier.contacts >= recent.contacts
                and earlier.views * 5 >= recent.views * 4
                and earlier.position <= item.target_max
                and earlier.cpl * 5 <= recent.cpl * 4
            ):
                action, reason, suggested, effect = (
                    "LOWER_BID",
                    "LOWER_BID_WITH_LOWER_HISTORICAL_CPL",
                    int(earlier.average_bid),
                    "При меньшей наблюдавшейся ставке CPL был ниже. "
                    "Повторение результата и сохранение позиции требуют проверки.",
                )
        return Recommendation(
            item_id=item.item_id,
            recommendation_type=action,
            current_value=current_bid,
            suggested_value=suggested,
            reason_code=reason,
            calculated_at=now,
            analysis_period=(earlier.start, recent.end),
            data_quality="COMPARABLE" if comparable else "INSUFFICIENT_DATA",
            expected_effect=effect,
        )
