from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Literal, Never
from uuid import UUID, uuid4

import httpx

from ..domain.application_access import (
    GatewayApplicationAuditEvent,
    GatewayApplicationAvatar,
    GatewayApplicationAvatarUpdate,
    GatewayApplicationBudget,
    GatewayApplicationBudgetUpdate,
    GatewayApplicationBulkDepartment,
    GatewayApplicationBulkDepartmentResult,
    GatewayApplicationDepartmentUpdate,
    GatewayApplicationDetail,
    GatewayApplicationDiscovery,
    GatewayApplicationList,
    GatewayApplicationModelAccessUpdate,
    GatewayApplicationOwnerUpdate,
    GatewayApplicationProvisioningDefaults,
    GatewayApplicationSubscription,
    GatewayApplicationSubscriptionKeyRotation,
    GatewayApplicationSubscriptionKeySecret,
    GatewayApplicationSubscriptionProvisionSpec,
    GatewayApplicationSummary,
    GatewayApplicationUsage,
    GatewayApplicationUserUsage,
    application_id_for,
    application_subscription_id_for,
    decode_application_avatar_data_url,
)
from ..domain.application_attribution import (
    OwnerDerivation,
    department_for_owner,
    derive_owner,
    person_group,
)
from ..domain.enterprise import governance_departments
from ..domain.models import TrendResponse
from ..integrations.apim_control_plane_contract import (
    ApimSubscriptionKeyClient,
    PolicyCompilationError,
)
from ..persistence.repository import QueryRepository
from .control_plane import (
    ControlPlaneConflictError,
    ControlPlaneNotFoundError,
    ControlPlaneUnavailableError,
)


def _period_bounds(moment: datetime | None = None) -> tuple[date, date]:
    current = (moment or datetime.now(UTC)).date().replace(day=1)
    next_month = (
        date(current.year + 1, 1, 1)
        if current.month == 12
        else date(current.year, current.month + 1, 1)
    )
    return current, next_month


def _avatar_url(application_id: UUID, updated_at: datetime | None) -> str | None:
    if updated_at is None:
        return None
    version = int(updated_at.timestamp() * 1_000_000)
    return f"/api/v1/application-access/applications/{application_id}/avatar?v={version}"


class ApplicationAccessService:
    def __init__(
        self,
        repository: QueryRepository,
        *,
        sync_available: bool,
        sync_unavailable_reason: str | None = None,
        provisioning_available: bool = False,
        provisioning_unavailable_reason: str | None = None,
        key_management_available: bool = False,
        key_management_unavailable_reason: str | None = None,
        key_client: ApimSubscriptionKeyClient | None = None,
        default_token_limit: int = 100_000,
        default_tokens_per_minute: int = 100_000,
    ) -> None:
        self._repository = repository
        self._sync_available = sync_available
        self._sync_unavailable_reason = sync_unavailable_reason
        self._provisioning_available = provisioning_available
        self._provisioning_unavailable_reason = provisioning_unavailable_reason
        self._key_management_available = key_management_available and key_client is not None
        self._key_management_unavailable_reason = key_management_unavailable_reason
        self._key_client = key_client
        self._default_token_limit = default_token_limit
        self._default_tokens_per_minute = default_tokens_per_minute

    def sync_discovery(
        self, discovery: GatewayApplicationDiscovery, actor: str
    ) -> Sequence[dict[str, object]]:
        period_start, _ = _period_bounds(discovery.discovered_at)
        # Attribution is worked out here rather than in the repository so that the rule is
        # testable without a database, and so the whole existing estate is read once instead of
        # once per discovered subscription.
        known = [
            {
                "owner_id": row.get("owner_id"),
                "department_id": row.get("department_id"),
                "person_group": person_group(str(row.get("display_name") or "")),
            }
            for row in self._repository.list_gateway_applications()
        ]
        values = []
        for item in discovery.items:
            derivation = derive_owner(item.display_name, apim_owner_email=item.owner_email)
            # A system subscription is the platform talking to itself. Giving it a holder would
            # put the platform in the person directory and hang a budget off it.
            if item.system_managed:
                derivation = OwnerDerivation()
            inherited = department_for_owner(
                derivation.owner_id, derivation.person_group, known
            )
            values.append(
                {
                    **item.model_dump(exclude={"owner_email"}),
                    "application_id": application_id_for(
                        discovery.gateway_profile_id, item.apim_subscription_id
                    ),
                    "subscription_id": application_subscription_id_for(
                        discovery.gateway_profile_id, item.apim_subscription_id
                    ),
                    "audit_id": uuid4(),
                    "slug": item.apim_subscription_id.casefold(),
                    "source": (
                        "bicep"
                        if item.apim_subscription_id
                        in {"turnstile-dashboard", "turnstile-publisher-probe"}
                        else "discovered"
                    ),
                    "owner_id": derivation.owner_id,
                    "owner_source": derivation.owner_source,
                    "department_id": inherited,
                    "department_source": "derived" if inherited else None,
                }
            )
            # A key adopted in this same batch is a candidate parent for the next one, so two new
            # keys for one person do not need two separate syncs to end up together.
            known.append(
                {
                    "owner_id": derivation.owner_id,
                    "department_id": inherited,
                    "person_group": derivation.person_group,
                }
            )
        return self._repository.sync_gateway_applications(
            discovery.gateway_profile_id,
            values,
            actor,
            period_start,
            self._default_token_limit,
            self._default_tokens_per_minute,
        )

    def provision(
        self,
        gateway_profile_id: UUID,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        actor: str,
    ) -> dict[str, object]:
        period_start, _ = _period_bounds()
        return self._repository.provision_gateway_application(
            gateway_profile_id,
            spec.model_dump(mode="python"),
            actor,
            period_start,
            spec.initial_monthly_token_limit or self._default_token_limit,
            spec.initial_tokens_per_minute or self._default_tokens_per_minute,
        )

    def applications(self) -> GatewayApplicationList:
        period_start, period_end = _period_bounds()
        rows = list(self._repository.list_gateway_applications())
        summaries = self._summaries(rows, period_start, period_end)
        return GatewayApplicationList(
            items=summaries,
            period_start=period_start,
            total=len(summaries),
            active=sum(item.status == "active" for item in summaries),
            suspended=sum(item.status == "suspended" for item in summaries),
            retired=sum(item.status == "retired" for item in summaries),
            stale_subscriptions=sum(item.stale_subscription_count for item in summaries),
            sync_available=self._sync_available,
            sync_unavailable_reason=(
                None if self._sync_available else self._sync_unavailable_reason
            ),
            provisioning_available=self._provisioning_available,
            provisioning_defaults=GatewayApplicationProvisioningDefaults(
                monthly_token_limit=self._default_token_limit,
                tokens_per_minute=self._default_tokens_per_minute,
            ),
            provisioning_unavailable_reason=(
                None if self._provisioning_available else self._provisioning_unavailable_reason
            ),
            key_management_available=self._key_management_available,
            key_management_unavailable_reason=(
                None if self._key_management_available else self._key_management_unavailable_reason
            ),
        )

    def application(self, application_id: UUID) -> GatewayApplicationDetail:
        period_start, period_end = _period_bounds()
        row = self._repository.get_gateway_application(application_id)
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        summary = self._summaries([row], period_start, period_end)[0]
        subscriptions = [
            GatewayApplicationSubscription.model_validate(item)
            for item in self._repository.list_gateway_application_subscriptions([application_id])
        ]
        audit = [
            GatewayApplicationAuditEvent.model_validate(item)
            for item in self._repository.list_gateway_application_audit(application_id)
        ]
        user_rows = list(
            self._repository.gateway_application_user_usage(
                period_start, period_end, application_id
            )
        )
        user_count = int(user_rows[0]["user_count"]) if user_rows else 0
        users = [
            GatewayApplicationUserUsage.model_validate(
                {key: value for key, value in item.items() if key != "user_count"}
            )
            for item in user_rows
        ]
        return GatewayApplicationDetail(
            **summary.model_dump(),
            subscriptions=subscriptions,
            users=users,
            user_count=user_count,
            audit=audit,
            key_management_available=self._key_management_available,
            key_management_unavailable_reason=(
                None if self._key_management_available else self._key_management_unavailable_reason
            ),
        )

    def application_avatar(self, application_id: UUID) -> tuple[str, bytes]:
        row = self._repository.get_gateway_application_avatar(application_id)
        if row is None:
            raise ControlPlaneNotFoundError("Application avatar not found")
        return str(row["media_type"]), bytes(row["image_bytes"])

    def application_usage_activity(
        self,
        application_id: UUID,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
    ) -> TrendResponse:
        if self._repository.get_gateway_application(application_id) is None:
            raise ControlPlaneNotFoundError("Application not found")
        return TrendResponse.model_validate(
            {
                "from": from_,
                "to": to,
                "timezone": timezone,
                "interval": interval,
                "group_by": "none",
                "points": self._repository.gateway_application_usage_activity(
                    application_id,
                    from_,
                    to,
                    interval,
                    timezone,
                ),
            }
        )

    def reveal_application_subscription_key(
        self,
        application_id: UUID,
        application_subscription_id: UUID,
        key_kind: Literal["primary", "secondary"],
    ) -> GatewayApplicationSubscriptionKeySecret:
        subscription = self._key_subscription(application_id, application_subscription_id)
        client = self._require_key_client()
        try:
            primary_key, secondary_key = client.application_subscription_keys(
                str(subscription["apim_subscription_id"])
            )
        except (httpx.HTTPError, PolicyCompilationError) as error:
            self._raise_key_management_error(error)
        return GatewayApplicationSubscriptionKeySecret(
            key_kind=key_kind,
            value=primary_key if key_kind == "primary" else secondary_key,
        )

    def rotate_application_subscription_key(
        self,
        application_id: UUID,
        application_subscription_id: UUID,
        key_kind: Literal["primary", "secondary"],
        request: GatewayApplicationSubscriptionKeyRotation,
    ) -> None:
        subscription = self._key_subscription(application_id, application_subscription_id)
        apim_subscription_id = str(subscription["apim_subscription_id"])
        if request.confirmation != apim_subscription_id:
            raise ValueError("confirmation must match the APIM subscription ID")
        client = self._require_key_client()
        try:
            client.regenerate_application_subscription_key(apim_subscription_id, key_kind)
        except (httpx.HTTPError, PolicyCompilationError) as error:
            self._raise_key_management_error(error)

    def _key_subscription(
        self, application_id: UUID, application_subscription_id: UUID
    ) -> Mapping[str, object]:
        application = self._repository.get_gateway_application(application_id)
        if application is None:
            raise ControlPlaneNotFoundError("Application not found")
        if bool(application.get("system_managed")):
            raise ControlPlaneConflictError(
                "System-managed subscription keys cannot be managed here"
            )
        subscription = next(
            (
                item
                for item in self._repository.list_gateway_application_subscriptions(
                    [application_id]
                )
                if UUID(str(item["id"])) == application_subscription_id
            ),
            None,
        )
        if subscription is None:
            raise ControlPlaneNotFoundError("Application subscription not found")
        if subscription.get("state") == "cancelled":
            raise ControlPlaneConflictError("APIM subscription is cancelled")
        return subscription

    def _require_key_client(self) -> ApimSubscriptionKeyClient:
        if not self._key_management_available or self._key_client is None:
            raise ControlPlaneUnavailableError(
                self._key_management_unavailable_reason
                or "Application subscription key management is unavailable"
            )
        return self._key_client

    @staticmethod
    def _raise_key_management_error(
        error: httpx.HTTPError | PolicyCompilationError,
    ) -> Never:
        if isinstance(error, httpx.HTTPStatusError):
            if error.response.status_code == 404:
                raise ControlPlaneNotFoundError("APIM subscription no longer exists") from error
            if error.response.status_code in {401, 403}:
                raise ControlPlaneUnavailableError(
                    "APIM key management is not authorized"
                ) from error
        raise ControlPlaneUnavailableError("APIM key management request failed") from error

    def update_application_avatar(
        self,
        application_id: UUID,
        request: GatewayApplicationAvatarUpdate,
        actor: str,
    ) -> GatewayApplicationAvatar:
        media_type: str | None = None
        image_bytes: bytes | None = None
        if request.avatar_data_url is not None:
            media_type, image_bytes = decode_application_avatar_data_url(request.avatar_data_url)
        row = self._repository.update_gateway_application_avatar(
            application_id, media_type, image_bytes, actor
        )
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        updated_at = row.get("updated_at")
        return GatewayApplicationAvatar(
            avatar_url=_avatar_url(
                application_id,
                updated_at if isinstance(updated_at, datetime) else None,
            ),
            updated_at=updated_at if isinstance(updated_at, datetime) else None,
        )

    def update_application_budget(
        self,
        application_id: UUID,
        request: GatewayApplicationBudgetUpdate,
        actor: str,
    ) -> GatewayApplicationDetail:
        period_start, _ = _period_bounds()
        row = self._repository.update_gateway_application_budget(
            application_id,
            period_start,
            request.model_dump(mode="python"),
            actor,
        )
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        return self.application(application_id)

    def update_application_department(
        self,
        application_id: UUID,
        request: GatewayApplicationDepartmentUpdate,
        actor: str,
    ) -> GatewayApplicationDetail:
        """File a subscription under a department.

        The department is checked against the catalog because the id is a join key: a
        subscription filed under one that does not exist is filed nowhere, while still
        reading on screen as though it had been filed.

        The gateway reads this column on every request that declares no department of its
        own, so filing a subscription is what makes its traffic count against that
        department's budget. It takes effect from the next request; usage already recorded
        keeps the department it was recorded under.
        """
        if request.department_id is not None:
            known = {item.id for item in governance_departments(self._repository.org_units())}
            if request.department_id not in known:
                raise ValueError(f"Unknown department: {request.department_id}")
        row = self._repository.update_gateway_application_department(
            application_id, request.department_id, actor
        )
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        return self.application(application_id)

    def update_application_owner(
        self,
        application_id: UUID,
        request: GatewayApplicationOwnerUpdate,
        actor: str,
    ) -> GatewayApplicationDetail:
        """Say who holds a subscription, when neither APIM nor its name could say.

        Writing this marks the field as decided by a person, and from then on no sync will
        touch it. That is the whole contract: a sync is allowed to fill blanks so that an
        estate of several hundred keys can be attributed without anyone typing, and is never
        allowed to undo a correction someone made afterwards.
        """
        row = self._repository.update_gateway_application_owner(
            application_id, request.owner_id, actor
        )
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        return self.application(application_id)

    def update_application_department_bulk(
        self, request: GatewayApplicationBulkDepartment, actor: str
    ) -> GatewayApplicationBulkDepartmentResult:
        if request.department_id is not None:
            known = {item.id for item in governance_departments(self._repository.org_units())}
            if request.department_id not in known:
                raise ValueError(f"Unknown department: {request.department_id}")
        # Snapshotted, not held by reference. The in-memory repository hands back the same
        # dicts it stores and updates them in place, so comparing afterwards against a row
        # read before the write reports every change as a no-op.
        rows = {
            UUID(str(row["id"])): {
                "department_id": row.get("department_id"),
                "system_managed": row.get("system_managed"),
            }
            for row in self._repository.list_gateway_applications()
        }
        updated = 0
        unchanged = 0
        for application_id in request.application_ids:
            row = rows.get(application_id)
            if row is None or row.get("system_managed"):
                # A system-managed subscription belongs to the platform; silently skipping it
                # is kinder than failing a batch of four hundred over one row.
                unchanged += 1
                continue
            if row.get("department_id") == request.department_id:
                unchanged += 1
                continue
            if self._repository.update_gateway_application_department(
                application_id, request.department_id, actor
            ) is None:
                unchanged += 1
            else:
                updated += 1
        return GatewayApplicationBulkDepartmentResult(updated=updated, unchanged=unchanged)

    def update_application_model_access(
        self,
        application_id: UUID,
        request: GatewayApplicationModelAccessUpdate,
        actor: str,
    ) -> GatewayApplicationDetail:
        row = self._repository.update_gateway_application_model_access(
            application_id,
            request.mode,
            request.model_ids,
            actor,
        )
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        return self.application(application_id)

    def _summaries(
        self,
        rows: Sequence[Mapping[str, object]],
        period_start: date,
        period_end: date,
    ) -> list[GatewayApplicationSummary]:
        application_ids = [UUID(str(row["id"])) for row in rows]
        subscriptions: dict[UUID, list[Mapping[str, object]]] = {}
        for item in self._repository.list_gateway_application_subscriptions(application_ids):
            subscriptions.setdefault(UUID(str(item["application_id"])), []).append(item)
        budgets = {
            UUID(str(item["application_id"])): item
            for item in self._repository.list_gateway_application_budgets(
                period_start, application_ids
            )
        }
        ledger_states = {
            UUID(str(item["application_id"])): item
            for item in self._repository.list_gateway_application_ledger_states(
                period_start, application_ids
            )
        }
        configured_policies: set[UUID] = set()
        allowed_models: dict[UUID, list[UUID]] = {}
        for item in self._repository.list_gateway_application_model_access(application_ids):
            application_id = UUID(str(item["application_id"]))
            configured_policies.add(application_id)
            if item.get("model_id") is not None:
                allowed_models.setdefault(application_id, []).append(UUID(str(item["model_id"])))
        usage = {
            UUID(str(item["application_id"])): item
            for item in self._repository.gateway_application_usage(
                period_start, period_end, application_ids
            )
        }
        avatars = {
            UUID(str(item["application_id"])): item
            for item in self._repository.list_gateway_application_avatars(application_ids)
        }
        department_names = {
            item.id: item.name
            for item in governance_departments(self._repository.org_units())
        }
        attribution = {
            UUID(str(item["application_id"])): item
            for item in self._repository.list_gateway_application_attribution()
        }
        # Counted across the whole estate rather than per row: a holder with two keys is only
        # visible as such when both are in view, and the screen needs to say so on either one.
        group_sizes: dict[str, int] = {}
        row_groups: dict[UUID, str | None] = {}
        for row in rows:
            if bool(row.get("system_managed")):
                continue
            group = person_group(str(row.get("display_name") or ""))
            row_groups[UUID(str(row["id"]))] = group
            if group:
                group_sizes[group] = group_sizes.get(group, 0) + 1
        summaries: list[GatewayApplicationSummary] = []
        for row, application_id in zip(rows, application_ids, strict=True):
            application_subscriptions = subscriptions.get(application_id, [])
            usage_model = GatewayApplicationUsage.model_validate(
                {
                    key: value
                    for key, value in usage.get(application_id, {}).items()
                    if key != "application_id"
                }
            )
            budget_row = budgets.get(application_id)
            budget = None
            if budget_row is not None:
                token_limit = int(budget_row["token_limit"])
                budget_tokens = self._repository.budget_scope_confirmed_tokens(
                    "application", str(application_id), period_start, period_end
                )
                remaining = max(token_limit - budget_tokens, 0)
                ledger_state = ledger_states.get(application_id)
                budget = GatewayApplicationBudget.model_validate(
                    {
                        "period_start": budget_row["period_start"],
                        "token_limit": token_limit,
                        "tokens_per_minute": budget_row["tokens_per_minute"],
                        "enforce": budget_row["enforce"],
                        "warning_threshold_percent": budget_row["warning_threshold_percent"],
                        "updated_by": budget_row["updated_by"],
                        "updated_at": budget_row["updated_at"],
                        "used_tokens": budget_tokens,
                        "remaining_tokens": remaining,
                        "usage_percent": round(budget_tokens / token_limit * 100, 2),
                        **{
                            field: None if ledger_state is None else ledger_state[field]
                            for field in (
                                "pending_reserved_tokens",
                                "pending_reservation_count",
                                "finalized_upper_bound_tokens",
                                "finalized_upper_bound_count",
                                "stale_reservation_count",
                                "oldest_reservation_at",
                                "available_tokens",
                            )
                        },
                        "ledger_snapshot_at": None
                        if ledger_state is None
                        else ledger_state["snapshot_at"],
                    }
                )
            summaries.append(
                GatewayApplicationSummary.model_validate(
                    {
                        **dict(row),
                        # Resolved per response rather than stored: a
                        # department that gets renamed would otherwise keep
                        # showing its old name on every subscription filed
                        # under it until someone re-saved each one.
                        "department_name": department_names.get(
                            str(row["department_id"] or "")
                        ),
                        "avatar_url": _avatar_url(
                            application_id,
                            avatars.get(application_id, {}).get("updated_at"),
                        ),
                        "subscription_count": len(application_subscriptions),
                        "active_subscription_count": sum(
                            item["state"] == "active" for item in application_subscriptions
                        ),
                        "stale_subscription_count": sum(
                            not bool(item["scope_exists"]) for item in application_subscriptions
                        ),
                        "model_policy_configured": (application_id in configured_policies),
                        "allowed_model_ids": sorted(
                            allowed_models.get(application_id, []), key=str
                        ),
                        "budget": budget,
                        "usage": usage_model,
                        "owner_source": attribution.get(application_id, {}).get("owner_source"),
                        "department_source": attribution.get(application_id, {}).get(
                            "department_source"
                        ),
                        "person_group": row_groups.get(application_id),
                        "person_group_size": group_sizes.get(
                            row_groups.get(application_id) or "", 1
                        ),
                    }
                )
            )
        return summaries
