from __future__ import annotations

import ast
import base64
import hashlib
import json
import logging
import re
import traceback
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import SecretStr, ValidationError

from ..config import Settings
from ..domain.model_availability import (
    AVAILABILITY_PARTITION,
    ControlledGatewayFleet,
    ModelAvailabilityCommand,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeEnrollment,
    ModelAvailabilityOperation,
    ModelAvailabilityReadback,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
    ModelAvailabilityUnavailable,
)
from ..domain.storage_routes import (
    FixedProgramInstallation,
    ProgramCapabilityProof,
    canonical_document,
    document_digest,
)
from ..integrations.apim_control_plane_contract import (
    ApimPublisherClientResolver,
    PolicyCompilationError,
    missing_management_actions,
)
from ..integrations.apim_publisher_client import AzureApimPublisherClient
from ..integrations.apim_storage_routes import FixedProgramVerifier
from ..integrations.apim_target_client_factory import (
    apim_target_client_config_resolver,
    route_target_identity_matches,
)
from ..integrations.ledger import TableStorageLedger
from ..integrations.reconciliation import ManagedIdentityTokenProvider
from ..persistence.repository_contract import QueryRepository
from .worker_lease import WorkerLease

logger = logging.getLogger(__name__)
_MAX_CONCURRENT_TARGETS = 3
_TARGET_RENEW_BACKOFF_SECONDS = 300.0
_TARGET_RENEW_BACKOFF_LOCK = Lock()
_TARGET_RENEW_BACKOFF_UNTIL: dict[tuple[UUID, str], float] = {}
_TARGET_AUDIT_BACKOFF_UNTIL: dict[tuple[UUID, str], float] = {}
_TARGET_AUDIT_FAILING_SINCE: dict[tuple[UUID, str], float] = {}


def require_controlled_gateway_token_authority(
    gateway_resource_id: str,
    runtime_principals: set[str],
    platform_principals: set[str],
    assignments: Sequence[Mapping[str, Any]],
    *,
    complete: bool,
) -> None:
    if not complete or not runtime_principals or runtime_principals & platform_principals:
        raise ValueError("Controlled Gateway token authority inventory is incomplete")
    verified = set()
    action = "Microsoft.ApiManagement/service/gateways/generateToken/action"
    for assignment in assignments:
        role = assignment.get("role")
        if not isinstance(role, dict) or not isinstance(role.get("permissions"), list):
            raise ValueError("Controlled Gateway token authority role is unavailable")
        if missing_management_actions({"value": role["permissions"]}, [action]):
            continue
        if assignment.get("principal_type") in {"User", "Group"}:
            continue
        principal = str(assignment.get("principal_id", ""))
        if (assignment.get("principal_type") != "ServicePrincipal"
                or principal not in runtime_principals | platform_principals
                or assignment.get("condition") not in (None, "")):
            raise ValueError("Controlled Gateway token authority has an undeclared runtime")
        if principal in runtime_principals:
            if str(assignment.get("scope", "")).casefold() != gateway_resource_id.casefold():
                raise ValueError("Controlled runtime token role exceeds its Gateway scope")
            verified.add(principal)
    if verified != runtime_principals:
        raise ValueError("Controlled Gateway runtime token authority is missing")


def controlled_gateway_installation_command(
    runtime: bytes, configuration: Mapping[str, Any],
) -> str:
    ast.parse(runtime)
    encoded_runtime = base64.b64encode(runtime).decode()
    encoded_values = base64.b64encode(canonical_document(configuration).encode()).decode()
    code = (
        "import base64, json, os, subprocess\nfrom pathlib import Path\n"
        "runtime = Path('/opt/turnstile-gateway/vm-gateway-runtime.py')\n"
        "settings = Path('/etc/turnstile-gateway/settings.json')\n"
        "state = Path('/var/lib/turnstile-gateway')\n"
        "units = [Path('/etc/systemd/system') / name for name in "
        "('turnstile-gateway.service', 'turnstile-gateway-tls.service')]\n"
        "if any(path.exists() for path in (runtime, settings, state, *units)):\n"
        "    raise SystemExit('existing primary Gateway state')\n"
        "runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
        "settings.parent.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
        f"files = ((runtime, base64.b64decode('{encoded_runtime}')), "
        f"(settings, base64.b64decode('{encoded_values}')))\n"
        "for path, content in files:\n"
        "    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL "
        "| os.O_NOFOLLOW, 0o600)\n"
        "    with os.fdopen(descriptor, 'wb') as stream:\n"
        "        stream.write(content)\n        stream.flush()\n        os.fsync(stream.fileno())\n"
        "subprocess.run(['/usr/bin/python3', str(runtime), 'install'], check=True)\n"
    )
    ast.parse(code)
    return "set -eu\numask 077\npython3 - <<'TURNSTILE_HOST'\n" + code + "\nTURNSTILE_HOST"


def controlled_gateway_inspection_command() -> str:
    code = (
        "import hashlib, json, subprocess\nfrom pathlib import Path\n"
        "def run(arguments):\n"
        "    result = subprocess.run(arguments, capture_output=True, text=True, timeout=20)\n"
        "    return result.stdout.strip() if result.returncode == 0 else 'UNAVAILABLE'\n"
        "services = {name: run(['systemctl', 'is-active', name]) for name in "
        "('turnstile-gateway.service', 'turnstile-gateway-tls.service', "
        "'turnstile-gateway-token.timer')}\n"
        "containers = {}\napim_instances = []\n"
        "identifiers = run(['docker', 'ps', '-a', '--format', '{{.ID}}']).splitlines()\n"
        "for identifier in identifiers:\n"
        "    image = run(['docker', 'inspect', '--format', '{{.Config.Image}}', identifier])\n"
        "    if not image.startswith('mcr.microsoft.com/azure-api-management/gateway'):\n"
        "        continue\n"
        "    hostname = run(['docker', 'inspect', '--format', "
        "'{{.Config.Hostname}}', identifier])\n"
        "    apim_instances.append(hostname)\n"
        "for name in ('turnstile-vm-apim', 'turnstile-vm-tls'):\n"
        "    raw = run(['docker', 'inspect', '--format', '{{json .State}}', name])\n"
        "    state = json.loads(raw) if raw != 'UNAVAILABLE' else {}\n"
        "    containers[name] = {'running': state.get('Running'), "
        "'exit_code': state.get('ExitCode'), 'health': state.get('Health', {}).get('Status'), "
        "'image': run(['docker', 'inspect', '--format', '{{.Image}}', name]), "
        "'instance_id': run(['docker', 'inspect', '--format', '{{.Config.Hostname}}', name])}\n"
        "hashes = {}\n"
        "for name in ('/opt/turnstile-gateway/vm-gateway-runtime.py', "
        "'/etc/turnstile-gateway/settings.json', '/etc/turnstile-gateway/Caddyfile'):\n"
        "    path = Path(name)\n"
        "    hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() "
        "if path.is_file() else None\n"
        "print(json.dumps({'services': services, 'containers': containers, 'hashes': hashes, "
        "'listeners': run(['ss', '-lnt']), 'apim_instances': sorted(apim_instances)}))\n"
    )
    ast.parse(code)
    return "set -eu\npython3 - <<'TURNSTILE_HOST_CHECK'\n" + code + "\nTURNSTILE_HOST_CHECK"


def read_controlled_gateway_fleet(
    fleet: ControlledGatewayFleet,
    read_resource: Callable[[str], Mapping[str, Any]],
    now: datetime,
    gateway_resource_id: str,
) -> dict[str, Any]:
    approved = json.loads((Path(__file__).resolve().parents[1] / "domain"
                           / "controlled_gateway_runtimes.json").read_bytes())
    if (approved.get("schema_version") != 1
            or approved.get("controller_kind") != fleet.controller_kind
            or any(host.runtime_sha256 not in approved.get("runtime_sha256", [])
                   for host in fleet.hosts)):
        raise ValueError("Controlled Gateway runtime is not an approved guarded implementation")
    evidence = []
    instances = []
    principals = []
    observations = []
    for host in fleet.hosts:
        vm = read_resource(host.vm_resource_id)
        installation = read_resource(host.installation_command_id)
        inspection = read_resource(host.inspection_command_id)
        if (str(vm.get("id", "")).casefold() != host.vm_resource_id.casefold()
                or vm.get("properties", {}).get("provisioningState") != "Succeeded"):
            raise ValueError("Controlled Gateway VM identity or readiness changed")
        for expected, record in ((host.installation_command_id, installation),
                                 (host.inspection_command_id, inspection)):
            properties = record.get("properties", {})
            state = properties.get("instanceView", {})
            if (str(record.get("id", "")).casefold() != expected.casefold()
                    or properties.get("provisioningState") != "Succeeded"
                    or state.get("executionState") != "Succeeded" or state.get("exitCode") != 0):
                raise ValueError("Controlled Gateway host command did not succeed")
        installed_script = installation.get("properties", {}).get("source", {}).get("script", "")
        cache_guarded = False
        if not installed_script.startswith("set -eu\numask 077\npython3 - <<'TURNSTILE_HOST'\n"):
            raise ValueError("Controlled Gateway installation is not a complete reviewed wrapper")
        if installed_script.startswith("set -eu\numask 077\npython3 - <<'TURNSTILE_HOST'\n"):
            wrapper = installed_script.split("\n", 3)[3].rsplit("\nTURNSTILE_HOST", 1)[0]
            tree = ast.parse(wrapper)
            payloads = [node.args[0].value for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "base64"
                        and node.func.attr == "b64decode" and len(node.args) == 1
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)]
            if len(payloads) != 2:
                raise ValueError("Controlled Gateway complete runtime payload is missing")
            asset, settings = (base64.b64decode(value, validate=True) for value in payloads)
            if hashlib.sha256(asset).hexdigest() != host.runtime_sha256:
                raise ValueError("Controlled Gateway installed runtime digest changed")
            configuration = json.loads(settings)
            if installed_script != controlled_gateway_installation_command(asset, configuration):
                raise ValueError("Controlled Gateway installation wrapper was modified")
            identity = vm.get("identity", {})
            assigned = identity.get("userAssignedIdentities", {})
            actual_identity = next(iter(assigned.values())) if len(assigned) == 1 else {}
            if (configuration.get("instance_id") != host.instance_id
                    or configuration.get("gateway_image") != host.gateway_image
                    or hashlib.sha256(settings).hexdigest() != host.settings_sha256
                    or identity.get("type") != "UserAssigned" or len(assigned) != 1
                    or actual_identity.get("clientId") != configuration.get("client_id")
                    or not actual_identity.get("principalId")
                    or str(configuration.get("gateway_resource_id", "")).casefold()
                    != gateway_resource_id.casefold()):
                raise ValueError("Controlled Gateway installed settings changed")
            principals.append(str(actual_identity["principalId"]))
            runtime_tree = ast.parse(asset)
            cache_guarded = any(
                isinstance(node, ast.Dict) and any(
                    isinstance(key, ast.Constant) and key.value == "config.service.backup.enabled"
                    and isinstance(value, ast.Constant) and value.value == "false"
                    for key, value in zip(node.keys, node.values, strict=True)
                ) for node in ast.walk(runtime_tree)
            ) and any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and " --hostname " in node.value for node in ast.walk(runtime_tree))
        if not cache_guarded:
            raise ValueError("Controlled Gateway rejoin cache is not guarded")
        state = inspection.get("properties", {}).get("instanceView", {})
        if inspection.get("properties", {}).get("source", {}).get("script") != (
            controlled_gateway_inspection_command()
        ):
            raise ValueError("Controlled Gateway runtime inspection is not the reviewed command")
        observed_at = datetime.fromisoformat(str(state.get("endTime", "")).replace("Z", "+00:00"))
        if observed_at.tzinfo is None or not 0 <= (now - observed_at).total_seconds() <= 120:
            raise ValueError("Controlled Gateway runtime inspection is stale")
        observations.append(observed_at)
        runtime = json.loads(str(state.get("output", "")))
        containers = runtime.get("containers", {})
        active = containers.get("turnstile-vm-apim", {})
        if (set(containers) - {"turnstile-vm-apim", "turnstile-vm-tls"}
                or runtime.get("apim_instances") != [host.instance_id]
                or active.get("running") is not True
                or active.get("instance_id") != host.instance_id
                or active.get("image") != host.gateway_image.split("@", 1)[1]
                or runtime.get("services", {}).get("turnstile-gateway.service") != "active"
                or runtime.get("services", {}).get("turnstile-gateway-tls.service") != "active"
                or runtime.get("hashes", {}).get("/opt/turnstile-gateway/vm-gateway-runtime.py")
                != host.runtime_sha256
                or runtime.get("hashes", {}).get("/etc/turnstile-gateway/settings.json")
                != host.settings_sha256):
            raise ValueError("Controlled Gateway runtime or complete instance set changed")
        evidence.append({"vm": vm, "installation": installation, "inspection": inspection})
        instances.append(host.instance_id)
    return {"closed_fleet": True, "guarded_rejoin": True, "observed_at": min(observations),
            "instances": instances, "runtime_principals": principals,
            "controller_sha256": document_digest({"hosts": evidence})}


def enroll_controlled_gateway_nodes(
    repository: ModelAvailabilityRepository,
    installed: FixedProgramInstallation,
    enrollment: ModelAvailabilityNodeEnrollment,
    evidence: Mapping[str, Any],
    verify_node: Callable[[str, str], ProgramCapabilityProof],
    actor: str,
    now: datetime,
    *,
    clock: Callable[[], datetime] | None = None,
) -> bool:
    current_time = clock or (lambda: datetime.now(UTC))
    observed_at = evidence.get("observed_at")
    instances = evidence.get("instances")
    if (installed.native_gateway_scope is not True
            or enrollment.target_id != UUID(installed.program.scope.target_id)
            or enrollment.api_id != installed.program.scope.api_id
            or evidence.get("closed_fleet") is not True
            or evidence.get("guarded_rejoin") is not True
            or not isinstance(observed_at, datetime) or observed_at.tzinfo is None
            or not 0 <= (now - observed_at).total_seconds() <= 120
            or not isinstance(instances, list) or len(instances) != len(set(instances))
            or set(instances) != {node.instance_id for node in enrollment.nodes}
            or evidence.get("controller_sha256") != enrollment.controller_evidence_sha256
            or enrollment.fenced_installation_sha256
            != document_digest(installed.model_dump(mode="json"))
            or not actor.strip() or not 0 <= (now - enrollment.enrolled_at).total_seconds() <= 120):
        raise ValueError("Controlled node enrollment evidence is incomplete or stale")
    inventory = repository.get_model_availability_inventory().get(enrollment.target_id)
    if inventory is None:
        raise ValueError("Controlled node enrollment target inventory is unavailable")
    previous = [item for item in repository.list_model_availability_node_enrollments(
        enrollment.target_id,
    ) if item.deployment_id == enrollment.deployment_id]
    if len(previous) > 1:
        raise ValueError("Controlled node enrollment revision is ambiguous")
    revision = previous[0].revision if previous else 0
    if enrollment.revision != revision + 1:
        raise ValueError("Controlled node enrollment revision changed")
    proofs = []
    for node in enrollment.nodes:
        identity = installed.for_native_gateway(
            enrollment.native_gateway_id, node.instance_id, str(node.endpoint_url).rstrip("/"),
        ).program
        proof = verify_node(node.instance_id, str(node.endpoint_url).rstrip("/"))
        instant = int(current_time().timestamp())
        if (proof.identity != identity or proof.supported_profiles != installed.supported_profiles
                or not proof.issued_epoch <= instant < proof.expires_epoch):
            raise ValueError("Controlled node enrollment live proof does not match")
        proofs.append(proof)
    completed = current_time()
    if (not 0 <= (completed - observed_at).total_seconds() <= 120
            or any(not proof.issued_epoch <= int(completed.timestamp()) < proof.expires_epoch
                   for proof in proofs)):
        raise ValueError("Controlled node enrollment evidence expired before save")
    if repository.get_model_availability_inventory().get(enrollment.target_id) != inventory:
        raise ValueError("Controlled node enrollment inventory changed")
    return bool(repository.save_model_availability_node_enrollment(
        enrollment, expected_revision=revision, expected_inventory_digest=inventory, actor=actor,
    ))


def build_controlled_gateway_enroller(
    repository: QueryRepository, settings: Settings,
) -> Callable[[ControlledGatewayFleet, str], ModelAvailabilityNodeEnrollment]:
    availability = cast("ModelAvailabilityRepository", repository)

    def enroll(fleet: ControlledGatewayFleet, actor: str) -> ModelAvailabilityNodeEnrollment:
        deployment = repository.get_gateway_deployment(fleet.deployment_id)
        if (deployment is None or deployment.get("kind") != "self_hosted"
                or deployment.get("retired_at") is not None):
            raise ModelAvailabilityUnavailable("Controlled Gateway deployment is unavailable")
        target_id = UUID(str(deployment["apim_target_id"]))
        target = repository.get_apim_target(target_id)
        current = repository.effective_gateway_publication(target_id)
        if target is None or current is None:
            raise ModelAvailabilityUnavailable("Controlled Gateway fixed baseline is unavailable")
        raw = current.get("resource_manifest", {}).get("fixed_route_program")
        if not isinstance(raw, dict):
            raise ModelAvailabilityUnavailable("Controlled Gateway fixed program is missing")
        installed = FixedProgramInstallation.model_validate_json(json.dumps(raw))
        configs = apim_target_client_config_resolver(repository, settings)
        config = configs.resolve(target_id, include_probe_credentials=True)
        if (installed.native_gateway_scope is not True
            or config.settings.apim_probe_subscription_key is None
                or not route_target_identity_matches(config, installed.target_identity_sha256)
                or str(deployment.get("endpoint_url", "")).rstrip("/")
                not in {str(host.endpoint_url).rstrip("/") for host in fleet.hosts}):
            raise ModelAvailabilityUnavailable("Controlled Gateway program or endpoint changed")
        subscription = str(target["subscription_id"])
        if any(not host.vm_resource_id.startswith("/subscriptions/" + subscription + "/")
               for host in fleet.hosts):
            raise ValueError("Controlled Gateway hosts belong to another subscription")
        with httpx.Client(timeout=30, follow_redirects=False) as http:
            tokens = ManagedIdentityTokenProvider(client=http)
            management = AzureApimPublisherClient(
                config.settings, token_provider=tokens, client=http
            )

            def read_resource(resource: str) -> Mapping[str, Any]:
                declared = {value for host in fleet.hosts for value in (
                    host.vm_resource_id, host.installation_command_id, host.inspection_command_id,
                )}
                if resource not in declared:
                    raise ValueError("Controlled Gateway resource is not declared")
                response = http.get(
                    "https://management.azure.com" + resource,
                    params={"api-version": "2024-11-01", "$expand": "instanceView"},
                    headers={"Authorization": "Bearer " + tokens.token(
                        "https://management.azure.com/",
                    )},
                )
                response.raise_for_status()
                value: dict[str, Any] = response.json()
                return value

            now = datetime.now(UTC)
            gateway_resource = str(target["arm_resource_id"]) + "/gateways/"
            gateway_resource += str(deployment["native_gateway_id"])
            evidence = read_controlled_gateway_fleet(fleet, read_resource, now, gateway_resource)
            runtime_principals = set(evidence.get("runtime_principals", []))
            if len(runtime_principals) != len(fleet.hosts):
                raise ValueError("Controlled Gateway runtime identities are not distinct")

            def arm_json(
                resource: str, *, params: Mapping[str, str] | None = None,
            ) -> dict[str, Any]:
                response = http.get(
                    "https://management.azure.com" + resource,
                    params=params or {"api-version": "2022-04-01"},
                    headers={"Authorization": "Bearer " + tokens.token(
                        "https://management.azure.com/",
                    )},
                )
                response.raise_for_status()
                value: dict[str, Any] = response.json()
                return value

            role_path = gateway_resource + "/providers/Microsoft.Authorization/roleAssignments"
            roles = arm_json(role_path,
                             params={"api-version": "2022-04-01", "$filter": "atScope()"})
            if roles.get("nextLink") not in (None, "") or not isinstance(roles.get("value"), list):
                raise ValueError("Controlled Gateway role inventory is incomplete")
            declarations = []
            platform_principals = set()
            for site in settings.controlled_gateway_bootstrap_api_resources:
                if (not re.fullmatch(
                    r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/"
                    r"Microsoft\.Web/sites/[A-Za-z0-9-]+", site,
                ) or not site.startswith("/subscriptions/" + subscription + "/")):
                    raise ValueError("Controlled bootstrap API resource identity is invalid")
                platform = arm_json(site, params={"api-version": "2024-11-01"})
                principal = platform.get("identity", {}).get("principalId")
                if isinstance(principal, str):
                    platform_principals.add(principal)
            for assignment in roles["value"]:
                fields = assignment.get("properties", {})
                definition_id = fields.get("roleDefinitionId")
                if (not isinstance(definition_id, str)
                        or not definition_id.startswith("/subscriptions/" + subscription
                                                        + "/providers/Microsoft.Authorization/")):
                    raise ValueError("Controlled Gateway role definition identity is invalid")
                definition = arm_json(definition_id)
                declarations.append({"principal_id": fields.get("principalId"),
                                     "principal_type": fields.get("principalType"),
                                     "scope": fields.get("scope"),
                                     "condition": fields.get("condition"),
                                     "role": definition.get("properties", {})})
            require_controlled_gateway_token_authority(
                gateway_resource, runtime_principals, platform_principals, declarations,
                complete=True,
            )
            previous = [item for item in availability.list_model_availability_node_enrollments(
                target_id,
            ) if item.deployment_id == fleet.deployment_id]
            prior_revision = previous[0].revision if previous else 0
            if len(previous) > 1 or prior_revision != fleet.expected_revision:
                raise ValueError("Controlled Gateway enrollment expected revision changed")
            revision = prior_revision + 1
            native_id = str(deployment["native_gateway_id"])
            enrollment = ModelAvailabilityNodeEnrollment.model_validate({
                "enrollment_id": uuid5(fleet.deployment_id, "controlled-node-enrollment:"
                                       + str(revision) + ":" + evidence["controller_sha256"]),
                "target_id": target_id, "deployment_id": fleet.deployment_id,
                "api_id": installed.program.scope.api_id, "native_gateway_id": native_id,
                "revision": revision, "controller_evidence_sha256": evidence["controller_sha256"],
                "fenced_installation_sha256": document_digest(installed.model_dump(mode="json")),
                "rejoin_contract": "enrolled_nodes_only_guarded_bootstrap", "enrolled_at": now,
                "nodes": [{"instance_id": host.instance_id, "endpoint_url": host.endpoint_url}
                          for host in fleet.hosts],
            })

            def verify_node(instance: str, endpoint: str) -> ProgramCapabilityProof:
                node = installed.for_native_gateway(native_id, instance, endpoint)
                key = cast(SecretStr, config.settings.apim_probe_subscription_key)
                return FixedProgramVerifier(node, management, key, http).verify()

            enroll_controlled_gateway_nodes(
                availability, installed, enrollment, evidence, verify_node, actor, now,
            )
            return enrollment

    return enroll


def loggable_reason(error: Exception) -> str | None:
    reason = str(error) if isinstance(
        error, (ModelAvailabilityUnavailable, PolicyCompilationError),
    ) else None
    if reason is not None and (
        len(reason) > 180
        or any(not (character.isalnum() or character in " _.-") for character in reason)
    ):
        return None
    return reason


def _log_verification_failure(target_id: UUID, error: Exception) -> None:
    frames = traceback.extract_tb(error.__traceback__)
    locations = ",".join(
        f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
        for frame in frames[-4:]
    )
    transport = error if isinstance(error, httpx.HTTPStatusError) else error.__cause__
    status = (
        transport.response.status_code if isinstance(transport, httpx.HTTPStatusError) else None
    )
    path = (
        transport.request.url.path
        if isinstance(transport, httpx.HTTPStatusError)
        and transport.request.url.host == "management.azure.com"
        else None
    )
    reason = loggable_reason(error)
    logger.warning(
        "Model availability coverage failed target=%s error=%s http_status=%s "
        "path=%s locations=%s reason=%s",
        target_id,
        type(error).__name__,
        status,
        path,
        locations,
        reason,
    )


class AvailabilityStateReader(Protocol):
    def get_model_availability_state(self, model_id: UUID) -> ModelAvailabilityState | None: ...


class ModelAvailabilityRepository(AvailabilityStateReader, Protocol):
    def model_availability_supported(self) -> bool: ...

    def model_availability_proof_guard(
        self, target_id: UUID, *, wait: bool,
    ) -> AbstractContextManager[None]: ...

    def list_model_availability(self) -> list[ModelAvailabilityReadback]: ...

    def change_model_availability(
        self,
        model_id: UUID,
        command: ModelAvailabilityCommand,
        actor: str,
    ) -> ModelAvailabilityOperation: ...

    def retry_model_availability(
        self,
        operation_id: UUID,
        expected_revision: int,
        actor: str,
    ) -> ModelAvailabilityOperation: ...

    def get_model_availability_operation(
        self,
        operation_id: UUID,
    ) -> ModelAvailabilityOperation | None: ...

    def get_model_availability_inventory(self, model_id: UUID | None = None) -> dict[UUID, str]: ...

    def get_model_availability_states(
        self, model_ids: Sequence[UUID],
    ) -> dict[UUID, ModelAvailabilityState]: ...

    def get_model_availability_coverage(
        self, target_id: UUID,
    ) -> ModelAvailabilityCoverage | None: ...

    def save_model_availability_coverage(self, coverage: ModelAvailabilityCoverage) -> bool: ...

    def list_model_availability_node_enrollments(
        self,
        target_id: UUID,
    ) -> list[ModelAvailabilityNodeEnrollment]: ...

    def save_model_availability_node_enrollment(
        self,
        enrollment: ModelAvailabilityNodeEnrollment,
        *,
        expected_revision: int,
        expected_inventory_digest: str,
        actor: str,
    ) -> bool: ...

    def stage_model_availability(
        self,
        model_id: UUID,
        publication_id: UUID,
    ) -> ModelAvailabilityState: ...

    def claim_model_availability_task(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...

    def renew_model_availability_lease(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        lease_seconds: int,
    ) -> bool: ...

    def finish_model_availability_task(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        projected: ModelAvailabilityState | None,
        results: Sequence[ModelAvailabilityTargetResult],
        error_code: str | None,
    ) -> bool: ...

    def mark_model_availability_projected(
        self,
        model_id: UUID,
        revision: int,
        worker_id: str,
        projected: ModelAvailabilityState,
    ) -> bool: ...


class ModelAvailabilityService:
    def __init__(
        self, repository: ModelAvailabilityRepository,
        node_enroller: Callable[[ControlledGatewayFleet, str], ModelAvailabilityNodeEnrollment]
        | None = None,
    ) -> None:
        self._repository = repository
        self._node_enroller = node_enroller

    def enroll_nodes(
        self, fleet: ControlledGatewayFleet, actor: str,
    ) -> ModelAvailabilityNodeEnrollment:
        self._require_supported()
        if self._node_enroller is None:
            raise ModelAvailabilityUnavailable("Controlled Gateway enrollment is not configured")
        return self._node_enroller(fleet, actor)

    def _require_supported(self) -> None:
        if not self._repository.model_availability_supported():
            raise ModelAvailabilityUnavailable("Model availability migration is not installed")

    def inventory(self) -> list[ModelAvailabilityReadback]:
        self._require_supported()
        return self._repository.list_model_availability()

    def read(self, model_id: UUID) -> ModelAvailabilityReadback:
        item = next((item for item in self.inventory() if item.model_id == model_id), None)
        if item is None:
            raise LookupError("Model availability identity is unknown")
        return item

    def change(
        self,
        model_id: UUID,
        command: ModelAvailabilityCommand,
        actor: str,
    ) -> ModelAvailabilityOperation:
        self._require_supported()
        return self._repository.change_model_availability(model_id, command, actor)

    def operation(self, operation_id: UUID) -> ModelAvailabilityOperation:
        self._require_supported()
        operation = self._repository.get_model_availability_operation(operation_id)
        if operation is None:
            raise LookupError("Model availability operation is unknown")
        return operation

    def retry(
        self,
        operation_id: UUID,
        expected_revision: int,
        actor: str,
    ) -> ModelAvailabilityOperation:
        self._require_supported()
        return self._repository.retry_model_availability(operation_id, expected_revision, actor)


class ModelAvailabilityWorker:
    def __init__(
        self,
        repository: ModelAvailabilityRepository,
        store_factory: Callable[[], TableStorageLedger],
        verify: Callable[
            [ModelAvailabilityState, dict[UUID, str]], Sequence[ModelAvailabilityTargetResult]
        ],
    ) -> None:
        self._repository = repository
        self._store_factory = store_factory
        self._verify = verify

    def run_once(self, worker_id: str, lease_seconds: int = 180) -> bool:
        owner = f"{worker_id}:{uuid4().hex}"
        task = self._repository.claim_model_availability_task(owner, lease_seconds)
        if task is None:
            return False
        model_id, revision = UUID(str(task["model_id"])), int(task["revision"])
        projected = None
        results: Sequence[ModelAvailabilityTargetResult] = ()
        error_code = None
        try:
            with WorkerLease(
                lambda: self._repository.renew_model_availability_lease(
                    model_id,
                    revision,
                    owner,
                    lease_seconds,
                ),
                lease_seconds,
            ) as lease:
                with self._store_factory() as store:
                    projected = project_model_availability(self._repository, store, model_id)
                if projected.revision != revision:
                    raise ModelAvailabilityUnavailable("Availability source revision changed")
                if not self._repository.mark_model_availability_projected(
                    model_id, revision, owner, projected,
                ):
                    raise ModelAvailabilityUnavailable("Availability projection lease changed")
                inventory = self._repository.get_model_availability_inventory(model_id)
                results = self._verify(projected, inventory)
                lease.heartbeat()
        except Exception:
            error_code = "model_availability_unavailable"
        self._repository.finish_model_availability_task(
            model_id,
            revision,
            owner,
            projected,
            results,
            error_code,
        )
        return True


class ModelAvailabilityCoordinator:
    def __init__(
        self,
        repository: QueryRepository,
        clients: ApimPublisherClientResolver,
        table_endpoint: str,
        table_name: str,
        *, fixed_program_verifier: Callable[
            [UUID, str, ModelAvailabilityState | None, Callable[[], None]],
            ModelAvailabilityCoverage
        ] | None = None,
        fixed_program_renewer: Callable[
            [UUID, str, ModelAvailabilityState | None, Callable[[], None]],
            ModelAvailabilityCoverage
        ] | None = None,
    ) -> None:
        self._repository = repository
        self._availability = cast(ModelAvailabilityRepository, repository)
        self._clients = clients
        self._table_endpoint, self._table_name = table_endpoint, table_name
        self._fixed_program_verifier = fixed_program_verifier
        self._fixed_program_renewer = fixed_program_renewer

    def _fixed_coverage(
        self, target_id: UUID, digest: str, *, renew: bool = False,
        projected: ModelAvailabilityState | None = None,
    ) -> ModelAvailabilityCoverage | None:
        publication = self._repository.effective_gateway_publication(target_id)
        gateways = self._repository.get_gateway_profiles([target_id]) if (
            isinstance(publication, Mapping)
            and "fixed_route_program" not in (publication.get("resource_manifest") or {})
        ) else []
        initialized = len(gateways) == 1 and gateways[0]["id"] == target_id and (
            gateways[0].get("config") or {}
        ).get("managed_route_program_initialization") is not None
        if not isinstance(publication, Mapping) or not isinstance(
            publication.get("resource_manifest"), Mapping,
        ) or ("fixed_route_program" not in publication["resource_manifest"] and not initialized):
            return None
        if (str(publication.get("gateway_profile_id")) != str(target_id)
                or self._fixed_program_verifier is None):
            raise ModelAvailabilityUnavailable("Fixed route availability verifier is unavailable")
        with ExitStack() as proofs:
            acquired = False

            def before_proof() -> None:
                nonlocal acquired
                if not acquired:
                    proofs.enter_context(self._availability.model_availability_proof_guard(
                        target_id, wait=not renew,
                    ))
                    acquired = True

            verifier = self._fixed_program_renewer if renew and self._fixed_program_renewer \
                else self._fixed_program_verifier
            coverage = verifier(target_id, digest, projected, before_proof)
            if not acquired:
                raise ModelAvailabilityUnavailable("Fixed route proof writer was not acquired")
            if (coverage.target_id != target_id or coverage.inventory_digest != digest
                    or not self._availability.save_model_availability_coverage(coverage)):
                raise ModelAvailabilityUnavailable("Fixed route availability inventory changed")
            return coverage

    def _verify_target(self, target_id: UUID, digest: str) -> ModelAvailabilityCoverage:
        fixed = self._fixed_coverage(target_id, digest)
        if fixed is not None:
            return fixed
        resolved = self._clients.resolve(target_id)
        if not isinstance(resolved.client, AzureApimPublisherClient):
            raise ModelAvailabilityUnavailable("Availability target verifier is unavailable")
        with ExitStack() as proofs:
            coverage = resolved.client.verify_model_availability(
                target_id,
                digest,
                self._repository.list_gateway_deployments(target_id, include_retired=True),
                self._table_endpoint,
                self._table_name,
                self._availability.get_model_availability_state,
                node_enrollment_reader=lambda: (
                    self._availability.list_model_availability_node_enrollments(target_id)
                ),
                state_batch_reader=self._availability.get_model_availability_states,
                before_proof=lambda: proofs.enter_context(
                    self._availability.model_availability_proof_guard(target_id, wait=True),
                ),
            )
            if not self._availability.save_model_availability_coverage(coverage):
                raise ModelAvailabilityUnavailable("Availability target inventory changed")
            return coverage

    def _renew_target(self, target_id: UUID, digest: str) -> ModelAvailabilityCoverage:
        fixed = self._fixed_coverage(target_id, digest, renew=True)
        if fixed is not None:
            return fixed
        resolved = self._clients.resolve(target_id)
        if not isinstance(resolved.client, AzureApimPublisherClient):
            raise ModelAvailabilityUnavailable("Availability target verifier is unavailable")
        with self._availability.model_availability_proof_guard(target_id, wait=False):
            coverage = resolved.client.renew_model_availability(
                target_id, digest,
                self._repository.list_gateway_deployments(target_id, include_retired=True),
                self._table_endpoint, self._table_name,
                self._availability.get_model_availability_states,
                node_enrollment_reader=partial(
                    self._availability.list_model_availability_node_enrollments, target_id,
                ),
            )
            if not self._availability.save_model_availability_coverage(coverage):
                raise ModelAvailabilityUnavailable("Availability target inventory changed")
            return coverage

    def refresh(self, *, renew: bool = False) -> int:
        inventory = self._availability.get_model_availability_inventory()
        now_mono = monotonic()
        with _TARGET_RENEW_BACKOFF_LOCK:
            for state in (_TARGET_AUDIT_BACKOFF_UNTIL, _TARGET_AUDIT_FAILING_SINCE):
                for key in list(state):
                    if inventory.get(key[0]) != key[1]:
                        state.pop(key, None)
            backoff = _TARGET_RENEW_BACKOFF_UNTIL if renew else _TARGET_AUDIT_BACKOFF_UNTIL
            items = [
                (target_id, digest)
                for target_id, digest in inventory.items()
                if backoff.get((target_id, digest), 0.0) <= now_mono
            ]
        if not items:
            return 0

        def refresh_target(entry: tuple[UUID, str]) -> Exception | None:
            target_id, digest = entry
            try:
                if renew:
                    self._renew_target(target_id, digest)
                else:
                    self._verify_target(target_id, digest)
            except Exception as error:
                return error
            return None

        with ThreadPoolExecutor(
            max_workers=min(_MAX_CONCURRENT_TARGETS, len(items)),
        ) as executor:
            completed = 0
            for (target_id, digest), error in zip(
                items, executor.map(refresh_target, items), strict=True,
            ):
                if error is None:
                    with _TARGET_RENEW_BACKOFF_LOCK:
                        _TARGET_RENEW_BACKOFF_UNTIL.pop((target_id, digest), None)
                        _TARGET_AUDIT_BACKOFF_UNTIL.pop((target_id, digest), None)
                        _TARGET_AUDIT_FAILING_SINCE.pop((target_id, digest), None)
                    completed += 1
                else:
                    with _TARGET_RENEW_BACKOFF_LOCK:
                        if (
                            isinstance(error, ModelAvailabilityUnavailable)
                            and str(error) == "Availability proof writer is busy"
                        ):
                            _TARGET_AUDIT_BACKOFF_UNTIL.pop((target_id, digest), None)
                            _TARGET_AUDIT_FAILING_SINCE.pop((target_id, digest), None)
                        else:
                            failed_at = monotonic()
                            if renew:
                                backoff[(target_id, digest)] = (
                                    failed_at + _TARGET_RENEW_BACKOFF_SECONDS
                                )
                            else:
                                failing_since = _TARGET_AUDIT_FAILING_SINCE.setdefault(
                                    (target_id, digest), failed_at,
                                )
                                if failed_at - failing_since >= _TARGET_RENEW_BACKOFF_SECONDS:
                                    backoff[(target_id, digest)] = (
                                        failed_at + _TARGET_RENEW_BACKOFF_SECONDS
                                    )
                    _log_verification_failure(target_id, error)
            return completed

    def verify(
        self,
        projected: ModelAvailabilityState,
        inventory: dict[UUID, str],
    ) -> Sequence[ModelAvailabilityTargetResult]:
        if not inventory:
            return []

        def confirm_target(entry: tuple[UUID, str]) -> ModelAvailabilityTargetResult:
            target_id, digest = entry
            try:
                fixed = self._fixed_coverage(target_id, digest, projected=projected)
                if fixed is not None:
                    return ModelAvailabilityTargetResult(
                        target_id=target_id, inventory_digest=digest, status="verified",
                        observed_revision=projected.revision, observed_enabled=projected.enabled,
                        policy_digest=fixed.policy_digest, verified_at=fixed.verified_at,
                    )
                coverage = self._availability.get_model_availability_coverage(target_id)
                if coverage is None or not coverage.current(datetime.now(UTC), digest):
                    raise ModelAvailabilityUnavailable("Availability coverage is stale")
                resolved = self._clients.resolve(target_id)
                if not isinstance(resolved.client, AzureApimPublisherClient):
                    raise ModelAvailabilityUnavailable("Availability verifier is unavailable")
                return resolved.client.confirm_model_availability(
                    target_id, projected, coverage,
                    self._repository.list_gateway_deployments(target_id, include_retired=True),
                    self._table_endpoint, self._table_name,
                    self._availability.get_model_availability_states,
                    node_enrollment_reader=partial(
                        self._availability.list_model_availability_node_enrollments, target_id,
                    ),
                )
            except Exception as error:
                _log_verification_failure(target_id, error)
                reason = (
                    "self_hosted_node_coverage_unverified"
                    if isinstance(error, ModelAvailabilityUnavailable)
                    and str(error) == "self_hosted_node_coverage_unverified"
                    else "model_availability_capability_unverified"
                )
                return ModelAvailabilityTargetResult(
                    target_id=target_id,
                    inventory_digest=digest,
                    status="unknown",
                    reason=reason,
                )

        with ThreadPoolExecutor(
            max_workers=min(_MAX_CONCURRENT_TARGETS, len(inventory)),
        ) as executor:
            return list(executor.map(confirm_target, inventory.items()))


def _projected_state(
    current: tuple[dict[str, object], str] | None,
    model_id: UUID,
) -> ModelAvailabilityState | None:
    if current is None:
        return None
    try:
        return ModelAvailabilityState.from_entity(current[0], model_id)
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ModelAvailabilityUnavailable("Model availability projection is invalid") from error


def project_model_availability(
    repository: AvailabilityStateReader,
    store: TableStorageLedger,
    model_id: UUID,
) -> ModelAvailabilityState:
    for _ in range(3):
        current = store.read_versioned_entity(AVAILABILITY_PARTITION, str(model_id))
        projected = _projected_state(current, model_id)
        desired = repository.get_model_availability_state(model_id)
        if desired is None or desired.model_id != model_id:
            raise ModelAvailabilityUnavailable("Model availability source is missing")
        if projected is not None and (
            projected.revision > desired.revision
            or projected.revision == desired.revision
            and projected != desired
        ):
            raise ModelAvailabilityUnavailable("Model availability revision cannot roll back")
        if not store.replace_if_match(
            AVAILABILITY_PARTITION,
            str(model_id),
            desired.entity(),
            current[1] if current is not None else None,
        ):
            continue
        observed = _projected_state(
            store.read_versioned_entity(AVAILABILITY_PARTITION, str(model_id)),
            model_id,
        )
        latest = repository.get_model_availability_state(model_id)
        if latest == desired and observed == desired:
            return desired
    raise ModelAvailabilityUnavailable("Model availability projection is pending")
