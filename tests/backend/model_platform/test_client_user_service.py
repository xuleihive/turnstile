from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import Mock
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr

from backend.services.auth_service import SessionIdentity
from backend.services.client_user import ClientUserService
from backend.services.client_user_store import ClientUserStore
from tests.backend.model_platform.test_application_access import _discovery
from tests.platform.api.test_application_access_api import StubSubscriptionKeyClient
from turnstile_core.config import Settings
from turnstile_core.domain.budget_counting import BudgetPublicationContracts, ModelCountContract
from turnstile_core.domain.models import ModelPrice
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.services.application_access import ApplicationAccessService


class PlatformSubscriptionKeys(StubSubscriptionKeyClient):
    def application_subscription_keys(self, apim_subscription_id: str) -> tuple[str, str]:
        self.calls.append(("reveal", apim_subscription_id, None))
        if apim_subscription_id == "turnstile-dashboard":
            return "platform-primary", "platform-secondary"
        return "primary-secret", "secondary-secret"


class DesktopStore:
    def issue_download(self, *args: Any) -> None:
        pass

    def activate(self, grant_id: Any) -> dict[str, Any]:
        return {"id": grant_id, "activated": True}


def client_service() -> tuple[ClientUserService, InMemoryRepository, SessionIdentity]:
    repository = InMemoryRepository()
    gateway = repository.gateways[0]
    gateway["base_url"] = "https://gateway.example.com/turnstile/llm"
    pub = uuid4()
    repository.effective_gateway_releases[gateway["id"]] = pub
    repository.gateway_publications.append(
        {
            "id": pub,
            "desired_spec": {
                "bindings": [],
                "discovery_models": [{"id": m["model_key"]} for m in repository.models],
            },
        }
    )
    applications = ApplicationAccessService(
        repository,
        sync_available=True,
        key_management_available=True,
        key_client=PlatformSubscriptionKeys(),
    )
    applications.sync_discovery(_discovery("cc-switch", "turnstile-dashboard", "master"), "test")
    settings = Settings(
        apim_gateway_url=str(gateway["base_url"]),
        apim_dashboard_subscription_key=SecretStr("platform-primary"),
        credential_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    service = ClientUserService(
        cast(ClientUserStore, DesktopStore()),
        repository,
        applications,
        settings,
        enabled=True,
        origin="https://platform.example.com",
    )
    identity = SessionIdentity(
        id=str(uuid4()),
        email="test@example.com",
        name="Test",
        role="owner",
        method="password",
        session_expires_at=datetime.now(UTC),
    )
    repository.upsert_token_budget(
        datetime.now(UTC).date().replace(day=1), "user", identity.effective_user_id,
        None, 1_000_000, 80, "fixture",
    )
    return service, repository, identity


@pytest.mark.parametrize("role", ["owner", "member"])
def test_user_download_without_personal_budget_denies_before_creating_grant(
    role: Literal["owner", "member"],
) -> None:
    service, repo, identity = client_service()
    identity = replace(identity, role=role)
    repo.token_budgets.clear()
    issue = Mock()
    service.store.issue_download = issue  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as denied:
        service.package(identity)
    assert denied.value.status_code == 403
    assert denied.value.detail == "暂未开通预算，请联系部门管理员。"
    issue.assert_not_called()


@pytest.mark.parametrize("budget_mode", ["tokens", "usd"])
@pytest.mark.parametrize("mode", ["audit", "block"])
def test_download_budget_gate_reads_configuration_not_balance_or_execution(
    monkeypatch: pytest.MonkeyPatch, budget_mode: str, mode: str,
) -> None:
    service, repo, identity = client_service()
    identity = replace(identity, governance_user_id="governance-person")
    repo.token_budgets.clear()
    period = datetime.now(UTC).date().replace(day=1)
    repo.upsert_token_budget(
        period, "user", identity.effective_user_id, "department-test",
        0 if budget_mode == "tokens" else None, 80, "fixture",
        budget_mode=budget_mode, usd_limit=Decimal("10") if budget_mode == "usd" else None,
    )
    repo.department_enforcement["department-test"] = {"mode": mode}
    monkeypatch.setattr(repo, "person_budget_state", Mock(
        side_effect=AssertionError("Download eligibility must not query usage or balance"),
    ))
    monkeypatch.setattr(service, "_execution_budget", Mock(
        side_effect=AssertionError("Download eligibility must not activate atomic execution"),
    ))
    assert service.has_personal_budget(identity)
    assert service.package(identity).startswith(b"PK")


@pytest.mark.parametrize("scope", ["organization", "department", "other-user", "past", "future"])
def test_only_current_month_own_personal_budget_qualifies(scope: str) -> None:
    service, repo, identity = client_service()
    repo.token_budgets.clear()
    now = datetime.now(UTC)
    period = date(now.year, now.month, 1)
    if scope == "past":
        period = date(now.year - 1, now.month, 1)
    elif scope == "future":
        period = date(now.year + 1, now.month, 1)
    repo.upsert_token_budget(
        period, scope if scope in {"organization", "department"} else "user",
        "someone-else" if scope == "other-user" else identity.effective_user_id,
        None, 1_000_000, 80, "fixture",
    )
    # The fixture still has valid application subscriptions; they do not qualify either.
    assert service.default_subscription(identity)["models"]
    assert not service.has_personal_budget(identity)


@pytest.mark.parametrize("name", ["Test", "研发用户"])
def test_personal_package_has_bound_authorization_and_distinct_identity(name: str) -> None:
    service, _, identity = client_service()
    identity = replace(identity, name=name)
    archive_root = f"aigateway-proxy-user-{name}"
    with ZipFile(BytesIO(service.package(identity))) as archive:
        assert {entry.split("/", 1)[0] for entry in archive.namelist()} == {archive_root}
        config = json.loads(archive.read(f"{archive_root}/aigateway-user.config.json"))
        label = "test" if name == "Test" else "xn--oory84avxopxc"
        assert config["providerId"].startswith(f"aigateway-user-{label}-")
        assert config["userName"] == name
        assert config["name"] == f"AIGateway User · {name}"
        assert identity.id not in config["providerId"]
        assert UUID(identity.id).hex not in config["providerId"]
        assert config["userId"] == identity.id
        assert not {"subscriptionKey", "password"}.intersection(config)
        assert config["authorization"]["access"].startswith("tsca_")
        assert config["authorization"]["refresh"].startswith("tscr_")
        assert config["authorization"]["installationId"] == config["installationId"]
        assert config["selection"]["apim_subscription_id"] == "turnstile-dashboard"
        assert config["authorization"]["accountId"] == identity.name
        assert config["authorization"]["uid"] == identity.id
        assert config["catalog"]["models"]
        assert "images.mjs" in {name.rsplit("/", 1)[-1] for name in archive.namelist()}
        package = json.loads(archive.read(f"{archive_root}/package.json"))
        assert package["name"].startswith("opencode-aigateway-user-")
        assert package["version"] == "0.5.1"
        assert f"{archive_root}/native-images.mjs" in archive.namelist()
        image_source = archive.read(f"{archive_root}/images.mjs").decode()
        assert "isAbsolute(entry.spec)" in image_source
        assert 'entry.spec?.startsWith("/")' not in image_source
        assert package["magpie"]["icon"] == config["icon"]
        assert f"{archive_root}/icons/icon.svg" in archive.namelist()
        icon_manifest = json.loads(archive.read(f"{archive_root}/icons/manifest.json"))
        assert config["iconRevision"] == icon_manifest["revision"]
        assert all((f.external_attr >> 16) & 0o777 == 0o600 for f in archive.infolist())


@pytest.mark.parametrize(("name", "label"), [
    ("Edwin", "Edwin"),
    ("研发用户", "研发用户"),
    ('../../研发 "Agent": A\\B\r\n', "研发-Agent-A-B"),
    ("..", "user"),
    ("03ba5bbc-07df-42bb-a13d-6faebf421ae2", "user"),
    ("", "test-example.com"),
    ("用户" * 100, "用户" * 13),
])
def test_user_archive_extracts_to_its_safe_download_name(
    tmp_path: Path, name: str, label: str,
) -> None:
    service, _, identity = client_service()
    identity = replace(identity, name=name)
    filename = service.package_filename(identity)
    archive_root = f"aigateway-proxy-user-{label}"
    assert filename == archive_root + ".zip"
    with ZipFile(BytesIO(service.package(identity))) as archive:
        assert archive.testzip() is None
        assert {entry.split("/", 1)[0] for entry in archive.namelist()} == {archive_root}
        archive.extractall(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == [archive_root]
    assert (tmp_path / archive_root / "package.json").is_file()
    assert (tmp_path / archive_root / "icons/icon.svg").is_file()
    config = json.loads((tmp_path / archive_root / "aigateway-user.config.json").read_text())
    assert config["userId"] == identity.id


def test_member_default_uses_the_actual_platform_credential_not_owned_name_order() -> None:
    service, repo, owner = client_service()
    member = replace(owner, role="member", id=str(uuid4()), email="member@example.com")
    selected = service.default_subscription(member)
    assert selected["apim_subscription_id"] == "turnstile-dashboard"
    assert selected["models"]
    application = repo.get_gateway_application(UUID(selected["application_id"]))
    assert application is not None and application["owner_id"] is None
    assert service.subscription_credential(selected) == "platform-primary"
    repo.gateway_applications[0]["owner_id"] = member.email
    assert service.default_subscription(member)["apim_subscription_id"] == "turnstile-dashboard"


def test_catalog_protocol_aliases_and_live_empty_policy() -> None:
    service, repo, owner = client_service()
    choice = service.default_subscription(owner)
    grant = {"id": uuid4(), "selection": choice}
    data = service.catalog(grant, owner)
    assert data["models"] and data["aliases"]
    assert data["user"] == {"id": owner.id, "name": owner.name}
    repo.user_model_policies[owner.email] = {"user_id": owner.email, "model_ids": []}
    assert service.catalog(grant, owner, fresh=True)["models"] == {}


def test_platform_secondary_and_configured_gateway_key_take_precedence() -> None:
    service, repo, owner = client_service()
    service.settings.apim_dashboard_subscription_key = SecretStr("platform-secondary")
    selected = service.default_subscription(owner)
    assert selected["apim_subscription_id"] == "turnstile-dashboard"
    assert service.subscription_credential(selected) == "platform-secondary"
    repo.gateways[0]["credential_ciphertext"] = service.cipher.encrypt("primary-secret")
    assert service.default_subscription(owner)["apim_subscription_id"] == "cc-switch"


def test_unknown_platform_key_fails_closed_instead_of_choosing_an_owned_application() -> None:
    service, _, owner = client_service()
    service.settings.apim_dashboard_subscription_key = SecretStr("unknown")
    with pytest.raises(HTTPException) as denied:
        service.default_subscription(owner)
    assert denied.value.status_code == 409


def test_existing_user_selection_remains_bound_until_reauthentication() -> None:
    service, _, owner = client_service()
    old = next(x for x in service.subscriptions(owner) if x["apim_subscription_id"] == "cc-switch")
    grant = {"id": uuid4(), "selection": old}
    assert service.catalog(grant, owner)["selection"]["subscription_id"] == old["subscription_id"]
    assert service.default_subscription(owner)["apim_subscription_id"] == "turnstile-dashboard"


def test_model_permission_revocation_is_checked_even_with_cached_catalog() -> None:
    service, repo, owner = client_service()
    selected = service.default_subscription(owner)
    model = selected["models"][0]
    service.validate_model(owner, selected, model)
    repo.user_model_policies[owner.email] = {"user_id": owner.email, "model_ids": []}
    with pytest.raises(HTTPException) as denied:
        service.validate_model(owner, selected, model)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("mode", ["audit", "block"])
def test_usage_shows_only_the_verified_personal_monthly_budget(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    service, repo, owner = client_service()
    owner = replace(owner, governance_user_id="person@example.com")
    personal = Mock(return_value={"used_tokens": 22555, "token_limit": 60000000, "mode": mode})
    application = Mock(side_effect=AssertionError("Usage must not read the shared application"))
    selection = Mock(side_effect=AssertionError("Usage must not depend on a subscription"))
    monkeypatch.setattr(repo, "person_budget_state", personal)
    monkeypatch.setattr(service.applications, "application", application)
    monkeypatch.setattr(service, "selected", selection)

    result = service.usage({"id": uuid4(), "selection": {"application_id": str(uuid4())}}, owner)

    assert result["user"] == "Test"
    assert len(result["windows"]) == 1
    window = result["windows"][0]
    assert window["name"] == "个人月度 Token"
    assert window["amount"] == 22555
    assert window["limit"] == 60000000
    assert window["unit"] == "tokens"
    assert window["used"] == pytest.approx(22555 / 60000000 * 100)
    assert window["aside"] is (mode == "audit")
    start = datetime.fromisoformat(result["as_of"]).date().replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    personal.assert_called_once_with("person@example.com", start, end)
    assert window["resetsAt"] == datetime(end.year, end.month, 1, tzinfo=UTC).isoformat()
    assert window["span"] == (end - start).days * 86400
    application.assert_not_called()
    selection.assert_not_called()


def test_usage_without_personal_budget_does_not_fall_back_to_an_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, owner = client_service()
    monkeypatch.setattr(repo, "person_budget_state", Mock(return_value=None))
    application = Mock(side_effect=AssertionError("No shared allowance fallback"))
    monkeypatch.setattr(service.applications, "application", application)

    result = service.usage({"id": uuid4(), "selection": {}}, owner)

    assert result["windows"] == []
    assert result["balance"] == "未配置额度"
    assert result["signIn"] == "kept"
    application.assert_not_called()


def test_usd_personal_usage_retains_exact_costs_and_counts_reserved_amounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, owner = client_service()
    monkeypatch.setattr(repo, "person_budget_state", Mock(return_value={
        "budget_mode": "usd", "token_limit": None, "used_tokens": 0, "mode": "block",
    }))
    monkeypatch.setattr(service, "_execution_budget", Mock(return_value={
        "budget_mode": "usd", "ledger_status": "current", "policy_pending": False,
        "usd_limit": Decimal("10"), "used_usd": Decimal("0.12345678901234"),
        "pending_reserved_usd": Decimal("0.2"), "finalized_upper_bound_usd": Decimal("0.1"),
        "enforce": True, "ledger_version": 3,
    }))
    result = service.usage({"id": uuid4()}, owner)
    assert result["quota_contract_version"] == 2
    assert result["budget"]["confirmed"] == "0.12345678901234"
    assert result["budget"]["available"] == "9.57654321098766"
    assert result["windows"][0]["name"] == "个人月度 USD"
    assert result["windows"][0]["unit"] == "USD"
    assert result["windows"][0]["amount"] == pytest.approx(0.42345678901234)
    assert result["windows"][0]["aside"] is False


@pytest.mark.parametrize("view", [None, {"ledger_status": "unavailable"}, {
    "ledger_status": "current", "budget_mode": "usd", "policy_pending": True,
}])
def test_usd_usage_never_invents_zero_for_an_unverified_or_pending_scope(
    monkeypatch: pytest.MonkeyPatch, view: Any,
) -> None:
    service, repo, owner = client_service()
    monkeypatch.setattr(repo, "person_budget_state", Mock(return_value={
        "budget_mode": "usd", "token_limit": None, "used_tokens": 0, "mode": "block",
    }))
    monkeypatch.setattr(service, "_execution_budget", Mock(return_value=view))
    with pytest.raises(HTTPException) as error:
        service.usage({"id": uuid4()}, owner)
    assert error.value.status_code == 503


def test_personal_usd_catalog_requires_verified_operation_model_identity_and_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, owner = client_service()
    status = {"budget_mode": "usd", "policy_revision": 1}
    monkeypatch.setattr(service, "_personal_execution_status", lambda *_: status)
    models = repo.models
    publication = repo.gateway_publications[0]
    contracts = {
        m["model_key"]: ModelCountContract(
            model_id=m["id"], algorithm="request_total_ceiling_v1", output_field="none",
            request_token_ceiling=100000, evidence_sha256="a" * 64,
            verified_at=datetime.now(UTC),
        ) for m in models
    }
    snapshot = BudgetPublicationContracts(
        publication_id=publication["id"], gateway_profile_id=repo.gateways[0]["id"], epoch=uuid4(),
        release_spec_sha256="b" * 64, binding_sha256={}, model_contracts=contracts,
    )
    publication["resource_manifest"] = {"budget_contracts": snapshot.model_dump(mode="json")}
    selected = service.default_subscription(owner)
    assert selected["models"]
    first = selected["models"][0]["id"]
    raw_contracts = publication["resource_manifest"]["budget_contracts"]["model_contracts"]
    for contract in raw_contracts.values():
        contract["model_id"] = str(uuid4())
    # A new policy revision invalidates the catalogue cache; old assignments cannot leak through.
    status["policy_revision"] = 2
    assert all(not item["models"] for item in service.subscriptions(owner))
    matching = next(m for m in models if m["model_key"] == first)
    raw = raw_contracts[first]
    raw.update(model_id=str(matching["id"]), algorithm="legacy_request_estimate_v1",
               output_field="max_tokens", request_token_ceiling=None, output_estimate=4096)
    status["policy_revision"] = 3
    assert all(not item["models"] for item in service.subscriptions(owner))
    raw.update(algorithm="request_total_ceiling_v1", output_field="none",
               request_token_ceiling=100000, output_estimate=None)
    with monkeypatch.context() as patch:
        patch.setattr(ModelPrice, "from_registry", lambda *_: None)
        status["policy_revision"] = 4
        assert all(not item["models"] for item in service.subscriptions(owner))
    status.update(budget_mode="tokens", policy_revision=5)
    assert first in {m["id"] for m in service.default_subscription(owner)["models"]}


def test_personal_usage_keeps_the_sixty_second_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repo, owner = client_service()
    personal = Mock(return_value={"used_tokens": 10, "token_limit": 100, "mode": "audit"})
    clock = Mock(return_value=10)
    monkeypatch.setattr(repo, "person_budget_state", personal)
    monkeypatch.setattr("backend.services.client_user.time.monotonic", clock)
    grant = {"id": uuid4(), "selection": None}
    first = service.usage(grant, owner)
    personal.return_value = {"used_tokens": 20, "token_limit": 100, "mode": "audit"}
    clock.return_value = 69
    assert service.usage(grant, owner) == first
    personal.assert_called_once()
    clock.return_value = 71
    assert service.usage(grant, owner)["windows"][0]["amount"] == 20
    assert personal.call_count == 2
    assert first["cache_seconds"] == 60


def test_personal_usage_control_bypasses_cache_and_updates_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo, owner = client_service()
    personal = Mock(return_value={"used_tokens": 10, "token_limit": 100, "mode": "audit"})
    monkeypatch.setattr(repo, "person_budget_state", personal)
    grant = {"id": uuid4(), "selection": None}
    assert service.usage(grant, owner)["windows"][0]["aside"] is True
    personal.return_value = {"used_tokens": 20, "token_limit": 100, "mode": "block"}
    assert service.usage(grant, owner)["windows"][0]["aside"] is True
    fresh = service.usage(grant, owner, fresh=True)
    assert fresh["windows"][0]["aside"] is False
    assert fresh["windows"][0]["amount"] == 20
    assert service.usage(grant, owner) == fresh
    assert personal.call_count == 2
