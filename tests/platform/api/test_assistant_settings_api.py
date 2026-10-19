from __future__ import annotations

from backend.api import app
from backend.http.dependencies import get_repository
from tests.platform.api.api_support import MEMBER_SESSION, client
from turnstile_core.persistence.in_memory import InMemoryRepository

pytest_plugins = ("tests.platform.api.api_fixtures",)


def test_assistant_api_protocol_is_owner_only_and_read_back_after_save() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    settings = client.get("/api/v1/assistant/settings").json()
    picked = settings["available_models"][0]
    body = {
        "model_id": picked["id"], "auto_title": False, "api_format": "openai_responses",
    }
    saved = client.put("/api/v1/assistant/settings", json=body)
    assert saved.status_code == 200
    assert saved.json()["effective_api_format"] == "openai_responses"
    assert saved.json()["effective_api_path"] == "/responses"
    assert client.get("/api/v1/assistant/settings").json()["api_format"] == "openai_responses"

    client.cookies.set("turnstile_session", MEMBER_SESSION)
    denied = client.put("/api/v1/assistant/settings", json={**body, "api_format": None})
    assert denied.status_code == 403
    assert repository.assistant_settings()["api_format"] == "openai_responses"


def test_assistant_api_does_not_accept_an_arbitrary_or_incompatible_protocol() -> None:
    repository = InMemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    picked = client.get("/api/v1/assistant/settings").json()["available_models"][0]
    body = {"model_id": picked["id"], "auto_title": True}
    malformed = client.put(
        "/api/v1/assistant/settings", json={**body, "api_format": "https://other-provider.test"},
    )
    assert malformed.status_code == 422
    incompatible = client.put(
        "/api/v1/assistant/settings", json={**body, "api_format": "anthropic_messages"},
    )
    assert incompatible.status_code == 409
    assert repository.assistant_settings()["model_id"] is None
