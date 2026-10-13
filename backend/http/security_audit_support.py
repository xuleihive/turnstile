from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, Request

from turnstile_core.config import Settings, get_settings
from turnstile_core.domain.access_control import RequestAuditContext
from turnstile_core.domain.security_audit import AuditEventWrite, resolve_client_ip
from turnstile_core.persistence.audit_store import AuditStore
from turnstile_core.services.security_audit import SecurityAuditService

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_security_audit_store() -> AuditStore:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for security audit")
    return AuditStore(settings.database_url)


def get_security_audit_service() -> SecurityAuditService:
    return SecurityAuditService(get_security_audit_store())


def get_optional_security_audit_service() -> SecurityAuditService | None:
    if not get_settings().database_url:
        return None
    try:
        return get_security_audit_service()
    except Exception as error:
        logger.warning(
            "Security audit service unavailable: error_type=%s",
            type(error).__name__,
        )
        return None


AuditService = Annotated[SecurityAuditService, Depends(get_security_audit_service)]
OptionalAuditService = Annotated[
    SecurityAuditService | None, Depends(get_optional_security_audit_service)
]


def audit_request_context(request: Request, settings: Settings) -> RequestAuditContext:
    def stable_identifier(header_name: str) -> str:
        value = request.headers.get(header_name, "")
        try:
            return str(UUID(value))
        except (ValueError, AttributeError):
            return str(uuid4())

    return RequestAuditContext(
        request_id=stable_identifier("x-request-id"),
        correlation_id=stable_identifier("x-correlation-id"),
        client_ip=resolve_client_ip(
            peer_ip=request.client.host if request.client else None,
            headers=request.headers,
            trust_azure_arr_client_ip=settings.trust_azure_arr_client_ip,
            trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
        ),
        user_agent=request.headers.get("user-agent", "")[:500] or None,
    )


def record_audit_best_effort(
    service: SecurityAuditService | None,
    event: AuditEventWrite,
    *,
    connection: object | None = None,
) -> None:
    if service is None:
        return
    try:
        service.record(event, connection=connection)
    except Exception as error:
        # Never log exception text: database drivers may include bound parameters.
        logger.warning(
            "Security audit persistence failed: action=%s error_type=%s",
            event.action,
            type(error).__name__,
        )
