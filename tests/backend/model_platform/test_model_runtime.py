from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from inspect import signature
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import HttpUrl, SecretStr

from backend.services.runtime_service import ModelRuntimeService
from tests.backend.model_platform.test_gateway_targets import deployment, target
from turnstile_core.config import Settings
from turnstile_core.domain.control_plane import (
    ApiFormat,
    GatewayCredentialRotation,
    GatewayPublicationRetry,
    GatewayReleaseDependencies,
    ModelCreateTarget,
    RuntimeTarget,
)
from turnstile_core.domain.gateway_targets import (
    SelfHostedGatewayCreate,
    SelfHostedGatewayProvisionSpec,
    gateway_deployment_id_for,
    gateway_provision_digest,
    self_hosted_gateway_native_id_for,
)
from turnstile_core.domain.models import TokenUsageRecord
from turnstile_core.domain.runtime_models import (
    AuthType,
    BrandKey,
    ChatMessage,
    DecisionInvocationRequest,
    DecisionRequest,
    GatewayKind,
    GatewayProfileWrite,
    InvocationMetadata,
    ManagedModelWrite,
    ModelConnectionCreate,
    ModelFamilyKey,
    ModelInvocationRequest,
    ModelVendorKey,
    OAuthClientCredentialsConfig,
    PriceSource,
    ProviderKind,
    ProviderTarget,
    ProviderWrite,
    RuntimeKind,
    RuntimeWrite,
    databricks_connection_config,
    databricks_runtime_name,
    databricks_workspace_url,
    openai_compatible_endpoint_values,
    openai_compatible_runtime_name,
)
from turnstile_core.integrations.gateway import (
    AnthropicMessagesGatewayAdapter,
    CliGatewayAdapter,
    GatewayInvocationError,
    GatewayRouter,
    MicrosoftDecisionGatewayAdapter,
    OpenAICompatibleGatewayAdapter,
    _decision_usage,
    _openai_usage,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.pricing.catalog import (
    CatalogEntry,
    CatalogModel,
    CatalogOption,
    CatalogOptions,
    CompositeCatalog,
)
from turnstile_core.security import CredentialCipher


def test_native_decision_contract_retains_json_state_and_three_question_kinds() -> None:
    payload: dict[str, Any] = {
        "model": "unit-decision", "state": {"service": "checkout", "status": 500},
        "questions": {
            "urgent": {"type": "noul", "instructions": "Is this urgent?"},
            "team": {"type": "choice", "instructions": "Choose a team", "criteria": {
                "billing": "Charges", "engineering": "Errors",
            }},
            "severity": {"type": "score", "instructions": "Rate severity",
                         "criteria": ["Minor", "Critical"]},
        },
    }
    decision = DecisionRequest.model_validate(payload)
    assert decision.model_dump(mode="json") == payload
    assert ApiFormat.MICROSOFT_DECISION.value == "microsoft_decision"
    for field, value in [("messages", []), ("stream", True), ("max_output_tokens", 64)]:
        with pytest.raises(ValueError):
            DecisionRequest.model_validate({**payload, field: value})
    selected = DecisionInvocationRequest(
        model_id=UUID(int=901), metadata=request().metadata, state=payload["state"],
        questions=decision.questions,
    )
    assert "router_id" not in selected.model_dump()
    with pytest.raises(ValueError, match="selected together"):
        DecisionInvocationRequest.model_validate({
            **selected.model_dump(), "gateway_profile_id": UUID(int=903),
        })


@pytest.mark.parametrize("change", [
    {"is_default": True}, {"capabilities": ["decision", "chat"]},
    {"capabilities": ["decision", "streaming"]},
])
def test_decision_model_cannot_be_chat_default_or_advertise_chat_fields(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        ManagedModelWrite.model_validate({
            "provider_id": UUID(int=1), "runtime_id": UUID(int=2),
            "model_key": "unit-decision", "display_name": "Decision",
            "capabilities": ["decision"], **change,
        })


def test_native_decision_management_requires_publication_and_preserves_operation() -> None:
    repository = InMemoryRepository()
    service = ModelRuntimeService(repository, Settings())
    assert service.registry().decision_supported is True
    write = ManagedModelWrite(provider_id=UUID(int=1), runtime_id=UUID(int=2),
                              model_key="unit-decision", display_name="Unit Decision",
                              capabilities=["decision"])
    with pytest.raises(HTTPException, match="Publish Decision") as created:
        service.save_model(write)
    assert created.value.status_code == 409
    existing = repository.models[0]
    with pytest.raises(HTTPException, match="Changing model operation") as changed:
        service.save_model(write.model_copy(update={
            "provider_id": existing["provider_id"], "runtime_id": existing["runtime_id"],
        }), existing["id"])
    assert changed.value.status_code == 409


def test_native_decision_publication_requires_existing_deployment_and_supported_prices() -> None:
    target = ModelCreateTarget(operation="decision", deployment_name="unit-decision")
    assert target.operation == "decision"
    with pytest.raises(ValueError, match="existing Foundry deployment"):
        ModelCreateTarget(operation="decision", model_key="unit", display_name="Unit",
                          upstream_model_id="unit")
    with pytest.raises(ValueError, match="manual or Azure"):
        ModelCreateTarget(operation="decision", deployment_name="unit-decision",
                          price_source=PriceSource.ANTHROPIC,
                          price_reference="anthropic:unit")


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 93, "output_tokens": 3}, (93, 0, 0, 3)),
    ({"input_tokens": 100, "output_tokens": 3, "total_tokens": 103,
      "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 5}}, (75, 25, 5, 3)),
    ({"input_tokens": 93, "output_tokens": 3, "total_tokens": 95}, None),
    ({"input_tokens": 93, "output_tokens": True}, None),
    ({"input_tokens": 93}, None), (None, None),
])
def test_native_decision_usage_handles_missing_total_without_guessing(
    usage: Any, expected: tuple[int, int, int, int] | None,
) -> None:
    actual = _decision_usage(usage)
    if expected is None:
        assert actual is None
    else:
        assert actual is not None and actual.estimated is False
        assert (actual.input_tokens, actual.cached_tokens,
                actual.cache_write_tokens, actual.output_tokens) == expected


@pytest.mark.parametrize("complete", [True, False])
def test_native_decision_http_sends_only_alias_state_questions_and_preserves_answers(
    complete: bool,
) -> None:
    calls: list[httpx.Request] = []
    questions = {"urgent": {"type": "noul", "instructions": "Is it urgent?"}}
    decision = DecisionInvocationRequest.model_validate({
        "metadata": request().metadata.model_dump(), "model_id": str(UUID(int=901)),
        "state": {"status": 500}, "questions": questions,
    })
    answers = {"urgent": {"type": "noul", "probability": 0.8}}
    route = {
        "runtime_kind": "foundry", "gateway_implementation": "apim",
        "model_id": UUID(int=901), "runtime_id": UUID(int=902),
        "model_key": "unit-decision-alias", "runtime_name": "Unit Foundry",
        "gateway_base_url": "https://unit-apim.example/llm", "gateway_auth_type": "api_key",
        "gateway_config": {"header_name": "Ocp-Apim-Subscription-Key"},
        "gateway_credential": "unit-subscription-key",
        "runtime_config": {"api_format": "microsoft_decision", "control_plane_managed": True},
    }

    def handler(outgoing: httpx.Request) -> httpx.Response:
        calls.append(outgoing)
        assert outgoing.url.path == "/llm/providers/microsoft/v1/systemone"
        assert outgoing.headers["Ocp-Apim-Subscription-Key"] == "unit-subscription-key"
        assert json.loads(outgoing.content) == {
            "model": "unit-decision-alias", "state": {"status": 500}, "questions": questions,
        }
        return httpx.Response(200, json={"answers": answers, **({"usage": {
            "input_tokens": 93, "output_tokens": 3,
        }} if complete else {})}, headers={"x-correlation-id": "unit-gateway-correlation"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = GatewayRouter(client).adapter(route)
        assert isinstance(adapter, MicrosoftDecisionGatewayAdapter)
        result = adapter.invoke(decision, route)
        assert result.answers == answers and result.content == ""
        assert result.correlation_id == "unit-gateway-correlation"
        assert (result.usage is not None) is complete
        with pytest.raises(GatewayInvocationError, match="Decision invocation endpoint"):
            adapter.invoke(request(), route)
    assert len(calls) == 1


def test_native_decision_service_reuses_governance_and_platform_trace() -> None:
    encryption_key = Fernet.generate_key()
    cipher = CredentialCipher(encryption_key)
    repository = Mock(spec=InMemoryRepository)
    route = {
        "model_id": UUID(int=901), "runtime_id": UUID(int=902), "gateway_id": UUID(int=903),
        "model_key": "unit-decision", "display_name": "Unit Decision", "runtime_name": "Foundry",
        "runtime_kind": "foundry", "provider_name": "Microsoft Foundry",
        "provider_kind": "microsoft_foundry", "model_capabilities": ["decision"],
        "gateway_implementation": "apim", "native_gateway_id": "managed",
        "gateway_base_url": "https://unit-apim.example/llm", "gateway_auth_type": "api_key",
        "gateway_config": {"header_name": "Ocp-Apim-Subscription-Key"},
        "gateway_credential_ciphertext": cipher.encrypt("unit-subscription-key"),
        "runtime_config": {"control_plane_managed": True},
    }
    decision = DecisionInvocationRequest.model_validate({
        "model_id": str(route["model_id"]), "metadata": request().metadata.model_dump(),
        "state": "Unit outage", "questions": {"urgent": {
            "type": "noul", "instructions": "Is it urgent?",
        }},
    })
    answers = {"urgent": {"type": "noul", "probability": 0.8}}
    with httpx.Client(transport=httpx.MockTransport(lambda outgoing: httpx.Response(200, json={
        "answers": answers, "usage": {"input_tokens": 93, "output_tokens": 3},
    }, headers={"x-correlation-id": "unit-gateway-correlation"}))) as client:
        service = ModelRuntimeService(
            repository, Settings(credential_encryption_key=SecretStr(encryption_key.decode())),
            GatewayRouter(client),
        )
        with patch.object(service, "_invocation_route", return_value=route), \
                patch.object(service, "_assert_model_allowed") as allowed, \
                patch.object(service, "_prepare_budget_delegation") as delegated, \
                patch.object(service, "_assert_budget_available") as budget:
            result = service.invoke_decision(decision)
    assert result.answers == answers
    assert result.usage is not None and result.usage.input_tokens == 93
    assert result.correlation_id == "unit-gateway-correlation"
    allowed.assert_called_once()
    delegated.assert_called_once()
    budget.assert_called_once()
    repository.begin_platform_invocation.assert_called_once()
    repository.finish_platform_invocation.assert_called_once()


def test_http_runtime_requests_reuse_the_price_catalog() -> None:
    from backend.http.service_dependencies import runtime_service

    repository = InMemoryRepository()
    first = runtime_service(repository)
    second = runtime_service(repository)

    assert first is not second
    assert first._price_catalog is second._price_catalog


@pytest.mark.parametrize("outcome", ["stale", "unmapped", "review_needed"])
def test_price_sync_response_reports_superseded_nonwriting_plans(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
    outcome: str,
) -> None:
    service, catalog, write, model_id = price_edit_service
    service.save_model(write, model_id)

    def changed_during_lookup(reference: str) -> CatalogEntry | None:
        service._repository.update_registry_item("model", model_id, {
            "price_reference": "azure_retail:Unit:new:Global:*",
            "input_cost_per_million": 9,
        })
        if outcome == "stale":
            raise TimeoutError("offline")
        if outcome == "unmapped":
            return None
        return CatalogEntry(
            reference=reference, label="Arbitrary unit", source=PriceSource.AZURE_RETAIL,
            input_per_million=20, output_per_million=80,
        )

    catalog.lookup.side_effect = changed_during_lookup
    response = service.sync_prices([model_id])
    assert response.considered == 1
    assert response.updated == 0
    assert response.superseded == 1
    assert response.review_needed == response.stale == response.unmapped == 0
    assert len(response.details) == 1
    assert response.details[0].status == "superseded"
    current = next(model for model in response.registry.models if model.id == model_id)
    assert current.input_cost_per_million == 9
    assert current.pending_list_price is None


def request(*, model: str = "gpt-4.1", runtime: str = "Foundry Test") -> ModelInvocationRequest:
    return ModelInvocationRequest(
        metadata=InvocationMetadata(
            organization_id="org-contoso",
            organization="contoso",
            department_id="department-platform",
            department="platform",
            project_id="project-finops",
            project="finops",
            agent_id="agent-delivery-engineer",
            agent="delivery-engineer",
            user_id="alex@contoso.com",
            user="alex@contoso.com",
            workflow="release-review",
            model_id=model,
            model=model,
            runtime=runtime,
            request_source="test-suite",
            run_id="run-test",
            turn_index=2,
        ),
        messages=[ChatMessage(role="user", content="Return OK")],
    )


@pytest.mark.parametrize("url,path", (
    ("https://api.example.test", "/chat/completions"),
    ("https://api.example.test/v1/", "/v1/chat/completions"),
    ("https://api.example.test/v1/chat/completions", "/v1/chat/completions"),
))
def test_openai_connection_endpoint_keeps_origin_and_provider_path_separate(
    url: str, path: str,
) -> None:
    base, origin, backend_path = openai_compatible_endpoint_values(url)
    assert origin == "https://api.example.test"
    assert backend_path == path
    assert base == origin + path.removesuffix("/chat/completions")


@pytest.mark.parametrize("url", (
    "http://api.example.test/v1",
    "https://sample-user:sample-value@api.example.test/v1",
    "https://api.example.test/v1?key=sample",
    "https://api.example.test/v1#fragment",
    "/v1/chat/completions",
))
def test_openai_connection_endpoint_rejects_unsafe_shapes(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS without credentials"):
        openai_compatible_endpoint_values(url)


def test_openai_connection_names_are_bounded_and_retain_vendor_metadata() -> None:
    base = "https://api.example.test/" + "segment/" * 40
    name = openai_compatible_runtime_name(base, ModelVendorKey.KIMI)
    assert name.startswith("Kimi ") and name.endswith(" via APIM")
    assert len(name) == 160
    assert name == openai_compatible_runtime_name(base, ModelVendorKey.KIMI)
    assert name != openai_compatible_runtime_name(base + "another", ModelVendorKey.KIMI)


def test_openai_connection_contract_does_not_mix_provider_or_authentication_shapes() -> None:
    values = {
        "gateway_profile_id": UUID(int=1),
        "provider": ProviderTarget(template="openai_compatible"),
        "openai_base_url": HttpUrl("https://api.example.test/v1"),
        "model_vendor": ModelVendorKey.DEEPSEEK,
    }
    connection = ModelConnectionCreate.model_validate(values)
    assert connection.model_vendor is ModelVendorKey.DEEPSEEK
    assert "api_key" not in ModelConnectionCreate.model_fields
    with pytest.raises(ValueError, match="accepts only its Base URL"):
        ModelConnectionCreate.model_validate({**values, "auth_mode": "managed_identity"})
    with pytest.raises(ValueError, match="select exactly one"):
        ModelConnectionCreate.model_validate({
            **values,
            "bedrock_runtime_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
        })
    with pytest.raises(ValueError, match="requires its Base URL and API key"):
        RuntimeTarget(openai_base_url=connection.openai_base_url)
    with pytest.raises(ValueError, match="cannot redefine"):
        RuntimeTarget(existing_id=UUID(int=2), openai_base_url=connection.openai_base_url)
    runtime = RuntimeTarget(
        openai_base_url=connection.openai_base_url, api_key=SecretStr("sample-test-key")
    )
    assert runtime.existing_id is None


def test_openai_connection_registration_defers_credentials_and_model_publication(
    tmp_path: Path,
) -> None:
    repository = InMemoryRepository()
    service = ModelRuntimeService(repository, Settings(credential_key_file=tmp_path / "key"))
    model_count = len(repository.models)
    gateway_id = next(
        item["id"] for item in repository.gateways if item["implementation"] == "apim"
    )
    registry = service.save_connection(ModelConnectionCreate(
        gateway_profile_id=gateway_id,
        provider=ProviderTarget(template="openai_compatible"),
        openai_base_url=HttpUrl("https://api.example.test/v1/chat/completions"),
        model_vendor=ModelVendorKey.KIMI,
    ))

    runtime = next(item for item in registry.runtimes if item.config.get("model_vendor") == "kimi")
    assert runtime.name == "Kimi api.example.test/v1 via APIM"
    assert runtime.config["base_url"] == "https://api.example.test/v1"
    assert runtime.config["backend_url"] == "https://api.example.test"
    assert runtime.config["backend_path"] == "/v1/chat/completions"
    assert runtime.config["auth_strategy"] == "named_value_bearer"
    assert runtime.config["max_tokens_field"] == "max_tokens"
    assert runtime.config["credential_provisioned"] is False
    assert runtime.is_default is False
    assert len(repository.models) == model_count
    assert repository.gateway_publications == []
    assert repository.gateway_publication_secrets == {}

    with pytest.raises(HTTPException) as duplicate:
        service.save_connection(ModelConnectionCreate(
            gateway_profile_id=gateway_id,
            provider=ProviderTarget(existing_id=runtime.provider_id),
            openai_base_url=HttpUrl("https://api.example.test/v1/"),
        ))
    assert duplicate.value.status_code == 409
    with pytest.raises(HTTPException) as mismatched_vendor:
        service.save_connection(ModelConnectionCreate(
            gateway_profile_id=gateway_id,
            provider=ProviderTarget(existing_id=runtime.provider_id),
            openai_base_url=HttpUrl("https://another.example.test/v1"),
            model_vendor=ModelVendorKey.DEEPSEEK,
        ))
    assert mismatched_vendor.value.status_code == 409


def test_foundry_connections_on_different_gateways_have_unique_names(tmp_path: Path) -> None:
    repository = InMemoryRepository()
    service = ModelRuntimeService(repository, Settings(
        apim_principal_id=str(UUID(int=99)), credential_key_file=tmp_path / "key",
    ))
    first_gateway = next(item for item in repository.gateways if item["implementation"] == "apim")
    second_gateway_id = UUID(int=911)
    repository.gateways.append({
        **first_gateway, "id": second_gateway_id, "name": "Second APIM", "is_default": False,
    })
    endpoint = "https://same-project.services.ai.azure.com/api/projects/shared"
    request = {
        "gateway_profile_id": str(first_gateway["id"]),
        "provider": {"template": "microsoft_foundry"},
        "auth_mode": "managed_identity",
        "foundry_project_endpoint": endpoint,
    }
    first_registry = service.save_connection(ModelConnectionCreate.model_validate(request))
    first = next(
        item for item in first_registry.runtimes if item.config.get("project_endpoint") == endpoint
    )
    second_request = ModelConnectionCreate.model_validate({
        **request, "gateway_profile_id": str(second_gateway_id),
        "provider": {"existing_id": str(first.provider_id)},
    })

    registry = service.save_connection(second_request)

    connections = [
        item for item in registry.runtimes if item.config.get("project_endpoint") == endpoint
    ]
    assert len(connections) == 2
    assert len({item.id for item in connections}) == 2
    assert {item.gateway_profile_id for item in connections} == {
        first_gateway["id"], second_gateway_id,
    }
    assert next(item for item in connections if item.id == first.id) == first
    second = next(item for item in connections if item.gateway_profile_id == second_gateway_id)
    assert second.name == first.name
    assert second.id != first.id
    assert second.provider_id == first.provider_id
    assert second.config["auth_strategy"] == "managed_identity"
    with pytest.raises(HTTPException) as duplicate:
        service.save_connection(second_request)
    assert duplicate.value.status_code == 409


def test_databricks_connection_normalizes_workspace_and_uses_native_anthropic_route() -> None:
    workspace = "https://adb-unit.1.azuredatabricks.net"
    assert databricks_workspace_url(f"{workspace.upper()}/") == workspace
    config = databricks_connection_config(workspace, str(UUID(int=7)))
    assert config["backend_url"] == workspace
    assert config["backend_path"] == "/serving-endpoints/anthropic/v1/messages"
    assert config["path"] == "/v1/messages"
    assert config["managed_identity_resource"] == "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
    assert config["authorization"]["principal_id"] == str(UUID(int=7))
    assert config["authorization"]["role_name"] == "CAN_QUERY"
    assert databricks_runtime_name(workspace) == (
        "Azure Databricks adb-unit.1.azuredatabricks.net via APIM"
    )


@pytest.mark.parametrize("workspace", (
    "http://adb-unit.1.azuredatabricks.net",
    "https://adb-unit.1.azuredatabricks.net/serving-endpoints",
    "https://adb-unit.1.azuredatabricks.net:443",
    "https://adb-unit.1.azuredatabricks.net?token=unit-value",
    "https://adb-unit.1.azuredatabricks.net#fragment",
    "https://unit-user:unit-password@adb-unit.1.azuredatabricks.net",
    "https://adb-unit.1.azuredatabricks.net.example.test",
))
def test_databricks_connection_rejects_non_workspace_origins(workspace: str) -> None:
    with pytest.raises(ValueError, match="Azure Databricks HTTPS workspace URL"):
        databricks_workspace_url(workspace)


def test_databricks_connection_contract_keeps_provider_and_credential_shapes_separate() -> None:
    values = {
        "gateway_profile_id": UUID(int=1),
        "provider": {"template": "azure_databricks"},
        "databricks_workspace_url": "https://adb-unit.1.azuredatabricks.net",
        "auth_mode": "managed_identity",
    }
    connection = ModelConnectionCreate.model_validate(values)
    assert connection.auth_mode == "managed_identity"
    assert connection.oauth_client_id is None
    oauth = ModelConnectionCreate.model_validate({
        **values, "auth_mode": "oauth_m2m", "oauth_client_id": UUID(int=7),
    })
    assert oauth.oauth_client_id == UUID(int=7)
    for invalid in (
        {"auth_mode": "api_key"}, {"auth_mode": "oauth_m2m"},
        {"oauth_client_id": UUID(int=7)}, {"provider": {"template": "openai_compatible"}},
        {"openai_base_url": "https://api.example.test/v1"}, {"model_vendor": "anthropic"},
    ):
        with pytest.raises(ValueError):
            ModelConnectionCreate.model_validate({**values, **invalid})


@pytest.mark.parametrize("oauth", (False, True))
def test_databricks_connection_registration_defers_model_and_credentials(
    tmp_path: Path, oauth: bool,
) -> None:
    repository = InMemoryRepository()
    principal_id = str(UUID(int=7))
    settings = Settings(
        credential_key_file=tmp_path / "key", apim_principal_id=principal_id,
        databricks_oauth_enabled=oauth,
    )
    service = ModelRuntimeService(repository, settings)
    model_count = len(repository.models)
    gateway_id = next(row["id"] for row in repository.gateways if row["implementation"] == "apim")
    values: dict[str, Any] = {
        "gateway_profile_id": gateway_id,
        "provider": {"template": "azure_databricks"},
        "databricks_workspace_url": "https://adb-unit.1.azuredatabricks.net",
        "auth_mode": "oauth_m2m" if oauth else "managed_identity",
    }
    if oauth:
        values["oauth_client_id"] = UUID(int=8)
    registry = service.save_connection(ModelConnectionCreate.model_validate(values))
    runtime = next(
        row for row in registry.runtimes
        if row.config.get("workspace_url") == values["databricks_workspace_url"]
    )
    assert runtime.brand_key is BrandKey.AZURE_DATABRICKS
    assert runtime.config["control_plane_managed"] is True
    assert runtime.config["backend_path"] == "/serving-endpoints/anthropic/v1/messages"
    assert runtime.config["auth_strategy"] == (
        "oauth_client_credentials" if oauth else "managed_identity"
    )
    assert registry.databricks_connections_supported is True
    assert registry.databricks_oauth_supported is oauth
    if oauth:
        assert runtime.config["credential_provisioned"] is False
        assert runtime.config["oauth"]["provider_id"].startswith("turnstile-oauth-")
    else:
        assert runtime.config["authorization"]["principal_id"] == principal_id
    assert len(repository.models) == model_count
    assert repository.gateway_publications == []
    assert repository.gateway_publication_secrets == {}
    with pytest.raises(HTTPException) as duplicate:
        service.save_connection(ModelConnectionCreate.model_validate({
            **values, "provider": {"existing_id": runtime.provider_id},
        }))
    assert duplicate.value.status_code == 409
    with pytest.raises(HTTPException, match="publication workflows"):
        service.save_runtime(RuntimeWrite(
            provider_id=runtime.provider_id, gateway_profile_id=gateway_id,
            name="Bypass attempt", runtime_kind=runtime.runtime_kind,
            brand_key=BrandKey.GENERIC,
        ), runtime.id)


@pytest.mark.parametrize("missing", ("principal", "oauth_feature"))
def test_databricks_connection_registration_requires_deployed_authentication(
    tmp_path: Path, missing: str,
) -> None:
    repository = InMemoryRepository()
    settings = Settings(
        credential_key_file=tmp_path / "key",
        apim_principal_id=None if missing == "principal" else str(UUID(int=7)),
    )
    service = ModelRuntimeService(repository, settings)
    gateway_id = next(row["id"] for row in repository.gateways if row["implementation"] == "apim")
    runtime_count = len(repository.runtimes)
    with pytest.raises(HTTPException) as rejected:
        service.save_connection(ModelConnectionCreate.model_validate({
            "gateway_profile_id": gateway_id, "provider": {"template": "azure_databricks"},
            "databricks_workspace_url": "https://adb-unit.1.azuredatabricks.net",
            "auth_mode": "oauth_m2m", "oauth_client_id": UUID(int=8),
        }))
    assert rejected.value.status_code == 409
    assert len(repository.runtimes) == runtime_count


def test_databricks_oauth_contract_scopes_credentials_and_preserves_legacy_dependencies() -> None:
    oauth = OAuthClientCredentialsConfig(
        provider_id=f"turnstile-oauth-{UUID(int=8).hex}", client_id=UUID(int=7),
        token_url=HttpUrl("https://adb-unit.1.azuredatabricks.net/oidc/v1/token"),
    )
    assert oauth.scopes == "all-apis"
    for endpoint in (
        "https://adb-unit.1.azuredatabricks.net/oauth/token",
        "https://tokens.example.test/oidc/v1/token",
    ):
        with pytest.raises(ValueError):
            OAuthClientCredentialsConfig.model_validate({
                **oauth.model_dump(), "token_url": endpoint,
            })
    secret = SecretStr("unit-oauth-value")
    runtime = RuntimeTarget(existing_id=UUID(int=9), oauth_client_secret=secret)
    assert "unit-oauth-value" not in runtime.model_dump_json()
    with pytest.raises(ValueError, match="existing connection"):
        RuntimeTarget(oauth_client_secret=secret)
    with pytest.raises(ValueError, match="one credential"):
        GatewayPublicationRetry(api_key=secret, oauth_client_secret=secret)
    with pytest.raises(ValueError, match="exactly one"):
        GatewayCredentialRotation(model_key="unit-model")
    rotation = GatewayCredentialRotation(model_key="unit-model", oauth_client_secret=secret)
    assert rotation.api_key is None
    dependencies = GatewayReleaseDependencies(
        apim_revision="unit", parent_policy_sha256=None, compiled_policy_sha256=None,
        recorded_complete=True, live_status="not_checked",
    )
    assert "oauth_credentials" not in dependencies.model_dump(mode="json")
    dependencies.oauth_credentials = [oauth.provider_id]
    assert dependencies.model_dump(mode="json")["oauth_credentials"] == [oauth.provider_id]


@pytest.mark.parametrize("details,top_level,expected", (
    (None, 6, 6),
    ({"cached_tokens": 0}, 6, 0),
    ({"cached_tokens": 3}, 6, 3),
    ({"cached_tokens": None}, 6, 6),
    (None, None, 0),
))
def test_openai_cache_usage_prefers_explicit_nested_measurement(
    details: dict[str, int | None] | None, top_level: int | None, expected: int,
) -> None:
    usage = _openai_usage({
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "prompt_tokens_details": details,
        "cached_tokens": top_level,
    })
    assert usage.cached_tokens == expected
    assert usage.input_tokens == 10 - expected
    assert usage.output_tokens == 2
    assert usage.estimated is False


def test_credential_cipher_round_trip() -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    encrypted = cipher.encrypt("secret-value")
    assert encrypted != b"secret-value"
    assert cipher.decrypt(encrypted) == "secret-value"


@pytest.mark.parametrize("value", [[], {}, "", "1", True, 1.0, -1])
@pytest.mark.parametrize("nested", [False, True])
def test_openai_cached_tokens_rejects_illegal_values(value: object, nested: bool) -> None:
    payload = {"prompt_tokens": 20, "completion_tokens": 3, "cached_tokens": value}
    if nested:
        payload["prompt_tokens_details"] = {"cached_tokens": value}
    with pytest.raises(ValueError, match="nonnegative integer"):
        _openai_usage(payload)


def _assert_storable(record: TokenUsageRecord) -> None:
    """Check the row against the CHECK constraints PostgreSQL actually enforces.

    InMemoryRepository stores anything, so a record can pass every test here and still be
    rejected by the real database. That is not hypothetical: the first denial row used
    `et_coeff_m=0`, which violates `CHECK (et_coeff_m > 0)`, and the broad except around
    the write swallowed it — enforcement worked while every trace silently vanished.
    """
    assert record.turn_index > 0
    assert record.provider.strip()
    assert record.input_tokens >= 0
    assert record.cached_tokens >= 0
    assert record.output_tokens >= 0
    assert record.cache_write_tokens <= record.cached_tokens
    assert record.et >= 0
    assert record.et_coeff_m > 0
    assert record.latency_ms >= 0
    assert 100 <= record.status_code <= 599
    assert record.ingest_source in {"eventhub", "backfill", "gateway", "policy"}


def _foundry_route(
    repository: InMemoryRepository,
) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = next(
        item for item in repository.runtimes if item["name"] == "Microsoft Foundry via APIM"
    )
    gateway = next(item for item in repository.gateways
                   if item["id"] == runtime["gateway_profile_id"])
    gateway["base_url"] = "https://gateway.example.test/turnstile/llm"
    model = next(item for item in repository.models if item["model_key"] == "gpt-5.3-chat")
    return runtime, model


def test_internal_invocation_enforces_user_model_policy() -> None:
    # The APIM ledger check only covers employee tokens, because that is the only path
    # where the identity cannot be forged. The dashboard path authenticates with a
    # subscription key, so it never reaches that check — a deny-all person kept getting
    # HTTP 200 from the invocation console. Here we are the caller and the policy is in
    # PostgreSQL beside us, so this is where that path can be enforced exactly.
    def handler(incoming: httpx.Request) -> httpx.Response:
        raise AssertionError("the provider must not be reached for a denied model")

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    repository.bulk_upsert_user_budgets(
        date(2026, 7, 1),
        "department-platform",
        [],
        80,
        "owner",
        selected_user_ids=["alex@contoso.com"],
        model_ids=[],
    )
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    with pytest.raises(HTTPException) as error:
        service.invoke(
            request(model=model["model_key"], runtime=runtime["name"]).model_copy(
                update={"model_id": model["id"]}
            )
        )

    assert error.value.status_code == 403
    assert error.value.detail == "model_not_assigned"
    # A denial nobody can see is the worse failure: the person is still stopped but the
    # dashboard cannot explain why. The APIM policy learned this, so this path traces too.
    assert len(repository.usage_records) == 1
    denial = repository.usage_records[0]
    assert denial.status_code == 403
    assert denial.model_admission == "denied"
    assert denial.budget_admission is None
    assert denial.ingest_source == "policy"
    assert (denial.input_tokens, denial.cached_tokens, denial.output_tokens) == (0, 0, 0)
    # A rejected request's zero usage is exact, not missing.
    assert denial.estimated is False
    assert denial.estimated_cost == 0.0
    # The console names the model by registry UUID. Storing that verbatim showed a bare
    # UUID as the model name in the trace, which is the split identity migration 013
    # collapsed for the ingestion path.
    assert denial.model_id == str(model["id"])
    assert denial.model == model["display_name"]
    assert denial.model != str(model["id"])
    _assert_storable(denial)


def test_dynamic_model_does_not_allow_legacy_health_identity_to_bypass_assignment() -> None:
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    repository = InMemoryRepository()
    _, model = _foundry_route(repository)
    model["assignment_required"] = True
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )
    person_request = request().model_copy(update={"model_id": model["id"]})

    with pytest.raises(HTTPException) as error:
        service.invoke(person_request)
    assert error.value.detail == "model_not_assigned"
    assert calls == 0

    assert "enforce_user_model_access" not in signature(service.invoke).parameters
    assert calls == 0

    health_request = person_request.model_copy(
        update={
            "metadata": person_request.metadata.model_copy(
                update={
                    "user_id": "system-runtime-health-check",
                    "user": "Runtime Health Check",
                }
            )
        }
    )
    with pytest.raises(HTTPException) as health_error:
        service.invoke(health_request)
    assert health_error.value.detail == "model_not_assigned"
    assert calls == 0


def test_a_denial_trace_names_the_runtime_it_routed_to() -> None:
    # The console asserted metadata.runtime = the registry UUID, and the denial path stored
    # it verbatim while resolving only the model server-side. Three live rows landed under
    # '30000000-0000-4000-8000-000000000003' and drew a fourth bar in the runtime
    # distribution beside the same runtime's real name. Runtime has no id column in
    # telemetry, so a UUID there is not a second identifier space to reconcile later — it
    # is simply a wrong name, and nothing downstream can tell.
    def handler(incoming: httpx.Request) -> httpx.Response:
        raise AssertionError("the provider must not be reached for a denied model")

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    repository.bulk_upsert_user_budgets(
        date(2026, 7, 1),
        "department-platform",
        [],
        80,
        "owner",
        selected_user_ids=["alex@contoso.com"],
        model_ids=[],
    )
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    with pytest.raises(HTTPException):
        service.invoke(
            # Exactly what the console used to send: primary keys in both display fields.
            request(model=str(model["id"]), runtime=str(runtime["id"])).model_copy(
                update={"model_id": model["id"], "runtime_id": runtime["id"]}
            )
        )

    denial = repository.usage_records[0]
    assert denial.runtime == runtime["name"]
    assert denial.runtime != str(runtime["id"])
    # The model half of the same defect must not regress either.
    assert denial.model == model["display_name"]
    assert denial.model_id == str(model["id"])
    _assert_storable(denial)


def test_internal_invocation_allows_a_person_with_no_model_policy() -> None:
    # No policy row means the person was never restricted. Blocking them would take out
    # everyone who has not been configured, which is the same rule the gateway applies.
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["x-model-id"] == str(model["id"])
        assert incoming.headers["x-runtime-id"] == str(runtime["id"])
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ALLOWED"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    response = service.invoke(
        request(model=model["model_key"], runtime=runtime["name"]).model_copy(
            update={"model_id": model["id"]}
        )
    )

    assert response.content == "ALLOWED"
    assert response.model == model["model_key"]
    assert response.estimated_cost == 0.00005
    assert repository.usage_records == []


def test_internal_invocation_applies_router_timeout_to_http_transport() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        timeout = incoming.extensions["timeout"]
        assert timeout["connect"] == 1.25
        assert timeout["read"] == 1.25
        assert timeout["write"] == 1.25
        assert timeout["pool"] == 1.25
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "TIMEOUT_APPLIED"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    response = service.invoke(
        request(model=model["model_key"], runtime=runtime["name"]).model_copy(
            update={"model_id": model["id"]}
        ),
        timeout_ms=1_250,
    )

    assert response.content == "TIMEOUT_APPLIED"


def test_internal_invocation_keeps_missing_registry_prices_unpriced() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "UNPRICED"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    model["output_cost_per_million"] = None
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    response = service.invoke(
        request(model=model["model_key"], runtime=runtime["name"]).model_copy(
            update={"model_id": model["id"]}
        )
    )

    assert response.estimated_cost is None
    assert repository.usage_records == []


def test_internal_invocation_prices_cache_read_and_write_separately() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "PRICED"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {
                        "cached_tokens": 30,
                        "cache_write_tokens": 5,
                    },
                },
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    model.update(
        input_cost_per_million=2,
        output_cost_per_million=10,
        cached_cost_per_million=0.2,
        cache_write_cost_per_million=2.5,
    )
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    response = service.invoke(
        request(model=model["model_key"], runtime=runtime["name"]).model_copy(
            update={"model_id": model["id"]}
        )
    )

    assert response.usage is not None
    assert response.usage.input_tokens == 65
    assert response.usage.cached_tokens == 35
    assert response.usage.cache_write_tokens == 5
    assert response.estimated_cost == 0.0002485


def test_foundry_claude_invocation_uses_messages_on_shared_openai_runtime() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.path.endswith("/v1/messages")
        assert incoming.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(incoming.content)
        assert payload["model"] == "claude-opus-5-foundry-8c7e7f9d"
        assert payload["max_tokens"] == 512
        assert "max_completion_tokens" not in payload
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "CLAUDE_OK"}],
                "usage": {"input_tokens": 4, "output_tokens": 1},
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    model.update(
        model_key="claude-opus-5-foundry-8c7e7f9d",
        display_name="claude-opus-5 · Microsoft Foundry",
        upstream_model_id="claude-opus-5",
        family_key="claude",
    )
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    response = service.invoke(
        request(model=model["model_key"], runtime=runtime["name"]).model_copy(
            update={"model_id": model["id"]}
        )
    )

    assert response.content == "CLAUDE_OK"
    assert response.model == model["model_key"]


def _seed_budget(repository: InMemoryRepository, *, limit: int, used: int, mode: str) -> None:
    """Give the test person an allowance, some usage against it, and a department mode.

    The period is derived from the wall clock exactly as the service does, so the test
    keeps meaning the same thing next month.
    """
    now = datetime.now(UTC)
    period = date(now.year, now.month, 1)
    repository.upsert_token_budget(
        period, "department", "department-platform", None, limit * 10, 80, "owner"
    )
    repository.bulk_upsert_user_budgets(
        period,
        "department-platform",
        [("alex@contoso.com", limit)],
        80,
        "owner",
    )
    repository.set_department_enforcement("department-platform", mode, "owner")
    repository.write_token_usage(
        TokenUsageRecord(
            id="seeded-usage",
            request_id="seeded-usage",
            correlation_id="seeded-usage",
            ts=now,
            team="platform",
            user="alex@contoso.com",
            user_id="alex@contoso.com",
            agent="delivery-engineer",
            workflow="release-review",
            run_id="run-seed",
            turn_index=1,
            provider="microsoft_foundry",
            model="gpt-5.6-luna",
            input_tokens=used,
            cached_tokens=0,
            output_tokens=0,
            et=0.0,
            et_coeff_m=0.0,
            latency_ms=1,
            status="200",
            status_code=200,
            estimated_cost=0.0,
            estimated=False,
            ingest_source="eventhub",
        )
    )


@pytest.mark.parametrize("mode", ["audit", "block"])
def test_internal_usd_budget_requires_verified_atomic_admission_even_in_audit(mode: str) -> None:
    repository = Mock(spec=InMemoryRepository)
    repository.person_budget_state.return_value = {
        "budget_mode": "usd", "token_limit": None, "mode": mode, "used_tokens": 100,
    }
    service = ModelRuntimeService(repository, Settings())
    with pytest.raises(HTTPException, match="atomic APIM admission") as error:
        service._assert_budget_available(request(), {}, "unit-usd-preflight")
    assert error.value.status_code == 503
    repository.person_budget_state.reset_mock()
    service._assert_budget_available(
        request(), {"budget_delegation_token": SecretStr("unit-only")}, "unit-delegated",
    )
    repository.person_budget_state.assert_not_called()


def test_atomic_budget_delegation_never_bypasses_person_model_assignment() -> None:
    repository = Mock(spec=InMemoryRepository)
    repository.list_user_model_policies.return_value = []
    service = ModelRuntimeService(repository, Settings())
    route = {
        "budget_delegation_token": SecretStr("unit-only"), "assignment_required": True,
        "model_id": UUID(int=77), "display_name": "Unit model", "runtime_name": "Unit runtime",
    }
    with pytest.raises(HTTPException) as error:
        service._assert_model_allowed(request(), route, "unit-assignment")
    assert error.value.status_code == 403 and error.value.detail == "model_not_assigned"
    repository.list_user_model_policies.assert_called_once_with([request().metadata.user_id])


def test_internal_invocation_enforces_monthly_budget() -> None:
    # Same blind spot as the model policy: the reservation ledger only guards the employee
    # token branch, so a console call was never budget-checked no matter how far over the
    # person was. The allowance and the usage are both one query away here, so the check is
    # a direct comparison rather than a reservation.
    def handler(incoming: httpx.Request) -> httpx.Response:
        raise AssertionError("the provider must not be reached once the budget is spent")

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    _seed_budget(repository, limit=1000, used=1000, mode="block")
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    with pytest.raises(HTTPException) as error:
        service.invoke(
            request(model=model["model_key"], runtime=runtime["name"]).model_copy(
                update={"model_id": model["id"]}
            )
        )

    assert error.value.status_code == 403
    assert error.value.detail == "budget_exceeded"
    denial = repository.usage_records[-1]
    assert denial.status_code == 403
    assert denial.budget_admission == "denied"
    assert denial.model_admission is None
    assert denial.ingest_source == "policy"
    assert (denial.input_tokens, denial.output_tokens) == (0, 0)
    _assert_storable(denial)


def test_internal_invocation_does_not_block_a_department_in_audit_mode() -> None:
    # Audit mode is the deployed default and means "report, do not stop". Enforcing it
    # anyway would turn a reporting choice into an outage on rollout.
    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ALLOWED"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    _seed_budget(repository, limit=1000, used=5000, mode="audit")
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    response = service.invoke(
        request(model=model["model_key"], runtime=runtime["name"]).model_copy(
            update={"model_id": model["id"]}
        )
    )

    assert response.content == "ALLOWED"
    assert all(record.status_code != 403 for record in repository.usage_records)


def test_apim_denial_is_traced_without_overwriting_formal_telemetry() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "x-request-id": "request-apim-denied",
                "x-correlation-id": "correlation-apim-denied",
                "retry-after": "60",
            },
            json={
                "error": {
                    "message": "Model is not assigned to this user",
                    "type": "policy_denied",
                    "code": "model_not_assigned",
                }
            },
        )

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    service = ModelRuntimeService(
        repository,
        Settings(),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    with pytest.raises(HTTPException) as blocked:
        service.invoke(
            request(model=model["model_key"], runtime=runtime["name"]).model_copy(
                update={"model_id": model["id"]}
            ), request_id="platform-denied-first",
        )

    assert blocked.value.status_code == 403
    assert blocked.value.headers is not None
    assert blocked.value.headers["x-request-id"] == "platform-denied-first"
    assert blocked.value.headers["x-upstream-request-id"] == "request-apim-denied"
    assert blocked.value.headers["x-correlation-id"] == "correlation-apim-denied"
    assert blocked.value.headers["retry-after"] == "60"
    assert repository.usage_records == []
    attempt = repository.invocation_attempts["platform-denied-first"]
    completion = repository.invocation_completions[attempt.request_id]
    assert completion.status_code == 403 and completion.failure_kind == "http_error"
    trace = repository.get_usage_request(attempt.request_id)
    assert trace is not None and not trace["usage_available"] and not trace["cost_available"]
    formal = TokenUsageRecord(
        id="formal-denial", request_id=attempt.request_id, correlation_id="correlation-apim-denied",
        ts=attempt.started_at, team="unit", user=attempt.metadata.user,
        user_id=attempt.metadata.user_id, agent="unit", workflow="unit",
        run_id="unit", turn_index=1,
        provider="unit", model=model["display_name"], model_id=str(model["id"]),
        runtime=runtime["name"], input_tokens=7, output_tokens=3, cached_tokens=0,
        estimated=False, status="200", status_code=200, ingest_source="eventhub",
        et=10, et_coeff_m=1, latency_ms=1, estimated_cost=0.000026,
        input_price_per_million=2, output_price_per_million=4,
    )
    repository.write_token_usage(formal)
    assert len(repository.usage_records) == 1
    assert repository.usage_records[0].estimated_cost == 0.000026
    settled = repository.usage_records[0]
    with pytest.raises(HTTPException):
        service.invoke(request().model_copy(update={"model_id": model["id"]}))
    assert repository.usage_records == [settled]


@pytest.mark.parametrize("failure", ["bad_request", "timeout"])
@pytest.mark.parametrize("completion_available", [True, False])
def test_failed_invocation_preserves_request_identity_and_unknown_usage(
    monkeypatch: pytest.MonkeyPatch, failure: str, completion_available: bool,
) -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=incoming)
        return httpx.Response(400, json={"error": {"message": "not published"}})

    repository = InMemoryRepository()
    runtime, model = _foundry_route(repository)
    if not completion_available:
        monkeypatch.setattr(
            repository, "finish_platform_invocation", Mock(side_effect=RuntimeError("offline")),
        )
    service = ModelRuntimeService(
        repository, Settings(), GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )
    with pytest.raises(HTTPException) as caught:
        service.invoke(
            request().model_copy(update={"model_id": model["id"]}), request_id="unit-request",
        )
    assert caught.value.status_code == (504 if failure == "timeout" else 400)
    assert caught.value.headers is not None
    assert caught.value.headers["x-request-id"] == "unit-request"
    record = repository.get_usage_request("unit-request")
    assert record is not None and record["correlation_id"] is None
    assert record["usage_available"] is False and record["cost_available"] is False
    assert record["runtime"] == runtime["name"]
    assert record["platform_outcome"] == ("failed" if completion_available else "dispatched")
    assert record["status_code"] == (caught.value.status_code if completion_available else None)
    assert caught.value.headers["x-platform-trace-state"] == (
        "recorded" if completion_available else "pending"
    )
    assert repository.usage_records == []




def test_production_invocation_rejects_non_apim_transport() -> None:
    repository = InMemoryRepository()
    runtime = next(item for item in repository.runtimes if item["runtime_kind"] == "copilot_cli")
    model = dict(next(item for item in repository.models if item["model_key"] == "gpt-5.3-chat"))
    model.update(
        id=UUID("40000000-0000-4000-8000-000000000099"),
        provider_id=runtime["provider_id"],
        provider_name=runtime["provider_name"],
        runtime_id=runtime["id"],
        runtime_name=runtime["name"],
        model_key="test-only-copilot-model",
        display_name="Test-only Copilot model",
        enabled=True,
        is_default=False,
    )
    repository.models.append(model)
    service = ModelRuntimeService(
        repository,
        Settings(
            production=True,
            credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
        ),
    )

    with pytest.raises(HTTPException) as blocked:
        service.invoke(
            request(model=model["model_key"], runtime=runtime["name"]).model_copy(
                update={"model_id": model["id"]}
            )
        )

    assert blocked.value.status_code == 409
    assert blocked.value.detail == (
        "Production model invocations must use Azure API Management"
    )
    assert repository.usage_records == []


def test_cli_adapter_executes_restricted_command(tmp_path: Path) -> None:
    executable = tmp_path / "fake-model"
    executable.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo fake-1.0; else echo CLI_OK; fi\n"
    )
    executable.chmod(0o700)
    route = {
        "runtime_id": UUID("30000000-0000-4000-8000-000000000099"),
        "runtime_config": {
            "command": str(executable),
            "args": ["-p", "{prompt}"],
            "working_directory": str(tmp_path),
        },
        "model_key": "fake-model",
    }
    result = CliGatewayAdapter().invoke(request(model="fake-model", runtime="Fake CLI"), route)
    assert result.content == "CLI_OK"
    assert result.gateway == "local-cli"
    assert result.usage is not None and result.usage.estimated is True
    assert CliGatewayAdapter().check(route).status.value == "available"


def test_http_adapter_injects_business_metadata() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.headers["x-hive-organization"] == "contoso"
        assert incoming.headers["x-hive-department"] == "platform"
        assert incoming.headers["x-hive-project"] == "finops"
        assert incoming.headers["x-hive-agent"] == "delivery-engineer"
        assert incoming.headers["x-hive-user"] == "alex@contoso.com"
        assert incoming.headers["x-hive-model"] == "gpt-4.1"
        assert incoming.headers["x-hive-runtime"] == "Foundry Test"
        assert incoming.headers["x-org-id"] == "org-contoso"
        assert incoming.headers["x-org-name"] == "contoso"
        assert incoming.headers["x-department-id"] == "department-platform"
        assert incoming.headers["x-project-id"] == "project-finops"
        assert incoming.headers["x-agent-id"] == "agent-delivery-engineer"
        assert incoming.headers["x-user-id"] == "alex@contoso.com"
        assert incoming.headers["x-request-source"] == "test-suite"
        assert incoming.headers["x-request-id"] == "request-123"
        assert incoming.headers["authorization"] == "Bearer gateway-secret"
        payload = json.loads(incoming.content)
        assert payload["model"] == "gpt-4.1"
        return httpx.Response(
            200,
            headers={"x-correlation-id": "apim-correlation-123"},
            json={
                "choices": [{"message": {"content": "HTTP_OK"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.LITELLM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-123",
        "model_id": UUID("40000000-0000-4000-8000-000000000003"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/v1/chat/completions"},
        "model_key": "gpt-4.1",
        "runtime_name": "Foundry Test",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "bearer",
        "gateway_credential": "gateway-secret",
        "gateway_config": {},
    }
    result = adapter.invoke(request(), route)
    assert result.content == "HTTP_OK"
    assert result.usage is not None and result.usage.cached_tokens == 2
    assert result.correlation_id == "apim-correlation-123"


def test_http_adapter_supports_current_gpt_token_parameter() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["max_completion_tokens"] == 16
        assert "max_tokens" not in payload
        assert "temperature" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-gpt5",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {
            "path": "/chat/completions",
            "max_tokens_field": "max_completion_tokens",
            "supports_temperature": False,
        },
        "model_key": "gpt-5.6-terra",
        "runtime_name": "Foundry Test",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }
    invocation = request(model="gpt-5.6-terra").model_copy(
        update={"max_output_tokens": 16, "temperature": 0}
    )
    result = adapter.invoke(invocation, route)
    assert result.content == "OK"


def test_http_adapter_forwards_json_object_response_format() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"model_id":"selected"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-json-object",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/chat/completions"},
        "model_key": "routing-model",
        "runtime_name": "Foundry Test",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }
    invocation = request(model="routing-model").model_copy(
        update={"response_format": "json_object"}
    )

    result = adapter.invoke(invocation, route)

    assert result.content == '{"model_id":"selected"}'


def test_http_adapter_sends_upstream_model_without_losing_turnstile_alias() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["model"] == "gpt-5.6-luna"
        assert incoming.headers["x-hive-model"] == "customer-luna-alias"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-envoy-alias",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/chat/completions"},
        "model_key": "customer-luna-alias",
        "upstream_model_id": "gpt-5.6-luna",
        "runtime_name": "Envoy Cache Experiment",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }

    result = adapter.invoke(request(model="customer-luna-alias"), route)

    assert result.content == "OK"


def test_apim_managed_route_sends_alias_for_policy_rewrite() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["model"] == "fw-kimi-k3-foundry-8c7e7f9d"
        assert incoming.headers["x-hive-model"] == "fw-kimi-k3-foundry-8c7e7f9d"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-kimi-alias",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {
            "path": "/chat/completions",
            "control_plane_managed": True,
        },
        "model_key": "fw-kimi-k3-foundry-8c7e7f9d",
        "upstream_model_id": "FW-Kimi-K3",
        "runtime_name": "Microsoft Foundry via APIM",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }

    result = adapter.invoke(request(model="fw-kimi-k3-foundry-8c7e7f9d"), route)

    assert result.content == "OK"


def test_http_adapter_consumes_streamed_cache_read_and_write_usage() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "apim-request-id": "apim-stream-123",
            },
            content=(
                'data: {"id":"chatcmpl-stream","choices":[{"delta":'
                '{"content":"STREAM_"}}],"usage":null}\n\n'
                'data: {"id":"chatcmpl-stream","choices":[{"delta":'
                '{"content":"OK"}}],"usage":null}\n\n'
                'data: {"id":"chatcmpl-stream","choices":[],"usage":'
                '{"prompt_tokens":1200,"completion_tokens":30,'
                '"prompt_tokens_details":{"cached_tokens":1000,'
                '"cache_write_tokens":128}}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-stream",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/chat/completions"},
        "model_key": "gpt-5.6-luna",
        "runtime_name": "Envoy Cache Experiment",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }

    result = adapter.invoke(
        request(model="gpt-5.6-luna").model_copy(update={"stream": True}), route
    )

    assert result.content == "STREAM_OK"
    assert result.correlation_id is None
    assert result.upstream_request_id == "apim-stream-123"
    assert result.usage is not None
    assert result.usage.input_tokens == 72
    assert result.usage.cached_tokens == 1128
    assert result.usage.cache_write_tokens == 128
    assert result.usage.output_tokens == 30
    assert result.usage.estimated is False


def test_http_adapter_preserves_streamed_error_detail() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert json.loads(incoming.content)["stream"] is True
        return httpx.Response(
            429,
            json={"error": {"message": "Token quota exceeded", "code": "quota_exceeded"}},
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-stream-error",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/chat/completions"},
        "model_key": "gpt-5.6-luna",
        "runtime_name": "Envoy Cache Experiment",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }

    with pytest.raises(GatewayInvocationError) as caught:
        adapter.invoke(
            request(model="gpt-5.6-luna").model_copy(update={"stream": True}), route
        )

    assert caught.value.status_code == 429
    assert str(caught.value) == (
        "Gateway request failed (429): message=Token quota exceeded; code=quota_exceeded"
    )


def test_http_adapter_reports_transport_timeout_as_504() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out", request=incoming)

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-timeout",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/chat/completions"},
        "model_key": "gpt-5.6-luna",
        "runtime_name": "Foundry Test",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }

    with pytest.raises(GatewayInvocationError) as caught:
        adapter.invoke(request(model="gpt-5.6-luna"), route)

    assert caught.value.status_code == 504
    assert str(caught.value) == "Gateway request timed out"


def test_cli_adapter_reports_timeout_as_504(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "turnstile_core.integrations.gateway.shutil.which", lambda _: "/usr/bin/fake-cli"
    )

    def time_out(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["timeout"] == 1.25
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr("turnstile_core.integrations.gateway.subprocess.run", time_out)
    route = {
        "runtime_config": {
            "command": "fake-cli",
            "working_directory": str(tmp_path),
            "timeout_seconds": 1.25,
        },
        "model_key": "gpt-5.6-luna",
    }

    with pytest.raises(GatewayInvocationError) as caught:
        CliGatewayAdapter().invoke(request(model="gpt-5.6-luna"), route)

    assert caught.value.status_code == 504
    assert str(caught.value) == "CLI runtime timed out"


def test_managed_foundry_runtime_defaults_to_provider_temperature() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        assert "temperature" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-managed-foundry",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {
            "path": "/chat/completions",
            "api_format": "openai_chat",
            "control_plane_managed": True,
        },
        "model_key": "gpt-5.6-sol-foundry-bd33e608",
        "runtime_name": "Microsoft Foundry admin-8382 via APIM",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }
    invocation = request(model="gpt-5.6-sol-foundry-bd33e608").model_copy(
        update={"temperature": 0}
    )

    result = adapter.invoke(invocation, route)

    assert result.content == "OK"


def test_http_adapter_reports_safe_structured_upstream_error() -> None:
    adapter = OpenAICompatibleGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Unsupported parameter: stream",
                            "type": "invalid_request_error",
                            "param": "stream",
                            "code": "unsupported_parameter",
                            "request_body": "must not be exposed",
                        }
                    },
                )
            )
        ),
    )
    route = {
        "request_id": "request-error",
        "model_id": UUID("40000000-0000-4000-8000-000000000004"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
        "runtime_config": {"path": "/chat/completions"},
        "model_key": "gpt-5.6-terra",
        "runtime_name": "Foundry Test",
        "gateway_base_url": "https://gateway.test",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }
    with pytest.raises(GatewayInvocationError) as captured:
        adapter.invoke(request(model="gpt-5.6-terra"), route)
    detail = str(captured.value)
    assert "param=stream" in detail
    assert "code=unsupported_parameter" in detail
    assert "must not be exposed" not in detail


def test_anthropic_adapter_maps_messages_and_usage() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url.path == "/turnstile/llm/v1/messages"
        assert incoming.headers["anthropic-version"] == "2023-06-01"
        assert incoming.headers["x-model-id"] == "40000000-0000-4000-8000-000000000005"
        payload = json.loads(incoming.content)
        assert payload == {
            "model": "databricks-claude-sonnet-5",
            "messages": [{"role": "user", "content": "Return OK"}],
            "max_tokens": 64,
            "stream": False,
            "system": "Be concise.",
        }
        return httpx.Response(
            200,
            headers={"request-id": "databricks-request-123"},
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ANTHROPIC_OK"}],
                "usage": {
                    "input_tokens": 8,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 2,
                    "output_tokens": 4,
                },
            },
        )

    adapter = AnthropicMessagesGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-anthropic",
        "model_id": UUID("40000000-0000-4000-8000-000000000005"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000004"),
        "runtime_config": {
            "path": "/v1/messages",
            "api_format": "anthropic_messages",
            "default_max_tokens": 64,
        },
        "model_key": "databricks-claude-sonnet-5",
        "runtime_name": "Azure Databricks Claude via APIM",
        "gateway_base_url": "https://gateway.test/turnstile/llm",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }
    invocation = request(
        model="databricks-claude-sonnet-5",
        runtime="Azure Databricks Claude via APIM",
    ).model_copy(
        update={
            "temperature": 0,
            "messages": [
                ChatMessage(role="system", content="Be concise."),
                ChatMessage(role="user", content="Return OK"),
            ]
        }
    )

    result = adapter.invoke(invocation, route)

    assert result.content == "ANTHROPIC_OK"
    assert result.correlation_id is None
    assert result.upstream_request_id == "databricks-request-123"
    assert result.usage is not None
    assert result.usage.input_tokens == 8
    assert result.usage.cached_tokens == 5
    assert result.usage.output_tokens == 4
    assert result.usage.estimated is False


def test_anthropic_adapter_reports_databricks_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_code": "BAD_REQUEST",
                "message": '{"message":"`temperature` is deprecated for this model."}',
            },
        )

    adapter = AnthropicMessagesGatewayAdapter(
        GatewayKind.APIM,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route = {
        "request_id": "request-anthropic-error",
        "model_id": UUID("40000000-0000-4000-8000-000000000005"),
        "runtime_id": UUID("30000000-0000-4000-8000-000000000004"),
        "runtime_config": {"path": "/v1/messages", "api_format": "anthropic_messages"},
        "model_key": "databricks-claude-sonnet-5",
        "runtime_name": "Azure Databricks Claude via APIM",
        "gateway_base_url": "https://gateway.test/turnstile/llm",
        "gateway_auth_type": "none",
        "gateway_config": {},
    }

    with pytest.raises(GatewayInvocationError) as captured:
        adapter.invoke(request(model="databricks-claude-sonnet-5"), route)

    detail = str(captured.value)
    assert "`temperature` is deprecated for this model" in detail
    assert "error_code=BAD_REQUEST" in detail


@pytest.fixture
def price_edit_service(
    tmp_path: Path,
) -> tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID]:
    repository = InMemoryRepository()
    model_id = UUID("40000000-0000-4000-8000-000000000003")
    repository.update_registry_item("model", model_id, {"cached_cost_per_million": 0.45})
    catalog = Mock(spec=CompositeCatalog)
    reference = "azure_retail:Azure OpenAI:gpt 4.1:Global:*"
    catalog.lookup.return_value = CatalogEntry(
        reference=reference, label="gpt 4.1", source=PriceSource.AZURE_RETAIL,
        input_per_million=2.0, output_per_million=8.0,
    )
    service = ModelRuntimeService(
        repository, Settings(credential_key_file=tmp_path / "credential.key"),
        price_catalog=catalog,
    )
    current = next(model for model in service.registry().models if model.id == model_id)
    write = ManagedModelWrite.model_validate(
        current.model_dump(include=set(ManagedModelWrite.model_fields))
        | {"price_source": "azure_retail", "price_reference": reference,
           "cached_cost_per_million": None}
    )
    return service, catalog, write, model_id


@pytest.mark.parametrize("complete", [True, False])
def test_price_catalog_response_preserves_completeness_and_region_references(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
    complete: bool,
) -> None:
    service, catalog, _write, _model_id = price_edit_service
    references = {
        region: f"azure_retail:Azure OpenAI:gpt 4.1:Regional:{region}"
        for region in ("eastus", "westus")
    }
    model = CatalogModel(
        key="azure_retail:Azure OpenAI:gpt 4.1", label="gpt 4.1",
        product="Azure OpenAI", source=PriceSource.AZURE_RETAIL,
    )
    entry = CatalogEntry(
        reference=references["eastus"], label=model.label, source=model.source,
        input_per_million=2.0, output_per_million=8.0, complete=complete,
    )
    catalog.options.return_value = CatalogOptions(
        model=model, complete=complete,
        options=(CatalogOption(
            reference=entry.reference, deployment="Regional", entry=entry,
            regions=tuple(references), region_required=True, references_by_region=references,
        ),),
    )

    response = service.price_catalog_options(model.key).model_dump(mode="json")

    assert response["complete"] is complete
    assert response["options"][0]["references_by_region"] == references
    assert response["options"][0]["regions"] == ["eastus", "westus"]


@pytest.mark.parametrize("with_range", [False, True])
def test_price_catalog_response_preserves_context_metadata_from_any_source(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
    with_range: bool,
) -> None:
    from turnstile_core.domain.runtime_models import PriceCatalogContextTier
    from turnstile_core.pricing.catalog import ModelSearch

    service, catalog, _write, _model_id = price_edit_service
    metadata = {
        "group_key": "opaque-provider-model", "model_label": "Provider model",
        "service_tier": "flexible", "key": "middle", "label": "Middle band", "order": 1,
        "input_range": (
            {"min_input_tokens": 12346, "max_input_tokens": 98765} if with_range else None
        ),
    }
    model = CatalogModel(
        key="anthropic:Any product:opaque-sku", label="Opaque SKU",
        product="Any product", source=PriceSource.ANTHROPIC,
        context_tier=PriceCatalogContextTier.model_validate(metadata),
    )
    catalog.search_models.return_value = ModelSearch(models=(model,))
    catalog.options.return_value = CatalogOptions(model=model)

    search = service.price_catalog_models("Provider model").model_dump(mode="json")
    details = service.price_catalog_options(model.key).model_dump(mode="json")

    assert search["models"][0]["context_tier"] == metadata
    assert details["model_entry"]["context_tier"] == metadata
    assert details["options"] == []


@pytest.mark.parametrize("band,minimum,maximum", [
    ("ShortCo", 0, 272000), ("LongCo", 272001, None),
])
def test_price_catalog_response_preserves_published_threshold_and_source(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
    band: str, minimum: int, maximum: int | None,
) -> None:
    from turnstile_core.domain.runtime_models import PriceCatalogInputRange, PriceCatalogRuleSource
    from turnstile_core.pricing.catalog import ModelSearch, parse_context_tier

    service, catalog, _write, _model_id = price_edit_service
    label = f"Future Model {band} Std"
    context_tier = parse_context_tier(label)
    assert context_tier is not None
    context_tier.input_range = PriceCatalogInputRange(
        min_input_tokens=minimum, max_input_tokens=maximum,
    )
    context_tier.input_range_source = PriceCatalogRuleSource(
        rule_id="publisher-input-context", url="https://publisher.example/model#pricing",
        revision="sha256:published-document", verified_on="2026-09-21",
    )
    model = CatalogModel(
        key=f"azure_retail:New Product:{label}", label=label,
        product="New Product", source=PriceSource.AZURE_RETAIL,
        context_tier=context_tier,
    )
    catalog.search_models.return_value = ModelSearch(models=(model,))
    catalog.options.return_value = CatalogOptions(model=model)
    search = service.price_catalog_models("Future Model").model_dump(mode="json")
    details = service.price_catalog_options(model.key).model_dump(mode="json")
    descriptor = search["models"][0]["context_tier"]
    assert descriptor == details["model_entry"]["context_tier"]
    assert descriptor["input_range"] == {
        "min_input_tokens": minimum, "max_input_tokens": maximum,
    }
    assert descriptor["input_range_source"] == context_tier.input_range_source.model_dump()


@pytest.mark.parametrize("creating", [False, True])
@pytest.mark.parametrize("failure", ["partial", "missing", "unavailable"])
def test_catalog_price_save_rejects_unusable_prices_without_changing_registry(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
    creating: bool,
    failure: str,
) -> None:
    service, catalog, write, model_id = price_edit_service
    if failure == "partial":
        catalog.lookup.return_value = CatalogEntry(
            reference=write.price_reference or "", label="gpt 4.1",
            source=PriceSource.AZURE_RETAIL, input_per_million=2.0,
            output_per_million=8.0, complete=False,
        )
    elif failure == "missing":
        catalog.lookup.return_value = None
    else:
        catalog.lookup.side_effect = TimeoutError("price source unavailable")
    before = service.registry().models

    with pytest.raises(HTTPException) as caught:
        service.save_model(write, None if creating else model_id)

    assert caught.value.status_code == (503 if failure == "unavailable" else 409)
    assert service.registry().models == before
    assert next(model for model in before if model.id == model_id).cached_cost_per_million == 0.45


@pytest.mark.parametrize("custom_rule", [False, True])
def test_context_prices_save_and_reopen_without_trusting_client_rates(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
    custom_rule: bool,
) -> None:
    from turnstile_core.domain.runtime_models import InvocationUsage, PriceCatalogInputRange
    from turnstile_core.pricing.catalog import parse_context_tier

    service, catalog, write, model_id = price_edit_service
    responses = {}
    references = []
    for band, input_rate, output_rate in [("ShortCo", 4, 20), ("LongCo", 8, 30)]:
        label = f"5.6 sol {band} Std"
        context_tier = parse_context_tier(label)
        assert context_tier is not None
        context_tier.input_range = None if custom_rule else PriceCatalogInputRange(
            min_input_tokens=0 if band == "ShortCo" else 272001,
            max_input_tokens=272000 if band == "ShortCo" else None,
        )
        model = CatalogModel(
            key=f"azure_retail:Azure OpenAI GPT5:{label}", label=label,
            product="Azure OpenAI GPT5", source=PriceSource.AZURE_RETAIL,
            context_tier=context_tier,
        )
        reference = f"{model.key}:Global:*"
        references.append(reference)
        entry = CatalogEntry(
            reference=reference, label=label, source=model.source,
            input_per_million=input_rate, output_per_million=output_rate,
        )
        responses[model.key] = CatalogOptions(model=model, options=(CatalogOption(
            reference=reference, deployment="Global", entry=entry, regions=("eastus",),
            region_required=False,
        ),))
    catalog.options.side_effect = responses.get
    assert isinstance(service._repository, InMemoryRepository)
    next(model for model in service._repository.models if model["id"] == model_id)["enabled"] = True
    values = write.model_dump() | {
        "context_price_references": references, "price_reference": None,
        "price_discount_percent": 80, "input_cost_per_million": 0.000001,
        "enabled": True,
    }
    if custom_rule:
        values["context_price_rule"] = {"short_context_max_tokens": 272000}
    result = service.save_model(ManagedModelWrite.model_validate(values), model_id)
    assert result.context_pricing_supported is True
    saved = next(model for model in result.models if model.id == model_id)
    assert saved.context_price_references == references
    assert [tier.input_price_per_million for tier in saved.context_price_tiers] == [3.2, 6.4]
    assert saved.input_cost_per_million is None
    assert saved.price_reference is not None and ":context:" in saved.price_reference
    assert saved.context_price_tiers[0].max_input_tokens == 272000
    assert saved.context_price_tiers[1].min_input_tokens == 272001
    assert saved.context_price_tiers[0].model_label == "5.6 sol ShortCo Std"
    assert saved.context_price_tiers[1].deployment == "Global"
    assert saved.context_price_tiers[0].regions == ["eastus"]
    if custom_rule:
        assert saved.context_price_rule is not None
        assert saved.context_price_rule.short_context_max_tokens == 272000
        source = saved.context_price_tiers[0].input_range_source
        assert source is not None and source.origin == "custom"
        assert source.configured_at is not None
    price = service._repository.model_prices()[str(model_id)]
    if custom_rule:
        assert price.context_tiers[0].input_range_source == source
    assert price.for_input_tokens(272001).input_price_per_million == 6.4
    route = service._repository.invocation_route(saved.runtime_id, saved.id)
    assert route is not None
    usage = InvocationUsage(
        input_tokens=270000, cached_tokens=2001, output_tokens=100, estimated=False,
    )
    assert service._estimated_cost(route, usage) == price.cost(270000, 2001, 100)
    assert service._estimated_cost(route, usage.model_copy(update={"estimated": True})) is None
    catalog.options.reset_mock()
    catalog.options.side_effect = TimeoutError("offline")
    renamed = ManagedModelWrite.model_validate(
        saved.model_dump(include=set(ManagedModelWrite.model_fields)) | {"display_name": "Renamed"}
    )
    result = service.save_model(renamed, model_id)
    reopened = next(model for model in result.models if model.id == model_id)
    assert reopened.context_price_tiers == saved.context_price_tiers
    catalog.options.assert_not_called()


def test_single_context_entry_cannot_be_saved_as_a_flat_price(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
) -> None:
    service, catalog, write, model_id = price_edit_service
    catalog.lookup.return_value = CatalogEntry(
        reference=write.price_reference or "", label="Any model ShortCo Std",
        source=PriceSource.AZURE_RETAIL, input_per_million=1, output_per_million=4,
    )
    before = service.registry().models
    with pytest.raises(HTTPException) as error:
        service.save_model(write, model_id)
    assert error.value.status_code == 409
    assert service.registry().models == before


def test_complete_catalog_price_can_be_saved(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
) -> None:
    service, catalog, write, model_id = price_edit_service

    result = service.save_model(write.model_copy(update={
        "input_cost_per_million": 0.000001, "output_cost_per_million": 99999,
    }), model_id)

    saved = next(model for model in result.models if model.id == model_id)
    assert saved.price_source is PriceSource.AZURE_RETAIL
    assert saved.cached_cost_per_million == 0.45
    catalog.lookup.assert_called_once_with(write.price_reference)


def test_non_pricing_edit_does_not_require_an_available_catalog(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
) -> None:
    service, catalog, write, model_id = price_edit_service
    accepted = service.save_model(write, model_id)
    saved = next(model for model in accepted.models if model.id == model_id)
    write = ManagedModelWrite.model_validate(
        saved.model_dump(include=set(ManagedModelWrite.model_fields)),
    )
    catalog.lookup.reset_mock()
    catalog.lookup.side_effect = TimeoutError("price source unavailable")

    result = service.save_model(write.model_copy(update={"display_name": "Renamed"}), model_id)

    assert next(model for model in result.models if model.id == model_id).display_name == "Renamed"
    catalog.lookup.assert_not_called()


def test_manual_price_save_does_not_read_the_catalog(
    price_edit_service: tuple[ModelRuntimeService, Mock, ManagedModelWrite, UUID],
) -> None:
    service, catalog, write, model_id = price_edit_service
    catalog.lookup.side_effect = TimeoutError("price source unavailable")
    manual = write.model_copy(update={
        "price_source": PriceSource.MANUAL, "price_reference": None,
        "input_cost_per_million": 7.0,
    })

    result = service.save_model(manual, model_id)

    saved = next(model for model in result.models if model.id == model_id)
    assert saved.input_cost_per_million == 7.0
    catalog.lookup.assert_not_called()


def test_registry_redacts_secret_and_invocation_records_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert json.loads(incoming.content)["model"] == "gpt-4.1"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "SERVICE_OK"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )

    repository = InMemoryRepository()
    settings = Settings(credential_key_file=tmp_path / "credential.key")
    service = ModelRuntimeService(
        repository,
        settings,
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )
    gateway_id = UUID("10000000-0000-4000-8000-000000000001")
    service.save_gateway(
        GatewayProfileWrite(
            name="Azure API Management",
            implementation=GatewayKind.APIM,
            base_url="https://gateway.test",
            auth_type=AuthType.BEARER,
            credential=SecretStr("gateway-secret-9876"),
            enabled=True,
            is_default=True,
        ),
        gateway_id,
    )
    provider_id = UUID("20000000-0000-4000-8000-000000000003")
    runtime_id = UUID("30000000-0000-4000-8000-000000000003")
    model_id = UUID("40000000-0000-4000-8000-000000000003")
    next(model for model in repository.models if model["id"] == model_id)["enabled"] = True
    service.save_runtime(
        RuntimeWrite(
            provider_id=provider_id,
            gateway_profile_id=gateway_id,
            name="Foundry Test",
            runtime_kind=RuntimeKind.FOUNDRY,
            enabled=True,
            is_default=True,
            config={"path": "/v1/chat/completions"},
        ),
        runtime_id,
    )
    service.save_model(
        ManagedModelWrite(
            provider_id=provider_id,
            runtime_id=runtime_id,
            model_key="gpt-4.1-alias",
            display_name="GPT-4.1",
            upstream_model_id="gpt-4.1",
            enabled=True,
            is_default=True,
            input_cost_per_million=2,
            output_cost_per_million=8,
        ),
        model_id,
    )

    registry = service.registry()
    gateway = next(item for item in registry.gateways if item.id == gateway_id)
    assert gateway.credential_configured is True
    assert gateway.credential_hint == "...9876"
    assert "gateway-secret" not in gateway.model_dump_json()
    stored = next(item for item in repository.gateways if item["id"] == gateway_id)
    ciphertext = stored["credential_ciphertext"]
    assert isinstance(ciphertext, bytes)
    assert b"gateway-secret" not in ciphertext

    response = service.invoke(request())
    assert response.content == "SERVICE_OK"
    assert response.correlation_id is None
    assert response.request_id in repository.invocation_attempts
    assert response.gateway == "apim"
    assert response.estimated_cost == 0.00008
    assert repository.usage_records == []

    def fail_telemetry(_: object) -> None:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(repository, "write_token_usage", fail_telemetry)
    assert service.invoke(request()).content == "SERVICE_OK"


def test_apim_invocation_does_not_fallback_when_registry_url_is_empty(
    tmp_path: Path,
) -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        assert str(incoming.url) == "https://gateway.test/turnstile/llm/chat/completions"
        assert incoming.headers["Ocp-Apim-Subscription-Key"] == "dashboard-key"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "CONFIGURED_GATEWAY_OK"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    repository = InMemoryRepository()
    repository.gateways[0]["base_url"] = None
    service = ModelRuntimeService(
        repository,
        Settings(
            apim_gateway_url="https://gateway.test/turnstile/llm",
            apim_dashboard_subscription_key=SecretStr("dashboard-key"),
            credential_key_file=tmp_path / "credential.key",
        ),
        GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )

    invocation = request(
        model="gpt-5.6-luna", runtime="Microsoft Foundry via APIM"
    ).model_copy(
        update={
            "runtime_id": UUID("30000000-0000-4000-8000-000000000003"),
            "model_id": UUID("9b45b7e1-403d-4c6c-a877-5dc77775b911"),
        }
    )

    with pytest.raises(HTTPException) as error:
        service.invoke(invocation)
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "mode", ["trusted", "inventory", "managed", "untrusted", "empty", "disabled",
             "provisioned", "approval_mismatch"],
)
def test_invocation_uses_only_a_trusted_serving_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    registered = target(
        api_path="turnstile/llm", drift_state="in_sync",
        last_inventory_sync_at=datetime(2026, 9, 23, tzinfo=UTC),
        capabilities={"self_hosted_gateway": "supported"},
    )
    request_id = UUID(int=72)
    native_id = "managed" if mode == "managed" else self_hosted_gateway_native_id_for(
        registered.gateway_profile_id, request_id,
    )
    endpoint = "https://unit-service.azure-api.net/turnstile/llm" if mode == "managed" else (
        "https://edge.test/turnstile/llm"
    )
    deployment_id = gateway_deployment_id_for(registered.gateway_profile_id, native_id)
    sent: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        sent.append(incoming)
        assert str(incoming.url) == endpoint + "/chat/completions"
        assert incoming.headers["Ocp-Apim-Subscription-Key"] == "dashboard-key"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "EDGE_OK"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        })

    repository = InMemoryRepository()
    model_id = UUID("9b45b7e1-403d-4c6c-a877-5dc77775b911")
    runtime_id = UUID("30000000-0000-4000-8000-000000000003")
    route = repository.invocation_route(runtime_id, model_id)
    assert route is not None
    selected = deployment(
        id=deployment_id, native_gateway_id=native_id, endpoint_url=endpoint,
        kind="managed" if mode == "managed" else "self_hosted",
        hosting_mode="azure_managed" if mode == "managed" else "docker",
        declared_state="present", last_arm_sync_at=datetime(2026, 9, 23, tzinfo=UTC),
        provisioning_operation_id=UUID(int=73)
        if mode in {"provisioned", "approval_mismatch"} else None,
    )
    inventory: dict[str, Any] = {
        "associated_api_ids": [registered.api_id],
        "hostname_configurations": [{"hostname": "edge.test"}] if mode == "inventory" else [],
    }
    if mode in {"provisioned", "approval_mismatch"}:
        spec = SelfHostedGatewayProvisionSpec(
            request_id=request_id, target=registered, native_gateway_id=native_id,
            deployment_id=deployment_id,
            configuration=SelfHostedGatewayCreate(
                display_name="Unit edge", endpoint_url=HttpUrl(endpoint),
                endpoint_control_confirmed=True,
            ),
        )
        inventory["provisioning_operation"] = {
            "id": selected.provisioning_operation_id,
            "gateway_profile_id": registered.gateway_profile_id,
            "operation_kind": "self_hosted_gateway_provision",
            "status": "succeeded",
            "semantic_preview": spec.model_dump(mode="json"),
            "confirmation_sha256": gateway_provision_digest(spec)
            if mode == "provisioned"
            else "f" * 64,
        }
    route.update(
        gateway_id=registered.gateway_profile_id, gateway_base_url=endpoint,
        gateway_deployment_id=deployment_id, native_gateway_id=native_id,
        gateway_target=registered.model_dump(mode="python"),
        gateway_deployment=None if mode == "empty" else selected.model_dump(mode="python"),
        gateway_inventory=inventory,
    )
    monkeypatch.setattr(repository, "invocation_route", Mock(return_value=route))
    settings = Settings(
        credential_key_file=tmp_path / "key",
        apim_target_registry_enabled=True,
        self_hosted_gateway_enabled=mode != "disabled",
        self_hosted_gateway_trusted_endpoints={deployment_id: HttpUrl(endpoint)}
        if mode in {"trusted", "disabled"} else {},
        apim_dashboard_subscription_key=SecretStr("dashboard-key"),
    )
    service = ModelRuntimeService(
        repository, settings, GatewayRouter(httpx.Client(transport=httpx.MockTransport(handler))),
    )
    route.update(
        gateway_auth_type="api_key",
        gateway_config={"header_name": "Ocp-Apim-Subscription-Key"},
        gateway_credential_ciphertext=service._cipher.encrypt("dashboard-key"),
    )
    invocation = request().model_copy(update={"model_id": model_id, "runtime_id": runtime_id})
    if mode in {"untrusted", "empty", "disabled", "approval_mismatch"}:
        decrypt = Mock(side_effect=AssertionError("Credentials must not be decrypted"))
        monkeypatch.setattr(service, "_decrypt_route", decrypt)
        with pytest.raises(HTTPException) as caught:
            service.invoke(invocation)
        assert caught.value.status_code == 503
        decrypt.assert_not_called()
        assert sent == []
    else:
        assert service.invoke(invocation).content == "EDGE_OK"
        assert len(sent) == 1


def test_apim_registry_excludes_github_copilot_telemetry_identity() -> None:
    repository = InMemoryRepository()
    service = ModelRuntimeService(repository, Settings())
    copilot_provider = next(
        provider
        for provider in repository.providers
        if provider["provider_kind"] == ProviderKind.GITHUB
    )
    foundry_runtime = next(
        runtime
        for runtime in repository.runtimes
        if runtime["runtime_kind"] == RuntimeKind.FOUNDRY
    )
    github_bound_runtime_id = UUID("30000000-0000-4000-8000-000000000099")
    repository.runtimes.append(
        {
            **foundry_runtime,
            "id": github_bound_runtime_id,
            "provider_id": copilot_provider["id"],
            "provider_name": copilot_provider["name"],
            "name": "GitHub-bound Foundry runtime",
        }
    )
    copilot_family_model_id = UUID("40000000-0000-4000-8000-000000000099")
    repository.models.append(
        {
            **repository.models[0],
            "id": copilot_family_model_id,
            "model_key": "copilot-family-model",
            "display_name": "Copilot family model",
            "family_key": ModelFamilyKey.COPILOT,
        }
    )

    registry = service.registry()

    assert all(provider.provider_kind != ProviderKind.GITHUB for provider in registry.providers)
    assert all(runtime.runtime_kind != RuntimeKind.COPILOT_CLI for runtime in registry.runtimes)
    assert {model.provider_id for model in registry.models} <= {
        provider.id for provider in registry.providers
    }
    assert {model.runtime_id for model in registry.models} <= {
        runtime.id for runtime in registry.runtimes
    }
    assert ProviderKind.MICROSOFT_FOUNDRY in {
        provider.provider_kind for provider in registry.providers
    }
    assert github_bound_runtime_id not in {runtime.id for runtime in registry.runtimes}
    assert copilot_family_model_id not in {model.id for model in registry.models}
    assert any(
        runtime["runtime_kind"] == RuntimeKind.COPILOT_CLI
        for runtime in repository.runtimes
    )

    with pytest.raises(HTTPException, match="separate data source"):
        service.save_provider(
            ProviderWrite(name="GitHub Copilot", provider_kind=ProviderKind.GITHUB)
        )
    with pytest.raises(HTTPException, match="separate data source"):
        service.save_runtime(
            RuntimeWrite(
                provider_id=copilot_provider["id"],
                name="GitHub-bound runtime",
                runtime_kind=RuntimeKind.FOUNDRY,
            )
        )
    with pytest.raises(HTTPException, match="separate data source"):
        service.save_model(
            ManagedModelWrite(
                provider_id=foundry_runtime["provider_id"],
                runtime_id=foundry_runtime["id"],
                model_key="copilot-family-model",
                display_name="Copilot family model",
                family_key=ModelFamilyKey.COPILOT,
            )
        )


def test_production_management_key_only_protects_mutations(tmp_path: Path) -> None:
    repository = InMemoryRepository()
    settings = Settings(
        production=True,
        management_api_key=SecretStr("management-secret"),
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
        credential_key_file=tmp_path / "credential.key",
    )
    service = ModelRuntimeService(repository, settings)
    service.authorize("member", None, manage=False)
    with pytest.raises(Exception) as error:
        service.authorize("owner", None, manage=True)
    assert getattr(error.value, "status_code", None) == 401
    service.authorize("owner", "Bearer management-secret", manage=True)


def test_in_memory_apim_seed_separates_public_and_provider_paths() -> None:
    repository = InMemoryRepository()
    runtime = next(
        item
        for item in repository.runtimes
        if item["name"] == "Microsoft Foundry via APIM"
    )

    assert runtime["gateway_profile_id"] is not None
    assert runtime["config"]["path"] == "/chat/completions"
    assert runtime["config"]["backend_path"] == "/openai/v1/chat/completions"
    assert runtime["config"]["api_format"] == "openai_chat"
