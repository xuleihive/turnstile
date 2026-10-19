from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from ..domain.application_access import (
    UsageApplicationAttribution,
)
from ..domain.ledger import (
    BudgetReservationFinalization,
    canonical_budget_tokens,
    strict_budget_evidence,
)
from ..domain.models import TokenUsageRecord
from .in_memory_budget_evidence import InMemoryBudgetEvidenceRepositoryMixin
from .repository_support import BudgetConstraintViolation, proportional_budget_tree


class InMemoryBudgetRepositoryMixin(InMemoryBudgetEvidenceRepositoryMixin):
    budget_roll_forward: dict[date, dict[str, Any]]
    department_enforcement: dict[str, dict[str, Any]]
    department_enforcement_audit: list[dict[str, Any]]
    models: list[dict[str, Any]]
    token_budget_audit: list[dict[str, Any]]
    token_budgets: dict[tuple[date, str, str], dict[str, Any]]
    usage_records: list[TokenUsageRecord]
    user_model_access_audit: list[dict[str, Any]]
    user_model_policies: dict[str, dict[str, Any]]
    budget_reservation_finalizations: dict[
        tuple[str, str, str, str, str, datetime], BudgetReservationFinalization
    ]
    usage_application_attributions: dict[str, UsageApplicationAttribution]

    def list_token_budgets(
        self, period_start: date, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            row
            for (row_period, scope_type, scope_id), row in self.token_budgets.items()
            if row_period == period_start and (
                user_id is None or (scope_type == "user" and scope_id == user_id)
            )
        ]

    def token_usage_by_budget_scope(
        self, from_: datetime, to: datetime, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        totals: dict[tuple[str, str], int] = {}
        usd_totals: dict[tuple[str, str], Decimal] = {}
        unpriced_usd: dict[tuple[str, str], int] = {}
        for record in self.usage_records:
            if user_id is not None and record.user_id != user_id:
                continue
            if not from_ <= record.ts < to or record.usage_domain != "apim":
                continue
            strict_record = self._strict_budget_record(record, "person", record.user_id)
            tokens = record.input_tokens + record.cached_tokens + record.output_tokens
            for scope_type, scope_id in (
                ("organization", record.organization_id),
                ("department", record.department_id),
                ("user", record.user_id),
            ):
                if scope_id == "unattributed":
                    continue
                key = (scope_type, scope_id)
                if not strict_record:
                    totals[key] = totals.get(key, 0) + tokens
                if record.usd_cost is None:
                    unpriced_usd[key] = unpriced_usd.get(key, 0) + 1
                else:
                    usd_totals[key] = usd_totals.get(key, Decimal(0)) + record.usd_cost
        canonical = {
            (record.user_id, record.correlation_id)
            for record in self.usage_records
            if record.usage_domain == "apim"
            and (not record.estimated or record.ingest_error == "stream_cache_usage_unavailable")
        }
        for item in self._effective_person_finalizations():
            if user_id is not None and item.scope_id != user_id:
                continue
            if (
                from_ <= item.reservation_created_at < to
                and item.finalization_kind != "unverified_upper_bound"
                and (item.scope_id, item.correlation_id) not in canonical
                and not strict_budget_evidence(
                    item.reservation_created_at, self.evidence_policy_effective_at,
                )
            ):
                person_budget = self.token_budgets.get(
                    (item.period_start, "user", item.scope_id), {}
                )
                department_id = person_budget.get("parent_scope_id") or "unattributed"
                department_budget = self.token_budgets.get(
                    (item.period_start, "department", department_id), {}
                )
                organization_id = department_budget.get("parent_scope_id") or "unattributed"
                for scope, scope_id in (
                    ("organization", organization_id),
                    ("department", department_id),
                    ("user", item.scope_id),
                ):
                    if scope_id == "unattributed":
                        continue
                    key = (scope, scope_id)
                    totals[key] = totals.get(key, 0) + item.total_tokens
                    unpriced_usd[key] = unpriced_usd.get(key, 0) + 1
        people = {record.user_id for record in self.usage_records}
        people.update(item.scope_id for item in self.budget_reservation_finalizations.values()
                      if item.scope_type == "person")
        people.update(item.scope_id for item in self.billable_requests.values()
                      if item.scope_type == "person")
        for person in people:
            if user_id is not None and person != user_id:
                continue
            choices = self._strict_budget_choices("person", person)
            for identity, (admitted, tokens, rank) in choices.items():
                if not from_ <= admitted < to:
                    continue
                key = ("user", person)
                totals[key] = totals.get(key, 0) + tokens
                if rank == 3:
                    canonical_record = next((
                        record for record in self.usage_records
                        if record.user_id == person and record.usage_domain == "apim"
                        and self._budget_record_identity(record, "person", person)[0] == identity
                    ), None)
                    if canonical_record is not None:
                        for scope, scope_id in (("organization", canonical_record.organization_id),
                                                ("department", canonical_record.department_id)):
                            if scope_id != "unattributed":
                                key = (scope, scope_id)
                                totals[key] = totals.get(key, 0) + tokens
        keys = totals.keys() | usd_totals.keys() | unpriced_usd.keys()
        return [
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "used_tokens": totals.get((scope_type, scope_id), 0),
                "used_usd": usd_totals.get((scope_type, scope_id), Decimal(0)),
                "unpriced_usage_count": unpriced_usd.get((scope_type, scope_id), 0),
            }
            for scope_type, scope_id in keys
            if user_id is None or (scope_type == "user" and scope_id == user_id)
        ]

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
    ) -> None:
        key = (period_start, scope_type, scope_id)
        previous = self.token_budgets.get(key)
        amount = usd_limit if budget_mode == "usd" else token_limit
        if budget_mode not in {"tokens", "usd"} or amount is None:
            raise BudgetConstraintViolation("Budget limit does not match its mode")
        parent_type = {"department": "organization", "user": "department"}.get(scope_type)
        child_type = {"organization": "department", "department": "user"}.get(scope_type)
        if parent_type is not None and parent_scope_id is not None:
            parent = self.token_budgets.get((period_start, parent_type, parent_scope_id))
            if parent is not None and parent.get("budget_mode", "tokens") != budget_mode:
                raise BudgetConstraintViolation("Parent and child budgets must use the same mode")
            siblings = [row for row in self.token_budgets.values() if (
                row["period_start"] == period_start and row["scope_type"] == scope_type
                and row["parent_scope_id"] == parent_scope_id and row["scope_id"] != scope_id
            )]
            if any(row.get("budget_mode", "tokens") != budget_mode for row in siblings):
                raise BudgetConstraintViolation("Sibling budgets must use the same mode")
            sibling_limits = [
                row.get("usd_limit") if budget_mode == "usd" else row.get("token_limit")
                for row in siblings
            ]
            if any(value is None for value in sibling_limits):
                raise BudgetConstraintViolation("Budget hierarchy has an incomplete limit")
            parent_limit = (
                parent.get("usd_limit") if budget_mode == "usd" else parent.get("token_limit")
            ) if parent is not None else None
            if (parent_limit is not None
                    and sum(value for value in sibling_limits if value is not None) + amount
                    > parent_limit):
                raise BudgetConstraintViolation("Child budgets exceed parent budget")
        if child_type is not None:
            children = [row for row in self.token_budgets.values() if (
                row["period_start"] == period_start and row["scope_type"] == child_type
                and row["parent_scope_id"] == scope_id
            )]
            if any(row.get("budget_mode", "tokens") != budget_mode for row in children):
                raise BudgetConstraintViolation("Parent and child budgets must use the same mode")
            child_limits = [
                row.get("usd_limit") if budget_mode == "usd" else row.get("token_limit")
                for row in children
            ]
            if any(value is None for value in child_limits):
                raise BudgetConstraintViolation("Budget hierarchy has an incomplete limit")
            child_total = sum(value for value in child_limits if value is not None)
            if child_total > amount:
                raise BudgetConstraintViolation("Parent budget is below allocated child budgets")
        if previous is not None and (
            previous["parent_scope_id"] == parent_scope_id
            and previous["token_limit"] == token_limit
            and previous.get("budget_mode", "tokens") == budget_mode
            and previous.get("usd_limit") == usd_limit
            and previous["warning_threshold_percent"] == warning_threshold_percent
        ):
            return
        now = datetime.now(UTC)
        self.token_budgets[key] = {
            "period_start": period_start,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "parent_scope_id": parent_scope_id,
            "budget_mode": budget_mode,
            "token_limit": token_limit,
            "usd_limit": usd_limit,
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
                "previous_budget_mode": previous.get("budget_mode", "tokens") if previous else None,
                "new_budget_mode": budget_mode,
                "previous_token_limit": previous["token_limit"] if previous else None,
                "new_token_limit": token_limit,
                "previous_usd_limit": previous.get("usd_limit") if previous else None,
                "new_usd_limit": usd_limit,
                "previous_warning_threshold_percent": (
                    previous["warning_threshold_percent"] if previous else None
                ),
                "new_warning_threshold_percent": warning_threshold_percent,
                "changed_at": now,
                "changed_by": changed_by,
            },
        )

    def switch_organization_budget(
        self, period_start: date, organization_id: str, budget_mode: str,
        amount: int | Decimal, warning_threshold_percent: int, changed_by: str,
    ) -> None:
        updated = proportional_budget_tree(
            self.list_token_budgets(period_start), organization_id, budget_mode,
            amount, warning_threshold_percent,
        )
        moment = datetime.now(UTC)
        for row in updated:
            key = (period_start, row["scope_type"], row["scope_id"])
            previous = self.token_budgets[key]
            self.token_budget_audit.insert(0, {
                "id": uuid4(), "period_start": period_start,
                "scope_type": row["scope_type"], "scope_id": row["scope_id"],
                "action": "updated", "previous_budget_mode": previous.get("budget_mode", "tokens"),
                "new_budget_mode": budget_mode,
                "previous_token_limit": previous["token_limit"],
                "new_token_limit": row["token_limit"],
                "previous_usd_limit": previous.get("usd_limit"), "new_usd_limit": row["usd_limit"],
                "previous_warning_threshold_percent": previous["warning_threshold_percent"],
                "new_warning_threshold_percent": row["warning_threshold_percent"],
                "changed_at": moment, "changed_by": changed_by,
            })
            self.token_budgets[key] = {**row, "updated_at": moment, "updated_by": changed_by}

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
                "previous_budget_mode": previous.get("budget_mode", "tokens"),
                "new_budget_mode": None,
                "previous_token_limit": previous["token_limit"],
                "new_token_limit": None,
                "previous_usd_limit": previous.get("usd_limit"),
                "new_usd_limit": None,
                "previous_warning_threshold_percent": previous[
                    "warning_threshold_percent"
                ],
                "new_warning_threshold_percent": None,
                "changed_at": datetime.now(UTC),
                "changed_by": changed_by,
            },
        )
        return True

    def roll_forward_budgets(
        self, period_start: date, changed_by: str
    ) -> dict[str, Any] | None:
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
                        "previous_budget_mode": None,
                        "new_budget_mode": row.get("budget_mode", "tokens"),
                        "previous_token_limit": None,
                        "new_token_limit": row["token_limit"],
                        "previous_usd_limit": None,
                        "new_usd_limit": row.get("usd_limit"),
                        "previous_warning_threshold_percent": None,
                        "new_warning_threshold_percent": row[
                            "warning_threshold_percent"
                        ],
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

    def list_token_budget_audit(
        self, period_start: date, limit: int
    ) -> list[dict[str, Any]]:
        return [
            row for row in self.token_budget_audit if row["period_start"] == period_start
        ][:limit]

    def list_user_model_policies(
        self, user_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
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

    def set_department_enforcement(
        self, department_id: str, mode: str, changed_by: str
    ) -> bool:
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
            if (
                period != period_start
                or scope_type != "user"
                or budget.get("budget_mode", "tokens") != "tokens"
            ):
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

    def budget_ledger_snapshot(
        self, period_start: date, period_end: date
    ) -> list[dict[str, Any]]:
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
        scope_type: str,
        scope_id: str | None = None,
    ) -> set[str]:
        wanted = set(correlation_ids)
        canonical = {
            record.correlation_id
            for record in self.usage_records
            if record.correlation_id in wanted
            and record.usage_domain == "apim"
            and (
                scope_id is None
                or (
                    scope_type == "person"
                    and record.user_id == scope_id
                )
                or (
                    scope_type == "application"
                    and (
                        attribution := self.usage_application_attributions.get(record.id)
                    ) is not None
                    and str(attribution.application_id) == scope_id
                )
            )
            and (
                canonical_budget_tokens(
                    record, reconciled=record.id in self.reconciled_usage_ids,
                ) is not None
                if scope_id is not None and self._strict_budget_record(record, scope_type, scope_id)
                else record.status_code >= 400 or not record.estimated
                or record.ingest_error == "stream_cache_usage_unavailable"
            )
        }
        return canonical | {
            correlation_id
            for (item_scope, item_id, correlation_id, _, _, _), _item in (
                self.budget_reservation_finalizations.items()
            )
            if item_scope == scope_type
            and item_id == scope_id
            and _item.finalization_kind != "unverified_upper_bound"
            and correlation_id in wanted
        }

    def existing_usage_correlations(
        self,
        correlation_ids: Sequence[str],
        *,
        scope_type: str,
        scope_id: str | None = None,
    ) -> set[str]:
        wanted = set(correlation_ids)
        return {
            record.correlation_id
            for record in self.usage_records
            if record.correlation_id in wanted
            and record.usage_domain == "apim"
            and (
                scope_id is None
                or (scope_type == "person" and record.user_id == scope_id)
                or (
                    scope_type == "application"
                    and (
                        attribution := self.usage_application_attributions.get(record.id)
                    ) is not None
                    and str(attribution.application_id) == scope_id
                )
            )
        }

    def save_budget_reservation_finalizations(
        self, items: Sequence[BudgetReservationFinalization]
    ) -> int:
        written = 0
        for item in items:
            key = (
                item.scope_type,
                item.scope_id,
                item.correlation_id,
                item.finalization_kind,
                item.source,
                item.evidence_at,
            )
            existing = self.budget_reservation_finalizations.get(key)
            if existing is not None:
                if existing != item:
                    raise ValueError("Budget reservation finalization is immutable")
                continue
            self.budget_reservation_finalizations[key] = item
            self._finalization_evidence_key(item)
            written += 1
        return written

    def reservation_finalization_kind(
        self, scope_type: str, scope_id: str, correlation_id: str
    ) -> str | None:
        strength = {
            "unverified_upper_bound": 1,
            "terminal_zero": 2,
            "exact_usage": 3,
        }
        candidates = [
            item.finalization_kind
            for (item_scope, item_id, item_correlation, _, _, _), item in (
                self.budget_reservation_finalizations.items()
            )
            if item_scope == scope_type
            and item_id == scope_id
            and item_correlation == correlation_id
        ]
        return max(candidates, key=strength.__getitem__) if candidates else None

    def reservation_finalization_kinds(
        self, scope_type: str, scope_id: str, correlation_ids: Sequence[str]
    ) -> dict[str, str]:
        return {
            correlation_id: kind
            for correlation_id in correlation_ids
            if (
                kind := self.reservation_finalization_kind(
                    scope_type, scope_id, correlation_id
                )
            )
            is not None
        }

    def _effective_person_finalizations(
        self,
    ) -> list[BudgetReservationFinalization]:
        strength = {
            "unverified_upper_bound": 1,
            "terminal_zero": 2,
            "exact_usage": 3,
        }
        strongest: dict[tuple[str, str], BudgetReservationFinalization] = {}
        for item in self.budget_reservation_finalizations.values():
            if item.scope_type != "person":
                continue
            key = (item.scope_id, item.correlation_id)
            existing = strongest.get(key)
            if existing is None or strength[item.finalization_kind] > strength[
                existing.finalization_kind
            ]:
                strongest[key] = item
        return list(strongest.values())

    def budget_scope_confirmed_tokens(
        self,
        scope_type: str,
        scope_id: str,
        period_start: date,
        period_end: date,
    ) -> int:
        canonical: dict[str, int] = {}
        for record in self.usage_records:
            if (
                record.usage_domain != "apim"
                or not period_start <= record.ts.date() < period_end
            ):
                continue
            if scope_type == "person":
                matches = record.user_id == scope_id
            else:
                attribution = self.usage_application_attributions.get(record.id)
                matches = (
                    attribution is not None
                    and str(attribution.application_id) == scope_id
                )
            if (
                matches
                and (
                    not record.estimated
                    or record.ingest_error == "stream_cache_usage_unavailable"
                )
                and not self._strict_budget_record(record, scope_type, scope_id)
            ):
                canonical[record.correlation_id] = (
                    record.input_tokens + record.cached_tokens + record.output_tokens
                )
        strength = {
            "unverified_upper_bound": 1,
            "terminal_zero": 2,
            "exact_usage": 3,
        }
        strongest: dict[str, BudgetReservationFinalization] = {}
        for (item_scope, item_id, correlation_id, _, _, _), item in (
            self.budget_reservation_finalizations.items()
        ):
            if (
                item_scope != scope_type
                or item_id != scope_id
                or item.period_start != period_start
                or correlation_id in canonical
                or item.finalization_kind == "unverified_upper_bound"
                or strict_budget_evidence(
                    self._budget_admitted_at(
                        scope_type, scope_id, correlation_id, item.reservation_created_at,
                    ),
                    self.evidence_policy_effective_at,
                )
            ):
                continue
            existing = strongest.get(correlation_id)
            if existing is None or strength[item.finalization_kind] > strength[
                existing.finalization_kind
            ]:
                strongest[correlation_id] = item
        return sum(canonical.values()) + sum(
            item.total_tokens for item in strongest.values()
        ) + sum(
            tokens for admitted, tokens, _rank in self._strict_budget_choices(
                scope_type, scope_id,
            ).values()
            if period_start <= admitted.astimezone(UTC).date() < period_end
        )

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
                    "model_keys": [
                        keys[model_id] for model_id in model_ids if model_id in keys
                    ],
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
            "token_limit": (
                int(budget["token_limit"]) if budget["token_limit"] is not None else None
            ),
            "budget_mode": budget.get("budget_mode", "tokens"),
            "department_id": department_id,
            "mode": str(enforcement["mode"]) if enforcement else "audit",
            "used_tokens": used_tokens,
        }

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
    ) -> None:
        if budget_mode not in {"tokens", "usd"}:
            raise BudgetConstraintViolation("Unsupported budget mode")
        user_ids = list(selected_user_ids or [user_id for user_id, _ in entries])
        normalized_model_ids = (
            list(dict.fromkeys(model_ids)) if model_ids is not None else None
        )
        if entries:
            parent = self.token_budgets.get((period_start, "department", department_id))
            if parent is None:
                raise ValueError(
                    "Assign the department budget before allocating user budgets"
                )
            if parent.get("budget_mode", "tokens") != budget_mode:
                raise BudgetConstraintViolation(
                    "Parent and child budgets must use the same mode"
                )
            limit_field = "usd_limit" if budget_mode == "usd" else "token_limit"
            selected_ids = {user_id for user_id, _ in entries}
            unselected_rows = [
                row for (row_period, scope_type, scope_id), row in self.token_budgets.items()
                if row_period == period_start
                and scope_type == "user"
                and row["parent_scope_id"] == department_id
                and scope_id not in selected_ids
            ]
            if any(row.get("budget_mode", "tokens") != budget_mode for row in unselected_rows):
                raise BudgetConstraintViolation(
                    "Parent and child budgets must use the same mode"
                )
            unselected_total = sum(row[limit_field] for row in unselected_rows)
            if unselected_total + sum(limit for _, limit in entries) > parent[limit_field]:
                raise BudgetConstraintViolation(
                    "User allocations would exceed the department budget"
                )
            for user_id, token_limit in entries:
                self.upsert_token_budget(
                    period_start,
                    "user",
                    user_id,
                    department_id,
                    token_limit if budget_mode == "tokens" else None,
                    warning_threshold_percent,
                    changed_by,
                    budget_mode=budget_mode,
                    usd_limit=token_limit if budget_mode == "usd" else None,
                )

        if normalized_model_ids is not None:
            requested_models = set(normalized_model_ids)
            enabled_models = {
                model["id"] for model in self.models if model["enabled"]
            }
            if not requested_models.issubset(enabled_models):
                raise BudgetConstraintViolation(
                    "One or more selected models are unavailable"
                )
            now = datetime.now(UTC)
            for user_id in user_ids:
                previous = self.user_model_policies.get(user_id)
                if previous is not None and set(previous["model_ids"]) == set(
                    normalized_model_ids
                ):
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
