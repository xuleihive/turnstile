from tests.support.paths import FRONTEND_SOURCE


def test_directory_controls_use_explicit_size_metadata_and_scoped_menu_styles() -> None:
    button = (FRONTEND_SOURCE / "components/ui/button.tsx").read_text()
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    styles = (FRONTEND_SOURCE / "styles/organization-management.css").read_text()
    assert "data-size={size}" in button and "data-variant={variant}" in button
    assert 'className="directory-person-menu"' in directory
    assert '[data-size="sm"]' in styles
    assert '[data-size^="icon"]' in styles
    assert '.directory-person-menu [data-slot="dropdown-menu-item"] > svg' in styles
    assert "width: 14px; height: 14px; flex: 0 0 14px" in styles


def test_directory_header_and_permission_tags_keep_compact_readable_dimensions() -> None:
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    styles = (FRONTEND_SOURCE / "styles/organization-management.css").read_text()
    assert '<FolderTree size={17} aria-hidden="true" />' in directory
    assert 'className="directory-table directory-people-table"' in directory
    assert "minWidths={peopleColumnMinWidths}" in directory
    assert "const peopleColumnMinWidths = [160, 160, 200, 160, 80, 100, 60] as const" in directory
    tags = styles.split('.directory-permission-tags [data-slot="badge"]', 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in tags and "flex: 0 0 auto" in tags
    assert "overflow-wrap: normal" in tags and "max-width: none" in tags
    assert (
        ".directory-people-table th:nth-child(3) { width: 200px; white-space: nowrap; }"
        in styles
    )


def test_directory_creation_buttons_and_hover_colors_follow_the_theme_variants() -> None:
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    styles = (FRONTEND_SOURCE / "styles/organization-management.css").read_text()
    for label in ("新增人员", "添加成员"):
        line = next(line for line in directory.splitlines() if f">{label}</Button>" in line)
        assert 'variant="outline" size="sm"' in line
    assert '[data-slot="button"]:hover:not(:disabled)' not in styles
    outline = styles.split(
        '.directory-dialog [data-slot="button"][data-variant="outline"] {', 1
    )[1].split("}", 1)[0]
    assert "background: var(--background)" in outline
    assert "color: var(--foreground)" in outline
    assert "border-color: var(--border)" in outline
    assert '[data-variant="outline"]:hover:not(:disabled)' in styles
    assert '[data-variant="default"]:hover:not(:disabled)' in styles
    assert "var(--primary) 85%, var(--background)" in styles


def test_person_action_dialog_controls_and_status_values_are_consistent() -> None:
    source = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    action = source.split("function PersonActionDialog", 1)[1].split(
        "function AddMembersDialog", 1,
    )[0]
    controls = (FRONTEND_SOURCE / "components/directory-controls.tsx").read_text()
    styles = (FRONTEND_SOURCE / "styles/organization-management.css").read_text()
    assert "DialogClose" not in action
    assert '<Button type="button" variant="outline" disabled={save.isPending}' in action
    assert 'onClick={onClose}>取消</Button>' in action
    assert '<dl className="directory-status-facts">' in action
    assert "<dt>人员状态</dt><dd><Badge>" in action
    assert 'className="directory-team-name" data-no-localize' in action
    assert 'title={selectedLabel}' in controls
    assert 'className="directory-select-content"' in controls
    trigger = styles.split('\n.directory-dialog [data-slot="select-trigger"] {', 1)[1].split(
        "}", 1,
    )[0]
    assert "width: 100%" in trigger and "min-width: 0" in trigger and "font-size: 12px" in trigger
    assert '.directory-status-facts [data-slot="badge"] { width: fit-content' in styles
