from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    FakeApimClient,
    GatewayControlPlaneService,
    GatewayPublicationWorker,
    bedrock_publication,
    transition_claimed_publication,
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
from turnstile_core.integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    ProbeRequestLimitExceeded,
    ResolvedApimPublisherClient,
)
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


def test_rollback_probe_allowance_is_cumulative_across_stages_and_leases() -> None:
    repository = InMemoryRepository()
    operation = repository.create_gateway_release_operation(APIM_ID, "rollback", "owner")
    operation.update({
        "status": "preflight_probing", "lease_owner": "worker-one",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    reserve = repository.reserve_gateway_release_operation_probe
    assert reserve(operation["id"], "worker-one", 2, "GET", "a" * 64)
    first_audit = deepcopy(repository.gateway_release_operation_audit)
    operation.update({"status": "post_promotion_probing", "lease_owner": "worker-two"})
    assert not reserve(operation["id"], "worker-one", 2, "POST", "b" * 64)
    assert reserve(operation["id"], "worker-two", 2, "POST", "b" * 64)
    operation.update({"status": "restoring", "lease_owner": "worker-three"})
    assert not reserve(operation["id"], "worker-three", 2, "GET", "c" * 64)
    assert repository.gateway_release_operation_audit[:len(first_audit)] == first_audit
    events = [item["detail"]["probe_request"] for item in repository.gateway_release_operation_audit
              if "probe_request" in item["detail"]]
    assert [(item["method"], item["request_index"]) for item in events] == [("GET", 1), ("POST", 2)]


@pytest.mark.parametrize("defect", ["expired", "wrong_kind", "wrong_stage"])
def test_rollback_probe_allowance_requires_current_rollback_lease(defect: str) -> None:
    repository = InMemoryRepository()
    operation = repository.create_gateway_release_operation(APIM_ID, "rollback", "owner")
    operation.update({
        "status": "preflight_probing", "lease_owner": "worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    if defect == "expired":
        operation["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif defect == "wrong_kind":
        operation["operation_kind"] = "integrity_check"
    else:
        operation["status"] = "succeeded"
    before = deepcopy(repository.gateway_release_operation_audit)
    assert not repository.reserve_gateway_release_operation_probe(
        operation["id"], "worker", 30, "GET", "a" * 64,
    )
    assert repository.gateway_release_operation_audit == before


def test_rollback_journal_blocks_http_before_exceeding_target_allowance() -> None:
    repository = InMemoryRepository()
    operation = repository.create_gateway_release_operation(APIM_ID, "rollback", "owner")
    operation.update({
        "status": "preflight_probing", "lease_owner": "worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    worker = GatewayReleaseOperationWorker(
        repository, FakeApimClient(), probe_request_limits={APIM_ID: 1},
    )
    journal = worker._probe_journal(operation, "worker")
    journal.record_request("GET", "https://unit.example/api/v1/models")
    with pytest.raises(ProbeRequestLimitExceeded):
        journal.record_request("POST", "https://unit.example/api/chat/completions")
    assert sum("probe_request" in item["detail"]
               for item in repository.gateway_release_operation_audit) == 1


def test_rollback_cannot_remove_installed_model_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, client, first, second = _two_active_releases()
    monkeypatch.setattr(repository, "model_availability_supported", lambda: True)
    before = {model["id"]: model["enabled"] for model in repository.models}
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
    with pytest.raises(PolicyCompilationError, match="cannot remove model availability"):
        worker._validate_rollback_parent(first, client)
    assert client.current_revision() == second.apim_revision
    assert {model["id"]: model["enabled"] for model in repository.models} == before


@pytest.mark.parametrize("removing", [False, True])
def test_single_model_route_changes_do_not_report_unrelated_baseline_models_removed(
    removing: bool,
) -> None:
    from turnstile_core.domain.control_plane import ModelRemovalTarget, PublicationKind

    _, _, _, first, second = _two_active_releases()
    selected = second.desired_spec.bindings[-1]
    if removing:
        specification = second.desired_spec.model_copy(update={
            "bindings": [], "removed_models": [ModelRemovalTarget(
                model_id=UUID(int=999), model_key=selected.model.model_key,
                display_name=selected.model.display_name, api_format=selected.api_format,
            )],
        })
        release = second.model_copy(update={
            "publication_kind": PublicationKind.MODEL_REMOVE, "desired_spec": specification,
            "resource_manifest": {"model_route_removal": {"alias": selected.model.model_key}},
        })
    else:
        release = second.model_copy(update={
            "desired_spec": second.desired_spec.model_copy(update={"bindings": [selected]}),
            "resource_manifest": {"model_route_addition": {"model_id": str(UUID(int=999))}},
        })
    changes = GatewayControlPlaneService._release_changes(release, first)
    assert changes.removed_models == ([selected.model.model_key] if removing else [])
    assert changes.added_models == ([] if removing else [selected.model.model_key])
    assert changes.changed_models == []
    assert changes.removed_backend_pools == []


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
    service.seed_legacy_publication(
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


@pytest.mark.parametrize("malformed_audit", [False, True])
def test_release_exposes_safe_failure_reason_and_original_audit_stage_without_writes(
    malformed_audit: bool,
) -> None:
    repository = InMemoryRepository()
    service = GatewayControlPlaneService(repository)
    publication = service.publish(bedrock_publication(), "owner@example.com")
    transition_claimed_publication(repository, publication.id, "queued", "validating", {}, "worker")
    transition_claimed_publication(repository, publication.id, "validating", "failed", {
        "error_code": "PolicyCompilationError",
        "error_message": "APIM management failed: " + json.dumps({
            "code": "AuthorizationFailed", "status": 403,
            "path": "/namedValues/private-reference",
            "request_id": "10000000-0000-4000-8000-000000000093",
        }),
    }, "worker")
    if malformed_audit:
        repository.gateway_publication_audit[-1]["detail"]["error_message"] = {
            "error": "private-reference",
        }
    before = deepcopy((repository.gateway_publications, repository.gateway_publication_audit))
    summary = service.releases(APIM_ID).items[0]
    detail = service.release(publication.id)
    assert summary.failure is not None and summary.failure.stage is None
    assert detail.failure is not None
    assert detail.failure.code == "publisher_authorization_failed"
    assert detail.failure.stage == "validating"
    assert detail.failure.http_status == 403
    assert str(detail.failure.request_id) == "10000000-0000-4000-8000-000000000093"
    assert "private-reference" not in detail.model_dump_json()
    audit_failure = detail.audit[-1].detail["failure"]
    assert isinstance(audit_failure, dict)
    if malformed_audit:
        assert audit_failure["code"] == "publication_failed"
    else:
        assert audit_failure == detail.failure.model_dump(mode="json")
    assert (repository.gateway_publications, repository.gateway_publication_audit) == before


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


@pytest.mark.parametrize("current_in_page", [True, False])
def test_release_list_does_not_read_each_gateway_separately(
    current_in_page: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, service, _, first, second = _two_active_releases()
    if not current_in_page:
        monkeypatch.setattr(
            repository, "list_gateway_publications",
            lambda gateway_profile_id, limit: [first.model_dump(mode="python")],
        )

    def reject_individual_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("release list used an individual gateway or publication read")

    for method in (
        "effective_gateway_publication", "get_gateway_publication",
        "list_gateway_release_operations",
    ):
        monkeypatch.setattr(repository, method, reject_individual_read)

    releases = {release.id: release for release in service.releases(APIM_ID).items}

    if current_in_page:
        assert releases[second.id].role is GatewayReleaseRole.CURRENT
    else:
        assert second.id not in releases
    assert releases[first.id].role is GatewayReleaseRole.IMMEDIATE_ROLLBACK


def test_release_context_batches_missing_bases_and_keeps_gateway_retention_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _, first, second = _two_active_releases()
    other_gateway_id = UUID(int=99)
    repository.gateways.append({
        **repository.gateways[0], "id": other_gateway_id, "name": "Other APIM",
    })
    listed = []
    for gateway_id, publication_id in ((APIM_ID, UUID(int=901)), (other_gateway_id, UUID(int=902))):
        publication = first.model_copy(update={
            "id": publication_id, "gateway_profile_id": gateway_id,
            "generation": 0, "base_release_id": None,
            "created_at": datetime(2020, 1, 1, tzinfo=UTC),
            "desired_spec": first.desired_spec.model_copy(update={
                "gateway_profile_id": gateway_id,
            }),
        })
        repository.gateway_publications.append(publication.model_dump(mode="python"))
        listed.append(publication)
    repository.create_gateway_release_operation(
        APIM_ID, "integrity_check", "owner@example.com", target_release_id=listed[0].id,
    )
    policy = GatewayReleaseRetentionPolicy(
        retained_count=1, retained_days=1, failed_retained_days=1, protected_labels=[],
    )
    service = GatewayControlPlaneService(repository, retention_policy=policy)

    def reject_individual_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("release context used an individual read")

    monkeypatch.setattr(repository, "get_gateway_publication", reject_individual_read)
    contexts = service._release_contexts(listed)
    retention = service._release_retention(listed, contexts)

    assert contexts[APIM_ID]["current_id"] == second.id
    assert contexts[APIM_ID]["rollback_id"] == first.id
    assert contexts[other_gateway_id]["current_id"] is None
    assert contexts[other_gateway_id]["rollback_id"] is None
    first_reasons = retention[listed[0].id]["retention_reasons"]
    second_reasons = retention[listed[1].id]["retention_reasons"]
    assert isinstance(first_reasons, list) and isinstance(second_reasons, list)
    assert "active_operation" in first_reasons
    assert "active_operation" not in second_reasons


def test_batched_release_operations_keep_each_gateway_limit_and_order() -> None:
    repository = InMemoryRepository()
    gateway_ids = [APIM_ID, UUID(int=99)]
    for gateway_id in gateway_ids:
        repository.create_gateway_release_operation(gateway_id, "integrity_check", "owner")
        repository.gateway_release_operations[-1]["status"] = "succeeded"
        repository.create_gateway_release_operation(gateway_id, "integrity_check", "owner")
    expected = [
        operation for gateway_id in sorted(gateway_ids, key=str)
        for operation in repository.list_gateway_release_operations(gateway_id, 1)
    ]

    assert repository.list_gateway_release_operations_for_gateways(gateway_ids, 1) == expected
    assert repository.list_gateway_release_operations_for_gateways([], 1) == []
    assert len(expected) == 2


def test_postgres_release_batches_bind_targets_and_limit_each_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import import_module
    from unittest.mock import MagicMock

    from turnstile_core.persistence.repository_publications import (
        PostgreSqlPublicationRepositoryMixin,
    )

    database = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = database
    monkeypatch.setattr(PostgreSqlPublicationRepositoryMixin, "_connection", lambda self: context)
    repository = PostgreSqlPublicationRepositoryMixin()
    gateway_ids = [APIM_ID, UUID(int=99)]
    current_rows = [{"gateway_profile_id": gateway_id} for gateway_id in gateway_ids]
    operation_rows = [{"gateway_profile_id": gateway_id, "release_rank": 1}
                      for gateway_id in gateway_ids]
    database.execute.return_value.fetchall.side_effect = [current_rows, operation_rows]

    assert repository.effective_gateway_publications(gateway_ids) == current_rows
    assert repository.list_gateway_release_operations_for_gateways(gateway_ids, 1) == current_rows
    current_query, operation_query = database.execute.call_args_list
    parse_sql = import_module("pglast").parse_sql
    assert parse_sql(current_query.args[0].replace("%s", "NULL"))
    assert parse_sql(operation_query.args[0].replace("%s", "NULL"))
    assert current_query.args[1] == (gateway_ids,)
    assert operation_query.args[1] == (gateway_ids, 1)
    assert "PARTITION BY gateway_profile_id" in operation_query.args[0]
    assert "ORDER BY created_at DESC, id DESC" in operation_query.args[0]
    assert "release_rank <= %s" in operation_query.args[0]
    assert repository.effective_gateway_publications([]) == []
    assert repository.list_gateway_release_operations_for_gateways([], 1) == []
    assert database.execute.call_count == 2


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
    service.seed_legacy_publication(
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


@pytest.mark.parametrize("base_in_page", [True, False])
def test_release_detail_reuses_base_and_audit_reads(
    base_in_page: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, service, _, first, second = _two_active_releases()
    get_publication = repository.get_gateway_publication
    list_audit = repository.list_gateway_publication_audit
    publication_reads: list[UUID] = []
    audit_reads: list[UUID] = []

    def get_once(publication_id: UUID) -> dict[str, object] | None:
        publication_reads.append(publication_id)
        return get_publication(publication_id)

    def audit_once(publication_id: UUID) -> list[dict[str, object]]:
        audit_reads.append(publication_id)
        return list(list_audit(publication_id))

    monkeypatch.setattr(repository, "get_gateway_publication", get_once)
    monkeypatch.setattr(repository, "list_gateway_publication_audit", audit_once)
    if not base_in_page:
        monkeypatch.setattr(repository, "list_gateway_publications",
                            lambda gateway_profile_id, limit: [second.model_dump(mode="python")])
    detail = service.release(second.id)
    assert (publication_reads.count(first.id), audit_reads.count(second.id)) == (
        0 if base_in_page else 1, 1,
    )
    assert detail.base_release_id == first.id
    assert detail.diff_from_base.against_release_id == first.id
    assert detail.diff_from_base.changes.added_models == ["claude-secondary"]
    assert len(detail.audit) == len(list_audit(second.id))
    assert detail.recorded_dependencies_complete


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


@pytest.mark.parametrize("marker", ["model_route_addition", "model_route_removal"])
def test_model_release_is_not_a_gateway_rollback_target(marker: str) -> None:
    repository, service, client, first, _ = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
    _record_healthy_integrity(service, worker, first.id)
    assert service.rollback_preview(first.id).rollback_eligible
    recorded = repository.get_gateway_publication(first.id)
    assert recorded is not None
    recorded["resource_manifest"][marker] = {"version": 1}

    detail = service.release(first.id)
    preview = service.rollback_preview(first.id)
    assert not detail.rollback_eligible
    assert detail.role is not GatewayReleaseRole.IMMEDIATE_ROLLBACK
    assert not preview.rollback_eligible
    assert "model_release_not_gateway_rollback_target" in preview.rollback_blockers
    with pytest.raises(
        ControlPlaneConflictError, match="model_release_not_gateway_rollback_target",
    ):
        service.request_rollback(
            first.id,
            GatewayReleaseRollbackRequest(confirmation_sha256=preview.confirmation_sha256),
            "owner@example.com",
        )
    service.protect_release(
        first.id, GatewayReleaseProtectionWrite(pinned=True), "owner@example.com",
    )
    protected = service.release(first.id)
    assert protected.pinned and protected.retention_eligible
    assert not protected.rollback_eligible


@pytest.mark.parametrize("target_fixed,current_fixed", [
    (False, True), (True, False), (True, True),
])
def test_release_rollback_rejects_incompatible_gateway_programs(
    target_fixed: bool, current_fixed: bool,
) -> None:
    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
    _record_healthy_integrity(service, worker, first.id)
    for identifier, fixed, version in (
        (first.id, target_fixed, 1), (second.id, current_fixed, 2),
    ):
        recorded = repository.get_gateway_publication(identifier)
        assert recorded is not None
        if fixed:
            recorded["resource_manifest"]["fixed_route_program"] = {"version": version}
    detail = service.release(first.id)
    preview = service.rollback_preview(first.id)
    assert "gateway_program_mismatch" in detail.rollback_blockers
    assert "gateway_program_mismatch" in preview.rollback_blockers
    assert not detail.rollback_eligible and not preview.rollback_eligible
    assert detail.role is not GatewayReleaseRole.IMMEDIATE_ROLLBACK
    with pytest.raises(ControlPlaneConflictError, match="gateway_program_mismatch"):
        service.request_rollback(
            first.id,
            GatewayReleaseRollbackRequest(confirmation_sha256=preview.confirmation_sha256),
            "owner@example.com",
        )


@pytest.mark.parametrize("fixed", [False, True])
def test_release_rollback_preserves_matching_gateway_programs(fixed: bool) -> None:
    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
    _record_healthy_integrity(service, worker, first.id)
    target = repository.get_gateway_publication(first.id)
    current = repository.get_gateway_publication(second.id)
    assert target is not None and current is not None
    if fixed:
        installation = {"program": {"api_revision": "fixed-1", "program_sha256": "a" * 64}}
        target["resource_manifest"]["fixed_route_program"] = deepcopy(installation)
        current["resource_manifest"]["fixed_route_program"] = deepcopy(installation)
    publication = GatewayPublication.model_validate(target)
    dependencies = GatewayControlPlaneService._recorded_release_dependencies(first).model_copy(
        update={"live_status": "healthy", "issues": []},
    )
    assert not GatewayControlPlaneService._rollback_blockers(
        publication, dependencies, is_current=False,
        current=GatewayPublication.model_validate(current),
    )
    context = service._release_contexts([GatewayPublication.model_validate(current), publication])
    assert context[APIM_ID]["rollback_id"] == first.id


@pytest.mark.parametrize("configured", [False, True])
def test_fixed_route_integrity_never_uses_legacy_revision_inspection(
    configured: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from turnstile_core.domain.control_plane import GatewayReleaseDependencies

    repository, service, client, _, current = _two_active_releases()
    row = repository.get_gateway_publication(current.id)
    assert row is not None
    row["resource_manifest"]["fixed_route_program"] = {"version": 2}
    legacy = Mock(side_effect=AssertionError("FIXED_ROUTE_REACHED_LEGACY_INSPECTION"))
    monkeypatch.setattr(client, "inspect_revision_dependencies", legacy)
    verifier = Mock(return_value=GatewayReleaseDependencies(
        apim_revision=current.apim_revision, parent_policy_sha256="a" * 64,
        compiled_policy_sha256=current.policy_sha256, recorded_complete=True,
        live_status="healthy",
    ))
    worker = GatewayReleaseOperationWorker(
        repository, client,
        **({"route_integrity_verifier": verifier} if configured else {}),
    )
    resolver = Mock(side_effect=AssertionError("FIXED_INTEGRITY_LEGACY_TARGET_IDENTITY"))
    monkeypatch.setattr(worker._client_resolver, "resolve", resolver)
    accepted = service.request_integrity_check(current.id, "owner@example.com")
    initial = worker.run_once("release-worker")
    assert initial is not None and initial.status == "validating_dependencies"
    completed = worker.run_once("release-worker")
    assert completed is not None and completed.id == accepted.operation.id
    assert completed.status == ("succeeded" if configured else "failed")
    legacy.assert_not_called()
    resolver.assert_not_called()
    if configured:
        verifier.assert_called_once()
        assert verifier.call_args.args[0].id == current.id
        assert completed.checkpoint["integrity_status"] == "healthy"
    else:
        verifier.assert_not_called()
        assert "Fixed route integrity" in str(completed.error_message)


@pytest.mark.parametrize("stalled", [False, True])
@pytest.mark.parametrize("pool", [False, True])
def test_fixed_route_integrity_worker_preserves_bounded_progress(
    stalled: bool, pool: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from turnstile_core.domain.control_plane import GatewayReleaseDependencies
    from turnstile_core.services.route_publication import (
        RouteIntegrityCheckpoint,
        RouteIntegrityDeferred,
    )

    repository, service, client, _, current = _two_active_releases()
    row = repository.get_gateway_publication(current.id)
    assert row is not None
    row["resource_manifest"]["fixed_route_program"] = {"version": 2}
    checkpoints: list[int] = []
    promote = Mock(wraps=client.promote_revision)
    monkeypatch.setattr(client, "promote_revision", promote)

    def verify(target: GatewayPublication, checkpoint: object) -> GatewayReleaseDependencies:
        assert target.id == current.id
        progress = (
            RouteIntegrityCheckpoint.model_validate(checkpoint) if checkpoint is not None
            else RouteIntegrityCheckpoint(context_sha256="a" * 64)
        )
        cursor = progress.connection_resources if pool else progress.entities
        checkpoints.append(cursor)
        if cursor < 4:
            raise RouteIntegrityDeferred(progress.model_copy(update={
                "connection_resources" if pool else "entities": cursor + (
                    0 if stalled and cursor else 1
                ),
                **({"entities": 3, "dependencies_started_epoch": 1100} if pool else {}),
            }))
        return GatewayReleaseDependencies(
            apim_revision=current.apim_revision, parent_policy_sha256="b" * 64,
            compiled_policy_sha256=current.policy_sha256,
            route_configuration_sha256="c" * 64, route_head_sha256="d" * 64,
            recorded_complete=True, live_status="healthy",
        )

    worker = GatewayReleaseOperationWorker(repository, client, route_integrity_verifier=verify)
    accepted = service.request_integrity_check(current.id, "owner@example.com")
    original = repository.get_gateway_release_operation(accepted.operation.id)
    assert original is not None
    original["checkpoint"] = {"preserved": "original"}
    seen = []
    for _ in range(7):
        result = worker.run_once("integrity-worker", max_attempts=2)
        assert result is not None
        seen.append(result.status)
        assert result.checkpoint["preserved"] == "original"
        if result.status in {"succeeded", "failed"}:
            break
        assert repository.gateway_release_integrity_snapshots == []
        saved = repository.get_gateway_release_operation(result.id)
        assert saved is not None and saved["lease_owner"] is None
    assert result is not None
    assert result.status == ("failed" if stalled else "succeeded")
    if stalled:
        assert result.error_code == "max_attempts_exceeded"
        progress = RouteIntegrityCheckpoint.model_validate(result.checkpoint["route_integrity"])
        assert (progress.connection_resources if pool else progress.entities) == 1
        assert repository.gateway_release_integrity_snapshots == []
    else:
        assert checkpoints == [0, 1, 2, 3, 4]
        assert len(repository.gateway_release_integrity_snapshots) == 1
        assert result.attempt_count == 6
    assert all(state == "validating_dependencies" for state in seen[:-1])
    promote.assert_not_called()


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("pool", [False, True])
def test_fixed_route_integrity_is_bound_to_configuration_not_only_policy(
    changed: bool, pool: bool,
) -> None:
    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.domain.storage_routes import ActiveRouteHead, document_digest
    from turnstile_core.integrations.apim_storage_routes import (
        AUXILIARY_OPERATIONS,
        FIXED_OPERATIONS,
    )
    from turnstile_core.services.storage_route_publisher import FixedProgramInstallation

    repository, _, _, _, current = _two_active_releases()
    snapshot, program, _ = snapshot_fixture(pool=pool)
    head = ActiveRouteHead(approval=snapshot.approval, activated_epoch=1100)
    operations = {**FIXED_OPERATIONS, **AUXILIARY_OPERATIONS}
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="f" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/models", publisher_subscription_id="unit",
        parent_policy_sha256="a" * 64, operation_contracts=operations,
        operation_policies_sha256={name: "b" * 64 for name in operations},
        supported_profiles=snapshot.approval.required_profiles,
        pool_enabled=pool,
    )
    current = current.model_copy(update={
        "gateway_profile_id": UUID(program.scope.target_id),
        "desired_spec": current.desired_spec.model_copy(update={
            "gateway_profile_id": UUID(program.scope.target_id),
        }),
        "apim_revision": program.api_revision, "policy_sha256": program.program_sha256,
        "resource_manifest": {
            "fixed_route_program": installation.model_dump(mode="json"),
            "fixed_route_snapshot": snapshot.model_dump(mode="json"),
            "fixed_route_head": head.model_dump(mode="json"),
        },
    })
    recorded = GatewayControlPlaneService._release_dependencies_from_snapshot(current, None)
    assert recorded.recorded_complete is True
    assert recorded.route_configuration_sha256 == snapshot.configuration_sha256
    assert recorded.route_head_sha256 == document_digest(head)
    backend_ids = {route.connection.backend_id for route in snapshot.routes}
    assert recorded.backends == sorted(backend_ids | (
        {"unit-member-0", "unit-member-1"} if pool else set()
    ))
    assert recorded.backend_pools == (sorted(backend_ids) if pool else [])
    assert recorded.named_values == (["unit-pool-key"] if pool else [])
    checked = recorded.model_dump(mode="json")
    if changed:
        checked["route_configuration_sha256"] = "0" * 64
    evidence = {
        "dependencies": checked, "status": "healthy", "issues": [],
        "checked_at": datetime.now(UTC),
    }
    result = GatewayControlPlaneService._release_dependencies_from_snapshot(current, evidence)
    assert result.live_status == ("mismatched" if changed else "healthy")
    assert repository.gateway_release_integrity_snapshots == []


def test_guarded_rollback_promotes_only_after_post_probe() -> None:
    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
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


@pytest.mark.parametrize("phase", [
    "validating_dependencies", "preflight_probing", "promoting", "verifying_readback",
    "post_promotion_probing", "restoring",
])
@pytest.mark.parametrize("fixed_side", ["target", "prior"])
@pytest.mark.parametrize("marker", [
    "fixed_route_program", "model_route_addition", "model_route_removal",
])
def test_fixed_route_rollback_never_uses_legacy_revision_workflow(
    phase: str, fixed_side: str, marker: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    repository, _, client, first, second = _two_active_releases()
    chosen = repository.get_gateway_publication(first.id if fixed_side == "target" else second.id)
    assert chosen is not None
    chosen["resource_manifest"][marker] = {"version": 2}
    operation = repository.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id,
    )
    operation["status"] = phase
    forbidden = Mock(side_effect=AssertionError("FIXED_ROUTE_LEGACY_ROLLBACK"))
    for method in ("inspect_revision_dependencies", "probe_revision", "promote_revision"):
        monkeypatch.setattr(client, method, forbidden)
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
    result = worker.run_once("release-worker")
    assert result is not None and result.status == "failed"
    assert result.error_code == "PolicyCompilationError"
    assert "Fixed route rollback" in str(result.error_message)
    forbidden.assert_not_called()
    assert repository.effective_gateway_releases[APIM_ID] == second.id
    assert client.current == second.apim_revision


@pytest.mark.parametrize("outcome", ["active", "failed", "lost-ack", "foreign-effective"])
def test_fixed_rollback_tracks_one_configuration_publication(
    outcome: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from turnstile_core.integrations.apim_control_plane_contract import RetryablePublicationError

    repository, _, client, first, second = _two_active_releases()
    for identifier in (first.id, second.id):
        release = repository.get_gateway_publication(identifier)
        assert release is not None
        release["resource_manifest"]["fixed_route_program"] = {
            "program": {"api_revision": "fixed-1", "program_sha256": "a" * 64},
        }
    operation = repository.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id, confirmation_sha256="b" * 64,
    )
    dependencies = GatewayControlPlaneService._release_dependencies_from_snapshot(first, None)
    verifier = Mock(return_value=dependencies.model_copy(update={
        "live_status": "healthy", "issues": [], "route_configuration_sha256": "c" * 64,
        "route_head_sha256": "d" * 64,
    }))
    worker = GatewayReleaseOperationWorker(repository, client, route_integrity_verifier=verifier)
    monkeypatch.setattr(worker._service, "rollback_preview", Mock(return_value=Mock(
        current_release_id=second.id, confirmation_sha256="b" * 64,
        rollback_eligible=True, rollback_blockers=[],
    )))
    forbidden = Mock(side_effect=AssertionError("FIXED_ROLLBACK_LEGACY_CLIENT"))
    monkeypatch.setattr(worker._client_resolver, "resolve", forbidden)
    enqueue = repository.enqueue_gateway_configuration_rollback
    calls = 0

    def enqueue_once(operation_id: UUID, actor: str) -> dict[str, Any]:
        nonlocal calls
        child = enqueue(operation_id, actor)
        calls += 1
        if outcome == "lost-ack" and calls == 1:
            raise RetryablePublicationError("UNIT_ENQUEUE_COMMITTED_ACK_LOST")
        return child

    monkeypatch.setattr(repository, "enqueue_gateway_configuration_rollback", enqueue_once)
    result = worker.run_once("release-worker")
    assert result is not None and result.status == "validating_dependencies"
    result = worker.run_once("release-worker")
    assert result is not None
    if outcome == "lost-ack":
        assert "route_rollback_publication_id" in operation["checkpoint"]
        result = worker.run_once("replacement-worker")
    assert result is not None and result.status == "verifying_readback"
    child_id = UUID(str(operation["checkpoint"]["route_rollback_publication_id"]))
    child = repository.get_gateway_publication(child_id)
    assert child is not None and len(repository.gateway_publications) == 3
    for _ in range(4):
        result = worker.run_once("wait-worker", max_attempts=2)
        assert result is not None and result.status == "verifying_readback"
    if outcome == "failed":
        child["status"] = "failed"
        child["error_code"] = "RouteActivationNotApplied"
    else:
        child["status"] = "active"
        repository.effective_gateway_releases[APIM_ID] = (
            second.id if outcome == "foreign-effective" else child_id
        )
    result = worker.run_once("finish-worker", max_attempts=2)
    assert result is not None
    assert result.status == (
        "failed" if outcome in {"failed", "foreign-effective"} else "succeeded"
    )
    assert len(repository.gateway_publications) == 3
    original_target = repository.get_gateway_publication(first.id)
    original_prior = repository.get_gateway_publication(second.id)
    assert original_target is not None and original_target["status"] == "superseded"
    assert original_prior is not None and original_prior["status"] == "active"
    forbidden.assert_not_called()
    assert client.current == second.apim_revision


@pytest.mark.parametrize("fault", [None, "lease", "pointer", "child", "association"])
def test_configuration_rollback_completion_requires_its_active_child(fault: str | None) -> None:
    repository, _, _, first, second = _two_active_releases()
    for identifier in (first.id, second.id):
        release = repository.get_gateway_publication(identifier)
        assert release is not None
        release["resource_manifest"]["fixed_route_program"] = {"version": 1}
    operation = repository.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id, confirmation_sha256="b" * 64,
    )
    operation.update({
        "status": "validating_dependencies", "lease_owner": "worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    child = repository.enqueue_gateway_configuration_rollback(operation["id"], "worker")
    operation["status"] = "verifying_readback"
    child["status"] = "active"
    repository.effective_gateway_releases[APIM_ID] = child["id"]
    if fault == "lease":
        operation["lease_expires_at"] = datetime.now(UTC)
    elif fault == "pointer":
        repository.effective_gateway_releases[APIM_ID] = second.id
    elif fault == "child":
        child["status"] = "promoting"
    elif fault == "association":
        child["resource_manifest"]["route_rollback"]["operation_id"] = str(UUID(int=950))
    before = deepcopy((repository.gateway_publications, repository.gateway_release_operation_audit))
    if fault:
        with pytest.raises(ValueError):
            repository.complete_gateway_configuration_rollback(operation["id"], "worker")
        assert operation["status"] == "verifying_readback"
        assert before == (
            repository.gateway_publications, repository.gateway_release_operation_audit,
        )
        return
    completed = repository.complete_gateway_configuration_rollback(operation["id"], "worker")
    assert completed["status"] == "succeeded"
    assert completed["lease_owner"] is None and completed["completed_at"] is not None
    assert completed["checkpoint"]["route_rollback_publication_id"] == str(child["id"])
    assert repository.gateway_publications == before[0]
    assert repository.gateway_release_operation_audit[-1]["to_status"] == "succeeded"


@pytest.mark.parametrize("fault", [
    None, "lease", "base", "mixed", "confirmation", "model_route_addition", "model_route_removal",
])
def test_configuration_rollback_enqueues_once_without_rewriting_history(fault: str | None) -> None:
    repository, _, _, first, second = _two_active_releases()
    target = repository.get_gateway_publication(first.id)
    prior = repository.get_gateway_publication(second.id)
    assert target is not None and prior is not None
    installation = {"program": {"api_revision": "fixed-1", "program_sha256": "a" * 64}}
    target["resource_manifest"]["fixed_route_program"] = deepcopy(installation)
    prior["resource_manifest"]["fixed_route_program"] = deepcopy(installation)
    operation = repository.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id, confirmation_sha256="b" * 64,
    )
    operation.update({
        "status": "validating_dependencies", "lease_owner": "first-worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    if fault == "lease":
        operation["lease_expires_at"] = datetime.now(UTC)
    elif fault == "base":
        repository.effective_gateway_releases[APIM_ID] = first.id
    elif fault == "mixed":
        target["resource_manifest"].pop("fixed_route_program")
    elif fault == "confirmation":
        operation["confirmation_sha256"] = None
    elif fault in {"model_route_addition", "model_route_removal"}:
        target["resource_manifest"][fault] = {"version": 1}
    before = deepcopy((repository.gateway_publications, repository.gateway_publication_audit,
                       repository.gateway_publication_outbox, operation))
    if fault is not None:
        with pytest.raises(ValueError):
            repository.enqueue_gateway_configuration_rollback(operation["id"], "first-worker")
        assert before == (repository.gateway_publications, repository.gateway_publication_audit,
                          repository.gateway_publication_outbox, operation)
        return
    publication = repository.enqueue_gateway_configuration_rollback(operation["id"], "first-worker")
    assert publication["id"] not in {first.id, second.id}
    assert publication["base_release_id"] == second.id
    assert publication["publication_kind"] == "route_reconcile"
    assert publication["generation"] == second.generation + 1
    assert publication["desired_spec"] == {**first.desired_spec.model_dump(mode="json"),
                                           "removed_models": []}
    assert publication["apim_revision"] is None and publication["policy_sha256"] is None
    assert publication["resource_manifest"]["route_rollback"] == {
        "operation_id": str(operation["id"]), "target_release_id": str(first.id),
        "prior_release_id": str(second.id), "confirmation_sha256": "b" * 64,
    }
    assert operation["checkpoint"]["route_rollback_publication_id"] == str(publication["id"])
    operation["lease_owner"] = "second-worker"
    queued = deepcopy((repository.gateway_publications, repository.gateway_publication_audit,
                       repository.gateway_publication_outbox, operation))
    assert repository.enqueue_gateway_configuration_rollback(
        operation["id"], "second-worker",
    )["id"] == publication["id"]
    assert queued == (repository.gateway_publications, repository.gateway_publication_audit,
                      repository.gateway_publication_outbox, operation)
    assert repository.gateway_publications[:2] == before[0]
    assert repository.effective_gateway_releases[APIM_ID] == second.id


@pytest.mark.parametrize("marker", ["model_route_addition", "model_route_removal"])
def test_configuration_publisher_rejects_a_model_rollback_source(marker: str) -> None:
    from unittest.mock import Mock

    from turnstile_core.services.storage_route_publisher import FixedRoutePublisherFactory

    repository, _, _, first, second = _two_active_releases()
    target = repository.get_gateway_publication(first.id)
    prior = repository.get_gateway_publication(second.id)
    assert target is not None and prior is not None
    installation = {"program": {"api_revision": "fixed-1", "program_sha256": "a" * 64}}
    for release in (target, prior):
        release["resource_manifest"]["fixed_route_program"] = deepcopy(installation)
    operation = repository.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id, confirmation_sha256="b" * 64,
    )
    operation.update({
        "status": "validating_dependencies", "lease_owner": "worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    child = repository.enqueue_gateway_configuration_rollback(operation["id"], "worker")
    target["resource_manifest"][marker] = {"version": 1}
    factory = object.__new__(FixedRoutePublisherFactory)
    factory._repository = repository
    before = deepcopy(repository.gateway_publications)
    with pytest.raises(PolicyCompilationError, match="Model-scoped release cannot restore"):
        factory._rollback_source(
            GatewayPublication.model_validate(child), second,
            Mock(model_dump=Mock(return_value=installation)), Mock(),
        )
    assert repository.gateway_publications == before


@pytest.mark.parametrize("fault", [
    None, "lease", "base", "inflight", "link-expired", "audit", "linked", "foreign-linked",
])
def test_postgres_configuration_rollback_links_in_one_transaction(
    fault: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    from importlib import import_module
    from unittest.mock import MagicMock, Mock

    from turnstile_core.persistence.repository_publications import (
        PostgreSqlPublicationRepositoryMixin,
    )

    parse_sql = import_module("pglast").parse_sql
    source, _, _, first, second = _two_active_releases()
    target, prior = first.model_dump(mode="python"), second.model_dump(mode="python")
    installation = {"program": {"api_revision": "fixed-1", "program_sha256": "a" * 64}}
    for release in (target, prior):
        release["resource_manifest"]["fixed_route_program"] = deepcopy(installation)
    operation = source.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id, confirmation_sha256="b" * 64,
    )
    operation.update(status="validating_dependencies", lease_owner="worker")
    detail = {
        "operation_id": str(operation["id"]), "target_release_id": str(first.id),
        "prior_release_id": str(second.id), "confirmation_sha256": "b" * 64,
    }
    spec = {**first.desired_spec.model_dump(mode="json"), "removed_models": []}
    digest = hashlib.sha256(json.dumps(
        {"route_rollback": detail, "desired_spec": spec},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    publication = {
        **prior, "id": UUID(int=901), "status": "queued", "desired_spec": spec,
        "base_release_id": second.id, "desired_spec_sha256": digest,
        "resource_manifest": {"route_rollback": detail},
    }
    linked = fault in {"linked", "foreign-linked"}
    if linked:
        operation["checkpoint"]["route_rollback_publication_id"] = str(publication["id"])
    if fault == "foreign-linked":
        publication["base_release_id"] = first.id
    replies = [
        operation if fault != "lease" else None, None, target, prior,
        *([publication] if linked else [
            {"publication_id": first.id if fault == "base" else second.id},
            {"enabled": True, "implementation": "apim"},
            {"id": UUID(int=902)} if fault == "inflight" else None,
            publication, None, None,
            None if fault == "link-expired" else {"id": operation["id"]},
            RuntimeError("UNIT_AUDIT_FAILURE") if fault == "audit" else None,
        ]),
    ]
    connection = MagicMock()
    transaction = connection.transaction.return_value

    def execute(query: str, parameters: object = None) -> Mock:
        del parameters
        parse_sql(query.replace("%s", "NULL"))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Mock(fetchone=Mock(return_value=reply))

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlPublicationRepositoryMixin()
    monkeypatch.setattr(repository, "_connection", Mock(return_value=context))
    if fault not in {None, "linked"}:
        with pytest.raises(RuntimeError if fault == "audit" else ValueError):
            repository.enqueue_gateway_configuration_rollback(operation["id"], "worker")
        assert transaction.__exit__.call_args.args[0] is not None
    else:
        result = repository.enqueue_gateway_configuration_rollback(operation["id"], "worker")
        assert result["id"] == publication["id"]
        assert transaction.__exit__.call_args.args[0] is None
    transaction.__enter__.assert_called_once()
    calls = connection.execute.call_args_list
    assert "FOR UPDATE" in calls[0].args[0] and "lease_owner = %s" in calls[0].args[0]
    assert "clock_timestamp()" in calls[0].args[0]
    inserts = [call for call in calls if call.args[0].startswith("INSERT")]
    assert len(inserts) == (4 if fault in {None, "audit"} else 3 if fault == "link-expired" else 0)
    assert not any(
        call.args[0].startswith(("DELETE", "UPDATE gateway_publication")) for call in calls
    )
    if fault is None:
        assert "pg_advisory_xact_lock" in calls[1].args[0]
        assert "FOR UPDATE" in calls[4].args[0]
        assert calls[7].args[1][1].obj == spec
        assert calls[7].args[1][5].obj == {"route_rollback": detail}
        assert calls[10].args[1][0].obj["route_rollback_publication_id"] == str(publication["id"])


@pytest.mark.parametrize("fault", [None, "existing", "id-conflict", "audit"])
def test_postgres_program_migration_queues_manifest_outbox_and_audit_atomically(
    fault: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import import_module
    from unittest.mock import MagicMock, Mock

    from tests.backend.model_platform.test_storage_route_projection import snapshot_fixture
    from turnstile_core.domain.storage_routes import document_digest
    from turnstile_core.persistence.repository_publications import (
        PostgreSqlPublicationRepositoryMixin,
    )
    from turnstile_core.services.storage_route_publisher import (
        FixedProgramInstallation,
        FixedProgramMigration,
    )

    _, _, _, _, previous = _two_active_releases()
    _, program, _ = snapshot_fixture()
    program = program.model_copy(update={"scope": program.scope.model_copy(
        update={"target_id": str(APIM_ID)},
    )})
    installation = FixedProgramInstallation(
        program=program, target_identity_sha256="b" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/llm",
        publisher_subscription_id="unit-publisher", parent_policy_sha256="a" * 64,
        operation_policies_sha256={"chat-completions": "c" * 64},
        operation_contracts={"chat-completions": ("POST", "/chat/completions")},
        supported_profiles=("openai-v1",),
    )
    assert previous.apim_revision is not None and previous.policy_sha256 is not None
    migration = FixedProgramMigration(
        request_id=UUID(int=970), previous_publication_id=previous.id,
        previous_revision=previous.apim_revision, previous_spec_sha256=previous.desired_spec_sha256,
        previous_policy_sha256=previous.policy_sha256, installation=installation,
    )
    manifest = {"fixed_route_migration": migration.model_dump(mode="json")}
    spec = previous.desired_spec.model_copy(update={"removed_models": []}).model_dump(mode="json")
    digest = document_digest({**manifest, "desired_spec": spec})
    original = previous.model_dump(mode="python")
    publication = {**original, "id": migration.request_id, "status": "queued",
                   "base_release_id": previous.id, "desired_spec": spec,
                   "desired_spec_sha256": digest, "resource_manifest": manifest}
    replies = [
        None, {"id": APIM_ID, "enabled": True, "implementation": "apim"},
        {"publication_id": previous.id}, original,
        publication if fault == "existing" else None,
        None, {"generation": previous.generation + 1}, {"publication_id": previous.id},
        None if fault == "id-conflict" else publication, None,
        RuntimeError("UNIT_AUDIT_FAILURE") if fault == "audit" else None,
    ]
    connection = MagicMock()
    transaction = connection.transaction.return_value
    parse_sql = import_module("pglast").parse_sql

    def execute(query: str, parameters: object = None) -> Mock:
        del parameters
        parse_sql(query.replace("%s", "NULL"))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Mock(fetchone=Mock(return_value=reply))

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlPublicationRepositoryMixin()
    monkeypatch.setattr(repository, "_connection", Mock(return_value=context))
    monkeypatch.setattr(repository, "_validate_publication_deployments", Mock())
    arguments = (APIM_ID, spec, digest, "route_reconcile", "unit-owner")
    if fault in {"id-conflict", "audit"}:
        with pytest.raises(RuntimeError if fault == "audit" else ValueError):
            repository.create_gateway_publication(
                *arguments, expected_base_release_id=previous.id, initial_manifest=manifest,
            )
        assert transaction.__exit__.call_args.args[0] is not None
    else:
        stored = repository.create_gateway_publication(
            *arguments, expected_base_release_id=previous.id, initial_manifest=manifest,
        )
        assert stored["id"] == migration.request_id
        assert transaction.__exit__.call_args.args[0] is None
    transaction.__enter__.assert_called_once()
    calls = connection.execute.call_args_list
    assert "pg_advisory_xact_lock" in calls[0].args[0]
    assert "FOR UPDATE" in calls[3].args[0]
    assert not any(call.args[0].startswith(("UPDATE", "DELETE")) for call in calls)
    inserts = [call for call in calls if call.args[0].startswith("INSERT")]
    assert len(inserts) == (0 if fault == "existing" else 1 if fault == "id-conflict" else 3)
    if inserts:
        statement = parse_sql(inserts[0].args[0].replace("%s", "NULL"))[0].stmt
        values = dict(zip((column.name for column in statement.cols), inserts[0].args[1],
                          strict=True))
        assert values["id"] == migration.request_id and values["base_release_id"] == previous.id
        assert values["desired_spec"].obj == spec and values["resource_manifest"].obj == manifest
    assert original == previous.model_dump(mode="python")


@pytest.mark.parametrize("fault", [None, "lease", "pointer", "association", "expired", "audit"])
def test_postgres_configuration_rollback_completion_locks_pointer_and_audit(
    fault: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import import_module
    from unittest.mock import MagicMock, Mock

    from turnstile_core.persistence.repository_publications import (
        PostgreSqlPublicationRepositoryMixin,
    )

    source, _, _, first, second = _two_active_releases()
    target = source.get_gateway_publication(first.id)
    prior = source.get_gateway_publication(second.id)
    assert target is not None and prior is not None
    for release in (target, prior):
        release["resource_manifest"]["fixed_route_program"] = {"version": 1}
    operation = source.create_gateway_release_operation(
        APIM_ID, "rollback", "owner@example.com", target_release_id=first.id,
        prior_release_id=second.id, confirmation_sha256="b" * 64,
    )
    operation.update({
        "status": "validating_dependencies", "lease_owner": "worker",
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    })
    child = source.enqueue_gateway_configuration_rollback(operation["id"], "worker")
    child["status"] = "active"
    operation["status"] = "verifying_readback"
    if fault == "association":
        child["resource_manifest"]["route_rollback"]["target_release_id"] = str(UUID(int=951))
    completed = {**operation, "status": "succeeded", "lease_owner": None}
    replies = [
        None if fault == "lease" else operation, None if fault == "pointer" else child,
        target, prior, None if fault == "expired" else completed,
        RuntimeError("UNIT_COMPLETION_AUDIT_FAILED") if fault == "audit" else None,
    ]
    parse_sql = import_module("pglast").parse_sql
    connection = MagicMock()
    transaction = connection.transaction.return_value

    def execute(query: str, parameters: object = None) -> Mock:
        del parameters
        parse_sql(query.replace("%s", "NULL"))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Mock(fetchone=Mock(return_value=reply))

    connection.execute.side_effect = execute
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlPublicationRepositoryMixin()
    monkeypatch.setattr(repository, "_connection", Mock(return_value=context))
    if fault:
        with pytest.raises(RuntimeError if fault == "audit" else ValueError):
            repository.complete_gateway_configuration_rollback(operation["id"], "worker")
        assert transaction.__exit__.call_args.args[0] is not None
    else:
        assert repository.complete_gateway_configuration_rollback(
            operation["id"], "worker",
        )["status"] == "succeeded"
        assert transaction.__exit__.call_args.args[0] is None
    transaction.__enter__.assert_called_once()
    calls = connection.execute.call_args_list
    assert "FOR UPDATE" in calls[0].args[0] and "clock_timestamp()" in calls[0].args[0]
    if fault != "lease":
        assert "FOR UPDATE OF publication, effective" in calls[1].args[0]
        assert "effective.publication_id = publication.id" in calls[1].args[0]
        assert "publication.status = 'active'" in calls[1].args[0]
    assert not any(
        call.args[0].startswith(("DELETE", "UPDATE gateway_publication")) for call in calls
    )
    updates = [call for call in calls if call.args[0].startswith("UPDATE")]
    assert len(updates) == (1 if fault in {None, "expired", "audit"} else 0)
    if updates:
        assert "lease_expires_at > clock_timestamp()" in updates[0].args[0]


def test_failed_post_promotion_probe_restores_prior_release() -> None:
    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
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


@pytest.mark.parametrize("phase", ["preflight_probing", "promoting", "post_promotion_probing"])
def test_rollback_rechecks_parent_denial_before_remote_work(phase: str) -> None:
    from turnstile_core.integrations.apim_policy_components import parse_policy, serialize_policy

    repository, service, client, first, second = _two_active_releases()
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)
    _record_healthy_integrity(service, worker, first.id)
    preview = service.rollback_preview(first.id)
    service.request_rollback(
        first.id, GatewayReleaseRollbackRequest(confirmation_sha256=preview.confirmation_sha256),
        "owner@example.com",
    )
    for _ in range({"preflight_probing": 2, "promoting": 3, "post_promotion_probing": 5}[phase]):
        progress = worker.run_once("release-worker")
        assert progress is not None
    assert first.apim_revision is not None
    root = parse_policy(client.api_policies[first.apim_revision])
    denial = next(node for node in root.iter("return-response")
                  if "budgetDenialReason" in "".join(node.itertext()))
    next(parent for parent in root.iter() if denial in list(parent)).remove(denial)
    client.api_policies[first.apim_revision] = serialize_policy(root)
    count = len(client.calls)
    result = worker.run_once("release-worker")
    assert result is not None and result.status.value in {"failed", "restoring"}
    assert not any(name in {"probe", "promote"} for name, _ in client.calls[count:])
    assert repository.effective_gateway_releases[APIM_ID] == second.id


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


@pytest.mark.parametrize(
    "manifest_field", ["route_preparation", "route_dependencies", "route_rollback"],
)
def test_gc_operation_ignores_failed_uninstalled_route_record(manifest_field: str) -> None:
    repository, service, client, first, _ = _two_active_releases()
    failed = repository.get_gateway_publication(first.id)
    assert failed is not None
    failed.update({
        "status": "failed", "apim_revision": None, "policy_sha256": None,
        "resource_manifest": {manifest_field: {}},
    })
    before = deepcopy(failed)
    accepted = service.request_gc_plan(APIM_ID, "owner@example.com")
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)

    assert worker.run_once("release-worker") is not None
    completed = worker.run_once("release-worker")

    assert completed is not None and completed.id == accepted.operation.id
    assert completed.status == "succeeded"
    assert completed.gc_plan is not None
    assert completed.checkpoint == {"dry_run_only": True}
    assert repository.get_gateway_publication(first.id) == before


@pytest.mark.parametrize(
    "manifest_field", ["fixed_route_program", "fixed_route_snapshot", "route_configuration"],
)
def test_gc_operation_rejects_incomplete_installed_route_record(manifest_field: str) -> None:
    repository, service, client, first, _ = _two_active_releases()
    failed = repository.get_gateway_publication(first.id)
    assert failed is not None
    failed.update({"status": "failed", "resource_manifest": {manifest_field: {}}})
    service.request_gc_plan(APIM_ID, "owner@example.com")
    worker = GatewayReleaseOperationWorker(repository, client, parent_policy=client.policy)

    assert worker.run_once("release-worker") is not None
    completed = worker.run_once("release-worker")

    assert completed is not None and completed.status == "failed"
    assert completed.gc_plan is None


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
    service.seed_legacy_publication(
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


@pytest.mark.parametrize(
    "recorded", ["route-identity", "legacy-route-identity", "client-identity", "changed"],
)
def test_release_operation_accepts_the_identity_flavor_recorded_by_the_release(
    recorded: str,
) -> None:
    repository, service, client, _, second = _two_active_releases()
    row = repository.get_gateway_publication(second.id)
    assert row is not None
    row["resource_manifest"] = {
        **row["resource_manifest"], "apim_target_identity_sha256": recorded,
    }
    accepted = service.request_gc_plan(APIM_ID, "owner@example.com")
    resolver = Mock()
    resolver.resolve.return_value = ResolvedApimPublisherClient(
        APIM_ID, "client-identity", True, client,
        route_target_identity_sha256="route-identity",
        legacy_route_target_identity_sha256="legacy-route-identity",
    )
    worker = GatewayReleaseOperationWorker(repository, resolver)

    first = worker.run_once("release-worker")
    if recorded == "changed":
        assert first is not None and first.status == "failed"
        assert first.error_message == "APIM target identity changed after the Release was created"
        return
    completed = worker.run_once("release-worker")
    assert completed is not None and completed.id == accepted.operation.id
    assert completed.status == "succeeded" and completed.gc_plan is not None


@pytest.mark.parametrize("projection_state", ["ready", "missing", "error", "unconfigured"])
def test_application_sync_operation_adopts_read_only_apim_inventory(
    projection_state: str,
) -> None:
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

    def project(application_id: UUID) -> int | None:
        budget = repository.get_effective_gateway_application_budget(
            application_id, datetime.now(UTC).date().replace(day=1),
        )
        assert budget is not None and budget["tokens_per_minute"] == 100_000
        if projection_state == "error":
            raise RuntimeError("unit projection unavailable")
        return None if projection_state == "missing" else 100_000

    worker = GatewayReleaseOperationWorker(
        repository, ApplicationDiscoveryClient(),
        application_rate_limit_projector=project if projection_state != "unconfigured" else None,
    )

    queued = worker.run_once("release-worker")
    completed = worker.run_once("release-worker")

    assert queued is not None and queued.status == "validating_dependencies"
    if projection_state != "ready":
        assert completed is not None and completed.status == "validating_dependencies"
        assert completed.id == accepted.operation.id
        assert completed.error_message is not None and "TPM" in completed.error_message
        assert repository.gateway_applications[0]["display_name"] == "Outline Assistant"
        return
    assert completed is not None and completed.status == "succeeded"
    assert completed.id == accepted.operation.id
    assert completed.checkpoint == {
        "application_count": 1,
        "system_application_count": 0,
        "stale_subscription_count": 0,
        "read_only_apim_discovery": True,
    }
    assert repository.gateway_applications[0]["display_name"] == "Outline Assistant"


def test_owner_provisions_subscription_with_one_time_key_and_no_secret_storage() -> None:
    class ProvisioningClient(FakeApimClient):
        def __init__(self) -> None:
            super().__init__()
            self.provisioned: tuple[
                GatewayApplicationSubscriptionProvisionSpec, str, str
            ] | None = None
            self.activated = False

        def ensure_application_subscription(
            self,
            spec: GatewayApplicationSubscriptionProvisionSpec,
            primary_key: str,
            secondary_key: str,
        ) -> None:
            self.provisioned = (spec, primary_key, secondary_key)

        def activate_application_subscription(
            self, spec: GatewayApplicationSubscriptionProvisionSpec,
            primary_key: str, secondary_key: str,
        ) -> None:
            assert projections == [spec.application_id]
            assert self.provisioned == (spec, primary_key, secondary_key)
            self.activated = True

    repository = InMemoryRepository()
    cipher = CredentialCipher(Fernet.generate_key())
    service = GatewayControlPlaneService(repository, cipher)
    client = ProvisioningClient()
    projections: list[UUID] = []

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
    assert accepted.primary_key
    assert accepted.primary_key not in str(row)
    ciphertext = repository.gateway_release_operation_secret(
        accepted.operation.id
    )
    assert ciphertext is not None
    assert accepted.primary_key.encode() not in ciphertext

    worker = GatewayReleaseOperationWorker(
        repository, client, cipher=cipher,
        application_projector=lambda _gateway, application: projections.append(application),
    )
    queued = worker.run_once("release-worker")
    provisioned = worker.run_once("release-worker")
    materialized = worker.run_once("release-worker")
    assert materialized is not None and materialized.status == "verifying_readback"
    assert client.activated is False
    assert repository.gateway_release_operation_secret(accepted.operation.id) is not None
    completed = worker.run_once("release-worker")

    assert queued is not None and queued.status == "validating_dependencies"
    assert provisioned is not None and provisioned.status == "promoting"
    assert completed is not None and completed.status == "succeeded"
    assert client.provisioned is not None
    assert client.provisioned[1] == accepted.primary_key
    assert client.provisioned[2] != accepted.primary_key
    assert repository.gateway_release_operation_secret(accepted.operation.id) is None
    application = repository.gateway_applications[0]
    subscription = repository.gateway_application_subscriptions[0]
    assert application["display_name"] == "Owner Created Agent"
    assert application["application_type"] == "agent"
    assert application["owner_id"] == "owner@example.com"
    assert subscription["source"] == "managed"
    assert subscription["scope_id"] == "turnstile-ai-consumers"
    assert completed.checkpoint["key_stored"] is False
    assert completed.checkpoint["admission_ready"] is True
    assert client.activated is True


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
            primary_key: str,
            secondary_key: str,
        ) -> None:
            del spec, primary_key, secondary_key
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
    service.seed_legacy_publication(
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
    service.seed_legacy_publication(
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