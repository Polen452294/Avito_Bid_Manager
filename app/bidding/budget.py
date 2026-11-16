from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.database.models import RuntimeRow
from app.domain.models import Advertisement, Spending


class BudgetController:
    def __init__(self, settings: Settings):
        self.settings = settings

    def reliable(self, item: Advertisement, spending: Spending | None, now: datetime) -> bool:
        s = self.settings
        if spending is None:
            return False
        age = now - spending.checked_at
        cutoff = timedelta(minutes=s.max_spending_data_age_minutes)
        day = now.astimezone(ZoneInfo(s.timezone)).date()
        return not (
            spending.item_id != item.item_id
            or not spending.reliable
            or spending.item_spent is None
            or spending.account_spent is None
            or spending.data_type == "preliminary"
            or (spending.data_type == "test" and item.promotion_model.value != "MOCK")
            or age < timedelta(0)
            or age > cutoff
            or spending.period_date != day.isoformat()
            or (spending.source != "legacy" and spending.data_as_of is None)
            or (
                spending.data_as_of is not None
                and (
                    not now - cutoff <= spending.data_as_of <= spending.checked_at
                    or spending.data_as_of.astimezone(ZoneInfo(s.timezone)).date() != day
                )
            )
            or spending.item_spent > spending.account_spent
        )

    def warnings(self, item, spending, now, thresholds=(80, 95)) -> dict[str, int]:
        if not self.reliable(item, spending, now):
            return {}
        result = {}
        for scope, spent, limit in (
            ("item", spending.item_spent, item.daily_budget),
            ("account", spending.account_spent, self.settings.account_daily_limit_kopecks),
        ):
            for threshold in sorted(thresholds, reverse=True):
                if limit and spent * 100 >= limit * threshold:
                    result[scope] = threshold
                    break
        return result

    def check(self, item: Advertisement, spending: Spending | None, now: datetime) -> str | None:
        if not self.reliable(item, spending, now):
            return "UNRELIABLE_SPENDING"
        s = self.settings
        reserve = 100 - s.budget_reserve_percent
        if spending.item_spent * 100 >= item.daily_budget * reserve:
            return "ITEM_BUDGET_GUARD"
        if spending.account_spent * 100 >= s.account_daily_limit_kopecks * reserve:
            return "ACCOUNT_BUDGET_GUARD"
        return None

    @staticmethod
    async def claim_increase(session, stage, spending, operation_id):
        """Caller holds SQLite's write lock. Claim a financial cut, never the bid price."""
        key = f"budget_increase:{stage}"
        cut = spending.data_as_of or spending.checked_at
        claim = await session.get(RuntimeRow, key)
        if claim is not None and (
            claim.payload["day"] == spending.period_date
            and cut <= datetime.fromisoformat(claim.payload["cut"])
        ):
            return claim.payload["operation_id"] == operation_id
        await session.merge(
            RuntimeRow(
                key=key,
                payload={
                    "cut": cut.isoformat(),
                    "day": spending.period_date,
                    "operation_id": operation_id,
                },
            )
        )
        return True
