from __future__ import annotations

import hashlib
import time
from pathlib import Path

import psycopg

from turnstile_core.config import get_settings

CREATE_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migration_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise RuntimeError(f"Migration directory does not exist: {directory}")
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.up.sql"))
    if not files:
        raise RuntimeError(f"Migration directory contains no up migrations: {directory}")
    return files


def migrate() -> list[str]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required to run migrations")
    applied: list[str] = []
    with psycopg.connect(settings.database_url) as connection:
        deadline = time.monotonic() + 120
        while True:
            lock = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('turnstile:schema-migration', 0))"
            ).fetchone()
            if lock is not None and lock[0]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for another schema migration")
            time.sleep(0.1)
        connection.execute(CREATE_MIGRATION_TABLE)
        for path in migration_files(settings.migrations_dir):
            version = path.name.removesuffix(".up.sql")
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            existing = connection.execute(
                "SELECT checksum FROM schema_migration WHERE version = %s", (version,)
            ).fetchone()
            if existing is not None:
                if existing[0] != checksum:
                    raise RuntimeError(f"Migration checksum changed after apply: {version}")
                continue
            with connection.transaction():
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO schema_migration (version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
            applied.append(version)
    return applied


def main() -> None:
    for version in migrate():
        print(f"applied {version}")


if __name__ == "__main__":
    main()
