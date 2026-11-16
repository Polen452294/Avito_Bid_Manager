from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.analytics.store import latest_spending
from app.avito.diagnostics import diagnose
from app.avito.exceptions import AvitoError
from app.bidding.budget import BudgetController
from app.database.models import DecisionRow, NotificationRow, OperationRow, RuntimeRow
from app.positions.analyzer import PositionAnalyzer
from app.positions.validation import context_key, usable
from app.services.readiness import ReadinessService
from app.telegram.analytics import format_report, money, number


class PanelViews:
    def __init__(self, state, analytics, history, scheduler=None, monitor=None):
        self.state = state
        self.repo = state.repository
        self.settings = state.settings
        self.analytics = analytics
        self.history = history
        self.scheduler = scheduler
        self.monitor = monitor
        self.budget = BudgetController(self.settings)
        self.zone = ZoneInfo(self.settings.timezone)

    def stamp(self, value):
        return value.astimezone(self.zone).strftime("%d.%m.%Y %H:%M %Z") if value else "—"

    async def item_data(self, item):
        async with self.repo.sessions() as session:
            return (
                await self.state.bidding.stored_bid(session, item.item_id),
                await latest_spending(session, item.item_id),
            )

    def spend_label(self, item, spend):
        if not spend:
            return "нет данных"
        quality = (
            "достоверно"
            if self.budget.reliable(item, spend, datetime.now(UTC))
            else "устарело или не подтверждено"
        )
        return (
            f"{money(spend.item_spent)} · {spend.source[:60]} · {spend.data_type} · "
            f"{quality} · {self.stamp(spend.data_as_of or spend.checked_at)}"
        )

    async def card(self, item):
        bid, spend = await self.item_data(item)
        confirmed = "нет данных"
        now = datetime.now(UTC)
        if bid and bid.data_type == "actual" and bid.capabilities.model == item.promotion_model:
            fresh = timedelta(0) <= now - bid.checked_at <= timedelta(minutes=15)
            trusted = fresh and bid.capabilities.allows(bid.bid, now)
            quality = "достоверно" if trusted else "устарело или не подтверждено"
            source = "источник не указан" if bid.source == "unspecified" else bid.source[:60]
            confirmed = (
                f"{money(bid.bid)} · {source} ({bid.capabilities.model}) · "
                f"{self.stamp(bid.checked_at)} · {quality}"
            )
        if simulated := await self.simulator_bid(item):
            confirmed = simulated
        recent = await self.repo.decisions(item.item_id)
        proposed = next((row for row in recent if "manual_owner" not in row.get("limits", {})), {})
        query = item.queries[item.primary_query_index] if item.queries else None
        rows = await self.position_rows(item, item.primary_query_index)
        latest = self.current_position(item, rows)
        options = await self.state.options()
        return (
            f"📢 {item.title[:150]}\nID {item.item_id} · "
            f"{'Активно' if item.active and not options['paused'] else 'Приостановлено'}\n"
            f"{item.management_mode} · {item.promotion_model} · "
            f"{'Avito: принадлежность подтверждена' if item.avito_verified else 'UNVERIFIED'}\n\n"
            f"🎯 Цель {item.target_min}–{item.target_max}\n"
            f"🔎 {query.text[:90] if query else 'Запрос не настроен'}\n"
            f"📍 {self.position_label(latest) if latest else 'Нет достоверных данных'}\n"
            f"Последняя проверка: {self.stamp(rows[-1].checked_at) if rows else '—'}\n\n"
            f"Подтверждённая ставка: {confirmed}\n"
            f"Расчёт ставки: {money(proposed.get('new_bid'))} · {proposed.get('created_at', '—')}\n"
            f"Расходы: {self.spend_label(item, spend)}\n"
            f"Локальный дневной лимит: {money(item.daily_budget)}"
        )

    async def simulator_bid(self, item):
        provider = self.state.bidding.provider
        if item.promotion_model.value == "MOCK" and getattr(provider, "simulated", False):
            try:
                simulated = await provider.get_bid_info(item.item_id)
            except (AvitoError, KeyError):
                return None
            return f"{money(simulated.bid)} · LOCAL SIMULATOR · {self.stamp(simulated.checked_at)}"
        return None

    def current_position(self, item, rows):
        latest = {(row.provider, row.position_type): row for row in rows}
        now = datetime.now(UTC)
        max_age = timedelta(minutes=self.settings.max_position_data_age_minutes)
        return max(
            (
                row
                for row in latest.values()
                if row.provider in item.position_sources
                and row.provider != "mock"
                and usable(row, now, max_age)
            ),
            key=lambda row: row.checked_at,
            default=None,
        )

    async def position_rows(self, item, index):
        if not 0 <= index < len(item.queries):
            return []
        key = context_key(item.item_id, item.queries[index])
        return [
            row
            for row in await self.history.history(item.item_id)
            if context_key(row.item_id, row.search_query, row.region, row.filters) == key
        ]

    def position_label(self, row):
        source = {"manual": "ручное наблюдение", "mock": "SIMULATOR"}.get(
            row.provider, row.provider
        )
        return (
            f"{row.position if row.position is not None else row.status} · {source[:50]} · "
            f"{row.position_type} · confidence {row.confidence} · {self.stamp(row.checked_at)}"
        )

    async def positions(self, item, index, days):
        if not 0 <= index < len(item.queries):
            return "Нет настроенных поисковых запросов."
        query = item.queries[index]
        rows = await self.position_rows(item, index)
        now = datetime.now(UTC)
        recent = [row for row in rows if now - timedelta(days=days) <= row.checked_at <= now]
        current = self.current_position(item, rows)
        trends = PositionAnalyzer().analyze(item, recent, now)
        key = context_key(item.item_id, query)
        trend_text = (
            ", ".join(
                f"{row['provider']} · {row['kind']} · {row['trend']}"
                for row in trends
                if row["context"] == key
            )
            or "нет сопоставимой динамики"
        )
        last_success = next((row for row in reversed(rows) if row.position is not None), None)
        success_stamp = self.stamp(last_success.checked_at) if last_success else "—"
        return "\n".join(
            [
                f"📍 {query.text[:120]} · {query.region[:100]}",
                f"Фильтры: {str(query.filters)[:150]}",
                self.position_label(current)
                if current
                else "Нет достоверных данных. Источник недоступен или данные устарели.",
                f"Последнее успешное наблюдение: {success_stamp}",
                f"Динамика за {days} дн.: {trend_text}",
                *(self.position_label(row) for row in reversed(recent[-12:])),
            ]
        )

    async def budgets(self):
        reserve = self.settings.budget_reserve_percent
        limit = self.settings.account_daily_limit_kopecks
        enabled = (await self.state.options())["budget_thresholds"]
        thresholds = ", ".join(f"{percent}%" for percent in enabled) or "выключены"
        lines = [
            f"💰 Общий локальный дневной лимит: {money(limit)} · резерв {reserve}%",
            "Жёсткий лимит в Avito через этот интерфейс недоступен. "
            "Авито продолжает списания по своим настройкам.",
            f"Пороги уведомлений: {thresholds}.",
        ]
        account_cuts = []
        now = datetime.now(UTC)
        for item in await self.repo.items():
            _, spend = await self.item_data(item)
            valid = self.budget.reliable(item, spend, now) and spend.data_type == "actual"
            remaining = (
                max(0, item.daily_budget * (100 - reserve) // 100 - spend.item_spent)
                if valid
                else None
            )
            if valid:
                account_cuts.append(spend)
            warnings = self.budget.warnings(item, spend, now)
            lines.append(
                f"\n{item.title[:60]} · лимит {money(item.daily_budget)} · "
                f"остаток {money(remaining)}\n"
                f"Расходы: {self.spend_label(item, spend)}\n"
                f"Контроль: {self.budget.check(item, spend, now) or 'доступен'}"
                + (f" · пороги {warnings}" if warnings else "")
            )
        if account_cuts:
            newest = max(account_cuts, key=lambda row: row.data_as_of or row.checked_at)
            remaining = max(0, limit * (100 - reserve) // 100 - newest.account_spent)
            lines.insert(
                1,
                f"Расходы аккаунта {money(newest.account_spent)} · "
                f"остаток {money(remaining)} · "
                f"{newest.source[:60]} · {self.stamp(newest.data_as_of or newest.checked_at)}",
            )
        else:
            lines.insert(1, "Расходы аккаунта и остаток: нет достоверных данных.")
        return "\n".join(lines)

    async def schedule(self, options):
        lines = [
            f"⏰ Расписание · {self.settings.timezone}",
            f"Ежедневный отчёт: {options['report_time']}",
            f"Локальный мониторинг: {'пауза' if options['monitor_paused'] else 'включён'}",
        ]
        async with self.repo.sessions() as session:
            now = datetime.now(self.zone)
            day = now.date() - timedelta(days=1)
            admin_ids = self.settings.telegram_admin_ids
            deliveries = [
                await session.get(NotificationRow, f"telegram:delivery:report:{day}:{admin}")
                for admin in admin_ids
            ]
            report_time = time.fromisoformat(options["report_time"])
            delivery_job = self.scheduler.get_job("telegram_delivery") if self.scheduler else None
            upcoming = getattr(delivery_job, "next_run_time", None)
            if upcoming:
                report_at = datetime.combine(now.date(), report_time, self.zone)
                if all(row and row.state in {"SENT", "REJECTED", "CLAIMED"} for row in deliveries):
                    report_at = datetime.combine(
                        now.date() + timedelta(days=1), report_time, self.zone
                    )
                lines.append(f"Ближайший отчёт: {self.stamp(max(upcoming, report_at))}")
            for job_id, label in (
                ("position_monitor", "Цикл позиций"),
                ("refresh_analytics", "Сбор статистики"),
                ("telegram_delivery", "Доставка Telegram"),
            ):
                job = self.scheduler.get_job(job_id) if self.scheduler else None
                row = await session.get(RuntimeRow, f"telegram:job:{job_id}")
                state = row.payload if row else {}
                last = datetime.fromisoformat(state["last_run"]) if "last_run" in state else None
                lines.append(
                    f"\n{label}\nПоследний запуск: {self.stamp(last)}\n"
                    f"Следующий: {self.stamp(getattr(job, 'next_run_time', None))}\n"
                    f"Последняя ошибка: {state.get('last_error') or '—'}"
                )
        return "\n".join(lines)

    async def analytics_text(self, days, item=None, *, compare=False):
        reports = await self.analytics.reports(days, items=[item] if item else None)
        label = f"{days} закрытых дней" if days else "Сегодня · незакрытый период"
        if item and reports and not compare:
            allocations = self.analytics.budget_recommendations(await self.analytics.reports(7))
            report = reports[0]
            actual = report["actual"]
            _, spending = await self.item_data(item)
            reliable = self.budget.reliable(item, spending, datetime.now(UTC))
            rem = (
                max(
                    0,
                    item.daily_budget * (100 - self.settings.budget_reserve_percent) // 100
                    - spending.item_spent,
                )
                if reliable
                else None
            )
            proposed = [r for r in allocations if r.item_id == item.item_id]
            result = (
                f"📊 {label}\n{format_report(report, allocations=proposed)}\n"
                f"{self.analytics_sources(report)}\n"
                f"Расходы и свежесть: {self.spend_label(item, spending)}\n"
                f"Остаток с резервом: {money(rem)}"
                f"{await self.simulator_metrics(report)}"
            )
            if actual.contacts is None or actual.contacts < self.settings.optimization_min_contacts:
                result += "\nНедостаточно обращений для уверенных экономических выводов."
            return result
        lines = [f"📊 {label} · сравнение"]
        for report in reports:
            actual, previous = report["actual"], report["previous"]
            trend = None
            if (
                previous
                and actual.position_context
                and actual.position_context == previous.position_context
                and actual.position_days == previous.position_days == days
            ):
                trend = actual.position - previous.position
            lines.append(
                f"\n{report['item'].title[:45]} · #{report['item'].item_id}\n"
                f"Расходы {money(actual.spend)} · просмотры {number(actual.views)} · "
                f"обращения {number(actual.contacts)}\n"
                f"CPL {number(actual.cpl, ' ₽')} · CPV {number(actual.cpv, ' ₽')} · "
                f"конверсия {number(actual.conversion, '%')}\n"
                f"Ставка {money(report['current_bid'])} · изменений {number(actual.bid_changes)}\n"
                f"Позиция {number(actual.position)} · динамика {number(trend)}\n"
                f"{self.analytics_sources(report, compact=True)}"
                f"{await self.simulator_metrics(report)}"
            )
        return "\n".join(lines if reports else [*lines, "Нет объявлений."])

    async def simulator_metrics(self, report):
        lines = []
        if bid := await self.simulator_bid(report["item"]):
            lines.append(f"\nСтавка симулятора: {bid}")
        if changes := report["simulated_changes"]:
            stamp = self.stamp(report["last_simulated_change"])
            lines.append(f"\nИзменений в LOCAL SIMULATOR: {changes} · журнал · {stamp}")
        return "".join(lines)

    def analytics_sources(self, report, *, compact=False):
        actual = report["actual"]

        def source(label, name, timestamp):
            stamp = timestamp.astimezone(self.zone).strftime("%d.%m %H:%M") if timestamp else "—"
            return f"{label} {str(name)[:24]} {stamp}" if name else f"{label} —"

        current = report["current_bid_info"]
        details = [
            source(label, name, stamp)
            for label, name, stamp in zip(
                ("Расходы", "Просмотры", "Обращения"),
                actual.metric_sources,
                actual.metric_received_at,
                strict=True,
            )
        ]
        details.extend(
            [
                source(
                    "Ставка",
                    current.source if current else None,
                    current.checked_at if current else None,
                ),
                source("Изменения", ", ".join(actual.bid_sources), actual.bid_as_of),
                source(
                    "Позиции",
                    actual.position_context[3] if actual.position_context else None,
                    actual.position_as_of,
                ),
            ]
        )
        prefix = "Источники и время данных"
        if not compact:
            prefix += " (CPL, CPV и конверсия рассчитаны по указанным метрикам)"
        return prefix + ": " + (" · " if compact else "\n").join(details)

    async def recommendations(self):
        reports = await self.analytics.reports(7)
        allocations = self.analytics.budget_recommendations(reports)
        lines = ["🎯 Рекомендации · 7 закрытых дней"]
        for report in reports:
            item = report["item"]
            current = [
                report["recommendation"],
                *(row for row in allocations if row.item_id == item.item_id),
            ]
            lines.append(f"{item.title[:65]} · #{item.item_id}")
            lines.extend(
                f"{row.recommendation_type} · {row.reason_code} · {row.expected_effect[:100]}"
                + (f" · {money(row.suggested_value)}" if row.suggested_value is not None else "")
                for row in current
                if row
            )
        if not reports:
            lines.append("Нет объявлений для анализа.")
        return "\n".join(lines)

    async def history_page(self, item, page, category):
        size = 6
        async with self.repo.sessions() as session:
            if category == "admin":
                records = select(RuntimeRow).where(RuntimeRow.key.like("telegram:audit:%"))
                rows = list(
                    await session.scalars(
                        records.order_by(RuntimeRow.key.desc()).offset(page * size).limit(size + 1)
                    )
                )
                lines = []
                for row in rows[:size]:
                    p = row.payload
                    old, new = p["old"], p["new"]
                    if p["kind"] == "item":
                        changed = {
                            key: value
                            for key, value in new.items()
                            if (old or {}).get(key) != value
                        }
                        old = {key: (old or {}).get(key) for key in changed} if old else None
                        new = changed
                    lines.append(
                        f"{p['at']} · администратор {p['actor']} · {p['kind']}\n"
                        f"{str(old)[:150]} → {str(new)[:150]}"
                    )
            else:
                query = select(DecisionRow, OperationRow).outerjoin(
                    OperationRow, DecisionRow.id == OperationRow.decision_id
                )
                if item:
                    query = query.where(DecisionRow.item_id == item.item_id)
                if category != "all":
                    states = {
                        "calculated": ("DRY_RUN", "AWAITING_CONFIRMATION"),
                        "simulated": ("SIMULATED", "SIMULATED_RECONCILED"),
                        "rejected": ("REJECTED", "EXPIRED"),
                        "errors": ("PREFLIGHT_BLOCKED", "UNKNOWN", "EXECUTION_BLOCKED"),
                    }
                    query = query.where(OperationRow.status.in_(states[category]))
                rows = (
                    await session.execute(
                        query.order_by(DecisionRow.id.desc()).offset(page * size).limit(size + 1)
                    )
                ).all()
                lines = [self.history_line(row, op) for row, op in rows[:size]]
        return f"📈 История · страница {page + 1}\n\n" + (
            "\n\n".join(lines) or "Событий нет."
        ), len(rows) > size

    @staticmethod
    def history_line(row, operation):
        raw = row.payload
        status = operation.status if operation else raw.get("authorization", "—")
        bid = operation.requested_bid if operation else raw.get("new_bid")
        limits = raw.get("limits", {})
        simulated = status.startswith("SIMULATED") or limits.get("confirmation_expires_at")
        mode = "LOCAL SIMULATOR" if simulated else "расчёт/архив"
        sources = ", ".join(
            sorted({r.get("provider", "?")[:24] for r in raw.get("observations", [])})
        )
        actor = f" · администратор {limits['owner']}" if limits.get("owner") is not None else ""
        return (
            f"{row.created_at} · #{row.item_id} · {raw.get('action', '—')}\n"
            f"{money(raw.get('old_bid'))} → {money(bid)}\n"
            f"{raw.get('reason_code') or raw.get('reason', '—')} · {status} · {mode} · "
            f"{sources or 'источник не записан'}{actor}"
        )

    async def status(self, *, check=False, bot=None):
        api = "NOT VERIFIED"
        if check:
            try:
                result = await diagnose(self.settings)
                api = result["status"]
                issues = [
                    f"{row['name']}: {row['status']}"
                    for row in result["checks"]
                    if row["status"] != "AVAILABLE"
                ]
                if issues:
                    api += " · " + ", ".join(issues)[:300]
            except Exception as exc:
                api = type(exc).__name__
        elif not (self.settings.avito_client_id and self.settings.avito_client_secret):
            api = "NOT CONFIGURED"
        telegram = "обновление получено" if bot else "недоступен"
        if check and bot:
            try:
                await bot.get_me()
                telegram = "OK"
            except Exception as exc:
                telegram = type(exc).__name__
        try:
            async with self.repo.sessions() as session:
                await session.execute(text("SELECT 1"))
                cycle = await session.get(RuntimeRow, "telegram:last_cycle")
                guards = list(
                    await session.scalars(
                        select(RuntimeRow).where(RuntimeRow.key.like("fail_safe:%"))
                    )
                )
            items = await self.repo.items()
            positions, finance, financial_as_of = False, False, None
            for item in items:
                positions |= (
                    self.current_position(
                        item, await self.position_rows(item, item.primary_query_index)
                    )
                    is not None
                )
                _, spending = await self.item_data(item)
                if spending and spending.data_type == "actual":
                    stamp = spending.data_as_of or spending.checked_at
                    financial_as_of = max(financial_as_of or stamp, stamp)
                finance |= bool(
                    spending
                    and spending.data_type == "actual"
                    and self.budget.reliable(item, spending, datetime.now(UTC))
                )
            options = await self.state.options()
        except Exception as exc:
            return f"🛡 База данных: {type(exc).__name__}\nTelegram: {telegram}\nAUTO: BLOCKED"
        return (
            f"🛡 Состояние Avito Bid Manager\n\nПриложение: запущено\nБаза данных: OK\n"
            f"Планировщик: {'OK' if self.scheduler and self.scheduler.running else 'STOPPED'}\n"
            f"Telegram: {telegram}\nAvito API: {api}\n"
            f"Последняя позиция: {'есть наблюдение' if positions else 'нет достоверных данных'}\n"
            f"Финансовый срез: {'есть достоверный срез' if finance else 'NOT VERIFIED'} · "
            f"{self.stamp(financial_as_of)}\n"
            f"AUTO: BLOCKED · платная запись отключена, нужна проверка оперативного "
            f"источника позиций и финансовой статистики Avito.\n"
            f"Режимы: {', '.join(sorted({item.management_mode for item in items})) or 'DRY_RUN'}\n"
            f"Объявлений: {len(items)}/5 · "
            f"активных: {sum(i.active and not options['paused'] for i in items)}\n"
            f"Последний цикл: {cycle.payload['at'] if cycle else '—'}\n"
            f"FAIL_SAFE: {', '.join(g.key.split(':')[-1] for g in guards) or '—'}\n"
            + "\n".join(f"• {reason}" for reason in ReadinessService.auto_blockers())
        )
