import logging
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, text

from app.analytics.advisory import performance_conflict
from app.analytics.store import latest_spending
from app.avito.bidding import MockBidProvider
from app.avito.exceptions import CapabilityBlocked
from app.avito.simulator import LocalSimulator
from app.database.models import (
    AdvertisementRow,
    BudgetRow,
    DecisionRow,
    OperationRow,
    PositionRow,
    RuntimeRow,
)
from app.domain.enums import Mode, PromotionModel
from app.domain.models import Advertisement, AppliedChange, Decision

UNRESOLVED = ("PENDING", "SENT", "UNKNOWN")
APPLIED = ("SIMULATED", "SIMULATED_RECONCILED")
logger = logging.getLogger(__name__)


async def proposal_revisions(session, item_id):
    revisions = {}
    for key in (f"item_revision:{item_id}", "account_limit_revision", "global_pause_revision"):
        row = await session.get(RuntimeRow, key)
        if row is not None:
            revisions[key] = row.payload["version"]
    return revisions


class ExecutionService:
    """Dispatches only owner-authorized simulator changes and reconciles by reading."""

    def __init__(self, sessions, provider, engine, clock):
        self.sessions = sessions
        self.provider = provider
        self.engine = engine
        self.clock = clock

    async def propose_manual_bid(self, item, bid, actor):
        """Record a local simulator proposal. Both preview and execution check the budget."""
        if (
            not isinstance(self.provider, LocalSimulator)
            or item.promotion_model != PromotionModel.MOCK
        ):
            raise ValueError("Ручная запись доступна только в LOCAL SIMULATOR.")
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            current = await session.get(AdvertisementRow, item.item_id)
            options = await session.get(RuntimeRow, "telegram:options")
            if (
                current is None
                or current.account_id != 1
                or Advertisement.model_validate(current.config) != item
                or not item.active
                or item.archived
                or item.management_mode != Mode.ASSISTED
                or (options and options.payload.get("paused"))
            ):
                raise ValueError("Нужны активное объявление в ASSISTED и включённое управление.")
            try:
                info = await self.provider.get_bid_info(item.item_id, session=session)
            except CapabilityBlocked:
                raise ValueError("Сначала задайте стартовую ставку симулятора.") from None
            now = self.clock()
            if bid == info.bid or not info.capabilities.allows(bid, now):
                raise ValueError("Введите новую ставку в пределах допустимого диапазона.")
            spending = await latest_spending(session, item.item_id)
            if reason := self.engine.budget.check(item, spending, now):
                raise ValueError(f"Локальный бюджет заблокировал изменение · {reason}")
            account_limit = await session.get(BudgetRow, "account")
            decision = Decision(
                item_id=item.item_id,
                old_bid=info.bid,
                new_bid=bid,
                action="INCREASE" if bid > info.bid else "DECREASE",
                reason="Ручная ставка · LOCAL SIMULATOR",
                reason_code="MANUAL_SIMULATOR_BID",
                created_at=now,
                authorization="AWAITING_CONFIRMATION",
                limits={
                    "manual_owner": actor,
                    "configuration": item.model_dump(mode="json"),
                    "confirmation_expires_at": (
                        now + timedelta(minutes=self.engine.settings.telegram_confirmation_minutes)
                    ).isoformat(),
                    "account_limit": (
                        account_limit.limit_kopecks
                        if account_limit
                        else self.engine.settings.account_daily_limit_kopecks
                    ),
                    "spending": spending.model_dump(mode="json"),
                    "revisions": await proposal_revisions(session, item.item_id),
                },
            )
            row = DecisionRow(
                item_id=item.item_id,
                created_at=now.isoformat(),
                schema_version=2,
                payload=decision.model_dump(mode="json"),
            )
            session.add(row)
            await session.flush()
            operation_id = str(uuid4())
            session.add(
                OperationRow(
                    id=operation_id,
                    item_id=item.item_id,
                    decision_id=row.id,
                    requested_bid=bid,
                    status="AWAITING_CONFIRMATION",
                    created_at=now.isoformat(),
                )
            )
        return operation_id

    async def confirm_proposal(self, operation_id, accept, actor):
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            operation = await session.get(OperationRow, operation_id)
            current = await session.get(AdvertisementRow, operation.item_id) if operation else None
            if not current or current.account_id != 1:
                return "NOT_FOUND"
            if operation.status != "AWAITING_CONFIRMATION":
                return operation.status
            saved = await session.get(DecisionRow, operation.decision_id)
            expiry = saved.payload.get("limits", {}).get("confirmation_expires_at")
            if not expiry:
                return "NOT_FOUND"
            owner = saved.payload["limits"].get("manual_owner")
            expired = self.clock() >= datetime.fromisoformat(expiry)
            if owner is not None and owner != actor and not (actor is None and expired):
                return "NOT_FOUND"
            config = saved.payload["limits"]["configuration"]
            if expired or not accept:
                operation.status = "EXPIRED" if expired else "REJECTED"
                saved.payload = {
                    **saved.payload,
                    "authorization": operation.status,
                    "limits": {**saved.payload["limits"], "owner": actor},
                }
                return operation.status
            item = Advertisement.model_validate(config)
            decision = Decision.model_validate(saved.payload)
            spending = await latest_spending(session, item.item_id)
        return await self.execute(
            operation_id, item, decision, spending, Mode.ASSISTED, True, actor=actor
        )

    async def execute(
        self, operation_id, item, decision, spending, mode, owner_confirmed, *, actor=None
    ):
        if (
            mode != Mode.ASSISTED
            or not owner_confirmed
            or decision.new_bid is None
            or not isinstance(self.provider, MockBidProvider)
            or not self.provider.simulated
        ):
            return "EXECUTION_BLOCKED"

        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            operation = await session.get(OperationRow, operation_id)
            saved = await session.get(DecisionRow, operation.decision_id) if operation else None
            if (
                not operation
                or operation.status != "AWAITING_CONFIRMATION"
                or operation.item_id != item.item_id
                or operation.requested_bid != decision.new_bid
                or saved is None
                or saved.payload != decision.model_dump(mode="json")
            ):
                return "EXECUTION_BLOCKED"

            current = await session.get(AdvertisementRow, item.item_id)

            def block(reason):
                logger.info(
                    "event=execution_blocked operation_id=%s reason=%s", operation_id, reason
                )
                operation.status = "PREFLIGHT_BLOCKED"
                saved.payload = {
                    **saved.payload,
                    "authorization": operation.status,
                    "action": "HOLD",
                    "new_bid": None,
                    "expected_result": None,
                    "restore_bid": None,
                    "reason": reason,
                    "reason_code": reason,
                    "limits": {
                        **decision.limits,
                        "original_reason": decision.reason_code,
                        "owner": actor,
                    },
                }
                return operation.status

            conflict = await session.scalar(
                select(OperationRow.id)
                .where(
                    OperationRow.id != operation_id,
                    or_(
                        OperationRow.status.in_(UNRESOLVED),
                        OperationRow.status.in_(APPLIED)
                        & (
                            (OperationRow.applied_at >= decision.created_at.isoformat())
                            | (OperationRow.decision_id > operation.decision_id)
                        ),
                    ),
                )
                .limit(1)
            )
            if conflict or await session.get(RuntimeRow, f"fail_safe:{item.item_id}"):
                return block("ACCOUNT_OPERATION_CONFLICT")
            if (
                current is None
                or current.account_id != 1
                or Advertisement.model_validate(current.config) != item
            ):
                return block("ITEM_CONFIGURATION_CHANGED")

            expiry = decision.limits.get("confirmation_expires_at")
            manual = "manual_owner" in decision.limits
            if expiry:
                if actor is None or self.clock() >= datetime.fromisoformat(expiry):
                    return block("CONFIRMATION_EXPIRED")
                options = await session.get(RuntimeRow, "telegram:options")
                if (
                    not item.active
                    or item.archived
                    or item.management_mode != Mode.ASSISTED
                    or (options and options.payload.get("paused"))
                ):
                    return block("MANAGEMENT_PAUSED_OR_MODE_CHANGED")
                limit = await session.get(BudgetRow, "account")
                if decision.limits["account_limit"] != (
                    limit.limit_kopecks
                    if limit
                    else self.engine.settings.account_daily_limit_kopecks
                ):
                    return block("ACCOUNT_BUDGET_CHANGED")
                if not manual:
                    latest_position_id = await session.scalar(
                        select(func.max(PositionRow.id)).where(PositionRow.item_id == item.item_id)
                    )
                    if decision.limits["position_cursor"] != latest_position_id:
                        return block("POSITION_OBSERVATIONS_CHANGED")
                if decision.limits.get("revisions", {}) != await proposal_revisions(
                    session, item.item_id
                ):
                    return block("CONFIGURATION_REVISION_CHANGED")

            try:
                latest = (
                    await self.provider.get_bid_info(item.item_id, session=session)
                    if isinstance(self.provider, LocalSimulator)
                    else await self.provider.get_bid_info(item.item_id)
                )
            except Exception:
                return block("BID_READ_UNAVAILABLE")
            now = self.clock()
            stored = await latest_spending(session, item.item_id)
            if stored is not None and stored != spending:
                reason = self.engine.budget.check(item, stored, now)
                if reason == "UNRELIABLE_SPENDING":
                    return block("BUDGET_DATA_UNAVAILABLE")
                return block(reason or "BUDGET_CHANGED_BEFORE_EXECUTION")
            spending = stored if stored is not None else spending
            if reason := self.engine.budget.check(item, spending, now):
                return block(
                    "BUDGET_DATA_UNAVAILABLE" if reason == "UNRELIABLE_SPENDING" else reason
                )
            if manual:
                if (
                    not isinstance(self.provider, LocalSimulator)
                    or item.promotion_model != PromotionModel.MOCK
                    or decision.limits["manual_owner"] != actor
                    or latest.bid != decision.old_bid
                    or not latest.capabilities.allows(decision.new_bid, now)
                    or spending.model_dump(mode="json") != decision.limits["spending"]
                    or now < decision.created_at
                    or now >= datetime.fromisoformat(expiry)
                ):
                    return block("MANUAL_PREVIEW_CHANGED")
            elif reason := await self._strategy_preflight(
                session, item, decision, spending, latest, now, mode, expiry
            ):
                return block(reason)
            if decision.new_bid > decision.old_bid and not await self.engine.budget.claim_increase(
                session, "execution", spending, operation_id
            ):
                return block("ACCOUNT_BUDGET_SNAPSHOT_CLAIMED")
            if isinstance(self.provider, LocalSimulator):
                result = await self.provider.apply_in_session(
                    session, item.item_id, decision.new_bid, operation_id
                )
                operation.status = "SIMULATED"
                operation.confirmed_bid = result.confirmed_bid
                operation.applied_at = now.isoformat()
            else:
                operation.status = "PENDING"
            saved.payload = {
                **saved.payload,
                "authorization": operation.status,
                "limits": {**decision.limits, "owner": actor},
            }

        if operation.status == "SIMULATED":
            logger.info("event=execution_simulated operation_id=%s", operation_id)
            return operation.status

        try:
            await self.provider.set_bid(item.item_id, decision.new_bid, operation_id)
            confirmed = await self._read_confirmation(operation, item.promotion_model)
            if confirmed is None:
                raise CapabilityBlocked("Write verification mismatch")
        except Exception:
            confirmed = await self._read_confirmation(operation, item.promotion_model)
            return await self._record(
                operation_id, confirmed, "SIMULATED_RECONCILED" if confirmed else "UNKNOWN"
            )
        return await self._record(operation_id, confirmed, "SIMULATED")

    async def _strategy_preflight(
        self, session, item, decision, spending, latest, now, mode, expiry
    ):
        refreshed = self.engine.decide(
            item,
            latest,
            list(decision.observations),
            spending,
            now,
            last_change=(
                datetime.fromisoformat(decision.limits["last_applied_at"])
                if decision.limits["last_applied_at"]
                else None
            ),
            changes_today=decision.limits["changes_today"],
            effective_floor=decision.limits["effective_floor"],
            mode=mode if expiry else Mode.DRY_RUN,
            applied=tuple(
                AppliedChange.model_validate(change) for change in decision.limits["applied"]
            ),
        )
        if (
            latest.bid != decision.old_bid
            or refreshed.new_bid != decision.new_bid
            or now < decision.created_at
            or (
                now >= datetime.fromisoformat(expiry)
                if expiry
                else now - decision.created_at > timedelta(minutes=1)
            )
        ):
            return f"DECISION_CHANGED:{refreshed.reason_code}"
        if reason := await performance_conflict(
            session, item, refreshed, now, self.engine.settings
        ):
            return f"STRATEGY_CONFLICT:{reason}"
        return None

    async def _read_confirmation(self, operation, model):
        if isinstance(self.provider, LocalSimulator) and not await self.provider.has_receipt(
            operation.id
        ):
            return None
        try:
            info = await self.provider.get_bid_info(operation.item_id)
        except Exception:
            return None
        now = self.clock()
        if (
            info.item_id == operation.item_id
            and info.capabilities.model == model
            and info.bid == operation.requested_bid
            and datetime.fromisoformat(operation.created_at) <= info.checked_at <= now
            and now - info.checked_at <= timedelta(minutes=15)
            and info.capabilities.allows(info.bid, now)
        ):
            return info
        return None

    async def _record(self, operation_id, info, status, *, recovering=False):
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            operation = await session.get(OperationRow, operation_id)
            if recovering and operation.status != "UNKNOWN":
                return operation.status
            operation.status = status
            if info is not None:
                operation.confirmed_bid = info.bid
                operation.applied_at = self.clock().isoformat()
                pending = await session.scalar(
                    select(OperationRow.id)
                    .where(
                        OperationRow.item_id == operation.item_id,
                        OperationRow.id != operation_id,
                        OperationRow.status.in_(UNRESOLVED),
                    )
                    .limit(1)
                )
                guard = await session.get(RuntimeRow, f"fail_safe:{operation.item_id}")
                if (
                    not pending
                    and guard
                    and guard.payload.get("reason")
                    in {"Uncertain write", "Unfinished operation requires provider reconciliation"}
                ):
                    await session.delete(guard)
            else:
                key = f"fail_safe:{operation.item_id}"
                if await session.get(RuntimeRow, key) is None:
                    session.add(RuntimeRow(key=key, payload={"reason": "Uncertain write"}))
            saved = await session.get(DecisionRow, operation.decision_id)
            if saved.schema_version >= 2:
                saved.payload = {**saved.payload, "authorization": status}
        logger.info("event=execution_outcome operation_id=%s status=%s", operation_id, status)
        return status

    async def reconcile(self):
        if not isinstance(self.provider, MockBidProvider) or not self.provider.simulated:
            return 0
        async with self.sessions() as session:
            operations = (
                await session.execute(
                    select(OperationRow, DecisionRow.payload)
                    .join(DecisionRow, DecisionRow.id == OperationRow.decision_id)
                    .where(OperationRow.status == "UNKNOWN", DecisionRow.schema_version == 2)
                )
            ).all()
        reconciled = 0
        for operation, decision in operations:
            capabilities = decision["limits"].get("capabilities") or {}
            model = capabilities.get("model", PromotionModel.MOCK)
            if (confirmed := await self._read_confirmation(operation, model)) is not None:
                await self._record(operation.id, confirmed, "SIMULATED_RECONCILED", recovering=True)
                reconciled += 1
        return reconciled
