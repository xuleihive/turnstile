from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from time import time
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid5
from xml.etree import ElementTree as ET

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.application_access import (
    application_id_for,
    application_subscription_id_for,
)
from turnstile_core.domain.control_plane import GatewayPublication, GatewayReleaseSpec
from turnstile_core.domain.gateway_targets import (
    ApimTarget,
    ApimTargetRegistration,
    GatewayDeployment,
    GatewayDeploymentInventory,
    gateway_deployment_id_for,
)
from turnstile_core.domain.storage_routes import (
    ConfigurationApproval,
    Digest,
    ProgramIdentity,
    RouteSnapshot,
    canonical_document,
    catalog_digest,
    configuration_manifests,
    document_digest,
    text_digest,
)
from turnstile_core.integrations.apim_budget_execution import bind_budget_delegation
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.integrations.apim_storage_routes import (
    FixedRouteBinding,
    compact_fixed_policy,
    fixed_operation_contracts,
    fixed_policy_digest,
    installed_route_binding,
    refresh_fixed_budget_execution_policy,
    upgrade_fixed_pool_program,
)
from turnstile_core.integrations.apim_storage_routes import (
    prepare_budget_delegation_upgrade as prepare_budget_delegation_upgrade,
)
from turnstile_core.integrations.apim_target_client_factory import (
    apim_target_client_config_resolver,
)
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.ledger import LedgerSyncService, TableStorageLedger
from turnstile_core.integrations.reconciliation import ManagedIdentityTokenProvider
from turnstile_core.integrations.storage_route_projection import (
    RouteActivationIntent,
    TableRouteProjection,
)
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_support import _registered_program_installation
from turnstile_core.services.budget_settlement import BudgetScopeService
from turnstile_core.services.storage_route_publisher import (
    FixedProgramInstallation,
    FixedProgramVerifier,
    register_verified_program_upgrade,
)


class FixedInstallationDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    database_name: str = Field(min_length=1)
    expected_server: str
    api_name: str
    epoch: UUID
    registration: ApimTargetRegistration
    binding: FixedRouteBinding
    parent_policy_sha256: Digest
    operation_policies_sha256: dict[str, Digest]

    @property
    def program(self) -> ProgramIdentity:
        return ProgramIdentity(
            scope=self.binding.scope,
            api_revision=self.binding.api_revision,
            instance_id="managed",
            program_sha256=self.binding.program_sha256,
            trust_binding_sha256=self.binding.trust_binding_sha256,
        )

    @model_validator(mode="after")
    def require_target_identity(self) -> FixedInstallationDescriptor:
        registration = self.registration
        operations = set(fixed_operation_contracts(
            self.binding.models_path, self.binding.messages_path, self.binding.retired_operations,
            decision_enabled=self.binding.decision_enabled,
        ))
        if (
            str(registration.managed_gateway_url).rstrip("/")
            != "https://" + registration.service_name + ".azure-api.net/" + registration.api_path
            or registration.self_hosted_gateway_id is not None
            or self.binding.scope.target_id != str(registration.gateway_profile_id)
            or self.binding.scope.api_id != registration.api_id
            or len(self.binding.trusted_vault_origins) != 1
            or self.binding.publisher_subscription_id != registration.probe_subscription_id
            or set(self.operation_policies_sha256) != operations
        ):
            raise ValueError("FIXED_INSTALLATION_IDENTITY_INVALID")
        return self


def installation_record(
    descriptor: FixedInstallationDescriptor,
    target_sha256: str,
) -> FixedProgramInstallation:
    binding = descriptor.binding
    return FixedProgramInstallation(
        program=descriptor.program,
        target_identity_sha256=target_sha256,
        storage_endpoint=binding.table_endpoint,
        storage_table=binding.table_name,
        gateway_endpoint=str(descriptor.registration.managed_gateway_url).rstrip("/"),
        publisher_subscription_id=binding.publisher_subscription_id,
        parent_policy_sha256=descriptor.parent_policy_sha256,
        operation_contracts=fixed_operation_contracts(
            binding.models_path, binding.messages_path, binding.retired_operations,
            decision_enabled=binding.decision_enabled,
        ),
        retired_operations=binding.retired_operations,
        operation_policies_sha256=descriptor.operation_policies_sha256,
        supported_profiles=binding.supported_profiles,
        trusted_vault_origins=binding.trusted_vault_origins,
        credential_vault_origin=binding.trusted_vault_origins[0],
        image_limits=binding.image_limits,
        pool_enabled=binding.pool_enabled,
        router_enabled=binding.router_enabled,
        oauth_enabled=binding.oauth_enabled,
        decision_enabled=binding.decision_enabled,
        model_additions_enabled=binding.model_additions_enabled,
        native_gateway_scope=binding.native_gateway_scope,
        legacy_program_sha256=binding.legacy_program_sha256,
        predecessor_program_sha256=binding.predecessor_program_sha256,
        supported_adapters=binding.supported_adapters,
    )


class InitialActivationJournal:
    def __init__(self, connection: psycopg.Connection[Any], target_id: UUID) -> None:
        self.connection, self.target_id = connection, target_id
        row = connection.execute(
            "SELECT config->'fixed_route_activation' FROM gateway_profile WHERE id = %s",
            (target_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("FIXED_INSTALLATION_TARGET_MISSING")
        self.record: dict[str, Any] | None = row[0]

    def _save(self, value: dict[str, Any]) -> None:
        row = self.connection.execute(
            """UPDATE gateway_profile SET config = jsonb_set(config, '{fixed_route_activation}', %s)
               WHERE id = %s AND config->'fixed_route_activation' IS NOT DISTINCT FROM %s::jsonb
               RETURNING id""",
            (Jsonb(value), self.target_id, Jsonb(self.record) if self.record is not None else None),
        ).fetchone()
        if row is None:
            raise RuntimeError("FIXED_INSTALLATION_INTENT_CHANGED")
        self.record = value

    def begin_once(self, intent: RouteActivationIntent) -> None:
        if self.record is not None:
            raise RuntimeError("FIXED_INSTALLATION_ACTIVATION_RECOVERY_ONLY")
        self._save({"intent": intent.to_record(), "outcome": "started"})

    def record_outcome(self, operation_id: str, outcome: str) -> None:
        if self.record is None:
            raise RuntimeError("FIXED_INSTALLATION_INTENT_MISSING")
        intent = RouteActivationIntent.from_record(self.record["intent"])
        if intent.receipt.operation_id != operation_id:
            raise RuntimeError("FIXED_INSTALLATION_OPERATION_CHANGED")
        self._save({**self.record, "outcome": outcome})


def require_runtime(descriptor: FixedInstallationDescriptor, settings: Settings) -> str:
    address = urlsplit(settings.database_url or "")
    if (
        os.environ.get("WEBSITE_SITE_NAME") != descriptor.api_name
        or address.hostname != descriptor.expected_server
        or address.path != "/" + descriptor.database_name
        or settings.apim_gateway_profile_id != descriptor.registration.gateway_profile_id
        or settings.apim_api_id != descriptor.registration.api_id
        or settings.apim_service_name != descriptor.registration.service_name
        or settings.apim_resource_group != descriptor.registration.resource_group
        or settings.azure_subscription_id != str(descriptor.registration.subscription_id)
        or settings.ledger_table_endpoint != descriptor.binding.ledger_table_endpoint
        or settings.ledger_table_name != descriptor.binding.ledger_table_name
        or settings.apim_probe_subscription_key is None
        or not settings.database_url
    ):
        raise RuntimeError("FIXED_INSTALLATION_RUNTIME_TARGET_MISMATCH")
    return settings.database_url


def system_applications(registration: ApimTargetRegistration) -> list[dict[str, Any]]:
    # Same identity as inventory sync and diagnostics, so a later sync updates these rows.
    target_id = registration.gateway_profile_id
    return [
        {
            "application_id": application_id_for(target_id, subscription_id),
            "subscription_id": application_subscription_id_for(target_id, subscription_id),
            "apim_subscription_id": subscription_id,
            "slug": name,
            "display_name": registration.name + " " + name,
            "application_type": "system" if name == "publisher" else "service",
            "system_managed": name == "publisher",
            "state": "active",
            "scope_type": "api",
            "scope_id": registration.api_id,
            "scope_exists": True,
        }
        for name, subscription_id in (
            ("dashboard", registration.dashboard_subscription_id),
            ("publisher", registration.probe_subscription_id),
        )
    ]


def bootstrap(descriptor: FixedInstallationDescriptor, settings: Settings) -> dict[str, Any]:
    database_url = require_runtime(descriptor, settings)
    registration = descriptor.registration
    target_id = registration.gateway_profile_id
    request_sha = document_digest(descriptor.model_dump(mode="json"))
    publication_id = uuid5(target_id, "fixed-program-installation")
    with psycopg.connect(database_url, autocommit=True) as owner:
        if owner.execute("SELECT current_database()").fetchone() != (descriptor.database_name,):
            raise RuntimeError("FIXED_INSTALLATION_CONNECTED_DATABASE_MISMATCH")
        lock = "fixed-program-installation:" + str(target_id)
        if owner.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (lock,)
        ).fetchone() != (True,):
            raise RuntimeError("FIXED_INSTALLATION_WRITER_BUSY")
        repository = PostgreSqlOpsDbProxy(database_url, apim_api_id=registration.api_id)
        try:
            existing = repository.get_gateway_publication(publication_id)
            if existing is not None:
                recorded = GatewayPublication.model_validate(existing)
                if recorded.resource_manifest.get("installation_request_sha256") != request_sha:
                    raise RuntimeError("FIXED_INSTALLATION_RECORD_CHANGED")
                return {"status": "ALREADY_INSTALLED", "publication_id": str(recorded.id)}
            gateway = next(
                (
                    row
                    for row in repository.registry()["gateways"]
                    if str(row["id"]) == str(target_id)
                ),
                None,
            )
            if gateway is None:
                raise RuntimeError("FIXED_INSTALLATION_TARGET_MISSING")
            if "fixed_installation" not in gateway.get("config", {}):
                claimed = owner.execute(
                    """UPDATE gateway_profile
                       SET config = jsonb_set(config, '{fixed_installation}', %s)
                       WHERE id = %s AND NOT config ? 'fixed_installation' RETURNING id""",
                    (Jsonb(request_sha), target_id),
                ).fetchone()
                if claimed is None:
                    raise RuntimeError("FIXED_INSTALLATION_GATEWAY_ALREADY_EXISTS")
            elif gateway.get("config", {}).get("fixed_installation") != request_sha:
                raise RuntimeError("FIXED_INSTALLATION_GATEWAY_ALREADY_EXISTS")
            with (
                httpx.Client(timeout=15, follow_redirects=False) as http,
                TableStorageLedger(
                    descriptor.binding.ledger_table_endpoint,
                    descriptor.binding.ledger_table_name,
                    timeout=5,
                ) as ledger,
            ):
                fence = {
                    "protocol_version": 2,
                    "mode": "active",
                    "api_id": registration.api_id,
                    "apim_service_id": registration.service_name,
                    "gateway_profile_id": str(target_id),
                    "epoch": str(descriptor.epoch),
                    "gateways": {"managed": [descriptor.binding.api_revision]},
                    "model_contracts": {},
                }
                partition = (
                    "budget-protocol|"
                    + text_digest(registration.service_name.lower())
                    + "|"
                    + registration.api_id
                )
                current = ledger.read_versioned_entity(partition, "F")
                if current is None:
                    claimed = owner.execute(
                        """UPDATE gateway_profile
                                    SET config = jsonb_set(config, '{fixed_fence_intent}', %s)
                           WHERE id = %s AND NOT config ? 'fixed_fence_intent' RETURNING id""",
                        (Jsonb(fence), target_id),
                    ).fetchone()
                    if claimed is None:
                        raise RuntimeError("FIXED_INSTALLATION_FENCE_RECOVERY_ONLY")
                    ledger.replace_if_match(
                        partition, "F", {"Document": canonical_document(fence)}, None
                    )
                    current = ledger.read_versioned_entity(partition, "F")
                if current is None or current[0].get("Document") != canonical_document(fence):
                    raise RuntimeError("FIXED_INSTALLATION_FENCE_CHANGED")
                management = AzureApimPublisherClient(settings, client=http)
                provisional = installation_record(descriptor, request_sha)
                assert settings.apim_probe_subscription_key is not None
                verifier = FixedProgramVerifier(
                    provisional, management, settings.apim_probe_subscription_key, http
                )
                proof = verifier.verify()
                projection = TableRouteProjection(
                    descriptor.binding.table_endpoint,
                    descriptor.binding.table_name,
                    descriptor.binding.scope,
                    ManagedIdentityTokenProvider(),
                    http,
                )
                prior_proof = projection.read_program_proof(descriptor.program)
                projection.publish_program_proof(
                    proof, descriptor.program, prior_proof[1] if prior_proof else None, int(time())
                )
                journal = InitialActivationJournal(owner, target_id)
                if journal.record is not None:
                    intent = RouteActivationIntent.from_record(journal.record["intent"])
                    if projection.recover_activation(intent) != "confirmed":
                        raise RuntimeError("FIXED_INSTALLATION_ACTIVATION_UNCONFIRMED")
                    head = intent.head
                    snapshot = RouteSnapshot(approval=head.approval, routes=(), members=())
                else:
                    now = int(time())
                    version = str(uuid5(target_id, "initial-empty-configuration"))
                    _, manifest, dependencies = configuration_manifests(
                        descriptor.binding.scope,
                        version,
                        (),
                        {},
                        now,
                    )
                    approval = ConfigurationApproval(
                        approval_id=str(uuid5(target_id, "initial-approval")),
                        scope=descriptor.binding.scope,
                        route_version=version,
                        generation=1,
                        manifest_sha256=manifest,
                        dependency_manifest_sha256=dependencies,
                        catalog_sha256=catalog_digest(()),
                        entry_count=0,
                        required_program_sha256=descriptor.program.program_sha256,
                        required_profiles=(),
                        budget_epoch=str(descriptor.epoch),
                        issued_epoch=now,
                        activate_before_epoch=now + 600,
                    )
                    snapshot = RouteSnapshot.prepare(approval, (), {}, now)
                    projection.stage(snapshot, now)
                    projection.activate(
                        snapshot,
                        descriptor.program,
                        proof,
                        str(descriptor.epoch),
                        str(uuid5(target_id, "initial-activation")),
                        None,
                        lambda: int(time()),
                        journal,
                    )
                    intent = RouteActivationIntent.from_record(
                        journal.record["intent"] if journal.record else {}
                    )
                    head = intent.head
                observed_head = projection.read_active()
                if observed_head is None or observed_head[0] != head:
                    raise RuntimeError("FIXED_INSTALLATION_HEAD_CHANGED")
                verifier.confirm(head)
                inventory = management.discover_apim_target_inventory(target_id)
                if (
                    inventory.api_id != registration.api_id
                    or inventory.api_path != registration.api_path
                    or inventory.principal_id != registration.service_principal_id
                ):
                    raise RuntimeError("FIXED_INSTALLATION_ARM_TARGET_CHANGED")
                moment = datetime.now(UTC)
                repository.upsert_apim_target(
                    ApimTarget.model_validate(
                        {
                            **{
                                name: value
                                for name, value in inventory.model_dump(mode="python").items()
                                if name in ApimTarget.model_fields
                            },
                            "tenant_id": registration.tenant_id,
                            "management_auth_profile_ref": "system_managed_identity",
                            "probe_subscription_id": registration.probe_subscription_id,
                            "ownership_mode": "turnstile_managed",
                            "last_inventory_sync_at": moment,
                            "last_validated_at": moment,
                            "drift_state": "in_sync",
                        }
                    )
                )
                managed_id = gateway_deployment_id_for(target_id, "managed")
                repository.upsert_gateway_deployment(
                    GatewayDeployment.model_validate(
                        {
                            "id": managed_id,
                            "apim_target_id": target_id,
                            "native_gateway_id": "managed",
                            "kind": "managed",
                            "display_name": registration.name + " Managed",
                            "location": inventory.service_location,
                            "endpoint_url": registration.managed_gateway_url,
                            "hosting_mode": "azure_managed",
                            "lifecycle_ownership": "turnstile_iac",
                            "release_gate": "required",
                            "declared_state": "present",
                            "reachability_state": "reachable",
                            "observation_state": "observed",
                            "runtime_version_support": "supported",
                            "rate_limit_synchronization": "supported",
                            "last_arm_sync_at": moment,
                            "last_probe_at": moment,
                            "last_observed_at": moment,
                        }
                    )
                )
                repository.upsert_gateway_deployment_inventory(
                    GatewayDeploymentInventory(
                        deployment_id=managed_id,
                        associated_api_ids=(registration.api_id,),
                        source_api_version=inventory.source_api_version,
                        synced_at=moment,
                    )
                )
                config = apim_target_client_config_resolver(repository, settings).resolve(target_id)
                if config.route_target_identity_sha256 is None:
                    raise RuntimeError("FIXED_INSTALLATION_TARGET_DIGEST_MISSING")
                installed = installation_record(descriptor, config.route_target_identity_sha256)
                repository.sync_gateway_applications(
                    target_id,
                    system_applications(registration),
                    "fixed-program-installation",
                    moment.date().replace(day=1),
                    100000,
                    100000,
                )
                with owner.transaction():
                    rollout = owner.execute("SELECT epoch FROM budget_execution_rollout").fetchall()
                    if rollout and rollout != [(descriptor.epoch,)]:
                        raise RuntimeError("FIXED_INSTALLATION_BUDGET_ROLLOUT_EXISTS")
                    if not rollout:
                        if owner.execute("SELECT count(*) FROM token_usage").fetchone() != (0,):
                            raise RuntimeError("FIXED_INSTALLATION_DATABASE_HAS_USAGE")
                        owner.execute(
                            """INSERT INTO budget_execution_rollout (
                                         epoch,state,gateway_profile_ids,
                               admitted_after,legacy_fence_verified_at,scope_sync_enabled,prepared_period)
                               VALUES (%s,'active',%s,%s,%s,true,NULL)""",
                            (
                                descriptor.epoch,
                                [target_id],
                                moment,
                                moment,
                            ),
                        )
                    else:
                        owner.execute(
                            """UPDATE budget_execution_rollout
                               SET gateway_profile_ids = CASE
                                   WHEN %s = ANY(gateway_profile_ids) THEN gateway_profile_ids
                                   ELSE array_append(gateway_profile_ids, %s)
                               END,
                               prepared_period = NULL
                               WHERE epoch = %s""",
                            (target_id, target_id, descriptor.epoch),
                        )
                LedgerSyncService(repository, ledger).run(moment)
                with AtomicTableLedger(
                    descriptor.binding.ledger_table_endpoint,
                    descriptor.binding.ledger_table_name,
                    timeout=5,
                ) as atomic_store:
                    for _ in range(5):
                        if BudgetScopeService(repository, atomic_store).prepare(now=moment) == 0:
                            break
                spec = GatewayReleaseSpec(
                    gateway_profile_id=target_id, bindings=[], discovery_models=[]
                )
                publication = GatewayPublication.model_validate(
                    {
                        "id": publication_id,
                        "gateway_profile_id": target_id,
                        "publication_kind": "route_reconcile",
                        "generation": 1,
                        "desired_spec": spec,
                        "desired_spec_sha256": document_digest(spec),
                        "status": "active",
                        "base_release_id": None,
                        "apim_revision": descriptor.binding.api_revision,
                        "policy_sha256": descriptor.program.program_sha256,
                        "resource_manifest": {
                            "fixed_route_program": installed.model_dump(mode="json"),
                            "fixed_route_snapshot": snapshot.model_dump(mode="json"),
                            "fixed_route_head": head.model_dump(mode="json"),
                            "installation_request_sha256": request_sha,
                            "installation_activation": journal.record,
                        },
                        "error_code": None,
                        "error_message": None,
                        "attempt_count": 0,
                        "created_by": "fixed-program-installation",
                        "created_at": moment,
                        "started_at": moment,
                        "completed_at": moment,
                        "updated_at": moment,
                    }
                )
                proof = verifier.verify()
                prior_proof = projection.read_program_proof(descriptor.program)
                projection.publish_program_proof(
                    proof, descriptor.program, prior_proof[1] if prior_proof else None, int(time()),
                )
                verifier.confirm(head)
                repository.record_fixed_route_installation(publication)
                return {
                    "status": "FIXED_PROGRAM_INSTALLED",
                    "publication_id": str(publication.id),
                    "program_sha256": descriptor.program.program_sha256,
                }
        finally:
            repository.close()
            owner.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock,))


# Inputs of each target's last no-op refresh in this process; matching ticks skip recompiling.
_CONVERGED_REFRESH: dict[UUID, tuple[str, ...]] = {}


def configure_budget_delegation(repository: Any, settings: Settings, target_id: UUID) -> None:
    trust = settings.budget_delegation_trust.get(target_id)
    if trust is None:
        return
    previous = repository.effective_gateway_publication(target_id)
    gateways = repository.get_gateway_profiles([target_id])
    if previous is None or len(gateways) != 1 or gateways[0]["id"] != target_id:
        raise RuntimeError("Budget delegation fixed installation is unavailable")
    config = gateways[0].get("config") or {}
    installed = _registered_program_installation(
        str(previous["id"]), FixedProgramInstallation.model_validate_json(canonical_document(
            previous["resource_manifest"]["fixed_route_program"],
        )), config,
    )
    with httpx.Client(timeout=15, follow_redirects=False) as http:
        management = AzureApimPublisherClient(settings, client=http)
        path = f"/apis/{installed.program.scope.api_id};rev={installed.program.api_revision}"
        response, parent = management._read_policy(path + "/policies/policy")
        native_digest = fixed_policy_digest(parent)
        recovering = native_digest != installed.parent_policy_sha256
        checkpoint = config.get("budget_delegation_cutover")
        if recovering:
            if (not isinstance(checkpoint, dict)
                    or checkpoint.get("previous_publication_id") != str(previous["id"])
                    or checkpoint.get("previous_installation_sha256") != document_digest(installed)
                    or not isinstance(checkpoint.get("parent_policy"), str)
                    or fixed_policy_digest(checkpoint["parent_policy"])
                    != installed.parent_policy_sha256):
                raise RuntimeError("Budget delegation current approved parent changed")
            parent = checkpoint["parent_policy"]
        candidate = bind_budget_delegation(parent, trust, gateway_profile_id=target_id)
        enable_pool = not installed.pool_enabled
        enable_router = not installed.router_enabled
        refreshed_program = None
        if candidate == parent and not (enable_pool or enable_router):
            from turnstile_core.integrations.apim_model_availability import (
                compose_availability_parent,
            )
            from turnstile_core.integrations.apim_onboarding_policy import (
                onboarding_canonical_parent,
            )
            from turnstile_core.services.apim_onboarding import onboarding_template_path

            binding = installed_route_binding(
                installed, settings.ledger_table_endpoint, settings.ledger_table_name,
                settings.apim_usage_observer_key_named_value,
            )
            template = onboarding_template_path(settings).read_text(encoding="utf-8")
            converged = (
                native_digest, document_digest(installed), binding.model_dump_json(),
                text_digest(template), trust.model_dump_json(),
            )
            if not recovering and _CONVERGED_REFRESH.get(target_id) == converged:
                return
            canonical = compose_availability_parent(onboarding_canonical_parent(
                template, binding.scope.api_id,
            ))
            canonical = bind_budget_delegation(canonical, trust, gateway_profile_id=target_id)
            refreshed_program = upgrade_fixed_pool_program(
                refresh_fixed_budget_execution_policy(parent, binding, canonical), binding,
            )
            candidate = refreshed_program.parent_policy
            if (fixed_policy_digest(candidate) == installed.parent_policy_sha256
                    and {name: fixed_policy_digest(policy)
                         for name, policy in refreshed_program.operation_policies}
                    == installed.operation_policies_sha256):
                if recovering:
                    raise RuntimeError("Budget delegation current approved parent changed")
                _CONVERGED_REFRESH[target_id] = converged
                return
        candidate = compact_fixed_policy(candidate)
        if fixed_policy_digest(candidate) == installed.parent_policy_sha256 and not (
            enable_pool or enable_router
        ):
            if recovering:
                raise RuntimeError("Budget delegation current approved parent changed")
            return
        operations = management.route_program_operations(installed.program.api_revision)
        operation_policies = None
        operation_etags = None
        if enable_pool or enable_router or refreshed_program is not None:
            pool_program = refreshed_program or upgrade_fixed_pool_program(
                candidate, installed_route_binding(
                    installed, settings.ledger_table_endpoint, settings.ledger_table_name,
                    settings.apim_usage_observer_key_named_value,
                ), enable_router=enable_router,
            )
            candidate = pool_program.parent_policy
            operation_policies = dict(pool_program.operation_policies)
            operations = {name: (method, template, operation_policies[name])
                          for name, method, template in pool_program.operation_contracts}
            operation_etags = {name: management._request(
                "HEAD", path + "/operations/" + name + "/policies/policy",
            ).headers.get("ETag") for name in operation_policies}
        upgrade, candidate = prepare_budget_delegation_upgrade(
            installed, str(previous["id"]), candidate, operations, enable_pool=enable_pool,
            enable_router=enable_router,
        )
        pending = {
            "previous_publication_id": str(previous["id"]),
            "previous_installation_sha256": document_digest(installed),
            "parent_policy": parent,
            "candidate_parent_policy_sha256": upgrade.installation.parent_policy_sha256,
        }
        if recovering:
            normalized = ET.tostring(ET.fromstring(candidate), encoding="unicode")
            if (checkpoint != pending or native_digest not in {
                upgrade.installation.parent_policy_sha256, fixed_policy_digest(normalized),
            }):
                raise RuntimeError("Budget delegation current approved parent changed")
        elif checkpoint != pending:
            updated = {**config, "budget_delegation_cutover": pending}
            with repository._connection() as connection, connection.transaction():
                saved = connection.execute(
                    """UPDATE gateway_profile SET config = %s, updated_at = now()
                       WHERE id = %s AND enabled AND implementation = 'apim'
                       AND config = %s RETURNING id""",
                    (Jsonb(updated), target_id, Jsonb(config)),
                ).fetchone()
                if saved is None:
                    raise RuntimeError("Budget delegation Gateway configuration changed")
            config = updated
        register_verified_program_upgrade(
            repository, apim_target_client_config_resolver(repository, settings),
            upgrade, document_digest(config), parent_policy=candidate,
            expected_parent_etag=response.headers.get("ETag"),
            operation_policies=operation_policies, expected_operation_etags=operation_etags,
            original_parent_policy=parent if refreshed_program is not None else None,
        )


def main() -> None:
    try:
        descriptor = FixedInstallationDescriptor.model_validate_json(
            os.environ.get("TURNSTILE_FIXED_BOOTSTRAP_DESCRIPTOR", "{}"),
        )
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("FIXED_INSTALLATION_RUNTIME_TARGET_MISMATCH")
        repository = PostgreSqlOpsDbProxy(settings.database_url, apim_api_id=settings.apim_api_id)
        try:
            scoped = apim_target_client_config_resolver(repository, settings).resolve(
                descriptor.registration.gateway_profile_id,
            )
            result = bootstrap(descriptor, scoped.settings)
            configure_budget_delegation(
                repository, scoped.settings, descriptor.registration.gateway_profile_id,
            )
            print(json.dumps(result, sort_keys=True))
        finally:
            repository.close()
    except Exception as error:
        print(json.dumps({"status": "BLOCKED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
