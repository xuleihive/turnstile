from __future__ import annotations

import shutil
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.migrate import migrate, migration_files
from tests.backend.persistence.test_price_sync_concurrency import (
    pricing_postgres as pricing_postgres,
)
from tests.support.paths import REPOSITORY_ROOT


@pytest.mark.parametrize("previous_installation", ["clean", "upstream", "assistant"])
def test_assistant_migration_coexists_with_v12_and_preserves_applied_checksums(
    pricing_postgres: tuple[str, Event], tmp_path: Path, previous_installation: str,
) -> None:
    info, _ = pricing_postgres
    database = "assistant_migration_" + uuid4().hex
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    isolated_info = make_conninfo(info, dbname=database)
    source = REPOSITORY_ROOT / "migrations"
    files = migration_files(source)
    initial = "001_initial_schema.up.sql"
    assistant = "009_assistant_api_format.up.sql"
    for path in files:
        if previous_installation == "upstream" and path.name == assistant:
            continue
        if previous_installation == "assistant" and path.name not in {initial, assistant}:
            continue
        shutil.copyfile(path, tmp_path / path.name)
    settings = SimpleNamespace(database_url=isolated_info, migrations_dir=tmp_path)
    with patch("backend.migrate.get_settings", return_value=settings):
        migrate()
        with psycopg.connect(isolated_info) as connection:
            before: dict[str, str] = dict(
                connection.execute("SELECT version, checksum FROM schema_migration"),
            )
            connection.execute(
                "INSERT INTO assistant_setting(id, auto_title, updated_by) "
                "VALUES(true, false, 'migration-test')",
            )
        for path in files:
            shutil.copyfile(path, tmp_path / path.name)
        applied = migrate()
        assert ("009_assistant_api_format" in applied) == (previous_installation == "upstream")
        assert migrate() == []
    with psycopg.connect(isolated_info) as connection:
        after: dict[str, str] = dict(
            connection.execute("SELECT version, checksum FROM schema_migration"),
        )
        assert before.items() <= after.items()
        assert set(after) == {path.name.removesuffix(".up.sql") for path in files}
        assert connection.execute(
            "SELECT auto_title, api_format, updated_by FROM assistant_setting",
        ).fetchone() == (False, None, "migration-test")
        for protocol in ("openai_chat", "openai_responses", "anthropic_messages"):
            connection.execute("UPDATE assistant_setting SET api_format = %s", (protocol,))
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute("UPDATE assistant_setting SET api_format = 'microsoft_decision'")
