import hmac
import json
import logging
import re
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.avito.exceptions import AccessDenied, AvitoError
from app.domain.enums import Mode, PromotionModel
from app.domain.models import Advertisement
from app.telegram.lookup import lookup_item
from app.telegram.state import sync_monitor_job
from app.telegram.views import PanelViews


class Wizard(StatesGroup):
    add = State()
    edit = State()
    bid = State()


STRATEGY = {
    "target_min": "Минимальная целевая позиция",
    "target_max": "Максимальная целевая позиция",
    "min_bid": "Минимальная ставка, коп.",
    "max_bid": "Максимальная ставка, коп.",
    "increase_step_kopecks": "Шаг повышения, коп.",
    "decrease_step_kopecks": "Шаг снижения, коп.",
    "max_step_kopecks": "Максимальный шаг, коп.",
    "max_step_percent": "Максимальный шаг, %",
    "increase_step_percent": "Шаг повышения, %",
    "decrease_step_percent": "Шаг снижения, %",
    "stabilization_minutes": "Стабилизация, мин.",
    "increase_interval_minutes": "Интервал повышений, мин.",
    "cooldown_minutes": "Cooldown, мин.",
    "increase_confirmations": "Подтверждений повышения",
    "decrease_confirmations": "Подтверждений снижения",
    "max_changes_per_day": "Изменений в сутки",
    "min_position_coverage": "Минимальное покрытие (0–1)",
    "active_hours": "Часы работы, список [9,10,11]",
    "monitoring_interval_minutes": "Интервал проверки позиций, мин.",
    "trial_decreases": "Пробные снижения (true/false)",
}
ITEM_FIELDS = {
    **STRATEGY,
    "title": "Название",
    "daily_budget": "Дневной лимит, коп.",
    "queries": 'Запросы с регионом и фильтрами, JSON [{"text":"…","region":"…"}]',
    "region": "Регион основного запроса",
    "confirmed_bid": "Стартовая ставка LOCAL SIMULATOR, коп.",
    "target_range": "Целевой диапазон позиций, два числа через пробел",
    "bid_range": "Диапазон ставки, две суммы в копейках через пробел",
    "query_add": 'Новый запрос, JSON {"text":"…","region":"…"}',
}
ADD_STEPS = (
    ("item_id", "Введите ID объявления."),
    ("title", "Введите название объявления."),
    ("query", "Введите поисковый запрос."),
    ("region", "Введите регион поиска."),
    ("target", "Введите две целевые позиции через пробел, например 3 7."),
    ("bids", "Введите минимальную и максимальную ставки в копейках через пробел."),
    ("daily_budget", "Введите дневной локальный лимит в копейках."),
)
PERIODS = (("Сегодня", 0), ("7 дней", 7), ("14 дней", 14))


def button(label, data):
    return InlineKeyboardButton(text=label, callback_data=data)


def proposal_signature(bot_token, kind, value):
    return hmac.digest(bot_token.encode(), f"{kind}:{value}".encode(), "sha256").hex()[:16]


def proposal_markup(operation_id, bot_token, *, back=None):
    value = UUID(operation_id).hex
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    label, f"proposal:{kind}:{value}:{proposal_signature(bot_token, kind, value)}"
                )
                for label, kind in (("✅ Подтвердить", "y"), ("❌ Отклонить", "n"))
            ],
            ([button("⬅️ Назад", back)] if back else []) + [button("🏠 Меню", "panel:home")],
        ]
    )


async def render(event, text, markup=None):
    text = text[:4000]
    if isinstance(event, Message):
        return await event.answer(text, reply_markup=markup)
    msg = event.message
    if isinstance(msg, Message):
        try:
            return await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message.lower():
                return None
        return await msg.answer(text, reply_markup=markup)
    return None


class TelegramPanel:
    def __init__(self, router, state, analytics, history, scheduler=None, monitor=None):
        self.state = state
        self.repo = state.repository
        self.settings = state.settings
        self.views = PanelViews(state, analytics, history, scheduler, monitor)
        self.tokens = {}
        self.pending = {}
        router.callback_query.register(self.callback, F.data.startswith("panel:"))
        router.callback_query.register(self.proposal, F.data.startswith("proposal:"))
        router.message.register(self.wizard_input, StateFilter(Wizard.add, Wizard.edit, Wizard.bid))

    def keyboard(self, rows, actor):
        now = time.monotonic()
        self.tokens = {key: value for key, value in self.tokens.items() if value[0] > now}
        result = []
        for row in rows:
            buttons = []
            for label, *action in row:
                if action == ["home"]:
                    buttons.append(button(label, "panel:home"))
                    continue
                token = uuid4().hex
                self.tokens[token] = now + (300 if action[0] == "apply" else 86400), action, actor
                if action[0] == "apply":
                    previous = self.pending.get(actor)
                    if previous:
                        self.tokens.pop(previous, None)
                    self.pending[actor] = token
                buttons.append(button(label[:64], f"panel:{token}"))
            result.append(buttons)
        while len(self.tokens) > 4000:
            self.tokens.pop(next(iter(self.tokens)))
        return InlineKeyboardMarkup(inline_keyboard=result)

    async def show(self, event, text, rows=(), back=("home",)):
        keyboard = list(rows)
        if back is not None:
            keyboard.append(
                [("⬅️ Назад", *back), ("🏠 Меню", "home")]
                if back != ("home",)
                else [("⬅️ В меню", "home")]
            )
        await render(event, text, self.keyboard(keyboard, event.from_user.id))

    def cancel_change(self, actor):
        token = self.pending.pop(actor, None)
        if token:
            self.tokens.pop(token, None)

    async def home(self, event):
        self.cancel_change(event.from_user.id)
        options = await self.state.options()
        await self.show(
            event,
            "🤖 Avito Bid Manager",
            [
                [("📢 Мои объявления", "items"), ("📊 Аналитика", "analytics", 0, None)],
                [("🎯 Управление ставками", "bids"), ("💰 Бюджеты", "budgets")],
                [("⏰ Расписание", "schedule")],
                [("📈 История изменений", "history", None, 0, "all")],
                [("⚙️ Настройки", "settings"), ("🛡 Состояние системы", "status", False)],
                [
                    (
                        "▶️ Возобновить управление"
                        if options["paused"]
                        else "⏸ Остановить автоматизацию",
                        "pause_all",
                    )
                ],
                [("🎯 Предложения ASSISTED", "proposals")],
            ],
            back=None,
        )

    async def callback(self, callback, state):
        token = callback.data.removeprefix("panel:")
        record = self.tokens.get(token)
        if token != "home" and (
            record is None or record[0] <= time.monotonic() or record[2] != callback.from_user.id
        ):
            await callback.answer("Кнопка устарела. Откройте /start.", show_alert=True)
            return
        await callback.answer()
        action, *args = ["home"] if token == "home" else record[1]
        if action == "apply":
            if self.pending.get(callback.from_user.id) != token:
                return
            self.cancel_change(callback.from_user.id)
            await self.apply(callback, state, args[0])
            return
        self.cancel_change(callback.from_user.id)
        await state.clear()
        await self.navigate(callback, state, action, args)

    async def apply(self, event, state, change):
        try:
            await self.state.apply(change, event.from_user.id)
        except ValueError:
            await self.show(
                event, "Настройки изменились или параметры недопустимы. Откройте раздел заново."
            )
        else:
            if (
                change[0] == "options"
                and change[1]["monitor_paused"] != change[2]["monitor_paused"]
            ):
                sync_monitor_job(self.views.scheduler, change[2]["monitor_paused"])
            await state.clear()
            await self.show(
                event,
                "Локальные настройки сохранены.",
                [[("Открыть объявление", "card", change[2]["item_id"])]]
                if change[0] == "item" and not change[2].get("archived")
                else [],
            )

    async def proposal(self, callback):
        try:
            _, kind, encoded, signature = callback.data.split(":")
            value = UUID(hex=encoded)
            if (
                kind not in ("y", "n")
                or value.hex != encoded
                or not hmac.compare_digest(
                    signature, proposal_signature(callback.bot.token, kind, encoded)
                )
            ):
                raise ValueError
        except ValueError:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        await callback.answer()
        status = await self.state.resolve(str(value), kind == "y", callback.from_user.id)
        rows = await self.state.proposals(include_one=str(value))
        if rows and status not in {"NOT_FOUND", "EXECUTION_BLOCKED"}:
            await self.show(callback, self.state.proposal_text(*rows[0]))
        else:
            await self.show(callback, f"Подтверждение недоступно · {status}")

    async def navigate(self, event, fsm, action, args):
        if action == "home":
            return await self.home(event)
        if action in {"items", "select", "archived"}:
            return await self.item_list(event, action, args)
        if action in {
            "card",
            "strategy",
            "setup",
            "positions",
            "edit",
            "item_change",
            "bid",
            "manual_bid",
            "item_schedule",
            "queries",
            "query_remove",
        }:
            item = await self.repo.item(args[0], include_archived=action == "item_change")
            if item is None:
                return await self.show(
                    event, "Объявление исключено или отсутствует.", back=("items",)
                )
            return await self.item_view(event, fsm, action, item, args[1:])
        if action == "bids":
            return await self.show(
                event,
                "🎯 Управление ставками",
                [
                    [("Выбрать объявление", "select", "bid")],
                    [("Стратегии", "select", "strategy")],
                    [("Предложения ASSISTED", "proposals")],
                    [("AUTO · BLOCKED", "status", False)],
                ],
            )
        if action == "schedule":
            opt = await self.state.options()
            return await self.show(
                event,
                await self.views.schedule(opt),
                [
                    [("Время отчёта", "edit_global", "report_time")],
                    [("Рабочие часы и интервал", "select", "item_schedule")],
                    [
                        (
                            "Возобновить мониторинг"
                            if opt["monitor_paused"]
                            else "Приостановить мониторинг",
                            "pause_monitor",
                        )
                    ],
                ],
            )
        if action == "pause_monitor":
            old = await self.state.options()
            return await self.preview(
                event,
                ("options", old, {**old, "monitor_paused": not old["monitor_paused"]}),
                back=("schedule",),
            )
        if action == "add":
            if len(await self.repo.items()) >= 5:
                return await self.show(event, "Достигнут лимит пяти объявлений.", back=("items",))
            await fsm.set_state(Wizard.add)
            await fsm.set_data({"draft": {}, "step": 0})
            return await self.show(event, ADD_STEPS[0][1], back=("items",))
        if action == "recommendations":
            return await self.show(
                event, await self.views.recommendations(), back=("analytics", 7, None)
            )
        if action in {"analytics", "history"}:
            return await self.reports(event, action, args)
        if action == "budgets":
            items = await self.repo.items()
            thresholds = (await self.state.options())["budget_thresholds"]
            return await self.show(
                event,
                await self.views.budgets(),
                [
                    [("Изменить общий лимит", "edit_global", "limit")],
                    [
                        (
                            f"{percent}% · {'вкл.' if percent in thresholds else 'выкл.'}",
                            "toggle_threshold",
                            percent,
                        )
                        for percent in (80, 95)
                    ],
                    *[
                        [(f"{item.title[:40]} · лимит", "edit", item.item_id, "daily_budget")]
                        for item in items
                    ],
                ],
            )
        if action == "toggle_threshold":
            old = await self.state.options()
            thresholds = sorted(set(old["budget_thresholds"]) ^ {args[0]})
            return await self.preview(
                event,
                ("options", old, {**old, "budget_thresholds": thresholds}),
                back=("budgets",),
            )
        if action == "settings":
            opt = await self.state.options()
            muted = ", ".join(opt["muted_alerts"]) or "—"
            return await self.show(
                event,
                f"⚙️ Настройки · {self.settings.timezone}\n"
                f"Отключённые категории: {muted}\n"
                f"Срок подтверждения ставки: {self.settings.telegram_confirmation_minutes} мин.",
                [
                    *[
                        [
                            (
                                category
                                + (" · выкл." if category in opt["muted_alerts"] else " · вкл."),
                                "toggle_alerts",
                                category,
                            )
                        ]
                        for category in ("Бюджеты", "Позиции", "Стратегии")
                    ],
                ],
            )
        if action == "edit_global":
            field = args[0]
            old = (
                await self.state.options()
                if field == "report_time"
                else self.settings.account_daily_limit_kopecks
            )
            await fsm.set_state(Wizard.edit)
            await fsm.set_data({"field": field, "old": old})
            return await self.show(
                event,
                "Введите время ЧЧ:ММ."
                if field == "report_time"
                else "Введите общий дневной лимит в копейках.",
                back=("schedule",) if field == "report_time" else ("budgets",),
            )
        if action == "toggle_alerts":
            old = await self.state.options()
            muted = set(old["muted_alerts"])
            muted.symmetric_difference_update([args[0]])
            return await self.preview(
                event, ("options", old, {**old, "muted_alerts": sorted(muted)})
            )
        if action == "pause_all":
            old = await self.state.options()
            title = (
                "Возобновить локальное управление?"
                if old["paused"]
                else "Приостановить локальное управление? Продвижение в Авито продолжает "
                "работать по настройкам кабинета."
            )
            return await self.preview(
                event, ("options", old, {**old, "paused": not old["paused"]}), title
            )
        if action == "status":
            return await self.show(
                event,
                await self.views.status(check=args[0], bot=event.bot),
                [
                    [("Проверить подключение", "status", True)],
                ],
            )
        if action == "monitor":
            if not self.views.monitor or (await self.state.options())["monitor_paused"]:
                return await self.show(event, "Планировщик мониторинга недоступен.")
            item_id, index, _ = args
            item = await self.repo.item(item_id)
            if item is None or not 0 <= index < len(item.queries):
                return await self.show(event, "Объявление или запрос недоступны.")
            await self.views.monitor.tick(item_id=item_id, query_index=index)
            return await self.show(
                event,
                "Запрошен цикл мониторинга. Учитываются интервалы источников.",
                back=("positions", *args),
            )
        if action == "proposals":
            proposals = await self.state.proposals()
            now = datetime.now(UTC).isoformat()
            rows = [
                [(f"#{op.item_id} · {op.requested_bid} коп.", "proposal_view", op.id)]
                for op, row, _ in proposals
                if row.payload.get("limits", {}).get("confirmation_expires_at", "") > now
                and row.payload.get("limits", {}).get("manual_owner", event.from_user.id)
                == event.from_user.id
            ]
            return await self.show(
                event,
                "🎯 Предложения ASSISTED" if rows else "Нет действующих предложений.",
                rows[:15],
            )
        if action == "proposal_view":
            rows = await self.state.proposals(include_one=args[0])
            if (
                not rows
                or rows[0][1].payload.get("limits", {}).get("manual_owner", event.from_user.id)
                != event.from_user.id
            ):
                return await self.show(event, "Предложение недоступно.")
            back = self.keyboard([[("Назад", "proposals")]], event.from_user.id)
            return await render(
                event,
                self.state.proposal_text(*rows[0]),
                proposal_markup(
                    args[0], event.bot.token, back=back.inline_keyboard[0][0].callback_data
                ),
            )
        await self.home(event)

    async def item_list(self, event, action, args):
        items = await self.repo.items(include_archived=action == "archived")
        if action == "archived":
            return await self.show(
                event,
                "Исключённые объявления",
                [
                    [(i.title[:60], "item_change", i.item_id, "restore")]
                    for i in items
                    if i.archived
                ],
                back=("items",),
            )
        opt = await self.state.options()
        rows = [
            [
                (
                    self.item_button_label(i, opt["paused"]),
                    args[0] if args else "card",
                    i.item_id,
                )
            ]
            for i in items
        ]
        if action == "items":
            rows += [
                [("➕ Добавить объявление", "add"), ("🔄 Обновить", "items")],
                [("Исключённые объявления", "archived")],
            ]
        await self.show(event, "📢 Мои объявления" if items else "Объявлений пока нет.", rows)

    @staticmethod
    def item_button_label(item, paused):
        if paused or not item.active:
            icon = "⏸"
        else:
            icon = "🟡" if item.management_mode == Mode.DRY_RUN else "🟢"
        return f"{icon} {item.title[:48]}"

    async def item_view(self, event, fsm, action, item, args):
        item_id = item.item_id
        old = item.model_dump(mode="json")
        back = ("card", item_id)
        if action == "card":
            await self.show(
                event,
                await self.views.card(item),
                [
                    [("Ставки", "bid", item_id)],
                    [("Настройки", "setup", item_id), ("Стратегия", "strategy", item_id)],
                    [
                        ("Бюджет", "edit", item_id, "daily_budget"),
                        ("Позиции", "positions", item_id, item.primary_query_index, 7),
                    ],
                    [
                        ("Аналитика", "analytics", 0, item_id),
                        ("История", "history", item_id, 0, "all"),
                    ],
                    [
                        (
                            "Приостановить" if item.active else "Возобновить",
                            "item_change",
                            item_id,
                            "active",
                        )
                    ],
                    [("Исключить", "item_change", item_id, "archive")],
                ],
                back=("items",),
            )
        elif action == "bid":
            await self.show(
                event,
                await self.views.card(item),
                [
                    [("Ввести ставку", "manual_bid", item_id)],
                    [("Стратегия", "strategy", item_id), ("Режим", "setup", item_id)],
                ],
                back=back,
            )
        elif action == "manual_bid":
            if item.promotion_model != PromotionModel.MOCK:
                return await self.show(
                    event,
                    "Реальная запись в Avito заблокирована до проверки интеграции.",
                    back=("bid", item_id),
                )
            await fsm.set_state(Wizard.bid)
            await fsm.set_data({"old": old})
            await self.show(
                event,
                f"LOCAL SIMULATOR · введите ставку в копейках ({item.min_bid}–{item.max_bid}).",
                back=("bid", item_id),
            )
        elif action == "item_schedule":
            await self.show(
                event,
                f"⏰ {item.title[:100]} · {self.settings.timezone}\n"
                f"Рабочие часы: {', '.join(map(str, sorted(set(item.active_hours)))) or '—'}\n"
                f"Интервал: {item.monitoring_interval_minutes} мин. с учётом лимитов источника.",
                [
                    [(ITEM_FIELDS[field], "edit", item_id, field)]
                    for field in ("active_hours", "monitoring_interval_minutes")
                ],
                back=("schedule",),
            )
        elif action == "strategy":
            await self.show(
                event,
                f"🎯 Стратегия · {item.title[:100]}",
                [
                    [("Целевой диапазон", "edit", item_id, "target_range")],
                    [("Диапазон ставки", "edit", item_id, "bid_range")],
                    *[
                        [
                            (
                                f"{label} · "
                                f"{old[field] if old[field] is not None else 'по умолчанию'}",
                                "edit",
                                item_id,
                                field,
                            )
                        ]
                        for field, label in STRATEGY.items()
                    ],
                ],
                back=back,
            )
        elif action == "setup":
            rows = [
                [
                    ("Название", "edit", item_id, "title"),
                    ("Поисковые запросы", "queries", item_id),
                ],
                [("Регион", "edit", item_id, "region")],
                [("DRY_RUN", "item_change", item_id, "dry"), ("AUTO · BLOCKED", "status", False)],
            ]
            rows.append(
                [
                    (
                        "Отключить расчёт ставок"
                        if item.auto_enabled
                        else "Включить расчёт ставок",
                        "item_change",
                        item_id,
                        "calculate",
                    )
                ]
            )
            if item.promotion_model == PromotionModel.UNKNOWN and not item.avito_verified:
                rows.append([("Настроить LOCAL SIMULATOR", "item_change", item_id, "mock")])
            if item.promotion_model == PromotionModel.MOCK:
                if item.confirmed_bid is None:
                    rows.append([("Стартовая ставка", "edit", item_id, "confirmed_bid")])
                else:
                    rows.append([("ASSISTED", "item_change", item_id, "assisted")])
            await self.show(
                event, f"⚙️ {item.title[:100]} · {item.management_mode}", rows, back=back
            )
        elif action == "queries":
            await self.show(
                event,
                f"Поисковые запросы · {item.title[:100]}",
                [
                    *[
                        [
                            (
                                f"Удалить · {q.text[:28]} / {q.region[:22]}",
                                "query_remove",
                                item_id,
                                index,
                                q.model_dump(mode="json"),
                            )
                        ]
                        for index, q in enumerate(item.queries)
                    ],
                    [("Добавить запрос", "edit", item_id, "query_add")]
                    if len(item.queries) < 5
                    else [("Заменить список", "edit", item_id, "queries")],
                ],
                back=("setup", item_id),
            )
        elif action == "query_remove":
            index, expected = args
            if index >= len(item.queries) or old["queries"][index] != expected:
                return await self.show(
                    event, "Список запросов изменился.", back=("queries", item_id)
                )
            queries = [q for i, q in enumerate(old["queries"]) if i != index]
            primary = item.primary_query_index - (index < item.primary_query_index)
            new = Advertisement.model_validate(
                {
                    **old,
                    "queries": queries,
                    "primary_query_index": min(primary, max(0, len(queries) - 1)),
                }
            ).model_dump(mode="json")
            return await self.preview(event, ("item", old, new), back=("queries", item_id))
        elif action == "positions":
            index, days = args
            await self.show(
                event,
                await self.views.positions(item, index, days),
                [
                    [
                        (q.text[:35], "positions", item_id, idx, days)
                        for idx, q in enumerate(item.queries)
                    ]
                ]
                * bool(item.queries)
                + [
                    [
                        ("1 день", "positions", item_id, index, 1),
                        ("7 дней", "positions", item_id, index, 7),
                        ("14 дней", "positions", item_id, index, 14),
                    ],
                    [("Проверить источник", "monitor", item_id, index, days)],
                ],
                back=back,
            )
        elif action == "edit":
            field = args[0]
            value = old.get(field)
            if field == "target_range":
                value = f"{item.target_min} {item.target_max}"
            elif field == "bid_range":
                value = f"{item.min_bid} {item.max_bid}"
            if field == "region" and item.queries:
                value = item.queries[item.primary_query_index].region
            await fsm.set_state(Wizard.edit)
            await fsm.set_data({"field": field, "old": old})
            await self.show(
                event,
                f"{ITEM_FIELDS[field]}\nТекущее: {str(value)[:1200]}\n"
                "Введите новое значение. /cancel — отмена.",
                back=back,
            )
        elif action == "item_change":
            if args[0] == "mock" and (
                item.promotion_model != PromotionModel.UNKNOWN or item.avito_verified
            ):
                return await self.show(event, "Конфигурация объявления изменилась.")
            patch = {
                "active": {"active": not item.active},
                "calculate": {"auto_enabled": not item.auto_enabled},
                "archive": {"archived": True, "active": False, "management_mode": Mode.DRY_RUN},
                "restore": {"archived": False, "active": False},
                "dry": {"management_mode": Mode.DRY_RUN},
                "mock": {"promotion_model": PromotionModel.MOCK, "bid_unit": None},
                "assisted": {"management_mode": Mode.ASSISTED, "auto_enabled": True},
            }[args[0]]
            await self.preview(
                event,
                ("item", old, {**old, **patch}),
                "Изменение локального управления. Публикация и списания управляются "
                "в кабинете Авито.",
            )

    async def reports(self, event, action, args):
        item_id = args[1] if action == "analytics" else args[0]
        item = await self.repo.item(item_id) if item_id is not None else None
        if item_id is not None and item is None:
            return await self.show(event, "Объявление недоступно.")
        if action == "analytics":
            days = args[0]
            return await self.show(
                event,
                await self.views.analytics_text(days, item),
                [
                    [(label, "analytics", period, item_id) for label, period in PERIODS],
                    *[
                        [(i.title[:50], "analytics", days, i.item_id)]
                        for i in await self.repo.items()
                    ],
                    [
                        ("Сравнить все", "analytics", days, None),
                        ("Рекомендации", "recommendations"),
                    ],
                ],
                back=("card", item_id) if item_id else ("home",),
            )
        _, page, category = args
        text, more = await self.views.history_page(item, page, category)
        pages = []
        if page:
            pages.append(("← Предыдущая", "history", item_id, page - 1, category))
        if more:
            pages.append(("Следующая →", "history", item_id, page + 1, category))
        await self.show(
            event,
            text,
            [
                [
                    ("Все", "history", item_id, 0, "all"),
                    ("Расчёты", "history", item_id, 0, "calculated"),
                    ("Симуляции", "history", item_id, 0, "simulated"),
                ],
                [
                    ("Отклонённые", "history", item_id, 0, "rejected"),
                    ("Ошибки", "history", item_id, 0, "errors"),
                    ("Администратор", "history", None, 0, "admin"),
                ],
                *([pages] if pages else []),
            ],
            back=("card", item_id) if item_id else ("home",),
        )

    async def preview(self, event, change, prompt="Проверьте изменения.", back=("home",)):
        self.cancel_change(event.from_user.id)
        kind, old, new, *_ = change
        if isinstance(new, dict):
            keys = [key for key in new if (old or {}).get(key) != new[key]]
            if old is None and kind == "item":
                keys = [
                    "item_id",
                    "title",
                    "avito_verified",
                    "promotion_model",
                    "queries",
                    "target_min",
                    "target_max",
                    "min_bid",
                    "max_bid",
                    "daily_budget",
                ]
            if not keys:
                return await self.show(event, "Значения уже сохранены.")
            detail = "\n".join(
                f"{ITEM_FIELDS.get(key, key)}: {(old or {}).get(key, '—')} → {new[key]}"
                for key in keys
            )
            if kind == "item" and old is None and not new["avito_verified"]:
                prompt += "\nUNVERIFIED · локальная конфигурация."
        else:
            detail = f"{old} → {new} коп. · локальный дневной лимит"
        if len(detail) > 3500:
            return await self.show(
                event, "Изменение слишком длинное для проверки. Сократите данные."
            )
        return await self.show(
            event,
            f"{prompt}\n\n{detail}",
            [[("✅ Сохранить", "apply", change), ("❌ Отмена", "home")]],
            back=back,
        )

    async def wizard_input(self, message, state):
        data = await state.get_data()
        raw = (message.text or "").strip()
        if await state.get_state() == Wizard.bid.state:
            item = Advertisement.model_validate(data["old"])
            if not re.fullmatch(r"[0-9]{1,19}", raw):
                return await message.answer("Введите целое число копеек.")
            try:
                operation_id = await self.state.bidding.execution.propose_manual_bid(
                    item, int(raw), message.from_user.id
                )
            except ValueError as exc:
                return await message.answer(str(exc))
            await state.clear()
            return await self.navigate(message, state, "proposal_view", [operation_id])

        try:
            if "draft" in data:
                return await self.add_step(message, state, data, raw)
            field, old = data["field"], data["old"]
            if field == "limit":
                if (value := int(raw)) <= 0 or value > 2**63 - 1:
                    raise ValueError
                change = ("limit", old, value)
            elif field == "report_time":
                if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", raw) is None:
                    raise ValueError
                change = ("options", old, {**old, field: raw})
            else:
                if field == "title":
                    patch = {field: raw}
                elif field in ("target_range", "bid_range"):
                    low, high = map(int, raw.split())
                    keys = (
                        ("target_min", "target_max")
                        if field == "target_range"
                        else ("min_bid", "max_bid")
                    )
                    patch = dict(zip(keys, (low, high), strict=True))
                elif field == "query_add":
                    patch = {"queries": [*old["queries"], json.loads(raw)]}
                elif field == "region":
                    queries = old["queries"]
                    index = old["primary_query_index"]
                    if not queries:
                        raise ValueError
                    patch = {
                        "queries": [
                            {**query, "region": raw} if i == index else query
                            for i, query in enumerate(queries)
                        ]
                    }
                else:
                    patch = {field: json.loads(raw)}
                new = Advertisement.model_validate({**old, **patch}).model_dump(mode="json")
                change = ("item", old, new)
            await state.clear()
            return await self.preview(message, change)
        except (ValueError, TypeError, KeyError, OverflowError):
            await message.answer("Недопустимое значение. Попробуйте снова или используйте /cancel.")

    async def add_step(self, message, state, data, raw):
        draft, step = data["draft"], data["step"]
        field = ADD_STEPS[step][0]
        if field == "item_id":
            if (
                not raw.isascii()
                or not raw.isdecimal()
                or len(raw) > 19
                or not 0 < (item_id := int(raw)) < 2**63
            ):
                raise ValueError
            if await self.repo.item(item_id, include_archived=True):
                return await message.answer(
                    "Этот ID уже сохранён. Исключённое объявление можно восстановить из списка."
                )
            if len(await self.repo.items()) >= 5:
                await state.clear()
                return await self.show(message, "Достигнут лимит пяти объявлений.")
            draft["item_id"] = item_id
            try:
                title, bid, verified = await lookup_item(self.settings, item_id)
            except AccessDenied:
                return await message.answer(
                    "Avito отказал в доступе к объявлению. Проверьте аккаунт и ID."
                )
            except (AvitoError, OSError, ValueError, TypeError) as exc:
                logging.getLogger(__name__).warning("Avito lookup: %s", type(exc).__name__)
                title, bid, verified = None, None, False
            draft.update(avito_verified=verified)
            if bid:
                draft["promotion_model"] = bid.capabilities.model
                bounds = bid.capabilities.allowed_bids
                draft["min_bid"] = min(bounds) if bounds else bid.capabilities.minimum
                draft["max_bid"] = max(bounds) if bounds else bid.capabilities.maximum
                data["bid"] = bid.model_dump(mode="json")
            if title:
                draft["title"] = title
                step += 1
            await message.answer(
                ("Принадлежность подтверждена" if verified else "UNVERIFIED")
                + f" · модель {draft.get('promotion_model', 'UNKNOWN')}"
            )
        elif field in {"title", "query", "region"}:
            if not raw or len(raw) > 250:
                raise ValueError
            draft[field] = raw
        elif field in {"target", "bids"}:
            low, high = map(int, raw.split())
            if not (1 if field == "target" else 0) <= low <= high < 2**63:
                raise ValueError
            if field == "target":
                draft.update(target_min=low, target_max=high)
            else:
                draft.update(min_bid=low, max_bid=high)
        elif field == "daily_budget":
            from app.domain.models import BidInfo

            if not 0 <= (budget := int(raw)) < 2**63:
                raise ValueError
            draft["daily_budget"] = budget
            candidate = {k: v for k, v in draft.items() if k not in {"region", "query"}}
            candidate["queries"] = [{"text": draft["query"], "region": draft["region"]}]
            item = Advertisement.model_validate(candidate)
            if data.get("bid"):
                bid = BidInfo.model_validate(data["bid"])
                if not all(
                    bid.capabilities.allows(value, datetime.now(UTC))
                    for value in (item.min_bid, item.max_bid)
                ):
                    await state.update_data(step=5, draft=draft)
                    return await message.answer(
                        "Диапазон вне подтверждённых значений API. "
                        "Введите допустимые границы заново."
                    )
            await state.clear()
            return await self.preview(
                message,
                (
                    "item",
                    None,
                    item.model_dump(mode="json"),
                    data.get("bid"),
                ),
            )
        step += 1
        await state.update_data(**{**data, "step": step})
        hint = (
            f" · допустимо {draft['min_bid']}–{draft['max_bid']} коп."
            if step == 5 and data.get("bid")
            else ""
        )
        await self.show(message, ADD_STEPS[step][1] + hint, back=("items",))
