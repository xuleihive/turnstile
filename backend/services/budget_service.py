from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from typing import Any, cast

from turnstile_core.domain.enterprise import (
    governance_directory,
    merge_application_owners,
)
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
        *,
        seed_demo_directory: bool = True,
    ) -> None:
        self._repository = repository
        self._project_model_access = project_model_access
        # Defaults to True so an existing caller keeps the behaviour it had; a deployment that
        # is not a demo turns it off and the fixture people leave the budget pages.
        self._seed_demo_directory = seed_demo_directory

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
            governance_directory(
                self._repository.observed_users(),
                include_seeded_people=self._seed_demo_directory,
            ),
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
        now = datetime.now(UTC)
        items: list[dict[str, Any]] = []
        risk_items: list[dict[str, Any]] = []
        entities = self._entities()
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
                token_limit = int(budget["token_limit"]) if budget else None
                warning_threshold = (
                    int(budget["warning_threshold_percent"]) if budget else 80
                )
                used_tokens = usage.get((scope_type, entity.id), 0)
                forecast_tokens = self._forecast_tokens(
                    used_tokens, period_start, period_end, now
                )
                usage_percent = (
                    round(used_tokens / token_limit * 100, 1) if token_limit else None
                )
                forecast_percent = (
                    round(forecast_tokens / token_limit * 100, 1)
                    if token_limit
                    else None
                )
                if token_limit is None:
                    status = "unallocated"
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
                    "token_limit": token_limit,
                    "warning_threshold_percent": warning_threshold,
                    "used_tokens": used_tokens,
                    "remaining_tokens": token_limit - used_tokens if token_limit else None,
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
            or row["previous_token_limit"] != row["new_token_limit"]
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
            return item.token_limit is not None
        if status == "unallocated":
            return item.token_limit is None
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
        all_people = [
            item.model_copy(
                update={
                    "model_policy_configured": item.scope_id in policies,
                    "allowed_model_ids": policies.get(item.scope_id, {}).get(
                        "model_ids", []
                    ),
                }
            )
            for item in all_people
        ]
        assigned_count = sum(item.token_limit is not None for item in all_people)
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
        department_limit = department_budget.token_limit if department_budget else None
        return TokenBudgetPeopleResponse(
            period=period,
            department_id=department_id,
            department_name=department.name,
            department_token_limit=department_limit,
            department_allocated_tokens=allocated,
            department_available_tokens=(
                department_limit - allocated if department_limit is not None else None
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
        total_allocated: int | None = None
        entries: list[tuple[str, int]] = []
        if write.allocation_mode != "preserve":
            department_budget = next(
                (
                    item
                    for item in overview.items
                    if item.scope_type == "department"
                    and item.scope_id == write.department_id
                ),
                None,
            )
            if department_budget is None or department_budget.token_limit is None:
                raise BudgetConflictError(
                    "Assign the department budget before allocating user budgets"
                )
            unselected_total = sum(
                item.token_limit or 0
                for item in user_items.values()
                if item.scope_id not in selected_ids
            )
            available_for_selection = department_budget.token_limit - unselected_total
            if write.allocation_mode == "equal_remaining":
                token_limit = available_for_selection // len(selected)
                if token_limit < 1:
                    raise BudgetConflictError(
                        "The department has no remaining budget to distribute"
                    )
            else:
                if write.token_limit is None:
                    raise BudgetConflictError(
                        "token_limit is required for fixed bulk allocation"
                    )
                token_limit = write.token_limit
            total_allocated = token_limit * len(selected)
            if total_allocated > available_for_selection:
                raise BudgetConflictError(
                    "User allocations would exceed the department budget of "
                    f"{department_budget.token_limit} tokens"
                )
            entries = [(entity.id, token_limit) for entity in selected]
        try:
            self._repository.bulk_upsert_user_budgets(
                period_start,
                department.id,
                entries,
                write.warning_threshold_percent,
                changed_by,
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
            total_allocated=total_allocated,
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

        parent_type = self._parent_scope.get(scope_type)
        if parent_type is not None:
            parent = budgets.get((parent_type, entity.parent_id))
            if parent is None:
                raise BudgetConflictError(
                    f"Assign the {parent_type} budget before allocating {scope_type} budgets"
                )
            sibling_total = write.token_limit + sum(
                int(budgets[(scope_type, sibling.id)]["token_limit"])
                for sibling in entities[scope_type]
                if sibling.parent_id == entity.parent_id
                and sibling.id != scope_id
                and (scope_type, sibling.id) in budgets
            )
            if sibling_total > int(parent["token_limit"]):
                raise BudgetConflictError(
                    f"{scope_type.title()} allocations would exceed the "
                    f"{parent_type} budget of {int(parent['token_limit'])} tokens"
                )

        child_type = self._child_scope.get(scope_type)
        if child_type is not None:
            child_total = sum(
                int(budgets[(child_type, child.id)]["token_limit"])
                for child in entities[child_type]
                if child.parent_id == scope_id and (child_type, child.id) in budgets
            )
            if child_total > write.token_limit:
                raise BudgetConflictError(
                    f"The {scope_type} budget cannot be lower than its "
                    f"{child_total} allocated child tokens"
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