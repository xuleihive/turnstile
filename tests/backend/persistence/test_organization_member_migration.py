import shutil
from pathlib import Path

import psycopg
import pytest

from backend import migrate as runner
from tests.backend.persistence.test_user_settings_migration import ledger
from tests.backend.persistence.test_user_settings_migration import (
    local_postgres as local_postgres,
)
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings


def test_organization_backfill_preserves_history_and_retires_pending_work(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "016_organization_members.up.sql"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for file in runner.migration_files(REPOSITORY_ROOT / "migrations"):
        if file.name < target:
            shutil.copy2(file, migrations / file.name)
    monkeypatch.setattr(runner, "get_settings", lambda: Settings(
        database_url=local_postgres, migrations_dir=migrations,
    ))
    runner.migrate()
    with psycopg.connect(local_postgres) as db:
        db.execute(
            """INSERT INTO directory_organization(id,code,name,updated_by)
               VALUES ('org','org','Org','test');
               INSERT INTO directory_unit(id,organization_id,kind,code,name,updated_by)
               VALUES ('dept','org','department','dept','Department','test'),
                      ('other','org','department','other','Other','test');
               INSERT INTO directory_person(governance_user_id,display_name,updated_by)
               VALUES ('legacy@example.com','Legacy','test');
               INSERT INTO directory_membership(person_id,unit_id,organization_id,membership_kind)
               SELECT id,'dept','org','primary_department' FROM directory_person;
               INSERT INTO directory_transfer(
                 person_id,source_department_id,target_department_id,effective_month,
                 expected_person_revision,requested_by,idempotency_key)
               SELECT id,'dept','other','2027-01-01',revision,'test','test-key'
               FROM directory_person"""
        )
        before = db.execute(
            "SELECT id,governance_user_id,display_name,revision FROM directory_person"
        ).fetchall()
        memberships = db.execute("SELECT * FROM directory_membership").fetchall()
    old = ledger(local_postgres)
    shutil.copy2(REPOSITORY_ROOT / "migrations" / target, migrations / target)
    assert runner.migrate() == ["016_organization_members"]
    assert ledger(local_postgres)[:-1] == old
    assert runner.migrate() == []
    with psycopg.connect(local_postgres) as db:
        assert db.execute(
            "SELECT id,governance_user_id,display_name,revision FROM directory_person"
        ).fetchall() == before
        assert db.execute("SELECT * FROM directory_membership").fetchall() == memberships
        assert db.execute("SELECT organization_id FROM directory_person").fetchone() == ("org",)
        assert db.execute(
            "SELECT status,conflict_code FROM directory_transfer"
        ).fetchone() == ("cancelled", "workflow_retired")
