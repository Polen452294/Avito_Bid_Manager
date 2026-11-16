from datetime import timedelta
from decimal import Decimal


def money(kopecks):
    return f"{Decimal(kopecks) / 100:.2f} ₽" if kopecks is not None else "нет данных"


def number(value, suffix=""):
    return f"{value:.2f}{suffix}" if value is not None else "нет данных"


def format_report(report, blocked=(), allocations=()):
    item, actual, previous = (report[key] for key in ("item", "actual", "previous"))
    spend, provisional = report["spending"], report["provisional"]
    spend_today = "нет данных"
    if spend and spend.item_spent is not None:
        status = {"preliminary": "предварительно", "test": "тестовые данные"}.get(
            spend.data_type, "снимок"
        )
        checked_at = spend.checked_at.astimezone(report["timezone"])
        spend_today = f"{money(spend.item_spent)} ({status}, {checked_at:%d.%m %H:%M %z})"
    lines = [
        f"📢 {item.title[:120]} · {item.item_id}",
        f"💰 Расходы сегодня: {spend_today}",
        f"Дневной лимит: {money(item.daily_budget)} · "
        f"{report['budget_share']:.1f}% лимита аккаунта",
        f"🎯 Подтверждённая ставка: {money(report['current_bid'])} · {item.promotion_model}",
        f"Период {actual.start:%d.%m.%Y} — {actual.end - timedelta(days=1):%d.%m.%Y}",
        f"Качество набора метрик: {actual.quality}",
        f"Расходы: {money(actual.spend)} · в среднем за день: {money(actual.average_daily_spend)}",
        f"Просмотры: {number(actual.views)} · обращения: {number(actual.contacts)}",
        f"CPL: {number(actual.cpl, ' ₽')} · CPV: {number(actual.cpv, ' ₽')} · "
        f"конверсия: {number(actual.conversion, '%')}",
        f"Средняя наблюдавшаяся ставка: {money(actual.average_bid)} · "
        f"замеченные изменения: {number(actual.bid_changes)}",
    ]
    if report["budget_status"] == "UNRELIABLE_SPENDING":
        lines.append("Контроль расходов: BUDGET_DATA_UNAVAILABLE")
    if spend and spend.account_spent is not None:
        lines.append(
            f"Расходы аккаунта ({spend.data_type}) в том же срезе: {money(spend.account_spent)} / "
            f"{money(report['account_limit'])}"
        )
    if any(
        value is not None for value in (provisional.spend, provisional.views, provisional.contacts)
    ):
        lines.append(
            f"Предварительно по Авито · расходы {money(provisional.spend)}, "
            f"просмотры {number(provisional.views)}, обращения {number(provisional.contacts)}."
        )
    if actual.position_context:
        query, region, _, provider, kind = actual.position_context
        lines.append(
            f"Средняя позиция: {actual.position:.2f} · {actual.position_days} дн. · "
            f"{provider[:40]}/{kind} · {query[:50]} / {region[:40]}"
        )
    if previous:
        if (
            actual.spend is not None
            and previous.spend is not None
            and actual.metric_sources[0] == previous.metric_sources[0]
        ):
            change = money(actual.spend - previous.spend)
            lines.append(f"Изменение расходов к предыдущему периоду: {change}")
        if (
            actual.contacts is not None
            and previous.contacts is not None
            and actual.metric_sources[2] == previous.metric_sources[2]
        ):
            lines.append(f"Изменение обращений: {actual.contacts - previous.contacts:+d}")
        if (
            actual.position_context
            and actual.position_context == previous.position_context
            and actual.position_days == previous.position_days == actual.days
        ):
            lines.append(f"Изменение средней позиции: {actual.position - previous.position:+.2f}")
    for scope, threshold in report["warnings"].items():
        label = "объявления" if scope == "item" else "аккаунта"
        lines.append(f"⚠ Достигнуто {threshold}% дневного лимита {label}.")
    received = report.get("latest_metric_received")
    if received:
        lines.append(
            f"Последнее получение метрик: {received.astimezone(report['timezone']):%d.%m %H:%M %z}."
        )
    for recommendation in (report["recommendation"], *allocations):
        if recommendation is None:
            continue
        label = {
            "LOWER_BID": "Рассмотреть снижение ставки",
            "KEEP_BID": "Сохранить текущую ставку",
            "INSUFFICIENT_DATA": "Приостановить эксперименты с эффективностью",
            "REALLOCATE_BUDGET": "Рассмотреть перераспределение бюджета",
        }[recommendation.recommendation_type]
        lines.append(
            f"Рекомендация: {label} · {recommendation.reason_code}\n"
            f"{recommendation.expected_effect}"
        )
        if recommendation.suggested_value is not None:
            lines.append(f"Предложение: {money(recommendation.suggested_value)}")
    if blocked:
        lines.append("Последние блокировки: " + ", ".join(blocked))
    return "\n".join(lines)
