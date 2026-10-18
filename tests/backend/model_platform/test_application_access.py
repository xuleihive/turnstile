from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import TypeAdapter

from tests.backend.model_platform.control_plane_support import (
    APIM_ID,
    StubTokenProvider,
    publisher_settings,
)
from tests.platform.api.api_support import _usage_record
from turnstile_core.config import Settings
from turnstile_core.domain.application_access import (
    MAX_TOKENS_PER_MINUTE,
    GatewayApplicationAvatarUpdate,
    GatewayApplicationBudgetUpdate,
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationLedgerState,
    GatewayApplicationModelAccessUpdate,
    GatewayApplicationOrganizationUpdate,
    GatewayApplicationProvisioningDefaults,
    GatewayApplicationSubscriptionCreate,
    GatewayApplicationSubscriptionKeyRotation,
    GatewayApplicationSubscriptionProvisionSpec,
    TokensPerMinute,
    UsageApplicationAttribution,
    application_id_for,
    application_organization,
    application_subscription_id_for,
    decode_application_avatar_data_url,
)
from turnstile_core.integrations.apim_control_plane import (
    AzureApimPublisherClient,
    AzureApimSubscriptionKeyClient,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository_applications import (
    PostgreSqlApplicationRepositoryMixin,
    _json_value,
)
from turnstile_core.services.application_access import ApplicationAccessService


def test_application_organization_uses_registered_identity_not_freeform_name() -> None:
    assert application_organization({}).organization_id is None
    assigned = application_organization({"organization_id": "org-contoso-global"})
    assert assigned.organization_name == "Contoso Global"
    with pytest.raises(ValueError, match="Unknown application organization"):
        application_organization({"organization_id": "Contoso Global"})


def test_application_department_uses_registered_identity_within_organization() -> None:
    from turnstile_core.domain.enterprise import enterprise_catalog

    department = next(
        item for item in enterprise_catalog().departments if item.id == "department-platform"
    )
    assigned = application_organization(
        {"organization_id": "org-contoso-global"},
        department_id=department.id,
    )
    assert assigned.department_id == department.id
    assert assigned.department_name == department.name


@pytest.mark.parametrize(
    "organization_id,department_id,reason",
    [
        (None, "department-platform", "requires an organization"),
        ("org-contoso-global", "unknown-department", "Unknown application department"),
        ("unit-other-org", "department-platform", "does not belong"),
    ],
)
def test_application_department_rejects_invalid_assignment(
    organization_id: str | None,
    department_id: str,
    reason: str,
) -> None:
    from turnstile_core.domain.enterprise import enterprise_catalog
    from turnstile_core.domain.models import EnterpriseEntity

    catalog = enterprise_catalog()
    catalog.organizations.append(EnterpriseEntity(id="unit-other-org", name="Unit other org"))
    with (
        patch("turnstile_core.domain.enterprise.enterprise_catalog", return_value=catalog),
        pytest.raises(ValueError, match=reason),
    ):
        application_organization(
            {"organization_id": organization_id} if organization_id else {},
            department_id=department_id,
        )


def test_application_person_uses_directory_identity_in_selected_department() -> None:
    from turnstile_core.domain.enterprise import enterprise_catalog

    catalog = enterprise_catalog()
    person = next(item for item in catalog.users if item.parent_id == "department-platform")
    assigned = application_organization(
        {"organization_id": "org-contoso-global"},
        "department-platform",
        person_id=person.id,
        catalog=catalog,
    )
    assert assigned.person_id == person.id
    assert assigned.person_name == person.name


@pytest.mark.parametrize(
    "department_id,person_id,reason",
    [
        (None, "test.user01@contoso.com", "requires a department"),
        ("department-platform", "unknown-person", "Unknown application person"),
        ("department-commerce", "test.user01@contoso.com", "does not belong"),
    ],
)
def test_application_person_rejects_invalid_directory_assignment(
    department_id: str | None,
    person_id: str,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        application_organization(
            {"organization_id": "org-contoso-global"},
            department_id,
            person_id=person_id,
        )


def test_application_person_save_clear_preserve_and_parent_change_do_not_rebill() -> None:
    from copy import deepcopy

    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=False)
    service.sync_discovery(_discovery("person-owner"), "owner@example.com")
    application_id = application_id_for(APIM_ID, "person-owner")
    protected = deepcopy(
        (
            repository.gateway_application_budgets,
            repository.usage_records,
            repository.gateway_application_model_policies,
        )
    )
    with patch.object(
        repository,
        "observed_users",
        return_value=[
            {
                "user_id": "observed.owner@contoso.com",
                "user_ref": "Observed Owner",
                "department_id": "department-platform",
            }
        ],
    ):
        selected = GatewayApplicationOrganizationUpdate(
            organization_id="org-contoso-global",
            department_id="department-platform",
            person_id="observed.owner@contoso.com",
        )
        saved = service.update_application_organization(application_id, selected, "owner")
        assert saved.person_name == "Observed Owner"
        detail = service.application(application_id)
        assert detail.person_id == detail.owner_id == "observed.owner@contoso.com"
        assert detail.person_name == saved.person_name
        assert detail.person_assignment_supported is True
        count = len(repository.gateway_application_audit)
        service.update_application_organization(application_id, selected, "owner")
        assert len(repository.gateway_application_audit) == count
        preserved = service.update_application_organization(
            application_id,
            GatewayApplicationOrganizationUpdate(
                organization_id="org-contoso-global",
                department_id="department-platform",
            ),
            "owner",
        )
        assert preserved.person_id == saved.person_id
        cleared = service.update_application_organization(
            application_id,
            GatewayApplicationOrganizationUpdate(
                organization_id="org-contoso-global",
                department_id="department-platform",
                person_id=None,
            ),
            "owner",
        )
        assert cleared.person_id is None
        assert cleared.department_id == "department-platform"
        service.update_application_organization(application_id, selected, "owner")
        changed = service.update_application_organization(
            application_id,
            GatewayApplicationOrganizationUpdate(
                organization_id="org-contoso-global",
                department_id="department-commerce",
            ),
            "owner",
        )
        assert changed.person_id is None
        service.update_application_organization(application_id, selected, "owner")
        before_audit = len(repository.gateway_application_audit)
        with pytest.raises(ValueError, match="does not belong"):
            service.update_application_organization(
                application_id,
                GatewayApplicationOrganizationUpdate(
                    organization_id="org-contoso-global",
                    department_id="department-commerce",
                    person_id="observed.owner@contoso.com",
                ),
                "owner",
            )
        assert len(repository.gateway_application_audit) == before_audit
        service.update_application_organization(
            application_id,
            GatewayApplicationOrganizationUpdate(),
            "owner",
        )
        assert service.application(application_id).person_id is None
    assert (
        repository.gateway_application_budgets,
        repository.usage_records,
        repository.gateway_application_model_policies,
    ) == protected


def test_application_organization_save_preserves_tags_and_audits_only_changes() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=False)
    service.sync_discovery(_discovery("codex"), "owner@example.com")
    application_id = application_id_for(APIM_ID, "codex")
    repository.gateway_applications[0]["tags"] = {"environment": "test"}
    request = GatewayApplicationOrganizationUpdate(organization_id="org-contoso-global")
    assigned = service.update_application_organization(application_id, request, "owner@example.com")
    assert assigned.organization_name == "Contoso Global"
    assert repository.gateway_applications[0]["tags"] == {
        "environment": "test",
        "organization_id": "org-contoso-global",
    }
    audit_count = len(repository.gateway_application_audit)
    service.update_application_organization(application_id, request, "owner@example.com")
    assert len(repository.gateway_application_audit) == audit_count
    with pytest.raises(ValueError, match="Unknown application organization"):
        service.update_application_organization(
            application_id,
            GatewayApplicationOrganizationUpdate(organization_id="unknown"),
            "owner@example.com",
        )
    assert len(repository.gateway_application_audit) == audit_count
    service.update_application_organization(
        application_id,
        GatewayApplicationOrganizationUpdate(),
        "owner@example.com",
    )
    assert repository.gateway_applications[0]["tags"] == {"environment": "test"}


@pytest.mark.parametrize("projection_fails", [False, True])
def test_application_organization_projection_reports_committed_assignment(
    projection_fails: bool,
) -> None:
    repository = InMemoryRepository()
    projector = MagicMock(return_value=1)
    if projection_fails:
        projector.side_effect = RuntimeError("unit projection unavailable")
    service = ApplicationAccessService(
        repository,
        sync_available=False,
        project_organization=projector,
    )
    service.sync_discovery(_discovery("codex"), "owner@example.com")
    application_id = application_id_for(APIM_ID, "codex")
    result = service.update_application_organization(
        application_id,
        GatewayApplicationOrganizationUpdate(organization_id="org-contoso-global"),
        "owner@example.com",
    )
    assert result.projected is not projection_fails
    assert repository.gateway_applications[0]["tags"]["organization_id"] == "org-contoso-global"
    projector.assert_called_once_with(application_id)


def test_application_organization_sql_preserves_other_tags_and_records_before_after() -> None:
    application_id = UUID(int=99)
    connection = MagicMock()
    application_cursor = MagicMock()
    application_cursor.fetchone.return_value = {"id": application_id}
    tags_cursor = MagicMock()
    tags_cursor.fetchone.return_value = {"tags": {"environment": "test"}}
    connection.execute.side_effect = [
        MagicMock(**{"fetchone.return_value": {"present": None}}),
        application_cursor,
        tags_cursor,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlApplicationRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        result = repository.update_gateway_application_organization(
            application_id,
            "org-contoso-global",
            "owner",
        )
    assert result == {
        "organization_id": "org-contoso-global",
        "department_id": None,
        "owner_id": None,
    }
    statements = [call.args[0] for call in connection.execute.call_args_list]
    assert "FOR UPDATE" in statements[1] and "FOR UPDATE" in statements[2]
    tag_values = connection.execute.call_args_list[3].args[1]
    assert tag_values[0] == application_id
    assert tag_values[1].obj == {"environment": "test", "organization_id": "org-contoso-global"}
    audit_values = connection.execute.call_args_list[5].args[1]
    assert audit_values[1].obj == {"organization_id": None, "department_id": None, "owner_id": None}
    assert audit_values[2].obj == {
        "organization_id": "org-contoso-global",
        "department_id": None,
        "owner_id": None,
    }
    assert all(
        "token_usage" not in statement and "gateway_application_budget" not in statement
        for statement in statements
    )


def test_application_department_save_read_clear_and_preserve_legacy_organization_call() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=False)
    service.sync_discovery(_discovery("department-owner"), "owner@example.com")
    application_id = application_id_for(APIM_ID, "department-owner")
    repository.gateway_applications[0]["tags"] = {"environment": "test"}
    budgets = dict(repository.gateway_application_budgets)
    assigned = GatewayApplicationOrganizationUpdate(
        organization_id="org-contoso-global",
        department_id="department-platform",
    )
    result = service.update_application_organization(application_id, assigned, "owner@example.com")
    assert result.department_id == "department-platform"
    detail = service.application(application_id)
    assert detail.department_id == result.department_id
    assert detail.department_name == result.department_name
    assert detail.department_assignment_supported is True
    audit_count = len(repository.gateway_application_audit)
    service.update_application_organization(application_id, assigned, "owner@example.com")
    assert len(repository.gateway_application_audit) == audit_count
    preserved = service.update_application_organization(
        application_id,
        GatewayApplicationOrganizationUpdate(organization_id="org-contoso-global"),
        "owner@example.com",
    )
    assert preserved.department_id == "department-platform"
    cleared = service.update_application_organization(
        application_id,
        GatewayApplicationOrganizationUpdate(
            organization_id="org-contoso-global",
            department_id=None,
        ),
        "owner@example.com",
    )
    assert cleared.department_id is None and cleared.organization_id == "org-contoso-global"
    service.update_application_organization(application_id, assigned, "owner@example.com")
    service.update_application_organization(
        application_id,
        GatewayApplicationOrganizationUpdate(),
        "owner@example.com",
    )
    assert service.application(application_id).department_id is None
    assert repository.gateway_applications[0]["tags"] == {"environment": "test"}
    assert repository.gateway_application_budgets == budgets


@pytest.mark.parametrize("changed", [False, True])
def test_application_department_sql_writes_column_and_audit_in_one_transaction(
    changed: bool,
) -> None:
    application_id = UUID(int=100)
    connection = MagicMock()
    application_cursor = MagicMock()
    application_cursor.fetchone.return_value = {
        "id": application_id,
        "department_id": None if changed else "department-platform",
    }
    tags_cursor = MagicMock()
    tags_cursor.fetchone.return_value = {
        "tags": {"environment": "test", "organization_id": "org-contoso-global"},
    }
    connection.execute.side_effect = [
        MagicMock(**{"fetchone.return_value": {"present": None}}),
        application_cursor,
        tags_cursor,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlApplicationRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        result = repository.update_gateway_application_organization(
            application_id,
            "org-contoso-global",
            "owner",
            department_id="department-platform",
            update_department=True,
        )
    assert result == {
        "organization_id": "org-contoso-global",
        "department_id": "department-platform",
        "owner_id": None,
    }
    connection.transaction.assert_called_once()
    if not changed:
        assert connection.execute.call_count == 3
        return
    column_statement, column_values = connection.execute.call_args_list[4].args
    assert "SET department_id = %s" in column_statement
    assert column_values == ("department-platform", None, "owner", application_id)
    before = connection.execute.call_args_list[5].args[1][1].obj
    after = connection.execute.call_args_list[5].args[1][2].obj
    assert before == {
        "organization_id": "org-contoso-global",
        "department_id": None,
        "owner_id": None,
    }
    assert after == {
        "organization_id": "org-contoso-global",
        "department_id": "department-platform",
        "owner_id": None,
    }


def test_application_person_sql_updates_management_owner_and_audits_no_consumption() -> None:
    application_id = UUID(int=101)
    connection = MagicMock()
    application_cursor = MagicMock()
    application_cursor.fetchone.return_value = {
        "id": application_id,
        "department_id": "department-platform",
        "owner_id": None,
    }
    tags_cursor = MagicMock()
    tags_cursor.fetchone.return_value = {"tags": {"organization_id": "org-contoso-global"}}
    connection.execute.side_effect = [
        MagicMock(**{"fetchone.return_value": {"present": None}}),
        application_cursor,
        tags_cursor,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlApplicationRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        result = repository.update_gateway_application_organization(
            application_id,
            "org-contoso-global",
            "owner",
            department_id="department-platform",
            update_department=True,
            owner_id="test.user01@contoso.com",
            update_owner=True,
        )
    assert result is not None and result["owner_id"] == "test.user01@contoso.com"
    connection.transaction.assert_called_once()
    statement, values = connection.execute.call_args_list[4].args
    assert "owner_id = %s" in statement
    assert values == ("department-platform", "test.user01@contoso.com", "owner", application_id)
    audit = connection.execute.call_args_list[5].args[1]
    assert audit[1].obj["owner_id"] is None
    assert audit[2].obj["owner_id"] == "test.user01@contoso.com"
    assert all(
        not any(
            name in call.args[0]
            for name in (
                "token_usage",
                "token_budget",
                "gateway_application_budget",
                "budget_execution_attempt",
            )
        )
        for call in connection.execute.call_args_list
    )


@pytest.mark.parametrize("mode", ["restricted", "unrestricted"])
@pytest.mark.parametrize("projection_fails", [False, True])
def test_application_model_access_projects_committed_policy(
    mode: Literal["restricted", "unrestricted"],
    projection_fails: bool,
) -> None:
    repository = InMemoryRepository()
    application_id = application_id_for(APIM_ID, "unit-model-access")
    calls: list[UUID] = []

    def project(identifier: UUID) -> int:
        calls.append(identifier)
        assert identifier == application_id
        assert (identifier in repository.gateway_application_model_policies) == (
            mode == "restricted"
        )
        if projection_fails:
            raise RuntimeError("unit projection unavailable")
        return 1

    service = ApplicationAccessService(
        repository,
        sync_available=False,
        project_model_access=project,
    )
    service.sync_discovery(_discovery("unit-model-access"), "worker")
    before_budgets = dict(repository.gateway_application_budgets)
    model = next(row for row in repository.models if row["enabled"])
    repository.update_gateway_application_model_access(
        application_id,
        "restricted",
        [model["id"]],
        "owner@example.com",
    )
    result = service.update_application_model_access(
        application_id,
        GatewayApplicationModelAccessUpdate(mode=mode, model_ids=[]),
        "owner@example.com",
    )
    assert calls == [application_id]
    assert result.model_policy_configured == (mode == "restricted")
    assert result.allowed_model_ids == []
    assert repository.gateway_application_budgets == before_budgets


@pytest.mark.parametrize("mode", ["restricted", "unrestricted"])
def test_application_model_access_save_projects_only_own_application(
    mode: Literal["restricted", "unrestricted"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.http.service_dependencies import application_access_service
    from turnstile_core.integrations.ledger import application_partition_key

    repository = InMemoryRepository()
    access = ApplicationAccessService(repository, sync_available=False)
    access.sync_discovery(_discovery("unit-model-access", "neighbor"), "worker")
    application_id = application_id_for(APIM_ID, "unit-model-access")
    model = next(row for row in repository.models if row["enabled"])
    repository.update_gateway_application_model_access(
        application_id,
        "restricted",
        [model["id"]],
        "owner@example.com",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    settings = Settings(
        ledger_sync_enabled=True,
        ledger_table_endpoint="https://unit.table.core.windows.net",
    )
    with (
        patch("backend.http.service_dependencies.get_settings", return_value=settings),
        patch(
            "backend.http.service_dependencies.TableStorageLedger",
        ) as store_factory,
    ):
        store = store_factory.return_value.__enter__.return_value
        service = application_access_service(repository)
        result = service.update_application_model_access(
            application_id,
            GatewayApplicationModelAccessUpdate(mode=mode, model_ids=[]),
            "owner@example.com",
        )
        store.upsert.assert_called_once_with(
            application_partition_key(application_id, datetime.now(UTC).strftime("%Y-%m")),
            "M",
            {"Configured": mode == "restricted", "Models": "||"},
        )
    assert result.model_policy_configured == (mode == "restricted")


def test_application_avatar_accepts_bounded_supported_image() -> None:
    value = GatewayApplicationAvatarUpdate(avatar_data_url="data:image/png;base64,iVBORw0KGgo=")

    assert decode_application_avatar_data_url(value.avatar_data_url or "") == (
        "image/png",
        b"\x89PNG\r\n\x1a\n",
    )


def test_application_avatar_rejects_mismatched_image_type() -> None:
    with pytest.raises(ValueError, match="declared media type"):
        GatewayApplicationAvatarUpdate(avatar_data_url="data:image/jpeg;base64,iVBORw0KGgo=")


def _discovery(
    *subscription_ids: str,
    discovered_at: datetime | None = None,
) -> GatewayApplicationDiscovery:
    return GatewayApplicationDiscovery(
        gateway_profile_id=APIM_ID,
        discovered_at=discovered_at or datetime(2026, 8, 26, 2, tzinfo=UTC),
        items=[
            GatewayApplicationDiscoveryItem(
                apim_subscription_id=subscription_id,
                display_name=subscription_id.replace("-", " ").title(),
                state="active",
                scope_type="product",
                scope_id="finops-applications",
                scope_exists=True,
                application_type=(
                    "system"
                    if subscription_id in {"master", "turnstile-publisher-probe"}
                    else "service"
                ),
                system_managed=subscription_id in {"master", "turnstile-publisher-probe"},
            )
            for subscription_id in subscription_ids
        ],
    )


def test_owner_subscription_create_normalizes_human_fields() -> None:
    request = GatewayApplicationSubscriptionCreate(
        subscription_id="invoice-assistant",
        display_name="  Invoice Assistant  ",
        description="   ",
        application_type="agent",
    )

    assert request.display_name == "Invoice Assistant"
    assert request.description is None
    assert request.application_type == "agent"


@pytest.mark.parametrize("projection_failure", [False, True])
def test_application_budget_save_projects_tpm_without_resetting_monthly_budget(
    projection_failure: bool,
) -> None:
    repository = InMemoryRepository()
    projected = 100_000
    calls: list[UUID] = []

    def project(application_id: UUID) -> int:
        nonlocal projected
        calls.append(application_id)
        stored = repository.list_gateway_application_budgets(
            datetime.now(UTC).date().replace(day=1),
            [application_id],
        )[0]
        assert stored["tokens_per_minute"] == 1_000_000
        assert stored["token_limit"] == 20_000_000
        if projection_failure:
            raise RuntimeError("unit projection unavailable")
        projected = int(stored["tokens_per_minute"])
        return projected

    service = ApplicationAccessService(
        repository,
        sync_available=True,
        project_rate_limit=project,
        read_rate_limit=lambda _application: projected,
    )
    row = service.sync_discovery(
        _discovery("unit-rate-limit", discovered_at=datetime.now(UTC)),
        "worker",
    )[0]
    application_id = UUID(str(row["id"]))
    result = service.update_application_budget(
        application_id,
        GatewayApplicationBudgetUpdate(
            token_limit=20_000_000,
            tokens_per_minute=1_000_000,
            enforce=True,
            warning_threshold_percent=80,
        ),
        "owner@example.com",
    )
    assert calls == [application_id]
    assert result.budget is not None
    assert result.budget.token_limit == 20_000_000
    assert result.budget.tokens_per_minute == 1_000_000
    assert result.budget.projected_tokens_per_minute == (
        100_000 if projection_failure else 1_000_000
    )
    assert result.budget.tpm_sync_status == ("pending" if projection_failure else "projected")


def test_application_usd_budget_uses_exact_priced_usage_and_fails_closed_unpriced() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        read_execution_budget=lambda *_: {"ledger_status": "current"},
    )
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("usd-application", discovered_at=moment),
        "worker",
    )[0]
    application_id = UUID(str(application["id"]))
    subscription = repository.gateway_application_subscriptions[0]
    attribution = UsageApplicationAttribution(
        application_id=application_id,
        application_subscription_id=subscription["id"],
        application_name_snapshot=str(application["display_name"]),
        apim_subscription_id="usd-application",
        actor_type="service",
        actor_id="service:usd-application",
    )
    service.update_application_budget(
        application_id,
        GatewayApplicationBudgetUpdate(
            budget_mode="usd",
            usd_limit=Decimal("10.00"),
            tokens_per_minute=100_000,
            enforce=True,
            warning_threshold_percent=80,
        ),
        "owner",
    )
    repository.write_token_usage(
        _usage_record("usd-priced", "Foundry", 100).model_copy(
            update={"ts": moment, "usd_cost": Decimal("1.23456789012345")}
        ),
        attribution,
    )

    budget = service.application(application_id).budget
    assert budget is not None
    assert budget.budget_mode == "usd"
    assert budget.token_limit is None
    assert budget.usd_limit == Decimal("10.00")
    assert budget.used_usd == Decimal("1.23456789012345")
    assert budget.remaining_usd == Decimal("8.76543210987655")
    assert budget.usd_cost_available is True

    repository.write_token_usage(
        _usage_record("usd-unpriced", "Foundry", 10).model_copy(
            update={"ts": moment, "usd_cost": None}
        ),
        attribution.model_copy(update={"apim_subscription_id": "usd-application"}),
    )
    budget = service.application(application_id).budget
    assert budget is not None
    assert budget.used_usd is None
    assert budget.remaining_usd is None
    assert budget.usd_cost_available is False


@pytest.mark.parametrize("status", [None, "missing", "prepared", "unavailable"])
def test_usd_application_budget_cannot_disable_legacy_protection_without_atomic_scope(
    status: str | None,
) -> None:
    from turnstile_core.services.control_plane import ControlPlaneUnavailableError

    repository = InMemoryRepository()
    read = (
        None
        if status is None
        else lambda *_: None if status == "missing" else {"ledger_status": status}
    )
    service = ApplicationAccessService(repository, sync_available=True, read_execution_budget=read)
    application = service.sync_discovery(_discovery("usd-unready"), "worker")[0]
    application_id = UUID(str(application["id"]))
    with patch.object(repository, "update_gateway_application_budget") as update:
        with pytest.raises(ControlPlaneUnavailableError, match="atomic"):
            service.update_application_budget(
                application_id,
                GatewayApplicationBudgetUpdate(
                    budget_mode="usd",
                    usd_limit=Decimal("10"),
                    tokens_per_minute=100_000,
                    enforce=True,
                    warning_threshold_percent=80,
                ),
                "owner",
            )
        update.assert_not_called()


@pytest.mark.parametrize("condition", ["inactive", "foreign_epoch", "draining"])
def test_application_execution_read_rejects_retired_or_foreign_scope(condition: str) -> None:
    from unittest.mock import Mock

    from backend.http.service_dependencies import application_access_service
    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy

    epoch = UUID(int=99)
    repository = Mock(spec=PostgreSqlOpsDbProxy)
    repository.active_budget_execution_rollout.return_value = (
        None if condition == "inactive" else {"epoch": epoch}
    )
    repository.budget_execution_status.return_value = {
        "epoch": UUID(int=98) if condition == "foreign_epoch" else epoch,
        "state": "draining" if condition == "draining" else "active",
    }
    settings = Settings().model_copy(
        update={
            "budget_execution_enabled": True,
            "ledger_table_endpoint": "https://unit.table.core.windows.net",
        }
    )
    with (
        patch("backend.http.service_dependencies.get_settings", return_value=settings),
        patch("backend.http.service_dependencies.AtomicTableLedger") as table,
    ):
        service = application_access_service(repository)
        assert service._read_execution_budget is not None
        month = datetime.now(UTC).date().replace(day=1)
        assert service._read_execution_budget(UUID(int=100), month) is None
        table.assert_not_called()


def test_postgres_application_budget_write_persists_usd_mode_and_audit() -> None:
    application_id = UUID(int=910)
    budget = {
        "period_start": datetime.now(UTC).date().replace(day=1),
        "application_id": application_id,
        "budget_mode": "usd",
        "token_limit": None,
        "usd_limit": Decimal("12.50"),
        "tokens_per_minute": 100_000,
        "enforce": True,
        "warning_threshold_percent": 80,
        "updated_by": "owner",
        "updated_at": datetime.now(UTC),
    }
    connection = MagicMock()

    def cursor(row: dict[str, object] | None = None) -> MagicMock:
        value = MagicMock()
        value.fetchone.return_value = row
        return value

    connection.execute.side_effect = [
        cursor({"id": application_id}),
        cursor(None),
        cursor(budget),
        cursor(None),
        cursor(None),
    ]
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlApplicationRepositoryMixin()
    with patch.object(repository, "_connection", return_value=context):
        result = repository.update_gateway_application_budget(
            application_id,
            cast(date, budget["period_start"]),
            {
                "budget_mode": "usd",
                "token_limit": None,
                "usd_limit": Decimal("12.50"),
                "tokens_per_minute": 100_000,
                "enforce": True,
                "warning_threshold_percent": 80,
            },
            "owner",
        )

    assert result == budget
    insert_sql, insert_values = connection.execute.call_args_list[2].args
    assert "budget_mode, token_limit, usd_limit" in insert_sql
    assert insert_values[2:5] == ("usd", None, Decimal("12.50"))
    audit_sql, audit_values = connection.execute.call_args_list[4].args
    assert "budget_updated" in audit_sql
    assert audit_values[1] is None
    assert audit_values[2].obj["budget"]["budget_mode"] == "usd"
    assert audit_values[2].obj["budget"]["usd_limit"] == "12.50"


@pytest.mark.parametrize("rate", [0, -1, 2_147_483_648, True, "100000", 1.5])
def test_application_tpm_write_requires_a_native_positive_int(rate: object) -> None:
    with pytest.raises(ValueError):
        GatewayApplicationBudgetUpdate.model_validate(
            {
                "token_limit": 100_000,
                "tokens_per_minute": rate,
                "enforce": True,
                "warning_threshold_percent": 80,
            }
        )


@pytest.mark.parametrize("rate", [0, -1, 2_147_483_648, True, 1.0, 1.5])
def test_application_tpm_defaults_and_snapshots_reject_invalid_rates(
    rate: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for model, values in (
        (
            GatewayApplicationProvisioningDefaults,
            {
                "monthly_token_limit": 100_000,
                "tokens_per_minute": rate,
            },
        ),
        (
            GatewayApplicationSubscriptionProvisionSpec,
            {
                "application_id": UUID(int=750),
                "application_subscription_id": UUID(int=751),
                "apim_subscription_id": "unit-tpm",
                "slug": "unit-tpm",
                "display_name": "Unit TPM",
                "scope_id": "unit-product",
                "initial_tokens_per_minute": rate,
            },
        ),
    ):
        with pytest.raises(ValueError):
            model.model_validate(values)
    with pytest.raises(ValueError):
        TypeAdapter(TokensPerMinute).validate_python(rate)
    with pytest.raises(ValueError):
        Settings.model_validate({"gateway_application_default_tokens_per_minute": rate})
    monkeypatch.setenv("GATEWAY_APPLICATION_DEFAULT_TOKENS_PER_MINUTE", str(rate))
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize("rate", [1, 200, 24_000, MAX_TOKENS_PER_MINUTE])
def test_application_tpm_default_environment_and_domain_share_native_bounds(
    rate: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("GATEWAY_APPLICATION_DEFAULT_TOKENS_PER_MINUTE", str(rate))
    settings = Settings()
    assert settings.gateway_application_default_tokens_per_minute == rate
    assert TypeAdapter(TokensPerMinute).validate_python(rate) == rate
    with pytest.raises(ValueError):
        TypeAdapter(TokensPerMinute).validate_python(str(rate))
    assert (
        GatewayApplicationProvisioningDefaults(
            monthly_token_limit=1,
            tokens_per_minute=rate,
        ).tokens_per_minute
        == rate
    )


def test_legacy_out_of_range_tpm_remains_readable_and_can_be_corrected() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    row = service.sync_discovery(
        _discovery("legacy-tpm", discovered_at=datetime.now(UTC)),
        "worker",
    )[0]
    application_id = UUID(str(row["id"]))
    period = datetime.now(UTC).date().replace(day=1)
    repository.gateway_application_budgets[(period, application_id)]["tokens_per_minute"] = (
        MAX_TOKENS_PER_MINUTE + 1
    )
    before = service.application(application_id)
    assert before.budget is not None
    assert before.budget.tokens_per_minute == MAX_TOKENS_PER_MINUTE + 1
    assert before.budget.tpm_sync_status == "unavailable"
    corrected = service.update_application_budget(
        application_id,
        GatewayApplicationBudgetUpdate(
            token_limit=before.budget.token_limit,
            tokens_per_minute=24_000,
            enforce=before.budget.enforce,
            warning_threshold_percent=before.budget.warning_threshold_percent,
        ),
        "owner@example.com",
    )
    assert corrected.budget is not None and corrected.budget.tokens_per_minute == 24_000


def test_agent_provision_persists_specialized_application_type() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    spec = GatewayApplicationSubscriptionProvisionSpec(
        application_id=application_id_for(APIM_ID, "invoice-assistant"),
        application_subscription_id=application_subscription_id_for(APIM_ID, "invoice-assistant"),
        apim_subscription_id="invoice-assistant",
        slug="invoice-assistant",
        display_name="Invoice Assistant",
        application_type="agent",
        scope_id="finops-applications",
    )

    application = service.provision(APIM_ID, spec, "owner@example.com")

    assert application["application_type"] == "agent"
    assert (
        repository.gateway_application_ledger_mappings()[0]["application_slug"]
        == "invoice-assistant"
    )

    service.sync_discovery(_discovery("invoice-assistant"), "worker")

    assert service.applications().items[0].application_type == "agent"


def test_application_discovery_is_idempotent_and_preserves_budget() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)

    first = service.sync_discovery(_discovery("turnstile-dashboard", "master"), "worker-a")
    dashboard_id = UUID(str(first[0]["id"]))
    first_updated_at = first[0]["updated_at"]
    repository.gateway_application_budgets[(datetime(2026, 8, 1).date(), dashboard_id)][
        "token_limit"
    ] = 750_000

    second = service.sync_discovery(_discovery("turnstile-dashboard", "master"), "worker-b")

    assert second[0]["updated_at"] == first_updated_at
    assert len(repository.gateway_application_audit) == 2
    assert (
        repository.gateway_application_budgets[(datetime(2026, 8, 1).date(), dashboard_id)][
            "token_limit"
        ]
        == 750_000
    )
    master = next(item for item in second if item["slug"] == "master")
    master_id = UUID(str(master["id"]))
    assert master["system_managed"] is True
    assert (
        repository.gateway_application_budgets[(datetime(2026, 8, 1).date(), master_id)]["enforce"]
        is False
    )


def test_missing_subscription_is_marked_cancelled_and_stale() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    service.sync_discovery(_discovery("outline-assistant"), "worker-a")

    service.sync_discovery(_discovery(), "worker-b")

    subscription = repository.gateway_application_subscriptions[0]
    assert subscription["state"] == "cancelled"
    assert subscription["scope_exists"] is False
    application = service.applications().items[0]
    assert application.stale_subscription_count == 1
    assert application.active_subscription_count == 0


def test_application_ledger_sql_initializes_retained_finalization_placeholders() -> None:
    moment = datetime.now(UTC)
    state = GatewayApplicationLedgerState(
        period_start=moment.date().replace(day=1),
        application_id=UUID(int=99),
        token_limit=100_000,
        confirmed_tokens=125,
        pending_reserved_tokens=0,
        pending_reservation_count=0,
        stale_reservation_count=0,
        oldest_reservation_at=None,
        available_tokens=99_875,
        snapshot_at=moment,
    )
    connection = MagicMock()
    repository = PostgreSqlApplicationRepositoryMixin()

    repository._write_application_ledger_states(connection, [state])

    cursor = connection.cursor.return_value.__enter__.return_value
    statement, records = cursor.executemany.call_args.args
    normalized = " ".join(statement.split())
    assert "finalized_upper_bound_tokens, finalized_upper_bound_count," in normalized
    assert "%(pending_reservation_count)s, 0, 0," in normalized
    assert records == [state.model_dump(mode="python")]


def test_usage_application_attribution_is_immutable_and_drives_ledger() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("outline-assistant", discovered_at=moment), "worker-a"
    )[0]
    application_id = UUID(str(application["id"]))
    application_name = str(application["display_name"])
    subscription = repository.gateway_application_subscriptions[0]
    attribution = UsageApplicationAttribution(
        application_id=application_id,
        application_subscription_id=subscription["id"],
        application_name_snapshot=application_name,
        apim_subscription_id="outline-assistant",
        actor_type="service",
        actor_id="service:outline-assistant",
    )
    record = _usage_record("application-usage", "Microsoft Foundry via APIM", 125).model_copy(
        update={"ts": moment}
    )

    repository.write_token_usage(record, attribution)
    repository.write_token_usage(
        record,
        attribution.model_copy(update={"application_admission": "confirmed"}),
    )
    period_start = moment.date().replace(day=1)
    repository.save_gateway_application_ledger_states(
        [
            GatewayApplicationLedgerState(
                period_start=period_start,
                application_id=application_id,
                token_limit=100_000,
                confirmed_tokens=125,
                pending_reserved_tokens=875,
                pending_reservation_count=2,
                stale_reservation_count=1,
                oldest_reservation_at=moment - timedelta(minutes=20),
                available_tokens=99_000,
                snapshot_at=moment,
            )
        ]
    )
    repository.save_gateway_application_ledger_finalization_states(
        [
            {
                "period_start": period_start,
                "application_id": application_id,
                "finalized_upper_bound_tokens": 125,
                "finalized_upper_bound_count": 1,
                "snapshot_at": moment,
            }
        ]
    )

    stored = repository.usage_application_attributions[record.id]
    assert stored.application_admission == "confirmed"
    view = service.applications().items[0]
    assert view.usage.total_tokens == 125
    assert view.budget is not None
    assert view.budget.remaining_tokens == 99_875
    assert view.budget.pending_reserved_tokens == 875
    assert view.budget.finalized_upper_bound_tokens == 125
    assert view.budget.finalized_upper_bound_count == 1
    assert view.budget.pending_reservation_count == 2
    assert view.budget.stale_reservation_count == 1
    assert view.budget.available_tokens == 98_875
    assert view.budget.ledger_snapshot_at == moment
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    assert (
        repository.gateway_application_ledger_snapshot(period_start, period_end)[0][
            "confirmed_tokens"
        ]
        == 125
    )

    with pytest.raises(ValueError, match="immutable"):
        repository.write_token_usage(
            record,
            attribution.model_copy(update={"actor_id": "service:other"}),
        )


def test_application_ledger_state_rejects_an_older_timer_snapshot() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("outline-assistant", discovered_at=moment), "worker"
    )[0]
    period_start = moment.date().replace(day=1)
    application_id = UUID(str(application["id"]))

    def state(snapshot_at: datetime, available_tokens: int) -> GatewayApplicationLedgerState:
        return GatewayApplicationLedgerState(
            period_start=period_start,
            application_id=application_id,
            token_limit=100_000,
            confirmed_tokens=100_000 - available_tokens,
            pending_reserved_tokens=0,
            pending_reservation_count=0,
            stale_reservation_count=0,
            oldest_reservation_at=None,
            available_tokens=available_tokens,
            snapshot_at=snapshot_at,
        )

    repository.save_gateway_application_ledger_states([state(moment, 90_000)])
    repository.save_gateway_application_ledger_states(
        [state(moment - timedelta(minutes=5), 95_000)]
    )

    budget = service.application(application_id).budget
    assert budget is not None
    assert budget.available_tokens == 90_000


def test_older_finalization_sidecar_is_ignored_after_rollback_function_snapshot() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("rollback-compatible", discovered_at=moment), "worker"
    )[0]
    application_id = UUID(str(application["id"]))
    period_start = moment.date().replace(day=1)
    repository.save_gateway_application_ledger_finalization_states(
        [
            {
                "period_start": period_start,
                "application_id": application_id,
                "finalized_upper_bound_tokens": 5_000,
                "finalized_upper_bound_count": 1,
                "snapshot_at": moment,
            }
        ]
    )
    repository.save_gateway_application_ledger_states(
        [
            GatewayApplicationLedgerState(
                period_start=period_start,
                application_id=application_id,
                token_limit=100_000,
                confirmed_tokens=1_000,
                pending_reserved_tokens=5_000,
                pending_reservation_count=1,
                stale_reservation_count=1,
                oldest_reservation_at=moment,
                available_tokens=94_000,
                snapshot_at=moment + timedelta(minutes=5),
            )
        ]
    )

    budget = service.application(application_id).budget
    assert budget is not None
    assert budget.finalized_upper_bound_tokens == 0
    assert budget.finalized_upper_bound_count == 0
    assert budget.pending_reserved_tokens == 5_000
    assert budget.available_tokens == 94_000


def test_application_detail_aggregates_people_and_service_users() -> None:
    repository = InMemoryRepository()
    service = ApplicationAccessService(repository, sync_available=True)
    moment = datetime.now(UTC)
    application = service.sync_discovery(
        _discovery("outline-assistant", discovered_at=moment), "worker-a"
    )[0]
    application_id = UUID(str(application["id"]))
    subscription = repository.gateway_application_subscriptions[0]
    person = UsageApplicationAttribution(
        application_id=application_id,
        application_subscription_id=subscription["id"],
        application_name_snapshot=str(application["display_name"]),
        apim_subscription_id="outline-assistant",
        actor_type="person",
        actor_id="alice@contoso.com",
        person_id="person-alice",
    )
    service_actor = person.model_copy(
        update={
            "actor_type": "service",
            "actor_id": "service:outline-assistant",
            "person_id": None,
        }
    )
    for usage_id, tokens, status_code, attribution in (
        ("alice-1", 80, 200, person),
        ("alice-2", 20, 403, person),
        ("service-1", 30, 200, service_actor),
    ):
        record = _usage_record(
            usage_id, "Microsoft Foundry via APIM", tokens, status_code=status_code
        ).model_copy(
            update={
                "ts": moment,
                "user": "Alice" if attribution.actor_type == "person" else "Service",
                "user_id": attribution.person_id or "unattributed",
            }
        )
        repository.write_token_usage(record, attribution)

    detail = service.application(application_id)

    assert detail.user_count == 2
    assert [item.user_id for item in detail.users] == [
        "person-alice",
        "service:outline-assistant",
    ]
    alice = detail.users[0]
    assert alice.display_name == "Alice"
    assert alice.actor_type == "person"
    assert alice.request_count == 2
    assert alice.denied_request_count == 1
    assert alice.total_tokens == 100
    assert detail.users[1].actor_type == "service"


def test_application_audit_state_is_json_serializable() -> None:
    value = {
        "id": uuid4(),
        "at": datetime(2026, 8, 26, 2, tzinfo=UTC),
        "nested": {"ids": [uuid4()]},
    }

    encoded = _json_value(value)

    assert json.loads(json.dumps(encoded))["at"] == "2026-08-26T02:00:00Z"


def test_apim_subscription_discovery_uses_list_without_secrets() -> None:
    requests: list[httpx.Request] = []
    root = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-finops/providers/Microsoft.ApiManagement/"
        "service/apim-finops"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/apis"):
            return httpx.Response(
                200,
                json={"value": [{"id": f"{root}/apis/turnstile-llm"}]},
            )
        if request.url.path.endswith("/products"):
            return httpx.Response(
                200,
                json={"value": [{"id": f"{root}/products/finops-applications"}]},
            )
        if request.url.path.endswith("/subscriptions"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": f"{root}/subscriptions/outline-assistant",
                            "properties": {
                                "displayName": "Outline Assistant",
                                "scope": f"{root}/products/finops-applications",
                                "state": "active",
                                "primaryKey": "must-be-ignored",
                                "secondaryKey": "must-be-ignored",
                            },
                        },
                        {
                            "id": f"{root}/subscriptions/master",
                            "properties": {
                                "displayName": "Built-in all-access",
                                "scope": f"{root}/apis",
                                "state": "active",
                            },
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    settings = publisher_settings()
    settings.apim_subscription_agent_map = {
        "outline-assistant": {
            "id": "agent-outline-assistant",
            "name": "Outline Assistant",
        }
    }
    client = AzureApimPublisherClient(
        settings,
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    discovered = client.discover_gateway_applications(APIM_ID)

    assert {item.apim_subscription_id for item in discovered.items} == {
        "master",
        "outline-assistant",
    }
    master = next(item for item in discovered.items if item.apim_subscription_id == "master")
    assistant = next(
        item for item in discovered.items if item.apim_subscription_id == "outline-assistant"
    )
    assert master.scope_type == "service"
    assert assistant.application_type == "agent"
    assert all(request.method == "GET" for request in requests)
    assert all("listSecrets" not in str(request.url) for request in requests)
    assert "must-be-ignored" not in discovered.model_dump_json()


def test_apim_subscription_provision_uses_put_without_list_secrets() -> None:
    requests: list[httpx.Request] = []
    root = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-finops/providers/Microsoft.ApiManagement/"
        "service/apim-finops"
    )
    spec = GatewayApplicationSubscriptionProvisionSpec(
        application_id=uuid4(),
        application_subscription_id=uuid4(),
        apim_subscription_id="owner-created-app",
        slug="owner-created-app",
        display_name="Owner Created App",
        scope_id="turnstile-ai-consumers",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/products/turnstile-ai-consumers"):
            return httpx.Response(200, json={"id": f"{root}/products/turnstile-ai-consumers"})
        if request.url.path.endswith("/subscriptions/owner-created-app"):
            if request.method == "GET":
                return httpx.Response(404)
            body = json.loads(request.content)
            assert body["properties"]["primaryKey"] == "primary-test-key"
            assert body["properties"]["secondaryKey"] == "secondary-test-key"
            return httpx.Response(
                201,
                json={
                    "id": f"{root}/subscriptions/owner-created-app",
                    "properties": {
                        "displayName": "Owner Created App",
                        "scope": f"{root}/products/turnstile-ai-consumers",
                        "state": "active",
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = AzureApimPublisherClient(
        publisher_settings(),
        StubTokenProvider(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.ensure_application_subscription(spec, "primary-test-key", "secondary-test-key")

    assert [request.method for request in requests] == ["GET", "GET", "PUT"]
    assert all("listSecrets" not in str(request.url) for request in requests)


def test_apim_subscription_keys_can_be_revealed_and_rotated() -> None:
    requests: list[httpx.Request] = []
    primary_key = "primary-before-rotation"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_key
        requests.append(request)
        assert request.method == "POST"
        assert request.url.params["api-version"] == "2024-05-01"
        if request.url.path.endswith("/regeneratePrimaryKey"):
            primary_key = "primary-after-rotation"
            return httpx.Response(204)
        if request.url.path.endswith("/listSecrets"):
            return httpx.Response(
                200,
                json={
                    "primaryKey": primary_key,
                    "secondaryKey": "secondary-key",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = AzureApimSubscriptionKeyClient(
        publisher_settings(),
        StubTokenProvider(),  # type: ignore[arg-type]
        httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.application_subscription_keys("owner app") == (
        "primary-before-rotation",
        "secondary-key",
    )
    client.regenerate_application_subscription_key("owner app", "primary")
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "listSecrets",
        "regeneratePrimaryKey",
    ]
    assert all("owner%20app" in str(request.url) for request in requests)


def test_application_usd_dtos_tags_and_registered_organization() -> None:
    update = GatewayApplicationBudgetUpdate(
        budget_mode="usd",
        usd_limit=Decimal("10.25"),
        tokens_per_minute=1000,
        enforce=True,
        warning_threshold_percent=80,
    )
    assert update.token_limit is None and update.usd_limit == Decimal("10.25")
    request = GatewayApplicationSubscriptionCreate(
        subscription_id="unit-usd",
        display_name="Unit",
        budget_mode="usd",
        usd_limit=Decimal("10.25"),
        tags={"environment": "test"},
    )
    assert request.tags == {"environment": "test"}
    assert application_organization({}).organization_id is None
    unresolved = GatewayApplicationSubscriptionCreate(
        subscription_id="unit",
        display_name="Unit",
        tags={"organization_id": "not-a-directory-id"},
    )
    with pytest.raises(ValueError, match="Unknown application organization"):
        application_organization(unresolved.tags)
    for invalid in [{"": "value"}, {"key": "x" * 257}, {str(index): "v" for index in range(33)}]:
        with pytest.raises(ValueError):
            GatewayApplicationSubscriptionCreate(
                subscription_id="unit", display_name="Unit", tags=invalid
            )


def test_application_usd_snapshot_retains_exact_amount_and_public_token_upper_bounds() -> None:
    values = {
        "period_start": datetime(2026, 10, 1, tzinfo=UTC).date(),
        "application_id": UUID(int=1),
        "confirmed_tokens": 100,
        "pending_reserved_tokens": 50,
        "pending_reservation_count": 1,
        "stale_reservation_count": 0,
        "oldest_reservation_at": datetime(2026, 10, 1, tzinfo=UTC),
        "snapshot_at": datetime(2026, 10, 2, tzinfo=UTC),
    }
    usd = GatewayApplicationLedgerState.model_validate(
        {
            **values,
            "budget_mode": "usd",
            "usd_limit": "1.00",
            "confirmed_usd": "0.0001",
            "pending_reserved_usd": "0.25",
            "upper_bound_usd": "0.10",
            "available_tokens": 0,
        }
    )
    assert usd.available_usd == Decimal("0.6499")
    token = GatewayApplicationLedgerState.model_validate(
        {
            **values,
            "token_limit": 1000,
            "finalized_upper_bound_tokens": 20,
            "finalized_upper_bound_count": 1,
            "available_tokens": 830,
        }
    )
    assert token.available_tokens == 830 and token.available_usd is None
    with pytest.raises(ValueError, match="available tokens"):
        GatewayApplicationLedgerState.model_validate(
            {
                **token.model_dump(),
                "available_tokens": 850,
            }
        )


@pytest.mark.parametrize("state", ["current", "unavailable", "prepared", "legacy"])
def test_application_detail_uses_execution_balance_without_false_legacy_fallback(
    state: str,
) -> None:
    repository = InMemoryRepository()
    now = datetime.now(UTC)
    reader = Mock(
        return_value=(
            None
            if state == "legacy"
            else {
                "ledger_source": "execution",
                "ledger_status": state,
                "used_tokens": 125,
                "remaining_tokens": 99875,
                "usage_percent": 0.125,
                "pending_reserved_tokens": 75 if state == "current" else None,
                "finalized_upper_bound_tokens": 20 if state == "current" else None,
                "available_tokens": 99780 if state == "current" else None,
                "ledger_snapshot_at": now if state == "current" else None,
                "ledger_read_at": now if state == "current" else None,
                "ledger_version": 7 if state == "current" else None,
                "ledger_balance_tokens": -25 if state == "current" else None,
                "policy_pending": state == "current",
            }
        )
    )
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        read_execution_budget=reader,
    )
    service.sync_discovery(_discovery("unit-execution", discovered_at=now), "unit-worker")
    application_id = application_id_for(APIM_ID, "unit-execution")
    detail = service.application(application_id)
    reader.assert_called_once_with(application_id, now.date().replace(day=1))
    assert detail.budget is not None
    if state == "legacy":
        assert detail.budget.used_tokens == 0
        assert detail.budget.ledger_source == "snapshot"
        assert detail.budget.ledger_status == "snapshot"
    else:
        assert detail.budget.used_tokens == 125
        assert detail.budget.available_tokens == (99780 if state == "current" else None)
        assert detail.budget.ledger_snapshot_at == (now if state == "current" else None)
        assert detail.budget.ledger_source == "execution"
        assert detail.budget.ledger_status == state
        assert detail.budget.ledger_read_at == (now if state == "current" else None)
        assert detail.budget.ledger_version == (7 if state == "current" else None)
        assert detail.budget.ledger_balance_tokens == (-25 if state == "current" else None)
        assert detail.budget.policy_pending is (state == "current")


@pytest.mark.parametrize("projected", [250, 100, None])
def test_budget_save_projects_rate_and_returns_actual_readback(projected: int | None) -> None:
    repository = InMemoryRepository()
    projector = Mock(side_effect=RuntimeError("transport") if projected is None else None)
    reader = Mock(return_value=projected)
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        project_rate_limit=projector,
        read_rate_limit=reader,
    )
    service.sync_discovery(_discovery("unit-tpm", discovered_at=datetime.now(UTC)), "unit-worker")
    application_id = application_id_for(APIM_ID, "unit-tpm")
    result = service.update_application_budget(
        application_id,
        GatewayApplicationBudgetUpdate(
            token_limit=100000,
            tokens_per_minute=250,
            enforce=True,
            warning_threshold_percent=80,
        ),
        "unit-owner",
    )
    projector.assert_called_once_with(application_id)
    reader.assert_called_once_with(application_id)
    assert result.budget is not None
    assert result.budget.tokens_per_minute == 250
    assert result.budget.projected_tokens_per_minute == projected
    assert result.budget.tpm_sync_status == ("projected" if projected == 250 else "pending")


def test_application_keys_resolve_the_stored_target_without_default_fallback() -> None:
    repository = InMemoryRepository()
    resolver = Mock()
    client = resolver.resolve.return_value
    client.application_subscription_keys.return_value = ("unit-primary", "unit-secondary")
    service = ApplicationAccessService(
        repository,
        sync_available=True,
        key_management_available=True,
        key_client_resolver=resolver,
    )
    now = datetime.now(UTC)
    other_target = UUID(int=47)
    service.sync_discovery(
        _discovery("unit-key", discovered_at=now).model_copy(
            update={"gateway_profile_id": other_target},
        ),
        "unit-worker",
    )
    application_id = application_id_for(other_target, "unit-key")
    subscription_id = application_subscription_id_for(other_target, "unit-key")
    secret = service.reveal_application_subscription_key(application_id, subscription_id, "primary")
    assert secret.value == "unit-primary"
    resolver.resolve.assert_called_once_with(other_target)
    resolver.reset_mock()
    service.rotate_application_subscription_key(
        application_id,
        subscription_id,
        "secondary",
        GatewayApplicationSubscriptionKeyRotation(confirmation="unit-key"),
    )
    resolver.resolve.assert_called_once_with(other_target)
    client.regenerate_application_subscription_key.assert_called_once_with("unit-key", "secondary")
