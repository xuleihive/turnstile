from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet

from backend.services.auth_service import hash_password
from scripts.apim_upgrade import (
    UPGRADE_VERSION,
    ApimUpgradeError,
    AzureUpgradeBackend,
    GatewaySnapshot,
    ImageUpgradePlan,
    document_digest,
    execute_image_upgrade,
    plan_image_upgrade,
    upgrade_parameters,
    validate_upgrade_what_if,
)
from scripts.stage_deployment import REPOSITORY_ROOT, stage_deployment, validate_source_snapshot

JsonObject = dict[str, Any]
PasswordReader = Callable[[str], str]
SECRET_PARAMETER_NAMES = {
    "postgresAdministratorPassword",
    "credentialEncryptionKey",
    "managementApiKey",
    "apimSubscriptionKey",
    "apimProbeSubscriptionKey",
    "bootstrapOwnerPasswordHash",
}
STATE_SECRET_NAMES = SECRET_PARAMETER_NAMES | {"observerAdapterSharedKey"}
EXPECTED_FUNCTIONS = {
    "telemetryFunctionName": {
        "telemetry_health",
        "process_usage_events",
        "reconcile_stream_usage",
        "sync_budget_ledger",
    },
    "controlPlaneFunctionName": {
        "publish_gateway_changes",
        "process_gateway_release_operations",
    },
}
FIXED_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
POSTGRES_VERSION = "16"
POSTGRES_AVAILABILITY_ZONE = "1"
DEFAULT_POSTGRES_SKU_NAME = "Standard_B1ms"
DEFAULT_POSTGRES_TIER = "Burstable"
DEFAULT_OBSERVER_PLAN_SKU_NAME = "P0v3"
DEFAULT_OBSERVER_PLAN_WORKER_COUNT = 1
OBSERVER_PLAN_SKU_NAMES = frozenset({"P0v3", "P1v3", "P2v3", "P3v3"})
OBSERVER_ORCHESTRATOR_PARAMETERS = {
    "observerPlanSkuName",
    "observerPlanWorkerCount",
}


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentInputs:
    subscription: str
    parameters_path: Path
    parameters: JsonObject
    resource_group_name: str
    resource_prefix: str
    location: str
    postgres_location: str
    postgres_sku_name: str
    postgres_tier: str
    observer_plan_sku_name: str
    observer_plan_worker_count: int
    owner_email: str
    state_path: Path

    @classmethod
    def load(
        cls,
        subscription: str,
        parameters_path: Path,
        state_path: Path | None = None,
    ) -> DeploymentInputs:
        try:
            document = json.loads(parameters_path.read_text(encoding="utf-8"))
            raw_parameters = document["parameters"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise DeploymentError(f"Invalid ARM parameter file: {parameters_path}") from error
        if not isinstance(raw_parameters, dict):
            raise DeploymentError("The ARM parameter file must contain a parameters object")
        parameters: JsonObject = {}
        for name, entry in raw_parameters.items():
            if not isinstance(entry, dict) or "value" not in entry:
                raise DeploymentError(f"Parameter {name} must contain a value")
            parameters[str(name)] = entry["value"]
        leaked = sorted(name for name in SECRET_PARAMETER_NAMES if name in parameters)
        if leaked:
            raise DeploymentError(
                "Keep secure parameters out of the public parameter file: " + ", ".join(leaked)
            )
        resource_prefix = _required_string(parameters, "resourcePrefix")
        location = _required_string(parameters, "location")
        postgres_location = _string_parameter(parameters, "postgresLocation", location)
        postgres_sku_name = _string_parameter(
            parameters, "postgresSkuName", DEFAULT_POSTGRES_SKU_NAME
        )
        postgres_tier = _string_parameter(
            parameters, "postgresTier", DEFAULT_POSTGRES_TIER
        )
        observer_plan_sku_name = _string_parameter(
            parameters, "observerPlanSkuName", DEFAULT_OBSERVER_PLAN_SKU_NAME
        )
        if observer_plan_sku_name not in OBSERVER_PLAN_SKU_NAMES:
            raise DeploymentError(
                "Parameter observerPlanSkuName must be one of: "
                + ", ".join(sorted(OBSERVER_PLAN_SKU_NAMES))
            )
        observer_plan_worker_count = _integer_parameter(
            parameters,
            "observerPlanWorkerCount",
            DEFAULT_OBSERVER_PLAN_WORKER_COUNT,
            minimum=1,
            maximum=30,
        )
        owner_email = _required_string(parameters, "bootstrapOwnerEmail").strip().lower()
        if "@" not in owner_email:
            raise DeploymentError("bootstrapOwnerEmail must be an email address")
        resource_group_name = str(
            parameters.get("resourceGroupName") or f"{resource_prefix}-platform"
        )
        resolved_state = state_path or (
            REPOSITORY_ROOT / ".turnstile" / "deployments" / f"{resource_group_name}.json"
        )
        return cls(
            subscription=subscription,
            parameters_path=parameters_path,
            parameters=parameters,
            resource_group_name=resource_group_name,
            resource_prefix=resource_prefix,
            location=location,
            postgres_location=postgres_location,
            postgres_sku_name=postgres_sku_name,
            postgres_tier=postgres_tier,
            observer_plan_sku_name=observer_plan_sku_name,
            observer_plan_worker_count=observer_plan_worker_count,
            owner_email=owner_email,
            state_path=resolved_state,
        )


@dataclass(frozen=True)
class SecretMaterial:
    values: dict[str, str]
    owner_password: str | None


@dataclass(frozen=True)
class ExistingCore:
    apim_name: str
    apim_resource_group_name: str
    apim_principal_id: str
    apim_gateway_url: str

    @classmethod
    def from_outputs(cls, outputs: Mapping[str, Any]) -> ExistingCore:
        apim_resource_group_name = outputs.get("apimResourceGroupName")
        if not isinstance(apim_resource_group_name, str) or not apim_resource_group_name:
            apim_resource_group_name = _output_string(outputs, "resourceGroupName")
        return cls(
            apim_name=_output_string(outputs, "apimName"),
            apim_resource_group_name=apim_resource_group_name,
            apim_principal_id=_output_string(outputs, "apimPrincipalId"),
            apim_gateway_url=_output_string(outputs, "apimGatewayUrl"),
        )

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any]) -> ExistingCore | None:
        fields = {
            "existingApimName": "apim_name",
            "existingApimResourceGroupName": "apim_resource_group_name",
            "existingApimPrincipalId": "apim_principal_id",
            "existingApimGatewayUrl": "apim_gateway_url",
        }
        configured = {
            field: str(parameters.get(parameter) or "").strip()
            for parameter, field in fields.items()
        }
        if not any(configured.values()):
            return None
        missing = [
            parameter
            for parameter, field in fields.items()
            if not configured[field]
        ]
        if missing:
            raise DeploymentError(
                "Existing APIM adoption requires: " + ", ".join(sorted(missing))
            )
        return cls(**configured)


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        print("$ " + " ".join(command))
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=True,
            capture_output=capture,
            text=True,
        )

    def run_json(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> JsonObject:
        result = self.run(command, cwd=cwd, env=env, capture=True)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DeploymentError(f"Command did not return JSON: {' '.join(command)}") from error
        if not isinstance(value, dict):
            raise DeploymentError(f"Command returned a non-object JSON value: {' '.join(command)}")
        return value


def _required_string(parameters: Mapping[str, Any], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DeploymentError(f"Parameter {name} is required")
    return value.strip()


def _string_parameter(parameters: Mapping[str, Any], name: str, default: str) -> str:
    value = parameters.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise DeploymentError(f"Parameter {name} must be a non-empty string")
    return value.strip()


def _integer_parameter(
    parameters: Mapping[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = parameters.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeploymentError(f"Parameter {name} must be an integer")
    if value < minimum or value > maximum:
        raise DeploymentError(
            f"Parameter {name} must be between {minimum} and {maximum}"
        )
    return value


def _read_owner_password(read_password: PasswordReader) -> str:
    password = read_password("Initial Owner password: ")
    if password != read_password("Repeat Initial Owner password: "):
        raise DeploymentError("Initial Owner passwords do not match")
    if len(password) < 12:
        raise DeploymentError("Initial Owner password must be at least 12 characters")
    return password


def owner_credentials_password(path: Path, expected_email: str) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise DeploymentError(f"Owner credentials permissions must be 0600: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentError(f"Invalid Owner credentials file: {path}") from error
    if not isinstance(document, dict) or set(document) != {"email", "password"}:
        raise DeploymentError("Owner credentials must contain only email and password")
    email = document.get("email")
    password = document.get("password")
    if not isinstance(email, str) or email.strip().lower() != expected_email:
        raise DeploymentError("Owner credentials email does not match bootstrapOwnerEmail")
    if not isinstance(password, str) or len(password) < 12:
        raise DeploymentError("Initial Owner password must be at least 12 characters")
    return password


def load_or_create_secret_material(
    inputs: DeploymentInputs,
    *,
    read_password: PasswordReader = getpass.getpass,
    require_owner_password: bool,
) -> SecretMaterial:
    state_path = inputs.state_path
    owner_password: str | None = None
    if state_path.exists():
        mode = stat.S_IMODE(state_path.stat().st_mode)
        if mode & 0o077:
            raise DeploymentError(f"Secret state permissions must be 0600: {state_path}")
        try:
            document = json.loads(state_path.read_text(encoding="utf-8"))
            values = document["parameters"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise DeploymentError(f"Invalid secret state: {state_path}") from error
        if not isinstance(values, dict) or any(
            not isinstance(values.get(name), str) or not values[name]
            for name in STATE_SECRET_NAMES
        ):
            raise DeploymentError(f"Secret state is incomplete: {state_path}")
        if require_owner_password:
            owner_password = read_password("Initial Owner password for verification: ")
        return SecretMaterial(dict(values), owner_password)

    owner_password = _read_owner_password(read_password)
    values = {
        "postgresAdministratorPassword": secrets.token_urlsafe(32),
        "credentialEncryptionKey": Fernet.generate_key().decode("ascii"),
        "managementApiKey": secrets.token_urlsafe(32),
        "apimSubscriptionKey": secrets.token_hex(32),
        "apimProbeSubscriptionKey": secrets.token_hex(32),
        "bootstrapOwnerPasswordHash": hash_password(owner_password),
        "observerAdapterSharedKey": secrets.token_urlsafe(32),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "parameters": values}, handle, indent=2)
        handle.write("\n")
    print(f"Created secret state: {state_path}")
    return SecretMaterial(values, owner_password if require_owner_password else None)


def deployment_parameters(
    inputs: DeploymentInputs,
    secrets_: SecretMaterial,
    *,
    observer: Mapping[str, str] | None = None,
    existing_core: ExistingCore | None = None,
    resume_existing_environment: bool = False,
) -> JsonObject:
    values = dict(inputs.parameters)
    for name in OBSERVER_ORCHESTRATOR_PARAMETERS:
        values.pop(name, None)
    if existing_core is not None and not resume_existing_environment:
        isolated_apim_defaults = {
            "apimApiId": f"{inputs.resource_prefix}-llm",
            "gatewayApiRelativePath": f"{inputs.resource_prefix}/llm",
            "apimProductId": f"{inputs.resource_prefix}-ai-consumers",
            "apimDashboardSubscriptionId": f"{inputs.resource_prefix}-dashboard",
            "apimProbeSubscriptionId": f"{inputs.resource_prefix}-publisher-probe",
            "apimAppInsightsLoggerId": f"{inputs.resource_prefix}-appinsights",
            "apimEventHubLoggerId": f"{inputs.resource_prefix}-eventhub",
            "apimDiagnosticSettingName": f"{inputs.resource_prefix}-gateway-logs",
            "observerAdapterKeyNamedValueName": f"{inputs.resource_prefix}-observer-key",
        }
        for name, default in isolated_apim_defaults.items():
            if not str(values.get(name) or "").strip():
                values[name] = default
    values.update({name: secrets_.values[name] for name in SECRET_PARAMETER_NAMES})
    values.update(
        bootstrapOwnerEmail=inputs.owner_email,
        provisionControlPlane=True,
        gatewayApplicationKeyManagementEnabled=True,
        controlPlaneEnabled=observer is not None,
        gatewayReleaseWorkerEnabled=observer is not None,
        provisionApimService=existing_core is None,
        provisionPostgres=not resume_existing_environment,
        deployApimBootstrap=not resume_existing_environment,
        existingApimName=existing_core.apim_name if existing_core else "",
        existingApimResourceGroupName=(
            existing_core.apim_resource_group_name if existing_core else ""
        ),
        existingApimPrincipalId=(
            existing_core.apim_principal_id if existing_core else ""
        ),
        existingApimGatewayUrl=existing_core.apim_gateway_url if existing_core else "",
    )
    if observer is None:
        values["apimUsageObserver"] = {
            "mode": "disabled",
            "url": "",
            "keyNamedValue": "",
            "legacyFoundryUpstreamHost": "",
            "legacyFoundryUpstreamBasePath": "",
            "legacyDatabricksUpstreamHost": "",
            "legacyDatabricksUpstreamBasePath": "",
        }
    else:
        values["apimUsageObserver"] = {
            "mode": "enabled",
            "url": observer["webAppUrl"],
            "keyNamedValue": observer["adapterKeyNamedValueName"],
            "legacyFoundryUpstreamHost": "unused.invalid",
            "legacyFoundryUpstreamBasePath": "/",
            "legacyDatabricksUpstreamHost": "unused.invalid",
            "legacyDatabricksUpstreamBasePath": "/",
        }
    return _arm_parameter_document(values)


def _arm_parameter_document(values: Mapping[str, Any]) -> JsonObject:
    return {
        "$schema": (
            "https://schema.management.azure.com/schemas/"
            "2019-04-01/deploymentParameters.json#"
        ),
        "contentVersion": "1.0.0.0",
        "parameters": {name: {"value": value} for name, value in values.items()},
    }


@contextmanager
def temporary_parameter_file(document: Mapping[str, Any], directory: Path) -> Iterator[Path]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_path = tempfile.mkstemp(prefix="parameters-", suffix=".json", dir=directory)
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
            handle.write("\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def require_prerequisites(runner: CommandRunner, subscription: str) -> None:
    missing = [name for name in ("az", "git", "uv", "npm") if shutil.which(name) is None]
    if missing:
        raise DeploymentError("Missing required tools: " + ", ".join(missing))
    runner.run_json(
        ["az", "account", "show", "--subscription", subscription, "--output", "json"]
    )
    runner.run(["az", "bicep", "version"], capture=True)


def validate_flex_consumption_capabilities(
    runner: CommandRunner, inputs: DeploymentInputs
) -> None:
    provider = runner.run(
        [
            "az",
            "provider",
            "show",
            "--subscription",
            inputs.subscription,
            "--namespace",
            "Microsoft.App",
            "--query",
            "registrationState",
            "--output",
            "tsv",
        ],
        capture=True,
    )
    if provider.stdout.strip() != "Registered":
        raise DeploymentError(
            "Microsoft.App must be registered before deploying Flex Consumption apps"
        )
    locations = runner.run(
        [
            "az",
            "functionapp",
            "list-flexconsumption-locations",
            "--subscription",
            inputs.subscription,
            "--query",
            "[].name",
            "--output",
            "tsv",
        ],
        capture=True,
    )
    available = {location.strip().lower() for location in locations.stdout.splitlines()}
    if inputs.location.lower() not in available:
        raise DeploymentError(
            f"Flex Consumption is unavailable in {inputs.location} for this subscription"
        )


def validate_postgres_capabilities(
    runner: CommandRunner, inputs: DeploymentInputs
) -> None:
    if inputs.parameters.get("provisionPostgres", True) is False:
        return
    capabilities = runner.run_json(
        [
            "az",
            "postgres",
            "flexible-server",
            "list-skus",
            "--subscription",
            inputs.subscription,
            "--location",
            inputs.postgres_location,
            "--query",
            (
                "[0].{reason:reason,versions:supportedServerVersions[].name,"
                "editions:supportedServerEditions[].{name:name,"
                "skus:supportedServerSkus[].{name:name,zones:supportedZones}}}"
            ),
            "--output",
            "json",
        ]
    )
    versions = capabilities.get("versions")
    if not isinstance(versions, list) or POSTGRES_VERSION not in versions:
        reason = capabilities.get("reason")
        detail = f": {reason}" if isinstance(reason, str) and reason else ""
        raise DeploymentError(
            f"PostgreSQL {POSTGRES_VERSION} is unavailable in "
            f"{inputs.postgres_location}{detail}"
        )

    combination_supported = False
    editions = capabilities.get("editions")
    if isinstance(editions, list):
        for edition in editions:
            if not isinstance(edition, dict) or edition.get("name") != inputs.postgres_tier:
                continue
            skus = edition.get("skus")
            if not isinstance(skus, list):
                continue
            for sku in skus:
                if not isinstance(sku, dict) or sku.get("name") != inputs.postgres_sku_name:
                    continue
                zones = sku.get("zones")
                combination_supported = (
                    isinstance(zones, list) and POSTGRES_AVAILABILITY_ZONE in zones
                )
                if combination_supported:
                    break
    if not combination_supported:
        raise DeploymentError(
            f"PostgreSQL {inputs.postgres_tier}/{inputs.postgres_sku_name} in zone "
            f"{POSTGRES_AVAILABILITY_ZONE} is unavailable in {inputs.postgres_location}"
        )


def _deployment_command(
    action: str,
    inputs: DeploymentInputs,
    template: Path,
    parameter_file: Path,
    deployment_name: str,
) -> list[str]:
    command = [
        "az",
        "deployment",
        "sub",
        action,
        "--subscription",
        inputs.subscription,
        "--name",
        deployment_name,
        "--location",
        inputs.location,
        "--template-file",
        str(template),
        "--parameters",
        f"@{parameter_file}",
        "--output",
        "json",
    ]
    if action == "what-if":
        command.extend(["--no-pretty-print", "--result-format", "FullResourcePayloads"])
    return command


def _resource_group_deployment_command(
    action: str,
    inputs: DeploymentInputs,
    template: Path,
    parameter_file: Path,
    deployment_name: str,
) -> list[str]:
    command = [
        "az",
        "deployment",
        "group",
        action,
        "--subscription",
        inputs.subscription,
        "--resource-group",
        inputs.resource_group_name,
        "--name",
        deployment_name,
        "--template-file",
        str(template),
        "--parameters",
        f"@{parameter_file}",
        "--output",
        "json",
    ]
    if action == "what-if":
        command.extend(["--no-pretty-print", "--result-format", "FullResourcePayloads"])
    return command


def _what_if_counts(result: Mapping[str, Any]) -> Counter[str]:
    changes = result.get("changes")
    if not isinstance(changes, list):
        properties = result.get("properties")
        changes = properties.get("changes", []) if isinstance(properties, dict) else []
    if not isinstance(changes, list):
        raise DeploymentError("Azure what-if returned an invalid changes value")
    counts = Counter(
        str(change.get("changeType", "Unknown"))
        for change in changes
        if isinstance(change, dict)
    )
    print("What-if: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    if counts["Delete"]:
        raise DeploymentError("What-if contains Delete changes; deployment stopped")
    return counts


def what_if(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    template: Path,
    parameters: Mapping[str, Any],
    deployment_name: str,
) -> Counter[str]:
    with temporary_parameter_file(parameters, inputs.state_path.parent) as parameter_file:
        result = runner.run_json(
            _deployment_command(
                "what-if", inputs, template, parameter_file, deployment_name
            )
        )
    return _what_if_counts(result)


def what_if_resource_group(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    template: Path,
    parameters: Mapping[str, Any],
    deployment_name: str,
) -> Counter[str]:
    with temporary_parameter_file(parameters, inputs.state_path.parent) as parameter_file:
        result = runner.run_json(
            _resource_group_deployment_command(
                "what-if", inputs, template, parameter_file, deployment_name
            )
        )
    return _what_if_counts(result)


def deploy_template(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    template: Path,
    parameters: Mapping[str, Any],
    deployment_name: str,
) -> JsonObject:
    with temporary_parameter_file(parameters, inputs.state_path.parent) as parameter_file:
        return runner.run_json(
            _deployment_command("create", inputs, template, parameter_file, deployment_name)
        )


def deploy_resource_group_template(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    template: Path,
    parameters: Mapping[str, Any],
    deployment_name: str,
) -> JsonObject:
    with temporary_parameter_file(parameters, inputs.state_path.parent) as parameter_file:
        return runner.run_json(
            _resource_group_deployment_command(
                "create", inputs, template, parameter_file, deployment_name
            )
        )


def deployment_outputs(result: Mapping[str, Any]) -> dict[str, Any]:
    properties = result.get("properties")
    raw_outputs = properties.get("outputs") if isinstance(properties, dict) else None
    if not isinstance(raw_outputs, dict):
        raise DeploymentError("Azure deployment returned no outputs")
    outputs: dict[str, Any] = {}
    for name, entry in raw_outputs.items():
        if isinstance(entry, dict) and "value" in entry:
            outputs[str(name)] = entry["value"]
    return outputs


def source_version(runner: CommandRunner) -> str:
    result = runner.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture=True,
    )
    return result.stdout.strip()


def observer_source_version(runner: CommandRunner) -> str:
    result = runner.run(
        ["git", "rev-parse", "--short=12", "HEAD:infra/envoy-cache-adapter"],
        cwd=REPOSITORY_ROOT,
        capture=True,
    )
    return result.stdout.strip()


def deterministic_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            source_mode = path.stat().st_mode
            archive_permissions = 0o755 if source_mode & 0o111 else 0o644
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | archive_permissions) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def linux_dependency_command(staged: Path) -> list[str]:
    target = staged / ".python_packages" / "lib" / "site-packages"
    target.mkdir(parents=True)
    return [
        "uv",
        "pip",
        "install",
        "--python-platform",
        "x86_64-manylinux_2_28",
        "--python-version",
        "3.11",
        "--target",
        str(target),
        "--requirements",
        str(staged / "requirements.txt"),
        "--constraint",
        str(staged / "constraints.txt"),
    ]


def pip_linux_dependency_command(staged: Path, pip: str) -> list[str]:
    target = staged / ".python_packages" / "lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    return [
        pip,
        "install",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--platform",
        "manylinux_2_28_x86_64",
        "--platform",
        "manylinux_2_17_x86_64",
        "--platform",
        "manylinux2014_x86_64",
        "--implementation",
        "cp",
        "--python-version",
        "3.11",
        "--target",
        str(target),
        "--requirement",
        str(staged / "requirements.txt"),
        "--constraint",
        str(staged / "constraints.txt"),
    ]


def validate_packaged_dependencies(staged: Path) -> None:
    lock_bytes = (REPOSITORY_ROOT / "uv.lock").read_bytes()
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    allowed: dict[str, set[str]] = {}
    for package in lock["package"]:
        name = re.sub(r"[-_.]+", "-", package["name"]).lower()
        allowed.setdefault(name, set()).add(package["version"])
    installed: dict[str, str] = {}
    target = staged / ".python_packages" / "lib" / "site-packages"
    for distribution in distributions(path=[str(target)]):
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"] or "").lower()
        version = distribution.version
        if name in installed or version not in allowed.get(name, set()):
            raise DeploymentError(f"Packaged dependency does not match uv.lock: {name}=={version}")
        installed[name] = version
    if not installed:
        raise DeploymentError("The deployment package contains no installed dependencies")
    (staged / "dependency-manifest.json").write_text(
        json.dumps(
            {
                "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
                "packages": dict(sorted(installed.items())),
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def _install_linux_dependencies(runner: CommandRunner, staged: Path) -> None:
    runner.run(
        [
            "uv", "export", "--locked", "--no-dev", "--no-emit-project", "--no-hashes",
            "--format", "requirements-txt", "--output-file", str(staged / "constraints.txt"),
        ],
        cwd=REPOSITORY_ROOT,
        capture=True,
    )
    try:
        runner.run(linux_dependency_command(staged), cwd=REPOSITORY_ROOT)
    except subprocess.CalledProcessError:
        pip = shutil.which("pip3")
        if pip is None:
            raise DeploymentError(
                "uv could not install Linux dependencies and pip3 is unavailable for fallback"
            ) from None
        print("uv dependency installation failed; retrying with pip.")
        runner.run(
            pip_linux_dependency_command(staged, pip),
            cwd=REPOSITORY_ROOT,
        )
    validate_packaged_dependencies(staged)


def build_packages(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    version: str,
) -> dict[str, Path]:
    environment = dict(os.environ)
    environment["VITE_ENTRA_CLIENT_ID"] = str(inputs.parameters.get("entraClientId") or "")
    runner.run(["npm", "--prefix", "frontend", "ci"], cwd=REPOSITORY_ROOT)
    runner.run(
        ["npm", "--prefix", "frontend", "run", "build"],
        cwd=REPOSITORY_ROOT,
        env=environment,
    )
    build_root = inputs.state_path.parent / "build" / version
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    packages: dict[str, Path] = {}
    for target in ("api", "telemetry", "control-plane"):
        staged = build_root / target
        stage_deployment(target, staged)
        _install_linux_dependencies(runner, staged)
        archive = build_root / f"{target}.zip"
        deterministic_zip(staged, archive)
        packages[target] = archive
    return packages


def deploy_webapp_package(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    app_name: str,
    package: Path,
) -> None:
    token_result = runner.run_json(
        [
            "az",
            "account",
            "get-access-token",
            "--subscription",
            inputs.subscription,
            "--resource",
            "https://management.azure.com/",
            "--query",
            "{accessToken:accessToken}",
            "--output",
            "json",
        ]
    )
    access_token = _output_string(token_result, "accessToken")
    publish_url = (
        f"https://{app_name}.scm.azurewebsites.net/api/publish"
        "?type=zip&clean=true&restart=true"
    )
    try:
        payload = package.read_bytes()
        request = urllib.request.Request(
            publish_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/zip",
            },
            method="POST",
        )
        print(f"$ POST {publish_url} (Microsoft Entra authentication)")
        _open_without_proxy(request, 1800)
    except (OSError, urllib.error.URLError) as error:
        raise DeploymentError(
            f"Microsoft Entra package deployment failed for {app_name}: {error}"
        ) from error


def deploy_packages(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Any],
    packages: Mapping[str, Path],
) -> None:
    resource_group = _output_string(outputs, "resourceGroupName")
    api_name = _output_string(outputs, "apiName")
    deploy_webapp_package(runner, inputs, api_name, packages["api"])
    wait_for_health(_output_string(outputs, "apiUrl"), timeout_seconds=1800)
    for output_name, package_name in (
        ("telemetryFunctionName", "telemetry"),
        ("controlPlaneFunctionName", "control-plane"),
    ):
        function_name = _output_string(outputs, output_name)
        deploy_function_package(
            runner,
            inputs,
            resource_group,
            function_name,
            packages[package_name],
        )


def deploy_function_package(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    resource_group: str,
    function_name: str,
    package: Path,
) -> None:
    for attempt in range(1, 4):
        try:
            deploy_webapp_package(runner, inputs, function_name, package)
            return
        except DeploymentError:
            if attempt == 3:
                raise
            print(
                f"Function package deployment failed for {function_name}; "
                f"restarting before retry {attempt + 1}/3."
            )
            runner.run(
                [
                    "az",
                    "functionapp",
                    "restart",
                    "--subscription",
                    inputs.subscription,
                    "--resource-group",
                    resource_group,
                    "--name",
                    function_name,
                ]
            )
            time.sleep(15)


def restart_runtime_apps(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Any],
) -> None:
    resource_group = _output_string(outputs, "resourceGroupName")
    api_name = _output_string(outputs, "apiName")
    for action in ("stop", "start"):
        runner.run(
            [
                "az",
                "webapp",
                action,
                "--subscription",
                inputs.subscription,
                "--resource-group",
                resource_group,
                "--name",
                api_name,
            ]
        )
    runner.run(
        [
            "az",
            "functionapp",
            "restart",
            "--subscription",
            inputs.subscription,
            "--resource-group",
            resource_group,
            "--name",
            _output_string(outputs, "controlPlaneFunctionName"),
        ]
    )


def _output_string(outputs: Mapping[str, Any], name: str) -> str:
    value = outputs.get(name)
    if not isinstance(value, str) or not value:
        raise DeploymentError(f"Deployment output {name} is missing")
    return value


def observer_names(inputs: DeploymentInputs) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"{inputs.subscription}:{inputs.resource_group_name}".encode()
    ).hexdigest()[:12]
    compact_prefix = re.sub(r"[^a-z0-9]", "", inputs.resource_prefix.lower())
    return f"cr{compact_prefix}{digest}"[:50], f"obs-{compact_prefix}-{digest}"[:60]


def observer_plan_name(inputs: DeploymentInputs) -> str:
    digest = hashlib.sha256(
        f"{inputs.subscription}:{inputs.resource_group_name}".encode()
    ).hexdigest()[:12]
    return f"plan-obs-{digest}"


def observer_parameters(
    inputs: DeploymentInputs,
    platform_outputs: Mapping[str, Any],
    secrets_: SecretMaterial,
    version: str,
    existing_observer: Mapping[str, Any] | None = None,
) -> JsonObject:
    if existing_observer is None:
        acr_name, web_app_name = observer_names(inputs)
    else:
        acr_name = _output_string(existing_observer, "acrName")
        web_app_name = _output_string(existing_observer, "webAppName")
    apim_resource_group_name = platform_outputs.get("apimResourceGroupName")
    if not isinstance(apim_resource_group_name, str) or not apim_resource_group_name:
        apim_resource_group_name = _output_string(platform_outputs, "resourceGroupName")
    if existing_observer is None:
        acr_resource_group_name = _output_string(platform_outputs, "resourceGroupName")
    elif "acrResourceGroupName" in existing_observer:
        acr_resource_group_name = _output_string(existing_observer, "acrResourceGroupName")
    else:
        acr_resource_group_name = apim_resource_group_name
    adapter_key_named_value_name = platform_outputs.get(
        "observerAdapterKeyNamedValueName"
    )
    if not isinstance(adapter_key_named_value_name, str) or not adapter_key_named_value_name:
        adapter_key_named_value_name = "turnstile-envoy-adapter-key"
    return _arm_parameter_document(
        {
            "resourceGroupName": _output_string(platform_outputs, "resourceGroupName"),
            "apimResourceGroupName": apim_resource_group_name,
            "location": inputs.location,
            "appServicePlanName": observer_plan_name(inputs),
            "appServicePlanSkuName": inputs.observer_plan_sku_name,
            "appServicePlanWorkerCount": inputs.observer_plan_worker_count,
            "webAppName": web_app_name,
            "acrName": acr_name,
            "acrResourceGroupName": acr_resource_group_name,
            "provisionAcr": existing_observer is None,
            "imageTag": version,
            "eventHubNamespaceName": _output_string(
                platform_outputs, "eventHubNamespaceName"
            ),
            "eventHubName": "token-usage",
            "apimName": _output_string(platform_outputs, "apimName"),
            "adapterKeyNamedValueName": adapter_key_named_value_name,
            "adapterSharedKey": secrets_.values["observerAdapterSharedKey"],
        }
    )


def _resolved_base_parameter(base_parameters: Mapping[str, Any], name: str, default: Any) -> Any:
    """Read a value out of the document already built for `main.bicep`.

    Reading the resolved document rather than `inputs.parameters` is deliberate: it is the
    same object the base template receives, so a value written by the runtime release cannot
    drift from the one the base template would have written for the same run.
    """
    entry = base_parameters.get("parameters")
    if not isinstance(entry, Mapping):
        return default
    value = entry.get(name)
    if not isinstance(value, Mapping) or "value" not in value:
        return default
    return value["value"]


def runtime_release_parameters(
    secrets_: SecretMaterial,
    platform_outputs: Mapping[str, Any],
    observer_outputs: Mapping[str, Any],
    current_api_settings: Mapping[str, str],
    current_control_plane_settings: Mapping[str, str],
    base_parameters: Mapping[str, Any] | None = None,
) -> JsonObject:
    base = base_parameters or {}
    # `main.bicep` gates both of these on `provisionControlPlane`; mirroring that here keeps the
    # value identical to the one `data-plane.bicep` would receive.
    control_plane = bool(_resolved_base_parameter(base, "provisionControlPlane", False))
    return _arm_parameter_document(
        {
            "apiName": _output_string(platform_outputs, "apiName"),
            "controlPlaneFunctionName": _output_string(
                platform_outputs, "controlPlaneFunctionName"
            ),
            "apimGatewayUrl": _output_string(platform_outputs, "gatewayApiPath"),
            "apimSubscriptionKey": secrets_.values["apimSubscriptionKey"],
            "usageObserverUrl": _output_string(observer_outputs, "webAppUrl"),
            "usageObserverKeyNamedValue": _output_string(
                observer_outputs, "adapterKeyNamedValueName"
            ),
            "publicationWorkerEnabled": True,
            "releaseWorkerEnabled": True,
            "entraClientId": str(_resolved_base_parameter(base, "entraClientId", "") or ""),
            "entraAllowedEmailDomains": list(
                _resolved_base_parameter(base, "entraAllowedEmailDomains", []) or []
            ),
            "applicationKeyManagementEnabled": bool(
                _resolved_base_parameter(base, "gatewayApplicationKeyManagementEnabled", False)
            ),
            "applicationProvisioningEnabled": control_plane
            and bool(
                _resolved_base_parameter(base, "gatewayApplicationProvisioningEnabled", False)
            ),
            "applicationDefaultMonthlyTokenLimit": int(
                _resolved_base_parameter(base, "gatewayApplicationDefaultMonthlyTokenLimit", 100000)
            ),
            "applicationDefaultTokensPerMinute": int(
                _resolved_base_parameter(base, "gatewayApplicationDefaultTokensPerMinute", 100000)
            ),
            "databricksOAuthEnabled": control_plane
            and bool(_resolved_base_parameter(base, "databricksOAuthEnabled", False)),
            "currentApiSettings": dict(current_api_settings),
            "currentControlPlaneSettings": dict(current_control_plane_settings),
        }
    )


def current_app_settings(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    app_name: str,
) -> dict[str, str]:
    resource_id = (
        f"/subscriptions/{inputs.subscription}/resourceGroups/{inputs.resource_group_name}"
        f"/providers/Microsoft.Web/sites/{app_name}/config/appsettings/list"
    )
    result = runner.run_json(
        [
            "az",
            "rest",
            "--method",
            "post",
            "--url",
            f"https://management.azure.com{resource_id}?api-version=2024-11-01",
            "--output",
            "json",
        ]
    )
    properties = result.get("properties")
    if not isinstance(properties, dict) or any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in properties.items()
    ):
        raise DeploymentError(f"App Service returned invalid settings for {app_name}")
    return dict(properties)


def build_and_start_observer(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    observer_outputs: Mapping[str, Any],
    version: str,
) -> None:
    acr_name = _output_string(observer_outputs, "acrName")
    image = _output_string(observer_outputs, "image")
    image_repository_and_tag = image.split("/", maxsplit=1)[1]
    try:
        runner.run(
            [
                "az",
                "acr",
                "repository",
                "show",
                "--subscription",
                inputs.subscription,
                "--name",
                acr_name,
                "--image",
                image_repository_and_tag,
                "--output",
                "none",
            ],
            capture=True,
        )
    except subprocess.CalledProcessError:
        pass
    else:
        print(f"Observer image already exists: {image_repository_and_tag} ({version})")
        return
    runner.run(
        [
            "az",
            "acr",
            "build",
            "--subscription",
            inputs.subscription,
            "--registry",
            acr_name,
            "--image",
            image_repository_and_tag,
            "--file",
            "Dockerfile",
            ".",
        ],
        cwd=REPOSITORY_ROOT / "infra" / "envoy-cache-adapter",
    )
    web_app_name = _output_string(observer_outputs, "webAppName")
    runner.run(
        [
            "az",
            "webapp",
            "restart",
            "--subscription",
            inputs.subscription,
            "--resource-group",
            inputs.resource_group_name,
            "--name",
            web_app_name,
        ]
    )
    print(f"Observer image ready: {image_repository_and_tag} ({version})")


def _open_without_proxy(request: urllib.request.Request, timeout: float) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        body = response.read()
    if not isinstance(body, bytes):
        raise DeploymentError("HTTP response body was not bytes")
    return body


def wait_for_health(api_url: str, timeout_seconds: int = 180) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            body = _open_without_proxy(
                urllib.request.Request(f"{api_url.rstrip('/')}/health"), 15
            ).decode("utf-8")
            return body
        except (OSError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(3)
    raise DeploymentError(f"API health check did not recover: {last_error}")


def wait_for_observer_health(observer_url: str) -> str:
    return wait_for_health(observer_url, timeout_seconds=1800)


def verify_owner_login(
    api_url: str, email: str, password: str, timeout_seconds: int = 180
) -> None:
    payload = json.dumps({"email": email, "password": password}).encode()
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"{api_url.rstrip('/')}/api/v1/auth/login",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = json.loads(_open_without_proxy(request, 30))
            if (
                isinstance(response, dict)
                and response.get("email") == email
                and response.get("role") == "owner"
                and response.get("method") == "password"
            ):
                print(f"Initial Owner verified: {email} (role=owner, method=password)")
                return
            last_error = "unexpected identity response"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(3)
    raise DeploymentError(f"Initial Owner password login failed: {last_error}")


def frontend_asset(index_path: Path = REPOSITORY_ROOT / "frontend" / "dist" / "index.html") -> str:
    match = re.search(
        r"assets/index-[A-Za-z0-9_-]+\.js", index_path.read_text(encoding="utf-8")
    )
    if match is None:
        raise DeploymentError("Frontend index does not identify its built JavaScript asset")
    return match.group(0)


def verify_frontend_asset(api_url: str, expected_asset: str) -> None:
    body = _open_without_proxy(urllib.request.Request(f"{api_url.rstrip('/')}/"), 30).decode(
        "utf-8"
    )
    if expected_asset not in body:
        raise DeploymentError(
            f"Served frontend asset does not match the package: expected {expected_asset}"
        )
    print(f"Frontend asset verified: {expected_asset}")


def verify_function_indexing(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Any],
) -> None:
    resource_group = _output_string(outputs, "resourceGroupName")
    for output_name, expected in EXPECTED_FUNCTIONS.items():
        function_name = _output_string(outputs, output_name)
        result = runner.run(
            [
                "az",
                "functionapp",
                "function",
                "list",
                "--subscription",
                inputs.subscription,
                "--resource-group",
                resource_group,
                "--name",
                function_name,
                "--query",
                "[].name",
                "--output",
                "tsv",
            ],
            capture=True,
        )
        indexed = {name.rsplit("/", maxsplit=1)[-1] for name in result.stdout.splitlines()}
        missing = expected - indexed
        if missing:
            raise DeploymentError(
                f"Function app {function_name} did not index: {', '.join(sorted(missing))}"
            )


def verify_telemetry_function_health(outputs: Mapping[str, Any]) -> None:
    function_name = _output_string(outputs, "telemetryFunctionName")
    health_url = f"https://{function_name}.azurewebsites.net/api"
    wait_for_health(health_url, timeout_seconds=1800)
    print(f"Telemetry Function host verified: {function_name}")


def _confirm_deployment(assume_yes: bool) -> None:
    if assume_yes:
        return
    if input("Type 'deploy' to create or update these resources: ").strip() != "deploy":
        raise DeploymentError("Deployment cancelled")


def _outputs_path(inputs: DeploymentInputs) -> Path:
    return inputs.state_path.with_name(inputs.state_path.stem + ".outputs.json")


def load_saved_outputs(inputs: DeploymentInputs) -> dict[str, Any] | None:
    path = _outputs_path(inputs)
    if not path.exists():
        return None
    try:
        outputs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentError(f"Invalid deployment outputs: {path}") from error
    if not isinstance(outputs, dict):
        raise DeploymentError(f"Deployment outputs must be an object: {path}")
    return outputs


def load_existing_core(inputs: DeploymentInputs) -> ExistingCore | None:
    outputs = load_saved_outputs(inputs)
    return ExistingCore.from_outputs(outputs) if outputs is not None else None


def _write_outputs(inputs: DeploymentInputs, outputs: Mapping[str, Any]) -> None:
    destination = _outputs_path(inputs)
    destination.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    print(f"Deployment outputs: {destination}")


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="upgrade-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _upgrade_lock(directory: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:
        raise DeploymentError("APIM upgrades require a POSIX deployment host") from error
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("Another process owns this APIM upgrade") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def gateway_upgrade(
    runner: CommandRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Any],
    action: str,
    *,
    assume_yes: bool = False,
) -> None:
    if (
        _output_string(outputs, "resourceGroupName").casefold()
        != inputs.resource_group_name.casefold()
    ):
        raise DeploymentError("Saved deployment outputs belong to a different resource group")
    core = ExistingCore.from_outputs(outputs)
    api_id = _output_string(outputs, "apimApiId")
    resource_id = (
        f"/subscriptions/{inputs.subscription}/resourceGroups/{core.apim_resource_group_name}"
        f"/providers/Microsoft.ApiManagement/service/{core.apim_name}/apis/{api_id}"
    )
    application_ids = tuple(
        f"/subscriptions/{inputs.subscription}/resourceGroups/{inputs.resource_group_name}"
        f"/providers/Microsoft.Web/sites/{_output_string(outputs, name)}"
        for name in ("apiName", "controlPlaneFunctionName")
    )
    template = REPOSITORY_ROOT / "infra/apim-upgrade.bicep"
    parent_path = REPOSITORY_ROOT / "infra/policies/foundry-finops-policy.xml"
    denial_path = REPOSITORY_ROOT / "infra/policies/provider-neutral-images-policy.xml"
    canonical = parent_path.read_text(encoding="utf-8")
    denial = denial_path.read_text(encoding="utf-8")
    bindings = {
        "apiResourceId": resource_id,
        "applications": list(application_ids),
        "templates": {
            str(path.relative_to(REPOSITORY_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (template, REPOSITORY_ROOT / "infra/modules/apim-upgrade.bicep",
                         parent_path, denial_path, REPOSITORY_ROOT / "scripts/apim_upgrade.py",
                         REPOSITORY_ROOT / "scripts/deploy.py")
        },
    }
    directory = inputs.state_path.with_suffix(".upgrades") / UPGRADE_VERSION
    credentials = runner.run_json([
        "az", "account", "get-access-token", "--subscription", inputs.subscription,
        "--resource", "https://management.azure.com/", "--output", "json",
    ])
    token = credentials.pop("accessToken")
    with _upgrade_lock(directory), httpx.Client(
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=60, trust_env=False,
    ) as client:
        token = ""

        def preview(
            plan: ImageUpgradePlan, stage: str, revision: str, create_revision: bool,
        ) -> tuple[JsonObject, str]:
            parameters = _arm_parameter_document(upgrade_parameters(
                plan, stage, revision, create_revision=create_revision
            ))
            name = f"apim-{document_digest(plan.document())[:24]}-{stage}"
            with temporary_parameter_file(parameters, directory) as parameter_file:
                result = runner.run_json(_deployment_command(
                    "what-if", inputs, template, parameter_file, name
                ))
            validate_upgrade_what_if(result, plan, stage, revision)
            _what_if_counts(result)
            _write_private_json(directory / f"what-if-{stage}.json", result)
            return parameters, name

        def deploy(
            plan: ImageUpgradePlan, stage: str, revision: str, create_revision: bool,
        ) -> None:
            name = f"apim-{document_digest(plan.document())[:24]}-{stage}"
            prior = client.get(
                f"https://management.azure.com/subscriptions/{inputs.subscription}"
                f"/providers/Microsoft.Resources/deployments/{name}",
                params={"api-version": "2025-04-01"},
            )
            if prior.status_code == 200:
                state = prior.json().get("properties", {}).get("provisioningState")
                if state not in {"Succeeded", "Failed", "Canceled"}:
                    raise ApimUpgradeError(
                        "The previous ARM upgrade operation is not terminal; do not retry"
                    )
            elif prior.status_code != 404:
                raise ApimUpgradeError("The previous ARM upgrade status could not be verified")
            parameters, name = preview(plan, stage, revision, create_revision)
            with temporary_parameter_file(parameters, directory) as parameter_file:
                result = runner.run_json(_deployment_command(
                    "create", inputs, template, parameter_file, name
                ))
            if result.get("properties", {}).get("provisioningState") != "Succeeded":
                raise ApimUpgradeError("ARM upgrade has not completed successfully")

        backend = AzureUpgradeBackend(
            client, resource_id, (application_ids[0], application_ids[1]), deploy
        )
        current = backend.read()
        if current is None:
            raise DeploymentError("Saved API is absent; an upgrade cannot initialize a new API")
        if action == "check":
            if plan_image_upgrade(resource_id, current, canonical).required:
                raise DeploymentError(
                    "APIM infrastructure upgrade required before package deployment: "
                    "run scripts.deploy plan-upgrade, then upgrade in a maintenance window"
                )
            return
        plan_path, journal_path = directory / "plan.json", directory / "journal.json"
        if plan_path.exists():
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            if document.get("binding") != bindings:
                raise DeploymentError(
                    "The saved upgrade plan inputs changed; review the prior plan"
                )
            raw = document["plan"]
            plan = ImageUpgradePlan(**{**raw, "source": GatewaySnapshot(**raw["source"])})
            if plan.document() != plan_image_upgrade(
                resource_id, plan.source, canonical
            ).document():
                raise DeploymentError("The saved upgrade plan failed validation")
        else:
            if action != "plan-upgrade":
                raise DeploymentError("Run plan-upgrade before applying or rolling back an upgrade")
            plan = plan_image_upgrade(resource_id, current, canonical)
            _write_private_json(plan_path, {"binding": bindings, "plan": plan.document()})
        if not plan.required:
            if plan_image_upgrade(resource_id, current, canonical).required:
                raise DeploymentError("The API no longer satisfies the recorded no-change plan")
            print("APIM infrastructure is current; no resource writes are required")
            return
        journal = (
            json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else None
        )
        if (
            action != "rollback-upgrade" and journal is not None
            and journal.get("status") == "passed"
            and current.revision != plan.revision
            and not plan_image_upgrade(resource_id, current, canonical).required
        ):
            print("The current revision retains the completed upgrade; no resource writes required")
            return
        if action == "plan-upgrade":
            candidate = backend.read(plan.revision)
            preview(plan, "prepare", plan.revision, candidate is None)
            print(f"APIM upgrade plan: {plan_path}")
            print("Drain publication work and stop API/Control-plane before applying this plan")
            return
        _confirm_deployment(assume_yes)
        result = execute_image_upgrade(
            plan, backend, denial, journal,
            lambda value: _write_private_json(journal_path, value),
            rollback=action == "rollback-upgrade",
        )
        print(f"APIM infrastructure upgrade: {result['status']}")
        print("API and Control-plane remain stopped; resume through the reviewed package rollout")


def execute(args: argparse.Namespace, runner: CommandRunner) -> None:
    inputs = DeploymentInputs.load(
        args.subscription,
        args.parameters.resolve(),
        args.state.resolve() if args.state else None,
    )
    require_prerequisites(runner, inputs.subscription)
    if not args.allow_dirty:
        validate_source_snapshot(REPOSITORY_ROOT)
    saved_outputs = load_saved_outputs(inputs)
    if args.action in {"plan-upgrade", "upgrade", "rollback-upgrade"}:
        if saved_outputs is None:
            raise DeploymentError("An incremental upgrade requires the original deployment outputs")
        gateway_upgrade(runner, inputs, saved_outputs, args.action, assume_yes=args.yes)
        return
    if saved_outputs is not None:
        gateway_upgrade(runner, inputs, saved_outputs, "check")
    if saved_outputs is None:
        validate_flex_consumption_capabilities(runner, inputs)
        validate_postgres_capabilities(runner, inputs)
    password_reader: PasswordReader = getpass.getpass
    if args.owner_credentials is not None:
        configured_password = owner_credentials_password(
            args.owner_credentials.resolve(), inputs.owner_email
        )

        def configured_password_reader(_: str) -> str:
            return configured_password

        password_reader = configured_password_reader
    secrets_ = load_or_create_secret_material(
        inputs,
        read_password=password_reader,
        require_owner_password=args.action == "deploy",
    )
    existing_core = (
        ExistingCore.from_outputs(saved_outputs)
        if saved_outputs is not None
        else ExistingCore.from_parameters(inputs.parameters)
    )
    base_parameters = deployment_parameters(
        inputs,
        secrets_,
        existing_core=existing_core,
        resume_existing_environment=saved_outputs is not None,
    )
    main_template = REPOSITORY_ROOT / "infra" / "main.bicep"
    release_template = REPOSITORY_ROOT / "infra" / "runtime-release.bicep"
    if saved_outputs is None:
        what_if(
            runner,
            inputs,
            main_template,
            base_parameters,
            f"{inputs.resource_prefix}-platform",
        )
        if args.action == "plan":
            return
        _confirm_deployment(args.yes)
        base_result = deploy_template(
            runner,
            inputs,
            main_template,
            base_parameters,
            f"{inputs.resource_prefix}-platform",
        )
        platform_outputs = deployment_outputs(base_result)
    else:
        platform_outputs = saved_outputs
        api_settings = current_app_settings(
            runner, inputs, _output_string(platform_outputs, "apiName")
        )
        control_plane_settings = current_app_settings(
            runner,
            inputs,
            _output_string(platform_outputs, "controlPlaneFunctionName"),
        )
        release_parameters = runtime_release_parameters(
            secrets_,
            platform_outputs,
            platform_outputs,
            api_settings,
            control_plane_settings,
            base_parameters=base_parameters,
        )
        what_if_resource_group(
            runner,
            inputs,
            release_template,
            release_parameters,
            f"{inputs.resource_prefix}-runtime-release",
        )
        if args.action == "plan":
            return
        _confirm_deployment(args.yes)
    version = source_version(runner)
    observer_version = observer_source_version(runner)
    packages = build_packages(runner, inputs, version)
    expected_asset = frontend_asset()
    deploy_packages(runner, inputs, platform_outputs, packages)

    observer_document = observer_parameters(
        inputs,
        platform_outputs,
        secrets_,
        observer_version,
        existing_observer=platform_outputs if saved_outputs is not None else None,
    )
    observer_template = REPOSITORY_ROOT / "infra" / "envoy-cache-adapter" / "main.bicep"
    what_if(
        runner,
        inputs,
        observer_template,
        observer_document,
        f"{inputs.resource_prefix}-observer",
    )
    observer_result = deploy_template(
        runner,
        inputs,
        observer_template,
        observer_document,
        f"{inputs.resource_prefix}-observer",
    )
    observer_outputs = deployment_outputs(observer_result)
    build_and_start_observer(runner, inputs, observer_outputs, observer_version)
    wait_for_observer_health(_output_string(observer_outputs, "webAppUrl"))

    api_settings = current_app_settings(
        runner, inputs, _output_string(platform_outputs, "apiName")
    )
    control_plane_settings = current_app_settings(
        runner,
        inputs,
        _output_string(platform_outputs, "controlPlaneFunctionName"),
    )
    release_parameters = runtime_release_parameters(
        secrets_,
        platform_outputs,
        observer_outputs,
        api_settings,
        control_plane_settings,
        base_parameters=base_parameters,
    )
    what_if_resource_group(
        runner,
        inputs,
        release_template,
        release_parameters,
        f"{inputs.resource_prefix}-runtime-release",
    )
    deploy_resource_group_template(
        runner,
        inputs,
        release_template,
        release_parameters,
        f"{inputs.resource_prefix}-runtime-release",
    )
    final_outputs = platform_outputs
    restart_runtime_apps(runner, inputs, final_outputs)
    api_url = _output_string(final_outputs, "apiUrl")
    wait_for_health(api_url, timeout_seconds=1800)
    verify_frontend_asset(api_url, expected_asset)
    if secrets_.owner_password is None:
        raise DeploymentError("Initial Owner password is required for verification")
    verify_owner_login(api_url, inputs.owner_email, secrets_.owner_password)
    verify_function_indexing(runner, inputs, final_outputs)
    verify_telemetry_function_health(final_outputs)
    _write_outputs(inputs, {**final_outputs, **observer_outputs})
    print("Turnstile deployment completed.")
    print(f"Open: {api_url}")
    print(
        "Foundry onboarding uses APIM principal "
        f"{_output_string(final_outputs, 'apimPrincipalId')} with Cognitive Services User."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or deploy a complete self-hosted Turnstile environment."
    )
    parser.add_argument(
        "action", choices=("plan", "deploy", "plan-upgrade", "upgrade", "rollback-upgrade")
    )
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--owner-credentials",
        type=Path,
        help="Optional private 0600 JSON file containing the Owner email and password.",
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Development only: package the current dirty worktree.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        execute(build_parser().parse_args(argv), CommandRunner())
    except (
        DeploymentError, ApimUpgradeError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())