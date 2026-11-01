from __future__ import annotations

import re

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
    assert "Model Intelligent Router" in readme


def test_repository_documents_one_database_entry_point() -> None:
    migrations = sorted((REPOSITORY_ROOT / "migrations").glob("*.up.sql"))
    documents = [
        REPOSITORY_ROOT / "README.md",
        *sorted((REPOSITORY_ROOT / "docs").glob("*.md")),
    ]

    assert migrations[0].name == "001_initial_schema.up.sql"
    testing = (REPOSITORY_ROOT / "docs/testing.md").read_text(encoding="utf-8")
    assert "uv run python -m backend.migrate" in testing
    for migration in migrations:
        assert migration.name.removesuffix(".up.sql") in testing
    for path in documents:
        text = path.read_text(encoding="utf-8")
        assert "database-baseline" not in text
        assert "migration-manifest" not in text


def test_clean_install_verification_lists_every_migration() -> None:
    testing = (REPOSITORY_ROOT / "docs/testing.md").read_text(encoding="utf-8")
    clean_install = next(
        line for line in testing.splitlines() if line.startswith("For a clean installation")
    )

    assert re.findall(r"`(\d{3}_[a-z0-9_]+)`", clean_install) == sorted(
        path.name.removesuffix(".up.sql")
        for path in (REPOSITORY_ROOT / "migrations").glob("*.up.sql")
    )


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
    testing = (REPOSITORY_ROOT / "docs/testing.md").read_text()
    for version in (
        "017_budget_terminal_observation", "018_budget_reservation_resolution",
        "019_runtime_display_name", "020_failed_publication_spec", "022_subscription_tags",
    ):
        assert version in testing
    assert "existing usage, financial/audit history" in testing
    assert "SQL source checks do not prove applied migrations" in testing
    assert "Do not initialize a shared database" in testing


def test_image_upgrade_documents_independent_defaults_and_validation_limits() -> None:
    configuration = (REPOSITORY_ROOT / "docs/configuration.md").read_text()
    architecture = (REPOSITORY_ROOT / "docs/architecture.md").read_text()
    testing = (REPOSITORY_ROOT / "docs/testing.md").read_text()
    assert (
        "Image generation is a standard capability and requires no enablement setting"
        in configuration
    )
    assert "effective_at = NULL" in configuration
    assert configuration.count("## Runtime attribution") == 1
    assert architecture.count("## Ledger finalization") == 1
    assert "SQL source checks cannot prove" in testing
    assert "separately authorized real targets" in testing


def test_model_availability_documents_installation_and_evidence_boundaries() -> None:
    configuration = (REPOSITORY_ROOT / "docs/configuration.md").read_text()
    deployment = " ".join((REPOSITORY_ROOT / "docs/deployment.md").read_text().split())
    testing = (REPOSITORY_ROOT / "docs/testing.md").read_text()
    assert "202` response" in configuration and "not effective" in configuration
    assert "Ordinary model updates cannot change `enabled`" in configuration
    assert "every retained Revision" in deployment
    assert "Publisher remains unable" in deployment
    assert "do not" in deployment and "grant those rights to the ordinary Publisher" in deployment
    assert "does not provide a generic controller" in deployment
    assert "enrolled_nodes_only_guarded_bootstrap" in deployment
    assert "016_model_availability" in testing
    assert "do not prove PostgreSQL transaction isolation" in testing
