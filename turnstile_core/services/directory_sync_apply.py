"""Set-based application of a complete reviewed source snapshot in one transaction."""

from __future__ import annotations

from typing import Any

from ..domain.directory import DirectoryError
from ..persistence.directory_store import DirectoryConnection


def apply_staged_directory(
    connection: DirectoryConnection,
    job: dict[str, Any],
    saved: dict[str, Any],
) -> None:
    source_key = f"entra:{saved['id']}"
    connection.execute(
        """CREATE TEMP TABLE sync_apply ON COMMIT DROP AS
           SELECT s.object_id,s.payload,s.decision,
             COALESCE((s.payload->>'person_id')::uuid,gen_random_uuid()) AS person_id,
             s.payload->>'department_id' AS department_id,
             COALESCE((s.payload->>'observed_at')::timestamptz,%s) AS observed_at
           FROM directory_sync_stage s WHERE s.job_id=%s""",
        (job["previewed_at"], job["id"]),
    )
    connection.execute(
        """CREATE UNIQUE INDEX ON sync_apply(object_id);
           CREATE INDEX ON sync_apply(person_id);
           ANALYZE sync_apply"""
    )
    invalid = connection.execute(
        """SELECT 1 FROM sync_apply s
           LEFT JOIN directory_person p ON p.id=s.person_id
           LEFT JOIN directory_unit u ON u.id=s.department_id
           LEFT JOIN directory_organization o ON o.id=u.organization_id
           WHERE s.decision NOT IN ('create','update','ignored','missing')
             OR (s.decision IN ('create','update') AND (
               u.kind IS DISTINCT FROM 'department' OR u.status IS DISTINCT FROM 'active'
               OR o.status IS DISTINCT FROM 'active' OR u.organization_id<>%s))
             OR (s.decision='update' AND (
               p.revision IS DISTINCT FROM (s.payload->>'expected_revision')::bigint))
             OR (s.decision='missing' AND NOT %s) LIMIT 1""",
        (saved["organization_id"], bool(job["summary"].get("approve_missing"))),
    ).fetchone()
    if invalid:
        raise DirectoryError("preview_stale", "Reviewed directory changes are no longer valid")
    connection.execute(
        """SELECT p.id FROM directory_person p JOIN sync_apply s ON s.person_id=p.id
           ORDER BY p.id FOR UPDATE OF p""",
    ).fetchall()
    connection.execute(
        """CREATE TEMP TABLE sync_before ON COMMIT DROP AS
           SELECT s.object_id,to_jsonb(p) AS person,b.source_disabled
           FROM sync_apply s LEFT JOIN directory_person p ON p.id=s.person_id
           LEFT JOIN directory_external_binding b ON b.person_id=s.person_id
             AND b.connection_id=%s AND b.object_type='user'""",
        (saved["id"],),
    )
    connection.execute("CREATE UNIQUE INDEX ON sync_before(object_id)")
    connection.execute(
        """ALTER TABLE sync_before ADD COLUMN memberships jsonb;
           UPDATE sync_before old SET memberships=(
             SELECT jsonb_agg(to_jsonb(m) ORDER BY m.unit_id,m.source_key)
             FROM directory_membership m JOIN sync_apply s ON s.person_id=m.person_id
             WHERE s.object_id=old.object_id AND m.valid_to IS NULL)""",
    )
    connection.execute("ANALYZE sync_before")
    connection.execute(
        """INSERT INTO directory_person(id,governance_user_id,display_name,contact_email,
             job_title,updated_by)
           SELECT person_id,payload->>'governance_user_id',payload->'profile'->>'displayName',
             payload->>'governance_user_id',COALESCE(payload->'profile'->>'jobTitle',''),
             'entra-directory-worker' FROM sync_apply WHERE decision='create'""",
    )
    connection.execute(
        """INSERT INTO directory_membership(person_id,unit_id,organization_id,
             membership_kind,source_key)
           SELECT person_id,department_id,%s,'primary_department',%s
           FROM sync_apply WHERE decision='create'""",
        (saved["organization_id"], source_key),
    )
    connection.execute(
        """INSERT INTO directory_external_binding(connection_id,cloud,tenant_id,
             object_type,object_id,person_id)
           SELECT %s,%s,%s,'user',object_id,person_id FROM sync_apply WHERE decision='create'""",
        (saved["id"], saved["cloud"], saved["tenant_id"]),
    )
    connection.execute(
        """UPDATE directory_person p SET display_name=s.payload->'profile'->>'displayName',
             contact_email=s.payload->>'governance_user_id',
             job_title=COALESCE(s.payload->'profile'->>'jobTitle',''),
             revision=p.revision+1,updated_at=now(),updated_by='entra-directory-worker'
           FROM sync_apply s WHERE s.person_id=p.id AND s.decision='update'""",
    )
    connection.execute(
        """UPDATE directory_external_binding b
           SET source_fields=CASE WHEN s.decision='missing' THEN b.source_fields ELSE s.payload END,
             source_disabled=CASE WHEN s.decision='missing' THEN TRUE
               WHEN s.decision='ignored' THEN b.source_disabled
               ELSE NOT (s.payload->'profile'->>'accountEnabled')::boolean END,
             last_seen_at=CASE WHEN s.decision='missing' THEN b.last_seen_at ELSE s.observed_at END
           FROM sync_apply s WHERE b.connection_id=%s AND b.object_type='user'
             AND b.object_id=s.object_id""",
        (saved["id"],),
    )
    connection.execute(
        """UPDATE directory_membership m SET valid_to=clock_timestamp(),revision=m.revision+1
           FROM sync_apply s WHERE m.person_id=s.person_id AND m.membership_kind='team'
             AND m.source_key=%s AND m.valid_to IS NULL AND s.decision IN ('create','update')
             AND NOT (s.payload->'team_ids' ? m.unit_id)""",
        (source_key,),
    )
    connection.execute(
        """INSERT INTO directory_membership(person_id,unit_id,organization_id,
             membership_kind,source_key)
           SELECT DISTINCT s.person_id,team.value,%s,'team',%s FROM sync_apply s
           CROSS JOIN LATERAL jsonb_array_elements_text(s.payload->'team_ids') team
           WHERE s.decision IN ('create','update') AND NOT EXISTS(
             SELECT 1 FROM directory_membership m WHERE m.person_id=s.person_id
               AND m.unit_id=team.value AND m.source_key=%s AND m.valid_to IS NULL)""",
        (saved["organization_id"], source_key, source_key),
    )
    connection.execute(
        """INSERT INTO directory_change_audit(entity_type,entity_id,organization_id,department_id,
             action,actor_label,source,before_value,after_value,reason,job_id)
           SELECT 'person',s.person_id::text,%s,COALESCE(s.department_id,m.unit_id),
             'sync_'||s.decision,'entra-directory-worker','entra',
             jsonb_build_object('person',old.person,'source_disabled',old.source_disabled,
               'memberships',old.memberships),
             jsonb_build_object('person',to_jsonb(p),'source_disabled',b.source_disabled,
               'memberships',(SELECT jsonb_agg(to_jsonb(team) ORDER BY team.unit_id,team.source_key)
                 FROM directory_membership team WHERE team.person_id=p.id
                   AND team.valid_to IS NULL)),
             %s,%s FROM sync_apply s JOIN directory_person p ON p.id=s.person_id
           LEFT JOIN sync_before old ON old.object_id=s.object_id
           LEFT JOIN directory_external_binding b ON b.connection_id=%s
             AND b.object_type='user' AND b.object_id=s.object_id
           LEFT JOIN directory_membership m ON m.person_id=p.id
             AND m.membership_kind='primary_department' AND m.valid_to IS NULL
           WHERE s.decision<>'ignored'""",
        (
            saved["organization_id"],
            f"Reviewed by {job['summary'].get('approved_label', '')}",
            job["id"],
            saved["id"],
        ),
    )
