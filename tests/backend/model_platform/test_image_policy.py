from __future__ import annotations

from xml.etree import ElementTree

import pytest

from tests.backend.model_platform.test_image_options import _limits
from tests.backend.model_platform.test_image_publication import image_publication
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.domain.image_profiles import create_image_profile
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_policy_compiler import ApimPolicyCompiler
from turnstile_core.integrations.apim_policy_components import (
    compose_image_parent_policy,
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.control_plane import GatewayControlPlaneService


def test_image_operation_has_budget_marker_and_bypasses_text_observer() -> None:
    repository = InMemoryRepository()
    publication = GatewayControlPlaneService(
        repository,
        apim_principal_id="unit-principal",
    image_generation_enabled=True).publish(image_publication(repository), "owner@example.com")
    compiled = ApimPolicyCompiler(
        usage_observer_url="https://observer.example",
        usage_observer_key_named_value="adapter-key",
    ).compile(publication)
    assert compiled.images_generations_policy is not None
    root = ElementTree.fromstring(compiled.images_generations_policy)
    assert compiled.operations[0].id == "images-generations"
    assert compiled.operations[0].path == "/images/generations"
    assert "imageGenerationPolicyVersion" in compiled.images_generations_policy
    assert "imageOutputBound" in compiled.images_generations_policy
    assert "api-version=preview" in compiled.images_generations_policy
    forward = root.find("./backend/forward-request")
    assert forward is not None and forward.get("timeout") == "180"
    assert all("observer.example" not in backend.url for backend in compiled.backends)
    assert "image-deployment-foundry" not in compiled.chat_completions_policy
    assert "x-adapter-key" not in compiled.images_generations_policy
    assert "max_completion_tokens" not in compiled.images_generations_policy


def image_parent() -> tuple[str, str]:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    profile = create_image_profile(_limits(), "unit-image")
    return source, compose_image_parent_policy(source, source, [profile], previous_profiles=())


def test_public_parent_composition_preserves_text_and_has_full_image_budget() -> None:
    _source, parent = image_parent()
    profile = create_image_profile(_limits(), "unit-image")
    assert validate_parent_policy(parent, parent, [profile]) == parent
    root = parse_policy(parent)
    assert root.find("./inbound/set-variable[@name='isImagesOperation']") is not None
    assert len(root.findall(".//set-variable[@name='maxOutputBound']")) == 2
    assert len(root.findall(".//set-variable[@name='applicationMaxOutputBound']")) == 2
    assert "input_tokens_details" in parent and "image_tokens" in parent


@pytest.mark.parametrize(
    "component", ["ledgerState", "applicationLedgerState", "isImagesOperation", "usagePayload"]
)
def test_changed_parent_component_cannot_pass_on_marker_alone(component: str) -> None:
    _source, parent = image_parent()
    changed = parse_policy(parent)
    variable = changed.find(f".//set-variable[@name='{component}']")
    assert variable is not None
    variable.set("value", "@(false)")
    with pytest.raises(PolicyCompilationError):
        validate_parent_policy(
            serialize_policy(changed), parent, [create_image_profile(_limits(), "unit-image")]
        )


def test_xml_roundtrip_preserves_expression_line_comments_and_escaping() -> None:
    source = (
        '<policies><inbound><set-variable name="unit" value="@{\n'
        '// Keep newline\nreturn &quot;a&lt;b&quot;;\n}" /></inbound></policies>'
    )
    first = parse_policy(source)
    second = parse_policy(serialize_policy(first))
    original_variable = first.find("./inbound/set-variable")
    parsed_variable = second.find("./inbound/set-variable")
    assert original_variable is not None and parsed_variable is not None
    assert original_variable.get("value") == parsed_variable.get("value")
