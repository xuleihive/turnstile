import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_budget_directory_filtering_and_read_pagination() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("budget-directory.test.mjs"))],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_budget_directory_controls_preserve_budget_mutations_and_primary_department() -> None:
    source = (FRONTEND_SOURCE / "data-sources/apim/pages/budget-page.tsx").read_text()
    assert "const [hideArchived, setHideArchived] = useState(true)" in source
    assert "const [expanded, setExpanded] = useState<Set<string>>(new Set())" in source
    assert "if (!expanded.has(organization.scope_id)) continue" in source
    assert "aria-expanded={expanded}" in source and "aria-pressed={selected}" in source
    assert 'aria-label="人员预算团队"' in source
    assert (
        '<SelectItem key={row.id} value={row.id} data-no-localize>{row.name}</SelectItem>'
        in source
    )
    assert '<span className="people-name-heading"><span>人员</span></span>' in source
    assert '<span><span>范围</span></span>' in source
    assert 'aria-label="人员预算部门"' not in source
    assert "budgetScopeDepartments" in source and "budgetScopeTeams" in source
    assert "readDepartmentBudgetPeople" in source and "readTeamBudgetMembers" in source
    assert 'dataSource.peopleBudgets(period, id, "", "all", start, size)' in source
    assert "selectedBudgetDepartment" in source and "selectionTooLarge" in source
    assert "dataSource.saveBudget(period, item.scope_type, item.scope_id, value)" in source
    assert "dataSource.deleteBudget(period, item.scope_type, item.scope_id)" in source
    assert "dataSource.saveDepartmentEnforcement(period, departmentId, mode)" in source
    assert "dataSource.bulkSavePeopleBudgets(period, write)" in source
    editor = source.split("function BulkPeopleEditor", 1)[1].split(
        "function ModelAccessPicker", 1,
    )[0]
    assert "department_id: people.department_id" in editor
    assert 'allocation_mode: updateBudget ? mode : "preserve"' in editor
    assert 'user_ids: selection === "ids" ? selectedIds : []' in editor
