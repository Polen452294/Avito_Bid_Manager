import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select, text

from app.analytics.metrics import ratio
from app.analytics.store import day_bounds, latest_spending
from app.avito.exceptions import AuthenticationError
from app.bidding.budget import BudgetController
from app.database.models import NotificationRow, OperationRow, RuntimeRow
from app.positions.analyzer import PositionAnalyzer
from app.positions.validation import context_key, usable
from app.services.execution_service import APPLIED
from app.telegram.analytics import money, number
from app.telegram.panel import proposal_markup

logger = logging.getLogger(__name__)


class TelegramJobs:
    def __init__(self, state, analytics, *, bot_factory=Bot, clock=None):
        self.state = state
        self.settings = state.settings
        self.analytics = analytics
        self.sessions = state.sessions
        self.bot_factory = bot_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.zone = ZoneInfo(self.settings.timezone)
        self.lock = asyncio.Lock()
        self.budget = BudgetController(self.settings)

    async def deliver(self, key, content, *, markup=None, priority="INFO", once=False):
        async with self.lock:
            async with self.bot_factory(self.settings.telegram_bot_token.get_secret_value()) as bot:
                for admin in self.settings.telegram_admin_ids:
                    delivery_key = f"telegram:delivery:{key}:{admin}"
                    async with self.sessions.begin() as session:
                        await session.execute(text("BEGIN IMMEDIATE"))
                        row = await session.get(NotificationRow, delivery_key)
                        if row and (
                            row.state in {"SENT", "REJECTED"} or (once and row.state == "CLAIMED")
                        ):
                            continue
                        now = self.clock()
                        last = await session.get(NotificationRow, f"telegram:rate:{admin}")
                        if last and now - datetime.fromisoformat(last.sent_at) < timedelta(
                            seconds=1
                        ):
                            continue
                        if row and now - datetime.fromisoformat(row.sent_at) < timedelta(minutes=1):
                            continue
                        stamp = now.isoformat()
                        await session.merge(
                            NotificationRow(key=delivery_key, state="CLAIMED", sent_at=stamp)
                        )
                        await session.merge(
                            NotificationRow(
                                key=f"telegram:rate:{admin}", state="CLAIMED", sent_at=stamp
                            )
                        )
                    try:
                        await bot.send_message(
                            admin, f"[{priority}] {content}"[:4000], reply_markup=markup
                        )
                        status = "SENT"
                    except (TelegramBadRequest, TelegramForbiddenError):
                        status = "REJECTED"
                    except TelegramRetryAfter as exc:
                        status = "DEFERRED"
                        async with self.sessions.begin() as session:
                            rate = await session.get(NotificationRow, f"telegram:rate:{admin}")
                            rate.sent_at = (now + timedelta(seconds=exc.retry_after)).isoformat()
                    except Exception as exc:
                        logger.warning("Telegram delivery uncertain: %s", type(exc).__name__)
                        status = "CLAIMED" if once else "FAILED"
                    async with self.sessions.begin() as session:
                        row = await session.get(NotificationRow, delivery_key)
                        row.state = status

    async def transition(self, category, scope, status, alert, *, critical=False):
        key = f"telegram:event:{category}:{scope}"
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(RuntimeRow, key)
            if row is None or row.payload["status"] != status:
                previous = row.payload["status"] if row else None
                if alert is None and previous not in (None, "OK"):
                    alert = f"Состояние восстановлено · {category} · {scope}"
                payload = {
                    "status": status,
                    "sequence": row.payload["sequence"] + 1 if row else 1,
                    "at": self.clock().isoformat(),
                    "alert": alert,
                    "critical": critical,
                    "category": category,
                }
                await session.merge(RuntimeRow(key=key, payload=payload))
                if critical and alert:
                    session.add(
                        RuntimeRow(
                            key=f"telegram:critical:{payload['at']}:{category}:{scope}:{payload['sequence']}",
                            payload=payload,
                        )
                    )

    async def api_failure(self, exc=None):
        category = "Avito API"
        label = (
            (
                "Авторизация Avito недоступна"
                if isinstance(exc, AuthenticationError)
                else f"Ошибка чтения Avito API · {type(exc).__name__}"
            )
            if exc
            else None
        )
        await self.transition(
            category, "statistics", type(exc).__name__ if exc else "OK", label, critical=True
        )

    async def positions(self, item, rows):
        result = PositionAnalyzer().analyze(item, rows, self.clock())
        for query in item.queries:
            key = context_key(item.item_id, query)
            latest = {}
            for row in rows:
                if context_key(row.item_id, row.search_query, row.region, row.filters) == key:
                    latest[row.provider, row.position_type] = row
            available = any(
                usable(
                    row,
                    self.clock(),
                    timedelta(minutes=self.settings.max_position_data_age_minutes),
                )
                for row in latest.values()
                if row.provider in item.position_sources and row.provider != "mock"
            )
            worsening = any(
                r["context"] == key and r["trend"] == "WORSENING" and r["provider"] != "mock"
                for r in result
            )
            await self.transition(
                "Позиции",
                key,
                "WORSENING" if worsening else "OK" if available else "UNAVAILABLE",
                f"Позиции ухудшаются · {item.item_id} · {query.text[:80]}"
                if worsening
                else None
                if available
                else f"Нет достоверных позиций · {item.item_id} · {query.text[:80]}",
            )

    async def scan(self):
        opt = await self.state.options()
        await self.transition(
            "Управление",
            "account",
            "PAUSED" if opt["paused"] else "OK",
            "Локальное управление приостановлено." if opt["paused"] else None,
            critical=True,
        )
        now = self.clock()
        account_warning = 0
        for item in await self.state.repository.items():
            async with self.sessions() as session:
                spending = await latest_spending(session, item.item_id)
                guard = await session.get(RuntimeRow, f"fail_safe:{item.item_id}")
            scope = str(item.item_id)
            await self.transition(
                "FAIL_SAFE",
                scope,
                "BLOCKED" if guard else "OK",
                f"FAIL_SAFE · объявление {scope}" if guard else None,
                critical=True,
            )
            if item.promotion_model.value == "MOCK":
                continue
            warnings = self.budget.warnings(item, spending, now, opt["budget_thresholds"])
            threshold = warnings.get("item", 0)
            account_warning = max(account_warning, warnings.get("account", 0))
            await self.transition(
                "Бюджеты",
                scope,
                str(threshold) if threshold else "OK",
                f"Достигнут порог {threshold}% бюджета объявления {scope}." if threshold else None,
            )
            problem = not self.budget.reliable(item, spending, now)
            await self.transition(
                "Финансы",
                scope,
                "UNAVAILABLE" if problem else "OK",
                f"Нет достоверных расходов · объявление {scope}" if problem else None,
                critical=True,
            )
            recent = await self.state.repository.decisions(item.item_id, 1)
            ceiling = bool(recent and recent[0].get("reason_code") == "MAX_BID_REACHED")
            await self.transition(
                "Стратегии",
                scope,
                "AT_LIMIT" if ceiling else "OK",
                f"Достигнут предел ставки · объявление {scope}" if ceiling else None,
            )
        await self.transition(
            "Бюджеты",
            "account",
            str(account_warning) if account_warning else "OK",
            f"Достигнут порог {account_warning}% бюджета аккаунта." if account_warning else None,
        )
        async with self.sessions() as session:
            events = list(
                await session.scalars(
                    select(RuntimeRow)
                    .where(RuntimeRow.key.like("telegram:event:%"))
                    .order_by(RuntimeRow.key)
                )
            )
        for row in sorted(events, key=lambda event: not event.payload["critical"]):
            event = row.payload
            if event["alert"] and (
                event["critical"] or event["category"] not in opt["muted_alerts"]
            ):
                await self.deliver(
                    f"{row.key}:{event['sequence']}",
                    event["alert"],
                    priority="CRITICAL" if event["critical"] else "WARNING",
                )

    async def proposals(self):
        now = self.clock()
        for op, row, item in await self.state.proposals():
            expiry = row.payload.get("limits", {}).get("confirmation_expires_at")
            if not expiry:
                continue
            if now >= datetime.fromisoformat(expiry):
                await self.state.resolve(op.id, False, None)
            elif "manual_owner" not in row.payload["limits"]:
                await self.deliver(
                    op.id,
                    self.state.proposal_text(op, row, item),
                    markup=proposal_markup(
                        op.id, self.settings.telegram_bot_token.get_secret_value()
                    ),
                )
        async with self.sessions() as session:
            outcomes = list(
                await session.scalars(
                    select(OperationRow)
                    .where(
                        OperationRow.status.in_((*APPLIED, "REJECTED", "PREFLIGHT_BLOCKED")),
                        OperationRow.created_at >= (now - timedelta(days=1)).isoformat(),
                    )
                    .order_by(OperationRow.created_at.desc())
                    .limit(20)
                )
            )
        for op in outcomes:
            await self.deliver(
                f"result:{op.id}", f"LOCAL SIMULATOR · #{op.item_id} · {op.status} · {op.id}"
            )

    async def report(self):
        now = self.clock().astimezone(self.zone)
        opt = await self.state.options()
        if now.strftime("%H:%M") < opt["report_time"]:
            return
        day = now.date() - timedelta(days=1)
        key = f"report:{day}"
        async with self.sessions() as session:
            existing = [
                await session.get(NotificationRow, f"telegram:delivery:{key}:{admin}")
                for admin in self.settings.telegram_admin_ids
            ]
        if all(row and row.state in {"SENT", "CLAIMED", "REJECTED"} for row in existing):
            return
        daily = await self.analytics.reports(1, now=now)
        weekly = await self.analytics.reports(7, now=now)
        recommendations = {r["item"].item_id: r["recommendation"] for r in weekly}
        allocations = self.analytics.budget_recommendations(weekly)
        spent = [r["actual"].spend for r in daily]
        sources = {r["actual"].metric_sources[0] for r in daily if r["actual"].spend is not None}
        total = (
            sum(spent)
            if spent and all(v is not None for v in spent) and len(sources) == 1
            else None
        )
        start, end = (stamp.astimezone(UTC) for stamp in day_bounds(day, self.zone))
        contacts = [r["actual"].contacts for r in daily]
        total_contacts = (
            sum(contacts) if contacts and all(v is not None for v in contacts) else None
        )
        comparable = all(
            r["actual"].metric_sources[0] == r["actual"].metric_sources[2] for r in daily
        )
        total_cpl = ratio(total, total_contacts * 100) if total_contacts and comparable else None
        async with self.sessions() as session:
            changes = await session.scalar(
                select(func.count())
                .select_from(OperationRow)
                .where(
                    OperationRow.status.in_(APPLIED),
                    OperationRow.applied_at >= start.isoformat(),
                    OperationRow.applied_at < end.isoformat(),
                )
            )
            critical = list(
                await session.scalars(
                    select(RuntimeRow).where(
                        RuntimeRow.key.like("telegram:event:%"),
                        RuntimeRow.payload["critical"].as_boolean().is_(True),
                    )
                )
            )
            yesterday_events = list(
                await session.scalars(
                    select(RuntimeRow)
                    .where(
                        RuntimeRow.key.like("telegram:critical:%"),
                        RuntimeRow.payload["at"].as_string() >= start.isoformat(),
                        RuntimeRow.payload["at"].as_string() < end.isoformat(),
                    )
                    .order_by(RuntimeRow.key.desc())
                    .limit(10)
                )
            )
        lines = [
            f"📊 Ежедневный отчёт за {day:%d.%m.%Y} · {self.settings.timezone}",
            f"Активных: {sum(r['item'].active and not opt['paused'] for r in daily)}",
            f"Подтверждённые расходы управляемых объявлений: {money(total)}",
            f"Обращения: {total_contacts if total_contacts is not None else 'нет данных'}"
            f" · CPL {number(total_cpl, ' ₽')}",
            f"Изменения ставок LOCAL SIMULATOR: {changes}",
        ]
        for report in daily:
            item, actual, previous = report["item"], report["actual"], report["previous"]
            item_id = item.item_id
            position = "нет сопоставимых данных"
            if actual.position_context and previous.position_context == actual.position_context:
                position = f"{actual.position - previous.position:+.2f} к предыдущему дню"
            advice = [
                recommendations.get(item_id),
                *(a for a in allocations if a.item_id == item_id),
            ]
            labels = ", ".join(r.reason_code for r in advice if r) or "нет"
            lines.append(
                f"{item.title[:70]} · #{item_id} · {money(actual.spend)} · "
                f"обращения {actual.contacts if actual.contacts is not None else '—'} · "
                f"CPL {number(actual.cpl, ' ₽')}\n"
                f"Позиция: {position} · бюджет: {report['budget_status']}\n"
                f"Рекомендации за 7 дней: {labels}"
            )
        lines.append("Критические события и текущие блокировки:")
        alerts = [
            row.payload["alert"]
            for row in critical
            if row.payload["alert"] and row.payload["status"] != "OK"
        ]
        alerts.extend(row.payload["alert"] for row in yesterday_events)
        lines.extend(dict.fromkeys(alerts) if alerts else ["Нет записей."])
        await self.deliver(key, "\n\n".join(lines), once=True)

    async def tick(self):
        await self.proposals()
        await self.report()
        await self.scan()
