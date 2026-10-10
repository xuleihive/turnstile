from __future__ import annotations

import pytest

from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_directory_policy import (
    compose_directory_identity_policy,
    strip_validated_directory_identity_policy,
)
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)

BASE = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()


def composed() -> str:
    return compose_directory_identity_policy(
        BASE,
        cloud="public",
        endpoint="__LEDGER_TABLE_ENDPOINT__",
        table="__LEDGER_TABLE_NAME__",
    )


def test_directory_lookup_preserves_base_and_runs_after_signature_before_reservation() -> None:
    value = composed()
    root = parse_policy(value)
    assert component_digest(
        parse_policy(strip_validated_directory_identity_policy(value))
    ) == component_digest(parse_policy(BASE))
    validate_parent_policy(value, BASE)
    branch = root.find("./inbound/choose/when/validate-azure-ad-token/..")
    assert branch is not None
    elements = list(branch)
    block = next(
        node
        for node in elements
        if node.find("./when/set-variable[@name='directoryIdentityCloud']") is not None
    )
    validation = branch.find("validate-azure-ad-token")
    assert validation is not None
    assert elements.index(validation) < elements.index(block)
    ledger = next(
        node
        for node in elements
        if node.find(".//set-variable[@name='ledgerPartition']") is not None
    )
    assert elements.index(block) < elements.index(ledger)
    partition = branch.find(".//set-variable[@name='ledgerPartition']")
    assert partition is not None and "requestStarted" in partition.get("value", "")
    reservation = next(node for node in branch.iter("set-body") if '"RowKey"' in (node.text or ""))
    assert 'context.Variables["requestStarted"]' in (reservation.text or "")
    request = block.find(".//send-request")
    assert request is not None
    method = request.find("set-method")
    assert method is not None and method.text == "GET"
    assert block.find(".//cache-lookup-value") is None
    assert block.find(".//set-variable[@name='employeeName']") is not None
    assert {node.get("name") for node in block.findall(".//set-header")} >= {
        "x-user-id",
        "x-user-name",
        "x-org-id",
        "x-org-name",
        "x-department-id",
    }
    status = block.find(".//set-variable[@name='directoryIdentityStatus']")
    assert status is not None and "ExpiresAt" in status.get("value", "")


def test_missing_disabled_and_unavailable_are_fail_closed_without_legacy_fallback() -> None:
    root = parse_policy(composed())
    identity = next(
        node
        for node in root.iter("choose")
        if node.find("./when/set-variable[@name='directoryIdentityCloud']") is not None
    )
    statuses = {node.get("code") for node in identity.findall(".//set-status")}
    assert statuses == {"403", "503"}
    assert not identity.findall(".//otherwise")
    assert "budget_scope" not in serialize_policy(identity)


@pytest.mark.parametrize("change", ["disabled", "url", "position", "version"])
def test_identity_policy_drift_is_rejected(change: str) -> None:
    root = parse_policy(composed())
    if change == "version":
        marker = root.find("./inbound/set-variable[@name='directoryIdentityPolicyVersion']")
        assert marker is not None
        marker.set("value", "@(2)")
    else:
        employee = root.find("./inbound/choose/when/validate-azure-ad-token/..")
        assert employee is not None
        block = next(
            node
            for node in employee.findall("choose")
            if node.find("./when/set-variable[@name='directoryIdentityCloud']") is not None
        )
        if change == "position":
            employee.remove(block)
            employee.append(block)
        elif change == "url":
            url = block.find(".//set-url")
            assert url is not None
            url.text = "https://attacker.invalid"
        else:
            status = block.find(".//set-variable[@name='directoryIdentityStatus']")
            assert status is not None
            status.set("value", status.get("value", "").replace('return "disabled"', 'return "ok"'))
    with pytest.raises(PolicyCompilationError):
        validate_parent_policy(serialize_policy(root), BASE)


def test_directory_upgrade_requires_exact_existing_ledger_target() -> None:
    with pytest.raises(PolicyCompilationError, match="existing employee ledger"):
        compose_directory_identity_policy(
            BASE,
            cloud="public",
            endpoint="https://another.table.core.windows.net",
            table="OtherLedger",
        )
    with pytest.raises(PolicyCompilationError, match="already present"):
        compose_directory_identity_policy(
            composed(),
            cloud="public",
            endpoint="__LEDGER_TABLE_ENDPOINT__",
            table="__LEDGER_TABLE_NAME__",
        )


def test_legacy_double_slash_ledger_url_keeps_exact_table_target() -> None:
    endpoint = "https://example.table.core.windows.net/"
    live = BASE.replace(
        "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__",
        endpoint + "/TurnstileLedger",
    )
    upgraded = compose_directory_identity_policy(
        live,
        cloud="public",
        endpoint=endpoint,
        table="TurnstileLedger",
    )
    assert component_digest(parse_policy(strip_validated_directory_identity_policy(upgraded))) == (
        component_digest(parse_policy(live))
    )
    with pytest.raises(PolicyCompilationError, match="existing employee ledger"):
        compose_directory_identity_policy(
            live,
            cloud="public",
            endpoint=endpoint,
            table="WrongTable",
        )


def test_collection_lookup_distinguishes_missing_from_invalid_or_unavailable() -> None:
    root = parse_policy(composed())
    url = root.find(".//set-variable[@name='directoryIdentityUrl']")
    payload = root.find(".//set-variable[@name='directoryIdentityJson']")
    status = root.find(".//set-variable[@name='directoryIdentityStatus']")
    assert url is not None and payload is not None and status is not None
    assert "Uri.EscapeDataString" in url.get("value", "")
    assert "RowKey eq" in next(
        line for line in url.get("value", "").splitlines() if "Uri.EscapeDataString" in line
    )
    assert "()?$filter=" in url.get("value", "")
    assert "(PartitionKey=" not in url.get("value", "")
    expression = payload.get("value", "")
    assert 'rows.Count == 0) { return "{}"; }' in expression
    assert 'rows.Count > 1' in expression
    assert "x-ms-continuation-NextPartitionKey" in expression
    assert "x-ms-continuation-NextRowKey" in expression
    assert 'catch { return ""; }' in expression
    validation = status.get("value", "")
    assert 'response.StatusCode != 200) { return "unavailable"; }' in validation
    assert 'StatusCode == 404' not in validation
    assert 'row["PartitionKey"]' in validation and 'row["RowKey"]' in validation
