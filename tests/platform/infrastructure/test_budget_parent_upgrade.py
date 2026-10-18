from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import pytest

from backend.services.client_user_policy import (
    compose_client_user_policy,
    strip_verified_client_user_policy,
)
from scripts.budget_execution_upgrade import plan_budget_parent_upgrade
from turnstile_core.domain.budget_delegation import BudgetDelegationTrust
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy

ROOT = Path(__file__).resolve().parents[3]
TARGET = UUID("10000000-0000-4000-8000-000000000001")


def legacy_parent() -> tuple[str, str]:
    template = (ROOT / "infra/policies/foundry-finops-policy.schema1.xml").read_text()
    parameters = {
        "__EMPLOYEE_TOKEN_ENABLED__": "false", "__EMPLOYEE_TENANT_ID__": str(UUID(int=1)),
        "__EMPLOYEE_CLIENT_ID__": str(UUID(int=2)), "__EMPLOYEE_AUDIENCE__": "api://unit",
        "__GATEWAY_PROFILE_ID__": str(TARGET), "__EMPLOYEE_ORG_ID__": "unit-organization",
        "__EMPLOYEE_ORG_NAME__": "Unit organization", "__EMPLOYEE_DEPARTMENT_MAP__": "{}",
        "__EMPLOYEE_CLIENT_MAP__": "{}", "__EMPLOYEE_SURFACE_MAP__": "[]",
        "__SUBSCRIPTION_AGENT_MAP__": "{}", "__EMPLOYEE_TOKENS_PER_MINUTE__": "100000",
        "__TOKENS_PER_MINUTE__": "200000", "__DEFAULT_MAX_OUTPUT_TOKENS__": "4096",
        "__LEDGER_TABLE_ENDPOINT__": "https://unit.table.core.windows.net",
        "__LEDGER_TABLE_NAME__": "UnitLedger", "__LOGGER_ID__": "unit-logger",
        "__LEGACY_PROVIDER_ROUTING__": "", "__USAGE_OBSERVER_HEADERS__": "",
    }
    materialized = re.sub(r"__[A-Z][A-Z0-9_]*__", lambda m: parameters[m.group()], template)
    return template, compose_client_user_policy(materialized)


def test_offline_upgrade_preserves_configuration_and_uses_the_shared_delegation_protocol() -> None:
    template, source = legacy_parent()
    result = plan_budget_parent_upgrade(
        source, legacy_template=template,
        canonical_parent=(ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        delegation=BudgetDelegationTrust(
            tenant_id=UUID(int=1), audience="api://trusted", client_ids=(UUID(int=3),),
            object_ids=(UUID(int=4),),
        ),
        gateway_profile_id=TARGET,
    )
    root = parse_policy(result.candidate_parent)
    assert root.find(".//set-variable[@name='telemetryUsageId']") is not None
    assert root.find(".//set-variable[@name='budgetExecutionVersion']") is not None
    assert root.find(".//set-variable[@name='clientContextPolicyVersion']") is None
    assert root.find(
        ".//validate-azure-ad-token[@header-name='x-turnstile-delegation-token']",
    ) is not None
    assert result.adopted_bindings["__TOKENS_PER_MINUTE__"] == "200000"
    assert result.adopted_bindings["__EMPLOYEE_TOKEN_ENABLED__"] == "false"
    assert result.adopted_bindings["__LEDGER_TABLE_NAME__"] == "UnitLedger"
    assert not re.search(r"__[A-Z][A-Z0-9_]*__", result.candidate_parent)
    assert "https://unit.table.core.windows.net/UnitLedger" in result.candidate_parent
    assert all(node.get("logger-id") == "unit-logger" for node in root.iter("log-to-eventhub"))
    execution_plan = root.find(".//set-variable[@name='executionPlan']")
    assert execution_plan is not None
    assert str(TARGET) in execution_plan.get("value", "")


def test_offline_upgrade_refuses_the_wrong_gateway() -> None:
    template, source = legacy_parent()
    with pytest.raises(PolicyCompilationError, match="different Gateway"):
        plan_budget_parent_upgrade(
            source, legacy_template=template,
            canonical_parent=(ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
            delegation=BudgetDelegationTrust(
                tenant_id=UUID(int=1), audience="api://trusted", client_ids=(UUID(int=3),),
                object_ids=(UUID(int=4),),
            ),
            gateway_profile_id=UUID(int=99),
        )


def test_offline_upgrade_never_silently_discards_a_custom_policy_branch() -> None:
    from xml.etree import ElementTree as ET

    template, source = legacy_parent()
    root = parse_policy(strip_verified_client_user_policy(source))
    inbound = root.find("inbound")
    assert inbound is not None
    ET.SubElement(inbound, "set-variable", {"name": "customerExtension", "value": "@(true)"})
    with pytest.raises(PolicyCompilationError, match="unrecognized policy content"):
        plan_budget_parent_upgrade(
            serialize_policy(root), legacy_template=template,
            canonical_parent=(ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
            delegation=BudgetDelegationTrust(
                tenant_id=UUID(int=1), audience="api://trusted", client_ids=(UUID(int=3),),
                object_ids=(UUID(int=4),),
            ), gateway_profile_id=TARGET,
        )
