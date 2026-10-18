"""Read-only Microsoft Graph connector with fixed cloud authorities and safe paging."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx

from ..domain.directory import DirectoryError
from .reconciliation import ManagedIdentityTokenProvider

CLOUD_ENDPOINTS = {
    "public": (
        "https://graph.microsoft.com",
        "https://login.microsoftonline.com",
        ".vault.azure.net",
    ),
    "usgov": (
        "https://graph.microsoft.us",
        "https://login.microsoftonline.us",
        ".vault.usgovcloudapi.net",
    ),
    "china": (
        "https://microsoftgraph.chinacloudapi.cn",
        "https://login.chinacloudapi.cn",
        ".vault.azure.cn",
    ),
}
USER_FIELDS = "id,displayName,userPrincipalName,mail,accountEnabled,userType,department,jobTitle"


class TokenProvider(Protocol):
    def token(self, resource: str) -> str: ...


class GraphDirectoryClient:
    def __init__(
        self,
        connection: Mapping[str, Any],
        *,
        managed_identity_tenant_id: str | None,
        deployment_cloud: str = "public",
        token_provider: TokenProvider | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.connection = connection
        self.cloud = str(connection["cloud"])
        if self.cloud not in CLOUD_ENDPOINTS:
            raise DirectoryError("unsupported_cloud", "Unsupported directory cloud", 422)
        self.graph, self.authority, self.vault_suffix = CLOUD_ENDPOINTS[self.cloud]
        if deployment_cloud not in CLOUD_ENDPOINTS:
            raise DirectoryError("unsupported_cloud", "Unsupported credential-hosting cloud", 422)
        self.vault_suffix = CLOUD_ENDPOINTS[deployment_cloud][2]
        self.deployment_cloud = deployment_cloud
        self.tenant_id = str(UUID(str(connection["tenant_id"])))
        self.provider = token_provider or ManagedIdentityTokenProvider()
        if connection["authentication_mode"] == "managed_identity" and (
            not managed_identity_tenant_id
            or self.tenant_id != str(UUID(managed_identity_tenant_id))
            or deployment_cloud != self.cloud
        ):
            raise DirectoryError(
                "managed_identity_tenant_mismatch",
                "Managed identity synchronization is limited to the deployment tenant and cloud",
                422,
            )
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None
        self.sleep = sleep
        self._token: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _credential_token(self) -> str:
        try:
            return self._load_credential_token()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, DirectoryError):
                raise
            raise DirectoryError(
                "directory_auth_failed",
                "Directory credential could not be loaded",
                503,
            ) from error

    def _load_credential_token(self) -> str:
        if self.connection["authentication_mode"] == "managed_identity":
            return self.provider.token(self.graph)
        reference = str(self.connection.get("credential_ref") or "")
        parsed = urlparse(reference)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(self.vault_suffix)
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/secrets/")
            or len(parsed.path.strip("/").split("/")) not in {2, 3}
        ):
            raise DirectoryError(
                "invalid_credential_reference", "Use an HTTPS Key Vault secret reference", 422
            )
        vault_resource = {
            "public": "https://vault.azure.net",
            "usgov": "https://vault.usgovcloudapi.net",
            "china": "https://vault.azure.cn",
        }[self.deployment_cloud]
        response = self.client.get(
            reference,
            params={"api-version": "7.4"},
            headers={"Authorization": f"Bearer {self.provider.token(vault_resource)}"},
        )
        if response.status_code != 200:
            raise DirectoryError(
                "credential_unavailable", "Directory credential reference is not accessible", 503
            )
        secret = self._credential_payload(response).get("value")
        if not isinstance(secret, str) or not secret:
            raise DirectoryError(
                "credential_unavailable", "Directory credential is unavailable", 503
            )
        token = self.client.post(
            f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": str(self.connection["client_id"]),
                "client_secret": secret,
                "scope": f"{self.graph}/.default",
                "grant_type": "client_credentials",
            },
        )
        if token.status_code != 200:
            raise DirectoryError(
                "directory_auth_failed", "Directory application authentication failed", 503
            )
        value = self._credential_payload(token).get("access_token")
        if not isinstance(value, str) or not value:
            raise DirectoryError("directory_auth_failed", "Directory token is unavailable", 503)
        return value

    @staticmethod
    def _credential_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as error:
            raise DirectoryError(
                "directory_auth_failed",
                "Directory credential response is malformed",
                503,
            ) from error
        if not isinstance(value, dict):
            raise DirectoryError(
                "directory_auth_failed",
                "Directory credential response is malformed",
                503,
            )
        return value

    def _safe_url(self, path: str) -> str:
        url = path if path.startswith("https://") else f"{self.graph}/v1.0/{path.lstrip('/')}"
        parsed = urlparse(url)
        expected = urlparse(self.graph)
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.path.startswith("/v1.0/")
        ):
            raise DirectoryError(
                "unsafe_graph_link", "Graph paging link is outside the configured cloud"
            )
        return url

    def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        url = self._safe_url(path)
        for attempt in range(4):
            if self._token is None:
                self._token = self._credential_token()
            try:
                response = self.client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            except httpx.HTTPError as error:
                raise DirectoryError(
                    "graph_unavailable", "Directory request failed", 503
                ) from error
            if response.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if response.status_code in {429, 503} and attempt < 3:
                retry = response.headers.get("Retry-After", "1")
                delay = min(float(retry), 60) if retry.isdigit() else min(2**attempt, 30)
                self.sleep(delay + attempt * 0.25)
                continue
            if response.status_code == 403:
                raise DirectoryError(
                    "graph_permission_denied",
                    "Directory read permission or administrator consent is missing",
                    403,
                )
            if response.status_code == 410:
                raise DirectoryError(
                    "graph_delta_expired", "Directory delta checkpoint has expired", 409
                )
            if response.status_code != 200:
                raise DirectoryError(
                    "graph_request_failed", "Directory request did not complete", 503
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise DirectoryError(
                    "graph_invalid_response", "Directory response is not valid JSON", 503
                ) from error
            if not isinstance(payload, dict):
                raise DirectoryError(
                    "graph_invalid_response", "Directory response is malformed", 503
                )
            return payload
        raise DirectoryError("graph_throttled", "Directory request remains throttled", 503)

    def collection_page(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        payload = self.get(path, params)
        items = payload.get("value")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise DirectoryError("graph_incomplete_page", "Directory collection is incomplete", 503)
        next_link, delta_link = payload.get("@odata.nextLink"), payload.get("@odata.deltaLink")
        for link in (next_link, delta_link):
            if link is not None:
                if not isinstance(link, str):
                    raise DirectoryError(
                        "graph_invalid_link", "Directory paging link is malformed", 503
                    )
                self._safe_url(link)
        if next_link is not None and delta_link is not None:
            raise DirectoryError(
                "graph_invalid_link", "Directory page has conflicting cursors", 503
            )
        return items, next_link, delta_link

    def collection(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        visited: set[str] = set()
        next_path: str | None = path
        for _ in range(5000):
            if next_path is None:
                return
            url = self._safe_url(next_path)
            if url in visited:
                raise DirectoryError("graph_paging_loop", "Directory paging did not advance", 503)
            visited.add(url)
            payload = self.get(url, params)
            params = None
            items = payload.get("value")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise DirectoryError(
                    "graph_incomplete_page", "Directory collection is incomplete", 503
                )
            yield from items
            next_path = payload.get("@odata.nextLink")
            if next_path is not None and not isinstance(next_path, str):
                raise DirectoryError(
                    "graph_invalid_link", "Directory paging link is malformed", 503
                )
        raise DirectoryError("graph_page_limit", "Directory page limit was exceeded", 503)

    def group(self, group_id: UUID) -> dict[str, Any]:
        value = self.get(f"groups/{group_id}", {"$select": "id,displayName,visibility,groupTypes"})
        if value.get("id") != str(group_id):
            raise DirectoryError(
                "graph_invalid_group", "Directory Group identity is incomplete", 503
            )
        return value

    def delta(
        self,
        resource: str,
        *,
        checkpoint: str | None = None,
        params: Mapping[str, str] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        next_path = checkpoint or f"{resource}/delta"
        results: list[dict[str, Any]] = []
        visited: set[str] = set()
        for _ in range(5000):
            if heartbeat:
                heartbeat()
            url = self._safe_url(next_path)
            if url in visited:
                raise DirectoryError(
                    "graph_paging_loop", "Directory delta paging did not advance", 503
                )
            visited.add(url)
            payload = self.get(url, params if checkpoint is None else None)
            params = None
            items = payload.get("value")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise DirectoryError(
                    "graph_incomplete_page", "Directory delta response is incomplete", 503
                )
            results.extend(items)
            if len(results) > 200_000:
                raise DirectoryError(
                    "graph_object_limit", "Directory delta object limit exceeded", 503
                )
            next_link = payload.get("@odata.nextLink")
            if isinstance(next_link, str):
                next_path = next_link
                continue
            delta_link = payload.get("@odata.deltaLink")
            if not isinstance(delta_link, str):
                raise DirectoryError(
                    "graph_checkpoint_missing", "Directory delta checkpoint is missing", 503
                )
            self._safe_url(delta_link)
            return results, delta_link
        raise DirectoryError("graph_page_limit", "Directory delta page limit exceeded", 503)

    def user(self, object_id: UUID) -> dict[str, Any]:
        payload = self.get(f"users/{object_id}", {"$select": USER_FIELDS})
        if str(payload.get("id")) != str(object_id) or not isinstance(
            payload.get("accountEnabled"), bool
        ):
            raise DirectoryError(
                "graph_incomplete_user", "Directory user fields are incomplete", 503
            )
        return payload

    def members(self, group_id: UUID, *, transitive: bool = False) -> Iterator[dict[str, Any]]:
        group = self.group(group_id)
        if group.get("visibility") == "HiddenMembership":
            raise DirectoryError(
                "hidden_group_not_supported",
                "Hidden group synchronization requires a separately approved permission policy",
                422,
            )
        path = "transitiveMembers" if transitive else "members"
        for member in self.collection(f"groups/{group_id}/{path}", {"$select": "id"}):
            if not isinstance(member.get("@odata.type"), str):
                raise DirectoryError(
                    "graph_incomplete_member",
                    "Directory member type is incomplete",
                    503,
                )
            if member.get("@odata.type") != "#microsoft.graph.user":
                continue
            try:
                object_id = UUID(str(member["id"]))
            except (ValueError, KeyError) as error:
                raise DirectoryError(
                    "graph_invalid_user", "Directory member identity is malformed", 503
                ) from error
            yield self.user(object_id)
