from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from turnstile_core.domain.enterprise import (
    enterprise_catalog,
    merge_application_owners,
    merge_observed_users,
)
from turnstile_core.domain.model_router import (
    ModelRouter,
    ModelRouterAssignedList,
    ModelRouterAssignment,
    ModelRouterAssignmentWrite,
    ModelRouterBulkAssignmentResult,
    ModelRouterBulkAssignmentWrite,
    ModelRouterDecisionCandidatePreview,
    ModelRouterDecisionPreview,
    ModelRouterDecisionPreviewWrite,
    ModelRouterDecisionProbability,
    ModelRouterDecisionProtocol,
    ModelRouterDecisionTrialResult,
    ModelRouterDecisionTrialWrite,
    ModelRouterDefinition,
    ModelRouterDepartmentAccessSummary,
    ModelRouterDuplicateWrite,
    ModelRouterListResponse,
    ModelRouterModelCatalog,
    ModelRouterModelReference,
    ModelRouterNode,
    ModelRouterPeopleAccessResponse,
    ModelRouterPersonAccessItem,
    ModelRouterReasoningEffort,
    ModelRouterStrategyModel,
    ModelRouterValidationIssue,
    ModelRouterValidationResult,
    ModelRouterWrite,
)
from turnstile_core.domain.models import ModelPrice
from turnstile_core.domain.runtime_models import (
    ChatMessage,
    ChoiceQuestion,
    DecisionInvocationRequest,
    DecisionInvocationResponse,
    GatewayModelCatalogItem,
    GatewayModelCatalogResponse,
    InvocationMetadata,
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from turnstile_core.integrations.router_table_projection import (
    project_router_to_table_if_configured,
)
from turnstile_core.persistence.repository import QueryRepository

from .runtime_service import ModelRuntimeService

_USER_REQUEST_RE = re.compile(r"<userRequest>\s*(.*?)\s*</userRequest>", re.DOTALL)
_CONTEXT_BLOCK_RE = re.compile(r"<context>.*?</context>", re.DOTALL | re.IGNORECASE)
_CURRENT_DATETIME_RE = re.compile(
    r"<current_datetime>.*?</current_datetime>\s*",
    re.DOTALL | re.IGNORECASE,
)
_QUOTED_USER_MESSAGE_RE = re.compile(
    r"(<user_message>\s*)(.*?)(\s*</user_message>)",
    re.DOTALL | re.IGNORECASE,
)
_SDK_SYSTEM_PREFIX_END_MARKERS = (
    "</system_notifications>",
    "</custom_instructions>",
    "</tools>",
    "</environment_limitations>",
    "</code_change_instructions>",
)
_SDK_SYSTEM_SUFFIX_START_MARKERS = (
    "<memories>",
    "<session_search_context>",
    "<session_context>",
    "<task_completion>",
    "Your goal is to deliver complete, working solutions.",
    "Use only tools exposed by this application.",
    "Current page context:",
    "Conversation history:",
)
_CATALOG_PLACEHOLDER = "{catalog}"
logger = logging.getLogger(__name__)

_DECISION_PROMPT_MAX_CHARS = 4_000
_DECISION_TIMEOUT_MS = 5_000
_DEFAULT_SESSION_STICKY_TTL_SECONDS = 1_800
_DEFAULT_INTERACTION_STICKY_TTL_SECONDS = 300
_DECISION_MAX_OUTPUT_TOKENS = 200
_DECISION_REASONING_MAX_OUTPUT_TOKENS = 1_024
_DECISION_JSON_SHAPE = (
    "Return only a JSON object with this shape: "
    '{"model_id":"<candidate UUID>","rationale":"<one short sentence>"}. '
    "Never return a model outside the catalog."
)
_DEFAULT_DECISION_PROMPT = (
    "You are Turnstile's model routing classifier. Select the single best "
    "model from the candidate catalog for the user's request based on each "
    "candidate's description and unit cost.\n\n"
    "Candidate catalog:\n{catalog}\n\n" + _DECISION_JSON_SHAPE
)
_DEFAULT_SYSTEM_ONE_INSTRUCTIONS = (
    "Select the single best candidate model for the user's request based on "
    "each candidate's description and unit cost."
)


@dataclass(frozen=True)
class _RouterExecutionPlan:
    candidates: list[ModelRouterStrategyModel]
    fallback_events: set[str]
    strategy_type: str
    routing_model_id: UUID | None
    attempt_timeout_ms: int | None
    decision_timeout_ms: int
    decision_prompt: str | None
    max_prompt_chars: int
    condition_evaluations: list[dict[str, Any]]
    decision_reasoning_effort: ModelRouterReasoningEffort | None = None


class ModelRouterConflictError(ValueError):
    pass


class ModelRouterNotFoundError(ValueError):
    pass


class _ModelRouterLifecycleService(ABC):
    def __init__(
        self,
        repository: QueryRepository,
        project_model_access: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self._repository = repository
        self._project_model_access = project_model_access

    def _publish_assignee_model_access(self, user_ids: Sequence[str]) -> None:
        if self._project_model_access is None or not user_ids:
            return
        try:
            self._project_model_access(list(user_ids))
        except Exception:
            logger.warning(
                "Router model access saved but not projected to ledger for %s people",
                len(user_ids),
                exc_info=True,
            )

    @staticmethod
    @abstractmethod
    def validate_definition(
        definition: ModelRouterDefinition,
        *,
        available_model_ids: set[UUID] | None = None,
    ) -> ModelRouterValidationResult: ...

    @abstractmethod
    def _assert_models_allowed(
        self, definition: ModelRouterDefinition, user_email: str, role: str
    ) -> None: ...

    @abstractmethod
    def _available_model_ids(self, user_email: str, role: str) -> set[UUID]: ...

    @staticmethod
    @abstractmethod
    def _candidate_model_ids(definition: ModelRouterDefinition) -> set[UUID]: ...

    @staticmethod
    @abstractmethod
    def _values(write: ModelRouterWrite, changed_by: str) -> Mapping[str, Any]: ...

    def list(self, owner_id: UUID) -> ModelRouterListResponse:
        items = [
            ModelRouter.model_validate(row)
            for row in self._repository.list_model_routers(owner_id)
        ]
        model_ids = sorted({
            model_id
            for router in items
            for node in router.definition.nodes
            for model_id in (
                *(candidate.model_id for candidate in node.candidates),
                *(config.model_id for config in node.model_configs),
                node.routing_model_id,
            )
            if model_id is not None
        }, key=str)
        return ModelRouterListResponse(
            items=items,
            model_references=[
                ModelRouterModelReference.model_validate(row)
                for row in self._repository.model_router_model_references(model_ids)
            ] if model_ids else [],
        )

    def catalog(self, user_email: str, role: str) -> ModelRouterModelCatalog:
        return ModelRouterModelCatalog(
            model_ids=sorted(
                self._available_model_ids(user_email, role),
                key=str,
            )
        )

    def preview_decision(
        self,
        write: ModelRouterDecisionPreviewWrite,
        user_email: str,
        role: str,
    ) -> ModelRouterDecisionPreview:
        self._assert_models_allowed(write.definition, user_email, role)
        available = self._available_model_ids(user_email, role)
        models = {
            UUID(str(model["id"])): model
            for model in self._repository.registry()["models"]
            if UUID(str(model["id"])) in available
        }
        return ModelRouterExecutionService.decision_preview(write, models)

    def get(self, router_id: UUID, owner_id: UUID) -> ModelRouter:
        row = self._repository.get_model_router(router_id, owner_id)
        if row is None:
            raise ModelRouterNotFoundError("Model router not found")
        return ModelRouter.model_validate(row)

    def create(
        self,
        write: ModelRouterWrite,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouter:
        values = self._values(write, owner_email)
        self._assert_models_allowed(write.definition, owner_email, role)
        row = self._repository.create_model_router(owner_id, owner_email, values)
        if row is None:
            raise ModelRouterConflictError("A model router with this name already exists")
        return ModelRouter.model_validate(row)

    def update(
        self,
        router_id: UUID,
        write: ModelRouterWrite,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouter:
        existing = self._repository.get_model_router(router_id, owner_id)
        values = self._values(write, owner_email)
        self._assert_models_allowed(write.definition, owner_email, role)
        row = self._repository.update_model_router(router_id, owner_id, values)
        if row is None:
            if existing is None:
                raise ModelRouterNotFoundError("Model router not found")
            raise ModelRouterConflictError("A model router with this name already exists")
        updated = ModelRouter.model_validate(row)
        if existing and str(existing.get("name", "")) != updated.name:
            project_router_to_table_if_configured(
                None,
                self._repository,
                deleted_id=router_id,
                deleted_name=str(existing["name"]),
            )
            published_row = self._repository.get_published_model_router(router_id)
            if published_row is not None:
                project_router_to_table_if_configured(
                    ModelRouter.model_validate(published_row),
                    self._repository,
                )
        return updated

    def duplicate(
        self,
        router_id: UUID,
        write: ModelRouterDuplicateWrite,
        owner_id: UUID,
        owner_email: str,
        role: str,
    ) -> ModelRouter:
        source = self.get(router_id, owner_id)
        duplicate = ModelRouterWrite(
            name=write.name,
            description=source.description,
            definition=source.definition,
        )
        return self.create(duplicate, owner_id, owner_email, role)

    def validate(
        self, router_id: UUID, owner_id: UUID, user_email: str, role: str
    ) -> ModelRouterValidationResult:
        router = self.get(router_id, owner_id)
        return self.validate_definition(
            router.definition,
            available_model_ids=self._available_model_ids(user_email, role),
        )

    def publish(
        self, router_id: UUID, owner_id: UUID, user_email: str, role: str
    ) -> ModelRouter:
        result = self.validate(router_id, owner_id, user_email, role)
        if not result.valid:
            raise ModelRouterConflictError(
                "Model router validation failed: "
                + "; ".join(issue.message for issue in result.issues)
            )
        router = self.get(router_id, owner_id)
        assignees = list(self._repository.list_model_router_assignees(router_id))
        for assignee in assignees:
            self._assert_assignee_models(router.definition, assignee)
        def project_before_commit(candidate: dict[str, Any]) -> None:
            from turnstile_core.config import get_settings

            settings = get_settings()
            project_router_to_table_if_configured(
                ModelRouter.model_validate(candidate), self._repository,
                required=bool(
                    settings.ledger_table_endpoint and settings.ledger_table_name
                ),
                use_published_snapshot=False,
                prepare_only=True,
            )

        row = self._repository.publish_model_router(
            router_id, owner_id, user_email,
            before_record=project_before_commit,
        )
        if row is None:
            raise ModelRouterNotFoundError("Model router not found")
        published = ModelRouter.model_validate(row)
        project_router_to_table_if_configured(published, self._repository)
        self._publish_assignee_model_access([*assignees, user_email])
        return published

    def disable(
        self, router_id: UUID, owner_id: UUID, user_email: str
    ) -> ModelRouter:
        assignees = list(self._repository.list_model_router_assignees(router_id))
        row = self._repository.disable_model_router(router_id, owner_id, user_email)
        if row is None:
            router = self._repository.get_model_router(router_id, owner_id)
            if router is None:
                raise ModelRouterNotFoundError("Model router not found")
            raise ModelRouterConflictError("Only a published model router can be disabled")
        disabled = ModelRouter.model_validate(row)
        project_router_to_table_if_configured(disabled, self._repository)
        self._publish_assignee_model_access([*assignees, user_email])
        return disabled

    def remove(self, router_id: UUID, owner_id: UUID) -> None:
        existing = self._repository.get_model_router(router_id, owner_id)
        assignees = list(self._repository.list_model_router_assignees(router_id))
        if not self._repository.delete_model_router(router_id, owner_id):
            raise ModelRouterNotFoundError("Model router not found")
        project_router_to_table_if_configured(
            None,
            self._repository,
            deleted_id=router_id,
            deleted_name=str(existing["name"]) if existing else None,
        )
        creator = str(existing.get("created_by") or "") if existing else ""
        self._publish_assignee_model_access(
            [*assignees, *([creator] if creator else [])]
        )

    def assigned(self, user_id: str) -> ModelRouterAssignedList:
        return ModelRouterAssignedList(
            items=[
                ModelRouter.model_validate(row)
                for row in self._repository.list_assigned_model_routers(user_id)
            ]
        )

    def assignees(self, router_id: UUID, owner_id: UUID) -> Sequence[str]:
        self.get(router_id, owner_id)
        return list(self._repository.list_model_router_assignees(router_id))

    def _directory_people(
        self,
        owner_email: str | None = None,
        owner_name: str | None = None,
    ) -> tuple[Sequence[tuple[str, str]], Sequence[tuple[str, str, str, str]]]:
        catalog = merge_application_owners(
            merge_observed_users(enterprise_catalog(), self._repository.observed_users()),
            self._repository.application_owners(),
        )
        departments = [(dept.id, dept.name) for dept in catalog.departments]
        dept_names = dict(departments)
        fallback_dept_id = departments[0][0] if departments else "__unassigned__"
        fallback_dept_name = dept_names.get(fallback_dept_id, "未分配")

        raw_users = list(catalog.users)
        if owner_email and not any(person.id == owner_email for person in raw_users):
            raw_users.insert(
                0,
                type(raw_users[0])(
                    id=owner_email,
                    name=owner_name or owner_email,
                    parent_id=fallback_dept_id,
                )
                if raw_users
                else None,  # type: ignore[arg-type]
            )
            raw_users = [person for person in raw_users if person is not None]

        resolved_users: list[tuple[str, str, str, str]] = []
        for person in raw_users:
            dept_id = (
                person.parent_id
                if person.parent_id and person.parent_id in dept_names
                else fallback_dept_id
            )
            dept_name = dept_names.get(dept_id, fallback_dept_name)
            resolved_users.append((person.id, person.name, dept_id, dept_name))
        if not departments and resolved_users:
            departments = [(fallback_dept_id, fallback_dept_name)]
        return departments, resolved_users

    def people(
        self,
        router_id: UUID,
        owner_id: UUID,
        owner_email: str,
        owner_name: str | None = None,
        *,
        department_id: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> ModelRouterPeopleAccessResponse:
        self.get(router_id, owner_id)
        assigned_set = set(self._repository.list_model_router_assignees(router_id))
        departments, resolved_users = self._directory_people(owner_email, owner_name)
        normalized_query = (query or "").strip().casefold()
        active_dept = (
            department_id
            if department_id and department_id != "all"
            else None
        )

        dept_totals: Counter[str] = Counter()
        dept_assigned: Counter[str] = Counter()
        filtered: list[ModelRouterPersonAccessItem] = []

        for user_id, user_name, dept_id, dept_name in resolved_users:
            is_assigned = user_id in assigned_set
            dept_totals[dept_id] += 1
            if is_assigned:
                dept_assigned[dept_id] += 1
            if active_dept and dept_id != active_dept:
                continue
            if normalized_query and not (
                normalized_query in user_name.casefold()
                or normalized_query in user_id.casefold()
                or normalized_query in dept_name.casefold()
            ):
                continue
            filtered.append(
                ModelRouterPersonAccessItem(
                    id=user_id,
                    name=user_name,
                    department_id=dept_id,
                    department_name=dept_name,
                    assigned=is_assigned,
                )
            )

        department_summaries = [
            ModelRouterDepartmentAccessSummary(
                id=dept_id,
                name=dept_name,
                total_users=dept_totals[dept_id],
                assigned_users=dept_assigned[dept_id],
            )
            for dept_id, dept_name in departments
            if dept_totals[dept_id] > 0
        ]
        matching_assigned = sum(1 for item in filtered if item.assigned)
        return ModelRouterPeopleAccessResponse(
            router_id=router_id,
            total=len(filtered),
            offset=offset,
            limit=limit,
            total_users=len(resolved_users),
            assigned_count=sum(dept_assigned.values()),
            matching_assigned_count=matching_assigned,
            departments=department_summaries,
            items=filtered[offset : offset + limit],
        )

    def assign(
        self,
        router_id: UUID,
        write: ModelRouterAssignmentWrite,
        owner_id: UUID,
        changed_by: str,
    ) -> ModelRouterAssignment:
        router = self.get(router_id, owner_id)
        if router.status != "published":
            raise ModelRouterConflictError("Only a published model router can be assigned")
        if write.assigned:
            self._assert_assignee_models(router.definition, write.user_id)
        if not self._repository.set_model_router_assignment(
            router_id, write.user_id, write.assigned, changed_by
        ):
            raise ModelRouterNotFoundError("Published model router not found")
        project_router_to_table_if_configured(router, self._repository)
        self._publish_assignee_model_access([write.user_id])
        return ModelRouterAssignment(
            router_id=router_id,
            user_id=write.user_id,
            assigned=write.assigned,
        )

    def assign_bulk(
        self,
        router_id: UUID,
        write: ModelRouterBulkAssignmentWrite,
        owner_id: UUID,
        changed_by: str,
        owner_name: str | None = None,
    ) -> ModelRouterBulkAssignmentResult:
        router = self.get(router_id, owner_id)
        if router.status != "published":
            raise ModelRouterConflictError("Only a published model router can be assigned")

        effective: dict[str, bool] = {}
        if write.all_matching_assigned is not None or write.department_assignments:
            _, resolved_users = self._directory_people(changed_by, owner_name)
            if write.all_matching_assigned is not None:
                active_dept = (
                    write.department_id
                    if write.department_id and write.department_id != "all"
                    else None
                )
                normalized_query = (write.query or "").strip().casefold()
                for user_id, user_name, dept_id, dept_name in resolved_users:
                    if active_dept and dept_id != active_dept:
                        continue
                    if normalized_query and not (
                        normalized_query in user_name.casefold()
                        or normalized_query in user_id.casefold()
                        or normalized_query in dept_name.casefold()
                    ):
                        continue
                    effective[user_id] = write.all_matching_assigned

            for dept_write in write.department_assignments:
                dept_query = (dept_write.query or "").strip().casefold()
                for user_id, user_name, dept_id, dept_name in resolved_users:
                    if dept_id != dept_write.department_id:
                        continue
                    if dept_query and not (
                        dept_query in user_name.casefold()
                        or dept_query in user_id.casefold()
                        or dept_query in dept_name.casefold()
                    ):
                        continue
                    effective[user_id] = dept_write.assigned

        for change in write.changes:
            effective[change.user_id] = change.assigned

        if any(effective.values()):
            self._assert_assignee_models(router.definition, changed_by)

        if not effective:
            return ModelRouterBulkAssignmentResult(
                router_id=router_id,
                updated_count=0,
                assigned_count=len(self._repository.list_model_router_assignees(router_id)),
            )

        changed_users = self._repository.set_model_router_assignments_bulk(
            router_id, effective, changed_by
        )
        if changed_users is None:
            raise ModelRouterNotFoundError("Published model router not found")
        project_router_to_table_if_configured(router, self._repository)
        self._publish_assignee_model_access(changed_users)
        return ModelRouterBulkAssignmentResult(
            router_id=router_id,
            updated_count=len(changed_users),
            assigned_count=len(self._repository.list_model_router_assignees(router_id)),
        )

    def _assert_assignee_models(
        self, definition: ModelRouterDefinition, user_id: str
    ) -> None:
        del user_id
        requested = self._candidate_model_ids(definition)
        registry = self._repository.registry()
        providers = {UUID(str(row["id"])): row for row in registry["providers"]}
        runtimes = {UUID(str(row["id"])): row for row in registry["runtimes"]}
        gateways = {UUID(str(row["id"])): row for row in registry["gateways"]}
        registry_models = {
            UUID(str(model["id"])): model for model in registry["models"]
        }
        for model_id in requested:
            model = registry_models.get(model_id)
            if model is None or not model.get("enabled"):
                raise ModelRouterConflictError(
                    "One or more candidate models are disabled or unavailable"
                )
            if "image_generation" in (model.get("capabilities") or []):
                raise ModelRouterConflictError(
                    "One or more candidate models are disabled or unavailable"
                )
            runtime = runtimes.get(UUID(str(model["runtime_id"])))
            provider = providers.get(UUID(str(model["provider_id"])))
            gateway = (
                gateways.get(UUID(str(runtime["gateway_profile_id"])))
                if runtime is not None and runtime.get("gateway_profile_id") is not None
                else None
            )
            if (
                runtime is None
                or not runtime.get("enabled")
                or provider is None
                or not provider.get("enabled")
                or (gateway is not None and not gateway.get("enabled"))
            ):
                raise ModelRouterConflictError(
                    "One or more candidate models are disabled or unavailable"
                )


class ModelRouterExecutionService:
    def __init__(
        self,
        repository: QueryRepository,
        runtime_service: ModelRuntimeService,
    ) -> None:
        self._repository = repository
        self._runtime_service = runtime_service

    def _callable_router_rows(
        self,
        user_id: str,
        *,
        include_published: bool = False,
        allowed_model_ids: set[UUID] | None = None,
    ) -> list[dict[str, Any]]:
        rows_by_id: dict[UUID, dict[str, Any]] = {
            UUID(str(row["id"])): dict(row)
            for row in self._repository.list_assigned_model_routers(user_id)
        }
        if (
            include_published
            and not rows_by_id
            and user_id.strip().lower().endswith("@applications.turnstile.local")
        ):
            for row in self._repository.list_published_model_routers():
                rows_by_id.setdefault(UUID(str(row["id"])), dict(row))
        if allowed_model_ids is None:
            return list(rows_by_id.values())
        filtered: list[dict[str, Any]] = []
        for row in rows_by_id.values():
            router = ModelRouter.model_validate(row)
            cand_ids = ModelRouterService._candidate_model_ids(router.definition)
            if cand_ids and cand_ids <= allowed_model_ids:
                filtered.append(row)
        return filtered

    def _get_callable_router_row(
        self,
        router_id: UUID,
        user_id: str,
        *,
        include_published: bool = False,
        allowed_model_ids: set[UUID] | None = None,
    ) -> dict[str, Any] | None:
        row = self._repository.get_callable_model_router(router_id, user_id)
        if (
            row is None
            and include_published
            and user_id.strip().lower().endswith("@applications.turnstile.local")
        ):
            row = self._repository.get_published_model_router(router_id)
        if row is None:
            return None
        if allowed_model_ids is not None:
            router = ModelRouter.model_validate(row)
            cand_ids = ModelRouterService._candidate_model_ids(router.definition)
            if not cand_ids or not (cand_ids <= allowed_model_ids):
                return None
        return row

    def list_gateway_models(
        self,
        user_id: str,
        *,
        include_published_routers: bool = False,
        allowed_model_ids: set[UUID] | None = None,
    ) -> GatewayModelCatalogResponse:
        registry = self._repository.registry()
        models_by_id = {
            UUID(str(model["id"])): model for model in registry["models"]
        }
        providers_by_id = {
            UUID(str(row["id"])): row for row in registry["providers"]
        }
        runtimes_by_id = {
            UUID(str(row["id"])): row for row in registry["runtimes"]
        }
        callable_models = self._callable_models(registry, user_id)
        router_rows = self._callable_router_rows(
            user_id,
            include_published=include_published_routers,
            allowed_model_ids=allowed_model_ids,
        )
        catalog_items: list[GatewayModelCatalogItem] = []
        for row in router_rows:
            router = ModelRouter.model_validate(row)
            ts = router.published_at or router.updated_at
            created_epoch = int(ts.timestamp()) if ts is not None else 0
            strategy_types = list(
                dict.fromkeys(
                    str(node.strategy_type)
                    for node in router.definition.nodes
                    if node.type == "strategy" and node.strategy_type
                )
            )
            condition_fields = list(
                dict.fromkeys(
                    str(node.condition.field)
                    for node in router.definition.nodes
                    if node.type == "condition" and node.condition is not None
                )
            )
            cand_names: list[str] = []
            for node in router.definition.nodes:
                if node.type == "candidate_models":
                    for cand in node.candidates:
                        m = models_by_id.get(cand.model_id)
                        name = str(
                            (m or {}).get("display_name")
                            or (m or {}).get("model_key")
                            or cand.model_id
                        )
                        if name not in cand_names:
                            cand_names.append(name)
            catalog_items.append(
                GatewayModelCatalogItem(
                    id=router.name,
                    object="model",
                    type="model_router",
                    created=max(0, created_epoch),
                    owned_by="turnstile-model-router",
                    display_name=router.name,
                    router_id=str(router.id),
                    router_version=router.version,
                    description=router.description,
                    capabilities=[
                        "chat",
                        "tools",
                        "intelligent_routing",
                        "session_sticky",
                    ],
                    strategy_types=strategy_types,
                    condition_fields=condition_fields,
                    candidate_models=cand_names,
                )
            )
        for model_id, model in sorted(
            callable_models.items(),
            key=lambda item: str(item[1].get("display_name") or item[1].get("model_key") or ""),
        ):
            capabilities = list(model.get("capabilities") or [])
            if "decision" in capabilities:
                continue
            if allowed_model_ids is not None and model_id not in allowed_model_ids:
                continue
            provider = providers_by_id.get(UUID(str(model["provider_id"])))
            runtime = runtimes_by_id.get(UUID(str(model["runtime_id"])))
            created_at = model.get("created_at")
            created_epoch = (
                int(created_at.timestamp())
                if created_at is not None and hasattr(created_at, "timestamp")
                else 0
            )
            catalog_items.append(
                GatewayModelCatalogItem(
                    id=str(model.get("model_key") or model["display_name"]),
                    object="model",
                    type="model",
                    created=max(0, created_epoch),
                    owned_by=str((provider or {}).get("name") or "turnstile"),
                    display_name=str(model["display_name"]),
                    model_id=str(model_id),
                    runtime=str((runtime or {}).get("name") or ""),
                    capabilities=capabilities,
                )
            )
        return GatewayModelCatalogResponse(object="list", data=catalog_items)

    def resolve_invocation_target(
        self, request: ModelInvocationRequest
    ) -> ModelInvocationRequest:
        if request.router_id is not None:
            return request
        registry = self._repository.registry()
        models = registry["models"]
        model_ids = {UUID(str(model["id"])) for model in models}
        user_id = request.metadata.user_id
        include_published = bool(
            request.router_context.get("subscription_key_authenticated")
        )
        if request.model_id is not None:
            if request.model_id in model_ids:
                return request
            if (
                self._get_callable_router_row(
                    request.model_id,
                    user_id,
                    include_published=include_published,
                )
                is not None
                or self._repository.get_published_model_router(request.model_id)
                is not None
            ):
                return request.model_copy(
                    update={"router_id": request.model_id, "model_id": None}
                )
            return request
        if request.runtime_id is not None:
            return request
        assigned_routers = self._callable_router_rows(
            user_id,
            include_published=include_published,
        )
        published_routers = list(self._repository.list_published_model_routers())
        refs = [
            value.strip()
            for value in (request.metadata.model_id, request.metadata.model)
            if isinstance(value, str) and value.strip()
        ]
        for ref in refs:
            folded = ref.casefold()
            matched_router = next(
                (
                    row
                    for row in assigned_routers
                    if str(row["id"]).casefold() == folded
                    or str(row["name"]).strip().casefold() == folded
                ),
                None,
            )
            if matched_router is None:
                matched_router = next(
                    (
                        row
                        for row in published_routers
                        if str(row["id"]).casefold() == folded
                        or str(row["name"]).strip().casefold() == folded
                    ),
                    None,
                )
            if matched_router is not None:
                return request.model_copy(
                    update={
                        "router_id": UUID(str(matched_router["id"])),
                        "model_id": None,
                    }
                )
            try:
                ref_uuid = UUID(ref)
            except ValueError:
                ref_uuid = None
            if (
                ref_uuid is not None
                and ref_uuid not in model_ids
                and (
                    self._get_callable_router_row(
                        ref_uuid,
                        user_id,
                        include_published=include_published,
                    )
                    is not None
                    or self._repository.get_published_model_router(ref_uuid)
                    is not None
                )
            ):
                return request.model_copy(
                    update={"router_id": ref_uuid, "model_id": None}
                )
        callable_models = self._callable_models(registry, user_id)
        for ref in refs:
            folded = ref.casefold()
            matched_model = next(
                (
                    model
                    for model_id, model in callable_models.items()
                    if str(model_id).casefold() == folded
                    or str(model.get("model_key") or "").strip().casefold() == folded
                    or str(model.get("display_name") or "").strip().casefold() == folded
                ),
                None,
            )
            if matched_model is None:
                matched_model = next(
                    (
                        model
                        for model in models
                        if str(model["id"]).casefold() == folded
                        or str(model.get("model_key") or "").strip().casefold() == folded
                        or str(model.get("display_name") or "").strip().casefold()
                        == folded
                    ),
                    None,
                )
            if matched_model is not None:
                return request.model_copy(
                    update={"model_id": UUID(str(matched_model["id"]))}
                )
        return request

    def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        allowed_model_ids: set[UUID] | None = None,
    ) -> ModelInvocationResponse:
        if request.router_id is None:
            raise HTTPException(status_code=422, detail="router_id is required")
        include_published = bool(
            request.router_context.get("subscription_key_authenticated")
        )
        router_row = self._get_callable_router_row(
            request.router_id,
            request.metadata.user_id,
            include_published=include_published,
        )
        if router_row is None:
            raise HTTPException(
                status_code=403,
                detail="Model router is not published or assigned to this user",
            )
        router = ModelRouter.model_validate(router_row)
        if allowed_model_ids is not None:
            cand_ids = ModelRouterService._candidate_model_ids(router.definition)
            if not cand_ids or not (cand_ids <= allowed_model_ids):
                raise HTTPException(
                    status_code=403,
                    detail="Model is not permitted for this application subscription",
                )
        registry = self._repository.registry()
        models = self._callable_models(
            registry, request.metadata.user_id, router_authorized=True
        )
        selection_key = ":".join(
            (
                str(router.id),
                str(router.version),
                request.metadata.user_id,
                request.metadata.run_id,
            )
        )
        try:
            plan = self._build_execution_plan(
                router.definition,
                self._routing_context(request),
                selection_key=selection_key,
            )
        except ModelRouterConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        candidates = list(plan.candidates)
        fallback_events = plan.fallback_events
        strategy_type = plan.strategy_type
        attempts: list[dict[str, Any]] = []
        preferred_basis: str | None = None
        sticky_bound_by: str | None = None
        sticky_key: str | None = None
        interaction_id = (
            str(
                request.router_context.get("interaction_id")
                or request.router_context.get("x_interaction_id")
                or ""
            ).strip()[:128]
            or None
        )
        session_id = (
            str(
                request.router_context.get("session_id")
                or request.router_context.get("x_session_id")
                or ""
            ).strip()[:128]
            or None
        )
        derived_run_id = self._derived_interaction_run_id(request)
        lookup_chain: list[tuple[str, str, str]] = []
        if interaction_id:
            lookup_chain.append(
                (f"interaction:{interaction_id}", "interaction", interaction_id)
            )
            lookup_chain.append((interaction_id, "interaction", interaction_id))
        if session_id:
            lookup_chain.append((f"session:{session_id}", "session", session_id))
            lookup_chain.append((session_id, "session", session_id))
        lookup_chain.append(
            (request.metadata.run_id, "interaction", request.metadata.run_id)
        )
        if derived_run_id is not None and self._is_agent_continuation(request):
            lookup_chain.append(
                (derived_run_id, "derived_interaction", derived_run_id)
            )
        raw_ttl = request.router_context.get(
            "session_ttl_seconds",
            request.router_context.get("x_session_ttl_seconds"),
        )
        custom_ttl_seconds: int | None = None
        if raw_ttl is not None and not isinstance(raw_ttl, bool):
            try:
                custom_ttl_seconds = min(86400, max(1, int(float(raw_ttl))))
            except (TypeError, ValueError):
                custom_ttl_seconds = None
        session_ttl_seconds = (
            custom_ttl_seconds
            if custom_ttl_seconds is not None
            else _DEFAULT_SESSION_STICKY_TTL_SECONDS
        )
        interaction_ttl_seconds = (
            custom_ttl_seconds
            if custom_ttl_seconds is not None
            else _DEFAULT_INTERACTION_STICKY_TTL_SECONDS
        )
        bound_model_id: UUID | None = None
        ordered_lookup: list[tuple[str, str, str]] = []
        seen_lookup_keys: set[str] = set()
        for raw_key, kind, display_key in lookup_chain:
            if not raw_key or raw_key in seen_lookup_keys:
                continue
            seen_lookup_keys.add(raw_key)
            ordered_lookup.append((raw_key, kind, display_key))
        batch_getter = getattr(
            self._repository, "get_model_router_run_bindings_batch", None
        )
        if callable(batch_getter):
            bindings_map: dict[str, UUID] = {}
            keys_by_ttl: dict[int, list[str]] = {}
            for raw_key, kind, _display_key in ordered_lookup:
                tier_ttl = (
                    session_ttl_seconds
                    if kind == "session"
                    else interaction_ttl_seconds
                )
                keys_by_ttl.setdefault(tier_ttl, []).append(raw_key)
            for tier_ttl, ttl_keys in keys_by_ttl.items():
                bindings_map.update(
                    batch_getter(
                        router.id,
                        router.version,
                        request.metadata.user_id,
                        ttl_keys,
                        ttl_seconds=tier_ttl,
                    )
                )
            for raw_key, kind, display_key in ordered_lookup:
                found = bindings_map.get(raw_key)
                if found is not None:
                    bound_model_id = found
                    sticky_bound_by = kind
                    sticky_key = display_key
                    break
        else:
            for raw_key, kind, display_key in ordered_lookup:
                found = self._repository.get_model_router_run_binding(
                    router.id,
                    router.version,
                    request.metadata.user_id,
                    raw_key,
                )
                if found is not None:
                    bound_model_id = found
                    sticky_bound_by = kind
                    sticky_key = display_key
                    break
        if bound_model_id is not None and bound_model_id not in models:
            disabled = any(
                model["id"] == bound_model_id and model["enabled"] is False
                for model in registry["models"]
            )
            raise HTTPException(status_code=403, detail={
                "code": "model_disabled" if disabled else "model_unavailable",
                "message": "The conversation's bound model is unavailable; its binding is retained",
            })
        all_router_configs = {
            config.model_id: config
            for node in router.definition.nodes
            if node.type in {"strategy", "priority"}
            for config in node.model_configs
        }
        if bound_model_id is not None and (
            any(candidate.model_id == bound_model_id for candidate in candidates)
            or bound_model_id in all_router_configs
        ) and bound_model_id in models:
            bound_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.model_id == bound_model_id
                ),
                all_router_configs.get(bound_model_id)
                or ModelRouterStrategyModel(model_id=bound_model_id),
            )
            eligible_for_escalation = [
                candidate
                for candidate in candidates
                if candidate.model_id in models
                and not self._is_system_one_model(models[candidate.model_id])
                and not self._over_cost_limit(
                    models[candidate.model_id], candidate.max_cost_per_million
                )
            ]
            is_new_turn = self._is_new_user_turn(request)
            bound_in_branch = any(
                candidate.model_id == bound_model_id
                for candidate in eligible_for_escalation
            )
            # A user turn leaves the bound model only for a pricier model in the matched
            # branch; tool continuations always stay on the bound model.
            can_escalate_condition_branch = (
                self._is_user_turn(request)
                and not bound_in_branch
                and bool(eligible_for_escalation)
                and any(
                    self._candidate_effective_cost(models.get(target.model_id))
                    > self._candidate_effective_cost(
                        models.get(bound_candidate.model_id)
                    )
                    for target in eligible_for_escalation
                )
            )
            can_escalate_bound = (
                strategy_type == "model_based"
                and is_new_turn
                and bound_in_branch
                and any(
                    self._can_escalate_candidate(
                        bound_candidate,
                        target,
                        eligible_for_escalation,
                        models,
                    )
                    for target in eligible_for_escalation
                )
            )
            if can_escalate_condition_branch:
                sticky_bound_by = None
                sticky_key = None
                if strategy_type == "model_based":
                    candidates, decision_attempt, selected = (
                        self._model_based_order(
                            request,
                            plan,
                            models,
                            router_id=router.id,
                        )
                    )
                    attempts.append(decision_attempt)
                    preferred_basis = (
                        "model_based_selection"
                        if selected
                        else "model_based_fallback"
                    )
            elif can_escalate_bound:
                decided_candidates, decision_attempt, selected = (
                    self._model_based_order(
                        request,
                        plan,
                        models,
                        bound_candidate=bound_candidate,
                        router_id=router.id,
                    )
                )
                attempts.append(decision_attempt)
                if (
                    selected
                    and decided_candidates
                    and self._can_escalate_candidate(
                        bound_candidate,
                        decided_candidates[0],
                        eligible_for_escalation,
                        models,
                    )
                ):
                    candidates = decided_candidates
                    preferred_basis = "model_based_selection"
                    sticky_bound_by = None
                    sticky_key = None
                else:
                    candidates = [
                        bound_candidate,
                        *(
                            candidate
                            for candidate in decided_candidates
                            if candidate.model_id != bound_model_id
                        ),
                    ]
                    preferred_basis = (
                        "session_sticky"
                        if sticky_bound_by == "session"
                        else "interaction_sticky"
                    )
            else:
                candidates = [
                    bound_candidate,
                    *(
                        candidate
                        for candidate in candidates
                        if candidate.model_id != bound_model_id
                    ),
                ]
                preferred_basis = (
                    "session_sticky"
                    if sticky_bound_by == "session"
                    else "interaction_sticky"
                )
        elif strategy_type == "model_based":
            candidates, decision_attempt, selected = self._model_based_order(
                request,
                plan,
                models,
                router_id=router.id,
            )
            attempts.append(decision_attempt)
            preferred_basis = (
                "model_based_selection" if selected else "model_based_fallback"
            )
        last_error: HTTPException | None = None
        for index, candidate in enumerate(candidates):
            model = models.get(candidate.model_id)
            if model is None or self._is_system_one_model(model):
                attempts.append(
                    self._attempt(
                        len(attempts) + 1,
                        candidate.model_id,
                        "unavailable",
                        priority=candidate.priority,
                        weight=candidate.weight,
                        max_latency_ms=candidate.max_latency_ms,
                        max_cost_per_million=candidate.max_cost_per_million,
                        strategy_type=strategy_type,
                        decision_basis="candidate_unavailable",
                    )
                )
                continue
            if self._over_cost_limit(model, candidate.max_cost_per_million):
                attempts.append(
                    self._attempt(
                        len(attempts) + 1,
                        candidate.model_id,
                        "cost_limit_exceeded",
                        model_name=str(model["display_name"]),
                        priority=candidate.priority,
                        weight=candidate.weight,
                        max_latency_ms=candidate.max_latency_ms,
                        max_cost_per_million=candidate.max_cost_per_million,
                        strategy_type=strategy_type,
                        decision_basis="cost_limit_exceeded",
                    )
                )
                continue
            attempt_request_id = str(uuid4())
            concrete = request.model_copy(
                update={
                    "router_id": None,
                    "router_context": {
                        **request.router_context,
                        "_router_authorized_id": str(router.id),
                    },
                    "runtime_id": UUID(str(model["runtime_id"])),
                    "model_id": candidate.model_id,
                    "metadata": request.metadata.model_copy(
                        update={
                            "model_id": str(candidate.model_id),
                            "model": str(model["model_key"]),
                            "runtime": str(model["runtime_name"]),
                        }
                    ),
                }
            )
            try:
                effective_timeout_ms = candidate.max_latency_ms
                if plan.attempt_timeout_ms is not None:
                    effective_timeout_ms = (
                        min(effective_timeout_ms, plan.attempt_timeout_ms)
                        if effective_timeout_ms is not None
                        else plan.attempt_timeout_ms
                    )
                if effective_timeout_ms is None:
                    response = self._runtime_service.invoke(
                        concrete, request_id=attempt_request_id
                    )
                else:
                    response = self._runtime_service.invoke(
                        concrete,
                        request_id=attempt_request_id,
                        timeout_ms=effective_timeout_ms,
                    )
            except HTTPException as error:
                event = self._failure_event(error.status_code)
                actual_request_id = (
                    error.headers.get("x-request-id", attempt_request_id)
                    if error.headers
                    else attempt_request_id
                )
                attempt = self._attempt(
                    len(attempts) + 1,
                    candidate.model_id,
                    "failed",
                    model_name=str(model["display_name"]),
                    request_id=actual_request_id,
                    status_code=error.status_code,
                    reason=str(error.detail),
                    priority=candidate.priority,
                    weight=candidate.weight,
                    max_latency_ms=candidate.max_latency_ms,
                    max_cost_per_million=candidate.max_cost_per_million,
                    strategy_type=strategy_type,
                    fallback_trigger=event,
                    decision_basis="provider_error",
                )
                attempts.append(attempt)
                self._attach_condition_trace(attempts, plan)
                self._save_attempt_trace(
                    actual_request_id, router, candidate.model_id, model, attempts
                )
                last_error = error
                if bound_model_id is not None and error.status_code in {403, 404, 409}:
                    raise
                if event in fallback_events and index < len(candidates) - 1:
                    continue
                raise
            has_prior_candidate = any(
                attempt.get("attempt_role") == "candidate"
                for attempt in attempts
            )
            attempts.append(
                self._attempt(
                    len(attempts) + 1,
                    candidate.model_id,
                    "succeeded",
                    model_name=str(model["display_name"]),
                    request_id=response.request_id,
                    status_code=200,
                    latency_ms=response.latency_ms,
                    priority=candidate.priority,
                    weight=candidate.weight,
                    max_latency_ms=candidate.max_latency_ms,
                    max_cost_per_million=candidate.max_cost_per_million,
                    strategy_type=strategy_type,
                    decision_basis=(
                        "fallback_success"
                        if has_prior_candidate
                        else preferred_basis
                        if preferred_basis is not None
                        else "weighted_selection_success"
                        if strategy_type == "weighted"
                        else "highest_priority_success"
                    ),
                    sticky_bound_by=None if has_prior_candidate else sticky_bound_by,
                    sticky_key=None if has_prior_candidate else sticky_key,
                    **self._response_usage(response, model),
                )
            )
            self._attach_condition_trace(attempts, plan)
            self._save_attempt_trace(
                response.request_id, router, candidate.model_id, model, attempts
            )
            binding_keys = [request.metadata.run_id]
            if interaction_id:
                binding_keys.extend((f"interaction:{interaction_id}", interaction_id))
            if session_id:
                binding_keys.extend((f"session:{session_id}", session_id))
            if derived_run_id is not None:
                binding_keys.append(derived_run_id)
            unique_binding_keys = list(dict.fromkeys(k for k in binding_keys if k))
            batch_saver = getattr(
                self._repository, "save_model_router_run_bindings_batch", None
            )
            if callable(batch_saver):
                batch_saver(
                    router.id,
                    router.version,
                    request.metadata.user_id,
                    unique_binding_keys,
                    candidate.model_id,
                    str(model["display_name"]),
                )
            else:
                for b_key in unique_binding_keys:
                    self._repository.save_model_router_run_binding(
                        router.id,
                        router.version,
                        request.metadata.user_id,
                        b_key,
                        candidate.model_id,
                        str(model["display_name"]),
                    )
            return response.model_copy(
                update={
                    "router_id": router.id,
                    "router_name": router.name,
                    "router_version": router.version,
                    "selected_model_id": candidate.model_id,
                    "selected_model_name": str(model["display_name"]),
                    "router_attempts": attempts,
                }
            )
        if last_error is not None:
            raise last_error
        self._runtime_service.deny(
            request,
            {"display_name": router.name, "model_id": "unattributed",
             "runtime_name": "unattributed"},
            "router_no_callable_candidates",
            str(uuid4()),
            model_admission="router_no_callable_candidates",
            status_code=409,
            detail="Model router has no callable candidates",
        )

    @classmethod
    def decision_preview(
        cls,
        write: ModelRouterDecisionPreviewWrite,
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> ModelRouterDecisionPreview:
        strategy = next(
            (
                node
                for node in write.definition.nodes
                if node.id == write.strategy_node_id and node.type == "strategy"
            ),
            None,
        )
        if strategy is None or strategy.strategy_type != "model_based":
            raise ModelRouterConflictError(
                "Decision preview requires a Model-based Strategy node"
            )
        if strategy.routing_model_id is None:
            raise ModelRouterConflictError("The Strategy has no Routing Model")
        routing_model = models.get(strategy.routing_model_id)
        if routing_model is None:
            raise ModelRouterConflictError("The Routing Model is unavailable")
        nodes = {node.id: node for node in write.definition.nodes}
        candidate_ids = {
            candidate.model_id
            for edge in write.definition.edges
            if edge.target == strategy.id
            for candidate_node in [nodes.get(edge.source)]
            if candidate_node is not None
            and candidate_node.type == "candidate_models"
            for candidate in candidate_node.candidates
        }
        candidates = sorted(
            (
                config
                for config in strategy.model_configs
                if config.model_id in candidate_ids
                and config.model_id in models
                and not cls._is_system_one_model(models[config.model_id])
            ),
            key=lambda config: (config.priority, str(config.model_id)),
        )
        if not candidates:
            raise ModelRouterConflictError(
                "The Strategy has no callable candidate models"
            )
        catalog = cls._decision_catalog(candidates, models)
        protocol: ModelRouterDecisionProtocol = (
            "system_one"
            if cls._is_system_one_model(routing_model)
            else "chat_json"
        )
        if protocol == "system_one":
            _, system, is_default, has_placeholder = cls._render_system_one_question(
                strategy.decision_prompt,
                catalog,
            )
        else:
            system, is_default, has_placeholder = cls._render_decision_prompt(
                strategy.decision_prompt,
                catalog,
            )
        user, truncated = cls._truncate_decision_prompt(
            write.sample_prompt,
            strategy.max_prompt_chars,
        )
        return ModelRouterDecisionPreview(
            system=system,
            user=user,
            sample_truncated=truncated,
            candidates=[
                ModelRouterDecisionCandidatePreview.model_validate(item)
                for item in catalog
            ],
            routing_model_id=strategy.routing_model_id,
            routing_model_name=str(routing_model["display_name"]),
            decision_protocol=protocol,
            decision_reasoning_effort=strategy.decision_reasoning_effort,
            is_default_prompt=is_default,
            has_placeholder=has_placeholder,
            models_without_description=[
                candidate.model_id
                for candidate in candidates
                if not candidate.description.strip()
            ],
            chars=len(system),
        )

    def trial_decision(
        self,
        write: ModelRouterDecisionTrialWrite,
        user_email: str,
        *,
        available_model_ids: set[UUID],
    ) -> ModelRouterDecisionTrialResult:
        models = {
            UUID(str(model["id"])): model
            for model in self._repository.registry()["models"]
            if UUID(str(model["id"])) in available_model_ids
        }
        strategy = next(
            (
                node
                for node in write.definition.nodes
                if node.id == write.strategy_node_id and node.type == "strategy"
            ),
            None,
        )
        if strategy is None or strategy.strategy_type != "model_based":
            raise ModelRouterConflictError(
                "Decision trial requires a Model-based Strategy node"
            )
        if strategy.routing_model_id is None:
            raise ModelRouterConflictError("The Strategy has no Routing Model")
        routing_model = models.get(strategy.routing_model_id)
        if routing_model is None:
            raise ModelRouterConflictError("The Routing Model is unavailable")
        nodes = {node.id: node for node in write.definition.nodes}
        candidate_ids = {
            candidate.model_id
            for edge in write.definition.edges
            if edge.target == strategy.id
            for candidate_node in [nodes.get(edge.source)]
            if candidate_node is not None
            and candidate_node.type == "candidate_models"
            for candidate in candidate_node.candidates
        }
        eligible = sorted(
            (
                config
                for config in strategy.model_configs
                if config.model_id in candidate_ids
                and config.model_id in models
                and not self._is_system_one_model(models[config.model_id])
                and not self._over_cost_limit(
                    models[config.model_id], config.max_cost_per_million
                )
            ),
            key=lambda config: (config.priority, str(config.model_id)),
        )
        if not eligible:
            raise ModelRouterConflictError(
                "The Strategy has no callable candidate models"
            )
        prompt = self._extract_user_prompt(
            [ChatMessage(role="user", content=write.sample_prompt)]
        )
        decision_input, prompt_truncated = self._truncate_decision_prompt(
            prompt,
            strategy.max_prompt_chars,
        )
        catalog = self._decision_catalog(eligible, models)
        protocol: ModelRouterDecisionProtocol = (
            "system_one"
            if self._is_system_one_model(routing_model)
            else "chat_json"
        )
        trial_request_id = str(uuid4())
        metadata = InvocationMetadata(
            organization_id="unattributed",
            organization="unattributed",
            department_id="unattributed",
            department="unattributed",
            project_id="unattributed",
            project="unattributed",
            agent_id="model-router-editor",
            agent="Model Router Editor",
            user_id=user_email,
            user=user_email.split("@")[0] or user_email,
            workflow="model-router-decision-trial",
            model_id=str(strategy.routing_model_id),
            model=str(routing_model["model_key"]),
            runtime=str(routing_model["runtime_name"]),
            request_source="model-router-trial",
            run_id=f"trial-{trial_request_id}",
            turn_index=1,
        )
        fallback_used = False
        probabilities_map: dict[str, float] = {}
        confidence: float | None = None
        rationale: str | None = None
        if protocol == "system_one":
            question, _, _, _ = self._render_system_one_question(
                strategy.decision_prompt,
                catalog,
            )
            decision_req = DecisionInvocationRequest(
                metadata=metadata,
                model_id=strategy.routing_model_id,
                runtime_id=UUID(str(routing_model["runtime_id"])),
                state=decision_input,
                questions={"route": question},
            )
            decision_resp = self._invoke_decision_service(
                decision_req,
                request_id=trial_request_id,
                timeout_ms=_DECISION_TIMEOUT_MS,
                editor_trial_authorized=True,
            )
            usage_info = self._response_usage(decision_resp)
            latency_ms = decision_resp.latency_ms
            try:
                (
                    selected_model_id,
                    rationale,
                    probabilities_map,
                    confidence,
                ) = self._parse_system_one_decision(
                    decision_resp.answers,
                    eligible,
                    models,
                )
            except (TypeError, ValueError) as error:
                selected_model_id = eligible[0].model_id
                fallback_used = True
                rationale = f"Fallback to P1 ({error})"
        else:
            system_prompt, _, _ = self._render_decision_prompt(
                strategy.decision_prompt,
                catalog,
            )
            max_tokens = (
                _DECISION_REASONING_MAX_OUTPUT_TOKENS
                if strategy.decision_reasoning_effort not in {None, "none"}
                else _DECISION_MAX_OUTPUT_TOKENS
            )
            chat_req = ModelInvocationRequest(
                metadata=metadata,
                runtime_id=UUID(str(routing_model["runtime_id"])),
                model_id=strategy.routing_model_id,
                messages=[
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=decision_input),
                ],
                max_output_tokens=max_tokens,
                reasoning_effort=strategy.decision_reasoning_effort,
                response_format="json_object",
                stream=False,
            )
            chat_resp = self._runtime_service.invoke(
                chat_req,
                request_id=trial_request_id,
                timeout_ms=_DECISION_TIMEOUT_MS,
            )
            usage_info = self._response_usage(chat_resp)
            latency_ms = chat_resp.latency_ms
            try:
                (
                    selected_model_id,
                    rationale,
                    probabilities_map,
                    confidence,
                ) = self._parse_model_decision_details(
                    chat_resp.content,
                    eligible,
                    models,
                )
            except (TypeError, ValueError) as error:
                selected_model_id = eligible[0].model_id
                fallback_used = True
                rationale = f"Fallback to P1 ({error})"
        probability_items = [
            ModelRouterDecisionProbability(
                model_id=candidate.model_id,
                display_name=str(models[candidate.model_id]["display_name"]),
                probability=probabilities_map[str(candidate.model_id)],
            )
            for candidate in eligible
            if str(candidate.model_id) in probabilities_map
        ]
        probability_items.sort(key=lambda item: (-item.probability, str(item.model_id)))
        return ModelRouterDecisionTrialResult(
            routing_model_id=strategy.routing_model_id,
            routing_model_name=str(routing_model["display_name"]),
            decision_protocol=protocol,
            selected_model_id=selected_model_id,
            selected_model_name=str(models[selected_model_id]["display_name"]),
            fallback_used=fallback_used,
            rationale=rationale or None,
            confidence=confidence,
            probabilities=probability_items,
            latency_ms=latency_ms,
            input_tokens=usage_info["input_tokens"],
            output_tokens=usage_info["output_tokens"],
            total_tokens=usage_info["total_tokens"],
            estimated_cost=usage_info["estimated_cost"],
            sample_truncated=prompt_truncated,
            decision_input_chars=len(decision_input),
        )

    @staticmethod
    def _resolved_model_costs(
        model: Mapping[str, Any] | None,
        *,
        input_tokens: int = 0,
    ) -> tuple[float | None, float | None]:
        if not model:
            return None, None
        raw_tiers = model.get("context_price_tiers") or []
        if raw_tiers:
            price = ModelPrice.from_registry(model)
            if price is not None and price.context_tiers:
                resolved = price.for_input_tokens(max(0, input_tokens))
                return (
                    float(resolved.input_price_per_million),
                    float(resolved.output_price_per_million),
                )
        raw_in = model.get("input_cost_per_million")
        raw_out = model.get("output_cost_per_million")
        return (
            float(raw_in) if raw_in is not None else None,
            float(raw_out) if raw_out is not None else None,
        )

    @classmethod
    def _decision_catalog(
        cls,
        candidates: Sequence[ModelRouterStrategyModel],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            model = models[candidate.model_id]
            in_cost, out_cost = cls._resolved_model_costs(model)
            items.append(
                {
                    "model_id": candidate.model_id,
                    "display_name": str(model["display_name"]),
                    "provider_name": str(model["provider_name"]),
                    "description": candidate.description.strip(),
                    "capabilities": list(model.get("capabilities") or []),
                    "context_window": model.get("context_window"),
                    "input_cost_per_million": in_cost,
                    "output_cost_per_million": out_cost,
                    "priority": candidate.priority,
                    "weight": candidate.weight,
                    "max_latency_ms": candidate.max_latency_ms,
                    "max_cost_per_million": candidate.max_cost_per_million,
                }
            )
        return items

    @staticmethod
    def _is_system_one_model(model: Mapping[str, Any] | None) -> bool:
        return bool(model and "decision" in (model.get("capabilities") or []))

    @staticmethod
    def _prompt_catalog(
        catalog: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "model_id": item["model_id"],
                "display_name": item["display_name"],
                "provider_name": item["provider_name"],
                "description": item["description"],
                "input_cost_per_million": item["input_cost_per_million"],
                "output_cost_per_million": item["output_cost_per_million"],
            }
            for item in catalog
        ]

    @classmethod
    def _render_decision_prompt(
        cls,
        template: str | None,
        catalog: Sequence[Mapping[str, Any]],
    ) -> tuple[str, bool, bool]:
        custom = (template or "").strip()
        effective = custom or _DEFAULT_DECISION_PROMPT
        catalog_text = json.dumps(
            cls._prompt_catalog(catalog),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        has_placeholder = _CATALOG_PLACEHOLDER in effective
        system = (
            effective.replace(_CATALOG_PLACEHOLDER, catalog_text)
            if has_placeholder
            else f"{effective}\n\nCandidate catalog:\n{catalog_text}"
        )
        if "json" not in effective.casefold():
            # json_object responses are rejected unless the messages mention JSON.
            system = f"{system}\n\n{_DECISION_JSON_SHAPE}"
        return system, not custom, has_placeholder

    @staticmethod
    def _render_system_one_question(
        template: str | None,
        catalog: Sequence[Mapping[str, Any]],
    ) -> tuple[ChoiceQuestion, str, bool, bool]:
        custom = (template or "").strip()
        has_placeholder = _CATALOG_PLACEHOLDER in custom
        if has_placeholder:
            custom = custom.replace(_CATALOG_PLACEHOLDER, "").strip()
        instructions = custom or _DEFAULT_SYSTEM_ONE_INSTRUCTIONS
        criteria: dict[str, str] = {}
        for item in catalog:
            model_id = str(item["model_id"])
            display_name = str(item.get("display_name") or model_id)
            provider_name = str(item.get("provider_name") or "")
            description = str(item.get("description") or "").strip()
            cost_parts: list[str] = []
            if item.get("input_cost_per_million") is not None:
                cost_parts.append(f"in ${item['input_cost_per_million']}/1M")
            if item.get("output_cost_per_million") is not None:
                cost_parts.append(f"out ${item['output_cost_per_million']}/1M")
            meta = f"{display_name} ({provider_name}"
            if cost_parts:
                meta += f", {', '.join(cost_parts)}"
            meta += ")"
            criteria[model_id] = f"{meta} — {description}" if description else meta
        if len(criteria) == 1:
            criteria["__none__"] = (
                "Placeholder option; never select this option when a candidate model is present."
            )
        question = ChoiceQuestion(
            type="choice",
            instructions=instructions,
            criteria=criteria,
        )
        rendered = json.dumps(
            {"questions": {"route": question.model_dump(mode="json")}},
            ensure_ascii=False,
            indent=2,
        )
        return question, rendered, not custom, has_placeholder

    def _invoke_decision_service(
        self,
        request: DecisionInvocationRequest,
        *,
        request_id: str,
        timeout_ms: int,
        authorized_router_id: UUID | None = None,
        editor_trial_authorized: bool = False,
    ) -> DecisionInvocationResponse:
        invoke_fn = self._runtime_service.invoke_decision
        try:
            return invoke_fn(
                request,
                request_id=request_id,
                timeout_ms=timeout_ms,
                authorized_router_id=authorized_router_id,
                editor_trial_authorized=editor_trial_authorized,
            )
        except TypeError:
            try:
                return invoke_fn(
                    request,
                    request_id=request_id,
                    timeout_ms=timeout_ms,
                )
            except TypeError:
                return invoke_fn(request, request_id=request_id)

    def _model_based_order(
        self,
        request: ModelInvocationRequest,
        plan: _RouterExecutionPlan,
        models: Mapping[UUID, Mapping[str, Any]],
        *,
        bound_candidate: ModelRouterStrategyModel | None = None,
        router_id: UUID | None = None,
    ) -> tuple[list[ModelRouterStrategyModel], dict[str, Any], bool]:
        raw_eligible = [
            candidate
            for candidate in plan.candidates
            if candidate.model_id in models
            and not self._is_system_one_model(models[candidate.model_id])
            and not self._over_cost_limit(
                models[candidate.model_id], candidate.max_cost_per_million
            )
        ]
        if bound_candidate is not None and bound_candidate.model_id in models:
            filtered = [
                candidate
                for candidate in raw_eligible
                if candidate.model_id == bound_candidate.model_id
                or self._can_escalate_candidate(
                    bound_candidate,
                    candidate,
                    raw_eligible,
                    models,
                )
            ]
            if not any(
                candidate.model_id == bound_candidate.model_id
                for candidate in filtered
            ):
                filtered.insert(0, bound_candidate)
            eligible = filtered if len(filtered) > 1 else raw_eligible
        else:
            eligible = raw_eligible
        candidate_ids = [str(candidate.model_id) for candidate in eligible]
        routing_model_id = plan.routing_model_id
        routing_model = models.get(routing_model_id) if routing_model_id else None
        bound_model_name = (
            str(models[bound_candidate.model_id]["display_name"])
            if bound_candidate is not None and bound_candidate.model_id in models
            else None
        )
        prompt = self._extract_decision_prompt(
            request.messages,
            bound_model_name=bound_model_name,
            request=request,
        )
        decision_input, prompt_truncated = self._truncate_decision_prompt(
            prompt,
            plan.max_prompt_chars,
        )
        catalog = self._decision_catalog(eligible, models)
        protocol: ModelRouterDecisionProtocol = (
            "system_one"
            if self._is_system_one_model(routing_model)
            else "chat_json"
        )
        question: ChoiceQuestion | None = None
        if protocol == "system_one" and eligible:
            question, system_prompt, _, _ = self._render_system_one_question(
                plan.decision_prompt,
                catalog,
            )
        else:
            system_prompt, _, _ = self._render_decision_prompt(
                plan.decision_prompt,
                catalog,
            )
        decision_evidence: dict[str, Any] = {
            "decision_protocol": protocol,
            "decision_system": system_prompt,
            "decision_system_sha256": hashlib.sha256(
                system_prompt.encode()
            ).hexdigest(),
            "decision_input_chars": len(decision_input),
            "decision_input_sha256": hashlib.sha256(
                decision_input.encode()
            ).hexdigest(),
        }
        if routing_model_id is None or routing_model is None or not eligible:
            reason = (
                "Routing model is unavailable"
                if routing_model is None
                else "No callable candidates are available"
            )
            return list(plan.candidates), self._attempt(
                1,
                routing_model_id or UUID(int=0),
                "failed",
                model_name=(
                    str(routing_model["display_name"])
                    if routing_model is not None
                    else "Routing Model"
                ),
                reason=reason,
                strategy_type=plan.strategy_type,
                decision_basis="model_based_fallback",
                attempt_role="routing_decision",
                decision_candidates=candidate_ids,
                prompt_truncated=prompt_truncated,
                **decision_evidence,
            ), False

        decision_request_id = str(uuid4())
        response: ModelInvocationResponse | DecisionInvocationResponse | None = None
        response_text = ""
        probabilities: dict[str, float] = {}
        confidence: float | None = None
        try:
            if protocol == "system_one":
                assert question is not None
                decision_req = DecisionInvocationRequest(
                    metadata=request.metadata.model_copy(
                        update={
                            "model_id": str(routing_model_id),
                            "model": str(routing_model["model_key"]),
                            "runtime": str(routing_model["runtime_name"]),
                            "workflow": "model-router-decision",
                        }
                    ),
                    model_id=routing_model_id,
                    runtime_id=UUID(str(routing_model["runtime_id"])),
                    state=decision_input,
                    questions={"route": question},
                )
                decision_resp = self._invoke_decision_service(
                    decision_req,
                    request_id=decision_request_id,
                    timeout_ms=plan.decision_timeout_ms,
                    authorized_router_id=router_id,
                )
                response = decision_resp
                response_text = json.dumps(
                    decision_resp.answers,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                (
                    selected_model_id,
                    _rationale,
                    probabilities,
                    confidence,
                ) = self._parse_system_one_decision(
                    decision_resp.answers,
                    eligible,
                    models,
                )
            else:
                max_tokens = (
                    _DECISION_REASONING_MAX_OUTPUT_TOKENS
                    if plan.decision_reasoning_effort not in {None, "none"}
                    else _DECISION_MAX_OUTPUT_TOKENS
                )
                decision_request = request.model_copy(
                    update={
                        "router_id": None,
                        "runtime_id": UUID(str(routing_model["runtime_id"])),
                        "model_id": routing_model_id,
                        "router_context": (
                            {**request.router_context, "_router_authorized_id": str(router_id)}
                            if router_id is not None
                            else dict(request.router_context)
                        ),
                        "messages": [
                            ChatMessage(role="system", content=system_prompt),
                            ChatMessage(role="user", content=decision_input),
                        ],
                        "tools": None,
                        "temperature": None,
                        "max_output_tokens": max_tokens,
                        "reasoning_effort": plan.decision_reasoning_effort,
                        "response_format": "json_object",
                        "stream": False,
                        "metadata": request.metadata.model_copy(
                            update={
                                "model_id": str(routing_model_id),
                                "model": str(routing_model["model_key"]),
                                "runtime": str(routing_model["runtime_name"]),
                                "workflow": "model-router-decision",
                            }
                        ),
                    }
                )
                chat_resp = self._runtime_service.invoke(
                    decision_request,
                    request_id=decision_request_id,
                    timeout_ms=plan.decision_timeout_ms,
                )
                response = chat_resp
                response_text = chat_resp.content
                (
                    selected_model_id,
                    _rationale,
                    probabilities,
                    confidence,
                ) = self._parse_model_decision_details(
                    chat_resp.content,
                    eligible,
                    models,
                )
        except HTTPException as error:
            actual_request_id = (
                error.headers.get("x-request-id", decision_request_id)
                if error.headers
                else decision_request_id
            )
            attempt = self._attempt(
                1,
                routing_model_id,
                "failed",
                model_name=str(routing_model["display_name"]),
                request_id=actual_request_id,
                status_code=error.status_code,
                reason="Routing model request failed",
                strategy_type=plan.strategy_type,
                fallback_trigger=self._failure_event(error.status_code),
                decision_basis="model_based_fallback",
                attempt_role="routing_decision",
                decision_candidates=candidate_ids,
                prompt_truncated=prompt_truncated,
                **decision_evidence,
            )
            return list(plan.candidates), attempt, False
        except (TypeError, ValueError) as error:
            response_details: dict[str, Any] = {}
            if response is not None:
                response_details = {
                    "latency_ms": response.latency_ms,
                    "decision_response_chars": len(response_text),
                    "decision_response_sha256": hashlib.sha256(
                        response_text.encode()
                    ).hexdigest(),
                    **self._response_usage(response, routing_model),
                }
            attempt = self._attempt(
                1,
                routing_model_id,
                "failed",
                model_name=str(routing_model["display_name"]),
                request_id=decision_request_id,
                status_code=200,
                reason=str(error),
                strategy_type=plan.strategy_type,
                fallback_trigger="error",
                decision_basis="model_based_fallback",
                attempt_role="routing_decision",
                decision_candidates=candidate_ids,
                prompt_truncated=prompt_truncated,
                **decision_evidence,
                **response_details,
            )
            return list(plan.candidates), attempt, False

        selected = next(
            candidate
            for candidate in eligible
            if candidate.model_id == selected_model_id
        )
        ordered = [
            selected,
            *[
                candidate
                for candidate in plan.candidates
                if candidate.model_id != selected_model_id
            ],
        ]
        attempt = self._attempt(
            1,
            routing_model_id,
            "succeeded",
            model_name=str(routing_model["display_name"]),
            request_id=response.request_id,
            status_code=200,
            latency_ms=response.latency_ms,
            reason="Routing model selected an authorized candidate",
            strategy_type=plan.strategy_type,
            decision_basis="model_based_selection",
            attempt_role="routing_decision",
            decision_candidates=candidate_ids,
            selected_candidate_model_id=selected_model_id,
            prompt_truncated=prompt_truncated,
            decision_response_chars=len(response_text),
            decision_response_sha256=hashlib.sha256(
                response_text.encode()
            ).hexdigest(),
            decision_confidence=confidence,
            decision_probabilities=probabilities or None,
            **decision_evidence,
            **self._response_usage(response, routing_model),
        )
        return ordered, attempt, True

    def _callable_models(
        self,
        registry: Mapping[str, Sequence[Mapping[str, Any]]],
        user_id: str,
        *,
        router_authorized: bool = False,
    ) -> dict[UUID, Mapping[str, Any]]:
        providers = {UUID(str(row["id"])): row for row in registry["providers"]}
        runtimes = {UUID(str(row["id"])): row for row in registry["runtimes"]}
        gateways = {UUID(str(row["id"])): row for row in registry["gateways"]}
        policy = (
            None
            if router_authorized
            else next(
                (
                    row
                    for row in self._repository.list_user_model_policies([user_id])
                    if str(row["user_id"]) == user_id
                ),
                None,
            )
        )
        assigned = (
            {UUID(str(value)) for value in policy.get("model_ids", [])}
            if policy is not None
            else None
        )
        callable_models: dict[UUID, Mapping[str, Any]] = {}
        for model in registry["models"]:
            model_id = UUID(str(model["id"]))
            capabilities = model.get("capabilities") or []
            if "image_generation" in capabilities:
                continue
            runtime = runtimes.get(UUID(str(model["runtime_id"])))
            provider = providers.get(UUID(str(model["provider_id"])))
            gateway = None
            if runtime is not None and runtime.get("gateway_profile_id") is not None:
                gateway = gateways.get(UUID(str(runtime["gateway_profile_id"])))
            if not model.get("enabled") or runtime is None or not runtime.get("enabled"):
                continue
            if provider is None or not provider.get("enabled"):
                continue
            if gateway is not None and not gateway.get("enabled"):
                continue
            if not router_authorized and "decision" not in capabilities:
                if assigned is not None and model_id not in assigned:
                    continue
                if assigned is None and model.get("assignment_required"):
                    continue
            callable_models[model_id] = model
        return callable_models

    @staticmethod
    def _clean_user_message_content(raw_content: str) -> str:
        matches = _USER_REQUEST_RE.findall(raw_content)
        extracted = matches[-1] if matches else raw_content
        extracted = _CURRENT_DATETIME_RE.sub("", extracted).strip()

        def _bound_quoted_block(match: re.Match[str]) -> str:
            inner = match.group(2).strip()
            if len(inner) <= 600:
                return match.group(0)
            bounded = f"{inner[:300].rstrip()} ... {inner[-150:].lstrip()}"
            return f"{match.group(1)}{bounded}{match.group(3)}"

        return _QUOTED_USER_MESSAGE_RE.sub(_bound_quoted_block, extracted).strip()

    @classmethod
    def _extract_user_prompt(cls, messages: Sequence[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role != "user":
                continue
            return cls._clean_user_message_content(message.content or "")
        return ""

    @classmethod
    def _extract_system_instruction(cls, messages: Sequence[ChatMessage]) -> str:
        for message in messages:
            if (
                message.role or ""
            ).strip().casefold() == "user" and _USER_REQUEST_RE.search(
                message.content or ""
            ):
                return ""
        parts: list[str] = []
        for message in messages:
            if (message.role or "").strip().casefold() not in {
                "system",
                "developer",
            }:
                continue
            raw = message.content or ""
            max_prefix_end = -1
            for marker in _SDK_SYSTEM_PREFIX_END_MARKERS:
                idx = raw.rfind(marker)
                if idx >= 0:
                    max_prefix_end = max(max_prefix_end, idx + len(marker))
            if max_prefix_end >= 0:
                raw = raw[max_prefix_end:]
            min_suffix_start = len(raw)
            for marker in _SDK_SYSTEM_SUFFIX_START_MARKERS:
                idx = raw.find(marker)
                if 0 <= idx < min_suffix_start:
                    min_suffix_start = idx
            raw = raw[:min_suffix_start]
            raw = _CONTEXT_BLOCK_RE.sub(" ", raw)
            raw = _CURRENT_DATETIME_RE.sub(" ", raw)
            cleaned = " ".join(raw.split())
            if cleaned:
                parts.append(cleaned)
        if not parts:
            return ""
        merged = " ".join(parts)
        if len(merged) <= 600:
            return merged
        return f"{merged[:420].rstrip()} ... {merged[-140:].lstrip()}"

    @staticmethod
    def _extract_request_constraints(
        request: ModelInvocationRequest | None,
    ) -> str:
        if request is None:
            return ""
        items: list[str] = []
        if (
            request.max_output_tokens is not None
            and 0 < request.max_output_tokens <= 128
        ):
            items.append(f"max_output_tokens={request.max_output_tokens}")
        if request.response_format:
            items.append(f"response_format={request.response_format}")
        if request.tools:
            items.append(f"tools={len(request.tools)}")
        return ", ".join(items)

    @classmethod
    def _extract_decision_prompt(
        cls,
        messages: Sequence[ChatMessage],
        *,
        bound_model_name: str | None = None,
        request: ModelInvocationRequest | None = None,
    ) -> str:
        system_instruction = cls._extract_system_instruction(messages)
        constraints = cls._extract_request_constraints(request)
        first_user = ""
        prev_user = ""
        last_user = ""
        prev_assistant = ""
        pending_assistant = ""
        user_count = 0
        for message in messages:
            role = (message.role or "").strip().casefold()
            if role == "assistant":
                asst_text = (message.content or "").strip()
                if asst_text:
                    pending_assistant = asst_text
            elif role == "user":
                extracted = cls._clean_user_message_content(message.content or "")
                if not extracted:
                    continue
                user_count += 1
                if user_count == 1:
                    first_user = extracted
                else:
                    prev_user = last_user
                    prev_assistant = pending_assistant
                last_user = extracted
        base_prompt = last_user or cls._extract_user_prompt(messages)
        has_multi_turn = (
            user_count > 1 and bool(prev_user) and prev_user != last_user
        )
        if (
            not system_instruction
            and not constraints
            and not bound_model_name
            and not has_multi_turn
        ):
            return base_prompt
        sections: list[str] = []
        if system_instruction:
            sections.append(f"Task / System instruction:\n{system_instruction}")
        if constraints:
            sections.append(f"Request constraints: {constraints}")
        sections.append(f"Current user request:\n{base_prompt}")
        if bound_model_name:
            sections.append(
                f"Active conversation model: {bound_model_name} "
                "(keep this model to preserve conversation prompt cache unless the "
                "current user request clearly requires a higher-capability candidate "
                "based on the candidate descriptions and unit costs)"
            )
        if has_multi_turn:
            prior_lines = [
                "Prior turn context (for reference only if the current request is a follow-up):"
            ]
            if user_count > 2 and first_user and first_user != prev_user:
                first_clean = " ".join(first_user.split())
                first_snippet = (
                    f"{first_clean[:200]}..."
                    if len(first_clean) > 200
                    else first_clean
                )
                prior_lines.append(f"- Initial topic: {first_snippet}")
            prev_user_clean = " ".join(prev_user.split())
            prev_user_snippet = (
                f"{prev_user_clean[:240]}..."
                if len(prev_user_clean) > 240
                else prev_user_clean
            )
            prior_lines.append(f"- Previous user message: {prev_user_snippet}")
            if prev_assistant:
                prev_asst_clean = " ".join(prev_assistant.split())
                prev_asst_snippet = (
                    f"{prev_asst_clean[:240]}..."
                    if len(prev_asst_clean) > 240
                    else prev_asst_clean
                )
                prior_lines.append(
                    f"- Previous assistant reply: {prev_asst_snippet}"
                )
            sections.append("\n".join(prior_lines))
        return "\n\n".join(sections)

    @classmethod
    def _extract_first_user_prompt(cls, messages: Sequence[ChatMessage]) -> str:
        for message in messages:
            if message.role != "user":
                continue
            cleaned = cls._clean_user_message_content(message.content or "")
            if cleaned:
                return cleaned
        return ""

    @classmethod
    def _derived_interaction_run_id(
        cls, request: ModelInvocationRequest
    ) -> str | None:
        first_prompt = cls._extract_first_user_prompt(request.messages).strip()
        if not first_prompt:
            return None
        sys_inst = cls._extract_system_instruction(request.messages)
        seed = f"{sys_inst}\n{first_prompt}" if sys_inst else first_prompt
        digest = hashlib.sha256(
            f"{request.metadata.user_id}:{seed}".encode()
        ).hexdigest()[:16]
        return f"derived-{digest}"

    @staticmethod
    def _is_agent_continuation(request: ModelInvocationRequest) -> bool:
        if any(message.role in {"assistant", "tool"} for message in request.messages):
            return True
        if request.metadata.turn_index > 1:
            return True
        initiator = str(
            request.router_context.get("x_initiator")
            or request.router_context.get("initiator")
            or ""
        ).strip().casefold()
        if initiator == "agent":
            return True
        user_count = sum(
            1
            for message in request.messages
            if (message.role or "").strip().casefold() == "user"
            and (message.content or "").strip()
        )
        return user_count > 1

    @classmethod
    def _is_user_turn(cls, request: ModelInvocationRequest) -> bool:
        initiator = str(
            request.router_context.get("x_initiator")
            or request.router_context.get("initiator")
            or ""
        ).strip().casefold()
        non_system = [
            message
            for message in request.messages
            if (message.role or "").strip().casefold() not in {"system", "developer"}
        ]
        return (
            initiator != "agent"
            and bool(non_system)
            and (non_system[-1].role or "").strip().casefold() == "user"
        )

    @classmethod
    def _is_new_user_turn(cls, request: ModelInvocationRequest) -> bool:
        initiator = str(
            request.router_context.get("x_initiator")
            or request.router_context.get("initiator")
            or ""
        ).strip().casefold()
        if initiator == "agent":
            return False
        non_system = [
            message
            for message in request.messages
            if (message.role or "").strip().casefold() not in {"system", "developer"}
        ]
        if not non_system or (non_system[-1].role or "").strip().casefold() != "user":
            return False
        user_messages: list[str] = []
        for message in non_system:
            if (message.role or "").strip().casefold() != "user":
                continue
            extracted = cls._clean_user_message_content(message.content or "")
            if extracted:
                user_messages.append(extracted)
        return len(user_messages) > 1 and user_messages[-1] != user_messages[-2]

    @classmethod
    def _candidate_effective_cost(cls, model: Mapping[str, Any] | None) -> float:
        in_cost, out_cost = cls._resolved_model_costs(model)
        return round(float(in_cost or 0.0) + float(out_cost or 0.0), 8)

    @classmethod
    def _can_escalate_candidate(
        cls,
        bound: ModelRouterStrategyModel,
        target: ModelRouterStrategyModel,
        eligible: Sequence[ModelRouterStrategyModel],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> bool:
        if target.model_id == bound.model_id or not eligible:
            return False
        bound_cost = cls._candidate_effective_cost(models.get(bound.model_id))
        target_cost = cls._candidate_effective_cost(models.get(target.model_id))
        if target_cost > bound_cost:
            return True
        costs = [
            cls._candidate_effective_cost(models.get(candidate.model_id))
            for candidate in eligible
        ]
        return max(costs) == min(costs) and any(
            candidate.description.strip() for candidate in eligible
        )

    @classmethod
    def _routing_context(
        cls, request: ModelInvocationRequest
    ) -> dict[str, str | float | bool]:
        prompt = cls._extract_user_prompt(request.messages)
        context = dict(request.router_context)
        context["user_id"] = request.metadata.user_id
        context["request_parameter"] = prompt
        context["prompt_length"] = len(prompt)
        context["required_capability"] = "tools" if request.tools else "chat"
        return context

    @staticmethod
    def _truncate_decision_prompt(
        prompt: str,
        max_chars: int = _DECISION_PROMPT_MAX_CHARS,
    ) -> tuple[str, bool]:
        if len(prompt) <= max_chars:
            return prompt, False
        marker = "\n...[middle omitted]...\n"
        remaining = max_chars - len(marker)
        head = remaining // 2
        tail = remaining - head
        return f"{prompt[:head]}{marker}{prompt[-tail:]}", True

    @staticmethod
    def _resolve_candidate_id(
        selected_value: Any,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> UUID | None:
        candidate_ids = {candidate.model_id for candidate in candidates}
        try:
            candidate_uuid = UUID(str(selected_value))
            if candidate_uuid in candidate_ids:
                return candidate_uuid
        except (TypeError, ValueError, AttributeError):
            pass
        alias = str(selected_value or "").strip().casefold()
        if not alias:
            return None
        matches = [
            model_id
            for model_id in candidate_ids
            if alias
            in {
                str(models[model_id].get("display_name") or "").casefold(),
                str(models[model_id].get("model_key") or "").casefold(),
            }
        ]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _extract_candidate_probabilities(
        cls,
        raw_probabilities: Any,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> dict[str, float]:
        if not isinstance(raw_probabilities, Mapping):
            return {}
        probabilities: dict[str, float] = {}
        for key, value in raw_probabilities.items():
            if str(key) == "__none__":
                continue
            resolved = cls._resolve_candidate_id(key, candidates, models)
            if resolved is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric < 0:
                continue
            probabilities[str(resolved)] = round(min(1.0, numeric), 4)
        return probabilities

    @classmethod
    def _parse_model_decision_details(
        cls,
        content: str,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[UUID, str, dict[str, float], float | None]:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Routing model response must be a JSON object")
        selected_value = payload.get("model_id", payload.get("model"))
        selected_model_id = cls._resolve_candidate_id(
            selected_value,
            candidates,
            models,
        )
        if selected_model_id is None:
            raise ValueError("Routing model returned a model outside the candidate catalog")
        rationale = str(payload.get("rationale") or "").strip()[:500]
        probabilities = cls._extract_candidate_probabilities(
            payload.get("probabilities"),
            candidates,
            models,
        )
        confidence: float | None = None
        raw_confidence = payload.get("confidence")
        if raw_confidence is not None:
            try:
                confidence = round(min(1.0, max(0.0, float(raw_confidence))), 4)
            except (TypeError, ValueError):
                confidence = None
        if confidence is None and str(selected_model_id) in probabilities:
            confidence = probabilities[str(selected_model_id)]
        return selected_model_id, rationale, probabilities, confidence

    @classmethod
    def _parse_model_decision(
        cls,
        content: str,
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[UUID, str]:
        selected_model_id, rationale, _, _ = cls._parse_model_decision_details(
            content,
            candidates,
            models,
        )
        return selected_model_id, rationale

    @classmethod
    def _parse_system_one_decision(
        cls,
        answers: Mapping[str, Any],
        candidates: Sequence[Any],
        models: Mapping[UUID, Mapping[str, Any]],
    ) -> tuple[UUID, str, dict[str, float], float | None]:
        if not isinstance(answers, Mapping) or not answers:
            raise ValueError("System One decision response contained no answers")
        route_answer = answers.get("route")
        if route_answer is None and len(answers) == 1:
            route_answer = next(iter(answers.values()))
        if route_answer is None:
            raise ValueError("System One decision response missed the route question")
        probabilities: dict[str, float] = {}
        confidence: float | None = None
        rationale = ""
        selected_value: Any = None
        if isinstance(route_answer, str):
            selected_value = route_answer
        elif isinstance(route_answer, Mapping):
            for key in (
                "choice",
                "value",
                "result",
                "answer",
                "selected",
                "model_id",
                "model",
            ):
                if route_answer.get(key) is not None:
                    selected_value = route_answer.get(key)
                    break
            raw_probs = (
                route_answer.get("probabilities")
                or route_answer.get("distribution")
                or route_answer.get("scores")
                or route_answer
            )
            probabilities = cls._extract_candidate_probabilities(
                raw_probs,
                candidates,
                models,
            )
            raw_conf = route_answer.get("confidence", route_answer.get("probability"))
            if raw_conf is not None:
                try:
                    confidence = round(min(1.0, max(0.0, float(raw_conf))), 4)
                except (TypeError, ValueError):
                    confidence = None
            rationale = str(
                route_answer.get("rationale") or route_answer.get("reason") or ""
            ).strip()[:500]
        else:
            raise ValueError("System One route answer must be an object or string")
        selected_model_id = cls._resolve_candidate_id(
            selected_value,
            candidates,
            models,
        )
        if selected_model_id is None and probabilities:
            best_id_str = max(
                probabilities.items(),
                key=lambda item: item[1],
            )[0]
            selected_model_id = cls._resolve_candidate_id(
                best_id_str,
                candidates,
                models,
            )
        if selected_model_id is None:
            raise ValueError("Routing model returned a model outside the candidate catalog")
        if confidence is None and str(selected_model_id) in probabilities:
            confidence = probabilities[str(selected_model_id)]
        if not rationale and confidence is not None:
            rationale = f"System One choice confidence {confidence * 100:.1f}%"
        return selected_model_id, rationale, probabilities, confidence

    @staticmethod
    def _attach_condition_trace(
        attempts: list[dict[str, Any]], plan: _RouterExecutionPlan
    ) -> None:
        if attempts and plan.condition_evaluations:
            attempts[0].setdefault(
                "condition_evaluations",
                list(plan.condition_evaluations),
            )

    def _save_attempt_trace(
        self,
        request_id: str,
        router: ModelRouter,
        model_id: UUID,
        model: Mapping[str, Any],
        attempts: list[dict[str, Any]],
    ) -> None:
        self._repository.save_model_router_invocation(
            request_id,
            router.id,
            router.name,
            router.version,
            model_id,
            str(model["display_name"]),
            attempts,
        )

    @staticmethod
    def _attempt(
        sequence: int,
        model_id: UUID,
        outcome: str,
        *,
        model_name: str | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
        latency_ms: int | None = None,
        reason: str | None = None,
        priority: int | None = None,
        weight: int | None = None,
        max_latency_ms: int | None = None,
        max_cost_per_million: float | None = None,
        strategy_type: str,
        fallback_trigger: str | None = None,
        decision_basis: str | None = None,
        input_tokens: int | None = None,
        cached_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        estimated_cost: float | None = None,
        input_price_per_million: float | None = None,
        cached_price_per_million: float | None = None,
        cache_write_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        price_context_tier: str | None = None,
        attempt_role: str = "candidate",
        decision_protocol: str | None = None,
        decision_candidates: Sequence[str] | None = None,
        selected_candidate_model_id: UUID | None = None,
        prompt_truncated: bool | None = None,
        decision_system: str | None = None,
        decision_system_sha256: str | None = None,
        decision_input_chars: int | None = None,
        decision_input_sha256: str | None = None,
        decision_response_chars: int | None = None,
        decision_response_sha256: str | None = None,
        decision_confidence: float | None = None,
        decision_probabilities: Mapping[str, float] | None = None,
        sticky_bound_by: str | None = None,
        sticky_key: str | None = None,
    ) -> dict[str, Any]:
        return {
            "sequence": sequence,
            "attempt_role": attempt_role,
            "model_id": str(model_id),
            "model_name": model_name or str(model_id),
            "request_id": request_id,
            "outcome": outcome,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "reason": reason,
            "priority": priority,
            "weight": weight,
            "max_latency_ms": max_latency_ms,
            "max_cost_per_million": max_cost_per_million,
            "strategy_type": strategy_type,
            "fallback_trigger": fallback_trigger,
            "decision_basis": decision_basis,
            "sticky_bound_by": sticky_bound_by,
            "sticky_key": sticky_key,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost": estimated_cost,
            "input_price_per_million": input_price_per_million,
            "cached_price_per_million": cached_price_per_million,
            "cache_write_price_per_million": cache_write_price_per_million,
            "output_price_per_million": output_price_per_million,
            "price_context_tier": price_context_tier,
            "decision_protocol": decision_protocol,
            "decision_candidates": list(decision_candidates or []),
            "selected_candidate_model_id": (
                str(selected_candidate_model_id)
                if selected_candidate_model_id is not None
                else None
            ),
            "prompt_truncated": prompt_truncated,
            "decision_system": decision_system,
            "decision_system_sha256": decision_system_sha256,
            "decision_input_chars": decision_input_chars,
            "decision_input_sha256": decision_input_sha256,
            "decision_response_chars": decision_response_chars,
            "decision_response_sha256": decision_response_sha256,
            "decision_confidence": decision_confidence,
            "decision_probabilities": (
                dict(decision_probabilities)
                if decision_probabilities is not None
                else None
            ),
        }

    @staticmethod
    def _response_usage(
        response: ModelInvocationResponse | DecisionInvocationResponse,
        model: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        usage = response.usage
        if usage is None:
            return {
                "input_tokens": None,
                "cached_tokens": None,
                "cache_write_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "estimated_cost": response.estimated_cost,
            }
        price_info: dict[str, Any] = {}
        if model is not None:
            price = ModelPrice.from_registry(model)
            if price is not None and not (price.context_tiers and usage.estimated):
                snap = price.snapshot_prices(
                    usage.input_tokens,
                    usage.cached_tokens,
                    usage.output_tokens,
                    usage.cache_write_tokens,
                )
                price_info = {
                    "input_price_per_million": snap["input_price_per_million"],
                    "cached_price_per_million": snap["cached_price_per_million"],
                    "cache_write_price_per_million": snap["cache_write_price_per_million"],
                    "output_price_per_million": snap["output_price_per_million"],
                    "price_context_tier": snap["price_context_tier"],
                }
        return {
            "input_tokens": usage.input_tokens,
            "cached_tokens": usage.cached_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.cached_tokens + usage.output_tokens,
            "estimated_cost": (
                response.estimated_cost
                if response.estimated_cost is not None
                else snap["estimated_cost"]
                if price_info
                else None
            ),
            **price_info,
        }

    @classmethod
    def _execution_plan(
        cls,
        definition: ModelRouterDefinition,
        context: Mapping[str, str | float | bool],
        *,
        selection_key: str = "model-router-validation",
    ) -> tuple[list[ModelRouterStrategyModel], set[str], str]:
        plan = cls._build_execution_plan(
            definition,
            context,
            selection_key=selection_key,
        )
        return plan.candidates, plan.fallback_events, plan.strategy_type

    @classmethod
    def _build_execution_plan(
        cls,
        definition: ModelRouterDefinition,
        context: Mapping[str, str | float | bool],
        *,
        selection_key: str,
    ) -> _RouterExecutionPlan:
        nodes = {node.id: node for node in definition.nodes}
        outgoing: dict[str, list[Any]] = {}
        for edge in definition.edges:
            outgoing.setdefault(edge.source, []).append(edge)
        starts = [node for node in definition.nodes if node.type == "start"]
        pending = [starts[0].id] if starts else []
        visited: set[str] = set()
        candidates: list[Any] = []
        candidate_node_ids: set[str] = set()
        reached_strategy_ids: set[str] = set()
        condition_evaluations: list[dict[str, Any]] = []
        while pending:
            node_id = pending.pop(0)
            if node_id in visited or node_id not in nodes:
                continue
            visited.add(node_id)
            node = nodes[node_id]
            if node.type == "candidate_models":
                candidate_node_ids.add(node.id)
                candidates.extend(node.candidates)
            if node.type in {"strategy", "priority"}:
                reached_strategy_ids.add(node.id)
            edges = outgoing.get(node_id, [])
            if (
                node.type == "condition"
                and node.condition is not None
                and len(edges) > 1
            ):
                matched = cls._condition_matches(node.condition, context)
                raw_actual = context.get(node.condition.field)
                if isinstance(raw_actual, str) and len(raw_actual) > 120:
                    actual_preview: str | float | bool | None = f"{raw_actual[:117]}..."
                elif isinstance(raw_actual, (str, int, float, bool)) or raw_actual is None:
                    actual_preview = raw_actual
                else:
                    actual_preview = str(raw_actual)[:120]
                condition_evaluations.append(
                    {
                        "node_id": node.id,
                        "node_label": node.label,
                        "field": node.condition.field,
                        "operator": node.condition.operator,
                        "expected": node.condition.value,
                        "actual": actual_preview,
                        "matched": matched,
                        "branch": "Match" if matched else "Else",
                    }
                )
                edges = [
                    edge
                    for edge in edges
                    if cls._condition_edge_matches(edge.label, matched)
                ]
            pending.extend(edge.target for edge in edges)
        unique_model_ids: list[UUID] = []
        for candidate in candidates:
            if candidate.model_id not in unique_model_ids:
                unique_model_ids.append(candidate.model_id)
        fallback_node_ids: set[str] = set()
        pending_policy_nodes = list(reached_strategy_ids or candidate_node_ids)
        visited_policy_nodes: set[str] = set()
        while pending_policy_nodes:
            source_id = pending_policy_nodes.pop()
            if source_id in visited_policy_nodes:
                continue
            visited_policy_nodes.add(source_id)
            for edge in outgoing.get(source_id, []):
                target = nodes.get(edge.target)
                if target is None:
                    continue
                if target.type == "fallback":
                    fallback_node_ids.add(target.id)
                elif target.type in {"strategy", "priority"}:
                    pending_policy_nodes.append(target.id)
        fallback_events: set[str] = {
            str(event)
            for node_id in fallback_node_ids
            for event in nodes[node_id].fallback_on
            if event in {"error", "timeout", "rate_limit"}
        }
        strategy_type = "priority"
        strategy = None
        if len(reached_strategy_ids) != 1:
            raise ModelRouterConflictError(
                "The selected Router path must reach exactly one Strategy"
            )
        strategy = nodes[next(iter(reached_strategy_ids))]
        strategy_type = strategy.strategy_type or "priority"
        configs_by_model = {
            config.model_id: config
            for config in (strategy.model_configs if strategy is not None else [])
        }
        available = [
            configs_by_model[model_id]
            for model_id in unique_model_ids
            if model_id in configs_by_model
        ]
        ordered = (
            cls._weighted_order(available, selection_key)
            if strategy_type == "weighted"
            else sorted(
                available,
                key=lambda item: (item.priority, -item.weight, str(item.model_id)),
            )
        )
        return _RouterExecutionPlan(
            candidates=ordered,
            fallback_events=fallback_events,
            strategy_type=strategy_type,
            routing_model_id=(strategy.routing_model_id if strategy is not None else None),
            attempt_timeout_ms=strategy.timeout_ms,
            decision_timeout_ms=_DECISION_TIMEOUT_MS,
            decision_prompt=(strategy.decision_prompt if strategy is not None else None),
            max_prompt_chars=(
                strategy.max_prompt_chars
                if strategy is not None
                else _DECISION_PROMPT_MAX_CHARS
            ),
            condition_evaluations=condition_evaluations,
            decision_reasoning_effort=(
                strategy.decision_reasoning_effort if strategy is not None else None
            ),
        )

    @staticmethod
    def _weighted_order(
        candidates: list[ModelRouterStrategyModel], selection_key: str
    ) -> list[ModelRouterStrategyModel]:
        remaining = sorted(candidates, key=lambda item: str(item.model_id))
        ordered: list[ModelRouterStrategyModel] = []
        round_index = 0
        while remaining:
            total_weight = sum(candidate.weight for candidate in remaining)
            digest = hashlib.sha256(
                f"{selection_key}:{round_index}".encode()
            ).digest()
            draw = int.from_bytes(digest[:8], "big") % total_weight
            cumulative = 0
            selected_index = 0
            for index, candidate in enumerate(remaining):
                cumulative += candidate.weight
                if draw < cumulative:
                    selected_index = index
                    break
            ordered.append(remaining.pop(selected_index))
            round_index += 1
        return ordered

    @staticmethod
    def _condition_matches(condition: Any, context: Mapping[str, Any]) -> bool:
        actual = context.get(condition.field)
        expected = condition.value
        if condition.operator == "exists":
            return actual is not None
        if condition.operator == "equals":
            return bool(actual == expected)
        if condition.operator == "not_equals":
            return bool(actual != expected)
        if condition.operator == "contains":
            return expected is not None and str(expected).casefold() in str(
                actual or ""
            ).casefold()
        if condition.operator == "matches_any":
            if expected is None:
                return False
            haystack = str(actual or "").casefold()
            raw_items = (
                expected
                if isinstance(expected, (list, tuple, set))
                else re.split(r"[,;\n|，；]+", str(expected))
            )
            keywords = [
                str(item).strip().casefold()
                for item in raw_items
                if str(item).strip()
            ]
            return bool(keywords) and any(
                keyword in haystack for keyword in keywords
            )
        try:
            if condition.operator == "greater_than":
                return float(str(actual)) > float(str(expected))
            if condition.operator == "greater_than_or_equal":
                return float(str(actual)) >= float(str(expected))
            if condition.operator == "less_than":
                return float(str(actual)) < float(str(expected))
            if condition.operator == "less_than_or_equal":
                return float(str(actual)) <= float(str(expected))
        except (TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _condition_edge_matches(label: str, matched: bool) -> bool:
        normalized = label.strip().casefold()
        if not normalized:
            return True
        true_labels = {"match", "matched", "true", "yes", "匹配"}
        false_labels = {"else", "default", "false", "no", "未匹配"}
        if normalized in true_labels:
            return matched
        if normalized in false_labels:
            return not matched
        return matched

    @staticmethod
    def _failure_event(status_code: int) -> str:
        if status_code == 429:
            return "rate_limit"
        if status_code in {408, 504}:
            return "timeout"
        return "error"

    @classmethod
    def _over_cost_limit(cls, model: Mapping[str, Any], maximum: float | None) -> bool:
        if maximum is None:
            return False
        in_cost, out_cost = cls._resolved_model_costs(model)
        prices = [
            float(value)
            for value in (in_cost, out_cost)
            if value is not None
        ]
        return bool(prices) and max(prices) > maximum


class ModelRouterService(_ModelRouterLifecycleService):

    @staticmethod
    def validate_definition(
        definition: ModelRouterDefinition,
        *,
        available_model_ids: set[UUID] | None = None,
    ) -> ModelRouterValidationResult:
        issues: list[ModelRouterValidationIssue] = []
        node_counts = Counter(node.id for node in definition.nodes)
        edge_counts = Counter(edge.id for edge in definition.edges)
        node_ids = set(node_counts)
        duplicate_nodes = sorted(node_id for node_id, count in node_counts.items() if count > 1)
        duplicate_edges = sorted(edge_id for edge_id, count in edge_counts.items() if count > 1)
        for node_id in duplicate_nodes:
            issues.append(ModelRouterValidationIssue(
                code="duplicate_node_id",
                message="节点 ID 必须唯一。",
                node_id=node_id,
            ))
        for edge_id in duplicate_edges:
            issues.append(ModelRouterValidationIssue(
                code="duplicate_edge_id",
                message=f"连线 ID {edge_id} 重复。",
            ))

        starts = [node for node in definition.nodes if node.type == "start"]
        outputs = [node for node in definition.nodes if node.type == "output"]
        if len(starts) != 1:
            issues.append(ModelRouterValidationIssue(
                code="start_count",
                message="路由必须且只能包含一个 Start 节点。",
            ))
        if len(outputs) != 1:
            issues.append(ModelRouterValidationIssue(
                code="output_count",
                message="路由必须且只能包含一个 Output 节点。",
            ))

        selected_model_ids: set[UUID] = set()
        for node in definition.nodes:
            if node.type == "condition" and node.condition is None:
                issues.append(ModelRouterValidationIssue(
                    code="condition_missing",
                    message="Condition 节点缺少判断条件。",
                    node_id=node.id,
                ))
            if node.type == "candidate_models":
                if not node.candidates:
                    issues.append(ModelRouterValidationIssue(
                        code="candidate_empty",
                        message="Candidate Models 节点至少需要一个模型。",
                        node_id=node.id,
                    ))
                selected_model_ids.update(candidate.model_id for candidate in node.candidates)
                candidate_counts = Counter(candidate.model_id for candidate in node.candidates)
                if any(count > 1 for count in candidate_counts.values()):
                    issues.append(ModelRouterValidationIssue(
                        code="candidate_duplicate",
                        message="同一候选池不能重复添加模型。",
                        node_id=node.id,
                    ))
            if node.type == "strategy":
                if node.strategy_type is None:
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_type_missing",
                        message="Strategy 节点必须选择一种策略。",
                        node_id=node.id,
                    ))
                elif (
                    node.strategy_type == "model_based"
                    and node.routing_model_id is None
                ):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_routing_model_missing",
                        message="模型判路由策略必须指定 Routing Model。",
                        node_id=node.id,
                    ))
                elif node.strategy_type == "model_based":
                    assert node.routing_model_id is not None
                    selected_model_ids.add(node.routing_model_id)
                config_counts = Counter(
                    config.model_id for config in node.model_configs
                )
                if any(count > 1 for count in config_counts.values()):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_model_duplicate",
                        message="Strategy 不能重复配置同一个候选模型。",
                        node_id=node.id,
                    ))
                priorities = [
                    config.priority for config in node.model_configs
                ]
                if node.strategy_type == "priority" and len(priorities) != len(
                    set(priorities)
                ):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_priority_duplicate",
                        message="Priority 策略中每个候选模型必须有唯一优先级。",
                        node_id=node.id,
                    ))
            if node.type == "fallback" and not node.fallback_on:
                issues.append(ModelRouterValidationIssue(
                    code="fallback_event_missing",
                    message="Fallback 节点至少需要一个触发条件。",
                    node_id=node.id,
                ))
            if node.type == "fallback" and "quality" in node.fallback_on:
                issues.append(ModelRouterValidationIssue(
                    code="fallback_quality_unsupported",
                    message="质量不足尚无在线评估信号，不能作为 Fallback 条件。",
                    node_id=node.id,
                ))

        if available_model_ids is not None:
            unavailable = selected_model_ids - available_model_ids
            if unavailable:
                unavailable_node_id = next(
                    (
                        node.id
                        for node in definition.nodes
                        if any(c.model_id in unavailable for c in node.candidates)
                        or (node.routing_model_id in unavailable)
                    ),
                    None,
                )
                issues.append(ModelRouterValidationIssue(
                    code="candidate_unavailable",
                    message="候选池包含已停用或当前用户无权访问的模型。",
                    node_id=unavailable_node_id,
                ))

        outgoing: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        incoming: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        for edge in definition.edges:
            if edge.source == edge.target:
                issues.append(ModelRouterValidationIssue(
                    code="self_edge",
                    message="节点不能连接到自身。",
                    node_id=edge.source,
                ))
            if edge.source not in node_ids or edge.target not in node_ids:
                issues.append(ModelRouterValidationIssue(
                    code="edge_endpoint_missing",
                    message="连线引用了不存在的节点。",
                ))
                continue
            outgoing[edge.source].add(edge.target)
            incoming[edge.target].add(edge.source)

        nodes_by_id = {node.id: node for node in definition.nodes}
        for condition_node in (
            node for node in definition.nodes if node.type == "condition"
        ):
            condition_edges = [
                edge
                for edge in definition.edges
                if edge.source == condition_node.id
            ]
            true_labels = {"match", "matched", "true", "yes", "匹配"}
            false_labels = {"else", "default", "false", "no", "未匹配"}
            match_edges = [
                edge
                for edge in condition_edges
                if edge.label.strip().casefold() in true_labels
            ]
            else_edges = [
                edge
                for edge in condition_edges
                if edge.label.strip().casefold() in false_labels
            ]
            if len(match_edges) != 1 or len(else_edges) != 1:
                issues.append(ModelRouterValidationIssue(
                    code="condition_branches_incomplete",
                    message="Condition 必须且只能连接一条 Match 和一条 Else 分支。",
                    node_id=condition_node.id,
                ))
            if len(condition_edges) != 2 or len(match_edges) + len(else_edges) != 2:
                issues.append(ModelRouterValidationIssue(
                    code="condition_branches_ambiguous",
                    message="Condition 不能包含未命名或额外分支。",
                    node_id=condition_node.id,
                ))
            if (
                len(match_edges) == 1
                and len(else_edges) == 1
                and match_edges[0].target == else_edges[0].target
            ):
                issues.append(ModelRouterValidationIssue(
                    code="condition_branches_same_target",
                    message="Condition 的 Match 和 Else 必须指向不同分支。",
                    node_id=condition_node.id,
                ))
        for candidate_node in (
            node for node in definition.nodes if node.type == "candidate_models"
        ):
            strategy_targets = {
                target_id
                for target_id in outgoing[candidate_node.id]
                if nodes_by_id[target_id].type in {"strategy", "priority"}
            }
            if not strategy_targets:
                issues.append(ModelRouterValidationIssue(
                    code="candidate_strategy_missing",
                    message="Candidate Models 必须连接到 Strategy 节点。",
                    node_id=candidate_node.id,
                ))
            elif outgoing[candidate_node.id] - strategy_targets:
                issues.append(ModelRouterValidationIssue(
                    code="candidate_strategy_bypass",
                    message="Candidate Models 不能绕过 Strategy 连接其他节点。",
                    node_id=candidate_node.id,
                ))
            for strategy_id in strategy_targets:
                strategy_model_ids = {
                    config.model_id
                    for config in nodes_by_id[strategy_id].model_configs
                }
                candidate_model_ids = {
                    candidate.model_id for candidate in candidate_node.candidates
                }
                if strategy_model_ids != candidate_model_ids:
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_models_mismatch",
                        message="Strategy 必须配置候选池中的每一个模型，且不能包含池外模型。",
                        node_id=strategy_id,
                    ))

        has_cycle = ModelRouterService._has_cycle(node_ids, outgoing)
        if len(starts) == 1:
            if incoming[starts[0].id]:
                issues.append(ModelRouterValidationIssue(
                    code="start_has_input",
                    message="Start 节点不能有入线。",
                    node_id=starts[0].id,
                ))
            if len(outgoing[starts[0].id]) > 1:
                issues.append(ModelRouterValidationIssue(
                    code="start_branches_ambiguous",
                    message="Start 节点只能连出一条线，分支请用 Condition 节点。",
                    node_id=starts[0].id,
                ))
            reachable = ModelRouterService._reachable(starts[0].id, outgoing)
            disconnected = node_ids - reachable
            for node_id in sorted(disconnected):
                issues.append(ModelRouterValidationIssue(
                    code="node_disconnected",
                    message="节点未连接到路由入口。",
                    node_id=node_id,
                ))
            if len(outputs) == 1 and outputs[0].id not in reachable:
                issues.append(ModelRouterValidationIssue(
                    code="output_unreachable",
                    message="Start 节点无法到达 Output 节点。",
                    node_id=outputs[0].id,
                ))
            elif len(outputs) == 1:
                can_reach_output = ModelRouterService._reachable(outputs[0].id, incoming)
                terminal_policy_ids = {
                    node.id for node in definition.nodes if node.type == "fallback"
                }
                for node_id in sorted(reachable - can_reach_output - terminal_policy_ids):
                    issues.append(ModelRouterValidationIssue(
                        code="dead_end",
                        message="该节点无法到达 Output 节点。",
                        node_id=node_id,
                    ))
            if not has_cycle:
                outcomes = ModelRouterService._strategy_outcomes(
                    starts[0].id,
                    nodes_by_id,
                    outgoing,
                )
                if any(len(outcome) != 1 for outcome in outcomes):
                    issues.append(ModelRouterValidationIssue(
                        code="strategy_path_ambiguous",
                        message="每条可执行路径必须且只能到达一个 Strategy。",
                    ))
        if len(outputs) == 1 and outgoing[outputs[0].id]:
            issues.append(ModelRouterValidationIssue(
                code="output_has_exit",
                message="Output 节点不能有出线。",
                node_id=outputs[0].id,
            ))
        if has_cycle:
            issues.append(ModelRouterValidationIssue(
                code="cycle",
                message="路由流程存在循环依赖。",
            ))
        return ModelRouterValidationResult(valid=not issues, issues=issues)

    @staticmethod
    def _strategy_outcomes(
        start_id: str,
        nodes: Mapping[str, ModelRouterNode],
        outgoing: Mapping[str, set[str]],
    ) -> set[tuple[str, ...]]:
        memo: dict[str, set[tuple[str, ...]]] = {}

        def merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
            if len(left) > 1 or len(right) > 1:
                return ("", "")
            if not left:
                return right
            if not right or left == right:
                return left
            return ("", "")

        def visit(node_id: str) -> set[tuple[str, ...]]:
            if node_id in memo:
                return memo[node_id]
            node = nodes[node_id]
            own = (node.id,) if node.type in {"strategy", "priority"} else ()
            child_outcomes = [
                visit(target_id)
                for target_id in outgoing.get(node_id, set())
                if target_id in nodes
            ]
            if node.type == "condition":
                outcomes = {
                    merge(own, outcome)
                    for alternatives in child_outcomes
                    for outcome in alternatives
                }
                if not child_outcomes:
                    outcomes.add(own)
            else:
                outcomes = {own}
                for alternatives in child_outcomes:
                    outcomes = {
                        merge(current, outcome)
                        for current in outcomes
                        for outcome in alternatives
                    }
            memo[node_id] = outcomes
            return outcomes

        return visit(start_id)

    @staticmethod
    def _reachable(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            pending.extend(adjacency.get(node_id, ()))
        return seen

    @staticmethod
    def _has_cycle(node_ids: Iterable[str], adjacency: Mapping[str, set[str]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            visiting.add(node_id)
            if any(visit(target) for target in adjacency.get(node_id, ())):
                return True
            visiting.remove(node_id)
            visited.add(node_id)
            return False

        return any(visit(node_id) for node_id in node_ids if node_id not in visited)

    def _assert_models_allowed(
        self, definition: ModelRouterDefinition, user_email: str, role: str
    ) -> None:
        requested = self._candidate_model_ids(definition)
        if requested - self._available_model_ids(user_email, role):
            raise ModelRouterConflictError(
                "One or more candidate models are disabled or not assigned to this user"
            )

    def _available_model_ids(self, user_email: str, role: str) -> set[UUID]:
        registry = self._repository.registry()
        providers = {row["id"]: row for row in registry["providers"]}
        runtimes = {row["id"]: row for row in registry["runtimes"]}
        gateways = {row["id"]: row for row in registry["gateways"]}
        policy = next(
            (
                row
                for row in self._repository.list_user_model_policies([user_email])
                if str(row["user_id"]) == user_email
            ),
            None,
        )
        assigned = {UUID(str(value)) for value in policy.get("model_ids", [])} if policy else None
        available: set[UUID] = set()
        for model in registry["models"]:
            if "image_generation" in (model.get("capabilities") or []):
                continue
            runtime = runtimes.get(model["runtime_id"])
            provider = providers.get(model["provider_id"])
            gateway = gateways.get(runtime.get("gateway_profile_id")) if runtime else None
            if not model.get("enabled") or not runtime or not runtime.get("enabled"):
                continue
            if not provider or not provider.get("enabled"):
                continue
            if gateway is not None and not gateway.get("enabled"):
                continue
            if role not in (model.get("allowed_roles") or []):
                continue
            if role not in (runtime.get("allowed_roles") or []):
                continue
            model_id = UUID(str(model["id"]))
            if "decision" not in (model.get("capabilities") or []):
                if assigned is not None and model_id not in assigned:
                    continue
                if assigned is None and model.get("assignment_required"):
                    continue
            available.add(model_id)
        return available

    @staticmethod
    def _candidate_model_ids(definition: ModelRouterDefinition) -> set[UUID]:
        candidate_ids = {
            candidate.model_id
            for node in definition.nodes
            for candidate in node.candidates
        }
        return candidate_ids | {
            node.routing_model_id
            for node in definition.nodes
            if node.routing_model_id is not None
        }

    @staticmethod
    def _values(write: ModelRouterWrite, changed_by: str) -> Mapping[str, Any]:
        name = write.name.strip()
        if not name:
            raise ModelRouterConflictError("Model router name cannot be blank")
        return {
            "name": name,
            "description": write.description.strip(),
            "definition": write.definition.model_dump(mode="json"),
            "updated_by": changed_by,
        }
