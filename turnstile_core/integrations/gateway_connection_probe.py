from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import HttpUrl

from .apim_control_plane_contract import PolicyCompilationError, RetryablePublicationError


def verify_pool_deployment_observer(client: httpx.Client, origin: str, credential: str) -> None:
    address = urlsplit(origin)
    if (address.scheme != "https" or not address.hostname or address.username or address.password
            or address.query or address.fragment or address.path not in {"", "/"}):
        raise PolicyCompilationError("Deployment Pool Observer origin is invalid")
    try:
        response = client.post(
            origin.rstrip("/") + "/_turnstile/pool-deployment-probe",
            headers={"x-adapter-key": credential,
                     "x-turnstile-upstream-host": "127.0.0.1:1",
                     "x-turnstile-pool-deployment": "turnstile-deployment-probe"},
            json={"model": "turnstile-original-probe"},
            timeout=5, follow_redirects=False,
        )
        valid = (response.status_code == 200
                 and response.headers.get("x-turnstile-pool-deployment-version") == "1"
                 and response.json() == {"model": "turnstile-deployment-probe"})
    except (httpx.HTTPError, ValueError):
        valid = False
    if not valid:
        raise PolicyCompilationError(
            "Deployment Pool Observer requires a deployment rewrite upgrade"
        )


def foundry_model_identity(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    values = [(metadata or {}).get(name)
              for name in ("modelPublisher", "modelName", "modelVersion")]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise PolicyCompilationError("Foundry deployment model identity is unavailable")
    return tuple(str(value).strip().casefold() for value in values)


def foundry_deployment_catalog(
    client: httpx.Client, project: str, backend_url: str, deployment: str,
    headers: Callable[[], Mapping[str, str]],
) -> dict[str, Any]:
    address, origin = urlsplit(project), urlsplit(backend_url)
    if (
        address.scheme != "https" or address.username or address.password
        or address.query or address.fragment or address.port is not None
        or not re.fullmatch(r"/api/projects/[a-zA-Z0-9_.-]{1,128}", address.path)
        or not address.hostname or not origin.hostname
        or not address.hostname.endswith(".services.ai.azure.com")
        or address.hostname.removesuffix(".services.ai.azure.com")
        != origin.hostname.removesuffix(".openai.azure.com").removesuffix(".services.ai.azure.com")
    ):
        raise PolicyCompilationError("Foundry deployment metadata target does not match connection")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", deployment):
        raise PolicyCompilationError("Foundry deployment name is invalid")
    try:
        request_headers = headers()
        with client.stream(
            "GET", project.rstrip("/") + "/deployments", params={"api-version": "v1"},
            headers=request_headers, timeout=5, follow_redirects=False,
        ) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > 1024 * 1024:
                    raise PolicyCompilationError("Deployment metadata response exceeds its bound")
                raw.extend(chunk)
            if response.status_code != 200:
                details: dict[str, Any] = {
                    "code": "deployment_metadata_unavailable", "deployment": deployment,
                    "http_status": response.status_code,
                }
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                error = body.get("error") if isinstance(body, dict) else None
                upstream = error.get("code") if isinstance(error, dict) else None
                if isinstance(upstream, str) and re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_.-]{0,63}", upstream,
                ):
                    details["upstream_code"] = upstream
                if "Authorization" in request_headers:
                    details["auth"] = "managed_identity"
                raise PolicyCompilationError(
                    "Deployment validation failed: " + json.dumps(details),
                )
            document = json.loads(raw)
    except PolicyCompilationError:
        raise
    except httpx.HTTPError:
        raise RetryablePublicationError("Deployment metadata transport is unavailable") from None
    except (ValueError, TypeError):
        raise PolicyCompilationError("Deployment metadata response is invalid") from None
    items = document.get("value") if isinstance(document, dict) else None
    if (
        not isinstance(items, list) or len(items) > 10000 or document.get("nextLink")
        or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
               for item in items)
    ):
        raise PolicyCompilationError("Deployment metadata is incomplete or invalid")
    catalog = {item["name"]: dict(item) for item in items}
    if len(catalog) != len(items):
        raise PolicyCompilationError("Deployment metadata is ambiguous")
    return catalog


def foundry_deployment_metadata(
    client: httpx.Client, project: str, backend_url: str, deployment: str,
    headers: Callable[[], Mapping[str, str]],
) -> dict[str, Any]:
    catalog = foundry_deployment_catalog(client, project, backend_url, deployment, headers)
    if deployment not in catalog:
        raise PolicyCompilationError("Deployment validation failed: " + json.dumps({
            "code": "deployment_not_found", "deployment": deployment, "http_status": 404,
        }))
    return dict(catalog[deployment])


class GatewayConnectionProbeError(RuntimeError):
    pass


class GatewayConnectionProbe:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

    def validate(
        self,
        endpoint_url: HttpUrl,
        *,
        header_name: str,
        credential: str,
    ) -> None:
        url = f"{str(endpoint_url).rstrip('/')}/v1/models"
        headers = {header_name: credential}
        try:
            if self._client is not None:
                response = self._client.get(url, headers=headers, follow_redirects=False)
            else:
                with httpx.Client(
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = client.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise GatewayConnectionProbeError(
                "Self-hosted Gateway endpoint could not be reached"
            ) from error
        if response.status_code != 200:
            raise GatewayConnectionProbeError(
                f"Self-hosted Gateway validation returned HTTP {response.status_code}"
            )
