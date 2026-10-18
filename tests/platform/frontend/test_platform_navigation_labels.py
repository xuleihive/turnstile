from tests.support.paths import FRONTEND_SOURCE


def test_model_platform_labels_order_and_page_headings_match() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text()
    group = app.split('label: "模型平台"', 1)[1].split('label: "平台管理"', 1)[0]
    items = [
        '{ label: "模型管理", icon: Cpu, page: "models" }',
        '{ label: "订阅管理", icon: KeyRound, page: "applications" }',
        '{ label: "负载均衡", icon: Network, page: "apim-native-routes" }',
        '{ label: "网关发布", icon: History, page: "gateway-releases" }',
    ]
    positions = [group.index(item) for item in items]
    assert positions == sorted(positions)
    assert '{ label: "订阅",' not in group and '{ label: "APIM 后端池",' not in group
    assert 'page === "applications"\n          ? "订阅管理"' in app
    assert 'page === "apim-native-routes"\n        ? "负载均衡"' in app
    pools = (FRONTEND_SOURCE / "pages/apim-native-routes-page.tsx").read_text()
    subscriptions = (FRONTEND_SOURCE / "pages/applications-page.tsx").read_text()
    releases = (FRONTEND_SOURCE / "pages/gateway-releases-page.tsx").read_text()
    assert "<h1>负载均衡</h1>" in pools
    assert "<DialogTitle>负载均衡</DialogTitle>" in pools
    assert 'aria-label="搜索负载均衡"' in pools
    assert 'aria-label="打开负载均衡列表"' in app
    assert "<h1>订阅管理</h1>" in subscriptions
    assert 'onClick={openApplicationRoute(null, detailCategory)}>订阅管理</a>' in subscriptions
    assert 'ChangeColumn title="负载均衡"' in releases
    assert "APIM 后端池列表" not in pools
    for locale in ("en", "ja", "ko"):
        catalog = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text()
        for phrase in (
            "负载均衡", "订阅管理", "打开负载均衡列表", "搜索负载均衡", "负载均衡列表",
            "加载负载均衡列表", "无法加载负载均衡", "无法加载负载均衡：", "调整负载均衡列表宽度",
        ):
            assert f'"{phrase}":' in catalog


def test_platform_menu_uses_new_labels_and_directory_precedes_configuration() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text()
    group = app.split('label: "平台管理"', 1)[1].split("const visibleNavGroups", 1)[0]
    assert group.index('label: "组织管理"') < group.index('label: "系统配置"')
    assert 'selectedDataSource === "apim"' in group
    assert "directoryCapabilities" not in group
    assert 'page: "settings"' in group and 'page: "organization-management"' in group
    assert 'label: "系统管理"' not in app
    assert 'label: "设置"' not in group
    settings = (FRONTEND_SOURCE / "pages/settings-page.tsx").read_text()
    assert "<h1>系统配置</h1>" in settings
    assert 'aria-label="系统配置"' in settings
    for locale in ("en", "ja", "ko"):
        catalog = (FRONTEND_SOURCE / f"locales/{locale}/phrases-core.ts").read_text()
        assert '"平台管理":' in catalog
        assert '"系统配置":' in catalog
