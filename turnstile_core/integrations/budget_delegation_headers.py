"""ASCII transport for authoritative Unicode metadata on trusted backend requests."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

DELEGATION_ENCODING_HEADER = "x-turnstile-metadata-encoding"
DELEGATION_METADATA_HEADERS = (
    "x-user-id", "x-user-name", "x-org-id", "x-org-name",
    "x-department-id", "x-department-name", "x-project-id", "x-project-name",
    "x-agent-id", "x-agent-name", "x-hive-organization", "x-hive-department",
    "x-hive-project", "x-hive-agent", "x-hive-user", "x-hive-workflow",
    "x-hive-run-id", "x-hive-model", "x-hive-runtime",
)


def encode_delegation_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    result = dict(headers)
    values = [result.get(name, "") for name in DELEGATION_METADATA_HEADERS]
    if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values):
        raise ValueError("Delegated metadata contains invalid header characters")
    if any(not value.isascii() for value in values):
        for name in DELEGATION_METADATA_HEADERS:
            if name in result:
                result[name] = quote(result[name], safe="")
        result[DELEGATION_ENCODING_HEADER] = "percent-v1"
    return result
