"""Approved one-time directory backfill without changing billing identities."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from ..config import core_runtime_digest
from ..domain.directory import DirectoryError
from ..domain.enterprise import enterprise_catalog, merge_application_owners, merge_observed_users
from ..persistence.directory_store import DirectoryConnection, DirectoryStore
from ..persistence.repository_contract import QueryRepository
from .directory_service import json_value

_PRESERVED_TABLES = (
    "app_user",
    "user_session",
    "token_budget",
    "user_model_policy",
    "user_model_access",
    "token_usage",
    "billable_request_attempt",
    "budget_reservation_admission",
    "budget_reservation_finalization",
    "gateway_application",
    "token_usage_application_attribution",
    "budget_reservation_recovery",
    "budget_legacy_usage",
    "token_budget_audit",
    "user_model_access_audit",
    "department_enforcement",
    "department_enforcement_audit",
    "budget_evidence_policy",
    "gateway_application_budget",
    "gateway_application_model_policy",
    "gateway_application_model_access",
)
_DIRECTORY_TABLES = (
    "directory_organization",
    "directory_unit",
    "directory_person",
    "directory_membership",
    "directory_account_link",
    "directory_department_grant",
    "directory_project_reference",
    "directory_agent_reference",
    "directory_observation_candidate",
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(json_value(value), sort_keys=True).encode()).hexdigest()


def billing_baseline(
    connection: DirectoryConnection,
    tables: tuple[str, ...] = _PRESERVED_TABLES,
) -> dict[str, Any]:
    baseline: dict[str, Any] = {}
    for table in tables:
        checksum = hashlib.sha256()
        count = 0
        with connection.cursor(name=f"directory_baseline_{uuid4().hex}") as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT md5(to_jsonb(item)::TEXT) AS row_digest FROM {} item "
                    "ORDER BY row_digest"
                ).format(sql.Identifier(table))
            )
            for row in cursor:
                checksum.update(str(row["row_digest"]).encode("ascii"))
                count += 1
        baseline[table] = {"count": count, "digest": checksum.hexdigest()}
    return baseline


class DirectoryUpgrade:
    def __init__(self, store: DirectoryStore, repository: QueryRepository) -> None:
        self.store = store
        self.repository = repository

    def configure_admission(
        self,
        evidence: dict[str, Any],
        *,
        approved: bool,
        actor: str,
    ) -> None:
        if not approved:
            raise DirectoryError("approval_required", "Approve admission-path evidence first")
        with self.store.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE"
            ).fetchone()
            if state is None or state["source"] != "database":
                raise DirectoryError(
                    "directory_not_ready",
                    "Activate the directory before admission configuration",
                    503,
                )
            try:
                mode = evidence["person_admission_mode"]
                age = datetime.now(UTC) - datetime.fromisoformat(evidence["captured_at"])
                policy_hash = evidence["apim_policy_fingerprint"]
                if (
                    mode not in {"bff_only", "identity_v1"}
                    or not timedelta(0) <= age <= timedelta(minutes=15)
                    or evidence["instance_digest"] != digest(str(state["instance_id"]))
                    or evidence["expected_directory_version"] != state["active_version"]
                    or evidence["api_directory_admission_protocol"] != 1
                    or evidence["core_digest"] != core_runtime_digest()
                    or len(policy_hash) != 64
                    or any(char not in "0123456789abcdef" for char in policy_hash)
                ):
                    raise ValueError("Incomplete admission evidence")
                if mode == "bff_only":
                    if evidence["employee_token_enabled"] is not False:
                        raise ValueError("Legacy employee token paths are still enabled")
                elif (
                    evidence["directory_identity_policy_protocol"] != 1
                    or evidence["identity_gateway_probes_verified"] is not True
                    or state["projected_version"] < state["active_version"]
                ):
                    raise ValueError("Dynamic employee admission is not verified")
            except (KeyError, TypeError, ValueError) as error:
                raise DirectoryError(
                    "admission_evidence_invalid",
                    "Current API and employee admission-path evidence is required",
                ) from error
            if state["person_admission_mode"] == mode:
                return
            version = state["active_version"] + 1
            connection.execute(
                """UPDATE directory_control_state SET person_admission_mode=%s,
                     active_version=%s,permission_revision=permission_revision+1,updated_at=now()""",
                (mode, version),
            )
            connection.execute(
                """INSERT INTO directory_outbox(entity_id,directory_version)
                   VALUES ('admission-mode',%s)""",
                (version,),
            )
            connection.execute(
                """INSERT INTO directory_change_audit(entity_type,entity_id,action,actor_label,
                     source,after_value)
                   VALUES ('control','admission-mode','configured',%s,'operator',%s)""",
                (actor, Jsonb({"mode": mode, "policy_fingerprint": policy_hash})),
            )

    def _legacy_catalog(self) -> Any:
        return merge_application_owners(
            merge_observed_users(enterprise_catalog(), list(self.repository.observed_users())),
            list(self.repository.application_owners()),
        )

    def plan(self) -> dict[str, Any]:
        state = self.store.state()
        if state["source"] != "legacy" or state["phase"] == "active":
            raise DirectoryError("already_active", "An active directory must not be reinitialized")
        observed = list(self.repository.observed_users())
        owners = list(self.repository.application_owners())
        seed = enterprise_catalog()
        catalog = merge_application_owners(merge_observed_users(seed, observed), owners)
        with self.store.connection() as connection:
            baseline = billing_baseline(connection)
            migrations = connection.execute(
                "SELECT version,checksum FROM schema_migration ORDER BY version"
            ).fetchall()
            user_references = {
                str(row["user_id"])
                for row in connection.execute(
                    """SELECT user_id FROM user_model_policy
                       UNION SELECT scope_id FROM token_budget WHERE scope_type='user'
                       UNION SELECT user_id FROM token_usage WHERE usage_domain='apim'"""
                ).fetchall()
            }
            accounts = connection.execute(
                "SELECT id,email,role FROM app_user WHERE enabled ORDER BY email"
            ).fetchall()
            budgets = connection.execute(
                "SELECT scope_type,scope_id,parent_scope_id FROM token_budget ORDER BY period_start"
            ).fetchall()
        owner_emails = {str(row["email"]).casefold() for row in owners}
        known_users = {row.id for row in catalog.users}
        people = [
            {
                "governance_user_id": row.id,
                "display_name": row.name,
                "department_id": row.parent_id,
                "provenance": "owner_default"
                if row.id.casefold() in owner_emails and row.id not in user_references
                else "legacy_catalog",
            }
            for row in catalog.users
            if row.id in user_references or row.id.casefold() in owner_emails
        ]
        organizations = [row.model_dump() for row in catalog.organizations]
        departments = [row.model_dump() for row in catalog.departments]
        org_ids = {row["id"] for row in organizations}
        department_ids = {row["id"] for row in departments}
        candidates: list[dict[str, Any]] = []
        for row in budgets:
            if row["scope_type"] == "organization" and row["scope_id"] not in org_ids:
                org_ids.add(row["scope_id"])
                organizations.append(
                    {
                        "id": row["scope_id"],
                        "name": row["scope_id"],
                        "parent_id": None,
                        "status": "archived",
                    }
                )
            if row["scope_type"] == "department" and row["scope_id"] not in department_ids:
                department_ids.add(row["scope_id"])
                parent = row["parent_scope_id"]
                if parent not in org_ids:
                    org_ids.add(parent)
                    organizations.append(
                        {
                            "id": parent,
                            "name": parent,
                            "parent_id": None,
                            "status": "archived",
                        }
                    )
                departments.append(
                    {
                        "id": row["scope_id"],
                        "name": row["scope_id"],
                        "parent_id": parent,
                        "status": "archived",
                    }
                )
        for user_id in sorted(user_references - known_users):
            candidates.append(
                {
                    "original_user_id": user_id,
                    "source": "legacy_upgrade",
                    "status": "pending",
                }
            )
        links = [
            {"governance_user_id": row["email"], "app_user_id": str(row["id"])}
            for row in accounts
            if row["role"] == "owner"
            and row["email"] in {person["governance_user_id"] for person in people}
        ]
        plan = {
            "protocol_version": 1,
            "core_digest": core_runtime_digest(),
            "legacy_source_digest": digest(catalog.model_dump(mode="json")),
            "instance_digest": digest(str(state["instance_id"])),
            "migration_digest": digest(migrations),
            "billing_baseline": baseline,
            "organizations": organizations,
            "departments": departments,
            "people": people,
            "projects": [row.model_dump() for row in catalog.projects],
            "agents": [row.model_dump() for row in catalog.agents],
            "account_links": links,
            "candidates": candidates,
            "approval_required": True,
        }
        return json_value(plan)

    def _validate_plan(self, connection: DirectoryConnection, plan: dict[str, Any]) -> None:
        state = connection.execute("SELECT * FROM directory_control_state FOR UPDATE").fetchone()
        if state is None or state["source"] != "legacy":
            raise DirectoryError("already_active", "An active directory must not be reinitialized")
        if digest(str(state["instance_id"])) != plan.get("instance_digest"):
            raise DirectoryError("target_mismatch", "The plan belongs to another database")
        if plan.get("protocol_version") != 1 or plan.get("core_digest") != core_runtime_digest():
            raise DirectoryError("runtime_drift", "Directory runtime changed after preview")
        if plan.get("legacy_source_digest") != digest(
            self._legacy_catalog().model_dump(mode="json")
        ):
            raise DirectoryError("source_drift", "Legacy directory source changed after preview")
        migrations = connection.execute(
            "SELECT version,checksum FROM schema_migration ORDER BY version"
        ).fetchall()
        if digest(migrations) != plan.get("migration_digest"):
            raise DirectoryError("migration_drift", "Migration chain changed after preview")
        if billing_baseline(connection) != plan.get("billing_baseline"):
            raise DirectoryError("data_drift", "Billing or account data changed after preview")

    def _stage(self, plan: dict[str, Any]) -> UUID:
        plan_digest = digest(plan)
        with self.store.connection() as connection, connection.transaction():
            self._validate_plan(connection, plan)
            prior = connection.execute(
                "SELECT id FROM directory_upgrade_run WHERE plan_digest=%s",
                (plan_digest,),
            ).fetchone()
            if prior:
                return UUID(str(prior["id"]))
            if connection.execute("SELECT 1 FROM directory_upgrade_run LIMIT 1").fetchone():
                raise DirectoryError(
                    "another_upgrade", "Another approved upgrade is already present"
                )
            run_id = uuid4()
            connection.execute(
                "INSERT INTO directory_upgrade_run(id,plan_digest,plan) VALUES (%s,%s,%s)",
                (run_id, plan_digest, Jsonb(plan)),
            )
            for kind in (
                "organizations",
                "departments",
                "people",
                "projects",
                "agents",
                "account_links",
                "candidates",
            ):
                for index, entity in enumerate(plan[kind]):
                    key = (
                        entity.get("id")
                        or entity.get("governance_user_id")
                        or entity.get("original_user_id")
                    )
                    connection.execute(
                        """INSERT INTO directory_upgrade_stage(
                             run_id,entity_type,stable_key,payload)
                           VALUES (%s,%s,%s,%s)""",
                        (run_id, kind, str(key or index), Jsonb(entity)),
                    )
            connection.execute("UPDATE directory_control_state SET phase='importing'")
            return run_id

    def apply(self, plan: dict[str, Any], *, approved: bool, actor: str) -> str:
        if not approved:
            raise DirectoryError("approval_required", "Approve the saved directory plan first")
        run_id = self._stage(plan)
        with self.store.connection() as connection, connection.transaction():
            self._validate_plan(connection, plan)
            run = connection.execute(
                "SELECT * FROM directory_upgrade_run WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            assert run is not None
            if run["materialized"]:
                return str(run_id)
            plan_digest = digest(plan)
            for entity in plan["organizations"]:
                connection.execute(
                    """INSERT INTO directory_organization(id,code,name,status,updated_by)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (
                        entity["id"],
                        f"org-{digest(entity['id'])[:16]}",
                        entity["name"],
                        entity.get("status", "active"),
                        actor,
                    ),
                )
            for entity in plan["departments"]:
                connection.execute(
                    """INSERT INTO directory_unit(
                         id,organization_id,kind,code,name,status,updated_by)
                       VALUES (%s,%s,'department',%s,%s,%s,%s)""",
                    (
                        entity["id"],
                        entity["parent_id"],
                        f"dept-{digest(entity['id'])[:16]}",
                        entity["name"],
                        entity.get("status", "active"),
                        actor,
                    ),
                )
            for person in plan["people"]:
                row = connection.execute(
                    """INSERT INTO directory_person(
                         governance_user_id,display_name,contact_email,updated_by)
                       VALUES (%s,%s,%s,%s) RETURNING id""",
                    (
                        person["governance_user_id"],
                        person["display_name"],
                        person["governance_user_id"],
                        actor,
                    ),
                ).fetchone()
                assert row is not None
                connection.execute(
                    """INSERT INTO directory_membership(
                         person_id,unit_id,organization_id,membership_kind,source_key)
                       SELECT %s,id,organization_id,'primary_department','legacy_upgrade'
                       FROM directory_unit WHERE id=%s""",
                    (row["id"], person["department_id"]),
                )
            for project in plan["projects"]:
                connection.execute(
                    """INSERT INTO directory_project_reference(id,name,department_id)
                       VALUES (%s,%s,%s)""",
                    (project["id"], project["name"], project["parent_id"]),
                )
            for agent in plan["agents"]:
                connection.execute(
                    "INSERT INTO directory_agent_reference(id,name,project_id) VALUES (%s,%s,%s)",
                    (agent["id"], agent["name"], agent["parent_id"]),
                )
            for link in plan["account_links"]:
                connection.execute(
                    """INSERT INTO directory_account_link(person_id,app_user_id,verified_by)
                       SELECT id,%s,%s FROM directory_person WHERE governance_user_id=%s""",
                    (UUID(link["app_user_id"]), actor, link["governance_user_id"]),
                )
            for candidate in plan["candidates"]:
                connection.execute(
                    """INSERT INTO directory_observation_candidate(
                         original_user_id,source,status) VALUES (%s,%s,%s)""",
                    (candidate["original_user_id"], candidate["source"], candidate["status"]),
                )
            connection.execute(
                """INSERT INTO directory_change_audit(
                     entity_type,entity_id,action,actor_label,source,after_value)
                   VALUES ('upgrade',%s,'imported',%s,'legacy_upgrade',%s)""",
                (str(run_id), actor, Jsonb({"plan_digest": plan_digest})),
            )
            connection.execute(
                """UPDATE directory_upgrade_run SET materialized=TRUE,
                   materialized_digest=%s,updated_at=now() WHERE id=%s""",
                (digest(billing_baseline(connection, _DIRECTORY_TABLES)), run_id),
            )
            return str(run_id)

    def verify(self, run_id: UUID) -> dict[str, Any]:
        with self.store.connection() as connection, connection.transaction():
            connection.execute("SELECT singleton FROM directory_control_state FOR UPDATE")
            run = connection.execute(
                "SELECT * FROM directory_upgrade_run WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if run is None:
                raise DirectoryError("upgrade_not_found", "Upgrade run not found", 404)
            preserved = billing_baseline(connection) == run["plan"]["billing_baseline"]
            missing = connection.execute(
                """SELECT count(*) AS count FROM directory_person p
                   WHERE NOT EXISTS(SELECT 1 FROM directory_membership m
                     WHERE m.person_id=p.id AND m.membership_kind='primary_department')"""
            ).fetchone()
            directory_preserved = (
                digest(billing_baseline(connection, _DIRECTORY_TABLES))
                == run["materialized_digest"]
            )
            valid = (
                preserved
                and directory_preserved
                and run["materialized"]
                and run["plan"]["core_digest"] == core_runtime_digest()
                and missing is not None
                and missing["count"] == 0
            )
            if valid and run["phase"] != "active":
                connection.execute(
                    """UPDATE directory_upgrade_run SET phase='verified',updated_at=now()
                       WHERE id=%s""",
                    (run_id,),
                )
                connection.execute("UPDATE directory_control_state SET phase='verified'")
            return {"run_id": str(run_id), "verified": valid, "billing_preserved": preserved}

    @staticmethod
    def _runtime_gate(plan: dict[str, Any], manifest: dict[str, Any] | None) -> None:
        if manifest is None:
            raise DirectoryError(
                "runtime_manifest_required", "Supply reviewed three-package maintenance evidence"
            )
        try:
            captured = datetime.fromisoformat(manifest["captured_at"])
            age = datetime.now(UTC) - captured
            if (
                captured.tzinfo is None
                or not timedelta(0) <= age <= timedelta(minutes=15)
                or manifest["instance_digest"] != plan["instance_digest"]
                or manifest["protocol_version"] != 1
                or manifest["backup_verified"] is not True
                or manifest["maintenance_confirmed"] is not True
                or set(manifest["applications"]) != {"api", "telemetry", "control-plane"}
            ):
                raise ValueError("Maintenance evidence is incomplete")
            for target, evidence in manifest["applications"].items():
                if (
                    evidence["state"] != "Stopped"
                    or evidence["core_digest"] != core_runtime_digest()
                    or evidence["target"] != target
                    or evidence["configured_source"] != "database"
                    or evidence["configured_core_digest"] != evidence["core_digest"]
                    or len(evidence["package_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in evidence["package_sha256"])
                ):
                    raise ValueError("Mounted packages or settings do not match")
        except (KeyError, ValueError, TypeError) as error:
            raise DirectoryError(
                "runtime_manifest_invalid",
                "Three matching stopped packages, backup and configuration evidence are required",
            ) from error

    def activate(self, run_id: UUID, *, runtime_manifest: dict[str, Any] | None = None) -> None:
        with self.store.connection() as connection, connection.transaction():
            state = connection.execute(
                "SELECT * FROM directory_control_state FOR UPDATE"
            ).fetchone()
            run = connection.execute(
                "SELECT * FROM directory_upgrade_run WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if run is None:
                raise DirectoryError("upgrade_not_found", "Upgrade run not found", 404)
            if run["phase"] == "active":
                return
            if state is None or state["phase"] != "verified" or run["phase"] != "verified":
                raise DirectoryError("upgrade_unverified", "Verify the imported directory first")
            self._runtime_gate(run["plan"], runtime_manifest)
            if billing_baseline(connection) != run["plan"]["billing_baseline"]:
                raise DirectoryError(
                    "data_drift", "Billing or account data changed after verification"
                )
            if (
                digest(billing_baseline(connection, _DIRECTORY_TABLES))
                != run["materialized_digest"]
            ):
                raise DirectoryError(
                    "directory_drift", "Imported directory changed after verification"
                )
            connection.execute(
                """UPDATE directory_control_state SET source='database',phase='active',
                   active_version=active_version+1,updated_at=now()"""
            )
            connection.execute(
                "UPDATE directory_upgrade_run SET phase='active',updated_at=now() WHERE id=%s",
                (run_id,),
            )
