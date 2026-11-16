from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.database.models import Base
from app.database.repository import Repository
from app.database.session import make_database
from app.domain.enums import PositionStatus, PositionType, PromotionModel
from app.domain.models import Advertisement, BidCapabilities, BidInfo, PositionSnapshot, Spending


@pytest.fixture
def case():
    now = datetime.now(UTC)
    settings = Settings(_env_file=None)
    item = Advertisement(
        item_id=1,
        title="Test",
        min_bid=100,
        max_bid=5000,
        daily_budget=10000,
        auto_enabled=True,
        promotion_model=PromotionModel.MOCK,
        queries=({"text": "q", "region": "r"},),
    )
    caps = BidCapabilities(
        model=PromotionModel.MOCK,
        verified=True,
        can_write=True,
        minimum=100,
        maximum=5000,
        expires_at=now + timedelta(hours=1),
    )
    info = BidInfo(item_id=1, bid=2000, checked_at=now, capabilities=caps)
    from zoneinfo import ZoneInfo

    spending = Spending(
        item_id=1,
        item_spent=0,
        account_spent=0,
        checked_at=now,
        period_date=now.astimezone(ZoneInfo(settings.timezone)).date().isoformat(),
        reliable=True,
    )

    def positions(values=(12, 12)):
        return [
            PositionSnapshot(
                item_id=1,
                search_query="q",
                region="r",
                position=value,
                position_type=PositionType.MIXED,
                provider="mock",
                confidence=1,
                checked_at=now - timedelta(minutes=(len(values) - idx - 1) * 10),
                status=PositionStatus.FOUND,
            )
            for idx, value in enumerate(values)
        ]

    return settings, item, info, spending, now, positions


@pytest.fixture
async def database(tmp_path, case):
    engine, sessions = make_database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = Repository(sessions)
    await repository.save_item(case[1])
    yield engine, sessions, repository
    await engine.dispose()
