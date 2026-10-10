from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from backend import migrate as migration_runner
from tests.backend.persistence.test_user_settings_migration import (
    historical_data,
    ledger,
    seed_history,
)
from tests.backend.persistence.test_user_settings_migration import (
    local_postgres as local_postgres,
)
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.persistence.directory_store import DirectoryStore

TARGET = "012_organization_directory.up.sql"


@pytest.fixture
def directory_database(
    local_postgres: str,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setattr(
        migration_runner,
        "get_settings",
        lambda: Settings(database_url=local_postgres),
    )
    migration_runner.migrate()
    return local_postgres


def test_empty_installation_and_concurrent_migration_reruns(
    directory_database: str,
) -> None:
    before = ledger(directory_database)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: migration_runner.migrate(), range(2))) == [[], []]
    assert ledger(directory_database) == before
    with psycopg.connect(directory_database) as connection:
        for table in ("directory_organization", "directory_unit", "directory_person"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    store = DirectoryStore(directory_database)
    try:
        store.initialize_empty()
        state = store.require_ready()
        assert state["phase"] == "fresh_empty_ready"
        assert store.catalog().organizations == []
        store.initialize_empty()
        assert store.state()["active_version"] == state["active_version"]
    finally:
        store.close()


def test_upgrade_keeps_history_and_migration_checksums(
    local_postgres: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for file in migration_runner.migration_files(REPOSITORY_ROOT / "migrations"):
        if file.name < TARGET:
            shutil.copy2(file, migrations / file.name)
    settings = Settings(database_url=local_postgres, migrations_dir=migrations)
    monkeypatch.setattr(migration_runner, "get_settings", lambda: settings)
    migration_runner.migrate()
    seed_history(local_postgres)
    old_data, old_ledger = historical_data(local_postgres), ledger(local_postgres)
    shutil.copy2(REPOSITORY_ROOT / "migrations" / TARGET, migrations / TARGET)
    assert migration_runner.migrate() == ["012_organization_directory"]
    assert ledger(local_postgres)[:-1] == old_ledger
    assert historical_data(local_postgres) == old_data
    store = DirectoryStore(local_postgres)
    try:
        with pytest.raises(ValueError, match="approved directory upgrade"):
            store.initialize_empty()
        assert store.state()["phase"] == "schema_only"
    finally:
        store.close()


def test_database_rejects_invalid_hierarchy_and_overlapping_primary_memberships(
    directory_database: str,
) -> None:
    person = uuid4()
    with psycopg.connect(directory_database) as connection:
        connection.execute(
            """INSERT INTO directory_organization(id, code, name, updated_by)
               VALUES ('org-a', 'a', 'A', 'test')"""
        )
        connection.execute(
            """INSERT INTO directory_unit(id, organization_id, kind, code, name, updated_by)
               VALUES ('department-a', 'org-a', 'department', 'a', 'A', 'test'),
                      ('department-b', 'org-a', 'department', 'b', 'B', 'test')"""
        )
        connection.execute(
            """INSERT INTO directory_person(id, governance_user_id, display_name, updated_by)
               VALUES (%s, 'person@example.com', 'Person', 'test')""",
            (person,),
        )
        connection.execute(
            """INSERT INTO directory_membership(
                 person_id, unit_id, organization_id, membership_kind)
               VALUES (%s, 'department-a', 'org-a', 'primary_department')""",
            (person,),
        )
    with psycopg.connect(directory_database) as connection:
        with (
            pytest.raises(psycopg.errors.CheckViolation, match="overlap"),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO directory_membership(
                     person_id, unit_id, organization_id, membership_kind)
                   VALUES (%s, 'department-b', 'org-a', 'primary_department')""",
                (person,),
            )
        with (
            pytest.raises(psycopg.errors.CheckViolation, match="identity"),
            connection.transaction(),
        ):
            connection.execute(
                """UPDATE directory_person SET governance_user_id = 'other@example.com'
                   WHERE id=%s""",
                (person,),
            )
