import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.database.models import RuntimeRow
from app.domain.enums import PositionStatus
from app.domain.models import PositionSnapshot
from app.positions.base import unavailable
from app.positions.validation import context_key

logger = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, repository, history, sources, timeout=20, notify=None, evaluate=None):
        self.repository = repository
        self.history = history
        self.sources = tuple(sources)
        self.timeout = timeout
        self.notify = notify
        self.evaluate = evaluate
        self.lock = asyncio.Lock()

    async def tick(self, *, item_id=None, query_index=None):
        if self.lock.locked():
            return False
        async with self.lock:
            for item in await self.repository.items():
                if not item.active or (item_id is not None and item.item_id != item_id):
                    continue
                for index, query in enumerate(item.queries):
                    if query_index is not None and index != query_index:
                        continue
                    key = context_key(item.item_id, query)
                    for source in self.sources:
                        if source.interval_minutes is None:
                            continue
                        now = datetime.now(UTC)
                        interval = max(source.interval_minutes, item.monitoring_interval_minutes)
                        schedule_key = f"position_attempt:{key}:{source.name}"
                        async with self.history.sessions.begin() as session:
                            previous = await session.get(RuntimeRow, schedule_key)
                            if previous and now - datetime.fromisoformat(
                                previous.payload["at"]
                            ) < timedelta(minutes=interval):
                                continue
                            await session.merge(
                                RuntimeRow(key=schedule_key, payload={"at": now.isoformat()})
                            )
                        try:
                            async with asyncio.timeout(self.timeout):
                                result = await source.observe(item.item_id, query)
                            row = PositionSnapshot.model_validate(
                                result.model_dump()
                                if isinstance(result, PositionSnapshot)
                                else result
                            )
                            if (
                                row.provider != source.name
                                or row.checked_at > datetime.now(UTC)
                                or context_key(
                                    row.item_id, row.search_query, row.region, row.filters
                                )
                                != key
                            ):
                                raise ValueError("Observation context mismatch")
                        except (ValueError, TypeError):
                            row = unavailable(
                                item.item_id,
                                query,
                                source.name,
                                PositionStatus.INVALID_DATA,
                                "Invalid response",
                            )
                        except Exception as exc:
                            row = unavailable(
                                item.item_id, query, source.name, error=type(exc).__name__
                            )
                        await self.history.append([row])
                        logger.info(
                            "event=position_observation item_id=%s source=%s status=%s",
                            item.item_id,
                            source.name,
                            row.status,
                        )
                if self.evaluate:
                    await self.evaluate(item)
                if self.notify:
                    await self.notify(item, await self.history.history(item.item_id))
        return True
