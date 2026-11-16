"""Read-only account diagnostics. Never dispatches any promotion mutation."""

import argparse
import asyncio
import json
import logging
from hashlib import sha256

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.avito.diagnostics import diagnose
from app.config import Settings
from app.database.models import RuntimeRow
from app.database.session import make_database
from app.main import database_path
from app.services.readiness import ReadinessService


async def check_account(settings):
    report = await diagnose(settings)
    if not settings.avito_client_id:
        return report
    try:
        path = database_path(settings)
    except ValueError:
        report["history_status"] = "LOCAL_HISTORY_UNAVAILABLE"
        return report
    if not path.is_file():
        return report
    scope = json.dumps(
        [
            settings.avito_client_id.get_secret_value(),
            settings.avito_expected_account_id,
            settings.avito_item_ids,
            settings.avito_official_spec_paths,
        ]
    )
    key = "avito:last_success:" + sha256(scope.encode()).hexdigest()
    checked = report["last_successful_check_at"]
    stamp = checked
    engine, sessions = make_database(settings.database_url)
    try:
        async with sessions.begin() as session:
            if checked:
                await session.execute(text("BEGIN IMMEDIATE"))
            previous = await session.get(RuntimeRow, key)
            if previous and (not checked or previous.payload["at"] > checked):
                stamp = previous.payload["at"]
            elif checked:
                await session.merge(RuntimeRow(key=key, payload={"at": checked}))
        report["last_successful_check_at"] = stamp
    except SQLAlchemyError:
        report["history_status"] = "LOCAL_HISTORY_UNAVAILABLE"
    finally:
        await engine.dispose()
    return report


async def run(command, settings, check_avito):
    if command == "check-avito":
        return await check_account(settings)
    evidence = await check_account(settings) if check_avito else None
    report = await ReadinessService(settings).evaluate(evidence)
    if evidence is not None:
        report["avito"] = evidence
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check-avito", "readiness"])
    parser.add_argument("--check-avito", action="store_true", help="Include live read-only probes")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = Settings()
    except (ValidationError, ValueError):
        print(
            json.dumps(
                {
                    "status": "INVALID_CONFIGURATION",
                    "reason": "Check environment values; secrets are not displayed",
                }
            )
        )
        return 2
    try:
        report = asyncio.run(run(args.command, settings, args.check_avito))
    except Exception:
        # CLI must not expose arbitrary exceptions containing request/config secrets.
        print(
            json.dumps(
                {"status": "DIAGNOSTIC_FAILED", "reason": "Unexpected local diagnostic error"}
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.command == "readiness":
        return 0 if report["levels"]["SIMULATION_READY"]["ready"] else 1
    return (
        0 if report["status"] in {"NOT_CONFIGURED", "READ_CHECKS_PASSED", "CONTRACT_BLOCKED"} else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
