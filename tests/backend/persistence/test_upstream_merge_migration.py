"""Validate the combined histories in an owned PostgreSQL, never the deployed database."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from backend import migrate as runner
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.persistence.test_user_settings_migration import (
    ledger,
    seed_history,
)
from tests.backend.persistence.test_user_settings_migration import (
    local_postgres as local_postgres,
)
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

MIGRATIONS = REPOSITORY_ROOT / "migrations"
FORK_EXTENSIONS = {
    "009_assistant_api_format.up.sql", "010_models_dev_pricing.up.sql",
    "011_user_settings_profile.up.sql", "012_organization_directory.up.sql",
    "013_menu_permission_groups.up.sql", "014_multiple_menu_permission_groups.up.sql",
    "015_scoped_menu_administrators.up.sql", "016_organization_members.up.sql",
    "017_department_memberships.up.sql", "018_permission_management.up.sql",
}
BUSINESS_TABLES = (
    "app_user", "user_session", "token_usage", "token_budget",
    "directory_organization", "directory_person", "directory_account_link",
    "managed_model", "managed_model_price",
)


def snapshot(info: str) -> dict[str, list[dict[str, Any]]]:
    with psycopg.connect(info) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()}
        return {
            table: [row[0] for row in connection.execute(
                sql.SQL("SELECT to_jsonb(record) FROM {} record").format(sql.Identifier(table))
            ).fetchall()]
            for table in BUSINESS_TABLES
            if table in tables
        }


@pytest.mark.parametrize("installation", ["fresh", "fork", "upstream"])
def test_combined_migrations_preserve_fork_history_and_are_repeatable(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation: str,
) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    settings = Settings(database_url=local_postgres, migrations_dir=directory, production=False)
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    before: dict[str, list[dict[str, Any]]] = {}
    old_ledger: dict[str, tuple[Any, ...]] = {}
    if installation != "fresh":
        for path in runner.migration_files(MIGRATIONS):
            included = (
                path.name < "009_" or path.name in FORK_EXTENSIONS
                if installation == "fork" else path.name not in FORK_EXTENSIONS
            )
            if included:
                shutil.copy2(path, directory / path.name)
        assert len(runner.migrate()) == (18 if installation == "fork" else 28)
        seed_history(local_postgres)
        if installation == "fork":
            seed_directory_history(local_postgres)
        before = snapshot(local_postgres)
        old_ledger = {row[0]: row for row in ledger(local_postgres)}

    for path in runner.migration_files(MIGRATIONS):
        shutil.copy2(path, directory / path.name)
    applied = runner.migrate()
    assert len(applied) == {"fresh": 38, "fork": 20, "upstream": 10}[installation]
    complete = ledger(local_postgres)
    assert len(complete) == 38
    for row in complete:
        if row[0] in old_ledger:
            assert row == old_ledger[row[0]]
    after = snapshot(local_postgres)
    for table, rows in before.items():
        assert len(after[table]) == len(rows)
        for record in rows:
            assert any(all(saved[key] == value for key, value in record.items())
                       for saved in after[table]), table
    assert runner.migrate() == []
    assert ledger(local_postgres) == complete
    assert snapshot(local_postgres) == after
    repository = PostgreSqlOpsDbProxy(local_postgres)
    try:
        ModelRuntimeService(repository, settings).registry()
    finally:
        repository.close()


def seed_directory_history(local_postgres: str) -> None:
    with psycopg.connect(local_postgres) as connection:
        connection.execute(
            """INSERT INTO directory_organization(id,code,name,updated_by)
               VALUES ('preserved-org','preserved','Preserved organization','merge-test')"""
        )
        person = connection.execute(
            """INSERT INTO directory_person(governance_user_id,display_name,updated_by)
               VALUES ('preserved@example.com','Preserved Person','merge-test') RETURNING id"""
        ).fetchone()
        assert person is not None
        connection.execute(
            """INSERT INTO directory_account_link(person_id,app_user_id,verified_by)
               SELECT %s,id,'merge-test' FROM app_user WHERE email='preserved@example.com'""",
            (person[0],),
        )
