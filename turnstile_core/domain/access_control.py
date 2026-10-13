from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PERMISSION_CODES = (
    "platform.manage",
    "users.read",
    "users.manage",
    "org.read",
    "org.manage",
    "roles.read",
    "applications.read.all",
    "applications.read.own",
    "keys.manage.all",
    "keys.manage.own",
    "usage.read.all",
    "usage.read.own",
    "reports.create.all",
    "reports.create.own",
    "security_audit.read",
)


class AccessControlError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class AccessActor:
    user_id: str
    email: str
    role: str


@dataclass(frozen=True)
class RequestAuditContext:
    request_id: str | None = None
    correlation_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class SafeUserState:
    id: str
    email: str
    display_name: str | None
    role: str
    enabled: bool
    organization_unit_id: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SafeUserState:
        return cls(
            id=str(row["id"]),
            email=str(row["email"]),
            display_name=row.get("display_name"),
            role=str(row["role"]),
            enabled=bool(row["enabled"]),
            organization_unit_id=(
                str(row["organization_unit_id"])
                if row.get("organization_unit_id") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "enabled": self.enabled,
            "organization_unit_id": self.organization_unit_id,
        }
