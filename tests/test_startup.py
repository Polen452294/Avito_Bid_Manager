import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("revision", [None, "0001"])
def test_migration_service_health_and_single_instance(tmp_path, revision):
    env = dict(os.environ)
    env.update(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}",
        MODE="DRY_RUN",
        PYTHONUTF8="1",
        TELEGRAM_BOT_TOKEN="",
    )
    root = Path(__file__).resolve().parents[1]

    def command(*args):
        return subprocess.run(
            [sys.executable, *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    if revision:
        migration = command("-m", "alembic", "upgrade", revision)
        assert migration.returncode == 0, migration.stderr
    service = subprocess.Popen(
        [sys.executable, "-m", "app.main", "serve", "--migrate"],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (tmp_path / "startup.heartbeat").exists():
            if service.poll() is not None:
                raise AssertionError("Service failed during startup")
            time.sleep(0.1)
        health = command("-m", "app.main", "health")
        assert health.returncode == 0, health.stderr
        assert command("-m", "alembic", "check").returncode == 0
        assert bool(list((tmp_path / "backups").glob("before-migration-*.db"))) == bool(revision)
        second = command("-m", "app.main", "serve", "--migrate")
        assert second.returncode != 0
        assert "Timeout" in second.stderr
    finally:
        service.terminate()
        service.wait(timeout=10)
    assert command("-m", "app.main", "health").returncode != 0
