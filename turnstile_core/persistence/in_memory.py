from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from ..domain.anomaly_engine import evaluate_anomaly_rules
from ..domain.application_access import UsageApplicationAttribution
from ..domain.billable_requests import BillableRequestAttempt
from ..domain.enterprise import enterprise_catalog
from ..domain.ledger import BudgetReservationAdmission, BudgetReservationFinalization
from ..domain.models import (
    ApimCacheReadBucket,
    AuditFindingUpdate,
    ModelIdentity,
    ModelPrice,
    OptimizationEventCreate,
    ReconciledUsage,
    TokenUsageRecord,
)
from .in_memory_applications import InMemoryApplicationRepositoryMixin
from .in_memory_assistant import InMemoryAssistantRepositoryMixin
from .in_memory_billable_requests import InMemoryBillableRequestRepositoryMixin
from .in_memory_budgets import InMemoryBudgetRepositoryMixin
from .in_memory_organization import InMemoryOrganizationRepositoryMixin
from .in_memory_publications import InMemoryPublicationRepositoryMixin
from .in_memory_registry import InMemoryRegistryRepositoryMixin
from .repository import (
    CACHE_DIMENSION_FIELDS,
    QueryRepository,
    UsageFilters,
    cache_distribution_supported,
    cache_scope_for_filters,
)


def _repriced(record: TokenUsageRecord, item: ReconciledUsage) -> float:
    """Reprice from the unit prices stored at ingestion, never from today's registry."""
    if record.input_price_per_million is None or record.output_price_per_million is None:
        return record.estimated_cost
    cached_price = (
        record.cached_price_per_million
        if record.cached_price_per_million is not None
        else record.input_price_per_million
    )
    write_price = (
        record.cache_write_price_per_million
        if record.cache_write_price_per_million is not None
        else cached_price
    )
    # Missing streamed cache usage remains a zero-valued lower bound in legacy numeric columns.
    cached_tokens = record.cached_tokens if item.cached_tokens is None else item.cached_tokens
    cache_reads = max(cached_tokens - record.cache_write_tokens, 0)
    return round(
        (
            item.input_tokens * record.input_price_per_million
            + cache_reads * cached_price
            + record.cache_write_tokens * write_price
            + item.output_tokens * record.output_price_per_million
        )
        / 1_000_000,
        8,
    )


def metric(et: float, input_: int, cached: int, output: int, calls: int) -> dict[str, Any]:
    return {
        "et": et,
        "total_tokens": input_ + cached + output,
        "input_tokens": input_,
        "cached_tokens": cached,
        "cache_read_tokens": cached,
        "output_tokens": output,
        "calls": calls,
    }


class InMemoryRepository(
    InMemoryRegistryRepositoryMixin,
    InMemoryApplicationRepositoryMixin,
    InMemoryPublicationRepositoryMixin,
    InMemoryAssistantRepositoryMixin,
    InMemoryBudgetRepositoryMixin,
    InMemoryBillableRequestRepositoryMixin,
    InMemoryOrganizationRepositoryMixin,
    QueryRepository,
):
    def __init__(self) -> None:
        self.billable_request_lock = threading.RLock()
        self.budget_evidence_received_at: dict[str, datetime] = {}
        self.budget_evidence_conflicts: set[tuple[str, str, str, int, int]] = set()
        self.reconciled_usage_ids: set[str] = set()
        self.billable_requests: dict[UUID, BillableRequestAttempt] = {}
        self.evidence_policy_effective_at: datetime | None = None
        self.budget_reservation_admissions: dict[
            tuple[str, str, str], BudgetReservationAdmission
        ] = {}
        now = datetime(2026, 7, 17, 8, tzinfo=UTC)
        self.reconciliation_state: dict[str, datetime] = {}
        self.apim_api_id = "turnstile-llm"
        self.apim_cache_read_buckets: dict[tuple[str, str, str, datetime], int] = {}
        self.findings = [
            {
                "id": UUID("a1000000-0000-4000-8000-000000000001"),
                "created_at": now,
                "updated_at": now,
                "rule_id": "run-turn-spike",
                "severity": "critical",
                "workflow": "release-regression-check",
                "run_id": "run-foundry-loop-0717",
                "evidence": {"turns": 18, "historical_p95": 5},
                "status": "new",
                "assignee": None,
                "suggestion": "检查工具调用终止条件与重试上限",
                "resolution_note": None,
            }
        ]
        self.optimizations = [
            self._optimization(
                UUID("b1000000-0000-4000-8000-000000000001"),
                "pull-request-review",
                "裁剪 MCP 工具集",
                -62.0,
            )
        ]
        self.usage_records: list[TokenUsageRecord] = []
        self.budget_reservation_finalizations: list[BudgetReservationFinalization] = []
        self.usage_application_attributions: dict[str, UsageApplicationAttribution] = {}
        self.pinned_reports: list[dict[str, Any]] = []
        self.pinned_charts: list[dict[str, Any]] = []
        self.conversations: list[dict[str, Any]] = []
        self.application_owner_rows: list[dict[str, Any]] = []
        # Mirrors the single seeded row migration 027 creates.
        self.assistant_setting: dict[str, Any] = {
            "model_id": None,
            "auto_title": True,
            "updated_at": None,
            "updated_by": None,
        }
        self.token_budgets: dict[tuple[date, str, str], dict[str, Any]] = {}
        self.budget_roll_forward: dict[date, dict[str, Any]] = {}
        self.token_budget_audit: list[dict[str, Any]] = []
        self.user_model_policies: dict[str, dict[str, Any]] = {}
        self.user_model_access_audit: list[dict[str, Any]] = []
        self.gateway_publications: list[dict[str, Any]] = []
        self.gateway_publication_audit: list[dict[str, Any]] = []
        self.gateway_publication_outbox: list[dict[str, Any]] = []
        self.gateway_publication_secrets: dict[UUID, bytes] = {}
        self.effective_gateway_releases: dict[UUID, UUID] = {}
        self.gateway_release_protections: dict[UUID, dict[str, Any]] = {}
        self.gateway_release_protection_audit: list[dict[str, Any]] = []
        self.gateway_release_operations: list[dict[str, Any]] = []
        self.gateway_release_operation_secrets: dict[UUID, bytes] = {}
        self.gateway_release_operation_audit: list[dict[str, Any]] = []
        self.gateway_release_integrity_snapshots: list[dict[str, Any]] = []
        self.gateway_release_gc_plans: list[dict[str, Any]] = []
        self.gateway_applications: list[dict[str, Any]] = []
        self.gateway_application_avatars: dict[UUID, dict[str, Any]] = {}
        self.gateway_application_subscriptions: list[dict[str, Any]] = []
        self.gateway_application_budgets: dict[tuple[date, UUID], dict[str, Any]] = {}
        self.gateway_application_ledger_states: dict[tuple[date, UUID], dict[str, Any]] = {}
        self.gateway_application_model_policies: dict[UUID, dict[str, Any]] = {}
        self.gateway_application_model_access: dict[UUID, set[UUID]] = {}
        self.gateway_application_audit: list[dict[str, Any]] = []
        self.gateway_application_attribution: dict[UUID, dict[str, Any]] = {}
        self.gateway_application_attribution_audit: list[dict[str, Any]] = []
        # Mirrors what migration 009 seeds, so the fake and a migrated database answer the
        # same structure to anything that reads it.
        self.org_units_by_id: dict[str, dict[str, Any]] = {
            entity.id: {
                "id": entity.id,
                "unit_type": unit_type,
                "parent_id": entity.parent_id,
                "display_name": entity.name,
                "status": "active",
                "created_by": "seed",
                "created_at": datetime.now(UTC),
                "updated_by": "seed",
                "updated_at": datetime.now(UTC),
            }
            for unit_type, entities in (
                ("organization", enterprise_catalog().organizations),
                ("department", enterprise_catalog().departments),
            )
            for entity in entities
        }
        self.org_unit_audit: list[dict[str, Any]] = []
        self.department_enforcement: dict[str, dict[str, Any]] = {
            department.id: {
                "department_id": department.id,
                "mode": "audit",
                "updated_at": datetime.now(UTC),
                "updated_by": "seed",
            }
            for department in enterprise_catalog().departments
        }
        self.department_enforcement_audit: list[dict[str, Any]] = []
        self.anomaly_rules = [
            self._anomaly_rule(
                UUID("61000000-0000-4000-8000-000000000001"),
                "错误率超过 5%",
                "error_rate_percent",
                "absolute",
                5,
                20,
                "critical",
                now,
            ),
            self._anomaly_rule(
                UUID("61000000-0000-4000-8000-000000000002"),
                "慢请求超过 2 秒",
                "request_latency_ms",
                "absolute",
                2000,
                1,
                "warning",
                now,
            ),
            self._anomaly_rule(
                UUID("61000000-0000-4000-8000-000000000003"),
                "高成本请求 P99",
                "request_cost_usd",
                "percentile",
                99,
                2,
                "warning",
                now,
            ),
            self._anomaly_rule(
                UUID("61000000-0000-4000-8000-000000000004"),
                "Agent 高频调用 P99",
                "agent_request_count",
                "percentile",
                99,
                2,
                "warning",
                now,
            ),
        ]
        self.gateways = [
            {
                "id": UUID("10000000-0000-4000-8000-000000000001"),
                "name": "Azure API Management",
                "implementation": "apim",
                "base_url": "http://127.0.0.1:8000/mock-apim",
                "auth_type": "api_key",
                "credential_ciphertext": None,
                "credential_hint": None,
                "enabled": True,
                "is_default": True,
                "config": {"header_name": "Ocp-Apim-Subscription-Key"},
                "created_at": now,
                "updated_at": now,
            },
        ]
        self.providers = [
            self._provider(
                UUID("20000000-0000-4000-8000-000000000001"),
                "GitHub Copilot",
                "github",
                now,
            ),
            self._provider(
                UUID("20000000-0000-4000-8000-000000000003"),
                "Microsoft Foundry",
                "microsoft_foundry",
                now,
                auth_type="api_key",
            ),
        ]
        self.runtimes = [
            self._runtime(
                UUID("30000000-0000-4000-8000-000000000001"),
                self.providers[0],
                "GitHub Copilot CLI",
                "copilot_cli",
                now,
                is_default=True,
                config={
                    "command": "copilot",
                    "args": [
                        "-p",
                        "{prompt}",
                        "--silent",
                        "--no-color",
                        "--disable-builtin-mcps",
                        "--no-custom-instructions",
                    ],
                    "timeout_seconds": 120,
                },
            ),
            self._runtime(
                UUID("30000000-0000-4000-8000-000000000003"),
                self.providers[1],
                "Microsoft Foundry via APIM",
                "foundry",
                now,
                gateway=self.gateways[0],
                enabled=True,
                config={
                    "path": "/chat/completions",
                    "api_format": "openai_chat",
                    "backend_path": "/openai/v1/chat/completions",
                },
            ),
        ]
        self.models = [
            self._model(
                UUID("40000000-0000-4000-8000-000000000003"),
                self.providers[1],
                self.runtimes[1],
                "gpt-4.1",
                "GPT-4.1 (Foundry)",
                now,
                enabled=False,
                capabilities=["chat", "tools", "vision"],
            ),
            self._model(
                UUID("9b45b7e1-403d-4c6c-a877-5dc77775b911"),
                self.providers[1],
                self.runtimes[1],
                "gpt-5.6-luna",
                "gpt-5.6-luna",
                now,
                capabilities=["chat", "tools", "reasoning"],
                context_window=None,
                input_cost_per_million=1,
                output_cost_per_million=6,
                allowed_roles=["owner", "admin", "member", "service"],
            ),
            self._model(
                UUID("cd73c88d-9511-4e20-9bcf-d591382086fd"),
                self.providers[1],
                self.runtimes[1],
                "gpt-5.3-chat",
                "gpt-5.3-chat",
                now,
                capabilities=["chat", "tools", "reasoning"],
                context_window=None,
                input_cost_per_million=5,
                output_cost_per_million=30,
                allowed_roles=["owner", "admin", "member", "service"],
            ),
            self._model(
                UUID("81028b7f-b1ea-40c6-a0ae-1c1e29f9efba"),
                self.providers[1],
                self.runtimes[1],
                "gpt-5.4",
                "gpt-5.4",
                now,
                capabilities=["chat", "tools", "reasoning"],
                context_window=None,
                input_cost_per_million=2.5,
                output_cost_per_million=15,
                allowed_roles=["owner", "admin", "member", "service"],
            ),
            self._model(
                UUID("355eae31-4332-499e-a47e-74ab568aa52a"),
                self.providers[1],
                self.runtimes[1],
                "gpt-5.6-sol",
                "gpt-5.6-sol",
                now,
                capabilities=["chat", "tools", "reasoning"],
                context_window=None,
                input_cost_per_million=5,
                output_cost_per_million=30,
                allowed_roles=["owner", "admin", "member", "service"],
            ),
            self._model(
                UUID("a1b70d94-3012-461b-a3cd-ace46e2cb9ab"),
                self.providers[1],
                self.runtimes[1],
                "gpt-5.6-terra",
                "gpt-5.6-terra",
                now,
                capabilities=["chat", "tools", "reasoning"],
                context_window=None,
                input_cost_per_million=2.5,
                output_cost_per_million=15,
                allowed_roles=["owner", "admin", "member", "service"],
            ),
        ]

    def write_token_usage(
        self,
        record: TokenUsageRecord,
        application: UsageApplicationAttribution | None = None,
    ) -> None:
        with self.billable_request_lock:
            self._write_token_usage(record, application)

    def _write_token_usage(
        self,
        record: TokenUsageRecord,
        application: UsageApplicationAttribution | None,
    ) -> None:
        if record.usage_domain == "apim":
            candidates = [
                item
                for item in self.usage_records
                if (item.usage_domain == "apim" and item.correlation_id == record.correlation_id)
                or item.id == record.id
            ]
            if len(candidates) > 1:
                raise ValueError("APIM correlation identity is ambiguous")
            if candidates:
                existing = candidates[0]
                if (
                    existing.usage_domain != "apim"
                    or existing.correlation_id != record.correlation_id
                    or existing.user_id != record.user_id
                ):
                    raise ValueError("APIM correlation identity conflicts with stored usage")
                record = record.model_copy(update={"id": existing.id})
        if application is not None:
            self._write_usage_application_attribution(record.id, application)
        self.budget_evidence_received_at.setdefault("usage:" + record.id, datetime.now(UTC))
        for index, existing in enumerate(self.usage_records):
            if existing.id != record.id:
                continue
            if existing.estimated and not record.estimated:
                self.usage_records[index] = record.model_copy(
                    update={
                        "budget_admission": record.budget_admission
                        or existing.budget_admission,
                        "model_admission": record.model_admission
                        or existing.model_admission,
                    }
                )
            elif not existing.estimated and not record.estimated:
                existing_tokens = (
                    existing.input_tokens + existing.cached_tokens + existing.output_tokens
                )
                incoming_tokens = record.input_tokens + record.cached_tokens + record.output_tokens
                if existing_tokens != incoming_tokens:
                    self.budget_evidence_conflicts.add(
                        (
                            "person",
                            existing.user_id,
                            existing.correlation_id,
                            existing_tokens,
                            incoming_tokens,
                        )
                    )
                updates = {
                    field: getattr(record, field)
                    for field in ("budget_admission", "model_admission")
                    if getattr(existing, field) is None and getattr(record, field) is not None
                }
                if record.runtime_authoritative:
                    updates["runtime"] = record.runtime
                if updates:
                    self.usage_records[index] = existing.model_copy(update=updates)
            return
        self.usage_records.append(record)

    def model_prices(self) -> dict[str, ModelPrice]:
        prices: dict[str, ModelPrice] = {}
        for model in self.models:
            input_price = model["input_cost_per_million"]
            output_price = model["output_cost_per_million"]
            if input_price is None or output_price is None:
                continue
            cached_price = model.get("cached_cost_per_million")
            write_price = model.get("cache_write_cost_per_million")
            cached = input_price if cached_price is None else cached_price
            price = ModelPrice(
                input_price_per_million=input_price,
                cached_price_per_million=cached,
                cache_write_price_per_million=cached if write_price is None else write_price,
                output_price_per_million=output_price,
            )
            prices[str(model["id"])] = price
            prices[model["model_key"]] = price
        return prices

    def model_identities(self) -> dict[str, ModelIdentity]:
        identities: dict[str, ModelIdentity] = {}
        for publication in self.gateway_publications:
            if publication["publication_kind"] != "model_remove" or publication[
                "status"
            ] not in {"active", "superseded"}:
                continue
            for target in publication["desired_spec"].get("removed_models", []):
                model_id = str(target["model_id"])
                identity = ModelIdentity(
                    model_id=model_id,
                    display_name=str(target["display_name"]),
                )
                identities[model_id] = identity
                identities[str(target["model_key"])] = identity
        for model in self.models:
            identity = ModelIdentity(
                model_id=str(model["id"]),
                display_name=model.get("display_name") or model["model_key"],
            )
            identities[str(model["id"])] = identity
            identities[model["model_key"]] = identity
        return identities

    def reconciliation_watermark(self, source: str) -> datetime | None:
        return self.reconciliation_state.get(source)

    def oldest_unreconciled_usage(self, since: datetime) -> datetime | None:
        # `estimated` is what this repository flips when a row is reconciled, standing in
        # for the `reconciled_at IS NULL` half of the SQL guard.
        pending = [
            record.ts
            for record in self.usage_records
            if record.estimated and record.status_code < 400 and record.ts >= since
        ]
        return min(pending) if pending else None

    def save_reconciliation_state(
        self, source: str, watermark: datetime, matched: int, scanned: int
    ) -> None:
        self.reconciliation_state[source] = watermark

    def apply_reconciled_usage(self, items: Sequence[ReconciledUsage]) -> int:
        by_correlation = {item.correlation_id: item for item in items}
        matched = 0
        for index, record in enumerate(self.usage_records):
            item = by_correlation.get(record.correlation_id)
            if (
                item is None
                or not record.estimated
                or record.status_code >= 400
                or record.ingest_error == "stream_cache_usage_unavailable"
            ):
                continue
            cache_unknown = item.cached_tokens is None
            self.usage_records[index] = record.model_copy(
                update={
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "cached_tokens": (
                        record.cached_tokens if cache_unknown else item.cached_tokens
                    ),
                    "estimated_cost": _repriced(record, item),
                    "estimated": cache_unknown,
                    "ingest_error": (
                        "stream_cache_usage_unavailable" if cache_unknown else None
                    ),
                }
            )
            self.reconciled_usage_ids.add(record.id)
            self.budget_evidence_received_at.setdefault(
                "reconciled:" + record.id, datetime.now(UTC)
            )
            matched += 1
        return matched

    def upsert_apim_cache_read_buckets(
        self,
        api_id: str,
        window_start: datetime,
        window_end: datetime,
        items: Sequence[ApimCacheReadBucket],
    ) -> int:
        for item in items:
            self.apim_cache_read_buckets[
                (
                    item.api_id,
                    item.dimension_type,
                    item.dimension_value,
                    item.bucket_start,
                )
            ] = item.cache_read_tokens
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
        allowed = None if dimension_values is None else set(dimension_values)
        totals: dict[str, int] = {}
        for (
            api_id,
            item_dimension_type,
            dimension_value,
            bucket_start,
        ), value in self.apim_cache_read_buckets.items():
            if (
                api_id != self.apim_api_id
                or item_dimension_type != dimension_type
                or not from_ <= bucket_start < to
                or (allowed is not None and dimension_value not in allowed)
            ):
                continue
            totals[dimension_value] = totals.get(dimension_value, 0) + value
        return totals

    def _apply_metric_cache(
        self,
        totals: dict[str, Any],
        records: Sequence[TokenUsageRecord],
        from_: datetime,
        to: datetime,
        dimension_type: str,
        dimension_values: Sequence[str],
    ) -> None:
        metric = self.apim_cache_read_totals(
            from_, to, dimension_type, dimension_values
        )
        if not metric:
            return
        dimension_field = (
            dimension_type if dimension_type == "runtime" else f"{dimension_type}_id"
        )
        database_cache = int(totals.get("cache_read_tokens", 0))
        metric_cache = 0
        database_apim_cache = 0
        for dimension_value, value in metric.items():
            metric_cache += value
            database_apim_cache += sum(
                max(record.cached_tokens - record.cache_write_tokens, 0)
                for record in records
                if record.ingest_source == "eventhub"
                and (
                    dimension_type == "global"
                    or str(getattr(record, dimension_field)) == dimension_value
                )
            )
        corrected = database_cache + max(metric_cache - database_apim_cache, 0)
        totals["cache_read_tokens"] = corrected
        totals["total_tokens"] = max(
            int(totals["total_tokens"]) + corrected - database_cache, 0
        )

    @staticmethod
    def _matches(record: TokenUsageRecord, filters: UsageFilters) -> bool:
        if record.usage_domain != "apim":
            return False
        for field, value in (
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
            actual = getattr(record, field)
            if isinstance(value, tuple):
                if value and actual not in value:
                    return False
                continue
            if actual != value:
                return False
        return True

    def _usage_between(
        self, from_: datetime, to: datetime, filters: UsageFilters
    ) -> list[TokenUsageRecord]:
        return [
            record
            for record in self.usage_records
            if from_ <= record.ts < to and self._matches(record, filters)
        ]

    @staticmethod
    def _percentile_cont(values: Sequence[float], fraction: float) -> float:
        """Linear interpolation between ordered values, matching PostgreSQL `percentile_cont`.

        The two repositories back the same API, so a test that passes against this one must
        mean the same thing against PostgreSQL. A discrete percentile would drift from the
        deployed behaviour on small samples, which is exactly where tests live.
        """
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return float(ordered[0])
        position = fraction * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)

    @staticmethod
    def _executive_totals(records: Sequence[TokenUsageRecord]) -> dict[str, Any]:
        count = len(records)
        latencies = [float(record.latency_ms) for record in records]

        def in_band(low: float, high: float | None) -> int:
            return sum(
                1
                for value in latencies
                if value >= low and (high is None or value < high)
            )

        return {
            "total_tokens": sum(
                record.input_tokens + record.cached_tokens + record.output_tokens
                for record in records
            ),
            "cache_read_tokens": sum(
                max(record.cached_tokens - record.cache_write_tokens, 0)
                for record in records
            ),
            "total_requests": count,
            "estimated_cost": round(sum(record.estimated_cost for record in records), 8),
            "average_latency_ms": round(
                sum(record.latency_ms for record in records) / count if count else 0, 2
            ),
            "error_rate": round(
                100 * sum(record.status_code >= 400 for record in records) / count
                if count
                else 0,
                2,
            ),
            "p50_latency_ms": InMemoryRepository._percentile_cont(latencies, 0.5),
            "p95_latency_ms": InMemoryRepository._percentile_cont(latencies, 0.95),
            "p99_latency_ms": InMemoryRepository._percentile_cont(latencies, 0.99),
            "success_requests": sum(record.status_code < 400 for record in records),
            "client_error_requests": sum(
                400 <= record.status_code < 500 for record in records
            ),
            "server_error_requests": sum(record.status_code >= 500 for record in records),
            "latency_under_1s": in_band(0, 1000),
            "latency_1_to_2s": in_band(1000, 2000),
            "latency_2_to_5s": in_band(2000, 5000),
            "latency_over_5s": in_band(5000, None),
        }

    @staticmethod
    def _change(current: float, previous: float) -> float | None:
        return None if previous == 0 else round((current - previous) / previous * 100, 2)

    def executive_overview(
        self, from_: datetime, to: datetime, filters: UsageFilters
    ) -> dict[str, Any]:
        previous_from = from_ - (to - from_)
        current_records = self._usage_between(from_, to, filters)
        previous_records = self._usage_between(previous_from, from_, filters)
        current = self._executive_totals(current_records)
        previous = self._executive_totals(previous_records)
        scope = cache_scope_for_filters(filters)
        if scope is not None:
            dimension_type, dimension_values = scope
            for totals, records, start, end in (
                (current, current_records, from_, to),
                (previous, previous_records, previous_from, from_),
            ):
                self._apply_metric_cache(
                    totals,
                    records,
                    start,
                    end,
                    dimension_type,
                    dimension_values,
                )
        return {
            "from": from_,
            "to": to,
            "previous_from": previous_from,
            "generated_at": datetime.now(UTC),
            "totals": current,
            "changes_percent": {
                key: self._change(float(current[key]), float(previous[key]))
                for key in current
            },
        }

    def distribution(
        self,
        from_: datetime,
        to: datetime,
        dimension: str,
        filters: UsageFilters,
        limit: int,
        split_by: str | None = None,
    ) -> list[dict[str, Any]]:
        fields = {
            "organization": ("organization_id", "organization"),
            "department": ("department_id", "department"),
            "project": ("project_id", "project"),
            "agent": ("agent_id", "agent"),
            "model": ("model_id", "model"),
            "user": ("user_id", "user"),
            # Runtime has no separate id in telemetry, so the name is its own key.
            "runtime": ("runtime", "runtime"),
        }
        id_field, name_field = fields[dimension]
        grouped: dict[tuple[str, str], list[TokenUsageRecord]] = {}
        records = self._usage_between(from_, to, filters)
        for record in records:
            key = (str(getattr(record, id_field)), str(getattr(record, name_field)))
            grouped.setdefault(key, []).append(record)
        total_cost = sum(record.estimated_cost for record in records)
        items = []
        for (id_, name), values in grouped.items():
            totals = self._executive_totals(values)
            if cache_distribution_supported(dimension, filters):
                self._apply_metric_cache(
                    totals, values, from_, to, dimension, (id_,)
                )
            cost = float(totals["estimated_cost"])
            breakdown: list[dict[str, Any]] = []
            if split_by is not None:
                split_id, split_name = fields[split_by]
                children: dict[tuple[str, str], list[TokenUsageRecord]] = {}
                for record in values:
                    child_key = (
                        str(getattr(record, split_id)),
                        str(getattr(record, split_name)),
                    )
                    children.setdefault(child_key, []).append(record)
                breakdown = sorted(
                    (
                        {
                            "id": child_id,
                            "name": child_name,
                            "total_tokens": sum(
                                item.input_tokens + item.cached_tokens + item.output_tokens
                                for item in child_values
                            ),
                            "total_requests": len(child_values),
                        }
                        for (child_id, child_name), child_values in children.items()
                    ),
                    key=lambda entry: -int(entry["total_tokens"]),
                )
            items.append(
                {
                    "id": id_,
                    "name": name,
                    **totals,
                    "share_percent": round(100 * cost / total_cost, 2) if total_cost else 0,
                    "breakdown": breakdown,
                }
            )
        return sorted(
            items,
            key=lambda item: (-int(item["total_tokens"]), str(item["name"])),
        )[:limit]

    @staticmethod
    def _request_summary(record: TokenUsageRecord) -> dict[str, Any]:
        return {
            "request_id": record.request_id,
            "correlation_id": record.correlation_id,
            "timestamp": record.ts,
            "organization_id": record.organization_id,
            "organization_name": record.organization,
            "department_id": record.department_id,
            "department_name": record.department,
            "project_id": record.project_id,
            "project_name": record.project,
            "agent_id": record.agent_id,
            "agent_name": record.agent,
            "user_id": record.user_id,
            "user_name": record.user,
            "model_id": record.model_id,
            "model_name": record.model,
            "request_source": record.request_source,
            "runtime": record.runtime,
            "prompt_tokens": record.input_tokens,
            "completion_tokens": record.output_tokens,
            "total_tokens": record.input_tokens + record.cached_tokens + record.output_tokens,
            "latency_ms": record.latency_ms,
            "status_code": record.status_code,
            "estimated_cost": record.estimated_cost,
            "error_message": record.error_message,
        }

    def list_usage_requests(
        self, from_: datetime, to: datetime, filters: UsageFilters, limit: int
    ) -> list[dict[str, Any]]:
        records = sorted(
            self._usage_between(from_, to, filters),
            key=lambda record: record.ts,
            reverse=True,
        )
        return [self._request_summary(record) for record in records[:limit]]

    def get_usage_request(self, request_id: str) -> dict[str, Any] | None:
        records = [
            record
            for record in self.usage_records
            if record.usage_domain == "apim"
            and (record.correlation_id == request_id or record.request_id == request_id)
        ]
        exact_matches = [record for record in records if record.correlation_id == request_id]
        record = max(
            exact_matches or records,
            key=lambda item: (item.ts, item.id),
            default=None,
        )
        if record is None:
            return None
        return {
            **self._request_summary(record),
            "provider": record.provider,
            "workflow": record.workflow,
            "run_id": record.run_id,
            "turn_index": record.turn_index,
            "cached_tokens": record.cached_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "estimated": record.estimated,
            "ingest_source": record.ingest_source,
            "ingest_error": record.ingest_error,
            "budget_admission": record.budget_admission,
            "model_admission": record.model_admission,
        }

    def observed_users(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in sorted(self.usage_records, key=lambda item: item.ts):
            if record.user_id == "unattributed" or record.usage_domain != "apim":
                continue
            latest[record.user_id] = {
                "user_id": record.user_id,
                "user_ref": record.user,
                "department_id": record.department_id,
            }
        return list(latest.values())

    def application_owners(self) -> list[dict[str, Any]]:
        return list(self.application_owner_rows)

    def list_usage_anomalies(
        self, from_: datetime, to: datetime, filters: UsageFilters, limit: int
    ) -> list[dict[str, Any]]:
        records = self._usage_between(from_, to, filters)
        return evaluate_anomaly_rules(records, self.anomaly_rules, to, limit)

    @staticmethod
    def _anomaly_rule(
        id_: UUID,
        name: str,
        metric_name: str,
        threshold_mode: str,
        threshold_value: float,
        minimum_sample_size: int,
        severity: str,
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "id": id_,
            "name": name,
            "description": "",
            "metric": metric_name,
            "threshold_mode": threshold_mode,
            "threshold_value": threshold_value,
            "minimum_sample_size": minimum_sample_size,
            "severity": severity,
            "scope_type": "global",
            "scope_id": None,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "updated_by": "system",
        }

    def list_anomaly_rules(self) -> list[dict[str, Any]]:
        return sorted(self.anomaly_rules, key=lambda item: (item["created_at"], str(item["id"])))

    def create_anomaly_rule(self, values: Mapping[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        rule = {"id": uuid4(), "created_at": now, "updated_at": now, **values}
        self.anomaly_rules.append(rule)
        return rule

    def update_anomaly_rule(
        self, rule_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        rule = next((item for item in self.anomaly_rules if item["id"] == rule_id), None)
        if rule is not None:
            rule.update(values, updated_at=datetime.now(UTC))
        return rule

    def delete_anomaly_rule(self, rule_id: UUID) -> bool:
        before = len(self.anomaly_rules)
        self.anomaly_rules = [item for item in self.anomaly_rules if item["id"] != rule_id]
        return len(self.anomaly_rules) < before

    @staticmethod
    def _provider(
        id_: UUID,
        name: str,
        provider_kind: str,
        now: datetime,
        auth_type: str = "none",
    ) -> dict[str, Any]:
        return {
            "id": id_,
            "name": name,
            "provider_kind": provider_kind,
            "endpoint_url": None,
            "auth_type": auth_type,
            "credential_ciphertext": None,
            "credential_hint": None,
            "enabled": True,
            "brand_key": (
                "github"
                if provider_kind == "github"
                else "anthropic"
                if provider_kind == "anthropic"
                else "microsoft_foundry"
                if provider_kind == "microsoft_foundry"
                else "generic"
            ),
            "config": {},
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _runtime(
        id_: UUID,
        provider: dict[str, Any],
        name: str,
        runtime_kind: str,
        now: datetime,
        *,
        gateway: dict[str, Any] | None = None,
        enabled: bool = True,
        is_default: bool = False,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": id_,
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "gateway_profile_id": gateway["id"] if gateway else None,
            "gateway_name": gateway["name"] if gateway else None,
            "name": name,
            "runtime_kind": runtime_kind,
            "enabled": enabled,
            "is_default": is_default,
            "brand_key": provider.get("brand_key", "generic"),
            "config": config or {},
            "allowed_roles": ["owner", "admin", "member"],
            "health_status": "unknown",
            "health_message": None,
            "last_checked_at": None,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _model(
        id_: UUID,
        provider: dict[str, Any],
        runtime: dict[str, Any],
        model_key: str,
        display_name: str,
        now: datetime,
        *,
        enabled: bool = True,
        is_default: bool = False,
        capabilities: list[str] | None = None,
        context_window: int | None = 128000,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        cached_cost_per_million: float | None = None,
        cache_write_cost_per_million: float | None = None,
        allowed_roles: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": id_,
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "runtime_id": runtime["id"],
            "runtime_name": runtime["name"],
            "model_key": model_key,
            "display_name": display_name,
            "family_key": (
                "claude"
                if "claude" in f"{model_key} {display_name}".lower()
                else "copilot"
                if "copilot" in f"{model_key} {display_name}".lower()
                else "openai"
                if any(
                    value in f"{model_key} {display_name}".lower()
                    for value in ("gpt", "openai")
                )
                else "generic"
            ),
            "upstream_model_id": model_key,
            "assignment_required": False,
            "publication_id": None,
            "enabled": enabled,
            "is_default": is_default,
            "capabilities": capabilities or ["chat"],
            "context_window": context_window,
            "input_cost_per_million": input_cost_per_million,
            "output_cost_per_million": output_cost_per_million,
            "cached_cost_per_million": cached_cost_per_million,
            "cache_write_cost_per_million": cache_write_cost_per_million,
            "allowed_roles": allowed_roles or ["owner", "admin", "member"],
            "created_at": now,
            "updated_at": now,
        }

    def overview(self, timezone: str) -> dict[str, Any]:
        now = datetime(2026, 7, 17, 8, tzinfo=UTC)
        top = [
            ("release-regression-check", 824600, 29),
            ("pull-request-review", 612300, 21.6),
            ("support-triage", 387900, 13.7),
        ]
        return {
            "as_of": now,
            "timezone": timezone,
            "today": {
                "from": now - timedelta(hours=16),
                "to": now,
                "totals": metric(188420, 92380, 28600, 14420, 61),
                "comparison_percent": -8.4,
            },
            "month": {
                "from": datetime(2026, 6, 30, 16, tzinfo=UTC),
                "to": now,
                "totals": metric(2840630, 1324100, 411000, 265400, 927),
                "comparison_percent": 12.7,
            },
            "top_workflows": [
                {
                    "workflow": name,
                    "totals": metric(et, 100000, 30000, 25000, 100),
                    "share_percent": share,
                }
                for name, et, share in top
            ],
            "unattributed_percent": 7.8,
            "estimated_percent": 3.2,
        }

    def trends(
        self,
        from_: datetime,
        to: datetime,
        interval: str,
        group_by: str,
        timezone: str,
        filters: UsageFilters,
    ) -> dict[str, Any]:
        fields = {
            "organization": ("organization_id", "organization"),
            "department": ("department_id", "department"),
            "project": ("project_id", "project"),
            "agent": ("agent_id", "agent"),
            "user": ("user_id", "user"),
            "model": ("model_id", "model"),
            "runtime": ("runtime", "runtime"),
            "workflow": ("workflow", "workflow"),
            "team": ("team", "team"),
        }
        id_field, name_field = fields[group_by] if group_by != "none" else (None, None)
        grouped: dict[tuple[datetime, str, str], list[TokenUsageRecord]] = {}
        for record in self._usage_between(from_, to, filters):
            if interval == "hour":
                bucket = record.ts.replace(minute=0, second=0, microsecond=0)
            elif interval == "week":
                start = record.ts - timedelta(days=record.ts.weekday())
                bucket = start.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                bucket = record.ts.replace(hour=0, minute=0, second=0, microsecond=0)
            key = "all" if id_field is None else str(getattr(record, id_field))
            label = "all" if name_field is None else str(getattr(record, name_field))
            grouped.setdefault((bucket, key, label), []).append(record)
        points: list[dict[str, Any]] = []
        for (bucket, key, label), records in sorted(grouped.items()):
            points.append(
                {
                    "bucket_start": bucket,
                    "key": key,
                    "label": label,
                    "totals": {
                        "et": sum(record.et for record in records),
                        "total_tokens": sum(
                            record.input_tokens + record.cached_tokens + record.output_tokens
                            for record in records
                        ),
                        "input_tokens": sum(record.input_tokens for record in records),
                        "cached_tokens": sum(record.cached_tokens for record in records),
                        "cache_read_tokens": sum(
                            max(record.cached_tokens - record.cache_write_tokens, 0)
                            for record in records
                        ),
                        "cache_write_tokens": sum(
                            record.cache_write_tokens for record in records
                        ),
                        "output_tokens": sum(record.output_tokens for record in records),
                        "calls": len(records),
                        "estimated_cost": sum(
                            record.estimated_cost or 0.0 for record in records
                        ),
                        "p95_latency_ms": InMemoryRepository._percentile_cont(
                            [float(record.latency_ms) for record in records], 0.95
                        ),
                        "failed_calls": sum(
                            1 for record in records if record.status_code >= 400
                        ),
                    },
                }
            )
        cache_dimension: str | None = None
        cache_values: tuple[str, ...] | None = None
        if group_by in CACHE_DIMENSION_FIELDS and cache_distribution_supported(
            group_by, filters
        ):
            cache_dimension = group_by
            cache_values = tuple(sorted({str(point["key"]) for point in points}))
        elif group_by == "none":
            scope = cache_scope_for_filters(filters)
            if scope is not None:
                cache_dimension, cache_values = scope
        if cache_dimension is not None:
            for point in points:
                bucket = point["bucket_start"]
                key = str(point["key"])
                assert isinstance(bucket, datetime)
                end = (
                    bucket + timedelta(hours=1)
                    if interval == "hour"
                    else bucket + timedelta(days=7 if interval == "week" else 1)
                )
                records = grouped[(bucket, key, str(point["label"]))]
                totals = point["totals"]
                assert isinstance(totals, dict)
                self._apply_metric_cache(
                    totals,
                    records,
                    bucket,
                    end,
                    cache_dimension,
                    (key,) if group_by != "none" else cache_values or (key,),
                )
        return {
            "from": from_,
            "to": to,
            "timezone": timezone,
            "interval": interval,
            "group_by": group_by,
            "points": points,
        }

    def _run(self, turns: int) -> dict[str, Any]:
        abnormal = turns > 10
        started = datetime(2026, 7, 17, 2 if not abnormal else 4, tzinfo=UTC)
        rows: list[dict[str, Any]] = []
        for index in range(turns):
            growth = 1 + index * 0.12 if abnormal else 1
            input_ = round((4100 if abnormal else 2200) * growth)
            cached = round(input_ * (0.18 if abnormal else 0.46))
            output = round((1200 if abnormal else 520) * growth)
            rows.append(
                {
                    "usage_id": f"usage-{turns}-{index + 1}",
                    "turn_index": index + 1,
                    "ts": started + timedelta(minutes=index * 2),
                    "model": "gpt-5.6-sol" if abnormal else "gpt-4.1-mini",
                    "input_tokens": input_,
                    "cached_tokens": cached,
                    "output_tokens": output,
                    "et": input_ + cached * 0.1 + output * 4,
                    "et_coeff_m": 1 if abnormal else 0.35,
                    "latency_ms": round((3300 if abnormal else 1450) * growth),
                    "status": "success",
                    "estimated": abnormal and index > 14,
                    "ingest_source": "eventhub",
                }
            )
        total_input = sum(int(row["input_tokens"]) for row in rows)
        total_cached = sum(int(row["cached_tokens"]) for row in rows)
        total_output = sum(int(row["output_tokens"]) for row in rows)
        return {
            "run_id": "run-foundry-loop-0717" if abnormal else "run-foundry-normal-0717",
            "started_at": started,
            "ended_at": rows[-1]["ts"],
            "team": "commerce-platform" if abnormal else "developer-experience",
            "user": "release-bot@smarthive.local" if abnormal else "lei@smarthive.local",
            "agent": "release-steward" if abnormal else "delivery-engineer",
            "workflow": "release-regression-check" if abnormal else "pull-request-review",
            "provider": "microsoft_foundry",
            "models": [rows[0]["model"]],
            "turn_count": turns,
            "totals": metric(
                sum(float(row["et"]) for row in rows),
                total_input,
                total_cached,
                total_output,
                turns,
            ),
            "latency_ms": sum(int(row["latency_ms"]) for row in rows),
            "estimated": any(bool(row["estimated"]) for row in rows),
            "turns": rows,
        }

    def list_runs(self, from_: datetime, to: datetime, limit: int) -> list[dict[str, Any]]:
        del from_, to
        return [self._run(4), self._run(18)][:limit]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return next(
            (
                run
                for run in self.list_runs(
                    datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC), 2
                )
                if run["run_id"] == run_id
            ),
            None,
        )

    def list_findings(self, from_: datetime, to: datetime, limit: int) -> list[dict[str, Any]]:
        del from_, to
        return self.findings[:limit]

    def update_finding(self, finding_id: UUID, update: AuditFindingUpdate) -> dict[str, Any] | None:
        finding = next((item for item in self.findings if item["id"] == finding_id), None)
        if finding is not None:
            finding.update(
                status=update.status.value,
                assignee=update.assignee,
                resolution_note=update.resolution_note,
                updated_at=datetime.now(UTC),
            )
        return finding

    def _optimization(self, id_: UUID, workflow: str, label: str, change: float) -> dict[str, Any]:
        now = datetime(2026, 7, 12, 8, tzinfo=UTC)
        return {
            "id": id_,
            "created_at": now,
            "workflow": workflow,
            "occurred_at": now - timedelta(days=2),
            "label": label,
            "notes": "从 28 个工具缩减为任务相关的 9 个",
            "comparison": {
                "window_days": 7,
                "before": metric(622400, 390000, 90000, 55850, 310),
                "after": metric(236500, 151000, 62000, 19825, 294),
                "et_change_percent": change,
                "token_change_percent": -59.4,
            },
        }

    def list_optimizations(self, limit: int) -> list[dict[str, Any]]:
        return self.optimizations[:limit]

    def create_optimization(self, create: OptimizationEventCreate) -> dict[str, Any]:
        item = self._optimization(uuid4(), create.workflow, create.label, -41.2)
        item["notes"] = create.notes
        item["occurred_at"] = create.occurred_at
        item["comparison"]["window_days"] = create.comparison_window_days
        self.optimizations.insert(0, item)
        return item
