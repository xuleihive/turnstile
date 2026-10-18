"""Keep client-reported tracing separate from server-authoritative identity."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException


def client_telemetry_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    aliases = {
        "x-agent-name": ("x-agent-name", "x-hive-agent", "x-magpie-agent"),
        "x-agent-id": ("x-agent-id",),
        "x-hive-workflow": ("x-hive-workflow",),
        "x-hive-run-id": (
            "x-hive-run-id", "x-magpie-session", "x-opencode-session", "x-session-affinity",
            "x-session-id", "session_id", "session-id", "x-claude-code-session-id",
        ),
        "x-hive-turn-index": ("x-hive-turn-index",),
    }
    for target, names in aliases.items():
        value = next((headers.get(name, "") for name in names if headers.get(name)), "")
        if not value:
            continue
        if len(value) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise HTTPException(400, "Invalid client tracing header")
        if target == "x-hive-turn-index" and (
            not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 2**31 - 1
        ):
            raise HTTPException(400, "Invalid client tracing turn index")
        result[target] = value
    if "x-agent-name" in result:
        result["x-hive-agent"] = result["x-agent-name"]
        result.setdefault("x-agent-id", "agent-" + result["x-agent-name"].lower().replace(" ", "-"))
    return result
