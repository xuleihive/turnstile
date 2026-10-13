from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from turnstile_core.config import Settings
from turnstile_core.domain.security_audit import (
    AuditEventWrite,
    resolve_client_ip,
    sanitize_audit_metadata,
)
from turnstile_core.persistence.audit_store import AuditStore
from turnstile_core.services.security_audit import SecurityAuditService

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")


class FakeAuditRepository:
    def __init__(self) -> None:
        self.transaction_count = 0
        self.events: list[dict[str, Any]] = []
        self.expired_rows: list[datetime] = []

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        yield self

    def primary_tenant_id(self, connection: object) -> UUID:
        return TENANT_ID

    def write_audit_event(self, connection: object, **event: Any) -> None:
        self.events.append(event)

    def list_events(self, connection: object, tenant_id: UUID, **filters: Any):
        return []

    def delete_expired_events(self, connection: object, cutoff: datetime) -> int:
        original_count = len(self.expired_rows)
        self.expired_rows = [created_at for created_at in self.expired_rows if created_at >= cutoff]
        return original_count - len(self.expired_rows)


def test_audit_proxy_trust_is_disabled_without_explicit_configuration() -> None:
    fields = Settings.model_fields

    assert "trust_azure_arr_client_ip" in fields
    assert "trusted_proxy_cidrs" in fields
    settings = Settings()
    assert settings.trust_azure_arr_client_ip is False
    assert settings.trusted_proxy_cidrs == []


def test_audit_proxy_cidr_configuration_rejects_invalid_networks() -> None:
    with pytest.raises(ValueError):
        Settings(trusted_proxy_cidrs=["not-a-network"])


def test_client_ip_ignores_forwarding_headers_from_an_untrusted_peer() -> None:
    assert (
        resolve_client_ip(
            peer_ip="198.51.100.8",
            headers={"X-Forwarded-For": "203.0.113.77"},
            trusted_proxy_cidrs=["10.0.0.0/8"],
        )
        == "198.51.100.8"
    )


def test_client_ip_reads_azure_arr_header_only_when_enabled() -> None:
    headers = {"X-ARR-ClientIP": "203.0.113.42"}

    assert resolve_client_ip(peer_ip="10.0.0.4", headers=headers) == "10.0.0.4"
    assert (
        resolve_client_ip(peer_ip="10.0.0.4", headers=headers, trust_azure_arr_client_ip=True)
        == "203.0.113.42"
    )


def test_client_ip_discards_untrusted_prefix_from_a_trusted_proxy_chain() -> None:
    assert (
        resolve_client_ip(
            peer_ip="10.0.0.10",
            headers={"X-Forwarded-For": "198.51.100.99, 203.0.113.42"},
            trusted_proxy_cidrs=["10.0.0.0/24"],
        )
        == "203.0.113.42"
    )


def test_audit_metadata_allowlist_drops_credentials_and_request_bodies() -> None:
    safe = sanitize_audit_metadata(
        {
            "email": "user@example.com",
            "role": "member",
            "password": "secret",
            "cookie": "session-secret",
            "authorization": "bearer-secret",
            "api_key": "subscription-secret",
            "prompt": "do not store",
            "request_body": {"messages": ["private"]},
            "subscription_id": "sub-123",
        }
    )

    assert safe == {
        "email": "user@example.com",
        "role": "member",
        "subscription_id": "sub-123",
    }


def test_audit_record_reuses_caller_transaction_and_filters_sensitive_metadata() -> None:
    store = FakeAuditRepository()
    service = SecurityAuditService(store)
    connection = object()
    event = AuditEventWrite(
        action="user.created",
        result="success",
        resource_type="user",
        actor_id="00000000-0000-4000-8000-000000000002",
        actor_email="owner@example.com",
        actor_role="owner",
        client_ip="203.0.113.8",
        before={"password": "do-not-keep"},
        after={
            "email": "new@example.com",
            "role": "member",
            "password_hash": "do-not-keep",
            "prompt": "private content",
        },
    )

    service.record(event, connection=connection)

    assert store.transaction_count == 0
    assert store.events[0]["tenant_id"] == TENANT_ID
    assert store.events[0]["actor_user_id"] == UUID("00000000-0000-4000-8000-000000000002")
    assert store.events[0]["after_data"] == {
        "email": "new@example.com",
        "role": "member",
    }
    assert "do-not-keep" not in str(store.events)
    assert store.events[0]["client_ip"] == "203.0.113.8"


def test_retention_deletes_events_strictly_older_than_365_day_cutoff() -> None:
    store = FakeAuditRepository()
    service = SecurityAuditService(store)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    cutoff = now - timedelta(days=365)
    store.expired_rows = [cutoff - timedelta(microseconds=1), cutoff, cutoff + timedelta(days=1)]

    deleted = service.delete_expired_before(cutoff)

    assert deleted == 1
    assert store.expired_rows == [cutoff, cutoff + timedelta(days=1)]


def test_audit_store_uses_strict_retention_boundary() -> None:
    class Cursor:
        rowcount = 1

    class Connection:
        statement = ""
        parameters: tuple[object, ...] = ()

        def execute(self, statement: str, parameters: tuple[object, ...]) -> Cursor:
            self.statement = statement
            self.parameters = parameters
            return Cursor()

    connection = Connection()
    cutoff = datetime(2025, 10, 12, tzinfo=UTC)

    deleted = AuditStore.delete_expired_events(connection, cutoff)

    assert deleted == 1
    assert "created_at < %s" in connection.statement
    assert connection.parameters == (cutoff,)
