from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.analytics.store import DailyMetric, day_bounds
from app.avito.accounts import AccountsAPI
from app.avito.contracts import ContractRegistry
from app.avito.diagnostics import avito_client
from app.avito.exceptions import AccessDenied, AvitoError
from app.avito.statistics import StatisticsAPI
from app.domain.models import Spending


class StatisticsCollector:
    """Reads only the registry-gated statistics endpoints."""

    def __init__(self, api, account_id, store, settings, *, clock=None):
        self.api = api
        self.account_id = account_id
        self.store = store
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))

    async def collect(self, items):
        if not items:
            return
        # Avito date filters use Moscow dates. Report periods may use a different timezone.
        zone = ZoneInfo("Europe/Moscow")
        today = self.clock().astimezone(zone).date()
        ids = [item.item_id for item in items]
        simulated = self.api.client.offline
        source = "avito_fixture" if simulated else "avito_item_analytics"
        data_type = "test" if simulated else "preliminary"
        metrics = (
            "spend",
            "promotion_spend",
            "views",
            "contacts",
            "messenger_contacts",
            "phone_reveals",
        )
        for offset in range(self.settings.statistics_backfill_days + 1):
            day = today - timedelta(days=offset)
            start, end = day_bounds(day, zone)
            if offset > 2 and await self.store.collected_day(items, start, end, source):
                continue
            totals = await self.api.daily_item_metrics(self.account_id, ids, day)
            account = None
            account_error = None
            if day == today and not simulated:
                try:
                    account = await self.api.daily_account_spend(self.account_id, day)
                except AvitoError as exc:
                    # Preserve already collected item metrics even when account spending fails.
                    account_error = exc
            received = self.clock()
            await self.store.append(
                [
                    DailyMetric(
                        item_id=item.item_id,
                        metric=metric,
                        value=totals.get(item.item_id, {}).get(metric),
                        period_start=start,
                        period_end=end,
                        received_at=received,
                        source=source,
                        data_type=data_type,
                        promotion_model=item.promotion_model,
                        quality="unverified"
                        if metric in totals.get(item.item_id, {})
                        else "invalid",
                    )
                    for item in items
                    for metric in metrics
                ],
                [
                    Spending(
                        item_id=item.item_id,
                        item_spent=totals.get(item.item_id, {}).get("spend"),
                        account_spent=account,
                        checked_at=received,
                        period_date=day.isoformat(),
                        data_type=data_type,
                        source=source,
                        reliable=False,
                    )
                    for item in items
                ]
                if day == today and not simulated
                else (),
            )
            if account_error is not None:
                raise account_error


async def collect_avito(settings, items, store):
    items = [item for item in items if item.promotion_model.value != "MOCK"]
    if not items:
        return
    if not (
        settings.avito_client_id
        and settings.avito_client_secret
        and settings.avito_expected_account_id
    ):
        raise ValueError("Statistics collection requires credentials and an expected account ID")
    contracts = ContractRegistry.from_official_exports(
        [Path(path) for path in settings.avito_official_spec_paths]
    )
    for operation in ("getAccessToken", "getUserInfoSelf", "itemAnalytics", "accountSpendings"):
        contracts.require(operation)
    client = avito_client(settings, contracts)
    try:
        account = await AccountsAPI(client).account()
        if account.id != settings.avito_expected_account_id:
            raise AccessDenied("Statistics account mismatch")
        await StatisticsCollector(StatisticsAPI(client), account.id, store, settings).collect(items)
    finally:
        await client.aclose()
