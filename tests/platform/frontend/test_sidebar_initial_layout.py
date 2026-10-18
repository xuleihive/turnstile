from tests.support.paths import FRONTEND_SOURCE


def test_fixed_navigation_does_not_depend_on_directory_or_report_fetch() -> None:
    source = (FRONTEND_SOURCE / "app.tsx").read_text()
    platform = source.split('label: "平台管理"', 1)[1].split("const visibleNavGroups", 1)[0]
    assert "directoryCapabilities" not in platform
    assert 'label: "组织管理"' in platform
    assert "group.items.filter((item) => canAccessMenu(user, item.page))" in source
    assert 'label: "AI FinOps"' in source
    assert 'label: "FinOps助手"' in source and 'label: "报表中心"' in source
    assert "pinnedChartsQuery" not in source and "pinnedCharts.data" not in source
    assert 'key="pinned"' not in source and "pinnedOpen" not in source
    assert 'page === "pinned-report" && (' in source
    assert "<ReportCenterPage onToggleSidebar={toggleSidebar}" in source
    controller = (
        FRONTEND_SOURCE / "components/assistant/use-assistant-controller.ts"
    ).read_text()
    assert 'enabled: source === "apim" && Boolean(owner && pinTarget)' in controller
    assert "capabilities={directoryCapabilities.data}" in source


def test_mobile_layout_is_known_on_the_initial_render() -> None:
    source = (FRONTEND_SOURCE / "hooks/use-mobile.ts").read_text()
    assert "React.useState(() =>" in source
    initializer = source.split("React.useState(() =>", 1)[1].split("React.useEffect", 1)[0]
    assert "window.matchMedia" in initializer
    assert "undefined>(undefined)" not in source
