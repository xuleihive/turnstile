from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol, cast, runtime_checkable
from uuid import UUID

from ..domain.budget_completion import rejection_completion, validate_terminal_observation
from ..domain.ledger import (
    BudgetPriceProjection,
    BudgetReservationResolution,
    BudgetReservationResolutionWrite,
    LedgerAdmissionReceipt,
    LedgerAttempt,
    LedgerAttemptIdentity,
    LedgerCharge,
    LedgerDispatch,
    LedgerPolicyUpdate,
    LedgerProtocolConflict,
    LedgerRecoveryCursor,
    LedgerScope,
    LedgerScopeSeed,
    LedgerSettlementTask,
    LedgerSummary,
)
from ..domain.models import ModelPrice
from ..integrations.atomic_ledger import AtomicTableLedger
from ..integrations.ledger import (
    ROLL_FORWARD_ACTOR,
    application_partition_key,
    partition_key,
)
from ..persistence.directory_catalog import catalog_for
from ..persistence.repository_contract import QueryRepository

logger = logging.getLogger(__name__)


@runtime_checkable
class BudgetPriceRepository(Protocol):
    def registry(self) -> dict[str, Sequence[dict[str, Any]]]: ...


def _initialize_model_prices(
    repository: BudgetPriceRepository, ledger: AtomicTableLedger, now: datetime,
) -> None:
    for model in repository.registry()["models"]:
        price = ModelPrice.from_registry(model)
        if price is not None:
            ledger.project_model_price(
                BudgetPriceProjection.create(UUID(str(model["id"])), price, now),
                create_only=True,
            )


@runtime_checkable
class BudgetScopeRepository(BudgetPriceRepository, Protocol):
    def active_budget_execution_rollout(self) -> dict[str, Any] | None: ...
    def roll_forward_budgets(self, period_start: date, changed_by: str) -> object: ...
    def roll_forward_gateway_application_budgets(self, period_start: date, actor: str) -> int: ...
    def prepare_budget_execution_period(
        self, epoch: UUID, period: date, known_people: Sequence[str],
    ) -> None: ...
    def budget_scope_seed_candidates(
        self, *, limit: int = 16, application_id: UUID | None = None,
    ) -> list[LedgerScopeSeed]: ...
    def activate_budget_scope_seed(self, seed: LedgerScopeSeed) -> None: ...
    def defer_budget_scope_seed(self, scope: LedgerScope) -> None: ...
    def complete_budget_policy(self, update: LedgerPolicyUpdate) -> None: ...


class BudgetScopeService:
    def __init__(self, repository: BudgetScopeRepository, ledger: AtomicTableLedger) -> None:
        self._repository, self._ledger = repository, ledger

    def prepare(
        self, *, application_id: UUID | None = None, gateway_profile_id: UUID | None = None,
        now: datetime | None = None,
    ) -> int:
        rollout = self._repository.active_budget_execution_rollout()
        if rollout is None:
            return 0
        if (
            gateway_profile_id is not None
            and gateway_profile_id not in rollout["gateway_profile_ids"]
        ):
            return 0
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        period = moment.date().replace(day=1)
        if rollout["prepared_period"] != period:
            self._repository.roll_forward_budgets(period, ROLL_FORWARD_ACTOR)
            self._repository.roll_forward_gateway_application_budgets(period, ROLL_FORWARD_ACTOR)
            catalog = catalog_for(cast(QueryRepository, self._repository))
            self._repository.prepare_budget_execution_period(
                rollout["epoch"], period, [person.id for person in catalog.users],
            )
        candidates = self._repository.budget_scope_seed_candidates(application_id=application_id)
        if application_id is not None and not candidates:
            raise RuntimeError("Application execution scope is not yet prepared")
        prepared = 0
        prices_initialized = False
        for seed in candidates:
            try:
                scope = seed.summary.scope
                if (
                    seed.policy.budget_mode == "usd" and seed.policy.enabled
                    and not prices_initialized
                ):
                    _initialize_model_prices(self._repository, self._ledger, moment)
                    prices_initialized = True
                if not seed.active:
                    legacy = (
                        application_partition_key(scope.scope_id, f"{scope.period_start:%Y-%m}")
                        if scope.scope_type == "application"
                        else partition_key(scope.scope_id, f"{scope.period_start:%Y-%m}")
                    )
                    if self._ledger.list_reservations(legacy):
                        raise ValueError("Legacy outstanding charges prevent scope initialization")
                    self._ledger.initialize(seed.summary)
                    self._repository.activate_budget_scope_seed(seed)
                policy = seed.policy
                self._ledger.write_policy(
                    policy.scope, epoch=policy.epoch, revision=policy.revision,
                    token_limit=policy.token_limit, enforce=policy.enforce, enabled=policy.enabled,
                    budget_mode=policy.budget_mode, usd_limit=policy.usd_limit,
                )
                self._repository.complete_budget_policy(policy)
                prepared += 1
            except Exception as error:
                self._repository.defer_budget_scope_seed(seed.summary.scope)
                if application_id is not None:
                    raise
                logger.warning(
                    "Budget scope preparation deferred error_type=%s", type(error).__name__,
                )
        return prepared


@runtime_checkable
class BudgetStatusRepository(Protocol):
    def budget_execution_status(self, scope: LedgerScope) -> dict[str, Any] | None: ...


def execution_budget_view(
    repository: BudgetStatusRepository,
    store: AtomicTableLedger,
    scope: LedgerScope,
) -> dict[str, Any] | None:
    unknown: dict[str, Any] = {
        "ledger_source": "execution",
        "ledger_status": "unavailable",
        "ledger_snapshot_at": None,
        "ledger_read_at": None,
        "ledger_version": None,
        "available_tokens": None,
        "ledger_balance_tokens": None,
        "pending_reserved_tokens": None,
        "pending_reservation_count": None,
        "finalized_upper_bound_tokens": None,
        "finalized_upper_bound_count": None,
        "stale_reservation_count": None,
        "oldest_reservation_at": None,
        "pending_reserved_usd": None,
        "finalized_upper_bound_usd": None,
    }
    try:
        status = repository.budget_execution_status(scope)
        if status is None:
            return None
        if status["state"] == "prepared":
            return {**unknown, "ledger_status": "prepared"}
        summary = store.read_summary(scope)
        if summary.epoch != status["epoch"]:
            raise ValueError("Execution ledger epoch does not match the registered scope")
        if summary.budget_mode == "usd":
            return {
                **unknown,
                "ledger_status": "current",
                "ledger_read_at": datetime.now(UTC),
                "ledger_snapshot_at": summary.updated_at,
                "ledger_version": summary.version,
                "budget_mode": "usd",
                "token_limit": None,
                "usd_limit": summary.usd_limit,
                "enforce": summary.enforce,
                "used_tokens": 0,
                "remaining_tokens": None,
                "used_usd": summary.confirmed_usd,
                "remaining_usd": max(summary.available_usd or 0, 0),
                "usd_cost_available": True,
                "usage_percent": (
                    float(summary.confirmed_usd / summary.usd_limit * 100)
                    if summary.usd_limit else 0
                ),
                "pending_reserved_usd": summary.active_reserved_usd,
                "finalized_upper_bound_usd": summary.upper_bound_usd,
                "policy_pending": summary.policy_revision < status["policy_revision"],
            }
        return {
            **unknown,
            "ledger_status": "current",
            "ledger_read_at": datetime.now(UTC),
            "ledger_snapshot_at": summary.updated_at,
            "ledger_version": summary.version,
            "token_limit": summary.token_limit,
            "enforce": summary.enforce,
            "used_tokens": summary.confirmed_tokens,
            "remaining_tokens": max(summary.token_limit - summary.confirmed_tokens, 0),
            "usage_percent": 100 * summary.confirmed_tokens / summary.token_limit
            if summary.token_limit
            else 0,
            "available_tokens": max(summary.available_tokens, 0),
            "ledger_balance_tokens": summary.available_tokens,
            "pending_reserved_tokens": summary.active_reserved_tokens,
            "finalized_upper_bound_tokens": summary.upper_bound_tokens,
            "policy_pending": summary.policy_revision < status["policy_revision"],
        }
    except Exception as error:
        logger.warning("Execution balance read failed error_type=%s", type(error).__name__)
        return unknown


@runtime_checkable
class BudgetSettlementRepository(Protocol):
    def claim_budget_settlement(
        self,
        *,
        usage_id: str | None = None,
        lease_seconds: int = 30,
    ) -> LedgerSettlementTask | None: ...

    def complete_budget_settlement(self, task: LedgerSettlementTask) -> bool: ...

    def retry_budget_settlement(self, task: LedgerSettlementTask, error_code: str) -> bool: ...

    def budget_settlement_health(self) -> dict[str, int | float]: ...


@runtime_checkable
class BudgetRecoveryRepository(BudgetPriceRepository, Protocol):
    def budget_recovery_scopes(self, limit: int = 8) -> list[LedgerRecoveryCursor]: ...

    def advance_budget_recovery(
        self,
        cursor: LedgerRecoveryCursor,
        next_partition: str | None,
        next_row: str | None,
    ) -> bool: ...

    def register_budget_dispatch(self, dispatch: LedgerDispatch) -> None: ...

    def pending_budget_completions(
        self, limit: int = 8, *, usage_id: str | None = None,
    ) -> list[LedgerAdmissionReceipt]: ...

    def budget_terminal_observations(
        self, receipt: LedgerAdmissionReceipt,
    ) -> list[dict[str, Any]]: ...

    def record_budget_completion(
        self,
        receipt: LedgerAdmissionReceipt,
        completion: dict[str, Any] | None,
    ) -> None: ...

    def pending_budget_policies(self, limit: int = 16) -> list[LedgerPolicyUpdate]: ...

    def budget_switch_usage(self, update: LedgerPolicyUpdate) -> dict[str, Any] | None: ...

    def complete_budget_policy(self, update: LedgerPolicyUpdate) -> None: ...

    def unverified_execution_attempts(self, limit: int = 16) -> list[LedgerAttemptIdentity]: ...

    def complete_execution_finalization(self, identity: LedgerAttemptIdentity) -> None: ...


class BudgetRecoveryService:
    def __init__(self, repository: BudgetRecoveryRepository, ledger: AtomicTableLedger) -> None:
        self._repository, self._ledger = repository, ledger

    def recover(self, *, seconds: float = 15, scopes: int = 16) -> int:
        started, recovered = time.monotonic(), 0
        for cursor in self._repository.budget_recovery_scopes(scopes):
            if time.monotonic() - started >= seconds:
                break
            try:
                plans, next_partition, next_row = self._ledger.pending_dispatch_page(cursor)
                for plan in plans:
                    if time.monotonic() - started >= seconds:
                        self._repository.advance_budget_recovery(
                            cursor, cursor.next_partition, cursor.next_row,
                        )
                        return recovered
                    if plan.state == "preparing" and plan.expires_at > datetime.now(UTC):
                        continue
                    if plan.state in {"preparing", "cancelled"}:
                        if not self._ledger.cancel_dispatch(plan, recovery=True):
                            continue
                        self._ledger.compensate_dispatch(plan)
                        receipts = []
                        for receipt in plan.receipts:
                            retained = self._ledger.read_attempt(receipt)
                            receipts.append(LedgerAdmissionReceipt(
                                identity=receipt.identity, epoch=receipt.epoch,
                                reserved_tokens=receipt.reserved_tokens,
                                budget_mode=retained.budget_mode,
                                reserved_usd=retained.reserved_usd,
                                price_snapshot=retained.price_snapshot,
                            ))
                        registered = plan.model_copy(update={"receipts": receipts})
                    else:
                        registered = plan
                    self._repository.register_budget_dispatch(registered)
                    self._ledger.finish_dispatch_recovery(plan)
                    recovered += 1
                self._repository.advance_budget_recovery(cursor, next_partition, next_row)
            except Exception as error:
                logger.warning(
                    "Budget dispatch recovery deferred error_type=%s", type(error).__name__
                )
                self._repository.advance_budget_recovery(
                    cursor, cursor.next_partition, cursor.next_row,
                )
        return recovered

    def completions(
        self, *, limit: int = 8, seconds: float = 10, usage_id: str | None = None,
    ) -> int:
        recovered, started = 0, time.monotonic()
        for receipt in self._repository.pending_budget_completions(limit, usage_id=usage_id):
            if time.monotonic() - started >= seconds:
                break
            try:
                attempt = self._ledger.read_attempt(receipt)
                evidence = attempt.completion_evidence or attempt.rejection_evidence
                if evidence is None and attempt.rejection_policy is not None:
                    observations = [
                        validate_terminal_observation(value)
                        for value in self._repository.budget_terminal_observations(receipt)
                    ]
                    digests = {value["rejection"]["observation_sha256"] for value in observations}
                    if len(digests) > 1:
                        raise ValueError("Conflicting terminal observations require review")
                    if observations:
                        observed = observations[0]
                        decision = rejection_completion(
                            attempt.rejection_policy, observed["rejection"],
                            source=observed["source"], observed_at=observed["observed_at"],
                            admitted_at=receipt.identity.admitted_at,
                        )
                        if decision is not None:
                            _, retained = self._ledger.complete(receipt, decision)
                            evidence = retained.completion_evidence or retained.rejection_evidence
                self._repository.record_budget_completion(receipt, evidence)
                recovered += int(evidence is not None)
            except Exception as error:
                logger.warning(
                    "Completion evidence recovery deferred error_type=%s", type(error).__name__
                )
                self._repository.record_budget_completion(receipt, None)
        return recovered

    def policies(self, *, limit: int = 8) -> int:
        applied = 0
        prices_initialized = False
        for update in self._repository.pending_budget_policies(limit):
            try:
                current = self._ledger.read_summary(update.scope)
                if current.epoch != update.epoch or current.policy_revision > update.revision:
                    continue
                opening_balance: int | Decimal | None = None
                if current.budget_mode != update.budget_mode:
                    current = self._ledger.quiesce_budget_scope(
                        update.scope, update.epoch, update.revision,
                    )
                    if current.policy_revision >= update.revision:
                        continue
                    if (current.active_reserved_tokens or current.upper_bound_tokens
                        or current.active_reserved_usd or current.upper_bound_usd):
                        continue
                    if update.scope.scope_type == "person":
                        plans, next_partition, next_row = self._ledger.pending_dispatch_page(
                            LedgerRecoveryCursor(scope=update.scope, epoch=update.epoch, version=0),
                            limit=1,
                        )
                        if plans or next_partition or next_row:
                            continue
                    usage = self._repository.budget_switch_usage(update)
                    source_field = "usd" if current.budget_mode == "usd" else "tokens"
                    source_balance = (current.confirmed_usd if current.budget_mode == "usd"
                                      else current.confirmed_tokens)
                    if usage is None or usage[source_field] != source_balance:
                        continue
                    opening_balance = usage["usd" if update.budget_mode == "usd" else "tokens"]
                    if opening_balance is None:
                        continue
                if update.budget_mode == "usd" and update.enabled and not prices_initialized:
                    _initialize_model_prices(self._repository, self._ledger, datetime.now(UTC))
                    prices_initialized = True
                applied_policy = self._ledger.write_policy(
                    update.scope,
                    epoch=update.epoch,
                    revision=update.revision,
                    token_limit=update.token_limit,
                    enforce=update.enforce,
                    enabled=update.enabled,
                    budget_mode=update.budget_mode,
                    usd_limit=update.usd_limit,
                    **({"opening_balance": opening_balance} if opening_balance is not None else {}),
                )
                if applied_policy.policy_revision != update.revision:
                    continue
                self._repository.complete_budget_policy(update)
                applied += 1
            except Exception as error:
                logger.warning(
                    "Budget policy delivery deferred error_type=%s", type(error).__name__
                )
        return applied

    def finalize_uncertain(self, *, limit: int = 8) -> int:
        finalized = 0
        for identity in self._repository.unverified_execution_attempts(limit):
            try:
                self._ledger.finalize_uncertain(identity)
                self._repository.complete_execution_finalization(identity)
                finalized += 1
            except Exception as error:
                logger.warning("Budget finalization deferred error_type=%s", type(error).__name__)
        return finalized


@runtime_checkable
class BudgetResolutionRepository(Protocol):
    def budget_reservation_receipts(
        self, scope: LedgerScope, *, limit: int = 20, include_resolved: bool = False,
    ) -> list[LedgerAdmissionReceipt]: ...

    def get_budget_resolution(self, resolution_id: UUID) -> BudgetReservationResolution | None: ...

    def approve_budget_resolution(
        self, write: BudgetReservationResolutionWrite, actor: str,
    ) -> BudgetReservationResolution: ...


class BudgetReservationService:
    def __init__(self, repository: BudgetResolutionRepository, ledger: AtomicTableLedger) -> None:
        self._repository, self._ledger = repository, ledger

    def review(
        self, scope: LedgerScope, *, limit: int = 20, include_resolved: bool = False,
    ) -> list[LedgerAttempt]:
        receipts = self._repository.budget_reservation_receipts(
            scope, limit=limit, include_resolved=include_resolved,
        )
        return [self._ledger.read_attempt(receipt) for receipt in receipts]

    def approve(
        self, write: BudgetReservationResolutionWrite, actor: str,
    ) -> BudgetReservationResolution:
        prior = self._repository.get_budget_resolution(write.id)
        if prior is not None:
            if (
                prior.receipt != write.receipt or prior.actor != actor
                or prior.reason != write.reason or prior.evidence_sha256 != write.evidence_sha256
            ):
                raise LedgerProtocolConflict("Resolution idempotency identity conflicts")
            return prior
        attempt = self._ledger.read_attempt(write.receipt)
        if attempt.state in {"settled", "released", "cancelled"} or attempt.evidence_rank:
            raise LedgerProtocolConflict("Reservation already has a settlement decision")
        return self._repository.approve_budget_resolution(write, actor)


class SettlementLedger(Protocol):
    def settle(self, charge: LedgerCharge) -> tuple[LedgerSummary, LedgerAttempt]: ...


class BudgetSettlementService:
    def __init__(self, repository: BudgetSettlementRepository, ledger: SettlementLedger) -> None:
        self._repository = repository
        self._ledger = ledger

    def drain(self, usage_id: str | None = None, *, limit: int = 32, seconds: float = 20) -> int:
        started = time.monotonic()
        completed = 0
        for _ in range(max(1, min(limit, 100))):
            if time.monotonic() - started >= seconds:
                break
            task = self._repository.claim_budget_settlement(usage_id=usage_id, lease_seconds=120)
            if task is None:
                break
            try:
                if task.lease_until <= datetime.now(UTC):
                    raise RuntimeError("Settlement claim expired before execution")
                self._ledger.settle(task.charge)
                if self._repository.complete_budget_settlement(task):
                    completed += 1
                    logger.info(
                        "Budget settlement applied age_seconds=%.3f attempt=%d",
                        max(0, (datetime.now(UTC) - task.created_at).total_seconds()),
                        task.attempts,
                    )
                else:
                    logger.warning("Budget settlement acknowledgement fenced by an expired claim")
            except Exception as error:
                self._repository.retry_budget_settlement(task, type(error).__name__)
                logger.warning("Budget settlement deferred error_type=%s", type(error).__name__)
        return completed
