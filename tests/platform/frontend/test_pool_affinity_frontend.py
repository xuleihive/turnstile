from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT, read_frontend_styles


def test_pool_affinity_legacy_payload_unit_cases() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend unit tests"
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         str(Path(__file__).with_name("pool-session-affinity.test.mjs"))],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pool_affinity_editor_is_capability_and_owner_gated() -> None:
    editor = (
        FRONTEND_SOURCE / "components/model-management/deployment-resilience-editor.tsx"
    ).read_text(encoding="utf-8")
    api = (FRONTEND_SOURCE / "data-sources/apim/api.ts").read_text(encoding="utf-8")
    assert "registry.data?.backend_pool_session_affinity_supported === true" in editor
    assert "poolPublicationPayload(draft, affinitySupported)" in editor
    assert "session_affinity: pool.session_affinity === true" in editor
    assert "session_affinity: false" in editor
    assert "disabled={!canManage || busy || !affinitySupported || !validMemberCount}" in editor
    assert 'const canManage = user?.role === "owner"' in editor
    assert (
        "const canConfigure = canManage && pool.isSuccess && "
        "(hasEffectivePool || selectableCandidates.length >= 2)" in editor
    )
    assert "disabled={busy || !canConfigure || !validDraft}" in editor
    assert 'provisioning: "正在同步路由数据"' in editor
    assert 'building_revision: "正在校验路由快照"' in editor
    assert 'promoting: "正在切换生效路由"' in editor
    assert "<h4" not in editor
    assert '<span className="router-resilience-member-role"' in editor
    assert '<span className="router-resilience-member-connection"' in editor
    assert "createOptimisticPoolPublication(" in editor
    assert "onSubmitted?.(submission)" in editor
    assert 'action: "configure"' in editor
    assert 'action: "remove"' in editor
    assert "const hasEffectivePool = Boolean(pool.data || providedInitialDraft)" in editor
    assert "const isRemoveFlow =" in editor
    assert 'data-pending>{PUBLICATION_LABELS[publicationStatus]}' in editor
    assert 'message: publication.data?.error_message ?? "APIM 后端池发布失败",\n' \
        "        publicationId,\n" in editor
    assert (
        "{!publicationStatus && notice?.publicationId && "
        '<span className="router-resilience-task">任务 ID '
        "<code data-no-localize>{notice.publicationId}</code></span>}"
    ) in editor
    assert ".router-resilience-task {" in read_frontend_styles()
    dialog = (
        FRONTEND_SOURCE / "components/model-management/apim-native-route-create-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert "onSubmitted={handleSubmitted}" in dialog
    assert "return !open ? null :" in dialog
    native_page = (
        FRONTEND_SOURCE / "pages/apim-native-routes-page.tsx"
    ).read_text(encoding="utf-8")
    assert "PENDING_SUBMISSIONS_STORAGE_KEY" in native_page
    assert "subscribePendingBackendPoolSubmissions" in native_page
    assert "const isRemovedModel =" in native_page
    assert "const [pendingSubmissions, setPendingSubmissions] =" in native_page
    assert (
        'initialDraft={selectedPending && selectedPending.action !== "remove" '
        "? selectedPending.draft : null}" in native_page
    )
    assert "initialPublicationId={selectedPending?.publication.id ?? null}" in native_page
    assert "initialPublicationAction={selectedPending?.action ?? null}" in native_page
    assert "onSubmitted={handleRouteSubmitted}" in native_page
    assert '<LoaderCircle className="spin" size={15} />' in native_page
    assert (
        '<details className="application-card application-quickstart-card '
        'apim-native-route-quickstart-card">' in native_page
    )
    assert "<ClientSampleCode" in native_page
    assert "clientSampleFor(language, baseUrl, selectedModel.model_key, operation)" in native_page
    assert "客户端调用零改动，继续使用模型标识" in native_page
    assert "registry.backend_pool_session_affinity_supported === true" in api
    styles = read_frontend_styles()
    assert ".apim-native-route-quickstart-card > summary {" in styles
    assert ".router-resilience-affinity { min-width: 0;" in styles
    assert (
        '.router-resilience-runtime-list[data-pool="false"] '
        ".router-resilience-runtime {" in styles
    )
    assert '.router-resilience-runtime-list[data-pool] .router-resilience-member-role {' in styles