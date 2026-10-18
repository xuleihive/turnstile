import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_report_directory_selection_search_and_old_links() -> None:
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("report-directory.test.mjs"))],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_report_center_reuses_viewer_cache_permissions_and_in_place_navigation() -> None:
    center = (FRONTEND_SOURCE / "pages/report-center-page.tsx").read_text()
    viewer = (FRONTEND_SOURCE / "pages/pinned-report-page.tsx").read_text()
    assert "useQuery(pinnedChartsQuery(owner))" in center
    assert 'aria-current={selected?.id === report.id ? "page" : undefined}' in center
    assert 'window.addEventListener("popstate", syncSelection)' in center
    assert 'window.removeEventListener("popstate", syncSelection)' in center
    assert "window.history.pushState" in center
    assert "window.location.search =" not in center
    assert "queryClient.setQueryData<PinnedReport[]>(pinnedChartsKey(owner)" in center
    assert "selectedReport(current, reportIdFromUrl())?.id === id" in center
    assert "if (deletingSelected) select(remaining[0]?.id ?? null, true)" in center
    assert "<PinnedReportPage key={selected.id}" in center
    assert "embedded onToggleSidebar" in center
    assert "report.can_manage && <Button" in center
    assert "if (deleting?.can_manage) remove.mutate(deleting)" in center
    assert "if (!report.can_manage) throw" in center
    assert "<AlertDialogTitle>删除报表</AlertDialogTitle>" in center
    assert "title={report.title} data-no-localize" in center
    assert "pinned.can_manage" in viewer and "if (!pinned?.can_manage) return" in viewer
    chart = (FRONTEND_SOURCE / "components/charts/chart-card.tsx").read_text()
    assert "<th><span>{chart.category_label}</span></th>" in chart
    pin = (FRONTEND_SOURCE / "components/assistant/pin-dialog.tsx").read_text()
    assert "reports.filter((report) => report.can_manage).map" in pin
    for locale in ("en", "ja", "ko"):
        catalog = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text()
        for phrase in ("FinOps助手", "报表中心", "报表目录", "搜索报表", "保存报表",
                       "暂无报表", "报表不存在或无权访问", "刷新报表目录"):
            assert f'"{phrase}":' in catalog
