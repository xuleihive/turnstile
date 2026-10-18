from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from backend import migrate as migration_runner
from tests.backend.persistence.test_user_settings_migration import (
    historical_data,
    ledger,
    seed_history,
)
from tests.backend.persistence.test_user_settings_migration import local_postgres as local_postgres
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings

TARGET = "013_menu_permission_groups.up.sql"


def test_upgrade_preserves_history_titles_and_grants_and_reruns_without_changes(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in migration_runner.migration_files(REPOSITORY_ROOT / "migrations"):
        if path.name < TARGET:
            shutil.copy2(path, migrations / path.name)
    settings = Settings(database_url=local_postgres, migrations_dir=migrations)
    monkeypatch.setattr(migration_runner, "get_settings", lambda: settings)
    migration_runner.migrate()
    seed_history(local_postgres)
    with psycopg.connect(local_postgres) as connection:
        connection.execute(
            """INSERT INTO directory_organization(id,code,name,updated_by)
               VALUES ('org-a','a','A','test');
               INSERT INTO directory_unit(id,organization_id,kind,code,name,updated_by)
               VALUES ('department-a','org-a','department','a','A','test')"""
        )
        connection.execute(
            """INSERT INTO directory_person(governance_user_id,display_name,job_title,updated_by)
               VALUES ('title@example.com','Original','Organization Administrator','test')"""
        )
        connection.execute(
            """INSERT INTO directory_department_grant(app_user_id,department_id,granted_by)
               SELECT id,'department-a','test' FROM app_user"""
        )
        grants = connection.execute("SELECT * FROM directory_department_grant").fetchall()
    before_history, before_ledger = historical_data(local_postgres), ledger(local_postgres)
    shutil.copy2(REPOSITORY_ROOT / "migrations" / TARGET, migrations / TARGET)
    assert migration_runner.migrate() == ["013_menu_permission_groups"]
    assert historical_data(local_postgres) == before_history
    assert ledger(local_postgres)[:-1] == before_ledger
    with psycopg.connect(local_postgres) as connection:
        assert connection.execute("SELECT * FROM directory_department_grant").fetchall() == grants
        assert connection.execute(
            "SELECT job_title,menu_permission_group FROM directory_person"
        ).fetchall() == [("Organization Administrator", "user")]
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute("UPDATE directory_person SET menu_permission_group='owner'")
    assert migration_runner.migrate() == []
    assert historical_data(local_postgres) == before_history
    assert ledger(local_postgres)[:-1] == before_ledger


def test_single_group_upgrade_preserves_assignment_as_an_array(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "single-group"
    migrations.mkdir()
    target = "014_multiple_menu_permission_groups.up.sql"
    for path in migration_runner.migration_files(REPOSITORY_ROOT / "migrations"):
        if path.name < target:
            shutil.copy2(path, migrations / path.name)
    settings = Settings(database_url=local_postgres, migrations_dir=migrations)
    monkeypatch.setattr(migration_runner, "get_settings", lambda: settings)
    migration_runner.migrate()
    with psycopg.connect(local_postgres) as connection:
        connection.execute(
            """INSERT INTO directory_person(
                 governance_user_id,display_name,menu_permission_group,updated_by)
               VALUES ('admin@example.com','Admin','department_admin','test'),
                      ('user@example.com','User','user','test')"""
        )
    previous = ledger(local_postgres)
    shutil.copy2(REPOSITORY_ROOT / "migrations" / target, migrations / target)
    assert migration_runner.migrate() == ["014_multiple_menu_permission_groups"]
    assert ledger(local_postgres)[:-1] == previous
    with psycopg.connect(local_postgres) as connection:
        assert connection.execute(
            """SELECT menu_permission_groups FROM directory_person
               ORDER BY governance_user_id"""
        ).fetchall() == [(["department_admin"],), (["user"],)]
    assert migration_runner.migrate() == []
