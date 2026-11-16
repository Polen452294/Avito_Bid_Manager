from datetime import UTC, datetime

from sqlalchemy import select, text

from app.avito.exceptions import InvalidRequest, UncertainWrite
from app.database.models import DecisionRow, OperationRow, RuntimeRow


class OperationJournal:
    """Durable intent and reconciliation, using the existing Phase 1 tables."""

    def __init__(self, sessions):
        self.sessions = sessions

    async def lookup(self, operation_id, item_id, bid):
        if not operation_id or len(operation_id) > 128:
            raise InvalidRequest("A nonempty operation ID of at most 128 characters is required")
        async with self.sessions() as session:
            row = await session.get(OperationRow, operation_id)
            if row and (row.item_id != item_id or row.requested_bid != bid):
                raise InvalidRequest("Operation ID already belongs to another request")
            return row

    async def intent(self, operation_id, item_id, bid, payload):
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            if await session.get(OperationRow, operation_id):
                raise UncertainWrite("Operation already recorded; read journal before dispatch")
            pending = await session.scalar(
                select(OperationRow.id)
                .where(
                    OperationRow.item_id == item_id,
                    OperationRow.status.in_(["PENDING", "SENT", "UNKNOWN"]),
                )
                .limit(1)
            )
            blocked = await session.get(RuntimeRow, f"fail_safe:{item_id}")
            if pending or blocked:
                raise UncertainWrite("Item is in FAIL_SAFE; reconcile before another write")
            decision = DecisionRow(
                item_id=item_id,
                payload={"source": "snapshot_wire_test", "simulated": True, **payload},
            )
            session.add(decision)
            await session.flush()
            session.add(
                OperationRow(
                    id=operation_id,
                    item_id=item_id,
                    decision_id=decision.id,
                    requested_bid=bid,
                    status="PENDING",
                )
            )
            await session.commit()

    async def finish(self, operation_id, status, confirmed=None):
        async with self.sessions.begin() as session:
            row = await session.get(OperationRow, operation_id)
            if row is None:
                raise InvalidRequest("Unknown operation")
            row.status, row.confirmed_bid = status, confirmed
            if status == "UNKNOWN":
                await session.merge(
                    RuntimeRow(
                        key=f"fail_safe:{row.item_id}",
                        payload={
                            "reason": "API operation outcome unknown",
                            "operation_id": operation_id,
                            "at": datetime.now(UTC).isoformat(),
                        },
                    )
                )
            # Successful reconciliation does not automatically clear FAIL_SAFE/enable AUTO.

    async def details(self, operation_id):
        async with self.sessions() as session:
            row = await session.get(OperationRow, operation_id)
            if row is None:
                raise InvalidRequest("Unknown operation")
            decision = await session.get(DecisionRow, row.decision_id)
            return row, decision.payload
