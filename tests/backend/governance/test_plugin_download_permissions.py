from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.http.permission_management import save_permission_configuration
from backend.services.client_download_permissions import require_plugin_download
from tests.backend.model_platform.test_client_user_service import client_service
from turnstile_core.config import Settings
from turnstile_core.domain.data_access import DataAccessScope
from turnstile_core.domain.menu_permissions import (
    DOWNLOAD_GROUPS,
    MENU_GROUPS,
    PermissionPolicyWrite,
    download_permissions,
)


def test_download_defaults_owner_and_multi_group_union_are_separate_from_menus() -> None:
    assert download_permissions() == ("user-plugin",)
    denied: dict[str, list[str]] = {group: [] for group in DOWNLOAD_GROUPS}
    assert download_permissions(policy=denied) == ()
    assert download_permissions(owner=True, policy=denied) == (
        "user-plugin", "subscription-plugin",
    )
    denied["team_admin"] = ["subscription-plugin"]
    assert download_permissions(("user", "team_admin"), policy=denied) == (
        "subscription-plugin",
    )


@pytest.mark.parametrize("change", ["missing-role", "duplicate", "unknown", "unknown-role"])
def test_download_write_rejects_ambiguous_or_unknown_capabilities(change: str) -> None:
    downloads: dict[str, list[str]] = {
        group: list(values) for group, values in DOWNLOAD_GROUPS.items()
    }
    if change == "missing-role":
        downloads.pop("user")
    elif change == "duplicate":
        downloads["user"] = ["user-plugin", "user-plugin"]
    elif change == "unknown":
        downloads["user"] = ["settings"]
    else:
        downloads["owner"] = []
    with pytest.raises(ValidationError):
        PermissionPolicyWrite.model_validate({
            "expected_revision": 1, "groups": MENU_GROUPS, "download_groups": downloads,
        })


def test_disabled_user_download_cannot_issue_a_grant_and_does_not_revoke_runtime() -> None:
    service, _, owner = client_service()
    member = replace(owner, role="member", download_permissions=())
    issue = Mock()
    service.store.issue_download = issue  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as failure:
        service.package(member)
    assert failure.value.status_code == 403
    issue.assert_not_called()
    require_plugin_download(replace(owner, download_permissions=()), "subscription-plugin")


def test_directory_scoped_inactive_person_cannot_use_delegated_downloads() -> None:
    _, _, owner = client_service()
    member = replace(owner, role="member", download_permissions=("subscription-plugin",),
                     data_scope=DataAccessScope(), directory_person_active=False)
    with pytest.raises(HTTPException) as failure:
        require_plugin_download(member, "subscription-plugin")
    assert failure.value.status_code == 403


def test_legacy_authority_rejects_download_policy_writes_before_database_access() -> None:
    _, _, owner = client_service()
    write = PermissionPolicyWrite.model_validate({
        "expected_revision": 1, "groups": MENU_GROUPS, "download_groups": DOWNLOAD_GROUPS,
    })
    with pytest.raises(HTTPException) as failure:
        save_permission_configuration(write, owner, Settings(directory_source="legacy"))
    assert failure.value.status_code == 409
