"""Portable display-name filenames and ASCII-safe HTTP attachment headers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote
from uuid import UUID


def plugin_filename(prefix: str, name: str, fallback: str) -> str:
    label = unicodedata.normalize("NFKC", name).strip()
    try:
        UUID(label)
    except ValueError:
        pass
    else:
        label = ""
    label = re.sub(r"[^\w.-]+", "-", label)
    label = re.sub(r"-+", "-", label).strip(".-_")
    # Limit bytes, not characters, so multibyte names fit common filesystem limits.
    label = label.encode("utf-8")[:80].decode("utf-8", errors="ignore").rstrip(".-_")
    return f"{prefix}-{label or fallback}.zip"


def attachment_header(filename: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"-+", "-", ascii_name).replace("-.zip", "-download.zip")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
