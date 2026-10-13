from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any, Protocol, cast
from uuid import UUID

from turnstile_core.domain.security_audit import AuditEventWrite, sanitize_audit_metadata


class AuditWriter(Protocol):
    def transaction(self) -> Any: ...

    def primary_tenant_id(self, connection: Any) -> UUID: ...

    def write_audit_event(self, connection: Any, **event: Any) -> None: ...


class AuditRepository(AuditWriter, Protocol):
    def list_events(
        self, connection: Any, tenant_id: UUID, **filters: Any
    ) -> list[dict[str, Any]]: ...

    def delete_expired_events(self, connection: Any, cutoff: datetime) -> int: ...


@dataclass(frozen=True)
class AuditEventFilters:
    from_: datetime | None = None
    to: datetime | None = None
    actor_id: UUID | None = None
    action: str | None = None
    result: str | None = None


class SecurityAuditService:
    def __init__(self, store: AuditWriter) -> None:
        self._store = store

    def record(self, event: AuditEventWrite, connection: Any | None = None) -> None:
        if connection is None:
            with self._store.transaction() as transaction:
                self.record(event, connection=transaction)
            return

        action = event.action.strip()
        resource_type = event.resource_type.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,159}", action):
            raise ValueError("Invalid security audit action")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", resource_type):
            raise ValueError("Invalid security audit resource type")
        if event.result not in {"success", "failure", "denied"}:
            raise ValueError("Invalid security audit result")

        client_ip: str | None = None
        if event.client_ip:
            try:
                client_ip = str(ip_address(event.client_ip))
            except ValueError:
                client_ip = None
        actor_user_id = self._uuid_or_none(event.actor_id)
        tenant_id = self._store.primary_tenant_id(connection)
        self._store.write_audit_event(
            connection,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_email=(event.actor_email or "")[:320] or None,
            actor_role=(event.actor_role or "")[:64] or None,
            action=action,
            resource_type=resource_type,
            resource_id=(event.resource_id or "")[:256] or None,
            result=event.result,
            request_id=(event.request_id or "")[:128] or None,
            correlation_id=(event.correlation_id or "")[:128] or None,
            client_ip=client_ip,
            user_agent=(event.user_agent or "")[:500] or None,
            before_data=sanitize_audit_metadata(event.before),
            after_data=sanitize_audit_metadata(event.after),
        )

    def list_events(
        self,
        filters: AuditEventFilters,
        cursor: UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._store.transaction() as connection:
            tenant_id = self._store.primary_tenant_id(connection)
            repository = cast(AuditRepository, self._store)
            return repository.list_events(
                connection,
                tenant_id,
                from_=filters.from_,
                to=filters.to,
                actor_id=filters.actor_id,
                action=filters.action,
                result=filters.result,
                cursor=cursor,
                limit=limit,
            )

    def delete_expired_before(self, cutoff: datetime) -> int:
        with self._store.transaction() as connection:
            repository = cast(AuditRepository, self._store)
            return repository.delete_expired_events(connection, cutoff)

    @staticmethod
    def _uuid_or_none(value: str | None) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(value)
        except ValueError:
            return None
