from tests.support.paths import FRONTEND_SOURCE


def test_security_audit_page_has_owner_navigation_and_metadata_filters() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    source = (FRONTEND_SOURCE / "data-sources" / "apim" / "source.tsx").read_text(encoding="utf-8")
    page = (FRONTEND_SOURCE / "pages" / "security-audit-page.tsx").read_text(encoding="utf-8")

    assert '"security-audit"' in app
    assert 'user?.role === "owner"' in app
    assert "SecurityAuditPage" in app
    assert 'id: "security-audit"' in source
    assert "操作者 ID" in page
    assert "开始时间" in page
    assert "结束时间" in page
    assert "操作" in page
    assert "结果" in page
    assert "下一页" in page
    assert "导出 CSV" in page
    assert 'role="alert"' in page


def test_security_audit_source_uses_read_only_list_and_csv_endpoints() -> None:
    source = (FRONTEND_SOURCE / "data-sources" / "apim" / "security-audit.ts").read_text(
        encoding="utf-8"
    )

    assert "`/api/v1/security/audit-events?${query}`" in source
    assert "`/api/v1/security/audit-events/export?${query}`" in source
    assert "requestBlob" in source
    assert "request<SecurityAuditEventPage>" in source
    assert 'method: "POST"' not in source
    assert 'method: "DELETE"' not in source
