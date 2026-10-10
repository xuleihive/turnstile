from tests.support.paths import FRONTEND_SOURCE
from turnstile_core.domain.menu_permissions import ALL_MENUS, MENU_GROUPS


def test_all_page_ids_have_menu_definitions_and_personal_fallback_is_available() -> None:
    source = (FRONTEND_SOURCE / "app.tsx").read_text()
    ids = source.split("const pageIds: Page[] = [", 1)[1].split("];", 1)[0]
    import re

    assert set(re.findall(r'"([a-z-]+)"', ids)) == set(ALL_MENUS)
    for menus in MENU_GROUPS.values():
        assert {"user-settings", "finops-invoke"} <= set(menus)
        assert len(set(menus)) == len(menus)


def test_menu_gates_cover_routes_search_prefetch_and_floating_entry_points() -> None:
    source = (FRONTEND_SOURCE / "app.tsx").read_text()
    assert "normalizePageForAccess(selectedDataSource, requestedPage, user)" in source
    assert "normalizePageForAccess(nextSource, requestedPage, user)" in source
    assert "normalizePageForAccess(next, page, user)" in source
    assert "visibleNavGroups.map" in source
    assert ".filter((item) => canAccessMenu(user, item.id))" in source
    for page in ("assistant", "finops-governance"):
        assert f'canAccessMenu(user, "{page}")' in source
    assert '{ label: "报表中心", icon: FileSpreadsheet, page: "pinned-report" as Page }' in source
    assert "group.items.filter((item) => canAccessMenu(user, item.page))" in source
    assert 'if (!canAccessMenu(user, nextPage)) return' in source
    person = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    assert "菜单权限组" in person and 'label="岗位"' not in person
    assert "job_title:" not in person
    assert 'MenuSelect label="菜单权限组"' not in person
    assert "<MenuGroupTags groups={person.scope_menu_permission_groups" in person
    editor = person.split("function DirectoryEditor(", 1)[1].split(
        "function PersonActionDialog(", 1
    )[0]
    assert "const menuGroups = person?.scope_menu_permission_groups" in editor
    assert (
        "<MenuGroupTags groups={menuGroups} scopes={person?.scope_menu_administrator_scopes}"
        in editor
    )
    assert "setMenuGroups" not in editor
    assert "canAssignMenus" not in person
    assert "menu_permission_groups:" not in editor
    assert "menu_permission_group:" not in editor
    permission_field = editor.split("<FieldLabel>菜单权限组</FieldLabel>", 1)[1].split(
        "</Field>", 1
    )[0]
    assert "Checkbox" not in permission_field


def test_scope_admin_configuration_is_present_at_all_levels_without_parent_alias() -> None:
    person = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    assert 'value="administrators">管理员' in person
    assert 'kind={selectedUnit?.kind ?? "organization"}' in person
    assert "scope={selectedUnit ?? organization}" in person
    assert "capabilities?.department_capabilities?.[departmentId]" in person
    assert "capabilities?.capabilities.includes" not in person
    assert "directoryApi.menuAdministrators(kind, scope.id)" in person
    assert "directoryApi.setMenuAdministrators(kind, scope.id, value)" in person
    assert '...(kind === "team" ? { team_id: scope.id } : {})' in person
    assert "DepartmentAdministrators" not in person
    assert 'value="data-authorization"' not in person
