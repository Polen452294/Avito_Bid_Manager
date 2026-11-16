import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, text

from app.database.models import (
    AdvertisementRow,
    BidSnapshotRow,
    BudgetRow,
    DecisionRow,
    OperationRow,
    RuntimeRow,
)
from app.domain.enums import Mode, PromotionModel
from app.domain.models import Advertisement, BidInfo, Decision

DEFAULT_OPTIONS = {
    "paused": False,
    "monitor_paused": False,
    "report_time": "09:00",
    "muted_alerts": [],
    "budget_thresholds": [80, 95],
}
logger = logging.getLogger(__name__)


class PanelState:
    def __init__(self, repository, settings, bidding):
        self.repository = repository
        self.sessions = repository.sessions
        self.settings = settings
        self.bidding = bidding
        self.lock = asyncio.Lock()

    async def options(self):
        async with self.sessions() as session:
            row = await session.get(RuntimeRow, "telegram:options")
            return {**DEFAULT_OPTIONS, **(row.payload if row else {})}

    async def load(self):
        async with self.sessions() as session:
            row = await session.get(BudgetRow, "account")
            if row:
                self.settings.account_daily_limit_kopecks = row.limit_kopecks

    async def apply(self, change, actor):
        kind, old, new, *metadata = change
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            if kind == "item":
                item = Advertisement.model_validate(new)
                if old is not None:
                    await self.validate_item(item, Advertisement.model_validate(old), session)
                await self.repository.write_item(session, item, expected=old)
                if metadata and metadata[0]:
                    info = BidInfo.model_validate(metadata[0])
                    if not all(
                        info.capabilities.allows(value, datetime.now(UTC))
                        for value in (item.min_bid, item.max_bid)
                    ):
                        raise ValueError("Verified limits have expired")
                    session.add(BidSnapshotRow(item_id=item.item_id, payload=metadata[0]))
            elif kind == "limit":
                row = await session.get(BudgetRow, "account")
                current = row.limit_kopecks if row else self.settings.account_daily_limit_kopecks
                if current != old or new <= 0:
                    raise ValueError("Budget changed")
                await session.merge(BudgetRow(key="account", limit_kopecks=new))
                await session.merge(
                    RuntimeRow(key="account_limit_revision", payload={"version": uuid4().hex})
                )
            elif kind == "options":
                row = await session.get(RuntimeRow, "telegram:options")
                if {**DEFAULT_OPTIONS, **(row.payload if row else {})} != old:
                    raise ValueError("Options changed")
                await session.merge(RuntimeRow(key="telegram:options", payload=new))
                if old["paused"] != new["paused"]:
                    await session.merge(
                        RuntimeRow(key="global_pause_revision", payload={"version": uuid4().hex})
                    )
            else:
                raise ValueError("Unknown change")
            stamp = datetime.now(UTC).isoformat()
            session.add(
                RuntimeRow(
                    key=f"telegram:audit:{stamp}:{actor}",
                    payload={"at": stamp, "actor": actor, "kind": kind, "old": old, "new": new},
                )
            )
        if kind == "limit":
            self.settings.account_daily_limit_kopecks = new
        logger.info("event=telegram_configuration_changed kind=%s", kind)
        if kind == "options" and old["paused"] != new["paused"]:
            logger.info("event=management_pause_changed paused=%s", new["paused"])

    async def validate_item(self, item, previous, session):
        if previous.confirmed_bid is not None and item.confirmed_bid != previous.confirmed_bid:
            raise ValueError("Use ExecutionService for simulator bid changes")
        if item.management_mode == Mode.ASSISTED and (
            item.promotion_model != PromotionModel.MOCK
            or (
                item.confirmed_bid is None
                and not getattr(self.bidding.provider, "bids", {}).get(item.item_id)
            )
            or not getattr(self.bidding.provider, "simulated", False)
        ):
            raise ValueError("ASSISTED requires simulator")
        if (item.min_bid, item.max_bid) == (previous.min_bid, previous.max_bid):
            return
        info = await self.bidding.stored_bid(session, item.item_id)
        if (
            info
            and info.data_type == "actual"
            and info.capabilities.model == item.promotion_model
            and info.capabilities.verified
            and not all(
                info.capabilities.allows(value, datetime.now(UTC))
                for value in (item.min_bid, item.max_bid)
            )
        ):
            raise ValueError("Outside verified API capabilities")

    async def proposals(self, *, include_one=None):
        async with self.sessions() as session:
            query = (
                select(OperationRow, DecisionRow, AdvertisementRow)
                .join(DecisionRow, DecisionRow.id == OperationRow.decision_id)
                .join(AdvertisementRow, AdvertisementRow.item_id == OperationRow.item_id)
            )
            query = query.where(AdvertisementRow.account_id == 1)
            if include_one is None:
                query = query.where(OperationRow.status == "AWAITING_CONFIRMATION")
            else:
                query = query.where(OperationRow.id == include_one)
            return (await session.execute(query.order_by(OperationRow.created_at.desc()))).all()

    async def resolve(self, operation_id, accept, actor):
        status = await self.bidding.execution.confirm_proposal(operation_id, accept, actor)
        logger.info("event=telegram_confirmation operation_id=%s status=%s", operation_id, status)
        return status

    @staticmethod
    def proposal_text(operation, saved, item):
        from app.telegram.analytics import money

        decision = Decision.model_validate(saved.payload)
        outcome = {
            "AWAITING_CONFIRMATION": "Изменение рассчитано · ожидает подтверждения",
            "SIMULATED": "Изменение выполнено в симуляторе",
            "SIMULATED_RECONCILED": "Изменение подтверждено чтением в симуляторе",
            "SENT": "Изменение отправлено · ожидает сверки",
            "UNKNOWN": "Результат требует сверки",
        }.get(operation.status, "Изменение не выполнено")
        return (
            f"🎯 Предложение {operation.id}\n{item.config['title'][:120]} · {operation.item_id}\n"
            f"{money(decision.old_bid)} → {money(operation.requested_bid)}\n"
            f"{decision.reason}\nLOCAL SIMULATOR · {operation.created_at}\n"
            f"Подтверждение до {decision.limits.get('confirmation_expires_at', '—')}\n"
            f"{outcome} · {operation.status}"
        )


def sync_monitor_job(scheduler, paused):
    job = scheduler.get_job("position_monitor") if scheduler else None
    if job and bool(job.next_run_time) == paused:
        if paused:
            job.pause()
        else:
            job.resume()
