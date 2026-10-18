from tests.support.paths import FRONTEND_SOURCE


def test_member_creation_and_selection_are_separate_and_retired_tabs_are_absent() -> None:
    source = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    assert '!selectedUnit && owner && <Button' in source
    assert 'selectedUnit && editPeople && <Button' in source
    assert 'selectedUnit?.kind === "department" && editTeams' in source
    assert 'available_unit_id: unit.id' in source
    assert 'directoryApi.addMembers(unit.id' in source
    assert "organization_id: organizationId, display_name: name, email, password" in source
    assert 'label="登录密码"' in source and 'autoComplete="new-password"' in source
    assert 'label="归属部门" value={primaryDepartmentId} required' in source
    assert "department_id: primaryDepartmentId" in source
    assert "添加成员" in source and ">添加人员<" not in source
    assert "required minLength={12}" in source
    assert 'label="人员标识"' not in source
    for value in ('value="transfers"', 'value="candidates"', 'action: "transfer"'):
        assert value not in source
    assert "DirectoryCandidatesPanel" not in source and "cancelTransfer" not in source
