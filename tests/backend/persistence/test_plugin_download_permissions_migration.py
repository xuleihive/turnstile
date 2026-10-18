import shutil
from pathlib import Path

import psycopg
import pytest

from backend import migrate as runner
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
from turnstile_core.domain.menu_permissions import DOWNLOAD_GROUPS


def test_020_preserves_menu_policy_history_and_old_checksums_and_reruns(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "migrations"
    folder.mkdir()
    target = "020_plugin_download_permissions.up.sql"
    for path in runner.migration_files(REPOSITORY_ROOT / "migrations"):
        if path.name < target:
            shutil.copy2(path, folder / path.name)
    monkeypatch.setattr(runner, "get_settings", lambda: Settings(
        database_url=local_postgres, migrations_dir=folder,
    ))
    runner.migrate()
    seed_history(local_postgres)
    before, versions = historical_data(local_postgres), ledger(local_postgres)
    with psycopg.connect(local_postgres) as db:
        policy = db.execute("SELECT revision,groups,updated_at FROM permission_policy").fetchone()
    shutil.copy2(REPOSITORY_ROOT / "migrations" / target, folder / target)
    assert runner.migrate() == ["020_plugin_download_permissions"]
    assert runner.migrate() == []
    assert historical_data(local_postgres) == before
    assert ledger(local_postgres)[:-1] == versions
    with psycopg.connect(local_postgres) as db:
        assert db.execute(
            "SELECT revision,groups,updated_at FROM permission_policy"
        ).fetchone() == policy
        row = db.execute("SELECT download_groups FROM permission_policy").fetchone()
        assert row is not None
        assert row[0] == {group: list(values) for group, values in DOWNLOAD_GROUPS.items()}
