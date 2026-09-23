from __future__ import annotations

import json
import re
import stat
import subprocess
import tomllib
import urllib.error
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from packaging.tags import cpython_tags
from packaging.utils import parse_wheel_filename

from scripts.deploy import (
    CommandRunner,
    DeploymentError,
    DeploymentInputs,
    ExistingCore,
    _install_linux_dependencies,
    _upgrade_lock,
    _write_private_json,
    build_and_start_observer,
    build_parser,
    deploy_packages,
    deploy_webapp_package,
    deployment_parameters,
    deterministic_zip,
    execute,
    frontend_asset,
    linux_dependency_command,
    load_existing_core,
    load_or_create_secret_material,
    observer_names,
    observer_parameters,
    observer_plan_name,
    observer_source_version,
    owner_credentials_password,
    pip_linux_dependency_command,
    restart_runtime_apps,
    runtime_release_parameters,
    temporary_parameter_file,
    validate_flex_consumption_capabilities,
    validate_packaged_dependencies,
    validate_postgres_capabilities,
    verify_owner_login,
    wait_for_health,
    wait_for_observer_health,
    what_if,
)
from scripts.stage_deployment import REPOSITORY_ROOT


def _parameters(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "parameters": {
                    "resourcePrefix": {"value": "turnstile"},
                    "resourceGroupName": {"value": "turnstile-test"},
                    "location": {"value": "eastus2"},
                    "apimPublisherEmail": {"value": "admin@example.com"},
                    "bootstrapOwnerEmail": {"value": "owner@example.com"},
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_secret_state_is_private_stable_and_excludes_plaintext(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "00000000-0000-0000-0000-000000000001",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    first = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=True,
    )
    second = load_or_create_secret_material(
        inputs,
        read_password=lambda _: "a-secure-owner-password",
        require_owner_password=True,
    )

    assert first.values == second.values
    assert first.owner_password == "a-secure-owner-password"
    assert first.values["observerAdapterSharedKey"] != first.values["managementApiKey"]
    assert "a-secure-owner-password" not in inputs.state_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(inputs.state_path.stat().st_mode) == 0o600


def test_public_parameter_file_rejects_secure_values(tmp_path: Path) -> None:
    path = _parameters(tmp_path / "parameters.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["parameters"]["managementApiKey"] = {"value": "do-not-store-here"}
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(DeploymentError, match="Keep secure parameters out"):
        DeploymentInputs.load("subscription", path)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        ("observerPlanSkuName", "B1", "must be one of"),
        ("observerPlanWorkerCount", 0, "must be between 1 and 30"),
        ("observerPlanWorkerCount", 31, "must be between 1 and 30"),
        ("observerPlanWorkerCount", "2", "must be an integer"),
    ),
)
def test_observer_plan_parameters_are_validated_before_deployment(
    tmp_path: Path, name: str, value: object, message: str
) -> None:
    path = _parameters(tmp_path / "parameters.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["parameters"][name] = {"value": value}
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(DeploymentError, match=message):
        DeploymentInputs.load("subscription", path)


def test_postgres_preflight_rejects_restricted_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = CommandRunner()
    monkeypatch.setattr(
        runner,
        "run_json",
        lambda *_args, **_kwargs: {
            "reason": "Subscriptions are restricted from provisioning in this region.",
            "versions": [],
            "editions": [],
        },
    )

    with pytest.raises(DeploymentError, match="PostgreSQL 16 is unavailable in eastus2"):
        validate_postgres_capabilities(runner, inputs)


def test_flex_preflight_accepts_registered_supported_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = CommandRunner()
    responses = iter(("Registered\n", "eastus2\nwestus3\n"))

    def run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=next(responses))

    monkeypatch.setattr(runner, "run", run)

    validate_flex_consumption_capabilities(runner, inputs)


def test_flex_preflight_rejects_unsupported_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = CommandRunner()
    responses = iter(("Registered\n", "westus3\n"))

    def run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=next(responses))

    monkeypatch.setattr(runner, "run", run)

    with pytest.raises(DeploymentError, match="Flex Consumption is unavailable"):
        validate_flex_consumption_capabilities(runner, inputs)


def test_postgres_preflight_accepts_requested_sku_and_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = CommandRunner()
    commands: list[Sequence[str]] = []

    def run_json(command: Sequence[str], **_kwargs: object) -> dict[str, object]:
        commands.append(command)
        return {
            "reason": None,
            "versions": ["16"],
            "editions": [
                {
                    "name": "Burstable",
                    "skus": [
                        {"name": "Standard_B1ms", "zones": ["1", "2", "3"]}
                    ],
                }
            ],
        }

    monkeypatch.setattr(runner, "run_json", run_json)

    validate_postgres_capabilities(runner, inputs)

    assert commands[0][:4] == ["az", "postgres", "flexible-server", "list-skus"]
    assert commands[0][commands[0].index("--location") + 1] == "eastus2"


def test_owner_credentials_are_private_and_match_public_email(tmp_path: Path) -> None:
    path = tmp_path / "owner.credentials.json"
    path.write_text(
        json.dumps(
            {
                "email": "owner@example.com",
                "password": "a-secure-owner-password",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert (
        owner_credentials_password(path, "owner@example.com")
        == "a-secure-owner-password"
    )
    with pytest.raises(DeploymentError, match="does not match"):
        owner_credentials_password(path, "other@example.com")


def test_owner_credentials_reject_group_or_world_access(tmp_path: Path) -> None:
    path = tmp_path / "owner.credentials.json"
    path.write_text(
        json.dumps(
            {
                "email": "owner@example.com",
                "password": "a-secure-owner-password",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o644)

    with pytest.raises(DeploymentError, match="permissions must be 0600"):
        owner_credentials_password(path, "owner@example.com")


def test_base_parameters_disable_workers_until_observer_exists(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )

    parameters = deployment_parameters(inputs, material)["parameters"]

    assert parameters["provisionControlPlane"]["value"] is True
    assert parameters["controlPlaneEnabled"]["value"] is False
    assert parameters["gatewayReleaseWorkerEnabled"]["value"] is False
    assert parameters["apimUsageObserver"]["value"]["mode"] == "disabled"
    assert "observerAdapterSharedKey" not in parameters


def test_external_apim_still_provisions_a_clean_platform(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )
    core = ExistingCore(
        apim_name="apim-existing",
        apim_resource_group_name="apim-shared",
        apim_principal_id="00000000-0000-4000-8000-000000000010",
        apim_gateway_url="https://apim-existing.azure-api.net",
    )

    parameters = deployment_parameters(
        inputs, material, existing_core=core
    )["parameters"]

    assert parameters["provisionApimService"]["value"] is False
    assert parameters["provisionPostgres"]["value"] is True
    assert parameters["deployApimBootstrap"]["value"] is True
    assert parameters["existingApimName"]["value"] == core.apim_name
    assert (
        parameters["existingApimResourceGroupName"]["value"]
        == core.apim_resource_group_name
    )
    assert parameters["existingApimPrincipalId"]["value"] == core.apim_principal_id
    assert parameters["existingApimGatewayUrl"]["value"] == core.apim_gateway_url
    assert parameters["apimApiId"]["value"] == "turnstile-llm"
    assert parameters["gatewayApiRelativePath"]["value"] == "turnstile/llm"
    assert parameters["observerAdapterKeyNamedValueName"]["value"] == (
        "turnstile-observer-key"
    )


def test_runtime_release_recovers_legacy_shared_apim_system_ids(tmp_path: Path) -> None:
    path = _parameters(tmp_path / "parameters.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["parameters"].update(
        {
            "resourcePrefix": {"value": "shared"},
        }
    )
    path.write_text(json.dumps(document), encoding="utf-8")
    inputs = DeploymentInputs.load("subscription", path, tmp_path / "state.json")
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )

    release = runtime_release_parameters(
        inputs,
        material,
        {
            "apiName": "api-shared-test",
            "controlPlaneFunctionName": "func-shared-control-test",
            "gatewayApiPath": "https://apim-existing.azure-api.net/shared/llm",
            "apimApiId": "shared-llm",
            "apimProbeSubscriptionId": "shared-publisher-probe",
        },
        {
            "webAppUrl": "https://observer.test",
            "adapterKeyNamedValueName": "shared-observer-key",
        },
        {},
        {},
    )["parameters"]

    assert release["dashboardSubscriptionId"]["value"] == "shared-dashboard"
    assert release["probeSubscriptionId"]["value"] == "shared-publisher-probe"


def test_external_apim_adoption_rejects_partial_configuration(tmp_path: Path) -> None:
    path = _parameters(tmp_path / "parameters.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["parameters"]["existingApimName"] = {"value": "apim-existing"}
    path.write_text(json.dumps(document), encoding="utf-8")
    inputs = DeploymentInputs.load("subscription", path, tmp_path / "state.json")

    with pytest.raises(
        DeploymentError,
        match="Existing APIM adoption requires:.*existingApimResourceGroupName",
    ):
        ExistingCore.from_parameters(inputs.parameters)


def test_resume_skips_existing_platform_and_gateway_bootstrap(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )
    core = ExistingCore(
        apim_name="apim-existing",
        apim_resource_group_name="turnstile-test",
        apim_principal_id="00000000-0000-4000-8000-000000000010",
        apim_gateway_url="https://apim-existing.azure-api.net",
    )

    parameters = deployment_parameters(
        inputs,
        material,
        existing_core=core,
        resume_existing_environment=True,
    )["parameters"]

    assert parameters["provisionApimService"]["value"] is False
    assert parameters["provisionPostgres"]["value"] is False
    assert parameters["deployApimBootstrap"]["value"] is False


def test_saved_outputs_enable_existing_core_on_rerun(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    outputs_path = tmp_path / "state.outputs.json"
    outputs_path.write_text(
        json.dumps(
            {
                    "resourceGroupName": "turnstile-test",
                "apimName": "apim-existing",
                "apimPrincipalId": "00000000-0000-4000-8000-000000000010",
                "apimGatewayUrl": "https://apim-existing.azure-api.net",
            }
        ),
        encoding="utf-8",
    )

    core = load_existing_core(inputs)

    assert core is not None
    assert core.apim_name == "apim-existing"
    assert core.apim_resource_group_name == "turnstile-test"


def test_temporary_parameter_file_is_private_and_deleted(tmp_path: Path) -> None:
    with temporary_parameter_file({"parameters": {}}, tmp_path) as path:
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.exists()


def test_observer_names_are_stable_and_azure_safe(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "00000000-0000-0000-0000-000000000001",
        _parameters(tmp_path / "parameters.json"),
    )

    acr_name, web_app_name = observer_names(inputs)

    assert acr_name == observer_names(inputs)[0]
    assert acr_name.isalnum() and acr_name.islower() and len(acr_name) <= 50
    assert web_app_name.startswith("obs-turnstile-") and len(web_app_name) <= 60
    assert observer_plan_name(inputs).startswith("plan-obs-")
    assert len(observer_plan_name(inputs)) <= 40


def test_observer_parameters_reuse_apps_but_isolate_the_observer_plan(
    tmp_path: Path,
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )
    document = observer_parameters(
        inputs,
        {
            "resourceGroupName": "turnstile-test",
            "apimResourceGroupName": "shared-apim",
            "appServicePlanName": "plan-turnstile-test",
            "eventHubNamespaceName": "eh-turnstile-test",
            "apimName": "apim-turnstile-test",
            "observerAdapterKeyNamedValueName": "turnstile-test-observer-key",
        },
        material,
        "abc123",
        existing_observer={
            "acrName": "acrexisting",
            "webAppName": "observer-existing",
            "observerAppServicePlanName": "plan-observer-existing",
        },
    )

    assert document["parameters"]["acrName"]["value"] == "acrexisting"
    assert document["parameters"]["webAppName"]["value"] == "observer-existing"
    assert document["parameters"]["apimResourceGroupName"]["value"] == "shared-apim"
    assert document["parameters"]["adapterKeyNamedValueName"]["value"] == (
        "turnstile-test-observer-key"
    )
    assert (
        document["parameters"]["appServicePlanName"]["value"]
        == observer_plan_name(inputs)
    )
    assert document["parameters"]["appServicePlanName"]["value"] != "plan-turnstile-test"
    assert document["parameters"]["appServicePlanSkuName"]["value"] == "P0v3"
    assert document["parameters"]["appServicePlanWorkerCount"]["value"] == 1
    assert document["parameters"]["provisionAcr"]["value"] is False


@pytest.mark.parametrize(
    ("existing_observer", "expected_acr_group"),
    (
        (None, "turnstile-test"),
        ({"acrName": "acrexisting", "webAppName": "observer-existing"}, "shared-apim"),
        (
            {
                "acrName": "acrexisting",
                "webAppName": "observer-existing",
                "acrResourceGroupName": "turnstile-test",
            },
            "turnstile-test",
        ),
    ),
)
def test_observer_registry_scope_is_independent_of_reused_apim(
    tmp_path: Path,
    existing_observer: dict[str, str] | None,
    expected_acr_group: str,
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )
    document = observer_parameters(
        inputs,
        {
            "resourceGroupName": "turnstile-test",
            "apimResourceGroupName": "shared-apim",
            "eventHubNamespaceName": "eh-turnstile-test",
            "apimName": "apim-shared",
        },
        material,
        "abc123",
        existing_observer=existing_observer,
    )

    assert document["parameters"]["acrResourceGroupName"]["value"] == expected_acr_group
    assert document["parameters"]["apimResourceGroupName"]["value"] == "shared-apim"


def test_deterministic_zip_has_stable_bytes_and_order(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "b.txt").write_text("b", encoding="utf-8")
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").chmod(0o600)
    (source / "a.txt").chmod(0o700)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    deterministic_zip(source, first)
    deterministic_zip(source, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["a.txt", "b.txt"]
        assert stat.S_IMODE(archive.getinfo("a.txt").external_attr >> 16) == 0o755
        assert stat.S_IMODE(archive.getinfo("b.txt").external_attr >> 16) == 0o644


def test_observer_version_is_scoped_to_its_source_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CommandRunner()
    commands: list[Sequence[str]] = []

    def run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ee355910a942\n")

    monkeypatch.setattr(runner, "run", run)

    assert observer_source_version(runner) == "ee355910a942"
    assert commands == [
        ["git", "rev-parse", "--short=12", "HEAD:infra/envoy-cache-adapter"]
    ]


def test_existing_observer_image_skips_rebuild_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = CommandRunner()
    commands: list[Sequence[str]] = []

    def run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner, "run", run)

    build_and_start_observer(
        runner,
        inputs,
        {
            "acrName": "acrexisting",
            "image": (
                "acrexisting.azurecr.io/turnstile/"
                "envoy-cache-adapter:ee355910a942"
            ),
            "webAppName": "observer-existing",
        },
        "ee355910a942",
    )

    assert len(commands) == 1
    assert commands[0][:4] == ["az", "acr", "repository", "show"]


def test_api_deployment_uses_entra_publish_and_turnstile_health_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    api_deployments: list[tuple[str, Path]] = []
    health_calls: list[tuple[str, int]] = []

    class Runner:
        def run(self, command: Sequence[str], **_: object) -> None:
            commands.append(list(command))

    monkeypatch.setattr(
        "scripts.deploy.wait_for_health",
        lambda url, timeout_seconds=180: health_calls.append((url, timeout_seconds)),
    )
    monkeypatch.setattr(
        "scripts.deploy.deploy_webapp_package",
        lambda _runner, _inputs, app_name, package: api_deployments.append(
            (app_name, package)
        ),
    )
    deploy_packages(
        Runner(),  # type: ignore[arg-type]
        type("Inputs", (), {"subscription": "sub", "resource_group_name": "rg"})(),
        {
            "resourceGroupName": "rg",
            "apiName": "api",
            "apiUrl": "https://api.example.test",
            "telemetryFunctionName": "telemetry",
            "controlPlaneFunctionName": "control",
        },
        {
            "api": Path("api.zip"),
            "telemetry": Path("telemetry.zip"),
            "control-plane": Path("control-plane.zip"),
        },
    )

    assert api_deployments == [
        ("api", Path("api.zip")),
        ("telemetry", Path("telemetry.zip")),
        ("control", Path("control-plane.zip")),
    ]
    assert health_calls == [("https://api.example.test", 1800)]


def test_webapp_package_deployment_uses_entra_without_logging_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []
    requests: list[tuple[urllib.request.Request, float]] = []
    package = tmp_path / "api.zip"
    package.write_bytes(b"package-bytes")

    class Runner:
        def run_json(self, command: Sequence[str], **_: object) -> dict[str, str]:
            commands.append(list(command))
            return {"accessToken": "secret-token"}

    def capture_request(request: urllib.request.Request, timeout: float) -> bytes:
        requests.append((request, timeout))
        return b"{}"

    monkeypatch.setattr(
        "scripts.deploy._open_without_proxy",
        capture_request,
    )

    deploy_webapp_package(
        Runner(),  # type: ignore[arg-type]
        type("Inputs", (), {"subscription": "sub"})(),
        "api",
        package,
    )

    assert commands == [
        [
            "az",
            "account",
            "get-access-token",
            "--subscription",
            "sub",
            "--resource",
            "https://management.azure.com/",
            "--query",
            "{accessToken:accessToken}",
            "--output",
            "json",
        ]
    ]
    request, timeout = requests[0]
    assert request.full_url == (
        "https://api.scm.azurewebsites.net/api/publish"
        "?type=zip&clean=true&restart=true"
    )
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert request.data == b"package-bytes"
    assert timeout == 1800
    assert "secret-token" not in capsys.readouterr().out


def test_function_package_deployment_restarts_and_retries_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    deployments: list[tuple[str, Path]] = []

    class Runner:
        def run(self, command: Sequence[str], **_: object) -> None:
            commands.append(list(command))

    def deploy_package(
        _runner: object, _inputs: object, app_name: str, package: Path
    ) -> None:
        if app_name == "api":
            return
        deployments.append((app_name, package))
        if len(deployments) == 1:
            raise DeploymentError("transient deployment failure")

    monkeypatch.setattr("scripts.deploy.wait_for_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("scripts.deploy.deploy_webapp_package", deploy_package)
    monkeypatch.setattr("scripts.deploy.time.sleep", lambda _: None)
    deploy_packages(
        Runner(),  # type: ignore[arg-type]
        type("Inputs", (), {"subscription": "sub", "resource_group_name": "rg"})(),
        {
            "resourceGroupName": "rg",
            "apiName": "api",
            "apiUrl": "https://api.example.test",
            "telemetryFunctionName": "telemetry",
            "controlPlaneFunctionName": "control",
        },
        {
            "api": Path("api.zip"),
            "telemetry": Path("telemetry.zip"),
            "control-plane": Path("control-plane.zip"),
        },
    )

    assert deployments == [
        ("telemetry", Path("telemetry.zip")),
        ("telemetry", Path("telemetry.zip")),
        ("control", Path("control-plane.zip")),
    ]
    assert len(commands) == 1
    assert commands[0][:3] == ["az", "functionapp", "restart"]
    assert commands[0][commands[0].index("--name") + 1] == "telemetry"


def test_health_gate_accepts_successful_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scripts.deploy._open_without_proxy", lambda *_: b"")

    assert wait_for_health("https://observer.example.test") == ""


def test_observer_health_gate_allows_slow_container_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def wait_for_health(url: str, timeout_seconds: int = 180) -> str:
        calls.append((url, timeout_seconds))
        return "ok"

    monkeypatch.setattr("scripts.deploy.wait_for_health", wait_for_health)

    assert wait_for_observer_health("https://observer.example.test") == "ok"
    assert calls == [("https://observer.example.test", 1800)]


def test_owner_login_retries_transient_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        urllib.error.URLError("app restarting"),
        json.dumps(
            {
                "email": "owner@example.com",
                "role": "owner",
                "method": "password",
            }
        ).encode(),
    ]

    def open_request(*_: object) -> bytes:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, bytes)
        return response

    monkeypatch.setattr("scripts.deploy._open_without_proxy", open_request)
    monkeypatch.setattr("scripts.deploy.time.sleep", lambda _: None)

    verify_owner_login(
        "https://api.example.test", "owner@example.com", "secret", timeout_seconds=10
    )

    assert responses == []


def test_linux_dependency_command_uses_pinned_target_platform(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "requirements.txt").write_text("fastapi==0.139.2\n", encoding="utf-8")

    command = linux_dependency_command(staged)

    assert command[:3] == ["uv", "pip", "install"]
    assert "x86_64-manylinux_2_28" in command
    assert "3.11" in command
    assert "--compile-bytecode" not in command
    assert "--no-compile" not in command
    assert "--requirements" in command
    assert command[command.index("--constraint") + 1] == str(staged / "constraints.txt")


def test_pip_fallback_uses_pinned_target_platform(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "requirements.txt").write_text("fastapi==0.139.2\n", encoding="utf-8")

    command = pip_linux_dependency_command(staged, "/usr/bin/pip3")

    assert command[:2] == ["/usr/bin/pip3", "install"]
    assert "manylinux_2_28_x86_64" in command
    assert "manylinux_2_17_x86_64" in command
    assert "manylinux2014_x86_64" in command
    assert "3.11" in command
    assert "--only-binary=:all:" in command
    assert "--requirement" in command
    assert command[command.index("--constraint") + 1] == str(staged / "constraints.txt")


@pytest.mark.parametrize("fallback", [False, True])
def test_linux_install_locks_primary_and_fallback_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    commands: list[list[str]] = []
    runner = CommandRunner()

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if list(command[:2]) == ["uv", "export"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if fallback and list(command[:3]) == ["uv", "pip", "install"]:
            raise subprocess.CalledProcessError(1, command)
        metadata = staged / ".python_packages/lib/site-packages/fastapi-0.139.2.dist-info"
        metadata.mkdir(parents=True, exist_ok=True)
        (metadata / "METADATA").write_text("Name: fastapi\nVersion: 0.139.2\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr("scripts.deploy.shutil.which", lambda _: "/usr/bin/pip3")
    _install_linux_dependencies(runner, staged)
    assert commands[0][:3] == ["uv", "export", "--locked"]
    assert "--no-dev" in commands[0] and "--no-emit-project" in commands[0]
    assert commands[0][-1] == str(staged / "constraints.txt")
    for command in commands[1:]:
        assert command[command.index("--constraint") + 1] == str(staged / "constraints.txt")
    manifest = json.loads((staged / "dependency-manifest.json").read_text())
    assert manifest["packages"] == {"fastapi": "0.139.2"}
    assert len(manifest["lock_sha256"]) == 64


@pytest.mark.parametrize("name,version", [("fastapi", "0.1.0"), ("unexpected-package", "1.0")])
def test_packaged_dependencies_reject_versions_outside_the_lock(
    tmp_path: Path, name: str, version: str
) -> None:
    metadata = tmp_path / ".python_packages/lib/site-packages/unit.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    with pytest.raises(DeploymentError, match="does not match uv.lock"):
        validate_packaged_dependencies(tmp_path)


def test_linux_fallback_accepts_locked_pillow_and_older_binary_wheels(tmp_path: Path) -> None:
    command = pip_linux_dependency_command(tmp_path / "staged", "/usr/bin/pip3")
    platforms = [command[index + 1] for index, value in enumerate(command) if value == "--platform"]
    compatible = set(cpython_tags(python_version=(3, 11), abis=["cp311"], platforms=platforms))
    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    for package_name in ("pillow", "cryptography", "psycopg-binary"):
        package = next(item for item in lock["package"] if item["name"] == package_name)
        available = set().union(*(
            parse_wheel_filename(wheel["url"].rsplit("/", 1)[-1])[3]
            for wheel in package["wheels"]
        ))
        assert compatible.intersection(available), f"No compatible locked wheel for {package_name}"


def test_frontend_asset_reads_the_hashed_entrypoint(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text(
        '<script type="module" src="/assets/index-Ab_12-c.js"></script>',
        encoding="utf-8",
    )

    assert frontend_asset(index) == "assets/index-Ab_12-c.js"


def test_runtime_release_recycles_updated_apps_without_stale_api_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = CommandRunner()
    commands: list[Sequence[str]] = []

    def run(command: Sequence[str], **_kwargs: object) -> None:
        commands.append(command)

    monkeypatch.setattr(runner, "run", run)

    restart_runtime_apps(
        runner,
        inputs,
        {
            "resourceGroupName": "turnstile-test",
            "apiName": "api-turnstile-test",
            "controlPlaneFunctionName": "func-turnstile-control-test",
        },
    )

    assert [command[:3] for command in commands] == [
        ["az", "webapp", "stop"],
        ["az", "webapp", "start"],
        ["az", "functionapp", "restart"],
    ]
    assert [command[command.index("--name") + 1] for command in commands] == [
        "api-turnstile-test",
        "api-turnstile-test",
        "func-turnstile-control-test",
    ]


def test_upgrade_files_are_private_and_concurrent_execution_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "upgrade"
    path = directory / "journal.json"
    _write_private_json(path, {"status": "preparing"})
    _write_private_json(path, {"status": "passed"})
    assert json.loads(path.read_text()) == {"status": "passed"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    with (
        _upgrade_lock(directory), pytest.raises(DeploymentError, match="Another process"),
        _upgrade_lock(directory),
    ):
        pass
    with _upgrade_lock(directory):
        pass


@pytest.mark.parametrize("action", ["plan-upgrade", "upgrade", "rollback-upgrade"])
def test_incremental_commands_do_not_recreate_secrets_or_deploy_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    from scripts import deploy

    arguments = build_parser().parse_args([
        action, "--subscription", "subscription", "--parameters",
        str(_parameters(tmp_path / "parameters.json")), "--state", str(tmp_path / "state.json"),
    ])
    outputs = {"resourceGroupName": "turnstile-test"}
    monkeypatch.setattr(deploy, "require_prerequisites", lambda *_: None)
    monkeypatch.setattr(deploy, "validate_source_snapshot", lambda *_: None)
    monkeypatch.setattr(deploy, "load_saved_outputs", lambda *_: outputs)
    calls: list[str] = []

    def upgrade(*args: object, **kwargs: object) -> None:
        assert args[2] is outputs
        calls.append(str(args[3]))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("An upgrade must not touch platform secrets, packages or bootstrap")

    monkeypatch.setattr(deploy, "gateway_upgrade", upgrade)
    monkeypatch.setattr(deploy, "load_or_create_secret_material", forbidden)
    monkeypatch.setattr(deploy, "build_packages", forbidden)
    monkeypatch.setattr(deploy, "deploy_template", forbidden)
    execute(arguments, CommandRunner())
    assert calls == [action]


def test_rerun_checks_apim_upgrade_before_secrets_or_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import deploy

    arguments = build_parser().parse_args([
        "deploy", "--subscription", "subscription", "--parameters",
        str(_parameters(tmp_path / "parameters.json")),
    ])
    monkeypatch.setattr(deploy, "require_prerequisites", lambda *_: None)
    monkeypatch.setattr(deploy, "validate_source_snapshot", lambda *_: None)
    monkeypatch.setattr(deploy, "load_saved_outputs", lambda *_: {"existing": True})

    def blocked(*args: object, **kwargs: object) -> None:
        assert args[3] == "check"
        raise DeploymentError("APIM upgrade required")

    monkeypatch.setattr(deploy, "gateway_upgrade", blocked)
    with pytest.raises(DeploymentError, match="APIM upgrade required"):
        execute(arguments, CommandRunner())


def test_repository_parameter_example_and_generated_documents_match_bicep(
    tmp_path: Path,
) -> None:
    inputs = DeploymentInputs.load(
        "00000000-0000-0000-0000-000000000001",
        REPOSITORY_ROOT / "infra" / "main.parameters.example.json",
        tmp_path / "state.json",
    )
    answers = iter(("a-secure-owner-password", "a-secure-owner-password"))
    material = load_or_create_secret_material(
        inputs,
        read_password=lambda _: next(answers),
        require_owner_password=False,
    )
    platform_outputs = {
        "resourceGroupName": inputs.resource_group_name,
        "appServicePlanName": "plan-turnstile-test",
        "eventHubNamespaceName": "eh-turnstile-test",
        "apimName": "apim-turnstile-test",
    }
    root_document = deployment_parameters(inputs, material)
    observer_document = observer_parameters(inputs, platform_outputs, material, "abc123")
    release_document = runtime_release_parameters(
        inputs,
        material,
        {
            "apiName": "api-turnstile-test",
            "controlPlaneFunctionName": "func-turnstile-control-test",
            "gatewayApiPath": "https://apim.test/turnstile/llm",
        },
        {
            "webAppUrl": "https://observer.test",
            "adapterKeyNamedValueName": "turnstile-observer-key",
        },
        {"EXISTING_API_SETTING": "preserved"},
        {"EXISTING_CONTROL_SETTING": "preserved"},
    )

    root_declared = set(
        re.findall(
            r"(?m)^param\s+(\w+)",
            (REPOSITORY_ROOT / "infra" / "main.bicep").read_text(encoding="utf-8"),
        )
    )
    observer_declared = set(
        re.findall(
            r"(?m)^param\s+(\w+)",
            (REPOSITORY_ROOT / "infra" / "envoy-cache-adapter" / "main.bicep").read_text(
                encoding="utf-8"
            ),
        )
    )
    release_declared = set(
        re.findall(
            r"(?m)^param\s+(\w+)",
            (REPOSITORY_ROOT / "infra" / "runtime-release.bicep").read_text(
                encoding="utf-8"
            ),
        )
    )

    assert set(root_document["parameters"]) <= root_declared
    assert set(observer_document["parameters"]) <= observer_declared
    assert set(release_document["parameters"]) <= release_declared
    assert (
        release_document["parameters"]["currentApiSettings"]["value"]
        == {"EXISTING_API_SETTING": "preserved"}
    )
    assert (
        release_document["parameters"]["dashboardSubscriptionId"]["value"]
        == "turnstile-dashboard"
    )
    assert (
        release_document["parameters"]["probeSubscriptionId"]["value"]
        == "turnstile-publisher-probe"
    )


class WhatIfRunner(CommandRunner):
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result

    def run_json(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        del command, cwd, env
        return self.result


def test_what_if_reads_root_level_changes_and_rejects_delete(tmp_path: Path) -> None:
    inputs = DeploymentInputs.load(
        "subscription",
        _parameters(tmp_path / "parameters.json"),
        tmp_path / "state.json",
    )
    runner = WhatIfRunner(
        {
            "status": "Succeeded",
            "changes": [
                {"changeType": "Deploy", "resourceId": "/safe"},
                {"changeType": "Delete", "resourceId": "/unsafe"},
            ],
        }
    )

    with pytest.raises(DeploymentError, match="contains Delete changes"):
        what_if(
            runner,
            inputs,
            Path("infra/main.bicep"),
            {"parameters": {}},
            "test-deployment",
        )
