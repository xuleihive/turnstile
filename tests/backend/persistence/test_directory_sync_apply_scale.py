from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.persistence.directory_store import DirectoryStore
from turnstile_core.services.directory_sync_apply import apply_staged_directory


def test_reviewed_snapshot_apply_preserves_complete_audit_at_100000_people(
    directory_database: str,
    record_property: Any,
) -> None:
    store = DirectoryStore(directory_database)
    store.initialize_empty()
    connection_id, job_id, tenant_id = uuid4(), uuid4(), uuid4()
    try:
        with store.connection() as connection:
            connection.execute(
                """INSERT INTO directory_organization(id,code,name,updated_by)
                   VALUES ('org-scale-sync','scale-sync','Scale Sync','test');
                   INSERT INTO directory_unit(id,organization_id,kind,code,name,updated_by)
                   SELECT 'department-sync-'||n,'org-scale-sync','department',
                     'sync-'||n,'Sync Department '||n,'test' FROM generate_series(0,99) n"""
            )
            connection.execute(
                """INSERT INTO directory_connection(id,organization_id,name,tenant_id,updated_by)
                   VALUES (%s,'org-scale-sync','Scale fixture',%s,'test')""",
                (connection_id, tenant_id),
            )
            connection.execute(
                """INSERT INTO directory_sync_job(
                     id,connection_id,connection_revision,directory_version,mode,
                     requested_by,idempotency_key,status,previewed_at)
                   VALUES (%s,%s,1,1,'full','test','scale-fixture','applying',now())""",
                (job_id, connection_id),
            )
            connection.execute(
                """INSERT INTO directory_sync_stage(job_id,object_id,decision,payload)
                   SELECT %s,md5('sync-scale-'||n)::uuid,'create',jsonb_build_object(
                     'governance_user_id','sync-scale-'||n||'@example.com',
                     'department_id','department-sync-'||(n%%100),
                     'team_ids','[]'::jsonb,
                     'observed_at',now(),
                     'profile',jsonb_build_object(
                       'displayName','Scale Person '||n,'accountEnabled',true,'jobTitle',''))
                   FROM generate_series(0,99999) n""",
                (job_id,),
            )
        started = time.perf_counter()
        with store.connection() as connection, connection.transaction():
            connection.execute("SET LOCAL statement_timeout='20s'")
            apply_staged_directory(
                connection,
                {
                    "id": job_id,
                    "previewed_at": datetime.now(UTC),
                    "summary": {"approved_label": "test"},
                },
                {
                    "id": connection_id,
                    "organization_id": "org-scale-sync",
                    "cloud": "public",
                    "tenant_id": tenant_id,
                },
            )
        duration = time.perf_counter() - started
        record_property("directory_sync_apply_100000_seconds", round(duration, 4))
        with store.connection() as connection:
            for table in ("directory_person", "directory_membership", "directory_external_binding"):
                row = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
                assert row and row["n"] == 100_000
            audit = connection.execute(
                """SELECT count(*) AS n FROM directory_change_audit WHERE job_id=%s
                   AND action='sync_create' AND after_value->'person' IS NOT NULL
                   AND jsonb_array_length(after_value->'memberships')=1""",
                (job_id,),
            ).fetchone()
            assert audit and audit["n"] == 100_000
            assert not connection.execute("SELECT 1 FROM token_budget").fetchone()
            assert not connection.execute("SELECT 1 FROM app_user").fetchone()
    finally:
        store.close()
