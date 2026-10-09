from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.stage_deployment import (
    REPOSITORY_ROOT,
    stage_deployment,
    validate_source_snapshot,
)

EXACT_DEPENDENCY = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^=]+$")
EXPECTED_DEPENDENCY_NAMES = {
    "requirements.txt": {
        "cryptography",
        "fastapi",
        "httpx",
        "pillow",
        "psycopg[binary]",
        "psycopg-pool",
        "pydantic",
        "pydantic-settings",
        "pyjwt[crypto]",
        "uvicorn",
    },
    "functions/telemetry/requirements.txt": {
        "azure-functions",
        "httpx",
        "psycopg[binary]",
        "psycopg-pool",
        "pydantic",
        "pydantic-settings",
    },
    "functions/control_plane/requirements.txt": {
        "azure-functions",
        "cryptography",
        "httpx",
        "pillow",
        "psycopg[binary]",
        "psycopg-pool",
        "pydantic",
        "pydantic-settings",
    },
}


def _requirements(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _dependency_name(requirement: str) -> str:
    return requirement.split("==", maxsplit=1)[0].casefold()


@pytest.fixture
def staging_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    for relative in (
        "backend",
        "backend/http",
        "turnstile_core",
        "frontend/dist",
        "migrations",
        "functions/telemetry",
        "functions/control_plane",
        "infra/policies",
        "tests/platform",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    files = {
        "backend/api.py": "app = object()\n",
        "backend/migrate.py": "def migrate(): return []\n",
        "backend/bootstrap.py": "def main(): return 0\n",
        "backend/http/user_settings.py": "router = object()\n",
        "turnstile_core/config.py": "class Settings: pass\n",
        "frontend/dist/index.html": "<!doctype html>\n",
        "migrations/001_token_observability.up.sql": "SELECT 1;\n",
        "migrations/011_user_settings_profile.up.sql": (
            REPOSITORY_ROOT / "migrations/011_user_settings_profile.up.sql"
        ).read_text(encoding="utf-8"),
        "migrations/040_latest.up.sql": "SELECT 1;\n",
        "functions/telemetry/function_app.py": "app = object()\n",
        "functions/telemetry/host.json": "{}\n",
        "functions/telemetry/requirements.txt": "azure-functions==1.24.0\n",
        "functions/control_plane/function_app.py": "app = object()\n",
        "functions/control_plane/host.json": "{}\n",
        "functions/control_plane/requirements.txt": "azure-functions==1.24.0\n",
        "infra/policies/foundry-finops-policy.xml": "<policies />\n",
        "requirements.txt": "fastapi==0.139.2\n",
        "pyproject.toml": "[project]\nname='test'\nversion='0'\n",
        "tests/platform/sentinel.py": "TEST_ONLY = True\n",
        "uv.lock": "version = 1\n",
    }
    for relative, content in files.items():
        (root / relative).write_text(content, encoding="utf-8")
    (root / "backend/__pycache__").mkdir()
    (root / "backend/__pycache__/api.pyc").write_bytes(b"cache")
    (root / "turnstile_core/__pycache__").mkdir()
    (root / "turnstile_core/__pycache__/config.pyc").write_bytes(b"cache")
    return root


@pytest.mark.parametrize("manifest,expected_names", EXPECTED_DEPENDENCY_NAMES.items())
def test_deployment_requirements_are_exact_project_pins(
    manifest: str,
    expected_names: set[str],
) -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_dependencies = {
        _dependency_name(requirement): requirement
        for requirement in project["project"]["dependencies"]
    }
    requirements = _requirements(REPOSITORY_ROOT / manifest)

    assert all(EXACT_DEPENDENCY.fullmatch(requirement) for requirement in requirements)
    assert requirements == {project_dependencies[name] for name in expected_names}


def test_repository_contains_every_staging_source() -> None:
    for relative in (
        "backend/api.py",
        "backend/migrate.py",
        "turnstile_core/config.py",
        "frontend/package.json",
            "migrations/001_initial_schema.up.sql",
        "functions/telemetry/function_app.py",
        "functions/control_plane/function_app.py",
        "infra/policies/foundry-finops-policy.xml",
    ):
        assert (REPOSITORY_ROOT / relative).is_file(), relative


def test_observer_registry_accepts_managed_identity_arm_tokens() -> None:
    template = (
        REPOSITORY_ROOT / "infra/envoy-cache-adapter/main.bicep"
    ).read_text(encoding="utf-8")

    assert "azureADAuthenticationAsArmPolicyStatus: 'enabled'" in template


def test_observer_registry_and_role_use_the_registry_resource_group() -> None:
    template = (
        REPOSITORY_ROOT / "infra/envoy-cache-adapter/main.bicep"
    ).read_text(encoding="utf-8")

    assert (
        "param acrResourceGroupName string = provisionAcr ? resourceGroupName : "
        "apimResourceGroupName"
    ) in template
    assert "name: acrResourceGroupName" in template
    for module_name in ("registry", "acrRole"):
        module = re.search(rf"module {module_name}\b.*?\n}}", template, re.DOTALL)
        assert module is not None
        assert "scope: acrResourceGroup" in module.group()
    assert "output acrResourceGroupName string = acrResourceGroupName" in template


def test_api_staging_contains_runtime_contract(tmp_path: Path, staging_root: Path) -> None:
    destination = tmp_path / "api"
    stage_deployment("api", destination, root=staging_root)

    assert (destination / "backend/api.py").is_file()
    assert (destination / "backend/migrate.py").is_file()
    assert (destination / "backend/bootstrap.py").is_file()
    assert (destination / "backend/http/user_settings.py").is_file()
    assert (destination / "turnstile_core/config.py").is_file()
    assert (destination / "frontend/dist/index.html").is_file()
    assert (destination / "migrations/001_token_observability.up.sql").is_file()
    assert (destination / "migrations/011_user_settings_profile.up.sql").read_bytes() == (
        REPOSITORY_ROOT / "migrations/011_user_settings_profile.up.sql"
    ).read_bytes()
    assert list((destination / "migrations").glob("040_*.up.sql"))
    assert not list(destination.rglob("__pycache__"))
    assert not list(destination.rglob("*.pyc"))
    assert not (destination / "tests").exists()


@pytest.mark.parametrize("target", ("telemetry", "control-plane"))
def test_function_staging_contains_runtime_contract(
    tmp_path: Path,
    staging_root: Path,
    target: str,
) -> None:
    destination = tmp_path / target
    stage_deployment(target, destination, root=staging_root)

    assert (destination / "function_app.py").is_file()
    assert (destination / "host.json").is_file()
    assert (destination / "requirements.txt").is_file()
    assert (destination / "turnstile_core/config.py").is_file()
    assert not (destination / "backend").exists()
    if target == "control-plane":
        assert (destination / "policies/foundry-finops-policy.xml").is_file()
        assert not (destination / "infra").exists()
    assert not list(destination.rglob("__pycache__"))
    assert not list(destination.rglob("*.pyc"))
    assert not (destination / "tests").exists()


@pytest.mark.parametrize(
    ("target", "expected_functions"),
    (("telemetry", 4), ("control-plane", 2)),
)
def test_staged_function_indexes_without_backend(
    tmp_path: Path,
    target: str,
    expected_functions: int,
) -> None:
    destination = tmp_path / target
    stage_deployment(target, destination)
    script = """
import builtins
import sys

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "backend" or name.startswith("backend."):
        raise AssertionError(f"backend import from staged Function: {name}")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import function_app

assert len(function_app.app.get_functions()) == int(sys.argv[1])
"""

    subprocess.run(
        [sys.executable, "-c", script, str(expected_functions)],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_staging_rejects_nonempty_destination(
    tmp_path: Path,
    staging_root: Path,
) -> None:
    destination = tmp_path / "artifact"
    destination.mkdir()
    (destination / "keep.txt").write_text("do not delete", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be empty"):
        stage_deployment("telemetry", destination, root=staging_root)

    assert (destination / "keep.txt").read_text(encoding="utf-8") == "do not delete"


def test_source_snapshot_without_git_metadata_is_accepted(staging_root: Path) -> None:
    validate_source_snapshot(staging_root)


def test_dirty_git_worktree_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()

    def dirty_status(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=" M backend/api.py\n", stderr="")

    monkeypatch.setattr(subprocess, "run", dirty_status)

    with pytest.raises(RuntimeError, match="dirty Git worktree"):
        validate_source_snapshot(tmp_path)


def test_clean_git_worktree_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()

    def clean_status(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", clean_status)

    validate_source_snapshot(tmp_path)
