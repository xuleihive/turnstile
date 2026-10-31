from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ..domain.model_router_v3 import RouterSpecV3


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
            from ..domain.storage_routes import (
                FixedProgramInstallation,
                FixedProgramUpgrade,
                canonical_document,
            )

            original = FixedProgramInstallation.model_validate_json(canonical_document(
                result["resource_manifest"]["fixed_route_program"],
            ))
            current_upgrade = (
                FixedProgramUpgrade.model_validate_json(
                    canonical_document(config["fixed_route_program_upgrade"]),
                )
                if config.get("fixed_route_program_upgrade") is not None else None
            )
            if current_upgrade is not None and (
                current_upgrade.previous_publication_id != str(result["id"])
                and current_upgrade.installation == original
            ):
                installed = _registered_program_installation(str(result["id"]), original, config)
            else:
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
    upgrades = [
        FixedProgramUpgrade.model_validate_json(canonical_document(record))
        for record in records
    ]
    absorbed = -1
    for index, upgrade in enumerate(upgrades):
        if (
            upgrade.previous_publication_id != publication_id
            and upgrade.installation == installation
        ):
            absorbed = index
    if absorbed >= 0:
        for index in range(1, absorbed + 1):
            upgrades[index].require_previous(
                upgrades[index].previous_publication_id, upgrades[index - 1].installation,
            )
        approved_programs = [
            *([installation.legacy_program_sha256] if installation.legacy_program_sha256 else []),
            *installation.predecessor_program_sha256,
            installation.program.program_sha256,
        ]
        upgrades = upgrades[absorbed + 1:]
    else:
        approved_programs = [installation.program.program_sha256]
    for upgrade in upgrades:
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


def _approved_preparation_installations(
    publication_id: str, installation: Any, config: Mapping[str, Any],
) -> tuple[Any, tuple[Any, ...]]:
    from ..domain.storage_routes import FixedProgramUpgrade, canonical_document

    approved = _registered_program_installation(publication_id, installation, config)
    records = list(config.get("fixed_route_program_upgrade_history", []))
    if config.get("fixed_route_program_upgrade") is not None:
        records.append(config["fixed_route_program_upgrade"])
    candidates = (installation, *(FixedProgramUpgrade.model_validate_json(
        canonical_document(record),
    ).installation for record in records))
    return approved, candidates


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
    *, config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from ..domain.control_plane import GatewayPublication
    from ..domain.storage_routes import (
        FixedProgramInstallation,
        canonical_document,
        document_digest,
    )

    manifest = publication.get("resource_manifest") or {}
    configuration = config or {}
    previous = _managed_program_publication(previous, {
        "managed_route_program_initialization": configuration.get(
            "managed_route_program_initialization",
        ),
    })
    message = publication.get("error_message") or ""
    try:
        diagnostic = json.loads(message.removeprefix("APIM management failed: "))
    except (ValueError, TypeError):
        diagnostic = None
    budget_denied = (
        publication.get("error_code") == "ValueError"
        and message == "Budget publication target is not enabled"
    )
    credential_denied = (
        publication.get("error_code") == "PolicyCompilationError"
        and isinstance(diagnostic, dict) and diagnostic.get("code") == "AuthorizationFailed"
        and diagnostic.get("method") == "POST" and diagnostic.get("status") == 403
        and re.fullmatch(r"/namedValues/[A-Za-z0-9._-]{1,256}/listValue",
                         str(diagnostic.get("path", ""))) is not None
    )
    if (publication.get("status") != "failed"
            or publication.get("publication_kind") != "model_add"
            or "model_route_addition" not in manifest
            or set(manifest) - {"model_route_addition", "route_preparation", "route_credentials",
                                "route_last_progress_attempt"}
            or publication.get("apim_revision") is not None
            or publication.get("policy_sha256") is not None
            or type(probe_count) is not int or probe_count != 0
            or previous is None or publication.get("base_release_id") != previous.get("id")
            or not (budget_denied or credential_denied)):
        raise ValueError(
            "Model addition recovery requires an unchanged zero-probe preparation denial",
        )
    _model_addition_manifest(
        previous, publication["desired_spec"], publication["desired_spec_sha256"],
        {"model_route_addition": manifest["model_route_addition"]},
    )
    if set(manifest) != {"model_route_addition"}:
        installation = FixedProgramInstallation.model_validate_json(canonical_document(
            previous["resource_manifest"]["fixed_route_program"],
        ))
        candidate = GatewayPublication.model_validate(publication)
        _, installations = _approved_preparation_installations(
            str(previous["id"]), installation, configuration,
        )
        preparation = manifest.get("route_preparation")
        for approved in installations:
            expected = document_digest({
                "publication_id": str(candidate.id),
                "desired_spec_sha256": candidate.desired_spec_sha256,
                "specification": candidate.desired_spec.model_dump(mode="json"),
                "installation": approved.model_dump(mode="json"),
            })
            if isinstance(preparation, dict) and preparation.get("context_sha256") == expected:
                installation = approved
                break
        _validate_preparation_checkpoint(
            candidate, manifest, installation,
        )
    return {"kind": "model_addition_budget_admission_recovery" if budget_denied
                   else "model_addition_credential_read_recovery",
        "attempt_count": publication["attempt_count"], "manifest_sha256": document_digest(manifest),
        "previous_error_code": publication["error_code"],
        "previous_error_sha256": document_digest({"message": message})}


def _validate_preparation_checkpoint(
    candidate: Any, manifest: Mapping[str, Any], installation: Any,
) -> None:
    from ..domain.storage_routes import document_digest

    preparation = manifest.get("route_preparation")
    expected = document_digest({
        "publication_id": str(candidate.id), "desired_spec_sha256": candidate.desired_spec_sha256,
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
        raise ValueError("Route preparation checkpoint changed")
    credentials = manifest.get("route_credentials")
    if "route_credentials" in manifest and (
        not isinstance(credentials, dict) or set(credentials) != {"version", "bindings"}
        or type(credentials.get("version")) is not int or credentials["version"] != 1
        or not isinstance(credentials.get("bindings"), dict) or len(credentials["bindings"]) > 10000
    ):
        raise ValueError("Route credential checkpoint changed")


def _route_reconcile_credential_read_denied(publication: Mapping[str, Any]) -> bool:
    if (publication.get("publication_kind") != "route_reconcile"
            or publication.get("error_code") != "PolicyCompilationError"):
        return False
    message = publication.get("error_message") or ""
    if not message.startswith("APIM management failed: "):
        return False
    try:
        diagnostic = json.loads(message.removeprefix("APIM management failed: "))
    except (ValueError, TypeError):
        return False
    return (isinstance(diagnostic, dict)
        and diagnostic.get("code") == "AuthorizationFailed"
        and diagnostic.get("method") == "POST" and diagnostic.get("status") == 403
        and re.fullmatch(r"/namedValues/[A-Za-z0-9._-]{1,256}/listValue",
                         str(diagnostic.get("path", ""))) is not None)


def _route_reconcile_preparation_failed(publication: Mapping[str, Any]) -> bool:
    if (
        publication.get("publication_kind") not in {"route_reconcile", "model_remove"}
        or publication.get("status", "failed") != "failed"
        or publication.get("apim_revision") is not None
        or publication.get("policy_sha256") is not None
        or _route_reconcile_credential_read_denied(publication)
    ):
        return False
    manifest = publication.get("resource_manifest")
    if not isinstance(manifest, Mapping):
        return False
    keys = set(manifest)
    if publication.get("publication_kind") == "model_remove" and (
        publication.get("base_release_id") is None or not keys
    ):
        return False
    if not keys <= {
        "route_credentials",
        "route_preparation",
        "route_dependencies",
        "route_configuration",
        "route_last_progress_attempt",
    }:
        return False
    error_code = str(publication.get("error_code") or "")
    message = str(publication.get("error_message") or "")
    if error_code == "PolicyCompilationError" and message.startswith(
        "APIM management failed: "
    ):
        return False
    if error_code == "ValueError" and keys == {
        "route_credentials",
        "route_preparation",
        "route_dependencies",
        "route_last_progress_attempt",
    }:
        return message == "An existing model is missing its approved budget contract"
    if error_code == "ValidationError" and keys == {
        "route_credentials",
        "route_dependencies",
        "route_last_progress_attempt",
    }:
        return (
            message.startswith("1 validation error for RoutePublicationTask")
            and "Value error, ROUTE_PUBLICATION_TASK_INVALID [type=value_error," in message
        )
    return bool(error_code and message)


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
        _validate_preparation_checkpoint(candidate, manifest, installation)
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


@dataclass(frozen=True)
class UsageFilters:
    organization_id: str | None = None
    department_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    model_id: str | None = None
    user_id: str | None = None
    # The access points a request arrived through, e.g. 'Microsoft Foundry via APIM' or
    # 'GitHub Copilot CLI'. Matched on the display name because token_usage.runtime is the
    # name itself; the registry UUID is never written into telemetry. Several names are
    # accepted because one gateway fronts several runtimes, and "spend through APIM" has to
    # be answerable without inventing a channel column the ingestion path does not produce.
    runtime: tuple[str, ...] | None = None
    status_code: int | None = None
    apim_target_id: UUID | None = None
    gateway_deployment_id: UUID | None = None


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
        )
    ):
        return None
    return "global", ("all",)


def cache_distribution_supported(dimension: str, filters: UsageFilters) -> bool:
    del dimension, filters
    return False


class BudgetConstraintViolation(ValueError):
    pass


def zero_cost_budget_denial(record: Any) -> bool:
    return bool(
        record.ingest_source == "eventhub"
        and record.ingest_error == "budget_pre_dispatch_denial"
        and record.budget_admission not in (None, "ok")
        and 400 <= record.status_code <= 599
        and record.input_tokens == record.cached_tokens == 0
        and record.cache_write_tokens == record.output_tokens == 0
    )


def budget_usage_cost_sql(usage: str, scope_type: str, scope_id: str) -> str:
    # The same pre-dispatch budget denials that budget_switch_usage excludes cost nothing.
    return (
        f"CASE WHEN {usage}.ingest_source = 'eventhub' "
        f"AND {usage}.ingest_error IS NOT DISTINCT FROM 'budget_pre_dispatch_denial' "
        f"AND {usage}.budget_admission IS NOT NULL AND {usage}.budget_admission <> 'ok' "
        f"AND {usage}.status_code BETWEEN 400 AND 599 "
        f"AND {usage}.input_tokens = 0 AND {usage}.cached_tokens = 0 "
        f"AND {usage}.cache_write_tokens = 0 AND {usage}.output_tokens = 0 "
        "AND NOT EXISTS (SELECT 1 FROM budget_execution_attempt dispatched "
        f"WHERE dispatched.scope_type = {scope_type} AND dispatched.scope_id = {scope_id} "
        "AND dispatched.period_start = "
        f"date_trunc('month', {usage}.ts AT TIME ZONE 'UTC')::date "
        f"AND dispatched.correlation_id = {usage}.correlation_id "
        "AND dispatched.dispatch_id IS NOT NULL) THEN 0::NUMERIC(30,14) "
        f"ELSE budget_usage_usd_cost({usage}.usd_cost, {usage}.estimated, "
        f"{usage}.input_tokens, {usage}.cached_tokens, {usage}.cache_write_tokens, "
        f"{usage}.output_tokens, {usage}.input_price_per_million, "
        f"{usage}.cached_price_per_million, {usage}.cache_write_price_per_million, "
        f"{usage}.output_price_per_million) END"
    )


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


def model_router_view(router: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(router)
    draft = deepcopy(row.pop("draft_config"))
    published = row.pop("published_config", None)
    row.pop("owner_id", None)
    row.pop("deleted_at", None)
    row["definition"] = draft
    row["has_unpublished_changes"] = published is None or published != draft
    return row


def published_model_router_view(router: Mapping[str, Any]) -> dict[str, Any]:
    row = model_router_view(router)
    row["definition"] = deepcopy(router["published_config"])
    row["has_unpublished_changes"] = router["published_config"] != router["draft_config"]
    return row


def model_router_v3_view(router: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(router)
    draft = deepcopy(row.pop("draft_spec"))
    published = row.pop("published_spec", None)
    plan = row.pop("published_plan", None)
    row.pop("alias", None)
    row.pop("owner_id", None)
    row.pop("deleted_at", None)
    row["spec"] = draft
    row["compiled_plan"] = deepcopy(plan)
    row["has_unpublished_changes"] = published is None or published != draft
    return row


def router_v3_spec_sha256(spec: Mapping[str, Any]) -> str:
    canonical = RouterSpecV3.model_validate(spec).model_dump(mode="json")
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


ROUTER_ATTEMPT_PRICE_FIELDS = (
    "input_price_per_million",
    "cached_price_per_million",
    "cache_write_price_per_million",
    "output_price_per_million",
)

ROUTER_ATTEMPT_USAGE_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "output_tokens",
    "total_tokens",
    "estimated_cost",
    *ROUTER_ATTEMPT_PRICE_FIELDS,
)


def enrich_router_attempts(
    attempts: Sequence[Mapping[str, Any]],
    usage_by_request: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    enriched = [dict(item) for item in attempts]
    for index, attempt in enumerate(enriched):
        if attempt.get("sequence") is None:
            attempt["sequence"] = int(attempt.get("attempt_number") or (index + 1))
        if attempt.get("attempt_role") in {None, "candidate_invocation"}:
            attempt["attempt_role"] = "candidate"
        if attempt.get("outcome") == "skipped":
            attempt["outcome"] = "cost_limit_exceeded"
        if attempt.get("decision_basis") == "policy_filtered":
            attempt["decision_basis"] = "cost_limit_exceeded"
        probs = attempt.get("decision_probabilities")
        if isinstance(probs, list):
            attempt["decision_probabilities"] = {
                str(p["model_id"]): float(p["probability"])
                for p in probs
                if isinstance(p, Mapping)
                and p.get("model_id") is not None
                and p.get("probability") is not None
            }
        if attempt.get("strategy_type") is None:
            attempt["strategy_type"] = "priority"
        if attempt.get("priority") is None and attempt["attempt_role"] == "candidate":
            attempt["priority"] = attempt.get("sequence")
        if (
            attempt.get("decision_basis") is None
            and attempt["attempt_role"] == "candidate"
        ):
            outcome = str(attempt.get("outcome") or "")
            attempt["decision_basis"] = {
                "unavailable": "candidate_unavailable",
                "cost_limit_exceeded": "cost_limit_exceeded",
                "failed": "provider_error",
                "latency_exceeded": "latency_limit_exceeded",
                "succeeded": (
                    "fallback_success" if index > 0 else "highest_priority_success"
                ),
            }.get(outcome)
        usage = usage_by_request.get(str(attempt.get("request_id") or ""))
        if usage is None:
            continue
        has_price_keys = (
            "input_price_per_million" in usage and "output_price_per_million" in usage
        )
        if (
            has_price_keys
            and usage.get("input_price_per_million") is None
            and usage.get("output_price_per_million") is None
            and (usage.get("estimated_cost") or 0) == 0
            and (attempt.get("estimated_cost") or 0) == 0
            and all((attempt.get(f) or 0) == 0 for f in ROUTER_ATTEMPT_PRICE_FIELDS)
        ):
            for price_field in ROUTER_ATTEMPT_PRICE_FIELDS:
                if price_field in attempt:
                    attempt[price_field] = None
        has_context_tier = usage.get("price_context_tier") is not None
        for field in ROUTER_ATTEMPT_USAGE_FIELDS:
            usage_val = usage.get(field)
            if usage_val is None:
                continue
            current_val = attempt.get(field)
            if (
                current_val is None
                or (current_val == 0 and usage_val > 0)
                or (field in ROUTER_ATTEMPT_PRICE_FIELDS and has_context_tier)
            ):
                attempt[field] = (
                    float(usage_val)
                    if field.endswith("_price_per_million") or field == "estimated_cost"
                    else usage_val
                )
        if (
            attempt.get("price_context_tier") is None
            and usage.get("price_context_tier") is not None
        ):
            attempt["price_context_tier"] = str(usage["price_context_tier"])
        if (
            attempt.get("price_context_snapshot") is None
            and usage.get("price_context_snapshot") is not None
        ):
            attempt["price_context_snapshot"] = deepcopy(
                usage["price_context_snapshot"]
            )
    return enriched


def apply_router_totals(row: dict[str, Any], attempts: Sequence[Mapping[str, Any]]) -> None:
    measured = [attempt for attempt in attempts if attempt.get("request_id")]
    if not measured:
        return
    row["prompt_tokens"] = sum(int(item.get("input_tokens") or 0) for item in measured)
    if "cached_tokens" in row:
        row["cached_tokens"] = sum(
            int(item.get("cached_tokens") or 0) for item in measured
        )
    if "cache_write_tokens" in row:
        row["cache_write_tokens"] = sum(
            int(item.get("cache_write_tokens") or 0) for item in measured
        )
    row["completion_tokens"] = sum(
        int(item.get("output_tokens") or 0) for item in measured
    )
    row["total_tokens"] = sum(int(item.get("total_tokens") or 0) for item in measured)
    row["latency_ms"] = sum(int(item.get("latency_ms") or 0) for item in measured)
    row["estimated_cost"] = round(
        sum(float(item.get("estimated_cost") or 0) for item in measured), 8
    )
    if any(item.get("total_tokens") is not None for item in measured):
        row["usage_available"] = True
        if row.get("ingest_error") == "platform_usage_unavailable":
            row["ingest_error"] = None
            row["estimated"] = False
        if any(item.get("estimated_cost") is not None for item in measured):
            row["cost_available"] = True
