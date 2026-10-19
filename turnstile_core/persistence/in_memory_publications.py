from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from ..domain.billable_requests import BillableRequestAttempt
from ..domain.control_plane import (
    GatewayPublication,
    GatewayPublicationIntent,
    publication_credential_evidence,
    publication_materialized_named_values,
    publication_model_id,
    publication_provisioning_completed,
)
from ..domain.gateway_targets import (
    ApimTargetRouteBinding,
    CapabilitySupport,
    GatewayDeploymentScope,
    apim_target_route_binding_id_for,
)
from .repository_support import (
    ModelActivationValidator,
    _activated_runtime_credentials,
    _activation_runtime_config,
    _configuration_rollback_values,
    _fixed_program_upgrade_config,
    _managed_model_request_revision,
    _managed_program_initialization_config,
    _managed_program_publication,
    _model_addition_credential_retry,
    _model_addition_manifest,
    _model_removal_manifest,
    _program_migration_manifest,
    _program_migration_retry,
    _require_program_migration_activation,
    _validate_gateway_deployment_snapshots,
)


class InMemoryPublicationRepositoryMixin:
    billable_requests: dict[UUID, BillableRequestAttempt]
    gateway_release_operation_secrets: dict[UUID, bytes]

    assistant_setting: dict[str, Any]
    apim_targets: dict[UUID, dict[str, Any]]
    apim_target_route_bindings: dict[UUID, dict[str, Any]]
    effective_gateway_releases: dict[UUID, UUID]
    gateway_publication_outbox: list[dict[str, Any]]
    gateway_publication_audit: list[dict[str, Any]]
    gateway_publication_secrets: dict[UUID, bytes]
    gateway_publications: list[dict[str, Any]]
    gateway_deployments: dict[UUID, dict[str, Any]]
    gateway_deployment_inventories: dict[UUID, dict[str, Any]]
    multi_target_rollouts: list[dict[str, Any]]
    multi_target_rollout_children: list[dict[str, Any]]
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

    def upsert_apim_target_route_binding(
        self, binding: ApimTargetRouteBinding
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
        initial_manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        GatewayPublicationIntent.model_validate({
            "gateway_profile_id": gateway_profile_id,
            "publication_kind": publication_kind,
            "desired_spec": desired_spec,
        })
        model_addition = initial_manifest is not None and set(initial_manifest) == {
            "model_route_addition",
        }
        model_removal = initial_manifest is not None and set(initial_manifest) == {
            "model_route_removal",
        }
        if model_removal and (publication_kind != "model_remove"
                              or expected_base_release_id is None
                              or credential_ciphertext is not None):
            raise ValueError("Model removal requires its current base without credentials")
        if model_addition and (publication_kind != "model_add" or expected_base_release_id is None):
            raise ValueError("Model addition requires its current program base")
        if initial_manifest is not None and not model_addition and not model_removal and (
            publication_kind != "route_reconcile" or credential_ciphertext is not None
            or expected_base_release_id is None
        ):
            raise ValueError("Program migration requires a new route reconcile without credentials")
        gateway = self._require(self.gateways, gateway_profile_id, "gateway")
        if (
            gateway["implementation"] != "apim"
            or (not gateway["enabled"] and publication_kind != "model_remove")
        ):
            raise ValueError("The selected APIM gateway is unavailable")
        if (publication_kind == "route_reconcile" or expected_base_release_id is not None) and (
            expected_base_release_id is None
            or self.effective_gateway_releases.get(gateway_profile_id)
            != expected_base_release_id
        ):
            raise ValueError("The effective gateway release changed; reconcile again")
        validate_manifest = (
            _model_addition_manifest if model_addition else _program_migration_manifest
        )
        manifest = validate_manifest(
            _managed_program_publication(
                self._find(self.gateway_publications, expected_base_release_id),
                gateway.get("config") or {},
            ) if model_addition else self._find(
                self.gateway_publications, expected_base_release_id,
            ),
            desired_spec, desired_spec_sha256, initial_manifest,
        ) if initial_manifest is not None and not model_removal else {}
        if model_removal:
            assert initial_manifest is not None
            marker = initial_manifest["model_route_removal"]
            manifest = _model_removal_manifest(
                self._find(self.gateway_publications, expected_base_release_id),
                self._find(self.gateway_publications, UUID(marker["source_publication_id"])),
                desired_spec, desired_spec_sha256, initial_manifest,
            )
        existing = next(
            (
                item
                for item in reversed(self.gateway_publications)
                if item["gateway_profile_id"] == gateway_profile_id
                and item["desired_spec_sha256"] == desired_spec_sha256
                and item["status"] not in {"superseded", "rolled_back"}
            ),
            None,
        )
        if existing is not None and model_addition and existing["status"] == "active":
            removed = next((item for item in reversed(self.gateway_publications)
                if item["gateway_profile_id"] == gateway_profile_id
                and item["publication_kind"] == "model_remove" and item["status"] == "active"
                and item.get("resource_manifest", {}).get("model_route_removal", {}).get(
                    "source_publication_id",
                ) == str(existing["id"])
                and item.get("resource_manifest", {}).get("route_activation", {}).get(
                    "outcome",
                ) == "confirmed"), None)
            if removed is not None and not any(
                route["apim_target_id"] == gateway_profile_id
                and route.get("current_publication_id") == existing["id"]
                for route in self.apim_target_route_bindings.values()
            ):
                existing["status"] = "superseded"
                existing["updated_at"] = datetime.now(UTC)
                self.gateway_publication_audit.append({
                    "publication_id": existing["id"], "from_status": "active",
                    "to_status": "superseded", "actor": created_by,
                    "detail": {"kind": "confirmed_model_removal",
                               "removal_publication_id": str(removed["id"])},
                    "created_at": datetime.now(UTC),
                })
                existing = None
        if (existing is not None and (model_addition or model_removal)
            and existing["status"] == "failed"
                and existing.get("base_release_id") == expected_base_release_id
                and existing.get("desired_spec") == dict(desired_spec)
                and existing.get("resource_manifest") == manifest
                and existing.get("apim_revision") is None
                and existing.get("policy_sha256") is None
                and any(outbox["publication_id"] == existing["id"]
                    and outbox["status"] == "completed"
                    and outbox.get("lease_owner") is None
                    and outbox.get("lease_expires_at") is None
                    for outbox in self.gateway_publication_outbox)
                and not any(audit["publication_id"] == existing["id"]
                    and "probe_request" in (audit.get("detail") or {})
                    for audit in self.gateway_publication_audit)
                and not any(attempt.scope_type == "system"
                    and attempt.scope_id == f"publication:{existing['id']}"
                    for attempt in self.billable_requests.values())):
            existing = None
        if existing is not None:
            if initial_manifest is not None and not model_addition and not model_removal and (
                existing["status"] == "failed"
                or existing["id"] != UUID(manifest["fixed_route_migration"]["request_id"])
                or existing["resource_manifest"].get("fixed_route_migration")
                != manifest["fixed_route_migration"]
            ):
                raise ValueError("Program migration recovery requires its original active intent")
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
        _validate_gateway_deployment_snapshots(
            gateway_profile_id, desired_spec, list(self.gateway_deployments.values()),
        )
        publication_id = (
            UUID(manifest["fixed_route_migration"]["request_id"])
            if "fixed_route_migration" in manifest else uuid4()
        )
        if self._find(self.gateway_publications, publication_id) is not None:
            raise ValueError("The publication request ID is already used by another intent")
        now = datetime.now(UTC)
        publication = {
            "id": publication_id,
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
            "resource_manifest": manifest,
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
                "detail": copy.deepcopy(manifest),
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

    def create_multi_target_rollout(
        self,
        rollout_id: UUID,
        rollout_kind: str,
        logical_model_id: UUID | None,
        request_sha256: str,
        created_by: str,
        children: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        existing = next(
            (
                row
                for row in self.multi_target_rollouts
                if row["request_sha256"] == request_sha256
            ),
            None,
        )
        if existing is not None:
            return self._multi_target_rollout_view(existing)
        if len(children) < 2:
            raise ValueError("A multi-target rollout requires at least two children")
        if rollout_kind in {"model_add", "model_remove"} and logical_model_id is None:
            raise ValueError("A model rollout requires a logical model identity")
        target_ids = [UUID(str(child["gateway_profile_id"])) for child in children]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("A multi-target rollout cannot repeat an APIM target")
        for child, target_id in zip(children, target_ids, strict=True):
            GatewayPublicationIntent.model_validate({
                "gateway_profile_id": target_id,
                "publication_kind": rollout_kind,
                "desired_spec": child["desired_spec"],
            })
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
        for child, target_id in zip(children, target_ids, strict=True):
            gateway = self._require(self.gateways, target_id, "gateway")
            if (
                gateway["implementation"] != "apim"
                or (not gateway["enabled"] and rollout_kind != "model_remove")
                or target_id not in self.apim_targets
            ):
                raise ValueError("The selected APIM target is unavailable")
            desired_sha256 = str(child["desired_spec_sha256"])
            if any(
                publication["gateway_profile_id"] == target_id
                and publication["desired_spec_sha256"] == desired_sha256
                and publication["status"] not in {"superseded", "rolled_back"}
                for publication in self.gateway_publications
            ):
                raise ValueError(
                    "A target already has this desired release; use its existing publication"
                )
            if any(
                publication["gateway_profile_id"] == target_id
                and publication["status"] in in_flight
                for publication in self.gateway_publications
            ):
                raise ValueError("Another gateway publication is already in progress")

        publication_count = len(self.gateway_publications)
        audit_count = len(self.gateway_publication_audit)
        outbox_count = len(self.gateway_publication_outbox)
        secret_snapshot = dict(self.gateway_publication_secrets)
        now = datetime.now(UTC)
        parent = {
            "schema_version": 1,
            "id": rollout_id,
            "rollout_kind": rollout_kind,
            "logical_model_id": logical_model_id,
            "request_sha256": request_sha256,
            "created_by": created_by,
            "created_at": now,
        }
        child_rows: list[dict[str, Any]] = []
        try:
            for child, target_id in zip(children, target_ids, strict=True):
                publication = self.create_gateway_publication(
                    target_id,
                    child["desired_spec"],
                    str(child["desired_spec_sha256"]),
                    rollout_kind,
                    created_by,
                    child.get("credential_ciphertext"),
                    expected_base_release_id=child.get("expected_base_release_id"),
                    initial_manifest=child.get("initial_manifest"),
                )
                child_rows.append(
                    {
                        "rollout_id": rollout_id,
                        "apim_target_id": target_id,
                        "publication_id": publication["id"],
                        "required": bool(child.get("required", True)),
                        "created_at": now,
                    }
                )
            self.multi_target_rollouts.append(parent)
            self.multi_target_rollout_children.extend(child_rows)
        except Exception:
            del self.gateway_publications[publication_count:]
            del self.gateway_publication_audit[audit_count:]
            del self.gateway_publication_outbox[outbox_count:]
            self.gateway_publication_secrets.clear()
            self.gateway_publication_secrets.update(secret_snapshot)
            raise
        return self._multi_target_rollout_view(parent)

    def get_multi_target_rollout(
        self, rollout_id: UUID
    ) -> dict[str, Any] | None:
        row = next(
            (item for item in self.multi_target_rollouts if item["id"] == rollout_id),
            None,
        )
        return self._multi_target_rollout_view(row) if row is not None else None

    def list_multi_target_rollouts(
        self, limit: int = 50
    ) -> Sequence[dict[str, Any]]:
        rows = sorted(
            self.multi_target_rollouts,
            key=lambda item: (item["created_at"], str(item["id"])),
            reverse=True,
        )[:limit]
        return [self._multi_target_rollout_view(row) for row in rows]

    def _multi_target_rollout_view(
        self, rollout: Mapping[str, Any]
    ) -> dict[str, Any]:
        children: list[dict[str, Any]] = []
        for child in sorted(
            (
                row
                for row in self.multi_target_rollout_children
                if row["rollout_id"] == rollout["id"]
            ),
            key=lambda item: str(item["apim_target_id"]),
        ):
            publication = self._require(
                self.gateway_publications,
                child["publication_id"],
                "publication",
            )
            children.append(
                {
                    **child,
                    "publication_status": publication["status"],
                    "publication": publication,
                }
            )
        return {**rollout, "children": children}

    def _sync_active_route_bindings(
        self,
        publication: Mapping[str, Any],
    ) -> None:
        target_id = UUID(str(publication["gateway_profile_id"]))
        if target_id not in self.apim_targets:
            return
        for raw_binding in publication["desired_spec"].get("bindings", []):
            binding = dict(raw_binding)
            logical_model_id = binding.get("logical_model_id")
            if logical_model_id is not None:
                model = next(
                    (
                        row
                        for row in self.models
                        if row["id"] == UUID(str(logical_model_id))
                    ),
                    None,
                )
            else:
                model = next(
                    (
                        row
                        for row in self.models
                        if str(row["model_key"]).casefold()
                        == str(binding["model"]["model_key"]).casefold()
                    ),
                    None,
                )
            if model is None:
                raise ValueError("An active release binding has no logical model")
            if str(model["model_key"]).casefold() != str(
                binding["model"]["model_key"]
            ).casefold():
                raise ValueError("An active release binding changed logical model alias")
            runtime_id = binding.get("runtime_id")
            if runtime_id is not None:
                runtime = next(
                    (
                        row
                        for row in self.runtimes
                        if row["id"] == UUID(str(runtime_id))
                    ),
                    None,
                )
            else:
                runtime = next(
                    (
                        row
                        for row in self.runtimes
                        if row.get("gateway_profile_id") == target_id
                        and str(row["name"]).casefold()
                        == str(binding["runtime_name"]).casefold()
                    ),
                    None,
                )
            if runtime is None or runtime.get("gateway_profile_id") != target_id:
                raise ValueError("An active release binding has no target Runtime")
            scope = GatewayDeploymentScope.model_validate(
                binding.get("deployment_scope") or {}
            )
            route = ApimTargetRouteBinding(
                id=apim_target_route_binding_id_for(model["id"], target_id),
                logical_model_id=model["id"],
                apim_target_id=target_id,
                runtime_id=runtime["id"],
                enabled=True,
                is_default_serving=model["runtime_id"] == runtime["id"],
                deployment_scope=scope,
                current_publication_id=UUID(str(publication["id"])),
                capability_state=CapabilitySupport.SUPPORTED,
            )
            self.upsert_apim_target_route_binding(route)

    def requeue_gateway_publication(
        self,
        publication_id: UUID,
        created_by: str,
        credential_ciphertext: bytes | None,
        desired_spec: Mapping[str, Any] | None = None,
        desired_spec_sha256: str | None = None,
        *, image_probe_authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        publication = self._require(
            self.gateway_publications, publication_id, "publication"
        )
        if publication["status"] != "failed":
            raise ValueError("Only a failed publication can be retried")
        model_addition = set(publication.get("resource_manifest") or {}) == {"model_route_addition"}
        if "fixed_route_migration" in publication.get("resource_manifest", {}) or model_addition:
            if any(value is not None for value in (
                credential_ciphertext, desired_spec, desired_spec_sha256, image_probe_authorization,
            )):
                raise ValueError("Program migration retry cannot replace its immutable intent")
            target = publication["gateway_profile_id"]
            previous_id = self.effective_gateway_releases.get(target)
            previous = next((item for item in self.gateway_publications
                             if item["id"] == previous_id), None)
            probe_count = sum(1 for item in self.gateway_publication_audit
                              if item.get("publication_id") == publication_id
                              and "probe_request" in item.get("detail", {}))
            if model_addition:
                gateway = next((item for item in self.gateways if item["id"] == target), None)
                previous = _managed_program_publication(
                    previous, (gateway or {}).get("config") or {},
                )
            detail = (_model_addition_credential_retry(publication, previous, probe_count)
                if model_addition else _program_migration_retry(publication, previous, probe_count))
            _validate_gateway_deployment_snapshots(
                target, publication["desired_spec"], list(self.gateway_deployments.values()),
            )
            outbox = next((item for item in self.gateway_publication_outbox
                           if item["publication_id"] == publication_id), None)
            if (outbox is None or outbox["status"] != "completed"
                    or outbox.get("lease_owner") is not None
                    or outbox.get("lease_expires_at") is not None
                    or any(item["gateway_profile_id"] == target and item["status"] not in {
                        "active", "failed", "superseded", "rolled_back",
                    } for item in self.gateway_publications)):
                raise ValueError("Program migration retry requires an idle completed worker")
            now = datetime.now(UTC)
            publication.update(status="queued", error_code=None, error_message=None,
                               completed_at=None, updated_at=now)
            outbox.update(status="queued", available_at=now, last_error_code=None,
                          last_error_message=None, updated_at=now)
            self.gateway_publication_audit.append({
                "id": uuid4(), "publication_id": publication_id, "from_status": "failed",
                "to_status": "queued", "actor": created_by, "created_at": now,
                "detail": {**detail, "outbox_attempts": outbox["attempts"]},
            })
            return publication
        if any(field in publication.get("resource_manifest", {}) for field in (
            "route_configuration", "route_preparation", "route_credentials", "route_dependencies",
            "route_rollback", "model_route_addition",
        )):
            raise ValueError(
                "Configuration publication requires explicit recovery or a new approval; "
                "its immutable intent and probe evidence cannot be reset",
            )
        if (desired_spec is None) != (desired_spec_sha256 is None):
            raise ValueError("A replacement release requires both its spec and hash")
        GatewayPublicationIntent.model_validate({
            "gateway_profile_id": publication["gateway_profile_id"],
            "publication_kind": publication["publication_kind"],
            "desired_spec": publication["desired_spec"] if desired_spec is None else desired_spec,
        })
        effective = self.effective_gateway_publication(publication["gateway_profile_id"])
        if effective is not None and any(
            field in (effective.get("resource_manifest") or {})
            for field in ("fixed_route_program", "route_configuration")
        ):
            raise ValueError("publication_retry_requires_new_release")
        _validate_gateway_deployment_snapshots(
            UUID(str(publication["gateway_profile_id"])),
            publication["desired_spec"] if desired_spec is None else desired_spec,
            list(self.gateway_deployments.values()),
        )
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
        materialized_named_values = publication_materialized_named_values(
            GatewayPublication.model_validate(publication),
            provisioning_completed=publication_provisioning_completed([
                item for item in self.gateway_publication_audit
                if item.get("publication_id") == publication_id
            ]),
        )
        preserved_manifest: dict[str, Any] = (
            {"named_values": list(materialized_named_values),
             "credential_evidence": publication_credential_evidence(
                 GatewayPublication.model_validate(publication),
                 {name: "present" for name in materialized_named_values},
             )}
            if credential_ciphertext is None
            and materialized_named_values
            else {}
        )
        oauth_credentials = publication["resource_manifest"].get("oauth_credentials")
        if credential_ciphertext is None and isinstance(oauth_credentials, list):
            preserved_manifest["oauth_credentials"] = list(oauth_credentials)
        authorization = image_probe_authorization or publication["resource_manifest"].get(
            "image_probe_authorization"
        )
        if authorization is not None:
            preserved_manifest["image_probe_authorization"] = dict(authorization)
        prior_credential_version = publication["resource_manifest"].get("credential_generation")
        if credential_ciphertext is not None:
            preserved_manifest["credential_generation"] = str(uuid4())
        elif prior_credential_version is not None:
            preserved_manifest["credential_generation"] = prior_credential_version
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
        self.gateway_publication_audit.append({
            "id": uuid4(), "publication_id": publication_id, "from_status": "failed",
            "to_status": "queued", "actor": created_by, "created_at": now,
            "detail": {"image_probe_authorization": image_probe_authorization},
        })
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
        archived = {event["publication_id"] for event in self.gateway_publication_audit
                    if (event.get("detail") or {}).get("archived_from_model_list") is True}
        rows = [
            item
            for item in self.gateway_publications
            if item["id"] not in archived
            and (gateway_profile_id is None or item["gateway_profile_id"] == gateway_profile_id)
        ]
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)[:limit]

    def archive_gateway_publication(self, publication_id: UUID, actor: str) -> None:
        publication = self.get_gateway_publication(publication_id)
        outbox = next((item for item in self.gateway_publication_outbox
                       if item["publication_id"] == publication_id), None)
        if (publication is None or publication["status"] != "failed" or outbox is None
                or outbox["status"] != "completed" or outbox.get("lease_owner") is not None
                or outbox.get("lease_expires_at") is not None):
            raise ValueError(
                "Only a terminal failed publication can be removed from the model list",
            )
        if any(event["publication_id"] == publication_id
               and (event.get("detail") or {}).get("archived_from_model_list") is True
               for event in self.gateway_publication_audit):
            return
        self.gateway_publication_audit.append({
            "id": uuid4(), "publication_id": publication_id, "from_status": "failed",
            "to_status": "failed", "actor": actor, "created_at": datetime.now(UTC),
            "detail": {"archived_from_model_list": True},
        })

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

    def record_managed_program_initialization(
        self, descriptor: Mapping[str, Any], expected_config_sha256: str,
    ) -> None:
        from ..domain.storage_routes import ManagedProgramInitialization, canonical_document

        initialized = ManagedProgramInitialization.model_validate_json(
            canonical_document(descriptor),
        )
        target_id = UUID(initialized.installation.program.scope.target_id)
        if any(row["gateway_profile_id"] == target_id and row["status"] not in {
            "active", "failed", "superseded", "rolled_back",
        } for row in self.gateway_publications) or any(
            row["gateway_profile_id"] == target_id and row["status"] not in {
                "succeeded", "failed", "restored",
            } for row in self.gateway_release_operations
        ):
            raise ValueError("Managed initialization requires an idle target")
        gateway = self._require(self.gateways, target_id, "gateway")
        if not gateway["enabled"] or gateway["implementation"] != "apim":
            raise ValueError("Managed initialization Gateway is unavailable")
        config = _managed_program_initialization_config(
            self.effective_gateway_publication(target_id), gateway.get("config") or {},
            descriptor, expected_config_sha256,
        )
        if config is not None:
            gateway["config"] = config

    def record_fixed_program_upgrade(
        self, descriptor: Mapping[str, Any], expected_config_sha256: str,
        *, before_record: Callable[[], None] | None = None,
    ) -> None:
        from ..domain.storage_routes import FixedProgramUpgrade, canonical_document

        upgrade = FixedProgramUpgrade.model_validate_json(canonical_document(descriptor))
        target_id = UUID(upgrade.installation.program.scope.target_id)
        if any(row["gateway_profile_id"] == target_id
               and row["status"] not in {"active", "failed", "superseded", "rolled_back"}
               for row in self.gateway_publications) or any(
            row["gateway_profile_id"] == target_id
            and row["status"] not in {"succeeded", "failed", "restored"}
            for row in self.gateway_release_operations
        ):
            raise ValueError("Program upgrade requires an idle target")
        gateway = self._require(self.gateways, target_id, "gateway")
        if not gateway["enabled"] or gateway["implementation"] != "apim":
            raise ValueError("Program upgrade Gateway is unavailable")
        previous_id = self.effective_gateway_releases.get(target_id)
        previous = self._find(self.gateway_publications, previous_id)
        config = _fixed_program_upgrade_config(
            previous, gateway.get("config") or {}, descriptor, expected_config_sha256,
        )
        if config is not None:
            if before_record is not None:
                before_record()
            gateway.update(config=config, updated_at=datetime.now(UTC))

    def active_model_route_publications(
        self, gateway_profile_id: UUID,
    ) -> Sequence[dict[str, Any]]:
        removed = {
            item["resource_manifest"]["model_route_removal"].get("source_publication_id")
            for item in self.gateway_publications
            if item["gateway_profile_id"] == gateway_profile_id
            and item["status"] == "active" and item["publication_kind"] == "model_remove"
            and "model_route_removal" in item.get("resource_manifest", {})
        }
        rows = [item for item in self.gateway_publications
                if item["gateway_profile_id"] == gateway_profile_id
                and item["status"] == "active" and item["publication_kind"] == "model_add"
                and "model_route_addition" in item.get("resource_manifest", {})
                and str(item["id"]) not in removed]
        if len(rows) > 10000:
            raise ValueError("Model route publication catalog exceeds its bound")
        return sorted(rows, key=lambda item: str(item["id"]))

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
        self, gateway_profile_ids: Sequence[UUID] | None = None
    ) -> Sequence[dict[str, Any]]:
        if gateway_profile_ids is None:
            return list(self.gateways)
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

    def effective_gateway_publications(
        self, gateway_profile_ids: Sequence[UUID]
    ) -> Sequence[dict[str, Any]]:
        requested = set(gateway_profile_ids)
        publication_ids = {
            publication_id
            for gateway_id, publication_id in self.effective_gateway_releases.items()
            if gateway_id in requested
        }
        return [
            publication for publication in self.gateway_publications
            if publication["id"] in publication_ids
        ]

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
        if operation_kind == "self_hosted_gateway_provision":
            from ..domain.gateway_targets import (
                SelfHostedGatewayProvisionSpec,
                gateway_provision_digest,
            )

            spec = SelfHostedGatewayProvisionSpec.model_validate(semantic_preview)
            if (spec.target.gateway_profile_id != gateway_profile_id
                    or confirmation_sha256 != gateway_provision_digest(spec)
                    or target_release_id is not None or prior_release_id is not None):
                raise ValueError("Gateway creation requires a target-bound idempotent request")
            if credential_ciphertext is not None:
                raise ValueError("Gateway creation must not persist credentials")
            previous = next((row for row in self.gateway_release_operations
                if row["operation_kind"] == operation_kind
                and row["semantic_preview"].get("request_id") == str(spec.request_id)), None)
            if previous is not None:
                if (previous["gateway_profile_id"] != gateway_profile_id
                        or previous["confirmation_sha256"] != confirmation_sha256
                        or previous["created_by"] != created_by):
                    raise ValueError("Gateway creation request was already used differently")
                return previous
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

    def list_gateway_release_operations_for_gateways(
        self, gateway_profile_ids: Sequence[UUID], limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        return [
            row
            for gateway_id in sorted(set(gateway_profile_ids), key=str)
            for row in sorted(
                (
                    operation for operation in self.gateway_release_operations
                    if operation["gateway_profile_id"] == gateway_id
                ),
                key=lambda operation: (operation["created_at"], str(operation["id"])),
                reverse=True,
            )[:limit]
        ]

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
        *, keep_lease: bool = False,
    ) -> dict[str, Any] | None:
        if keep_lease and (
            status != expected_status or status in {"succeeded", "failed", "restored"}
        ):
            raise ValueError("Only an in-progress checkpoint can retain its release lease")
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
            lease_owner=row["lease_owner"] if keep_lease else None,
            lease_expires_at=row["lease_expires_at"] if keep_lease else None,
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

    def enqueue_gateway_configuration_rollback(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        operation = self._require(
            self.gateway_release_operations, operation_id, "release operation",
        )
        if (operation["lease_owner"] != actor or operation["lease_expires_at"] is None
                or operation["lease_expires_at"] <= datetime.now(UTC)):
            raise ValueError("Configuration rollback requires the current worker lease")
        target = self._require(
            self.gateway_publications, operation["target_release_id"], "publication",
        )
        prior = self._require(
            self.gateway_publications, operation["prior_release_id"], "publication",
        )
        spec, digest, detail = _configuration_rollback_values(operation, target, prior)
        linked = operation["checkpoint"].get("route_rollback_publication_id")
        if linked is not None:
            publication = self._require(self.gateway_publications, UUID(linked), "publication")
            if (publication["desired_spec_sha256"] != digest
                    or publication["resource_manifest"].get("route_rollback") != detail
                    or publication["base_release_id"] != prior["id"]
                    or publication["gateway_profile_id"] != operation["gateway_profile_id"]
                    or publication["desired_spec"] != spec):
                raise ValueError("Configuration rollback publication changed")
            return publication
        if self.effective_gateway_releases.get(operation["gateway_profile_id"]) != prior["id"]:
            raise ValueError("The effective gateway release changed before configuration rollback")
        publication = self.create_gateway_publication(
            operation["gateway_profile_id"], spec, digest, "route_reconcile",
            operation["created_by"],
            expected_base_release_id=prior["id"],
        )
        publication["resource_manifest"] = {"route_rollback": detail}
        checkpoint = {**operation["checkpoint"],
                      "route_rollback_publication_id": str(publication["id"])}
        operation.update(checkpoint=checkpoint, updated_at=datetime.now(UTC))
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation_id, "from_status": operation["status"],
            "to_status": operation["status"], "actor": actor, "created_at": datetime.now(UTC),
            "detail": {"checkpoint": copy.deepcopy(checkpoint)},
        })
        return publication

    def complete_gateway_configuration_rollback(
        self, operation_id: UUID, actor: str,
    ) -> dict[str, Any]:
        operation = self._require(
            self.gateway_release_operations, operation_id, "release operation",
        )
        if (operation["status"] != "verifying_readback"
                or not operation["checkpoint"].get("route_rollback_publication_id")):
            raise ValueError("Configuration rollback is not ready to complete")
        child = self.enqueue_gateway_configuration_rollback(operation_id, actor)
        effective = self.effective_gateway_releases.get(operation["gateway_profile_id"])
        if child["status"] != "active" or effective != child["id"]:
            raise ValueError("Configuration rollback publication is not currently active")
        now = datetime.now(UTC)
        operation.update(
            status="succeeded", lease_owner=None, lease_expires_at=None,
            updated_at=now, completed_at=now, error_code=None, error_message=None,
        )
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation_id, "from_status": "verifying_readback",
            "to_status": "succeeded", "actor": actor, "created_at": now,
            "detail": {"checkpoint": copy.deepcopy(operation["checkpoint"])},
        })
        return operation

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
        self._sync_active_route_bindings(target)
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

    def _owns_publication_lease(self, publication_id: UUID, worker_id: str) -> bool:
        return any(
            row["publication_id"] == publication_id and row["status"] == "leased"
            and row["lease_owner"] == worker_id and row["lease_expires_at"] > datetime.now(UTC)
            for row in self.gateway_publication_outbox
        )

    def reserve_gateway_publication_probe(
        self, publication_id: UUID, worker_id: str, request_limit: int,
        method: str, endpoint_sha256: str,
    ) -> bool:
        if request_limit < 1:
            raise ValueError("Publication probe request limit must be positive")
        publication = self._find(self.gateway_publications, publication_id)
        if (publication is None or publication["status"] != "verifying"
                or not self._owns_publication_lease(publication_id, worker_id)):
            return False
        requests = sum(
            item["publication_id"] == publication_id and "probe_request" in item["detail"]
            for item in self.gateway_publication_audit
        )
        if requests >= request_limit:
            return False
        self.gateway_publication_audit.append({
            "id": uuid4(), "publication_id": publication_id,
            "from_status": publication["status"], "to_status": publication["status"],
            "actor": worker_id, "created_at": datetime.now(UTC),
            "detail": {"probe_request": {
                "method": method, "endpoint_sha256": endpoint_sha256,
                "request_index": requests + 1, "request_limit": request_limit,
            }},
        })
        return True

    def checkpoint_gateway_publication_probes(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "probe_verification",
        )

    def checkpoint_gateway_publication_activation(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_activation",
        )

    def checkpoint_gateway_publication_writer(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_writer",
        )

    def checkpoint_gateway_publication_dependencies(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_dependencies",
        )

    def checkpoint_gateway_publication_credentials(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_credentials",
        )

    def checkpoint_gateway_publication_preparation(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_preparation",
        )

    def checkpoint_gateway_program_promotion(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_program_promotion",
        )

    def checkpoint_gateway_publication_approval(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any],
    ) -> bool:
        return self._checkpoint_gateway_publication(
            publication_id, worker_id, expected, checkpoint, "route_commit_approval",
        )

    def _checkpoint_gateway_publication(
        self, publication_id: UUID, worker_id: str,
        expected: Mapping[str, Any] | None, checkpoint: Mapping[str, Any], field: str,
    ) -> bool:
        phase = {
            "probe_verification": "verifying", "route_activation": "promoting",
            "route_writer": "promoting", "route_dependencies": "queued",
            "route_commit_approval": "promoting", "route_preparation": "queued",
            "route_credentials": "queued", "route_program_promotion": "promoting",
        }[field]
        publication = self._find(self.gateway_publications, publication_id)
        if (publication is None or publication["status"] != phase
                or not self._owns_publication_lease(publication_id, worker_id)
                or publication["resource_manifest"].get(field) != expected):
            return False
        recorded = copy.deepcopy(dict(checkpoint))
        publication["resource_manifest"] = {
            **publication["resource_manifest"], field: recorded,
        }
        publication["updated_at"] = datetime.now(UTC)
        self.gateway_publication_audit.append({
            "id": uuid4(), "publication_id": publication_id,
            "from_status": phase, "to_status": phase,
            "actor": worker_id, "created_at": datetime.now(UTC),
            "detail": {field: copy.deepcopy(recorded)},
        })
        return True

    def reserve_gateway_release_operation_probe(
        self, operation_id: UUID, worker_id: str, request_limit: int,
        method: str, endpoint_sha256: str,
    ) -> bool:
        if request_limit < 1:
            raise ValueError("Release probe request limit must be positive")
        operation = self._find(self.gateway_release_operations, operation_id)
        if (
            operation is None or operation["operation_kind"] != "rollback"
            or operation["status"] not in {
                "preflight_probing", "post_promotion_probing", "restoring",
            }
            or operation.get("lease_owner") != worker_id
            or operation.get("lease_expires_at") is None
            or operation["lease_expires_at"] <= datetime.now(UTC)
        ):
            return False
        requests = sum(
            item["operation_id"] == operation_id and "probe_request" in item["detail"]
            for item in self.gateway_release_operation_audit
        )
        if requests >= request_limit:
            return False
        self.gateway_release_operation_audit.append({
            "id": uuid4(), "operation_id": operation_id, "from_status": operation["status"],
            "to_status": operation["status"], "actor": worker_id, "created_at": datetime.now(UTC),
            "detail": {"probe_request": {
                "method": method, "endpoint_sha256": endpoint_sha256,
                "request_index": requests + 1, "request_limit": request_limit,
            }},
        })
        return True

    def renew_gateway_publication_lease(
        self, publication_id: UUID, worker_id: str, lease_seconds: int,
    ) -> bool:
        if not self._owns_publication_lease(publication_id, worker_id):
            return False
        row = next(
            item
            for item in self.gateway_publication_outbox
            if item["publication_id"] == publication_id
        )
        row["lease_expires_at"] = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        return True

    def renew_gateway_release_operation_lease(
        self, operation_id: UUID, worker_id: str, lease_seconds: int,
    ) -> bool:
        now = datetime.now(UTC)
        row = self._find(self.gateway_release_operations, operation_id)
        if (row is not None and row["lease_owner"] == worker_id
                and row["lease_expires_at"] is not None and row["lease_expires_at"] > now):
            row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            return True
        return False

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
        if expected_status not in {"failed", "awaiting_authorization"} and not (
            self._owns_publication_lease(publication_id, actor)
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
        if terminal or (expected_status == "validating" and status == "provisioning"):
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

    def activate_gateway_publication(
        self, publication_id: UUID, actor: str,
        *, validate_model_activation: ModelActivationValidator | None = None,
    ) -> dict[str, Any]:
        publication = self._require(
            self.gateway_publications, publication_id, "publication"
        )
        if publication["status"] == "active":
            return publication
        if not self._owns_publication_lease(publication_id, actor):
            raise ValueError("Publication activation requires the current worker lease")
        if publication["status"] != "promoting":
            raise ValueError("Only a promoted gateway publication can be activated")
        if any(name in publication["resource_manifest"] for name in (
            "model_route_addition", "model_route_removal",
        )):
            if validate_model_activation is None:
                raise ValueError("Model activation requires its complete transaction validator")
            effective_id = self.effective_gateway_releases.get(publication["gateway_profile_id"])
            if effective_id != publication["base_release_id"]:
                raise ValueError("Model addition program base changed")
            gateway = self._require(self.gateways, publication["gateway_profile_id"], "gateway")
            validate_model_activation(
                publication, _managed_program_publication(
                    self._find(self.gateway_publications, effective_id),
                    gateway.get("config") or {},
                ),
                (gateway.get("config") or {}).get("fixed_route_program_upgrade"),
            )
        if "fixed_route_migration" in publication["resource_manifest"]:
            effective_id = self.effective_gateway_releases.get(publication["gateway_profile_id"])
            if effective_id != publication["base_release_id"]:
                raise ValueError("Program migration activation confirmation base changed")
            _require_program_migration_activation(
                publication, self._find(self.gateway_publications, effective_id),
            )
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
            target_id = UUID(str(publication["gateway_profile_id"]))
            removed_route_ids = [
                route_id
                for route_id, route in self.apim_target_route_bindings.items()
                if route["logical_model_id"] == model_id
                and route["apim_target_id"] == target_id
            ]
            for route_id in removed_route_ids:
                del self.apim_target_route_bindings[route_id]
            remaining_routes = [
                route
                for route in self.apim_target_route_bindings.values()
                if route["logical_model_id"] == model_id and route["enabled"]
            ]
            if remaining_routes:
                selected = next(
                    (
                        route
                        for route in remaining_routes
                        if route["is_default_serving"]
                    ),
                    remaining_routes[0],
                )
                for route in remaining_routes:
                    route["is_default_serving"] = route is selected
                selected_runtime = self._require(
                    self.runtimes,
                    selected["runtime_id"],
                    "runtime",
                )
                selected_provider = self._require(
                    self.providers,
                    selected_runtime["provider_id"],
                    "provider",
                )
                model.update(
                    runtime_id=selected_runtime["id"],
                    runtime_name=selected_runtime["name"],
                    provider_id=selected_provider["id"],
                    provider_name=selected_provider["name"],
                    updated_at=changed_at,
                )
            else:
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
            if "model_route_removal" not in publication["resource_manifest"]:
                self._sync_active_route_bindings(publication)
            previous_release_id = self.effective_gateway_releases.get(
                publication["gateway_profile_id"]
            )
            independent = "model_route_removal" in publication["resource_manifest"]
            if not independent and previous_release_id and previous_release_id != publication_id:
                self._require(
                    self.gateway_publications, previous_release_id, "publication"
                )["status"] = "superseded"
            if not independent:
                self.effective_gateway_releases[publication["gateway_profile_id"]] = publication_id
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
            self._sync_active_route_bindings(publication)
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
            self._sync_active_route_bindings(publication)
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
            existing_provider = next(
                (
                    item
                    for item in self.providers
                    if str(item["name"]).casefold()
                    == str(binding["provider_name"]).casefold()
                ),
                None,
            )
            if existing_provider is None:
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
            else:
                provider = existing_provider
            if (
                not provider["enabled"]
                or provider["provider_kind"] != binding["provider_kind"]
                or dict(provider.get("config") or {})
                != dict(binding.get("provider_config") or {})
            ):
                raise ValueError(
                    "A provider with this name exists with incompatible settings"
                )
        if binding.get("runtime_id"):
            runtime = self._require(
                self.runtimes, UUID(str(binding["runtime_id"])), "runtime"
            )
            if runtime["provider_id"] != provider["id"]:
                raise ValueError("The selected runtime no longer belongs to the provider")
            runtime["config"] = _activated_runtime_credentials(
                runtime["config"], binding, publication["resource_manifest"],
            )
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
        model_spec = {**model_spec, **(model_spec.get("price_configuration") or {})}
        logical_model_id = (
            UUID(str(binding["logical_model_id"]))
            if binding.get("logical_model_id") is not None
            else None
        )
        existing_model = next(
            (
                item
                for item in self.models
                if str(item["model_key"]).casefold()
                == str(model_spec["model_key"]).casefold()
            ),
            None,
        )
        if existing_model is not None:
            model = existing_model
            if logical_model_id is None or model["id"] != logical_model_id:
                raise ValueError("The model alias is already active")
            common_fields = (
                "display_name",
                "family_key",
                "capabilities",
                "context_window",
                "input_cost_per_million",
                "output_cost_per_million",
                "cached_cost_per_million",
                "cache_write_cost_per_million",
                "price_reference",
                "price_discount_percent",
                "list_input_cost_per_million",
                "list_output_cost_per_million",
                "list_cached_cost_per_million",
                "list_cache_write_cost_per_million",
                "allowed_roles",
                "assignment_required",
            )
            if any(
                model.get(field) != model_spec.get(field)
                for field in common_fields
            ) or model.get("price_source", "manual") != model_spec.get(
                "price_source", "manual"
            ) or (model.get("context_price_tiers") or []) != (
                model_spec.get("context_price_tiers") or []
            ):
                raise ValueError(
                    "Multi-target child conflicts with the logical model identity"
                )
        else:
            model = self.create_registry_item(
                "model",
                {
                    "id": (
                        logical_model_id
                        or publication_model_id(publication_id, model_spec["model_key"])
                    ),
                    "provider_id": provider["id"],
                    "runtime_id": runtime["id"],
                    "model_key": model_spec["model_key"],
                    "display_name": model_spec["display_name"],
                    "family_key": model_spec["family_key"],
                    "upstream_model_id": model_spec["upstream_model_id"],
                    "assignment_required": model_spec.get(
                        "assignment_required", True
                    ),
                    "enabled": True,
                    "is_default": False,
                    "capabilities": model_spec["capabilities"],
                    "context_window": model_spec.get("context_window"),
                    "input_cost_per_million": model_spec.get(
                        "input_cost_per_million"
                    ),
                    "output_cost_per_million": model_spec.get(
                        "output_cost_per_million"
                    ),
                    "cached_cost_per_million": model_spec.get(
                        "cached_cost_per_million"
                    ),
                    "cache_write_cost_per_million": model_spec.get(
                        "cache_write_cost_per_million"
                    ),
                    "price_source": model_spec.get("price_source", "manual"),
                    "price_reference": model_spec.get("price_reference"),
                    "price_discount_percent": model_spec.get("price_discount_percent"),
                    "context_price_tiers": model_spec.get("context_price_tiers") or [],
                    "list_input_cost_per_million": model_spec.get("list_input_cost_per_million"),
                    "list_output_cost_per_million": model_spec.get("list_output_cost_per_million"),
                    "list_cached_cost_per_million": model_spec.get("list_cached_cost_per_million"),
                    "list_cache_write_cost_per_million": model_spec.get(
                        "list_cache_write_cost_per_million"
                    ),
                    "price_synced_at": publication["created_at"]
                    if model_spec.get("price_source", "manual") != "manual"
                    and model_spec.get("price_reference") else None,
                    "price_sync_status": "ok"
                    if model_spec.get("price_source", "manual") != "manual"
                    and model_spec.get("price_reference") else None,
                    "allowed_roles": model_spec["allowed_roles"],
                    "publication_id": publication_id,
                    **(model_spec.get("price_configuration") or {}),
                },
            )
            model["publication_id"] = publication_id
        self._sync_active_route_bindings(publication)
        previous_release_id = self.effective_gateway_releases.get(
            publication["gateway_profile_id"]
        )
        if "model_route_addition" not in publication["resource_manifest"]:
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

    def runtime_diagnostic_contexts(
        self, selections: Sequence[tuple[UUID, UUID, UUID, UUID]],
    ) -> Sequence[dict[str, Any]]:
        contexts: list[dict[str, Any]] = []
        for runtime_id, model_id, gateway_id, deployment_id in selections:
            runtime = next((row for row in self.runtimes if row["id"] == runtime_id), None)
            model = next((row for row in self.models if row["id"] == model_id), None)
            deployment = self.gateway_deployments.get(deployment_id)
            binding = next((row for row in self.apim_target_route_bindings.values()
                            if row["logical_model_id"] == model_id
                            and row["apim_target_id"] == gateway_id), None)
            release_id = self.effective_gateway_releases.get(gateway_id)
            if binding is not None:
                release_id = binding.get("current_publication_id")
            publication = next((row for row in self.gateway_publications
                                if row["id"] == release_id and row["status"] == "active"
                                and row["gateway_profile_id"] == gateway_id), None)
            published_bindings = (publication or {}).get("desired_spec", {}).get("bindings", [])
            published = next((row for row in published_bindings
                              if str(row.get("runtime_id")) == str(runtime_id)
                              and row.get("model", {}).get("model_key", "").casefold()
                              == (model or {}).get("model_key", "").casefold()), None)
            contexts.append({
                "runtime_id": runtime_id, "model_id": model_id,
                "gateway_profile_id": gateway_id, "gateway_deployment_id": deployment_id,
                "runtime": runtime, "model": model, "model_metadata": model,
                "provider": next((row for row in self.providers
                                  if row["id"] == (runtime or {}).get("provider_id")), None),
                "gateway": next((row for row in self.gateways if row["id"] == gateway_id), None),
                "target": self.apim_targets.get(gateway_id), "deployment": deployment,
                "binding": binding, "published_route": published,
                "deployment_in_scope": (
                    binding["deployment_scope"]["mode"] == "all"
                    or deployment_id in binding["deployment_scope"]["deployment_ids"]
                ) if binding else bool(deployment and deployment["kind"] == "managed"),
            })
        return contexts

    def invocation_route(
        self, runtime_id: UUID | None, model_id: UUID | None,
        *, gateway_profile_id: UUID | None = None,
        gateway_deployment_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        if (gateway_profile_id is None) != (gateway_deployment_id is None):
            return None
        if gateway_profile_id is not None and model_id is None:
            return None
        candidates = [model for model in self.models if model["enabled"]]
        if model_id is not None:
            candidates = [model for model in candidates if model["id"] == model_id]
        if runtime_id is not None and gateway_profile_id is None:
            candidates = [model for model in candidates if model["runtime_id"] == runtime_id]
        if runtime_id is None and model_id is None:
            candidates = [model for model in candidates if model["is_default"]]
        if not candidates:
            return None
        model = sorted(candidates, key=lambda item: not bool(item["is_default"]))[0]
        route = next(
            (
                item
                for item in self.apim_target_route_bindings.values()
                if item["logical_model_id"] == model["id"]
                and item["enabled"]
                and (
                    item["apim_target_id"] == gateway_profile_id
                    if gateway_profile_id is not None
                    else item["runtime_id"] == model["runtime_id"] and item["is_default_serving"]
                )
            ),
            None,
        )
        selected_runtime_id = (
            route["runtime_id"] if gateway_profile_id is not None and route else model["runtime_id"]
        )
        runtime = self._require(self.runtimes, selected_runtime_id, "runtime")
        provider = self._require(self.providers, runtime["provider_id"], "provider")
        gateway = self._find(self.gateways, runtime.get("gateway_profile_id"))
        if route is None and any(
            item["logical_model_id"] == model["id"]
            for item in self.apim_target_route_bindings.values()
        ):
            return None
        if route is not None:
            if not gateway or not route.get("current_publication_id"):
                return None
            base_id = self.effective_gateway_releases.get(gateway["id"])
            if route["current_publication_id"] != base_id:
                publication = next((item for item in self.gateway_publications
                                    if item["id"] == route["current_publication_id"]), {})
                marker = publication.get("resource_manifest", {}).get("model_route_addition", {})
                bindings = publication.get("desired_spec", {}).get("bindings", [])
                if (
                    publication.get("status") != "active"
                    or publication.get("publication_kind") != "model_add"
                    or publication.get("gateway_profile_id") != gateway["id"]
                    or not base_id or publication.get("base_release_id") != base_id
                    or not isinstance(marker, dict) or type(marker.get("version")) is not int
                    or marker["version"] != 1 or marker.get("program_release_id") != str(base_id)
                    or str(marker.get("alias", "")).casefold() != model["model_key"].casefold()
                    or len(bindings) != 1
                    or bindings[0].get("model", {}).get("model_key", "").casefold()
                    != model["model_key"].casefold()
                    or str(bindings[0].get("logical_model_id") or model["id"]) != str(model["id"])
                    or str(bindings[0].get("runtime_id") or runtime["id"]) != str(runtime["id"])
                ):
                    return None
        selected_deployment = None
        selected_gateway_base_url = gateway["base_url"] if gateway else None
        if gateway_profile_id is not None:
            assert gateway_deployment_id is not None
            if not gateway or gateway["id"] != gateway_profile_id:
                return None
            if runtime_id is not None and runtime["id"] != runtime_id:
                return None
        if gateway:
            target = self.apim_targets.get(gateway["id"])
            eligible = []
            for deployment in self.gateway_deployments.values():
                if (
                    deployment["apim_target_id"] != gateway["id"]
                    or deployment["retired_at"] is not None
                    or deployment["declared_state"] != "present"
                    or not deployment["endpoint_url"]
                    or (
                        gateway_deployment_id is not None
                        and deployment["id"] != gateway_deployment_id
                    )
                ):
                    continue
                if route is None:
                    if deployment["kind"] != "managed":
                        continue
                elif (
                    route["deployment_scope"]["mode"] == "selected"
                    and deployment["id"] not in route["deployment_scope"]["deployment_ids"]
                ):
                    continue
                if deployment["kind"] == "self_hosted":
                    inventory = self.gateway_deployment_inventories.get(deployment["id"])
                    if not target or not inventory or not any(
                        api_id.casefold() == target["api_id"].casefold()
                        for api_id in inventory["associated_api_ids"]
                    ):
                        continue
                eligible.append(deployment)
            preferred_kind = (
                "managed" if route is None or route["deployment_scope"]["mode"] == "all"
                else "self_hosted"
            )
            eligible.sort(key=lambda item: (item["kind"] != preferred_kind, str(item["id"])))
            if eligible:
                selected_deployment = eligible[0]
                selected_gateway_base_url = selected_deployment["endpoint_url"]
            elif route is not None or target is not None or gateway_profile_id is not None:
                return None
        if (
            not runtime["enabled"]
            or not provider["enabled"]
            or (gateway and not gateway["enabled"])
        ):
            return None
        result = {
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
            "context_price_tiers": model.get("context_price_tiers") or [],
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
            "gateway_base_url": selected_gateway_base_url,
            "gateway_deployment_id": selected_deployment["id"] if selected_deployment else None,
            "native_gateway_id": (
                selected_deployment["native_gateway_id"] if selected_deployment else None
            ),
            "gateway_target": self.apim_targets.get(gateway["id"]) if gateway else None,
            "gateway_deployment": selected_deployment,
            "gateway_inventory": (
                {**(self.gateway_deployment_inventories.get(selected_deployment["id"]) or {}),
                 "provisioning_operation": self.get_gateway_release_operation(
                     selected_deployment["provisioning_operation_id"],
                 ) if selected_deployment.get("provisioning_operation_id") else None}
                if selected_deployment else None
            ),
            "gateway_auth_type": gateway["auth_type"] if gateway else None,
            "gateway_credential_ciphertext": gateway["credential_ciphertext"] if gateway else None,
            "gateway_config": gateway["config"] if gateway else {},
        }
        if gateway and route:
            own_publication = self._find(self.gateway_publications, route["current_publication_id"])
            try:
                revision = _managed_model_request_revision(
                    own_publication, self.effective_gateway_releases.get(gateway["id"]),
                    gateway.get("config") or {},
                )
            except ValueError:
                return None
            if revision is not None:
                result["gateway_api_revision"] = revision
        return result

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

    def claim_runtime_diagnostic(
        self, runtime_id: UUID, marker: str, checked_at: datetime, before: datetime,
    ) -> bool:
        runtime = self._find(self.runtimes, runtime_id)
        if runtime is None or not runtime["enabled"] or (
            runtime.get("last_checked_at") is not None and runtime["last_checked_at"] > before
        ):
            return False
        self.update_runtime_health(runtime_id, "unknown", marker, checked_at)
        return True

    def finish_runtime_diagnostic(
        self, runtime_id: UUID, marker: str, status: str, message: str, checked_at: datetime,
    ) -> bool:
        runtime = self._find(self.runtimes, runtime_id)
        if runtime is None or runtime.get("health_message") != marker:
            return False
        self.update_runtime_health(runtime_id, status, message, checked_at)
        return True
