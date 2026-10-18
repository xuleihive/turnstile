from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from backend.services.client_user_policy import (
    CONTEXT_KEY_NAME,
    compose_client_user_policy,
    strip_verified_client_user_policy,
)
from tests.support.paths import REPOSITORY_ROOT
from turnstile_core.integrations.apim_budget_execution import compose_budget_execution_parent
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    validate_parent_policy,
)


@pytest.mark.parametrize("template", [
    "foundry-finops-policy.xml", "foundry-finops-policy.schema1.xml",
])
def test_offline_client_policy_reversal_proves_the_entire_composition(template: str) -> None:
    source = (REPOSITORY_ROOT / "infra/policies" / template).read_text()
    installed = compose_client_user_policy(source)
    restored = strip_verified_client_user_policy(installed)
    assert component_digest(parse_policy(source)) == component_digest(parse_policy(restored))
    validate_parent_policy(restored, source)


@pytest.mark.parametrize("mutation", ["signing_key", "binding", "cleanup", "version"])
def test_offline_client_policy_reversal_refuses_tampered_or_unknown_authentication(
    mutation: str,
) -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.schema1.xml").read_text()
    root = parse_policy(compose_client_user_policy(source))
    if mutation == "signing_key":
        node = root.find(".//validate-jwt/issuer-signing-keys/key")
        assert node is not None
        node.text = "other-secret"
    elif mutation == "binding":
        node = root.find(".//validate-jwt/../choose/when")
        assert node is not None
        node.set("condition", "@(false)")
    elif mutation == "cleanup":
        node = root.find("./inbound/set-header[@name='x-turnstile-client-context']")
        assert node is not None
        node.set("exists-action", "override")
    else:
        node = root.find(".//set-variable[@name='clientContextPolicyVersion']")
        assert node is not None
        node.set("value", "@(2)")
    with pytest.raises(ValueError):
        strip_verified_client_user_policy(ET.tostring(root, encoding="unicode"))


def test_legacy_parent_can_be_verified_but_cannot_silently_enable_budget_execution() -> None:
    source = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.schema1.xml").read_text()
    validate_parent_policy(source, source)
    with pytest.raises(PolicyCompilationError, match="schema-two"):
        compose_budget_execution_parent(source, source, enabled=True)


def test_trusted_client_preserves_employee_and_subscription_branches() -> None:
    original = (REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    before = ET.fromstring(original)
    after = ET.fromstring(compose_client_user_policy(original))
    prior = next(
        x
        for x in before.findall("./inbound/choose")
        if x.find("./when/validate-azure-ad-token") is not None
    )
    current = next(
        x
        for x in after.findall("./inbound/choose")
        if x.find("./when/validate-azure-ad-token") is not None
    )
    assert len(current.findall("when")) == 2
    old_employee = prior.find("when")
    old_subscription, new_subscription = prior.find("otherwise"), current.find("otherwise")
    assert old_employee is not None
    assert old_subscription is not None and new_subscription is not None
    assert ET.tostring(old_employee) == ET.tostring(current.findall("when")[1])
    assert ET.tostring(old_subscription) == ET.tostring(new_subscription)
    trusted = current.findall("when")[0]
    validator = trusted.find("validate-jwt")
    assert validator is not None
    assert validator.get("require-signed-tokens") == "true"
    assert validator.findtext("issuer-signing-keys/key") == "{{" + CONTEXT_KEY_NAME + "}}"
    predicate = trusted.find("./choose/when")
    assert predicate is not None
    check = predicate.get("condition", "")
    assert 'token.Claims.GetValueOrDefault("subscription"' in check
    assert 'token.Claims.GetValueOrDefault("model"' in check
    assert trusted.find(".//set-variable[@name='ledgerUrl']") is not None
    assert trusted.find(".//set-variable[@name='applicationLedgerUrl']") is None
    common_application = after.find(".//set-variable[@name='applicationLedgerUrl']")
    assert common_application is not None
    assert (
        len(after.findall(".//send-request[@response-variable-name='applicationLedgerWrite']")) == 1
    )
    removed = after.find("./inbound/set-header[@name='x-turnstile-client-context']")
    assert removed is not None and removed.get("exists-action") == "delete"
    with pytest.raises(ValueError, match="already installed"):
        compose_client_user_policy(ET.tostring(after, encoding="unicode"))
