from __future__ import annotations

import hashlib
import json
import re

from ..domain.budget_delegation import BudgetDelegationTrust
from ..domain.gateway_targets import (
    ApimOnboardingConfiguration,
    ApimOnboardingRequest,
    CapabilitySupport,
    native_gateway_scope_for_capability,
)
from ..domain.image_profiles import DEFAULT_IMAGE_GENERATION_LIMITS
from ..domain.storage_routes import RouteScope, document_digest
from .apim_budget_execution import budget_execution_fence, compose_budget_execution_parent
from .apim_control_plane_contract import PolicyCompilationError
from .apim_model_availability import compose_availability_parent
from .apim_policy_components import (
    expression_tokens,
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)
from .apim_storage_routes import (
    FULL_PROGRAM_PROFILES,
    FixedRouteBinding,
    FixedRouteProgram,
    compile_fixed_route_program,
)


def render_onboarding_fixed_program(
    request: ApimOnboardingRequest,
    configuration: ApimOnboardingConfiguration,
    canonical_parent: str,
    *, self_hosted_gateway: CapabilitySupport,
    previous_program_sha256: str | None = None,
) -> FixedRouteProgram:
    routes = configuration.route_table_resource_id.split("/")
    ledger = configuration.ledger_table_resource_id.split("/")
    vault = configuration.credential_vault_resource_id.rsplit("/", 1)[-1].lower()
    binding = FixedRouteBinding(
        scope=RouteScope(
            target_id=str(request.gateway_profile_id), api_id=request.api_id, gateway_id="managed",
        ),
        api_revision="1",
        table_endpoint=f"https://{routes[8].lower()}.table.core.windows.net",
        table_name=routes[-1],
        ledger_table_endpoint=f"https://{ledger[8].lower()}.table.core.windows.net",
        ledger_table_name=ledger[-1],
        trust_binding_sha256=document_digest(configuration),
        trusted_vault_origins=(f"https://{vault}.vault.azure.net",),
        publisher_subscription_id=request.probe_subscription_id,
        supported_profiles=FULL_PROGRAM_PROFILES,
        image_limits=DEFAULT_IMAGE_GENERATION_LIMITS,
        decision_enabled=True,
        model_additions_enabled=True,
        pool_enabled=True,
        router_enabled=True,
        native_gateway_scope=native_gateway_scope_for_capability(self_hosted_gateway),
        legacy_program_sha256=previous_program_sha256,
        models_path="/v1/models",
        messages_path="/v1/messages",
    )
    canonical = compose_availability_parent(
        onboarding_canonical_parent(canonical_parent, request.api_id),
    )
    return compile_fixed_route_program(
        _render_onboarding_parent(request, configuration, canonical),
        binding,
        canonical_parent=canonical,
    )


def onboarding_logger_id(request: ApimOnboardingRequest) -> str:
    return f"{request.api_id}-usage"


def onboarding_policy_configuration_digest(
    configuration: ApimOnboardingConfiguration,
    canonical_parent: str,
) -> str:
    payload = {
        "version": 1,
        "configuration": configuration.model_dump(mode="json"),
        "canonical_parent_sha256": hashlib.sha256(
            onboarding_canonical_parent(canonical_parent, "turnstile-onboarding").encode()
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def onboarding_canonical_parent(
    canonical_parent: str, api_id: str, *, delegation: BudgetDelegationTrust | None = None,
) -> str:
    source = compose_budget_execution_parent(
        canonical_parent, canonical_parent, enabled=True, delegation=delegation,
    )
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("APIM initialization requires an inbound policy")
    base = inbound.find("base")
    if base is None:
        raise PolicyCompilationError("APIM initialization must preserve policy inheritance")
    inbound.insert(list(inbound).index(base) + 1, budget_execution_fence([api_id]))
    return serialize_policy(root)


def render_onboarding_parent(
    request: ApimOnboardingRequest,
    configuration: ApimOnboardingConfiguration,
    canonical_parent: str,
) -> str:
    return _render_onboarding_parent(
        request, configuration, onboarding_canonical_parent(canonical_parent, request.api_id),
    )


def _render_onboarding_parent(
    request: ApimOnboardingRequest,
    configuration: ApimOnboardingConfiguration,
    canonical: str,
) -> str:
    ledger = configuration.ledger_table_resource_id.split("/")
    ledger_endpoint = f"https://{ledger[8]}.table.core.windows.net"
    root = parse_policy(canonical)

    def quoted_text(value: str) -> str:
        return json.dumps(value, ensure_ascii=True)[1:-1]

    values = {
        "__GATEWAY_PROFILE_ID__": str(request.gateway_profile_id),
        "__LOGGER_ID__": onboarding_logger_id(request),
        "__EMPLOYEE_TOKEN_ENABLED__": "true",
        "__EMPLOYEE_TENANT_ID__": str(configuration.employee_tenant_id),
        "__EMPLOYEE_CLIENT_ID__": str(configuration.employee_client_id),
        "__EMPLOYEE_AUDIENCE__": quoted_text(configuration.employee_audience),
        "__EMPLOYEE_ORG_ID__": quoted_text(configuration.organization_id),
        "__EMPLOYEE_ORG_NAME__": quoted_text(configuration.organization_name),
        "__EMPLOYEE_TOKENS_PER_MINUTE__": str(configuration.employee_tokens_per_minute),
        "__TOKENS_PER_MINUTE__": str(configuration.employee_tokens_per_minute),
        "__DEFAULT_MAX_OUTPUT_TOKENS__": str(configuration.default_max_output_tokens),
        "__LEDGER_TABLE_ENDPOINT__": ledger_endpoint,
        "__LEDGER_TABLE_NAME__": ledger[-1],
        "__EMPLOYEE_DEPARTMENT_MAP__": quoted_text(json.dumps(configuration.department_map)),
        "__EMPLOYEE_CLIENT_MAP__": quoted_text(json.dumps(configuration.client_map)),
        "__EMPLOYEE_SURFACE_MAP__": quoted_text(json.dumps(
            configuration.model_dump(mode="json")["surface_map"]
        )),
        "__SUBSCRIPTION_AGENT_MAP__": "{}",
        "__LEGACY_PROVIDER_ROUTING__": "",
        "__USAGE_OBSERVER_HEADERS__": "",
    }
    placeholder = re.compile(r"__[A-Z][A-Z0-9_]*__")

    def bind(text: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            name = match.group()
            if name not in values:
                raise PolicyCompilationError(f"Unsupported onboarding policy parameter: {name}")
            return values[name]

        return placeholder.sub(replacement, text)

    for node in root.iter():
        for name, value in node.attrib.items():
            node.set(name, bind(value))
        if node.text:
            node.text = bind(node.text)
        if node.tail:
            node.tail = bind(node.tail)
    parents = {child: parent for parent in root.iter() for child in parent}
    employee_name = root.find("./inbound//set-variable[@name='employeeName']")
    if employee_name is None:
        raise PolicyCompilationError("Employee organization attribution branch is missing")
    employee_parent = parents[employee_name]
    for header, expected in (
        ("x-org-id", configuration.organization_id),
        ("x-org-name", configuration.organization_name),
    ):
        headers = employee_parent.findall(
            f"set-header[@name='{header}'][@exists-action='override']"
        )
        expected_value = quoted_text(expected)
        actual_value = headers[0].findtext("value", "") if len(headers) == 1 else ""
        delegated_value = (
            '@((bool)context.Variables["budgetDelegatedPerson"] ? '
            f'context.Request.Headers.GetValueOrDefault("{header}", "{expected_value}") '
            f': "{expected_value}")'
        )
        if actual_value != expected_value and (
            expression_tokens(actual_value) != expression_tokens(delegated_value)
        ):
            raise PolicyCompilationError(
                "Employee organization attribution differs from platform configuration"
            )
    rendered = serialize_policy(root)
    validate_parent_policy(rendered, canonical)
    return rendered
