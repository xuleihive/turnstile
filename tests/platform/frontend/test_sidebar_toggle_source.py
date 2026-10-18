from tests.support.paths import FRONTEND_SOURCE


def test_settings_and_directory_headers_use_the_shared_sidebar_callback() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text()
    settings = (FRONTEND_SOURCE / "pages/settings-page.tsx").read_text()
    directory = (FRONTEND_SOURCE / "pages/organization-management-page.tsx").read_text()
    settings_mount = app.split('<SettingsPage key={selectedDataSource}', 1)[1].split("/>", 1)[0]
    assert "onToggleSidebar={toggleSidebar}" in settings_mount
    assert "onToggleSidebar: () => void" in settings
    header = settings.split('<header className="smh-page-header">', 1)[1].split("</header>", 1)[0]
    assert 'className="model-mobile-sidebar-toggle"' in header
    assert 'aria-label="切换导航栏"' in header
    assert 'title="切换导航栏"' in header
    assert "onClick={onToggleSidebar}" in header
    assert "<PanelLeft size={16} />" in header
    directory_header = directory.split('<header className="finops-header">', 1)[1].split(
        "</header>", 1
    )[0]
    assert 'className="finops-sidebar-trigger"' in directory_header
    assert "onClick={onToggleSidebar}" in directory_header


def test_header_sidebar_toggles_are_visible_without_a_mobile_only_rule() -> None:
    finops = (FRONTEND_SOURCE / "styles/finops.css").read_text()
    models = (FRONTEND_SOURCE / "styles/model-platform.css").read_text()
    for styles, selector in (
        (finops, ".finops-header .finops-sidebar-trigger"),
        (models, ".model-mobile-sidebar-toggle"),
    ):
        rules = styles.split(selector, 1)[1].split("}", 1)[0]
        assert "display: inline-flex" in rules
        assert "display: none" not in rules
        assert "width: 30px" in rules and "height: 30px" in rules
