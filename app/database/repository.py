import json
from uuid import uuid4

from sqlalchemy import delete, func, select, text

from app.database.models import (
    Account,
    AdvertisementRow,
    DecisionRow,
    OperationRow,
    RuntimeRow,
    SearchQueryRow,
)
from app.domain.models import Advertisement


class Repository:
    def __init__(self, sessions):
        self.sessions = sessions

    async def save_item(self, item: Advertisement):
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            await self.write_item(session, item)

    async def write_item(self, session, item, *, expected=...):
        row = await session.get(AdvertisementRow, item.item_id)
        if row and row.account_id != 1:
            raise ValueError("Item belongs to another account")
        previous = Advertisement.model_validate(row.config) if row else None
        if expected is not ... and expected != (
            previous.model_dump(mode="json") if previous else None
        ):
            raise ValueError("Item changed")
        if not item.archived and (previous is None or previous.archived):
            count = await session.scalar(
                select(func.count())
                .select_from(AdvertisementRow)
                .where(
                    AdvertisementRow.account_id == 1,
                    func.coalesce(AdvertisementRow.config["archived"].as_boolean(), False).is_(
                        False
                    ),
                )
            )
            if count >= 5:
                raise ValueError("At most five advertisements are supported")
        if await session.get(Account, 1) is None:
            session.add(Account(id=1))
            await session.flush()
        if row is None:
            row = AdvertisementRow(item_id=item.item_id, account_id=1)
            session.add(row)
        row.config = item.model_dump(mode="json")
        if previous != item:
            await session.merge(
                RuntimeRow(key=f"item_revision:{item.item_id}", payload={"version": uuid4().hex})
            )
        await session.flush()
        await session.execute(delete(SearchQueryRow).where(SearchQueryRow.item_id == item.item_id))
        for query in item.queries:
            session.add(
                SearchQueryRow(
                    item_id=item.item_id,
                    query=query.text,
                    region=query.region,
                    weight=str(query.weight),
                    filters_key=json.dumps(query.filters, sort_keys=True),
                )
            )

    async def items(self, *, include_archived=False) -> list[Advertisement]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(AdvertisementRow)
                .where(AdvertisementRow.account_id == 1)
                .order_by(AdvertisementRow.item_id)
            )
            return [
                Advertisement.model_validate(row.config)
                for row in rows
                if include_archived or not row.config.get("archived")
            ]

    async def item(self, item_id: int, *, include_archived=False) -> Advertisement | None:
        async with self.sessions() as session:
            row = await session.get(AdvertisementRow, item_id)
            return (
                Advertisement.model_validate(row.config)
                if row
                and row.account_id == 1
                and (include_archived or not row.config.get("archived"))
                else None
            )

    async def decisions(self, item_id: int, limit: int = 10, *, offset=0) -> list[dict]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(DecisionRow, OperationRow)
                    .outerjoin(OperationRow, OperationRow.decision_id == DecisionRow.id)
                    .where(DecisionRow.item_id == item_id)
                    .order_by(DecisionRow.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [
                {
                    "created_at": row.created_at,
                    "old_bid": None,
                    "new_bid": operation.requested_bid if operation else None,
                    "authorization": operation.status if operation else "",
                    "reason": row.payload.get("source", "LEGACY_DECISION"),
                    **row.payload,
                }
                for row, operation in rows
            ]

    async def recover(self) -> int:
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            rows = list(
                await session.scalars(
                    select(OperationRow).where(
                        OperationRow.status.in_(["PENDING", "SENT", "UNKNOWN"])
                    )
                )
            )
            for row in rows:
                row.status = "UNKNOWN"
                saved = await session.get(DecisionRow, row.decision_id)
                if saved.schema_version >= 2:
                    saved.payload = {**saved.payload, "authorization": row.status}
                key = f"fail_safe:{row.item_id}"
                if await session.get(RuntimeRow, key) is None:
                    session.add(
                        RuntimeRow(
                            key=key,
                            payload={
                                "reason": "Unfinished operation requires provider reconciliation"
                            },
                        )
                    )
            return len(rows)
