from datetime import timedelta

import pytest

from app.avito.bidding import MockBidProvider
from app.bidding.engine import BiddingEngine
from app.domain.enums import PositionStatus, PositionType
from app.positions.analyzer import PositionAnalyzer
from app.positions.base import ExternalPositionProvider, MockPositionProvider
from app.positions.history import PositionHistory
from app.positions.monitor import PositionMonitor
from app.services.bidding_service import BiddingService


@pytest.mark.parametrize(
    ("values", "trend"),
    [
        ((4, 6, 8), "WORSENING"),
        ((8, 6, 4), "IMPROVING"),
        ((4, 8, 4), "STABLE_OR_NOISY"),
    ],
)
def test_comparable_trend(case, values, trend):
    _, item, _, _, now, positions = case
    rows = positions(values)
    rows += [rows[-1].model_copy(update={"region": "other", "position": 99})]
    (result,) = PositionAnalyzer().analyze(item, rows, now)
    assert result["trend"] == trend


async def test_restart_and_failed_source(database, case):
    _, sessions, repository = database
    _, _, _, _, _, positions = case
    row = positions()[-1]
    history = PositionHistory(sessions)

    async def fail(*args):
        raise TimeoutError

    sources = [ExternalPositionProvider(fail), MockPositionProvider([row])]
    await PositionMonitor(repository, history, sources).tick()
    await PositionMonitor(repository, history, sources).tick()
    assert {r.status for r in await history.history(1)} == {
        PositionStatus.FOUND,
        PositionStatus.UNAVAILABLE,
    }
    assert len(await history.history(1)) == 2
    assert await history.append([row]) == 0


@pytest.mark.parametrize("kind", [PositionType.MIXED, PositionType.AVERAGE])
async def test_history_to_dry_run(database, case, kind):
    _, sessions, _ = database
    settings, item, info, spending, now, positions = case
    history = PositionHistory(sessions)
    rows = [r.model_copy(update={"position_type": kind}) for r in positions()]
    await history.append(rows)
    provider = MockBidProvider({1: info})
    service = BiddingService(sessions, provider, BiddingEngine(settings))
    decision, status = await service.evaluate_history(item, history, spending)
    assert status == ("HOLD" if kind == PositionType.AVERAGE else "DRY_RUN")
    assert provider.write_count == 0
    old = PositionAnalyzer().analyze(item, rows, now + timedelta(hours=2))
    assert old == []


@pytest.mark.parametrize("extension", ["csv", "json"])
async def test_manual_import(database, case, tmp_path, extension):
    import csv
    import json

    from app.positions.manual import ManualPositionProvider

    _, sessions, _ = database
    _, item, _, _, _, positions = case
    data = positions()[-1].model_dump(mode="json", exclude_none=True, exclude={"filters"})
    data["provider"] = "manual"
    path = tmp_path / f"position.{extension}"
    if extension == "csv":
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(data))
            writer.writeheader()
            writer.writerow(data)
    else:
        path.write_text(json.dumps([data]), encoding="utf-8")
    provider = ManualPositionProvider(PositionHistory(sessions))
    assert await provider.import_file(path) == 1
    assert await provider.import_file(path) == 0
    assert (await provider.observe(1, item.queries[0])).provider == "manual"


@pytest.mark.parametrize("failure", [PositionStatus.NOT_FOUND_IN_CHECKED_RANGE, "invalid"])
async def test_monitor_invalid_and_absent(database, case, failure):
    _, sessions, repository = database
    _, _, _, _, _, positions = case
    row = positions()[-1].model_dump()
    row.update(provider="external", checked_range=50, status=failure)
    if failure != "invalid":
        row["position"] = None

    async def observe(*args):
        return row

    history = PositionHistory(sessions)
    await PositionMonitor(repository, history, [ExternalPositionProvider(observe)]).tick()
    (result,) = await history.history(1)
    assert result.position is None
    assert result.status == (PositionStatus.INVALID_DATA if failure == "invalid" else failure)
