"""Run the published user-settings upgrade in an owned, local PostgreSQL cluster."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from backend import migrate as migration_runner
from backend.bootstrap import bootstrap_initial_owner
from backend.services.auth_service import hash_password, hash_session_token, verify_password
from tests.platform.api.api_support import _usage_record
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.persistence.auth_store import AuthStore
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

TARGET = "011_user_settings_profile.up.sql"
VERSION = TARGET.removesuffix(".up.sql")
MIGRATIONS = REPOSITORY_ROOT / "migrations"


@pytest.fixture
def local_postgres() -> Iterator[str]:
    initdb, postgres = shutil.which("initdb"), shutil.which("postgres")
    if initdb is None or postgres is None:
        pytest.skip("Local PostgreSQL 16+ initdb and postgres are required")
    version = subprocess.check_output([postgres, "--version"], text=True)
    major = re.search(r"\b(\d+)\.", version)
    if major is None or int(major[1]) < 16:
        pytest.skip("Migration validation requires PostgreSQL 16+")
    with TemporaryDirectory(prefix="ts-profile-") as temporary:
        directory = Path(temporary)
        data, sockets = directory / "data", directory / "sockets"
        sockets.mkdir(mode=0o700)
        subprocess.run(
            [initdb, "-D", str(data), "-U", "profile_test", "--encoding=UTF8",
             "--no-locale", "--auth-local=trust", "--auth-host=reject"],
            check=True, capture_output=True, text=True,
        )
        server = subprocess.Popen(
            [postgres, "-D", str(data), "-h", "", "-k", str(sockets), "-F"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        ready = Event()
        output: list[str] = []

        def read_output() -> None:
            assert server.stdout is not None
            for line in server.stdout:
                output.append(line)
                if "database system is ready to accept connections" in line:
                    ready.set()

        reader = Thread(target=read_output, daemon=True)
        reader.start()
        try:
            assert ready.wait(15), "".join(output[-20:])
            yield make_conninfo(
                host=str(sockets), dbname="postgres", user="profile_test",
                connect_timeout=5, sslmode="disable",
            )
        finally:
            if server.poll() is None:
                assert int((data / "postmaster.pid").read_text().splitlines()[0]) == server.pid
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            reader.join(timeout=5)
            assert server.stdout is not None
            server.stdout.close()


@pytest.fixture
def candidate(
    local_postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in migration_runner.migration_files(MIGRATIONS):
        if path.name < TARGET:
            shutil.copy2(path, directory / path.name)
    settings = Settings(database_url=local_postgres, migrations_dir=directory, production=False)
    monkeypatch.setattr(migration_runner, "get_settings", lambda: settings)
    return local_postgres, directory


def ledger(info: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(info) as connection:
        return connection.execute(
            "SELECT version, checksum, applied_at FROM schema_migration ORDER BY version"
        ).fetchall()


def historical_data(info: str) -> dict[str, list[tuple[Any, ...]]]:
    with psycopg.connect(info) as connection:
        return {
            table: connection.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
            ).fetchall()
            for table in ("app_user", "user_session", "token_budget", "token_usage")
        }


def seed_history(info: str) -> None:
    user_id = uuid4()
    with psycopg.connect(info) as connection:
        connection.execute(
            """INSERT INTO app_user(id, email, display_name, password_hash)
               VALUES (%s, 'preserved@example.com', 'Preserved', 'opaque-test-hash')""",
            (user_id,),
        )
        connection.execute(
            """INSERT INTO user_session(token_sha256, user_id, method, expires_at)
               VALUES ('preserved-session', %s, 'password', now() + interval '1 hour')""",
            (user_id,),
        )
        connection.execute(
            """INSERT INTO token_budget(period_start, scope_type, scope_id, token_limit, updated_by)
               VALUES ('2026-10-01', 'organization', 'org-contoso-global',
                       100000, 'migration-test')"""
        )
    repository = PostgreSqlOpsDbProxy(info)
    try:
        repository.write_token_usage(_usage_record("preserved-usage", "Migration test", 2345))
    finally:
        repository.close()


def test_existing_installation_upgrade_preserves_history_and_skips_rerun(
    candidate: tuple[str, Path],
) -> None:
    info, directory = candidate
    assert migration_runner.migrate()
    seed_history(info)
    before, old_ledger = historical_data(info), ledger(info)
    shutil.copy2(MIGRATIONS / TARGET, directory / TARGET)

    assert migration_runner.migrate() == [VERSION]
    assert historical_data(info) == before
    after = ledger(info)
    assert after[:-1] == old_ledger and after[-1][0] == VERSION
    assert migration_runner.migrate() == []
    assert ledger(info) == after and historical_data(info) == before

    # The runner must reject even a comment-only edit to an applied migration.
    path = directory / TARGET
    path.write_text(path.read_text() + "\n-- changed after apply\n")
    with pytest.raises(RuntimeError, match="Migration checksum changed after apply"):
        migration_runner.migrate()
    assert ledger(info) == after and historical_data(info) == before


def test_clean_installation_through_user_settings_migration_is_repeatable(
    candidate: tuple[str, Path],
) -> None:
    info, directory = candidate
    shutil.copy2(MIGRATIONS / TARGET, directory / TARGET)
    expected = [
        path.name.removesuffix(".up.sql") for path in migration_runner.migration_files(directory)
    ]

    assert migration_runner.migrate() == expected
    assert [row[0] for row in ledger(info)] == expected
    assert all(not rows for rows in historical_data(info).values())
    with psycopg.connect(info) as connection:
        assert connection.execute("SELECT count(*) FROM app_user_avatar").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM account_security_event").fetchone() == (0,)
    before = ledger(info)
    assert migration_runner.migrate() == [] and ledger(info) == before


def test_fresh_installation_bootstraps_owner_and_preserves_changed_password_on_restart(
    candidate: tuple[str, Path],
) -> None:
    info, directory = candidate
    shutil.copy2(MIGRATIONS / TARGET, directory / TARGET)
    assert migration_runner.migrate()[-1] == VERSION
    original_hash = hash_password("original-owner-password")
    settings = Settings(
        database_url=info, production=False, bootstrap_owner_email="OWNER@EXAMPLE.COM",
        bootstrap_owner_password_hash=SecretStr(original_hash),
    )
    store = AuthStore(info)
    try:
        assert bootstrap_initial_owner(store, settings)
        user = store.find_user_by_email("owner@example.com")
        assert user is not None and user["role"] == "owner" and user["enabled"]
        digest = hash_session_token("fresh-owner-test-session")
        now = datetime.now(UTC)
        assert store.create_session(
            user["id"], digest, "password", now, now + timedelta(hours=1),
            expected_password_hash=original_hash,
        )
        owner = store.session_owner(digest)
        assert owner is not None and owner["avatar_revision"] is None
        store.update_display_name(user["id"], digest, "Updated Owner")
        store.change_password(
            user["id"], digest, "original-owner-password", "changed-owner-password",
            verify_password, hash_password,
        )
        assert store.session_owner(digest) is None

        before = ledger(info)
        assert migration_runner.migrate() == []
        assert not bootstrap_initial_owner(store, settings)
        assert ledger(info) == before
        saved = store.find_user_by_email("owner@example.com")
        assert saved is not None and saved["display_name"] == "Updated Owner"
        assert saved["role"] == "owner"
        assert verify_password("changed-owner-password", saved["password_hash"])
        assert not verify_password("original-owner-password", saved["password_hash"])
        assert store.create_session(
            saved["id"], hash_session_token("new-owner-test-session"), "password",
            now, now + timedelta(hours=1), expected_password_hash=saved["password_hash"],
        )
    finally:
        store.close()


def test_failed_pending_upgrade_rolls_back_schema_and_migration_ledger(
    candidate: tuple[str, Path],
) -> None:
    info, directory = candidate
    assert migration_runner.migrate()
    seed_history(info)
    before, old_ledger = historical_data(info), ledger(info)
    (directory / TARGET).write_text((MIGRATIONS / TARGET).read_text() + "\nSELECT 1 / 0;\n")

    with pytest.raises(psycopg.errors.DivisionByZero):
        migration_runner.migrate()
    assert ledger(info) == old_ledger and historical_data(info) == before
    with psycopg.connect(info) as connection:
        assert connection.execute(
            "SELECT to_regclass('app_user_avatar'), to_regclass('account_security_event')"
        ).fetchone() == (None, None)
