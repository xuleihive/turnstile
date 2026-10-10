from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from ..domain.data_access import DataAccessScope


def _activation_runtime_config(binding: Mapping[str, Any]) -> dict[str, Any]:
    config = {
        **(binding.get("runtime_config") or {}),
        "path": (
            "/chat/completions"
            if binding["api_format"] == "openai_chat"
            else "/v1/messages"
        ),
        "api_format": binding["api_format"],
        "streaming_mode": binding["streaming_mode"],
        "control_plane_managed": True,
        "backend_url": binding.get("backend_url"),
        "backend_path": binding["backend_path"],
        "auth_strategy": binding["auth_strategy"],
        "named_value_name": binding.get("named_value_name"),
        "key_vault_secret_id": binding.get("key_vault_secret_id"),
        "managed_identity_resource": binding.get("managed_identity_resource"),
    }
    if binding["runtime_brand_key"] != "microsoft_foundry":
        return config
    inference_endpoint = str(config.get("inference_endpoint") or "")
    if inference_endpoint:
        inference = urlsplit(inference_endpoint)
        backend_url = f"{inference.scheme}://{inference.netloc}"
    else:
        backend_url = str(config.get("project_endpoint") or binding.get("backend_url"))
    config.update(
        path="/chat/completions",
        api_format="openai_chat",
        backend_url=backend_url.rstrip("/"),
        backend_path="/openai/v1/chat/completions",
        max_tokens_field="max_completion_tokens",
        supports_temperature=False,
    )
    config.pop("anthropic_version", None)
    if binding["api_format"] == "openai_images":
        config["streaming_mode"] = "native"
    return config


UsageDirectoryStatus = Literal["active", "archived", "historical", "unattributed"]
USAGE_DIRECTORY_STATUSES: tuple[UsageDirectoryStatus, ...] = (
    "active", "archived", "historical", "unattributed",
)


@dataclass(frozen=True)
class UsageDirectoryScope:
    statuses: tuple[UsageDirectoryStatus, ...]
    active_organizations: tuple[str, ...]
    archived_organizations: tuple[str, ...]
    active_departments: Mapping[str, str]
    archived_departments: tuple[str, ...]
    archived_users: tuple[str, ...]

    def status(
        self, organization_id: str, department_id: str, user_id: str,
    ) -> UsageDirectoryStatus:
        if organization_id == "unattributed" or department_id == "unattributed":
            return "unattributed"
        if (
            organization_id in self.archived_organizations
            or department_id in self.archived_departments
            or user_id in self.archived_users
        ):
            return "archived"
        if (
            organization_id in self.active_organizations
            and self.active_departments.get(department_id) == organization_id
        ):
            return "active"
        return "historical"


@dataclass(frozen=True)
class UsageFilters:
    organization_id: str | tuple[str, ...] | None = None
    department_id: str | tuple[str, ...] | None = None
    project_id: str | tuple[str, ...] | None = None
    agent_id: str | tuple[str, ...] | None = None
    model_id: str | tuple[str, ...] | None = None
    user_id: str | tuple[str, ...] | None = None
    # The access points a request arrived through, e.g. 'Microsoft Foundry via APIM' or
    # 'GitHub Copilot CLI'. Matched on the display name because token_usage.runtime is the
    # name itself; the registry UUID is never written into telemetry. Several names are
    # accepted because one gateway fronts several runtimes, and "spend through APIM" has to
    # be answerable without inventing a channel column the ingestion path does not produce.
    runtime: tuple[str, ...] | None = None
    status_code: int | None = None
    allowed_department_ids: tuple[str, ...] | None = None
    directory_scope: UsageDirectoryScope | None = None
    data_scope: DataAccessScope | None = None


CACHE_DIMENSION_FIELDS = {
    "organization": "organization_id",
    "department": "department_id",
    "project": "project_id",
    "agent": "agent_id",
    "user": "user_id",
    "model": "model_id",
    "runtime": "runtime",
}


def cache_scope_for_filters(filters: UsageFilters) -> tuple[str, tuple[str, ...]] | None:
    """Use the bounded APIM metric only for an unfiltered global view."""
    if filters.allowed_department_ids is not None or filters.data_scope is not None:
        return None
    if any(
        (
            filters.organization_id,
            filters.department_id,
            filters.project_id,
            filters.agent_id,
            filters.user_id,
            filters.model_id,
            filters.runtime,
            filters.status_code,
            filters.directory_scope,
        )
    ):
        return None
    return "global", ("all",)


def cache_distribution_supported(dimension: str, filters: UsageFilters) -> bool:
    del dimension, filters
    return False


class BudgetConstraintViolation(ValueError):
    pass


def attach_charts(
    reports: Sequence[dict[str, Any]], charts: Sequence[Any]
) -> list[dict[str, Any]]:
    """Nest each report's charts under it, preserving the order the rows arrived in.

    Both repositories fetch reports and their charts as two flat result sets, so the
    grouping is shared rather than written twice and allowed to diverge.
    """
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for chart in charts:
        if chart is not None:
            grouped.setdefault(chart["report_id"], []).append(dict(chart))
    return [{**report, "charts": grouped.get(report["id"], [])} for report in reports]


def report_for_viewer(report: Mapping[str, Any], viewer_id: str) -> dict[str, Any]:
    """Expose authenticated creator semantics without leaking the compatibility owner.

    `owner_id` remains in PostgreSQL for the temporarily older production build. New code
    authorizes through `created_by`, and the API keeps its existing `owner_id` field by
    presenting that creator under the old response name.
    """
    row = dict(report)
    creator = str(row.pop("created_by", row["owner_id"]))
    row["owner_id"] = creator
    row["can_manage"] = creator == viewer_id
    return row


def conversation_for_creator(conversation: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(conversation)
    row["owner_id"] = str(row.pop("created_by", row["owner_id"]))
    return row
