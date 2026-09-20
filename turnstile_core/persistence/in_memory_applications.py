from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from ..domain.application_access import GatewayApplicationLedgerState, UsageApplicationAttribution
from ..domain.ledger import LedgerScopeType


class InMemoryApplicationRepositoryMixin:
    _budget_scope_usage: Callable[[LedgerScopeType, str], list[dict[str, Any]]]
    gateway_application_ledger_states: dict[tuple[date, UUID], dict[str, Any]]
    gateway_applications: list[dict[str, Any]]
    gateway_application_avatars: dict[UUID, dict[str, Any]]
    gateway_application_subscriptions: list[dict[str, Any]]
    gateway_application_budgets: dict[tuple[date, UUID], dict[str, Any]]
    gateway_application_model_policies: dict[UUID, dict[str, Any]]
    gateway_application_model_access: dict[UUID, set[UUID]]
    gateway_application_audit: list[dict[str, Any]]
    gateway_application_attribution: dict[UUID, dict[str, Any]]
    gateway_application_attribution_audit: list[dict[str, Any]]
    usage_application_attributions: dict[str, UsageApplicationAttribution]
    usage_records: list[Any]
    models: list[dict[str, Any]]

    def _record_attribution_sources(
        self,
        application_id: UUID,
        *,
        owner_source: str | None,
        department_source: str | None,
        actor: str,
    ) -> None:
        if owner_source is None and department_source is None:
            return
        current = self.gateway_application_attribution.setdefault(
            application_id,
            {
                "application_id": application_id,
                "owner_source": None,
                "department_source": None,
                "updated_by": actor,
                "updated_at": datetime.now(UTC),
            },
        )
        if owner_source is not None:
            current["owner_source"] = owner_source
        if department_source is not None:
            current["department_source"] = department_source
        current["updated_by"] = actor
        current["updated_at"] = datetime.now(UTC)

    def _fill_blank_attribution_sources(
        self,
        application: dict[str, Any],
        *,
        owner_id: str | None,
        owner_source: str | None,
        department_id: str | None,
        department_source: str | None,
        actor: str,
        now: datetime,
    ) -> None:
        """Fill what is blank; never write over what a person decided. Mirrors the SQL path."""
        sources = self.gateway_application_attribution.get(application["id"], {})
        filled_owner: str | None = None
        filled_department: str | None = None
        if (
            owner_id
            and owner_source is not None
            and not (application.get("owner_id") or "").strip()
            and sources.get("owner_source") != "manual"
        ):
            application["owner_id"] = owner_id
            filled_owner = owner_source
        if (
            department_id
            and department_source is not None
            and not (application.get("department_id") or "").strip()
            and sources.get("department_source") != "manual"
        ):
            application["department_id"] = department_id
            filled_department = department_source
        if filled_owner is None and filled_department is None:
            return
        application["updated_by"] = actor
        application["updated_at"] = now
        self._record_attribution_sources(
            application["id"],
            owner_source=filled_owner,
            department_source=filled_department,
            actor=actor,
        )

    def list_gateway_application_attribution(self) -> Sequence[dict[str, Any]]:
        return [deepcopy(row) for row in self.gateway_application_attribution.values()]

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
                    "owner_id": value.get("owner_id"),
                    "department_id": value.get("department_id"),
                    "application_type": application_type,
                    "status": status,
                    "system_managed": value["system_managed"],
                    "created_by": actor,
                    "created_at": now,
                    "updated_by": actor,
                    "updated_at": now,
                }
                self.gateway_applications.append(application)
                self._record_attribution_sources(
                    application_id,
                    owner_source=value.get("owner_source"),
                    department_source=value.get("department_source"),
                    actor=actor,
                )
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
                self._fill_blank_attribution_sources(
                    application,
                    owner_id=value.get("owner_id"),
                    owner_source=value.get("owner_source"),
                    department_id=value.get("department_id"),
                    department_source=value.get("department_source"),
                    actor=actor,
                    now=now,
                )
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
                and (subscription["state"] != "cancelled" or subscription["scope_exists"])
            ):
                before = deepcopy(subscription)
                subscription.update(state="cancelled", scope_exists=False, last_synced_at=now)
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
            "token_limit": default_token_limit,
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
            if (avatar := self.gateway_application_avatars.get(application_id)) is not None
        ]

    def get_gateway_application_avatar(self, application_id: UUID) -> dict[str, Any] | None:
        avatar = self.gateway_application_avatars.get(application_id)
        return None if avatar is None else deepcopy(avatar)

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
            "token_limit": value["token_limit"],
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
        available = {item["id"] for item in self.models if item.get("enabled", True)}
        if not wanted.issubset(available):
            raise ValueError("Model access includes an unavailable model")
        now = datetime.now(UTC)
        before_policy = deepcopy(self.gateway_application_model_policies.get(application_id))
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

    def update_gateway_application_department(
        self,
        application_id: UUID,
        department_id: str | None,
        actor: str,
    ) -> dict[str, Any] | None:
        application = self.get_gateway_application(application_id)
        if application is None:
            return None
        if application.get("department_id") == department_id:
            return application
        before_state = {"department_id": application.get("department_id")}
        now = datetime.now(UTC)
        application.update(
            department_id=department_id,
            updated_by=actor,
            updated_at=now,
        )
        self.gateway_application_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "operation": "updated",
                "before_state": before_state,
                "after_state": {"department_id": department_id},
                "actor": actor,
                "created_at": now,
            }
        )
        self._mark_attribution_manual(
            application_id, "department",
            previous=before_state["department_id"], new=department_id, actor=actor,
        )
        return application

    def update_gateway_application_owner(
        self,
        application_id: UUID,
        owner_id: str | None,
        actor: str,
    ) -> dict[str, Any] | None:
        application = self.get_gateway_application(application_id)
        if application is None:
            return None
        if (application.get("owner_id") or None) == (owner_id or None):
            return application
        before = application.get("owner_id")
        now = datetime.now(UTC)
        application.update(owner_id=owner_id, updated_by=actor, updated_at=now)
        self.gateway_application_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "operation": "updated",
                "before_state": {"owner_id": before},
                "after_state": {"owner_id": owner_id},
                "actor": actor,
                "created_at": now,
            }
        )
        self._mark_attribution_manual(
            application_id, "owner", previous=before, new=owner_id, actor=actor
        )
        return application

    def _mark_attribution_manual(
        self,
        application_id: UUID,
        field: str,
        *,
        previous: str | None,
        new: str | None,
        actor: str,
    ) -> None:
        now = datetime.now(UTC)
        current = self.gateway_application_attribution.setdefault(
            application_id,
            {
                "application_id": application_id,
                "owner_source": None,
                "department_source": None,
                "updated_by": actor,
                "updated_at": now,
            },
        )
        previous_source = current.get(f"{field}_source")
        current[f"{field}_source"] = "manual"
        current["updated_by"] = actor
        current["updated_at"] = now
        self.gateway_application_attribution_audit.append(
            {
                "id": uuid4(),
                "application_id": application_id,
                "field": field,
                "previous_value": previous,
                "new_value": new,
                "previous_source": previous_source,
                "new_source": "manual",
                "changed_by": actor,
                "changed_at": now,
            }
        )

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

    def save_gateway_application_ledger_states(
        self,
        states: Sequence[GatewayApplicationLedgerState],
    ) -> None:
        for state in states:
            key = (state.period_start, state.application_id)
            previous = self.gateway_application_ledger_states.get(key)
            if previous is None or previous["snapshot_at"] < state.snapshot_at:
                self.gateway_application_ledger_states[key] = state.model_dump()

    def list_gateway_application_ledger_states(
        self,
        period_start: date,
        application_ids: Sequence[UUID],
    ) -> Sequence[dict[str, Any]]:
        return [
            dict(row)
            for (period, application_id), row in self.gateway_application_ledger_states.items()
            if period == period_start and application_id in application_ids
        ]

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
        grouped = {
            application_id: [
                record.model_dump()
                for record in self.usage_records
                if record.usage_domain == "apim"
                and record.id in self.usage_application_attributions
                and self.usage_application_attributions[record.id].application_id == application_id
                and period_start <= record.ts.astimezone(UTC).date() < period_end
            ]
            for application_id in application_ids
        }
        return [
            {
                "application_id": application_id,
                "request_count": len(records),
                "denied_request_count": sum(row["status_code"] >= 400 for row in records),
                "input_tokens": sum(row["input_tokens"] for row in records),
                "cached_tokens": sum(row["cached_tokens"] for row in records),
                "cache_write_tokens": sum(row["cache_write_tokens"] for row in records),
                "output_tokens": sum(row["output_tokens"] for row in records),
                "total_tokens": sum(
                    row["input_tokens"] + row["cached_tokens"] + row["output_tokens"]
                    for row in records
                ),
                "estimated_cost": sum(row["estimated_cost"] for row in records),
                "last_request_at": max((row["ts"] for row in records), default=None),
            }
            for application_id, records in grouped.items()
            if records
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
        for usage in self.usage_records:
            attribution = self.usage_application_attributions.get(usage.id)
            if (
                usage.usage_domain != "apim"
                or attribution is None
                or attribution.application_id != application_id
                or not from_ <= usage.ts < to
            ):
                continue
            record = usage.model_dump()
            local = record["ts"].astimezone(zone)
            if interval == "week":
                local -= timedelta(days=local.weekday())
            bucket = local.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).astimezone(UTC)
            grouped.setdefault(bucket, []).append(record)
        rows: list[dict[str, Any]] = []
        for bucket, records in sorted(grouped.items()):
            latencies = sorted(
                float(record["latency_ms"])
                for record in records
                if record["latency_ms"] is not None
            )
            rank = (len(latencies) - 1) * 0.95
            lower = int(rank)
            upper = min(lower + 1, len(latencies) - 1)
            p95 = (
                latencies[lower] + (latencies[upper] - latencies[lower]) * (rank - lower)
                if latencies
                else 0
            )
            rows.append(
                {
                    "bucket_start": bucket,
                    "key": "all",
                    "label": "all",
                    "totals": {
                        "et": sum(record["et"] for record in records),
                        "total_tokens": sum(
                            record["input_tokens"]
                            + record["cached_tokens"]
                            + record["output_tokens"]
                            for record in records
                        ),
                        "input_tokens": sum(record["input_tokens"] for record in records),
                        "cached_tokens": sum(record["cached_tokens"] for record in records),
                        "cache_read_tokens": sum(
                            max(record["cached_tokens"] - record["cache_write_tokens"], 0)
                            for record in records
                        ),
                        "cache_write_tokens": sum(
                            record["cache_write_tokens"] for record in records
                        ),
                        "output_tokens": sum(record["output_tokens"] for record in records),
                        "calls": len(records),
                        "estimated_cost": sum(record["estimated_cost"] for record in records),
                        "p95_latency_ms": p95,
                        "failed_calls": sum(record["status_code"] >= 400 for record in records),
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
        rows: list[dict[str, Any]] = []
        for (actor_type, user_id, person_id, actor_id), records in grouped.items():
            display_name = actor_id
            if actor_type == "person":
                display_name = next(
                    (row.user for row in records if row.user != "unattributed"),
                    person_id or actor_id,
                )
            rows.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "actor_type": actor_type,
                    "person_id": person_id,
                    "request_count": len(records),
                    "denied_request_count": sum(row.status_code >= 400 for row in records),
                    "total_tokens": sum(
                        row.input_tokens + row.cached_tokens + row.output_tokens for row in records
                    ),
                    "estimated_cost": sum(row.estimated_cost for row in records),
                    "last_request_at": max(row.ts for row in records),
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
                "department_id": application.get("department_id"),
                "owner_id": application.get("owner_id"),
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
        usage = {
            item["application_id"]: item
            for item in self.gateway_application_usage(period_start, period_end, ids)
        }
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
                    "token_limit": budget["token_limit"],
                    "tokens_per_minute": budget["tokens_per_minute"],
                    "enforce": budget["enforce"],
                    "confirmed_tokens": usage.get(application_id, {}).get("total_tokens", 0),
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
                    "token_limit": budget["token_limit"],
                    "tokens_per_minute": budget["tokens_per_minute"],
                    "enforce": budget["enforce"],
                }
            )
        return sorted(rows, key=lambda item: str(item["application_id"]))

    def gateway_application_model_ledger_snapshot(
        self,
    ) -> Sequence[dict[str, Any]]:
        models = {item["id"]: item for item in self.models}
        rows: list[dict[str, Any]] = []
        for application_id in sorted(self.gateway_application_model_policies, key=str):
            model_ids = sorted(
                self.gateway_application_model_access.get(application_id, set()),
                key=str,
            )
            rows.append(
                {
                    "application_id": application_id,
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
                "apim_subscription_id": subscription["apim_subscription_id"],
                "subscription_state": subscription["state"],
                "scope_exists": subscription["scope_exists"],
            }
            for subscription in self.gateway_application_subscriptions
            if (application := applications.get(subscription["application_id"])) is not None
        ]

    def roll_forward_gateway_application_budgets(self, period_start: date, actor: str) -> int:
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
            immutable = existing.model_copy(update={"application_admission": None})
            incoming = attribution.model_copy(update={"application_admission": None})
            if immutable != incoming:
                raise ValueError("Usage application attribution is immutable")
            if existing.application_admission is None:
                self.usage_application_attributions[usage_id] = existing.model_copy(
                    update={"application_admission": attribution.application_admission}
                )
            return
        self.usage_application_attributions[usage_id] = attribution
