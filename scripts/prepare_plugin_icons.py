"""Prepare the built-in default, or publish a future authorized branding upload."""

from __future__ import annotations

import argparse
from pathlib import Path

from backend.services.client_icons import (
    DEFAULT,
    FilePluginIconStore,
    prepare_platform_icons,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", type=Path)
    parser.add_argument("--dark-upload", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--brand", default="platform")
    args = parser.parse_args()
    if args.upload:
        if not args.store:
            parser.error("--upload requires a durable --store")
        revision = FilePluginIconStore(args.store).update_from_upload(
            args.upload.read_bytes(),
            args.dark_upload.read_bytes() if args.dark_upload else None,
            brand=args.brand,
        )
    else:
        if args.dark_upload or args.store or args.brand != "platform":
            parser.error("upload options require --upload")
        source = Path(__file__).resolve().parents[1] / "frontend/public/turnstile.svg"
        icons = prepare_platform_icons(source.read_bytes())
        DEFAULT.mkdir(parents=True, exist_ok=True)
        for name, content in icons.files.items():
            path = DEFAULT / name
            if not path.exists() or path.read_bytes() != content:
                path.write_bytes(content)
        revision = icons.revision
    print("Prepared plugin icons:", revision)


if __name__ == "__main__":
    main()
