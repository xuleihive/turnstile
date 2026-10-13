from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from math import isfinite
from typing import Any, Literal

AuditResult = Literal["success", "failure", "denied"]
IPAddress = IPv4Address | IPv6Address

SAFE_AUDIT_METADATA_FIELDS = frozenset(
    {
        "id",
        "email",
        "display_name",
        "role",
        "enabled",
        "organization_unit_id",
        "parent_id",
        "code",
        "subscription_id",
        "application_id",
        "key_type",
        "export_job_id",
        "from",
        "to",
        "row_count",
        "permission",
        "reason_code",
    }
)


@dataclass(frozen=True)
class AuditEventWrite:
    action: str
    result: AuditResult
    resource_type: str
    resource_id: str | None = None
    actor_id: str | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    before: Mapping[str, Any] = field(default_factory=dict)
    after: Mapping[str, Any] = field(default_factory=dict)


def sanitize_audit_metadata(value: Mapping[str, Any]) -> dict[str, str | int | float | bool | None]:
    safe: dict[str, str | int | float | bool | None] = {}
    for key, item in value.items():
        if key not in SAFE_AUDIT_METADATA_FIELDS:
            continue
        if (
            item is None
            or isinstance(item, bool | int)
            or isinstance(item, float)
            and isfinite(item)
        ):
            safe[key] = item
        elif isinstance(item, str):
            safe[key] = item[:512]
    return safe


def _parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate or "," in candidate:
        return None
    try:
        return ip_address(candidate)
    except ValueError:
        return None


def resolve_client_ip(
    *,
    peer_ip: str | None,
    headers: Mapping[str, str],
    trust_azure_arr_client_ip: bool = False,
    trusted_proxy_cidrs: Sequence[str] = (),
) -> str | None:
    """Resolve a client IP using only explicitly trusted ingress metadata."""
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    peer = _parse_ip(peer_ip)

    if trust_azure_arr_client_ip:
        arr_ip = _parse_ip(normalized_headers.get("x-arr-clientip"))
        if arr_ip is not None:
            return str(arr_ip)

    if peer is None:
        return None

    try:
        trusted_networks = [ip_network(value, strict=False) for value in trusted_proxy_cidrs]
    except ValueError:
        trusted_networks = []

    def is_trusted(address: IPAddress) -> bool:
        return any(
            address.version == network.version and address in network
            for network in trusted_networks
        )

    if not is_trusted(peer):
        return str(peer)

    forwarded = normalized_headers.get("x-forwarded-for", "")
    if len(forwarded) > 2048:
        return str(peer)
    forwarded_ips = [_parse_ip(item) for item in forwarded.split(",") if item.strip()]
    if not forwarded_ips or any(item is None for item in forwarded_ips):
        return str(peer)

    for candidate in reversed([*forwarded_ips, peer]):
        assert candidate is not None
        if not is_trusted(candidate):
            return str(candidate)
    return str(peer)
