from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_image_options import _limits
from turnstile_core.config import Settings
from turnstile_core.domain.control_plane import ApiFormat, ModelCreateTarget, ModelTarget
from turnstile_core.domain.image_profiles import create_image_profile
from turnstile_core.domain.runtime_models import ManagedModelWrite
from turnstile_core.persistence.in_memory import InMemoryRepository


@pytest.mark.parametrize("legacy_value", [None, "false", "true"])
def test_image_capability_preserves_the_explicit_feature_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy_value: str | None,
) -> None:
    if legacy_value is None:
        monkeypatch.delenv("IMAGE_GENERATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("IMAGE_GENERATION_ENABLED", legacy_value)
    settings = Settings(credential_key_file=tmp_path / "key")
    assert settings.image_generation_enabled is (legacy_value == "true")
    assert settings.image_generation_defaults.output_reservation_tokens == 8192
    registry = ModelRuntimeService(InMemoryRepository(), settings).registry()
    assert registry.image_generation_supported is (legacy_value == "true")
    assert registry.image_configuration_schema_version == 4
    assert ApiFormat.OPENAI_IMAGES.value == "openai_images"


@pytest.mark.parametrize("extra", [{"context_window": 4096}, {"cache_write_cost_per_million": 0}])
def test_image_onboarding_rejects_text_only_settings(extra: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ModelCreateTarget.model_validate(
            {
                "operation": "image_generation",
                "deployment_name": "unit-image",
                **extra,
            }
        )


def test_text_cannot_attach_image_configuration() -> None:
    with pytest.raises(ValueError):
        ModelCreateTarget(deployment_name="text", image_configuration=_limits())


@pytest.mark.parametrize(
    "capabilities,is_default",
    [
        (["image_generation", "chat"], False),
        (["image_generation"], True),
    ],
)
def test_image_model_cannot_be_chat_default_or_mixed(
    capabilities: list[str], is_default: bool
) -> None:
    with pytest.raises(ValueError):
        ManagedModelWrite.model_validate(
            {
                "provider_id": "10000000-0000-4000-8000-000000000001",
                "runtime_id": "10000000-0000-4000-8000-000000000002",
                "model_key": "image",
                "display_name": "Image",
                "capabilities": capabilities,
                "is_default": is_default,
            }
        )


def test_image_snapshot_requires_image_only_capability() -> None:
    values = {
        "model_key": "image",
        "display_name": "Image",
        "upstream_model_id": "unit-image",
        "image_profile": create_image_profile(_limits(), "unit-image").model_dump(),
    }
    with pytest.raises(ValueError):
        ModelTarget.model_validate(values)
    assert ModelTarget.model_validate(
        {**values, "capabilities": ["image_generation"]}
    ).image_profile
