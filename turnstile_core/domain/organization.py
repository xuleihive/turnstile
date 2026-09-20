from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from .models import StrictModel

UnitType = Literal["organization", "department"]
UnitStatus = Literal["active", "retired"]
UNIT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_UNIT_ID = re.compile(UNIT_ID_PATTERN)


def suggested_unit_id(display_name: str, prefix: str = "department") -> str | None:
    """An id proposed from the name, or nothing when the name cannot produce one.

    Returning None rather than a mangled fallback is the point. The id is a join key that
    four tables, the gateway policy and a set of Entra app roles all carry as text, and it
    can never be changed once anything references it. A name written in a non-Latin script
    -- which is the normal case at an install whose departments are named in Chinese --
    slugifies to nothing, and inventing `department-1` on their behalf would hand them a
    permanent identifier that says nothing about what it identifies.

    So the screen asks. A suggestion is offered where one is obvious and the field is left
    for the administrator where it is not.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.strip().casefold()).strip("-")
    if not slug:
        return None
    candidate = f"{prefix}-{slug}" if prefix else slug
    return candidate[:63].rstrip("-") if _UNIT_ID.fullmatch(candidate[:63].rstrip("-")) else None


class OrgUnitReferences(StrictModel):
    """What keeps naming this unit once it stops being offered."""

    budgets: int = Field(ge=0)
    usage_records: int = Field(ge=0)
    applications: int = Field(ge=0)


class OrgUnit(StrictModel):
    id: str
    unit_type: UnitType
    parent_id: str | None
    display_name: str
    status: UnitStatus
    references: OrgUnitReferences
    updated_by: str
    updated_at: datetime


class OrganizationDirectory(StrictModel):
    organization: OrgUnit | None
    departments: list[OrgUnit]


class OrgUnitCreate(StrictModel):
    display_name: str = Field(min_length=1, max_length=120)
    id: str = Field(pattern=UNIT_ID_PATTERN)

    @field_validator("display_name")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized


class OrgUnitRename(StrictModel):
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized


class OrgUnitStatusUpdate(StrictModel):
    status: UnitStatus


class ConsoleMember(StrictModel):
    """Someone who can sign in to the console.

    Distinct from the people in the governance directory, who are there because they called
    the gateway or own an application. A colleague who signs in with Microsoft and looks at a
    report is in neither of those sets, so before this list existed they were stored in
    `app_user`, granted the member role, and shown on no screen anywhere -- including the one
    that would have explained why every edit control was missing for them.
    """

    email: str
    display_name: str | None
    role: Literal["owner", "member"]
    enabled: bool
    sign_in: Literal["password", "microsoft"]
    created_at: datetime
    last_login_at: datetime | None
    is_self: bool


class ConsoleMemberList(StrictModel):
    members: list[ConsoleMember]
    owner_count: int = Field(ge=0)


class ConsoleMemberRoleUpdate(StrictModel):
    role: Literal["owner", "member"]


class ConsoleMemberStatusUpdate(StrictModel):
    enabled: bool
