"""Add a reviewed trusted-desktop branch without changing the existing callers."""

from __future__ import annotations

import copy
from xml.etree import ElementTree as ET

from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    parse_policy,
    serialize_policy,
)

from .client_user import CLIENT_AUDIENCE, CLIENT_ISSUER

CONTEXT_KEY_NAME = "turnstile-client-context-key"


def strip_verified_client_user_policy(source: str) -> str:
    """Reverse only a complete v1 composition, for reviewed offline upgrades."""
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None:
        raise ValueError("APIM inbound policy is missing")
    markers = root.findall(".//set-variable[@name='clientContextPolicyVersion']")
    validators = root.findall(".//validate-jwt[@header-name='x-turnstile-client-context']")
    if not markers and not validators:
        return source
    if (len(markers) != 1 or markers[0] not in inbound or markers[0].get("value") != "@(1)"
        or len(validators) != 1):
        raise ValueError("Unrecognized client context policy")
    parents = {child: parent for parent in root.iter() for child in parent}
    branch = parents[validators[0]]
    choice = parents.get(branch)
    if branch.tag != "when" or choice is None or choice not in inbound:
        raise ValueError("Client context authentication is outside its reviewed boundary")
    choice.remove(branch)
    inbound.remove(markers[0])
    removals = inbound.findall("set-header[@name='x-turnstile-client-context']")
    if len(removals) != 1:
        raise ValueError("Client context header cleanup is missing or ambiguous")
    inbound.remove(removals[0])
    base = serialize_policy(root)
    restored = parse_policy(compose_client_user_policy(base))
    if component_digest(restored, normalize_text_defaults=False) != component_digest(
        parse_policy(source), normalize_text_defaults=False,
    ):
        raise ValueError("Client context policy differs from the complete reviewed v1 contract")
    return base


def _header(parent: ET.Element, name: str, value: str) -> None:
    node = ET.SubElement(parent, "set-header", {"name": name, "exists-action": "override"})
    ET.SubElement(node, "value").text = value


def compose_client_user_policy(source: str) -> str:
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None:
        raise ValueError("APIM inbound policy is missing")
    if root.find(".//set-variable[@name='clientContextPolicyVersion']") is not None:
        raise ValueError("Client context policy is already installed")
    auth_choose = next(
        (
            n
            for n in inbound.findall("choose")
            if n.find("./when/validate-azure-ad-token") is not None
        ),
        None,
    )
    if auth_choose is None:
        raise ValueError("Reviewed employee authentication branch is missing")
    employee = auth_choose.find("when")
    subscription = auth_choose.find("otherwise")
    if employee is None or subscription is None:
        raise ValueError("Reviewed subscription authentication branch is missing")
    branch = ET.Element(
        "when",
        {
            "condition": '@(context.Request.Headers.ContainsKey("x-turnstile-client-context"))',
        },
    )
    validator = ET.SubElement(
        branch,
        "validate-jwt",
        {
            "header-name": "x-turnstile-client-context",
            "require-scheme": "",
            "output-token-variable-name": "employeeToken",
            "require-expiration-time": "true",
            "require-signed-tokens": "true",
            "clock-skew": "5",
            "failed-validation-httpcode": "401",
            "failed-validation-error-message": "Invalid client context",
        },
    )
    keys = ET.SubElement(validator, "issuer-signing-keys")
    ET.SubElement(keys, "key").text = "{{" + CONTEXT_KEY_NAME + "}}"
    ET.SubElement(ET.SubElement(validator, "audiences"), "audience").text = CLIENT_AUDIENCE
    ET.SubElement(ET.SubElement(validator, "issuers"), "issuer").text = CLIENT_ISSUER
    ET.SubElement(branch, "set-variable", {"name": "hasEmployeeToken", "value": "@(true)"})
    verification = ET.SubElement(branch, "choose")
    denied = ET.SubElement(
        verification,
        "when",
        {
            "condition": """@{
        var token = (Jwt)context.Variables["employeeToken"];
        var asked = context.Request.Body.As<JObject>(preserveContent: true);
        return context.Subscription == null
          || (string)asked["model"] != token.Claims.GetValueOrDefault("model", "")
          || context.Subscription.Id.Split('/').Last()
            != token.Claims.GetValueOrDefault("subscription", "")
          || context.Request.Headers.GetValueOrDefault("x-request-id", "")
            != token.Claims.GetValueOrDefault("jti", "");
    }"""
        },
    )
    rejection = ET.SubElement(denied, "return-response")
    ET.SubElement(rejection, "set-status", {"code": "403", "reason": "Forbidden"})
    _header(rejection, "Content-Type", "application/json")
    ET.SubElement(
        rejection, "set-body"
    ).text = '{"error":"Client context does not match the invocation"}'

    def copy_children(parent: ET.Element) -> None:
        for original in parent:
            if original.tag == "validate-azure-ad-token":
                continue
            if original.find(".//set-variable[@name='directoryIdentityCloud']") is not None:
                continue

            # Published-model checking is performed by the trusted runtime before signing.
            # Keep the publisher's two original guard anchors unchanged for future releases.
            def model_guard(node: ET.Element) -> bool:
                return (
                    node.tag == "choose"
                    and len(node) == 1
                    and node[0].tag == "when"
                    and "selectedModelKnown" in node[0].get("condition", "")
                )

            if model_guard(original):
                continue
            cloned = copy.deepcopy(original)
            for container in list(cloned.iter()):
                for child in list(container):
                    if model_guard(child):
                        container.remove(child)
            branch.append(cloned)

    copy_children(employee)
    ledger_index = next(
        (
            i
            for i, n in enumerate(branch)
            if n.find(".//set-variable[@name='ledgerUrl']") is not None
        ),
        None,
    )
    if ledger_index is None:
        raise ValueError("Reviewed person ledger anchor is missing")
    identity = ET.Element("choose")
    trusted = ET.SubElement(identity, "when", {"condition": "@(true)"})
    for name, claim in (
        ("employeeName", "preferred_username"),
        ("employeeDepartmentId", "department_id"),
        ("employeeDepartmentName", "department_name"),
    ):
        ET.SubElement(
            trusted,
            "set-variable",
            {
                "name": name,
                "value": (
                    '@(((Jwt)context.Variables["employeeToken"]).Claims.'
                    f'GetValueOrDefault("{claim}", ""))'
                ),
            },
        )
    for name, claim in (
        ("x-user-id", "preferred_username"),
        ("x-user-name", "user_name"),
        ("x-org-id", "organization_id"),
        ("x-org-name", "organization_name"),
        ("x-department-id", "department_id"),
        ("x-department-name", "department_name"),
    ):
        _header(
            trusted,
            name,
            f'@(((Jwt)context.Variables["employeeToken"]).Claims.GetValueOrDefault("{claim}", ""))',
        )
    _header(trusted, "x-request-source", "aigateway-user")
    branch.insert(ledger_index, identity)
    # Application admission is already a common inbound block after authentication.
    # Copying it here would reserve the same application's budget a second time.
    auth_choose.insert(0, branch)
    ET.SubElement(inbound, "set-variable", {"name": "clientContextPolicyVersion", "value": "@(1)"})
    removals = ET.SubElement(
        inbound,
        "set-header",
        {
            "name": "x-turnstile-client-context",
            "exists-action": "delete",
        },
    )
    del removals
    return serialize_policy(root)
