from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock, call
from uuid import UUID, uuid4

import pytest

from tests.backend.model_platform.test_model_availability import gateway_availability_fixture
from turnstile_core.domain.model_availability import (
    ModelAvailabilityCoverage,
    ModelAvailabilityUnavailable,
)
from turnstile_core.integrations import model_availability_verifier as gateway_verification
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.services import model_availability as availability


@pytest.fixture(autouse=True)
def isolated_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(availability, "_TARGET_AUDIT_BACKOFF_UNTIL", {}, raising=False)
    monkeypatch.setattr(availability, "_TARGET_AUDIT_FAILING_SINCE", {}, raising=False)
    monkeypatch.setattr(availability, "_TARGET_RENEW_BACKOFF_UNTIL", {})


@pytest.fixture
def audit_clock(monkeypatch: pytest.MonkeyPatch) -> Mock:
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr(availability, "monotonic", clock)
    return clock


def coordinator_fixture(
    inventory: dict[UUID, str],
) -> tuple[availability.ModelAvailabilityCoordinator, Mock]:
    repository = Mock()
    repository.get_model_availability_inventory.return_value = inventory
    coordinator = availability.ModelAvailabilityCoordinator(repository, Mock(), "unit", "Unit")
    return coordinator, repository


def test_continuous_audit_failure_has_a_backoff_shared_across_coordinators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = uuid4()
    repository = Mock()
    repository.get_model_availability_inventory.return_value = {target_id: "a" * 64}
    first = availability.ModelAvailabilityCoordinator(repository, Mock(), "unit", "Unit")
    second = availability.ModelAvailabilityCoordinator(repository, Mock(), "unit", "Unit")
    audit = Mock(side_effect=ModelAvailabilityUnavailable("Unit audit failed"))
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr(availability.ModelAvailabilityCoordinator, "_verify_target", audit)
    monkeypatch.setattr(availability, "monotonic", clock)

    assert first.refresh() == 0
    audit.assert_called_once_with(target_id, "a" * 64)
    for instant in (1001.0, 1120.0, 1299.999):
        clock.return_value = instant
        assert second.refresh() == 0
        assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    assert audit.call_count == 4

    clock.return_value = 1300.0
    assert second.refresh() == 0
    assert audit.call_count == 5
    assert {(target_id, "a" * 64): 1600.0} == availability._TARGET_AUDIT_BACKOFF_UNTIL
    clock.return_value = 1599.999
    assert first.refresh() == 0
    assert audit.call_count == 5
    clock.return_value = 1600.0
    assert first.refresh() == 0
    assert audit.call_count == 6
    repository.save_model_availability_coverage.assert_not_called()


def test_single_transient_failure_is_reaudited_after_two_minutes(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})
    audit = Mock(side_effect=[ModelAvailabilityUnavailable("Transient timeout"), None])
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh() == 0
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    audit_clock.return_value = 1120.0
    assert coordinator.refresh() == 1
    assert audit.call_count == 2
    assert availability._TARGET_AUDIT_FAILING_SINCE == {}


def test_audit_backoff_starts_when_a_slow_failure_finishes(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})

    def timeout(_target: UUID, _digest: str) -> None:
        audit_clock.return_value += 150.0
        raise PolicyCompilationError("Unit audit time window exceeded")

    audit = Mock(side_effect=timeout)
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh() == 0
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 0
    assert audit.call_count == 2
    assert {(target_id, "a" * 64): 1750.0} == availability._TARGET_AUDIT_BACKOFF_UNTIL
    audit_clock.return_value = 1749.999
    assert coordinator.refresh() == 0
    assert audit.call_count == 2
    audit_clock.return_value = 1750.0
    assert coordinator.refresh() == 0
    assert audit.call_count == 3
    assert {(target_id, "a" * 64): 2200.0} == availability._TARGET_AUDIT_BACKOFF_UNTIL


def test_first_failure_does_not_count_time_spent_waiting_or_running(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})

    def delayed_failure(_target: UUID, _digest: str) -> None:
        audit_clock.return_value = 1301.0
        raise ModelAvailabilityUnavailable("First observed failure")

    audit = Mock(side_effect=delayed_failure)
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh() == 0
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    assert {(target_id, "a" * 64): 1301.0} == availability._TARGET_AUDIT_FAILING_SINCE
    audit_clock.return_value = 1421.0
    audit.side_effect = None
    assert coordinator.refresh() == 1
    assert audit.call_count == 2
    assert availability._TARGET_AUDIT_FAILING_SINCE == {}


def test_failed_audit_does_not_throttle_other_targets(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    failed, healthy = uuid4(), uuid4()
    coordinator, _repository = coordinator_fixture({failed: "a" * 64, healthy: "a" * 64})

    def check(target_id: UUID, _digest: str) -> None:
        if target_id == failed:
            raise ModelAvailabilityUnavailable("Unit audit failed")

    audit = Mock(side_effect=check)
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh() == 1
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 1
    audit_clock.return_value = 1301.0
    assert coordinator.refresh() == 1
    assert audit.call_args_list.count(call(failed, "a" * 64)) == 2
    assert audit.call_args_list.count(call(healthy, "a" * 64)) == 3
    assert {(failed, "a" * 64): 1600.0} == availability._TARGET_AUDIT_BACKOFF_UNTIL


def test_successful_audit_clears_failure_and_is_not_throttled(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})
    audit = Mock(side_effect=[
        ModelAvailabilityUnavailable("Unit audit failed"),
        ModelAvailabilityUnavailable("Unit audit failed"), None, None,
    ])
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1600.0
    assert coordinator.refresh() == 1
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    assert availability._TARGET_AUDIT_FAILING_SINCE == {}
    assert coordinator.refresh() == 1
    assert audit.call_count == 4


def test_digest_changes_discard_previous_audit_backoff(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, repository = coordinator_fixture({target_id: "a" * 64})
    audit = Mock(side_effect=ModelAvailabilityUnavailable("Unit audit failed"))
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    for instant, digest, deadline in (
        (1000.0, "a" * 64, None), (1300.0, "a" * 64, 1600.0),
        (1301.0, "b" * 64, None), (1302.0, "a" * 64, None),
    ):
        audit_clock.return_value = instant
        repository.get_model_availability_inventory.return_value = {target_id: digest}
        assert coordinator.refresh() == 0
        assert (
            {} if deadline is None else {(target_id, digest): deadline}
        ) == availability._TARGET_AUDIT_BACKOFF_UNTIL
    assert audit.call_args_list == [
        call(target_id, "a" * 64), call(target_id, "a" * 64),
        call(target_id, "b" * 64), call(target_id, "a" * 64),
    ]


@pytest.mark.parametrize("retain_other", [False, True])
def test_removed_inventory_discards_backoff_before_same_digest_returns(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock, retain_other: bool,
) -> None:
    removed, other = uuid4(), uuid4()
    coordinator, repository = coordinator_fixture({removed: "a" * 64, other: "b" * 64})
    audit = Mock(side_effect=ModelAvailabilityUnavailable("Unit audit failed"))
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1301.0
    repository.get_model_availability_inventory.return_value = (
        {other: "b" * 64} if retain_other else {}
    )
    assert coordinator.refresh() == 0
    assert audit.call_count == 4
    assert (
        {(other, "b" * 64): 1600.0} if retain_other else {}
    ) == availability._TARGET_AUDIT_BACKOFF_UNTIL
    repository.get_model_availability_inventory.return_value[removed] = "a" * 64
    assert coordinator.refresh() == 0
    assert audit.call_args_list.count(call(removed, "a" * 64)) == 3
    assert audit.call_args_list.count(call(other, "b" * 64)) == 2


@pytest.mark.parametrize("renew", [False, True])
def test_busy_writer_clears_audit_failure_without_a_new_backoff(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock, renew: bool,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})
    audit = Mock(side_effect=ModelAvailabilityUnavailable("Unit audit failed"))
    busy = ModelAvailabilityUnavailable("Availability proof writer is busy")
    renewal = Mock(side_effect=busy)
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    monkeypatch.setattr(coordinator, "_renew_target", renewal)
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1301.0 if renew else 1600.0
    if not renew:
        audit.side_effect = busy
    assert coordinator.refresh(renew=renew) == 0
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    assert availability._TARGET_AUDIT_FAILING_SINCE == {}
    assert availability._TARGET_RENEW_BACKOFF_UNTIL == {}
    audit.side_effect = None
    assert coordinator.refresh() == 1
    assert audit.call_count == (3 if renew else 4)


def test_successful_renewal_is_not_delayed_by_audit_backoff_and_clears_it(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})
    audit = Mock(side_effect=ModelAvailabilityUnavailable("Unit audit failed"))
    renewal = Mock()
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    monkeypatch.setattr(coordinator, "_renew_target", renewal)
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1301.0
    assert coordinator.refresh(renew=True) == 1
    renewal.assert_called_once_with(target_id, "a" * 64)
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    audit.side_effect = None
    assert coordinator.refresh() == 1
    assert audit.call_count == 3


def test_audit_failure_does_not_move_the_renewal_retry_deadline(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    target_id = uuid4()
    coordinator, _repository = coordinator_fixture({target_id: "a" * 64})
    failure = ModelAvailabilityUnavailable("Unit target unavailable")
    renewal, audit = Mock(side_effect=failure), Mock(side_effect=failure)
    monkeypatch.setattr(coordinator, "_renew_target", renewal)
    monkeypatch.setattr(coordinator, "_verify_target", audit)
    assert coordinator.refresh(renew=True) == 0
    audit_clock.return_value = 1100.0
    assert coordinator.refresh() == 0
    assert {(target_id, "a" * 64): 1300.0} == availability._TARGET_RENEW_BACKOFF_UNTIL
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    audit_clock.return_value = 1299.999
    assert coordinator.refresh(renew=True) == 0
    assert renewal.call_count == 1
    audit_clock.return_value = 1300.0
    assert coordinator.refresh(renew=True) == 0
    assert renewal.call_count == 2
    assert {(target_id, "a" * 64): 1600.0} == availability._TARGET_RENEW_BACKOFF_UNTIL
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    audit_clock.return_value = 1400.0
    assert coordinator.refresh() == 0
    assert {(target_id, "a" * 64): 1700.0} == availability._TARGET_AUDIT_BACKOFF_UNTIL
    assert {(target_id, "a" * 64): 1600.0} == availability._TARGET_RENEW_BACKOFF_UNTIL


def test_fixed_program_audit_recovers_with_the_existing_proof_guard(
    audit_clock: Mock,
) -> None:
    target_id = uuid4()
    repository, clients = Mock(), Mock()
    repository.get_model_availability_inventory.return_value = {target_id: "a" * 64}
    repository.effective_gateway_publication.return_value = {
        "gateway_profile_id": target_id, "resource_manifest": {"fixed_route_program": {}},
    }
    repository.model_availability_proof_guard.return_value = nullcontext()
    repository.save_model_availability_coverage.return_value = True
    audit = Mock(side_effect=PolicyCompilationError("Unit audit time window exceeded"))
    coordinator = availability.ModelAvailabilityCoordinator(
        repository, clients, "unit", "Unit", fixed_program_verifier=audit,
    )
    assert coordinator.refresh() == 0
    audit_clock.return_value = 1299.999
    assert coordinator.refresh() == 0
    assert audit.call_count == 2
    repository.save_model_availability_coverage.assert_not_called()
    now = datetime.now(UTC)
    coverage = ModelAvailabilityCoverage(
        target_id=target_id, inventory_digest="a" * 64, policy_digest="b" * 64,
        serving_surfaces=("managed/current/responses",),
        verified_surfaces=("managed/current/responses",),
        verified_at=now, expires_at=now + timedelta(seconds=120),
    )

    def verify(_target: UUID, _digest: str, _projected: Any, before_proof: Any) -> Any:
        before_proof()
        return coverage

    audit.side_effect = verify
    audit_clock.return_value = 1300.0
    assert coordinator.refresh() == 1
    assert coordinator.refresh() == 1
    assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
    assert repository.model_availability_proof_guard.call_args_list == [
        call(target_id, wait=True), call(target_id, wait=True),
    ]
    assert repository.save_model_availability_coverage.call_args_list == [
        call(coverage), call(coverage),
    ]
    clients.resolve.assert_not_called()


def test_backoff_cannot_extend_proof_expiry_or_the_management_evidence_window(
    monkeypatch: pytest.MonkeyPatch, audit_clock: Mock,
) -> None:
    verifier, state, requests = gateway_availability_fixture()
    with verifier.client:
        coverage = verifier.verify("a" * 64, [])
        assert verifier.confirmation_plan is not None
        management_verified_at = verifier.confirmation_plan.management_verified_at
        assert management_verified_at is not None
        original_coverage = coverage.model_dump()
        verifier.read_policy = Mock(side_effect=AssertionError("Unexpected policy read"))
        verifier.list_resources = Mock(side_effect=AssertionError("Unexpected inventory read"))
        requests.clear()
        repository, clients = Mock(), Mock()
        repository.get_model_availability_inventory.return_value = {coverage.target_id: "a" * 64}
        repository.effective_gateway_publication.return_value = None
        repository.list_gateway_deployments.return_value = []
        repository.get_model_availability_states.return_value = {state.model_id: state}
        repository.get_model_availability_coverage.return_value = coverage
        repository.model_availability_proof_guard.return_value = nullcontext()
        client = Mock(spec=AzureApimPublisherClient)
        client.verify_model_availability.side_effect = PolicyCompilationError("Unit audit failed")
        clients.resolve.return_value = Mock(client=client)
        coordinator = availability.ModelAvailabilityCoordinator(
            repository, clients, "https://unit.table.core.windows.net", "Unit",
        )
        wall_clock = Mock(wraps=datetime)
        wall_clock.now.return_value = coverage.verified_at
        monkeypatch.setattr(availability, "datetime", wall_clock)
        monkeypatch.setattr(gateway_verification, "datetime", wall_clock)

        def renew(
            _target: UUID, digest: str, deployments: Any, _endpoint: str,
            _table: str, read_states: Any, **_kwargs: Any,
        ) -> ModelAvailabilityCoverage:
            assert verifier.confirmation_plan is not None
            return verifier.renew(verifier.confirmation_plan, digest, deployments, read_states)

        def save(value: ModelAvailabilityCoverage) -> bool:
            repository.get_model_availability_coverage.return_value = value
            return True

        client.renew_model_availability.side_effect = renew
        repository.save_model_availability_coverage.side_effect = save
        assert coordinator.refresh() == 0
        audit_clock.return_value = 1121.0
        wall_clock.now.return_value = management_verified_at + timedelta(seconds=121)
        assert coordinator.refresh() == 0
        assert coordinator.verify(state, {coverage.target_id: "a" * 64})[0].status == "unknown"
        repository.save_model_availability_coverage.assert_not_called()
        assert coverage.model_dump() == original_coverage

        audit_clock.return_value = 1220.0
        wall_clock.now.return_value = management_verified_at + timedelta(seconds=220)
        assert coordinator.refresh(renew=True) == 1
        renewed = repository.save_model_availability_coverage.call_args.args[0]
        assert renewed.verified_at == wall_clock.now.return_value
        assert renewed.expires_at == management_verified_at + timedelta(seconds=300)
        assert (renewed.expires_at - renewed.verified_at).total_seconds() == 80
        assert verifier.confirmation_plan is not None
        assert verifier.confirmation_plan.management_verified_at == management_verified_at
        assert coverage.model_dump() == original_coverage

        audit_clock.return_value = 1221.0
        wall_clock.now.return_value = management_verified_at + timedelta(seconds=221)
        assert coordinator.refresh() == 0
        audit_clock.return_value = 1300.0
        wall_clock.now.return_value = management_verified_at + timedelta(seconds=300)
        assert coordinator.refresh() == 0
        request_count = len(requests)
        assert coordinator.refresh(renew=True) == 0
        assert len(requests) == request_count
        assert repository.save_model_availability_coverage.call_count == 1
        assert not renewed.current(wall_clock.now.return_value, "a" * 64)
        result = coordinator.verify(state, {coverage.target_id: "a" * 64})[0]
        assert result.status == "unknown"
        assert result.reason == "model_availability_capability_unverified"
        assert availability._TARGET_AUDIT_BACKOFF_UNTIL == {}
        assert client.verify_model_availability.call_count == 4
        assert client.renew_model_availability.call_count == 2
        client.confirm_model_availability.assert_not_called()
        verifier.read_policy.assert_not_called()
        verifier.list_resources.assert_not_called()
        assert {request.method for request in requests} == {"GET", "HEAD"}