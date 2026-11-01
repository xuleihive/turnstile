import ast
import gzip
import os
from pathlib import Path

import pytest
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from backend.http.static_files import validate_production_web_dist, web_application_file
from turnstile_core.config import Settings


def test_production_requires_frontend_index(tmp_path: Path) -> None:
    settings = Settings(production=True, web_dist_dir=tmp_path / "missing")

    with pytest.raises(RuntimeError, match="WEB_DIST_DIR must contain index.html"):
        validate_production_web_dist(settings)


def test_production_accepts_frontend_index(tmp_path: Path) -> None:
    web_dist = tmp_path / "dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")

    validate_production_web_dist(Settings(production=True, web_dist_dir=web_dist))


def test_non_production_allows_missing_frontend_build(tmp_path: Path) -> None:
    validate_production_web_dist(
        Settings(production=False, web_dist_dir=tmp_path / "missing")
    )


def test_unhashed_web_files_are_revalidated_across_fixed_timestamp_releases(
    tmp_path: Path,
) -> None:
    web_dist = tmp_path / "dist"
    web_dist.mkdir()
    index = web_dist / "index.html"
    index.write_text('<script src="/assets/index-AAAAAAAA.js"></script>', encoding="utf-8")
    (web_dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    os.utime(index, (1_577_836_800, 1_577_836_800))
    application = Starlette(routes=[Route(
        "/{client_path:path}",
        lambda request: web_application_file(web_dist, request.path_params["client_path"]),
    )])
    with TestClient(application) as client:
        previous = client.get("/")
        index.write_text('<script src="/assets/index-BBBBBBBB.js"></script>', encoding="utf-8")
        os.utime(index, (1_577_836_800, 1_577_836_800))
        for path, body in (
            ("/", "index-BBBBBBBB"),
            ("/dashboard/requests", "index-BBBBBBBB"),
            ("/index.html", "index-BBBBBBBB"),
            ("/favicon.svg", "<svg/>"),
        ):
            response = client.get(path, headers={
                "If-None-Match": previous.headers["etag"],
                "If-Modified-Since": previous.headers["last-modified"],
            })
            assert response.status_code == 200
            assert body in response.text
            assert response.headers["cache-control"] == "no-cache"
    assert web_application_file(web_dist, "../outside.txt").path == index


@pytest.mark.parametrize("encoding", ["gzip", "identity"])
def test_asset_compression_preserves_content_and_does_not_wrap_api(
    tmp_path: Path, encoding: str,
) -> None:
    content = b'export const text = "static application content";\n' * 4096
    (tmp_path / "index-unit.js").write_bytes(content)
    application = Starlette(routes=[
        Route("/api/value", lambda request: Response(content, media_type="application/json")),
        Mount("/assets", GZipMiddleware(
            StaticFiles(directory=tmp_path), minimum_size=1024, compresslevel=6,
        )),
    ])
    with TestClient(application) as client:
        asset = client.get("/assets/index-unit.js", headers={"Accept-Encoding": encoding})
        assert asset.status_code == 200
        assert asset.content == content
        assert asset.headers.get("content-encoding") == ("gzip" if encoding == "gzip" else None)
        if encoding == "gzip":
            assert "Accept-Encoding" in asset.headers["vary"]
            with client.stream(
                "GET", "/assets/index-unit.js", headers={"Accept-Encoding": encoding},
            ) as streamed:
                compressed = b"".join(streamed.iter_raw())
            assert len(compressed) < len(content)
            assert gzip.decompress(compressed) == content
        api = client.get("/api/value", headers={"Accept-Encoding": encoding})
        assert api.content == content
        assert "content-encoding" not in api.headers
        head = client.head("/assets/index-unit.js", headers={"Accept-Encoding": encoding})
        assert head.status_code == 200
        assert head.content == b""
        unchanged = client.get(
            "/assets/index-unit.js",
            headers={"Accept-Encoding": encoding, "If-None-Match": asset.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert client.get("/assets/missing.js").status_code == 404


def test_api_compression_is_scoped_to_the_static_asset_mount() -> None:
    source = (Path(__file__).resolve().parents[3] / "backend/api.py").read_text()
    tree = ast.parse(source)
    compressed = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "GZipMiddleware"
    ]
    assert len(compressed) == 1
    mounts = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mount" and compressed[0] in node.args
    ]
    assert len(mounts) == 1
    assert ast.literal_eval(mounts[0].args[0]) == "/assets"
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_middleware"
        and any(isinstance(argument, ast.Name) and argument.id == "GZipMiddleware"
                for argument in node.args)
        for node in ast.walk(tree)
    )