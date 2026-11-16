from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from app.bidding.budget import BudgetController
from app.config import Settings
from app.domain.enums import Mode
from app.domain.models import (
    Advertisement,
    AppliedChange,
    BidInfo,
    Decision,
    PositionSnapshot,
    Spending,
)
from app.positions.validation import context_key, usable


class BiddingEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetController(settings)

    def decide(
        self,
        item: Advertisement,
        info: BidInfo | None,
        observations: list[PositionSnapshot],
        spending: Spending | None,
        now: datetime,
        *,
        last_change: datetime | None = None,
        changes_today: int = 0,
        fail_safe: bool = False,
        effective_floor: int | None = None,
        applied: tuple[AppliedChange, ...] = (),
        mode: Mode = Mode.ASSISTED,
    ) -> Decision:
        s = self.settings
        up = item.increase_confirmations or s.increase_confirmations
        down = item.decrease_confirmations or s.decrease_confirmations
        daily_limit = item.max_changes_per_day or s.max_changes_per_day
        stabilization = timedelta(minutes=item.stabilization_minutes or item.cooldown_minutes)
        last = max(applied, key=lambda change: change.applied_at, default=None)
        last_increase = max(
            (change for change in applied if change.new_bid > change.old_bid),
            key=lambda change: change.applied_at,
            default=None,
        )
        recoveries = [change for change in applied if change.restore_bid is not None]
        floor_change = max(
            recoveries,
            key=lambda change: (change.restore_bid, change.applied_at),
            default=None,
        )
        latest_recovery = max(recoveries, key=lambda change: change.applied_at, default=None)
        relevant_changes = dict.fromkeys(c for c in (last, last_increase, floor_change) if c)
        if last and (last_change is None or last.applied_at > last_change):
            last_change = last.applied_at
        zone = ZoneInfo(s.timezone)
        day = now.astimezone(zone).date()
        changes_today = max(
            changes_today,
            sum(change.applied_at.astimezone(zone).date() == day for change in applied),
        )
        bid = info.bid if info else None
        limits = {
            "item": item.model_dump(mode="json", exclude={"title"}),
            "capabilities": info.capabilities.model_dump(mode="json") if info else None,
            "spending": spending.model_dump(mode="json") if spending else None,
            "account_daily_budget": s.account_daily_limit_kopecks,
            "budget_reserve_percent": s.budget_reserve_percent,
            "budget_warnings": self.budget.warnings(item, spending, now),
            "budget_status": self.budget.check(item, spending, now) or "AVAILABLE",
            "max_position_data_age_minutes": s.max_position_data_age_minutes,
            "max_spending_data_age_minutes": s.max_spending_data_age_minutes,
            "increase_confirmations": up,
            "decrease_confirmations": down,
            "max_changes_per_day": daily_limit,
            "changes_today": changes_today,
            "stabilization_minutes": int(stabilization.total_seconds()) // 60,
            "last_applied_at": last_change.isoformat() if last_change else None,
            "applied": [change.model_dump(mode="json") for change in relevant_changes],
        }
        evidence = []
        score = None
        coverage = Decimal(0)

        def result(reason, *, code=None, candidate=None, expected=None, restore=None):
            action = "HOLD"
            if candidate is not None:
                action = "INCREASE" if candidate > bid else "DECREASE"
            return Decision(
                item_id=item.item_id,
                old_bid=bid,
                new_bid=candidate,
                created_at=now,
                action=action,
                reason=reason,
                reason_code=code or reason,
                observations=tuple(evidence),
                confidence=min((p.confidence for p in evidence), default=None),
                position_score=score,
                coverage=coverage,
                limits=limits,
                expected_result=expected,
                restore_bid=restore,
                authorization="REQUIRES_PREFLIGHT" if candidate is not None else "NOT_APPLICABLE",
            )

        if fail_safe:
            return result("FAIL_SAFE", code="AWAITING_RECONCILIATION")
        if not item.active or not item.auto_enabled:
            return result("PAUSED")
        if now.astimezone(zone).hour not in item.active_hours:
            return result("OUTSIDE_SCHEDULE")
        if (
            info is None
            or info.item_id != item.item_id
            or info.capabilities.model != item.promotion_model
            or (mode != Mode.DRY_RUN and not info.capabilities.can_write)
            or not info.capabilities.allows(info.bid, now)
            or not timedelta(0) <= now - info.checked_at <= timedelta(minutes=15)
        ):
            return result("UNVERIFIED_BID")
        if last_change and now < last_change + max(
            stabilization, timedelta(minutes=item.cooldown_minutes)
        ):
            return result("COOLDOWN", code="COOLDOWN_ACTIVE")
        if changes_today >= daily_limit:
            return result("CHANGE_LIMIT")
        if not item.queries:
            return result("NO_QUERIES", code="POSITION_DATA_MISSING")

        max_age = timedelta(minutes=s.max_position_data_age_minutes)
        earliest = last_change + stabilization if last_change else None
        contexts = defaultdict(list)
        for row in observations:
            key = context_key(row.item_id, row.search_query, row.region, row.filters)
            contexts[key].append(row)

        directions = set()
        comparable = set()
        latest_times = []
        weighted = Decimal(0)
        covered_weight = Decimal(0)
        primary_issue = None
        unconfirmed = False
        issue = "INSUFFICIENT_CONFIRMATIONS"
        decrease_bound = item.target_max - item.hysteresis_positions
        for index, query in enumerate(item.queries):
            rows = contexts[context_key(item.item_id, query)]
            direction, reason, samples = self._confirmation(
                rows, item, now, max_age, earliest, up, down, decrease_bound
            )
            evidence.extend(samples)
            if reason in {"INCOMPARABLE_OBSERVATIONS", "DUPLICATE_OBSERVATIONS"}:
                return result(reason)
            if direction is None:
                issue = reason
                unconfirmed |= reason == "INSUFFICIENT_CONFIRMATIONS"
                if index == item.primary_query_index:
                    primary_issue = reason
                continue
            latest = samples[-1]
            latest_times.append(latest.period_end or latest.checked_at)
            comparable.add((latest.provider, latest.position_type))
            directions.add(direction)
            weighted += latest.position * query.weight
            covered_weight += query.weight

        coverage = covered_weight / sum(q.weight for q in item.queries)
        score = weighted / covered_weight if covered_weight else None
        if len(comparable) > 1:
            return result("INCOMPARABLE_OBSERVATIONS")
        if latest_times and max(latest_times) - min(latest_times) > timedelta(
            minutes=item.max_query_skew_minutes
        ):
            return result("INCOMPARABLE_OBSERVATIONS")
        if len(directions) > 1:
            return result("CONFLICTING_QUERIES")
        if primary_issue or unconfirmed or coverage < item.min_position_coverage:
            return result(primary_issue or ("INSUFFICIENT_CONFIRMATIONS" if unconfirmed else issue))
        direction = next(iter(directions))
        if direction == 0:
            return result("HYSTERESIS_BAND", code="TARGET_STABLE")
        if (reason := limits["budget_status"]) != "AVAILABLE":
            code = "BUDGET_DATA_UNAVAILABLE" if reason == "UNRELIABLE_SPENDING" else "BUDGET_LIMIT"
            return result(reason, code=code)

        increase = direction > 0
        restore = None
        if increase:
            if bid >= item.max_bid:
                return result("MAX_BID_REACHED")
            if last_increase and now - last_increase.applied_at < timedelta(
                minutes=item.increase_interval_minutes
            ):
                return result("INCREASE_INTERVAL_ACTIVE")
            if last and last.new_bid == bid:
                restore = last.restore_bid
                if last.probe and last.new_bid < last.old_bid:
                    restore = last.old_bid
                if restore is not None and restore <= bid:
                    restore = None

        if not increase and not item.trial_decreases:
            return result("TRIAL_DECREASES_DISABLED")

        configured_floor = max(item.min_bid, info.capabilities.minimum)
        learned_floor = floor_change.restore_bid if floor_change else 0
        review = (
            not increase
            and mode == Mode.DRY_RUN
            and latest_recovery is not None
            and bid <= max(configured_floor, effective_floor or 0, learned_floor)
            and self._can_review_floor(item, observations, now, latest_recovery, last_change)
        )
        if review:
            learned_floor = 0
        floor = (
            configured_floor
            if increase
            else max(configured_floor, effective_floor or 0, learned_floor)
        )
        limits["effective_floor"] = floor
        if not increase and bid <= floor:
            return result(
                "EFFECTIVE_FLOOR_REACHED" if floor > configured_floor else "MIN_BID_REACHED"
            )
        candidate = self._candidate(item, info, now, increase, floor, restore)
        if candidate is None:
            return result("NO_ALLOWED_STEP", code="UNSUPPORTED_BID_VALUE")
        if review:
            limits["floor_review_failure_at"] = latest_recovery.applied_at.isoformat()
            return result(
                "FLOOR_REVIEW_PROBE",
                candidate=candidate,
                expected="Test whether improved positions support a lower bid in DRY_RUN",
            )
        if restore is not None:
            return result(
                "RESTORE_AFTER_PROBE",
                code="TARGET_BELOW_RANGE",
                candidate=candidate,
                expected="Recover target positions after an unsuccessful decrease",
                restore=restore,
            )
        if increase:
            return result(
                "PERSISTENT_LOW_POSITION",
                code="TARGET_BELOW_RANGE",
                candidate=candidate,
                expected="Improve positions toward the target range",
            )
        return result(
            "CAUTIOUS_REDUCTION",
            code="POSITION_ABOVE_TARGET" if score < item.target_min else "TARGET_STABLE",
            candidate=candidate,
            expected="Reduce cost while retaining positions within or above target",
        )

    def _can_review_floor(self, item, observations, now, restored, last_change):
        s = self.settings
        review_at = restored.applied_at + timedelta(days=s.floor_review_days)
        if now < review_at:
            return False
        earliest = max(
            review_at,
            now - timedelta(hours=s.floor_review_window_hours),
            last_change or review_at,
        )
        query = item.queries[item.primary_query_index]
        key = context_key(item.item_id, query)
        rows = [
            row
            for row in observations
            if context_key(row.item_id, row.search_query, row.region, row.filters) == key
            and row.provider in item.position_sources
            and earliest <= row.checked_at <= now
        ]
        if len({(row.provider, row.position_type) for row in rows}) != 1:
            return False
        # A sustained buffer above the target is required after the previous failure.
        return (
            len({row.checked_at for row in rows}) == len(rows)
            and len(rows) >= s.floor_review_observations
            and all(
                usable(row, now, timedelta(hours=s.floor_review_window_hours))
                and row.position <= item.target_min
                and (row.provider != "mock" or item.promotion_model.value == "MOCK")
                for row in rows
            )
        )

    @staticmethod
    def _confirmation(rows, item, now, max_age, earliest, up, down, decrease_bound):
        if not rows:
            return None, "POSITION_DATA_MISSING", []
        trusted = sorted(
            (p for p in rows if p.provider in item.position_sources), key=lambda p: p.checked_at
        )[-max(up, down) :]
        if not trusted:
            return None, "SOURCE_NOT_VERIFIED", [max(rows, key=lambda p: p.checked_at)]
        valid = []
        issue = "INSUFFICIENT_CONFIRMATIONS"
        for p in trusted:
            sample_at = p.period_end or p.checked_at
            if not timedelta(0) <= now - sample_at <= max_age:
                issue = "POSITION_DATA_STALE"
            elif earliest and (p.period_start or p.checked_at) <= earliest:
                issue = "AWAITING_POST_STABILIZATION_DATA"
            elif not usable(p, now, max_age):
                issue = "POSITION_DATA_UNRELIABLE"
            else:
                valid.append(p)
                continue
            valid.clear()
        if not valid:
            return None, issue, trusted[-1:]
        times = [p.period_end or p.checked_at for p in valid]
        if len(set(times)) != len(times) or len({p.checked_at for p in valid}) != len(valid):
            return None, "DUPLICATE_OBSERVATIONS", valid
        if len({(p.provider, p.position_type) for p in valid}) > 1 or any(
            current.period_start is not None
            and current.period_start < (previous.period_end or previous.checked_at)
            for previous, current in pairwise(valid)
        ):
            return None, "INCOMPARABLE_OBSERVATIONS", valid
        if len(valid) >= up and all(p.position > item.target_max for p in valid[-up:]):
            return 1, None, valid[-up:]
        if len(valid) >= down and all(p.position <= item.target_max for p in valid[-down:]):
            tail = valid[-down:]
            return (-1 if all(p.position <= decrease_bound for p in tail) else 0), None, tail
        return None, issue, valid

    def _candidate(self, item, info, now, increase, floor, restore):
        s = self.settings
        bid, caps = info.bid, info.capabilities
        if increase:
            amount = item.increase_step_kopecks or s.increase_step_kopecks
            percent = item.increase_step_percent or s.increase_step_percent
            step = max(amount, (bid * percent + 99) // 100)
        else:
            amount = item.decrease_step_kopecks or s.decrease_step_kopecks
            percent = item.decrease_step_percent or s.decrease_step_percent
            step = max(amount, bid * percent // 100)
        step = min(step, item.max_step_kopecks, bid * item.max_step_percent // 100)
        if not increase:
            reverse_amount = item.increase_step_kopecks or s.increase_step_kopecks
            reverse_percent = item.increase_step_percent or s.increase_step_percent
            # Solve each percentage bound at the lower bid, including the upward rounding.
            step = min(
                step,
                bid * item.max_step_percent // (100 + item.max_step_percent),
                max(reverse_amount, (bid * reverse_percent + 99) // (100 + reverse_percent)),
            )
        ceiling = item.max_bid
        if caps.allowed_bids:
            if increase:
                ceiling = min(ceiling, restore) if restore is not None else ceiling
                low, high = max(floor, bid + 1), min(ceiling, bid + step)
            else:
                low, high = max(floor, bid - step), min(ceiling, bid - 1)
            values = [value for value in caps.allowed_bids if low <= value <= high]
            return (max(values) if increase else min(values)) if values else None
        ceiling = min(ceiling, caps.maximum)
        if increase:
            ceiling = min(ceiling, restore) if restore is not None else ceiling
            candidate = min(ceiling, bid + step) // caps.increment * caps.increment
            if candidate <= bid:
                return None
        else:
            desired = max(floor, bid - step)
            candidate = (desired + caps.increment - 1) // caps.increment * caps.increment
            if candidate >= bid:
                return None
        if floor <= candidate <= ceiling and caps.allows(candidate, now):
            return candidate
        return None
