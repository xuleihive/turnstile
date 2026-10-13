from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Protocol
from uuid import UUID

from turnstile_core.domain.access_control import (
    AccessActor,
    AccessControlError,
    RequestAuditContext,
    SafeUserState,
)
from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.persistence.access_control_store import AccessControlStore
from turnstile_core.persistence.repository import UsageFilters
from turnstile_core.services.security_audit import SecurityAuditService


class UsageIdentity(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def email(self) -> str: ...

    @property
    def permissions(self) -> frozenset[str]: ...


def scope_usage_filters(identity: UsageIdentity, filters: UsageFilters) -> UsageFilters:
    permissions = identity.permissions
    if "platform.manage" in permissions or "usage.read.all" in permissions:
        return filters
    if "usage.read.own" not in permissions:
        raise AccessControlError(403, "Permission denied")

    own_user_ids = tuple(
        dict.fromkeys(
            value.strip().lower()
            for value in (identity.id, identity.email)
            if value.strip()
        )
    )
    requested_user_ids = (
        tuple(value.strip().lower() for value in filters.user_id if value.strip())
        if isinstance(filters.user_id, tuple)
        else ((filters.user_id or "").strip().lower(),)
    )
    if any(value and value not in own_user_ids for value in requested_user_ids):
        raise AccessControlError(403, "Usage is limited to the current user")
    return replace(filters, user_id=own_user_ids)


class AccessControlService:
    def __init__(
        self, store: AccessControlStore, audit_service: SecurityAuditService | None = None
    ) -> None:
        self._store = store
        self._audit_service = audit_service or SecurityAuditService(store)

    def list_users(
        self,
        *,
        search: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        organization_unit_id: str | None = None,
    ) -> list[dict[str, object]]:
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            return self._store.list_users(
                connection,
                tenant_id,
                search=search,
                role=role,
                enabled=enabled,
                organization_unit_id=organization_unit_id,
            )

    def get_user(self, user_id: UUID) -> dict[str, object]:
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            row = self._store.user_by_id(connection, tenant_id, user_id)
            if row is None:
                raise AccessControlError(404, "User not found")
            return row

    def list_roles(self) -> list[dict[str, object]]:
        with self._store.transaction() as connection:
            return self._store.list_roles(connection)

    def list_organization_units(self) -> list[dict[str, object]]:
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            return self._store.list_organization_units(
                connection, tenant_id
            )

    def create_user(
        self,
        *,
        email: str,
        display_name: str | None,
        role: str,
        organization_unit_id: UUID | None,
        password_hash: str | None,
        actor: AccessActor,
        audit: RequestAuditContext,
    ) -> dict[str, object]:
        normalized_email = email.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized_email):
            raise AccessControlError(422, "A valid email address is required")
        normalized_name = (display_name or "").strip() or None
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            if self._store.user_by_email(connection, tenant_id, normalized_email):
                raise AccessControlError(409, "A user with this email already exists")
            if not self._store.role_exists(connection, role):
                raise AccessControlError(422, "Role does not exist")
            if organization_unit_id and not self._store.organization_unit_by_id(
                connection, tenant_id, organization_unit_id
            ):
                raise AccessControlError(422, "Organization unit does not exist")
            created = self._store.create_user(
                connection,
                tenant_id,
                email=normalized_email,
                display_name=normalized_name,
                role=role,
                organization_unit_id=organization_unit_id,
                password_hash=password_hash,
            )
            self._write_audit(
                connection,
                tenant_id,
                actor,
                audit,
                action="user.created",
                resource_type="user",
                resource_id=str(created["id"]),
                after=SafeUserState.from_row(created).to_dict(),
            )
            return created

    def update_user(
        self,
        user_id: UUID,
        patch: dict[str, object],
        *,
        actor: AccessActor,
        audit: RequestAuditContext,
    ) -> dict[str, object]:
        if not patch:
            raise AccessControlError(422, "At least one user field must be provided")
        clean_patch = dict(patch)
        if "display_name" in clean_patch:
            clean_patch["display_name"] = (str(clean_patch["display_name"] or "").strip() or None)
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            current = self._store.user_by_id(connection, tenant_id, user_id)
            if current is None:
                raise AccessControlError(404, "User not found")
            target_role = str(clean_patch.get("role", current["role"]))
            target_enabled = bool(clean_patch.get("enabled", current["enabled"]))
            if not self._store.role_exists(connection, target_role):
                raise AccessControlError(422, "Role does not exist")
            if current["role"] == "owner" and current["enabled"] and (
                target_role != "owner" or not target_enabled
            ) and self._store.count_enabled_owners(connection, tenant_id) <= 1:
                raise AccessControlError(
                    409, "The last enabled Owner cannot be demoted or disabled"
                )
            unit_id = clean_patch.get("organization_unit_id")
            if unit_id is not None and not self._store.organization_unit_by_id(
                connection, tenant_id, UUID(str(unit_id))
            ):
                raise AccessControlError(422, "Organization unit does not exist")
            updated = self._store.update_user(
                connection, tenant_id, user_id, clean_patch, actor=actor.email
            )
            if updated is None:
                raise AccessControlError(404, "User not found")
            if current["enabled"] and not target_enabled:
                self._store.revoke_user_sessions(connection, user_id)
            self._write_audit(
                connection,
                tenant_id,
                actor,
                audit,
                action="user.updated",
                resource_type="user",
                resource_id=str(user_id),
                before=SafeUserState.from_row(current).to_dict(),
                after=SafeUserState.from_row(updated).to_dict(),
            )
            return updated

    def create_organization_unit(
        self,
        *,
        parent_id: UUID | None,
        code: str,
        display_name: str,
        actor: AccessActor,
        audit: RequestAuditContext,
    ) -> dict[str, object]:
        normalized_code = code.strip().lower()
        normalized_name = display_name.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", normalized_code):
            raise AccessControlError(
                422,
                "Organization unit code must be lowercase letters, digits, or hyphens",
            )
        if not normalized_name:
            raise AccessControlError(422, "Organization unit name is required")
        if parent_id is None:
            raise AccessControlError(422, "A department must be created under the tenant root")
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            if parent_id and not self._store.organization_unit_by_id(
                connection, tenant_id, parent_id
            ):
                raise AccessControlError(422, "Parent organization unit does not exist")
            created = self._store.create_organization_unit(
                connection,
                tenant_id,
                parent_id=parent_id,
                code=normalized_code,
                display_name=normalized_name,
            )
            self._write_audit(
                connection,
                tenant_id,
                actor,
                audit,
                action="organization_unit.created",
                resource_type="organization_unit",
                resource_id=str(created["id"]),
                after={
                    "id": str(created["id"]),
                    "parent_id": str(created["parent_id"]) if created.get("parent_id") else None,
                    "code": created["code"],
                    "display_name": created["display_name"],
                    "enabled": created["enabled"],
                },
            )
            return created

    def update_organization_unit(
        self,
        unit_id: UUID,
        patch: dict[str, object],
        *,
        actor: AccessActor,
        audit: RequestAuditContext,
    ) -> dict[str, object]:
        if not patch:
            raise AccessControlError(422, "At least one organization unit field must be provided")
        clean_patch = dict(patch)
        if "display_name" in clean_patch:
            clean_patch["display_name"] = str(clean_patch["display_name"]).strip()
            if not clean_patch["display_name"]:
                raise AccessControlError(422, "Organization unit name is required")
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            current = self._store.organization_unit_by_id(connection, tenant_id, unit_id)
            if current is None:
                raise AccessControlError(404, "Organization unit not found")
            updated = self._store.update_organization_unit(
                connection, tenant_id, unit_id, clean_patch
            )
            if updated is None:
                raise AccessControlError(404, "Organization unit not found")
            self._write_audit(
                connection,
                tenant_id,
                actor,
                audit,
                action="organization_unit.updated",
                resource_type="organization_unit",
                resource_id=str(unit_id),
                before=self._safe_organization_unit(current),
                after=self._safe_organization_unit(updated),
            )
            return updated

    def delete_organization_unit(
        self,
        unit_id: UUID,
        *,
        actor: AccessActor,
        audit: RequestAuditContext,
    ) -> None:
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            current = self._store.organization_unit_by_id(connection, tenant_id, unit_id)
            if current is None:
                raise AccessControlError(404, "Organization unit not found")
            if current.get("parent_id") is None:
                raise AccessControlError(409, "The tenant root organization unit cannot be deleted")
            if self._store.organization_unit_in_use(connection, tenant_id, unit_id):
                raise AccessControlError(409, "Organization unit has users or child units")
            if not self._store.delete_organization_unit(connection, tenant_id, unit_id):
                raise AccessControlError(404, "Organization unit not found")
            self._write_audit(
                connection,
                tenant_id,
                actor,
                audit,
                action="organization_unit.deleted",
                resource_type="organization_unit",
                resource_id=str(unit_id),
                before={
                    "id": str(current["id"]),
                    "parent_id": str(current["parent_id"]) if current.get("parent_id") else None,
                    "code": current["code"],
                    "display_name": current["display_name"],
                    "enabled": current["enabled"],
                },
            )

    @staticmethod
    def _safe_organization_unit(row: dict[str, Any]) -> dict[str, object]:
        return {
            "id": str(row["id"]),
            "parent_id": str(row["parent_id"]) if row.get("parent_id") else None,
            "code": str(row["code"]),
            "display_name": str(row["display_name"]),
            "enabled": bool(row["enabled"]),
        }

    def _write_audit(
        self,
        connection: object,
        tenant_id: UUID,
        actor: AccessActor,
        audit: RequestAuditContext,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        del tenant_id
        self._audit_service.record(
            AuditEventWrite(
                action=action,
                result="success",
                resource_type=resource_type,
                resource_id=resource_id,
                actor_id=actor.user_id,
                actor_email=actor.email,
                actor_role=actor.role,
                request_id=audit.request_id,
                correlation_id=audit.correlation_id,
                client_ip=audit.client_ip,
                user_agent=audit.user_agent,
                before=before or {},
                after=after or {},
            ),
            connection=connection,
        )
