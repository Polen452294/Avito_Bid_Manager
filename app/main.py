import argparse
import asyncio
import json
import logging
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from filelock import FileLock, Timeout
from pydantic import ValidationError
from sqlalchemy import text

from app.analytics.collect import collect_avito
from app.analytics.import_data import StatisticsImport, import_statistics
from app.avito.bidding import MockBidProvider
from app.avito.exceptions import AvitoError
from app.avito.simulator import LocalSimulator
from app.bidding.backtest import ReplayFrame, backtest
from app.bidding.engine import BiddingEngine
from app.config import Settings
from app.database.models import RuntimeRow
from app.database.repository import Repository
from app.database.session import backup_database, make_database, prune_backups
from app.domain.enums import PositionStatus, PositionType, PromotionModel
from app.domain.models import Advertisement, BidCapabilities, BidInfo, PositionSnapshot, Spending
from app.positions.base import MockPositionProvider, OfficialPositionProvider
from app.positions.history import PositionHistory
from app.positions.manual import ManualPositionProvider, import_observations
from app.positions.monitor import PositionMonitor
from app.services.analytics_service import AnalyticsService
from app.services.bidding_service import BiddingService
from app.telegram.state import PanelState, sync_monitor_job


def database_path(settings):
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Use a persistent SQLite database")
    return Path(url.database)


async def demo(settings, sessions, repository):
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    # The demo always runs in its own in-memory database.
    item = Advertisement(
        item_id=1,
        title="SIMULATED advertisement",
        auto_enabled=True,
        promotion_model=PromotionModel.MOCK,
        min_bid=100,
        max_bid=5000,
        daily_budget=10000,
        queries=({"text": "demo", "region": "demo"},),
    )
    await repository.save_item(item)
    provider = MockBidProvider(
        {
            1: BidInfo(
                item_id=1,
                bid=1000,
                checked_at=now,
                capabilities=BidCapabilities(
                    model=PromotionModel.MOCK,
                    verified=True,
                    can_write=True,
                    minimum=100,
                    maximum=5000,
                    expires_at=now + timedelta(hours=1),
                ),
            )
        }
    )
    positions = [
        PositionSnapshot(
            item_id=1,
            search_query="demo",
            region="demo",
            position=12,
            position_type=PositionType.MIXED,
            checked_at=now - timedelta(minutes=minute),
            provider="mock",
            confidence=1,
            status=PositionStatus.FOUND,
        )
        for minute in (15, 0)
    ]
    spending = Spending(
        item_id=1,
        item_spent=0,
        account_spent=0,
        checked_at=now,
        period_date=now.astimezone(ZoneInfo(settings.timezone)).date().isoformat(),
        reliable=True,
    )
    decision, status = await BiddingService(sessions, provider, BiddingEngine(settings)).evaluate(
        item, positions, spending
    )
    return {
        "simulated": True,
        "status": status,
        "provider_writes": provider.write_count,
        "decision": decision.model_dump(mode="json"),
    }


async def simulate(settings):
    from app.database.models import Base

    engine, sessions = make_database("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return await demo(settings, sessions, Repository(sessions))
    finally:
        await engine.dispose()


async def run(args):
    settings = Settings()
    if args.command == "backtest":
        data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
        results = backtest(
            BiddingEngine(settings),
            Advertisement.model_validate(data["item"]),
            BidInfo.model_validate(data["bid_info"]),
            (ReplayFrame.model_validate(frame) for frame in data["frames"]),
            assume_applied=args.assume_applied,
            position_origin=args.position_origin,
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if args.command == "demo":
        print(json.dumps(await simulate(settings), ensure_ascii=False, indent=2))
        return
    path = database_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine, sessions = make_database(settings.database_url)
    try:
        repository = Repository(sessions)
        history = PositionHistory(sessions)
        analytics = AnalyticsService(repository, settings)
        if args.command == "health":
            async with sessions() as session:
                if await session.scalar(text("SELECT version_num FROM alembic_version")) != "0004":
                    raise RuntimeError("Unexpected database revision")
            try:
                with FileLock(str(path.resolve()) + ".lock", timeout=0):
                    pass
            except Timeout:
                pass
            else:
                raise RuntimeError("Service instance is not running")
            stamp = path.with_suffix(".heartbeat")
            if (
                not stamp.exists()
                or not 0 <= datetime.now(UTC).timestamp() - stamp.stat().st_mtime <= 180
            ):
                raise RuntimeError("Scheduler heartbeat unavailable")
            print("READY: DRY_RUN; Avito=BLOCKED; live_positions=UNVERIFIED")
            return
        with FileLock(str(path.resolve()) + ".lock", timeout=0):
            if args.command == "serve":
                path.with_suffix(".heartbeat").unlink(missing_ok=True)
            if args.command == "import-positions":
                print(f"Imported: {await ManualPositionProvider(history).import_file(args.file)}")
                return
            if args.command == "serve" and getattr(args, "migrate", False):
                if path.exists() and path.stat().st_size:
                    async with sessions() as session:
                        version_table = await session.scalar(
                            text("SELECT name FROM sqlite_master WHERE name='alembic_version'")
                        )
                        revision = (
                            await session.scalar(text("SELECT version_num FROM alembic_version"))
                            if version_table
                            else None
                        )
                    if revision != "0004":
                        target = (
                            path.parent
                            / "backups"
                            / (f"before-migration-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.db")
                        )
                        await backup_database(path, target)
                        prune_backups(
                            target.parent, "before-migration-*.db", settings.backup_retention
                        )
                config = Config("alembic.ini")

                def upgrade(connection):
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")

                async with engine.begin() as connection:
                    await connection.exec_driver_sql("BEGIN IMMEDIATE")
                    await connection.run_sync(upgrade)
            if args.command == "add-item":
                await repository.save_item(
                    Advertisement.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
                )
                print("Saved in DRY_RUN")
                return
            if args.command == "backup":
                await backup_database(path, Path(args.file))
                return
            if args.command == "import-statistics":
                data = StatisticsImport.model_validate_json(Path(args.file).read_text("utf-8-sig"))
                print(f"Metric versions imported: {await import_statistics(analytics, data)}")
                await analytics.refresh()
                return
            recovered = await repository.recover()
            logging.info("event=startup mode=DRY_RUN unresolved_operations=%s", recovered)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass  # asyncio.run handles keyboard interrupts on Windows.
            scheduler = AsyncIOScheduler(timezone=settings.timezone)
            active_jobs = set()

            def schedule(job, trigger, **kwargs):
                async def run_job():
                    task = asyncio.current_task()
                    active_jobs.add(task)
                    started = datetime.now(UTC).isoformat()
                    error = None
                    try:
                        try:
                            await job()
                        except Exception as exc:
                            error = type(exc).__name__
                            logging.error(
                                "event=scheduled_job_failed job=%s error=%s", job.__name__, error
                            )
                        if job_id := kwargs.get("id"):
                            async with sessions.begin() as session:
                                key = f"telegram:job:{job_id}"
                                previous = await session.get(RuntimeRow, key)
                                last_error = (
                                    previous.payload.get("last_error") if previous else None
                                )
                                await session.merge(
                                    RuntimeRow(
                                        key=key,
                                        payload={
                                            "last_run": started,
                                            "last_error": f"{started} · {error}"
                                            if error
                                            else last_error,
                                        },
                                    )
                                )
                    finally:
                        active_jobs.discard(task)

                scheduler.add_job(run_job, trigger, **kwargs)

            async def heartbeat():
                async with sessions() as session:
                    await session.execute(text("SELECT 1"))
                path.with_suffix(".heartbeat").write_text(datetime.now(UTC).isoformat())

            async def backup():
                day = datetime.now(scheduler.timezone).date()
                target = path.parent / "backups" / f"manager-{day:%Y%m%d}.db"
                await backup_database(path, target)
                prune_backups(target.parent, "manager-????????.db", settings.backup_retention)
                logging.info("event=backup_complete date=%s", target.stem)

            schedule(heartbeat, "interval", minutes=1, max_instances=1, coalesce=True)
            schedule(backup, "cron", hour=3, minute=0, max_instances=1, coalesce=True)
            sources = [OfficialPositionProvider()]
            if args.file:
                sources.append(MockPositionProvider(import_observations(Path(args.file))))
            bidding = BiddingService(sessions, LocalSimulator(sessions), BiddingEngine(settings))
            panel = PanelState(repository, settings, bidding)
            await panel.load()
            await bidding.execution.reconcile()
            jobs = None
            if settings.telegram_bot_token and settings.telegram_admin_ids:
                from app.telegram.jobs import TelegramJobs

                jobs = TelegramJobs(panel, analytics)

            async def evaluate_item(item):
                if not (await panel.options())["paused"]:
                    await bidding.evaluate_new_history(
                        item, history, mode=item.management_mode, managed=True
                    )

            monitor = PositionMonitor(
                repository,
                history,
                sources,
                evaluate=evaluate_item,
                notify=jobs.positions if jobs else None,
            )

            async def monitor_cycle():
                if (await panel.options())["monitor_paused"]:
                    return
                if not await monitor.tick():
                    return
                async with sessions.begin() as session:
                    await session.merge(
                        RuntimeRow(
                            key="telegram:last_cycle", payload={"at": datetime.now(UTC).isoformat()}
                        )
                    )

            async def refresh_analytics():
                avito_error = None
                if settings.statistics_read_enabled:
                    try:
                        await collect_avito(settings, await repository.items(), analytics.store)
                    except (AvitoError, OSError, ValueError, TypeError) as exc:
                        avito_error = exc
                        logging.warning("event=statistics_unavailable error=%s", type(exc).__name__)
                        if jobs:
                            await jobs.api_failure(exc)
                    else:
                        logging.info("event=statistics_cycle_complete")
                        if jobs:
                            await jobs.api_failure()
                await analytics.refresh()
                if avito_error:
                    raise avito_error

            schedule(
                refresh_analytics,
                "interval",
                id="refresh_analytics",
                minutes=settings.statistics_interval_minutes,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
            schedule(analytics.refresh, "cron", hour=8, max_instances=1, coalesce=True)
            if jobs:
                schedule(
                    jobs.tick,
                    "interval",
                    id="telegram_delivery",
                    seconds=15,
                    max_instances=1,
                    coalesce=True,
                )
            schedule(
                monitor_cycle,
                "interval",
                id="position_monitor",
                minutes=1,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
            monitor_paused = (await panel.options())["monitor_paused"]
            scheduler.start()
            sync_monitor_job(scheduler, monitor_paused)
            bot_task = None
            try:
                await heartbeat()
                if jobs:
                    from app.telegram.bot import run_bot

                    bot_task = asyncio.create_task(
                        run_bot(
                            settings,
                            repository,
                            bidding,
                            analytics,
                            panel_state=panel,
                            scheduler=scheduler,
                            monitor=monitor,
                        )
                    )
                    bot_task.add_done_callback(lambda _: stop.set())
                await stop.wait()
                if bot_task and bot_task.done():
                    await bot_task
            finally:
                scheduler.pause()
                if bot_task:
                    bot_task.cancel()
                    await asyncio.gather(bot_task, return_exceptions=True)
                scheduler.shutdown(wait=False)
                if active_jobs:
                    await asyncio.gather(*active_jobs, return_exceptions=True)
                await asyncio.sleep(0)
                path.with_suffix(".heartbeat").unlink(missing_ok=True)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "health",
            "demo",
            "add-item",
            "backup",
            "import-positions",
            "import-statistics",
            "backtest",
        ],
        default="serve",
        nargs="?",
    )
    parser.add_argument("--file")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--assume-applied", action="store_true")
    parser.add_argument(
        "--position-origin", choices=["historical", "fixture"], default="historical"
    )
    args = parser.parse_args()
    if (
        args.command
        in (
            "add-item",
            "backup",
            "import-positions",
            "import-statistics",
            "backtest",
        )
        and not args.file
    ):
        parser.error("--file is required")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiogram.dispatcher").setLevel(logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except ValidationError:
        logging.error("Invalid settings or data. Check the local configuration and input format.")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
