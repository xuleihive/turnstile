from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from PIL import Image, PngImagePlugin

from backend.services.client_icons import (
    DEFAULT,
    FilePluginIconStore,
    PluginIcons,
    PluginIconUnavailable,
    load_plugin_icons,
    prepare_platform_icons,
    prepare_uploaded_icons,
    read_prepared_icons,
)
from tests.backend.model_platform.test_client_user_service import client_service


def picture(colour: str = "black", size: tuple[int, int] = (96, 48)) -> bytes:
    image = Image.new("RGBA", size, colour)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private upload metadata")
    out = BytesIO()
    image.save(out, "PNG", pnginfo=metadata)
    return out.getvalue()


def test_default_is_prepared_from_current_platform_icon_and_square() -> None:
    source = Path("frontend/public/turnstile.svg").read_bytes()
    prepared = prepare_platform_icons(source)
    saved = read_prepared_icons(DEFAULT)
    assert saved == prepared
    assert (
        json.loads(saved.files["manifest.json"])["source_sha256"]
        == hashlib.sha256(source).hexdigest()
    )
    assert b'width="128"' in saved.files["icon.svg"]
    assert b'height="128"' in saved.files["icon.svg"]
    assert b"#2f6fdd" in saved.files["icon.svg"] and b"#fff" in saved.files["icon.svg"]
    assert base64.b64decode(saved.data_uri.partition(",")[2]) == saved.files["icon.svg"]
    assert len(saved.files["icon.svg"]) < 1024 * 1024


def test_upload_hook_prepares_once_and_downloads_reuse_published_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilePluginIconStore(tmp_path)
    revision = store.update_from_upload(picture(), picture("white"))
    assert store.load() is not None
    mtimes = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setenv("CLIENT_PLUGIN_ICON_DIRECTORY", str(tmp_path))
    generation = Mock(side_effect=AssertionError("Downloads must not generate icons"))
    monkeypatch.setattr("backend.services.client_icons.prepare_uploaded_icons", generation)
    monkeypatch.setattr("backend.services.client_icons.prepare_platform_icons", generation)
    service, _, identity = client_service()
    first, second = service.package(identity), service.package(identity)
    for data in (first, second):
        with ZipFile(BytesIO(data)) as z:
            cfg = json.loads(z.read("aigateway-proxy-user-Test/aigateway-user.config.json"))
            assert cfg["iconRevision"] == revision
            assert cfg["icon"] == store.load().data_uri  # type: ignore[union-attr]
            assert (
                z.read("aigateway-proxy-user-Test/icons/icon.png")
                == store.load().files["icon.png"]  # type: ignore[union-attr]
            )
    assert mtimes == {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    generation.assert_not_called()


def test_raster_variants_keep_aspect_ratio_remove_metadata_and_add_contrast() -> None:
    icons = prepare_uploaded_icons(picture(), picture("white"))
    assert icons.files["icon-light.png"] != icons.files["icon-dark.png"]
    for name in ("icon.png", "icon-light.png", "icon-dark.png"):
        with Image.open(BytesIO(icons.files[name])) as image:
            assert image.size == (128, 128) and image.mode == "RGBA"
            assert image.info == {}
            assert image.getpixel((0, 0)) == (0, 0, 0, 0)
            # A 2:1 logo occupies 112x56 pixels, not a stretched square.
            background = (35, 39, 47) if name == "icon-dark.png" else (245, 247, 250)
            assert image.getpixel((64, 32)) == (*background, 255)
    with Image.open(BytesIO(prepare_uploaded_icons(picture("white")).files["icon.png"])) as image:
        assert image.getpixel((64, 10)) == (35, 39, 47, 255)


def test_revisions_are_content_based_and_invalid_upload_preserves_active(tmp_path: Path) -> None:
    store = FilePluginIconStore(tmp_path)
    first = store.update_from_upload(picture())
    assert store.update_from_upload(picture()) == first
    assert len([p for p in (tmp_path / "platform").iterdir() if p.is_dir()]) == 1
    before = (tmp_path / "platform/active.json").read_bytes()
    with pytest.raises(ValueError):
        store.update_from_upload(b"<svg><script>alert(1)</script></svg>")
    assert (tmp_path / "platform/active.json").read_bytes() == before
    second = store.update_from_upload(picture("blue"))
    assert second != first
    assert read_prepared_icons(tmp_path / "platform" / first).revision == first
    assert store.load().revision == second  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "raw", [b"bad", b"x" * (2 * 1024 * 1024 + 1), picture(size=(4, 4)), picture("#00000000")]
)
def test_invalid_uploads_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        prepare_uploaded_icons(raw)


def test_brand_and_revision_paths_are_confined_and_load_does_not_create_store(
    tmp_path: Path,
) -> None:
    store = FilePluginIconStore(tmp_path / "missing")
    assert store.load() is None and not store.directory.exists()
    with pytest.raises(ValueError):
        store.update_from_upload(picture(), brand="../outside")
    with pytest.raises(ValueError):
        store.publish(PluginIcons("../outside", "", {}))


def test_corrupt_active_assets_fail_without_generating_fallback_or_issuing_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FilePluginIconStore(tmp_path)
    revision = store.update_from_upload(picture())
    (tmp_path / "platform" / revision / "icon.png").write_bytes(b"corrupt")
    monkeypatch.setenv("CLIENT_PLUGIN_ICON_DIRECTORY", str(tmp_path))
    with pytest.raises(PluginIconUnavailable):
        load_plugin_icons()
    service, _, identity = client_service()
    issue = Mock()
    monkeypatch.setattr(service.store, "issue_download", issue)
    with pytest.raises(HTTPException) as error:
        service.package(identity)
    assert error.value.status_code == 503
    issue.assert_not_called()


def test_missing_custom_brand_uses_bundled_default_without_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "not-created"
    monkeypatch.setenv("CLIENT_PLUGIN_ICON_DIRECTORY", str(root))
    assert load_plugin_icons("enterprise-example") == read_prepared_icons(DEFAULT)
    assert not root.exists()
