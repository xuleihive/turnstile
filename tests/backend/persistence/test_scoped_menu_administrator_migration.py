from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from backend import migrate as migration_runner
from tests.backend.persistence.test_user_settings_migration import ledger
from tests.backend.persistence.test_user_settings_migration import local_postgres as local_postgres
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.persistence.directory_store import DirectoryStore
from turnstile_core.services.directory_upgrade import billing_baseline


def test_legacy_department_appointment_is_recovered_without_inventing_team_or_owner(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg.rows import dict_row

    target = "015_scoped_menu_administrators.up.sql"
    migrations = tmp_path / "before"
    migrations.mkdir()
    for file in migration_runner.migration_files(REPOSITORY_ROOT / "migrations"):
        if file.name < target:
            shutil.copy2(file, migrations / file.name)
    settings = Settings(database_url=local_postgres, migrations_dir=migrations)
    monkeypatch.setattr(migration_runner, "get_settings", lambda: settings)
    migration_runner.migrate()
    store = DirectoryStore(local_postgres)
    store.initialize_empty()
    with psycopg.connect(local_postgres, row_factory=dict_row) as db:
        db.execute(
            """INSERT INTO directory_organization(id,code,name,updated_by)
               VALUES ('org-a','a','A','test');
               INSERT INTO directory_unit(
                 id,organization_id,kind,code,name,parent_unit_id,updated_by)
               VALUES ('dept-a','org-a','department','a','A',NULL,'test'),
                      ('team-a','org-a','team','team','Team','dept-a','test')"""
        )
        # Duplicate historical grants are legal in 012; import must deduplicate them.
        account = db.execute(
            "INSERT INTO app_user(email,role) VALUES ('legacy@example.com','owner') RETURNING id"
        ).fetchone()
        person = db.execute(
            """INSERT INTO directory_person(governance_user_id,display_name,updated_by)
               VALUES ('legacy@example.com','Legacy','test') RETURNING id"""
        ).fetchone()
        assert account and person
        db.execute(
            """INSERT INTO directory_account_link(app_user_id,person_id,verified_by)
               VALUES (%s,%s,'test')""",
            (account["id"], person["id"]),
        )
        for unit, kind in (("dept-a", "primary_department"), ("team-a", "team")):
            db.execute(
                """INSERT INTO directory_membership(
                     person_id,unit_id,organization_id,membership_kind)
                   VALUES (%s,%s,'org-a',%s)""",
                (person["id"], unit, kind),
            )
        for _ in range(2):
            db.execute(
                """INSERT INTO directory_department_grant(app_user_id,department_id,granted_by)
                   VALUES (%s,'dept-a','test')""",
                (account["id"],),
            )
        preserved_tables = (
            "app_user", "directory_person", "directory_membership",
            "directory_department_grant", "token_usage", "token_budget", "user_model_access",
        )
        before = billing_baseline(db, preserved_tables)
    old_ledger = ledger(local_postgres)
    shutil.copy2(REPOSITORY_ROOT / "migrations" / target, migrations / target)
    try:
        assert migration_runner.migrate() == ["015_scoped_menu_administrators"]
        assert ledger(local_postgres)[:-1] == old_ledger
        with psycopg.connect(local_postgres, row_factory=dict_row) as db:
            assert billing_baseline(db, preserved_tables) == before
            assert db.execute(
                "SELECT scope_kind,unit_id FROM directory_menu_administrator"
            ).fetchall() == [{"scope_kind": "department", "unit_id": "dept-a"}]
        assert migration_runner.migrate() == []
        shutil.copy2(
            REPOSITORY_ROOT / "migrations" / "016_organization_members.up.sql",
            migrations / "016_organization_members.up.sql",
        )
        migration_runner.migrate()
        principal = store.principal(account["id"], "legacy@example.com", "owner")
        assert principal.menu_permission_groups == ("department_admin",)
        assert principal.owner
        assert migration_runner.migrate() == []
    finally:
        store.close()
