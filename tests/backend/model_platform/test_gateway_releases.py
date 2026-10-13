from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    FakeApimClient,
    GatewayControlPlaneService,
    GatewayPublicationWorker,
    bedrock_publication,
)
from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionProvisionSpec,
)
from turnstile_core.domain.control_plane import (
    GatewayPublication,
    GatewayPublicationCreate,
    GatewayReleaseProtectionWrite,
    GatewayReleaseRetentionPolicy,
    GatewayReleaseRole,
    GatewayReleaseRollbackRequest,
    ModelCreateTarget,
    RuntimeTarget,
)
from turnstile_core.domain.runtime_models import ProviderTarget
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.control_plane import (
    ControlPlaneConflictError,
    ControlPlaneUnavailableError,
)
from turnstile_core.services.gateway_release_operation_worker import (
    GatewayReleaseOperationWorker,
)


def _activate(worker: GatewayPublicationWorker) -> GatewayPublication:
    for _ in range(8):
        publication = worker.run_once("worker")
        assert publication is not None
        if publication.status == "active":
            return publication
    raise AssertionError("publication did not become active")


def test_release_projection_identifies_current_rollback_diff_and_manifest() -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    client = FakeApimClient()
    worker = GatewayPublicationWorker(repository, client, client.policy)

    service.publish(bedrock_publication("claude-primary"), "owner@example.com")
    first = _activate(worker)
    runtime = next(
        item
        for item in repository.runtimes
        if item.get("gateway_profile_id") == APIM_ID
        and item.get("config", {}).get("control_plane_managed") is True
    )
    service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=runtime["provider_id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(
                model_key="claude-secondary",
                display_name="Claude Secondary",
                upstream_model_id="anthropic.claude-secondary",
            ),
        ),
        "owner@example.com",
    )
    second = _activate(worker)

    releases = service.releases(APIM_ID).items
    by_id = {release.id: release for release in releases}
    assert by_id[second.id].role is GatewayReleaseRole.CURRENT
    assert by_id[first.id].role is GatewayReleaseRole.IMMEDIATE_ROLLBACK
    assert by_id[second.id].change_set.added_models == ["claude-secondary"]
    assert by_id[first.id].recorded_dependencies_complete is True
    assert by_id[first.id].live_integrity_status == "not_checked"
    assert by_id[first.id].rollback_eligible is False
    assert "live_dependency_check_required" in by_id[first.id].rollback_blockers

    detail = service.release(second.id)
    assert detail.base_release_id == first.id
    assert detail.diff_from_base.changes.added_models == ["claude-secondary"]
    assert detail.dependencies.apim_revision == second.apim_revision
    assert detail.dependencies.compiled_policy_sha256 == second.policy_sha256
    assert detail.audit[-1].to_status == "active"


def test_release_list_batch_loads_base_releases_and_integrity() -> None:
    class BatchOnlyRepository(InMemoryRepository):
        def get_gateway_publication(self, publication_id: UUID) -> dict[str, object] | None:
            raise AssertionError(f"single publication lookup used for {publication_id}")

        def latest_gateway_release_integrity_snapshot(
            self, publication_id: UUID
        ) -> dict[str, object] | None:
            raise AssertionError(f"single integrity lookup used for {publication_id}")

    repository = BatchOnlyRepository()
    service = GatewayControlPlaneService(repository)
    publication = service.publish(
        bedrock_publication("batch-release-list"), "owner@example.com"
    )

    def reject_full_registry_lookup() -> dict[str, object]:
        raise AssertionError("full registry lookup used for release list")

    repository.registry = reject_full_registry_lookup  # type: ignore[assignment]

    releases = service.releases(APIM_ID).items

    assert [release.id for release in releases] == [publication.id]


def test_disabled_release_worker_rejects_queueing_and_marks_read_models() -> None:
    repository = InMemoryRepository()
    enabled = GatewayControlPlaneService(repository)
    publication = enabled.publish(
        bedrock_publication("worker-readiness"), "owner@example.com"
    )
    queued = enabled.request_integrity_check(publication.id, "owner@example.com")
    disabled = GatewayControlPlaneService(repository, release_worker_enabled=False)

    releases = disabled.releases(APIM_ID)
    operation = disabled.release_operation(queued.operation.id)

    assert releases.operations_enabled is False
    assert releases.operations_disabled_reason is not None
    assert operation.worker_available is False
    assert operation.worker_unavailable_reason == releases.operations_disabled_reason
    with pytest.raises(ControlPlaneUnavailableError, match="worker is not enabled"):
        disabled.request_integrity_check(publication.id, "owner@example.com")
    assert len(repository.gateway_release_operations) == 1


def test_release_diff_rejects_cross_gateway_comparison() -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    first = service.publish(bedrock_publication("claude-primary"), "owner@example.com")
    other_gateway = dict(repository.gateways[0])
    other_gateway["id"] = UUID("10000000-0000-4000-8000-000000000099")
    other_gateway["name"] = "Other APIM"
    repository.gateways.append(other_gateway)
    second_request = bedrock_publication("claude-secondary")
    second_request.gateway_profile_id = other_gateway["id"]
    second = service.publish(second_request, "owner@example.com")

    try:
        service.release_diff(first.id, second.id)
    except ValueError as error:
        assert str(error) == "Gateway releases belong to different gateways"
    else:
        raise AssertionError("cross-gateway diff should be rejected")


def _two_active_releases() -> tuple[
    InMemoryRepository,
    GatewayControlPlaneService,
    FakeApimClient,
    GatewayPublication,
    GatewayPublication,
]:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    client = FakeApimClient()
    publication_worker = GatewayPublicationWorker(repository, client, client.policy)
    service.publish(bedrock_publication("claude-primary"), "owner@example.com")
    first = _activate(publication_worker)
    runtime = next(
        item
        for item in repository.runtimes
        if item.get("gateway_profile_id") == APIM_ID
        and item.get("config", {}).get("control_plane_managed") is True
    )
    service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=runtime["provider_id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(
                model_key="claude-secondary",
                display_name="Claude Secondary",
                upstream_model_id="anthropic.claude-secondary",
            ),
        ),
        "owner@example.com",
    )
    second = _activate(publication_worker)
    return repository, service, client, first, second


def _record_healthy_integrity(
    service: GatewayControlPlaneService,
    worker: GatewayReleaseOperationWorker,
    release_id: UUID,
) -> None:
    accepted = service.request_integrity_check(release_id, "owner@example.com")
    assert worker.run_once("release-worker") is not None
    completed = worker.run_once("release-worker")
    assert completed is not None
    assert completed.id == accepted.operation.id
    assert completed.status == "succeeded"


def test_guarded_rollback_promotes_only_after_post_probe() -> None:
    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client)
    _record_healthy_integrity(service, worker, first.id)

    preview = service.rollback_preview(first.id)
    assert preview.rollback_eligible is True
    accepted = service.request_rollback(
        first.id,
        GatewayReleaseRollbackRequest(
            confirmation_sha256=preview.confirmation_sha256
        ),
        "owner@example.com",
    )
    statuses = []
    for _ in range(6):
        operation = worker.run_once("release-worker")
        assert operation is not None
        statuses.append(operation.status.value)

    assert statuses == [
        "validating_dependencies",
        "preflight_probing",
        "promoting",
        "verifying_readback",
        "post_promotion_probing",
        "succeeded",
    ]
    assert service.release_operation(accepted.operation.id).status == "succeeded"
    assert client.current == first.apim_revision
    assert repository.effective_gateway_releases[APIM_ID] == first.id
    first_row = repository.get_gateway_publication(first.id)
    second_row = repository.get_gateway_publication(second.id)
    assert first_row is not None and first_row["status"] == "active"
    assert second_row is not None and second_row["status"] == "rolled_back"


def test_failed_post_promotion_probe_restores_prior_release() -> None:
    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client)
    _record_healthy_integrity(service, worker, first.id)
    preview = service.rollback_preview(first.id)
    service.request_rollback(
        first.id,
        GatewayReleaseRollbackRequest(
            confirmation_sha256=preview.confirmation_sha256
        ),
        "owner@example.com",
    )
    assert first.apim_revision is not None
    client.fail_probe_at[first.apim_revision] = (
        client.probe_counts.get(first.apim_revision, 0) + 2
    )

    operation = None
    for _ in range(6):
        operation = worker.run_once("release-worker")
    assert operation is not None
    assert operation.status == "restoring"
    restored = worker.run_once("release-worker", max_attempts=1)
    assert restored is not None
    assert restored.status == "restored"
    assert client.current == second.apim_revision
    assert repository.effective_gateway_releases[APIM_ID] == second.id
    second_row = repository.get_gateway_publication(second.id)
    assert second_row is not None and second_row["status"] == "active"
    assert {event.to_status.value for event in restored.audit} >= {
        "restoring",
        "restored",
    }


def test_stale_release_operation_lease_cannot_transition() -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    queued = service.publish(bedrock_publication("lease-check"), "owner@example.com")
    accepted = service.request_integrity_check(queued.id, "owner@example.com")
    first_claim = repository.claim_gateway_release_operation("worker-a", 180)
    assert first_claim is not None
    first_claim["lease_expires_at"] = datetime(2020, 1, 1, tzinfo=UTC)
    second_claim = repository.claim_gateway_release_operation("worker-b", 180)
    assert second_claim is not None

    stale = repository.transition_gateway_release_operation(
        accepted.operation.id,
        "queued",
        "validating_dependencies",
        {},
        "worker-a",
    )
    current = repository.transition_gateway_release_operation(
        accepted.operation.id,
        "queued",
        "validating_dependencies",
        {},
        "worker-b",
    )

    assert stale is None
    assert current is not None
    assert current["status"] == "validating_dependencies"


def test_gc_operation_is_retention_aware_dry_run_only() -> None:
    repository, _, client, first, _ = _two_active_releases()
    policy = GatewayReleaseRetentionPolicy(
        retained_count=2,
        retained_days=1,
        failed_retained_days=1,
        protected_labels=["milestone"],
    )
    service = GatewayControlPlaneService(repository, retention_policy=policy)
    publication_worker = GatewayPublicationWorker(repository, client, client.policy)
    runtime = next(
        item
        for item in repository.runtimes
        if item.get("gateway_profile_id") == APIM_ID
        and item.get("config", {}).get("control_plane_managed") is True
    )
    service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=runtime["provider_id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(
                model_key="claude-tertiary",
                display_name="Claude Tertiary",
                upstream_model_id="anthropic.claude-tertiary",
            ),
        ),
        "owner@example.com",
    )
    _activate(publication_worker)
    first_row = repository.get_gateway_publication(first.id)
    assert first_row is not None
    first_row["created_at"] = datetime(2020, 1, 1, tzinfo=UTC)

    accepted = service.request_gc_plan(APIM_ID, "owner@example.com")
    worker = GatewayReleaseOperationWorker(repository, client, policy)
    assert worker.run_once("release-worker") is not None
    completed = worker.run_once("release-worker")
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.gc_plan is not None
    assert completed.gc_plan.operation_id == accepted.operation.id
    assert all(
        candidate.resource_type == "api_revision"
        for candidate in completed.gc_plan.candidates
    )
    assert str(first.id) not in client.last_gc_retained_ids
    assert completed.checkpoint == {"dry_run_only": True}


def test_application_sync_operation_adopts_read_only_apim_inventory() -> None:
    class ApplicationDiscoveryClient(FakeApimClient):
        def discover_gateway_applications(
            self, gateway_profile_id: UUID
        ) -> GatewayApplicationDiscovery:
            assert gateway_profile_id == APIM_ID
            return GatewayApplicationDiscovery(
                gateway_profile_id=gateway_profile_id,
                discovered_at=datetime(2026, 8, 26, tzinfo=UTC),
                items=[
                    GatewayApplicationDiscoveryItem(
                        apim_subscription_id="outline-assistant",
                        display_name="Outline Assistant",
                        state="active",
                        scope_type="product",
                        scope_id="finops-applications",
                        scope_exists=True,
                        application_type="service",
                        system_managed=False,
                    )
                ],
            )

    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    accepted = service.request_application_sync(APIM_ID, "owner@example.com")
    worker = GatewayReleaseOperationWorker(
        repository, ApplicationDiscoveryClient()
    )

    queued = worker.run_once("release-worker")
    completed = worker.run_once("release-worker")

    assert queued is not None and queued.status == "validating_dependencies"
    assert completed is not None and completed.status == "succeeded"
    assert completed.id == accepted.operation.id
    assert completed.checkpoint == {
        "application_count": 1,
        "system_application_count": 0,
        "stale_subscription_count": 0,
        "read_only_apim_discovery": True,
    }
    assert repository.gateway_applications[0]["display_name"] == "Outline Assistant"


def test_owner_provisions_subscription_without_generating_or_storing_keys() -> None:
    class ProvisioningClient(FakeApimClient):
        def __init__(self) -> None:
            super().__init__()
            self.provisioned: GatewayApplicationSubscriptionProvisionSpec | None = None

        def ensure_application_subscription(
            self,
            spec: GatewayApplicationSubscriptionProvisionSpec,
            *,
            legacy_pending_name: str | None = None,
        ) -> None:
            assert legacy_pending_name is None
            self.provisioned = spec

        def activate_application_subscription(
            self,
            spec: GatewayApplicationSubscriptionProvisionSpec,
            *,
            legacy_pending_name: str | None = None,
        ) -> None:
            assert legacy_pending_name is None
            assert self.provisioned == spec

    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository)
    client = ProvisioningClient()

    accepted = service.request_application_subscription_provision(
        APIM_ID,
        GatewayApplicationSubscriptionCreate(
            subscription_id="owner-created-agent",
            display_name="Owner Created Agent",
            description="Internal automation",
            application_type="agent",
        ),
        "owner@example.com",
    )

    row = repository.get_gateway_release_operation(accepted.operation.id)
    assert row is not None
    assert "primary_key" not in accepted.model_dump()
    assert "secondary_key" not in accepted.model_dump()
    assert repository.gateway_release_operation_secret(accepted.operation.id) is None
    assert "primary_key" not in json.dumps(row, default=str)
    assert "secondary_key" not in json.dumps(row, default=str)

    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher,
        application_projector=lambda _gateway, _application: None,
    )
    queued = worker.run_once("release-worker")
    provisioned = worker.run_once("release-worker")
    materialized = worker.run_once("release-worker")
    completed = worker.run_once("release-worker")

    assert queued is not None and queued.status == "validating_dependencies"
    assert provisioned is not None and provisioned.status == "promoting"
    assert materialized is not None and materialized.status == "verifying_readback"
    assert completed is not None and completed.status == "succeeded"
    assert client.provisioned is not None
    assert repository.gateway_release_operation_secret(accepted.operation.id) is None
    application = repository.gateway_applications[0]
    subscription = repository.gateway_application_subscriptions[0]
    assert application["display_name"] == "Owner Created Agent"
    assert application["application_type"] == "agent"
    assert application["owner_id"] == "owner@example.com"
    assert subscription["source"] == "managed"
    assert subscription["scope_id"] == "finops-ai-consumers"
    assert completed.checkpoint["key_stored"] is False


def test_subscription_provisioning_requires_explicit_capability() -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(
        repository,
        CredentialCipher(Fernet.generate_key()),
        application_provisioning_enabled=False,
    )

    with pytest.raises(
        ControlPlaneUnavailableError,
        match="Application subscription provisioning is not enabled",
    ):
        service.request_application_subscription_provision(
            APIM_ID,
            GatewayApplicationSubscriptionCreate(
                subscription_id="disabled-application",
                display_name="Disabled Application",
            ),
            "owner@example.com",
        )

    assert repository.gateway_release_operations == []


def test_failed_subscription_provisioning_deletes_encrypted_keys() -> None:
    class FailingProvisioningClient(FakeApimClient):
        def ensure_application_subscription(
            self,
            spec: GatewayApplicationSubscriptionProvisionSpec,
            *,
            legacy_pending_name: str | None = None,
        ) -> None:
            del spec, legacy_pending_name
            raise ValueError("injected APIM rejection")

    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    accepted = service.request_application_subscription_provision(
        APIM_ID,
        GatewayApplicationSubscriptionCreate(
            subscription_id="failed-app",
            display_name="Failed App",
        ),
        "owner@example.com",
    )
    worker = GatewayReleaseOperationWorker(
        repository, FailingProvisioningClient(), cipher=cipher,
        application_projector=lambda _gateway, _application: None,
    )

    assert worker.run_once("release-worker") is not None
    failed = worker.run_once("release-worker")

    assert failed is not None and failed.status == "failed"
    assert repository.gateway_release_operation_secret(accepted.operation.id) is None
    assert repository.gateway_applications == []


def test_rollback_rejects_a_stale_confirmation_hash() -> None:
    repository, service, client, first, _ = _two_active_releases()
    operation_worker = GatewayReleaseOperationWorker(repository, client)
    _record_healthy_integrity(service, operation_worker, first.id)
    preview = service.rollback_preview(first.id)
    publication_worker = GatewayPublicationWorker(repository, client, client.policy)
    runtime = next(
        item
        for item in repository.runtimes
        if item.get("gateway_profile_id") == APIM_ID
        and item.get("config", {}).get("control_plane_managed") is True
    )
    service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=runtime["provider_id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(
                model_key="claude-after-preview",
                display_name="Claude After Preview",
                upstream_model_id="anthropic.claude-after-preview",
            ),
        ),
        "owner@example.com",
    )
    _activate(publication_worker)

    with pytest.raises(ControlPlaneConflictError, match="preview changed"):
        service.request_rollback(
            first.id,
            GatewayReleaseRollbackRequest(
                confirmation_sha256=preview.confirmation_sha256
            ),
            "owner@example.com",
        )


def test_pin_overrides_expiry_and_unpin_restores_expired_role() -> None:
    repository, _, client, first, _ = _two_active_releases()
    policy = GatewayReleaseRetentionPolicy(
        retained_count=2,
        retained_days=1,
        failed_retained_days=1,
        protected_labels=["milestone"],
    )
    service = GatewayControlPlaneService(repository, retention_policy=policy)
    publication_worker = GatewayPublicationWorker(repository, client, client.policy)
    runtime = next(
        item
        for item in repository.runtimes
        if item.get("gateway_profile_id") == APIM_ID
        and item.get("config", {}).get("control_plane_managed") is True
    )
    service.publish(
        GatewayPublicationCreate(
            gateway_profile_id=APIM_ID,
            provider=ProviderTarget(existing_id=runtime["provider_id"]),
            runtime=RuntimeTarget(existing_id=runtime["id"]),
            model=ModelCreateTarget(
                model_key="claude-retention",
                display_name="Claude Retention",
                upstream_model_id="anthropic.claude-retention",
            ),
        ),
        "owner@example.com",
    )
    _activate(publication_worker)
    first_row = repository.get_gateway_publication(first.id)
    assert first_row is not None
    first_row["created_at"] = datetime(2020, 1, 1, tzinfo=UTC)

    service.protect_release(
        first.id,
        GatewayReleaseProtectionWrite(pinned=True),
        "owner@example.com",
    )
    pinned = next(item for item in service.releases(APIM_ID).items if item.id == first.id)
    assert pinned.role is GatewayReleaseRole.PINNED
    assert pinned.retention_reasons == ["pinned"]

    service.protect_release(
        first.id,
        GatewayReleaseProtectionWrite(pinned=False),
        "owner@example.com",
    )
    expired = next(item for item in service.releases(APIM_ID).items if item.id == first.id)
    assert expired.role is GatewayReleaseRole.EXPIRED
    assert expired.retention_eligible is False
    assert [event.pinned for event in service.release(first.id).protection_audit] == [
        True,
        False,
    ]
