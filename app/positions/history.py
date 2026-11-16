from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.database.models import PositionRow
from app.domain.models import PositionSnapshot
from app.positions.validation import context_key, observation_key


class PositionHistory:
    def __init__(self, sessions):
        self.sessions = sessions

    async def append(self, rows):
        count = 0
        async with self.sessions.begin() as session:
            for row in rows:
                result = await session.execute(
                    insert(PositionRow)
                    .values(
                        item_id=row.item_id,
                        payload=row.model_dump(mode="json"),
                        context_key=context_key(
                            row.item_id, row.search_query, row.region, row.filters
                        ),
                        observation_key=observation_key(row),
                    )
                    .on_conflict_do_nothing(index_elements=["observation_key"])
                )
                count += result.rowcount
        return count

    async def history(self, item_id, *, through_id=None):
        async with self.sessions() as session:
            query = select(PositionRow.payload).where(PositionRow.item_id == item_id)
            if through_id is not None:
                query = query.where(PositionRow.id <= through_id)
            payloads = await session.scalars(query)
            return sorted(
                (PositionSnapshot.model_validate(payload) for payload in payloads),
                key=lambda row: row.checked_at,
            )
