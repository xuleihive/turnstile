from tests.support.paths import FRONTEND_SOURCE


def source(path: str) -> str:
    return (FRONTEND_SOURCE / path).read_text(encoding="utf-8")


def test_user_settings_is_a_global_page_and_preserves_the_account_menu() -> None:
    app = source("app.tsx")
    assert 'if (page === "user-settings") return page;' in app
    assert 'navigatePage("user-settings")' in app
    assert "<UserSettingsPage onToggleSidebar={toggleSidebar}" in app
    assert app.index("<span>用户设置</span>") < app.index("<span>退出登录</span>")


def test_self_service_requests_do_not_accept_a_target_identity() -> None:
    api = source("api/user-settings.ts")
    assert "`/api/v1/user-settings/me/${path}`" in api
    assert 'credentials: "include", cache: "no-store"' in api
    assert "response.status === 401" in api
    assert "response.status === 204" in api
    assert "user_id" not in api and "target_email" not in api


def test_password_dialog_does_not_translate_or_retry_sensitive_values() -> None:
    profile = source("components/user-settings/profile-editor.tsx")
    assert "<Input data-no-localize id={field.id}" in profile
    assert '<Input data-no-localize id="display-name"' in profile
    assert "event.stopPropagation()" in profile
    assert "reason instanceof TypeError" in profile
    assert 'endSession("密码更新结果未确认，请重新登录。")' in profile
    assert 'endSession("密码已更新，请重新登录。")' in profile
    assert "localStorage" not in profile and "sessionStorage" not in profile


def test_avatar_source_and_identity_changes_are_isolated() -> None:
    auth = source("providers/auth-provider.tsx")
    assert 'user.method === "entra"' in auth
    assert "fetchEntraPhoto(user.email)" in auth
    assert "authApi.avatar(user.avatar_url)" in auth
    assert "activeUser.current?.id !== next.id" in auth
    assert "version !== identityVersion.current" in auth
    assert "cancelled = true" in auth


def test_personal_tables_and_charts_use_shared_components_without_portal_conflicts() -> None:
    page = source("pages/user-settings-page.tsx")
    assert "ResizableTable" in page and "<table" not in page
    assert 'modes={["bar", "line"]}' in page
    assert "<th><span>模型</span></th>" in page
    assert "<th key={label}><span>{label}</span></th>" in page
    assert "showHeatmap" not in page
    assert "finopsQueries" not in page
