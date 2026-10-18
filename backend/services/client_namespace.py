"""Readable plugin namespaces, separate from authorization identity."""

import hashlib
import json
import re
import unicodedata
from typing import Literal
from uuid import UUID


def plugin_namespace(
    kind: Literal["user", "subscription"], name: str, scope: str, identity: str,
) -> str:
    label = unicodedata.normalize("NFKC", name).strip().lower()
    try:
        UUID(label)
    except ValueError:
        pass
    else:
        label = ""
    label = "".join(
        char if unicodedata.category(char)[0] in {"L", "N"} else "-" for char in label
    )
    label = re.sub(r"-+", "-", label).strip("-")
    label = label.encode("utf-8")[:64].decode("utf-8", errors="ignore").rstrip("-")
    # magpie's per-model preference keys accept ASCII provider IDs only.
    if not label.isascii():
        label = "xn--" + label.encode("punycode").decode("ascii")
    # Stable across downloads; scopes and same-name principals cannot share an auth slot.
    digest = hashlib.sha256(
        json.dumps([kind, scope, identity], separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return f"aigateway-{kind}-{label or kind}-{digest}"
