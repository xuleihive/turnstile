import subprocess
from pathlib import Path

from tests.support.paths import FRONTEND_SOURCE, REPOSITORY_ROOT


def test_client_download_navigation_and_private_transfer() -> None:
    app = (FRONTEND_SOURCE / "app.tsx").read_text(encoding="utf-8")
    assert 'if (page === "client-downloads") return page;' in app
    assert 'navigatePage("client-downloads")' in app
    assert "<ClientDownloadsPage" in app
    api = (FRONTEND_SOURCE / "api/client-downloads.ts").read_text(encoding="utf-8")
    assert 'method: "POST", credentials: "include", cache: "no-store"' in api
    assert "SESSION_EXPIRED_EVENT" in api
    page = (FRONTEND_SOURCE / "pages/client-downloads-page.tsx").read_text(encoding="utf-8")
    assert "URL.revokeObjectURL" in page
    assert "localStorage" not in page
    assert 'aria-describedby="client-secret-warning"' in page
    assert 'className="finops-scroll-region"' in page
    assert "该订阅没有已发布且获授权的模型。" in page
    assert 'query.data?.plugin_version ?? "0.1.4"' not in page


def test_plugin_hooks_and_dispatch() -> None:
    subprocess.run(
        ["node", "--test", str(REPOSITORY_ROOT / "tests/platform/frontend/client-plugin.test.mjs")],
        check=True,
    )


def test_image_bridge_paths_and_lifecycle() -> None:
    subprocess.run(
        [
            "node", "--test",
            str(REPOSITORY_ROOT / "tests/platform/frontend/client-image-lifecycle.test.mjs"),
            str(REPOSITORY_ROOT / "tests/platform/frontend/client-native-images.test.mjs"),
            str(Path(__file__).with_name("client-native-image-integration.test.mjs")),
        ],
        check=True,
    )


def test_browser_authorization_request_transport() -> None:
    subprocess.run(
        [
            "node", "--test",
            str(REPOSITORY_ROOT / "tests/platform/frontend/client-authorization.test.mjs"),
        ],
        check=True,
    )


def test_browser_download_names() -> None:
    subprocess.run(
        [
            "node", "--test",
            str(REPOSITORY_ROOT / "tests/platform/frontend/client-downloads.test.mjs"),
        ],
        check=True,
    )
