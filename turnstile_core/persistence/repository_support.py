from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from ..domain.data_access import DataAccessScope


def _managed_model_request_revision(
    publication: Mapping[str, Any] | None, effective_id: UUID | None,
    config: Mapping[str, Any],
) -> str | None:
    record = config.get("managed_route_program_initialization")
    marker = (publication or {}).get("resource_manifest", {}).get("model_route_addition")
    if record is None or marker is None:
        return None
    from ..domain.storage_routes import ManagedProgramInitialization, canonical_document

    initialized = ManagedProgramInitialization.model_validate_json(canonical_document(record))
    installed = initialized.installation
    if (publication is None or str(effective_id) != str(initialized.previous_publication_id)
            or str(publication.get("base_release_id")) != str(effective_id)
            or publication.get("status") != "active"
            or publication.get("publication_kind") != "model_add"
            or str(publication.get("gateway_profile_id")) != installed.program.scope.target_id
            or publication.get("apim_revision") != installed.program.api_revision
            or publication.get("policy_sha256") != installed.program.program_sha256
            or not isinstance(marker, dict) or type(marker.get("version")) is not int
            or marker["version"] != 1 or marker.get("program_release_id") != str(effective_id)):
        raise ValueError("Managed model request publication changed")
    bindings = publication.get("desired_spec", {}).get("bindings", ())
    if (len(bindings) != 1 or str(marker.get("alias", "")).casefold()
            != str(bindings[0].get("model", {}).get("model_key", "")).casefold()):
        raise ValueError("Managed model request alias changed")
    return installed.program.api_revision


def _managed_program_publication(
    previous_row: Mapping[str, Any] | None, config: Mapping[str, Any],
) -> dict[str, Any] | None:
    record = config.get("managed_route_program_initialization")
    if record is None:
        result = dict(previous_row) if previous_row is not None else None
        if result is None:
            return None
        if config.get("fixed_route_program_upgrade_history"):
            from ..domain.storage_routes import FixedProgramInstallation, canonical_document

            original = FixedProgramInstallation.model_validate_json(canonical_document(
                result["resource_manifest"]["fixed_route_program"],
            ))
            installed = _registered_program_installation(str(result["id"]), original, {
                "fixed_route_program_upgrade_history": config[
                    "fixed_route_program_upgrade_history"
                ],
            })
            result["resource_manifest"] = {
                **result["resource_manifest"],
                "fixed_route_program": installed.model_dump(mode="json"),
            }
        return result
    if previous_row is None:
        raise ValueError("Managed initialization requires its original publication")
    from ..domain.control_plane import GatewayPublication
    from ..domain.storage_routes import ManagedProgramInitialization, canonical_document

    initialized = ManagedProgramInitialization.model_validate_json(canonical_document(record))
    initialized.require_previous(GatewayPublication.model_validate(previous_row))
    installed = initialized.installation
    return {
        **previous_row,
        "apim_revision": installed.program.api_revision,
        "policy_sha256": installed.program.program_sha256,
        "resource_manifest": {
            **previous_row["resource_manifest"],
            "fixed_route_program": installed.model_dump(mode="json"),
            "fixed_route_snapshot": initialized.snapshot.model_dump(mode="json"),
            "fixed_route_head": initialized.head.model_dump(mode="json"),
        },
    }


def _managed_program_initialization_config(
    previous_row: Mapping[str, Any] | None, config: Mapping[str, Any],
    descriptor: Mapping[str, Any], expected_config_sha256: str,
) -> dict[str, Any] | None:
    from ..domain.control_plane import GatewayPublication
    from ..domain.storage_routes import (
        ManagedProgramInitialization,
        canonical_document,
        document_digest,
    )

    initialized = ManagedProgramInitialization.model_validate_json(canonical_document(descriptor))
    if previous_row is None:
        raise ValueError("Managed initialization requires its original publication")
    initialized.require_previous(GatewayPublication.model_validate(previous_row))
    if config.get("fixed_route_program_upgrade") is not None:
        raise ValueError("Managed initialization cannot replace a program upgrade")
    recorded = config.get("managed_route_program_initialization")
    if recorded is not None:
        if recorded != initialized.model_dump(mode="json"):
            raise ValueError("Managed initialization already exists")
        return None
    if document_digest(config) != expected_config_sha256:
        raise ValueError("Managed initialization Gateway configuration changed")
    return {**config, "managed_route_program_initialization": initialized.model_dump(mode="json")}


def _registered_program_installation(
    publication_id: str, installation: Any, config: Mapping[str, Any],
) -> Any:
    from ..domain.storage_routes import FixedProgramUpgrade, canonical_document

    records = list(config.get("fixed_route_program_upgrade_history", []))
    if config.get("fixed_route_program_upgrade") is not None:
        records.append(config["fixed_route_program_upgrade"])
    approved_programs = [installation.program.program_sha256]
    for record in records:
        upgrade = FixedProgramUpgrade.model_validate_json(canonical_document(record))
        upgrade.require_previous(publication_id, installation)
        predecessors = upgrade.installation.predecessor_program_sha256
        if predecessors and predecessors != tuple(
            value for value in approved_programs
            if value != upgrade.installation.legacy_program_sha256
        ):
            raise ValueError("MODEL_PROGRAM_PREDECESSOR_HISTORY_CHANGED")
        installation = upgrade.installation
        approved_programs.append(installation.program.program_sha256)
    return installation


def _fixed_program_upgrade_config(
    previous_row: Mapping[str, Any] | None, config: Mapping[str, Any],
    descriptor: Mapping[str, Any], expected_config_sha256: str,
) -> dict[str, Any] | None:
    from ..domain.control_plane import GatewayPublication
    from ..domain.storage_routes import (
        FixedProgramInstallation,
        FixedProgramUpgrade,
        canonical_document,
        document_digest,
    )

    upgrade = FixedProgramUpgrade.model_validate_json(canonical_document(descriptor))
    if previous_row is None:
        raise ValueError("Program upgrade requires a current publication")
    previous = GatewayPublication.model_validate(previous_row)
    raw_installation = previous.resource_manifest.get("fixed_route_program")
    if not isinstance(raw_installation, dict):
        raise ValueError("Program upgrade requires an installed fixed program")
    installation = FixedProgramInstallation.model_validate_json(canonical_document(
        raw_installation,
    ))
    if (previous.status.value != "active"
            or str(previous.gateway_profile_id) != installation.program.scope.target_id):
        raise ValueError("Program upgrade current publication changed")
    recorded = config.get("fixed_route_program_upgrade")
    current = _registered_program_installation(str(previous.id), installation, config)
    if recorded == upgrade.model_dump(mode="json"):
        return None
    upgrade.require_previous(str(previous.id), current)
    if document_digest(config) != expected_config_sha256:
        raise ValueError("Program upgrade Gateway configuration changed")
    updated = {**config, "fixed_route_program_upgrade": upgrade.model_dump(mode="json")}
    if recorded is not None:
        updated["fixed_route_program_upgrade_history"] = [
            *config.get("fixed_route_program_upgrade_history", []), recorded,
        ]
    _registered_program_installation(str(previous.id), installation, updated)
    return updated


def _model_addition_manifest(
    previous_row: Mapping[str, Any] | None, desired_spec: Mapping[str, Any], digest: str,
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    from ..domain.control_plane import GatewayPublication, GatewayReleaseSpec
    from ..domain.storage_routes import document_digest

    if previous_row is None or set(requested) != {"model_route_addition"}:
        raise ValueError("Model addition requires an existing fixed program")
    previous = GatewayPublication.model_validate(previous_row)
    spec = GatewayReleaseSpec.model_validate(desired_spec)
    marker = requested["model_route_addition"]
    if (not isinstance(marker, dict)
            or set(marker) != {"version", "program_release_id", "alias"}
            or type(marker["version"]) is not int or marker["version"] != 1
            or marker["program_release_id"] != str(previous.id)
            or previous.status.value != "active"
            or "fixed_route_program" not in previous.resource_manifest
            or spec.gateway_profile_id != previous.gateway_profile_id
            or len(spec.bindings) != 1 or len(spec.discovery_models) != 1 or spec.removed_models
            or marker["alias"] != spec.bindings[0].model.model_key
            or spec.discovery_models[0].id != marker["alias"]
            or any(binding.model.model_key.casefold() == marker["alias"].casefold()
                   for binding in previous.desired_spec.bindings)
            or digest != document_digest({**requested, "desired_spec": dict(desired_spec)})):
        raise ValueError("Model addition cannot contain or replace existing models")
    return deepcopy(dict(requested))


def _model_removal_manifest(
    previous_row: Mapping[str, Any] | None, source_row: Mapping[str, Any] | None,
    desired_spec: Mapping[str, Any], digest: str, requested: Mapping[str, Any],
) -> dict[str, Any]:
    from ..domain.control_plane import GatewayPublication, GatewayReleaseSpec
    from ..domain.storage_routes import document_digest

    if previous_row is None or source_row is None or set(requested) != {"model_route_removal"}:
        raise ValueError("Model removal requires its current program and source publication")
    previous = GatewayPublication.model_validate(previous_row)
    source = GatewayPublication.model_validate(source_row)
    spec = GatewayReleaseSpec.model_validate(desired_spec)
    marker = requested["model_route_removal"]
    fields = {"version", "program_release_id", "source_publication_id", "source_spec_sha256",
              "model_id", "alias"}
    if (not isinstance(marker, dict) or set(marker) != fields
            or type(marker["version"]) is not int or marker["version"] != 1
            or previous.status.value != "active" or source.status.value != "active"
            or source.publication_kind.value != "model_add"
            or "model_route_addition" not in source.resource_manifest
            or source.gateway_profile_id != previous.gateway_profile_id
            or spec.gateway_profile_id != previous.gateway_profile_id
            or spec.bindings or spec.discovery_models or len(spec.removed_models) != 1
            or len(source.desired_spec.bindings) != 1
            or marker["program_release_id"] != str(previous.id)
            or marker["source_publication_id"] != str(source.id)
            or marker["source_spec_sha256"] != source.desired_spec_sha256
            or marker["model_id"] != str(spec.removed_models[0].model_id)
            or marker["alias"] != spec.removed_models[0].model_key
            or marker["alias"] != source.desired_spec.bindings[0].model.model_key
            or digest != document_digest({**requested, "desired_spec": dict(desired_spec)})):
        raise ValueError("Model removal source or target binding changed")
    return deepcopy(dict(requested))


def _model_addition_credential_retry(
    publication: Mapping[str, Any], previous: Mapping[str, Any] | None, probe_count: int,
) -> dict[str, Any]:
    from ..domain.storage_routes import document_digest

    manifest = publication.get("resource_manifest") or {}
    message = publication.get("error_message") or ""
    try:
        diagnostic = json.loads(message.removeprefix("APIM management failed: "))
    except (ValueError, TypeError):
        diagnostic = None
    if (publication.get("status") != "failed"
            or publication.get("publication_kind") != "model_add"
            or set(manifest) != {"model_route_addition"}
            or publication.get("apim_revision") is not None
            or publication.get("policy_sha256") is not None
            or type(probe_count) is not int or probe_count != 0
            or previous is None or publication.get("base_release_id") != previous.get("id")
            or publication.get("error_code") != "PolicyCompilationError"
            or not isinstance(diagnostic, dict) or diagnostic.get("code") != "AuthorizationFailed"
            or diagnostic.get("method") != "POST" or diagnostic.get("status") != 403
            or not re.fullmatch(r"/namedValues/[A-Za-z0-9._-]{1,256}/listValue",
                                str(diagnostic.get("path", "")))):
        raise ValueError(
            "Model addition recovery requires an unchanged zero-probe credential denial",
        )
    _model_addition_manifest(previous, publication["desired_spec"],
        publication["desired_spec_sha256"], manifest)
    return {"kind": "model_addition_credential_read_recovery",
        "attempt_count": publication["attempt_count"], "manifest_sha256": document_digest(manifest),
        "previous_error_code": publication["error_code"],
        "previous_error_sha256": document_digest({"message": message})}


def _program_migration_manifest(
    previous_row: Mapping[str, Any] | None, desired_spec: Mapping[str, Any], digest: str,
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    from ..domain.control_plane import (
        GatewayPublication,
        GatewayReleaseSpec,
        program_migration_spec_matches,
    )
    from ..domain.storage_routes import ProgramIdentity, canonical_document, document_digest

    if previous_row is None or set(requested) != {"fixed_route_migration"}:
        raise ValueError("Program migration requires an existing release and exact manifest")
    previous = GatewayPublication.model_validate(previous_row)
    migration = requested["fixed_route_migration"]
    if not isinstance(migration, dict) or set(migration) - {"known_unavailable_bindings"} != {
        "version", "request_id", "previous_publication_id", "previous_revision",
        "previous_spec_sha256", "previous_policy_sha256", "installation",
    }:
        raise ValueError("Program migration descriptor fields are invalid")
    program = ProgramIdentity.model_validate_json(
        canonical_document(migration["installation"]["program"]),
    )
    known_unavailable = migration.get("known_unavailable_bindings", {})
    previous_bindings = {binding.model.model_key: document_digest(binding)
                         for binding in previous.desired_spec.bindings}
    if not isinstance(known_unavailable, dict) or any(
        previous_bindings.get(alias) != digest for alias, digest in known_unavailable.items()
    ):
        raise ValueError("Known unavailable upstream binding changed")
    candidate = GatewayReleaseSpec.model_validate(desired_spec)
    expected = candidate.model_dump(mode="json")
    if (
        type(migration["version"]) is not int or migration["version"] != 1
        or str(UUID(str(migration["request_id"]))) != migration["request_id"]
        or migration["previous_publication_id"] != str(previous.id)
        or migration["previous_revision"] != previous.apim_revision
        or migration["previous_spec_sha256"] != previous.desired_spec_sha256
        or migration["previous_policy_sha256"] != previous.policy_sha256
        or previous.status.value != "active" or not previous.desired_spec.bindings
        or program.scope.target_id != str(previous.gateway_profile_id)
        or program.scope.gateway_id != "managed" or program.api_revision == previous.apim_revision
        or "fixed_route_program" in previous.resource_manifest
        or not program_migration_spec_matches(previous.desired_spec, candidate)
        or digest != document_digest({"fixed_route_migration": migration, "desired_spec": expected})
    ):
        raise ValueError("Program migration cannot change its previous release or model routes")
    return deepcopy(dict(requested))


def _program_migration_retry(
    publication: Mapping[str, Any], previous: Mapping[str, Any] | None, probe_count: int,
) -> dict[str, Any]:
    from ..domain.control_plane import GatewayPublication
    from ..domain.storage_routes import (
        FixedProgramInstallation,
        canonical_document,
        document_digest,
    )

    manifest = publication.get("resource_manifest") or {}
    if (publication.get("status") != "failed"
            or publication.get("publication_kind") != "route_reconcile"
            or "fixed_route_migration" not in manifest
            or set(manifest) - {"fixed_route_migration", "route_preparation", "route_credentials",
                                "route_last_progress_attempt"}
            or publication.get("apim_revision") is not None
            or publication.get("policy_sha256") is not None
            or type(probe_count) is not int or probe_count != 0
            or previous is None or publication.get("base_release_id") != previous.get("id")
            or manifest["fixed_route_migration"].get("request_id") != str(publication["id"])):
        raise ValueError("Program migration retry requires an unchanged zero-probe preparation")
    _program_migration_manifest(
        previous, publication["desired_spec"], publication["desired_spec_sha256"],
        {"fixed_route_migration": manifest["fixed_route_migration"]},
    )
    prepared = "route_preparation" in manifest
    if set(manifest) != {"fixed_route_migration"}:
        candidate = GatewayPublication.model_validate(publication)
        installation = FixedProgramInstallation.model_validate_json(canonical_document(
            manifest["fixed_route_migration"]["installation"],
        ))
        preparation = manifest.get("route_preparation")
        expected = document_digest({
            "publication_id": str(candidate.id),
            "desired_spec_sha256": candidate.desired_spec_sha256,
            "specification": candidate.desired_spec.model_dump(mode="json"),
            "installation": installation.model_dump(mode="json"),
        })
        progress = manifest.get("route_last_progress_attempt", 0)
        if (not isinstance(preparation, dict)
                or type(preparation.get("version")) is not int or preparation["version"] != 1
                or preparation.get("context_sha256") != expected
                or set(preparation) - {"version", "context_sha256", "connections", "credentials",
                                       "oauth"}
                or any(not isinstance(preparation.get(field, {}), dict)
                       or len(preparation.get(field, {})) > 10000
                       for field in ("connections", "credentials", "oauth"))
                or type(progress) is not int or not 0 <= progress <= candidate.attempt_count):
            raise ValueError("Program migration preparation checkpoint changed")
        credentials = manifest.get("route_credentials")
        if "route_credentials" in manifest and (
            not isinstance(credentials, dict) or set(credentials) != {"version", "bindings"}
            or type(credentials.get("version")) is not int or credentials["version"] != 1
            or not isinstance(credentials.get("bindings"), dict)
            or len(credentials["bindings"]) > 10000
        ):
            raise ValueError("Program migration credential checkpoint changed")
    return {
        "recovery": ("program_migration_from_preparation" if prepared
                     else "program_migration_before_preparation"),
        "preserved_manifest_sha256": document_digest(manifest),
        "attempt_count": publication["attempt_count"],
        "previous_error_code": publication.get("error_code"),
        "previous_error_message": publication.get("error_message"),
        "probe_requests": probe_count,
    }


def _require_program_migration_activation(
    publication: Mapping[str, Any], previous: Mapping[str, Any] | None,
) -> None:
    from ..domain.storage_routes import (
        ActiveRouteHead,
        RoutePublicationTask,
        RouteTransitionReceipt,
        canonical_document,
        document_digest,
    )

    manifest = publication.get("resource_manifest") or {}
    migration = manifest.get("fixed_route_migration")
    activation = manifest.get("route_activation")
    task_record = manifest.get("route_configuration")
    if (
        not isinstance(migration, dict) or not isinstance(activation, dict)
        or not isinstance(task_record, dict)
    ):
        raise ValueError("Program migration activation confirmation is incomplete")
    _program_migration_manifest(
        previous, publication["desired_spec"], publication["desired_spec_sha256"],
        {"fixed_route_migration": migration},
    )
    task = RoutePublicationTask.model_validate_json(canonical_document(task_record))
    task.require_publication(
        str(publication["id"]), str(publication["base_release_id"]),
        publication["desired_spec_sha256"], str(publication["gateway_profile_id"]),
    )
    expected = {
        "version": 1, "previous_revision": migration["previous_revision"],
        "revision": task.plan.program.api_revision,
        "release_name": "fixed-program-" + migration["request_id"], "outcome": "confirmed",
        "context_sha256": document_digest({
            "migration": migration, "task": task.context_sha256,
            "publication_id": str(publication["id"]),
        }),
    }
    intent = activation.get("intent")
    if (
        publication["status"] != "promoting"
        or migration["request_id"] != str(publication["id"])
        or manifest.get("route_program_promotion") != expected
        or activation.get("outcome") != "confirmed" or not isinstance(intent, dict)
        or task.program_migration_sha256 != document_digest(migration)
        or task.known_unavailable_bindings != migration.get("known_unavailable_bindings", {})
        or manifest.get("fixed_route_program") != migration["installation"]
        or task.plan.program.model_dump(mode="json") != migration["installation"]["program"]
        or publication.get("apim_revision") != task.plan.program.api_revision
        or publication.get("policy_sha256") != task.plan.program.program_sha256
        or intent.get("endpoint") != task.storage_endpoint
        or intent.get("table") != task.storage_table
    ):
        raise ValueError("Program migration activation confirmation changed")
    head = ActiveRouteHead.model_validate_json(canonical_document(intent["head"]))
    receipt = RouteTransitionReceipt.model_validate_json(canonical_document(intent["receipt"]))
    head.approval.require_same_configuration(task.plan.snapshot.approval)
    if (
        receipt.scope != head.approval.scope or receipt.after_sha256 != document_digest(head)
        or receipt.route_version != head.approval.route_version
        or receipt.approval_id != head.approval.approval_id
    ):
        raise ValueError("Program migration activation confirmation receipt changed")


ModelActivationValidator = Callable[
    [Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any] | None], None,
]


def _configuration_rollback_values(
    operation: Mapping[str, Any], target_row: Mapping[str, Any], prior_row: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, str]]:
    from ..domain.control_plane import GatewayPublication, GatewayPublicationIntent
    from ..domain.storage_routes import document_digest

    target = GatewayPublication.model_validate(target_row)
    prior = GatewayPublication.model_validate(prior_row)
    confirmation = operation.get("confirmation_sha256")
    installation = target.resource_manifest.get("fixed_route_program")
    if (
        operation.get("operation_kind") != "rollback"
        or operation.get("status") not in {"validating_dependencies", "verifying_readback"}
        or str(operation.get("target_release_id")) != str(target.id)
        or str(operation.get("prior_release_id")) != str(prior.id) or target.id == prior.id
        or str(operation.get("gateway_profile_id")) != str(target.gateway_profile_id)
        or prior.gateway_profile_id != target.gateway_profile_id
        or not isinstance(confirmation, str) or not re.fullmatch(r"[0-9a-f]{64}", confirmation)
        or not isinstance(installation, dict) or not installation
        or installation != prior.resource_manifest.get("fixed_route_program")
        or any(marker in target.resource_manifest for marker in (
            "model_route_addition", "model_route_removal",
        ))
    ):
        raise ValueError("Fixed route rollback requires matching releases and confirmation")
    spec = {**target.desired_spec.model_dump(mode="json"), "removed_models": []}
    GatewayPublicationIntent.model_validate({
        "gateway_profile_id": target.gateway_profile_id,
        "publication_kind": "route_reconcile", "desired_spec": spec,
    })
    detail = {
        "operation_id": str(operation["id"]), "target_release_id": str(target.id),
        "prior_release_id": str(prior.id), "confirmation_sha256": confirmation,
    }
    return spec, document_digest({"route_rollback": detail, "desired_spec": spec}), detail


def _validate_gateway_deployment_snapshots(
    gateway_profile_id: UUID,
    desired_spec: Mapping[str, Any],
    deployments: Sequence[Mapping[str, Any]],
) -> None:
    by_id = {str(row["id"]): row for row in deployments}
    for snapshot in desired_spec.get("gateway_deployments") or ():
        current = by_id.get(str(snapshot["deployment_id"]))
        if (
            current is None or current.get("retired_at") is not None
            or str(current["apim_target_id"]) != str(gateway_profile_id)
            or str(current["native_gateway_id"]).casefold()
            != str(snapshot["native_gateway_id"]).casefold()
            or str(current.get("endpoint_url") or "").rstrip("/")
            != str(snapshot["endpoint_url"]).rstrip("/")
        ):
            raise ValueError("Gateway deployment changed before publication; refresh and retry")


def _activated_runtime_credentials(
    config: Mapping[str, Any], binding: Mapping[str, Any], manifest: Mapping[str, Any],
) -> dict[str, Any]:
    from ..domain.control_plane import GatewayModelBinding
    from ..services.route_publication import route_credential_source

    updated = dict(config)
    if config.get("credential_provisioned") is False and (
        binding.get("runtime_config") or {}
    ).get("credential_provisioned") is True:
        updated["credential_provisioned"] = True
    secret_id = binding.get("key_vault_secret_id")
    credentials = (manifest.get("route_credentials") or {}).get("bindings", {})
    if secret_id is None and credentials:
        source = route_credential_source(GatewayModelBinding.model_validate(binding))
        secret_id = credentials.get(source)
    if secret_id is not None:
        updated["key_vault_secret_id"] = secret_id
    return updated


def _require_multi_target_model_identity(
    existing: Mapping[str, Any], model_spec: Mapping[str, Any], logical_model_id: UUID | None,
) -> None:
    if logical_model_id is None or UUID(str(existing["id"])) != logical_model_id:
        raise ValueError("The model alias is already active")
    fields = (
        "display_name", "family_key", "capabilities", "context_window",
        "input_cost_per_million", "output_cost_per_million", "cached_cost_per_million",
        "cache_write_cost_per_million", "price_reference", "price_discount_percent",
        "list_input_cost_per_million", "list_output_cost_per_million",
        "list_cached_cost_per_million", "list_cache_write_cost_per_million",
        "allowed_roles", "assignment_required",
    )
    matching = all(existing.get(name) == model_spec.get(name) for name in fields)
    same_source = existing.get("price_source", "manual") == model_spec.get("price_source", "manual")
    same_tiers = (existing.get("context_price_tiers") or []) == (
        model_spec.get("context_price_tiers") or []
    )
    if not matching or not same_source or not same_tiers:
        raise ValueError("Multi-target child conflicts with the logical model identity")



def _activation_runtime_config(binding: Mapping[str, Any]) -> dict[str, Any]:
    config = {
        **(binding.get("runtime_config") or {}),
        "path": (
            "/chat/completions"
            if binding["api_format"] == "openai_chat"
            else "/v1/messages"
        ),
        "api_format": binding["api_format"],
        "streaming_mode": binding["streaming_mode"],
        "control_plane_managed": True,
        "backend_url": binding.get("backend_url"),
        "backend_path": binding["backend_path"],
        "auth_strategy": binding["auth_strategy"],
        "named_value_name": binding.get("named_value_name"),
        "key_vault_secret_id": binding.get("key_vault_secret_id"),
        "managed_identity_resource": binding.get("managed_identity_resource"),
    }
    if binding["runtime_brand_key"] != "microsoft_foundry":
        return config
    inference_endpoint = str(config.get("inference_endpoint") or "")
    if inference_endpoint:
        inference = urlsplit(inference_endpoint)
        backend_url = f"{inference.scheme}://{inference.netloc}"
    else:
        backend_url = str(config.get("project_endpoint") or binding.get("backend_url"))
    config.update(
        path="/chat/completions",
        api_format="openai_chat",
        backend_url=backend_url.rstrip("/"),
        backend_path="/openai/v1/chat/completions",
        max_tokens_field="max_completion_tokens",
        supports_temperature=False,
    )
    config.pop("anthropic_version", None)
    if binding["api_format"] == "openai_images":
        config["streaming_mode"] = "native"
    return config


UsageDirectoryStatus = Literal["active", "archived", "historical", "unattributed"]
USAGE_DIRECTORY_STATUSES: tuple[UsageDirectoryStatus, ...] = (
    "active", "archived", "historical", "unattributed",
)


@dataclass(frozen=True)
class UsageDirectoryScope:
    statuses: tuple[UsageDirectoryStatus, ...]
    active_organizations: tuple[str, ...]
    archived_organizations: tuple[str, ...]
    active_departments: Mapping[str, str]
    archived_departments: tuple[str, ...]
    archived_users: tuple[str, ...]

    def status(
        self, organization_id: str, department_id: str, user_id: str,
    ) -> UsageDirectoryStatus:
        if organization_id == "unattributed" or department_id == "unattributed":
            return "unattributed"
        if (
            organization_id in self.archived_organizations
            or department_id in self.archived_departments
            or user_id in self.archived_users
        ):
            return "archived"
        if (
            organization_id in self.active_organizations
            and self.active_departments.get(department_id) == organization_id
        ):
            return "active"
        return "historical"


@dataclass(frozen=True)
class UsageFilters:
    organization_id: str | tuple[str, ...] | None = None
    department_id: str | tuple[str, ...] | None = None
    project_id: str | tuple[str, ...] | None = None
    agent_id: str | tuple[str, ...] | None = None
    model_id: str | tuple[str, ...] | None = None
    user_id: str | tuple[str, ...] | None = None
    # The access points a request arrived through, e.g. 'Microsoft Foundry via APIM' or
    # 'GitHub Copilot CLI'. Matched on the display name because token_usage.runtime is the
    # name itself; the registry UUID is never written into telemetry. Several names are
    # accepted because one gateway fronts several runtimes, and "spend through APIM" has to
    # be answerable without inventing a channel column the ingestion path does not produce.
    runtime: tuple[str, ...] | None = None
    status_code: int | None = None
    apim_target_id: UUID | None = None
    gateway_deployment_id: UUID | None = None

    allowed_department_ids: tuple[str, ...] | None = None

    directory_scope: UsageDirectoryScope | None = None

    data_scope: DataAccessScope | None = None


CACHE_DIMENSION_FIELDS = {
    "organization": "organization_id",
    "department": "department_id",
    "project": "project_id",
    "agent": "agent_id",
    "user": "user_id",
    "model": "model_id",
    "runtime": "runtime",
}


def cache_scope_for_filters(filters: UsageFilters) -> tuple[str, tuple[str, ...]] | None:
    """Use the bounded APIM metric only for an unfiltered global view."""
    if filters.allowed_department_ids is not None or filters.data_scope is not None:
        return None
    if any(
        (
            filters.organization_id,
            filters.department_id,
            filters.project_id,
            filters.agent_id,
            filters.user_id,
            filters.model_id,
            filters.runtime,
            filters.status_code,
            filters.apim_target_id,
            filters.gateway_deployment_id,
            filters.directory_scope,
        )
    ):
        return None
    return "global", ("all",)


def cache_distribution_supported(dimension: str, filters: UsageFilters) -> bool:
    del dimension, filters
    return False


class BudgetConstraintViolation(ValueError):
    pass


def proportional_budget_tree(
    budgets: Sequence[dict[str, Any]], organization_id: str,
    budget_mode: str, amount: int | Decimal, warning_threshold_percent: int,
) -> list[dict[str, Any]]:
    root = next(row for row in budgets
                if row["scope_type"] == "organization" and row["scope_id"] == organization_id)
    previous_mode = root.get("budget_mode", "tokens")
    if previous_mode == budget_mode:
        raise BudgetConstraintViolation("Organization budget unit changed before the switch")
    old_field = "usd_limit" if previous_mode == "usd" else "token_limit"
    new_field = "usd_limit" if budget_mode == "usd" else "token_limit"
    departments = [row for row in budgets
                   if row["scope_type"] == "department"
                   and row["parent_scope_id"] == organization_id]
    department_ids = {row["scope_id"] for row in departments}
    people = [row for row in budgets if row["scope_type"] == "user"
              and row["parent_scope_id"] in department_ids]
    tree = [root, *departments, *people]
    original = {(row["scope_type"], row["scope_id"]): row for row in tree}
    updated: dict[tuple[str, str], dict[str, Any]] = {}
    allocated: dict[tuple[str, str], Decimal] = {}
    with localcontext() as context:
        context.prec = 50
        for row in tree:
            value = Decimal(amount)
            if row is not root:
                parent_type = "organization" if row["scope_type"] == "department" else "department"
                parent_key = (parent_type, row["parent_scope_id"])
                if row.get("budget_mode", "tokens") != previous_mode:
                    raise BudgetConstraintViolation("Budget hierarchy must use one unit")
                value = (Decimal(updated[parent_key][new_field]) * Decimal(row[old_field])
                         / Decimal(original[parent_key][old_field]))
                if budget_mode == "usd" and value > 0:
                    value = max(Decimal(1), value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
                else:
                    value = value.quantize(Decimal(1), rounding=ROUND_DOWN)
                if value <= 0:
                    raise BudgetConstraintViolation(
                        "New budget is too small to preserve every configured allocation"
                    )
                allocated[parent_key] = allocated.get(parent_key, Decimal(0)) + value
            updated[(row["scope_type"], row["scope_id"])] = {
                **row, "budget_mode": budget_mode,
                "token_limit": int(value) if budget_mode == "tokens" else None,
                "usd_limit": value if budget_mode == "usd" else None,
                "warning_threshold_percent": (
                    warning_threshold_percent if row is root else row["warning_threshold_percent"]
                ),
            }
        if budget_mode == "usd":
            for parent_key, child_total in allocated.items():
                shortfall = child_total - Decimal(updated[parent_key][new_field])
                if shortfall > 0:
                    raise BudgetConstraintViolation(
                        "New budget is too small: rounded allocations exceed "
                        f"{parent_key[0]} budget by USD {shortfall:.2f}"
                    )
    return list(updated.values())


def attach_charts(
    reports: Sequence[dict[str, Any]], charts: Sequence[Any]
) -> list[dict[str, Any]]:
    """Nest each report's charts under it, preserving the order the rows arrived in.

    Both repositories fetch reports and their charts as two flat result sets, so the
    grouping is shared rather than written twice and allowed to diverge.
    """
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for chart in charts:
        if chart is not None:
            grouped.setdefault(chart["report_id"], []).append(dict(chart))
    return [{**report, "charts": grouped.get(report["id"], [])} for report in reports]


def report_for_viewer(report: Mapping[str, Any], viewer_id: str) -> dict[str, Any]:
    """Expose authenticated creator semantics without leaking the compatibility owner.

    `owner_id` remains in PostgreSQL for the temporarily older production build. New code
    authorizes through `created_by`, and the API keeps its existing `owner_id` field by
    presenting that creator under the old response name.
    """
    row = dict(report)
    creator = str(row.pop("created_by", row["owner_id"]))
    row["owner_id"] = creator
    row["can_manage"] = creator == viewer_id
    return row


def conversation_for_creator(conversation: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(conversation)
    row["owner_id"] = str(row.pop("created_by", row["owner_id"]))
    return row
