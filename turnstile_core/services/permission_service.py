from typing import Any

from psycopg.types.json import Jsonb

from ..domain.directory import DirectoryError, DirectoryPrincipal
from ..domain.menu_permissions import MENU_CATALOG, OWNER_MENUS, PermissionPolicyWrite
from ..persistence.directory_store import DirectoryStore


class PermissionService:
    def __init__(self, store: DirectoryStore):
        self.store = store

    def read(self, principal: DirectoryPrincipal) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection:
            row = connection.execute("SELECT * FROM permission_policy WHERE singleton").fetchone()
        if row is None:
            raise DirectoryError("permission_policy_missing", "Apply permission migration", 503)
        return {
            **dict(row),
            "catalog": [
                {
                    "module": module,
                    "id": menu,
                    "label": label,
                    "source": source,
                    "owner_only": menu in OWNER_MENUS,
                    "required": menu == "user-settings",
                }
                for module, menu, label, source in MENU_CATALOG
            ],
        }

    def save(self, principal: DirectoryPrincipal, write: PermissionPolicyWrite) -> dict[str, Any]:
        principal.require_owner()
        with self.store.connection() as connection, connection.transaction():
            before = connection.execute(
                "SELECT * FROM permission_policy WHERE singleton FOR UPDATE"
            ).fetchone()
            if before is None or before["revision"] != write.expected_revision:
                raise DirectoryError(
                    "permission_revision_conflict", "Permission configuration changed", 409
                )
            normalized = {key: sorted(value) for key, value in write.groups.items()}
            if normalized != {key: sorted(value) for key, value in before["groups"].items()}:
                connection.execute(
                    """UPDATE permission_policy SET groups=%s,revision=revision+1,
                       updated_at=now(),updated_by=%s WHERE singleton""",
                    (Jsonb(write.groups), principal.email),
                )
                connection.execute(
                    """INSERT INTO permission_policy_audit(
                       revision,before_value,after_value,changed_by)
                       VALUES (%s,%s,%s,%s)""",
                    (
                        before["revision"] + 1,
                        Jsonb(before["groups"]),
                        Jsonb(write.groups),
                        principal.email,
                    ),
                )
                connection.execute(
                    "UPDATE directory_control_state SET permission_revision=permission_revision+1"
                )
        return self.read(principal)
