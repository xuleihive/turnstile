from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from hmac import compare_digest
from time import time
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import Field, SecretStr

from ..domain.control_plane import GatewayModelBinding
from ..domain.storage_routes import (
    Digest,
    EpochSeconds,
    RouteCredential,
    RouteModel,
    canonical_document,
    document_digest,
    text_digest,
)
from ..security import CredentialCipher
from .apim_control_plane_contract import PolicyCompilationError, RetryablePublicationError
from .ledger import StorageTokenProvider


class _CredentialBindings(RouteModel):
    version: Literal[1] = 1
    bindings: dict[Digest, str] = Field(max_length=10000)


class VaultCheckpoint(RouteModel):
    version: Literal[1] = 1
    publication_id: str
    source_sha256: Digest
    url: str
    issued_epoch: EpochSeconds
    body_ciphertext: str = Field(repr=False, min_length=1, max_length=131072)
    body_sha256: Digest
    secret_id: str | None = None


def route_credential_source(binding: GatewayModelBinding) -> str:
    if binding.runtime_id is None or binding.named_value_name is None:
        raise PolicyCompilationError("Fixed route credential has no stable source")
    return document_digest({
        "runtime_id": str(binding.runtime_id), "auth_strategy": binding.auth_strategy.value,
        "named_value_name": binding.named_value_name,
        "key_vault_secret_id": (
            str(binding.key_vault_secret_id) if binding.key_vault_secret_id is not None else None
        ),
    })


def credential_bindings(manifest: Mapping[str, Any]) -> dict[str, str]:
    raw = manifest.get("route_credentials")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PolicyCompilationError("Route credential bindings are invalid")
    return _CredentialBindings.model_validate_json(canonical_document(raw)).bindings


class RouteCredentialVault:
    def __init__(
        self, origin: str, cipher: CredentialCipher, token_provider: StorageTokenProvider,
        client: httpx.Client, *, clock: Callable[[], int] = lambda: int(time()),
    ) -> None:
        if not re.fullmatch(r"https://[a-z0-9-]{3,24}\.vault\.azure\.net", origin):
            raise ValueError("ROUTE_CREDENTIAL_VAULT_ORIGIN_INVALID")
        self._origin = origin
        self._cipher = cipher
        self._tokens = token_provider
        self._client = client
        self._clock = clock

    def _request(
        self, method: str, url: str, body: str | None = None,
        *, read_only_recovery: bool = False,
    ) -> httpx.Response:
        address = urlsplit(url)
        if (
            address.scheme + "://" + address.netloc != self._origin
            or address.query or address.fragment or method not in {"GET", "PUT"}
            or not re.fullmatch(
                r"/secrets/[a-zA-Z0-9-]{1,127}(?:/[0-9a-f]{32})?", address.path,
            )
        ):
            raise ValueError("ROUTE_CREDENTIAL_REQUEST_TARGET_INVALID")
        try:
            with self._client.stream(
                method, url, params={"api-version": "7.4"}, headers={
                    "Authorization": "Bearer " + self._tokens.token("https://vault.azure.net"),
                    "Content-Type": "application/json", "Accept-Encoding": "identity",
                }, content=body.encode("utf-8") if body is not None else None,
                timeout=10.0, follow_redirects=False,
            ) as response:
                if response.status_code in {401, 403}:
                    error_type = (
                        RetryablePublicationError if read_only_recovery else PolicyCompilationError
                    )
                    raise error_type("Route credential Vault access was denied")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 131072:
                        raise PolicyCompilationError("Route credential response is too large")
                    raw.extend(chunk)
                return httpx.Response(response.status_code, content=bytes(raw))
        except httpx.HTTPError:
            raise RetryablePublicationError(
                "Route credential transport outcome is unconfirmed",
            ) from None

    def verify(self, secret_id: str, expected: SecretStr | None = None) -> SecretStr:
        RouteCredential(mode="key_vault_bearer", header="Authorization", secret_id=secret_id)
        response = self._request("GET", secret_id)
        if response.status_code == 404:
            raise PolicyCompilationError("Route credential version is unavailable")
        if response.status_code != 200:
            raise PolicyCompilationError("Route credential availability could not be verified")
        try:
            document = response.json()
            attributes = document["attributes"]
            value = document["value"]
            valid = (
                document["id"] == secret_id and isinstance(value, str) and bool(value)
                and len(value.encode("utf-8")) <= 25600 and attributes.get("enabled") is True
                and ("nbf" not in attributes or type(attributes["nbf"]) is int
                     and attributes["nbf"] <= self._clock())
                and ("exp" not in attributes or type(attributes["exp"]) is int
                     and attributes["exp"] > self._clock())
            )
            if expected is not None:
                valid = valid and compare_digest(
                    value.encode("utf-8"), expected.get_secret_value().encode("utf-8"),
                )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PolicyCompilationError("Route credential version readback is invalid") from None
        if not valid:
            raise PolicyCompilationError("Route credential version changed or is inactive")
        return SecretStr(value)

    def materialize(
        self, publication_id: UUID, source: str, value: SecretStr,
        *, checkpoint: Mapping[str, Any] | None,
        persist: Callable[[Mapping[str, Any] | None, dict[str, Any]], None],
    ) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", source):
            raise ValueError("ROUTE_CREDENTIAL_SOURCE_INVALID")
        secret = value.get_secret_value()
        if not secret or len(secret.encode("utf-8")) > 25600:
            raise ValueError("ROUTE_CREDENTIAL_VALUE_INVALID")
        url = self._origin + "/secrets/turnstile-route-" + publication_id.hex + "-" + source[:24]
        tags = {"turnstile-publication": str(publication_id), "turnstile-source": source}
        body = canonical_document({"value": secret, "tags": tags, "attributes": {"enabled": True}})
        if checkpoint is None:
            existing = self._request("GET", url)
            if existing.status_code == 200:
                raise PolicyCompilationError("Route credential name is not an unclaimed resource")
            if existing.status_code != 404:
                raise PolicyCompilationError("Route credential availability could not be verified")
            candidate = VaultCheckpoint(
                publication_id=str(publication_id), source_sha256=source, url=url,
                issued_epoch=self._clock(),
                body_ciphertext=self._cipher.encrypt(body).decode("ascii"),
                body_sha256=text_digest(body),
            )
            persist(None, candidate.model_dump(mode="json"))
            checkpoint = candidate.model_dump(mode="json")
            try:
                response = self._request("PUT", url, body)
            except RetryablePublicationError:
                response = None
            if (
                response is not None and response.status_code < 500
                and response.status_code not in {200, 201, 202, 429}
            ):
                raise PolicyCompilationError("Route credential creation was rejected")
        else:
            try:
                candidate = VaultCheckpoint.model_validate_json(canonical_document(checkpoint))
                restored = self._cipher.decrypt(candidate.body_ciphertext.encode("ascii"))
            except ValueError:
                raise ValueError("ROUTE_CREDENTIAL_CHECKPOINT_INVALID") from None
            if (
                candidate.publication_id != str(publication_id) or candidate.source_sha256 != source
                or candidate.url != url or restored is None
                or not compare_digest(restored.encode("utf-8"), body.encode("utf-8"))
                or candidate.body_sha256 != text_digest(body)
                or candidate.secret_id is not None and (
                    candidate.secret_id.rsplit("/", 1)[0] != url
                    or not re.fullmatch(r"[0-9a-f]{32}", candidate.secret_id.rsplit("/", 1)[-1])
                )
            ):
                raise ValueError("ROUTE_CREDENTIAL_CHECKPOINT_CHANGED")
        observed = self._request("GET", candidate.secret_id or url, read_only_recovery=True)
        if observed.status_code != 200:
            raise RetryablePublicationError("Route credential creation requires read-only recovery")
        try:
            document = observed.json()
            secret_id = document["id"]
            approved = RouteCredential(
                mode="key_vault_bearer", header="Authorization", secret_id=secret_id,
            )
            attributes = document["attributes"]
            valid = (
                approved.secret_id.rsplit("/", 1)[0] == url
                and document.get("tags") == tags
                and attributes.get("enabled") is True
                and ("nbf" not in attributes or type(attributes["nbf"]) is int
                     and attributes["nbf"] <= self._clock())
                and ("exp" not in attributes or type(attributes["exp"]) is int
                     and attributes["exp"] > self._clock())
                and isinstance(document.get("value"), str)
                and compare_digest(document["value"].encode("utf-8"), secret.encode("utf-8"))
                and candidate.secret_id in {None, secret_id}
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PolicyCompilationError("Route credential readback is invalid") from None
        if not valid:
            raise PolicyCompilationError("Route credential readback differs from its source")
        if candidate.secret_id is None:
            persist(checkpoint, candidate.model_copy(update={"secret_id": secret_id}).model_dump(
                mode="json",
            ))
        return str(secret_id)