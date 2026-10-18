from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ..domain.application_access import (
    GatewayApplicationLedgerState,
    UsageApplicationAttribution,
    application_organization,
)
from ..domain.ledger import BudgetReservationFinalization, validate_application_snapshot


class InMemoryApplicationRepositoryMixin:
    gateway_applications: list[dict[str, Any]]
    gateway_application_avatars: dict[UUID, dict[str, Any]]
    gateway_application_subscriptions: list[dict[str, Any]]
    gateway_application_budgets: dict[tuple[date, UUID], dict[str, Any]]
    gateway_application_model_policies: dict[UUID, dict[str, Any]]
    gateway_application_model_access: dict[UUID, set[UUID]]
    gateway_application_audit: list[dict[str, Any]]
    gateway_application_ledger_states: dict[
        tuple[date, UUID], GatewayApplicationLedgerState
    ]
    gateway_application_ledger_finalization_states: dict[
        tuple[date, UUID], dict[str, Any]
    ]
    budget_reservation_finalizations: dict[
        tuple[str, str, str, str, str, datetime], BudgetReservationFinalization
    ]
    usage_application_attributions: dict[str, UsageApplicationAttribution]
    usage_records: list[Any]
    models: list[dict[str, Any]]

    def _application_finalizations(self) -> list[BudgetReservationFinalization]:
        strength = {
            "unverified_upper_bound": 1,
            "terminal_zero": 2,
            "exact_usage": 3,
        }
        strongest: dict[tuple[str, str], BudgetReservationFinalization] = {}
        for item in self.budget_reservation_finalizations.values():
            if item.scope_type != "application":
                continue
            key = (item.scope_id, item.correlation_id)
            existing = strongest.get(key)
            if existing is None or strength[item.finalization_kind] > strength[
                existing.finalization_kind
            ]:
                strongest[key] = item
        return list(strongest.values())

    def sync_gateway_applications(
        self,
        gateway_profile_id: UUID,
        items: Sequence[Mapping[str, Any]],
        actor: str,
        period_start: date,
        default_token_limit: int,
        default_tokens_per_minute: int,
    ) -> Sequence[dict[str, Any]]:
        now = datetime.now(UTC)
        discovered = {str(item["apim_subscription_id"]) for item in items}
        rows: list[dict[str, Any]] = []
        for value in items:
            application_id = UUID(str(value["application_id"]))
            subscription_id = UUID(str(value["subscription_id"]))
            application = next(
                (item for item in self.gateway_applications if item["id"] == application_id),
                None,
            )
            before = deepcopy(application) if application else None
            status = (
                "retired"
                if value["state"] == "cancelled"
                else "suspended"
                if value["state"] == "suspended"
                else "active"
            )
            application_type = (
                application["application_type"]
                if application is not None
                and application["application_type"] in {"agent", "delegated_user"}
                and value["application_type"] == "service"
                else value["application_type"]
            )
            if application is None:
                application = {
                    "id": application_id,
                    "gateway_profile_id": gateway_profile_id,
                    "slug": value["slug"],
                    "display_name": value["display_name"],
                    "description": None,
                    "owner_id": None,
                    "department_id": None,
                    "application_type": application_type,
                    "status": status,
                    "system_managed": value["system_managed"],
                    "created_by": actor,
                    "created_at": now,
                    "updated_by": actor,
                    "updated_at": now,
                }
                self.gateway_applications.append(application)
                self.gateway_application_budgets[(period_start, application_id)] = {
                    "period_start": period_start,
                    "application_id": application_id,
                    "token_limit": default_token_limit,
                    "tokens_per_minute": default_tokens_per_minute,
                    "enforce": not bool(value["system_managed"]),
                    "warning_threshold_percent": 80,
                    "updated_by": actor,
                    "updated_at": now,
                }
                operation = "adopted"
            else:
                application_changed = any(
                    application[field] != expected
                    for field, expected in (
                        ("display_name", value["display_name"]),
                        ("application_type", application_type),
                        ("status", status),
                        ("system_managed", value["system_managed"]),
                    )
                )
                if application_changed:
                    application.update(
                        display_name=value["display_name"],
                        application_type=application_type,
                        status=status,
                        system_managed=value["system_managed"],
                        updated_by=actor,
                        updated_at=now,
                    )
                operation = "updated" if application_changed else "subscription_synced"
            subscription = next(
                (
                    item
                    for item in self.gateway_application_subscriptions
                    if item["gateway_profile_id"] == gateway_profile_id
                    and item["apim_subscription_id"] == value["apim_subscription_id"]
                ),
                None,
            )
            subscription_before = deepcopy(subscription) if subscription else None
            if subscription is None:
                subscription = {
                    "id": subscription_id,
                    "application_id": application_id,
                    "gateway_profile_id": gateway_profile_id,
                    "discovered_at": now,
                }
                self.gateway_application_subscriptions.append(subscription)
            subscription_changed = subscription_before is None or any(
                subscription_before[field] != expected
                for field, expected in (
                    ("application_id", application_id),
                    ("display_name", value["display_name"]),
                    ("scope_type", value["scope_type"]),
                    ("scope_id", value["scope_id"]),
                    ("state", value["state"]),
                    ("scope_exists", value["scope_exists"]),
                )
            )
            subscription.update(
                application_id=application_id,
                display_name=value["display_name"],
                apim_subscription_id=value["apim_subscription_id"],
                scope_type=value["scope_type"],
                scope_id=value["scope_id"],
                state=value["state"],
                scope_exists=value["scope_exists"],
                source=value.get("source", "discovered"),
                last_synced_at=now,
            )
            if before is None or before != application or subscription_changed:
                self.gateway_application_audit.append(
                    {
                        "id": value["audit_id"],
                        "application_id": application_id,
                        "operation": operation,
                        "before_state": (
                            None
                            if before is None
                            else {
                                "application": before,
                                "subscription": subscription_before,
                            }
                        ),
                        "after_state": {
                            "application": deepcopy(application),
                            "subscription": deepcopy(subscription),
                        },
                        "actor": actor,
                        "created_at": now,
                    }
                )
            rows.append(application)
        for subscription in self.gateway_application_subscriptions:
            if (
                subscription["gateway_profile_id"] == gateway_profile_id
                and subscription["apim_subscription_id"] not in discovered
                and (
                    subscription["state"] != "cancelled"
                    or subscription["scope_exists"]
                )
            ):
                before = deepcopy(subscription)
                subscription.update(
                    state="cancelled", scope_exists=False, last_synced_at=now
                )
                self.gateway_application_audit.append(
                    {
                        "id": uuid4(),
                        "application_id": subscription["application_id"],
                        "operation": "subscription_synced",
                        "before_state": {"subscription": before},
                        "after_state": {"subscription": deepcopy(subscription)},
                        "actor": actor,
                        "created_at": now,
                    }
                )
        return rows

    def provision_gateway_application(
        self,
        gateway_profile_id: UUID,
        value: Mapping[str, Any],
        actor: str,
        period_start: date,
        default_token_limit: int,
        default_tokens_per_minute: int,
    ) -> dict[str, Any]:
        application_id = UUID(str(value["application_id"]))
        application_subscription_id = UUID(str(value["application_subscription_id"]))
        existing = self.get_gateway_application(application_id)
        existing_subscription = next(
            (
                item
                for item in self.gateway_application_subscriptions
                if item["gateway_profile_id"] == gateway_profile_id
                and item["apim_subscription_id"] == value["apim_subscription_id"]
            ),
            None,
        )
        if existing is not None or existing_subscription is not None:
            if (
                existing is not None
                and existing_subscription is not None
                and existing_subscription["application_id"] == application_id
                and existing_subscription["source"] == "managed"
            ):
                return existing
            raise ValueError("Application or APIM subscription ID already exists")
        now = datetime.now(UTC)
        application = {
            "id": application_id,
            "gateway_profile_id": gateway_profile_id,
            "slug": value["slug"],
            "display_name": value["display_name"],
            "description": value.get("description"),
            "owner_id": actor,
            "department_id": None,
            "application_type": value["application_type"],
            "status": "active",
            "system_managed": False,
            "tags": dict(value.get("tags", {})),
            "created_by": actor,
            "created_at": now,
            "updated_by": actor,
            "updated_at": now,
        }
        subscription = {
            "id": application_subscription_id,
            "application_id": application_id,
            "gateway_profile_id": gateway_profile_id,
            "apim_subscription_id": value["apim_subscription_id"],
            "display_name": value["display_name"],
            "scope_type": "product",
            "scope_id": value["scope_id"],
            "state": "active",
            "scope_exists": True,
            "source": "managed",
            "discovered_at": now,
            "last_synced_at": now,
        }
        self.gateway_applications.append(application)
        self.gateway_application_subscriptions.append(subscription)
        self.gateway_application_budgets[(period_start, application_id)] = {
            "period_start": period_start,
            "application_id": application_id,
            "budget_mode": value.get("budget_mode", "tokens"),
            "token_limit": (
                default_token_limit if value.get("budget_mode", "tokens") == "tokens" else None
            ),
            "usd_limit": value.get("initial_usd_limit"),
            "tokens_per_minute": default_tokens_per_minute,
            "enforce": True,
            "warning_threshold_percent": 80,
            "updated_by": actor,
            "updated_at": now,
        }
        self.gateway_application_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "operation": "created",
                "before_state": None,
                "after_state": {
                    "application": deepcopy(application),
                    "subscription": deepcopy(subscription),
                    "budget": deepcopy(
                        self.gateway_application_budgets[(period_start, application_id)]
                    ),
                },
                "actor": actor,
                "created_at": now,
            }
        )
        return application

    def list_gateway_applications(self) -> Sequence[dict[str, Any]]:
        return sorted(
            self.gateway_applications,
            key=lambda item: (item["system_managed"], item["display_name"], str(item["id"])),
        )

    def get_gateway_application(self, application_id: UUID) -> dict[str, Any] | None:
        return next(
            (item for item in self.gateway_applications if item["id"] == application_id),
            None,
        )

    def list_gateway_application_avatars(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        return [
            {
                "application_id": application_id,
                "media_type": avatar["media_type"],
                "updated_at": avatar["updated_at"],
            }
            for application_id in application_ids
            if (avatar := self.gateway_application_avatars.get(application_id))
            is not None
        ]

    def get_gateway_application_avatar(
        self, application_id: UUID
    ) -> dict[str, Any] | None:
        avatar = self.gateway_application_avatars.get(application_id)
        return None if avatar is None else deepcopy(avatar)

    def update_gateway_application_organization(
        self, application_id: UUID, organization_id: str | None, actor: str,
        *, department_id: str | None = None, update_department: bool = False,
        owner_id: str | None = None, update_owner: bool = False,
    ) -> dict[str, Any] | None:
        from .directory_catalog import catalog_for
        from .repository_contract import QueryRepository

        application = self.get_gateway_application(application_id)
        if application is None:
            return None
        tags = dict(application.get("tags", {}))
        previous = tags.get("organization_id")
        previous_department = application.get("department_id")
        if not update_department:
            department_id = previous_department if previous == organization_id else None
        previous_owner = application.get("owner_id")
        if not update_owner:
            owner_id = (
                previous_owner
                if previous == organization_id and previous_department == department_id else None
            )
        application_organization(
            {"organization_id": organization_id} if organization_id else {}, department_id,
            catalog=catalog_for(cast(QueryRepository, self)),
        )
        if (previous != organization_id or previous_department != department_id
            or previous_owner != owner_id):
            if organization_id is None:
                tags.pop("organization_id", None)
            else:
                tags["organization_id"] = organization_id
            now = datetime.now(UTC)
            application.update(tags=tags, department_id=department_id, owner_id=owner_id,
                               updated_by=actor, updated_at=now)
            self.gateway_application_audit.append({
                "id": uuid4(), "application_id": application_id, "operation": "updated",
                "before_state": {"organization_id": previous,
                                 "department_id": previous_department, "owner_id": previous_owner},
                "after_state": {"organization_id": organization_id,
                                "department_id": department_id, "owner_id": owner_id},
                "actor": actor, "created_at": now,
            })
        return {
            "organization_id": organization_id, "department_id": department_id,
            "owner_id": owner_id,
        }

    def update_gateway_application_avatar(
        self,
        application_id: UUID,
        media_type: str | None,
        image_bytes: bytes | None,
        actor: str,
    ) -> dict[str, Any] | None:
        if (media_type is None) != (image_bytes is None):
            raise ValueError("Avatar media type and bytes must be set together")
        application = self.get_gateway_application(application_id)
        if application is None:
            return None
        existing = self.gateway_application_avatars.get(application_id)
        if (existing is None and image_bytes is None) or (
            existing is not None
            and existing["media_type"] == media_type
            and existing["image_bytes"] == image_bytes
        ):
            return {
                "application_id": application_id,
                "media_type": None if existing is None else existing["media_type"],
                "updated_at": None if existing is None else existing["updated_at"],
            }
        now = datetime.now(UTC)
        before_state = {
            "configured": existing is not None,
            "media_type": None if existing is None else existing["media_type"],
            "updated_at": None if existing is None else existing["updated_at"],
        }
        if image_bytes is None:
            self.gateway_application_avatars.pop(application_id, None)
            updated_at = None
        else:
            updated_at = now
            self.gateway_application_avatars[application_id] = {
                "application_id": application_id,
                "media_type": media_type,
                "image_bytes": image_bytes,
                "updated_by": actor,
                "updated_at": updated_at,
            }
        application.update(updated_by=actor, updated_at=now)
        after_state = {
            "configured": image_bytes is not None,
            "media_type": media_type,
            "updated_at": updated_at,
        }
        self.gateway_application_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "operation": "updated",
                "before_state": {"avatar": before_state},
                "after_state": {"avatar": after_state},
                "actor": actor,
                "created_at": now,
            }
        )
        return {
            "application_id": application_id,
            "media_type": media_type,
            "updated_at": updated_at,
        }

    def update_gateway_application_budget(
        self,
        application_id: UUID,
        period_start: date,
        value: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        application = self.get_gateway_application(application_id)
        if application is None:
            return None
        now = datetime.now(UTC)
        existing = self.gateway_application_budgets.get((period_start, application_id))
        before_state = deepcopy(existing)
        budget = {
            "period_start": period_start,
            "application_id": application_id,
            "budget_mode": value["budget_mode"],
            "token_limit": value["token_limit"],
            "usd_limit": value["usd_limit"],
            "tokens_per_minute": value["tokens_per_minute"],
            "enforce": value["enforce"],
            "warning_threshold_percent": value["warning_threshold_percent"],
            "updated_by": actor,
            "updated_at": now,
        }
        self.gateway_application_budgets[(period_start, application_id)] = budget
        application.update(updated_by=actor, updated_at=now)
        self.gateway_application_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "operation": "budget_updated",
                "before_state": {"budget": before_state},
                "after_state": {"budget": deepcopy(budget)},
                "actor": actor,
                "created_at": now,
            }
        )
        return budget

    def update_gateway_application_model_access(
        self,
        application_id: UUID,
        mode: str,
        model_ids: Sequence[UUID],
        actor: str,
    ) -> dict[str, Any] | None:
        application = self.get_gateway_application(application_id)
        if application is None:
            return None
        wanted = set(model_ids)
        available = {
            item["id"] for item in self.models if item.get("enabled", True)
        }
        if not wanted.issubset(available):
            raise ValueError("Model access includes an unavailable model")
        now = datetime.now(UTC)
        before_policy = deepcopy(
            self.gateway_application_model_policies.get(application_id)
        )
        before_models = sorted(
            self.gateway_application_model_access.get(application_id, set()), key=str
        )
        configured = mode == "restricted"
        if configured:
            self.gateway_application_model_policies[application_id] = {
                "application_id": application_id,
                "updated_by": actor,
                "updated_at": now,
            }
            self.gateway_application_model_access[application_id] = wanted
        else:
            self.gateway_application_model_policies.pop(application_id, None)
            self.gateway_application_model_access.pop(application_id, None)
        application.update(updated_by=actor, updated_at=now)
        after_state = {
            "configured": configured,
            "model_ids": sorted(wanted, key=str) if configured else [],
        }
        self.gateway_application_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "operation": "models_updated",
                "before_state": {
                    "policy": before_policy,
                    "model_ids": before_models,
                },
                "after_state": after_state,
                "actor": actor,
                "created_at": now,
            }
        )
        return after_state

    def list_gateway_application_subscriptions(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        wanted = set(application_ids)
        return [
            item
            for item in self.gateway_application_subscriptions
            if item["application_id"] in wanted
        ]

    def list_gateway_application_budgets(
        self, period_start: date, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        return [
            self.gateway_application_budgets[(period_start, application_id)]
            for application_id in application_ids
            if (period_start, application_id) in self.gateway_application_budgets
        ]

    def get_effective_gateway_application_budget(
        self, application_id: UUID, period_start: date,
    ) -> dict[str, Any] | None:
        periods = [
            period for period, identity in self.gateway_application_budgets
            if identity == application_id and period <= period_start
        ]
        return (
            deepcopy(self.gateway_application_budgets[(max(periods), application_id)])
            if periods else None
        )

    def list_gateway_application_ledger_states(
        self, period_start: date, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for application_id in application_ids:
            key = (period_start, application_id)
            state = self.gateway_application_ledger_states.get(key)
            if state is None:
                continue
            finalization = self.gateway_application_ledger_finalization_states.get(key)
            if finalization is not None and finalization["snapshot_at"] > state.snapshot_at:
                continue
            finalization_current = (
                finalization is not None
                and finalization["snapshot_at"] == state.snapshot_at
            )
            rows.append(
                {
                    **state.model_dump(mode="python"),
                    "finalized_upper_bound_tokens": (
                        int(finalization["finalized_upper_bound_tokens"])
                        if finalization_current and finalization is not None
                        else 0
                    ),
                    "finalized_upper_bound_count": (
                        int(finalization["finalized_upper_bound_count"])
                        if finalization_current and finalization is not None
                        else 0
                    ),
                }
            )
        return rows

    def save_gateway_application_ledger_states(
        self, items: Sequence[GatewayApplicationLedgerState]
    ) -> None:
        for item in items:
            key = (item.period_start, item.application_id)
            existing = self.gateway_application_ledger_states.get(key)
            if existing is None or existing.snapshot_at <= item.snapshot_at:
                self.gateway_application_ledger_states[key] = item

    def save_gateway_application_ledger_finalization_states(
        self, items: Sequence[Mapping[str, Any]]
    ) -> None:
        for item in items:
            period = item["period_start"]
            key = (
                period if isinstance(period, date) else date.fromisoformat(str(period)),
                UUID(str(item["application_id"])),
            )
            existing = self.gateway_application_ledger_finalization_states.get(key)
            if existing is None or existing["snapshot_at"] <= item["snapshot_at"]:
                self.gateway_application_ledger_finalization_states[key] = dict(item)

    def save_gateway_application_ledger_snapshot(
        self, states: Sequence[GatewayApplicationLedgerState],
        sidecars: Sequence[Mapping[str, Any]],
    ) -> None:
        validate_application_snapshot(states, sidecars)
        self.save_gateway_application_ledger_finalization_states(sidecars)
        self.save_gateway_application_ledger_states(states)

    def list_gateway_application_model_access(
        self, application_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for application_id in application_ids:
            policy = self.gateway_application_model_policies.get(application_id)
            if policy is None:
                continue
            model_ids = sorted(
                self.gateway_application_model_access.get(application_id, set()), key=str
            )
            if not model_ids:
                rows.append({**policy, "model_id": None})
            else:
                rows.extend({**policy, "model_id": model_id} for model_id in model_ids)
        return rows

    def gateway_application_usage(
        self,
        period_start: date,
        period_end: date,
        application_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        wanted = set(application_ids)
        grouped: dict[UUID, list[Any]] = {}
        for record in self.usage_records:
            attribution = self.usage_application_attributions.get(record.id)
            if (
                attribution is not None
                and attribution.application_id in wanted
                and period_start <= record.ts.date() < period_end
                and record.usage_domain == "apim"
            ):
                grouped.setdefault(attribution.application_id, []).append(record)
        canonical_correlations = {
            (attribution.application_id, record.correlation_id)
            for record in self.usage_records
            if record.usage_domain == "apim"
            if (attribution := self.usage_application_attributions.get(record.id))
            is not None
        }
        recovered: dict[UUID, list[Any]] = {}
        for item in self._application_finalizations():
            if (
                UUID(item.scope_id) in wanted
                and item.period_start == period_start
                and item.finalization_kind != "unverified_upper_bound"
                and (UUID(item.scope_id), item.correlation_id)
                not in canonical_correlations
            ):
                recovered.setdefault(UUID(item.scope_id), []).append(item)
        return [
            {
                "application_id": application_id,
                "request_count": len(records) + len(recovered.get(application_id, [])),
                "denied_request_count": sum(row.status_code >= 400 for row in records)
                + sum(
                    (row.status_code or 0) >= 400
                    for row in recovered.get(application_id, [])
                ),
                "input_tokens": sum(row.input_tokens for row in records)
                + sum(
                    row.input_tokens
                    if row.input_tokens is not None
                    else row.total_tokens
                    for row in recovered.get(application_id, [])
                ),
                "cached_tokens": sum(row.cached_tokens for row in records),
                "cache_write_tokens": sum(row.cache_write_tokens for row in records),
                "output_tokens": sum(row.output_tokens for row in records)
                + sum(
                    row.output_tokens or 0
                    for row in recovered.get(application_id, [])
                ),
                "total_tokens": sum(
                    row.input_tokens + row.cached_tokens + row.output_tokens
                    for row in records
                ) + sum(row.total_tokens for row in recovered.get(application_id, [])),
                "estimated_cost": sum(row.estimated_cost for row in records),
                "usd_cost": (
                    sum(
                        (row.usd_cost for row in records if row.usd_cost is not None),
                        start=Decimal(0),
                    )
                    if any(row.usd_cost is not None for row in records)
                    else None
                ),
                "unpriced_usage_count": sum(row.usd_cost is None for row in records)
                + len(recovered.get(application_id, [])),
                "last_request_at": max(
                    [row.ts for row in records]
                    + [
                        row.reservation_created_at
                        for row in recovered.get(application_id, [])
                    ],
                    default=None,
                ),
            }
            for application_id in sorted(grouped.keys() | recovered.keys(), key=str)
            for records in [grouped.get(application_id, [])]
        ]

    def gateway_application_usage_activity(
        self,
        application_id: UUID,
        from_: datetime,
        to: datetime,
        interval: str,
        timezone: str,
    ) -> Sequence[dict[str, Any]]:
        zone = ZoneInfo(timezone)
        grouped: dict[datetime, list[Any]] = {}
        canonical_correlations = {
            (attribution.application_id, record.correlation_id)
            for record in self.usage_records
            if record.usage_domain == "apim"
            if (attribution := self.usage_application_attributions.get(record.id))
            is not None
        }
        for record in self.usage_records:
            attribution = self.usage_application_attributions.get(record.id)
            if (
                attribution is None
                or attribution.application_id != application_id
                or record.ts < from_
                or record.ts >= to
                or record.usage_domain != "apim"
            ):
                continue
            local = record.ts.astimezone(zone)
            if interval == "week":
                local -= timedelta(days=local.weekday())
            bucket = local.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).astimezone(UTC)
            grouped.setdefault(bucket, []).append(record)
        for recovery in self._application_finalizations():
            if (
                recovery.scope_id != str(application_id)
                or recovery.period_start != from_.date().replace(day=1)
                or recovery.finalization_kind == "unverified_upper_bound"
                or (application_id, recovery.correlation_id)
                in canonical_correlations
            ):
                continue
            local = recovery.reservation_created_at.astimezone(zone)
            if interval == "week":
                local -= timedelta(days=local.weekday())
            bucket = local.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(UTC)
            grouped.setdefault(bucket, []).append(recovery)
        rows: list[dict[str, Any]] = []
        for bucket, records in sorted(grouped.items()):
            latencies = sorted(
                float(getattr(record, "latency_ms", 0)) for record in records
            )
            rank = (len(latencies) - 1) * 0.95
            lower = int(rank)
            upper = min(lower + 1, len(latencies) - 1)
            p95 = latencies[lower] + (latencies[upper] - latencies[lower]) * (
                rank - lower
            )
            rows.append(
                {
                    "bucket_start": bucket,
                    "key": "all",
                    "label": "all",
                    "totals": {
                        "et": sum(
                            record.et if hasattr(record, "et") else record.total_tokens
                            for record in records
                        ),
                        "total_tokens": sum(
                            record.input_tokens
                            + getattr(record, "cached_tokens", 0)
                            + record.output_tokens
                            for record in records
                        ),
                        "input_tokens": sum(record.input_tokens for record in records),
                        "cached_tokens": sum(
                            getattr(record, "cached_tokens", 0) for record in records
                        ),
                        "cache_read_tokens": sum(
                            max(
                                getattr(record, "cached_tokens", 0)
                                - getattr(record, "cache_write_tokens", 0),
                                0,
                            )
                            for record in records
                        ),
                        "cache_write_tokens": sum(
                            getattr(record, "cache_write_tokens", 0)
                            for record in records
                        ),
                        "output_tokens": sum(record.output_tokens for record in records),
                        "calls": len(records),
                        "estimated_cost": sum(
                            getattr(record, "estimated_cost", 0) for record in records
                        ),
                        "p95_latency_ms": p95,
                        "failed_calls": sum(
                            (record.status_code or 0) >= 400 for record in records
                        ),
                    },
                }
            )
        return rows

    def gateway_application_user_usage(
        self,
        period_start: date,
        period_end: date,
        application_id: UUID,
        limit: int = 100,
    ) -> Sequence[dict[str, Any]]:
        grouped: dict[tuple[str, str, str | None, str], list[Any]] = {}
        canonical_correlations = {
            (attribution.application_id, record.correlation_id)
            for record in self.usage_records
            if record.usage_domain == "apim"
            if (attribution := self.usage_application_attributions.get(record.id))
            is not None
        }
        for record in self.usage_records:
            attribution = self.usage_application_attributions.get(record.id)
            if (
                attribution is None
                or attribution.application_id != application_id
                or not period_start <= record.ts.date() < period_end
                or record.usage_domain != "apim"
            ):
                continue
            user_id = attribution.person_id or attribution.actor_id
            key = (
                attribution.actor_type,
                user_id,
                attribution.person_id,
                attribution.actor_id,
            )
            grouped.setdefault(key, []).append(record)
        recovered_key = (
            "system", "system:recovered", None, "system:recovered"
        )
        for recovery in self._application_finalizations():
            if (
            recovery.scope_id == str(application_id)
                and recovery.period_start == period_start
                and recovery.finalization_kind != "unverified_upper_bound"
            and (application_id, recovery.correlation_id)
                not in canonical_correlations
            ):
                grouped.setdefault(recovered_key, []).append(recovery)
        rows: list[dict[str, Any]] = []
        for (actor_type, user_id, person_id, actor_id), records in grouped.items():
            display_name = actor_id
            if actor_type == "person":
                display_name = next(
                    (
                        row.user
                        for row in records
                        if getattr(row, "user", "unattributed") != "unattributed"
                    ),
                    person_id or actor_id,
                )
            elif actor_id == "system:recovered":
                display_name = "Recovered APIM telemetry"
            rows.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "actor_type": actor_type,
                    "person_id": person_id,
                    "request_count": len(records),
                    "denied_request_count": sum(
                        (row.status_code or 0) >= 400 for row in records
                    ),
                    "total_tokens": sum(
                        row.input_tokens
                        + getattr(row, "cached_tokens", 0)
                        + row.output_tokens
                        for row in records
                    ),
                    "estimated_cost": sum(
                        getattr(row, "estimated_cost", 0) for row in records
                    ),
                    "last_request_at": max(
                        row.ts
                        if hasattr(row, "ts")
                        else row.reservation_created_at
                        for row in records
                    ),
                }
            )
        rows.sort(
            key=lambda row: (
                -int(row["request_count"]),
                -int(row["total_tokens"]),
                str(row["display_name"]).casefold(),
                str(row["user_id"]),
            )
        )
        user_count = len(rows)
        return [{**row, "user_count": user_count} for row in rows[:limit]]

    def list_gateway_application_audit(
        self, application_id: UUID, limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        return [
            item
            for item in sorted(
                self.gateway_application_audit,
                key=lambda row: (row["created_at"], str(row["id"])),
                reverse=True,
            )
            if item["application_id"] == application_id
        ][:limit]

    def gateway_application_attribution_map(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]:
        applications = {item["id"]: item for item in self.gateway_applications}
        return [
            {
                "application_id": application["id"],
                "application_name": application["display_name"],
                "application_type": application["application_type"],
                "application_status": application["status"],
                "application_subscription_id": subscription["id"],
                "apim_subscription_id": subscription["apim_subscription_id"],
                "subscription_state": subscription["state"],
                "scope_exists": subscription["scope_exists"],
            }
            for subscription in self.gateway_application_subscriptions
            if subscription["gateway_profile_id"] == gateway_profile_id
            if (application := applications.get(subscription["application_id"])) is not None
        ]

    def gateway_application_ledger_snapshot(
        self, period_start: date, period_end: date
    ) -> Sequence[dict[str, Any]]:
        ids = [item["id"] for item in self.gateway_applications]
        confirmed = {application_id: 0 for application_id in ids}
        confirmed_usd = {application_id: Decimal(0) for application_id in ids}
        usd_available = {application_id: True for application_id in ids}
        canonical_correlations: set[tuple[UUID, str]] = set()
        for record in self.usage_records:
            attribution = self.usage_application_attributions.get(record.id)
            if attribution is None or record.usage_domain != "apim":
                continue
            canonical_correlations.add(
                (attribution.application_id, record.correlation_id)
            )
            if period_start <= record.ts.date() < period_end:
                confirmed[attribution.application_id] = (
                    confirmed.get(attribution.application_id, 0)
                    + record.input_tokens
                    + record.cached_tokens
                    + record.output_tokens
                )
                if record.usd_cost is None:
                    usd_available[attribution.application_id] = False
                else:
                    confirmed_usd[attribution.application_id] += record.usd_cost
        for recovery in self._application_finalizations():
            if (
                recovery.period_start == period_start
                and recovery.finalization_kind != "unverified_upper_bound"
                and (UUID(recovery.scope_id), recovery.correlation_id)
                not in canonical_correlations
            ):
                application_id = UUID(recovery.scope_id)
                confirmed[application_id] = (
                    confirmed.get(application_id, 0)
                    + recovery.total_tokens
                )
                usd_available[application_id] = False
        rows: list[dict[str, Any]] = []
        for (period, application_id), budget in self.gateway_application_budgets.items():
            if period != period_start:
                continue
            application = self.get_gateway_application(application_id)
            if application is None:
                continue
            rows.append(
                {
                    "application_id": application_id,
                    "status": application["status"],
                    "budget_mode": budget.get("budget_mode", "tokens"),
                    "token_limit": budget.get("token_limit"),
                    "usd_limit": budget.get("usd_limit"),
                    "tokens_per_minute": budget["tokens_per_minute"],
                    "enforce": budget["enforce"],
                    "confirmed_tokens": confirmed.get(application_id, 0),
                    "confirmed_usd": (
                        confirmed_usd.get(application_id, Decimal(0))
                        if usd_available.get(application_id, True) else None
                    ),
                    "usd_cost_available": usd_available.get(application_id, True),
                    "quota_updated_at": budget["updated_at"],
                }
            )
        return rows

    def gateway_application_ledger_applications(
        self, period_start: date
    ) -> Sequence[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (period, application_id), budget in self.gateway_application_budgets.items():
            if period != period_start:
                continue
            application = self.get_gateway_application(application_id)
            if application is None:
                continue
            rows.append(
                {
                    "application_id": application_id,
                    "status": application["status"],
                    "budget_mode": budget.get("budget_mode", "tokens"),
                    "token_limit": budget.get("token_limit"),
                    "usd_limit": budget.get("usd_limit"),
                    "tokens_per_minute": budget["tokens_per_minute"],
                    "enforce": budget["enforce"],
                    "quota_updated_at": budget["updated_at"],
                }
            )
        return sorted(rows, key=lambda item: str(item["application_id"]))

    def gateway_application_model_ledger_snapshot(
        self,
    ) -> Sequence[dict[str, Any]]:
        models = {item["id"]: item for item in self.models}
        rows: list[dict[str, Any]] = []
        for application in sorted(self.gateway_applications, key=lambda row: str(row["id"])):
            application_id = application["id"]
            model_ids = sorted(
                self.gateway_application_model_access.get(application_id, set()),
                key=str,
            )
            rows.append(
                {
                    "application_id": application_id,
                    "configured": application_id in self.gateway_application_model_policies,
                    "model_uuids": [str(model_id) for model_id in model_ids],
                    "model_keys": [
                        str(models[model_id]["model_key"])
                        for model_id in model_ids
                        if model_id in models
                    ],
                }
            )
        return rows

    def gateway_application_ledger_mappings(
        self,
    ) -> Sequence[dict[str, Any]]:
        applications = {item["id"]: item for item in self.gateway_applications}
        return [
            {
                "gateway_profile_id": subscription["gateway_profile_id"],
                "application_id": application["id"],
                "application_slug": application["slug"],
                "application_name": application["display_name"],
                "application_type": application["application_type"],
                "application_status": application["status"],
                "department_id": application.get("department_id"),
                "tags": dict(application.get("tags", {})),
                "apim_subscription_id": subscription["apim_subscription_id"],
                "subscription_state": subscription["state"],
                "scope_exists": subscription["scope_exists"],
            }
            for subscription in self.gateway_application_subscriptions
            if (application := applications.get(subscription["application_id"]))
            is not None
        ]

    def roll_forward_gateway_application_budgets(
        self, period_start: date, actor: str
    ) -> int:
        created = 0
        for application in self.gateway_applications:
            application_id = application["id"]
            if application["status"] == "retired":
                continue
            prior = [
                (period, budget)
                for (period, budget_application_id), budget in (
                    self.gateway_application_budgets.items()
                )
                if budget_application_id == application_id and period < period_start
            ]
            if not prior or (period_start, application_id) in self.gateway_application_budgets:
                continue
            _, source = max(prior, key=lambda item: item[0])
            self.gateway_application_budgets[(period_start, application_id)] = {
                **deepcopy(source),
                "period_start": period_start,
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
            }
            created += 1
        return created

    def _write_usage_application_attribution(
        self, usage_id: str, attribution: UsageApplicationAttribution
    ) -> None:
        existing = self.usage_application_attributions.get(usage_id)
        if existing is not None:
            mutable = {"application_admission": None, "application_name_snapshot": ""}
            immutable = existing.model_copy(update=mutable)
            incoming = attribution.model_copy(update=mutable)
            if immutable != incoming:
                raise ValueError("Usage application attribution is immutable")
            if existing.application_admission is None:
                self.usage_application_attributions[usage_id] = existing.model_copy(
                    update={"application_admission": attribution.application_admission}
                )
            return
        self.usage_application_attributions[usage_id] = attribution
