"""Shared identities and validation for every position source."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from app.domain.enums import PositionStatus, PositionType


def context_key(item_id, query, region=None, filters=None):
    if region is None:
        query, region, filters = query.text, query.region, query.filters
    value = json.dumps([item_id, query, region, filters or {}], sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def observation_key(row):
    value = [
        context_key(row.item_id, row.search_query, row.region, row.filters),
        row.provider,
        row.checked_at.isoformat(),
        row.position_type.value,
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def usable(row, now, max_age=timedelta(minutes=30)):
    return (
        row.status == PositionStatus.FOUND
        and row.confidence >= Decimal("0.8")
        and row.position_type
        not in (PositionType.AVERAGE, PositionType.UNKNOWN, PositionType.UNVERIFIED)
        and timedelta(0) <= now - row.checked_at <= max_age
    )
