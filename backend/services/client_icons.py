"""Prepared, offline plugin icons; downloads never render uploaded images."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import struct
import tempfile
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps, ImageStat, UnidentifiedImageError

ICON_SIZE = 128
MAX_INPUT = 2 * 1024 * 1024
MAX_ICON = 1024 * 1024
GENERATOR = "turnstile-plugin-icons-v1"
DEFAULT = Path(__file__).with_name("client_assets") / "icons" / "default"
_FILES = {
    "icon.svg",
    "icon-light.svg",
    "icon-dark.svg",
    "icon.png",
    "icon-light.png",
    "icon-dark.png",
}
_BRAND = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_SVG_TAGS = {"svg", "g", "rect", "circle", "path"}
_SVG_ATTRIBUTES = {
    "width",
    "height",
    "viewBox",
    "rx",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "transform",
    "cx",
    "cy",
    "r",
    "d",
}


class PluginIconUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginIcons:
    revision: str
    data_uri: str
    files: dict[str, bytes]

    def package_files(self) -> dict[str, bytes]:
        return {"icons/" + name: content for name, content in self.files.items()}


def _bundle(files: dict[str, bytes], main: str, source_hash: str) -> PluginIcons:
    if (
        main not in files
        or set(files) - _FILES
        or any(not b or len(b) > MAX_ICON for b in files.values())
    ):
        raise ValueError("Invalid plugin icon files")
    media = "image/svg+xml" if main.endswith(".svg") else "image/png"
    uri = "data:" + media + ";base64," + base64.b64encode(files[main]).decode("ascii")
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}
    revision = hashlib.sha256(
        json.dumps([GENERATOR, ICON_SIZE, digests], sort_keys=True).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "generator": GENERATOR,
        "size": ICON_SIZE,
        "revision": revision,
        "source_sha256": source_hash,
        "main": main,
        "theme_policy": "single-slot-neutral-badge",
        "sha256": digests,
        "data_uri": uri,
    }
    return PluginIcons(
        revision,
        uri,
        {**files, "manifest.json": json.dumps(manifest, sort_keys=True, indent=2).encode()},
    )


def _inert_svg(source: bytes) -> ET.Element:
    if len(source) > MAX_ICON or b"<!" in source:
        raise ValueError("Invalid platform SVG")
    root = ET.fromstring(source)
    for element in root.iter():
        if (
            not element.tag.startswith("{http://www.w3.org/2000/svg}")
            or element.tag.rsplit("}", 1)[-1] not in _SVG_TAGS
            or set(element.attrib) - _SVG_ATTRIBUTES
            or (element.text or "").strip()
            or (element.tail or "").strip()
        ):
            raise ValueError("Only the platform's inert SVG geometry is supported")
        if any("url(" in value.lower() or ":" in value for value in element.attrib.values()):
            raise ValueError("External SVG references are not supported")
    if root.tag != "{http://www.w3.org/2000/svg}svg" or root.get("viewBox") != "0 0 32 32":
        raise ValueError("The platform icon must use its 32 by 32 viewBox")
    return root


def prepare_platform_icons(source: bytes) -> PluginIcons:
    """Build the checked-in default from the repository's trusted platform SVG."""
    root = _inert_svg(source)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    root.set("width", str(ICON_SIZE))
    root.set("height", str(ICON_SIZE))
    rendered = ET.tostring(root, encoding="utf-8")
    # The branded background gives contrast even when the host overrides the OS theme.
    return _bundle(
        {name: rendered for name in ("icon.svg", "icon-light.svg", "icon-dark.svg")},
        "icon.svg",
        hashlib.sha256(source).hexdigest(),
    )


def _pixels(raw: bytes) -> Image.Image:
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("Icon uploads must be at most 2 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if (
                    image.format not in {"PNG", "JPEG", "WEBP"}
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("Use a static PNG, JPEG or WebP icon, not uploaded SVG")
                if min(image.size) < 8 or max(image.size) > 2048:
                    raise ValueError("Icon dimensions must be between 8 and 2048 pixels")
                image.load()
                oriented = ImageOps.exif_transpose(image).convert("RGBA")
                clean = Image.new("RGBA", oriented.size)
                clean.paste(oriented)
                if clean.getchannel("A").getbbox() is None:
                    raise ValueError("The icon is transparent")
                return clean
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("Invalid icon image") from error


def _png(image: Image.Image, background: tuple[int, int, int, int] | None = None) -> bytes:
    logo = ImageOps.contain(image, (112, 112), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE))
    if background:
        ImageDraw.Draw(canvas).rounded_rectangle((0, 0, 127, 127), radius=24, fill=background)
    canvas.alpha_composite(logo, ((ICON_SIZE - logo.width) // 2, (ICON_SIZE - logo.height) // 2))
    output = BytesIO()
    canvas.save(output, "PNG", optimize=True)
    return output.getvalue()


def _matte(image: Image.Image) -> tuple[int, int, int, int]:
    red, green, blue = ImageStat.Stat(image.convert("RGB"), image.getchannel("A")).mean
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return (35, 39, 47, 255) if luminance > 0.6 else (245, 247, 250, 255)


def prepare_uploaded_icons(light: bytes, dark: bytes | None = None) -> PluginIcons:
    """Upload-completion hook: normalize once, strip metadata, preserve brand colours."""
    image = _pixels(light)
    # The single host slot cannot follow manual theme changes: choose a contrast matte.
    background = _matte(image)
    prepared = _png(image, background)
    alternative = _pixels(dark) if dark is not None else None
    files = {
        "icon.png": prepared,
        "icon-light.png": prepared,
        "icon-dark.png": _png(alternative, _matte(alternative))
        if alternative is not None
        else prepared,
    }
    return _bundle(files, "icon.png", hashlib.sha256(light + b"\x00" + (dark or b"")).hexdigest())


def read_prepared_icons(directory: Path) -> PluginIcons:
    manifest_path = directory / "manifest.json"
    if manifest_path.stat().st_size > 2 * MAX_ICON:
        raise ValueError("Icon manifest is too large")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("size") != ICON_SIZE
        or manifest.get("generator") != GENERATOR
    ):
        raise ValueError("Unsupported icon bundle")
    if not isinstance(manifest.get("sha256"), dict) or not isinstance(manifest.get("main"), str):
        raise ValueError("Invalid icon file manifest")
    files = {}
    for name, checksum in manifest["sha256"].items():
        if name not in _FILES:
            raise ValueError("Invalid icon asset path")
        path = directory / name
        if path.stat().st_size > MAX_ICON:
            raise ValueError("Icon asset is too large")
        content = path.read_bytes()
        if len(content) > MAX_ICON or hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError("Corrupt icon asset")
        if name.endswith(".svg"):
            svg = _inert_svg(content)
            if svg.get("width") != str(ICON_SIZE) or svg.get("height") != str(ICON_SIZE):
                raise ValueError("Invalid prepared SVG dimensions")
        elif (
            not content.startswith(b"\x89PNG\r\n\x1a\n")
            or len(content) < 24
            or struct.unpack(">II", content[16:24]) != (ICON_SIZE, ICON_SIZE)
        ):
            raise ValueError("Invalid prepared PNG")
        files[name] = content
    main = manifest["main"]
    if main not in files:
        raise ValueError("Invalid main icon")
    revision = hashlib.sha256(
        json.dumps(
            [GENERATOR, ICON_SIZE, manifest["sha256"]],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    media = "image/svg+xml" if main.endswith(".svg") else "image/png"
    prefix = "data:" + media + ";base64,"
    uri = manifest["data_uri"]
    if (
        revision != manifest["revision"]
        or not isinstance(uri, str)
        or not uri.startswith(prefix)
        or len(uri) > 3 << 19
        or base64.b64decode(uri[len(prefix) :], validate=True) != files[main]
    ):
        raise ValueError("Corrupt icon manifest")
    # Verification is hashing only; no image decoding/rendering or writes on download.
    return PluginIcons(revision, uri, {**files, "manifest.json": manifest_bytes})


class FilePluginIconStore:
    """Durable/shared-directory adapter; future branding storage can replace this adapter."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _brand(self, brand: str) -> Path:
        if not _BRAND.fullmatch(brand):
            raise ValueError("Invalid brand key")
        return self.directory / brand

    def load(self, brand: str = "platform") -> PluginIcons | None:
        folder = self._brand(brand)
        try:
            revision = (folder / "active.json").read_text()
        except FileNotFoundError:
            return None
        pointer = json.loads(revision)
        if (
            not isinstance(pointer, dict)
            or not isinstance(pointer.get("revision"), str)
            or not _REVISION.fullmatch(pointer["revision"])
        ):
            raise ValueError("Invalid active icon revision")
        return read_prepared_icons(folder / pointer["revision"])

    def publish(self, icons: PluginIcons, brand: str = "platform") -> str:
        if not _REVISION.fullmatch(icons.revision):
            raise ValueError("Invalid icon revision")
        folder = self._brand(brand)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / icons.revision
        with tempfile.TemporaryDirectory(prefix=".icons-", dir=folder) as temporary:
            stage = Path(temporary) / "bundle"
            stage.mkdir()
            for name, data in icons.files.items():
                if name not in _FILES | {"manifest.json"}:
                    raise ValueError("Invalid icon asset path")
                (stage / name).write_bytes(data)
            checked = read_prepared_icons(stage)
            if checked.revision != icons.revision or checked.data_uri != icons.data_uri:
                raise ValueError("Invalid prepared icon bundle")
            if not target.exists():
                try:
                    os.rename(stage, target)
                except OSError as error:
                    if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise
            if read_prepared_icons(target).revision != icons.revision:
                raise ValueError("Existing icon revision differs")
            pointer = Path(temporary) / "active.json"
            pointer.write_text(json.dumps({"revision": icons.revision}))
            os.replace(pointer, folder / "active.json")
        return icons.revision

    def update_from_upload(
        self, light: bytes, dark: bytes | None = None, *, brand: str = "platform"
    ) -> str:
        """Call after an authorized enterprise-icon upload, not from a download handler."""
        return self.publish(prepare_uploaded_icons(light, dark), brand)


def load_plugin_icons(brand: str = "platform") -> PluginIcons:
    try:
        if not _BRAND.fullmatch(brand):
            raise ValueError("Invalid brand key")
        root = os.getenv("CLIENT_PLUGIN_ICON_DIRECTORY")
        if root:
            prepared = FilePluginIconStore(Path(root)).load(brand)
            if prepared is not None:
                return prepared
        return read_prepared_icons(DEFAULT)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PluginIconUnavailable("Prepared plugin icon assets are unavailable") from error
