from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database.models import RecommendationRow


async def performance_conflict(session, item, decision, now, settings):
    """A current advisory may veto a new increase. Restorations retain priority."""
    if (
        decision.action != "INCREASE"
        or decision.restore_bid is not None
        or item.promotion_model.value == "MOCK"
    ):
        return None
    today = now.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    table = RecommendationRow
    rows = await session.scalars(
        select(table.payload)
        .where(
            table.item_id == item.item_id,
            table.payload["group"].as_string() == "bid",
            table.payload["analysis_period"][1].as_string() == today,
        )
        .order_by(table.id.desc())
    )
    config = item.model_dump(mode="json", exclude={"title"})
    seen = set()
    for row in rows:
        key = row["period_key"]
        if key in seen:
            continue
        seen.add(key)
        if (
            row["recommendation_type"] == "LOWER_BID"
            and row["data_quality"] == "COMPARABLE"
            and row.get("item_config") == config
            and row["current_value"] == decision.old_bid
        ):
            return row["reason_code"]
    return None
