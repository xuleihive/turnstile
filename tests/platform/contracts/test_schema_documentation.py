from __future__ import annotations

import hashlib
import re

from PIL import Image

from backend.migrate import migration_files
from tests.support.paths import REPOSITORY_ROOT

EXPECTED_DOCUMENTS = {
    "README.md",
    "architecture.md",
    "configuration.md",
    "deployment.md",
    "e2e-validation.md",
    "security.md",
    "testing.md",
    "troubleshooting.md",
}


FORK_UPGRADE = REPOSITORY_ROOT / "plan/upstream-main-merge-2026-10-16.md"
DIRECTORY_OPERATIONS = REPOSITORY_ROOT / "plan/organization-management/operations.md"
BUDGET_OPERATIONS = REPOSITORY_ROOT / "plan/aigateway-proxy-client/operations.md"
USER_SETTINGS_DESIGN = REPOSITORY_ROOT / "plan/user-settings/design.md"

# Pin the reviewed upstream documentation without requiring Git or network access in CI.
UPSTREAM_DOCS_REVISION = 'e59177d61c27d3641fff86caed8fe476bacf858e'
UPSTREAM_DOCS_SHA256 = {
    'docs/README.md':
        'd6a1bb1d500d7d2c775dc6cf4e9cc732f2981009becdfaf1a9c169c3350db2ff',
    'docs/architecture.md':
        '4e7dd447a268463ce16555612f92633b8cd9c0974cd407daa695ddb9e4a9c95d',
    'docs/assets/product-overview.png':
        'e8411697e7b553c3ed1aa76d2fcd7de286e0fe93b4c1558fd202b5bf1b0e05c6',
    'docs/configuration.md':
        'e493f6f4c4ac80beba3a2cab56196ab35eda8a7ba3278f00a258961a8d04c61a',
    'docs/deployment.md':
        'aab049f025e6da75d1a7463ad98113e0501cbf44021c5026c553d9e82d2298c8',
    'docs/e2e-validation.md':
        'b2918bdb55d949151111a6debd65abc2ee5b3480f9eca4a050f670b3c508e483',
    'docs/security.md':
        '568f707095a1f2f01f0f4333ffceebe01ecba53eb2a57c15e3c26e935ca4f017',
    'docs/testing.md':
        'c649957cb4fc703904b34e11ea93d9128f290f768049dbef0784a64e44bfcc74',
    'docs/troubleshooting.md':
        '9fee23d8f71bf850c45656efdd8caa55a4ea29b98751ae57644897a9e22b67d3',
}

ENTERPRISE_GUIDES = {
    "README.md", "user-guide.md", "admin-guide.md", "architecture.md",
}
ENTERPRISE_SCREENSHOTS = {
    "magpie-installed.png", "magpie-provider.png", "magpie-usage.png",
    "magpie-workbuddy.png", "workbuddy-model.png", "platform-downloads.png",
    "platform-budgets.png", "platform-permissions.png", "platform-request-trace.png",
    "platform-request-detail.png", "magpie-routing.png",
}
ENTERPRISE_DOC_FILES = {
    *(f"docs/enterprise-plugin/{name}" for name in ENTERPRISE_GUIDES),
    *(f"docs/enterprise-plugin/assets/{name}" for name in ENTERPRISE_SCREENSHOTS),
}


def branch_deployment_guidance() -> str:
    return "\n".join(path.read_text() for path in (
        FORK_UPGRADE, BUDGET_OPERATIONS, USER_SETTINGS_DESIGN,
    ))


def test_docs_match_the_reviewed_upstream_main_baseline() -> None:
    paths = {path.relative_to(REPOSITORY_ROOT).as_posix(): path
             for path in (REPOSITORY_ROOT / "docs").rglob("*") if path.is_file()}
    assert paths.keys() == UPSTREAM_DOCS_SHA256.keys() | ENTERPRISE_DOC_FILES
    assert {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items() if name in UPSTREAM_DOCS_SHA256} == (
                UPSTREAM_DOCS_SHA256
            ), UPSTREAM_DOCS_REVISION


def test_enterprise_plugin_guides_link_real_screenshots_without_capture_metadata() -> None:
    root = REPOSITORY_ROOT / "docs/enterprise-plugin"
    documents = {name: (root / name).read_text() for name in ENTERPRISE_GUIDES}
    source = "\n".join(documents.values())
    images = set(re.findall(r"!\[[^\]]+\]\(assets/([^/)]+\.png)\)", source))
    assert images == ENTERPRISE_SCREENSHOTS
    assert "终端用户" in documents["README.md"] and "企业管理员" in documents["README.md"]
    for title in ("下载", "安装", "WorkBuddy", "个人预算", "常见问题"):
        assert title in documents["user-guide.md"]
    for title in ("权限管理", "预算管理", "请求追踪", "订阅密钥"):
        assert title in documents["admin-guide.md"]
    assert "flowchart LR" in documents["architecture.md"]
    assert not re.search(r"https?://|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|/Users/", source)
    assert "没有新增模型调用" in documents["README.md"]
    for name in ENTERPRISE_SCREENSHOTS:
        with Image.open(root / "assets" / name) as image:
            assert image.format == "PNG" and image.mode == "RGB"
            assert image.width >= 500 and image.height >= 300
            assert not image.info, f"Remove original capture metadata: {name}"


def test_documentation_is_delivery_focused() -> None:
    docs = REPOSITORY_ROOT / "docs"

    assert {path.name for path in docs.glob("*.md")} == EXPECTED_DOCUMENTS
    assert not (docs / "interactive").exists()
    assert not (REPOSITORY_ROOT / "database-baseline").exists()


def test_readme_covers_the_operator_journey() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    for heading in (
        "## Core capabilities",
        "## Architecture",
        "## Repository layout",
        "## Prerequisites",
        "## Local setup",
        "## Configuration",
        "## Development and testing",
        "## Deployment",
        "## Post-deployment model onboarding",
        "## Security",
        "## Known limitations",
        "## Troubleshooting",
    ):
        assert heading in readme
    assert "does not create an Azure AI Foundry project" in readme
    assert "Model Intelligent Router is intentionally not part" in readme


def test_repository_documents_one_database_entry_point() -> None:
    migrations = migration_files(REPOSITORY_ROOT / "migrations")
    documents = [
        REPOSITORY_ROOT / "README.md",
        *sorted((REPOSITORY_ROOT / "docs").glob("*.md")),
    ]

    assert migrations[0].name == "001_initial_schema.up.sql"
    testing = FORK_UPGRADE.read_text(encoding="utf-8")
    assert "uv run python -m backend.migrate" in testing
    for migration in migrations:
        assert migration.name.removesuffix(".up.sql") in testing
    for path in documents:
        text = path.read_text(encoding="utf-8")
        assert "database-baseline" not in text
        assert "migration-manifest" not in text


def test_documented_migration_inventory_matches_runner_order_and_count() -> None:
    testing = FORK_UPGRADE.read_text(encoding="utf-8")
    inventory = testing.split("### Migration inventory", 1)[1].split("\n### ", 1)[0]
    documented = re.findall(r"^\| `([0-9]{3}_[^`]+\.up\.sql)` \|", inventory, re.MULTILINE)
    expected = [path.name for path in migration_files(REPOSITORY_ROOT / "migrations")]

    assert documented == expected
    count = re.search(r"current chain contains \*\*([0-9]+) migrations\*\*", inventory)
    assert count is not None and int(count[1]) == len(expected)
    assert "full filename minus `.up.sql`" in " ".join(testing.split())
    assert "per numbered migration (currently" not in testing


def test_deployment_prerequisites_require_the_complete_candidate_chain() -> None:
    deployment = branch_deployment_guidance()
    normalized = " ".join(deployment.split())
    cutover = deployment.split("## Budget execution cutover", 1)[1].split("\n## ", 1)[0]

    assert "all pending migrations shipped with the compatible candidate" in normalized
    assert "#migration-inventory" in deployment
    assert "backend.migrate" in cutover
    for version in (
        "010_budget_execution_protocol",
        "017_budget_terminal_observation",
        "018_budget_reservation_resolution",
        "021_usd_budget_reservations",
        "023_usd_reservation_resolution",
        "025_usage_cost_component_prices",
    ):
        assert f"`{version}`" in cutover
    assert not re.search(r"\|[^\n]*\b[Mm]igrations? (?:through )?[0-9]{3}\b", deployment)
    assert "apply only migration" not in normalized
    assert "Apply only the approved new migration and" not in normalized
    assert "If `011` is already recorded" not in normalized


def test_pricing_upgrade_distinguishes_sql_defaults_from_absent_row_reading() -> None:
    testing = " ".join(FORK_UPGRADE.read_text().split())

    assert "`007_model_price_source` creates the side tables" in testing
    assert "`model_runtime_price` and `managed_model_price`" in testing
    assert "SQL column default remains `manual`" in testing
    assert "Explicit manual rows remain manual" in testing
    assert "absent row follows the default public catalog" in testing
    assert "neither migration backfills existing models or rewrites charged rates" in testing
    assert "only adds nullable columns" not in testing
    assert "defaults every existing model" not in testing


def test_bootstrap_and_directory_docs_use_candidate_migrations_not_old_counts() -> None:
    e2e = " ".join(FORK_UPGRADE.read_text().split())
    directory = " ".join(DIRECTORY_OPERATIONS.read_text().split())

    assert "backend.migrate" in e2e
    assert "#migration-inventory" in e2e
    assert "- [ ] Apply `001_initial_schema`" not in e2e
    assert "#migration-inventory" in directory
    assert "`018_permission_management`" in directory
    assert "every applied checksum" in directory
    assert "schemas 012-017" not in directory
    assert "require all 18 applied checksums" not in directory


def test_documentation_contains_no_original_environment_identifiers() -> None:
    documents = [
        REPOSITORY_ROOT / "README.md",
        *sorted((REPOSITORY_ROOT / "docs").glob("*.md")),
    ]
    forbidden = (
        "/Users/",
        "azurewebsites.net",
        "azure-api.net",
        "sebpvmm",
        "xle@",
        "admin-8382",
    )

    violations = {
        str(path.relative_to(REPOSITORY_ROOT)): token
        for path in documents
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }
    assert not violations


def test_product_upgrade_docs_preserve_history_and_require_real_database_evidence() -> None:
    testing = FORK_UPGRADE.read_text()
    for version in (
        "017_budget_terminal_observation", "018_budget_reservation_resolution",
        "019_runtime_display_name", "020_failed_publication_spec", "022_subscription_tags",
    ):
        assert version in testing
    assert "existing usage, financial/audit history" in testing
    assert "SQL source checks do not prove applied migrations" in testing
    assert "Do not initialize a shared database" in testing


def test_image_upgrade_documents_independent_defaults_and_validation_limits() -> None:
    configuration = (
        (REPOSITORY_ROOT / "docs/configuration.md").read_text() + FORK_UPGRADE.read_text()
    )
    architecture = (REPOSITORY_ROOT / "docs/architecture.md").read_text()
    testing = FORK_UPGRADE.read_text()
    deployment = " ".join(branch_deployment_guidance().split())
    assert (
        "`IMAGE_GENERATION_ENABLED` defaults to false"
        in configuration
    )
    assert "effective_at = NULL" in configuration
    assert configuration.count("## Runtime attribution") == 1
    assert architecture.count("## Ledger finalization") == 1
    assert "SQL source checks cannot prove" in testing
    assert "separately authorized real targets" in testing
    assert "`IMAGE_GENERATION_ENABLED` defaults to false" in deployment
    assert "Keep `IMAGE_GENERATION_ENABLED=false`" in deployment
    assert "no separate enablement switch" not in deployment
    assert "without an enablement setting" not in deployment


def test_model_availability_documents_installation_and_evidence_boundaries() -> None:
    configuration = (REPOSITORY_ROOT / "docs/configuration.md").read_text()
    deployment = " ".join((REPOSITORY_ROOT / "docs/deployment.md").read_text().split())
    testing = FORK_UPGRADE.read_text()
    assert "202` response" in configuration and "not effective" in configuration
    assert "Ordinary model updates cannot change `enabled`" in configuration
    assert "every retained Revision" in deployment
    assert "Publisher remains unable" in deployment
    assert "do not" in deployment and "grant those rights to the ordinary Publisher" in deployment
    assert "does not provide a generic controller" in deployment
    assert "enrolled_nodes_only_guarded_bootstrap" in deployment
    assert "016_model_availability" in testing
    assert "do not prove PostgreSQL transaction isolation" in testing
