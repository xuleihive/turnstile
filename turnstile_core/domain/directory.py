from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from email.headerregistry import Address
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .data_access import DataAccessScope
from .menu_permissions import MenuPermissionGroup, menu_permissions

DirectoryStatus = Literal["active", "inactive", "archived"]
AdministratorScopeKind = Literal["organization", "department", "team"]
DirectoryCapability = Literal["directory.read", "directory.edit_people", "directory.edit_teams"]
Cloud = Literal["public", "usgov", "china"]
DIRECTORY_PROTOCOL_VERSION = 1


class DirectoryError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class DirectoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MenuAdministratorScope(DirectoryModel):
    scope_kind: AdministratorScopeKind
    scope_id: str
    organization_id: str
    scope_name: str


class DirectoryPrincipal(DirectoryModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    account_id: UUID | None = None
    email: str
    role: Literal["owner", "member", "system"] = "member"
    department_ids: tuple[str, ...] | None = None
    capabilities: tuple[DirectoryCapability, ...] = ()
    department_capabilities: dict[str, tuple[DirectoryCapability, ...]] | None = None
    permission_revision: int = 0
    menu_permission_group: MenuPermissionGroup = "user"
    menu_permission_groups: tuple[MenuPermissionGroup, ...] = ("user",)
    menu_administrator_scopes: tuple[MenuAdministratorScope, ...] = ()
    data_scope: DataAccessScope | None = None
    permission_policy: dict[str, list[str]] | None = None

    def is_menu_administrator(self, scope_kind: AdministratorScopeKind, scope_id: str) -> bool:
        return any(
            item.scope_kind == scope_kind and item.scope_id == scope_id
            for item in self.menu_administrator_scopes
        )

    def menu_permissions_for_scope(
        self, scope_kind: AdministratorScopeKind, scope_id: str
    ) -> tuple[str, ...]:
        scope_groups: dict[AdministratorScopeKind, MenuPermissionGroup] = {
            "organization": "organization_admin", "department": "department_admin",
            "team": "team_admin",
        }
        groups: tuple[MenuPermissionGroup, ...] = (
            (scope_groups[scope_kind],)
            if self.is_menu_administrator(scope_kind, scope_id) else ("user",)
        )
        return menu_permissions(groups, owner=self.owner, policy=self.permission_policy)

    @property
    def menu_permissions(self) -> tuple[str, ...]:
        return menu_permissions(
            self.menu_permission_groups, owner=self.owner, policy=self.permission_policy,
        )

    @property
    def owner(self) -> bool:
        return self.role == "owner"

    def require_owner(self) -> None:
        if not self.owner:
            raise DirectoryError("owner_required", "Owner role is required", 403)

    def require(self, capability: DirectoryCapability, department_id: str | None) -> None:
        if self.owner:
            return
        if (
            department_id is None
            or self.department_ids is None
            or department_id not in self.department_ids
            or capability not in (
                self.department_capabilities.get(department_id, ())
                if self.department_capabilities is not None else self.capabilities
            )
        ):
            raise DirectoryError("scope_not_found", "Directory scope not found", 404)


class DirectoryWrite(DirectoryModel):
    expected_revision: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=2000)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        return clean_text(value, allow_newline=True)


class OrganizationWrite(DirectoryWrite):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    contact_email: str | None = Field(default=None, max_length=320)
    status: DirectoryStatus = "active"

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: DirectoryStatus) -> DirectoryStatus:
        return "archived" if value == "inactive" else value

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ValueError("Code must contain ASCII letters, numbers, underscores or hyphens")
        return value

    @field_validator("name")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return clean_text(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return clean_text(value, allow_newline=True)

    @field_validator("contact_email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return normalized_email(value) if value else None


class UnitWrite(OrganizationWrite):
    kind: Literal["department", "team"]
    parent_unit_id: str | None = Field(default=None, max_length=255)


class PersonWrite(DirectoryWrite):
    display_name: str = Field(min_length=1, max_length=160)
    contact_email: str | None = Field(default=None, max_length=320)
    employee_number: str | None = Field(default=None, max_length=64)
    job_title: str = Field(default="", max_length=160)
    menu_permission_group: MenuPermissionGroup | None = None
    menu_permission_groups: list[MenuPermissionGroup] | None = Field(default=None, max_length=3)

    @field_validator("menu_permission_groups")
    @classmethod
    def normalize_menu_groups(
        cls, values: list[MenuPermissionGroup] | None
    ) -> list[MenuPermissionGroup] | None:
        if values is None:
            return None
        groups = sorted(set(values))
        if "user" in groups and len(groups) > 1:
            raise ValueError("Ordinary User cannot be combined with administrator groups")
        return groups or ["user"]

    @model_validator(mode="after")
    def one_menu_representation(self) -> PersonWrite:
        if self.menu_permission_group is not None and self.menu_permission_groups is not None:
            raise ValueError("Provide either menu_permission_groups or the legacy single group")
        return self

    @property
    def requested_menu_groups(self) -> list[MenuPermissionGroup] | None:
        if self.menu_permission_groups is not None:
            return self.menu_permission_groups
        return [self.menu_permission_group] if self.menu_permission_group is not None else None

    @field_validator("display_name", "employee_number", "job_title")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return clean_text(value) if value is not None else None

    @field_validator("contact_email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return normalized_email(value) if value else None


class PersonCreate(PersonWrite):
    governance_user_id: str = Field(min_length=3, max_length=255)
    department_id: str = Field(min_length=1, max_length=255)
    team_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("governance_user_id")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return normalized_email(value)


class TeamsWrite(DirectoryWrite):
    team_ids: list[str] = Field(default_factory=list, max_length=200)


class OrganizationPersonCreate(DirectoryWrite):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    organization_id: str = Field(min_length=1, max_length=255)
    department_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=255)
    password: SecretStr
    employee_number: str | None = Field(default=None, max_length=64)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return normalized_email(value)

    @field_validator("display_name", "employee_number", "organization_id", "department_id")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return clean_text(value) if value is not None else None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if not 12 <= len(value.get_secret_value()) <= 256:
            raise ValueError("Password must contain between 12 and 256 characters")
        return value


class UnitMembersWrite(DirectoryWrite):
    person_ids: list[UUID] = Field(min_length=1, max_length=100)


class AccountLinkWrite(DirectoryWrite):
    app_user_id: UUID


class AdministratorsWrite(DirectoryWrite):
    account_ids: list[UUID] = Field(default_factory=list, max_length=100)


class PersonStatusWrite(DirectoryWrite):
    status: DirectoryStatus
    disable_account: bool = False

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: DirectoryStatus) -> DirectoryStatus:
        return "archived" if value == "inactive" else value


class TransferWrite(DirectoryWrite):
    target_department_id: str = Field(min_length=1, max_length=255)
    effective_month: date
    preview_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("effective_month")
    @classmethod
    def first_day(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("Transfer month must start on the first day")
        return value


class TransferCancelWrite(DirectoryModel):
    reason: str = Field(default="", max_length=2000)


class ObservationResolveWrite(DirectoryModel):
    person_id: UUID | None = None
    ignore: bool = False
    reason: str = Field(default="", max_length=2000)


class ConnectionWrite(DirectoryWrite):
    organization_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=160)
    cloud: Cloud = "public"
    tenant_id: UUID
    client_id: UUID | None = None
    authentication_mode: Literal["managed_identity", "key_vault_secret"] = "managed_identity"
    credential_ref: str | None = Field(default=None, max_length=512)
    scope: Literal["selected_groups", "all_users"] = "selected_groups"
    default_department_id: str | None = Field(default=None, max_length=255)
    include_guests: bool = False
    enabled: bool = False
    sync_interval_minutes: int = Field(default=60, ge=15, le=1440)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return clean_text(value)


class GroupMapping(DirectoryModel):
    group_id: UUID
    unit_id: str = Field(min_length=1, max_length=255)
    membership_mode: Literal["direct", "transitive"] = "direct"
    enabled: bool = True


class GroupMappingsWrite(DirectoryWrite):
    items: list[GroupMapping] = Field(default_factory=list, max_length=200)


class ExternalBindingWrite(DirectoryWrite):
    person_id: UUID
    job_id: UUID
    bind_login_identity: bool = False


class SyncJobWrite(DirectoryModel):
    mode: Literal["full", "delta"] = "full"


class SyncApplyWrite(DirectoryModel):
    expected_directory_version: int = Field(ge=0)
    approve_missing: bool = False
    approve_mass_changes: bool = False


class DirectoryPage(DirectoryModel):
    items: list[dict[str, Any]]
    total: int
    next_cursor: str | None = None
    generated_at: datetime


def clean_text(value: str, *, allow_newline: bool = False) -> str:
    if any(
        unicodedata.category(char).startswith("C") and not (allow_newline and char in "\n\t")
        for char in value
    ):
        raise ValueError("Text must not contain control characters")
    return value.strip()


def normalized_email(value: str) -> str:
    value = value.strip().casefold()
    if (
        len(value) > 320
        or value.count("@") != 1
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError("A valid email-shaped identity is required")
    local, domain = value.rsplit("@", 1)
    try:
        parsed = Address(addr_spec=value)
    except ValueError as error:
        raise ValueError("A valid email-shaped identity is required") from error
    if parsed.username != local or parsed.domain != domain:
        raise ValueError("A valid email-shaped identity is required")
    if not local or len(local) > 64 or not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ValueError("A valid email-shaped identity is required")
    if domain.startswith((".", "-")) or domain.endswith((".", "-")) or ".." in domain:
        raise ValueError("Email domain is invalid")
    return value
