import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_usage_scope_selection_and_cascade() -> None:
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("usage-filter-scope.test.mjs"))],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_usage_pages_share_accessible_multiselect_and_default_status() -> None:
    component = (FRONTEND_SOURCE / "components/finops/filter-menu-field.tsx").read_text()
    dashboard = (FRONTEND_SOURCE / "data-sources/apim/pages/dashboard-page.tsx").read_text()
    queries = (FRONTEND_SOURCE / "data-sources/apim/queries.ts").read_text()
    assert "<Select multiple" in component
    assert "aria-label={label}" in component and "SelectGroup" in component
    assert "DropdownMenuRadioItem" not in component
    assert "directory_status: usageArchiveFilter(" in dashboard
    assert "usageArchiveSelection(scope.directory_status)" in dashboard
    status_options = dashboard.split('label="归档状态"', 1)[1].split("onChange=", 1)[0]
    assert 'value: "active"' in status_options and 'value: "archived"' in status_options
    assert '"historical"' not in status_options and '"unattributed"' not in status_options
    source = (FRONTEND_SOURCE / "data-sources/apim/source.tsx").read_text()
    assert "directory_status: usageArchiveFilter(scope.directory_status)" in source
    assert dashboard.count("<MultiSelectFilterField") == 5
    assert "reconcileUsageScope" in dashboard
    assert "usageFacetWindow(filters)" in queries and "directoryStatus" in queries
    assert "selection(filters.department_id)" in queries
    assert '<span><span>信号</span></span>' in dashboard
    rules = (FRONTEND_SOURCE / "data-sources/apim/pages/anomaly-rule-management.tsx").read_text()
    assert '<span><span>规则</span></span>' in rules
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    assert 'queryKey: ["reference", "enterprise-query-entities"]' in directory
