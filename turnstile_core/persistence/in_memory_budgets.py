from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from ..domain.application_access import UsageApplicationAttribution
from ..domain.billable_requests import BillableRequestAttempt
from ..domain.ledger import (
    BudgetReservationFinalization,
    LedgerScopeType,
    canonical_budget_tokens,
    strict_budget_evidence,
)
from ..domain.models import TokenUsageRecord
from .in_memory_budget_evidence import InMemoryBudgetEvidenceRepositoryMixin
from .repository_support import BudgetConstraintViolation


class InMemoryBudgetRepositoryMixin(InMemoryBudgetEvidenceRepositoryMixin):
    _billable_effective_tokens: Callable[[BillableRequestAttempt], int | None]
    budget_reservation_finalizations: list[BudgetReservationFinalization]
    # Supplied by the application and organization mixins; declared here because the
    # subscription rollup needs the department a key is filed under and that department's parent.
    gateway_applications: list[dict[str, Any]]
    org_units: Callable[[], Sequence[dict[str, Any]]]
    usage_application_attributions: dict[str, UsageApplicationAttribution]
    budget_roll_forward: dict[date, dict[str, Any]]
    department_enforcement: dict[str, dict[str, Any]]
    department_enforcement_audit: list[dict[str, Any]]
    models: list[dict[str, Any]]
    token_budget_audit: list[dict[str, Any]]
    token_budgets: dict[tuple[date, str, str], dict[str, Any]]
    usage_records: list[TokenUsageRecord]
    user_model_access_audit: list[dict[str, Any]]
    user_model_policies: dict[str, dict[str, Any]]

    def list_token_budgets(self, period_start: date) -> list[dict[str, Any]]:
        return [
            row
            for (row_period, _, _), row in self.token_budgets.items()
            if row_period == period_start
        ]

    def subscription_attributed_usage(
        self, from_: datetime, to: datetime
    ) -> list[dict[str, Any]]:
        """Mirrors the SQL: usage that declared nothing, attributed to its subscription."""
        applications = {item["id"]: item for item in self.gateway_applications}
        units = {str(row["id"]): row for row in self.org_units()}
        totals: dict[tuple[str, str], int] = {}
        for record in self.usage_records:
            if record.usage_domain != "apim" or not from_ <= record.ts < to:
                continue
            attribution = self.usage_application_attributions.get(record.correlation_id)
            if attribution is None:
                continue
            application = applications.get(attribution.application_id)
            if application is None or application.get("system_managed"):
                continue
            tokens = record.input_tokens + record.cached_tokens + record.output_tokens
            department_id = application.get("department_id")
            owner_id = application.get("owner_id")
            if department_id and record.department_id == "unattributed":
                for scope_type, scope_id in (
                    ("department", department_id),
                    ("organization", (units.get(department_id) or {}).get("parent_id")),
                ):
                    if scope_id:
                        key = (scope_type, str(scope_id))
                        totals[key] = totals.get(key, 0) + tokens
            if owner_id and record.user_id == "unattributed":
                key = ("user", str(owner_id))
                totals[key] = totals.get(key, 0) + tokens
        return [
            {"scope_type": scope_type, "scope_id": scope_id, "used_tokens": used_tokens}
            for (scope_type, scope_id), used_tokens in totals.items()
        ]

    def token_usage_by_budget_scope(self, from_: datetime, to: datetime) -> list[dict[str, Any]]:
        totals: dict[tuple[str, str], int] = {}
        users = {record.user_id for record in self.usage_records if record.usage_domain == "apim"}
        users.update(
            item.scope_id
            for item in self.budget_reservation_finalizations
            if item.scope_type == "person"
        )
        users.update(
            item.scope_id for item in self.billable_requests.values() if item.scope_type == "person"
        )
        for user_id in users:
            for row in self._confirmed_budget_usage("person", user_id):
                if not from_ <= row["ts"] < to:
                    continue
                period = row["ts"].astimezone(UTC).date().replace(day=1)
                person = self.token_budgets.get((period, "user", user_id), {})
                department_id = (
                    row.get("department_id") or person.get("parent_scope_id") or "unattributed"
                )
                department = self.token_budgets.get((period, "department", department_id), {})
                organization_id = (
                    row.get("organization_id")
                    or department.get("parent_scope_id")
                    or "unattributed"
                )
                tokens = row["total_tokens"]
                for scope_type, scope_id in (
                    ("organization", organization_id),
                    ("department", department_id),
                    ("user", user_id),
                ):
                    if scope_id != "unattributed":
                        key = (scope_type, scope_id)
                        totals[key] = totals.get(key, 0) + tokens
        return [
            {"scope_type": scope_type, "scope_id": scope_id, "used_tokens": used_tokens}
            for (scope_type, scope_id), used_tokens in totals.items()
        ]

    def upsert_token_budget(
        self,
        period_start: date,
        scope_type: str,
        scope_id: str,
        parent_scope_id: str | None,
        token_limit: int,
        warning_threshold_percent: int,
        changed_by: str,
    ) -> None:
        key = (period_start, scope_type, scope_id)
        previous = self.token_budgets.get(key)
        if previous is not None and (
            previous["parent_scope_id"] == parent_scope_id
            and previous["token_limit"] == token_limit
            and previous["warning_threshold_percent"] == warning_threshold_percent
        ):
            return
        now = datetime.now(UTC)
        self.token_budgets[key] = {
            "period_start": period_start,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "parent_scope_id": parent_scope_id,
            "token_limit": token_limit,
            "warning_threshold_percent": warning_threshold_percent,
            "updated_at": now,
            "updated_by": changed_by,
        }
        self.token_budget_audit.insert(
            0,
            {
                "id": uuid4(),
                "period_start": period_start,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "action": "assigned" if previous is None else "updated",
                "previous_token_limit": previous["token_limit"] if previous else None,
                "new_token_limit": token_limit,
                "previous_warning_threshold_percent": (
                    previous["warning_threshold_percent"] if previous else None
                ),
                "new_warning_threshold_percent": warning_threshold_percent,
                "changed_at": now,
                "changed_by": changed_by,
            },
        )

    def delete_token_budget(
        self, period_start: date, scope_type: str, scope_id: str, changed_by: str
    ) -> bool:
        previous = self.token_budgets.pop((period_start, scope_type, scope_id), None)
        if previous is None:
            return False
        self.token_budget_audit.insert(
            0,
            {
                "id": uuid4(),
                "period_start": period_start,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "action": "removed",
                "previous_token_limit": previous["token_limit"],
                "new_token_limit": None,
                "previous_warning_threshold_percent": previous["warning_threshold_percent"],
                "new_warning_threshold_percent": None,
                "changed_at": datetime.now(UTC),
                "changed_by": changed_by,
            },
        )
        return True

    def roll_forward_budgets(self, period_start: date, changed_by: str) -> dict[str, Any] | None:
        if period_start in self.budget_roll_forward:
            return None
        occupied = any(period == period_start for period, _, _ in self.token_budgets)
        earlier = [period for period, _, _ in self.token_budgets if period < period_start]
        source_period = None if occupied or not earlier else max(earlier)
        copied = 0
        if source_period is not None:
            for (period, scope_type, scope_id), row in list(self.token_budgets.items()):
                if period != source_period:
                    continue
                self.token_budgets[(period_start, scope_type, scope_id)] = {
                    **row,
                    "period_start": period_start,
                    "updated_at": datetime.now(UTC),
                    "updated_by": changed_by,
                }
                self.token_budget_audit.insert(
                    0,
                    {
                        "id": uuid4(),
                        "period_start": period_start,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "action": "assigned",
                        "previous_token_limit": None,
                        "new_token_limit": row["token_limit"],
                        "previous_warning_threshold_percent": None,
                        "new_warning_threshold_percent": row["warning_threshold_percent"],
                        "changed_at": datetime.now(UTC),
                        "changed_by": changed_by,
                    },
                )
                copied += 1
        # Written even when nothing was copied: the marker records that the period has
        # had its one chance to inherit, which is what stops a deliberate removal from
        # being undone on the next tick.
        self.budget_roll_forward[period_start] = {
            "period_start": period_start,
            "source_period_start": source_period,
            "scope_count": copied,
        }
        return dict(self.budget_roll_forward[period_start])

    def list_token_budget_audit(self, period_start: date, limit: int) -> list[dict[str, Any]]:
        return [row for row in self.token_budget_audit if row["period_start"] == period_start][
            :limit
        ]

    def list_user_model_policies(self, user_ids: Sequence[str]) -> list[dict[str, Any]]:
        return [
            self.user_model_policies[user_id]
            for user_id in user_ids
            if user_id in self.user_model_policies
        ]

    def list_user_model_access_audit(
        self,
        user_ids: Sequence[str],
        from_: datetime,
        to: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        selected = set(user_ids)
        return [
            row
            for row in self.user_model_access_audit
            if row["user_id"] in selected and from_ <= row["changed_at"] < to
        ][:limit]

    def list_department_enforcement(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in sorted(
                self.department_enforcement.values(), key=lambda r: r["department_id"]
            )
        ]

    def set_department_enforcement(self, department_id: str, mode: str, changed_by: str) -> bool:
        current = self.department_enforcement.get(department_id)
        previous = None if current is None else str(current["mode"])
        if previous == mode:
            return False
        now = datetime.now(UTC)
        self.department_enforcement[department_id] = {
            "department_id": department_id,
            "mode": mode,
            "updated_at": now,
            "updated_by": changed_by,
        }
        self.department_enforcement_audit.insert(
            0,
            {
                "id": uuid4(),
                "department_id": department_id,
                "previous_mode": previous,
                "new_mode": mode,
                "changed_at": now,
                "changed_by": changed_by,
            },
        )
        return True

    def list_department_enforcement_audit(
        self, from_: datetime, to: datetime, limit: int
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.department_enforcement_audit
            if from_ <= row["changed_at"] < to
        ][:limit]

    def budget_ledger_people(self, period_start: date) -> list[dict[str, Any]]:
        people: list[dict[str, Any]] = []
        active_user_ids: set[str] = set()
        for (period, scope_type, scope_id), budget in sorted(self.token_budgets.items()):
            if period != period_start or scope_type != "user":
                continue
            active_user_ids.add(scope_id)
            department_id = str(budget.get("parent_scope_id") or "")
            enforcement = self.department_enforcement.get(department_id)
            people.append(
                {
                    "user_id": scope_id,
                    "department_id": department_id,
                    "token_limit": int(budget["token_limit"]),
                    "mode": str(enforcement["mode"]) if enforcement else "audit",
                }
            )
        retired_user_ids = {
            str(event["scope_id"])
            for event in self.token_budget_audit
            if event["period_start"] == period_start
            and event["scope_type"] == "user"
            and event["action"] == "removed"
            and str(event["scope_id"]) not in active_user_ids
        }
        people.extend(
            {
                "user_id": user_id,
                "department_id": "",
                "token_limit": 0,
                "mode": "audit",
            }
            for user_id in sorted(retired_user_ids)
        )
        return people

    def budget_ledger_snapshot(self, period_start: date, period_end: date) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for person in self.budget_ledger_people(period_start):
            scope_id = str(person["user_id"])
            snapshot.append(
                {
                    "user_id": scope_id,
                    "department_id": person["department_id"],
                    "token_limit": int(person["token_limit"]),
                    "confirmed_tokens": self.budget_scope_confirmed_tokens(
                        "person", scope_id, period_start, period_end
                    ),
                    "mode": person["mode"],
                }
            )
        return snapshot

    def settled_reservation_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: LedgerScopeType | None = None,
        scope_id: str | None = None,
    ) -> set[str]:
        if (scope_type is None) != (scope_id is None):
            raise ValueError("ledger scope type and ID must be supplied together")
        wanted = set(correlation_ids)
        settled = {
            record.correlation_id
            for record in self.usage_records
            if record.correlation_id in wanted
            and self._ledger_scope_matches(record, scope_type, scope_id)
            and (
                canonical_budget_tokens(record, reconciled=record.id in self.reconciled_usage_ids)
                is not None
                if scope_type is not None
                and scope_id is not None
                and self._strict_budget_record(record, scope_type, scope_id)
                else self._ledger_usage_final(record)
            )
        }
        if scope_type is not None and scope_id is not None:
            settled.update(
                correlation
                for correlation, kind in self.reservation_finalization_kinds(
                    scope_type, scope_id, correlation_ids
                ).items()
                if kind != "unverified_upper_bound"
            )
            settled.update(wanted.intersection(self._strict_budget_choices(scope_type, scope_id)))
            settled.update(
                str(attempt.id)
                for attempt in self.billable_requests.values()
                if str(attempt.id) in wanted
                and attempt.scope_type == scope_type
                and attempt.scope_id == scope_id
                and self._billable_effective_tokens(attempt) is not None
            )
        return settled

    @staticmethod
    def _ledger_usage_final(record: TokenUsageRecord) -> bool:
        return (
            not record.estimated
            or record.status_code >= 400
            or record.ingest_error == "stream_cache_usage_unavailable"
        )

    def _ledger_scope_matches(
        self,
        record: TokenUsageRecord,
        scope_type: LedgerScopeType | None,
        scope_id: str | None,
    ) -> bool:
        if record.usage_domain != "apim":
            return False
        if scope_type is None:
            return True
        if scope_type == "person":
            return record.user_id == scope_id
        attribution = self.usage_application_attributions.get(record.id)
        return attribution is not None and str(attribution.application_id) == scope_id

    def existing_usage_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: LedgerScopeType,
        scope_id: str,
    ) -> set[str]:
        return {
            record.correlation_id
            for record in self.usage_records
            if record.correlation_id in correlation_ids
            and self._ledger_scope_matches(record, scope_type, scope_id)
        }

    def save_budget_reservation_finalizations(
        self,
        items: Sequence[BudgetReservationFinalization],
    ) -> int:
        written = 0
        for item in items:
            identity = (
                item.scope_type,
                item.scope_id,
                item.correlation_id,
                item.finalization_kind,
                item.source,
                item.evidence_at,
            )
            if any(
                (
                    row.scope_type,
                    row.scope_id,
                    row.correlation_id,
                    row.finalization_kind,
                    row.source,
                    row.evidence_at,
                )
                == identity
                for row in self.budget_reservation_finalizations
            ):
                continue
            self.budget_reservation_finalizations.append(item.model_copy(deep=True))
            self._finalization_evidence_key(item)
            written += 1
        return written

    def _effective_finalizations(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
    ) -> dict[str, BudgetReservationFinalization]:
        rank = {"exact_usage": 3, "terminal_zero": 2, "unverified_upper_bound": 1}
        effective: dict[str, BudgetReservationFinalization] = {}
        for item in self.budget_reservation_finalizations:
            if item.scope_type != scope_type or item.scope_id != scope_id:
                continue
            previous = effective.get(item.correlation_id)
            if previous is None or (rank[item.finalization_kind], item.evidence_at) >= (
                rank[previous.finalization_kind],
                previous.evidence_at,
            ):
                effective[item.correlation_id] = item
        return effective

    def reservation_finalization_kinds(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
        correlation_ids: Sequence[str],
    ) -> dict[str, str]:
        return {
            correlation: item.finalization_kind
            for correlation, item in self._effective_finalizations(scope_type, scope_id).items()
            if correlation in correlation_ids
        }

    def _budget_scope_usage(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
    ) -> list[dict[str, Any]]:
        records = [
            record
            for record in self.usage_records
            if self._ledger_scope_matches(record, scope_type, scope_id)
        ]
        final = {record.correlation_id for record in records if self._ledger_usage_final(record)}
        recoveries = {
            correlation: item
            for correlation, item in self._effective_finalizations(scope_type, scope_id).items()
            if correlation not in final and item.finalization_kind != "unverified_upper_bound"
        }
        rows = [
            record.model_dump() for record in records if record.correlation_id not in recoveries
        ]
        rows.extend(
            {
                "correlation_id": item.correlation_id,
                "ts": item.reservation_created_at,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "estimated_cost": 0.0,
                "status_code": item.status_code or 0,
                "et": 0.0,
                "latency_ms": None,
            }
            for item in recoveries.values()
        )
        return rows

    def budget_scope_confirmed_tokens(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
        period_start: date,
        period_end: date,
    ) -> int:
        return sum(
            row["total_tokens"]
            for row in self._confirmed_budget_usage(scope_type, scope_id)
            if period_start <= row["ts"].astimezone(UTC).date() < period_end
        )

    def _confirmed_budget_usage(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        records = {
            record.correlation_id: record
            for record in self.usage_records
            if self._ledger_scope_matches(record, scope_type, scope_id)
        }
        for row in self._budget_scope_usage(scope_type, scope_id):
            record = records.get(row["correlation_id"])
            admitted = self._budget_admitted_at(
                scope_type, scope_id, row["correlation_id"], row["ts"]
            )
            if strict_budget_evidence(admitted, self.evidence_policy_effective_at):
                continue
            if (
                record is not None
                and self._record_billable_attempt(record, scope_type, scope_id) is not None
            ):
                continue
            if self._budget_bound_attempt(scope_type, scope_id, row["correlation_id"]) is not None:
                continue
            rows.append(
                {
                    **row,
                    "total_tokens": (
                        row["input_tokens"] + row["cached_tokens"] + row["output_tokens"]
                    ),
                }
            )
        for attempt in self.billable_requests.values():
            if (
                attempt.scope_type == scope_type
                and attempt.scope_id == scope_id
                and not strict_budget_evidence(
                    attempt.created_at, self.evidence_policy_effective_at
                )
            ):
                tokens = self._billable_effective_tokens(attempt)
                if tokens is not None:
                    rows.append(
                        {
                            "ts": datetime.combine(attempt.period_start, datetime.min.time(), UTC),
                            "total_tokens": tokens,
                            "organization_id": "unattributed",
                            "department_id": "unattributed",
                        }
                    )
        for identity, (admitted, tokens, rank) in self._strict_budget_choices(
            scope_type, scope_id
        ).items():
            canonical = next(
                (
                    record
                    for record in records.values()
                    if rank == 3
                    and self._budget_record_identity(record, scope_type, scope_id)[0] == identity
                ),
                None,
            )
            rows.append(
                {
                    "ts": admitted,
                    "total_tokens": tokens,
                    "organization_id": canonical.organization_id if canonical else "unattributed",
                    "department_id": canonical.department_id if canonical else "unattributed",
                }
            )
        return rows

    def model_access_ledger_snapshot(
        self, user_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        wanted = None if user_ids is None else set(user_ids)
        keys = {str(model["id"]): str(model["model_key"]) for model in self.models}
        snapshot: list[dict[str, Any]] = []
        for user_id, policy in sorted(self.user_model_policies.items()):
            if wanted is not None and user_id not in wanted:
                continue
            model_ids = [str(value) for value in policy.get("model_ids", [])]
            snapshot.append(
                {
                    "user_id": user_id,
                    "model_uuids": model_ids,
                    "model_keys": [keys[model_id] for model_id in model_ids if model_id in keys],
                }
            )
        return snapshot

    def person_budget_state(
        self, user_id: str, period_start: date, period_end: date
    ) -> dict[str, Any] | None:
        budget = self.token_budgets.get((period_start, "user", user_id))
        if budget is None:
            return None
        department_id = str(budget.get("parent_scope_id") or "")
        enforcement = self.department_enforcement.get(department_id)
        used_tokens = self.budget_scope_confirmed_tokens(
            "person", user_id, period_start, period_end
        )
        return {
            "token_limit": int(budget["token_limit"]),
            "department_id": department_id,
            "mode": str(enforcement["mode"]) if enforcement else "audit",
            "used_tokens": used_tokens,
        }

    def bulk_upsert_user_budgets(
        self,
        period_start: date,
        department_id: str,
        entries: Sequence[tuple[str, int]],
        warning_threshold_percent: int,
        changed_by: str,
        *,
        selected_user_ids: Sequence[str] | None = None,
        model_ids: Sequence[UUID] | None = None,
    ) -> None:
        user_ids = list(selected_user_ids or [user_id for user_id, _ in entries])
        normalized_model_ids = list(dict.fromkeys(model_ids)) if model_ids is not None else None
        if entries:
            parent = self.token_budgets.get((period_start, "department", department_id))
            if parent is None:
                raise ValueError("Assign the department budget before allocating user budgets")
            selected_ids = {user_id for user_id, _ in entries}
            unselected_total = sum(
                int(row["token_limit"])
                for (row_period, scope_type, scope_id), row in self.token_budgets.items()
                if row_period == period_start
                and scope_type == "user"
                and row["parent_scope_id"] == department_id
                and scope_id not in selected_ids
            )
            if unselected_total + sum(limit for _, limit in entries) > int(parent["token_limit"]):
                raise BudgetConstraintViolation(
                    "User allocations would exceed the department budget of "
                    f"{int(parent['token_limit'])} tokens"
                )
            for user_id, token_limit in entries:
                self.upsert_token_budget(
                    period_start,
                    "user",
                    user_id,
                    department_id,
                    token_limit,
                    warning_threshold_percent,
                    changed_by,
                )

        if normalized_model_ids is not None:
            requested_models = set(normalized_model_ids)
            enabled_models = {model["id"] for model in self.models if model["enabled"]}
            if not requested_models.issubset(enabled_models):
                raise BudgetConstraintViolation("One or more selected models are unavailable")
            now = datetime.now(UTC)
            for user_id in user_ids:
                previous = self.user_model_policies.get(user_id)
                if previous is not None and set(previous["model_ids"]) == set(normalized_model_ids):
                    continue
                self.user_model_policies[user_id] = {
                    "user_id": user_id,
                    "model_ids": normalized_model_ids,
                    "updated_at": now,
                    "updated_by": changed_by,
                }
                self.user_model_access_audit.insert(
                    0,
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "previous_model_ids": previous["model_ids"] if previous else [],
                        "new_model_ids": normalized_model_ids,
                        "changed_at": now,
                        "changed_by": changed_by,
                    },
                )
