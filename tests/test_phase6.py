from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import SecretStr
from sqlalchemy import select

from app.avito.exceptions import AccessDenied, AvitoError
from app.avito.simulator import LocalSimulator
from app.bidding.engine import BiddingEngine
from app.database.models import NotificationRow, OperationRow, SpendingRow
from app.domain.enums import Mode, PromotionModel
from app.domain.models import Advertisement
from app.positions.history import PositionHistory
from app.services.analytics_service import AnalyticsService
from app.services.bidding_service import BiddingService
from app.telegram.bot import make_dispatcher
from app.telegram.jobs import TelegramJobs
from app.telegram.state import PanelState, sync_monitor_job


class ChatMock:
    def __init__(self, bot, dispatcher):
        self.bot = bot
        self.dispatcher = dispatcher
        self.answers = []

    def mock(self, monkeypatch):
        async def render(message, text, reply_markup=None):
            self.answers.append((text, reply_markup))

        async def acknowledge(callback, text=None, **kwargs):
            if text:
                self.answers.append((text, None))

        monkeypatch.setattr(Message, "answer", render)
        monkeypatch.setattr(Message, "edit_text", render)
        monkeypatch.setattr(CallbackQuery, "answer", acknowledge)

    def token(self, label):
        return next(
            key.callback_data
            for row in self.answers[-1][1].inline_keyboard
            for key in row
            if label in key.text
        )

    async def feed(self, value, *, callback=False, actor=44, chat_type="private"):
        user = User(id=actor, is_bot=False, first_name="Test")
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=actor, type=chat_type),
            from_user=user,
            text=None if callback else value,
        )
        update = (
            Update(update_id=len(self.answers) + 1, message=message)
            if not callback
            else Update(
                update_id=len(self.answers) + 1,
                callback_query=CallbackQuery(
                    id="q",
                    from_user=user,
                    chat_instance="test",
                    message=message,
                    data=value,
                ),
            )
        )
        return await self.dispatcher.feed_update(self.bot, update)

    async def click(self, label):
        return await self.feed(self.token(label), callback=True)


@pytest.mark.parametrize("outcome", ["SIMULATED", "EXPIRED", "PREFLIGHT_BLOCKED"])
async def test_manual_bid_through_telegram(case, database, monkeypatch, outcome):
    settings, item, info, spending, now, _ = case
    _, sessions, repo = database
    settings.telegram_admin_ids = [44, 45]
    item = item.model_copy(update={"confirmed_bid": info.bid, "management_mode": Mode.ASSISTED})
    await repo.save_item(item)
    simulator = LocalSimulator(sessions)
    bidding = BiddingService(sessions, simulator, BiddingEngine(settings))
    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repo, bidding))
        chat.mock(monkeypatch)
        await chat.feed("/start")
        await chat.click("Управление ставками")
        await chat.click("Выбрать объявление")
        await chat.click(item.title)
        await chat.click("Ввести ставку")
        await chat.feed(str(item.max_bid + 1))
        assert "допустимого диапазона" in chat.answers[-1][0]
        await chat.feed("2200")
        assert "UNRELIABLE_SPENDING" in chat.answers[-1][0]
        async with sessions.begin() as session:
            session.add(SpendingRow(item_id=item.item_id, payload=spending.model_dump(mode="json")))
        await chat.feed("2200")
        assert "ожидает подтверждения" in chat.answers[-1][0]
        token = chat.token("Подтвердить")
        assert (await simulator.get_bid_info(item.item_id)).bid == info.bid
        chat.dispatcher = make_dispatcher(settings, repo, bidding)
        await chat.feed(token, callback=True, actor=45)
        assert "NOT_FOUND" in chat.answers[-1][0]
        if outcome == "EXPIRED":
            bidding.execution.clock = lambda: now + timedelta(hours=1)
        if outcome == "PREFLIGHT_BLOCKED":
            await repo.save_item(item.model_copy(update={"max_bid": 4000}))
        for _ in range(2):
            await chat.feed(token, callback=True)
            assert outcome in chat.answers[-1][0]
        assert (await simulator.get_bid_info(item.item_id)).bid == (
            2200 if outcome == "SIMULATED" else info.bid
        )
        async with sessions() as session:
            assert len(list(await session.scalars(select(OperationRow)))) == 1


async def test_schedule_and_thresholds_after_restart(case, database, monkeypatch):
    settings, item, *_ = case
    _, _, repo = database
    settings.telegram_admin_ids = [44]
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(lambda: None, "interval", minutes=1, id="position_monitor")
    scheduler.start()
    try:
        async with Bot("42:TEST") as bot:
            chat = ChatMock(bot, make_dispatcher(settings, repo, scheduler=scheduler))
            chat.mock(monkeypatch)
            await chat.feed("/start")
            await chat.click("Расписание")
            assert "Следующий:" in chat.answers[-1][0]
            await chat.click("Рабочие часы и интервал")
            await chat.click(item.title)
            await chat.click("Интервал проверки")
            await chat.feed("0")
            assert "Недопустимое" in chat.answers[-1][0]
            await chat.feed("45")
            await chat.click("Сохранить")
            await chat.feed("/start")
            await chat.click("Расписание")
            await chat.click("Время отчёта")
            await chat.feed("24:00")
            assert "Недопустимое" in chat.answers[-1][0]
            await chat.feed("11:20")
            await chat.click("Сохранить")
            await chat.feed("/start")
            await chat.click("Расписание")
            await chat.click("Приостановить мониторинг")
            await chat.click("Сохранить")
            assert scheduler.get_job("position_monitor").next_run_time is None
            await chat.feed("/budget")
            await chat.click("95%")
            await chat.click("Сохранить")
            state = PanelState(repo, settings, None)
            options = await state.options()
            assert options["report_time"] == "11:20"
            assert options["budget_thresholds"] == [80]
            assert (await repo.item(item.item_id)).monitoring_interval_minutes == 45
            scheduler.get_job("position_monitor").resume()
            sync_monitor_job(scheduler, options["monitor_paused"])
            assert scheduler.get_job("position_monitor").next_run_time is None
            chat.dispatcher = make_dispatcher(settings, repo, scheduler=scheduler)
            await chat.feed("/start")
            await chat.click("Расписание")
            assert "11:20" in chat.answers[-1][0]
            await chat.click("Возобновить мониторинг")
            await chat.click("Сохранить")
            assert scheduler.get_job("position_monitor").next_run_time is not None
    finally:
        scheduler.shutdown(wait=False)


async def test_real_bid_write_and_auto_are_blocked(case, database, monkeypatch):
    settings, item, *_ = case
    _, _, repo = database
    settings.telegram_admin_ids = [44]
    await repo.save_item(
        item.model_copy(update={"promotion_model": PromotionModel.UNKNOWN, "bid_unit": "unknown"})
    )
    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repo))
        chat.mock(monkeypatch)
        await chat.feed("/start")
        await chat.click("Управление ставками")
        await chat.click("AUTO")
        assert "AUTO_OWNER_APPROVAL_REQUIRED" in chat.answers[-1][0]
        await chat.feed("/items")
        await chat.click(item.title)
        await chat.click("Ставки")
        await chat.click("Ввести ставку")
        assert "Реальная запись в Avito заблокирована" in chat.answers[-1][0]


async def test_ownership_lookup_failure_is_safe(case, database, monkeypatch):
    settings, _, *_ = case
    _, _, repo = database
    settings.telegram_admin_ids = [44]

    async def lookup(settings, item_id):
        if item_id == 2:
            raise AccessDenied("secret failure details")
        raise AvitoError("secret failure details")

    monkeypatch.setattr("app.telegram.panel.lookup_item", lookup)
    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repo))
        chat.mock(monkeypatch)
        await chat.feed("/items")
        await chat.click("Добавить")
        await chat.feed("2")
        assert "отказал в доступе" in chat.answers[-1][0]
        await chat.feed("3")
        assert "UNVERIFIED" in chat.answers[-2][0]
        assert "название" in chat.answers[-1][0]
        assert all("secret failure details" not in answer for answer, _ in chat.answers)


async def test_manage_queries_and_modes(case, database, monkeypatch):
    settings, item, info, *_ = case
    _, sessions, repo = database
    settings.telegram_admin_ids = [44]
    await repo.save_item(item.model_copy(update={"confirmed_bid": info.bid}))
    bidding = BiddingService(sessions, LocalSimulator(sessions), BiddingEngine(settings))

    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repo, bidding))
        chat.mock(monkeypatch)

        async def open_settings():
            await chat.feed("/items")
            await chat.click(item.title)
            await chat.click("Настройки")

        await open_settings()
        await chat.click("ASSISTED")
        assert (await repo.item(item.item_id)).management_mode == Mode.DRY_RUN
        await chat.click("Сохранить")
        assert (await repo.item(item.item_id)).management_mode == Mode.ASSISTED
        await open_settings()
        await chat.click("DRY_RUN")
        await chat.click("Сохранить")
        assert (await repo.item(item.item_id)).management_mode == Mode.DRY_RUN
        await open_settings()
        await chat.click("Поисковые запросы")
        await chat.click("Добавить запрос")
        await chat.feed('{"text":"новый запрос","region":"Казань"}')
        await chat.click("Сохранить")
        assert len((await repo.item(item.item_id)).queries) == 2
        await open_settings()
        await chat.click("Поисковые запросы")
        await chat.click("Удалить · q / r")
        await chat.click("Сохранить")
        saved = await repo.item(item.item_id)
        assert saved.queries[saved.primary_query_index].region == "Казань"


async def test_assisted_proposal_delivered_with_valid_signed_buttons(case, database, monkeypatch):
    settings, item, info, spending, now, positions = case
    _, sessions, repo = database
    settings.telegram_admin_ids = [44]
    settings.telegram_bot_token = SecretStr("42:TEST")
    item = item.model_copy(update={"confirmed_bid": info.bid, "management_mode": Mode.ASSISTED})
    await repo.save_item(item)
    simulator = LocalSimulator(sessions)
    service = BiddingService(sessions, simulator, BiddingEngine(settings))
    history = PositionHistory(sessions)
    await history.append(positions())
    async with sessions.begin() as session:
        session.add(SpendingRow(item_id=item.item_id, payload=spending.model_dump(mode="json")))
    await service.evaluate_new_history(item, history, mode=Mode.ASSISTED, managed=True)

    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repo, service))
        chat.mock(monkeypatch)

        async def send(self, admin, text, reply_markup):
            assert admin == 44
            chat.answers.append((text, reply_markup))

        monkeypatch.setattr(Bot, "send_message", send)
        state = PanelState(repo, settings, service)
        jobs = TelegramJobs(state, AnalyticsService(repo, settings), clock=lambda: now)
        await jobs.proposals()
        await jobs.proposals()
        assert len(chat.answers) == 1
        await chat.click("Подтвердить")
        assert "Изменение выполнено в симуляторе" in chat.answers[-1][0]


@pytest.mark.asyncio
async def test_menu_wizard_and_stale_actions(case, database, monkeypatch):
    settings, item, *_ = case
    _, _, repository = database
    settings.telegram_admin_ids = [44]
    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repository))
        chat.mock(monkeypatch)
        await chat.feed("/start", actor=88)
        await chat.feed("/start", chat_type="group")
        assert not chat.answers
        await chat.feed("/start")
        assert "Avito Bid Manager" in chat.answers[-1][0]
        await chat.click("Мои объявления")
        await chat.click("Добавить")
        await chat.feed("1")
        assert "уже сохранён" in chat.answers[-1][0]
        await chat.feed("wrong")
        assert "Недопустимое" in chat.answers[-1][0]
        for value in ("2", "Кресло", "мягкое кресло", "Москва", "9 3", "3 9", "100 2000", "9000"):
            await chat.feed(value)
        assert "UNVERIFIED" in chat.answers[-1][0]
        assert await repository.item(2) is None
        token = chat.token("Сохранить")
        await chat.feed(token, callback=True, actor=88)
        assert await repository.item(2) is None
        await chat.feed(token, callback=True)
        assert (await repository.item(2)).queries[0].region == "Москва"
        await chat.feed(token, callback=True)
        assert "устарела" in chat.answers[-1][0]
        await chat.feed("/items")
        await chat.click("Кресло")
        await chat.click("Настройки")
        await chat.click("Регион")
        await chat.feed("Казань")
        token = chat.token("Сохранить")
        await chat.feed("/cancel")
        await chat.feed(token, callback=True)
        assert (await repository.item(2)).queries[0].region == "Москва"
        await chat.feed("/items")
        await chat.click("Кресло")
        await chat.click("Исключить")
        await chat.click("Сохранить")
        assert await repository.item(2) is None
        assert (await repository.item(2, include_archived=True)).archived
        await chat.feed("/items")
        await chat.click("Исключённые объявления")
        await chat.click("Кресло")
        await chat.click("Сохранить")
        assert not (await repository.item(2)).active
        await chat.feed("/items")
        await chat.click("Кресло")
        await chat.click("Исключить")
        await chat.click("Сохранить")
        for item_id in (3, 4, 5, 6):
            await repository.save_item(
                Advertisement(
                    item_id=item_id,
                    title="Ещё объявление",
                    max_bid=1000,
                    daily_budget=1000,
                )
            )
        await chat.feed("/items")
        await chat.click("Добавить")
        assert "лимит пяти" in chat.answers[-1][0]
        assert len(await repository.items()) == 5
        assert await repository.item(item.item_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name", ["accept", "reject", "expired", "budget", "config", "pause", "positions"]
)
async def test_assisted_durable_confirmation(case, database, case_name):
    settings, item, info, spending, now, positions = case
    _, sessions, repository = database
    simulator = LocalSimulator(sessions)
    service = BiddingService(sessions, simulator, BiddingEngine(settings))
    item = item.model_copy(update={"confirmed_bid": info.bid, "management_mode": Mode.ASSISTED})
    await repository.save_item(item)
    history = PositionHistory(sessions)
    await history.append(positions())
    async with sessions.begin() as session:
        session.add(SpendingRow(item_id=item.item_id, payload=spending.model_dump(mode="json")))
    decision, status = await service.evaluate_new_history(
        item, history, mode=Mode.ASSISTED, managed=True
    )
    assert status == "AWAITING_CONFIRMATION", decision
    async with sessions() as session:
        operation = await session.scalar(select(OperationRow))
    assert (await simulator.get_bid_info(item.item_id)).bid == info.bid
    restarted = BiddingService(sessions, LocalSimulator(sessions), BiddingEngine(settings))
    state = PanelState(repository, settings, restarted)
    if case_name == "budget":
        limit = settings.account_daily_limit_kopecks
        await state.apply(("limit", limit, limit - 1), 44)
    elif case_name == "config":
        await repository.save_item(item.model_copy(update={"title": "Changed"}))
    elif case_name == "pause":
        old = await state.options()
        await state.apply(("options", old, {**old, "paused": True}), 44)
    elif case_name == "positions":
        await history.append(
            [positions()[-1].model_copy(update={"checked_at": now + timedelta(microseconds=1)})]
        )
    elif case_name == "expired":
        restarted.execution.clock = lambda: now + timedelta(minutes=60)
    status = await state.resolve(operation.id, case_name != "reject", 44)
    expected = {"accept": "SIMULATED", "reject": "REJECTED", "expired": "EXPIRED"}
    assert status == expected.get(case_name, "PREFLIGHT_BLOCKED")
    assert await state.resolve(operation.id, True, 44) == status
    assert (await restarted.provider.get_bid_info(item.item_id)).bid == (
        decision.new_bid if case_name == "accept" else info.bid
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["Europe/Moscow", "America/New_York"])
async def test_reports_and_notifications_survive_restart(case, database, timezone):
    settings, _, _, _, now, _ = case
    _, sessions, repository = database
    settings.telegram_admin_ids = [44]
    settings.timezone = timezone
    settings = settings.model_copy(
        update={"telegram_bot_token": "42:TEST", "timezone": "Europe/Moscow"}
    )
    settings.telegram_bot_token = SecretStr("42:TEST")
    state = PanelState(
        repository, settings, BiddingService(sessions, None, BiddingEngine(settings))
    )
    old = await state.options()
    await state.apply(("options", old, {**old, "report_time": "00:00"}), 44)
    sent = []

    class MockBot:
        def __init__(self, token):
            assert token == "42:TEST"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def send_message(self, admin, body, **kwargs):
            sent.append((admin, body))

    now = datetime(2026, 11, 1, 5, tzinfo=UTC)
    analytics = AnalyticsService(repository, settings)
    for step in (0, 1, 24):
        jobs = TelegramJobs(
            state,
            analytics,
            bot_factory=MockBot,
            clock=lambda step=step: now + timedelta(hours=step),
        )
        await jobs.report()
    assert len(sent) == 2
    assert "Ежедневный отчёт" in sent[0][1]
    jobs = TelegramJobs(
        state, analytics, bot_factory=MockBot, clock=lambda: now + timedelta(hours=25)
    )
    await jobs.deliver("event", "warning")
    await jobs.deliver("event", "warning")
    assert len(sent) == 3
    async with sessions() as session:
        assert (await session.get(NotificationRow, "telegram:delivery:event:44")).state == "SENT"


@pytest.mark.asyncio
async def test_panel_budget_analytics_strategy_and_diagnostics(case, database, monkeypatch):
    settings, item, *_ = case
    _, sessions, repository = database
    settings.telegram_admin_ids = [44]
    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repository))
        chat.mock(monkeypatch)
        await chat.feed("/status")
        assert "NOT CONFIGURED" in chat.answers[-1][0] and "AUTO: BLOCKED" in chat.answers[-1][0]
        await chat.feed("/start")
        await chat.click("Аналитика")
        await chat.click("7 дней")
        await chat.click(item.title)
        assert "7 закрытых дней" in chat.answers[-1][0]
        assert "Недостаточно обращений" in chat.answers[-1][0]
        await chat.feed("/start")
        await chat.click("Управление ставками")
        await chat.click("Стратегии")
        await chat.click(item.title)
        await chat.click("Минимальная ставка")
        await chat.feed(str(item.max_bid + 1))
        assert "Недопустимое" in chat.answers[-1][0]
        await chat.feed("300")
        assert (await repository.item(item.item_id)).min_bid == item.min_bid
        await chat.click("Сохранить")
        assert (await repository.item(item.item_id)).min_bid == 300
        await chat.feed("/budget")
        assert "нет достоверных данных" in chat.answers[-1][0]
        await chat.click("Изменить общий лимит")
        await chat.feed("-1")
        assert "Недопустимое" in chat.answers[-1][0]
        await chat.feed("92500")
        await chat.click("Сохранить")
        restarted = settings.model_copy(update={"account_daily_limit_kopecks": 100000})
        state = PanelState(
            repository, restarted, BiddingService(sessions, None, BiddingEngine(restarted))
        )
        await state.load()
        assert restarted.account_daily_limit_kopecks == 92500
        await chat.feed('/queries 1 [{"text":"кресло","region":"Пермь"}]')
        await chat.click("Сохранить")
        assert (await repository.item(item.item_id)).queries[0].text == "кресло"


@pytest.mark.asyncio
async def test_history_pagination(case, database, monkeypatch):
    settings, item, _, _, now, _ = case
    _, sessions, repository = database
    from app.database.models import DecisionRow

    settings.telegram_admin_ids = [44]
    async with sessions.begin() as session:
        session.add_all(
            DecisionRow(
                item_id=item.item_id, created_at=now.isoformat(), payload={"reason": f"reason {i}"}
            )
            for i in range(8)
        )
    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repository))
        chat.mock(monkeypatch)
        await chat.feed(f"/history {item.item_id}")
        assert "страница 1" in chat.answers[-1][0]
        assert "reason 7" in chat.answers[-1][0] and "reason 0" not in chat.answers[-1][0]
        await chat.click("Следующая")
        assert "страница 2" in chat.answers[-1][0] and "reason 0" in chat.answers[-1][0]
        await chat.click("Предыдущая")
        assert "страница 1" in chat.answers[-1][0]


@pytest.mark.asyncio
async def test_signed_callback_cannot_change_reject_to_accept(case, database, monkeypatch):
    settings, item, info, spending, _, positions = case
    _, sessions, repository = database
    settings.telegram_admin_ids = [44]
    simulator = LocalSimulator(sessions)
    service = BiddingService(sessions, simulator, BiddingEngine(settings))
    item = item.model_copy(update={"confirmed_bid": info.bid, "management_mode": Mode.ASSISTED})
    await repository.save_item(item)
    history = PositionHistory(sessions)
    await history.append(positions())
    async with sessions.begin() as session:
        session.add(SpendingRow(item_id=item.item_id, payload=spending.model_dump(mode="json")))
    await service.evaluate_new_history(item, history, mode=Mode.ASSISTED, managed=True)
    async with sessions() as session:
        operation = await session.scalar(select(OperationRow))
    from app.telegram.panel import proposal_markup

    async with Bot("42:TEST") as bot:
        chat = ChatMock(bot, make_dispatcher(settings, repository, service))
        chat.mock(monkeypatch)
        markup = proposal_markup(operation.id, bot.token)
        reject = markup.inline_keyboard[0][1].callback_data
        assert len(reject.encode()) <= 64
        await chat.feed(reject.replace(":n:", ":y:"), callback=True)
        assert "устарела" in chat.answers[-1][0]
        await chat.feed(reject, callback=True)
        assert "REJECTED" in chat.answers[-1][0]
        assert (await simulator.get_bid_info(item.item_id)).bid == info.bid
