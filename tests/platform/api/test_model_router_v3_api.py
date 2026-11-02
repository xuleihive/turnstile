from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from tests.backend.model_platform.test_model_router_v3_service import (
    install_model_facts,
    v3_write,
)
from turnstile_core.config import Settings
from turnstile_core.persistence.in_memory import InMemoryRepository

client = TestClient(app)
OWNER_ID = UUID("00000000-0000-4000-8000-000000000101")


def identity(id_: UUID, email: str) -> SessionIdentity:
    return SessionIdentity(
        id=str(id_),
        email=email,
        name=email.split("@")[0],
        role="owner",
        method="password",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def v3_repository() -> Iterator[tuple[InMemoryRepository, dict[str, SessionIdentity]]]:
    repository = InMemoryRepository()
    current = {"identity": identity(OWNER_ID, "owner@contoso.com")}
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[require_authenticated_session] = lambda: current[
        "identity"
    ]
    with patch(
        "backend.http.service_dependencies.get_settings",
        return_value=Settings(model_router_v3_enabled=True),
    ):
        yield repository, current
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(require_authenticated_session, None)


def test_v3_specs_are_hidden_while_no_model_facts_or_runtime_exist() -> None:
    app.dependency_overrides[get_repository] = InMemoryRepository
    app.dependency_overrides[require_authenticated_session] = lambda: identity(
        OWNER_ID, "owner@contoso.com",
    )
    try:
        with patch("backend.http.service_dependencies.get_settings", return_value=Settings()):
            assert client.get("/api/v1/model-router-specs").status_code == 404
            assert client.post(
                f"/api/v1/model-router-specs/{OWNER_ID}/publish",
            ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_repository, None)
        app.dependency_overrides.pop(require_authenticated_session, None)


def test_v3_api_compiles_and_publishes_without_entering_v2_surface(
    v3_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = v3_repository
    model_ids = tuple(
        UUID(str(model["id"])) for model in repository.models if model["enabled"]
    )[:2]
    write = v3_write(model_ids)

    created = client.post(
        "/api/v1/model-router-specs",
        json=write.model_dump(mode="json"),
    )

    assert created.status_code == 201
    router_id = created.json()["id"]
    assert client.get("/api/v1/model-routers").json() == {"items": [], "model_references": []}
    assert [
        item["id"]
        for item in client.get("/api/v1/model-router-specs").json()["items"]
    ] == [router_id]
    invalid = client.post(f"/api/v1/model-router-specs/{router_id}/validate")
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False
    assert invalid.json()["compiled_plan"] is None

    install_model_facts(repository, model_ids)
    valid = client.post(f"/api/v1/model-router-specs/{router_id}/validate")
    published = client.post(f"/api/v1/model-router-specs/{router_id}/publish")

    assert valid.status_code == 200
    assert valid.json()["valid"] is True
    plan_hash = valid.json()["compiled_plan"]["plan_sha256"]
    assert published.status_code == 200
    assert published.json()["version"] == 1
    assert published.json()["compiled_plan"]["plan_sha256"] == plan_hash
    assert client.get(f"/api/v1/model-router-specs/{router_id}").json()[
        "compiled_plan"
    ]["plan_sha256"] == plan_hash


def test_v3_api_enforces_owner_scope_alias_immutability_and_lifecycle(
    v3_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, current = v3_repository
    model_ids = tuple(
        UUID(str(model["id"])) for model in repository.models if model["enabled"]
    )[:2]
    install_model_facts(repository, model_ids)
    created = client.post(
        "/api/v1/model-router-specs",
        json=v3_write(model_ids).model_dump(mode="json"),
    )
    router_id = created.json()["id"]
    assert client.post(
        f"/api/v1/model-router-specs/{router_id}/publish"
    ).status_code == 200

    renamed_alias = client.put(
        f"/api/v1/model-router-specs/{router_id}",
        json=v3_write(model_ids, alias="changed-router").model_dump(mode="json"),
    )
    assert renamed_alias.status_code == 409

    current["identity"] = identity(
        UUID("00000000-0000-4000-8000-000000000999"),
        "other@contoso.com",
    )
    assert client.get(f"/api/v1/model-router-specs/{router_id}").status_code == 404
    current["identity"] = identity(OWNER_ID, "owner@contoso.com")

    assert client.post(
        f"/api/v1/model-router-specs/{router_id}/disable"
    ).json()["status"] == "disabled"
    assert client.delete(f"/api/v1/model-router-specs/{router_id}").status_code == 204
    assert client.get(f"/api/v1/model-router-specs/{router_id}").status_code == 404