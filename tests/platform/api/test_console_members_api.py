"""The console account list, and the two guards that keep an install reachable.

Recovering from either mistake needs shell access to the deployment: `backend.accounts` is a
command rather than a route on purpose, and that same property makes it a poor way to get an
administrator back in on a Friday afternoon.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from backend.api import app
from backend.http.session import get_auth_store
from tests.platform.api.api_support import client

pytest_plugins = ("tests.platform.api.api_fixtures",)

MEMBERS = "/api/v1/organization/members"


class FakeAccounts:
    """The parts of AuthStore these routes touch, with the same guards."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [
            {
                "id": "1", "email": "owner@contoso.com", "display_name": "Owner",
                "role": "owner", "enabled": True, "has_password": True,
                "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                "last_login_at": datetime(2026, 9, 20, tzinfo=UTC),
            },
            {
                "id": "2", "email": "jackson.chen@microsoft.com", "display_name": None,
                "role": "member", "enabled": True, "has_password": False,
                "created_at": datetime(2026, 9, 1, tzinfo=UTC),
                "last_login_at": datetime(2026, 9, 20, tzinfo=UTC),
            },
        ]

    # The session stub the shared fixtures install; sign-in is not what is under test.
    def session_owner(self, token_sha256: str) -> dict[str, object] | None:
        from tests.platform.api.api_support import StubAuthStore

        return StubAuthStore().session_owner(token_sha256)

    def list_users(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def _find(self, email: str) -> dict[str, Any] | None:
        return next(
            (row for row in self.rows if row["email"] == email.strip().lower()), None
        )

    def _enabled_owners(self, excluding: str) -> int:
        return sum(
            1 for row in self.rows
            if row["role"] == "owner" and row["enabled"] and row["email"] != excluding
        )

    def set_user_role(self, email: str, role: str, actor: str) -> dict[str, Any] | None:
        row = self._find(email)
        if row is None:
            return None
        if row["role"] == role:
            return row
        if role != "owner" and row["email"] == actor.strip().lower():
            raise ValueError("You cannot remove your own Owner role")
        if role != "owner" and row["role"] == "owner" and not self._enabled_owners(row["email"]):
            raise ValueError("This is the last Owner; promote someone else first")
        row["role"] = role
        return row

    def set_user_enabled(self, email: str, enabled: bool, actor: str) -> dict[str, Any] | None:
        row = self._find(email)
        if row is None:
            return None
        if row["enabled"] == enabled:
            return row
        if not enabled and row["email"] == actor.strip().lower():
            raise ValueError("You cannot disable your own account")
        if not enabled and row["role"] == "owner" and not self._enabled_owners(row["email"]):
            raise ValueError("This is the last Owner; promote someone else first")
        row["enabled"] = enabled
        return row


@pytest.fixture
def accounts() -> Any:
    store = FakeAccounts()
    app.dependency_overrides[get_auth_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_auth_store, None)


def test_the_list_shows_who_can_sign_in_and_how(accounts: FakeAccounts) -> None:
    """The screen that explains an empty toolbar.

    Someone who signed in with Microsoft and found every control missing had no way to see
    that they were a member, who could change that, or that they were in the system at all.
    """
    body = client.get(MEMBERS).json()
    by_email = {item["email"]: item for item in body["members"]}
    assert by_email["owner@contoso.com"]["role"] == "owner"
    assert by_email["owner@contoso.com"]["sign_in"] == "password"
    assert by_email["owner@contoso.com"]["is_self"] is True
    # Provisioned on first Microsoft sign-in: no password, member role, invisible until now.
    assert by_email["jackson.chen@microsoft.com"]["sign_in"] == "microsoft"
    assert by_email["jackson.chen@microsoft.com"]["role"] == "member"
    assert by_email["jackson.chen@microsoft.com"]["is_self"] is False
    assert body["owner_count"] == 1


def test_an_owner_can_promote_a_microsoft_account(accounts: FakeAccounts) -> None:
    response = client.put(
        f"{MEMBERS}/jackson.chen@microsoft.com/role", json={"role": "owner"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["owner_count"] == 2
    assert {item["email"]: item["role"] for item in body["members"]}[
        "jackson.chen@microsoft.com"
    ] == "owner"


def test_the_last_owner_cannot_be_demoted_or_disabled(accounts: FakeAccounts) -> None:
    accounts.rows[0]["email"] = "someone.else@contoso.com"  # not the caller
    demote = client.put(
        f"{MEMBERS}/someone.else@contoso.com/role", json={"role": "member"}
    )
    assert demote.status_code == 422
    assert "last Owner" in demote.json()["detail"]
    disable = client.put(
        f"{MEMBERS}/someone.else@contoso.com/status", json={"enabled": False}
    )
    assert disable.status_code == 422
    assert "last Owner" in disable.json()["detail"]
    assert accounts.rows[0]["role"] == "owner"
    assert accounts.rows[0]["enabled"] is True


def test_you_cannot_remove_your_own_way_back_in(accounts: FakeAccounts) -> None:
    """Refused even with another Owner present.

    The account that could undo it belongs to someone else, so the repair is a phone call --
    and the action reads as routine right up until it is not.
    """
    accounts.rows.append({
        "id": "3", "email": "second.owner@contoso.com", "display_name": None,
        "role": "owner", "enabled": True, "has_password": True,
        "created_at": datetime(2026, 9, 1, tzinfo=UTC), "last_login_at": None,
    })
    demote = client.put(f"{MEMBERS}/owner@contoso.com/role", json={"role": "member"})
    assert demote.status_code == 422
    assert "your own Owner role" in demote.json()["detail"]
    disable = client.put(f"{MEMBERS}/owner@contoso.com/status", json={"enabled": False})
    assert disable.status_code == 422
    assert "your own account" in disable.json()["detail"]


def test_an_unknown_account_is_a_404(accounts: FakeAccounts) -> None:
    response = client.put(f"{MEMBERS}/nobody@contoso.com/role", json={"role": "owner"})
    assert response.status_code == 404


def test_members_can_read_the_list_but_not_change_it(accounts: FakeAccounts) -> None:
    from backend.http.session import SessionIdentity, require_authenticated_session

    app.dependency_overrides[require_authenticated_session] = lambda: SessionIdentity(
        id="2",
        email="jackson.chen@microsoft.com",
        name=None,
        role="member",
        method="entra",
        session_expires_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    try:
        assert client.get(MEMBERS).status_code == 200
        assert client.put(
            f"{MEMBERS}/owner@contoso.com/role", json={"role": "member"}
        ).status_code == 403
        assert client.put(
            f"{MEMBERS}/owner@contoso.com/status", json={"enabled": False}
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(require_authenticated_session, None)
