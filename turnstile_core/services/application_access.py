from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    GatewayApplicationDetail,
    GatewayApplicationDiscovery,
    GatewayApplicationList,
    GatewayApplicationModelAccessUpdate,
    GatewayApplicationProvisioningDefaults,
    GatewayApplicationSubscription,
    GatewayApplicationSubscriptionCancel,
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
        application_mapping_projector: Callable[[UUID | None], int] | None = None,
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
        self._application_mapping_projector = application_mapping_projector
        self._default_token_limit = default_token_limit
        self._default_tokens_per_minute = default_tokens_per_minute

    def sync_discovery(
        self, discovery: GatewayApplicationDiscovery, actor: str
    ) -> Sequence[dict[str, object]]:
        period_start, _ = _period_bounds(discovery.discovered_at)
        values = [
            {
                **item.model_dump(),
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
            }
            for item in discovery.items
        ]
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

    def applications(self, owner_user_id: UUID | None = None) -> GatewayApplicationList:
        period_start, period_end = _period_bounds()
        rows = list(
            self._repository.list_gateway_applications()
            if owner_user_id is None
            else self._repository.list_gateway_applications_for_owner(owner_user_id)
        )
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

    def application(
        self, application_id: UUID, owner_user_id: UUID | None = None
    ) -> GatewayApplicationDetail:
        period_start, period_end = _period_bounds()
        row = self._repository.get_gateway_application(application_id)
        if row is None or (
            owner_user_id is not None
            and row.get("owner_user_id") != owner_user_id
        ):
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

    def user_subscription_rows(self, owner_user_id: UUID) -> list[dict[str, object]]:
        applications = list(
            self._repository.list_gateway_applications_for_owner(owner_user_id)
        )
        if not applications:
            return []
        application_by_id = {UUID(str(row["id"])): row for row in applications}
        subscriptions = self._repository.list_gateway_application_subscriptions(
            list(application_by_id)
        )
        return [
            {
                **dict(subscription),
                "application_display_name": application_by_id[
                    UUID(str(subscription["application_id"]))
                ]["display_name"],
                "application_type": application_by_id[
                    UUID(str(subscription["application_id"]))
                ]["application_type"],
                "created_at": subscription["discovered_at"],
            }
            for subscription in subscriptions
        ]

    def subscription_export_rows(self) -> list[dict[str, object]]:
        applications = list(self._repository.list_gateway_applications())
        if not applications:
            return []
        application_by_id = {UUID(str(row["id"])): row for row in applications}
        subscriptions = self._repository.list_gateway_application_subscriptions(
            list(application_by_id)
        )
        rows: list[dict[str, object]] = []
        for subscription in subscriptions:
            application = application_by_id[UUID(str(subscription["application_id"]))]
            owner_user_id = application.get("owner_user_id")
            if owner_user_id is None or bool(application.get("system_managed")):
                continue
            rows.append(
                {
                    "user_id": str(owner_user_id),
                    "department_id": str(application.get("organization_unit_id") or ""),
                    "application_id": str(application["id"]),
                    "application_name": str(application["display_name"]),
                    "application_type": str(application["application_type"]),
                    "subscription_record_id": str(subscription["id"]),
                    "subscription_id": str(subscription["apim_subscription_id"]),
                    "subscription_state": str(subscription["state"]),
                    "created_at": subscription["discovered_at"],
                }
            )
        return rows

    def subscription_keys_for_export(self, apim_subscription_id: str) -> tuple[str, str]:
        client = self._require_key_client()
        try:
            return client.application_subscription_keys(apim_subscription_id)
        except (httpx.HTTPError, PolicyCompilationError) as error:
            self._raise_key_management_error(error)

    def claim_application_owner(
        self,
        application_id: UUID,
        owner_user_id: UUID | None,
        tenant_id: UUID | None,
        organization_unit_id: UUID | None,
        owner_email: str | None,
        actor: str,
    ) -> dict[str, object]:
        current = self._repository.get_gateway_application(application_id)
        if current is None:
            raise ControlPlaneNotFoundError("Application not found")
        previous = dict(current)
        if bool(previous.get("system_managed")):
            raise ControlPlaneConflictError(
                "System-managed application ownership cannot be changed"
            )
        row = self._repository.update_gateway_application_owner(
            application_id,
            owner_user_id,
            tenant_id,
            organization_unit_id,
            owner_email,
            actor,
        )
        if row is None:
            raise ControlPlaneNotFoundError("Application not found")
        if self._application_mapping_projector is not None:
            try:
                self._application_mapping_projector(None)
            except Exception as error:
                try:
                    self._repository.update_gateway_application_owner(
                        application_id,
                        UUID(str(previous["owner_user_id"]))
                        if previous.get("owner_user_id")
                        else None,
                        UUID(str(previous["tenant_id"]))
                        if previous.get("tenant_id")
                        else None,
                        UUID(str(previous["organization_unit_id"]))
                        if previous.get("organization_unit_id")
                        else None,
                        str(previous.get("owner_email") or previous.get("owner_id") or "")
                        or None,
                        actor,
                    )
                    self._application_mapping_projector(None)
                except Exception:
                    # Keep the failure generic: neither database nor Azure errors may expose
                    # request or credential data through an ownership operation.
                    pass
                raise ControlPlaneUnavailableError(
                    "Application owner mapping could not be projected to APIM admission"
                ) from error
        return row

    def application_avatar(
        self, application_id: UUID, owner_user_id: UUID | None = None
    ) -> tuple[str, bytes]:
        application = self._repository.get_gateway_application(application_id)
        if application is None or (
            owner_user_id is not None
            and application.get("owner_user_id") != owner_user_id
        ):
            raise ControlPlaneNotFoundError("Application not found")
        row = self._repository.get_gateway_application_avatar(application_id)
        if row is None:
            raise ControlPlaneNotFoundError("Application avatar not found")
        return str(row["media_type"]), bytes(row["image_bytes"])

    def refresh_application_mappings(self, owner_user_id: UUID | None = None) -> int:
        """Project current owner state to the APIM admission ledger immediately."""
        if self._application_mapping_projector is None:
            return 0
        return self._application_mapping_projector(owner_user_id)

    def application_usage_activity(
        self,
        application_id: UUID,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
        owner_user_id: UUID | None = None,
    ) -> TrendResponse:
        application = self._repository.get_gateway_application(application_id)
        if application is None or (
            owner_user_id is not None
            and application.get("owner_user_id") != owner_user_id
        ):
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
        *,
        actor_user_id: UUID | None = None,
        can_manage_all: bool = True,
    ) -> GatewayApplicationSubscriptionKeySecret:
        subscription = self._key_subscription(
            application_id,
            application_subscription_id,
            actor_user_id=actor_user_id,
            can_manage_all=can_manage_all,
        )
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

    def apim_subscription_id_for_key_management(
        self,
        application_id: UUID,
        application_subscription_id: UUID,
        *,
        actor_user_id: UUID | None = None,
        can_manage_all: bool = True,
        allow_cancelled: bool = False,
    ) -> str:
        subscription = self._key_subscription(
            application_id,
            application_subscription_id,
            actor_user_id=actor_user_id,
            can_manage_all=can_manage_all,
            allow_cancelled=allow_cancelled,
        )
        return str(subscription["apim_subscription_id"])

    def rotate_application_subscription_key(
        self,
        application_id: UUID,
        application_subscription_id: UUID,
        key_kind: Literal["primary", "secondary"],
        request: GatewayApplicationSubscriptionKeyRotation,
        *,
        actor_user_id: UUID | None = None,
        can_manage_all: bool = True,
    ) -> None:
        subscription = self._key_subscription(
            application_id,
            application_subscription_id,
            actor_user_id=actor_user_id,
            can_manage_all=can_manage_all,
        )
        apim_subscription_id = str(subscription["apim_subscription_id"])
        if request.confirmation != apim_subscription_id:
            raise ValueError("confirmation must match the APIM subscription ID")
        client = self._require_key_client()
        try:
            client.regenerate_application_subscription_key(apim_subscription_id, key_kind)
        except (httpx.HTTPError, PolicyCompilationError) as error:
            self._raise_key_management_error(error)

    def cancel_application_subscription(
        self,
        application_id: UUID,
        application_subscription_id: UUID,
        request: GatewayApplicationSubscriptionCancel,
        actor: str,
        *,
        actor_user_id: UUID | None = None,
        can_manage_all: bool = True,
    ) -> None:
        subscription = self._key_subscription(
            application_id,
            application_subscription_id,
            actor_user_id=actor_user_id,
            can_manage_all=can_manage_all,
            allow_cancelled=True,
        )
        apim_subscription_id = str(subscription["apim_subscription_id"])
        if request.confirmation != apim_subscription_id:
            raise ValueError("confirmation must match the APIM subscription ID")
        if subscription.get("state") == "cancelled":
            return
        client = self._require_key_client()
        try:
            client.cancel_application_subscription(apim_subscription_id)
        except (httpx.HTTPError, PolicyCompilationError) as error:
            self._raise_key_management_error(error)
        updated = self._repository.update_gateway_application_subscription_state(
            application_subscription_id, "cancelled", actor
        )
        if updated is None:
            raise ControlPlaneNotFoundError("Application subscription not found")

    def _key_subscription(
        self,
        application_id: UUID,
        application_subscription_id: UUID,
        *,
        actor_user_id: UUID | None = None,
        can_manage_all: bool = True,
        allow_cancelled: bool = False,
    ) -> Mapping[str, object]:
        application = self._repository.get_gateway_application(application_id)
        if application is None:
            raise ControlPlaneNotFoundError("Application not found")
        if bool(application.get("system_managed")):
            raise ControlPlaneConflictError(
                "System-managed subscription keys cannot be managed here"
            )
        if not can_manage_all and (
            actor_user_id is None
            or str(application.get("owner_user_id") or "") != str(actor_user_id)
        ):
            raise ControlPlaneNotFoundError("Application subscription not found")
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
        if subscription.get("state") == "cancelled" and not allow_cancelled:
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
                    }
                )
            )
        return summaries
