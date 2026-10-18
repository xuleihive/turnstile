from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from ..domain.application_access import (
    GatewayApplicationLedgerState,
    UsageApplicationAttribution,
)
from ..domain.billable_requests import BillableRequestAttempt, BillableRequestPlan
from ..domain.control_plane import GatewayPublication
from ..domain.gateway_targets import (
    ApimOnboardingPlan,
    ApimTarget,
    ApimTargetRouteBinding,
    GatewayDeployment,
    GatewayDeploymentInventory,
    GatewayInstanceObservation,
    SelfHostedGatewayProvisionSpec,
    UsageGatewayAttribution,
)
from ..domain.ledger import (
    BudgetReservationAdmission,
    BudgetReservationFinalization,
    LedgerAdmissionReceipt,
)
from ..domain.models import (
    ApimCacheReadBucket,
    AuditFindingUpdate,
    ModelIdentity,
    ModelPrice,
    OptimizationEventCreate,
    ReconciledUsage,
    TokenUsageRecord,
    UsageQueryEntityCatalog,
)
from ..domain.runtime_models import PlatformInvocationAttempt, PlatformInvocationCompletion
from .repository_support import UsageFilters


class OpsDbProxy(Protocol):
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
    ) -> None: ...

    def model_prices(self) -> dict[str, ModelPrice]: ...

    def model_identities(self) -> dict[str, ModelIdentity]: ...

    def get_gateway_publication(self, publication_id: UUID) -> dict[str, Any] | None: ...

    def gateway_application_attribution_map(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]: ...


class QueryRepository(ABC):
    def model_availability_supported(self) -> bool:
        return False

    def list_self_hosted_gateway_provisions(self) -> Sequence[dict[str, Any]]:
        raise NotImplementedError

    def retry_self_hosted_gateway_provision(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def finish_self_hosted_gateway_provision(
        self, operation_id: UUID, worker_id: str, spec: SelfHostedGatewayProvisionSpec,
        deployment: GatewayDeployment, inventory: GatewayDeploymentInventory,
        checkpoint: Mapping[str, Any],
        *, enqueue_inventory_sync: bool = False,
    ) -> None:
        raise NotImplementedError

    def begin_platform_invocation(self, attempt: PlatformInvocationAttempt) -> None:
        raise NotImplementedError("Durable platform invocation records are required")

    def finish_platform_invocation(self, completion: PlatformInvocationCompletion) -> None:
        raise NotImplementedError("Durable platform invocation records are required")

    @abstractmethod
    def create_apim_onboarding_operation(
        self, plan: ApimOnboardingPlan, credential_ciphertext: bytes, actor: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list_apim_onboarding_operations(self) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def retry_apim_onboarding_operation(self, operation_id: UUID, actor: str) -> dict[str, Any]: ...

    @abstractmethod
    def finish_apim_onboarding_operation(
        self, operation_id: UUID, worker_id: str, credential_ciphertext: bytes,
        credential_hint: str, configuration_sha256: str,
        fixed_publication: GatewayPublication | None = None,
    ) -> dict[str, Any] | None: ...

    def enroll_apim_onboarding_budget_target(
        self, operation_id: UUID, worker_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError("APIM initialization requires durable budget execution metadata")

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    def model_prices(self) -> dict[str, ModelPrice]: ...

    @abstractmethod
    def model_identities(self) -> dict[str, ModelIdentity]: ...

    @abstractmethod
    def reconciliation_watermark(self, source: str) -> datetime | None: ...

    @abstractmethod
    def oldest_unreconciled_usage(self, since: datetime) -> datetime | None: ...

    @abstractmethod
    def save_reconciliation_state(
        self, source: str, watermark: datetime, matched: int, scanned: int
    ) -> None: ...

    @abstractmethod
    def apply_reconciled_usage(self, items: Sequence[ReconciledUsage]) -> int: ...

    @abstractmethod
    def upsert_apim_cache_read_buckets(
        self,
        api_id: str,
        window_start: datetime,
        window_end: datetime,
        items: Sequence[ApimCacheReadBucket],
    ) -> int: ...

    @abstractmethod
    def apim_cache_read_totals(
        self,
        from_: datetime,
        to: datetime,
        dimension_type: str,
        dimension_values: Sequence[str] | None = None,
    ) -> dict[str, int]: ...

    @abstractmethod
    def overview(self, timezone: str) -> dict[str, Any]: ...

    @abstractmethod
    def executive_overview(
        self, from_: datetime, to: datetime, filters: UsageFilters
    ) -> dict[str, Any]: ...

    @abstractmethod
    def distribution(
        self,
        from_: datetime,
        to: datetime,
        dimension: str,
        filters: UsageFilters,
        limit: int,
        split_by: str | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_usage_requests(
        self,
        from_: datetime,
        to: datetime,
        filters: UsageFilters,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_usage_request(self, request_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def observed_users(self) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def historical_entities(
        self,
        from_: datetime,
        to: datetime,
        filters: UsageFilters,
    ) -> UsageQueryEntityCatalog: ...

    @abstractmethod
    def application_owners(self) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_usage_anomalies(
        self, from_: datetime, to: datetime, filters: UsageFilters, limit: int
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_anomaly_rules(self) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def create_anomaly_rule(self, values: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def update_anomaly_rule(
        self, rule_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def delete_anomaly_rule(self, rule_id: UUID) -> bool: ...



























    @abstractmethod
    def list_pinned_reports(self, owner_id: str) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_pinned_report(self, report_id: UUID, owner_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def create_pinned_report(
        self,
        *,
        owner_id: str,
        title: str,
        description: str,
        original_question: str,
        chart: Mapping[str, Any],
        directory_scope: list[str] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def add_chart_to_pinned_report(
        self,
        *,
        report_id: UUID,
        owner_id: str,
        original_question: str,
        chart: Mapping[str, Any],
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_pinned_report(
        self, report_id: UUID, owner_id: str, title: str, description: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_pinned_report_visibility(
        self, report_id: UUID, owner_id: str, visibility: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_pinned_report_layout(
        self, report_id: UUID, owner_id: str, layout: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def replace_pinned_chart_data(
        self, chart_id: UUID, owner_id: str, chart: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def delete_pinned_report(self, report_id: UUID, owner_id: str) -> bool: ...

    @abstractmethod
    def delete_pinned_chart(self, chart_id: UUID, owner_id: str) -> bool: ...

    @abstractmethod
    def reorder_pinned_reports(self, owner_id: str, ordered_ids: Sequence[UUID]) -> None: ...

    @abstractmethod
    def reorder_pinned_charts(
        self, report_id: UUID, owner_id: str, ordered_ids: Sequence[UUID]
    ) -> None: ...

    @abstractmethod
    def list_conversations(self, owner_id: str, limit: int) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_conversation(
        self, conversation_id: UUID, owner_id: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def append_conversation_turn(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        title: str,
        question: str,
        reply: Mapping[str, Any],
        directory_scope: list[str] | None = None,
    ) -> None: ...

    @abstractmethod
    def rename_conversation(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def summarise_conversation_title(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def delete_conversation(self, conversation_id: UUID, owner_id: str) -> bool: ...

    @abstractmethod
    def assistant_settings(self) -> dict[str, Any]: ...

    @abstractmethod
    def save_assistant_settings(
        self,
        *,
        model_id: UUID | None,
        auto_title: bool,
        updated_by: str,
        api_format: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def trends(
        self,
        from_: datetime,
        to: datetime,
        interval: str,
        group_by: str,
        timezone: str,
        filters: UsageFilters,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list_runs(self, from_: datetime, to: datetime, limit: int) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_findings(
        self, from_: datetime, to: datetime, limit: int
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def update_finding(
        self, finding_id: UUID, update: AuditFindingUpdate
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_optimizations(self, limit: int) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def create_optimization(self, create: OptimizationEventCreate) -> dict[str, Any]: ...

    @abstractmethod
    def list_token_budgets(
        self,
        period_start: date,
        user_id: str | None = None,
        *,
        allowed_department_ids: tuple[str, ...] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def token_usage_by_budget_scope(
        self,
        from_: datetime,
        to: datetime,
        user_id: str | None = None,
        *,
        allowed_department_ids: tuple[str, ...] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def personal_usage(
        self, user_id: str, from_: datetime, to: datetime, interval: str, timezone: str
    ) -> dict[str, Any]: ...

    @abstractmethod
    def upsert_token_budget(
        self,
        period_start: date,
        scope_type: str,
        scope_id: str,
        parent_scope_id: str | None,
        token_limit: int | None,
        warning_threshold_percent: int,
        changed_by: str,
        *,
        budget_mode: str = "tokens",
        usd_limit: Any = None,
    ) -> None: ...

    @abstractmethod
    def switch_organization_budget(
        self, period_start: date, organization_id: str, budget_mode: str,
        amount: int | Decimal, warning_threshold_percent: int, changed_by: str,
    ) -> None: ...

    @abstractmethod
    def delete_token_budget(
        self, period_start: date, scope_type: str, scope_id: str, changed_by: str
    ) -> bool: ...

    @abstractmethod
    def roll_forward_budgets(
        self, period_start: date, changed_by: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_token_budget_audit(
        self,
        period_start: date,
        limit: int,
        *,
        allowed_department_ids: tuple[str, ...] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_user_model_policies(
        self, user_ids: Sequence[str]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_user_model_access_audit(
        self,
        user_ids: Sequence[str],
        from_: datetime,
        to: datetime,
        limit: int,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_department_enforcement(
        self,
        *,
        allowed_department_ids: tuple[str, ...] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def set_department_enforcement(
        self, department_id: str, mode: str, changed_by: str
    ) -> bool: ...

    @abstractmethod
    def list_department_enforcement_audit(
        self,
        from_: datetime,
        to: datetime,
        limit: int,
        *,
        allowed_department_ids: tuple[str, ...] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def budget_ledger_people(self, period_start: date) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def budget_ledger_snapshot(
        self, period_start: date, period_end: date
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def budget_evidence_effective_at(self) -> datetime | None: ...

    @abstractmethod
    def register_budget_reservations(self, items: Sequence[BudgetReservationAdmission]) -> None: ...

    @abstractmethod
    def reservation_evidence_correlations(
        self, scope_type: str, scope_id: str, request_ids: Sequence[str],
    ) -> Mapping[str, str]: ...

    @abstractmethod
    def settled_reservation_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: str,
        scope_id: str | None = None,
    ) -> set[str]: ...

    @abstractmethod
    def existing_usage_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: str,
        scope_id: str | None = None,
    ) -> set[str]: ...

    @abstractmethod
    def save_budget_reservation_finalizations(
        self, items: Sequence[BudgetReservationFinalization]
    ) -> int: ...

    @abstractmethod
    def reservation_finalization_kind(
        self, scope_type: str, scope_id: str, correlation_id: str
    ) -> str | None: ...

    @abstractmethod
    def reservation_finalization_kinds(
        self, scope_type: str, scope_id: str, correlation_ids: Sequence[str]
    ) -> Mapping[str, str]: ...

    @abstractmethod
    def budget_scope_confirmed_tokens(
        self,
        scope_type: str,
        scope_id: str,
        period_start: date,
        period_end: date,
    ) -> int: ...

    @abstractmethod
    def model_access_ledger_snapshot(
        self, user_ids: Sequence[str] | None = None
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def person_budget_state(
        self, user_id: str, period_start: date, period_end: date
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def bulk_upsert_user_budgets(
        self,
        period_start: date,
        department_id: str,
        entries: Sequence[tuple[str, Any]],
        warning_threshold_percent: int,
        changed_by: str,
        *,
        budget_mode: str = "tokens",
        selected_user_ids: Sequence[str] | None = None,
        model_ids: Sequence[UUID] | None = None,
    ) -> None: ...

    @abstractmethod
    def registry(self) -> dict[str, Sequence[dict[str, Any]]]: ...

    @abstractmethod
    def create_registry_item(self, kind: str, values: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def create_connection(
        self,
        provider_id: UUID | None,
        provider_values: Mapping[str, Any] | None,
        runtime_values: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    @abstractmethod
    def update_registry_item(
        self, kind: str, item_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def delete_runtime_if_empty(self, runtime_id: UUID) -> bool: ...

    @abstractmethod
    def delete_gateway_if_unused(self, gateway_id: UUID) -> bool: ...

    @abstractmethod
    def upsert_apim_target(self, target: ApimTarget) -> dict[str, Any]: ...

    @abstractmethod
    def get_apim_target(self, gateway_profile_id: UUID) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_apim_targets(self) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def upsert_gateway_deployment(
        self, deployment: GatewayDeployment, *, preserve_display_name: bool = False,
        retiring_deployment: GatewayDeployment | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def rename_gateway_deployment(self, deployment_id: UUID, display_name: str) -> bool: ...

    @abstractmethod
    def get_gateway_deployment(self, deployment_id: UUID) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_deployments(
        self, apim_target_id: UUID, *, include_retired: bool = False
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_gateway_deployments_for_targets(
        self, apim_target_ids: Sequence[UUID], *, include_retired: bool = False
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def upsert_gateway_instance_observation(
        self, observation: GatewayInstanceObservation
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list_gateway_instance_observations(
        self, deployment_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def upsert_gateway_deployment_inventory(
        self, inventory: GatewayDeploymentInventory
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_gateway_deployment_inventory(
        self, deployment_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_deployment_inventories(
        self, deployment_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def upsert_apim_target_route_binding(
        self, binding: ApimTargetRouteBinding
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_apim_target_route_binding(
        self, binding_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_apim_target_route_bindings(
        self,
        *,
        logical_model_id: UUID | None = None,
        apim_target_id: UUID | None = None,
        apim_target_ids: Sequence[UUID] | None = None,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def upsert_usage_gateway_attribution(
        self, attribution: UsageGatewayAttribution
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_usage_gateway_attribution(self, usage_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def create_gateway_publication(
        self,
        gateway_profile_id: UUID,
        desired_spec: Mapping[str, Any],
        desired_spec_sha256: str,
        publication_kind: str,
        created_by: str,
        credential_ciphertext: bytes | None = None,
        expected_base_release_id: UUID | None = None,
        initial_manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def requeue_gateway_publication(
        self,
        publication_id: UUID,
        created_by: str,
        credential_ciphertext: bytes | None,
        desired_spec: Mapping[str, Any] | None = None,
        desired_spec_sha256: str | None = None,
        *, image_probe_authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def resume_gateway_publication_authorization(
        self,
        publication_id: UUID,
        created_by: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def gateway_publication_credential(self, publication_id: UUID) -> bytes | None: ...

    @abstractmethod
    def delete_gateway_publication_credential(self, publication_id: UUID) -> None: ...

    @abstractmethod
    def list_gateway_publications(
        self, gateway_profile_id: UUID | None = None, limit: int = 50
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def archive_gateway_publication(self, publication_id: UUID, actor: str) -> None: ...

    @abstractmethod
    def all_gateway_publications(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def active_model_route_publications(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def record_managed_program_initialization(
        self, descriptor: Mapping[str, Any], expected_config_sha256: str,
    ) -> None: ...

    @abstractmethod
    def record_fixed_program_upgrade(
        self, descriptor: Mapping[str, Any], expected_config_sha256: str,
        *, before_record: Callable[[], None] | None = None,
    ) -> None: ...

    @abstractmethod
    def get_gateway_publication(self, publication_id: UUID) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_gateway_publications(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_gateway_profiles(
        self, gateway_profile_ids: Sequence[UUID] | None = None
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_gateway_publication_audit(
        self, publication_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def effective_gateway_publication(
        self, gateway_profile_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def effective_gateway_publications(
        self, gateway_profile_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_gateway_release_protection(
        self, publication_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_release_protections(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_gateway_release_protection_audit(
        self, publication_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def set_gateway_release_protection(
        self,
        publication_id: UUID,
        *,
        pinned: bool,
        protected_label: str | None,
        retain_until: datetime | None,
        updated_by: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def create_gateway_release_operation(
        self,
        gateway_profile_id: UUID,
        operation_kind: str,
        created_by: str,
        *,
        target_release_id: UUID | None = None,
        prior_release_id: UUID | None = None,
        confirmation_sha256: str | None = None,
        semantic_preview: Mapping[str, Any] | None = None,
        credential_ciphertext: bytes | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def gateway_release_operation_secret(self, operation_id: UUID) -> bytes | None: ...

    @abstractmethod
    def delete_gateway_release_operation_secret(self, operation_id: UUID) -> None: ...

    @abstractmethod
    def get_gateway_release_operation(
        self, operation_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_release_operations(
        self, gateway_profile_id: UUID, limit: int = 100
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_gateway_release_operations_for_gateways(
        self, gateway_profile_ids: Sequence[UUID], limit: int = 100
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def claim_gateway_release_operation(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def transition_gateway_release_operation(
        self,
        operation_id: UUID,
        expected_status: str,
        status: str,
        updates: Mapping[str, Any],
        actor: str,
        *, keep_lease: bool = False,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_release_operation_audit(
        self, operation_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    def enqueue_gateway_configuration_rollback(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def complete_gateway_configuration_rollback(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def complete_gateway_release_rollback(
        self,
        operation_id: UUID,
        target_release_id: UUID,
        prior_release_id: UUID,
        actor: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def save_gateway_release_integrity_snapshot(
        self,
        publication_id: UUID,
        operation_id: UUID | None,
        status: str,
        dependencies: Mapping[str, Any],
        issues: Sequence[str],
        checked_by: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def latest_gateway_release_integrity_snapshot(
        self, publication_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def latest_gateway_release_integrity_snapshots(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def save_gateway_release_gc_plan(
        self,
        operation_id: UUID,
        gateway_profile_id: UUID,
        retained_release_ids: Sequence[UUID],
        current_non_release_references: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        reference_graph_sha256: str,
        created_by: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_gateway_release_gc_plan(
        self, operation_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def sync_gateway_applications(
        self,
        gateway_profile_id: UUID,
        items: Sequence[Mapping[str, Any]],
        actor: str,
        period_start: date,
        default_token_limit: int,
        default_tokens_per_minute: int,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def provision_gateway_application(
        self,
        gateway_profile_id: UUID,
        value: Mapping[str, Any],
        actor: str,
        period_start: date,
        default_token_limit: int,
        default_tokens_per_minute: int,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list_gateway_applications(self) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_gateway_application(self, application_id: UUID) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_application_avatars(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_gateway_application_avatar(
        self, application_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_gateway_application_avatar(
        self,
        application_id: UUID,
        media_type: str | None,
        image_bytes: bytes | None,
        actor: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_gateway_application_budget(
        self,
        application_id: UUID,
        period_start: date,
        value: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_gateway_application_organization(
        self, application_id: UUID, organization_id: str | None, actor: str,
        *, department_id: str | None = None, update_department: bool = False,
        owner_id: str | None = None, update_owner: bool = False,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_gateway_application_model_access(
        self,
        application_id: UUID,
        mode: str,
        model_ids: Sequence[UUID],
        actor: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_application_subscriptions(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_gateway_application_budgets(
        self, period_start: date, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def get_effective_gateway_application_budget(
        self, application_id: UUID, period_start: date,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_gateway_application_ledger_states(
        self, period_start: date, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def save_gateway_application_ledger_states(
        self, items: Sequence[GatewayApplicationLedgerState]
    ) -> None: ...

    @abstractmethod
    def save_gateway_application_ledger_finalization_states(
        self, items: Sequence[Mapping[str, Any]]
    ) -> None: ...

    @abstractmethod
    def save_gateway_application_ledger_snapshot(
        self, states: Sequence[GatewayApplicationLedgerState],
        sidecars: Sequence[Mapping[str, Any]],
    ) -> None: ...

    @abstractmethod
    def list_gateway_application_model_access(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_usage(
        self,
        period_start: date,
        period_end: date,
        application_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_usage_activity(
        self,
        application_id: UUID,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_user_usage(
        self,
        period_start: date,
        period_end: date,
        application_id: UUID,
        limit: int = 100,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def list_gateway_application_audit(
        self, application_id: UUID, limit: int = 100
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_attribution_map(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_ledger_snapshot(
        self, period_start: date, period_end: date
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_ledger_applications(
        self, period_start: date
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_model_ledger_snapshot(
        self,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def gateway_application_ledger_mappings(
        self,
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def roll_forward_gateway_application_budgets(
        self, period_start: date, actor: str
    ) -> int: ...

    @abstractmethod
    def create_multi_target_rollout(
        self,
        rollout_id: UUID,
        rollout_kind: str,
        logical_model_id: UUID | None,
        request_sha256: str,
        created_by: str,
        children: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]: ...

    @abstractmethod
    def get_multi_target_rollout(
        self, rollout_id: UUID
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_multi_target_rollouts(
        self, limit: int = 50
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def claim_gateway_publication(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None: ...

    def renew_gateway_publication_lease(
        self, publication_id: UUID, worker_id: str, lease_seconds: int,
    ) -> bool:
        raise NotImplementedError

    def reserve_gateway_publication_probe(
        self, publication_id: UUID, worker_id: str, request_limit: int,
        method: str, endpoint_sha256: str,
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_probes(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_activation(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_writer(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_dependencies(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_credentials(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_preparation(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_publication_approval(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def checkpoint_gateway_program_promotion(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        raise NotImplementedError

    def reserve_gateway_release_operation_probe(
        self, operation_id: UUID, worker_id: str, request_limit: int,
        method: str, endpoint_sha256: str,
    ) -> bool:
        raise NotImplementedError

    def renew_gateway_release_operation_lease(
        self, operation_id: UUID, worker_id: str, lease_seconds: int,
    ) -> bool:
        raise NotImplementedError

    def begin_billable_request(self, plan: BillableRequestPlan) -> BillableRequestAttempt:
        raise NotImplementedError

    def recover_billable_request(self, plan: BillableRequestPlan) -> BillableRequestAttempt:
        raise NotImplementedError

    def finish_billable_request(
        self, request_id: UUID, *, actual_tokens: int | None,
        correlation_id: str | None, evidence: dict[str, Any],
    ) -> None:
        raise NotImplementedError

    def pending_billable_requests(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def settled_billable_requests(
        self, request_ids: list[str], scope_type: str, scope_id: str | None,
    ) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    def cancel_unstarted_gateway_publication(
        self, publication_id: UUID, actor: str
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def transition_gateway_publication(
        self,
        publication_id: UUID,
        expected_status: str,
        status: str,
        updates: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def activate_gateway_publication(
        self, publication_id: UUID, actor: str,
        *, validate_model_activation: Callable[
            [Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any] | None], None,
        ] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def invocation_route(
        self, runtime_id: UUID | None, model_id: UUID | None,
        *, gateway_profile_id: UUID | None = None,
        gateway_deployment_id: UUID | None = None,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def update_runtime_health(
        self, runtime_id: UUID, status: str, message: str, checked_at: datetime
    ) -> None: ...

    @abstractmethod
    def runtime_diagnostic_contexts(
        self, selections: Sequence[tuple[UUID, UUID, UUID, UUID]],
    ) -> Sequence[dict[str, Any]]: ...

    @abstractmethod
    def claim_runtime_diagnostic(
        self, runtime_id: UUID, marker: str, checked_at: datetime, before: datetime,
    ) -> bool: ...

    @abstractmethod
    def finish_runtime_diagnostic(
        self, runtime_id: UUID, marker: str, status: str, message: str, checked_at: datetime,
    ) -> bool: ...

    @abstractmethod
    def apply_model_price_sync(self, updates: Sequence[Mapping[str, Any]]) -> int:
        """Record what a price sync decided.

        Separate from `update_registry_item` because the sync writes fields no client may set:
        the outcome of the last run is something the system observed, not something a caller
        asserts. Returns how many rows had their charged rates actually changed.
        """
