from __future__ import annotations

import re

from backend.migrate import migration_files
from tests.support.paths import REPOSITORY_ROOT

MIGRATIONS = REPOSITORY_ROOT / "migrations"
INITIAL_SCHEMA = MIGRATIONS / "001_initial_schema.up.sql"


def schema() -> str:
    return INITIAL_SCHEMA.read_text(encoding="utf-8")


def table_names(sql: str) -> set[str]:
    return set(
        re.findall(
            r"^CREATE TABLE (?:public\.)?([a-z_]+)\s*\(",
            sql,
            flags=re.MULTILINE,
        )
    )


def test_migration_chain_preserves_clean_install_and_adds_attempt_identity() -> None:
    assert [path.name for path in migration_files(MIGRATIONS)] == [
        "001_initial_schema.up.sql",
        "002_apim_request_attempt_identity.up.sql",
        "003_budget_reservation_finalization.up.sql",
        "004_apim_usage_identity_guard.up.sql",
        "005_billable_request_lifecycle.up.sql",
        "006_versioned_budget_evidence.up.sql",
        "007_model_price_source.up.sql",
        "008_price_review_and_guard.up.sql",
        "009_org_units.up.sql",
        "010_application_attribution.up.sql",
    ]
    assert not list(MIGRATIONS.glob("*.down.sql"))


def test_the_review_baseline_and_the_pending_price_are_separate_columns() -> None:
    """007 kept one set of list_* columns and wrote them on every run, including the runs that
    refused the price. The refusal then approved itself on the next run by comparing the figure
    with the baseline it had just moved. 008 gives the proposal its own home."""
    sql = (MIGRATIONS / "008_price_review_and_guard.up.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN pending_list_price JSONB" in sql
    assert "list_input_cost_per_million" not in sql, (
        "the accepted baseline keeps the meaning it already had; only the proposal is new"
    )
    assert "'superseded'::text" in sql


def test_attempt_identity_upgrade_preserves_existing_usage() -> None:
    sql = (MIGRATIONS / "002_apim_request_attempt_identity.up.sql").read_text(encoding="utf-8")

    assert "CREATE UNIQUE INDEX token_usage_request_id_idx" in schema()
    assert "DROP INDEX public.token_usage_request_id_idx;" in sql
    assert "CREATE INDEX token_usage_request_id_idx" in sql
    assert "ON public.token_usage (request_id, ts DESC);" in sql
    assert "COMMENT ON COLUMN public.token_usage.request_id" in sql
    assert "COMMENT ON COLUMN public.token_usage.correlation_id" in sql
    assert "CREATE UNIQUE INDEX" not in sql
    assert "DROP TABLE" not in sql
    assert not re.search(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE)\b", sql, re.MULTILINE)


def test_initial_schema_excludes_router_and_historical_ledger_objects() -> None:
    sql = schema().lower()

    assert "model_router" not in sql
    assert "router_id" not in sql
    assert "router_name" not in sql
    assert "create table public.schema_migration" not in sql
    assert "alter table only public.schema_migration" not in sql
    assert "copy public.schema_migration" not in sql


def test_ledger_upgrade_is_append_only_and_does_not_rewrite_requests() -> None:
    sql = (MIGRATIONS / "003_budget_reservation_finalization.up.sql").read_text(encoding="utf-8")
    assert table_names(sql) == {
        "budget_reservation_finalization",
        "gateway_application_ledger_state",
    }
    assert "BEFORE UPDATE OR DELETE ON budget_reservation_finalization" in sql
    assert "total_tokens = reservation_tokens" in sql
    assert "WHEN 'exact_usage' THEN 3 WHEN 'terminal_zero' THEN 2 ELSE 1 END DESC" in sql
    assert "CREATE VIEW budget_scope_usage" in sql
    assert "CREATE VIEW budget_reservation_recovery" in sql
    assert "stream_cache_usage_unavailable" in sql
    assert "pending_reserved_tokens - finalized_upper_bound_tokens" in sql
    assert not re.search(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.MULTILINE)


def test_initial_schema_contains_no_customer_or_model_seed_data() -> None:
    sql = schema()

    assert "COPY public." not in sql
    for table in (
        "app_user",
        "managed_model",
        "model_provider",
        "model_runtime",
        "token_usage",
        "user_model_access",
    ):
        assert f"INSERT INTO public.{table}" not in sql
        assert f"INSERT INTO {table}" not in sql


def test_identity_upgrade_is_guarded_without_historical_seeding() -> None:
    sql = (MIGRATIONS / "004_apim_usage_identity_guard.up.sql").read_text()
    assert table_names(sql) == {"apim_usage_identity", "apim_usage_discrepancy"}
    assert "pg_advisory_xact_lock" in sql and "ORDER BY id LIMIT 2" in sql
    assert "APIM attempt identity is immutable" in sql
    assert "APIM correlation identity is ambiguous" in sql
    assert "BEFORE INSERT OR UPDATE ON token_usage" in sql
    assert "BEFORE UPDATE OR DELETE ON apim_usage_discrepancy" in sql
    assert "INSERT INTO token_usage" not in sql
    assert "UPDATE token_usage" not in sql


def test_billable_schema_preserves_request_intent_and_exact_acknowledgement() -> None:
    sql = (MIGRATIONS / "005_billable_request_lifecycle.up.sql").read_text()
    assert table_names(sql) == {"billable_request_attempt"}
    assert "UNIQUE NULLS NOT DISTINCT (operation_key, authorization_id)" in sql
    assert "CHECK ((state = 'exact') = (actual_tokens IS NOT NULL))" in sql
    assert "usage.request_id = attempt.id::text" in sql
    assert "usage.model_id = attempt.model_id" in sql
    assert "usage.correlation_id = attempt.correlation_id" in sql
    assert "OR OLD.state = 'exact'" in sql
    assert "budget_ordinary_usage_v1" in sql


def test_evidence_schema_is_future_only_and_keeps_public_legacy_branch() -> None:
    sql = (MIGRATIONS / "006_versioned_budget_evidence.up.sql").read_text()
    assert "VALUES (2, NULL)" in sql
    assert "OLD.effective_at IS NOT NULL" in sql
    assert "NEW.effective_at < clock_timestamp()" in sql
    assert "BEFORE UPDATE OR DELETE ON budget_reservation_admission" in sql
    assert "FROM budget_ordinary_usage_v1 usage" in sql
    assert "evidence_rank DESC, received_at, evidence_order, evidence_key" in sql
    assert "CREATE VIEW budget_evidence_conflicts" in sql
    assert "usage.ingest_error IS NULL THEN 3" in sql
    assert "2, updated_at" in sql
    assert "usage.ts, usage.organization_id, usage.department_id" in sql
    assert "AT TIME ZONE 'UTC'" in sql
    assert "UPDATE token_usage" not in sql


def test_initial_schema_seeds_only_the_platform_apim_gateway() -> None:
    sql = schema()

    assert sql.count("INSERT INTO public.gateway_profile") == 1
    assert "Azure API Management" in sql
    assert "'apim'" in sql
    assert '"header_name": "Ocp-Apim-Subscription-Key"' in sql
    assert "LiteLLM" not in sql


def test_initial_schema_contains_current_platform_tables() -> None:
    tables = table_names(schema())
    expected = {
        "anomaly_rule",
        "app_user",
        "assistant_conversation",
        "assistant_setting",
        "assistant_turn",
        "apim_cache_read_hourly",
        "copilot_connection",
        "copilot_oauth_setting",
        "copilot_oauth_state",
        "gateway_application",
        "gateway_application_audit",
        "gateway_application_avatar",
        "gateway_application_budget",
        "gateway_application_model_access",
        "gateway_application_model_policy",
        "gateway_application_subscription",
        "gateway_profile",
        "gateway_publication",
        "gateway_release_operation",
        "gateway_release_operation_audit",
        "gateway_release_operation_secret",
        "gateway_release_protection",
        "managed_model",
        "model_provider",
        "model_runtime",
        "token_budget",
        "token_usage",
        "token_usage_application_attribution",
        "user_model_access",
        "user_session",
    }

    assert expected <= tables


def test_initial_schema_preserves_key_integrity_constraints() -> None:
    sql = schema()

    assert "model_runtime_gateway_foundry_project_unique_idx" in sql
    assert "gateway_release_operation_one_open_idx" in sql
    assert "UNIQUE (gateway_profile_id, apim_subscription_id)" in sql
    assert "PRIMARY KEY (period_start, application_id)" in sql
    assert "actor_type IN ('person', 'service', 'system')" in sql
    assert "usage_domain IN ('apim', 'github_copilot')" in sql
    assert "octet_length(image_bytes) BETWEEN 1 AND 65536" in sql
    assert "credential_ciphertext BYTEA NOT NULL" in sql
    assert "primary_key" not in sql
    assert "secondary_key" not in sql
