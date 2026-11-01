from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

from turnstile_core.config import Settings


def web_application_file(web_dist: Path, client_path: str) -> FileResponse:
    candidate = (web_dist / client_path).resolve()
    if not (candidate.is_file() and web_dist.resolve() in candidate.parents):
        candidate = web_dist / "index.html"
    # Packages use one fixed file timestamp, so browsers must revalidate unhashed files.
    return FileResponse(candidate, headers={"Cache-Control": "no-cache"})


def validate_production_web_dist(settings: Settings) -> None:
    if not settings.production:
        return
    index = settings.web_dist_dir / "index.html"
    if not index.is_file():
        raise RuntimeError(
            "WEB_DIST_DIR must contain index.html when PRODUCTION=true: "
            f"{settings.web_dist_dir}"
        )