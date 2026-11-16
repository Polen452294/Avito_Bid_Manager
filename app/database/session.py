import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def make_database(url: str):
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def backup_database(source: Path, target: Path):
    if source.resolve() == target.resolve():
        raise ValueError("Backup must have a separate path")
    if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Backup target has SQLite sidecar files; use a fresh path")
    target.parent.mkdir(parents=True, exist_ok=True)

    def backup():
        with NamedTemporaryFile(dir=target.parent, delete=False) as file:
            temporary = Path(file.name)
        try:
            with (
                closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src,
                closing(sqlite3.connect(temporary)) as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("SQLite snapshot failed integrity check")
                if dst.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("SQLite snapshot failed foreign key check")
                dst.execute("PRAGMA journal_mode=DELETE")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    await asyncio.to_thread(backup)


def prune_backups(directory: Path, pattern: str, keep: int):
    for expired in sorted(directory.glob(pattern), reverse=True)[keep:]:
        expired.unlink()
