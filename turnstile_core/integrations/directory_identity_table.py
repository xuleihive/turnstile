"""Conditional identity-only Table writes with exact readback and replay protection."""

from __future__ import annotations

from typing import Any, Self

from ..domain.directory import DirectoryError
from ..domain.directory_identity import DirectoryIdentity, ProjectionOutcome
from .ledger import TableStorageLedger


class TableDirectoryIdentityStore(TableStorageLedger):
    def __enter__(self) -> Self:
        return self

    @staticmethod
    def _order(value: dict[str, Any]) -> tuple[int, int]:
        try:
            if value["ProtocolVersion"] != 1:
                raise ValueError("Unknown identity protocol")
            return int(value["DirectoryVersion"]), int(value["ProjectionSequence"])
        except (KeyError, TypeError, ValueError) as error:
            raise DirectoryError(
                "identity_projection_conflict",
                "Existing identity projection is not compatible",
                503,
            ) from error

    def project(self, identity: DirectoryIdentity, *, now: int) -> ProjectionOutcome:
        entity = identity.entity(now=now)
        wanted = self._order(entity)
        url = self._entity_url(identity.partition, identity.row_key)
        for _ in range(4):
            current = self._request("GET", url)
            if current.status_code == 404:
                result = self._request(
                    "POST",
                    f"{self._endpoint}/{self._table}",
                    json={
                        **entity,
                        "PartitionKey": identity.partition,
                        "RowKey": identity.row_key,
                    },
                )
                if result.status_code == 409:
                    continue
                result.raise_for_status()
            else:
                current.raise_for_status()
                value = current.json()
                if not isinstance(value, dict):
                    raise DirectoryError(
                        "identity_projection_conflict", "Malformed identity projection", 503
                    )
                order = self._order(value)
                if order > wanted:
                    return "superseded"
                if order == wanted:
                    stable = set(entity) - {"ExpiresAt", "ExpiresAt@odata.type"}
                    if any(
                        value.get(key) != entity[key] for key in stable if "@odata.type" not in key
                    ):
                        raise DirectoryError(
                            "identity_projection_conflict",
                            "A projection sequence has conflicting contents",
                            503,
                        )
                    if int(value.get("ExpiresAt", 0)) >= int(entity["ExpiresAt"]):
                        return "confirmed"
                etag = current.headers.get("ETag")
                if not etag:
                    raise DirectoryError(
                        "identity_etag_missing", "Identity projection ETag is missing", 503
                    )
                result = self._request("PUT", url, extra_headers={"If-Match": etag}, json=entity)
                if result.status_code == 412:
                    continue
                result.raise_for_status()
            observed = self._request("GET", url)
            observed.raise_for_status()
            value = observed.json()
            if not isinstance(value, dict):
                raise DirectoryError(
                    "identity_readback_failed", "Identity projection readback is malformed", 503
                )
            if self._order(value) > wanted:
                return "superseded"
            if any(
                value.get(key) != item for key, item in entity.items() if "@odata.type" not in key
            ):
                raise DirectoryError(
                    "identity_readback_failed",
                    "Identity projection was not confirmed",
                    503,
                )
            return "confirmed"
        raise DirectoryError(
            "identity_write_conflict", "Concurrent identity writes did not converge", 503
        )
