from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.support.paths import (
    FRONTEND_SOURCE,
    REPOSITORY_ROOT,
    read_apim_dashboard_source,
    read_frontend_styles,
)


def test_product_tables_use_shared_resizable_wrappers() -> None:
    primitive = FRONTEND_SOURCE / "components/ui/resizable-table.tsx"
    raw_native_tables: list[str] = []
    raw_grid_tables: list[str] = []

    for path in FRONTEND_SOURCE.rglob("*.tsx"):
        if path == primitive:
            continue
        source = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(FRONTEND_SOURCE))
        if "<table" in source:
            raw_native_tables.append(relative)
        for line_number, line in enumerate(source.splitlines(), 1):
            if 'role="table"' in line and "<ResizableGridTable" not in line:
                raw_grid_tables.append(f"{relative}:{line_number}")

    assert raw_native_tables == ["data-sources/apim/pages/budget-page.tsx"]
    budget = (FRONTEND_SOURCE / raw_native_tables[0]).read_text(encoding="utf-8")
    assert '<table><thead><tr><th>部门</th><th>预算额度</th><th>未分配余额</th>' in budget
    assert raw_grid_tables == []


def test_resizable_table_primitive_keeps_interaction_and_accessibility_contract() -> None:
    source = (
        FRONTEND_SOURCE / "components/ui/resizable-table.tsx"
    ).read_text(encoding="utf-8")
    styles = read_frontend_styles()

    assert "onPointerDown" in source
    assert 'event.key !== "ArrowLeft" && event.key !== "ArrowRight"' in source
    assert 'event.key === "Enter" || event.key === " "' in source
    assert "onDoubleClick" in source
    assert 'role="separator"' in source
    assert "aria-valuemin" in source
    assert "aria-valuemax" in source
    assert "aria-valuenow" in source
    assert "headerSignatureRef" not in source
    assert "cellsChanged && headerCells.length > 0" in source
    assert "container.parentElement !== cell" in source
    assert 'window.addEventListener("resize", update)' in source
    assert '["resizable-grid-table", className]' in source
    assert '<span\n        className="table-column-resizer"' in source
    assert '<div\n        className="table-column-resizer"' not in source
    assert "--resizable-table-columns" in source
    assert ".table-column-resizer" in styles
    selector = '\t.table-column-resizer:not([data-resize-at-all-widths="true"]),'
    assert selector in styles
    assert "display: none" in styles.split(selector, 1)[1].split("}", 1)[0]
    assert "widths ?? lastVisibleWidthsRef.current" in source
    assert "new ResizeObserver(rememberVisibleWidths)" in source
    assert "return resizeAtAllWidths || enabled" in source
    # Icon-only headers (such as Actions) name their resize handle from the header label.
    assert 'cell.textContent?.trim() || cell.getAttribute("aria-label")?.trim()' in source


def test_column_resize_handles_have_localizable_names() -> None:
    source = (FRONTEND_SOURCE / "components/ui/resizable-table.tsx").read_text(encoding="utf-8")
    handle = source[
        source.index('className="table-column-resizer"'):source.index("onPointerDown=")
    ]

    assert "aria-label={`调整列宽：${label || index + 1}`}" in handle
    assert "data-no-localize" not in handle


def test_column_width_behavior_and_narrow_model_tracks() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("column-widths.test.mjs"))],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    page = (FRONTEND_SOURCE / "pages/model-management-page.tsx").read_text(encoding="utf-8")
    styles = read_frontend_styles()
    assert "horizontalPadding={32} resizeAtAllWidths" in page
    assert "[180, 120, 160, 80, 48]" in page
    assert "minmax(180px, var(--resizable-column-1, 1fr))" in styles
    assert "minmax(80px, var(--resizable-column-4, 90px))" in styles
    assert (
        "minmax(var(--model-table-compact-actions-min, 48px), "
        "var(--resizable-column-5, 76px))" in styles
    )


def test_visual_disclosure_and_action_tables_are_included() -> None:
    apim_dashboard = read_apim_dashboard_source()
    copilot_governance = (
        FRONTEND_SOURCE / "data-sources/github-copilot/pages/governance-page.tsx"
    ).read_text(encoding="utf-8")

    assert '<ResizableGridTable className="governance-table"' in apim_dashboard
    assert (
        '<ResizableGridTable className="copilot-governance-list copilot-team-list"'
        in copilot_governance
    )