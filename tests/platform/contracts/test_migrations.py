from __future__ import annotations

import hashlib
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
        "009_assistant_api_format.up.sql",
        "009_context_tier_pricing.up.sql",
        "010_budget_execution_protocol.up.sql",
        "010_models_dev_pricing.up.sql",
        "011_gateway_targets.up.sql",
        "011_user_settings_profile.up.sql",
        "012_apim_onboarding.up.sql",
        "012_organization_directory.up.sql",
        "013_menu_permission_groups.up.sql",
        "013_multi_target_rollout.up.sql",
        "014_multiple_menu_permission_groups.up.sql",
        "014_self_hosted_gateway_provisioning.up.sql",
        "015_platform_invocation_trace.up.sql",
        "015_scoped_menu_administrators.up.sql",
        "016_model_availability.up.sql",
        "016_organization_members.up.sql",
        "017_budget_terminal_observation.up.sql",
        "017_department_memberships.up.sql",
        "018_budget_reservation_resolution.up.sql",
        "018_permission_management.up.sql",
        "019_client_user_authorization.up.sql",
        "019_runtime_display_name.up.sql",
        "020_failed_publication_spec.up.sql",
        "020_plugin_download_permissions.up.sql",
        "021_usd_budget_reservations.up.sql",
        "022_subscription_tags.up.sql",
        "023_usd_reservation_resolution.up.sql",
        "024_gateway_usage_attribution.up.sql",
        "025_usage_cost_component_prices.up.sql",
        "026_invocation_trace_service_identity.up.sql",
        "027_product_schema_alignment.up.sql",
        "028_product_index_alignment.up.sql",
    ]
    assert not list(MIGRATIONS.glob("*.down.sql"))


def test_assistant_api_upgrade_defaults_to_automatic_without_rewriting_settings() -> None:
    sql = (MIGRATIONS / "009_assistant_api_format.up.sql").read_text()
    assert "ADD COLUMN api_format TEXT" in sql
    assert "'openai_chat', 'openai_responses', 'anthropic_messages'" in sql
    assert "NULL selects" in sql
    assert not re.search(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.MULTILINE)


def test_user_settings_upgrade_is_additive_and_keeps_private_data_bounded() -> None:
    sql = (MIGRATIONS / "011_user_settings_profile.up.sql").read_text(encoding="utf-8")
    assert hashlib.sha256(sql.encode()).hexdigest() == (
        "c830385df79834f2730450365cc2fe3b590f325ea2ca550fd0906c4579747fa5"
    )
    assert table_names(sql) == {"app_user_avatar", "account_security_event"}
    assert sql.count("REFERENCES app_user(id) ON DELETE CASCADE") == 2
    assert "octet_length(image_bytes) BETWEEN 1 AND 65536" in sql
    assert "media_type IN ('image/png', 'image/jpeg', 'image/webp')" in sql
    assert "ON account_security_event(app_user_id, action, created_at)" in sql
    assert not re.search(r"^\s*(ALTER|INSERT|UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.MULTILINE)


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


def test_final_product_indexes_keep_source_keys_and_do_not_rewrite_history() -> None:
    sql = (MIGRATIONS / "028_product_index_alignment.up.sql").read_text()
    assert "pinned_chart" not in sql
    assert "CREATE INDEX pinned_chart_report_position_idx" in schema()
    assert "CREATE UNIQUE INDEX pinned_chart_report_query_idx" in schema()
    assert "scope_type, scope_id, period_start, reservation_created_at DESC" in sql
    assert "ON gateway_release_operation ((semantic_preview->>'request_id'))" in sql
    assert "WHERE operation_kind = 'self_hosted_gateway_provision'" in sql
    assert not re.search(r"^\s*(UPDATE|DELETE FROM|TRUNCATE|DROP|ALTER|INSERT INTO)\b",
                         sql, re.MULTILINE)
    assert "model_router" not in sql


def test_final_product_schema_alignment_adds_missing_sidecars_without_rewriting_history() -> None:
    sql = (MIGRATIONS / "027_product_schema_alignment.up.sql").read_text()
    assert table_names(sql) == {
        "gateway_application_usage_recovery",
        "gateway_application_ledger_finalization_state",
        "gateway_deployment_provisioning",
    }
    assert "BEFORE UPDATE OR DELETE ON gateway_application_usage_recovery" in sql
    assert "evidence_at >= occurred_at" in sql
    assert "PRIMARY KEY (application_id, correlation_id)" in sql
    assert "finalized_upper_bound_tokens >= 0" in sql
    assert "FOREIGN KEY (operation_id, apim_target_id)" in sql
    assert "FOREIGN KEY (deployment_id, apim_target_id)" in sql
    assert "ON DELETE RESTRICT" in sql and "model_router" not in sql
    assert "CREATE OR REPLACE VIEW budget_reservation_finalization_effective" in sql
    assert "FROM gateway_application_usage_recovery recovery" in sql
    assert "SELECT DISTINCT ON (scope_type, scope_id, correlation_id)" in sql
    assert "recovery.recovered_at AS finalized_at" in sql
    assert "CREATE OR REPLACE VIEW invocation_request_trace" in sql
    assert "gateway.native_instance_id, gateway.is_managed, gateway.apim_region" in sql
    assert "LEFT JOIN token_usage_gateway_attribution gateway ON gateway.usage_id = usage.id" in sql
    assert "LEFT JOIN platform_invocation_usage_match matched ON matched.usage_id = usage.id" in sql
    assert not re.search(r"^\s*(UPDATE|DELETE FROM|TRUNCATE|DROP|ALTER|INSERT INTO)\b",
                         sql, re.MULTILINE)


def test_current_snapshot_cost_and_trace_upgrades_do_not_rewrite_usage_history() -> None:
    cost = (MIGRATIONS / "025_usage_cost_component_prices.up.sql").read_text()
    trace = (MIGRATIONS / "026_invocation_trace_service_identity.up.sql").read_text()
    assert "CREATE OR REPLACE FUNCTION budget_usage_usd_cost" in cost
    assert "RETURNS NUMERIC(30,14)" in cost
    assert "WHEN is_estimated IS DISTINCT FROM FALSE THEN NULL" in cost
    assert "WHEN stored_cost IS NOT NULL THEN stored_cost::NUMERIC(30,14)" in cost
    assert "(input_tokens = 0 OR input_rate IS NOT NULL)" in cost
    assert "(output_tokens = 0 OR output_rate IS NOT NULL)" in cost
    assert "input_tokens * COALESCE(input_rate, 0)" in cost
    assert "CREATE OR REPLACE VIEW public.platform_invocation_usage_match" in trace
    assert "attempt.completion->>'gateway_correlation_id' IS NOT NULL" in trace
    assert "usage.correlation_id = attempt.completion->>'gateway_correlation_id'" in trace
    assert "gateway.apim_target_id::text = attempt.snapshot->>'apim_target_id'" in trace
    assert (
        "gateway.gateway_deployment_id::text = attempt.snapshot->>'gateway_deployment_id'" in trace
    )
    assert "lower(gateway.apim_service_id)" in trace and "regexp_replace(" in trace
    assert "usage.runtime =" not in trace
    assert "HAVING count(*) = 1" in trace
    for sql in (cost, trace):
        assert not re.search(
            r"^\s*(UPDATE|DELETE FROM|TRUNCATE|DROP|INSERT INTO)\b", sql, re.MULTILINE,
        )
        assert "model_router" not in sql


def test_terminal_observation_and_resolution_are_immutable_admission_bound_records() -> None:
    observation = (MIGRATIONS / "017_budget_terminal_observation.up.sql").read_text()
    resolution = (MIGRATIONS / "018_budget_reservation_resolution.up.sql").read_text()
    assert table_names(observation) == {"budget_execution_observation"}
    assert table_names(resolution) == {"budget_execution_resolution"}
    for sql in (observation, resolution):
        assert "REFERENCES budget_execution_attempt" in sql
        assert "reject_budget_reservation_finalization_mutation()" in sql
        assert "BEFORE UPDATE OR DELETE" in sql
        assert not re.search(r"^\s*(UPDATE|DELETE FROM|TRUNCATE|INSERT INTO)\b", sql, re.MULTILINE)
    assert "octet_length(observation::text) <= 4096" in observation
    assert "document->>'decision' = 'release_unverified'" in resolution
    assert "document #>> '{receipt,identity,scope,scope_id}' = scope_id" in resolution
    assert "UNIQUE (scope_type, scope_id, period_start, attempt_key)" in resolution


def test_gateway_usage_sidecar_is_forward_only_and_preserves_old_attempt_identity() -> None:
    sql = (MIGRATIONS / "024_gateway_usage_attribution.up.sql").read_text()
    assert table_names(sql) == {"token_usage_gateway_attribution"}
    assert "usage_id TEXT PRIMARY KEY REFERENCES token_usage(id) ON DELETE RESTRICT" in sql
    assert "REFERENCES gateway_deployment(id, apim_target_id) ON DELETE RESTRICT" in sql
    assert "event_schema_version >= 2" in sql
    assert "lower(native_gateway_id) = 'managed'" in sql
    assert "'eventhub', 'observer', 'reconciliation', 'backfill'" in sql
    assert "CREATE INDEX token_usage_gateway_attribution_target_idx" in sql
    assert not re.search(r"^\s*(UPDATE|DELETE FROM|TRUNCATE|DROP|INSERT INTO)\b", sql, re.MULTILINE)
    assert "guard_apim_usage_identity" not in sql and "model_router" not in sql


def test_runtime_display_names_and_failed_publication_identity_keep_other_constraints() -> None:
    names = (MIGRATIONS / "019_runtime_display_name.up.sql").read_text()
    publications = (MIGRATIONS / "020_failed_publication_spec.up.sql").read_text()
    assert names.strip() == "ALTER TABLE model_runtime DROP CONSTRAINT model_runtime_name_key;"
    assert "model_runtime_gateway_foundry_project_unique_idx" not in names
    assert "WHERE status NOT IN ('failed', 'superseded', 'rolled_back')" in publications
    assert not re.search(r"^\s*(UPDATE|DELETE FROM|TRUNCATE|INSERT INTO)\b", publications,
                         re.MULTILINE)


def test_subscription_tags_are_separate_and_cannot_delete_application_history() -> None:
    sql = (MIGRATIONS / "022_subscription_tags.up.sql").read_text()
    assert table_names(sql) == {"gateway_application_tags"}
    assert "application_id UUID PRIMARY KEY" in sql
    assert "REFERENCES gateway_application(id) ON DELETE RESTRICT" in sql
    assert "jsonb_typeof(tags) = 'object'" in sql
    assert "token_usage" not in sql and "gateway_application_budget" not in sql


def test_usd_upgrade_keeps_exact_price_evidence_and_public_legacy_snapshot_balance() -> None:
    sql = (MIGRATIONS / "021_usd_budget_reservations.up.sql").read_text()
    assert "budget_mode TEXT NOT NULL DEFAULT 'tokens'" in sql
    assert "NUMERIC(30,14)" in sql and "price_snapshot JSONB" in sql
    assert "budget_execution_attempt_mode_evidence_check" in sql
    assert "CREATE FUNCTION budget_usage_usd_cost" in sql
    assert "GREATEST(cached_tokens - cache_write_tokens, 0) * cache_read_rate" in sql
    assert "WHEN count(*) FILTER (WHERE usd_cost IS NULL) > 0 THEN NULL" in sql
    assert "usage.usd_price_sha256 = admitted_price->>'price_sha256'" in sql
    assert "SELECT ledger_constraint.conname INTO STRICT available_constraint_name" in sql
    assert "ledger_constraint.conrelid = 'gateway_application_ledger_state'::regclass" in sql
    assert "ledger_constraint.contype = 'c'" in sql
    assert "ledger_column.attname = 'available_tokens'" in sql
    assert "ledger_column.attnum = ANY(ledger_constraint.conkey)" in sql
    assert "ALTER TABLE gateway_application_ledger_state DROP CONSTRAINT %I" in sql
    assert "DROP CONSTRAINT IF EXISTS" not in sql
    assert "- finalized_upper_bound_tokens, 0" in sql
    assert "WHERE EXCLUDED.evidence_rank > stored.evidence_rank" in sql
    assert "UPDATE token_usage" not in sql and "DELETE FROM token_usage" not in sql
    assert "model_router" not in sql


def test_usd_resolution_retains_immutable_approval_and_unknown_actual_cost() -> None:
    sql = (MIGRATIONS / "023_usd_reservation_resolution.up.sql").read_text()
    assert "source_key LIKE 'resolution:%'" in sql
    assert "resolution.document->>'decision' = 'release_unverified'" in sql
    assert "resolution.document #> '{receipt,identity}' = execution.identity" in sql
    assert "resolution.document #> '{receipt,price_snapshot}' = admitted_price" in sql
    assert "= execution.reserved_usd" in sql
    assert "actual_usd := NULL" in sql
    assert "WHERE EXCLUDED.evidence_rank > stored.evidence_rank" in sql
    assert "UPDATE token_usage" not in sql and "DELETE FROM" not in sql


def test_availability_migration_guards_versioned_writes_and_retains_identities() -> None:
    sql = (MIGRATIONS / "016_model_availability.up.sql").read_text()
    assert table_names(sql) == {
        "model_availability_identity", "model_availability_capability",
        "model_availability_node_enrollment", "model_availability_operation",
        "model_availability_task", "model_availability_audit",
    }
    assert "operation.accepted_xid = txid_current()" in sql
    assert "UNIQUE (model_id, desired_revision)" in sql
    assert "idempotency_key UUID NOT NULL UNIQUE" in sql
    assert "Use the versioned model availability command" in sql
    assert "Model availability history is immutable" in sql
    assert "A retained model availability identity cannot be reused" in sql
    assert "FOREIGN KEY (deployment_id, target_id)" in sql
    assert "WHERE model_id = OLD.id FOR UPDATE" in sql
    assert "SELECT id, 'active' FROM managed_model" in sql
    assert "CREATE FUNCTION model_availability_inventory()" in sql
    assert "token_usage" not in sql and "managed_model_price" not in sql
    assert not re.search(
        r"^\s*(UPDATE|DELETE FROM|TRUNCATE|DROP) managed_model\b", sql, re.MULTILINE,
    )


def test_platform_trace_migration_is_nonbilling_and_requires_unique_real_correlation() -> None:
    sql = (MIGRATIONS / "015_platform_invocation_trace.up.sql").read_text()
    assert table_names(sql) == {"platform_invocation_attempt"}
    assert "CREATE VIEW public.invocation_request_trace" in sql
    assert "usage.correlation_id = attempt.completion->>'gateway_correlation_id'" in sql
    assert "HAVING count(*) = 1" in sql
    assert "usage.user_id = attempt.snapshot->'metadata'->>'user_id'" in sql
    assert "usage.model_id = attempt.snapshot->>'model_id'" in sql
    assert "usage.runtime = attempt.snapshot->>'runtime_name'" in sql
    assert "application.application_id::text = attempt.snapshot->>'application_id'" in sql
    assert "platform_usage_unavailable" in sql
    assert not re.search(r"^(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER)\b", sql, re.MULTILINE)


def test_gateway_creation_upgrade_binds_request_and_target_without_rewriting_history() -> None:
    sql = (MIGRATIONS / "014_self_hosted_gateway_provisioning.up.sql").read_text()
    assert table_names(sql) == set()
    assert "'self_hosted_gateway_provision'" in sql
    assert "(gateway_profile_id, (semantic_preview->>'request_id'))" in sql
    assert "FOREIGN KEY (provisioning_operation_id, apim_target_id)" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "provisioning_operation_id IS NULL OR kind = 'self_hosted'" in sql
    assert not re.search(r"^(INSERT|UPDATE|DELETE|TRUNCATE|DROP TABLE)\b", sql, re.MULTILINE)


def test_multi_target_upgrade_preserves_history_and_binds_child_publication_target() -> None:
    sql = (MIGRATIONS / "013_multi_target_rollout.up.sql").read_text()
    assert table_names(sql) == {"multi_target_rollout", "multi_target_rollout_child"}
    assert "request_sha256 TEXT NOT NULL UNIQUE" in sql
    assert "PRIMARY KEY (rollout_id, apim_target_id)" in sql
    assert "publication_target <> NEW.apim_target_id" in sql
    assert "ON DELETE RESTRICT" in sql
    assert not re.search(r"^(INSERT|UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.MULTILINE)


def test_onboarding_upgrade_only_extends_durable_operation_kind() -> None:
    sql = (MIGRATIONS / "012_apim_onboarding.up.sql").read_text()
    assert table_names(sql) == set()
    assert "'target_inventory_sync', 'apim_onboarding'" in sql
    assert "DROP CONSTRAINT gateway_release_operation_operation_kind_check" in sql
    assert not re.search(r"^(INSERT|UPDATE|DELETE|TRUNCATE|DROP TABLE)\b", sql, re.MULTILINE)


def test_gateway_target_migration_keeps_history_and_cross_target_constraints() -> None:
    sql = (MIGRATIONS / "011_gateway_targets.up.sql").read_text()
    assert table_names(sql) == {
        "apim_target_metadata", "gateway_deployment", "gateway_instance_observation",
        "apim_target_route_binding", "apim_target_route_binding_deployment",
        "gateway_deployment_inventory", "gateway_deployment_api_association",
        "gateway_deployment_hostname_inventory", "gateway_deployment_ca_inventory",
    }
    assert "FOREIGN KEY (gateway_deployment_id, apim_target_id)" in sql
    assert "FOREIGN KEY (route_binding_id, apim_target_id)" in sql
    assert "WHERE is_default_serving" in sql
    assert "CHECK (enabled OR NOT is_default_serving)" in sql
    assert "'target_inventory_sync'" in sql
    assert "token_usage" not in sql
    assert not re.search(r"^(INSERT|UPDATE|DELETE|TRUNCATE)\b", sql, re.MULTILINE)


def test_execution_upgrade_is_default_off_and_preserves_evidence_history() -> None:
    sql = (MIGRATIONS / "010_budget_execution_protocol.up.sql").read_text()
    assert table_names(sql) == {
        "budget_execution_rollout", "budget_execution_scope", "budget_execution_dispatch",
        "budget_execution_attempt", "budget_execution_evidence", "budget_settlement_task",
    }
    assert "scope_sync_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "DEFAULT 'prepared'" in sql
    assert "WHERE EXCLUDED.evidence_rank > stored.evidence_rank" in sql
    assert "IF evidence_id IS NULL OR source_rank = 1 THEN RETURN" in sql
    assert "legacy_fence_verified_at IS NOT NULL" in sql
    assert "reject_budget_reservation_finalization_mutation()" in sql
    assert "gateway_profile_id = ANY(rollout.gateway_profile_ids)" in sql
    assert not re.search(r"^(INSERT INTO|UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.MULTILINE)


def test_context_tier_upgrade_keeps_rules_in_price_metadata_and_preserves_history() -> None:
    sql = (MIGRATIONS / "009_context_tier_pricing.up.sql").read_text()
    assert "ALTER TABLE public.managed_model_price" in sql
    assert "ADD COLUMN context_price_tiers JSONB" in sql
    assert "ADD COLUMN pending_context_price_tiers JSONB" in sql
    assert "ADD COLUMN price_context_snapshot JSONB" in sql
    assert "ALTER TABLE public.managed_model\n" not in sql
    assert not re.search(r"^\s*(UPDATE|DELETE|TRUNCATE|DROP)\b", sql, re.MULTILINE)
