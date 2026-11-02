from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, cast

from turnstile_core.domain.enterprise import (
    enterprise_catalog,
    merge_application_owners,
    merge_observed_users,
)
from turnstile_core.domain.ledger import LedgerScope
from turnstile_core.domain.models import (
    BudgetScopeType,
    DepartmentEnforcementWrite,
    EnterpriseEntity,
    PeopleBudgetFilter,
    TokenBudgetBulkResult,
    TokenBudgetBulkWrite,
    TokenBudgetItem,
    TokenBudgetPeopleResponse,
    TokenBudgetResponse,
    TokenBudgetWrite,
)
from turnstile_core.persistence.repository import BudgetConstraintViolation, QueryRepository
from turnstile_core.services.budget_settlement import (
    BudgetResolutionRepository,
    BudgetStatusRepository,
)

logger = logging.getLogger(__name__)

# Publishes the given people's model policy into the gateway ledger. Injected rather than
# imported so the service keeps working without Table Storage configured, and so tests do
# not need a fake storage account.
ModelAccessProjector = Callable[[Sequence[str]], None]


class BudgetConflictError(ValueError):
    pass


class BudgetNotFoundError(ValueError):
    pass


def period_bounds(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split("-", maxsplit=1))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


class TokenBudgetService:
    _child_scope: dict[BudgetScopeType, BudgetScopeType] = {
        "organization": "department",
        "department": "user",
    }
    _parent_scope: dict[BudgetScopeType, BudgetScopeType] = {
        "department": "organization",
        "user": "department",
    }

    def __init__(
        self,
        repository: QueryRepository,
        project_model_access: ModelAccessProjector | None = None,
        *, require_atomic_budget: Callable[[], None] | None = None,
    ) -> None:
        self._repository = repository
        self._project_model_access = project_model_access
        self._require_atomic_budget = require_atomic_budget

    def _publish_model_access(self, user_ids: Sequence[str]) -> None:
        """Push a just-saved policy to the ledger the gateway reads.

        Deliberately after the commit and deliberately non-fatal: PostgreSQL is the source
        of truth and the ledger timer re-projects every policy, so a failure here costs
        freshness until the next tick rather than the administrator's change. Raising would
        report a save that actually succeeded as failed.
        """
        if self._project_model_access is None or not user_ids:
            return
        try:
            self._project_model_access(list(user_ids))
        except Exception:
            logger.warning(
                "Model access saved but not projected to the ledger for %s people; "
                "the next ledger sync will repair it",
                len(user_ids),
                exc_info=True,
            )

    def _entities(self) -> dict[BudgetScopeType, list[EnterpriseEntity]]:
        # Merged, not seeded: a person who has actually used the gateway must be
        # allocatable, otherwise governance only covers identities with no traffic.
        catalog = merge_application_owners(
            merge_observed_users(enterprise_catalog(), self._repository.observed_users()),
            self._repository.application_owners(),
        )
        return {
            "organization": catalog.organizations,
            "department": catalog.departments,
            "user": catalog.users,
        }

    def _entity(self, scope_type: BudgetScopeType, scope_id: str) -> EnterpriseEntity:
        entity = next(
            (item for item in self._entities()[scope_type] if item.id == scope_id), None
        )
        if entity is None:
            raise BudgetNotFoundError(f"Unknown {scope_type} budget scope: {scope_id}")
        return entity

    def _scope_needs_unit_switch(
        self, user_id: str, period_start: date, target_mode: str
    ) -> bool:
        scope = LedgerScope(
            scope_type="person", scope_id=user_id, period_start=period_start
        )
        if isinstance(self._repository, BudgetStatusRepository):
            status = self._repository.budget_execution_status(scope)
            if status is not None:
                scope_mode = status.get("budget_mode")
                applied = int(status.get("policy_applied_revision") or 0)
                revision = int(status.get("policy_revision") or 0)
                if scope_mode in {"tokens", "usd"}:
                    if applied < revision and scope_mode != target_mode:
                        return False
                    if applied >= revision and scope_mode == target_mode:
                        return False
        return True

    def _require_unit_switch_ready(
        self,
        period_start: date,
        organization_id: str,
        target_mode: str,
        budgets: Mapping[tuple[str, str], Mapping[str, Any]],
        entities: Mapping[BudgetScopeType, Sequence[EnterpriseEntity]],
    ) -> None:
        _, period_end = period_bounds(f"{period_start:%Y-%m}")
        department_ids = {
            entity.id
            for entity in entities["department"]
            if entity.parent_id == organization_id
        } | {
            scope_id
            for (scope_type, scope_id), row in budgets.items()
            if scope_type == "department" and row.get("parent_scope_id") == organization_id
        }
        user_ids = {
            entity.id
            for entity in entities["user"]
            if entity.parent_id in department_ids
        } | {
            scope_id
            for (scope_type, scope_id), row in budgets.items()
            if scope_type == "user" and row.get("parent_scope_id") in department_ids
        }
        allocated_users = sorted(
            user_id for user_id in user_ids if ("user", user_id) in budgets
        )
        switching_users = [
            user_id
            for user_id in allocated_users
            if self._scope_needs_unit_switch(user_id, period_start, target_mode)
        ]
        if target_mode == "usd" and (switching_users or not allocated_users):
            usage_rows = self._repository.token_usage_by_budget_scope(
                datetime.combine(period_start, time.min, tzinfo=UTC),
                datetime.combine(period_end, time.min, tzinfo=UTC),
            )
            for row in usage_rows:
                row_scope = (row["scope_type"], row["scope_id"])
                in_tree = (
                    row_scope == ("organization", organization_id)
                    or (row["scope_type"] == "department" and row["scope_id"] in department_ids)
                    or (
                        row["scope_type"] == "user"
                        and row["scope_id"] in (set(switching_users) or user_ids)
                    )
                )
                if in_tree and int(row.get("unpriced_usage_count") or 0) > 0:
                    raise BudgetConflictError(
                        "Cannot switch to USD budget while the period has unpriced usage"
                    )
        if isinstance(self._repository, BudgetResolutionRepository):
            for user_id in switching_users:
                scope = LedgerScope(
                    scope_type="person", scope_id=user_id, period_start=period_start,
                )
                receipts = self._repository.budget_reservation_receipts(scope, limit=20)
                if any(receipt.budget_mode != target_mode for receipt in receipts):
                    raise BudgetConflictError(
                        "Cannot switch budget unit while unresolved upper-bound reservations exist"
                    )
        finalizations = getattr(self._repository, "budget_reservation_finalizations", None)
        if isinstance(finalizations, Mapping):
            for item in finalizations.values():
                if (
                    getattr(item, "scope_type", None) == "person"
                    and getattr(item, "period_start", None) == period_start
                    and getattr(item, "scope_id", None) in switching_users
                    and getattr(item, "finalization_kind", None) == "unverified_upper_bound"
                    and getattr(item, "budget_mode", "tokens") != target_mode
                ):
                    raise BudgetConflictError(
                        "Cannot switch budget unit while unresolved upper-bound reservations exist"
                    )

    @staticmethod
    def _forecast_tokens(
        used_tokens: int, period_start: date, period_end: date, now: datetime
    ) -> int:
        if now.date() >= period_end:
            return used_tokens
        if now.date() < period_start:
            return 0
        elapsed_days = max(1, (now.date() - period_start).days + 1)
        period_days = (period_end - period_start).days
        return round(used_tokens * period_days / elapsed_days)

    @staticmethod
    def _forecast_usd(
        used_usd: Decimal, period_start: date, period_end: date, now: datetime,
    ) -> Decimal:
        if now.date() >= period_end:
            return used_usd
        if now.date() < period_start:
            return Decimal(0)
        elapsed_days = max(1, (now.date() - period_start).days + 1)
        period_days = (period_end - period_start).days
        return (used_usd * period_days / elapsed_days).quantize(
            Decimal("0.00000000000001"), rounding=ROUND_HALF_UP,
        )

    def overview(self, period: str, *, include_users: bool = False) -> TokenBudgetResponse:
        period_start, period_end = period_bounds(period)
        budgets = {
            (row["scope_type"], row["scope_id"]): row
            for row in self._repository.list_token_budgets(period_start)
        }
        usage_rows = self._repository.token_usage_by_budget_scope(
            datetime.combine(period_start, time.min, tzinfo=UTC),
            datetime.combine(period_end, time.min, tzinfo=UTC),
        )
        usage = {
            (row["scope_type"], row["scope_id"]): int(row["used_tokens"])
            for row in usage_rows
        }
        usd_usage = {
            (row["scope_type"], row["scope_id"]): Decimal(str(row.get("used_usd") or 0))
            for row in usage_rows
        }
        unpriced_usage = {
            (row["scope_type"], row["scope_id"]): int(row.get("unpriced_usage_count") or 0)
            for row in usage_rows
        }
        now = datetime.now(UTC)
        items: list[dict[str, Any]] = []
        risk_items: list[dict[str, Any]] = []
        entities = self._entities()
        scope_modes: dict[tuple[BudgetScopeType, str], str] = {}
        for scope_type in cast(
            tuple[BudgetScopeType, ...],
            ("organization", "department", "user"),
        ):
            scoped_entities = entities[scope_type]
            if scope_type == "user" and not include_users:
                # An unallocated person cannot be warning or exceeded, so the compact
                # overview only needs to evaluate people who actually have a budget.
                scoped_entities = [
                    entity
                    for entity in scoped_entities
                    if ("user", entity.id) in budgets
                ]
            for entity in scoped_entities:
                budget = budgets.get((scope_type, entity.id))
                parent_type = self._parent_scope.get(scope_type)
                inherited_mode = (
                    scope_modes.get((parent_type, entity.parent_id), "tokens")
                    if parent_type and entity.parent_id else "tokens"
                )
                budget_mode = (
                    budget.get("budget_mode", inherited_mode) if budget else inherited_mode
                )
                scope_modes[(scope_type, entity.id)] = budget_mode
                token_limit = (
                    int(budget["token_limit"])
                    if budget and budget_mode == "tokens" and budget["token_limit"] is not None
                    else None
                )
                usd_limit = (
                    Decimal(str(budget["usd_limit"]))
                    if budget and budget_mode == "usd" and budget.get("usd_limit") is not None
                    else None
                )
                warning_threshold = (
                    int(budget["warning_threshold_percent"]) if budget else 80
                )
                used_tokens = usage.get((scope_type, entity.id), 0)
                scope_key = (scope_type, entity.id)
                used_usd = usd_usage.get(scope_key, Decimal(0))
                unpriced_usage_count = unpriced_usage.get(scope_key, 0)
                has_unpriced_usage = unpriced_usage_count > 0
                forecast_tokens = self._forecast_tokens(
                    used_tokens, period_start, period_end, now
                )
                forecast_usd = self._forecast_usd(
                    used_usd, period_start, period_end, now,
                )
                usage_percent = (
                    round(used_tokens / token_limit * 100, 1)
                    if token_limit
                    else round(float(used_usd / usd_limit * 100), 1)
                    if usd_limit is not None and used_usd is not None
                    else None
                )
                forecast_percent = (
                    round(forecast_tokens / token_limit * 100, 1)
                    if token_limit
                    else round(float(forecast_usd / usd_limit * 100), 1)
                    if usd_limit is not None and forecast_usd is not None
                    else None
                )
                remaining_usd = (
                    usd_limit - used_usd
                    if usd_limit is not None and used_usd is not None
                    else None
                )
                if budget is None:
                    status = "unallocated"
                elif (
                    budget_mode == "usd"
                    and usd_limit is None
                ) or (token_limit is None and usd_limit is None):
                    status = "unverified"
                elif budget_mode == "usd" and used_usd is not None and usd_limit is not None:
                    if used_usd >= usd_limit:
                        status = "exceeded"
                    elif max(usage_percent or 0, forecast_percent or 0) >= warning_threshold:
                        status = "warning"
                    else:
                        status = "healthy"
                elif token_limit is None:
                    status = "unverified"
                elif used_tokens >= token_limit:
                    status = "exceeded"
                elif max(usage_percent or 0, forecast_percent or 0) >= warning_threshold:
                    status = "warning"
                else:
                    status = "healthy"
                item = {
                    "scope_type": scope_type,
                    "scope_id": entity.id,
                    "scope_name": entity.name,
                    "parent_scope_id": entity.parent_id,
                    "budget_mode": budget_mode,
                    "token_limit": token_limit,
                    "usd_limit": usd_limit,
                    "warning_threshold_percent": warning_threshold,
                    "used_tokens": used_tokens,
                    "remaining_tokens": token_limit - used_tokens if token_limit else None,
                    "used_usd": used_usd,
                    "remaining_usd": remaining_usd,
                    "forecast_usd": forecast_usd,
                    "usd_cost_available": not has_unpriced_usage,
                    "unpriced_usage_count": unpriced_usage_count,
                    "usage_percent": usage_percent,
                    "forecast_tokens": forecast_tokens,
                    "forecast_percent": forecast_percent,
                    "status": status,
                    "updated_at": budget["updated_at"] if budget else None,
                    "updated_by": budget["updated_by"] if budget else None,
                }
                if include_users or scope_type != "user":
                    items.append(item)
                if status in {"warning", "exceeded"}:
                    risk_items.append(
                        {
                            "scope_type": scope_type,
                            "scope_id": entity.id,
                            "scope_name": entity.name,
                            "status": status,
                            "usage_percent": usage_percent,
                            "forecast_percent": forecast_percent,
                        }
                    )

        if include_users:
            policies = {
                row["user_id"]: row["model_ids"]
                for row in self._repository.list_user_model_policies(
                    [item["scope_id"] for item in items if item["scope_type"] == "user"]
                )
            }
            for item in items:
                if item["scope_type"] == "user":
                    item["model_policy_configured"] = item["scope_id"] in policies
                    item["allowed_model_ids"] = policies.get(item["scope_id"], [])

        risk_items.sort(
            key=lambda item: (
                item["forecast_percent"] or 0,
                item["usage_percent"] or 0,
                item["scope_name"],
            ),
            reverse=True,
        )

        names = {
            (scope_type, entity.id): entity.name
            for scope_type, scoped_entities in entities.items()
            for entity in scoped_entities
        }
        budget_history = [
            {
                **row,
                "event_type": "budget",
                "scope_name": names.get(
                    (cast(BudgetScopeType, row["scope_type"]), row["scope_id"]),
                    row["scope_id"],
                ),
            }
            for row in self._repository.list_token_budget_audit(period_start, 50)
            if row["action"] != "updated"
            or row.get("previous_budget_mode", "tokens")
            != row.get("new_budget_mode", "tokens")
            or row["previous_token_limit"] != row["new_token_limit"]
            or row.get("previous_usd_limit") != row.get("new_usd_limit")
            or row["previous_warning_threshold_percent"]
            != row["new_warning_threshold_percent"]
        ]
        user_names = {entity.id: entity.name for entity in entities["user"]}
        model_identities = self._repository.model_identities()
        model_names = {
            key: identity.display_name for key, identity in model_identities.items()
        }
        model_history = [
            {
                **row,
                "event_type": "model_access",
                "user_name": user_names.get(row["user_id"], row["user_id"]),
                "previous_model_names": [
                    model_names.get(str(model_id), str(model_id))
                    for model_id in row["previous_model_ids"]
                ],
                "new_model_names": [
                    model_names.get(str(model_id), str(model_id))
                    for model_id in row["new_model_ids"]
                ],
            }
            for row in self._repository.list_user_model_access_audit(
                list(user_names),
                datetime.combine(period_start, time.min, tzinfo=UTC),
                datetime.combine(period_end, time.min, tzinfo=UTC),
                50,
            )
        ]
        department_names = {
            entity.id: entity.name for entity in self._entities()["department"]
        }
        enforcement_history = [
            {
                **row,
                "event_type": "enforcement",
                "department_name": department_names.get(
                    row["department_id"], row["department_id"]
                ),
            }
            for row in self._repository.list_department_enforcement_audit(
                datetime.combine(period_start, time.min, tzinfo=UTC),
                datetime.combine(period_end, time.min, tzinfo=UTC),
                50,
            )
        ]
        history = sorted(
            [*budget_history, *model_history, *enforcement_history],
            key=lambda row: (row["changed_at"], str(row["id"])),
            reverse=True,
        )[:50]
        stored_enforcement = {
            str(row["department_id"]): row
            for row in self._repository.list_department_enforcement()
        }
        enforcement = [
            {
                "department_id": department_id,
                "department_name": name,
                # No stored row means enforcement was never configured, which must not
                # silently start blocking people.
                "mode": stored_enforcement[department_id]["mode"]
                if department_id in stored_enforcement
                else "audit",
                "updated_at": stored_enforcement.get(department_id, {}).get("updated_at"),
                "updated_by": stored_enforcement.get(department_id, {}).get("updated_by"),
            }
            for department_id, name in department_names.items()
        ]
        return TokenBudgetResponse.model_validate(
            {
                "period": period,
                "period_start": period_start,
                "period_end": period_end,
                "generated_at": now,
                "items": items,
                "risk_count": len(risk_items),
                "risk_items": risk_items[:25],
                "history": history,
                "enforcement": enforcement,
            }
        )

    @staticmethod
    def _matches_people_filter(
        item: TokenBudgetItem, status: PeopleBudgetFilter
    ) -> bool:
        if status == "all":
            return True
        if status == "assigned":
            return item.token_limit is not None or item.usd_limit is not None
        if status == "unallocated":
            return item.token_limit is None and item.usd_limit is None
        return item.status == status

    def people(
        self,
        period: str,
        department_id: str,
        query: str | None,
        status: PeopleBudgetFilter,
        offset: int,
        limit: int,
    ) -> TokenBudgetPeopleResponse:
        department = self._entity("department", department_id)
        overview = self.overview(period, include_users=True)
        department_budget = next(
            (
                item
                for item in overview.items
                if item.scope_type == "department" and item.scope_id == department_id
            ),
            None,
        )
        all_people = [
            item
            for item in overview.items
            if item.scope_type == "user" and item.parent_scope_id == department_id
        ]
        policies = {
            row["user_id"]: row
            for row in self._repository.list_user_model_policies(
                [item.scope_id for item in all_people]
            )
        }
        enabled_model_ids = {
            row["id"]
            for row in self._repository.registry()["models"]
            if row["enabled"]
        }
        all_people = [
            item.model_copy(
                update={
                    "model_policy_configured": item.scope_id in policies,
                    "allowed_model_ids": [
                        model_id
                        for model_id in policies.get(item.scope_id, {}).get(
                            "model_ids", []
                        )
                        if model_id in enabled_model_ids
                    ],
                }
            )
            for item in all_people
        ]
        assigned_count = sum(
            item.token_limit is not None or item.usd_limit is not None for item in all_people
        )
        unallocated_count = len(all_people) - assigned_count
        risk_count = sum(item.status in {"warning", "exceeded"} for item in all_people)
        model_configured_count = sum(
            item.model_policy_configured for item in all_people
        )
        normalized_query = (query or "").strip().casefold()
        filtered = [
            item
            for item in all_people
            if (not normalized_query or normalized_query in item.scope_name.casefold())
            and self._matches_people_filter(item, status)
        ]
        allocated = sum(item.token_limit or 0 for item in all_people)
        allocated_usd = sum(
            (item.usd_limit or Decimal(0) for item in all_people), Decimal(0)
        )
        department_limit = department_budget.token_limit if department_budget else None
        department_usd_limit = department_budget.usd_limit if department_budget else None
        return TokenBudgetPeopleResponse(
            period=period,
            department_id=department_id,
            department_name=department.name,
            department_token_limit=department_limit,
            department_allocated_tokens=allocated,
            department_available_tokens=(
                department_limit - allocated if department_limit is not None else None
            ),
            department_usd_limit=department_usd_limit,
            department_allocated_usd=allocated_usd if department_usd_limit is not None else None,
            department_available_usd=(
                department_usd_limit - allocated_usd
                if department_usd_limit is not None else None
            ),
            total=len(filtered),
            offset=offset,
            limit=limit,
            assigned_count=assigned_count,
            unallocated_count=unallocated_count,
            risk_count=risk_count,
            model_configured_count=model_configured_count,
            items=filtered[offset : offset + limit],
        )

    def bulk_save_people(
        self,
        period: str,
        write: TokenBudgetBulkWrite,
        changed_by: str,
    ) -> TokenBudgetBulkResult:
        period_start, _ = period_bounds(period)
        department = self._entity("department", write.department_id)
        all_users = [
            entity
            for entity in self._entities()["user"]
            if entity.parent_id == write.department_id
        ]
        overview = self.overview(period, include_users=True)
        user_items = {
            item.scope_id: item
            for item in overview.items
            if item.scope_type == "user" and item.parent_scope_id == write.department_id
        }
        if write.selection == "ids":
            requested_ids = set(write.user_ids)
            selected = [entity for entity in all_users if entity.id in requested_ids]
            if len(selected) != len(requested_ids):
                raise BudgetNotFoundError(
                    "One or more selected users do not belong to the department"
                )
        else:
            normalized_query = (write.query or "").strip().casefold()
            selected = [
                entity
                for entity in all_users
                if (not normalized_query or normalized_query in entity.name.casefold())
                and self._matches_people_filter(user_items[entity.id], write.status)
            ]
        if not selected:
            raise BudgetConflictError("No users match the bulk allocation selection")

        if write.allocation_mode == "preserve" and write.model_ids is None:
            raise BudgetConflictError(
                "Select a budget update or a model access update"
            )
        if write.model_ids is not None:
            enabled_model_ids = {
                row["id"]
                for row in self._repository.registry()["models"]
                if row["enabled"]
            }
            if not set(write.model_ids).issubset(enabled_model_ids):
                raise BudgetConflictError(
                    "One or more selected models are unavailable"
                )

        selected_ids = {entity.id for entity in selected}
        token_limit: int | None = None
        usd_limit: Decimal | None = None
        total_allocated: int | None = None
        total_usd_allocated: Decimal | None = None
        entries: list[tuple[str, Any]] = []
        if write.allocation_mode != "preserve":
            if write.budget_mode == "usd" and self._require_atomic_budget is not None:
                self._require_atomic_budget()
            department_budget = next(
                (
                    item
                    for item in overview.items
                    if item.scope_type == "department"
                    and item.scope_id == write.department_id
                ),
                None,
            )
            if department_budget is None or (
                department_budget.token_limit is None and department_budget.usd_limit is None
            ):
                raise BudgetConflictError(
                    "Assign the department budget before allocating user budgets"
                )
            if department_budget.budget_mode != write.budget_mode:
                raise BudgetConflictError("Parent and child budgets must use the same mode")
            limit_field = "usd_limit" if write.budget_mode == "usd" else "token_limit"
            department_limit = getattr(department_budget, limit_field)
            if department_limit is None:
                raise BudgetConflictError("Department budget has an incomplete limit")
            existing_child_limits = [
                getattr(item, limit_field)
                for item in user_items.values()
                if item.scope_id not in selected_ids
                and (item.token_limit is not None or item.usd_limit is not None)
            ]
            if any(value is None for value in existing_child_limits):
                raise BudgetConflictError("Budget hierarchy has an incomplete limit")
            unselected_total = sum(
                (value for value in existing_child_limits if value is not None),
                Decimal(0) if write.budget_mode == "usd" else 0,
            )
            available_for_selection = department_limit - unselected_total
            if write.allocation_mode == "equal_remaining":
                if write.budget_mode == "usd":
                    usd_limit = (
                        available_for_selection / len(selected)
                    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    if usd_limit < Decimal("0.01"):
                        raise BudgetConflictError(
                            "The department has no remaining budget to distribute"
                        )
                else:
                    token_limit = available_for_selection // len(selected)
                    if token_limit < 1:
                        raise BudgetConflictError(
                            "The department has no remaining budget to distribute"
                        )
            else:
                token_limit = write.token_limit
                usd_limit = write.usd_limit
            amount_per_user: int | Decimal | None = (
                usd_limit if write.budget_mode == "usd" else token_limit
            )
            if amount_per_user is None:
                raise BudgetConflictError("A budget limit is required for fixed allocation")
            total = amount_per_user * len(selected)
            if total > available_for_selection:
                raise BudgetConflictError(
                    "User allocations would exceed the department budget"
                )
            if write.budget_mode == "usd":
                total_usd_allocated = cast(Decimal, total)
            else:
                total_allocated = cast(int, total)
            entries = [(entity.id, amount_per_user) for entity in selected]
        try:
            self._repository.bulk_upsert_user_budgets(
                period_start,
                department.id,
                entries,
                write.warning_threshold_percent,
                changed_by,
                budget_mode=write.budget_mode,
                selected_user_ids=[entity.id for entity in selected],
                model_ids=write.model_ids,
            )
        except BudgetConstraintViolation as error:
            raise BudgetConflictError(str(error)) from error
        if write.model_ids is not None:
            self._publish_model_access([entity.id for entity in selected])
        return TokenBudgetBulkResult(
            period=period,
            department_id=department.id,
            updated_count=len(selected),
            token_limit_per_user=token_limit,
            usd_limit_per_user=usd_limit,
            total_allocated=total_allocated,
            total_usd_allocated=total_usd_allocated,
            model_policy_updated_count=(
                len(selected) if write.model_ids is not None else 0
            ),
        )

    def save(
        self,
        period: str,
        scope_type: BudgetScopeType,
        scope_id: str,
        write: TokenBudgetWrite,
        changed_by: str,
    ) -> TokenBudgetResponse:
        period_start, _ = period_bounds(period)
        entity = self._entity(scope_type, scope_id)
        budgets = {
            (row["scope_type"], row["scope_id"]): row
            for row in self._repository.list_token_budgets(period_start)
        }
        entities = self._entities()
        amount = write.usd_limit if write.budget_mode == "usd" else write.token_limit
        if amount is None:
            raise BudgetConflictError("A budget limit is required")
        limit_field = "usd_limit" if write.budget_mode == "usd" else "token_limit"
        previous = budgets.get((scope_type, scope_id))
        if (self._require_atomic_budget is not None
            and (write.budget_mode == "usd" or (previous or {}).get("budget_mode") == "usd")):
            self._require_atomic_budget()
        if (scope_type == "organization" and previous is not None
            and previous.get("budget_mode", "tokens") != write.budget_mode):
            self._require_unit_switch_ready(
                period_start, scope_id, write.budget_mode, budgets, entities,
            )
            try:
                self._repository.switch_organization_budget(
                    period_start, scope_id, write.budget_mode, amount,
                    write.warning_threshold_percent, changed_by,
                )
            except BudgetConstraintViolation as error:
                raise BudgetConflictError(str(error)) from error
            return self.overview(period)

        parent_type = self._parent_scope.get(scope_type)
        if parent_type is not None:
            parent = budgets.get((parent_type, entity.parent_id))
            if parent is None:
                raise BudgetConflictError(
                    f"Assign the {parent_type} budget before allocating {scope_type} budgets"
                )
            if parent.get("budget_mode", "tokens") != write.budget_mode:
                raise BudgetConflictError(
                    "Parent and child budgets must use the same mode"
                )
            siblings = [
                budgets[(scope_type, sibling.id)]
                for sibling in entities[scope_type]
                if sibling.parent_id == entity.parent_id
                and sibling.id != scope_id
                and (scope_type, sibling.id) in budgets
            ]
            if any(item.get("budget_mode", "tokens") != write.budget_mode for item in siblings):
                raise BudgetConflictError("Sibling budgets must use the same mode")
            sibling_limits = [
                budgets[(scope_type, sibling.id)].get(limit_field)
                for sibling in entities[scope_type]
                if sibling.parent_id == entity.parent_id
                and sibling.id != scope_id
                and (scope_type, sibling.id) in budgets
            ]
            if any(value is None for value in sibling_limits):
                raise BudgetConflictError("Budget hierarchy has an incomplete limit")
            sibling_total = amount + sum(
                value for value in sibling_limits if value is not None
            )
            parent_limit = parent.get(limit_field)
            if parent_limit is None or sibling_total > parent_limit:
                raise BudgetConflictError(
                    f"{scope_type.title()} allocations would exceed the "
                    f"{parent_type} budget"
                )

        child_type = self._child_scope.get(scope_type)
        if child_type is not None:
            children = [
                budgets[(child_type, child.id)]
                for child in entities[child_type]
                if child.parent_id == scope_id and (child_type, child.id) in budgets
            ]
            if any(item.get("budget_mode", "tokens") != write.budget_mode for item in children):
                raise BudgetConflictError("Parent and child budgets must use the same mode")
            child_limits = [item.get(limit_field) for item in children]
            if any(value is None for value in child_limits):
                raise BudgetConflictError("Budget hierarchy has an incomplete limit")
            child_total = sum(value for value in child_limits if value is not None)
            if child_total > amount:
                raise BudgetConflictError(
                    f"The {scope_type} budget cannot be lower than its "
                    "allocated child budgets"
                )

        try:
            self._repository.upsert_token_budget(
                period_start,
                scope_type,
                scope_id,
                entity.parent_id,
                write.token_limit,
                write.warning_threshold_percent,
                changed_by,
                budget_mode=write.budget_mode,
                usd_limit=write.usd_limit,
            )
        except BudgetConstraintViolation as error:
            raise BudgetConflictError(str(error)) from error
        return self.overview(period)

    def remove(
        self,
        period: str,
        scope_type: BudgetScopeType,
        scope_id: str,
        changed_by: str,
    ) -> TokenBudgetResponse:
        period_start, _ = period_bounds(period)
        self._entity(scope_type, scope_id)
        child_type = self._child_scope.get(scope_type)
        if child_type is not None:
            entities = self._entities()
            budgets = {
                (row["scope_type"], row["scope_id"]): row
                for row in self._repository.list_token_budgets(period_start)
            }
            if any(
                child.parent_id == scope_id and (child_type, child.id) in budgets
                for child in entities[child_type]
            ):
                raise BudgetConflictError(
                    f"Remove allocated {child_type} budgets before removing "
                    f"this {scope_type} budget"
                )
        try:
            deleted = self._repository.delete_token_budget(
                period_start, scope_type, scope_id, changed_by
            )
        except BudgetConstraintViolation as error:
            raise BudgetConflictError(str(error)) from error
        if not deleted:
            raise BudgetNotFoundError("Token budget is not assigned")
        return self.overview(period)

    def set_enforcement(
        self,
        period: str,
        department_id: str,
        write: DepartmentEnforcementWrite,
        changed_by: str,
    ) -> TokenBudgetResponse:
        """Returns the whole overview so the caller needs no second round trip."""
        self._entity("department", department_id)
        self._repository.set_department_enforcement(
            department_id, write.mode, changed_by
        )
        return self.overview(period)