import asyncio
from datetime import UTC, datetime

from app.domain.enums import PositionStatus, PositionType
from app.domain.models import PositionSnapshot


def unavailable(item_id, query, provider, status=PositionStatus.UNAVAILABLE, error=None):
    return PositionSnapshot(
        item_id=item_id,
        search_query=query.text,
        region=query.region,
        filters=query.filters,
        checked_at=datetime.now(UTC),
        provider=provider,
        confidence=0,
        status=status,
        position_type=PositionType.UNKNOWN,
        error=error,
    )


class UnavailablePositionProvider:
    name = "unavailable"
    interval_minutes = 60

    async def observe(self, item_id, query):
        return unavailable(item_id, query, self.name)


class MockPositionProvider:
    name = "mock"
    interval_minutes = 15

    def __init__(self, observations, delay=0):
        self.observations = iter(observations)
        self.delay = delay

    async def observe(self, item_id, query):
        await asyncio.sleep(self.delay)
        row = next(self.observations, None)
        if isinstance(row, Exception):
            raise row
        if row is None:
            return unavailable(item_id, query, self.name, error="Scenario exhausted")
        if (row.item_id, row.search_query, row.region, row.filters) != (
            item_id,
            query.text,
            query.region,
            query.filters,
        ):
            raise ValueError("Mock observation does not match query")
        return row


class OfficialPositionProvider(UnavailablePositionProvider):
    name = "official"
    interval_minutes = 1440

    async def observe(self, item_id, query):
        return unavailable(
            item_id,
            query,
            self.name,
            PositionStatus.UNSUPPORTED,
            "Official position API contract not verified",
        )


class BrowserPositionProvider(UnavailablePositionProvider):
    name = "browser"
    interval_minutes = None  # No scheduled or browser access until separately verified.

    async def observe(self, item_id, query):
        return unavailable(
            item_id,
            query,
            self.name,
            PositionStatus.UNSUPPORTED,
            "Automated access permission and implementation not verified",
        )


class ExternalPositionProvider(UnavailablePositionProvider):
    """Inject a documented async adapter; no guessed endpoints or credentials."""

    def __init__(self, adapter=None, *, name="external", interval_minutes=60):
        if interval_minutes <= 0:
            raise ValueError("Positive source interval required")
        self.adapter = adapter
        self.name = name
        self.interval_minutes = interval_minutes

    async def observe(self, item_id, query):
        if self.adapter is None:
            return unavailable(
                item_id,
                query,
                self.name,
                PositionStatus.UNSUPPORTED,
                "External provider not configured",
            )
        return await self.adapter(item_id, query)
