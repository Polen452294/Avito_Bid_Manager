import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from app.domain.models import PositionSnapshot
from app.positions.base import unavailable
from app.positions.validation import context_key, observation_key


def import_observations(path: Path) -> list[PositionSnapshot]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            data = list(csv.DictReader(stream))
        for row in data:
            for key in list(row):
                if row[key] == "":
                    del row[key]
            if "filters" in row:
                row["filters"] = json.loads(row["filters"])
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        raise ValueError("Use CSV or JSON")
    rows = TypeAdapter(list[PositionSnapshot]).validate_python(data)
    if any(row.checked_at > datetime.now(UTC) for row in rows):
        raise ValueError("Future observation")
    keys = [observation_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate observations")
    return rows


class ManualPositionProvider:
    name = "manual"
    interval_minutes = None  # Import driven; never refresh the observation timestamp.

    def __init__(self, history):
        self.history = history

    async def import_file(self, path):
        rows = import_observations(Path(path))
        return await self.history.append(rows)

    async def observe(self, item_id, query):
        rows = await self.history.history(item_id)
        rows = [
            r
            for r in rows
            if context_key(r.item_id, r.search_query, r.region, r.filters)
            == context_key(item_id, query)
        ]
        return (
            max(rows, key=lambda r: r.checked_at)
            if rows
            else unavailable(item_id, query, self.name, error="No imported observation")
        )
