from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import (
    local_postgres as local_postgres,
)
from tests.backend.persistence.test_user_settings_migration import historical_data, seed_history
from turnstile_core.config import core_runtime_digest
from turnstile_core.domain.directory import DirectoryError
from turnstile_core.persistence.directory_store import DirectoryStore
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.services import directory_upgrade
from turnstile_core.services.directory_upgrade import DirectoryUpgrade


def runtime_evidence(plan: dict[str, Any]) -> dict[str, Any]:
    core_digest = core_runtime_digest()
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "instance_digest": plan["instance_digest"],
        "protocol_version": 1,
        "backup_verified": True,
        "maintenance_confirmed": True,
        "applications": {
            target: {
                "target": target,
                "state": "Stopped",
                "core_digest": core_digest,
                "configured_source": "database",
                "configured_core_digest": core_digest,
                "package_sha256": "a" * 64,
            }
            for target in ("api", "telemetry", "control-plane")
        },
    }


def test_approved_upgrade_is_repeatable_and_preserves_billing(
    directory_database: str,
) -> None:
    seed_history(directory_database)
    before = historical_data(directory_database)
    store = DirectoryStore(directory_database)
    repository = PostgreSqlOpsDbProxy(directory_database)
    try:
        upgrade = DirectoryUpgrade(store, repository)
        plan = upgrade.plan()
        assert store.state()["phase"] == "schema_only"
        assert len(plan["people"]) == 1
        with pytest.raises(DirectoryError, match="Approve"):
            upgrade.apply(plan, approved=False, actor="test-operator")
        run_id = upgrade.apply(plan, approved=True, actor="test-operator")
        assert upgrade.apply(plan, approved=True, actor="test-operator") == run_id
        assert historical_data(directory_database) == before
        with pytest.raises(DirectoryError, match="Verify"):
            upgrade.activate(UUID(run_id))
        assert upgrade.verify(UUID(run_id))["verified"]
        with pytest.raises(DirectoryError, match="maintenance evidence"):
            upgrade.activate(UUID(run_id))
        upgrade.activate(UUID(run_id), runtime_manifest=runtime_evidence(plan))
        upgrade.activate(UUID(run_id))
        assert store.require_ready()["phase"] == "active"
        assert store.catalog().users[0].id == "test.user01@contoso.com"
        assert historical_data(directory_database) == before
        with pytest.raises(DirectoryError, match="reinitialized"):
            upgrade.plan()
    finally:
        repository.close()
        store.close()


def test_upgrade_interrupted_materialization_resumes_approved_staging(
    directory_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_history(directory_database)
    store = DirectoryStore(directory_database)
    repository = PostgreSqlOpsDbProxy(directory_database)
    try:
        upgrade = DirectoryUpgrade(store, repository)
        plan = upgrade.plan()
        baseline = directory_upgrade.billing_baseline

        def interrupted(connection: Any, tables: Any = directory_upgrade._PRESERVED_TABLES) -> Any:
            if tables == directory_upgrade._DIRECTORY_TABLES:
                raise RuntimeError("Simulated interrupted materialization")
            return baseline(connection, tables)

        monkeypatch.setattr(directory_upgrade, "billing_baseline", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            upgrade.apply(plan, approved=True, actor="test")
        with store.connection() as connection:
            organizations = connection.execute(
                "SELECT count(*) AS n FROM directory_organization",
            ).fetchone()
            staging = connection.execute(
                "SELECT count(*) AS n FROM directory_upgrade_stage"
            ).fetchone()
            assert organizations and organizations["n"] == 0
            assert staging and staging["n"] > 0
            run = connection.execute("SELECT * FROM directory_upgrade_run").fetchone()
            assert run and not run["materialized"]
        assert store.state()["source"] == "legacy"
        monkeypatch.setattr(directory_upgrade, "billing_baseline", baseline)
        run_id = upgrade.apply(plan, approved=True, actor="test")
        assert str(run["id"]) == run_id
        assert upgrade.verify(UUID(run_id))["verified"]
        evidence = runtime_evidence(plan)
        evidence["applications"]["telemetry"]["core_digest"] = "wrong"
        with pytest.raises(DirectoryError, match="matching stopped packages"):
            upgrade.activate(UUID(run_id), runtime_manifest=evidence)
        upgrade.activate(UUID(run_id), runtime_manifest=runtime_evidence(plan))
        assert store.require_ready()["phase"] == "active"
    finally:
        repository.close()
        store.close()


def test_upgrade_target_and_data_drift_stop_before_writes(
    directory_database: str,
) -> None:
    seed_history(directory_database)
    store = DirectoryStore(directory_database)
    repository = PostgreSqlOpsDbProxy(directory_database)
    try:
        upgrade = DirectoryUpgrade(store, repository)
        plan = upgrade.plan()
        with pytest.raises(DirectoryError, match="another database"):
            upgrade.apply({**plan, "instance_digest": "wrong"}, approved=True, actor="test")
        with store.connection() as connection:
            connection.execute(
                "UPDATE app_user SET display_name='Changed' WHERE email='preserved@example.com'"
            )
        with pytest.raises(DirectoryError, match="changed"):
            upgrade.apply(plan, approved=True, actor="test")
        assert store.state()["phase"] == "schema_only"
        with store.connection() as connection:
            row = connection.execute(
                "SELECT count(*) AS count FROM directory_organization"
            ).fetchone()
            assert row is not None and row["count"] == 0
    finally:
        repository.close()
        store.close()
