import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_directory_visibility_and_selection_behavior() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("directory-visibility.test.mjs"))],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_directory_uses_two_managed_statuses_and_default_archive_filter() -> None:
    source = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    assert "const [hideArchived, setHideArchived] = useState(true)" in source
    assert 'aria-label="隐藏已归档"' in source
    assert "if (confirmDirectoryLeave()) setHideArchived" in source
    assert "visibleOrganizations.map" in source
    assert "visibleUnits.filter" in source
    assert "sortDirectoryNodes(organizations.data ?? [], locale)" in source
    assert "sortDirectoryNodes(units.data ?? [], locale)" in source
    assert "directoryOrganizationSelection(sortedOrganizations" in source
    assert 'status: person.status === "active" ? "archived" : "active"' in source
    assert "Record<ManagedDirectoryStatus, string>" in source
    assert "inactive:" not in source
    assert "setOrganizationId(effectiveOrgId); setUnitId" in source
    assert "setUnitId(effectiveUnitId)" in source
    for locale in ("en", "ja", "ko"):
        catalog = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text()
        assert '"隐藏已归档":' in catalog
