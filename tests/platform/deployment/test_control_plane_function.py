from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from functions.control_plane import function_app
from turnstile_core.security import CredentialCipher


def test_disabled_publication_timer_does_not_build_dependencies(monkeypatch: Any) -> None:
    settings = SimpleNamespace(
        control_plane_enabled=True,
        gateway_publication_worker_enabled=False,
    )
    monkeypatch.setattr(function_app, "get_settings", lambda: settings)

    def reject_repository(_: object) -> None:
        raise AssertionError("disabled publication timer created a repository")

    monkeypatch.setattr(function_app, "create_repository", reject_repository)

    function_app.publish_gateway_changes(None)


@pytest.mark.parametrize("provisioning_enabled", (False, True))
def test_release_timer_never_constructs_publication_worker(
    monkeypatch: Any, provisioning_enabled: bool,
) -> None:
    settings = SimpleNamespace(
        control_plane_enabled=True,
        gateway_release_worker_enabled=True,
        gateway_application_provisioning_enabled=provisioning_enabled,
        ledger_table_endpoint="https://ledger.example.test",
        ledger_table_name="TurnstileLedger",
        gateway_release_retention_count=20,
        gateway_release_retention_days=180,
        gateway_failed_release_retention_days=30,
        gateway_release_protected_labels=["milestone", "rollback"],
        gateway_application_default_monthly_token_limit=100_000,
        gateway_application_default_tokens_per_minute=100_000,
        apim_dashboard_subscription_id="unit-dashboard",
        apim_probe_subscription_id="unit-publisher-probe",
        control_plane_lease_seconds=180,
        control_plane_max_attempts=30,
    )
    repository = object()
    publisher = object()
    cipher = object()
    calls: list[tuple[str, int, int]] = []
    ledger_calls: list[tuple[object, ...]] = []

    class Ledger:
        def __init__(self, endpoint: str, table: str) -> None:
            ledger_calls.append((endpoint, table))

        def __enter__(self) -> Ledger:
            return self

        def __exit__(self, *_: object) -> None:
            ledger_calls.append(("closed",))

    def prepare(*args: object) -> None:
        assert args[0] is repository
        assert isinstance(args[1], Ledger)
        ledger_calls.append(args[2:])

    class ReleaseWorker:
        def __init__(
            self,
            actual_repository: object,
            actual_publisher: object,
            _: object,
            **kwargs: object,
        ):
            assert actual_repository is repository
            assert actual_publisher is publisher
            projector = kwargs.pop("application_projector")
            if provisioning_enabled:
                assert callable(projector)
                projector(UUID(int=1), UUID(int=2))
            else:
                assert projector is None
            assert kwargs == {
                "application_default_token_limit": 100_000,
                "application_default_tokens_per_minute": 100_000,
                "cipher": cipher,
                "parent_policy": None,
                "dashboard_subscription_id": "unit-dashboard",
                "probe_subscription_id": "unit-publisher-probe",
            }

        def run_once(
            self, worker_id: str, lease_seconds: int, max_attempts: int
        ) -> None:
            calls.append((worker_id, lease_seconds, max_attempts))

    class PublicationWorker:
        def __init__(self, *_: object, **__: object) -> None:
            raise AssertionError("release timer constructed publication worker")

    monkeypatch.setattr(function_app, "get_settings", lambda: settings)
    monkeypatch.setattr(function_app, "create_repository", lambda _: repository)
    monkeypatch.setattr(function_app, "AzureApimPublisherClient", lambda _: publisher)
    monkeypatch.setattr(
        CredentialCipher, "from_settings", lambda _: cipher
    )
    monkeypatch.setattr(function_app, "GatewayReleaseOperationWorker", ReleaseWorker)
    monkeypatch.setattr(function_app, "GatewayPublicationWorker", PublicationWorker)
    monkeypatch.setattr(function_app, "TableStorageLedger", Ledger)
    monkeypatch.setattr(function_app, "prepare_application_ledger", prepare)
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "release-worker-instance")

    function_app.process_gateway_release_operations(None)

    assert calls == [("release-worker-instance", 180, 30)]
    assert ledger_calls == ([
        ("https://ledger.example.test", "TurnstileLedger"),
        (UUID(int=1), UUID(int=2)),
        ("closed",),
    ] if provisioning_enabled else [])