from pathlib import Path

from tests.support.paths import REPOSITORY_ROOT

FRONTEND_COPILOT = REPOSITORY_ROOT / "frontend/src/data-sources/github-copilot"
FRONTEND_APIM = REPOSITORY_ROOT / "frontend/src/data-sources/apim"
BACKEND_COPILOT = REPOSITORY_ROOT / "backend/data_sources/github_copilot"


def source_text(root: Path, suffixes: tuple[str, ...]) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in suffixes
    )


def test_frontend_copilot_owns_its_api_queries_types_and_pages() -> None:
    copilot = source_text(FRONTEND_COPILOT, (".ts", ".tsx"))
    apim = source_text(FRONTEND_APIM, (".ts", ".tsx"))
    shared_api = source_text(REPOSITORY_ROOT / "frontend/src/api", (".ts", ".tsx"))

    for forbidden in (
        'from "../../api/data"',
        'from "../../api/queries"',
        'from "../../api/types"',
        "/api/v1/observability",
        "/api/v1/budgets",
    ):
        assert forbidden not in copilot
    for forbidden in (
        "/api/v1/copilot/dashboard",
        "/api/v1/copilot/governance",
        "CopilotDashboard",
        "CopilotGovernance",
    ):
        assert forbidden not in shared_api
        assert forbidden not in apim
    assert sorted(path.name for path in (REPOSITORY_ROOT / "frontend/src/api").iterdir()) == [
        "auth.ts",
        "cache-policies.ts",
        "client.ts",
        "permissions.ts",
        "user-settings.ts",
    ]


def test_backend_copilot_owns_business_and_http_layers() -> None:
    copilot = source_text(BACKEND_COPILOT, (".py",))
    composition_root = (REPOSITORY_ROOT / "backend/api.py").read_text(encoding="utf-8")

    for forbidden in (
        "from ...persistence.repository import",
        "from ...services.assistant import",
        "from ...services.assistant_tools import",
    ):
        assert forbidden not in copilot
    assert "from .data_sources.github_copilot.router import router" in composition_root
    assert "/api/v1/copilot/" not in composition_root
    for forbidden in (
        ".data_sources.github_copilot.contracts",
        ".data_sources.github_copilot.client",
        ".data_sources.github_copilot.store",
        ".data_sources.github_copilot.service",
    ):
        assert forbidden not in composition_root
