import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from filelock import FileLock, Timeout

from app.domain.enums import Mode
from app.services.execution_service import UNRESOLVED


def runtime_status(path):
    heartbeat = path.with_suffix(".heartbeat")
    scheduler = "STOPPED"
    try:
        with FileLock(str(path.resolve()) + ".lock", timeout=0):
            pass
    except Timeout:
        try:
            age = datetime.now(UTC).timestamp() - heartbeat.stat().st_mtime
        except FileNotFoundError:
            age = None
        scheduler = "RESPONSIVE" if age is not None and 0 <= age <= 180 else "STALE"
    report = {
        "database": "UNAVAILABLE",
        "scheduler": scheduler,
        "last_cycle_at": None,
        "last_spending_at": None,
        "last_position_at": None,
        "unresolved_operations": None,
        "paused": None,
    }
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                return {**report, "database": "INTEGRITY_FAILED"}
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return {**report, "database": "INTEGRITY_FAILED"}
            revision = db.execute("SELECT version_num FROM alembic_version").fetchone()
            if revision != ("0004",):
                return {**report, "database": "MIGRATION_REQUIRED"}
            cycle = db.execute(
                "SELECT json_extract(payload, '$.at') FROM runtime_states WHERE key=?",
                ("telegram:last_cycle",),
            ).fetchone()
            pause = db.execute(
                "SELECT json_extract(payload, '$.paused') FROM runtime_states WHERE key=?",
                ("telegram:options",),
            ).fetchone()
            report.update(
                database="AVAILABLE",
                last_cycle_at=cycle[0] if cycle else None,
                paused=bool(pause and pause[0]),
                last_spending_at=db.execute(
                    "SELECT max(json_extract(payload, '$.checked_at')) FROM spending_snapshots"
                ).fetchone()[0],
                last_position_at=db.execute(
                    "SELECT max(json_extract(payload, '$.checked_at')) "
                    "FROM position_snapshots WHERE json_extract(payload, '$.status') = 'FOUND'"
                ).fetchone()[0],
                unresolved_operations=db.execute(
                    "SELECT count(*) FROM bid_change_operations WHERE status IN (?, ?, ?)",
                    UNRESOLVED,
                ).fetchone()[0],
            )
    except sqlite3.Error:
        return report
    return report


class ReadinessService:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def auto_blockers():
        return [
            "ASSISTED_READINESS_REQUIRED",
            "VERIFIED_FRESH_COMPARABLE_LIVE_POSITIONS_REQUIRED",
            "FINANCIAL_FRESHNESS_AND_BUDGET_ENFORCEMENT_UNVERIFIED",
            "LIVE_RECOVERY_AND_INTEGRATION_TRIALS_REQUIRED",
            "AUTO_OWNER_APPROVAL_REQUIRED",
        ]

    async def evaluate(self, avito_report=None):
        from app.main import database_path, simulate

        path = database_path(self.settings)
        runtime = await asyncio.to_thread(runtime_status, path)
        simulation = await simulate(self.settings)
        levels = {
            "SIMULATION_READY": [
                reason
                for condition, reason in (
                    (runtime["database"] == "AVAILABLE", "DATABASE_" + runtime["database"]),
                    (self.settings.mode == Mode.DRY_RUN, "STARTUP_MODE_MUST_BE_DRY_RUN"),
                    (
                        simulation["status"] == "DRY_RUN"
                        and simulation["provider_writes"] == 0
                        and simulation["decision"]["new_bid"] is not None,
                        "LOCAL_DRY_RUN_CHECK_FAILED",
                    ),
                )
                if not condition
            ],
            "READ_ONLY_READY": [],
            "ASSISTED_READY": [
                "LIVE_EXECUTION_ADAPTER_DISABLED",
                "LIVE_WRITE_PERMISSION_AND_RECONCILIATION_UNVERIFIED",
                "LIVE_EXECUTION_OWNER_APPROVAL_AND_TRIAL_REQUIRED",
            ],
            "AUTO_READY": self.auto_blockers(),
        }
        read = levels["READ_ONLY_READY"]
        if not avito_report or avito_report.get("simulated", True):
            read.append("LIVE_READ_EVIDENCE_REQUIRED")
        else:
            for key in ("authorization", "account", "listings", "bid_read", "finance"):
                if avito_report["capabilities"].get(key) != "AVAILABLE":
                    read.append(f"AVITO_{key.upper()}_UNAVAILABLE")
        return {
            "mode": self.settings.mode.value,
            "runtime": runtime,
            "levels": {
                level: {"ready": not reasons, "blocking_reasons": reasons}
                for level, reasons in levels.items()
            },
        }
