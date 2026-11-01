from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from turnstile_core.domain.budget_counting import ModelCountContract
from turnstile_core.domain.storage_routes import (
    ActiveRouteHead,
    ConfigurationApproval,
    ConnectionApproval,
    ConnectionRevision,
    CountCredential,
    FixedProgramCutover,
    FixedProgramInstallation,
    FixedProgramUpgrade,
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteContent,
    RouteCredential,
    RouteScope,
    canonical_document,
    catalog_digest,
    changed_route_aliases,
    configuration_manifests,
    document_digest,
    parse_document,
)


@pytest.mark.parametrize("fault", [None, "path", "change-old", "remove-old", "profiles", "target"])
def test_decision_program_upgrade_only_adds_native_operation_and_profile(fault: str | None) -> None:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    contracts = {
        "models": ("GET", "/v1/models"), "chat-completions": ("POST", "/chat/completions"),
        "responses": ("POST", "/responses"), "responses-compact": ("POST", "/responses/compact"),
        "anthropic-messages": ("POST", "/v1/messages"),
        "anthropic-count-tokens": ("POST", "/v1/messages/count_tokens"),
        "images-generations": ("POST", "/images/generations"),
        "storage-route-program-probe": ("HEAD", "/_turnstile/program"),
        "storage-route-configuration-probe": ("HEAD", "/_turnstile/configuration"),
    }
    previous = FixedProgramInstallation(
        program=ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256="a" * 64, trust_binding_sha256="b" * 64),
        target_identity_sha256="c" * 64, storage_endpoint="https://unitstore.table.core.windows.net",
        storage_table="Routes", gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="d" * 64,
        operation_contracts=contracts,
        operation_policies_sha256={name: "e" * 64 for name in contracts},
        supported_profiles=("openai-v1", "anthropic-messages"), model_additions_enabled=True,
    )
    current_contracts = {**contracts, "microsoft-decision": (
        "POST", "/wrong" if fault == "path" else "/providers/microsoft/v1/systemone",
    )}
    if fault == "change-old":
        current_contracts["chat-completions"] = ("POST", "/wrong-chat")
    if fault == "remove-old":
        current_contracts.pop("models")
    current = previous.model_copy(update={
        "program": previous.program.model_copy(update={
            "program_sha256": "f" * 64,
            "scope": scope.model_copy(update={"target_id": str(UUID(int=99))})
            if fault == "target" else scope,
        }), "parent_policy_sha256": "f" * 64, "legacy_program_sha256": "a" * 64,
        "decision_enabled": True, "operation_contracts": current_contracts,
        "operation_policies_sha256": {name: "e" * 64 for name in current_contracts},
        "supported_profiles": ("openai-v1", "microsoft-decision") if fault == "profiles"
        else (*previous.supported_profiles, "microsoft-decision"),
    })
    if fault:
        with pytest.raises(ValueError):
            upgrade = FixedProgramUpgrade(previous_publication_id=str(UUID(int=9)),
                                          previous_installation_sha256=document_digest(previous),
                                          installation=current)
            upgrade.require_previous(str(UUID(int=9)), previous)
    else:
        upgrade = FixedProgramUpgrade(previous_publication_id=str(UUID(int=9)),
                                      previous_installation_sha256=document_digest(previous),
                                      installation=current)
        upgrade.require_previous(str(UUID(int=9)), previous)
        cutover = FixedProgramCutover(
            upgrade=upgrade, parent_policy="<policies/>", expected_parent_etag='"parent"',
            operation_policies={name: "<policies/>" for name in current_contracts},
            expected_operation_etags={name: None if name == "microsoft-decision" else '"operation"'
                                      for name in current_contracts},
        )
        assert cutover.expected_operation_etags["microsoft-decision"] is None
        values = cutover.model_dump(mode="json")
        values["expected_operation_etags"]["models"] = None
        with pytest.raises(ValidationError):
            FixedProgramCutover.model_validate_json(json.dumps(values))


@pytest.mark.parametrize("case", [
    "images", "images-and-decision", "images-after-decision", "order", "other-field",
    "operation", "resize", "disable",
])
def test_image_program_upgrade_only_appends_profile_and_capacity(case: str) -> None:
    from turnstile_core.domain.image_profiles import DEFAULT_IMAGE_GENERATION_LIMITS

    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    contracts = {
        "models": ("GET", "/v1/models"), "chat-completions": ("POST", "/chat/completions"),
        "anthropic-messages": ("POST", "/v1/messages"),
        "images-generations": ("POST", "/images/generations"),
    }
    decision = {"microsoft-decision": ("POST", "/providers/microsoft/v1/systemone")}
    prior_decision = case == "images-after-decision"
    prior_images = case in {"resize", "disable"}
    previous = FixedProgramInstallation(
        program=ProgramIdentity(scope=scope, api_revision="1", instance_id="managed",
                                program_sha256="a" * 64, trust_binding_sha256="b" * 64),
        target_identity_sha256="c" * 64, storage_endpoint="https://unitstore.table.core.windows.net",
        storage_table="Routes", gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="d" * 64,
        operation_contracts={**contracts, **(decision if prior_decision else {})},
        operation_policies_sha256={
            name: "e" * 64 for name in {**contracts, **(decision if prior_decision else {})}
        },
        supported_profiles=(
            "openai-v1", "anthropic-messages",
            *(("microsoft-decision",) if prior_decision else ()),
            *(("openai-images",) if prior_images else ()),
        ),
        image_limits=DEFAULT_IMAGE_GENERATION_LIMITS if prior_images else None,
        decision_enabled=prior_decision, model_additions_enabled=True,
    )
    adds_decision = case in {"images-and-decision", "order"}
    current_contracts = {**previous.operation_contracts, **(decision if adds_decision else {})}
    if case == "operation":
        current_contracts["unit-extra"] = ("POST", "/unit-extra")
    profiles: tuple[Any, ...] = (*previous.supported_profiles, "openai-images")
    if adds_decision:
        profiles = (*profiles, "microsoft-decision")
    if case == "order":
        profiles = (*previous.supported_profiles, "microsoft-decision", "openai-images")
    if prior_images:
        profiles = previous.supported_profiles
    if case == "disable":
        profiles = ("openai-v1", "anthropic-messages")
    current = previous.model_copy(update={
        "program": previous.program.model_copy(update={"program_sha256": "f" * 64}),
        "parent_policy_sha256": "f" * 64, "legacy_program_sha256": "a" * 64,
        "operation_contracts": current_contracts,
        "operation_policies_sha256": {name: "9" * 64 for name in current_contracts},
        "supported_profiles": profiles, "decision_enabled": prior_decision or adds_decision,
        "image_limits": None if case == "disable"
        else DEFAULT_IMAGE_GENERATION_LIMITS.model_copy(update={"timeout_seconds": 120})
        if case == "resize" else DEFAULT_IMAGE_GENERATION_LIMITS,
        **({"storage_table": "OtherRoutes"} if case == "other-field" else {}),
    })

    def upgrade() -> None:
        FixedProgramUpgrade(
            previous_publication_id=str(UUID(int=9)),
            previous_installation_sha256=document_digest(previous), installation=current,
        ).require_previous(str(UUID(int=9)), previous)

    if case in {"images", "images-and-decision", "images-after-decision"}:
        upgrade()
    else:
        with pytest.raises(ValueError):
            upgrade()


def route_fixture(alias: str = "Model:one") -> tuple[RouteContent, ConnectionApproval]:
    scope = RouteScope(target_id=str(UUID(int=1)), api_id="unit-api", gateway_id="managed")
    connection = ConnectionRevision(
        scope=scope,
        revision_id=str(UUID(int=2)),
        backend_id="finops-approved-backend",
        origin="https://unit.openai.azure.com",
        path="/openai/v1/chat/completions",
        dependency_sha256="a" * 64,
        credential=RouteCredential(mode="none"),
    )
    budget = ModelCountContract(
        model_id=UUID(int=3),
        algorithm="legacy_request_estimate_v1",
        output_estimate=4096,
        output_field="max_completion_tokens",
        operations=("chat-completions",),
        evidence_sha256="b" * 64,
        verified_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    route = RouteContent(
        scope=scope,
        model_id=str(UUID(int=3)),
        alias=alias,
        display_name=alias,
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
        upstream_model="unit-model",
        profile="openai-v1",
        operations=("chat-completions",),
        connection=connection,
        budget_epoch=str(UUID(int=4)),
        budget=budget,
    )
    approved = ConnectionApproval(
        approval_id=str(UUID(int=5)),
        connection=connection,
        state="approved",
        evidence_sha256="c" * 64,
        issued_epoch=1000,
        expires_epoch=2000,
    )
    return route, approved


@pytest.mark.parametrize("path", [
    "/anthropic/v1/messages/count_tokens", "https://foreign.example/messages/count_tokens",
    "//foreign.example/messages/count_tokens", "/messages", "/v1/../messages/count_tokens",
])
def test_public_count_endpoint_requires_approved_anthropic_path(path: str) -> None:
    route, _ = route_fixture()
    values = {**route.connection.model_dump(mode="python"), "public_count_path": path}
    if path == "/anthropic/v1/messages/count_tokens":
        connection = ConnectionRevision.model_validate(values)
        assert connection.public_count_path == path
        with pytest.raises(ValueError, match="PUBLIC_COUNT"):
            RouteContent.model_validate({
                **route.model_dump(mode="python"), "connection": connection,
            })
    else:
        with pytest.raises(ValueError):
            ConnectionRevision.model_validate(values)


@pytest.mark.parametrize("fault", [None, "missing", "model", "text"])
def test_image_route_keeps_original_immutable_image_limits(fault: str | None) -> None:
    from turnstile_core.domain.image_profiles import ImageGenerationLimits, create_image_profile

    route, _ = route_fixture()
    profile = create_image_profile(ImageGenerationLimits(
        max_request_bytes=24576, max_response_bytes=16777216,
        output_reservation_tokens=8192, timeout_seconds=180,
    ), "unit-image")
    values = {
        **route.model_dump(mode="python"), "profile": "openai-images",
        "upstream_model": "different" if fault == "model" else "unit-image",
        "operations": ("images-generations",), "streaming_mode": "buffered",
        "image_profile": None if fault == "missing" else profile,
        "budget": route.budget.model_copy(update={
            "operations": ("images-generations",), "output_field": "none", "output_estimate": 8192,
        }),
    }
    if fault == "text":
        values = {**route.model_dump(mode="python"), "image_profile": profile}
    if fault:
        with pytest.raises(ValueError):
            RouteContent.model_validate(values)
    else:
        candidate = RouteContent.model_validate(values)
        assert candidate.image_profile == profile
        assert candidate.image_profile.limits == profile.limits


@pytest.mark.parametrize("fault", [
    None, "missing", "secret", "audience", "origin", "self-hosted", "client-secret",
])
def test_oauth_reference_is_nonsecret_and_bound_to_its_managed_workspace(fault: str | None) -> None:
    route, _ = route_fixture()
    oauth = {
        "provider_id": "turnstile-oauth-" + "a" * 32, "authorization_id": "connection",
        "client_id": str(UUID(int=60)),
        "token_url": "https://adb-123.4.azuredatabricks.net/oidc/v1/token",
        "scopes": "all-apis",
    }
    values = {
        "mode": "oauth_client_credentials", "header": "Authorization", "oauth": oauth,
    }
    if fault == "missing":
        values.pop("oauth")
    elif fault == "secret":
        values["secret_id"] = "https://unit.vault.azure.net/secrets/key/" + "a" * 32
    elif fault == "audience":
        values["audience"] = "https://storage.azure.com/"
    elif fault == "client-secret":
        oauth["client_secret"] = "UNIT_FORBIDDEN_SECRET_FIELD"

    def make_connection() -> ConnectionRevision:
        credential = RouteCredential.model_validate_json(json.dumps(values))
        return ConnectionRevision.model_validate({
            **route.connection.model_dump(mode="python"), "credential": credential,
            "scope": route.scope.model_copy(update={"gateway_id": "unit-node"})
            if fault == "self-hosted" else route.scope,
            "origin": "https://adb-456.7.azuredatabricks.net" if fault == "origin"
            else "https://adb-123.4.azuredatabricks.net",
        })

    if fault:
        with pytest.raises(ValueError):
            make_connection()
        return
    connection = make_connection()
    assert connection.credential.oauth is not None
    assert connection.credential.oauth.provider_id == oauth["provider_id"]
    assert "client_secret" not in canonical_document(connection)
    assert canonical_document(route.connection.credential) == (
        '{"audience":"","header":"","mode":"none","secret_id":""}'
    )
    with pytest.raises(ValueError):
        RouteCredential.model_validate({"mode": "none", "oauth": connection.credential.oauth})


@pytest.mark.parametrize("fault", [None, "profile", "stream", "path", "version"])
def test_bedrock_adapter_requires_bound_invoke_model_and_buffered_messages(
    fault: str | None,
) -> None:
    route, _ = route_fixture()
    model = "anthropic.unit-model"
    values = {
        **route.model_dump(mode="python"), "upstream_model": model,
        "profile": "openai-v1" if fault == "profile" else "anthropic-messages",
        "streaming_mode": "native" if fault == "stream" else "buffered",
        "request_adapter": "unreviewed-v2" if fault == "version" else "bedrock-anthropic-v1",
        "operations": ("anthropic-messages",),
        "connection": route.connection.model_copy(update={
            "origin": "https://bedrock-runtime.us-east-1.amazonaws.com",
            "path": "/model/wrong/invoke" if fault == "path" else f"/model/{model}/invoke",
        }),
        "budget": route.budget.model_copy(update={"operations": ("anthropic-messages",)}),
    }
    if fault:
        with pytest.raises(ValueError):
            RouteContent.model_validate(values)
        return
    approved = RouteContent.model_validate(values)
    assert approved.request_adapter == "bedrock-anthropic-v1"
    assert "request_adapter" not in route.model_dump(mode="json")


@pytest.mark.parametrize("fault", [
    None, "duplicate-backend", "duplicate-runtime", "zero-weight", "cookie", "secret-version",
    "duplicate-backend-case", "self-backend", "primary-key",
])
def test_native_pool_snapshot_preserves_members_resilience_and_versioned_credentials(
    fault: str | None,
) -> None:
    from turnstile_core.domain.storage_routes import RouteBackendPool, text_digest

    route, _ = route_fixture()
    values: dict[str, Any] = {
        "members": [
            {"runtime_id": str(UUID(int=index)), "backend_id": "unit-member-" + str(index),
             "dependency_sha256": "a" * 64, "priority": 0,
             "weight": 0 if fault == "zero-weight" else 1}
            for index in (80, 81)
        ],
        "rate_limit": {
            "max_attempts_per_request": 2, "retry_interval_seconds": 1, "first_fast_retry": True,
            "backend_timeout_seconds": 120, "circuit_breaker": {
                "failure_count": 1, "interval_seconds": 60, "trip_duration_seconds": 60,
                "accept_retry_after": True,
            },
        },
        "session_cookie_name": "TurnstileAffinity-" + text_digest(
            route.scope.target_id + ":" + route.alias,
        )[:16],
        "credential_versions": {
            "turnstile-route-key-" + "a" * 40:
                "https://unitvault.vault.azure.net/secrets/key/" + "b" * 32,
        },
    }
    if fault == "duplicate-backend":
        values["members"][1]["backend_id"] = values["members"][0]["backend_id"]
    elif fault == "duplicate-backend-case":
        values["members"][1]["backend_id"] = values["members"][0]["backend_id"].upper()
    elif fault == "self-backend":
        values["members"][0]["backend_id"] = route.connection.backend_id.upper()
    elif fault == "duplicate-runtime":
        values["members"][1]["runtime_id"] = values["members"][0]["runtime_id"]
    elif fault == "cookie":
        values["session_cookie_name"] = "foreign-cookie"
    elif fault == "secret-version":
        values["credential_versions"] = {"unit-key": "https://unitvault.vault.azure.net/secrets/key"}
    if fault not in {None, "duplicate-runtime", "self-backend", "primary-key"}:
        with pytest.raises(ValueError):
            RouteBackendPool.model_validate_json(json.dumps(values))
        return
    pool = RouteBackendPool.model_validate_json(json.dumps(values))
    if fault in {"self-backend", "primary-key"}:
        connection = {
            **route.connection.model_dump(mode="json"), "pool": pool.model_dump(mode="json"),
        }
        if fault == "primary-key":
            connection["credential"] = {
                "mode": "key_vault_api_key", "header": "api-key",
                "secret_id": "https://unitvault.vault.azure.net/secrets/key/" + "b" * 32,
            }
        with pytest.raises(ValueError):
            RouteContent.model_validate_json(canonical_document({
                **route.model_dump(mode="json"), "connection": connection,
            }))
        return
    assert [member.priority for member in pool.members] == [0, 0]
    if fault == "duplicate-runtime":
        assert pool.members[0].runtime_id == pool.members[1].runtime_id
        assert pool.members[0].backend_id != pool.members[1].backend_id
    assert pool.rate_limit.max_attempts_per_request == 2
    updated = RouteContent.model_validate_json(canonical_document({
        **route.model_dump(mode="json"), "connection": {
            **route.connection.model_dump(mode="json"), "pool": pool.model_dump(mode="json"),
        },
    }))
    assert updated.connection.pool == pool
    assert "pool" not in route.connection.model_dump(mode="json")
    assert changed_route_aliases([route], [updated]) == (route.alias,)


@pytest.mark.parametrize("fault", [None, "header-limit", "runtime-limit", "missing", "mixed"])
def test_deployment_pool_reader_checks_real_compiled_parent(fault: str | None) -> None:
    from pathlib import Path
    from unittest.mock import Mock
    from xml.etree import ElementTree as ET

    import httpx
    from pydantic import SecretStr

    from tests.backend.model_platform.test_apim_onboarding import (
        onboarding_configuration,
        onboarding_plan,
    )
    from turnstile_core.domain.gateway_targets import CapabilitySupport
    from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
    from turnstile_core.integrations.apim_onboarding_policy import render_onboarding_fixed_program
    from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy
    from turnstile_core.integrations.apim_storage_routes import (
        READ_FIXED_CONTENT,
        FixedProgramVerifier,
        deployment_pool_reader_supported,
    )

    plan = onboarding_plan(service_tier="Developer")
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    program = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=CapabilitySupport.SUPPORTED,
    )
    assert all(parse_policy(policy).find(".//set-variable[@name='routeContent']") is None
               for _, policy in program.operation_policies)
    root = parse_policy(program.parent_policy)
    reader = root.find(".//set-variable[@name='routeContent']")
    assert reader is not None
    if fault == "header-limit":
        reader.set("value", READ_FIXED_CONTENT.replace("headers.Count > 90", "headers.Count > 60"))
    elif fault == "runtime-limit":
        reader.set("value", READ_FIXED_CONTENT.replace(
            '|| poolMember["backend_id"]?.Type',
            '|| !runtimeIds.Add((string)poolMember["runtime_id"])'
            '\n || poolMember["backend_id"]?.Type',
        ))
    elif fault == "missing":
        parent = next(node for node in root.iter() if reader in list(node))
        parent.remove(reader)
    elif fault == "mixed":
        inbound = root.find("inbound")
        assert inbound is not None
        ET.SubElement(inbound, "set-variable", {
            "name": "routeContent",
            "value": READ_FIXED_CONTENT.replace("headers.Count > 90", "headers.Count > 60"),
        })
    assert deployment_pool_reader_supported(serialize_policy(root)) is (fault is None)
    if fault is None:
        installed = program.installation("b" * 64, "https://managed.example.test/api")
        policies = dict(program.operation_policies)
        management = Mock()
        management.current_revision.return_value = installed.program.api_revision
        management.revision_api_policy.return_value = program.parent_policy
        management.route_program_operations.return_value = {
            name: (*contract, policies[name])
            for name, contract in installed.operation_contracts.items()
        }

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.method == "HEAD"
            values = {
                "abi": "2", "nonce": request.headers["x-turnstile-route-probe"],
                "target": installed.program.scope.target_id, "api": installed.program.scope.api_id,
                "revision": installed.program.api_revision,
                "gateway": installed.program.scope.gateway_id,
                "instance": installed.program.instance_id,
                "program": installed.program.program_sha256,
                "trust": installed.program.trust_binding_sha256,
            }
            return httpx.Response(204, headers={
                "x-turnstile-route-" + name: value for name, value in values.items()
            })

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            verifier = FixedProgramVerifier(
                installed, management, SecretStr("unit-only-key"), client, clock=lambda: 1000,
            )
            with pytest.raises(PolicyCompilationError, match="API parent policy"):
                verifier.require_deployment_pool_reader()
            verifier.verify()
            verifier.require_deployment_pool_reader()
            management.revision_api_policy.return_value = "<policies/>"
            with pytest.raises(PolicyCompilationError, match="parent changed"):
                verifier.verify()
            with pytest.raises(PolicyCompilationError, match="API parent policy"):
                verifier.require_deployment_pool_reader()


def test_native_pool_reader_preserves_deployment_identity_and_header_bound() -> None:
    from turnstile_core.integrations.apim_storage_routes import READ_FIXED_CONTENT

    assert "runtimeIds.Add" not in READ_FIXED_CONTENT
    assert "var runtimeIds =" not in READ_FIXED_CONTENT
    assert '!backendIds.Add((string)poolMember["backend_id"])' in READ_FIXED_CONTENT
    assert 'Guid.TryParseExact((string)poolMember["runtime_id"], "D", out runtimeId)' in (
        READ_FIXED_CONTENT
    )
    assert "headers.Count > 90" in READ_FIXED_CONTENT
    assert "headers.Count > 60" not in READ_FIXED_CONTENT


def empty_installation_record() -> dict[str, Any]:
    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.domain.storage_routes import (
        ActiveRouteHead,
        RouteSnapshot,
        catalog_digest,
        configuration_manifests,
        document_digest,
    )

    original, program, _ = snapshot_fixture()
    _, manifest, dependencies = configuration_manifests(
        program.scope, original.approval.route_version, (), {}, 1100,
    )
    snapshot = RouteSnapshot.prepare(original.approval.model_copy(update={
        "manifest_sha256": manifest, "dependency_manifest_sha256": dependencies,
        "entry_count": 0, "catalog_sha256": catalog_digest(()), "required_profiles": (),
    }), (), {}, 1100)
    record: dict[str, Any] = {
        "id": str(UUID(int=930)), "gateway_profile_id": program.scope.target_id,
        "publication_kind": "route_reconcile", "generation": 1,
        "desired_spec": {"gateway_profile_id": program.scope.target_id,
                         "bindings": [], "discovery_models": []},
        "status": "active", "base_release_id": None, "apim_revision": program.api_revision,
        "policy_sha256": program.program_sha256,
        "resource_manifest": {
            "fixed_route_program": {"program": program.model_dump(mode="json")},
            "fixed_route_snapshot": snapshot.model_dump(mode="json"),
            "fixed_route_head": ActiveRouteHead(
                approval=snapshot.approval, activated_epoch=1100,
            ).model_dump(mode="json"),
        },
        "error_code": None, "error_message": None, "attempt_count": 0,
        "created_by": "fixed-program-installation", "created_at": datetime.now(UTC),
        "started_at": datetime.now(UTC), "completed_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    record["desired_spec_sha256"] = document_digest(record["desired_spec"])
    return record


@pytest.mark.parametrize(
    "fault", [None, "intent", "binding", "not-empty", "base", "superseded", "failed"],
)
def test_empty_fixed_installation_is_not_an_empty_ordinary_publication(fault: str | None) -> None:
    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.domain.control_plane import GatewayPublication, GatewayPublicationIntent

    original, _, _ = snapshot_fixture()
    record = empty_installation_record()
    if fault == "intent":
        with pytest.raises(ValueError):
            GatewayPublicationIntent.model_validate({name: record[name] for name in (
                "gateway_profile_id", "publication_kind", "desired_spec",
            )})
        return
    if fault == "superseded":
        superseded = GatewayPublication.model_validate({**record, "status": "superseded"})
        with pytest.raises(ValueError, match="Only an initial fixed installation"):
            superseded.require_empty_fixed_installation()
        return
    if fault == "failed":
        record["status"] = "failed"
    if fault == "binding":
        record["policy_sha256"] = "f" * 64
    elif fault == "not-empty":
        record["resource_manifest"]["fixed_route_snapshot"] = original.model_dump(mode="json")
    elif fault == "base":
        record["base_release_id"] = str(UUID(int=931))
    if fault:
        with pytest.raises(ValueError):
            GatewayPublication.model_validate(record)
    else:
        result = GatewayPublication.model_validate(record)
        assert result.desired_spec.bindings == [] and result.generation == 1
        from unittest.mock import MagicMock

        from turnstile_core.persistence.repository_publications import (
            PostgreSqlPublicationRepositoryMixin,
        )

        connection = MagicMock()
        connection.execute.return_value.fetchone.side_effect = [
            None, {"id": result.gateway_profile_id}, record,
        ]
        repository = PostgreSqlPublicationRepositoryMixin()
        connection.__enter__.return_value = connection
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(repository, "_connection", MagicMock(return_value=connection))
            stored = repository.record_fixed_route_installation(result)
        assert stored["id"] == record["id"]
        statements = [call.args[0] for call in connection.execute.call_args_list]
        assert any("INSERT INTO effective_gateway_release" in value for value in statements)
        assert any("INSERT INTO gateway_publication_audit" in value for value in statements)
        assert not any("UPDATE gateway_publication" in value for value in statements)
        from importlib import import_module

        parse_sql = import_module("pglast").parse_sql
        inserted = next(call for call in connection.execute.call_args_list
                        if "INSERT INTO gateway_publication (" in call.args[0])
        statement = parse_sql(inserted.args[0].replace("%s", "NULL"))[0].stmt
        columns = [column.name for column in statement.cols]
        assert "started_at" in columns
        assert inserted.args[1][-3] == result.started_at
        for sql in statements:
            parse_sql(sql.replace("%s", "NULL"))


def test_configuration_versions_and_approval_renewals_do_not_change_route_content() -> None:
    route, connection = route_fixture()
    approvals = {connection.connection.revision_id: connection}
    original = configuration_manifests(route.scope, str(UUID(int=10)), [route], approvals, 1100)
    advanced = configuration_manifests(route.scope, str(UUID(int=11)), [route], approvals, 1100)
    assert original[1:] == advanced[1:]
    assert original[0][0].route_version != advanced[0][0].route_version
    renewed = connection.model_copy(update={"issued_epoch": 1050, "expires_epoch": 2050})
    result = configuration_manifests(
        route.scope,
        str(UUID(int=12)),
        [route],
        {
            connection.connection.revision_id: renewed,
        },
        1100,
    )
    assert result[1] != original[1] and result[2] == original[2]
    assert result[0][0].content_sha256 == original[0][0].content_sha256
    assert changed_route_aliases([route], [route]) == ()


@pytest.mark.parametrize("now", [1100, 2000, 86400 * 7])
def test_approved_connection_does_not_expire_without_a_configuration_change(now: int) -> None:
    route, approval = route_fixture()
    approval.require_current(route.connection, now)
    members, _, _ = configuration_manifests(
        route.scope, str(UUID(int=10)), [route], {route.connection.revision_id: approval}, now,
    )
    assert members[0].connection_approval == approval


@pytest.mark.parametrize("fault", ["revoked", "origin", "path", "credential", "future"])
def test_nonexpiring_connection_approval_preserves_binding_and_revocation(fault: str) -> None:
    route, approval = route_fixture()
    connection = route.connection
    if fault == "revoked":
        approval = approval.model_copy(update={"state": "revoked"})
    elif fault == "future":
        approval = approval.model_copy(update={"issued_epoch": 86400 * 8})
    elif fault == "credential":
        connection = connection.model_copy(update={
            "credential": RouteCredential(
                mode="key_vault_bearer", header="Authorization",
                secret_id="https://unitvault.vault.azure.net/secrets/different-key/" + "a" * 32,
            ),
        })
    else:
        connection = connection.model_copy(update={
            fault: "https://foreign.example" if fault == "origin" else "/different-model",
        })
    with pytest.raises(ValueError, match="CONNECTION_APPROVAL_INVALID"):
        approval.require_current(connection, 86400 * 7)


@pytest.mark.parametrize("alias", ["odata.etag", "odata.metadata", "id", "Model:one"])
def test_every_legal_business_alias_participates_in_manifest(alias: str) -> None:
    route, connection = route_fixture(alias)
    approvals = {connection.connection.revision_id: connection}
    original = configuration_manifests(route.scope, str(UUID(int=10)), [route], approvals, 1100)
    changed = route.model_copy(update={"upstream_model": "different-model"})
    candidate = configuration_manifests(route.scope, str(UUID(int=11)), [changed], approvals, 1100)
    assert candidate[1] != original[1]
    assert changed_route_aliases([route], [changed]) == (alias,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "https://unapproved.example"),
        ("backend_id", "foreign-backend"),
        ("path", "/other/inference"),
        ("dependency_sha256", "d" * 64),
    ],
)
def test_route_cannot_self_approve_changed_connection(field: str, value: str) -> None:
    route, connection = route_fixture()
    changed = route.model_copy(
        update={"connection": route.connection.model_copy(update={field: value})}
    )
    with pytest.raises(ValueError, match="CONNECTION_APPROVAL_INVALID"):
        configuration_manifests(
            route.scope,
            str(UUID(int=10)),
            [changed],
            {
                connection.connection.revision_id: connection,
            },
            1100,
        )


@pytest.mark.parametrize("fault", ["future", "revoked", "duplicate", "epoch", "typed-abi"])
def test_contract_denials(fault: str) -> None:
    route, connection = route_fixture()
    with pytest.raises((ValueError, ValidationError)):
        if fault in {"future", "revoked"}:
            connection = (
                connection.model_copy(update={"state": "revoked"})
                if fault == "revoked"
                else connection
            )
            configuration_manifests(
                route.scope,
                str(UUID(int=10)),
                [route],
                {
                    connection.connection.revision_id: connection,
                },
                999 if fault == "future" else 1100,
            )
        elif fault == "duplicate":
            configuration_manifests(
                route.scope,
                str(UUID(int=10)),
                [route, route.model_copy(update={"alias": "model:ONE"})],
                {
                    connection.connection.revision_id: connection,
                },
                1100,
            )
        elif fault == "epoch":
            RouteContent.model_validate({**route.model_dump(), "budget_epoch": "bad-epoch"})
        else:
            RouteScope.model_validate({**route.scope.model_dump(), "policy_abi": 2.0})


def test_head_approval_and_program_proof_have_distinct_lifetimes() -> None:
    route, connection = route_fixture()
    _, manifest, dependencies = configuration_manifests(
        route.scope,
        str(UUID(int=10)),
        [route],
        {
            connection.connection.revision_id: connection,
        },
        1100,
    )
    identity = ProgramIdentity(
        scope=route.scope,
        api_revision="1",
        instance_id="node-one",
        program_sha256="d" * 64,
        trust_binding_sha256="e" * 64,
    )
    proof = ProgramCapabilityProof(
        proof_id=str(UUID(int=7)),
        identity=identity,
        supported_profiles=("openai-v1",),
        issued_epoch=1050,
        expires_epoch=1170,
    )
    approval = ConfigurationApproval(
        approval_id=str(UUID(int=8)),
        scope=route.scope,
        route_version=str(UUID(int=10)),
        generation=1,
        manifest_sha256=manifest,
        dependency_manifest_sha256=dependencies,
        catalog_sha256=catalog_digest([route]),
        entry_count=1,
        required_program_sha256=identity.program_sha256,
        required_profiles=("openai-v1",),
        budget_epoch=route.budget_epoch,
        issued_epoch=1000,
        activate_before_epoch=1600,
    )
    head = ActiveRouteHead(approval=approval, activated_epoch=1100)
    head.require_ready(identity, proof, route.budget_epoch, 1100, activating=True)
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        head.require_ready(identity, proof, route.budget_epoch, 1101, activating=True)
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        head.require_ready(identity, proof, str(UUID(int=99)), 1100)
    renewed = proof.model_copy(update={"issued_epoch": 2000, "expires_epoch": 2120})
    head.require_ready(identity, renewed, route.budget_epoch, 2050)


def test_strict_json_and_namespace_include_api_identity() -> None:
    route, _ = route_fixture()
    document = canonical_document(route)
    assert parse_document(RouteContent, document, 16384) == route
    with pytest.raises(ValueError, match="DUPLICATE_JSON"):
        parse_document(RouteContent, document[:-1] + ',"alias":"another"}', 16384)
    assert route.scope.partition != route.scope.model_copy(update={"api_id": "other-api"}).partition
    assert document_digest({"odata.etag": 1}) != document_digest({"odata.etag": 2})


def test_catalog_only_change_does_not_trigger_inference_probes() -> None:
    route, _ = route_fixture()
    changed = route.model_copy(update={"display_name": "A renamed display label"})
    assert changed.sha256 != route.sha256
    assert catalog_digest([changed]) != catalog_digest([route])
    assert changed_route_aliases([route], [changed]) == ()


@pytest.mark.parametrize(
    "secret",
    [
        "https://vault-name.vault.azure.net/secrets/key/" + "a" * 32,
        "https://vault-name.vault.azure.net/secrets/key",
        "https://vault-name.vault.azure.net.evil.example/secrets/key/" + "a" * 32,
    ],
)
def test_count_credentials_require_exact_versioned_vault_reference(secret: str) -> None:
    fields = dict(
        mode="key_vault_api_key",
        origin="https://unit.openai.azure.com",
        header="api-key",
        secret_id=secret,
        endpoints=("/openai/v1/responses/input_tokens",),
    )
    if secret.endswith("a" * 32) and ".evil." not in secret:
        assert CountCredential.model_validate(fields).secret_id == secret
    else:
        with pytest.raises(ValidationError):
            CountCredential.model_validate(fields)
