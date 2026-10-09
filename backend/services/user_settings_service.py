from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from turnstile_core.config import Settings
from turnstile_core.domain.enterprise import (
    enterprise_catalog,
    merge_application_owners,
    merge_observed_users,
)
from turnstile_core.domain.models import EnterpriseEntity
from turnstile_core.persistence.repository import QueryRepository

from .auth_service import SessionIdentity
from .budget_service import TokenBudgetService, period_bounds
from .user_settings_models import (
    AccountEntity,
    AccountInformation,
    EditableProfile,
    PermissionGroup,
    PersonalBudget,
    PersonalModel,
    PersonalModelList,
    PersonalUsage,
)


def personal_period(period: str | None) -> tuple[str, date, date, str, str]:
    now = datetime.now(UTC)
    current = date(now.year, now.month, 1)
    month_index = now.year * 12 + now.month - 1 - 11
    minimum = date(month_index // 12, month_index % 12 + 1, 1)
    value = period or current.strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("预算周期无效。")
    try:
        start, end = period_bounds(value)
    except ValueError as error:
        raise ValueError("预算周期无效。") from error
    if not minimum <= start <= current:
        raise ValueError("只能查看最近 12 个月。")
    return value, start, end, minimum.strftime("%Y-%m"), current.strftime("%Y-%m")


class UserSettingsService:
    def __init__(self, repository: QueryRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    def account(self, identity: SessionIdentity) -> AccountInformation:
        seed = enterprise_catalog()
        observed = merge_observed_users(seed, self.repository.observed_users())
        catalog = merge_application_owners(observed, self.repository.application_owners())
        person = next(
            (row for row in catalog.users if row.id.casefold() == identity.email.casefold()), None
        )
        department = next(
            (row for row in catalog.departments if person and row.id == person.parent_id), None
        )
        organization = next(
            (row for row in catalog.organizations if department and row.id == department.parent_id),
            None,
        )
        source: Literal["catalog", "observed_usage", "owner_default", "unlinked"] = "unlinked"
        if person:
            source = (
                "catalog"
                if any(row.id == person.id for row in seed.users)
                else "observed_usage"
                if any(row.id == person.id for row in observed.users)
                else "owner_default"
            )

        def entity(row: EnterpriseEntity | None) -> AccountEntity | None:
            return AccountEntity(id=row.id, name=row.name) if row else None

        editable = identity.method == "password"
        return AccountInformation(
            account_id=UUID(identity.id),
            permission_groups=[
                PermissionGroup(
                    key=identity.role, name="Owner" if identity.role == "owner" else "Member"
                )
            ],
            organization=entity(organization),
            department=entity(department),
            membership_source=source,
            editable=EditableProfile(avatar=editable, display_name=editable, password=editable),
            generated_at=datetime.now(UTC),
        )

    def budget(self, identity: SessionIdentity, period: str | None) -> PersonalBudget:
        value, start, end, minimum, maximum = personal_period(period)
        person = identity.email.strip().lower()
        rows = self.repository.list_token_budgets(start, user_id=person)
        allocation = next(
            (row for row in rows if row["scope_type"] == "user" and row["scope_id"] == person), None
        )
        usage = self.repository.token_usage_by_budget_scope(
            datetime.combine(start, time.min, UTC),
            datetime.combine(end, time.min, UTC),
            user_id=person,
        )
        used = sum(
            int(row["used_tokens"])
            for row in usage
            if row["scope_type"] == "user" and row["scope_id"] == person
        )
        limit = int(allocation["token_limit"]) if allocation else None
        threshold = int(allocation["warning_threshold_percent"]) if allocation else None
        forecast = TokenBudgetService._forecast_tokens(used, start, end, datetime.now(UTC))
        percent = round(used / limit * 100, 1) if limit else None
        forecast_percent = round(forecast / limit * 100, 1) if limit else None
        status: Literal["unallocated", "healthy", "warning", "exceeded"] = "unallocated"
        if limit is not None:
            status = (
                "exceeded"
                if used >= limit
                else "warning"
                if max(percent or 0, forecast_percent or 0) >= (threshold or 80)
                else "healthy"
            )
        department_id = allocation["parent_scope_id"] if allocation else None
        if not department_id:
            department = self.account(identity).department
            department_id = department.id if department else None
        enforcement = next(
            (
                row
                for row in self.repository.list_department_enforcement()
                if row["department_id"] == department_id
            ),
            None,
        )
        return PersonalBudget(
            period=value,
            period_start=start,
            period_end=end,
            min_period=minimum,
            max_period=maximum,
            token_limit=limit,
            used_tokens=used,
            remaining_tokens=limit - used if limit is not None else None,
            warning_threshold_percent=threshold,
            usage_percent=percent,
            forecast_tokens=forecast,
            forecast_percent=forecast_percent,
            status=status,
            enforcement_mode=enforcement["mode"]
            if enforcement
            else "audit"
            if department_id
            else None,
            enforcement_department_id=department_id,
            generated_at=datetime.now(UTC),
        )

    def models(self, identity: SessionIdentity) -> PersonalModelList:
        person = identity.email.strip().lower()
        policy = next(
            (
                row
                for row in self.repository.list_user_model_policies([person])
                if row["user_id"] == person
            ),
            None,
        )
        allowed = {str(value) for value in policy["model_ids"]} if policy else set()
        registry = self.repository.registry()
        runtimes = {str(row["id"]): row for row in registry["runtimes"]}
        providers = {str(row["id"]): row for row in registry["providers"]}
        gateways = {str(row["id"]): row for row in registry["gateways"]}
        items = []
        for model in registry["models"]:
            if not model["enabled"] or (
                str(model["id"]) not in allowed
                if policy
                else model.get("assignment_required", False)
            ):
                continue
            runtime = runtimes.get(str(model["runtime_id"]))
            provider = providers.get(str(model["provider_id"]))
            if not runtime or not provider or not runtime["enabled"] or not provider["enabled"]:
                continue
            if (
                identity.role not in model["allowed_roles"]
                or identity.role not in runtime["allowed_roles"]
            ):
                continue
            gateway = gateways.get(str(runtime.get("gateway_profile_id")))
            if gateway and not gateway["enabled"]:
                continue
            if self.settings.production and (not gateway or gateway["implementation"] != "apim"):
                continue
            if (
                "image_generation" in model["capabilities"]
                and not self.settings.image_generation_enabled
            ):
                continue
            if (
                self.repository.invocation_route(UUID(str(runtime["id"])), UUID(str(model["id"])))
                is None
            ):
                continue
            health = runtime.get("health_status", "unknown")
            items.append(
                PersonalModel(
                    id=model["id"],
                    name=model["display_name"],
                    model_key=model["model_key"],
                    provider_name=provider["name"],
                    context_window=model.get("context_window"),
                    capabilities=model["capabilities"],
                    access_source="explicit" if policy else "legacy_compatibility",
                    runtime_status=health if health in {"available", "unavailable"} else "unknown",
                )
            )
        return PersonalModelList(
            policy_state="assigned" if allowed else "deny_all" if policy else "unconfigured",
            items=sorted(items, key=lambda row: row.name),
            generated_at=datetime.now(UTC),
        )

    def usage(
        self,
        identity: SessionIdentity,
        period: str | None,
        interval: Literal["hour", "day"],
        timezone: str,
    ) -> PersonalUsage:
        value, start, end, _, _ = personal_period(period)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("显示时区无效。") from error
        from_ = datetime.combine(start, time.min, UTC)
        to = datetime.combine(end, time.min, UTC)
        payload: dict[str, Any] = self.repository.personal_usage(
            identity.email.strip().lower(), from_, to, interval, timezone
        )
        return PersonalUsage.model_validate(
            {
                **payload,
                "period": value,
                "from": from_,
                "to": to,
                "interval": interval,
                "timezone": timezone,
                "generated_at": datetime.now(UTC),
                "demo": self.settings.data_backend == "demo",
            }
        )
