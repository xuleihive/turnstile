from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from ..domain.control_plane import (
    GatewayPublication,
    publication_materialized_named_values,
    publication_model_id,
)
from .repository_support import _activation_runtime_config


class InMemoryPublicationRepositoryMixin:
    gateway_release_operation_secrets: dict[UUID, bytes]

    assistant_setting: dict[str, Any]
    effective_gateway_releases: dict[UUID, UUID]
    gateway_publication_outbox: list[dict[str, Any]]
    gateway_publication_audit: list[dict[str, Any]]
    gateway_publication_secrets: dict[UUID, bytes]
    gateway_publications: list[dict[str, Any]]
    gateway_release_gc_plans: list[dict[str, Any]]
    gateway_release_integrity_snapshots: list[dict[str, Any]]
    gateway_release_operation_audit: list[dict[str, Any]]
    gateway_release_operations: list[dict[str, Any]]
    gateway_release_protection_audit: list[dict[str, Any]]
    gateway_release_protections: dict[UUID, dict[str, Any]]
    gateways: list[dict[str, Any]]
    models: list[dict[str, Any]]
    providers: list[dict[str, Any]]
    runtimes: list[dict[str, Any]]
    user_model_access_audit: list[dict[str, Any]]
    user_model_policies: dict[str, dict[str, Any]]

    def create_registry_item(
        self, kind: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _find(
        items: list[dict[str, Any]], item_id: Any
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    @classmethod
    def _require(
        cls, items: list[dict[str, Any]], item_id: Any, label: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def create_gateway_publication(
        self,
        gateway_profile_id: UUID,
        desired_spec: Mapping[str, Any],
        desired_spec_sha256: str,
        publication_kind: str,
        created_by: str,
        credential_ciphertext: bytes | None = None,
        expected_base_release_id: UUID | None = None,
    ) -> dict[str, Any]:
        gateway = self._require(self.gateways, gateway_profile_id, "gateway")
        if (
            gateway["implementation"] != "apim"
            or (not gateway["enabled"] and publication_kind != "model_remove")
        ):
            raise ValueError("The selected APIM gateway is unavailable")
        if publication_kind == "route_reconcile" and (
            expected_base_release_id is None
            or self.effective_gateway_releases.get(gateway_profile_id)
            != expected_base_release_id
        ):
            raise ValueError("The effective gateway release changed; reconcile again")
        existing = next(
            (
                item
                for item in self.gateway_publications
                if item["gateway_profile_id"] == gateway_profile_id
                and item["desired_spec_sha256"] == desired_spec_sha256
                and item["status"] not in {"superseded", "rolled_back"}
            ),
            None,
        )
        if existing is not None:
            if existing["status"] != "failed":
                return existing
            desired = dict(existing["desired_spec"])
            bindings = list(desired.get("bindings") or [])
            latest = dict(bindings[-1]) if bindings else {}
            requires_credential = (
                latest.get("auth_strategy")
                in {"named_value_bearer", "named_value_api_key"}
                and latest.get("key_vault_secret_id") is None
                and publication_kind != "route_reconcile"
            )
            if (
                credential_ciphertext is None
                and requires_credential
            ):
                raise ValueError("A failed publication requires a replacement API key")
            return self.requeue_gateway_publication(
                UUID(str(existing["id"])), created_by, credential_ciphertext
            )
        in_flight = {
            "queued",
            "validating",
            "provisioning",
            "building_revision",
            "verifying",
            "awaiting_authorization",
            "promoting",
            "rolling_back",
        }
        if any(
            item["gateway_profile_id"] == gateway_profile_id and item["status"] in in_flight
            for item in self.gateway_publications
        ):
            raise ValueError("Another gateway publication is already in progress")
        now = datetime.now(UTC)
        publication = {
            "id": uuid4(),
            "gateway_profile_id": gateway_profile_id,
            "generation": 1
            + max(
                (
                    item["generation"]
                    for item in self.gateway_publications
                    if item["gateway_profile_id"] == gateway_profile_id
                ),
                default=0,
            ),
            "desired_spec": dict(desired_spec),
            "desired_spec_sha256": desired_spec_sha256,
            "publication_kind": publication_kind,
            "status": "queued",
            "base_release_id": self.effective_gateway_releases.get(gateway_profile_id),
            "apim_revision": None,
            "policy_sha256": None,
            "resource_manifest": {},
            "error_code": None,
            "error_message": None,
            "attempt_count": 0,
            "created_by": created_by,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        self.gateway_publications.append(publication)
        self.gateway_publication_audit.append(
            {
                "id": uuid4(),
                "publication_id": publication["id"],
                "from_status": None,
                "to_status": "queued",
                "actor": created_by,
                "detail": {},
                "created_at": now,
            }
        )
        if credential_ciphertext is not None:
            self.gateway_publication_secrets[UUID(str(publication["id"]))] = (
                credential_ciphertext
            )
        self.gateway_publication_outbox.append(
            {
                "publication_id": publication["id"],
                "status": "queued",
                "available_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        return publication

    def requeue_gateway_publication(
        self,
        publication_id: UUID,
        created_by: str,
        credential_ciphertext: bytes | None,
        desired_spec: Mapping[str, Any] | None = None,
        desired_spec_sha256: str | None = None,
        *,
        image_probe_authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        publication = self._require(
            self.gateway_publications, publication_id, "publication"
        )
        if publication["status"] != "failed":
            raise ValueError("Only a failed publication can be retried")
        if (desired_spec is None) != (desired_spec_sha256 is None):
            raise ValueError("A replacement release requires both its spec and hash")
        if any(
            item["gateway_profile_id"] == publication["gateway_profile_id"]
            and item["status"] in {
                "queued",
                "validating",
                "provisioning",
                "building_revision",
                "verifying",
                "awaiting_authorization",
                "promoting",
                "rolling_back",
            }
            for item in self.gateway_publications
        ):
            raise ValueError("Another gateway publication is already in progress")
        now = datetime.now(UTC)
        materialized = publication_materialized_named_values(
            GatewayPublication.model_validate(publication),
            provisioning_completed=any(
                item.get("publication_id") == publication_id
                and item.get("from_status") == "validating"
                and item.get("to_status") == "provisioning"
                for item in self.gateway_publication_audit
            ),
        )
        preserved_manifest: dict[str, Any] = (
            {"named_values": materialized} if credential_ciphertext is None and materialized else {}
        )
        oauth_credentials = publication["resource_manifest"].get("oauth_credentials")
        if credential_ciphertext is None and isinstance(oauth_credentials, list):
            preserved_manifest["oauth_credentials"] = list(oauth_credentials)
        authorization = image_probe_authorization or publication["resource_manifest"].get(
            "image_probe_authorization"
        )
        if authorization is not None:
            preserved_manifest["image_probe_authorization"] = dict(authorization)
        generation = publication["resource_manifest"].get("credential_generation")
        if credential_ciphertext is not None:
            preserved_manifest["credential_generation"] = str(uuid4())
        elif generation is not None:
            preserved_manifest["credential_generation"] = generation
        publication.update(
            status="queued",
            apim_revision=None,
            policy_sha256=None,
            resource_manifest=preserved_manifest,
            error_code=None,
            error_message=None,
            attempt_count=0,
            started_at=None,
            completed_at=None,
            created_by=created_by,
            updated_at=now,
        )
        if desired_spec is not None and desired_spec_sha256 is not None:
            publication["desired_spec"] = dict(desired_spec)
            publication["desired_spec_sha256"] = desired_spec_sha256
        outbox = next(
            item
            for item in self.gateway_publication_outbox
            if item["publication_id"] == publication["id"]
        )
        outbox.update(
            status="queued",
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            attempts=0,
            updated_at=now,
        )
        if credential_ciphertext is not None:
            self.gateway_publication_secrets[publication_id] = credential_ciphertext
        else:
            self.gateway_publication_secrets.pop(publication_id, None)
        return publication

    def resume_gateway_publication_authorization(
        self,
        publication_id: UUID,
        created_by: str,
    ) -> dict[str, Any]:
        publication = self._require(
            self.gateway_publications, publication_id, "publication"
        )
        if publication["status"] != "awaiting_authorization":
            raise ValueError("The publication is not waiting for provider authorization")
        now = datetime.now(UTC)
        publication.update(
            status="verifying",
            error_code=None,
            error_message=None,
            created_by=created_by,
            updated_at=now,
        )
        outbox = next(
            item
            for item in self.gateway_publication_outbox
            if item["publication_id"] == publication_id
        )
        outbox.update(
            status="queued",
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
        return publication

    def gateway_publication_credential(self, publication_id: UUID) -> bytes | None:
        return self.gateway_publication_secrets.get(publication_id)

    def delete_gateway_publication_credential(self, publication_id: UUID) -> None:
        self.gateway_publication_secrets.pop(publication_id, None)

    def list_gateway_publications(
        self, gateway_profile_id: UUID | None = None, limit: int = 50
    ) -> Sequence[dict[str, Any]]:
        rows = [
            item
            for item in self.gateway_publications
            if gateway_profile_id is None or item["gateway_profile_id"] == gateway_profile_id
        ]
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)[:limit]

    def all_gateway_publications(
        self, gateway_profile_id: UUID
    ) -> Sequence[dict[str, Any]]:
        return sorted(
            (
                item
                for item in self.gateway_publications
                if item["gateway_profile_id"] == gateway_profile_id
            ),
            key=lambda item: (item["created_at"], str(item["id"])),
        )

    def get_gateway_publication(self, publication_id: UUID) -> dict[str, Any] | None:
        return self._find(self.gateway_publications, publication_id)

    def get_gateway_publications(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        requested = set(publication_ids)
        return [
            publication
            for publication in self.gateway_publications
            if publication["id"] in requested
        ]

    def get_gateway_profiles(
        self, gateway_profile_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        requested = set(gateway_profile_ids)
        return [gateway for gateway in self.gateways if gateway["id"] in requested]

    def list_gateway_publication_audit(
        self, publication_id: UUID
    ) -> Sequence[dict[str, Any]]:
        return sorted(
            (
                item
                for item in self.gateway_publication_audit
                if item["publication_id"] == publication_id
            ),
            key=lambda item: (item["created_at"], str(item["id"])),
        )

    def effective_gateway_publication(
        self, gateway_profile_id: UUID
    ) -> dict[str, Any] | None:
        publication_id = self.effective_gateway_releases.get(gateway_profile_id)
        return (
            self._find(self.gateway_publications, publication_id)
            if publication_id is not None
            else None
        )

    def get_gateway_release_protection(
        self, publication_id: UUID
    ) -> dict[str, Any] | None:
        return self.gateway_release_protections.get(publication_id)

    def list_gateway_release_protections(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        return [
            self.gateway_release_protections[value]
            for value in publication_ids
            if value in self.gateway_release_protections
        ]

    def list_gateway_release_protection_audit(
        self, publication_id: UUID
    ) -> Sequence[dict[str, Any]]:
        return sorted(
            (
                row
                for row in self.gateway_release_protection_audit
                if row["publication_id"] == publication_id
            ),
            key=lambda row: (row["created_at"], str(row["id"])),
        )

    def set_gateway_release_protection(
        self,
        publication_id: UUID,
        *,
        pinned: bool,
        protected_label: str | None,
        retain_until: datetime | None,
        updated_by: str,
    ) -> dict[str, Any] | None:
        self._require(self.gateway_publications, publication_id, "publication")
        if not pinned and protected_label is None and retain_until is None:
            self.gateway_release_protections.pop(publication_id, None)
            row = None
        else:
            row = {
                "publication_id": publication_id,
                "pinned": pinned,
                "protected_label": protected_label,
                "retain_until": retain_until,
                "updated_by": updated_by,
                "updated_at": datetime.now(UTC),
            }
            self.gateway_release_protections[publication_id] = row
        self.gateway_release_protection_audit.append(
            {
                "id": uuid4(),
                "publication_id": publication_id,
                "pinned": pinned,
                "protected_label": protected_label,
                "retain_until": retain_until,
                "actor": updated_by,
                "created_at": datetime.now(UTC),
            }
        )
        return row

    def create_gateway_release_operation(
        self,
        gateway_profile_id: UUID,
        operation_kind: str,
        created_by: str,
        *,
        target_release_id: UUID | None = None,
        prior_release_id: UUID | None = None,
        confirmation_sha256: str | None = None,
        semantic_preview: Mapping[str, Any] | None = None,
        credential_ciphertext: bytes | None = None,
    ) -> dict[str, Any]:
        if any(
            row["gateway_profile_id"] == gateway_profile_id
            and row["status"] not in {"succeeded", "failed", "restored"}
            for row in self.gateway_release_operations
        ):
            raise ValueError("Another gateway release operation is already in progress")
        now = datetime.now(UTC)
        row = {
            "id": uuid4(),
            "gateway_profile_id": gateway_profile_id,
            "operation_kind": operation_kind,
            "target_release_id": target_release_id,
            "prior_release_id": prior_release_id,
            "status": "queued",
            "confirmation_sha256": confirmation_sha256,
            "semantic_preview": dict(semantic_preview or {}),
            "checkpoint": {},
            "error_code": None,
            "error_message": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "attempt_count": 0,
            "created_by": created_by,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        self.gateway_release_operations.append(row)
        if credential_ciphertext is not None:
            self.gateway_release_operation_secrets[UUID(str(row["id"]))] = (
                credential_ciphertext
            )
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": row["id"],
                "from_status": None,
                "to_status": "queued",
                "actor": created_by,
                "detail": {},
                "created_at": now,
            }
        )
        return row

    def gateway_release_operation_secret(self, operation_id: UUID) -> bytes | None:
        return self.gateway_release_operation_secrets.get(operation_id)

    def delete_gateway_release_operation_secret(self, operation_id: UUID) -> None:
        self.gateway_release_operation_secrets.pop(operation_id, None)

    def get_gateway_release_operation(
        self, operation_id: UUID
    ) -> dict[str, Any] | None:
        return self._find(self.gateway_release_operations, operation_id)

    def list_gateway_release_operations(
        self, gateway_profile_id: UUID, limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        return sorted(
            (
                row
                for row in self.gateway_release_operations
                if row["gateway_profile_id"] == gateway_profile_id
            ),
            key=lambda row: (row["created_at"], str(row["id"])),
            reverse=True,
        )[:limit]

    def claim_gateway_release_operation(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        available = sorted(
            (
                row
                for row in self.gateway_release_operations
                if row["status"] not in {"succeeded", "failed", "restored"}
                and (
                    row["lease_expires_at"] is None
                    or row["lease_expires_at"] < now
                )
            ),
            key=lambda row: (row["created_at"], str(row["id"])),
        )
        if not available:
            return None
        row = available[0]
        row.update(
            lease_owner=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempt_count=row["attempt_count"] + 1,
            updated_at=now,
        )
        return row

    def transition_gateway_release_operation(
        self,
        operation_id: UUID,
        expected_status: str,
        status: str,
        updates: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        allowed_updates = {"checkpoint", "error_code", "error_message"}
        unexpected = set(updates) - allowed_updates
        if unexpected:
            raise ValueError(f"Unsupported release operation updates: {sorted(unexpected)}")
        row = self._find(self.gateway_release_operations, operation_id)
        if (
            row is None
            or row["status"] != expected_status
            or row["lease_owner"] != actor
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= datetime.now(UTC)
        ):
            return None
        now = datetime.now(UTC)
        row.update(
            updates,
            status=status,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
        if expected_status == "queued":
            row["started_at"] = row["started_at"] or now
        if status in {"succeeded", "failed", "restored"}:
            row["completed_at"] = now
            if row["operation_kind"] == "application_provision":
                self.delete_gateway_release_operation_secret(operation_id)
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "from_status": expected_status,
                "to_status": status,
                "actor": actor,
                "detail": dict(updates),
                "created_at": now,
            }
        )
        return row

    def list_gateway_release_operation_audit(
        self, operation_id: UUID
    ) -> Sequence[dict[str, Any]]:
        return sorted(
            (
                row
                for row in self.gateway_release_operation_audit
                if row["operation_id"] == operation_id
            ),
            key=lambda row: (row["created_at"], str(row["id"])),
        )

    def complete_gateway_release_rollback(
        self,
        operation_id: UUID,
        target_release_id: UUID,
        prior_release_id: UUID,
        actor: str,
    ) -> dict[str, Any]:
        operation = self._require(
            self.gateway_release_operations, operation_id, "release operation"
        )
        if (
            operation["status"] != "post_promotion_probing"
            or operation["lease_owner"] != actor
            or operation["lease_expires_at"] is None
            or operation["lease_expires_at"] <= datetime.now(UTC)
        ):
            raise ValueError("Rollback operation is not ready to complete")
        target = self._require(
            self.gateway_publications, target_release_id, "publication"
        )
        prior = self._require(
            self.gateway_publications, prior_release_id, "publication"
        )
        gateway_id = operation["gateway_profile_id"]
        if (
            target["gateway_profile_id"] != gateway_id
            or prior["gateway_profile_id"] != gateway_id
        ):
            raise ValueError("Rollback releases belong to another gateway")
        if self.effective_gateway_releases.get(gateway_id) != prior_release_id:
            raise ValueError("Effective gateway release changed during rollback")
        now = datetime.now(UTC)
        target_previous_status = target["status"]
        prior_previous_status = prior["status"]
        target.update(status="active", updated_at=now)
        prior.update(status="rolled_back", updated_at=now)
        self.effective_gateway_releases[gateway_id] = target_release_id
        detail = {"operation_id": str(operation_id)}
        for publication, from_status, to_status in (
            (target, target_previous_status, "active"),
            (prior, prior_previous_status, "rolled_back"),
        ):
            self.gateway_publication_audit.append(
                {
                    "id": uuid4(),
                    "publication_id": publication["id"],
                    "from_status": from_status,
                    "to_status": to_status,
                    "actor": actor,
                    "detail": detail,
                    "created_at": now,
                }
            )
        operation.update(
            status="succeeded",
            lease_owner=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
        self.gateway_release_operation_audit.append(
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "from_status": "post_promotion_probing",
                "to_status": "succeeded",
                "actor": actor,
                "detail": {},
                "created_at": now,
            }
        )
        return operation

    def save_gateway_release_integrity_snapshot(
        self,
        publication_id: UUID,
        operation_id: UUID | None,
        status: str,
        dependencies: Mapping[str, Any],
        issues: Sequence[str],
        checked_by: str,
    ) -> dict[str, Any]:
        row = {
            "id": uuid4(),
            "publication_id": publication_id,
            "operation_id": operation_id,
            "status": status,
            "dependencies": dict(dependencies),
            "issues": list(issues),
            "checked_by": checked_by,
            "checked_at": datetime.now(UTC),
        }
        self.gateway_release_integrity_snapshots.append(row)
        return row

    def latest_gateway_release_integrity_snapshot(
        self, publication_id: UUID
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.gateway_release_integrity_snapshots
            if row["publication_id"] == publication_id
        ]
        return max(rows, key=lambda row: (row["checked_at"], str(row["id"]))) if rows else None

    def latest_gateway_release_integrity_snapshots(
        self, publication_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        requested = set(publication_ids)
        latest: dict[UUID, dict[str, Any]] = {}
        for row in self.gateway_release_integrity_snapshots:
            publication_id = row["publication_id"]
            if publication_id not in requested:
                continue
            current = latest.get(publication_id)
            if current is None or (row["checked_at"], str(row["id"])) > (
                current["checked_at"],
                str(current["id"]),
            ):
                latest[publication_id] = row
        return list(latest.values())

    def save_gateway_release_gc_plan(
        self,
        operation_id: UUID,
        gateway_profile_id: UUID,
        retained_release_ids: Sequence[UUID],
        current_non_release_references: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        reference_graph_sha256: str,
        created_by: str,
    ) -> dict[str, Any]:
        if any(row["operation_id"] == operation_id for row in self.gateway_release_gc_plans):
            raise ValueError("The gateway release operation already has a GC plan")
        row = {
            "id": uuid4(),
            "operation_id": operation_id,
            "gateway_profile_id": gateway_profile_id,
            "retained_release_ids": list(retained_release_ids),
            "current_non_release_references": dict(current_non_release_references),
            "candidates": [dict(value) for value in candidates],
            "reference_graph_sha256": reference_graph_sha256,
            "created_by": created_by,
            "created_at": datetime.now(UTC),
        }
        self.gateway_release_gc_plans.append(row)
        return row

    def get_gateway_release_gc_plan(
        self, operation_id: UUID
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.gateway_release_gc_plans
                if row["operation_id"] == operation_id
            ),
            None,
        )

    def claim_gateway_publication(
        self, worker_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        for outbox in self.gateway_publication_outbox:
            expired = (
                outbox["status"] == "leased"
                and outbox["lease_expires_at"] is not None
                and outbox["lease_expires_at"] < now
            )
            ready = (
                outbox["status"] == "queued"
                and outbox["available_at"] <= now
            )
            if not ready and not expired:
                continue
            outbox.update(
                status="leased",
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=outbox["attempts"] + 1,
                updated_at=now,
            )
            publication = self._require(
                self.gateway_publications, outbox["publication_id"], "publication"
            )
            publication["attempt_count"] += 1
            publication["started_at"] = publication["started_at"] or now
            publication["updated_at"] = now
            return publication
        return None

    def cancel_unstarted_gateway_publication(
        self, publication_id: UUID, actor: str
    ) -> dict[str, Any] | None:
        publication = self._find(self.gateway_publications, publication_id)
        outbox = next(
            (
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
            ),
            None,
        )
        if (
            publication is None
            or outbox is None
            or publication["status"] != "queued"
            or publication["attempt_count"] != 0
            or publication["started_at"] is not None
            or outbox["status"] != "queued"
        ):
            return None
        now = datetime.now(UTC)
        detail = {
            "error_code": "cancelled_by_owner",
            "error_message": "Publication cancelled before processing",
        }
        publication.update(
            status="rolled_back",
            completed_at=now,
            updated_at=now,
            **detail,
        )
        self.delete_gateway_publication_credential(publication_id)
        outbox.update(
            status="completed",
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
        self.gateway_publication_audit.append(
            {
                "id": uuid4(),
                "publication_id": publication_id,
                "from_status": "queued",
                "to_status": "rolled_back",
                "actor": actor,
                "detail": detail,
                "created_at": now,
            }
        )
        return publication

    def transition_gateway_publication(
        self,
        publication_id: UUID,
        expected_status: str,
        status: str,
        updates: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        publication = self._find(self.gateway_publications, publication_id)
        if publication is None or publication["status"] != expected_status:
            return None
        outbox = next(
            (
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
            ),
            None,
        )
        if expected_status not in {"failed", "awaiting_authorization"} and (
            outbox is None
            or outbox["status"] != "leased"
            or outbox["lease_owner"] != actor
            or outbox["lease_expires_at"] is None
            or outbox["lease_expires_at"] <= datetime.now(UTC)
        ):
            return None
        publication.update(updates)
        publication["status"] = status
        publication["updated_at"] = datetime.now(UTC)
        self.gateway_publication_audit.append(
            {
                "id": uuid4(),
                "publication_id": publication_id,
                "from_status": expected_status,
                "to_status": status,
                "actor": actor,
                "detail": dict(updates),
                "created_at": publication["updated_at"],
            }
        )
        terminal = status in {"active", "failed", "rolled_back"}
        paused = status == "awaiting_authorization"
        if terminal:
            publication["completed_at"] = publication["updated_at"]
            self.delete_gateway_publication_credential(publication_id)
        outbox = next(
            item
            for item in self.gateway_publication_outbox
            if item["publication_id"] == publication_id
        )
        outbox.update(
            status="completed" if terminal or paused else "queued",
            available_at=publication["updated_at"],
            lease_owner=None,
            lease_expires_at=None,
            updated_at=publication["updated_at"],
        )
        return publication

    def renew_gateway_publication_lease(
        self,
        publication_id: UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(UTC)
        row = next(
            (
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
                and item["status"] == "leased"
                and item["lease_owner"] == worker_id
                and item["lease_expires_at"] > now
            ),
            None,
        )
        if row is None:
            return False
        row.update(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
        return True

    def renew_gateway_release_operation_lease(
        self,
        operation_id: UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(UTC)
        row = next(
            (
                item
                for item in self.gateway_release_operations
                if item["id"] == operation_id
                and item.get("lease_owner") == worker_id
                and item.get("lease_expires_at") is not None
                and item["lease_expires_at"] > now
            ),
            None,
        )
        if row is None:
            return False
        row.update(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
        return True

    def activate_gateway_publication(
        self, publication_id: UUID, actor: str
    ) -> dict[str, Any]:
        publication = self._require(
            self.gateway_publications, publication_id, "publication"
        )
        if publication["status"] == "active":
            return publication
        outbox = next(
            (
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
            ),
            None,
        )
        if (
            outbox is None
            or outbox["status"] != "leased"
            or outbox["lease_owner"] != actor
            or (
                outbox["lease_expires_at"] is None
                or outbox["lease_expires_at"] <= datetime.now(UTC)
            )
        ):
            raise ValueError("Publication activation requires the current worker lease")
        if publication["status"] != "promoting":
            raise ValueError("Only a promoted gateway publication can be activated")
        if publication["publication_kind"] == "model_remove":
            target = publication["desired_spec"]["removed_models"][0]
            model_id = UUID(str(target["model_id"]))
            model = self._require(self.models, model_id, "model")
            if str(model["model_key"]).casefold() != str(target["model_key"]).casefold():
                raise ValueError("The model removal target changed before activation")
            if model["is_default"]:
                raise ValueError("The default model cannot be deleted")
            changed_at = datetime.now(UTC)
            changed_by = str(publication["created_by"])
            for user_id, policy in self.user_model_policies.items():
                previous_model_ids = list(policy["model_ids"])
                current_model_ids = [
                    item for item in previous_model_ids if item != model_id
                ]
                if current_model_ids == previous_model_ids:
                    continue
                policy.update(
                    model_ids=current_model_ids,
                    updated_at=changed_at,
                    updated_by=changed_by,
                )
                self.user_model_access_audit.insert(
                    0,
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "previous_model_ids": previous_model_ids,
                        "new_model_ids": current_model_ids,
                        "changed_at": changed_at,
                        "changed_by": changed_by,
                    },
                )
            if self.assistant_setting.get("model_id") == model_id:
                self.assistant_setting.update(
                    model_id=None,
                    updated_at=changed_at,
                    updated_by=changed_by,
                )
            self.models.remove(model)
            previous_release_id = self.effective_gateway_releases.get(
                publication["gateway_profile_id"]
            )
            if previous_release_id and previous_release_id != publication_id:
                self._require(
                    self.gateway_publications, previous_release_id, "publication"
                )["status"] = "superseded"
            self.effective_gateway_releases[
                publication["gateway_profile_id"]
            ] = publication_id
            publication["status"] = "active"
            publication["completed_at"] = changed_at
            publication["updated_at"] = changed_at
            self.gateway_publication_audit.append(
                {
                    "id": uuid4(),
                    "publication_id": publication_id,
                    "from_status": "promoting",
                    "to_status": "active",
                    "actor": actor,
                    "detail": {},
                    "created_at": changed_at,
                }
            )
            next(
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
            )["status"] = "completed"
            return publication
        if publication["publication_kind"] == "route_reconcile" or (
            publication["publication_kind"] == "credential_rotation"
            and publication["desired_spec"]["bindings"][-1]["auth_strategy"]
            == "oauth_client_credentials"
        ):
            previous_release_id = self.effective_gateway_releases.get(
                publication["gateway_profile_id"]
            )
            if previous_release_id and previous_release_id != publication_id:
                self._require(
                    self.gateway_publications, previous_release_id, "publication"
                )["status"] = "superseded"
            self.effective_gateway_releases[
                publication["gateway_profile_id"]
            ] = publication_id
            publication["status"] = "active"
            publication["completed_at"] = datetime.now(UTC)
            publication["updated_at"] = publication["completed_at"]
            self.gateway_publication_audit.append(
                {
                    "id": uuid4(),
                    "publication_id": publication_id,
                    "from_status": "promoting",
                    "to_status": "active",
                    "actor": actor,
                    "detail": {},
                    "created_at": publication["updated_at"],
                }
            )
            self.gateway_publication_audit.append(
                {
                    "id": uuid4(),
                    "publication_id": publication_id,
                    "from_status": "promoting",
                    "to_status": "active",
                    "actor": actor,
                    "detail": {},
                    "created_at": publication["updated_at"],
                }
            )
            next(
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
            )["status"] = "completed"
            return publication
        binding = publication["desired_spec"]["bindings"][-1]
        if publication["publication_kind"] == "credential_rotation":
            runtime = next(
                (
                    item
                    for item in self.runtimes
                    if item.get("gateway_profile_id") == publication["gateway_profile_id"]
                    and (
                        str(item["id"]) == str(binding["runtime_id"])
                        if binding.get("runtime_id") is not None
                        else str(item["name"]).casefold() == str(binding["runtime_name"]).casefold()
                    )
                ),
                None,
            )
            if runtime is None:
                raise ValueError("The managed runtime no longer exists")
            config = dict(runtime.get("config") or {})
            if not config.get("control_plane_managed"):
                raise ValueError("The runtime is not managed by the control plane")
            config["named_value_name"] = binding["named_value_name"]
            runtime["config"] = config
            previous_release_id = self.effective_gateway_releases.get(
                publication["gateway_profile_id"]
            )
            if previous_release_id and previous_release_id != publication_id:
                self._require(
                    self.gateway_publications, previous_release_id, "publication"
                )["status"] = "superseded"
            self.effective_gateway_releases[
                publication["gateway_profile_id"]
            ] = publication_id
            publication["status"] = "active"
            publication["completed_at"] = datetime.now(UTC)
            publication["updated_at"] = publication["completed_at"]
            next(
                item
                for item in self.gateway_publication_outbox
                if item["publication_id"] == publication_id
            )["status"] = "completed"
            return publication
        model_spec = binding["model"]
        if binding.get("provider_id"):
            provider = self._require(
                self.providers, UUID(str(binding["provider_id"])), "provider"
            )
        else:
            provider = self.create_registry_item(
                "provider",
                {
                    "name": binding["provider_name"],
                    "provider_kind": binding["provider_kind"],
                    "endpoint_url": None,
                    "auth_type": "none",
                    "credential_ciphertext": None,
                    "credential_hint": None,
                    "enabled": True,
                    "brand_key": binding["provider_brand_key"],
                    "config": binding.get("provider_config") or {},
                },
            )
        if binding.get("runtime_id"):
            runtime = self._require(
                self.runtimes, UUID(str(binding["runtime_id"])), "runtime"
            )
            if runtime["provider_id"] != provider["id"]:
                raise ValueError("The selected runtime no longer belongs to the provider")
            binding_config = dict(binding.get("runtime_config") or {})
            if (
                runtime["config"].get("credential_provisioned") is False
                and binding_config.get("credential_provisioned") is True
            ):
                runtime["config"] = {
                    **runtime["config"],
                    "credential_provisioned": True,
                }
        else:
            runtime_config = _activation_runtime_config(binding)
            runtime = self.create_registry_item(
                "runtime",
                {
                    "provider_id": provider["id"],
                    "gateway_profile_id": publication["gateway_profile_id"],
                    "name": binding["runtime_name"],
                    "runtime_kind": binding["runtime_kind"],
                    "enabled": True,
                    "is_default": False,
                    "brand_key": binding["runtime_brand_key"],
                    "config": runtime_config,
                    "allowed_roles": ["owner", "admin", "member"],
                },
            )
        if any(
            model["model_key"].casefold() == model_spec["model_key"].casefold()
            for model in self.models
        ):
            raise ValueError("The model alias is already active")
        model = self.create_registry_item(
            "model",
            {
                "id": publication_model_id(publication_id, model_spec["model_key"]),
                "provider_id": provider["id"],
                "runtime_id": runtime["id"],
                "model_key": model_spec["model_key"],
                "display_name": model_spec["display_name"],
                "family_key": model_spec["family_key"],
                "upstream_model_id": model_spec["upstream_model_id"],
                "assignment_required": model_spec.get("assignment_required", True),
                "enabled": True,
                "is_default": False,
                "capabilities": model_spec["capabilities"],
                "context_window": model_spec.get("context_window"),
                "input_cost_per_million": model_spec.get("input_cost_per_million"),
                "output_cost_per_million": model_spec.get("output_cost_per_million"),
                "cached_cost_per_million": model_spec.get("cached_cost_per_million"),
                "cache_write_cost_per_million": model_spec.get("cache_write_cost_per_million"),
                "allowed_roles": model_spec["allowed_roles"],
                "publication_id": publication_id,
                **(model_spec.get("price_configuration") or {}),
            },
        )
        model["publication_id"] = publication_id
        previous_release_id = self.effective_gateway_releases.get(
            publication["gateway_profile_id"]
        )
        if previous_release_id and previous_release_id != publication_id:
            self._require(
                self.gateway_publications, previous_release_id, "publication"
            )["status"] = "superseded"
        self.effective_gateway_releases[publication["gateway_profile_id"]] = publication_id
        publication["status"] = "active"
        publication["completed_at"] = datetime.now(UTC)
        publication["updated_at"] = publication["completed_at"]
        self.gateway_publication_audit.append(
            {
                "id": uuid4(),
                "publication_id": publication_id,
                "from_status": "promoting",
                "to_status": "active",
                "actor": actor,
                "detail": {},
                "created_at": publication["updated_at"],
            }
        )
        next(
            item
            for item in self.gateway_publication_outbox
            if item["publication_id"] == publication_id
        )["status"] = "completed"
        return publication

    def invocation_route(
        self, runtime_id: UUID | None, model_id: UUID | None
    ) -> dict[str, Any] | None:
        candidates = [model for model in self.models if model["enabled"]]
        if model_id is not None:
            candidates = [model for model in candidates if model["id"] == model_id]
        if runtime_id is not None:
            candidates = [model for model in candidates if model["runtime_id"] == runtime_id]
        if runtime_id is None and model_id is None:
            candidates = [model for model in candidates if model["is_default"]]
        if not candidates:
            return None
        model = sorted(candidates, key=lambda item: not bool(item["is_default"]))[0]
        runtime = self._require(self.runtimes, model["runtime_id"], "runtime")
        provider = self._require(self.providers, model["provider_id"], "provider")
        gateway = self._find(self.gateways, runtime.get("gateway_profile_id"))
        if (
            not runtime["enabled"]
            or not provider["enabled"]
            or (gateway and not gateway["enabled"])
        ):
            return None
        return {
            "model_id": model["id"],
            "model_key": model["model_key"],
            "model_capabilities": list(model.get("capabilities") or []),
            "image_profile": self._published_image_profile(model),
            "model_family_key": model.get("family_key", "generic"),
            "upstream_model_id": model.get("upstream_model_id") or model["model_key"],
            "display_name": model["display_name"],
            "assignment_required": model.get("assignment_required", False),
            "input_cost_per_million": model["input_cost_per_million"],
            "output_cost_per_million": model["output_cost_per_million"],
            "cached_cost_per_million": model.get("cached_cost_per_million"),
            "cache_write_cost_per_million": model.get("cache_write_cost_per_million"),
            "model_allowed_roles": model["allowed_roles"],
            "runtime_id": runtime["id"],
            "runtime_name": runtime["name"],
            "runtime_kind": runtime["runtime_kind"],
            "runtime_config": runtime["config"],
            "runtime_allowed_roles": runtime["allowed_roles"],
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "provider_kind": provider["provider_kind"],
            "provider_endpoint_url": provider["endpoint_url"],
            "provider_auth_type": provider["auth_type"],
            "provider_credential_ciphertext": provider["credential_ciphertext"],
            "gateway_id": gateway["id"] if gateway else None,
            "gateway_name": gateway["name"] if gateway else None,
            "gateway_implementation": gateway["implementation"] if gateway else None,
            "gateway_base_url": gateway["base_url"] if gateway else None,
            "gateway_auth_type": gateway["auth_type"] if gateway else None,
            "gateway_credential_ciphertext": gateway["credential_ciphertext"] if gateway else None,
            "gateway_config": gateway["config"] if gateway else {},
        }

    def _published_image_profile(self, model: Mapping[str, Any]) -> object:
        raise NotImplementedError

    def update_runtime_health(
        self, runtime_id: UUID, status: str, message: str, checked_at: datetime
    ) -> None:
        runtime = self._find(self.runtimes, runtime_id)
        if runtime is not None:
            runtime.update(
                health_status=status,
                health_message=message,
                last_checked_at=checked_at,
                updated_at=checked_at,
            )
