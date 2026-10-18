from __future__ import annotations

from collections.abc import Iterator

import pytest

from backend.api import app
from backend.http.dependencies import get_repository
from backend.http.service_dependencies import runtime_service
from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_image_service import ImageRouter, image_fixture
from tests.platform.api.api_support import MEMBER_SESSION, client
from turnstile_core.config import Settings
from turnstile_core.domain.images import ImageInvocationRequest
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)
ImageFixture = tuple[InMemoryRepository, ImageInvocationRequest, ImageRouter]


@pytest.fixture
def images() -> Iterator[ImageFixture]:
    repository, request = image_fixture()
    router = ImageRouter()
    service = ModelRuntimeService(repository, Settings(image_generation_enabled=True), router)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[runtime_service] = lambda: service
    yield repository, request, router
    app.dependency_overrides.pop(runtime_service, None)
    app.dependency_overrides.pop(get_repository, None)


def test_image_api_uses_session_and_does_not_cache_response(images: ImageFixture) -> None:
    repository, request, router = images
    response = client.post(
        "/api/v1/model-gateway/images/generations", json=request.model_dump(mode="json")
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["usage"]["output_tokens"] == 20
    assert len(repository.billable_requests) == 1 and len(router.calls) == 1


@pytest.mark.parametrize("kind,expected", [("anonymous", 401), ("origin", 403), ("identity", 403)])
def test_image_api_protection_cannot_be_bypassed(
    images: ImageFixture,
    kind: str,
    expected: int,
) -> None:
    repository, request, router = images
    headers = {}
    if kind == "anonymous":
        client.cookies.clear()
    elif kind == "origin":
        headers["Origin"] = "https://untrusted.example"
    else:
        client.cookies.set("turnstile_session", MEMBER_SESSION)
    response = client.post(
        "/api/v1/model-gateway/images/generations",
        json=request.model_dump(mode="json"),
        headers=headers,
    )
    assert response.status_code == expected
    assert not router.calls and not repository.billable_requests


def test_image_api_rejects_non_apim_metadata_and_multiple_images(images: ImageFixture) -> None:
    repository, request, router = images
    for changes in (
        {"n": 2},
        {"stream": True},
        {"metadata": {**request.metadata.model_dump(), "usage_domain": "github_copilot"}},
    ):
        response = client.post(
            "/api/v1/model-gateway/images/generations",
            json={**request.model_dump(mode="json"), **changes},
        )
        assert response.status_code == 422
    assert not router.calls and not repository.billable_requests
