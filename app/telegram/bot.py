import json
import logging
from datetime import UTC, datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import Message
from pydantic import TypeAdapter

from app.bidding.engine import BiddingEngine
from app.domain.models import Advertisement, SearchQuery
from app.positions.history import PositionHistory
from app.positions.validation import context_key, usable
from app.services.analytics_service import AnalyticsService
from app.services.bidding_service import BiddingService
from app.telegram.analytics import format_report, money
from app.telegram.panel import STRATEGY, TelegramPanel
from app.telegram.state import PanelState

STRATEGY_FIELDS = {
    *STRATEGY,
    "increase_step_percent",
    "decrease_step_percent",
    "stabilization_minutes",
    "increase_interval_minutes",
    "hysteresis_positions",
    "primary_query_index",
    "max_query_skew_minutes",
    "position_sources",
}


class AdminOnly(BaseMiddleware):
    def __init__(self, admin_ids, lock=None):
        self.admin_ids = frozenset(admin_ids)
        self.lock = lock

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        # Only private chats: avoid leaking account data to group members.
        message = getattr(event, "message", None) or event
        chat = getattr(message, "chat", None)
        if user is None or user.id not in self.admin_ids or chat is None or chat.type != "private":
            return None
        try:
            if self.lock is None:
                return await handler(event, data)
            async with self.lock:
                return await handler(event, data)
        except Exception as exc:
            logging.getLogger(__name__).warning("Telegram update failed: %s", type(exc).__name__)
            if not isinstance(exc, TelegramAPIError) and isinstance(message, Message):
                try:
                    await message.answer("Операция недоступна. Проверьте состояние и журнал.")
                except TelegramAPIError:
                    pass
            return None


def make_dispatcher(
    settings,
    repository,
    bidding=None,
    analytics=None,
    *,
    panel_state=None,
    scheduler=None,
    monitor=None,
):
    bidding = bidding or BiddingService(repository.sessions, None, BiddingEngine(settings))
    analytics = analytics or AnalyticsService(repository, settings)
    history = PositionHistory(repository.sessions)
    router = Router()
    panel_state = panel_state or PanelState(repository, settings, bidding)
    guard = AdminOnly(settings.telegram_admin_ids, panel_state.lock)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        panel.cancel_change(message.from_user.id)
        await state.clear()
        await panel.show(message, "Редактирование отменено.")

    @router.message(Command("start", "help"))
    async def help_command(message: Message, state: FSMContext):
        await state.clear()
        await panel.home(message)

    @router.message(Command("positions", "position_history"))
    async def positions(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("/positions ID или /position_history ID")
            return
        item_id = int(parts[1])
        if await repository.item(item_id) is None:
            return await message.answer("Объявление не найдено.")
        rows = await history.history(item_id)
        if parts[0].split("@")[0] == "/positions":
            item = await repository.item(item_id)
            configured = {context_key(item_id, q) for q in item.queries} if item else set()
            latest = {}
            for row in rows:
                key = context_key(item_id, row.search_query, row.region, row.filters)
                if key in configured:
                    latest[key, row.provider] = row
            max_age = timedelta(minutes=settings.max_position_data_age_minutes)
            rows = [r for r in latest.values() if usable(r, datetime.now(UTC), max_age)]
        lines = [
            f"{r.search_query[:80]} / {r.region[:50]} · {r.position or r.status} · "
            f"{r.position_type} · {r.provider[:50]} · {r.confidence} · {r.checked_at.isoformat()}"
            for r in reversed(rows[-10:])
        ]
        await message.answer("\n".join(lines) or "Нет достоверных данных")

    @router.message(Command("queries"))
    async def queries(message: Message):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit():
            await message.answer('/queries ID [{"text":"запрос","region":"Москва"}]')
            return
        item = await repository.item(int(parts[1]))
        if item is None:
            await message.answer("Объявление не найдено.")
            return
        try:
            configured = TypeAdapter(tuple[SearchQuery, ...]).validate_json(parts[2])
            updated = Advertisement.model_validate({**item.model_dump(), "queries": configured})
        except ValueError:
            await message.answer("Некорректные настройки запросов.")
            return
        await panel.preview(
            message, ("item", item.model_dump(mode="json"), updated.model_dump(mode="json"))
        )

    @router.message(Command("strategy", "budget", "history", "recommend"))
    async def bidding_command(message: Message, state: FSMContext):
        parts = (message.text or "").split(maxsplit=2)
        command = parts[0].split("@")[0]
        if command == "/budget" and len(parts) == 1:
            await panel.navigate(message, state, "budgets", [])
            return
        if (
            len(parts) < 2
            or not parts[1].isdigit()
            or (len(parts) == 3 and command not in {"/strategy", "/budget"})
        ):
            await message.answer(f"{command} ID")
            return
        item = await repository.item(int(parts[1]))
        if item is None:
            await message.answer("Объявление не найдено.")
            return
        if command == "/strategy":
            if len(parts) == 3:
                try:
                    patch = json.loads(parts[2])
                    if not isinstance(patch, dict) or not patch or patch.keys() - STRATEGY_FIELDS:
                        raise ValueError("Invalid strategy fields")
                    updated = Advertisement.model_validate({**item.model_dump(), **patch})
                except ValueError:
                    await message.answer("Некорректные параметры стратегии.")
                    return
                await state.clear()
                return await panel.preview(
                    message, ("item", item.model_dump(mode="json"), updated.model_dump(mode="json"))
                )
            params = item.model_dump(mode="json", include=STRATEGY_FIELDS)
            for key, value in params.items():
                if value is None:
                    params[key] = (
                        item.cooldown_minutes
                        if key == "stabilization_minutes"
                        else getattr(settings, key)
                    )
            await message.answer(
                json.dumps({"strategy": "target_position", **params}, ensure_ascii=False, indent=2)[
                    :4000
                ]
            )
        elif command == "/budget":
            if len(parts) == 3:
                try:
                    updated = Advertisement.model_validate(
                        {
                            **item.model_dump(),
                            "daily_budget": int(parts[2]),
                        }
                    )
                except ValueError:
                    await message.answer("Укажите допустимый дневной бюджет целым числом копеек.")
                    return
                await state.clear()
                return await panel.preview(
                    message, ("item", item.model_dump(mode="json"), updated.model_dump(mode="json"))
                )
            await message.answer(
                f"Лимит объявления {item.daily_budget} коп. в день.\n"
                f"Лимит аккаунта {settings.account_daily_limit_kopecks} коп. в день.\n"
                f"Резерв {settings.budget_reserve_percent}%. "
                f"Ставка {item.min_bid}–{item.max_bid} коп."
            )
        elif command == "/history":
            await panel.reports(message, "history", [item.item_id, 0, "all"])
        else:
            decision, status = await bidding.evaluate_history(item, history)
            bid = f"{decision.new_bid} коп." if decision.new_bid is not None else "—"
            await message.answer(
                f"{status} · {decision.action} · {decision.reason_code}\nРекомендуемая ставка {bid}"
            )

    @router.message(Command("analytics", "stats", "compare"))
    async def analytics_command(message: Message):
        parts = (message.text or "").split()
        command = parts[0].split("@")[0]
        period = parts[1] if len(parts) > 1 else ("7" if command == "/compare" else "today")
        if (
            period not in {"today", "7", "14"}
            or len(parts) > 3
            or (len(parts) == 3 and not parts[2].isdigit())
        ):
            await message.answer("/analytics today|7|14 [ID]")
            return
        days = 0 if period == "today" else int(period)
        if len(parts) == 1:
            return await panel.reports(message, "analytics", [days, None])
        reports = await analytics.reports(days)
        requested_id = int(parts[2]) if len(parts) == 3 else None
        allocations = analytics.budget_recommendations(
            reports if days == 7 else await analytics.reports(7)
        )
        if requested_id is not None:
            reports = [r for r in reports if r["item"].item_id == requested_id]
        if not reports:
            await message.answer("Нет объявлений для отчёта.")
            return
        if command == "/compare":
            await message.answer(
                "\n".join(
                    f"{r['item'].item_id} · {r['item'].title[:60]} · "
                    f"расходы {money(r['actual'].spend)} · "
                    f"CPL {money(r['actual'].cpl * 100 if r['actual'].cpl is not None else None)}"
                    for r in reports
                )
            )
        for report in reports:
            item_id = report["item"].item_id
            recent = await repository.decisions(item_id, limit=20)
            blocked = list(
                dict.fromkeys(
                    r.get("reason_code")
                    for r in recent
                    if r.get("reason_code")
                    and (
                        "BUDGET" in r["reason_code"]
                        or r.get("authorization") == "PREFLIGHT_BLOCKED"
                    )
                )
            )[:3]
            await message.answer(
                format_report(
                    report,
                    blocked,
                    [r for r in allocations if r.item_id == item_id],
                )
            )

    @router.message(Command("status"))
    async def status(message: Message, state: FSMContext):
        await panel.navigate(message, state, "status", [False])

    @router.message(Command("items"))
    async def items(message: Message):
        await panel.item_list(message, "items", [])

    @router.message(Command("pause", "resume"))
    async def pause(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 18:
            return await message.answer("Укажите ID: /pause 123 или /resume 123")
        item = await repository.item(int(parts[1]))
        if item is None:
            return await message.answer("Объявление не найдено.")
        old = item.model_dump(mode="json")
        await panel.preview(
            message,
            ("item", old, {**old, "active": parts[0].split("@")[0] == "/resume"}),
            "Изменение локальной активности. Списания настраиваются в кабинете Авито.",
        )

    panel = TelegramPanel(router, panel_state, analytics, history, scheduler, monitor)

    @router.callback_query()
    async def old_callback(callback):
        await callback.answer("Кнопка устарела. Откройте /start.", show_alert=True)

    dispatcher = Dispatcher(fsm_strategy=FSMStrategy.USER_IN_CHAT)
    dispatcher.include_router(router)
    return dispatcher


async def run_bot(settings, repository, bidding=None, analytics=None, **kwargs):
    from hashlib import sha256
    from pathlib import Path
    from tempfile import gettempdir

    from filelock import FileLock

    token = settings.telegram_bot_token.get_secret_value()
    lock = Path(gettempdir()) / f"avito-telegram-{sha256(token.encode()).hexdigest()}.lock"
    with FileLock(str(lock), timeout=0):
        async with Bot(token) as bot:
            await make_dispatcher(settings, repository, bidding, analytics, **kwargs).start_polling(
                bot, handle_signals=False, close_bot_session=False, handle_as_tasks=False
            )
