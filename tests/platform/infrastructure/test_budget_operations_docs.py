from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS = tuple(sorted({
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    *(ROOT / "plan").rglob("*.md"),
    *(ROOT / "docs").rglob("*.md"),
}))


def markdown_anchors(source: str) -> set[str]:
    source = re.sub(r"```.*?```", "", source, flags=re.DOTALL)
    anchors = set(re.findall(r"(?:id|name)=[\"']([^\"']+)", source))
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+)$", source, re.MULTILINE):
        key = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        index = counts.get(key, 0)
        counts[key] = index + 1
        anchors.add(key + (f"-{index}" if index else ""))
    return anchors


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_budget_documentation_local_links_exist(document: Path) -> None:
    source = re.sub(r"```.*?```", "", document.read_text(), flags=re.DOTALL)
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        path = (document.parent / unquote(parsed.path)).resolve() if parsed.path else document
        assert path.is_relative_to(ROOT), f"{document}: link leaves repository: {target}"
        assert path.exists(), f"{document}: missing local link: {target}"
        if parsed.fragment and path.suffix == ".md":
            assert unquote(parsed.fragment) in markdown_anchors(path.read_text()), (
                f"{document}: missing local anchor: {target}"
            )


def test_budget_operations_runbook_references_maintained_sources_and_tests() -> None:
    source = (ROOT / "plan/aigateway-proxy-client/operations.md").read_text()
    tools = re.findall(r"`(scripts/[a-z_]+\.py)`", source)
    assert tools
    for tool in tools:
        assert (ROOT / tool).is_file(), f"Runbook references an absent tool: {tool}"
    tests = re.findall(r"tests/[\w/]+\.py", source)
    assert tests
    for test in tests:
        assert (ROOT / test).is_file(), f"Runbook references an absent test: {test}"


def test_budget_operations_keeps_one_section_per_topic() -> None:
    source = (ROOT / "plan/aigateway-proxy-client/operations.md").read_text()
    headings = re.findall(r"^## (.+)$", source, re.MULTILINE)
    topics = [heading.casefold() for heading in headings]
    assert len(topics) == len(set(topics)), "Merge repeated topics instead of appending snapshots"


@pytest.mark.parametrize(("module", "argument"), [
    ("scripts.prepare_budget_execution", "--configuration"),
    ("scripts.budget_execution_upgrade", "--current-parent"),
    ("scripts.install_budget_execution", "--plan"),
])
def test_documented_budget_cli_works_without_a_pythonpath_override(
    module: str, argument: str,
) -> None:
    source = (ROOT / "plan/aigateway-proxy-client/operations.md").read_text()
    assert f"uv run python -m {module} --help" in source
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout and argument in result.stdout


def test_directory_deployment_guard_points_to_the_migrated_runbook() -> None:
    runbook = "plan/organization-management/operations.md"
    assert "### Compatible Runtime Updates" in (ROOT / runbook).read_text()
    deploy = (ROOT / "scripts/deploy.py").read_text()
    assert f"Follow {runbook} Compatible Runtime Updates." in deploy
