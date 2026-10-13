# Shared client, authentication stub, and fixtures for platform API tests.

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from backend.api import (
    app,
)
from backend.services.auth_service import hash_session_token
from turnstile_core.domain.models import (
    TokenUsageRecord,
)

client = TestClient(app)

OWNER_SESSION = "test-owner-session"

MEMBER_SESSION = "test-member-session"

class StubAuthStore:
    def __init__(self, role: str | None = None, enabled: bool = True) -> None:
        self.role = role
        self.enabled = enabled

    @staticmethod
    def _permissions(role: str) -> list[str]:
        if role == "owner":
            return [
                "platform.manage", "users.read", "users.manage", "org.read",
                "org.manage", "roles.read", "applications.read.all",
                "applications.read.own", "keys.manage.all", "keys.manage.own",
                "usage.read.all", "usage.read.own", "reports.create.all",
                "reports.create.own", "security_audit.read",
            ]
        return [
            "applications.read.own", "keys.manage.own", "usage.read.own",
            "reports.create.own",
        ]

    def session_owner(self, token_sha256: str) -> dict[str, object] | None:
        if not self.enabled:
            return None
        owner_role = self.role or "owner"
        member_role = self.role or "member"
        sessions: dict[str, dict[str, object]] = {
            hash_session_token(OWNER_SESSION): {
                "id": "00000000-0000-4000-8000-000000000001",
                "email": "owner@contoso.com",
                "display_name": "Owner",
                "role": owner_role,
                "permissions": self._permissions(owner_role),
                "method": "password",
                "created_at": datetime(2026, 8, 10, tzinfo=UTC),
                "expires_at": datetime(2026, 8, 17, tzinfo=UTC),
            },
            hash_session_token(MEMBER_SESSION): {
                "id": "00000000-0000-4000-8000-000000000002",
                "email": "member@contoso.com",
                "display_name": "Member",
                "role": member_role,
                "permissions": self._permissions(member_role),
                "method": "password",
                "created_at": datetime(2026, 8, 10, tzinfo=UTC),
                "expires_at": datetime(2026, 8, 17, tzinfo=UTC),
            },
        }
        return sessions.get(token_sha256)

def _usage_record(
    usage_id: str,
    runtime: str,
    tokens: int,
    *,
    latency_ms: int = 100,
    status_code: int = 200,
) -> TokenUsageRecord:
    return TokenUsageRecord(
        id=usage_id,
        request_id=f"request-{usage_id}",
        correlation_id=f"correlation-{usage_id}",
        ts=datetime(2026, 7, 20, tzinfo=UTC),
        team="AI Platform",
        organization="Contoso Global",
        organization_id="org-contoso-global",
        department="AI Platform",
        department_id="department-platform",
        project="Model FinOps",
        project_id="project-finops",
        user="Test User 01",
        user_id="test.user01@contoso.com",
        agent="Delivery Engineer",
        agent_id="agent-delivery",
        workflow="usage-validation",
        run_id=f"run-{usage_id}",
        turn_index=1,
        provider="microsoft_foundry",
        model="gpt-5.6-luna",
        model_id="gpt-5.6-luna",
        runtime=runtime,
        request_source="agent-console",
        input_tokens=tokens,
        cached_tokens=0,
        output_tokens=0,
        et=1,
        et_coeff_m=1,
        latency_ms=latency_ms,
        status="success" if status_code < 400 else "error",
        status_code=status_code,
        estimated_cost=0.001,
        estimated=False,
        ingest_source="gateway",
    )
