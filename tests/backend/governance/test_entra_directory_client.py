from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from turnstile_core.domain.directory import DirectoryError
from turnstile_core.integrations.entra_directory import GraphDirectoryClient

TENANT = "10000000-0000-4000-8000-000000000001"
GROUP = UUID("20000000-0000-4000-8000-000000000001")
USER = UUID("30000000-0000-4000-8000-000000000001")


class Tokens:
    def __init__(self) -> None:
        self.calls = 0

    def token(self, resource: str) -> str:
        assert resource == "https://graph.microsoft.com"
        self.calls += 1
        return "test-token"


def client(handler: Any, tokens: Tokens | None = None) -> GraphDirectoryClient:
    return GraphDirectoryClient(
        {"cloud": "public", "tenant_id": TENANT, "authentication_mode": "managed_identity"},
        managed_identity_tenant_id=TENANT,
        token_provider=tokens or Tokens(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def test_paging_ignores_nonhuman_members_and_reads_complete_user() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.path == f"/v1.0/groups/{GROUP}":
            return httpx.Response(200, json={"id": str(GROUP), "visibility": "Private"})
        if request.url.path == f"/v1.0/groups/{GROUP}/members":
            if request.url.params.get("$skiptoken"):
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": str(USER),
                                "@odata.type": "#microsoft.graph.user",
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "device", "@odata.type": "#microsoft.graph.device"}],
                    "@odata.nextLink": f"https://graph.microsoft.com/v1.0/groups/{GROUP}/members?$skiptoken=x",
                },
            )
        assert request.url.path == f"/v1.0/users/{USER}"
        return httpx.Response(
            200,
            json={
                "id": str(USER),
                "displayName": "Example",
                "mail": "example@example.com",
                "accountEnabled": True,
            },
        )

    graph = client(handler)
    assert list(graph.members(GROUP))[0]["id"] == str(USER)
    assert requested.count(f"/v1.0/groups/{GROUP}/members") == 2


def test_next_link_cannot_send_token_to_another_host() -> None:
    graph = client(
        lambda _: httpx.Response(
            200,
            json={
                "value": [],
                "@odata.nextLink": "https://attacker.invalid/v1.0/users",
            },
        )
    )
    with pytest.raises(DirectoryError, match="outside"):
        list(graph.collection("users"))


def test_permission_failure_and_incomplete_users_are_not_empty_success() -> None:
    graph = client(lambda _: httpx.Response(403, json={"error": {"message": "private details"}}))
    with pytest.raises(DirectoryError) as error:
        list(graph.collection("users"))
    assert error.value.code == "graph_permission_denied"
    assert "private details" not in str(error.value)
    incomplete = client(lambda _: httpx.Response(200, json={"id": str(USER)}))
    with pytest.raises(DirectoryError, match="incomplete"):
        incomplete.user(USER)


def test_throttle_and_expired_token_retries_are_bounded() -> None:
    tokens = Tokens()
    responses = iter(
        [
            httpx.Response(401),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"value": []}),
        ]
    )
    graph = client(lambda _: next(responses), tokens)
    assert list(graph.collection("users")) == []
    assert tokens.calls == 2


def test_cross_tenant_managed_identity_and_hidden_groups_fail_closed() -> None:
    with pytest.raises(DirectoryError, match="deployment tenant"):
        GraphDirectoryClient(
            {"cloud": "public", "tenant_id": TENANT, "authentication_mode": "managed_identity"},
            managed_identity_tenant_id="99999999-0000-4000-8000-000000000001",
        )
    graph = client(
        lambda _: httpx.Response(
            200,
            json={
                "id": str(GROUP),
                "visibility": "HiddenMembership",
            },
        )
    )
    with pytest.raises(DirectoryError, match="separately approved"):
        list(graph.members(GROUP))


def test_foreign_cloud_directory_uses_credential_hosting_cloud_vault() -> None:
    calls: list[httpx.Request] = []

    class VaultTokens:
        def token(self, resource: str) -> str:
            assert resource == "https://vault.azure.net"
            return "vault-reader-token"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "example.vault.azure.net":
            return httpx.Response(200, json={"value": "private-credential"})
        if request.url.host == "login.chinacloudapi.cn":
            assert b"private-credential" in request.content
            return httpx.Response(200, json={"access_token": "directory-token"})
        assert request.url.host == "microsoftgraph.chinacloudapi.cn"
        assert request.headers["authorization"] == "Bearer directory-token"
        return httpx.Response(200, json={"value": []})

    graph = GraphDirectoryClient(
        {
            "cloud": "china",
            "tenant_id": TENANT,
            "authentication_mode": "key_vault_secret",
            "client_id": str(GROUP),
            "credential_ref": "https://example.vault.azure.net/secrets/directory",
        },
        managed_identity_tenant_id=TENANT,
        deployment_cloud="public",
        token_provider=VaultTokens(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert list(graph.collection("users")) == []
    assert len(calls) == 3
