# ruff: noqa: E501 - keep large SQL projections vertically aligned by selected column
from __future__ import annotations

import threading
import time as clock
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..domain.anomaly_engine import evaluate_anomaly_rules
from ..domain.application_access import UsageApplicationAttribution
from ..domain.gateway_targets import UsageGatewayAttribution
from ..domain.ledger import LedgerAdmissionReceipt
from ..domain.models import (
    ApimCacheReadBucket,
    ModelIdentity,
    ModelPrice,
    ReconciledUsage,
    TokenUsageRecord,
)
from ..domain.runtime_models import PlatformInvocationAttempt, PlatformInvocationCompletion
from .personal_usage import PostgreSqlPersonalUsageRepositoryMixin
from .repository_applications import PostgreSqlApplicationRepositoryMixin
from .repository_assistant import PostgreSqlAssistantRepositoryMixin
from .repository_billable_requests import PostgreSqlBillableRequestRepositoryMixin
from .repository_budget_execution import PostgreSqlBudgetExecutionMixin
from .repository_budgets import PostgreSqlBudgetRepositoryMixin
from .repository_contract import OpsDbProxy, QueryRepository
from .repository_gateway_targets import PostgreSqlGatewayTargetRepositoryMixin
from .repository_model_availability import PostgreSqlModelAvailabilityRepositoryMixin
from .repository_publications import PostgreSqlPublicationRepositoryMixin
from .repository_registry import PostgreSqlRegistryRepositoryMixin
from .repository_support import (
    CACHE_DIMENSION_FIELDS,
    BudgetConstraintViolation,
    UsageFilters,
    _activation_runtime_config,
    attach_charts,
    cache_distribution_supported,
    cache_scope_for_filters,
    conversation_for_creator,
    report_for_viewer,
)

__all__ = (
    "CACHE_DIMENSION_FIELDS",
    "BudgetConstraintViolation",
    "OpsDbProxy",
    "PostgreSqlOpsDbProxy",
    "QueryRepository",
    "UsageFilters",
    "_activation_runtime_config",
    "attach_charts",
    "cache_distribution_supported",
    "cache_scope_for_filters",
    "conversation_for_creator",
    "report_for_viewer",
)

# Matches the ingestion processor's registry window so the two paths cannot disagree about
# how stale a model's display name may be.
_IDENTITY_CACHE_SECONDS = 60.0


class PostgreSqlOpsDbProxy(
    PostgreSqlPersonalUsageRepositoryMixin,
    PostgreSqlModelAvailabilityRepositoryMixin,
    PostgreSqlBudgetExecutionMixin,
    PostgreSqlBillableRequestRepositoryMixin,
    PostgreSqlAssistantRepositoryMixin,
    PostgreSqlApplicationRepositoryMixin,
    PostgreSqlBudgetRepositoryMixin,
    PostgreSqlGatewayTargetRepositoryMixin,
    PostgreSqlRegistryRepositoryMixin,
    PostgreSqlPublicationRepositoryMixin,
    QueryRepository,
):
    def begin_platform_invocation(self, attempt: PlatformInvocationAttempt) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO platform_invocation_attempt (request_id, started_at, snapshot)
                   VALUES (%s, %s, %s)""",
                (attempt.request_id, attempt.started_at, Jsonb(attempt.model_dump(mode="json"))),
            )

    def finish_platform_invocation(self, completion: PlatformInvocationCompletion) -> None:
        document = Jsonb(completion.model_dump(mode="json"))
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE platform_invocation_attempt SET completion = %s
                   WHERE request_id = %s AND (completion IS NULL OR completion = %s)
                   RETURNING request_id""",
                (document, completion.request_id, document),
            ).fetchone()
        if row is None:
            raise ValueError("Platform invocation completion is missing or immutable")

    def settled_reservation_correlations(
        self, correlation_ids: Sequence[str], *, scope_type: str, scope_id: str | None = None,
    ) -> set[str]:
        return super().settled_reservation_correlations(
            correlation_ids, scope_type=scope_type, scope_id=scope_id
        ) | self.settled_billable_requests(list(correlation_ids), scope_type, scope_id)

    def gateway_application_usage_activity(
        self,
        application_id: UUID,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
    ) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                                """WITH canonical_correlations AS (
                                         SELECT DISTINCT attribution.application_id,
                                                        usage.correlation_id
                                         FROM token_usage_application_attribution attribution
                                         JOIN token_usage usage ON usage.id = attribution.usage_id
                                         WHERE usage.usage_domain = 'apim'
                                     ), combined AS (
                                              SELECT attribution.application_id,
                                                  usage.correlation_id,
                                                        usage.ts, usage.et, usage.input_tokens,
                                                        usage.cached_tokens,
                                                        usage.cache_write_tokens,
                                                        usage.output_tokens, usage.estimated_cost,
                                                        usage.latency_ms, usage.status_code
                                         FROM token_usage_application_attribution attribution
                                         JOIN token_usage usage ON usage.id = attribution.usage_id
                                         WHERE attribution.application_id = %s
                                             AND usage.ts >= %s AND usage.ts < %s
                                             AND usage.usage_domain = 'apim'
                                         UNION ALL
                                         SELECT recovery.scope_id::UUID,
                                                        recovery.correlation_id,
                                                        recovery.reservation_created_at,
                                                        recovery.total_tokens::DOUBLE PRECISION,
                                                                                                                COALESCE(
                                                                                                                    recovery.input_tokens,
                                                                                                                    recovery.total_tokens
                                                                                                                ),
                                                                                                                0::BIGINT, 0::BIGINT,
                                                                                                                COALESCE(recovery.output_tokens, 0),
                                                                                                                0::DOUBLE PRECISION,
                                                        0::BIGINT, recovery.status_code
                                         FROM budget_reservation_finalization_effective recovery
                                         WHERE recovery.scope_type = 'application'
                                             AND recovery.finalization_kind
                                                 <> 'unverified_upper_bound'
                                             AND recovery.scope_id::UUID = %s
                                             AND recovery.period_start = %s
                                             AND NOT EXISTS (
                                                 SELECT 1 FROM canonical_correlations canonical
                                                 WHERE canonical.application_id =
                                                       recovery.scope_id::UUID
                                                   AND canonical.correlation_id =
                                                       recovery.correlation_id
                                             )
                                     )
                                     SELECT
                                         date_trunc(
                                             %s, ts AT TIME ZONE %s
                                         ) AT TIME ZONE %s AS bucket_start,
                                         'all' AS key,
                                         'all' AS label,
                                         jsonb_build_object(
                                             'et', COALESCE(SUM(et), 0)::DOUBLE PRECISION,
                                             'total_tokens', SUM(
                                                 input_tokens + cached_tokens + output_tokens
                                             )::BIGINT,
                                             'input_tokens', SUM(input_tokens)::BIGINT,
                                             'cached_tokens', SUM(cached_tokens)::BIGINT,
                                             'cache_read_tokens', SUM(GREATEST(
                                                 cached_tokens - cache_write_tokens, 0
                                             ))::BIGINT,
                                             'cache_write_tokens',
                                                 SUM(cache_write_tokens)::BIGINT,
                                             'output_tokens', SUM(output_tokens)::BIGINT,
                                             'calls', COUNT(*)::BIGINT,
                                             'estimated_cost', COALESCE(
                                                 SUM(estimated_cost), 0
                                             )::DOUBLE PRECISION,
                                             'p95_latency_ms', COALESCE(
                                                 percentile_cont(0.95) WITHIN GROUP (
                                                     ORDER BY latency_ms
                                                 ), 0
                                             )::DOUBLE PRECISION,
                                             'failed_calls', COUNT(*) FILTER (
                                                 WHERE status_code >= 400
                                             )::BIGINT
                                         ) AS totals
                             FROM combined
                             GROUP BY 1
                             ORDER BY 1""",
                (
                    application_id,
                    from_,
                    to,
                    application_id,
                    from_.date().replace(day=1),
                    interval,
                    timezone,
                    timezone,
                ),
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def __init__(
        self,
        database_url: str,
        pool_min_size: int = 1,
        pool_max_size: int = 4,
        identity_cache_seconds: float = _IDENTITY_CACHE_SECONDS,
        apim_api_id: str = "finops-foundry-llm",
        *, autocommit: bool = False,
    ) -> None:
        self._database_url = database_url
        # One connection per query meant a fresh TCP + TLS + auth handshake every time.
        # Measured against the deployed pair - App Service in East US, PostgreSQL in East
        # Asia - that was about 2 s each, so a single denied invocation spent ~9 s in four
        # handshakes before it ever looked at a model. The pool is small because the server
        # is a Standard_B1ms and the Function app holds its own connections too.
        self._pool = ConnectionPool(
            database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            kwargs={"row_factory": dict_row, "autocommit": autocommit},
            open=False,
        )
        self._pool_lock = threading.Lock()
        self._pool_opened = False
        self._identity_cache_seconds = identity_cache_seconds
        self._apim_api_id = apim_api_id
        self._identities: dict[str, ModelIdentity] | None = None
        self._identities_read_at = 0.0
        self._identity_lock = threading.Lock()

    @contextmanager
    def _connection(self) -> Any:
        if not self._pool_opened:
            with self._pool_lock:
                if not self._pool_opened:
                    self._pool.open()
                    self._pool_opened = True
        with self._pool.connection() as connection:
            yield connection

    def close(self) -> None:
        with self._pool_lock:
            if self._pool_opened:
                self._pool.close()
                self._pool_opened = False

    def write_token_usage(
        self,
        record: TokenUsageRecord,
        application: UsageApplicationAttribution | None = None,
        *,
        gateway: UsageGatewayAttribution | None = None,
        terminal_observation: dict[str, Any] | None = None,
        execution_receipts: Sequence[LedgerAdmissionReceipt] = (),
        usage_complete: bool = False,
        gateway_profile_id: UUID | None = None,
        native_gateway_id: str | None = None,
        evidence_source: str = "apim",
    ) -> None:
        if terminal_observation is not None and not execution_receipts:
            raise ValueError("Terminal evidence must retain immutable admission receipts")
        values = record.model_dump()
        values.setdefault("usd_cost", record.usd_cost)
        values.setdefault("usd_price_sha256", record.usd_price_sha256)
        values.setdefault("usd_price_updated_at", record.usd_price_updated_at)
        values["price_context_tier"] = record.price_context_tier
        snapshot = record.price_context_snapshot
        values["runtime_authoritative"] = record.runtime_authoritative
        values["user_ref"] = values.pop("user")
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"budget-reservation:person:{record.user_id}:{record.correlation_id}",),
            )
            if application is not None:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"budget-reservation:application:{application.application_id}:"
                     f"{record.correlation_id}",),
                )
            if record.usage_domain == "apim":
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"apim-attempt:{record.correlation_id}",),
                )
                if gateway is not None:
                    candidates = connection.execute(
                        """SELECT usage.id, usage.correlation_id, usage.usage_domain, usage.user_id,
                                  usage.price_context_snapshot, usage.price_context_tier,
                                  usage.input_price_per_million, usage.cached_price_per_million,
                                  usage.cache_write_price_per_million,
                                  usage.output_price_per_million,
                                  usage.estimated_cost FROM token_usage usage
                           WHERE usage.id = %s OR (
                             usage.usage_domain = 'apim' AND usage.correlation_id = %s
                             AND EXISTS (
                               SELECT 1 FROM token_usage_gateway_attribution attribution
                               WHERE attribution.usage_id = usage.id
                                 AND attribution.apim_target_id = %s
                                 AND attribution.native_gateway_id = %s
                             )
                           ) ORDER BY usage.id LIMIT 2 FOR UPDATE OF usage""",
                        (record.id, record.correlation_id, gateway.apim_target_id,
                         gateway.native_gateway_id),
                    ).fetchall()
                else:
                    candidates = connection.execute(
                        """SELECT id, correlation_id, usage_domain, user_id,
                                  price_context_snapshot, price_context_tier,
                                  input_price_per_million, cached_price_per_million,
                                  cache_write_price_per_million, output_price_per_million,
                                  estimated_cost FROM token_usage
                           WHERE (usage_domain = 'apim' AND correlation_id = %s) OR id = %s
                           ORDER BY id LIMIT 2 FOR UPDATE""",
                        (record.correlation_id, record.id),
                    ).fetchall()
                if len(candidates) > 1:
                    raise ValueError("APIM correlation identity is ambiguous")
                if candidates:
                    existing = candidates[0]
                    if (
                        existing["usage_domain"] != "apim"
                        or existing["correlation_id"] != record.correlation_id
                        or existing["user_id"] != record.user_id
                    ):
                        raise ValueError("APIM correlation identity conflicts with stored usage")
                    values["id"] = str(existing["id"])
                    snapshot = existing.get("price_context_snapshot")
                    if not record.estimated:
                        values.update(ModelPrice.restore_usage_prices(
                            existing, record.input_tokens, record.cached_tokens,
                            record.output_tokens, record.cache_write_tokens,
                        ))
            values["price_context_snapshot"] = Jsonb(snapshot) if snapshot is not None else None
            connection.execute(
                """
                INSERT INTO token_usage AS existing (
                    id, request_id, correlation_id, ts, team,
                    organization, organization_id, department, department_id,
                    project, project_id, user_ref, user_id, agent, agent_id,
                    workflow, run_id, turn_index, provider, model, model_id,
                    runtime, request_source, usage_domain, input_tokens, cached_tokens,
                    cache_write_tokens, output_tokens, et, et_coeff_m, latency_ms, status,
                    status_code, estimated_cost, usd_cost, usd_price_sha256,
                    usd_price_updated_at, input_price_per_million,
                    cached_price_per_million, cache_write_price_per_million,
                    output_price_per_million,
                    price_context_tier, price_context_snapshot,
                    error_message, estimated, ingest_source, ingest_error,
                    budget_admission, model_admission
                ) VALUES (
                    %(id)s, %(request_id)s, %(correlation_id)s, %(ts)s, %(team)s,
                    %(organization)s, %(organization_id)s, %(department)s,
                    %(department_id)s, %(project)s, %(project_id)s, %(user_ref)s,
                    %(user_id)s, %(agent)s, %(agent_id)s, %(workflow)s,
                    %(run_id)s, %(turn_index)s, %(provider)s, %(model)s,
                    %(model_id)s, %(runtime)s, %(request_source)s, %(usage_domain)s,
                    %(input_tokens)s, %(cached_tokens)s, %(cache_write_tokens)s,
                    %(output_tokens)s,
                    %(et)s, %(et_coeff_m)s, %(latency_ms)s, %(status)s,
                    %(status_code)s, %(estimated_cost)s, %(usd_cost)s,
                    %(usd_price_sha256)s, %(usd_price_updated_at)s,
                    %(input_price_per_million)s, %(cached_price_per_million)s,
                    %(cache_write_price_per_million)s,
                    %(output_price_per_million)s, %(price_context_tier)s,
                    %(price_context_snapshot)s, %(error_message)s,
                    %(estimated)s, %(ingest_source)s, %(ingest_error)s,
                    %(budget_admission)s, %(model_admission)s
                ) ON CONFLICT (id) DO UPDATE SET
                    request_id = CASE WHEN existing.estimated
                        THEN EXCLUDED.request_id ELSE existing.request_id END,
                    correlation_id = CASE WHEN existing.estimated
                        THEN EXCLUDED.correlation_id ELSE existing.correlation_id END,
                    runtime = CASE WHEN existing.estimated
                        OR (%(runtime_authoritative)s AND NOT EXCLUDED.estimated)
                        THEN EXCLUDED.runtime ELSE existing.runtime END,
                    input_tokens = CASE WHEN existing.estimated
                        THEN EXCLUDED.input_tokens ELSE existing.input_tokens END,
                    cached_tokens = CASE WHEN existing.estimated
                        THEN EXCLUDED.cached_tokens ELSE existing.cached_tokens END,
                    cache_write_tokens = CASE WHEN existing.estimated
                        THEN EXCLUDED.cache_write_tokens ELSE existing.cache_write_tokens END,
                    output_tokens = CASE WHEN existing.estimated
                        THEN EXCLUDED.output_tokens ELSE existing.output_tokens END,
                    et = CASE WHEN existing.estimated THEN EXCLUDED.et ELSE existing.et END,
                    et_coeff_m = CASE WHEN existing.estimated
                        THEN EXCLUDED.et_coeff_m ELSE existing.et_coeff_m END,
                    latency_ms = CASE WHEN existing.estimated
                        THEN EXCLUDED.latency_ms ELSE existing.latency_ms END,
                    status = CASE WHEN existing.estimated
                        THEN EXCLUDED.status ELSE existing.status END,
                    status_code = CASE WHEN existing.estimated
                        THEN EXCLUDED.status_code ELSE existing.status_code END,
                    estimated_cost = CASE WHEN existing.estimated
                        THEN EXCLUDED.estimated_cost ELSE existing.estimated_cost END,
                    usd_cost = CASE WHEN existing.estimated
                        THEN EXCLUDED.usd_cost ELSE existing.usd_cost END,
                    usd_price_sha256 = CASE WHEN existing.estimated
                        THEN EXCLUDED.usd_price_sha256 ELSE existing.usd_price_sha256 END,
                    usd_price_updated_at = CASE WHEN existing.estimated
                        THEN EXCLUDED.usd_price_updated_at ELSE existing.usd_price_updated_at END,
                    input_price_per_million = CASE WHEN existing.estimated
                        THEN EXCLUDED.input_price_per_million
                        ELSE existing.input_price_per_million END,
                    cached_price_per_million = CASE WHEN existing.estimated
                        THEN EXCLUDED.cached_price_per_million
                        ELSE existing.cached_price_per_million END,
                    cache_write_price_per_million = CASE WHEN existing.estimated
                        THEN EXCLUDED.cache_write_price_per_million
                        ELSE existing.cache_write_price_per_million END,
                    output_price_per_million = CASE WHEN existing.estimated
                        THEN EXCLUDED.output_price_per_million
                        ELSE existing.output_price_per_million END,
                    price_context_tier = CASE WHEN existing.estimated
                        THEN EXCLUDED.price_context_tier ELSE existing.price_context_tier END,
                    price_context_snapshot = existing.price_context_snapshot,
                    error_message = CASE WHEN existing.estimated
                        THEN EXCLUDED.error_message ELSE existing.error_message END,
                    estimated = CASE WHEN existing.estimated
                        THEN EXCLUDED.estimated ELSE existing.estimated END,
                    ingest_source = CASE WHEN existing.estimated
                        THEN EXCLUDED.ingest_source ELSE existing.ingest_source END,
                    ingest_error = CASE WHEN existing.estimated
                        THEN EXCLUDED.ingest_error ELSE existing.ingest_error END,
                    reconciled_at = NOW(),
                    budget_admission = COALESCE(
                        existing.budget_admission, EXCLUDED.budget_admission
                    ),
                    model_admission = COALESCE(
                        existing.model_admission, EXCLUDED.model_admission
                    )
                WHERE (existing.estimated AND NOT EXCLUDED.estimated)
                   OR (
                        NOT existing.estimated
                        AND NOT EXCLUDED.estimated
                        AND (
                            (existing.budget_admission IS NULL
                             AND EXCLUDED.budget_admission IS NOT NULL)
                            OR (existing.model_admission IS NULL
                                AND EXCLUDED.model_admission IS NOT NULL)
                            OR (%(runtime_authoritative)s
                                AND existing.runtime IS DISTINCT FROM EXCLUDED.runtime)
                        )
                   )
                """,
                values,
            )
            if application is not None:
                self._write_usage_application_attribution(
                    connection, values["id"], application
                )
            if gateway is not None:
                if gateway.usage_id != record.id:
                    raise ValueError("Gateway attribution must match the canonical usage ID")
                self._write_usage_gateway_attribution(
                    connection, gateway.model_copy(update={"usage_id": str(values["id"])}),
                )
            if execution_receipts:
                self._write_execution_receipts(
                    connection, record, values["id"], application, gateway,
                    execution_receipts, usage_complete,
                    terminal_observation=terminal_observation,
                )

    def model_prices(self) -> dict[str, ModelPrice]:
        """Registry unit prices keyed by both registry id and model key.

        Telemetry carries whichever identifier the caller sent, so both are indexed. A model with no
        cached rate falls back to the input rate rather than being treated as free.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT model.id::TEXT AS model_uuid, model_key,
                       input_cost_per_million,
                       COALESCE(cached_cost_per_million, input_cost_per_million) AS cached_cost,
                       COALESCE(
                           cache_write_cost_per_million,
                           cached_cost_per_million,
                           input_cost_per_million
                       ) AS cache_write_cost,
                      output_cost_per_million,
                      price.context_price_tiers
                  FROM managed_model model
                  LEFT JOIN managed_model_price price ON price.model_id = model.id
                  WHERE (input_cost_per_million IS NOT NULL
                      AND output_cost_per_million IS NOT NULL)
                     OR jsonb_array_length(COALESCE(price.context_price_tiers, '[]'::jsonb)) > 0
                  ORDER BY model.updated_at ASC
                """
            ).fetchall()
        prices: dict[str, ModelPrice] = {}
        for row in rows:
            price = ModelPrice.from_registry({
                **row, "cached_cost_per_million": row["cached_cost"],
                "cache_write_cost_per_million": row["cache_write_cost"],
            })
            if price is None:
                continue
            prices[row["model_uuid"]] = price
            prices[row["model_key"]] = price
        return prices

    def model_identities(self) -> dict[str, ModelIdentity]:
        """Canonical registry identity keyed by both registry id and model key.

        Unlike prices this is not filtered on cost columns: an unpriced model still needs a stable
        identity so it does not appear twice under two different caller-supplied identifiers.

        Cached for the same window the ingestion processor already uses. The registry changes
        when an administrator edits it, while this is read on every denied invocation, so a
        full table read per denial was pure latency. A rename therefore shows up in a trace
        up to one window late, which is the trade the processor already makes.
        """
        now = clock.monotonic()
        with self._identity_lock:
            fresh = now - self._identities_read_at < self._identity_cache_seconds
            if self._identities is not None and fresh:
                return self._identities
        with self._connection() as connection:
            retired_rows = connection.execute(
                """SELECT target ->> 'model_id' AS model_uuid,
                          target ->> 'model_key' AS model_key,
                          target ->> 'display_name' AS display_name
                   FROM gateway_publication publication
                   CROSS JOIN LATERAL jsonb_array_elements(
                     COALESCE(
                       publication.desired_spec -> 'removed_models',
                       '[]'::jsonb
                     )
                   ) target
                   WHERE publication.publication_kind = 'model_remove'
                     AND publication.status IN ('active', 'superseded')
                   ORDER BY publication.completed_at ASC"""
            ).fetchall()
            rows = connection.execute(
                """
                SELECT id::TEXT AS model_uuid, model_key, display_name
                FROM managed_model
                ORDER BY updated_at ASC
                """
            ).fetchall()
        identities: dict[str, ModelIdentity] = {}
        for row in retired_rows:
            if not row["model_uuid"]:
                continue
            identity = ModelIdentity(
                model_id=row["model_uuid"],
                display_name=row["display_name"] or row["model_uuid"],
            )
            identities[row["model_uuid"]] = identity
            if row["model_key"]:
                identities[row["model_key"]] = identity
        for row in rows:
            identity = ModelIdentity(
                model_id=row["model_uuid"],
                display_name=row["display_name"] or row["model_key"],
            )
            identities[row["model_uuid"]] = identity
            identities[row["model_key"]] = identity
        with self._identity_lock:
            self._identities = identities
            self._identities_read_at = clock.monotonic()
        return identities

    def reconciliation_watermark(self, source: str) -> datetime | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT watermark FROM usage_reconciliation_state WHERE source = %(source)s",
                {"source": source},
            ).fetchone()
        return None if row is None else cast(datetime, row["watermark"])

    def oldest_unreconciled_usage(self, since: datetime) -> datetime | None:
        """Timestamp of the oldest row that still needs a token count.

        The predicate matches `apply_reconciled_usage`'s update guard exactly, so this
        answers "what is the earliest thing the next scan must still reach", which is what
        keeps the window from starting past work that was never done.
        """
        with self._connection() as connection:
            row = connection.execute(
                """SELECT MIN(ts) AS oldest FROM token_usage
                                     WHERE estimated AND reconciled_at IS NULL
                                         AND status_code < 400 AND ts >= %(since)s""",
                {"since": since},
            ).fetchone()
        if row is None or row["oldest"] is None:
            return None
        return cast(datetime, row["oldest"])

    def save_reconciliation_state(
        self, source: str, watermark: datetime, matched: int, scanned: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO usage_reconciliation_state (
                    source, watermark, last_run_at, last_matched, last_scanned
                ) VALUES (%(source)s, %(watermark)s, NOW(), %(matched)s, %(scanned)s)
                ON CONFLICT (source) DO UPDATE SET
                    watermark = EXCLUDED.watermark,
                    last_run_at = EXCLUDED.last_run_at,
                    last_matched = EXCLUDED.last_matched,
                    last_scanned = EXCLUDED.last_scanned
                """,
                {
                    "source": source,
                    "watermark": watermark,
                    "matched": matched,
                    "scanned": scanned,
                },
            )

    def apply_reconciled_usage(self, items: Sequence[ReconciledUsage]) -> int:
        if not items:
            return 0
        # Only fills rows that are still unmeasured. Measured rows and failed requests are never
        # touched, so a repeated window is a no-op rather than a source of drift.
        with self._connection() as connection:
            cursor = connection.execute(
                """
                WITH corrections AS (
                    SELECT incoming.*, stored.id,
                        CASE WHEN stored.price_context_snapshot IS NULL
                            THEN stored.input_price_per_million
                            ELSE (matched.tier->>'input_price_per_million')::numeric
                            END AS input_price,
                        CASE WHEN stored.price_context_snapshot IS NULL
                            THEN stored.cached_price_per_million
                            ELSE (matched.tier->>'cached_price_per_million')::numeric
                            END AS cached_price,
                        CASE WHEN stored.price_context_snapshot IS NULL
                            THEN COALESCE(stored.cache_write_price_per_million,
                                          stored.cached_price_per_million)
                            ELSE (matched.tier->>'cache_write_price_per_million')::numeric
                            END AS write_price,
                        CASE WHEN stored.price_context_snapshot IS NULL
                            THEN stored.output_price_per_million
                            ELSE (matched.tier->>'output_price_per_million')::numeric
                            END AS output_price,
                        matched.tier->>'key' AS tier_key
                    FROM UNNEST(
                        %(correlation_ids)s::text[], %(input_tokens)s::bigint[],
                        %(output_tokens)s::bigint[], %(cached_tokens)s::bigint[]
                    ) AS incoming(correlation_id, input_tokens, output_tokens, cached_tokens)
                    JOIN token_usage stored ON stored.correlation_id = incoming.correlation_id
                    LEFT JOIN LATERAL (
                        SELECT CASE WHEN COUNT(*) = 1 THEN jsonb_agg(tier)->0 END AS tier
                        FROM jsonb_array_elements(COALESCE(
                            stored.price_context_snapshot->'context_tiers', '[]'::jsonb
                        )) tier
                        WHERE incoming.cached_tokens IS NOT NULL
                          AND incoming.input_tokens + incoming.cached_tokens
                              >= (tier->>'min_input_tokens')::bigint
                          AND ((tier->>'max_input_tokens') IS NULL
                               OR incoming.input_tokens + incoming.cached_tokens
                                  <= (tier->>'max_input_tokens')::bigint)
                    ) matched ON TRUE
                    WHERE stored.estimated AND stored.reconciled_at IS NULL
                      AND stored.status_code < 400
                )
                UPDATE token_usage AS u
                SET input_tokens = v.input_tokens,
                    output_tokens = v.output_tokens,
                    cached_tokens = COALESCE(v.cached_tokens, u.cached_tokens),
                    et = ROUND(
                        (u.et_coeff_m * (
                            v.input_tokens
                            + 0.1 * COALESCE(v.cached_tokens, u.cached_tokens)
                            + 4.0 * v.output_tokens
                        ))::numeric,
                        6
                    ),
                    -- Reprice from the unit prices stored when the row was ingested, never from
                    -- today's registry, so a later price edit cannot move an already reported cost.
                    -- Streamed cache usage is unavailable, so the write subset stays 0 here.
                    estimated_cost = COALESCE(
                        ROUND(
                            (v.input_tokens * v.input_price
                                + GREATEST(
                                    COALESCE(v.cached_tokens, u.cached_tokens)
                                    - u.cache_write_tokens,
                                    0
                                  )
                                    * v.cached_price
                                + u.cache_write_tokens * v.write_price
                                + v.output_tokens * v.output_price) / 1000000,
                            8
                        ),
                        u.estimated_cost
                    ),
                    usd_cost = CASE
                        WHEN v.cached_tokens IS NULL
                          OR (u.price_context_snapshot IS NOT NULL AND v.tier_key IS NULL)
                        THEN NULL
                        ELSE COALESCE(
                            ROUND(
                                (v.input_tokens * v.input_price
                                    + GREATEST(
                                        COALESCE(v.cached_tokens, u.cached_tokens)
                                        - u.cache_write_tokens,
                                        0
                                      ) * v.cached_price
                                    + u.cache_write_tokens * v.write_price
                                    + v.output_tokens * v.output_price) / 1000000,
                                14
                            ),
                            u.usd_cost
                        )
                    END,
                    input_price_per_million = v.input_price,
                    cached_price_per_million = v.cached_price,
                    cache_write_price_per_million = v.write_price,
                    output_price_per_million = v.output_price,
                    price_context_tier = CASE WHEN u.price_context_snapshot IS NULL
                        THEN u.price_context_tier ELSE v.tier_key END,
                    estimated = v.cached_tokens IS NULL OR (
                        u.price_context_snapshot IS NOT NULL AND v.tier_key IS NULL
                    ),
                    ingest_error = CASE
                        WHEN v.cached_tokens IS NULL
                        THEN 'stream_cache_usage_unavailable'
                        WHEN u.price_context_snapshot IS NOT NULL AND v.tier_key IS NULL
                        THEN 'context_pricing_input_unavailable'
                        ELSE NULL
                    END,
                    reconciled_at = CASE
                        WHEN u.price_context_snapshot IS NOT NULL AND v.tier_key IS NULL
                        THEN NULL ELSE NOW() END
                FROM corrections v
                WHERE u.id = v.id
                  AND u.estimated
                  AND u.reconciled_at IS NULL
                  AND u.status_code < 400
                """,
                {
                    "correlation_ids": [item.correlation_id for item in items],
                    "input_tokens": [item.input_tokens for item in items],
                    "output_tokens": [item.output_tokens for item in items],
                    "cached_tokens": [item.cached_tokens for item in items],
                },
            )
        return max(int(cursor.rowcount), 0)

    def upsert_apim_cache_read_buckets(
        self,
        api_id: str,
        window_start: datetime,
        window_end: datetime,
        items: Sequence[ApimCacheReadBucket],
    ) -> int:
        if not items:
            return 0
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO apim_cache_read_hourly (
                       api_id, dimension_type, dimension_value,
                       bucket_start, cache_read_tokens, observed_at
                   ) VALUES (
                       %(api_id)s, %(dimension_type)s, %(dimension_value)s,
                       %(bucket_start)s, %(cache_read_tokens)s, NOW()
                   )
                   ON CONFLICT (
                       api_id, dimension_type, dimension_value, bucket_start
                   ) DO UPDATE SET
                       cache_read_tokens = EXCLUDED.cache_read_tokens,
                       observed_at = EXCLUDED.observed_at""",
                [item.model_dump() for item in items],
            )
        return len(items)

    def apim_cache_read_totals(
        self,
        from_: datetime,
        to: datetime,
        dimension_type: str,
        dimension_values: Sequence[str] | None = None,
    ) -> dict[str, int]:
        if dimension_type != "global" or (
            dimension_values is not None and tuple(dimension_values) != ("all",)
        ):
            return {}
        clauses = [
            "api_id = %(api_id)s",
            "dimension_type = %(dimension_type)s",
            "bucket_start >= %(from)s",
            "bucket_start < %(to)s",
        ]
        parameters: dict[str, Any] = {
            "api_id": self._apim_api_id,
            "dimension_type": dimension_type,
            "from": from_,
            "to": to,
        }
        if dimension_values is not None:
            clauses.append("dimension_value = ANY(%(dimension_values)s)")
            parameters["dimension_values"] = list(dimension_values)
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT dimension_value, SUM(cache_read_tokens)::BIGINT AS cache_read_tokens
                    FROM apim_cache_read_hourly
                    WHERE {' AND '.join(clauses)}
                    GROUP BY dimension_value""",
                parameters,
            ).fetchall()
        return {str(row["dimension_value"]): int(row["cache_read_tokens"]) for row in rows}

    def _cache_read_deltas(
        self,
        connection: Any,
        from_: datetime,
        to: datetime,
        dimension_type: str,
        filters: UsageFilters,
        dimension_values: Sequence[str] | None = None,
    ) -> dict[str, int]:
        if dimension_type != "global":
            return {}
        dimension_field = CACHE_DIMENSION_FIELDS.get(dimension_type)
        dimension_sql = "'all'" if dimension_type == "global" else f"usage.{dimension_field}"
        filter_sql, filter_parameters = self._filter_sql(filters)
        metric_filter = ""
        parameters: list[Any] = [from_, to, *filter_parameters]
        if dimension_values is not None:
            metric_filter = " AND metric.dimension_value = ANY(%s)"
        parameters.extend([self._apim_api_id, dimension_type, from_, to])
        if dimension_values is not None:
            parameters.append(list(dimension_values))
        rows = connection.execute(
            f"""WITH database_cache AS (
                    SELECT {dimension_sql}::TEXT AS dimension_value,
                           date_bin(
                               INTERVAL '1 hour', usage.ts,
                               TIMESTAMPTZ '2000-01-01 00:00:00+00'
                           ) AS bucket_start,
                           COALESCE(SUM(
                               GREATEST(usage.cached_tokens - usage.cache_write_tokens, 0)
                           ) FILTER (WHERE usage.ingest_source = 'eventhub'), 0)::BIGINT
                               AS cache_read_tokens
                    FROM token_usage usage
                    WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}
                    GROUP BY 1, 2
                ), metric_cache AS (
                    SELECT metric.dimension_value, metric.bucket_start,
                           SUM(metric.cache_read_tokens)::BIGINT AS cache_read_tokens
                    FROM apim_cache_read_hourly metric
                    WHERE metric.api_id = %s
                      AND metric.dimension_type = %s
                      AND metric.bucket_start >= %s AND metric.bucket_start < %s
                      {metric_filter}
                    GROUP BY 1, 2
                )
                SELECT metric.dimension_value,
                       SUM(
                           GREATEST(
                               metric.cache_read_tokens
                                   - COALESCE(database.cache_read_tokens, 0),
                               0
                           )
                       )::BIGINT AS delta
                FROM metric_cache metric
                LEFT JOIN database_cache database
                  ON database.dimension_value = metric.dimension_value
                 AND database.bucket_start = metric.bucket_start
                GROUP BY metric.dimension_value""",
            parameters,
        ).fetchall()
        return {str(row["dimension_value"]): int(row["delta"]) for row in rows}

    @staticmethod
    def _apply_cache_read_delta(totals: dict[str, Any], delta: int) -> dict[str, Any]:
        database_cache = int(totals.get("cache_read_tokens", 0))
        corrected_cache = max(database_cache + delta, 0)
        totals["cache_read_tokens"] = corrected_cache
        totals["total_tokens"] = max(
            int(totals.get("total_tokens", 0)) + corrected_cache - database_cache,
            0,
        )
        return totals

    def _cache_read_trend_deltas(
        self,
        connection: Any,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
        dimension_type: str,
        filters: UsageFilters,
        dimension_values: Sequence[str] | None = None,
    ) -> dict[tuple[datetime, str], int]:
        if dimension_type != "global":
            return {}
        dimension_field = CACHE_DIMENSION_FIELDS.get(dimension_type)
        dimension_sql = "'all'" if dimension_type == "global" else f"usage.{dimension_field}"
        filter_sql, filter_parameters = self._filter_sql(filters)
        metric_filter = ""
        parameters: list[Any] = [
            interval,
            timezone,
            timezone,
            from_,
            to,
            *filter_parameters,
            interval,
            timezone,
            timezone,
            self._apim_api_id,
            dimension_type,
            from_,
            to,
        ]
        if dimension_values is not None:
            metric_filter = " AND metric.dimension_value = ANY(%s)"
            parameters.append(list(dimension_values))
        rows = connection.execute(
            f"""WITH database_cache AS (
                    SELECT
                        date_trunc(%s, usage.ts AT TIME ZONE %s) AT TIME ZONE %s
                            AS bucket_start,
                        {dimension_sql}::TEXT AS dimension_value,
                        COALESCE(SUM(
                            GREATEST(usage.cached_tokens - usage.cache_write_tokens, 0)
                        ) FILTER (WHERE usage.ingest_source = 'eventhub'), 0)::BIGINT
                            AS cache_read_tokens
                    FROM token_usage usage
                    WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}
                    GROUP BY 1, 2
                ), metric_cache AS (
                    SELECT
                        date_trunc(%s, metric.bucket_start AT TIME ZONE %s) AT TIME ZONE %s
                            AS bucket_start,
                        metric.dimension_value,
                        SUM(metric.cache_read_tokens)::BIGINT AS cache_read_tokens
                    FROM apim_cache_read_hourly metric
                    WHERE metric.api_id = %s
                      AND metric.dimension_type = %s
                      AND metric.bucket_start >= %s AND metric.bucket_start < %s
                      {metric_filter}
                    GROUP BY 1, 2
                )
                SELECT metric.bucket_start, metric.dimension_value,
                       GREATEST(
                           metric.cache_read_tokens - COALESCE(database.cache_read_tokens, 0),
                           0
                       ) AS delta
                FROM metric_cache metric
                LEFT JOIN database_cache database
                  ON database.bucket_start = metric.bucket_start
                 AND database.dimension_value = metric.dimension_value""",
            parameters,
        ).fetchall()
        return {
            (row["bucket_start"], str(row["dimension_value"])): int(row["delta"])
            for row in rows
        }

    def overview(self, timezone: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT token_observability_overview(%s) AS payload", (timezone,)
            ).fetchone()
        return dict(row["payload"])

    @staticmethod
    def _filter_sql(
        filters: UsageFilters, *, alias: str = "usage", trace: bool = False,
    ) -> tuple[str, list[Any]]:
        clauses = [
            f"{alias}.usage_domain = 'apim'",
            f"NOT ({alias}.ingest_error IS NOT DISTINCT FROM 'budget_pre_dispatch_denial' "
            f"AND {alias}.budget_admission IS NOT DISTINCT FROM 'budget_protocol_fenced' "
            f"AND {alias}.user_id = 'unattributed' AND {alias}.model_id = 'unattributed' "
            f"AND {alias}.request_source = 'unattributed')",
        ]
        parameters: list[Any] = []
        for column, value in (
            ("organization_id", filters.organization_id),
            ("department_id", filters.department_id),
            ("project_id", filters.project_id),
            ("agent_id", filters.agent_id),
            ("model_id", filters.model_id),
            ("user_id", filters.user_id),
            ("runtime", filters.runtime),
            ("status_code", filters.status_code),
        ):
            if value is None:
                continue
            if isinstance(value, tuple):
                # An empty selection is "no constraint", not "match nothing"; the caller
                # clears the filter by dropping it rather than by sending an empty list.
                if not value:
                    continue
                clauses.append(f"{alias}.{column} = ANY(%s)")
                parameters.append(list(value))
                continue
            clauses.append(f"{alias}.{column} = %s")
            parameters.append(value)
        if filters.apim_target_id is not None:
            clauses.append(
                f"{alias}.apim_target_id = %s" if trace else
                "EXISTS (SELECT 1 FROM token_usage_gateway_attribution gateway "
                f"WHERE gateway.usage_id = {alias}.id AND gateway.apim_target_id = %s)"
            )
            parameters.append(filters.apim_target_id)
        if filters.gateway_deployment_id is not None:
            clauses.append(
                f"{alias}.gateway_deployment_id = %s" if trace else
                "EXISTS (SELECT 1 FROM token_usage_gateway_attribution gateway "
                f"WHERE gateway.usage_id = {alias}.id "
                "AND gateway.gateway_deployment_id = %s)"
            )
            parameters.append(filters.gateway_deployment_id)
        return (" AND " + " AND ".join(clauses), parameters)

    # Tail latency, the status split and the latency histogram, as one fragment so the
    # window totals and every ranking dimension are computed by identical SQL. They were
    # previously derived in the browser from a capped request page; see AGENTS.md item 23.
    _QUALITY_METRICS_SQL = """
                    COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms), 0)
                        ::DOUBLE PRECISION AS p50_latency_ms,
                    COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)
                        ::DOUBLE PRECISION AS p95_latency_ms,
                    COALESCE(percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms), 0)
                        ::DOUBLE PRECISION AS p99_latency_ms,
                    COUNT(*) FILTER (WHERE status_code < 400)::BIGINT AS success_requests,
                    COUNT(*) FILTER (
                        WHERE status_code >= 400 AND status_code < 500
                    )::BIGINT AS client_error_requests,
                    COUNT(*) FILTER (WHERE status_code >= 500)::BIGINT AS server_error_requests,
                    COUNT(*) FILTER (WHERE latency_ms < 1000)::BIGINT AS latency_under_1s,
                    COUNT(*) FILTER (
                        WHERE latency_ms >= 1000 AND latency_ms < 2000
                    )::BIGINT AS latency_1_to_2s,
                    COUNT(*) FILTER (
                        WHERE latency_ms >= 2000 AND latency_ms < 5000
                    )::BIGINT AS latency_2_to_5s,
                    COUNT(*) FILTER (WHERE latency_ms >= 5000)::BIGINT AS latency_over_5s"""

    def _executive_totals(
        self, connection: Any, from_: datetime, to: datetime, filters: UsageFilters
    ) -> dict[str, Any]:
        filter_sql, filter_parameters = self._filter_sql(filters)
        row = connection.execute(
            f"""SELECT
                    COALESCE(SUM(input_tokens + cached_tokens + output_tokens), 0)::BIGINT
                        AS total_tokens,
                    COALESCE(SUM(
                        GREATEST(cached_tokens - cache_write_tokens, 0)
                    ), 0)::BIGINT AS cache_read_tokens,
                    COUNT(*)::BIGINT AS total_requests,
                    COALESCE(SUM(estimated_cost), 0)::DOUBLE PRECISION AS estimated_cost,
                    COALESCE(AVG(latency_ms), 0)::DOUBLE PRECISION AS average_latency_ms,
                    COALESCE(
                        100.0 * COUNT(*) FILTER (WHERE status_code >= 400)
                        / NULLIF(COUNT(*), 0), 0
                    )::DOUBLE PRECISION AS error_rate,{self._QUALITY_METRICS_SQL}
                FROM token_usage usage
                WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}""",
            [from_, to, *filter_parameters],
        ).fetchone()
        totals = dict(row)
        scope = cache_scope_for_filters(filters)
        if scope is not None:
            dimension_type, dimension_values = scope
            deltas = self._cache_read_deltas(
                connection,
                from_,
                to,
                dimension_type,
                filters,
                dimension_values,
            )
            self._apply_cache_read_delta(totals, sum(deltas.values()))
        return totals

    @staticmethod
    def _percent_change(current: float, previous: float) -> float | None:
        return None if previous == 0 else round((current - previous) / previous * 100, 2)

    def executive_overview(
        self, from_: datetime, to: datetime, filters: UsageFilters
    ) -> dict[str, Any]:
        previous_from = from_ - (to - from_)
        with self._connection() as connection:
            current = self._executive_totals(connection, from_, to, filters)
            previous = self._executive_totals(connection, previous_from, from_, filters)
        return {
            "from": from_,
            "to": to,
            "previous_from": previous_from,
            "generated_at": datetime.now(to.tzinfo),
            "totals": current,
            "changes_percent": {
                key: self._percent_change(float(current[key]), float(previous[key]))
                for key in current
            },
        }

    _DIMENSION_FIELDS = {
        "organization": ("organization_id", "organization"),
        "department": ("department_id", "department"),
        "project": ("project_id", "project"),
        "agent": ("agent_id", "agent"),
        "model": ("model_id", "model"),
        "user": ("user_id", "user_ref"),
        # Runtime has no separate id column in telemetry, so the name is its own key.
        "runtime": ("runtime", "runtime"),
    }

    def distribution(
        self,
        from_: datetime,
        to: datetime,
        dimension: str,
        filters: UsageFilters,
        limit: int,
        split_by: str | None = None,
    ) -> Sequence[dict[str, Any]]:
        id_field, name_field = self._DIMENSION_FIELDS[dimension]
        filter_sql, filter_parameters = self._filter_sql(filters)
        with self._connection() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""WITH scoped AS (
                        SELECT * FROM token_usage usage
                        WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}
                    ), total AS (
                        SELECT COALESCE(SUM(estimated_cost), 0) AS cost FROM scoped
                    )
                    SELECT
                        {id_field} AS id,
                        {name_field} AS name,
                        SUM(input_tokens + cached_tokens + output_tokens)::BIGINT AS total_tokens,
                        SUM(GREATEST(cached_tokens - cache_write_tokens, 0))::BIGINT
                            AS cache_read_tokens,
                        COUNT(*)::BIGINT AS total_requests,
                        SUM(estimated_cost)::DOUBLE PRECISION AS estimated_cost,
                        AVG(latency_ms)::DOUBLE PRECISION AS average_latency_ms,
                        COALESCE(
                            100.0 * COUNT(*) FILTER (WHERE status_code >= 400)
                            / NULLIF(COUNT(*), 0), 0
                        )::DOUBLE PRECISION AS error_rate,{self._QUALITY_METRICS_SQL},
                        CASE WHEN total.cost = 0 THEN 0 ELSE
                            ROUND(100.0 * SUM(estimated_cost) / total.cost, 2)
                        END::DOUBLE PRECISION AS share_percent
                    FROM scoped CROSS JOIN total
                    GROUP BY {id_field}, {name_field}, total.cost
                    ORDER BY total_tokens DESC, name
                    LIMIT 100""",
                    [from_, to, *filter_parameters],
                ).fetchall()
            ]
            if cache_distribution_supported(dimension, filters):
                dimension_values = [str(row["id"]) for row in rows]
                deltas = self._cache_read_deltas(
                    connection,
                    from_,
                    to,
                    dimension,
                    filters,
                    dimension_values,
                )
                for row in rows:
                    self._apply_cache_read_delta(row, deltas.get(str(row["id"]), 0))
                rows.sort(key=lambda row: (-int(row["total_tokens"]), str(row["name"])))
            rows = rows[:limit]
            if split_by is None or not rows:
                return rows
            # A second pass rather than one nested query: the parent ranking is already
            # limited, so the split only has to cover the rows that will be rendered.
            split_id, split_name = self._DIMENSION_FIELDS[split_by]
            children = connection.execute(
                f"""SELECT
                        {id_field} AS parent_id,
                        {split_id} AS id,
                        {split_name} AS name,
                        SUM(input_tokens + cached_tokens + output_tokens)::BIGINT AS total_tokens,
                        COUNT(*)::BIGINT AS total_requests
                    FROM token_usage usage
                    WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}
                        AND {id_field} = ANY(%s)
                    GROUP BY 1, 2, 3
                    ORDER BY total_tokens DESC""",
                [from_, to, *filter_parameters, [str(row["id"]) for row in rows]],
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for child in children:
            entry = dict(child)
            grouped.setdefault(str(entry.pop("parent_id")), []).append(entry)
        for row in rows:
            row["breakdown"] = grouped.get(str(row["id"]), [])
        return rows

    def list_usage_requests(
        self, from_: datetime, to: datetime, filters: UsageFilters, limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> Sequence[dict[str, Any]]:
        filter_sql, filter_parameters = self._filter_sql(filters)
        cursor_sql = " AND (usage.ts, usage.trace_id) < (%s, %s)" if after is not None else ""
        cursor_parameters = list(after) if after is not None else []
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT
                        usage.request_id, usage.correlation_id,
                        usage.trace_id, usage.usage_available, usage.platform_outcome,
                        usage.gateway_status_code, usage.upstream_request_id,
                        usage.apim_target_id, usage.gateway_deployment_id, usage.native_gateway_id,
                        usage.ts AS timestamp,
                        usage.organization_id,
                        usage.organization AS organization_name,
                        usage.department_id,
                        usage.department AS department_name,
                        usage.project_id, usage.project AS project_name,
                        usage.agent_id, usage.agent AS agent_name,
                        usage.user_id, usage.user_ref AS user_name,
                        usage.model_id, usage.model AS model_name,
                        usage.request_source, usage.runtime,
                        usage.input_tokens AS prompt_tokens,
                        usage.output_tokens AS completion_tokens,
                        (usage.input_tokens + usage.cached_tokens
                            + usage.output_tokens)::BIGINT AS total_tokens,
                        usage.latency_ms, usage.status_code,
                        usage.estimated_cost::DOUBLE PRECISION,
                        (usage.usage_available AND (usage.estimated_cost > 0
                         OR (usage.input_price_per_million IS NOT NULL
                             AND usage.output_price_per_million IS NOT NULL))) AS cost_available,
                        usage.error_message
                    FROM invocation_request_trace usage
                    WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}{cursor_sql}
                        AND usage.usage_domain = 'apim'
                    ORDER BY usage.ts DESC, usage.trace_id DESC LIMIT %s""",
                [from_, to, *filter_parameters, *cursor_parameters, limit],
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def get_usage_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT
                        usage.request_id, usage.correlation_id,
                        usage.trace_id, usage.usage_available, usage.platform_outcome,
                        usage.gateway_status_code, usage.upstream_request_id,
                        usage.apim_target_id, usage.gateway_deployment_id, usage.native_gateway_id,
                        usage.ts AS timestamp,
                        usage.organization_id,
                        usage.organization AS organization_name,
                        usage.department_id,
                        usage.department AS department_name,
                        usage.project_id, usage.project AS project_name,
                        usage.agent_id, usage.agent AS agent_name,
                        usage.user_id, usage.user_ref AS user_name,
                        usage.model_id, usage.model AS model_name,
                        usage.request_source, usage.runtime,
                        usage.input_tokens AS prompt_tokens,
                        usage.output_tokens AS completion_tokens,
                        (usage.input_tokens + usage.cached_tokens
                            + usage.output_tokens)::BIGINT AS total_tokens,
                        usage.latency_ms, usage.status_code,
                        usage.estimated_cost::DOUBLE PRECISION,
                            (usage.usage_available AND (usage.estimated_cost > 0
                         OR (usage.input_price_per_million IS NOT NULL
                                AND usage.output_price_per_million IS NOT NULL))) AS cost_available,
                        usage.error_message, usage.provider, usage.workflow,
                        usage.run_id, usage.turn_index, usage.cached_tokens,
                        usage.cache_write_tokens, usage.estimated,
                        usage.input_price_per_million, usage.cached_price_per_million,
                        usage.cache_write_price_per_million, usage.output_price_per_million,
                        usage.price_context_tier, usage.price_context_snapshot,
                        usage.ingest_source, usage.ingest_error,
                        usage.reconciled_at, usage.budget_admission,
                        usage.model_admission
                    FROM invocation_request_trace usage
                    WHERE (usage.correlation_id = %s OR usage.request_id = %s
                        OR usage.trace_id = %s OR usage.id = %s)
                        AND usage.usage_domain = 'apim'
                    ORDER BY CASE WHEN usage.trace_id = %s THEN 0
                        WHEN usage.correlation_id = %s THEN 1 ELSE 2 END,
                        usage.ts DESC, usage.id DESC
                    LIMIT 1""",
                (request_id, request_id, request_id, request_id, request_id, request_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def observed_users(self) -> Sequence[dict[str, Any]]:
        """People who actually called the gateway, newest attribution wins.

        A person can move between departments, so the most recent request decides where
        they hang in the budget hierarchy rather than the first one ever recorded.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT ON (user_id) user_id, user_ref, department_id
                   FROM token_usage
                         WHERE user_id <> 'unattributed' AND usage_domain = 'apim'
                   ORDER BY user_id, ts DESC"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def application_owners(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT email, display_name, role
                   FROM app_user
                   WHERE enabled AND role = 'owner'
                   ORDER BY email"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def list_usage_anomalies(
        self, from_: datetime, to: datetime, filters: UsageFilters, limit: int
    ) -> Sequence[dict[str, Any]]:
        filter_sql, filter_parameters = self._filter_sql(filters)
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT request_id, ts, organization_id, department_id,
                           project_id, agent_id, agent, model_id, model,
                           user_id, status_code, latency_ms,
                           estimated_cost::DOUBLE PRECISION AS estimated_cost
                    FROM token_usage usage
                    WHERE usage.ts >= %s AND usage.ts < %s{filter_sql}""",
                [from_, to, *filter_parameters],
            ).fetchall()
            rules = connection.execute(
                """SELECT id, name, description, metric, threshold_mode,
                          threshold_value, minimum_sample_size, severity,
                          scope_type, scope_id, enabled
                   FROM anomaly_rule"""
            ).fetchall()
        return evaluate_anomaly_rules(rows, rules, to, limit)

    def list_anomaly_rules(self) -> Sequence[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, name, description, metric, threshold_mode,
                          threshold_value, minimum_sample_size, severity,
                          scope_type, scope_id, enabled, created_at, updated_at,
                          updated_by
                   FROM anomaly_rule
                   ORDER BY created_at, id"""
            ).fetchall()
        return cast(Sequence[dict[str, Any]], rows)

    def create_anomaly_rule(self, values: Mapping[str, Any]) -> dict[str, Any]:
        parameters = {"id": uuid4(), **values}
        with self._connection() as connection:
            row = connection.execute(
                """INSERT INTO anomaly_rule (
                       id, name, description, metric, threshold_mode,
                       threshold_value, minimum_sample_size, severity,
                       scope_type, scope_id, enabled, updated_by
                   ) VALUES (
                       %(id)s, %(name)s, %(description)s, %(metric)s,
                       %(threshold_mode)s, %(threshold_value)s,
                       %(minimum_sample_size)s, %(severity)s, %(scope_type)s,
                       %(scope_id)s, %(enabled)s, %(updated_by)s
                   ) RETURNING *""",
                parameters,
            ).fetchone()
        return cast(dict[str, Any], row)

    def update_anomaly_rule(
        self, rule_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        parameters = {"id": rule_id, **values}
        with self._connection() as connection:
            row = connection.execute(
                """UPDATE anomaly_rule SET
                       name = %(name)s,
                       description = %(description)s,
                       metric = %(metric)s,
                       threshold_mode = %(threshold_mode)s,
                       threshold_value = %(threshold_value)s,
                       minimum_sample_size = %(minimum_sample_size)s,
                       severity = %(severity)s,
                       scope_type = %(scope_type)s,
                       scope_id = %(scope_id)s,
                       enabled = %(enabled)s,
                       updated_at = now(),
                       updated_by = %(updated_by)s
                   WHERE id = %(id)s
                   RETURNING *""",
                parameters,
            ).fetchone()
        return cast(dict[str, Any] | None, row)

    def delete_anomaly_rule(self, rule_id: UUID) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "DELETE FROM anomaly_rule WHERE id = %s RETURNING id", (rule_id,)
            ).fetchone()
        return row is not None
