from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from turnstile_core.config import core_runtime_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("api", "telemetry", "control-plane")
IGNORED_SOURCE_FILES = shutil.ignore_patterns(
    "__pycache__",
    "*.py[cod]",
    ".DS_Store",
    "tests",
)


def _copy_file(root: Path, source: str, destination: Path, name: str | None = None) -> None:
    path = root / source
    if not path.is_file():
        raise RuntimeError(f"Required deployment file is missing: {source}")
    shutil.copy2(path, destination / (name or path.name))


def _copy_tree(root: Path, source: str, destination: Path) -> None:
    path = root / source
    if not path.is_dir():
        raise RuntimeError(f"Required deployment directory is missing: {source}")
    shutil.copytree(path, destination / Path(source).name, ignore=IGNORED_SOURCE_FILES)


def _prepare_destination(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise RuntimeError(f"Deployment staging directory must be empty: {destination}")


def validate_source_snapshot(root: Path) -> None:
    if not (root / ".git").exists():
        return
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", "."],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise RuntimeError(
            "Deployment source is a dirty Git worktree; stage from a clean checkout or git archive"
        )


def _stage_api(root: Path, destination: Path) -> None:
    if not (root / "frontend/dist/index.html").is_file():
        raise RuntimeError("Frontend build is missing: run npm --prefix frontend run build")
    migrations = sorted((root / "migrations").glob("[0-9][0-9][0-9]_*.up.sql"))
    if not migrations:
        raise RuntimeError("Migration chain is missing from the repository")

    _copy_tree(root, "backend", destination)
    _copy_tree(root, "turnstile_core", destination)
    _copy_tree(root, "migrations", destination)
    (destination / "scripts").mkdir()
    _copy_file(root, "scripts/__init__.py", destination / "scripts")
    _copy_file(root, "scripts/directory_upgrade.py", destination / "scripts")
    (destination / "frontend").mkdir()
    shutil.copytree(root / "frontend/dist", destination / "frontend/dist")
    for source in ("requirements.txt", "pyproject.toml", "uv.lock"):
        _copy_file(root, source, destination)


def _stage_function(root: Path, destination: Path, project: str) -> None:
    source = f"functions/{project}"
    _copy_file(root, f"{source}/function_app.py", destination, "function_app.py")
    _copy_file(root, f"{source}/host.json", destination)
    _copy_file(root, f"{source}/requirements.txt", destination)
    _copy_tree(root, "turnstile_core", destination)

    if project == "control_plane":
        policy_dir = destination / "policies"
        policy_dir.mkdir()
        _copy_file(
            root,
            "infra/policies/foundry-finops-policy.xml",
            policy_dir,
        )


def stage_deployment(target: str, destination: Path, root: Path = REPOSITORY_ROOT) -> None:
    if target not in TARGETS:
        raise ValueError(f"Unknown deployment target: {target}")
    _prepare_destination(destination)
    if target == "api":
        _stage_api(root, destination)
    elif target == "telemetry":
        _stage_function(root, destination, "telemetry")
    else:
        _stage_function(root, destination, "control_plane")
    (destination / "directory-runtime-manifest.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "target": target,
                "core_digest": core_runtime_digest(destination / "turnstile_core"),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Stage one Turnstile deployment artifact without installing dependencies.",
    )
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source-root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    validate_source_snapshot(args.source_root)
    stage_deployment(args.target, args.destination, root=args.source_root)


if __name__ == "__main__":
    main()
