from tests.support.paths import FRONTEND_SOURCE


def test_user_management_is_owner_only_in_navigation_and_route() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    auth = (FRONTEND_SOURCE / "providers" / "auth-provider.tsx").read_text(
        encoding="utf-8"
    )

    assert '"user-management"' in app
    assert 'user?.role === "owner"' in app
    assert "UserManagementPage" in app
    assert "安全审计" in app
    assert 'can("users.read")' in app
    assert 'can("security_audit.read")' in app
    assert "user.permissions?.includes(permission)" in auth


def test_user_management_calls_scoped_admin_apis_and_keeps_roles_read_only() -> None:
    source = (
        FRONTEND_SOURCE / "data-sources" / "apim" / "user-management.ts"
    ).read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages" / "user-management-page.tsx").read_text(
        encoding="utf-8"
    )

    for endpoint in (
        "/api/v1/admin/users",
        "/api/v1/admin/roles",
        "/api/v1/admin/organization-units",
    ):
        assert endpoint in source
    assert "initial_password" in source
    assert "requestBlob" not in source
    assert "method: \"DELETE\"" in source
    assert "角色权限为只读" in page
    assert "Entra" in page
    assert "初始密码" in page
    assert "组织部门" in page
    assert "停用" in page
    assert "ApiError" in page
    assert "isLoading" in page
    assert "isError" in page
    assert "isSuccess" in page
    assert "rootUnit?.id" in page
    assert "parent_id: parentId === NO_DEPARTMENT ? undefined : parentId" in page


def test_member_key_page_exposes_self_service_subscription_cancellation() -> None:
    page = (FRONTEND_SOURCE / "pages" / "my-api-keys-page.tsx").read_text(
        encoding="utf-8"
    )

    assert 'can("keys.manage.own")' in page
    assert "cancel.mutate(item)" in page
    assert "canManageOwn || canManageAll" in page
