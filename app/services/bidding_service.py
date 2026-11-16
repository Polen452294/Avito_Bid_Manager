import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.analytics.advisory import performance_conflict
from app.analytics.store import latest_spending
from app.avito.exceptions import AvitoError
from app.bidding.engine import BiddingEngine
from app.database.models import (
    AdvertisementRow,
    BidSnapshotRow,
    BudgetRow,
    DecisionRow,
    OperationRow,
    PositionRow,
    RuntimeRow,
)
from app.domain.enums import Mode
from app.domain.models import Advertisement, AppliedChange, BidInfo
from app.services.execution_service import APPLIED, UNRESOLVED, ExecutionService, proposal_revisions

_USE_STORED_SPENDING = object()
logger = logging.getLogger(__name__)


class BiddingService:
    def __init__(self, sessions, provider, engine: BiddingEngine, *, clock=None):
        self.sessions = sessions
        self.provider = provider
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()
        self.execution = ExecutionService(sessions, provider, engine, self.clock)

    async def evaluate_history(self, item, history, spending=_USE_STORED_SPENDING):
        return await self.evaluate(item, await history.history(item.item_id), spending)

    async def evaluate_new_history(self, item, history, *, mode=Mode.DRY_RUN, managed=False):
        key = f"bidding_history:{item.item_id}"
        async with self.sessions() as session:
            latest_id = await session.scalar(
                select(func.max(PositionRow.id)).where(PositionRow.item_id == item.item_id)
            )
            previous = await session.get(RuntimeRow, key)
            spending = await latest_spending(session, item.item_id)
            cut = (
                (spending.data_as_of or spending.checked_at).isoformat()
                if (self.engine.budget.reliable(item, spending, self.clock()))
                else None
            )
            if latest_id is None or (
                previous
                and previous.payload["id"] >= latest_id
                and previous.payload.get("budget_cut") == cut
                and (not managed or previous.payload.get("config") == item.model_dump(mode="json"))
            ):
                return None
        return await self.evaluate(
            item,
            await history.history(item.item_id, through_id=latest_id),
            _USE_STORED_SPENDING,
            history_cursor=(key, latest_id, cut),
            mode=mode,
            managed=managed,
        )

    async def evaluate(
        self,
        item,
        observations,
        spending,
        *,
        mode=Mode.DRY_RUN,
        owner_confirmed=False,
        last_change=None,
        changes_today=0,
        history_cursor=None,
        managed=False,
    ):
        async with self.lock:
            await self.execution.reconcile()
            info = None
            use_provider = self.provider is not None and (
                not managed or item.promotion_model.value == "MOCK"
            )
            if use_provider:
                try:
                    info = await self.provider.get_bid_info(item.item_id)
                except (AvitoError, TimeoutError, KeyError):
                    pass
            now = self.clock()
            async with self.sessions() as session:
                if not use_provider:
                    info = await self.stored_bid(session, item.item_id)
                use_stored = spending is _USE_STORED_SPENDING
                if use_stored:
                    spending = await latest_spending(session, item.item_id)
                fail_safe = await session.get(RuntimeRow, f"fail_safe:{item.item_id}")
                pending = await session.scalar(
                    select(OperationRow.id)
                    .where(
                        OperationRow.item_id == item.item_id, OperationRow.status.in_(UNRESOLVED)
                    )
                    .limit(1)
                )
                prior = (
                    await session.execute(
                        select(OperationRow, DecisionRow.payload)
                        .join(DecisionRow, DecisionRow.id == OperationRow.decision_id)
                        .where(
                            OperationRow.item_id == item.item_id, OperationRow.status.in_(APPLIED)
                        )
                        .order_by(OperationRow.id)
                    )
                ).all()
            applied = tuple(
                AppliedChange(
                    applied_at=datetime.fromisoformat(op.applied_at or op.created_at),
                    old_bid=payload["old_bid"],
                    new_bid=op.confirmed_bid,
                    restore_bid=payload.get("restore_bid"),
                    probe="manual_owner" not in payload.get("limits", {}),
                )
                for op, payload in prior
                if op.confirmed_bid is not None and payload.get("old_bid") is not None
            )
            decision = self.engine.decide(
                item,
                info,
                observations,
                spending,
                now,
                last_change=last_change,
                changes_today=changes_today,
                fail_safe=fail_safe is not None or pending is not None,
                applied=applied,
                mode=mode,
            )
            status = "HOLD"
            if decision.new_bid is not None:
                status = {
                    Mode.DRY_RUN: "DRY_RUN",
                    Mode.ASSISTED: "AWAITING_CONFIRMATION",
                    Mode.AUTO: "AUTO_DISABLED",
                }[mode]
            decision = decision.model_copy(update={"authorization": status})
            if managed and status == "AWAITING_CONFIRMATION":
                decision = decision.model_copy(
                    update={
                        "limits": {
                            **decision.limits,
                            "configuration": item.model_dump(mode="json"),
                            "account_limit": decision.limits["account_daily_budget"],
                            "confirmation_expires_at": (
                                now
                                + timedelta(
                                    minutes=self.engine.settings.telegram_confirmation_minutes
                                )
                            ).isoformat(),
                            "position_cursor": history_cursor[1] if history_cursor else None,
                        }
                    }
                )
            operation_id = str(uuid4())
            async with self.sessions.begin() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                if history_cursor is not None:
                    key, latest_id, cut = history_cursor
                    previous = await session.get(RuntimeRow, key)
                    config = item.model_dump(mode="json") if managed else None
                    if previous and (
                        previous.payload["id"] > latest_id
                        or previous.payload
                        == {"id": latest_id, "budget_cut": cut, "config": config}
                    ):
                        return None
                if managed:
                    row = await session.get(AdvertisementRow, item.item_id)
                    options = await session.get(RuntimeRow, "telegram:options")
                    if (
                        row is None
                        or row.account_id != 1
                        or Advertisement.model_validate(row.config) != item
                        or (options and options.payload.get("paused"))
                    ):
                        return None
                if managed and status == "AWAITING_CONFIRMATION":
                    versions = await proposal_revisions(session, item.item_id)
                    decision = decision.model_copy(
                        update={
                            "limits": {**decision.limits, "revisions": versions},
                        }
                    )
                if decision.new_bid is not None and mode != Mode.AUTO:
                    current_spending = await latest_spending(session, item.item_id)
                    account_limit = await session.get(BudgetRow, "account")
                    reason = None
                    if (
                        managed
                        and account_limit
                        and (decision.limits["account_daily_budget"] != account_limit.limit_kopecks)
                    ):
                        reason = "ACCOUNT_BUDGET_CHANGED"
                    elif (
                        use_stored or current_spending is not None
                    ) and current_spending != spending:
                        reason = "BUDGET_CHANGED_DURING_EVALUATION"
                    elif advisory := await performance_conflict(
                        session, item, decision, now, self.engine.settings
                    ):
                        reason = "STRATEGY_CONFLICT"
                        decision = decision.model_copy(
                            update={
                                "limits": {**decision.limits, "performance_reason": advisory},
                            }
                        )
                    elif (
                        current_spending is not None
                        and decision.new_bid > decision.old_bid
                        and not await self.engine.budget.claim_increase(
                            session, "decision", spending, operation_id
                        )
                    ):
                        reason = "ACCOUNT_BUDGET_SNAPSHOT_CLAIMED"
                    if not reason and decision.reason_code == "FLOOR_REVIEW_PROBE":
                        key = f"floor_review:{item.item_id}"
                        failure = decision.limits["floor_review_failure_at"]
                        previous = await session.get(RuntimeRow, key)
                        if (
                            previous
                            and previous.payload["failure"] == failure
                            and (now - datetime.fromisoformat(previous.payload["at"])).days
                            < self.engine.settings.floor_review_days
                        ):
                            reason = "FLOOR_REVIEW_ALREADY_PROPOSED"
                        else:
                            await session.merge(
                                RuntimeRow(
                                    key=key, payload={"failure": failure, "at": now.isoformat()}
                                )
                            )
                    if reason:
                        status = "HOLD"
                        decision = decision.model_copy(
                            update={
                                "authorization": status,
                                "action": "HOLD",
                                "reason": reason,
                                "reason_code": reason,
                                "new_bid": None,
                                "restore_bid": None,
                                "expected_result": None,
                            }
                        )
                row = DecisionRow(
                    item_id=item.item_id,
                    created_at=now.isoformat(),
                    schema_version=2,
                    payload=decision.model_dump(mode="json"),
                )
                session.add(row)
                if history_cursor is not None:
                    key, latest_id, cut = history_cursor
                    previous = await session.get(RuntimeRow, key)
                    if previous is None or previous.payload["id"] <= latest_id:
                        await session.merge(
                            RuntimeRow(
                                key=key,
                                payload={
                                    "id": latest_id,
                                    "budget_cut": cut,
                                    "config": item.model_dump(mode="json") if managed else None,
                                },
                            )
                        )
                await session.flush()
                if decision.new_bid is not None:
                    session.add(
                        OperationRow(
                            id=operation_id,
                            item_id=item.item_id,
                            decision_id=row.id,
                            requested_bid=decision.new_bid,
                            status=status,
                            created_at=now.isoformat(),
                        )
                    )
            logger.info(
                "event=bidding_decision decision_id=%s item_id=%s operation_id=%s "
                "status=%s reason=%s",
                row.id,
                item.item_id,
                operation_id if decision.new_bid is not None else None,
                status,
                decision.reason_code,
            )
            if status == "AWAITING_CONFIRMATION" and owner_confirmed:
                status = await self.execution.execute(
                    operation_id, item, decision, spending, mode, owner_confirmed
                )
                if status == "EXECUTION_BLOCKED":
                    async with self.sessions.begin() as session:
                        operation = await session.get(OperationRow, operation_id)
                        operation.status = status
                        row = await session.get(DecisionRow, operation.decision_id)
                        row.payload = {**row.payload, "authorization": status}
                decision = decision.model_copy(update={"authorization": status})
                if status == "PREFLIGHT_BLOCKED":
                    async with self.sessions() as session:
                        row = await session.get(DecisionRow, row.id)
                        decision = type(decision).model_validate(row.payload)
            return decision, status

    @staticmethod
    async def stored_bid(session, item_id):
        table = BidSnapshotRow
        payload = await session.scalar(
            select(table.payload)
            .where(table.item_id == item_id)
            .order_by(
                func.julianday(table.payload["checked_at"].as_string()).desc(), table.id.desc()
            )
            .limit(1)
        )
        try:
            return BidInfo.model_validate(payload) if payload is not None else None
        except ValidationError:
            return None
