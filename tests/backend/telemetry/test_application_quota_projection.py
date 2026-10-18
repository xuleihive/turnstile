from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from tests.backend.model_platform.test_application_access import _discovery
from tests.backend.telemetry.test_ledger import FakeLedgerStore
from turnstile_core.domain.application_access import (
    GatewayApplicationBudgetProjection,
    GatewayApplicationBudgetUpdate,
)
from turnstile_core.integrations.ledger import LedgerSyncService, TableStorageLedger
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService


class StorageToken:
    def token(self, resource: str) -> str:
        assert resource == "https://storage.azure.com/"
        return "token"


def test_budget_save_projects_immediately_after_database_commit() -> None:
    repository = InMemoryRepository()
    store = FakeLedgerStore()
    calls = []

    def project(application_id: Any) -> None:
        calls.append(application_id)
        LedgerSyncService(repository, store).project_application_budget(application_id)

    service = ApplicationAccessService(
        repository, sync_available=True, budget_projector=project,
        budget_projection_reader=lambda _, budget: GatewayApplicationBudgetProjection(
            status="projected", tokens_per_minute=budget.tokens_per_minute,
        ),
    )
    app = service.sync_discovery(_discovery("client"), "test")[0]
    detail = service.update_application_budget(UUID(str(app["id"])), GatewayApplicationBudgetUpdate(
        token_limit=100_000_000, tokens_per_minute=10_000_000, enforce=True,
        warning_threshold_percent=80,
    ), "test")
    assert calls == [app["id"]]
    assert detail.budget_projection and detail.budget_projection.status == "projected"
    assert len(store.entities) == 1
    quota = next(iter(store.entities.values()))
    assert quota["TokensPerMinute"] == 10_000_000
    assert quota["Limit"] == 100_000_000
    assert quota["Enforce"] is True
    assert "ConfigurationUpdatedAt" in quota


def test_projection_failure_does_not_report_committed_save_as_failed() -> None:
    repository = InMemoryRepository()

    def fail(_: Any) -> None:
        raise RuntimeError("unavailable")

    service = ApplicationAccessService(repository, sync_available=True, budget_projector=fail)
    app = service.sync_discovery(_discovery("client"), "test")[0]
    result = service.update_application_budget(UUID(str(app["id"])), GatewayApplicationBudgetUpdate(
        token_limit=1000, tokens_per_minute=900, enforce=False, warning_threshold_percent=80,
    ), "test")
    assert result.budget and result.budget.tokens_per_minute == 900
    assert result.budget_projection and result.budget_projection.status == "unavailable"


@pytest.mark.parametrize("incoming", ["2026-10-11T00:00:00.000000+00:00", ""])
def test_old_or_unversioned_quota_cannot_overwrite_newer_snapshot(incoming: str) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, headers={"ETag": 'W/"latest"'}, json={
            "ConfigurationUpdatedAt": "2026-10-11T00:00:01.000000+00:00",
            "TokensPerMinute": 10_000_000,
        })

    store = TableStorageLedger("https://storage.example.com", "Ledger", StorageToken(),
                              httpx.Client(transport=httpx.MockTransport(handle)))
    store.upsert("app|id|2026-10", "Q", {
        "ConfigurationUpdatedAt": incoming, "TokensPerMinute": 100_000,
    })
    assert calls == ["GET"]


def test_etag_conflict_rereads_and_keeps_newer_configuration() -> None:
    count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, headers={"ETag": 'W/"old"'}, json={
                "ConfigurationUpdatedAt": "2026-10-11T00:00:00.000000+00:00",
            })
        if count == 2:
            assert request.headers["If-Match"] == 'W/"old"'
            return httpx.Response(412)
        assert request.method == "GET"
        return httpx.Response(200, headers={"ETag": 'W/"new"'}, json={
            "ConfigurationUpdatedAt": "2026-10-11T00:00:02.000000+00:00",
        })

    store = TableStorageLedger("https://storage.example.com", "Ledger", StorageToken(),
                              httpx.Client(transport=httpx.MockTransport(handle)))
    store.upsert("app|id|2026-10", "Q", {
        "ConfigurationUpdatedAt": "2026-10-11T00:00:01.000000+00:00",
    })
    assert count == 3


def test_insertion_conflict_does_not_unconditionally_replace_quota() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(404)
        if request.method == "POST":
            assert json.loads(request.content)["RowKey"] == "Q"
            return httpx.Response(409)
        return httpx.Response(200, headers={"ETag": 'W/"new"'}, json={
            "ConfigurationUpdatedAt": "2026-10-11T00:00:02.000000+00:00",
        })

    store = TableStorageLedger("https://storage.example.com", "Ledger", StorageToken(),
                              httpx.Client(transport=httpx.MockTransport(handle)))
    store.upsert("app|id|2026-10", "Q", {
        "ConfigurationUpdatedAt": "2026-10-11T00:00:01.000000+00:00",
    })
    assert calls == ["GET", "POST", "GET"]
