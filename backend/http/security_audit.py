from __future__ import annotations

import csv
import json
from datetime import datetime
from io import StringIO
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from turnstile_core.domain.security_audit import AuditEventWrite
from turnstile_core.services.security_audit import AuditEventFilters

from .security_audit_support import (
    AuditService,
    OptionalAuditService,
    audit_request_context,
    record_audit_best_effort,
)
from .session import Config, SessionIdentity, require_permission

router = APIRouter()
AuditReader = Annotated[SessionIdentity, Depends(require_permission("security_audit.read"))]


class SecurityAuditEvent(BaseModel):
    id: UUID
    created_at: datetime
    actor_user_id: UUID | None
    actor_email: str | None
    actor_role: str | None
    action: str
    resource_type: str
    resource_id: str | None
    result: Literal["success", "failure", "denied"]
    request_id: str | None
    correlation_id: str | None
    client_ip: str | None
    user_agent: str | None
    before_data: dict[str, object]
    after_data: dict[str, object]


class SecurityAuditEventPage(BaseModel):
    items: list[SecurityAuditEvent]
    next_cursor: str | None


def _parse_cursor(cursor: str | None) -> UUID | None:
    if cursor is None:
        return None
    try:
        return UUID(cursor)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid audit cursor") from error


def _filters(
    from_: datetime | None,
    to: datetime | None,
    actor_id: UUID | None,
    action: str | None,
    result: Literal["success", "failure", "denied"] | None,
) -> AuditEventFilters:
    if any(bound is not None and bound.utcoffset() is None for bound in (from_, to)):
        raise HTTPException(status_code=422, detail="Audit time filters must include a timezone")
    if from_ is not None and to is not None and to <= from_:
        raise HTTPException(status_code=422, detail="to must be after from")
    return AuditEventFilters(from_=from_, to=to, actor_id=actor_id, action=action, result=result)


@router.get("/api/v1/security/audit-events", response_model=SecurityAuditEventPage)
def list_security_audit_events(
    _identity: AuditReader,
    service: AuditService,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    actor_id: UUID | None = None,
    action: Annotated[str | None, Query(max_length=160)] = None,
    result: Literal["success", "failure", "denied"] | None = None,
    cursor: Annotated[str | None, Query(max_length=36)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> SecurityAuditEventPage:
    rows = service.list_events(
        _filters(from_, to, actor_id, action, result), _parse_cursor(cursor), limit + 1
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1]["id"]) if has_more and page else None
    return SecurityAuditEventPage(
        items=[SecurityAuditEvent.model_validate(row) for row in page],
        next_cursor=next_cursor,
    )


def _csv_cell(value: object) -> str:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return f"'{text}" if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


@router.get("/api/v1/security/audit-events/export")
def export_security_audit_events(
    identity: AuditReader,
    service: AuditService,
    audit_service: OptionalAuditService,
    request: Request,
    settings: Config,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    actor_id: UUID | None = None,
    action: Annotated[str | None, Query(max_length=160)] = None,
    result: Literal["success", "failure", "denied"] | None = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 5_000,
) -> Response:
    filters = _filters(from_, to, actor_id, action, result)
    rows = service.list_events(filters, None, limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    columns = (
        "id",
        "created_at",
        "actor_user_id",
        "actor_email",
        "actor_role",
        "action",
        "resource_type",
        "resource_id",
        "result",
        "request_id",
        "correlation_id",
        "client_ip",
        "user_agent",
        "before_data",
        "after_data",
    )
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    context = audit_request_context(request, settings)
    record_audit_best_effort(
        audit_service,
        AuditEventWrite(
            action="security_audit.exported",
            result="success",
            resource_type="security_audit",
            actor_id=identity.id,
            actor_email=identity.email,
            actor_role=identity.role,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            client_ip=context.client_ip,
            user_agent=context.user_agent,
            after={"row_count": len(rows), "truncated": truncated},
        ),
    )
    headers = {
        "Content-Disposition": 'attachment; filename="turnstile_security_audit.csv"',
        "Cache-Control": "no-store",
        "X-Export-Truncated": str(truncated).lower(),
    }
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)
