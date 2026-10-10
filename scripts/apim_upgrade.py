from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_image_policy import IMAGE_OPERATION_ID
from turnstile_core.integrations.apim_policy_components import (
    component_digest,
    compose_image_parent_policy,
    parse_policy,
    serialize_policy,
    validate_parent_policy,
)

UPGRADE_VERSION = "images-v2"
IMAGE_OPERATION_PROPERTIES: dict[str, Any] = {
    "displayName": "Image generations",
    "method": "POST",
    "urlTemplate": "/images/generations",
    "templateParameters": [],
    "request": {
        "queryParameters": [],
        "headers": [],
        "representations": [{"contentType": "application/json"}],
    },
    "responses": [],
}


class ApimUpgradeError(RuntimeError):
    pass


def policy_digest(value: str) -> str:
    return component_digest(parse_policy(value), normalize_text_defaults=False)


def document_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def operation_definition(value: dict[str, Any]) -> dict[str, Any]:
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: normalize(child)
                for key, child in item.items()
                if not (key == "description" and child in (None, ""))
            }
        if isinstance(item, list):
            return [normalize(child) for child in item]
        return item

    return dict(normalize({key: item for key, item in value.items() if key != "policies"}))


def operation_readback_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    def without_empty_derived_path(value: dict[str, Any]) -> dict[str, Any]:
        return {
            name: item
            for name, item in value.items()
            if name != "effectivePath" or item is not None
        }

    return without_empty_derived_path(expected) == without_empty_derived_path(actual)


@dataclass(frozen=True)
class GatewaySnapshot:
    revision: str
    api_properties: dict[str, Any]
    parent_policy: str
    operations: dict[str, dict[str, Any]]
    operation_policies: dict[str, str | None]

    def fingerprint(self) -> str:
        return document_digest(
            {
                "revision": self.revision,
                "api": self.api_properties,
                "parent": policy_digest(self.parent_policy),
                "operations": self.operations,
                "policies": {
                    name: policy_digest(value) if value is not None else None
                    for name, value in self.operation_policies.items()
                },
            }
        )


@dataclass(frozen=True)
class ImageUpgradePlan:
    api_resource_id: str
    source: GatewaySnapshot
    parent_policy: str
    create_operation: bool
    initialize_image_policy: bool
    revision: str
    version: str = UPGRADE_VERSION

    @property
    def required(self) -> bool:
        return (
            self.create_operation
            or self.initialize_image_policy
            or policy_digest(self.source.parent_policy) != policy_digest(self.parent_policy)
        )

    def document(self) -> dict[str, Any]:
        return asdict(self)


def plan_image_upgrade(
    api_resource_id: str,
    snapshot: GatewaySnapshot,
    canonical_parent: str,
) -> ImageUpgradePlan:
    try:
        parent = snapshot.parent_policy
        root = parse_policy(parent)
        if not root.findall(".//set-variable[@name='imageGenerationPolicyVersion']"):
            parent = compose_image_parent_policy(parent, ())
            upgraded = parse_policy(parent)
            actual_rates = [
                rate
                for rate in upgraded.iter("rate-limit-by-key")
                if "images:" in rate.get("counter-key", "")
            ]
            reference_rates = [
                rate
                for rate in parse_policy(canonical_parent).iter("rate-limit-by-key")
                if "images:" in rate.get("counter-key", "")
            ]
            for actual, reference in zip(actual_rates, reference_rates, strict=True):
                count = reference.get("increment-count")
                if count is None or not 1 <= int(count) <= 2**31 - 1:
                    raise ValueError("Invalid canonical image counter")
                actual.set("increment-count", count)
            parent = serialize_policy(upgraded)
        validate_parent_policy(parent, canonical_parent)
    except (PolicyCompilationError, ElementTree.ParseError, KeyError, ValueError) as error:
        raise ApimUpgradeError(
            "The existing APIM parent policy is not a supported public version; "
            "review the policy migration without overwriting customer configuration"
        ) from error
    operation = snapshot.operations.get(IMAGE_OPERATION_ID)
    if operation is not None and any(
        operation.get(name, [] if name == "templateParameters" else None)
        != IMAGE_OPERATION_PROPERTIES[name]
        for name in ("method", "urlTemplate", "templateParameters")
    ):
        raise ApimUpgradeError("The existing image operation conflicts with the fixed route")
    identity = document_digest(
        {
            "target": api_resource_id.casefold(),
            "source": snapshot.fingerprint(),
            "parent": policy_digest(parent),
            "version": UPGRADE_VERSION,
        }
    )
    return ImageUpgradePlan(
        api_resource_id=api_resource_id,
        source=snapshot,
        parent_policy=parent,
        create_operation=operation is None,
        initialize_image_policy=snapshot.operation_policies.get(IMAGE_OPERATION_ID) is None,
        revision=f"turnstile-{UPGRADE_VERSION}-{identity[:16]}",
    )


def verify_upgrade_snapshot(
    plan: ImageUpgradePlan,
    observed: GatewaySnapshot,
    default_image_policy: str,
) -> None:
    if observed.revision != plan.revision or observed.api_properties != plan.source.api_properties:
        raise ApimUpgradeError("The upgrade candidate changed API identity or configuration")
    if policy_digest(observed.parent_policy) != policy_digest(plan.parent_policy):
        raise ApimUpgradeError("The upgrade parent-policy readback differs from the plan")
    expected_operations = dict(plan.source.operations)
    if plan.create_operation:
        expected_operations[IMAGE_OPERATION_ID] = dict(IMAGE_OPERATION_PROPERTIES)
    if observed.operations.keys() != expected_operations.keys() or any(
        not operation_readback_matches(properties, observed.operations[name])
        for name, properties in expected_operations.items()
    ):
        raise ApimUpgradeError("The upgrade changed existing operation definitions")
    expected_policies = dict(plan.source.operation_policies)
    if plan.initialize_image_policy:
        expected_policies[IMAGE_OPERATION_ID] = default_image_policy
    actual_digests = {
        name: policy_digest(value) if value is not None else None
        for name, value in observed.operation_policies.items()
    }
    expected_digests = {
        name: policy_digest(value) if value is not None else None
        for name, value in expected_policies.items()
    }
    if actual_digests != expected_digests:
        raise ApimUpgradeError("The upgrade changed an existing operation policy")


class UpgradeBackend(Protocol):
    def read(self, revision: str | None = None) -> GatewaySnapshot | None: ...

    def require_maintenance(self) -> None: ...

    def prepare(self, plan: ImageUpgradePlan, *, create_revision: bool) -> None: ...

    def promote(self, plan: ImageUpgradePlan, revision: str) -> None: ...


class AzureUpgradeBackend:
    def __init__(
        self,
        client: httpx.Client,
        api_resource_id: str,
        application_ids: tuple[str, str],
        deploy: Callable[[ImageUpgradePlan, str, str, bool], None],
    ) -> None:
        match = re.fullmatch(
            r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
            r"Microsoft\.ApiManagement/service/([^/]+)/apis/([A-Za-z0-9._-]+)",
            api_resource_id,
            re.IGNORECASE,
        )
        if match is None:
            raise ApimUpgradeError("An exact subscription, APIM and API resource ID is required")
        self.client = client
        self.api_resource_id = api_resource_id
        self.subscription, self.resource_group, self.apim_name, self.api_id = match.groups()
        self.application_ids = application_ids
        self.deploy = deploy
        if any(
            not resource.casefold().startswith(
                f"/subscriptions/{self.subscription}/resourcegroups/".casefold()
            )
            for resource in application_ids
        ):
            raise ApimUpgradeError("Maintenance targets must belong to the selected subscription")

    def _get(self, resource_id: str, *, policy: bool = False) -> dict[str, Any] | None:
        parsed = urlsplit(resource_id)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != "https" or parsed.netloc != "management.azure.com":
                raise ApimUpgradeError("ARM pagination left the authorized management endpoint")
            path = unquote(parsed.path)
            url = resource_id
        else:
            path = unquote(parsed.path)
            url = "https://management.azure.com" + resource_id
        if any(part in {".", ".."} for part in path.split("/")):
            raise ApimUpgradeError("ARM read left the exact upgrade target")
        allowed_api = (
            path == self.api_resource_id
            or path.startswith(self.api_resource_id + ";rev=")
            or path.startswith(self.api_resource_id + "/")
        )
        if not allowed_api and path not in self.application_ids:
            raise ApimUpgradeError("ARM read left the exact upgrade target")
        parameters = {} if parsed.query else {"api-version": "2024-05-01"}
        if path in self.application_ids:
            parameters["api-version"] = "2024-11-01"
        if policy:
            parameters["format"] = "rawxml"
        response = self.client.get(
            httpx.URL(url).copy_merge_params(parameters), follow_redirects=False
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ApimUpgradeError(f"ARM upgrade read returned HTTP {response.status_code}")
        policy_text = response.text.lstrip("\ufeff \t\r\n")
        if policy and policy_text.startswith("<"):
            try:
                parse_policy(policy_text)
            except ElementTree.ParseError as error:
                raise ApimUpgradeError("ARM returned malformed raw XML policy") from error
            return {"properties": {"value": policy_text}}
        try:
            value = response.json()
        except ValueError as error:
            raise ApimUpgradeError("ARM upgrade read returned invalid JSON") from error
        if not isinstance(value, dict):
            raise ApimUpgradeError("ARM upgrade read returned an invalid object")
        return value

    def _policy(self, resource_id: str) -> str | None:
        document = self._get(resource_id + "/policies/policy", policy=True)
        if document is None:
            return None
        value = document.get("properties", {}).get("value")
        if not isinstance(value, str) or not value:
            raise ApimUpgradeError("ARM returned an invalid raw XML policy")
        parse_policy(value)
        return value

    def read(self, revision: str | None = None) -> GatewaySnapshot | None:
        path = self.api_resource_id
        if revision is not None:
            path += ";rev=" + quote(revision, safe="")
        document = self._get(path)
        if document is None:
            return None
        properties = dict(document.get("properties", {}))
        if properties.get("provisioningState") not in {None, "Succeeded"}:
            raise ApimUpgradeError(
                "An ARM operation is still in progress; do not retry concurrently"
            )
        if properties.get("isOnline") is False:
            raise ApimUpgradeError("An offline API revision requires explicit operator review")
        observed_revision = properties.get("apiRevision")
        if not isinstance(observed_revision, str) or not observed_revision:
            raise ApimUpgradeError("ARM returned no API revision identity")
        if revision is not None and revision != observed_revision:
            raise ApimUpgradeError("ARM returned a different API revision")
        path = self.api_resource_id + ";rev=" + quote(observed_revision, safe="")
        for name in (
            "apiRevision",
            "apiRevisionDescription",
            "isCurrent",
            "isOnline",
            "provisioningState",
            "sourceApiId",
        ):
            properties.pop(name, None)
        parent = self._policy(path)
        if parent is None:
            raise ApimUpgradeError(
                "The existing API has no parent policy; automatic upgrade refused"
            )
        operations: dict[str, dict[str, Any]] = {}
        policies: dict[str, str | None] = {}
        next_page: str | None = path + "/operations"
        seen_pages: set[str] = set()
        while next_page:
            if next_page in seen_pages or len(seen_pages) >= 100:
                raise ApimUpgradeError("Operation pagination is incomplete or cyclic")
            seen_pages.add(next_page)
            page = self._get(next_page)
            if page is None or not isinstance(page.get("value"), list):
                raise ApimUpgradeError("Operation enumeration was incomplete")
            for operation in page["value"]:
                name = operation.get("name")
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                    raise ApimUpgradeError("ARM returned an invalid operation identity")
                if name in operations:
                    raise ApimUpgradeError("ARM returned duplicate operation identities")
                details = self._get(path + "/operations/" + quote(name, safe=""))
                if details is None or not isinstance(details.get("properties"), dict):
                    raise ApimUpgradeError("An operation changed during snapshot collection")
                operations[name] = operation_definition(details["properties"])
                policies[name] = self._policy(path + "/operations/" + quote(name, safe=""))
                embedded_policy = details["properties"].get("policies")
                if embedded_policy and (
                    not isinstance(embedded_policy, str)
                    or policies[name] is None
                    or policy_digest(embedded_policy) != policy_digest(str(policies[name]))
                ):
                    raise ApimUpgradeError("Operation policy representations disagree")
            next_page = page.get("nextLink")
            if next_page is not None and not isinstance(next_page, str):
                raise ApimUpgradeError("ARM returned an invalid pagination link")
        return GatewaySnapshot(observed_revision, properties, parent, operations, policies)

    def require_maintenance(self) -> None:
        for resource_id in self.application_ids:
            application = self._get(resource_id)
            if application is None or application.get("properties", {}).get("state") != "Stopped":
                raise ApimUpgradeError(
                    "Drain publication and rollback work, then stop the API and Control-plane "
                    "for the upgrade maintenance window; existing APIM text traffic is retained"
                )

    def prepare(self, plan: ImageUpgradePlan, *, create_revision: bool) -> None:
        self.deploy(plan, "prepare", plan.revision, create_revision)

    def promote(self, plan: ImageUpgradePlan, revision: str) -> None:
        self.require_maintenance()
        self.deploy(plan, "promote", revision, False)


def upgrade_parameters(
    plan: ImageUpgradePlan,
    stage: str,
    revision: str,
    *,
    create_revision: bool,
) -> dict[str, Any]:
    parts = plan.api_resource_id.split("/")
    if stage not in {"prepare", "promote"} or revision not in {plan.revision, plan.source.revision}:
        raise ApimUpgradeError("The deployment stage or revision is outside the upgrade plan")
    return {
        "apimResourceGroupName": parts[4],
        "apimName": parts[8],
        "apiId": parts[10],
        "sourceRevision": plan.source.revision,
        "revision": revision,
        "stage": stage,
        "createRevision": create_revision,
        "initializeImagePolicy": plan.initialize_image_policy,
        "apiProperties": plan.source.api_properties if stage == "prepare" else {},
        "parentPolicy": plan.parent_policy if stage == "prepare" else "",
        "imageOperationProperties": plan.source.operations.get(
            IMAGE_OPERATION_ID, IMAGE_OPERATION_PROPERTIES
        )
        if stage == "prepare"
        else {},
    }


def validate_upgrade_what_if(
    result: dict[str, Any],
    plan: ImageUpgradePlan,
    stage: str,
    revision: str,
) -> None:
    api = plan.api_resource_id.casefold()
    candidate = api + ";rev=" + revision.casefold()
    allowed = (
        {
            candidate,
            candidate + "/policies/policy",
            candidate + "/operations/images-generations",
            candidate + "/operations/images-generations/policies/policy",
        }
        if stage == "prepare"
        else {api + "/releases/infrastructure-" + revision.casefold()}
    )
    changes = result.get("changes", result.get("properties", {}).get("changes"))
    if not isinstance(changes, list):
        raise ApimUpgradeError("Upgrade what-if did not enumerate resource changes")
    for change in changes:
        kind = change.get("changeType")
        if kind in {"Ignore", "NoChange"}:
            continue
        resource_id = str(change.get("resourceId", "")).casefold()
        if kind not in {"Create", "Modify"} or resource_id not in allowed:
            raise ApimUpgradeError("Upgrade what-if contains an unapproved resource change")


def _verify_partial_candidate(
    plan: ImageUpgradePlan,
    observed: GatewaySnapshot,
    default_image_policy: str,
) -> None:
    if observed.revision != plan.revision or observed.api_properties != plan.source.api_properties:
        raise ApimUpgradeError("An interrupted candidate changed API configuration")
    if policy_digest(observed.parent_policy) not in {
        policy_digest(plan.source.parent_policy),
        policy_digest(plan.parent_policy),
    }:
        raise ApimUpgradeError("An interrupted candidate has an unrecognized parent policy")
    if set(observed.operations) - set(plan.source.operations) - {IMAGE_OPERATION_ID}:
        raise ApimUpgradeError("An interrupted candidate contains unexpected operations")
    for name, properties in plan.source.operations.items():
        if observed.operations.get(name) != properties:
            raise ApimUpgradeError("An interrupted candidate changed an existing operation")
    for name, policy in plan.source.operation_policies.items():
        actual = observed.operation_policies.get(name)
        if policy is not None and (
            actual is None or policy_digest(actual) != policy_digest(policy)
        ):
            raise ApimUpgradeError("An interrupted candidate changed an existing operation policy")
        if policy is None and actual is not None and name != IMAGE_OPERATION_ID:
            raise ApimUpgradeError("An interrupted candidate added an existing operation policy")
    image = observed.operations.get(IMAGE_OPERATION_ID)
    if (
        plan.create_operation
        and image is not None
        and not operation_readback_matches(IMAGE_OPERATION_PROPERTIES, image)
    ):
        raise ApimUpgradeError("An interrupted candidate changed the image operation")
    image_policy = observed.operation_policies.get(IMAGE_OPERATION_ID)
    if (
        plan.initialize_image_policy
        and image_policy is not None
        and (policy_digest(image_policy) != policy_digest(default_image_policy))
    ):
        raise ApimUpgradeError("An interrupted candidate contains an unrecognized image policy")


def execute_image_upgrade(
    plan: ImageUpgradePlan,
    backend: UpgradeBackend,
    default_image_policy: str,
    journal: dict[str, Any] | None,
    save: Callable[[dict[str, Any]], None],
    *,
    rollback: bool = False,
    prepare_only: bool = False,
    candidate_validator: Callable[[GatewaySnapshot], None] | None = None,
) -> dict[str, Any]:
    if not plan.required:
        return {"version": plan.version, "status": "not_required"}
    identity = document_digest(plan.document())
    if journal is not None and journal.get("planSha256") != identity:
        raise ApimUpgradeError("The upgrade journal belongs to a different target or plan")
    backend.require_maintenance()
    current = backend.read()
    if current is None:
        raise ApimUpgradeError("The existing API is missing; an upgrade must not bootstrap it")
    if rollback:
        if journal is None or journal.get("status") not in {
            "passed",
            "promoting",
            "rolling_back",
            "rolled_back",
        }:
            raise ApimUpgradeError("Rollback requires a recorded promotion of this upgrade")
        original = backend.read(plan.source.revision)
        if original is None or original.fingerprint() != plan.source.fingerprint():
            raise ApimUpgradeError("The original revision changed; rollback is unsafe")
        if current.revision == plan.source.revision:
            result = {**journal, "status": "rolled_back"}
            save(result)
            return result
        verify_upgrade_snapshot(plan, current, default_image_policy)
        save({**journal, "status": "rolling_back"})
        backend.promote(plan, plan.source.revision)
        restored = backend.read()
        if restored is None or restored.fingerprint() != plan.source.fingerprint():
            raise ApimUpgradeError("Rollback was not confirmed; inspect the recorded operation")
        result = {**journal, "status": "rolled_back"}
        save(result)
        return result
    if current.revision == plan.revision:
        if journal is None or journal.get("status") not in {"promoting", "passed"}:
            raise ApimUpgradeError("An upgrade revision is current without a promotion checkpoint")
        verify_upgrade_snapshot(plan, current, default_image_policy)
        result = {**journal, "status": "passed"}
        save(result)
        return result
    if current.fingerprint() != plan.source.fingerprint():
        raise ApimUpgradeError(
            "The current API changed after planning; regenerate the upgrade plan"
        )
    candidate = backend.read(plan.revision)
    if candidate is not None:
        if journal is None:
            raise ApimUpgradeError("An unowned upgrade candidate already exists")
        _verify_partial_candidate(plan, candidate, default_image_policy)
    record = {"version": plan.version, "planSha256": identity, "status": "preparing"}
    save(record)
    if candidate is not None:
        try:
            verify_upgrade_snapshot(plan, candidate, default_image_policy)
        except ApimUpgradeError:
            backend.prepare(plan, create_revision=False)
    else:
        backend.prepare(plan, create_revision=True)
    candidate = backend.read(plan.revision)
    if candidate is None:
        raise ApimUpgradeError("The candidate was not created; no promotion was attempted")
    verify_upgrade_snapshot(plan, candidate, default_image_policy)
    if prepare_only:
        result = {**record, "status": "prepared"}
        save(result)
        return result
    current = backend.read()
    if current is None or current.fingerprint() != plan.source.fingerprint():
        raise ApimUpgradeError("The current API changed before promotion")
    if candidate_validator is not None:
        candidate_validator(candidate)
    save({**record, "status": "promoting"})
    backend.promote(plan, plan.revision)
    observed = backend.read()
    if observed is None:
        raise ApimUpgradeError("Promotion outcome is unknown; inspect the recorded operation")
    verify_upgrade_snapshot(plan, observed, default_image_policy)
    result = {**record, "status": "passed"}
    save(result)
    return result
