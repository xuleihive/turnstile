from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid5

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import HttpUrl, SecretStr

from tests.backend.model_platform.control_plane_support import FakeApimClient, foundry_publication
from tests.backend.model_platform.test_application_provisioning import UnitLedger
from tests.backend.model_platform.test_gateway_targets import NOW, deployment, target
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.config import Settings
from turnstile_core.domain.gateway_targets import (
    ApimOnboardingConfiguration,
    ApimOnboardingPlan,
    ApimOnboardingRequest,
    ApimOnboardingStart,
    ApimTargetArmInventory,
    CapabilitySupport,
    EmployeeClientSurface,
    gateway_deployment_id_for,
    self_hosted_gateway_capability_for_tier,
)
from turnstile_core.domain.storage_routes import (
    ActiveRouteHead,
    ConfigurationApproval,
    ProgramCapabilityProof,
    RouteSnapshot,
    RouteTransitionReceipt,
    canonical_document,
    catalog_digest,
    configuration_manifests,
    document_digest,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from turnstile_core.integrations.apim_onboarding_client import (
    PLATFORM_GOVERNANCE_NAMED_VALUE,
    PLATFORM_GOVERNANCE_TAG,
    AzureApimOnboardingClient,
    AzureApimPlatformConfigurationClient,
)
from turnstile_core.integrations.apim_onboarding_client import (
    onboarding_configuration as onboarding_client_configuration,
)
from turnstile_core.integrations.apim_onboarding_policy import (
    onboarding_canonical_parent,
    render_onboarding_fixed_program,
    render_onboarding_parent,
)
from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy
from turnstile_core.integrations.apim_storage_routes import FixedRouteProgram
from turnstile_core.integrations.ledger import TableStorageLedger
from turnstile_core.integrations.storage_route_projection import (
    RouteActivationIntent,
    TableRouteProjection,
    route_entity,
)
from turnstile_core.integrations.table_batch import conditional_batch
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.apim_onboarding import (
    ApimOnboardingService,
    apim_onboarding_available,
    build_apim_onboarding_service,
    initialized_apim_targets,
    onboarding_fixed_publication,
)
from turnstile_core.services.apim_onboarding_ledger import (
    enroll_self_hosted_gateway_protocol,
    project_onboarding_ledger,
)
from turnstile_core.services.control_plane import GatewayControlPlaneService
from turnstile_core.services.gateway_release_operation_worker import GatewayReleaseOperationWorker


def onboarding_plan(service_tier: str = "StandardV2") -> ApimOnboardingPlan:
    request = ApimOnboardingRequest(
        tenant_id=UUID("20000000-0000-4000-8000-000000000002"),
        resource_id=(
            "/subscriptions/30000000-0000-4000-8000-000000000002/resourceGroups/customer-rg/"
            "providers/Microsoft.ApiManagement/service/customer-apim"
        ),
    )
    return ApimOnboardingPlan(
        request=request,
        gateway_profile_id=request.gateway_profile_id,
        service_name=request.service_name,
        service_tier=service_tier,
        location="eastus",
        managed_gateway_origin=HttpUrl("https://customer-apim.azure-api.net"),
        api_id=request.api_id,
        api_path=request.api_path,
        dashboard_subscription_id=request.dashboard_subscription_id,
        probe_subscription_id=request.probe_subscription_id,
        ready=True,
        confirmation_sha256="a" * 64,
    )


@pytest.mark.parametrize(("tier", "expected"), [
    ("Developer", CapabilitySupport.SUPPORTED),
    ("Premium", CapabilitySupport.SUPPORTED),
    ("Basic", CapabilitySupport.UNSUPPORTED),
    ("Standard", CapabilitySupport.UNSUPPORTED),
    ("Consumption", CapabilitySupport.UNSUPPORTED),
    ("Isolated", CapabilitySupport.UNSUPPORTED),
    ("BasicV2", CapabilitySupport.UNSUPPORTED),
    ("StandardV2", CapabilitySupport.UNSUPPORTED),
    ("PremiumV2", CapabilitySupport.UNSUPPORTED),
    ("FutureTier", CapabilitySupport.UNKNOWN),
])
def test_self_hosted_capability_uses_shared_apim_tier_mapping(
    tier: str, expected: CapabilitySupport,
) -> None:
    assert self_hosted_gateway_capability_for_tier(tier) is expected


@pytest.mark.parametrize(("tier", "expected"), [
    ("Developer", True),
    ("StandardV2", False),
])
@pytest.mark.parametrize("logger_id", ["unit-business-events-a", "unit-business-events-b"])
@pytest.mark.parametrize("legacy", [False, True])
def test_onboarding_fixed_program_scope_matches_apim_tier(
    tier: str, expected: bool, logger_id: str, legacy: bool,
) -> None:
    plan = onboarding_plan(service_tier=tier)
    source = (Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml")
    capability = self_hosted_gateway_capability_for_tier(plan.service_tier)
    program = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=capability,
    )
    assert program.binding.native_gateway_scope is expected
    assert program.binding.pool_enabled is True
    from turnstile_core.integrations.apim_model_availability import (
        _admission_locations,
        admission_guard,
        availability_kernel_digest,
        compose_availability_parent,
    )
    from turnstile_core.integrations.apim_onboarding_policy import (
        _render_onboarding_parent,
        onboarding_canonical_parent,
    )
    from turnstile_core.integrations.apim_policy_components import (
        policy_document_digest,
        serialize_policy,
    )
    from turnstile_core.integrations.apim_storage_routes import (
        _baseline_root,
        compile_fixed_route_program,
        upgrade_fixed_pool_program,
        upgrade_fixed_program_parent,
    )

    canonical = compose_availability_parent(onboarding_canonical_parent(
        source.read_text(), plan.request.api_id,
    ))
    baseline = _render_onboarding_parent(plan.request, onboarding_configuration(), canonical)
    parent = parse_policy(baseline)
    for event in parent.iter("log-to-eventhub"):
        event.set("logger-id", logger_id)
    if legacy:
        assert availability_kernel_digest() == (
            "83960caefc1b0f12e73e3877abb59e8ad40889e1f75c1980c761656b83d25ed9"
        )
        for branch, anchor in _admission_locations(parent):
            guard = serialize_policy(admission_guard())
            for parameter, value in (
                ("__GATEWAY_PROFILE_ID__", program.binding.scope.target_id),
                ("__LEDGER_TABLE_ENDPOINT__", program.binding.ledger_table_endpoint),
                ("__LEDGER_TABLE_NAME__", program.binding.ledger_table_name),
            ):
                guard = guard.replace(parameter, value)
            branch[list(branch).index(anchor) + 1] = parse_policy(guard)
    baseline = serialize_policy(parent)
    if legacy:
        _baseline_root(baseline)
        baseline = upgrade_fixed_program_parent(
            baseline, program.binding, canonical, policy_document_digest(baseline),
        )
        assert len([event for event in parse_policy(baseline).iter("log-to-eventhub")
                    if "model_availability_pre_dispatch_denial" in (event.text or "")]) == 2
    program = compile_fixed_route_program(baseline, program.binding, canonical_parent=canonical)
    events = [
        event for artifact in (program.parent_policy, *dict(program.operation_policies).values())
        for event in parse_policy(artifact).iter("log-to-eventhub")
    ]
    assert events
    assert {event.attrib["logger-id"] for event in events} == {logger_id}
    previous = compile_fixed_route_program(
        baseline, program.binding.model_copy(update={"pool_enabled": False}),
        canonical_parent=canonical,
    )
    assert previous.binding.pool_enabled is False
    upgraded = upgrade_fixed_pool_program(previous.parent_policy, previous.binding)
    assert upgraded.parent_policy == program.parent_policy
    assert upgraded.operation_policies == program.operation_policies


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_program_refresh_adopts_the_current_model_assignment_denial(tier: str) -> None:
    from turnstile_core.integrations.apim_model_availability import compose_availability_parent
    from turnstile_core.integrations.apim_policy_components import component_digest
    from turnstile_core.integrations.apim_storage_routes import (
        refresh_fixed_budget_execution_policy,
        upgrade_fixed_pool_program,
    )

    plan = onboarding_plan(service_tier=tier)
    capability = self_hosted_gateway_capability_for_tier(plan.service_tier)
    template = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    denial = (
        '{"type":"error","error":{"type":"permission_error","code":"model_not_assigned",'
        '"param":"model","message":"The requested model is not assigned to you"}}'
    )
    assert template.count(denial) == 1
    installed = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(),
        template.replace(denial, '{"error":"model_not_assigned"}'),
        self_hosted_gateway=capability,
    )
    current = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), template, self_hosted_gateway=capability,
    )
    canonical = compose_availability_parent(
        onboarding_canonical_parent(template, plan.request.api_id),
    )

    refreshed = upgrade_fixed_pool_program(
        refresh_fixed_budget_execution_policy(
            installed.parent_policy, installed.binding, canonical,
        ),
        installed.binding,
    )

    assert component_digest(parse_policy(refreshed.parent_policy)) == component_digest(
        parse_policy(current.parent_policy)
    )


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_program_refresh_adopts_the_current_application_denial(tier: str) -> None:
    # Programs installed before the application 403 used the standard envelope must still refresh.
    from turnstile_core.integrations.apim_model_availability import compose_availability_parent
    from turnstile_core.integrations.apim_policy_components import component_digest
    from turnstile_core.integrations.apim_storage_routes import (
        refresh_fixed_budget_execution_policy,
        upgrade_fixed_pool_program,
    )

    plan = onboarding_plan(service_tier=tier)
    capability = self_hosted_gateway_capability_for_tier(plan.service_tier)
    template = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    reason = template.index(
        "var reason = (string)context.Variables[&quot;applicationDenialReason&quot;];"
    )
    start = template.rindex('<set-header name="x-request-id"', 0, reason)
    end = template.index("</set-body>", reason) + len("</set-body>")
    legacy = template[:start] + (
        "<set-body>@(new JObject(new JProperty(&quot;error&quot;, (string)context.Variables"
        "[&quot;applicationDenialReason&quot;])).ToString(Newtonsoft.Json.Formatting.None))"
        "</set-body>"
    ) + template[end:]
    installed = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), legacy, self_hosted_gateway=capability,
    )
    current = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), template, self_hosted_gateway=capability,
    )
    canonical = compose_availability_parent(
        onboarding_canonical_parent(template, plan.request.api_id),
    )

    def refresh(program: FixedRouteProgram) -> FixedRouteProgram:
        return upgrade_fixed_pool_program(
            refresh_fixed_budget_execution_policy(
                program.parent_policy, program.binding, canonical,
            ),
            program.binding,
        )

    refreshed = refresh(installed)
    again = refresh(refreshed)

    assert installed.parent_policy != current.parent_policy
    assert component_digest(parse_policy(refreshed.parent_policy)) == component_digest(
        parse_policy(current.parent_policy)
    )
    assert refreshed.operation_policies == current.operation_policies
    assert again.parent_policy == refreshed.parent_policy
    assert again.operation_policies == refreshed.operation_policies


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_program_refresh_adopts_router_header_encoding_from_9f6d772(
    tier: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.integrations import apim_router_global_policy as router_policy
    from turnstile_core.integrations.apim_model_availability import compose_availability_parent
    from turnstile_core.integrations.apim_policy_components import component_digest
    from turnstile_core.integrations.apim_storage_routes import (
        refresh_fixed_budget_execution_policy,
        upgrade_fixed_pool_program,
    )

    plan = onboarding_plan(service_tier=tier)
    capability = self_hosted_gateway_capability_for_tier(plan.service_tier)
    template = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    current_condition = (
        "routerName.Any(character =&gt; character &lt; 32 || character &gt; 126)"
    )
    legacy_condition = "routerName.Any(character =&gt; character &gt; 127)"
    with monkeypatch.context() as previous:
        for name, count in (("_INBOUND_ROUTER_ENGINE_XML", 2), ("_OUTBOUND_ROUTER_ENGINE_XML", 1)):
            source = getattr(router_policy, name)
            assert source.count(current_condition) == count
            previous.setattr(
                router_policy, name, source.replace(current_condition, legacy_condition),
            )
        installed = render_onboarding_fixed_program(
            plan.request, onboarding_configuration(), template, self_hosted_gateway=capability,
        )
    current = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), template, self_hosted_gateway=capability,
    )
    canonical = compose_availability_parent(
        onboarding_canonical_parent(template, plan.request.api_id),
    )

    def refresh(program: FixedRouteProgram) -> FixedRouteProgram:
        return upgrade_fixed_pool_program(
            refresh_fixed_budget_execution_policy(
                program.parent_policy, program.binding, canonical,
            ),
            program.binding,
        )

    refreshed = refresh(installed)
    again = refresh(refreshed)

    assert installed.parent_policy != current.parent_policy
    assert installed.operation_policies != current.operation_policies
    assert component_digest(parse_policy(refreshed.parent_policy)) == component_digest(
        parse_policy(current.parent_policy),
    )
    assert refreshed.operation_policies == current.operation_policies
    assert again.parent_policy == refreshed.parent_policy
    assert again.operation_policies == refreshed.operation_policies


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_program_refresh_adopts_the_current_responses_cache_write_usage(
    tier: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.integrations import apim_budget_execution as budget_execution
    from turnstile_core.integrations.apim_model_availability import compose_availability_parent
    from turnstile_core.integrations.apim_policy_components import component_digest
    from turnstile_core.integrations.apim_storage_routes import (
        refresh_fixed_budget_execution_policy,
        upgrade_fixed_pool_program,
    )

    plan = onboarding_plan(service_tier=tier)
    capability = self_hosted_gateway_capability_for_tier(plan.service_tier)
    template = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    legacy_template = template.replace(
        '          ?? (long?)usage?[&quot;input_tokens_details&quot;]?'
        '[&quot;cache_write_tokens&quot;]\n',
        "",
    )
    legacy_usage = budget_execution.COMPLETE_USAGE.replace(
        'var write = anthropic ? usage["cache_creation_input_tokens"]\n'
        '        : (usage["prompt_tokens_details"] ?? usage["input_tokens_details"])'
        '?["cache_write_tokens"];',
        'var write = anthropic ? usage["cache_creation_input_tokens"] : '
        'usage["prompt_tokens_details"]?["cache_write_tokens"];',
    )
    with monkeypatch.context() as previous:
        previous.setattr(budget_execution, "COMPLETE_USAGE", legacy_usage)
        installed = render_onboarding_fixed_program(
            plan.request, onboarding_configuration(), legacy_template,
            self_hosted_gateway=capability,
        )
    current = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), template, self_hosted_gateway=capability,
    )
    canonical = compose_availability_parent(
        onboarding_canonical_parent(template, plan.request.api_id),
    )

    def refresh(program: FixedRouteProgram) -> FixedRouteProgram:
        return upgrade_fixed_pool_program(
            refresh_fixed_budget_execution_policy(
                program.parent_policy, program.binding, canonical,
            ),
            program.binding,
        )

    refreshed = refresh(installed)
    again = refresh(refreshed)
    assert installed.parent_policy != current.parent_policy
    assert component_digest(parse_policy(refreshed.parent_policy)) == component_digest(
        parse_policy(current.parent_policy),
    )
    assert refreshed.operation_policies == current.operation_policies
    assert again.parent_policy == refreshed.parent_policy
    assert again.operation_policies == refreshed.operation_policies


_APIM_EXPRESSION = re.compile(r"(>\s*|<!\[CDATA\[\s*|=[\"'])@[({]")
_APIM_EXPRESSION_END = {
    ">": re.compile(r"\s*<"), "<": re.compile(r"\s*\]\]>"),
    '="': re.compile('"'), "='": re.compile("'"),
}
_SINGLE_QUOTED = re.compile(r"'(?:[^'\\\n]|\\.)*'")


def apim_quote_errors(policy: str) -> list[str]:
    # APIM delimits expressions in the policy text as sent, where &quot; is no string quote to it:
    # a single quote outside literal "..." strings and // comments must close on its own line.
    errors: list[str] = []
    for match in _APIM_EXPRESSION.finditer(policy):
        opener = match.group(1)
        end = _APIM_EXPRESSION_END[opener[:2] if opener[0] == "=" else opener[0]]
        index, depth = match.end() - 1, 0
        while index < len(policy):
            if policy.startswith("//", index):
                newline = policy.find("\n", index)
                index = len(policy) if newline < 0 else newline
                continue
            character = policy[index]
            if character == '"':
                index += 1
                while index < len(policy) and policy[index] not in '"\n':
                    index += 2 if policy[index] == "\\" else 1
            elif character == "'":
                quoted = _SINGLE_QUOTED.match(policy, index)
                if quoted is None:
                    break
                index = quoted.end() - 1
            elif character in "({":
                depth += 1
            elif character in ")}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if depth != 0 or not end.match(policy, index + 1):
            line = max(policy.rfind("\n", 0, index) + 1, index - 80)
            errors.append(policy[line:index + 40].strip())
    return errors


@pytest.mark.parametrize(("policy", "valid"), [
    ("<value>@(path.Split('/')[0])</value>", True),
    ("<value>@(now.ToString(&quot;yyyy-MM-dd'T'HH&quot;) + '\\'')</value>", True),
    ('<set-body>@{\n  // the caller\'s body\n  return "it\'s";\n}</set-body>', True),
    ('<set-variable name="filter" value="@(&quot;eq \'&quot; + key + &quot;\'&quot;)" />', True),
    ("<value>@(&quot;The application's budget&quot;)</value>", False),
    (
        '<set-variable name="filter" value="@{\n  return &quot;eq \'&quot; + key\n'
        '    + &quot;\'&quot;;\n}" />',
        False,
    ),
    ("<value>@(&quot;it's&quot;) + x('y')</value>", False),
])
def test_apim_quote_check_rejects_unclosed_single_quotes(policy: str, valid: bool) -> None:
    assert (apim_quote_errors(policy) == []) is valid


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_policies_sent_to_apim_close_every_single_quote(tier: str) -> None:
    from turnstile_core.integrations.apim_model_availability import compose_availability_parent
    from turnstile_core.integrations.apim_storage_routes import (
        refresh_fixed_budget_execution_policy,
        upgrade_fixed_pool_program,
    )

    plan = onboarding_plan(service_tier=tier)
    template = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    program = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), template,
        self_hosted_gateway=self_hosted_gateway_capability_for_tier(plan.service_tier),
    )
    canonical = compose_availability_parent(
        onboarding_canonical_parent(template, plan.request.api_id),
    )
    refreshed = upgrade_fixed_pool_program(
        refresh_fixed_budget_execution_policy(program.parent_policy, program.binding, canonical),
        program.binding,
    )
    policies = {
        path.name: path.read_text()
        for path in sorted((REPOSITORY_ROOT / "infra/policies").glob("*.xml"))
    }
    for label, compiled in (("compiled", program), ("refreshed", refreshed)):
        policies[f"{label} parent"] = compiled.parent_policy
        policies.update({f"{label} {name}": xml for name, xml in compiled.operation_policies})

    assert all(_APIM_EXPRESSION.search(policies[name]) for name in (
        "foundry-finops-policy.xml", "compiled parent", "refreshed parent",
    ))
    assert {
        name: errors for name, policy in policies.items() if (errors := apim_quote_errors(policy))
    } == {}


def assert_decision_and_image_program(
    binding: Any, parent_policy: str, policies: dict[str, str],
    contracts: dict[str, tuple[str, str]],
) -> None:
    from turnstile_core.domain.image_profiles import DEFAULT_IMAGE_GENERATION_LIMITS
    from turnstile_core.domain.storage_routes import canonical_document

    assert binding.supported_profiles == (
        "openai-v1", "anthropic-messages", "openai-images", "microsoft-decision",
    )
    assert binding.decision_enabled is True
    assert binding.image_limits == DEFAULT_IMAGE_GENERATION_LIMITS
    assert contracts["microsoft-decision"] == ("POST", "/providers/microsoft/v1/systemone")
    assert set(policies) == set(contracts)
    variables = {
        node.get("name"): node.get("value")
        for node in parse_policy(parent_policy).iter("set-variable")
    }
    assert variables["routeDecisionEnabled"] == "@(true)"
    assert json.loads(variables["routeProfiles"] or "") == list(binding.supported_profiles)
    assert variables["routeImageLimits"] == canonical_document(DEFAULT_IMAGE_GENERATION_LIMITS)
    images = parse_policy(policies["images-generations"])
    assert "route_image_capacity_unavailable" not in policies["images-generations"]
    # Without a published image route the operation still answers 503 instead of forwarding.
    assert any(
        "IsNullOrEmpty" in when.get("condition", "")
        and "routeContent" in when.get("condition", "")
        and when.find("return-response/set-status[@code='503']") is not None
        for when in images.iter("when")
    )


@pytest.mark.parametrize("tier", ["Developer", "StandardV2"])
def test_onboarded_gateway_program_enables_decision_and_images(tier: str) -> None:
    plan = onboarding_plan(service_tier=tier)
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    program = render_onboarding_fixed_program(
        plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=self_hosted_gateway_capability_for_tier(plan.service_tier),
    )
    assert_decision_and_image_program(
        program.binding, program.parent_policy, dict(program.operation_policies),
        {name: (method, path) for name, method, path in program.operation_contracts},
    )


@pytest.mark.parametrize("display_name", [None, "Team inference gateway"])
def test_onboarding_queues_disabled_profile_and_preserves_retry_credentials(
    display_name: str | None,
) -> None:
    repository = InMemoryRepository()
    original = deepcopy(repository.gateways)
    plan = onboarding_plan()
    plan.request.name = display_name
    first = repository.create_apim_onboarding_operation(
        plan, b"encrypted-original", "owner@example.com"
    )
    second = repository.create_apim_onboarding_operation(
        plan, b"encrypted-duplicate", "owner@example.com"
    )
    assert first["id"] == second["id"]
    assert first["status"] == "queued"
    assert repository.gateway_release_operation_secret(first["id"]) == b"encrypted-original"
    assert repository.gateways[:-1] == original
    profile = repository.gateways[-1]
    assert profile["id"] == plan.gateway_profile_id
    assert profile["name"] == (
        display_name or f"{plan.service_name} ({plan.gateway_profile_id.hex[:8]})"
    )
    assert first["semantic_preview"]["request"]["name"] == display_name
    assert profile["enabled"] is False and profile["is_default"] is False
    assert profile["base_url"] is None
    assert repository.list_apim_onboarding_operations() == [first]
    assert not repository.gateway_deployments


def test_onboarding_plan_change_cannot_overwrite_existing_intent() -> None:
    repository = InMemoryRepository()
    plan = onboarding_plan()
    original = repository.create_apim_onboarding_operation(plan, b"encrypted", "owner@example.com")
    with pytest.raises(ValueError, match="plan has changed"):
        repository.create_apim_onboarding_operation(
            plan.model_copy(update={"confirmation_sha256": "b" * 64}),
            b"replacement",
            "owner@example.com",
        )
    assert repository.list_apim_onboarding_operations() == [original]


def test_blocked_onboarding_does_not_create_partial_platform_state() -> None:
    repository = InMemoryRepository()
    original = deepcopy(repository.gateways)
    plan = onboarding_plan().model_copy(
        update={"ready": False, "blockers": ["management_permission_missing"]}
    )
    with pytest.raises(ValueError, match="prerequisites"):
        repository.create_apim_onboarding_operation(plan, b"encrypted", "owner@example.com")
    assert repository.gateways == original
    assert repository.list_apim_onboarding_operations() == []


def test_onboarding_name_conflict_preserves_existing_profile() -> None:
    repository = InMemoryRepository()
    original = deepcopy(repository.gateways)
    plan = onboarding_plan()
    plan.request.name = repository.gateways[0]["name"]
    with pytest.raises(ValueError, match="identity or name"):
        repository.create_apim_onboarding_operation(plan, b"encrypted", "owner@example.com")
    assert repository.gateways == original
    assert repository.list_apim_onboarding_operations() == []


class OnboardingToken:
    def token(self, resource: str) -> str:
        assert resource == "https://management.azure.com/"
        return "isolated-unit-token"


@pytest.mark.parametrize("target", ["__GATEWAY_PROFILE_ID__", str(UUID(int=987))])
def test_initialization_rejects_invalid_final_policy_before_http(target: str) -> None:
    plan = onboarding_plan()
    calls: list[str] = []
    uploaded: dict[str, Any] | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(404) if uploaded is None else httpx.Response(200, json=uploaded)
        uploaded = json.loads(request.content)
        return httpx.Response(201)

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        client = AzureApimOnboardingClient(
            plan.request, token_provider=OnboardingToken(), client=transport,
        )
        policy = (
            '<policies><inbound><set-variable name="telemetryGatewayProfileId" '
            f'value="{target}" /></inbound></policies>'
        )
        with pytest.raises(PolicyCompilationError):
            client.ensure_policy(client.api_resource_path, policy)
    assert calls == []


@pytest.mark.parametrize("null_bearer", [False, True])
def test_managed_initialization_creates_only_owned_api_and_empty_catalog(null_bearer: bool) -> None:
    plan = onboarding_plan()
    resources: dict[str, dict[str, object]] = {}
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json

        methods.append(request.method)
        assert "/gateways" not in request.url.path
        assert "/deployments" not in request.url.path
        path = request.url.path
        if request.method == "GET":
            return (
                httpx.Response(200, json=resources[path])
                if path in resources
                else httpx.Response(404)
            )
        assert request.method == "PUT"
        assert request.headers["If-None-Match"] == "*"
        value = json.loads(request.content)
        if null_bearer and "subscriptionKeyParameterNames" in value["properties"]:
            value["properties"]["subscriptionKeyParameterNames"]["bearer"] = None
        resources[path] = {"id": path, **value}
        return httpx.Response(201, json=resources[path])

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimOnboardingClient(
            plan.request, token_provider=OnboardingToken(), client=http
        )
        client.ensure_api()
        client.ensure_catalog()
        writes = methods.count("PUT")
        client.ensure_api()
        client.ensure_catalog()
    assert methods.count("PUT") == writes == 3
    assert len(resources) == 3
    catalog = next(value for path, value in resources.items() if path.endswith("/policies/policy"))
    assert '"data":[]' in str(catalog)
    assert "forward-request" not in str(catalog)


def test_managed_initialization_refuses_an_unowned_api_without_writes() -> None:
    plan = onboarding_plan()
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "id": request.url.path,
                "properties": {"description": "Existing customer API", "path": plan.api_path},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimOnboardingClient(
            plan.request, token_provider=OnboardingToken(), client=http
        )
        with pytest.raises(PolicyCompilationError, match="unowned"):
            client.ensure_api()
    assert methods == ["GET"]


@pytest.mark.parametrize("revision", ["1", "2"])
@pytest.mark.parametrize("key_parameters,allowed", [
    ({}, True),
    ({"bearer": None}, True),
    ({"bearer": "Authorization"}, False),
    ({"bearer": ""}, False),
    ({"bearer": False}, False),
    ({"header": "X-Other-Key"}, False),
    ({"query": "other-key"}, False),
    ({"bearer": None, "header": "X-Other-Key"}, False),
    ({"bearer": None, "query": "other-key"}, False),
    ({"unknown": None}, False),
])
def test_existing_api_uses_arm_readback_type_and_requires_initial_revision(
    revision: str, key_parameters: dict[str, object], allowed: bool,
) -> None:
    plan = onboarding_plan()
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "id": request.url.path,
                "properties": {
                    "type": "http",
                    "displayName": "Turnstile Model API",
                    "description": (
                        f"Turnstile managed APIM initialization v1: {plan.gateway_profile_id}"
                    ),
                    "apiRevision": revision,
                    "isCurrent": True,
                    "path": plan.api_path,
                    "protocols": ["https"],
                    "subscriptionRequired": True,
                    "subscriptionKeyParameterNames": {
                        "header": "Ocp-Apim-Subscription-Key",
                        "query": "subscription-key",
                        **key_parameters,
                    },
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimOnboardingClient(
            plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        if revision == "1" and allowed:
            client.ensure_api()
        else:
            with pytest.raises(PolicyCompilationError, match="drifted"):
                client.ensure_api()
    assert methods == ["GET"]


@pytest.mark.parametrize("field,value", [
    ("api_id", "customer-api"),
    ("api_path", "existing/business"),
    ("probe_subscription_id", "customer-subscription"),
    ("managed_gateway_origin", "https://another-apim.azure-api.net"),
    ("managed_gateway_origin", "https://customer-apim.azure-api.net/another/path"),
])
def test_initialization_plan_cannot_select_arbitrary_resources(field: str, value: str) -> None:
    plan = onboarding_plan().model_dump(mode="json")
    plan[field] = value
    with pytest.raises(ValueError):
        ApimOnboardingPlan.model_validate(plan)


def onboarding_configuration() -> ApimOnboardingConfiguration:
    request = onboarding_plan().request
    prefix = f"/subscriptions/{request.subscription_id}/resourceGroups/platform-rg/providers/"
    return ApimOnboardingConfiguration(
        management_tenant_id=request.tenant_id,
        event_hub_resource_id=prefix
        + "Microsoft.EventHub/namespaces/platform-events/eventhubs/token-usage",
        ledger_table_resource_id=(
            prefix + "Microsoft.Storage/storageAccounts/platformledger/"
            "tableServices/default/tables/TurnstileLedger"
        ),
        route_table_resource_id=(
            prefix + "Microsoft.Storage/storageAccounts/platformroutes/"
            "tableServices/default/tables/ModelRoutes"
        ),
        credential_vault_resource_id=prefix + "Microsoft.KeyVault/vaults/platform-route-secrets",
        employee_tenant_id=request.tenant_id,
        employee_client_id=UUID("40000000-0000-4000-8000-000000000002"),
        employee_audience="api://platform-inference",
        organization_id="platform-org",
        organization_name="Platform & Engineering",
        client_map={"40000000-0000-4000-8000-000000000002": "Desktop Client"},
        surface_map=[EmployeeClientSurface(name="Code Client", tokens=["agent-sdk", "desktop"])],
    )


@pytest.mark.parametrize("header", ["x-org-id", "x-org-name"])
def test_onboarding_rejects_employee_organization_different_from_configuration(header: str) -> None:
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    root = parse_policy(source.read_text())
    value = root.find(f"./inbound//set-header[@name='{header}']/value")
    assert value is not None
    value.text = "another-organization"
    with pytest.raises(PolicyCompilationError, match="Employee organization attribution"):
        render_onboarding_fixed_program(
            onboarding_plan().request, onboarding_configuration(), serialize_policy(root),
            self_hosted_gateway=CapabilitySupport.UNSUPPORTED,
        )


def test_onboarding_fixed_program_derives_independent_target_bindings() -> None:
    request = onboarding_plan().request
    neighbor = ApimOnboardingRequest(
        tenant_id=request.tenant_id,
        resource_id=request.resource_id.rsplit("/", 1)[0] + "/another-customer-apim",
    )
    source = (Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml")
    configuration = onboarding_configuration()
    programs = [
        render_onboarding_fixed_program(
            item, configuration, source.read_text(),
            self_hosted_gateway=CapabilitySupport.UNSUPPORTED,
        )
        for item in (request, neighbor)
    ]
    assert len({program.binding.scope.partition for program in programs}) == 2
    assert len({program.binding.program_sha256 for program in programs}) == 2
    for item, program in zip((request, neighbor), programs, strict=True):
        assert program.binding.scope.target_id == str(item.gateway_profile_id)
        assert program.binding.scope.api_id == item.api_id
        assert program.binding.publisher_subscription_id == item.probe_subscription_id
        assert program.binding.model_additions_enabled is True
        assert program.binding.table_endpoint == "https://platformroutes.table.core.windows.net"
        assert program.binding.ledger_table_name == "TurnstileLedger"
        assert program.binding.trusted_vault_origins == (
            "https://platform-route-secrets.vault.azure.net",
        )
        assert "__GATEWAY_PROFILE_ID__" not in program.parent_policy
        assert ("models", "GET", "/v1/models") in program.operation_contracts
        installation = program.installation(
            "b" * 64, f"https://{item.service_name}.azure-api.net/{item.api_path}",
        )
        assert installation.program.scope == program.binding.scope
        assert installation.program.program_sha256 == program.binding.program_sha256
        assert installation.model_additions_enabled is True
        assert installation.operation_contracts["models"] == ("GET", "/v1/models")


def test_onboarding_has_no_development_switch_and_checks_shared_platform_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "apim_onboarding_enabled" not in Settings.model_fields
    assert "apim_onboarding_configuration" not in Settings.model_fields
    monkeypatch.setenv("APIM_ONBOARDING_ENABLED", "false")
    monkeypatch.setenv("APIM_ONBOARDING_CONFIGURATION", "obsolete and ignored")
    source = onboarding_plan().request
    values: dict[str, Any] = {
        "_env_file": None,
        "azure_subscription_id": str(source.subscription_id),
        "apim_resource_group": source.resource_group,
        "apim_service_name": source.service_name,
        "apim_target_registry_enabled": True,
        "gateway_release_worker_enabled": True,
        "budget_execution_enabled": True,
        "ledger_table_endpoint": "https://platformledger.table.core.windows.net",
        "ledger_table_name": "TurnstileLedger",
        "credential_encryption_key": SecretStr(Fernet.generate_key().decode()),
    }
    settings = Settings(**values)
    assert settings.control_plane_enabled is False
    assert settings.ledger_sync_enabled is False
    assert settings.apim_gateway_url is None
    assert apim_onboarding_available(settings) is True
    for key, value in (
        ("apim_target_registry_enabled", False),
        ("gateway_release_worker_enabled", False),
        ("budget_execution_enabled", False),
        ("ledger_table_endpoint", None),
        ("credential_encryption_key", None),
        ("azure_subscription_id", None),
        ("apim_resource_group", None),
        ("apim_service_name", None),
    ):
        assert not apim_onboarding_available(Settings(**{**values, key: value})), key


@pytest.mark.parametrize("problem", [None, "missing", "foreign", "secret", "unowned", "invalid"])
def test_platform_governance_configuration_is_a_readonly_owned_resource(
    problem: str | None,
) -> None:
    request = onboarding_plan().request
    resource = request.resource_id + f"/namedValues/{PLATFORM_GOVERNANCE_NAMED_VALUE}"
    expected = onboarding_configuration()
    calls: list[tuple[str, str]] = []

    def handle(incoming: httpx.Request) -> httpx.Response:
        calls.append((incoming.method, incoming.url.path))
        assert incoming.method == "GET" and incoming.url.path == resource
        if problem == "missing":
            return httpx.Response(404)
        return httpx.Response(200, json={
            "id": resource + "-other" if problem == "foreign" else resource,
            "properties": {
                "displayName": PLATFORM_GOVERNANCE_NAMED_VALUE,
                "secret": problem == "secret",
                "tags": [] if problem == "unowned" else [PLATFORM_GOVERNANCE_TAG],
                "value": "invalid" if problem == "invalid" else expected.model_dump_json(),
            },
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimPlatformConfigurationClient(
            onboarding_client_configuration(request).settings,
            token_provider=OnboardingToken(), client=http,
        )
        if problem is None:
            assert client.configuration() == expected
        else:
            with pytest.raises(PolicyCompilationError, match="configuration"):
                client.configuration()
    assert calls == [("GET", resource)]


def test_initialization_renders_canonical_governance_without_legacy_accounting() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    ).read_text()
    request = onboarding_plan().request
    policy = render_onboarding_parent(request, onboarding_configuration(), source)
    root = parse_policy(policy)
    assert root.find("inbound/set-variable[@name='budgetExecutionVersion']") is not None
    assert root.find("inbound/choose/when/set-variable[@name='budgetExecutionFence']") is not None
    assert request.api_id in policy
    assert "https://platformledger.table.core.windows.net" in policy
    assert str(request.gateway_profile_id) in policy
    assert "Platform &amp; Engineering" in policy
    assert "__LEDGER_TABLE_ENDPOINT__" not in policy
    assert "__GATEWAY_PROFILE_ID__" not in policy
    assert not list(root.iter("set-backend-service"))
    assert "Desktop Client" in policy and "Code Client" in policy and "agent-sdk" in policy


@pytest.mark.parametrize("ledger_mismatch", [False, True])
def test_factory_lazily_loads_shared_governance_and_checks_the_real_ledger(
    monkeypatch: pytest.MonkeyPatch, ledger_mismatch: bool,
) -> None:
    configuration = onboarding_configuration()
    platform = onboarding_plan().request
    settings = Settings(
        azure_subscription_id=str(platform.subscription_id),
        apim_resource_group=platform.resource_group,
        apim_service_name=platform.service_name,
        apim_target_registry_enabled=True,
        gateway_release_worker_enabled=True,
        budget_execution_enabled=True,
        ledger_table_endpoint="https://platformledger.table.core.windows.net",
        ledger_table_name="DifferentLedger" if ledger_mismatch else "TurnstileLedger",
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    reader = Mock(return_value=configuration)
    monkeypatch.setattr(AzureApimPlatformConfigurationClient, "configuration", reader)
    service = build_apim_onboarding_service(InMemoryRepository(), settings)
    assert service.operations().available is True
    reader.assert_not_called()
    if ledger_mismatch:
        with pytest.raises(PolicyCompilationError, match="budget ledger"):
            service._configuration_digest()
    else:
        original = service._configuration_digest()
        assert len(original) == 64
        assert service._configuration_digest() == original
    reader.assert_called_once_with()


def test_deployment_does_not_emit_removed_onboarding_environment_flags() -> None:
    root = Path(__file__).resolve().parents[3]
    for path in (
        "infra/main.bicep", "infra/modules/data-plane.bicep",
        "infra/modules/control-plane-function.bicep", "infra/modules/apim-integration.bicep",
    ):
        source = (root / path).read_text()
        assert "APIM_ONBOARDING_" not in source
        assert "apimOnboardingEnabled" not in source
    integration = (root / "infra/modules/apim-integration.bicep").read_text()
    assert PLATFORM_GOVERNANCE_NAMED_VALUE in integration
    assert "secret: false" in integration
    assert "scope: platformGovernance" in integration
    publisher = (root / "functions/control_plane/function_app.py").read_text()
    worker = next(
        node for node in ast.walk(ast.parse(publisher))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "GatewayReleaseOperationWorker"
    )
    handler = next(value.value for value in worker.keywords if value.arg == "onboarding_handler")
    assert isinstance(handler, ast.IfExp)
    assert ast.unparse(handler.body) == (
        "build_apim_onboarding_service(repository, settings).advance"
    )
    assert isinstance(handler.orelse, ast.Constant) and handler.orelse.value is None
    assert "apim_onboarding_enabled" not in publisher
    assert "apim_onboarding_configuration" not in publisher


class OnboardingHttpState:
    def __init__(self) -> None:
        self.plan = onboarding_plan()
        self.resources: dict[str, dict[str, Any]] = {
            self.plan.request.resource_id: {
                "id": self.plan.request.resource_id,
                "sku": {"name": "StandardV2"},
                "location": "eastus",
                "tags": {"customer": "retained"},
                "identity": {
                    "type": "SystemAssigned",
                    "principalId": "50000000-0000-4000-8000-000000000002",
                },
                "properties": {
                    "gatewayUrl": str(self.plan.managed_gateway_origin).rstrip("/"),
                    "provisioningState": "Succeeded",
                },
            },
        }
        self.requests: list[httpx.Request] = []
        self.catalog_identity = str(self.plan.gateway_profile_id)
        self.native_gateway = "managed"
        self.anonymous_status = 401
        self.catalog_status = 200
        self.catalog_body: dict[str, Any] | None = None
        self.permissions: list[dict[str, Any]] = [{"actions": ["*"], "notActions": []}]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert "/gateways" not in path
        if request.url.host != "management.azure.com":
            assert request.method == "GET"
            assert path == f"/{self.plan.api_path}/v1/models"
            key = request.headers.get("Ocp-Apim-Subscription-Key", "")
            active_keys = {
                str(value["properties"].get("primaryKey"))
                for resource, value in self.resources.items()
                if resource.startswith(self.plan.request.resource_id + "/subscriptions/")
                and value["properties"].get("state") == "active"
            }
            if key.startswith("unit-") or key in active_keys:
                catalog = self.catalog_body
                if catalog is None:
                    operation = (
                        self.plan.request.resource_id + "/apis/" + self.plan.api_id
                        + "/operations/models"
                    )
                    catalog = {"object": "list", "data": []}
                    if operation in self.resources:
                        catalog = {
                            "data": [], "has_more": False, "first_id": None, "last_id": None,
                        }
                return httpx.Response(
                    self.catalog_status,
                    json=catalog,
                    headers={
                        "x-turnstile-onboarding": self.catalog_identity,
                        "x-turnstile-native-gateway": self.native_gateway,
                    },
                )
            return httpx.Response(self.anonymous_status)
        if request.method == "GET":
            if path.endswith("/providers/Microsoft.Authorization/permissions"):
                return httpx.Response(200, json={"value": self.permissions})
            if path == self.plan.request.resource_id + "/apis":
                prefix = path + "/"
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            value
                            for key, value in self.resources.items()
                            if key.startswith(prefix) and "/" not in key[len(prefix) :]
                        ],
                    },
                )
            value = self.resources.get(path)
            return (
                httpx.Response(200, json=value, headers={"ETag": '"version-1"'})
                if value is not None
                else httpx.Response(404)
            )
        if request.method == "POST" and path.endswith("/listSecrets"):
            value = self.resources[path.removesuffix("/listSecrets")]["properties"]
            return httpx.Response(
                200, json={name: value[name] for name in ("primaryKey", "secondaryKey")}
            )
        body = json.loads(request.content)
        if request.method == "PATCH":
            assert path == self.plan.request.resource_id
            assert set(body) == {"identity"}
            self.resources[path]["identity"] = body["identity"]
            return httpx.Response(202, json=self.resources[path])
        assert request.method == "PUT"
        assert request.headers.get("If-None-Match") == "*" or (
            request.headers.get("If-Match") == '"version-1"'
        )
        if "/roleAssignments/" in path:
            body["properties"]["scope"] = path.rsplit("/providers/", 1)[0]
        self.resources[path] = {"id": path, **body}
        return httpx.Response(201, json=self.resources[path])


def onboarding_keys() -> dict[str, dict[str, str]]:
    return {
        purpose: {kind: f"unit-{purpose}-{kind}-" + "x" * 32 for kind in ("primary", "secondary")}
        for purpose in ("dashboard", "probe")
    }


def test_fixed_program_initialization_creates_once_and_never_replays_unknown_writes() -> None:
    state = OnboardingHttpState()
    source = (Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml")
    program = render_onboarding_fixed_program(
        state.plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=CapabilitySupport.UNSUPPORTED,
    )
    intents: dict[str, str] = {}

    def record(resource: str, digest: str) -> bool:
        if resource in intents:
            assert intents[resource] == digest
            return False
        intents[resource] = digest
        return True

    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request, token_provider=OnboardingToken(), client=http,
        )
        client.ensure_api()
        client.ensure_fixed_program(program, record)
        writes = sum(request.method == "PUT" for request in state.requests)
        assert len(intents) == 2 * len(program.operation_contracts) + 1
        uploaded = state.resources[
            state.plan.request.resource_id + client.api_resource_path + "/policies/policy"
        ]["properties"]["value"]
        assert len(uploaded.encode()) <= 512 * 1024 - 32 * 1024
        client.ensure_fixed_program(program, record)
        assert sum(request.method == "PUT" for request in state.requests) == writes
        missing = client.api_resource_path + "/operations/models/policies/policy"
        del state.resources[state.plan.request.resource_id + missing]
        with pytest.raises(RetryablePublicationError, match="outcome is unknown"):
            client.ensure_fixed_program(program, record)
        assert sum(request.method == "PUT" for request in state.requests) == writes


def test_onboarding_preflight_supports_managed_only_tier_without_writes() -> None:
    state = OnboardingHttpState()
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        plan = client.inspect(
            management_tenant_id=state.plan.request.tenant_id,
            configuration_digest="configuration",
        )
        assert plan.ready and not plan.blockers and plan.service_tier == "StandardV2"
        assert all(request.method == "GET" for request in state.requests)
        state.resources[state.plan.request.resource_id + "/apis/customer-api"] = {
            "name": "customer-api",
            "id": state.plan.request.resource_id + "/apis/customer-api",
            "properties": {"path": plan.api_path, "description": "Customer owned"},
        }
        blocked = client.inspect(
            management_tenant_id=state.plan.request.tenant_id,
            configuration_digest="configuration",
        )
        assert not blocked.ready and blocked.blockers == ["dedicated_api_path_conflict"]
    assert all(request.method == "GET" for request in state.requests)


def test_initialization_preserves_user_assigned_identity_and_customer_tags() -> None:
    state = OnboardingHttpState()
    service = state.resources[state.plan.request.resource_id]
    identity_id = (
        f"/subscriptions/{state.plan.request.subscription_id}/resourceGroups/customer-rg/"
        "providers/Microsoft.ManagedIdentity/userAssignedIdentities/customer-identity"
    )
    service["identity"] = {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}}
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        with pytest.raises(RetryablePublicationError, match="being initialized"):
            client.ensure_governance_dependencies(onboarding_configuration())
    assert service["identity"] == {
        "type": "SystemAssigned, UserAssigned",
        "userAssignedIdentities": {identity_id: {}},
    }
    assert service["tags"] == {"customer": "retained"}
    assert [request.method for request in state.requests] == ["GET", "PATCH"]


def test_governance_dependencies_are_scoped_and_idempotent() -> None:
    state = OnboardingHttpState()
    configuration = onboarding_configuration()
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        principal = client.ensure_governance_dependencies(configuration)
        client.ensure_governance_dependencies(configuration)
    writes = [request for request in state.requests if request.method != "GET"]
    assert len(writes) == 5
    assert str(principal) == "50000000-0000-4000-8000-000000000002"
    roles = [
        value["properties"]
        for path, value in state.resources.items()
        if "/roleAssignments/" in path
    ]
    assert {role["scope"] for role in roles} == {
        configuration.ledger_table_resource_id,
        configuration.event_hub_resource_id,
        configuration.route_table_resource_id,
        configuration.credential_vault_resource_id,
    }
    expected_roles = {
        configuration.route_table_resource_id: "76199698-9eea-4c19-bc75-cec21354c6b6",
        configuration.credential_vault_resource_id: "4633458b-17de-408a-b874-0445c86b69e6",
    }
    for role in roles:
        if role["scope"] in expected_roles:
            assert role["roleDefinitionId"].endswith("/" + expected_roles[role["scope"]])
    assert all(request.method == "PUT" for request in writes)


@pytest.mark.parametrize("identity_value", ["systemAssigned", "another-identity"])
@pytest.mark.parametrize("secret", [False, True])
def test_logger_readback_resolves_azure_managed_identity_reference(
    identity_value: str, secret: bool,
) -> None:
    state = OnboardingHttpState()
    configuration = onboarding_configuration()
    reference = "logger-identity-unit"
    named_value_path = state.plan.request.resource_id + f"/namedValues/{reference}"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == named_value_path + "/listValue":
            state.requests.append(request)
            assert request.method == "POST"
            return httpx.Response(200, json={"value": identity_value})
        return state.handle(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request, token_provider=OnboardingToken(), client=http,
        )
        client.ensure_governance_dependencies(configuration)
        logger = next(value for path, value in state.resources.items() if "/loggers/" in path)
        logger["properties"]["credentials"]["identityClientId"] = "{{" + reference + "}}"
        state.resources[named_value_path] = {
            "id": named_value_path,
            "properties": {
                "displayName": "Logger-Credentials-unit", "secret": secret,
                **({} if secret else {"value": identity_value}),
            },
        }
        original = deepcopy(state.resources)
        state.requests.clear()
        if identity_value == "systemAssigned":
            client.ensure_governance_dependencies(configuration)
        else:
            with pytest.raises(PolicyCompilationError, match="Logger"):
                client.ensure_governance_dependencies(configuration)
        assert state.resources == original
    assert any(
        request.url.path == named_value_path + "/listValue" for request in state.requests
    ) is secret
    assert all(
        request.method == "GET"
        or (request.method == "POST" and request.url.path == named_value_path + "/listValue")
        for request in state.requests
    )


@pytest.mark.parametrize("drift", [
    "logger_identity", "ownership", "event_hub_resource", "endpoint", "event_hub_name",
    "extra_credential", "invalid_reference", "missing_reference", "foreign_reference",
    "key_vault_reference", "missing_secret_flag",
])
def test_logger_readback_rejects_drift_before_reading_reference_value(drift: str) -> None:
    state = OnboardingHttpState()
    configuration = onboarding_configuration()
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request, token_provider=OnboardingToken(), client=http,
        )
        client.ensure_governance_dependencies(configuration)
        logger = next(value for path, value in state.resources.items() if "/loggers/" in path)
        credentials = logger["properties"]["credentials"]
        credentials["identityClientId"] = "{{logger-identity-unit}}"
        reference_path = state.plan.request.resource_id + "/namedValues/logger-identity-unit"
        reference: dict[str, Any] = {
            "id": reference_path,
            "properties": {"displayName": "Logger-Credentials-unit", "secret": True},
        }
        state.resources[reference_path] = reference
        if drift == "logger_identity":
            logger["id"] += "-other"
        elif drift == "ownership":
            logger["properties"]["description"] = "Customer Logger"
        elif drift == "event_hub_resource":
            logger["properties"]["resourceId"] += "-other"
        elif drift == "endpoint":
            credentials["endpointAddress"] = "other.servicebus.windows.net"
        elif drift == "event_hub_name":
            credentials["name"] = "another-hub"
        elif drift == "extra_credential":
            credentials["connectionString"] = None
        elif drift == "invalid_reference":
            credentials["identityClientId"] = "{{../other}}"
        elif drift == "missing_reference":
            del state.resources[reference_path]
        elif drift == "foreign_reference":
            reference["id"] += "-other"
        elif drift == "key_vault_reference":
            reference["properties"]["keyVault"] = {"secretIdentifier": "external"}
        else:
            del reference["properties"]["secret"]
        original = deepcopy(state.resources)
        state.requests.clear()
        with pytest.raises(PolicyCompilationError, match="Logger"):
            client.ensure_governance_dependencies(configuration)
        assert state.resources == original
    assert all(request.method == "GET" for request in state.requests)


def test_system_subscriptions_stay_suspended_until_explicit_activation() -> None:
    state = OnboardingHttpState()
    keys = onboarding_keys()
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        client.ensure_api()
        client.ensure_subscriptions(keys)
        subscriptions = [
            value["properties"]
            for path, value in state.resources.items()
            if path.startswith(state.plan.request.resource_id + "/subscriptions/")
        ]
        assert len(subscriptions) == 2
        assert all(item["state"] == "suspended" for item in subscriptions)
        client.ensure_subscriptions(keys, activate=True)
        writes = sum(request.method == "PUT" for request in state.requests)
        client.ensure_subscriptions(keys, activate=True)
        assert sum(request.method == "PUT" for request in state.requests) == writes
        subscription = state.resources[
            state.plan.request.resource_id
            + "/subscriptions/"
            + state.plan.dashboard_subscription_id
        ]["properties"]
        assert subscription["state"] == "active"
        subscription["primaryKey"] = "externally-rotated"
        with pytest.raises(PolicyCompilationError, match="credentials changed"):
            client.ensure_subscriptions(keys, activate=True)
        assert sum(request.method == "PUT" for request in state.requests) == writes


@pytest.mark.parametrize("failure", [None, "wrong_target", "self_hosted", "anonymous", "pending"])
def test_managed_readiness_requires_identity_and_rejection(failure: str | None) -> None:
    state = OnboardingHttpState()
    if failure == "wrong_target":
        state.catalog_identity = "another-target"
    elif failure == "self_hosted":
        state.native_gateway = "another-gateway"
    elif failure == "anonymous":
        state.anonymous_status = 200
    elif failure == "pending":
        state.catalog_status = 503
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        if failure:
            with pytest.raises((PolicyCompilationError, RetryablePublicationError)):
                client.verify_managed_gateway(state.plan, onboarding_keys())
        else:
            client.verify_managed_gateway(state.plan, onboarding_keys())
            assert len(state.requests) == 4
    assert all(request.method == "GET" for request in state.requests)


@pytest.mark.parametrize("failure", [
    None, "nonempty", "has_more", "first_id", "last_id", "legacy", "installation", "anonymous",
])
def test_fixed_managed_readiness_requires_complete_empty_catalog_contract(
    failure: str | None,
) -> None:
    state = OnboardingHttpState()
    state.catalog_body = {"data": [], "has_more": False, "first_id": None, "last_id": None}
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    program = render_onboarding_fixed_program(
        state.plan.request, onboarding_configuration(), source.read_text(),
        self_hosted_gateway=CapabilitySupport.UNSUPPORTED,
    )
    installation = program.installation(
        "b" * 64,
        str(state.plan.managed_gateway_origin).rstrip("/") + "/" + state.plan.api_path,
    )
    if failure == "nonempty":
        state.catalog_body["data"] = [{"id": "unexpected-model"}]
    elif failure == "has_more":
        state.catalog_body["has_more"] = True
    elif failure in {"first_id", "last_id"}:
        state.catalog_body[failure] = "unexpected-model"
    elif failure == "legacy":
        state.catalog_body = {"object": "list", "data": []}
    elif failure == "installation":
        installation = installation.model_copy(update={
            "gateway_endpoint": "https://foreign.example/api",
        })
    elif failure == "anonymous":
        state.anonymous_status = 200
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request, token_provider=OnboardingToken(), client=http,
        )
        if failure:
            with pytest.raises(PolicyCompilationError):
                client.verify_managed_gateway(
                    state.plan, onboarding_keys(), fixed_installation=installation,
                )
        else:
            client.verify_managed_gateway(
                state.plan, onboarding_keys(), fixed_installation=installation,
            )
            assert len(state.requests) == 4
    assert all(request.method == "GET" for request in state.requests)


def test_failed_initialization_retry_preserves_history_and_credentials() -> None:
    repository = InMemoryRepository()
    first = repository.create_apim_onboarding_operation(
        onboarding_plan(),
        b"original-encrypted-keys",
        "owner@example.com",
    )
    stored = repository.gateway_release_operations[-1]
    stored.update(status="failed", attempt_count=5, error_code="AuthorizationFailed")
    original = deepcopy(stored)
    retry = repository.retry_apim_onboarding_operation(first["id"], "owner@example.com")
    assert retry["id"] != first["id"]
    assert retry["checkpoint"] == {"retry_of": str(first["id"])}
    assert repository.gateway_release_operations[0] == original
    assert repository.gateway_release_operation_secret(retry["id"]) == b"original-encrypted-keys"
    with pytest.raises(ValueError, match="latest failed"):
        repository.retry_apim_onboarding_operation(first["id"], "owner@example.com")


@pytest.mark.parametrize("stolen", [False, True])
def test_expired_or_stolen_lease_cannot_enable_partial_apim(stolen: bool) -> None:
    repository = InMemoryRepository()
    operation = repository.create_apim_onboarding_operation(
        onboarding_plan(),
        b"encrypted",
        "owner@example.com",
    )
    stored = repository.gateway_release_operations[-1]
    stored.update(
        status="preflight_probing",
        lease_owner="other" if stolen else "worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1 if stolen else -1),
    )
    original = deepcopy(stored)
    assert (
        repository.finish_apim_onboarding_operation(
            operation["id"],
            "worker",
            b"profile-key",
            "hint",
            "a" * 64,
        )
        is None
    )
    assert stored == original
    assert repository.gateways[-1]["enabled"] is False


def onboarding_service_fixture() -> tuple[
    InMemoryRepository,
    OnboardingHttpState,
    ApimOnboardingService,
    list[UUID],
]:
    repository = InMemoryRepository()
    state = OnboardingHttpState()
    projected: list[UUID] = []

    @contextmanager
    def clients(request: ApimOnboardingRequest) -> Iterator[AzureApimOnboardingClient]:
        with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
            yield AzureApimOnboardingClient(request, token_provider=OnboardingToken(), client=http)

    def project(plan: ApimOnboardingPlan, operation_id: UUID, worker_id: str) -> None:
        assert worker_id
        assert repository.get_gateway_release_operation(operation_id) is not None
        assert plan.gateway_profile_id == state.plan.gateway_profile_id
        assert not repository.gateways[-1]["enabled"]
        projected.append(operation_id)

    source = (
        Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    ).read_text()

    def initialize(
        plan: ApimOnboardingPlan, program: FixedRouteProgram, operation_id: UUID, worker_id: str,
    ) -> Any:
        assert worker_id
        operation = repository.get_gateway_release_operation(operation_id)
        assert operation is not None and operation["status"] == "preflight_probing"
        assert operation["checkpoint"]["fixed_program_requests"]
        instant = int(datetime.now(UTC).timestamp())
        version = str(uuid5(operation_id, "unit-empty-configuration"))
        _, manifest, dependencies = configuration_manifests(
            program.binding.scope, version, (), {}, instant,
        )
        approval = ConfigurationApproval(
            approval_id=str(uuid5(operation_id, "unit-configuration-approval")),
            scope=program.binding.scope, route_version=version, generation=1,
            manifest_sha256=manifest, dependency_manifest_sha256=dependencies,
            catalog_sha256=catalog_digest(()), entry_count=0,
            required_program_sha256=program.binding.program_sha256,
            required_profiles=(), budget_epoch=str(uuid5(operation_id, "unit-budget-epoch")),
            issued_epoch=instant, activate_before_epoch=instant + 600,
        )
        snapshot = RouteSnapshot.prepare(approval, (), {}, instant)
        head = ActiveRouteHead(approval=approval, activated_epoch=instant)
        installed = program.installation(
            "b" * 64, str(plan.managed_gateway_origin).rstrip("/") + "/" + plan.api_path,
        )
        return onboarding_fixed_publication(
            plan, program, operation_id, installed, snapshot, head, "isolated-unit-initializer",
        )

    service = ApimOnboardingService(
        repository,
        enabled=True,
        configuration=onboarding_configuration(),
        canonical_parent=source,
        cipher=CredentialCipher(Fernet.generate_key()),
        budget_ready=lambda: True,
        client_factory=clients,
        projector=project,
        fixed_initializer=initialize,
    )
    return repository, state, service, projected


def test_worker_initializes_blank_managed_apim_then_activates_atomically() -> None:
    repository, state, service, projected = onboarding_service_fixture()
    plan = service.preview(state.plan.request)
    write = ApimOnboardingStart(request=plan.request, confirmation_sha256=plan.confirmation_sha256)
    accepted = service.start(write, "owner@example.com")
    assert service.start(write, "owner@example.com").operation.id == accepted.operation.id
    assert all(request.method == "GET" for request in state.requests)
    assert repository.gateways[-1]["enabled"] is False
    worker = GatewayReleaseOperationWorker(
        repository,
        FakeApimClient(),
        onboarding_handler=service.advance,
    )
    for expected in (
        "validating_dependencies",
        "promoting",
        "verifying_readback",
        "preflight_probing",
        "succeeded",
    ):
        operation = worker.run_once("initialization-worker")
        assert operation is not None
        assert operation.status == expected, operation.error_message
        assert repository.gateways[-1]["enabled"] is (expected == "succeeded")
    assert projected == [accepted.operation.id]
    assert repository.gateway_release_operation_secret(accepted.operation.id) is None
    deployments = list(repository.gateway_deployments.values())
    assert len(deployments) == 1 and deployments[0]["native_gateway_id"] == "managed"
    assert deployments[0]["reachability_state"] == "reachable"
    assert service.operations().items[0].checkpoint["managed_gateway_verified"] is True
    effective = repository.effective_gateway_publication(plan.gateway_profile_id)
    assert effective is not None
    assert effective["resource_manifest"]["fixed_route_program"]["model_additions_enabled"] is True
    assert effective["desired_spec"]["bindings"] == []
    assert effective["status"] == "active"
    assert initialized_apim_targets(repository) == {plan.gateway_profile_id: plan.request}
    assert not any("/gateways" in request.url.path for request in state.requests)
    assert all(
        request.method == "GET"
        for request in state.requests
        if request.url.host != "management.azure.com"
    )
    model_write = foundry_publication("gpt-new-target")
    provider = next(row for row in repository.providers if row["brand_key"] == "microsoft_foundry")
    first_model = GatewayControlPlaneService(repository, target_registry_enabled=True).publish(
        model_write.model_copy(update={
            "gateway_profile_id": plan.gateway_profile_id,
            "provider": model_write.provider.model_copy(update={
                "template": None, "existing_id": provider["id"],
            }),
        }), "isolated-unit-owner",
    )
    assert first_model.base_release_id == effective["id"]
    assert set(first_model.resource_manifest) == {"model_route_addition"}
    assert first_model.apim_revision is None and first_model.policy_sha256 is None
    assert repository.effective_gateway_publication(plan.gateway_profile_id) == effective


def test_onboarding_keeps_program_intent_after_unknown_native_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, state, service, _ = onboarding_service_fixture()
    original_claim = repository.claim_gateway_release_operation

    def claim(worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        return deepcopy(original_claim(worker_id, lease_seconds))

    monkeypatch.setattr(repository, "claim_gateway_release_operation", claim)
    plan = service.preview(state.plan.request)
    accepted = service.start(
        ApimOnboardingStart(request=plan.request, confirmation_sha256=plan.confirmation_sha256),
        "unit-owner",
    )
    worker = GatewayReleaseOperationWorker(
        repository, FakeApimClient(), onboarding_handler=service.advance,
    )
    for _ in range(2):
        assert worker.run_once("unit-worker") is not None
    original = state.handle

    def unknown(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/operations/" in request.url.path:
            raise httpx.ReadTimeout("isolated unknown native outcome", request=request)
        return original(request)

    monkeypatch.setattr(state, "handle", unknown)
    assert worker.run_once("unit-worker") is not None
    stored = repository.get_gateway_release_operation(accepted.operation.id)
    assert stored is not None
    assert len(stored["checkpoint"]["fixed_program_requests"]) == 1
    assert repository.effective_gateway_publication(plan.gateway_profile_id) is None
    assert repository.gateways[-1]["enabled"] is False


@pytest.mark.parametrize("condition", [
    "fresh", "confirmed", "unknown", "epoch_changed", "head_changed",
])
def test_production_initializer_recovers_only_confirmed_empty_activation(
    monkeypatch: pytest.MonkeyPatch, condition: str,
) -> None:
    from turnstile_core.services import apim_onboarding as module
    from turnstile_core.services.budget_settlement import BudgetScopeRepository
    from turnstile_core.services.control_plane import ControlPlaneConflictError

    repository, state, unit_service, _ = onboarding_service_fixture()
    plan = unit_service.preview(state.plan.request)
    accepted = unit_service.start(
        ApimOnboardingStart(request=plan.request, confirmation_sha256=plan.confirmation_sha256),
        "unit-owner",
    )
    worker = GatewayReleaseOperationWorker(
        repository, FakeApimClient(), onboarding_handler=unit_service.advance,
    )
    for _ in range(4):
        assert worker.run_once("unit-worker") is not None
    claimed = repository.claim_gateway_release_operation("unit-initializer", 300)
    assert claimed is not None and claimed["status"] == "preflight_probing"
    instant = int(datetime.now(UTC).timestamp())
    monkeypatch.setattr(module, "time", lambda: instant)
    rollout = {"epoch": UUID(int=478), "prepared_period": datetime.now(UTC).date().replace(day=1)}
    for name in (
        "active_budget_execution_rollout", "roll_forward_budgets",
        "roll_forward_gateway_application_budgets", "prepare_budget_execution_period",
        "budget_scope_seed_candidates", "activate_budget_scope_seed", "defer_budget_scope_seed",
        "complete_budget_policy",
    ):
        monkeypatch.setattr(repository, name, Mock(return_value=rollout), raising=False)
    assert isinstance(repository, BudgetScopeRepository)
    configuration = onboarding_configuration()
    monkeypatch.setattr(AzureApimPlatformConfigurationClient, "configuration",
                        Mock(return_value=configuration))
    settings = Settings(
        azure_subscription_id=str(plan.request.subscription_id),
        apim_resource_group=plan.request.resource_group, apim_service_name=plan.service_name,
        apim_target_registry_enabled=True, gateway_release_worker_enabled=True,
        budget_execution_enabled=True,
        ledger_table_endpoint="https://platformledger.table.core.windows.net",
        ledger_table_name="TurnstileLedger",
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    service = build_apim_onboarding_service(repository, settings)
    keys = unit_service._keys(accepted.operation.id)
    assert service._cipher is not None and service._fixed_initializer is not None
    repository.gateway_release_operation_secrets[accepted.operation.id] = service._cipher.encrypt(
        json.dumps(keys),
    )
    source = Path(__file__).resolve().parents[3] / "infra/policies/foundry-finops-policy.xml"
    program = render_onboarding_fixed_program(
        plan.request, configuration, source.read_text(),
        self_hosted_gateway=CapabilitySupport.UNSUPPORTED,
    )
    projection = Mock(spec=TableRouteProjection)
    projection.read_program_proof.return_value = None
    head: ActiveRouteHead | None = None

    def activate(
        snapshot: RouteSnapshot, identity: Any, proof: Any, epoch: str, operation_id: str,
        expected_etag: Any, now: Any, journal: Any,
    ) -> None:
        nonlocal head
        assert snapshot.routes == () and expected_etag is None and epoch == str(rollout["epoch"])
        head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=instant)
        receipt = RouteTransitionReceipt(
            operation_id=operation_id, scope=snapshot.approval.scope, before_etag=None,
            before_generation=0, after_sha256=document_digest(head),
            route_version=snapshot.approval.route_version,
            approval_id=snapshot.approval.approval_id,
        )
        content_type, body = conditional_batch(
            program.binding.table_endpoint, program.binding.table_name,
            snapshot.approval.scope.partition,
            [("active", route_entity(snapshot.approval.scope, snapshot.approval.scope.partition,
                                     "active", canonical_document(head)), None),
             ("transition:" + operation_id, route_entity(
                 snapshot.approval.scope, snapshot.approval.scope.partition,
                 "transition:" + operation_id, canonical_document(receipt)), None)],
        )
        intent = RouteActivationIntent(
            receipt, head, content_type, body, hashlib.sha256(body).hexdigest(),
            program.binding.table_endpoint, program.binding.table_name,
        )
        journal.begin_once(intent)
        journal.record_outcome(operation_id, "confirmed")

    projection.activate.side_effect = activate
    projection.read_active.side_effect = lambda: (head, '"unit-etag"') if head else None
    projection.recover_activation.return_value = "confirmed"
    monkeypatch.setattr(module, "TableRouteProjection", Mock(return_value=projection))
    verifier = Mock()

    def verified(installed: Any, *args: Any) -> Any:
        verifier.verify.return_value = ProgramCapabilityProof(
            proof_id=str(UUID(int=479)), identity=installed.program,
            supported_profiles=installed.supported_profiles,
            issued_epoch=instant, expires_epoch=instant + 120,
        )
        return verifier

    monkeypatch.setattr(module, "FixedProgramVerifier", verified)
    publication = service._fixed_initializer(
        plan, program, accepted.operation.id, "unit-initializer",
    )
    assert publication.desired_spec.bindings == []
    assert head is not None
    assert publication.resource_manifest["fixed_route_head"] == head.model_dump(mode="json")
    raw_program = publication.resource_manifest["fixed_route_program"]
    assert isinstance(raw_program, dict) and raw_program["model_additions_enabled"] is True
    if condition == "fresh":
        assert projection.activate.call_count == 1
        return
    if condition == "unknown":
        projection.recover_activation.return_value = "unknown"
    elif condition == "epoch_changed":
        rollout["epoch"] = UUID(int=480)
    elif condition == "head_changed":
        projection.read_active.side_effect = lambda: None
    if condition == "confirmed":
        recovered = service._fixed_initializer(
            plan, program, accepted.operation.id, "unit-initializer",
        )
        assert recovered.resource_manifest == publication.resource_manifest
    else:
        with pytest.raises((
            PolicyCompilationError, RetryablePublicationError, ControlPlaneConflictError,
        )):
            service._fixed_initializer(plan, program, accepted.operation.id, "unit-initializer")
    assert projection.activate.call_count == 1


@pytest.mark.parametrize("condition", ["ready", "disabled", "missing", "mismatched"])
def test_initialized_targets_reads_fresh_gateway_rows_without_model_registry(
    condition: str,
) -> None:
    request = onboarding_plan().request
    profile = {
        "id": request.gateway_profile_id, "implementation": "apim", "enabled": True,
        "config": {"onboarding_ready": True,
                   "onboarding_request": request.model_dump(mode="json")},
    }
    target = {
        "gateway_profile_id": request.gateway_profile_id, "ownership_mode": "turnstile_managed",
        "arm_resource_id": request.resource_id, "tenant_id": request.tenant_id,
        "api_id": request.api_id, "api_path": request.api_path,
        "probe_subscription_id": request.probe_subscription_id,
    }
    repository = Mock()
    repository.registry.side_effect = AssertionError("Model registry must not be read")
    repository.get_gateway_profiles.return_value = [profile]
    repository.list_apim_targets.return_value = [] if condition == "missing" else [target]
    if condition == "disabled":
        profile["enabled"] = False
    if condition == "mismatched":
        target["api_id"] = "different-api"
    if condition in {"missing", "mismatched"}:
        with pytest.raises(PolicyCompilationError, match="no longer matches"):
            initialized_apim_targets(repository)
    else:
        assert initialized_apim_targets(repository) == (
            {request.gateway_profile_id: request} if condition == "ready" else {}
        )
        profile["enabled"] = False
        assert initialized_apim_targets(repository) == {}
    repository.registry.assert_not_called()
    assert all(call.args == () for call in repository.get_gateway_profiles.call_args_list)
    assert repository.get_gateway_profiles.call_count == (
        1 if condition in {"missing", "mismatched"} else 2
    )


def test_incomplete_initialization_is_not_an_automatic_runtime_target() -> None:
    repository, state, service, _ = onboarding_service_fixture()
    plan = service.preview(state.plan.request)
    service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner@example.com",
    )
    worker = GatewayReleaseOperationWorker(
        repository,
        FakeApimClient(),
        onboarding_handler=service.advance,
    )
    for _ in range(4):
        worker.run_once("worker")
    assert initialized_apim_targets(repository) == {}


@pytest.mark.parametrize("missing_action", [
    "Microsoft.ApiManagement/service/apis/write",
    "Microsoft.ApiManagement/service/namedValues/read",
    "Microsoft.ApiManagement/service/namedValues/write",
    "Microsoft.ApiManagement/service/backends/read",
    "Microsoft.ApiManagement/service/backends/write",
    "Microsoft.ApiManagement/service/apis/releases/read",
    "Microsoft.ApiManagement/service/apis/releases/write",
    "Microsoft.ApiManagement/service/namedValues/listValue/action",
])
def test_worker_permissions_are_checked_before_any_resource_write(missing_action: str) -> None:
    repository, state, service, _ = onboarding_service_fixture()
    plan = service.preview(state.plan.request)
    service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner@example.com",
    )
    state.permissions = [{"actions": ["*"], "notActions": [missing_action]}]
    worker = GatewayReleaseOperationWorker(
        repository,
        FakeApimClient(),
        onboarding_handler=service.advance,
    )
    failed = worker.run_once("worker")
    assert failed is not None and failed.status == "failed"
    assert "Worker authorization is missing" in str(failed.error_message)
    assert missing_action in str(failed.error_message)
    assert not repository.gateways[-1]["enabled"]
    assert all(request.method == "GET" for request in state.requests)


@pytest.mark.parametrize("reference_kind", ["literal", "public", "protected"])
def test_existing_logger_preflight_accepts_exact_reference_access(reference_kind: str) -> None:
    state = OnboardingHttpState()
    configuration = onboarding_configuration()
    reference_path = state.plan.request.resource_id + "/namedValues/logger-identity-unit"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == reference_path + "/listValue":
            state.requests.append(request)
            return httpx.Response(200, json={"value": "systemAssigned"})
        return state.handle(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request, token_provider=OnboardingToken(), client=http,
        )
        client.ensure_governance_dependencies(configuration)
        if reference_kind != "literal":
            logger = next(value for path, value in state.resources.items() if "/loggers/" in path)
            logger["properties"]["credentials"]["identityClientId"] = "{{logger-identity-unit}}"
            state.resources[reference_path] = {
                "id": reference_path,
                "properties": {
                    "secret": reference_kind == "protected",
                    **({"value": "systemAssigned"} if reference_kind == "public" else {}),
                },
            }
        state.permissions = [{"actions": ["*"], "notActions": [
            "Microsoft.ApiManagement/service/namedValues/listValue/action",
        ]}]
        original = deepcopy(state.resources)
        state.requests.clear()
        client.verify_initialization_permissions(configuration)
    assert state.resources == original
    assert all(
        request.method == "GET"
        or (request.method == "POST" and request.url.path == reference_path + "/listValue")
        for request in state.requests
    )
    assert sum(request.url.path == reference_path for request in state.requests) == int(
        reference_kind != "literal"
    )
    assert sum(
        request.url.path == reference_path + "/listValue" for request in state.requests
    ) == int(reference_kind == "protected")


@pytest.mark.parametrize("denied_suffix", ["", "/listValue"])
def test_existing_logger_preflight_rejects_denied_reference_before_resource_writes(
    denied_suffix: str,
) -> None:
    state = OnboardingHttpState()
    configuration = onboarding_configuration()
    reference_path = state.plan.request.resource_id + "/namedValues/logger-identity-unit"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == reference_path + denied_suffix:
            state.requests.append(request)
            return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})
        return state.handle(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request, token_provider=OnboardingToken(), client=http,
        )
        client.ensure_governance_dependencies(configuration)
        logger = next(value for path, value in state.resources.items() if "/loggers/" in path)
        logger["properties"]["credentials"]["identityClientId"] = "{{logger-identity-unit}}"
        state.resources[reference_path] = {
            "id": reference_path, "properties": {"secret": True},
        }
        original = deepcopy(state.resources)
        state.requests.clear()
        with pytest.raises(PolicyCompilationError, match="AuthorizationFailed"):
            client.verify_initialization_permissions(configuration)
    assert state.resources == original
    assert any(request.url.path == reference_path + denied_suffix for request in state.requests)
    assert all(
        request.method == "GET"
        or (request.method == "POST" and request.url.path == reference_path + "/listValue")
        for request in state.requests
    )


def test_permission_not_actions_are_evaluated_per_role_union() -> None:
    state = OnboardingHttpState()
    state.permissions = [
        {"actions": ["*"], "notActions": ["Microsoft.Authorization/roleAssignments/write"]},
        {"actions": ["Microsoft.Authorization/roleAssignments/write"], "notActions": []},
    ]
    with httpx.Client(transport=httpx.MockTransport(state.handle)) as http:
        client = AzureApimOnboardingClient(
            state.plan.request,
            token_provider=OnboardingToken(),
            client=http,
        )
        client.verify_initialization_permissions(onboarding_configuration())
        state.permissions.pop()
        with pytest.raises(PolicyCompilationError, match="roleAssignments/write"):
            client.verify_initialization_permissions(onboarding_configuration())
    assert all(request.method == "GET" for request in state.requests)


def test_failed_readiness_can_retry_without_replacing_resources_or_keys() -> None:
    repository, state, service, _ = onboarding_service_fixture()
    plan = service.preview(state.plan.request)
    accepted = service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner@example.com",
    )
    worker = GatewayReleaseOperationWorker(
        repository,
        FakeApimClient(),
        onboarding_handler=service.advance,
    )
    for _ in range(4):
        worker.run_once("worker")
    state.anonymous_status = 200
    failed = worker.run_once("worker")
    assert failed is not None and failed.status == "failed"
    original = deepcopy(repository.gateway_release_operations[0])
    assert repository.gateways[-1]["enabled"] is False
    keys = {
        path: deepcopy(value["properties"])
        for path, value in state.resources.items()
        if path.startswith(state.plan.request.resource_id + "/subscriptions/")
    }
    state.anonymous_status = 401
    retry = service.retry(accepted.operation.id, "owner@example.com")
    assert retry.operation.id != accepted.operation.id
    for _ in range(5):
        completed = worker.run_once("worker")
    assert completed is not None and completed.status == "succeeded", completed
    assert repository.gateway_release_operations[0] == original
    assert all(state.resources[path]["properties"] == value for path, value in keys.items())
    assert repository.gateway_release_operation_secret(accepted.operation.id) is None


def test_initialization_snapshot_tampering_is_rejected_before_cloud_writes() -> None:
    repository, state, service, _ = onboarding_service_fixture()
    plan = service.preview(state.plan.request)
    service.start(
        ApimOnboardingStart(
            request=plan.request,
            confirmation_sha256=plan.confirmation_sha256,
        ),
        "owner@example.com",
    )
    repository.gateway_release_operations[-1]["semantic_preview"]["api_id"] = (
        "customer-business-api"
    )
    state.requests.clear()
    worker = GatewayReleaseOperationWorker(
        repository,
        FakeApimClient(),
        onboarding_handler=service.advance,
    )
    failed = worker.run_once("worker")
    assert failed is not None and failed.status == "failed"
    assert state.requests == []
    assert not repository.gateways[-1]["enabled"]


class OnboardingLedgerRepository(InMemoryRepository):
    def enroll_apim_onboarding_budget_target(
        self,
        operation_id: UUID,
        worker_id: str,
    ) -> dict[str, Any]:
        operation = self.get_gateway_release_operation(operation_id)
        assert operation is not None and worker_id == "worker"
        return {
            "epoch": UUID("60000000-0000-4000-8000-000000000002"),
            "gateway_profile_ids": [operation["gateway_profile_id"]],
        }


def test_new_target_ledger_projection_does_not_reset_other_rows_or_usage() -> None:
    repository = OnboardingLedgerRepository()
    plan = onboarding_plan()
    operation = repository.create_apim_onboarding_operation(plan, b"encrypted", "owner@example.com")
    ledger = UnitLedger()
    ledger.upsert(
        "existing-consumer", "S", {"confirmed_tokens": 572, "active_reserved_tokens": 352}
    )
    original = deepcopy(ledger.rows[("existing-consumer", "S")])
    prepared: list[UUID] = []

    def prepare_scope(application_id: UUID, epoch: UUID) -> None:
        assert str(epoch) == "60000000-0000-4000-8000-000000000002"
        prepared.append(application_id)

    project_onboarding_ledger(repository, ledger, plan, operation["id"], "worker", prepare_scope)
    assert len(prepared) == 1
    dashboard = next(
        item for item in repository.list_gateway_applications()
        if item["slug"] == plan.dashboard_subscription_id
    )
    assert dashboard["application_type"] == "service"
    assert ledger.rows[("existing-consumer", "S")] == original
    confirmed = [key for key in ledger.rows if key[1] == "C"]
    assert len(confirmed) == 2
    ledger.rows[confirmed[0]]["ConfirmedUsed"] = 19
    project_onboarding_ledger(repository, ledger, plan, operation["id"], "worker", prepare_scope)
    assert ledger.rows[confirmed[0]]["ConfirmedUsed"] == 19
    fences = [(key, value) for key, value in ledger.rows.items() if key[1] == "F"]
    assert len(fences) == 1
    assert json.loads(fences[0][1]["Document"])["gateways"] == {"managed": ["1"]}
    ledger.rows[fences[0][0]]["Document"] = json.dumps({"epoch": "another-epoch"})
    with pytest.raises(PolicyCompilationError, match="another configuration"):
        project_onboarding_ledger(
            repository, ledger, plan, operation["id"], "worker", prepare_scope
        )


def protocol_fixture() -> tuple[
    InMemoryRepository, ApimTargetArmInventory, dict[str, Any], MagicMock, str
]:
    request = onboarding_request()
    repository = InMemoryRepository()
    repository.gateways.append({**repository.gateways[0], "id": request.gateway_profile_id})
    metadata = target(
        gateway_profile_id=request.gateway_profile_id,
        tenant_id=request.tenant_id,
        subscription_id=request.subscription_id,
        resource_group=request.resource_group,
        service_name=request.service_name,
        arm_resource_id=request.resource_id,
        api_id=request.api_id,
        api_path=request.api_path,
        principal_id=UUID(int=47),
        capabilities={"self_hosted_gateway": "supported"},
    )
    repository.upsert_apim_target(metadata)
    repository.upsert_gateway_deployment(
        deployment(
            id=gateway_deployment_id_for(request.gateway_profile_id, "edge"),
            apim_target_id=request.gateway_profile_id,
            native_gateway_id="edge",
            kind="self_hosted",
            hosting_mode="docker",
            endpoint_url="https://edge.example.test/unit/llm",
        )
    )
    inventory = ApimTargetArmInventory.model_validate(
        {
            **{
                name: value
                for name, value in metadata.model_dump().items()
                if name in ApimTargetArmInventory.model_fields
            },
            "api_revision": "1",
            "managed_gateway_url": "https://unit-service.azure-api.net",
            "source_api_version": "2024-05-01",
            "discovered_at": NOW,
            "gateways": [
                {
                    "native_gateway_id": "edge",
                    "arm_resource_id": request.resource_id + "/gateways/edge",
                    "associated_api_ids": [request.api_id],
                }
            ],
        }
    )
    GatewayControlPlaneService(repository).apply_target_inventory(inventory)
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    config = governance_configuration()
    canonical = onboarding_canonical_parent(source, request.api_id)
    client = MagicMock()
    client.current_api_policy.return_value = (
        "1",
        render_onboarding_parent(request, config, source),
    )
    client.current_revision.return_value = "1"
    fence: dict[str, Any] = {
        "protocol_version": 2,
        "epoch": str(UUID(int=72)),
        "mode": "active",
        "gateway_profile_id": str(request.gateway_profile_id),
        "api_id": request.api_id,
        "apim_service_id": request.service_name,
        "gateways": {"managed": ["1", "previous"]},
        "model_contracts": {"unit-model": {"counting": "strict"}},
        "extension": {"keep": True},
    }
    return repository, inventory, fence, client, canonical


class ProtocolFenceRows:
    def __init__(self, fence: dict[str, Any]) -> None:
        self.document = deepcopy(fence)
        self.version = 1
        self.writes: list[dict[str, Any]] = []
        self.reads = 0
        self.before_replace: Callable[[dict[str, Any]], bool] | None = None

    def read_versioned_entity(self, partition: str, row_key: str) -> tuple[dict[str, Any], str]:
        assert partition.startswith("budget-protocol|") and row_key == "F"
        self.reads += 1
        return {
            "Document": json.dumps(self.document),
            "Preserved": "unit",
            "Timestamp": "old",
            "odata.etag": "metadata-only",
        }, f'W/"{self.version}"'

    def replace_if_match(
        self,
        partition: str,
        row_key: str,
        entity: dict[str, Any],
        etag: str,
    ) -> bool:
        assert partition.startswith("budget-protocol|") and row_key == "F"
        assert etag == f'W/"{self.version}"'
        assert set(entity) == {"Document", "Preserved"} and entity["Preserved"] == "unit"
        self.writes.append(deepcopy(entity))
        if self.before_replace is not None and not self.before_replace(entity):
            return False
        self.document = json.loads(entity["Document"])
        self.version += 1
        return True


@pytest.mark.parametrize("retired", [False, True])
def test_gateway_protocol_changes_only_membership_and_is_idempotent(retired: bool) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    if retired:
        fence["gateways"]["edge"] = ["1"]
        repository.gateway_deployments[
            gateway_deployment_id_for(
                inventory.gateway_profile_id,
                "edge",
            )
        ]["retired_at"] = NOW
    store = ProtocolFenceRows(fence)
    before_write = MagicMock()
    for _ in range(2):
        result = enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
            before_write=before_write,
        )
        assert result == (() if retired else ("edge",))
    assert len(store.writes) == 1
    before_write.assert_called_once()
    assert store.document["gateways"]["managed"] == ["1", "previous"]
    assert ("edge" in store.document["gateways"]) is not retired
    assert {key: value for key, value in store.document.items() if key != "gateways"} == {
        key: value for key, value in fence.items() if key != "gateways"
    }


@pytest.mark.parametrize("committed", [False, True])
def test_gateway_protocol_unknown_write_reads_back_without_replaying(committed: bool) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)

    def lost_response(entity: dict[str, Any]) -> bool:
        if committed:
            store.document = json.loads(entity["Document"])
            store.version += 1
        raise httpx.ReadTimeout("Unknown conditional write outcome")

    store.before_replace = lost_response
    if committed:
        assert enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        ) == ("edge",)
    else:
        with pytest.raises(RetryablePublicationError, match="outcome is uncertain"):
            enroll_self_hosted_gateway_protocol(
                repository,
                cast(TableStorageLedger, store),
                inventory,
                fence,
                client,
                canonical,
            )
    assert len(store.writes) == 1 and store.reads == 2


def test_gateway_protocol_conflict_preserves_concurrent_additions_and_rechecks_lease() -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)

    def conflict(_: dict[str, Any]) -> bool:
        store.document["gateways"]["concurrent"] = ["1"]
        store.version += 1
        store.before_replace = None
        return False

    store.before_replace = conflict
    before_write = MagicMock()
    assert enroll_self_hosted_gateway_protocol(
        repository,
        cast(TableStorageLedger, store),
        inventory,
        fence,
        client,
        canonical,
        before_write=before_write,
    ) == ("edge",)
    assert before_write.call_count == 2
    assert store.document["gateways"] == {
        "managed": ["1", "previous"],
        "concurrent": ["1"],
        "edge": ["1"],
    }


@pytest.mark.parametrize(
    "change", ["epoch", "contracts", "revision", "target", "deployment", "lease"]
)
def test_gateway_protocol_rechecks_protected_state_before_conditional_write(
    change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    callback = None
    if change == "epoch":
        store.document["epoch"] = str(UUID(int=73))
    elif change == "contracts":
        store.document["model_contracts"] = {}
    elif change == "revision":
        client.current_revision.return_value = "2"
    elif change == "target":
        original = repository.get_apim_target(inventory.gateway_profile_id)
        assert original is not None
        monkeypatch.setattr(repository, "get_apim_target", MagicMock(
            side_effect=[original, {**original, "api_path": "other"}]
        ))
    elif change == "deployment":
        monkeypatch.setattr(repository, "get_gateway_deployment", MagicMock(return_value=None))
    else:
        callback = MagicMock(side_effect=RetryablePublicationError("Lease expired"))
    with pytest.raises((PolicyCompilationError, RetryablePublicationError)):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
            before_write=callback,
        )
    assert store.writes == []


@pytest.mark.parametrize(
    "change,message",
    [
        ("stale", "latest inventory"),
        ("missing", "missing from Azure inventory"),
        ("foreign", "foreign Gateway"),
        ("association", "not associated"),
        ("unsupported", "does not support"),
        ("revision", "current API [Rr]evision changed"),
    ],
)
def test_gateway_protocol_rejects_unproven_inventory(change: str, message: str) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    values = inventory.model_dump()
    if change == "stale":
        values["discovered_at"] = NOW - timedelta(seconds=1)
    elif change == "missing":
        values["gateways"] = []
    elif change == "foreign":
        values["gateways"][0]["arm_resource_id"] += "/foreign"
    elif change == "association":
        values["gateways"][0]["associated_api_ids"] = ["foreign-api"]
    elif change == "unsupported":
        values["capabilities"]["self_hosted_gateway"] = "unsupported"
    else:
        client.current_api_policy.return_value = ("2", client.current_api_policy.return_value[1])
    with pytest.raises((PolicyCompilationError, RetryablePublicationError), match=message):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            ApimTargetArmInventory.model_validate(values),
            fence,
            client,
            canonical,
        )
    assert store.writes == [] and store.reads == 0


@pytest.mark.parametrize(
    "change,message",
    [
        ("mode", "active target fence"),
        ("epoch", "valid epoch"),
        ("protocol", "active target fence"),
        ("identity", "active target fence"),
        ("revision", "changed concurrently"),
        ("case", "different casing"),
    ],
)
def test_gateway_protocol_refuses_fence_identity_and_existing_membership_drift(
    change: str,
    message: str,
) -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    if change == "mode":
        fence["mode"] = "paused"
    elif change == "epoch":
        fence["epoch"] = "not-an-epoch"
    elif change == "protocol":
        fence["protocol_version"] = True
    elif change == "identity":
        fence["gateway_profile_id"] = str(UUID(int=99))
    store = ProtocolFenceRows(fence)
    if change == "revision":
        store.document["gateways"]["managed"] = ["different"]
    elif change == "case":
        store.document["gateways"]["EDGE"] = ["1"]
    with pytest.raises(PolicyCompilationError, match=message):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        )
    assert store.writes == []


def test_gateway_protocol_conditional_conflicts_have_a_fixed_retry_bound() -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    store.before_replace = lambda _: False
    with pytest.raises(RetryablePublicationError, match="fresh conditional retry"):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        )
    assert len(store.writes) == 3 and store.reads == 4
    assert store.document == fence


def test_gateway_protocol_refuses_parent_policy_drift_before_reading_fence() -> None:
    repository, inventory, fence, client, canonical = protocol_fixture()
    store = ProtocolFenceRows(fence)
    policy = parse_policy(client.current_api_policy.return_value[1])
    version = policy.find("./inbound/set-variable[@name='budgetExecutionVersion']")
    assert version is not None
    version.set("value", "@(1)")
    client.current_api_policy.return_value = ("1", serialize_policy(policy))
    with pytest.raises(PolicyCompilationError):
        enroll_self_hosted_gateway_protocol(
            repository,
            cast(TableStorageLedger, store),
            inventory,
            fence,
            client,
            canonical,
        )
    assert store.reads == 0 and store.writes == []


def onboarding_request(**changes: Any) -> ApimOnboardingRequest:
    return ApimOnboardingRequest.model_validate(
        {
            "tenant_id": UUID(int=3),
            "resource_id": target().arm_resource_id,
            "name": "Unit APIM",
            **changes,
        }
    )


def governance_configuration(**changes: Any) -> ApimOnboardingConfiguration:
    prefix = f"/subscriptions/{UUID(int=2)}/resourceGroups/unit-platform/providers"
    return ApimOnboardingConfiguration.model_validate(
        {
            "management_tenant_id": UUID(int=3),
            "employee_tenant_id": UUID(int=3),
            "employee_client_id": UUID(int=4),
            "employee_audience": "api://unit-client",
            "organization_id": "unit-org",
            "organization_name": "Unit organization",
            "event_hub_resource_id": prefix
            + "/Microsoft.EventHub/namespaces/unit-ns/eventhubs/usage",
            "ledger_table_resource_id": prefix
            + "/Microsoft.Storage/storageAccounts/unitstorage"
            + "/tableServices/default/tables/UnitLedger",
            "route_table_resource_id": prefix
            + "/Microsoft.Storage/storageAccounts/unitstorage"
            + "/tableServices/default/tables/UnitRoutes",
            "credential_vault_resource_id": prefix + "/Microsoft.KeyVault/vaults/unit-vault",
            **changes,
        }
    )
