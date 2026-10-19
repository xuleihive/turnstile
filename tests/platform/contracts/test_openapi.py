"""Keeps `contracts/openapi.yaml` honest about the routes the app actually serves.

Contract drift fails nothing: the app keeps working, the linter still passes, and the
document quietly starts describing a different API. The assistant surface reached nine
operations before anyone noticed it was absent from the contract entirely, which is what
this test exists to prevent happening again.

Only paths and methods are compared. Asserting every schema field against the generated
document would fail on formatting differences that mean nothing -- FastAPI emits `anyOf`
where the hand-written contract says `type: [string, 'null']` -- and a test that has to be
loosened after each edit stops being read.

PyYAML is a pinned dev dependency rather than an optional import: a guard that skips when
something is missing reports success while checking nothing.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

from backend.api import app
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.domain.runtime_models import ModelVendorKey

CONTRACT = REPOSITORY_ROOT / "contracts" / "openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}

# Documented deliberately as internal, or served by something other than this app.
UNDOCUMENTED = {
    "/health",
    "/{path:path}",
}


def _operations(paths: dict[str, dict[str, object]]) -> set[tuple[str, str]]:
    return {(path, method) for path, item in paths.items() for method in item if method in METHODS}


@cache
def _yaml_document(path: Path) -> dict[str, object]:
    return cast(
        dict[str, object],
        yaml.safe_load(path.read_text(encoding="utf-8")),
    )


def _resolve_local_ref(ref: str) -> object:
    file_name, fragment = ref.split("#", 1)
    value: object = _yaml_document((CONTRACT.parent / file_name).resolve())
    for raw_part in fragment.removeprefix("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        value = cast(dict[str, object], value)[part]
    return value


def _resolve_index(values: dict[str, object]) -> dict[str, dict[str, object]]:
    def canonicalize(value: object) -> object:
        if isinstance(value, str):
            return value.removeprefix("../../openapi.yaml")
        if isinstance(value, list):
            return [canonicalize(item) for item in value]
        if isinstance(value, dict):
            return {key: canonicalize(item) for key, item in value.items()}
        return value

    resolved: dict[str, dict[str, object]] = {}
    for name, value in values.items():
        item = cast(dict[str, object], value)
        if set(item) == {"$ref"}:
            item = cast(dict[str, object], _resolve_local_ref(cast(str, item["$ref"])))
        resolved[name] = cast(dict[str, object], canonicalize(item))
    return resolved


def _documented_paths() -> dict[str, dict[str, object]]:
    document = _yaml_document(CONTRACT.resolve())
    return _resolve_index(cast(dict[str, object], document["paths"]))


def _documented_schemas() -> dict[str, dict[str, object]]:
    document = _yaml_document(CONTRACT.resolve())
    components = cast(dict[str, object], document["components"])
    return _resolve_index(cast(dict[str, object], components["schemas"]))


def _schema_contract(value: object) -> object:
    if isinstance(value, list):
        return [_schema_contract(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _schema_contract(item)
        for key, item in value.items() if key not in {"title", "description"}
    }
    types = result.get("type")
    if isinstance(types, list):
        result = {"anyOf": [{**result, "type": kind} for kind in types]}
    variants = result.get("anyOf")
    if isinstance(variants, list):
        result["anyOf"] = sorted(variants, key=lambda item: json.dumps(item, sort_keys=True))
    return result


def test_execution_event_has_bounded_receipts_and_default_off_completion() -> None:
    schema = _documented_schemas()["UsageEvent"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    receipts = properties["budget_execution"]
    assert receipts["maxItems"] == 2 and receipts["default"] == []
    item = cast(dict[str, object], receipts["items"])
    assert item["additionalProperties"] is False
    assert set(cast(list[str], item["required"])) == {"identity", "epoch", "reserved_tokens"}
    fields = cast(dict[str, dict[str, object]], item["properties"])
    assert fields["reserved_tokens"]["minimum"] == 1
    assert set(cast(list[str], fields["identity"]["required"])) == {
        "scope", "gateway_profile_id", "native_gateway_id", "correlation_id", "model_id",
        "admitted_at",
    }
    assert properties["usage_complete"]["default"] is False
    assert properties["native_gateway_id"]["maxLength"] == 80
    assert "cache_write_tokens" in properties


def test_openapi_root_is_a_small_domain_index() -> None:
    root = _yaml_document(CONTRACT.resolve())
    paths = cast(dict[str, dict[str, str]], root["paths"])
    components = cast(dict[str, object], root["components"])
    schemas = cast(dict[str, dict[str, str]], components["schemas"])

    assert len(CONTRACT.read_text(encoding="utf-8").splitlines()) < 120 + 2 * (
        len(paths) + len(schemas)
    )
    assert set(paths) == set(app.openapi()["paths"]) - UNDOCUMENTED
    assert set(app.openapi()["components"]["schemas"]) <= set(schemas)
    assert all(set(value) == {"$ref"} for value in paths.values())
    assert all(set(value) == {"$ref"} for value in schemas.values())
    assert {path.name for path in (CONTRACT.parent / "openapi" / "paths").glob("*.yaml")} == {
        "application-access.yaml",
        "assistant.yaml",
        "authentication.yaml",
        "budgets.yaml",
        "github-copilot.yaml",
        "model-platform.yaml",
        "observability.yaml",
    }


def test_assistant_protocol_settings_match_runtime_schemas() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    for name in (
        "AssistantSettingsWrite", "AssistantSettings", "AssistantModelChoice",
        "InvocationApiFormat",
    ):
        assert _schema_contract(documented[name]) == _schema_contract(actual[name]), name


def test_model_availability_contract_matches_runtime_constraints_and_owner_routes() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]

    def normalize(value: object) -> object:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: {name: normalize(schema) for name, schema in item.items()}
            if key == "properties" and isinstance(item, dict) else normalize(item)
            for key, item in value.items() if key not in {"title", "description"}
        }

    names = {name for name in actual if name.startswith("ModelAvailability")}
    assert len(names) == 9
    for name in names:
        assert normalize(documented[name]) == normalize(actual[name]), name
    for path, methods in (
        ("/api/v1/model-management/model-availability", ("get",)),
        ("/api/v1/model-management/models/{item_id}/availability", ("get", "put")),
        ("/api/v1/model-management/model-availability/operations/{operation_id}", ("get",)),
        ("/api/v1/model-management/model-availability/operations/{operation_id}/retry", ("post",)),
    ):
        for method in methods:
            operation = cast(dict[str, object], _documented_paths()[path][method])
            assert operation["security"] == [{"sessionCookie": []}]
            responses = cast(dict[str, dict[str, object]], operation["responses"])
            accepted = responses["200" if method == "get" else "202"]
            headers = cast(dict[str, dict[str, object]], accepted["headers"])
            assert headers["Cache-Control"]["schema"] == {"type": "string", "const": "no-store"}


def test_budget_diagnostics_contract_matches_public_fields_and_owner_routes() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    for name in (
        "BudgetDelegationCheck", "BudgetDelegationReadiness", "BudgetExecutionVerificationOptions",
        "BudgetExecutionVerificationRequest", "BudgetExecutionVerification",
        "BudgetExecutionScopeReadback",
        "LedgerScope", "LedgerSummary", "LedgerAttemptIdentity", "LedgerAttempt",
    ):
        assert set(cast(dict[str, object], documented[name]["properties"])) == set(
            actual[name]["properties"],
        )
        assert documented[name]["additionalProperties"] is False
    for suffix in ("budget-readiness", "budget-execution-verification"):
        path = f"/api/v1/model-management/gateways/{{gateway_profile_id}}/{suffix}"
        route = _documented_paths()[path]
        assert cast(dict[str, object], route["post"])["security"] == [{"sessionCookie": []}]


def test_invocation_contract_matches_runtime_identity_tools_and_usage() -> None:
    document = _yaml_document((CONTRACT.parent / "model-gateway.openapi.yaml").resolve())
    components = cast(dict[str, object], document["components"])
    documented = cast(dict[str, dict[str, object]], components["schemas"])
    actual = app.openapi()["components"]["schemas"]

    def normalize(value: object) -> object:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, object] = {}
        for key, item in value.items():
            if key == "properties" and isinstance(item, dict):
                result[key] = {name: normalize(schema) for name, schema in item.items()}
            elif key not in {"title", "description"}:
                result[key] = normalize(item)
        declared_types = result.get("type")
        if isinstance(declared_types, list):
            nullable_variants = [
                {"type": "null"} if kind == "null" else {**result, "type": kind}
                for kind in declared_types
            ]
            return {"anyOf": sorted(
                nullable_variants, key=lambda item: json.dumps(item, sort_keys=True),
            )}
        if "oneOf" in result:
            result["anyOf"] = result.pop("oneOf")
        variants = result.get("anyOf")
        if isinstance(variants, list):
            result["anyOf"] = sorted(variants, key=lambda item: json.dumps(item, sort_keys=True))
        return result

    for name in (
        "InvocationMetadata", "ToolFunctionDefinition", "ToolDefinition", "ToolFunctionCall",
        "ToolCall", "ChatMessage", "ModelInvocationRequest", "InvocationUsage",
        "ModelInvocationResponse",
    ):
        described = dict(documented[name])
        described.pop("allOf", None)
        assert normalize(described) == normalize(actual[name]), name


def test_platform_invocation_trace_contract_preserves_unknown_and_explicit_identity() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    fields = cast(dict[str, dict[str, object]], documented["UsageRequestSummary"]["properties"])
    assert set(fields) == set(actual["UsageRequestSummary"]["properties"])
    assert fields["correlation_id"]["type"] == ["string", "null"]
    assert fields["status_code"]["type"] == ["integer", "null"]
    assert fields["usage_available"]["default"] is True
    for path in ("/api/v1/observability/requests", "/api/v1/observability/requests/{request_id}"):
        assert "get" in _documented_paths()[path]


def test_image_invocation_contract_requires_a_complete_gateway_selection() -> None:
    schema = _documented_schemas()["ImageInvocationRequest"]
    conditions = cast(list[dict[str, object]], schema["allOf"])
    selections = (
        ("gateway_profile_id", "gateway_deployment_id"),
        ("gateway_deployment_id", "gateway_profile_id"),
    )
    for (selected, counterpart), condition in zip(selections, conditions, strict=True):
        assert condition["if"] == {
            "required": [selected], "properties": {selected: {"type": "string"}},
        }
        assert condition["then"] == {
            "required": [counterpart],
            "properties": {counterpart: {"type": "string", "format": "uuid"}},
        }
    fields = cast(dict[str, dict[str, object]], schema["properties"])
    assert fields["n"]["const"] == 1
    assert fields["stream"]["const"] is False


def test_runtime_diagnostic_contract_has_explicit_selection_and_owner_routes() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    for name in ("RuntimeCheckSelection", "RuntimeCheckOption", "RuntimeCheckPlan",
                 "RuntimeDiagnosticAccess", "RuntimeHealth"):
        assert set(cast(dict[str, object], documented[name]["properties"])) == set(
            actual[name]["properties"],
        )
        assert documented[name]["additionalProperties"] is False
    plan = cast(dict[str, dict[str, object]], documented["RuntimeCheckPlan"]["properties"])
    assert plan["max_output_tokens"]["const"] == 64
    assert plan["request_limit"]["const"] == 1
    path = _documented_paths()["/api/v1/model-management/runtimes/{runtime_id}/check"]
    for method in ("get", "post"):
        operation = cast(dict[str, object], path[method])
        assert operation["security"] == [{"sessionCookie": []}]
        assert method in app.openapi()["paths"][
            "/api/v1/model-management/runtimes/{runtime_id}/check"
        ]


def test_publication_failure_contract_matches_safe_response_fields() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]["GatewayPublicationFailure"]
    failure = documented["GatewayPublicationFailure"]
    properties = cast(dict[str, dict[str, object]], failure["properties"])
    assert set(properties) == set(actual["properties"])
    assert properties["code"]["enum"] == actual["properties"]["code"]["enum"]
    assert failure["additionalProperties"] is False
    for name in ("GatewayPublication", "GatewayReleaseSummary"):
        assert "failure" in cast(dict[str, object], documented[name]["properties"])


def test_gateway_target_contract_matches_actual_structures_and_owner_operations() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    generated = _yaml_document(CONTRACT.parent / "openapi/schemas/gateway-targets.yaml")
    for name in cast(dict[str, object], generated["schemas"]):
        assert _schema_contract(documented[name]) == _schema_contract(actual[name]), name
    write = documented["ApimTargetWrite"]
    assert write["additionalProperties"] is False
    publication = documented["GatewayPublicationCreate"]
    assert "deployment_scope" not in cast(list[str], publication["required"])
    publication_properties = cast(dict[str, object], publication["properties"])
    assert publication_properties["deployment_scope"] == {
        "$ref": "#/components/schemas/GatewayDeploymentScope",
    }
    paths = _documented_paths()
    registration = paths["/api/v1/model-management/apim-targets"]["post"]
    assert cast(dict[str, object], registration)["security"] == [{"sessionCookie": []}]
    sync = paths["/api/v1/model-management/apim-targets/{gateway_profile_id}/inventory-syncs"]
    operation = cast(dict[str, object], sync["post"])
    assert "202" in cast(dict[str, object], operation["responses"])


def test_onboarding_contract_has_exact_models_and_durable_owner_operations() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    for name in ("ApimOnboardingRequest", "ApimOnboardingPlan", "ApimOnboardingStart",
                 "ApimOnboardingOperations"):
        assert _schema_contract(documented[name]) == _schema_contract(actual[name]), name
    paths = _documented_paths()
    root = "/api/v1/model-management/apim-onboarding"
    for path, method, response in (
        (root, "get", "200"), (root, "post", "202"),
        (root + "/preview", "post", "200"), (root + "/{operation_id}/retry", "post", "202"),
    ):
        operation = cast(dict[str, object], paths[path][method])
        assert operation["security"] == [{"sessionCookie": []}]
        assert "Owner" in str(operation["description"])
        assert response in cast(dict[str, object], operation["responses"])
    properties = cast(
        dict[str, dict[str, object]], documented["GatewayReleaseOperation"]["properties"],
    )
    operation_kind = properties["operation_kind"]
    if "$ref" in operation_kind:
        operation_kind = documented[cast(str, operation_kind["$ref"]).rsplit("/", 1)[-1]]
    assert "apim_onboarding" in cast(list[str], operation_kind["enum"])


def test_apim_workspace_contract_preserves_owner_lifecycle_and_capabilities() -> None:
    paths = _documented_paths()
    root = "/api/v1/model-management/apim-targets/{gateway_profile_id}"
    for path, method in ((root, "delete"), (root + "/removal-preview", "get")):
        operation = cast(dict[str, object], paths[path][method])
        assert operation["security"] == [{"sessionCookie": []}]
        assert "Owner" in str(operation["description"])
        assert "200" in cast(dict[str, object], operation["responses"])
    removal = cast(dict[str, object], paths[root]["delete"])
    assert "No Azure resource or container is deleted or modified" in str(removal["description"])
    for path, method in (
        ("/api/v1/model-management/apim-targets", "post"),
        ("/api/v1/model-management/apim-onboarding", "post"),
        ("/api/v1/model-management/apim-onboarding/preview", "post"),
        ("/api/v1/model-management/apim-onboarding/{operation_id}/retry", "post"),
        ("/api/v1/model-management/self-hosted-gateways/{deployment_id}/validations", "post"),
    ):
        operation = cast(dict[str, object], paths[path][method])
        assert operation["security"] == [{"sessionCookie": []}]
        assert "configured management authorization" not in str(operation["description"])
    properties = cast(
        dict[str, dict[str, object]], _documented_schemas()["ModelRegistry"]["properties"],
    )
    for name in ("apim_target_topology_available", "self_hosted_gateway_enabled"):
        assert properties[name]["type"] == "boolean"
        assert properties[name]["default"] is False


def test_self_hosted_lifecycle_contract_preserves_minimal_names_and_metadata_boundaries() -> None:
    paths = _documented_paths()
    lifecycle = [(path, item) for path, item in paths.items() if "self-hosted-gateways" in path]
    assert len(lifecycle) == 8
    for _, item in lifecycle:
        for operation in item.values():
            values = cast(dict[str, object], operation)
            assert values["security"] == [{"sessionCookie": []}]
            assert "Owner" in str(values["description"])
    root = "/api/v1/model-management/self-hosted-gateways/{deployment_id}"
    rename = cast(dict[str, object], paths[root + "/name"]["patch"])
    responses = cast(dict[str, dict[str, object]], rename["responses"])
    assert "content" not in responses["204"]
    removal = cast(dict[str, object], paths[root]["delete"])
    assert "No Azure resource or container is deleted" in str(removal["description"])


def test_gateway_creation_contract_requires_idempotency_and_protects_bootstrap() -> None:
    paths = _documented_paths()
    schemas = _documented_schemas()
    root = "/api/v1/model-management"
    create = cast(dict[str, object], paths[
        root + "/apim-targets/{gateway_profile_id}/self-hosted-gateway-provisions"
    ]["post"])
    parameters = cast(list[dict[str, object]], create["parameters"])
    key = next(item for item in parameters if item["name"] == "Idempotency-Key")
    assert key["required"] is True and key["in"] == "header"
    assert create["security"] == [{"sessionCookie": []}]
    properties = cast(dict[str, object], schemas["SelfHostedGatewayCreate"]["properties"])
    assert "native_gateway_id" not in properties
    assert properties["endpoint_control_confirmed"] == {
        "type": "boolean", "const": True, "title": "Endpoint Control Confirmed",
    }
    bootstrap = cast(dict[str, object], paths[
        root + "/self-hosted-gateways/{deployment_id}/bootstrap"
    ]["post"])
    responses = cast(dict[str, dict[str, object]], bootstrap["responses"])
    headers = cast(dict[str, dict[str, object]], responses["200"]["headers"])
    assert headers["Cache-Control"]["schema"] == {"type": "string", "const": "no-store"}
    assert bootstrap["security"] == [{"sessionCookie": []}]


def test_multi_target_contract_matches_runtime_and_never_exposes_child_secrets() -> None:
    documented = _documented_schemas()
    actual = app.openapi()["components"]["schemas"]
    for name in ("MultiTargetPublicationCreate", "MultiTargetPublicationChildCreate"):
        assert documented[name] == actual[name]
    accepted = actual["MultiTargetPublicationRequestAccepted"]
    assert _schema_contract(documented["MultiTargetPublicationRequestAccepted"]) == (
        _schema_contract(accepted)
    )
    publication_view = documented["GatewayPublicationView"]
    assert not {"credential_ciphertext", "api_key", "desired_spec"} & set(
        cast(dict[str, object], publication_view["properties"]),
    )
    paths = _documented_paths()
    root = "/api/v1/model-management/rollouts"
    assert set(paths[root]) == {"get", "post"}
    for route in (root, root + "/{rollout_id}"):
        for operation in paths[route].values():
            values = cast(dict[str, object], operation)
            assert values["security"] == [{"sessionCookie": []}]
            assert "Owner" in str(values["description"])


def test_gateway_name_contract_preserves_owner_only_name_write_and_minimal_response() -> None:
    schema = _documented_schemas()["GatewayProfileNameWrite"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert set(properties) == {"name"} and properties["name"]["maxLength"] == 120
    operation = cast(dict[str, object], _documented_paths()[
        "/api/v1/model-management/gateways/{item_id}/name"
    ]["patch"])
    assert operation["security"] == [{"sessionCookie": []}]
    responses = cast(dict[str, dict[str, object]], operation["responses"])
    assert {"200", "204", "401", "403", "404", "422"} <= set(responses)
    assert "content" not in responses["204"]
    actual = app.openapi()["components"]["schemas"]
    assert actual["GatewayProfileNameWrite"]["additionalProperties"] is False
    assert "gateway_name_edit_supported" in actual["RegistryResponse"]["properties"]


def test_publication_contract_describes_catalog_prices_and_discounts() -> None:
    schema = _documented_schemas()["GatewayModelTarget"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert properties["price_source"]["enum"] == ["manual", "azure_retail", "anthropic"]
    assert properties["price_source"]["default"] == "manual"
    assert properties["price_reference"]["maxLength"] == 1000
    assert properties["price_discount_percent"]["exclusiveMinimum"] == 0
    assert properties["price_discount_percent"]["maximum"] == 100
    assert properties["price_discount_percent"]["type"] == ["number", "null"]
    conditions = cast(list[dict[str, dict[str, object]]], schema["allOf"])
    image = cast(dict[str, dict[str, object]], conditions[0]["then"]["properties"])
    assert image["price_source"]["const"] == "manual"
    for condition, source in zip(conditions[1:3], ("azure_retail", "anthropic"), strict=True):
        assert condition["if"]["required"] == ["price_source"]
        alternatives = cast(list[dict[str, object]], condition["then"]["anyOf"])
        assert alternatives[0]["required"] == ["price_reference"]
        assert alternatives[1]["required"] == ["context_price_references"]
        fields = cast(dict[str, dict[str, object]], alternatives[0]["properties"])
        assert fields["price_reference"] == {"type": "string", "pattern": f"^{source}:"}


def test_custom_context_rule_contract_matches_strict_runtime_fields() -> None:
    from turnstile_core.domain.models import UsageRequestDetail, UsageRequestSummary
    from turnstile_core.domain.runtime_models import ContextPriceRule

    schemas = _documented_schemas()
    rule = schemas["ContextPriceRule"]
    actual = ContextPriceRule.model_json_schema()
    assert rule["additionalProperties"] is False
    assert rule["required"] == actual["required"] == ["short_context_max_tokens"]
    fields = cast(dict[str, dict[str, object]], rule["properties"])
    assert fields["short_context_max_tokens"]["maximum"] == (
        actual["properties"]["short_context_max_tokens"]["maximum"]
    )
    assert set(fields) == set(actual["properties"])
    registry = cast(dict[str, dict[str, object]], schemas["ModelRegistry"]["properties"])
    assert registry["context_price_rules_supported"]["default"] is False
    assert "cost_available" in UsageRequestSummary.model_fields
    assert {"input_price_per_million", "cached_price_per_million",
            "cache_write_price_per_million", "output_price_per_million",
            "price_context_tier", "price_context_snapshot"} <= set(UsageRequestDetail.model_fields)


def test_databricks_contract_describes_workspace_authentication_and_owned_adoption() -> None:
    schemas = _documented_schemas()
    connection = schemas["ModelConnectionCreate"]
    properties = cast(dict[str, dict[str, object]], connection["properties"])
    assert properties["databricks_workspace_url"]["format"] == "uri"
    assert properties["oauth_client_id"]["format"] == "uuid"
    assert "oauth_m2m" in cast(list[str], properties["auth_mode"]["enum"])
    assert len(cast(list[object], connection["oneOf"])) == 4
    for schema_name in (
        "GatewayRuntimeTarget", "GatewayPublicationRetry", "GatewayCredentialRotation",
    ):
        fields = cast(dict[str, dict[str, object]], schemas[schema_name]["properties"])
        assert fields["oauth_client_secret"]["writeOnly"] is True
        assert fields["oauth_client_secret"]["maxLength"] == 4096
    registry = cast(dict[str, dict[str, object]], schemas["ModelRegistry"]["properties"])
    assert registry["databricks_connections_supported"]["default"] is False
    assert registry["databricks_oauth_supported"]["default"] is False
    path = "/api/v1/model-management/connections/{runtime_id}/adopt"
    operation = cast(dict[str, object], _documented_paths()[path]["post"])
    assert operation["security"] == [{"sessionCookie": []}]
    assert (path, "post") in _operations(app.openapi()["paths"])
    mi = cast(
        dict[str, dict[str, object]], schemas["DatabricksAuthorizationRequirement"]["properties"],
    )
    assert mi["kind"]["const"] == "databricks_workspace" and mi["role_name"]["const"] == "CAN_QUERY"


def test_application_provisioning_defaults_are_nullable_positive_limits() -> None:
    schema = _documented_schemas()["GatewayApplicationList"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    defaults = properties["provisioning_defaults"]
    variants = cast(list[dict[str, object]], defaults["anyOf"])
    assert {"type": "null"} in variants
    reference = next(item for item in variants if "$ref" in item)
    value = _documented_schemas()[cast(str, reference["$ref"]).rsplit("/", 1)[-1]]
    assert value["additionalProperties"] is False
    limits = cast(dict[str, dict[str, object]], value["properties"])
    assert set(limits) == {"monthly_token_limit", "tokens_per_minute"}
    assert all(item["type"] == "integer" and item["exclusiveMinimum"] == 0
               for item in limits.values())


def test_image_contract_is_single_nonstreaming_passthrough_and_session_protected() -> None:
    schemas = _documented_schemas()
    request_schema = schemas["ImageInvocationRequest"]
    properties = cast(dict[str, dict[str, object]], request_schema["properties"])
    assert request_schema["additionalProperties"] is True
    assert properties["n"]["const"] == 1
    assert properties["stream"]["const"] is False
    for field in ("prompt", "size", "quality", "output_format"):
        assert "enum" not in properties[field] and "maxLength" not in properties[field]
    response = cast(dict[str, dict[str, object]], schemas["ImageInvocationResponse"]["properties"])
    assert response["data"]["minItems"] == response["data"]["maxItems"] == 1
    profile = cast(dict[str, dict[str, object]], schemas["ImageGenerationProfile"]["properties"])
    assert profile["version"]["const"] == 4
    operation = _documented_paths()["/api/v1/model-gateway/images/generations"]["post"]
    assert cast(dict[str, object], operation)["security"] == [{"sessionCookie": []}]
    assert ("/api/v1/model-gateway/images/generations", "post") in _operations(
        app.openapi()["paths"]
    )
    retry = cast(dict[str, dict[str, object]], schemas["GatewayPublicationRetry"]["properties"])
    assert retry["authorize_image_probes"]["default"] is False
    view = cast(dict[str, dict[str, object]], schemas["GatewayPublication"]["properties"])
    assert view["retry_can_authorize_image_probes"]["default"] is False
    actual_view = app.openapi()["components"]["schemas"]["GatewayPublicationView"]
    assert "desired_spec" not in actual_view["properties"]


def test_direct_publication_vendor_contract_matches_runtime_dto() -> None:
    schema = _documented_schemas()["GatewayRuntimeTarget"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert set(cast(list[str], properties["model_vendor"]["enum"])) == {
        vendor.value for vendor in ModelVendorKey
    }
    actual = app.openapi()["components"]["schemas"]["RuntimeTarget"]
    assert "model_vendor" in actual["properties"]
    for branch in cast(list[dict[str, object]], schema["oneOf"]):
        rejected = cast(dict[str, object], branch["not"])["anyOf"]
        if "openai_base_url" in cast(list[str], branch["required"]):
            assert {"required": ["model_vendor"]} not in cast(list[object], rejected)
        else:
            assert {"required": ["model_vendor"]} in cast(list[object], rejected)


def test_application_ledger_fields_preserve_unknown_and_zero() -> None:
    schema = _documented_schemas()["GatewayApplicationBudget"]
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    numeric = (
        "pending_reserved_tokens",
        "pending_reservation_count",
        "finalized_upper_bound_tokens",
        "finalized_upper_bound_count",
        "stale_reservation_count",
        "available_tokens",
    )
    for field in numeric:
        assert properties[field]["type"] == ["integer", "null"]
        assert properties[field].get("default") is None
        assert "default" not in properties[field]
        assert properties[field]["minimum"] == 0
    required = cast(list[str], schema["required"])
    assert not set(numeric).intersection(required)
    for field in ("oldest_reservation_at", "ledger_snapshot_at"):
        assert properties[field]["type"] == ["string", "null"]
        assert properties[field]["format"] == "date-time"


def test_event_contract_domain_mirror_matches_the_inline_extension() -> None:
    root = _yaml_document(CONTRACT.resolve())
    mirror = _yaml_document((CONTRACT.parent / "openapi" / "events" / "eventhub.yaml").resolve())[
        "eventContracts"
    ]

    def normalize(value: object) -> object:
        if isinstance(value, str):
            return value.removeprefix("../../openapi.yaml")
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    assert normalize(mirror) == root["x-event-contracts"]


def _assistant_operations(
    operations: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    return {operation for operation in operations if "/assistant/" in operation[0]}


def test_every_assistant_route_is_in_the_contract() -> None:
    documented = _operations(_documented_paths())
    served = _operations(app.openapi()["paths"])

    missing = sorted(_assistant_operations(served) - _assistant_operations(documented))
    stale = sorted(_assistant_operations(documented) - _assistant_operations(served))

    assert not missing, f"Routes the app serves but the contract omits: {missing}"
    assert not stale, f"Routes the contract claims but the app does not serve: {stale}"


def test_the_contract_never_claims_a_route_that_does_not_exist() -> None:
    """The direction that produces a broken client rather than an undocumented one."""
    documented = _operations(_documented_paths())
    served = _operations(app.openapi()["paths"])
    stale = sorted(op for op in documented - served if op[0] not in UNDOCUMENTED)
    assert not stale, f"Contract describes routes the app does not serve: {stale}"


def test_foundry_key_runtime_contract_is_distinct_from_managed_identity() -> None:
    schema = _documented_schemas()["GatewayRuntimeTarget"]
    alternatives = cast(list[dict[str, object]], schema["oneOf"])

    assert [set(cast(list[str], item["required"])) for item in alternatives] == [
        {"existing_id"},
        {"bedrock_runtime_url", "api_key"},
        {"foundry_project_endpoint"},
        {"foundry_project_endpoint", "foundry_inference_endpoint", "api_key"},
        {"openai_base_url", "api_key"},
    ]
    managed_identity_exclusions = cast(
        dict[str, list[dict[str, list[str]]]], alternatives[2]["not"]
    )["anyOf"]
    assert {"foundry_inference_endpoint"} in {
        frozenset(item["required"]) for item in managed_identity_exclusions
    }
    assert {"api_key"} in {frozenset(item["required"]) for item in managed_identity_exclusions}


def test_openai_connection_contract_exposes_no_registration_secret() -> None:
    schemas = _documented_schemas()
    connection = cast(dict[str, dict[str, object]], schemas["ModelConnectionCreate"]["properties"])
    provider = cast(dict[str, dict[str, object]], schemas["GatewayProviderTarget"]["properties"])
    runtime = cast(dict[str, dict[str, object]], schemas["GatewayRuntimeTarget"]["properties"])

    assert "api_key" not in connection
    assert connection["openai_base_url"]["pattern"] == "^https://"
    assert connection["model_vendor"]["enum"] == [
        "generic", "kimi", "deepseek", "openai", "anthropic",
    ]
    assert "openai_compatible" in cast(list[str], provider["template"]["enum"])
    assert runtime["api_key"]["writeOnly"] is True
    alternatives = cast(list[dict[str, object]], schemas["GatewayRuntimeTarget"]["oneOf"])
    for alternative in alternatives[:-1]:
        exclusions = cast(dict[str, list[dict[str, list[str]]]], alternative["not"])["anyOf"]
        assert {"openai_base_url"} in {frozenset(item["required"]) for item in exclusions}


def test_gateway_release_reads_separate_recorded_and_live_integrity() -> None:
    paths = _documented_paths()
    schemas = _documented_schemas()

    def get_operation(path: str) -> dict[str, object]:
        return cast(dict[str, object], paths[path]["get"])

    assert get_operation("/api/v1/model-management/releases")["operationId"] == (
        "listGatewayReleases"
    )
    assert (
        get_operation("/api/v1/model-management/releases/{release_id}")["operationId"]
        == "getGatewayRelease"
    )
    assert (
        get_operation("/api/v1/model-management/releases/{release_id}/diff")["operationId"]
        == "getGatewayReleaseDiff"
    )
    assert (
        get_operation("/api/v1/model-management/releases/{release_id}/integrity")["operationId"]
        == "getGatewayReleaseIntegrity"
    )
    dependencies = cast(
        dict[str, dict[str, object]],
        schemas["GatewayReleaseDependencies"]["properties"],
    )
    assert dependencies["live_status"]["enum"] == [
        "not_checked",
        "healthy",
        "missing",
        "mismatched",
    ]
    summary = cast(
        dict[str, dict[str, object]],
        schemas["GatewayReleaseSummary"]["properties"],
    )
    assert "rollback_eligible" in summary
    assert "rollback_blockers" in summary


def test_native_apim_pool_contract_is_bounded_and_model_scoped() -> None:
    paths = _documented_paths()
    schemas = _documented_schemas()

    pool_path = paths["/api/v1/model-management/models/{item_id}/backend-pool"]
    assert set(pool_path) == {"get", "put", "delete"}
    write = cast(
        dict[str, dict[str, object]],
        schemas["GatewayBackendPoolWrite"]["properties"],
    )
    rate_limit = cast(
        dict[str, dict[str, object]],
        schemas["GatewayRateLimitResilience"]["properties"],
    )
    breaker = cast(
        dict[str, dict[str, object]],
        schemas["GatewayRateLimitCircuitBreaker"]["properties"],
    )

    assert write["members"]["minItems"] == 2
    assert write["members"]["maxItems"] == 30
    pool_config = cast(
        dict[str, dict[str, object]], schemas["GatewayBackendPoolConfig"]["properties"]
    )
    registry = cast(dict[str, dict[str, object]], schemas["ModelRegistry"]["properties"])
    for field in (
        write["session_affinity"], pool_config["session_affinity"],
        registry["backend_pool_session_affinity_supported"],
    ):
        assert field["type"] == "boolean"
        assert field["default"] is False
    assert rate_limit["max_attempts_per_request"] == {
        "type": "integer",
        "const": 2,
    }
    assert breaker["failure_count"] == {"type": "integer", "const": 1}
    assert breaker["interval_seconds"] == {"type": "integer", "const": 60}
    assert breaker["trip_duration_seconds"] == {"type": "integer", "const": 60}
    assert breaker["accept_retry_after"] == {"type": "boolean", "const": True}
    assert rate_limit["backend_timeout_seconds"] == {
        "type": "integer",
        "const": 120,
        "default": 120,
    }
    assert breaker["status_code_ranges"]["default"] == [
        {"minimum": 429, "maximum": 429},
        {"minimum": 408, "maximum": 408},
        {"minimum": 500, "maximum": 599},
    ]
    assert breaker["error_reasons"]["default"] == [
        "BackendConnectionFailure",
        "Timeout",
    ]
    assert "existing compatible managed Runtimes" in str(
        cast(dict[str, object], pool_path["put"])["description"]
    )
