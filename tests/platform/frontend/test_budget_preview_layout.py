from tests.support.paths import FRONTEND_SOURCE, read_frontend_styles


def test_budget_editor_body_rows_keep_intrinsic_content_height() -> None:
    styles = read_frontend_styles()
    body = styles.split(".budget-editor-body {", 1)[1].split("}", 1)[0]

    assert "grid-auto-rows: max-content;" in body, (
        "BudgetEditor body rows must keep their intrinsic content height "
        "so a long allocation preview cannot overlap the budget controls"
    )


def test_budget_rows_use_the_shared_resized_width() -> None:
    source = (FRONTEND_SOURCE / "data-sources/apim/pages/budget-page.tsx").read_text(
        encoding="utf-8"
    )
    styles = (FRONTEND_SOURCE / "styles/model-platform.css").read_text(encoding="utf-8")
    assert 'className="budget-table-row" role="row"' in source
    assert ".budget-table { min-width: var(--resizable-table-width, 1094px); }" in (
        read_frontend_styles()
    )
    assert (
        '.resizable-grid-table [role="row"] { width: max(100%, '
        'var(--resizable-table-width, 100%)); }' in styles
    )


def test_budget_preview_keeps_scrollable_body_and_separate_footer() -> None:
    styles = read_frontend_styles()
    form = styles.split(".registry-editor {", 1)[1].split("}", 1)[0]
    body = styles.split(".registry-editor-body {", 1)[1].split("}", 1)[0]
    source = (FRONTEND_SOURCE / "data-sources/apim/pages/budget-page.tsx").read_text(
        encoding="utf-8"
    )
    editor = source.split("function BudgetEditor(", 1)[1].split(
        "function RemoveBudgetConfirm(", 1
    )[0]

    assert "max-height: min(820px, calc(100vh - 32px));" in form
    assert "display: flex;" in form
    assert "flex-direction: column;" in form
    assert "min-height: 0;" in body
    assert "display: grid;" in body
    assert "overflow-y: auto;" in body
    assert 'className="registry-editor-body budget-editor-body"' in editor
    assert (
        editor.index("value={budgetMode}")
        < editor.index('className="budget-allocation-preview"')
        < editor.index('name="warning_threshold_percent"')
        < editor.index("\n        </div>\n        <DialogClose")
        < editor.index('<DialogFooter className="registry-editor-footer budget-editor-footer"')
    )