"""Final, journaled admission gate for an independently verified installation.

Adapters hold the installation maintenance lock and supply real ARM/Table/SQL
readbacks. This module has no cloud credentials and never creates balances,
publications, usage or settlement evidence. Scope IDs and the promoted revision
come from the retained installation/promotion journals.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from scripts.install_budget_execution import BudgetInstallationPlan, InstallationJournal
from scripts.prepare_budget_execution import FenceConfiguration
from turnstile_core.domain.application_access import UsageApplicationAttribution
from turnstile_core.domain.budget_completion import usd_completion_charge, validate_completion
from turnstile_core.domain.budget_counting import BudgetOperation, verified_budget_operations
from turnstile_core.domain.gateway_targets import (
    UsageGatewayAttribution,
    canonical_usage_id_v2,
)
from turnstile_core.domain.ledger import (
    LedgerAdmissionReceipt,
    LedgerAttempt,
    LedgerCharge,
    LedgerDispatch,
    LedgerScope,
    LedgerSummary,
)
from turnstile_core.domain.models import StrictModel
from turnstile_core.integrations.table_batch import exact_etag

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0, strict=True)]
Money = Annotated[Decimal, Field(ge=0, max_digits=30, decimal_places=14)]
ACTIVATION_OPERATION = "activate-budget-execution"
_VALIDATION_FIELDS = (
    "validation_revision", "validation_application_id", "validation_subscription_id",
    "validation_person_id",
)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fresh(observed_at: datetime, now: datetime) -> None:
    if not 0 <= (now - observed_at).total_seconds() <= 30:
        raise ValueError("Activation requires readbacks no older than 30 seconds")


def _active_document(document: Mapping[str, Any]) -> dict[str, Any]:
    active = deepcopy(dict(document))
    active["mode"] = "active"
    for field in _VALIDATION_FIELDS:
        active.pop(field, None)
    FenceConfiguration.model_validate(active)
    return active


class FenceReadback(StrictModel):
    observed_at: AwareDatetime
    etag: str = Field(min_length=1)
    document: dict[str, Any] = Field(min_length=1)


class ActivationScopeReadback(StrictModel):
    """SQL scope row plus current-unit SQL totals and independently read Table S."""

    observed_at: AwareDatetime
    scope: LedgerScope
    epoch: UUID
    state: Literal["prepared", "active", "draining"]
    budget_mode: Literal["tokens", "usd"]
    policy_revision: Count
    policy_applied_revision: Count
    desired_limit: Count
    desired_usd_limit: Money | None
    desired_enforce: bool = Field(strict=True)
    desired_enabled: bool = Field(strict=True)
    sql_confirmed_tokens: Count
    sql_confirmed_usd: Money | None
    sql_pending_requests: Count
    sql_upper_bound_rows: Count
    sql_pending_settlements: Count
    summary: LedgerSummary
    summary_etag: str = Field(min_length=1)

    def require_ready(self, plan: BudgetInstallationPlan, now: datetime) -> None:
        _fresh(self.observed_at, now)
        exact_etag(self.summary_etag)
        state = self.summary
        if (self.state != "active" or self.epoch != plan.epoch or state.epoch != plan.epoch
            or self.scope != state.scope or self.scope.period_start != plan.period_start.date()
            or self.policy_revision < 1 or self.policy_revision != self.policy_applied_revision
            or state.policy_revision != self.policy_revision or state.updated_at > now):
            raise ValueError("Every planned scope must be active with its applied policy")
        if (state.budget_mode != self.budget_mode or state.token_limit != self.desired_limit
            or state.usd_limit != self.desired_usd_limit or state.enforce != self.desired_enforce
            or state.enabled != self.desired_enabled):
            raise ValueError("Table policy differs from the current SQL scope policy")
        if any((self.sql_pending_requests, self.sql_upper_bound_rows, self.sql_pending_settlements,
                state.active_reserved_tokens, state.upper_bound_tokens,
                state.active_reserved_usd, state.upper_bound_usd)):
            raise ValueError("Outstanding charges or settlements prevent activation")
        # USD S retains historical token counters; only its current unit is authoritative.
        if self.budget_mode == "tokens":
            agrees = self.sql_confirmed_tokens == state.confirmed_tokens
        else:
            agrees = (self.sql_confirmed_usd is not None
                      and self.sql_confirmed_usd == state.confirmed_usd)
        if not agrees:
            raise ValueError("Current-unit SQL consumption differs from the new execution ledger")


class AppliedSettlementReadback(StrictModel):
    id: int = Field(gt=0, strict=True)
    charge: LedgerCharge
    applied_revision: Count
    applied_at: AwareDatetime
    lease_until: AwareDatetime | None
    last_error: str | None


class SettledAttemptReadback(StrictModel):
    """A Table attempt, immutable SQL admission and its applied SQL settlement task."""

    attempt: LedgerAttempt
    attempt_etag: str = Field(min_length=1)
    sql_receipt: LedgerAdmissionReceipt
    sql_dispatch_id: UUID
    sql_caller_request_id: str
    sql_usage_id: str = Field(min_length=1)
    sql_usage_complete: bool = Field(strict=True)
    settlement: AppliedSettlementReadback


class FormalUsageReadback(StrictModel):
    """Selected token_usage columns joined to both formal attribution rows."""

    id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    model_id: UUID
    user_id: str = Field(min_length=1)
    ts: AwareDatetime
    status_code: int = Field(ge=100, le=599, strict=True)
    usage_domain: Literal["apim"]
    ingest_source: Literal["eventhub"]
    estimated: bool = Field(strict=True)
    ingest_error: str | None
    input_tokens: Count
    cached_tokens: Count
    cache_write_tokens: Count
    output_tokens: Count
    usd_cost: Money
    usd_price_sha256: Digest
    gateway: UsageGatewayAttribution
    application: UsageApplicationAttribution


class MixedAttemptReadback(StrictModel):
    observed_at: AwareDatetime
    operation: BudgetOperation
    dispatch: LedgerDispatch
    dispatch_etag: str = Field(min_length=1)
    person: SettledAttemptReadback
    application: SettledAttemptReadback
    usage: FormalUsageReadback

    def require_ready(
        self, plan: BudgetInstallationPlan, promoted_revision: str,
        scopes: Mapping[str, ActivationScopeReadback], now: datetime,
    ) -> None:
        _fresh(self.observed_at, now)
        exact_etag(self.dispatch_etag)
        fence, usage, dispatch = plan.expected_fence, self.usage, self.dispatch
        if (dispatch.state != "dispatching" or not dispatch.recovery_complete
            or len(dispatch.receipts) != 2 or usage.estimated or usage.ingest_error is not None
            or not 200 <= usage.status_code < 300 or usage.ts > now or dispatch.updated_at > now
            or self.person.settlement.id == self.application.settlement.id):
            raise ValueError("Mixed validation requires a real completed two-scope dispatch")
        gateway, application = usage.gateway, usage.application
        expected_id = canonical_usage_id_v2(
            gateway.apim_target_id, gateway.apim_service_id,
            gateway.native_gateway_id, usage.correlation_id,
        )
        if (usage.id != expected_id or gateway.usage_id != expected_id
            or gateway.apim_target_id != fence.gateway_profile_id
            or gateway.apim_service_id.casefold() != fence.apim_service_id.casefold()
            or gateway.api_revision != promoted_revision or gateway.event_schema_version != 2
            or gateway.attribution_source not in {"eventhub", "observer"}
            or gateway.native_gateway_id not in fence.gateways
            or application.application_id != fence.validation_application_id
            or application.apim_subscription_id != fence.validation_subscription_id
            or application.actor_type != "person" or application.person_id != usage.user_id
            or application.actor_id != usage.user_id
            or usage.user_id != fence.validation_person_id
            or gateway.observed_at.tzinfo is None or gateway.observed_at > now):
            raise ValueError("Formal usage does not match the reviewed mixed validation identity")
        contracts = [contract for contract in fence.model_contracts.values()
                     if contract.model_id == usage.model_id]
        if len(contracts) != 1 or self.operation not in verified_budget_operations(contracts[0]):
            raise ValueError("Mixed USD validation requires a verified model counting operation")
        contract = contracts[0].protocols.get(self.operation, contracts[0])
        evidence = validate_completion({
            "source": "observer" if gateway.attribution_source == "observer" else "apim",
            "observed_at": usage.ts.isoformat(), "input_tokens": usage.input_tokens,
            "cached_tokens": usage.cached_tokens, "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.cached_tokens + usage.output_tokens,
        })
        if evidence["total_tokens"] <= 0 or usage.usd_cost <= 0:
            raise ValueError("A zero-cost or empty response is not monetary execution evidence")
        for observed, receipt, kind, mode in zip(
            (self.person, self.application), dispatch.receipts,
            ("person", "application"), ("tokens", "usd"), strict=True,
        ):
            exact_etag(observed.attempt_etag)
            attempt, task = observed.attempt, observed.settlement
            identity, charge = attempt.identity, task.charge
            retained = LedgerAdmissionReceipt(
                identity=identity, epoch=attempt.epoch, reserved_tokens=attempt.reserved_tokens,
                budget_mode=attempt.budget_mode, reserved_usd=attempt.reserved_usd,
                price_snapshot=attempt.price_snapshot,
            )
            scope_id = usage.user_id if kind == "person" else str(application.application_id)
            if (receipt != retained or observed.sql_receipt != retained
                or identity.scope.scope_type != kind or identity.scope.scope_id != scope_id
                or identity.scope.partition not in scopes
                or identity.gateway_profile_id != fence.gateway_profile_id
                or identity.native_gateway_id != gateway.native_gateway_id
                or identity.correlation_id != usage.correlation_id
                or identity.model_id != str(usage.model_id) or identity.admitted_at > usage.ts
                or identity.admitted_at < plan.created_at or attempt.updated_at > now
                or contract.verified_at > identity.admitted_at
                or attempt.epoch != plan.epoch or attempt.state != "settled"
                or attempt.evidence_rank != 3 or attempt.budget_mode != mode
                or attempt.actual_tokens != evidence["total_tokens"]
                or observed.sql_dispatch_id != dispatch.owner
                or observed.sql_caller_request_id != dispatch.caller_request_id
                or observed.sql_usage_id != usage.id or observed.sql_usage_complete is not True):
                raise ValueError("Table, receipt and SQL admission disagree on the mixed attempt")
            if (charge.identity != identity or charge.epoch != plan.epoch
                or charge.budget_mode != mode or charge.total_tokens != attempt.actual_tokens
                or charge.evidence_rank != 3 or charge.actual_usd != attempt.actual_usd
                or charge.price_snapshot != attempt.price_snapshot
                or task.applied_revision != charge.revision or task.applied_at > now
                or task.applied_at < identity.admitted_at
                or task.lease_until is not None or task.last_error is not None):
                raise ValueError("The exact SQL settlement is not fully applied")
            summary = scopes[identity.scope.partition].summary
            if not summary.enabled or summary.budget_mode != mode:
                raise ValueError("Validated mixed scopes must retain their reviewed active units")
            if mode == "tokens":
                if summary.confirmed_tokens < evidence["total_tokens"]:
                    raise ValueError("Token consumption was lost after mixed validation")
            else:
                price = attempt.price_snapshot
                if (price is None or price.model_id != usage.model_id
                    or price.updated_at > identity.admitted_at
                    or usage.usd_price_sha256 != price.price_sha256
                    or usage.usd_cost != attempt.actual_usd
                    or summary.confirmed_usd < usage.usd_cost
                    or usd_completion_charge(price.model_dump(mode="json"), evidence)
                        != usage.usd_cost):
                    raise ValueError("USD usage and settlement differ from the admitted price")


class BudgetActivationReadback(StrictModel):
    observed_at: AwareDatetime
    fence: FenceReadback
    current_revision: str = Field(min_length=1)
    shared_policy_sha256: Digest
    revision_policy_sha256: dict[str, Digest]
    reachable_revisions: list[str] = Field(min_length=1)
    fenced_revisions: list[str] = Field(min_length=1)
    package_sha256: dict[str, Digest]
    rollout_epoch: UUID
    rollout_state: Literal["prepared", "active", "paused"]
    scope_sync_enabled: bool = Field(strict=True)
    prepared_period: date
    gateway_profile_ids: list[UUID] = Field(min_length=1)
    pending_publications: Count
    active_dispatches: Count
    pending_telemetry: Count
    pending_settlements: Count
    unresolved_dispatched_attempts: Count
    scopes: list[ActivationScopeReadback] = Field(min_length=1)
    mixed_attempts: list[MixedAttemptReadback] = Field(min_length=1)

    def require_ready(
        self, plan: BudgetInstallationPlan, expected_scopes: Sequence[LedgerScope],
        promoted_revision: str, shared_policy_sha256: str, now: datetime,
    ) -> None:
        plan.require_current(now)
        _fresh(self.observed_at, now)
        _fresh(self.fence.observed_at, now)
        exact_etag(self.fence.etag)
        fence = FenceConfiguration.model_validate(self.fence.document)
        if (fence != plan.expected_fence or fence.mode != "paused"
            or len(plan.gateway_profile_ids) != 1 or self.current_revision != promoted_revision
            or promoted_revision not in plan.policy_sha256
            or not all(promoted_revision in values for values in fence.gateways.values())
            or self.shared_policy_sha256 != shared_policy_sha256):
            raise ValueError("Activation requires the exact paused fence and promoted policies")
        revisions = set(self.reachable_revisions)
        if (len(revisions) != len(self.reachable_revisions)
            or len(set(self.fenced_revisions)) != len(self.fenced_revisions)
            or revisions != set(self.fenced_revisions) or revisions != plan.policy_sha256.keys()
            or self.revision_policy_sha256 != plan.policy_sha256
            or self.package_sha256 != plan.package_sha256):
            raise ValueError("Every reachable revision and all four runtime packages must match")
        if (self.rollout_epoch != plan.epoch or self.rollout_state != "active"
            or self.scope_sync_enabled is not True
            or self.prepared_period != plan.period_start.date()
            or set(self.gateway_profile_ids) != set(plan.gateway_profile_ids)
            or len(self.gateway_profile_ids) != len(plan.gateway_profile_ids)
            or any((self.pending_publications, self.active_dispatches, self.pending_telemetry,
                    self.pending_settlements, self.unresolved_dispatched_attempts))):
            raise ValueError("Coordinated rollout must be current with no outstanding work")
        planned = {scope.partition for scope in expected_scopes}
        scopes = {item.scope.partition: item for item in self.scopes}
        if (not planned or len(planned) != len(expected_scopes)
            or len(scopes) != len(self.scopes) or set(scopes) != planned):
            raise ValueError("Activation must cover each frozen installation scope exactly once")
        for item in self.scopes:
            item.require_ready(plan, now)
        for proof in self.mixed_attempts:
            proof.require_ready(plan, promoted_revision, scopes, now)


def _observed_active(
    intent: dict[str, Any], read_fence: Callable[[], FenceReadback], clock: Callable[[], datetime],
) -> dict[str, Any]:
    actual = read_fence()
    _fresh(actual.observed_at, clock())
    exact_etag(actual.etag)
    FenceConfiguration.model_validate(actual.document)
    if actual.etag == intent["prior_etag"] or actual.document != intent["active_document"]:
        raise ValueError("Activation outcome must be resolved by exact fence readback")
    return {**intent, "active_document": actual.document}


def activate_budget_installation(
    plan: BudgetInstallationPlan, journal: InstallationJournal, *,
    expected_scopes: Sequence[LedgerScope], promoted_revision: str,
    shared_policy_sha256: str, observe: Callable[[], BudgetActivationReadback],
    replace_fence: Callable[[str, str, dict[str, Any]], bool],
    read_fence: Callable[[], FenceReadback],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Replace only the existing F with its exact ETag; never retry an unknown write."""
    if journal.plan.digest != plan.digest:
        raise ValueError("Activation journal belongs to a different installation")
    proof = BudgetActivationReadback.model_validate(observe().model_dump())
    proof.require_ready(plan, expected_scopes, promoted_revision, shared_policy_sha256, clock())
    intent: dict[str, Any] = {
        "installation_plan_sha256": plan.digest,
        "scopes_sha256": _digest(sorted(scope.partition for scope in expected_scopes)),
        "promoted_revision": promoted_revision, "shared_policy_sha256": shared_policy_sha256,
        "readback_sha256": _digest(proof.model_dump(mode="json")),
        "partition": plan.expected_fence.partition, "prior_etag": proof.fence.etag,
        "active_document": _active_document(proof.fence.document),
    }

    def apply() -> None:
        # Recheck freshness after the intent fsync and compare F immediately before the PUT.
        proof.require_ready(plan, expected_scopes, promoted_revision, shared_policy_sha256, clock())
        current = read_fence()
        _fresh(current.observed_at, clock())
        if current.etag != proof.fence.etag or current.document != proof.fence.document:
            raise ValueError("Paused fence changed before activation")
        proof.require_ready(plan, expected_scopes, promoted_revision, shared_policy_sha256, clock())
        applied = replace_fence(
            intent["partition"], current.etag, deepcopy(intent["active_document"]),
        )
        if applied is not True:
            raise ValueError("Exact fence ETag no longer permits activation")

    return journal.perform(
        ACTIVATION_OPERATION, intent=intent, apply=apply,
        readback=lambda: _observed_active(intent, read_fence, clock),
    )


def recover_budget_activation(
    plan: BudgetInstallationPlan, journal: InstallationJournal, *,
    read_fence: Callable[[], FenceReadback],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Acknowledge the original F outcome only, even after plan expiry; never write/replay."""
    if journal.plan.digest != plan.digest:
        raise ValueError("Recovery journal belongs to a different installation")
    events = json.loads(journal.path.read_text())["events"]
    intents = [event["expected"] for event in events
               if event["operation"] == ACTIVATION_OPERATION and event["phase"] == "intent"]
    if len(intents) != 1:
        raise ValueError("Activation recovery requires exactly one original intent")
    intent = intents[0]
    if (intent["installation_plan_sha256"] != plan.digest
        or intent["partition"] != plan.expected_fence.partition
        or FenceConfiguration.model_validate(intent["active_document"]) !=
            FenceConfiguration.model_validate(_active_document(
                plan.expected_fence.model_dump(mode="json"),
            ))):
        raise ValueError("Recorded activation does not match the installation")
    return journal.recover(
        ACTIVATION_OPERATION, lambda: _observed_active(intent, read_fence, clock),
    )
