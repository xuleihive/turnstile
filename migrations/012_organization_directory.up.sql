CREATE TABLE directory_control_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    instance_id UUID NOT NULL DEFAULT gen_random_uuid(),
    protocol_version INTEGER NOT NULL DEFAULT 1 CHECK (protocol_version = 1),
    source TEXT NOT NULL DEFAULT 'legacy' CHECK (source IN ('legacy', 'database')),
    phase TEXT NOT NULL DEFAULT 'schema_only'
        CHECK (phase IN ('schema_only', 'importing', 'verified', 'active', 'fresh_empty_ready')),
    active_version BIGINT NOT NULL DEFAULT 0 CHECK (active_version >= 0),
    permission_revision BIGINT NOT NULL DEFAULT 0 CHECK (permission_revision >= 0),
    projection_sequence BIGINT NOT NULL DEFAULT 0 CHECK (projection_sequence >= 0),
    projected_sequence BIGINT NOT NULL DEFAULT 0 CHECK (projected_sequence >= 0),
    projected_version BIGINT NOT NULL DEFAULT 0 CHECK (projected_version >= 0),
    projected_at TIMESTAMPTZ,
    person_admission_mode TEXT NOT NULL DEFAULT 'unverified'
        CHECK (person_admission_mode IN ('unverified','bff_only','identity_v1')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO directory_control_state(singleton) VALUES (TRUE);

CREATE TABLE directory_organization (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 255),
    code TEXT NOT NULL UNIQUE CHECK (code = lower(code) AND code ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 160),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
    contact_email TEXT CHECK (length(contact_email) <= 320),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'archived')),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL
);

CREATE TABLE directory_unit (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 255),
    organization_id TEXT NOT NULL REFERENCES directory_organization(id) ON DELETE RESTRICT,
    parent_unit_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('department', 'team')),
    code TEXT NOT NULL CHECK (code = lower(code) AND code ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 160),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'archived')),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL,
    UNIQUE (id, organization_id),
    UNIQUE (organization_id, code),
    FOREIGN KEY (parent_unit_id, organization_id)
        REFERENCES directory_unit(id, organization_id) ON DELETE RESTRICT,
    CHECK ((kind = 'department' AND parent_unit_id IS NULL)
        OR (kind = 'team' AND parent_unit_id IS NOT NULL AND parent_unit_id <> id))
);
CREATE INDEX directory_unit_parent_idx ON directory_unit(organization_id, parent_unit_id, status);

CREATE TABLE directory_person (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    governance_user_id TEXT NOT NULL UNIQUE
        CHECK (length(governance_user_id) BETWEEN 3 AND 255 AND governance_user_id LIKE '%@%'),
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 160),
    contact_email TEXT CHECK (length(contact_email) <= 320),
    employee_number TEXT CHECK (length(employee_number) <= 64),
    job_title TEXT NOT NULL DEFAULT '' CHECK (length(job_title) <= 160),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'archived')),
    manual_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL
);
CREATE INDEX directory_person_name_idx ON directory_person(lower(display_name), id);
CREATE INDEX directory_person_contact_idx ON directory_person(lower(contact_email));

CREATE TABLE directory_membership (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES directory_person(id) ON DELETE RESTRICT,
    unit_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    membership_kind TEXT NOT NULL CHECK (membership_kind IN ('primary_department', 'team')),
    source_key TEXT NOT NULL DEFAULT 'manual',
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    FOREIGN KEY (unit_id, organization_id)
        REFERENCES directory_unit(id, organization_id) ON DELETE RESTRICT,
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX directory_membership_person_idx
    ON directory_membership(person_id, membership_kind, valid_from, valid_to);
CREATE INDEX directory_membership_unit_idx
    ON directory_membership(unit_id, valid_from, valid_to, person_id);

CREATE TABLE directory_account_link (
    person_id UUID PRIMARY KEY REFERENCES directory_person(id) ON DELETE RESTRICT,
    app_user_id UUID NOT NULL UNIQUE REFERENCES app_user(id) ON DELETE RESTRICT,
    verified_by TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE directory_department_grant (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
    department_id TEXT NOT NULL REFERENCES directory_unit(id) ON DELETE RESTRICT,
    capabilities TEXT[] NOT NULL DEFAULT ARRAY[
        'directory.read', 'directory.edit_people', 'directory.edit_teams'
    ]::TEXT[],
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    granted_by TEXT NOT NULL,
    CHECK (capabilities <@ ARRAY[
        'directory.read', 'directory.edit_people', 'directory.edit_teams'
    ]::TEXT[]),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX directory_grant_account_idx
    ON directory_department_grant(app_user_id, department_id, valid_to);

CREATE TABLE directory_change_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    organization_id TEXT,
    department_id TEXT,
    action TEXT NOT NULL,
    actor_account_id UUID,
    actor_label TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    before_value JSONB,
    after_value JSONB,
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 2000),
    request_id TEXT,
    job_id UUID,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX directory_audit_scope_idx
    ON directory_change_audit(department_id, changed_at DESC, id);

CREATE TABLE directory_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id TEXT NOT NULL,
    directory_version BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'leased', 'completed', 'failed')),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE(entity_id, directory_version)
);
CREATE INDEX directory_outbox_ready_idx ON directory_outbox(status, available_at);

CREATE TABLE directory_projection_run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    directory_version BIGINT NOT NULL,
    projection_sequence BIGINT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','completed','failed','superseded')),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    cursor BIGINT NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX directory_projection_one_open_idx ON directory_projection_run((TRUE))
    WHERE status IN ('queued','running','failed');
CREATE TABLE directory_projection_stage (
    ordinal BIGINT GENERATED ALWAYS AS IDENTITY,
    run_id UUID NOT NULL REFERENCES directory_projection_run(id) ON DELETE RESTRICT,
    cloud TEXT NOT NULL CHECK (cloud IN ('public','usgov','china')),
    tenant_id UUID NOT NULL,
    object_id UUID NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY(run_id,cloud,tenant_id,object_id),
    UNIQUE(run_id,ordinal)
);

CREATE TABLE directory_project_reference (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    department_id TEXT NOT NULL REFERENCES directory_unit(id) ON DELETE RESTRICT
);
CREATE TABLE directory_agent_reference (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES directory_project_reference(id) ON DELETE RESTRICT
);

CREATE TABLE directory_transfer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES directory_person(id) ON DELETE RESTRICT,
    source_department_id TEXT NOT NULL REFERENCES directory_unit(id) ON DELETE RESTRICT,
    target_department_id TEXT NOT NULL REFERENCES directory_unit(id) ON DELETE RESTRICT,
    effective_month DATE NOT NULL CHECK (extract(day FROM effective_month) = 1),
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'completed', 'conflicted', 'cancelled')),
    expected_person_revision BIGINT NOT NULL,
    approved_future_budgets JSONB NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    conflict_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (source_department_id <> target_department_id)
);
CREATE UNIQUE INDEX directory_transfer_one_open_idx
    ON directory_transfer(person_id) WHERE status = 'scheduled';

CREATE TABLE directory_invocation_admission (
    request_id TEXT PRIMARY KEY CHECK (length(request_id) BETWEEN 1 AND 255),
    governance_user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    department_id TEXT NOT NULL,
    directory_version BIGINT NOT NULL,
    admitted_at TIMESTAMPTZ NOT NULL,
    period_start DATE NOT NULL CHECK (extract(day FROM period_start)=1)
);
CREATE INDEX directory_invocation_person_period_idx
    ON directory_invocation_admission(governance_user_id,period_start,admitted_at);
CREATE FUNCTION directory_admission_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Directory admission attribution is immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER directory_admission_guard BEFORE UPDATE OR DELETE ON directory_invocation_admission
FOR EACH ROW EXECUTE FUNCTION directory_admission_immutable();
CREATE TABLE directory_invocation_terminal (
    request_id TEXT PRIMARY KEY REFERENCES directory_invocation_admission(request_id) ON DELETE RESTRICT,
    completed_at TIMESTAMPTZ NOT NULL
);
CREATE TRIGGER directory_terminal_guard BEFORE UPDATE OR DELETE ON directory_invocation_terminal
FOR EACH ROW EXECUTE FUNCTION directory_admission_immutable();

CREATE TABLE directory_connection (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id TEXT NOT NULL REFERENCES directory_organization(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 160),
    cloud TEXT NOT NULL DEFAULT 'public' CHECK (cloud IN ('public', 'usgov', 'china')),
    tenant_id UUID NOT NULL,
    client_id UUID,
    credential_ref TEXT,
    authentication_mode TEXT NOT NULL DEFAULT 'managed_identity'
        CHECK (authentication_mode IN ('managed_identity', 'key_vault_secret')),
    scope TEXT NOT NULL DEFAULT 'selected_groups' CHECK (scope IN ('selected_groups', 'all_users')),
    default_department_id TEXT REFERENCES directory_unit(id) ON DELETE RESTRICT,
    include_guests BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    sync_interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK (sync_interval_minutes BETWEEN 15 AND 1440),
    revision BIGINT NOT NULL DEFAULT 1,
    validated_revision BIGINT,
    validated_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL,
    CHECK ((authentication_mode = 'managed_identity' AND credential_ref IS NULL)
        OR (authentication_mode = 'key_vault_secret' AND client_id IS NOT NULL
            AND credential_ref IS NOT NULL)),
    CHECK (scope <> 'all_users' OR default_department_id IS NOT NULL)
);
CREATE TABLE directory_group_mapping (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES directory_connection(id) ON DELETE RESTRICT,
    group_id UUID NOT NULL,
    unit_id TEXT NOT NULL REFERENCES directory_unit(id) ON DELETE RESTRICT,
    membership_mode TEXT NOT NULL DEFAULT 'direct' CHECK (membership_mode IN ('direct', 'transitive')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    revision BIGINT NOT NULL DEFAULT 1,
    UNIQUE(connection_id, group_id)
);
CREATE TABLE directory_external_binding (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES directory_connection(id) ON DELETE RESTRICT,
    cloud TEXT NOT NULL,
    tenant_id UUID NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type IN ('user', 'group')),
    object_id UUID NOT NULL,
    person_id UUID REFERENCES directory_person(id) ON DELETE RESTRICT,
    unit_id TEXT REFERENCES directory_unit(id) ON DELETE RESTRICT,
    source_fields JSONB NOT NULL DEFAULT '{}',
    source_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(cloud, tenant_id, object_type, object_id),
    CHECK ((object_type = 'user' AND person_id IS NOT NULL AND unit_id IS NULL)
        OR (object_type = 'group' AND unit_id IS NOT NULL AND person_id IS NULL))
);
CREATE TABLE directory_identity_alias (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES directory_person(id) ON DELETE RESTRICT,
    cloud TEXT NOT NULL DEFAULT 'public',
    tenant_id UUID NOT NULL,
    alias_type TEXT NOT NULL CHECK (alias_type IN ('mail', 'upn')),
    value TEXT NOT NULL CHECK (value = lower(value) AND length(value) <= 320),
    verified_by TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ
);
CREATE UNIQUE INDEX directory_verified_alias_idx
    ON directory_identity_alias(cloud, tenant_id, alias_type, value) WHERE valid_to IS NULL;
CREATE TABLE app_user_external_identity (
    cloud TEXT NOT NULL DEFAULT 'public',
    tenant_id UUID NOT NULL,
    object_id UUID NOT NULL,
    app_user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(cloud, tenant_id, object_id)
);
CREATE INDEX app_user_external_account_idx ON app_user_external_identity(app_user_id);

CREATE TABLE directory_sync_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES directory_connection(id) ON DELETE RESTRICT,
    connection_revision BIGINT NOT NULL,
    directory_version BIGINT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('full', 'delta')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'awaiting_review', 'applying',
            'succeeded', 'failed', 'cancelled', 'conflicted')),
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    summary JSONB NOT NULL DEFAULT '{}',
    checkpoint_ciphertext BYTEA,
    previewed_at TIMESTAMPTZ,
    preview_expires_at TIMESTAMPTZ,
    read_progress_ciphertext BYTEA,
    analysis_cursor UUID,
    snapshot_completed_at TIMESTAMPTZ,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX directory_sync_one_open_idx ON directory_sync_job(connection_id)
    WHERE status IN ('queued', 'running', 'awaiting_review', 'applying');
CREATE INDEX directory_sync_queued_idx ON directory_sync_job(status, created_at);
CREATE TABLE directory_sync_stage (
    job_id UUID NOT NULL REFERENCES directory_sync_job(id) ON DELETE RESTRICT,
    object_id UUID NOT NULL,
    payload JSONB NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending'
        CHECK (decision IN ('pending', 'create', 'update', 'conflict', 'ignored', 'missing')),
    conflict_code TEXT,
    PRIMARY KEY(job_id, object_id)
);
CREATE TABLE directory_sync_source (
    job_id UUID NOT NULL REFERENCES directory_sync_job(id) ON DELETE RESTRICT,
    object_id UUID NOT NULL,
    payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(job_id,object_id)
);
CREATE INDEX directory_sync_source_email_idx ON directory_sync_source(
    job_id,lower(COALESCE(NULLIF(payload->'profile'->>'mail',''),
      payload->'profile'->>'userPrincipalName'))
);
CREATE TABLE directory_sync_checkpoint (
    connection_id UUID NOT NULL REFERENCES directory_connection(id) ON DELETE RESTRICT,
    resource TEXT NOT NULL,
    rule_revision BIGINT NOT NULL,
    checkpoint_ciphertext BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(connection_id, resource)
);
CREATE TABLE directory_observation_candidate (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_user_id TEXT NOT NULL,
    organization_id TEXT,
    department_id TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'linked', 'ignored', 'conflict')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(original_user_id, source)
);
CREATE TABLE directory_upgrade_run (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_digest TEXT NOT NULL UNIQUE,
    plan JSONB NOT NULL,
    materialized BOOLEAN NOT NULL DEFAULT FALSE,
    materialized_digest TEXT,
    phase TEXT NOT NULL DEFAULT 'importing' CHECK (phase IN ('importing', 'verified', 'active')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE directory_upgrade_stage (
    run_id UUID NOT NULL REFERENCES directory_upgrade_run(id) ON DELETE RESTRICT,
    entity_type TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY(run_id, entity_type, stable_key)
);
CREATE TABLE directory_idempotency (
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
    request_digest TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(actor_id, operation, key)
);

ALTER TABLE assistant_conversation ADD COLUMN directory_scope TEXT[];
ALTER TABLE pinned_report ADD COLUMN directory_scope TEXT[];

CREATE FUNCTION directory_validate_unit() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND
       (NEW.id, NEW.organization_id, NEW.parent_unit_id, NEW.kind)
       IS DISTINCT FROM (OLD.id, OLD.organization_id, OLD.parent_unit_id, OLD.kind) THEN
        RAISE EXCEPTION 'Directory unit identity and hierarchy are immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.kind = 'team' AND NOT EXISTS (
        SELECT 1 FROM directory_unit
        WHERE id = NEW.parent_unit_id AND organization_id = NEW.organization_id
          AND kind = 'department'
    ) THEN
        RAISE EXCEPTION 'Team parent must be a department' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER directory_unit_guard BEFORE INSERT OR UPDATE ON directory_unit
FOR EACH ROW EXECUTE FUNCTION directory_validate_unit();

CREATE FUNCTION directory_validate_membership() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target directory_unit; primary_unit TEXT;
BEGIN
    -- Bound advisory-lock cardinality for large reviewed imports; row locks remain per person.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'directory-membership-stripe:' ||
        (hashtextextended(NEW.person_id::TEXT, 0) & 255)::TEXT, 0));
    PERFORM 1 FROM directory_person WHERE id = NEW.person_id FOR UPDATE;
    SELECT * INTO target FROM directory_unit WHERE id = NEW.unit_id;
    IF (NEW.membership_kind = 'primary_department' AND target.kind <> 'department')
        OR (NEW.membership_kind = 'team' AND target.kind <> 'team') THEN
        RAISE EXCEPTION 'Membership kind does not match unit kind' USING ERRCODE = '23514';
    END IF;
    IF NEW.membership_kind = 'primary_department' AND EXISTS (
        SELECT 1 FROM directory_membership m
        WHERE m.person_id = NEW.person_id AND m.membership_kind = 'primary_department'
          AND m.id <> NEW.id
          AND tstzrange(m.valid_from, m.valid_to, '[)')
              && tstzrange(NEW.valid_from, NEW.valid_to, '[)')
    ) THEN
        RAISE EXCEPTION 'Primary department intervals overlap' USING ERRCODE = '23514';
    END IF;
    IF NEW.membership_kind = 'team' AND NEW.valid_to IS NULL THEN
        SELECT unit_id INTO primary_unit FROM directory_membership
        WHERE person_id = NEW.person_id AND membership_kind = 'primary_department'
          AND valid_from <= NEW.valid_from AND (valid_to IS NULL OR valid_to > NEW.valid_from);
        IF primary_unit IS DISTINCT FROM target.parent_unit_id THEN
            RAISE EXCEPTION 'Team membership must belong to the primary department'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER directory_membership_guard BEFORE INSERT OR UPDATE ON directory_membership
FOR EACH ROW EXECUTE FUNCTION directory_validate_membership();

CREATE FUNCTION directory_immutable_identity() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.governance_user_id IS DISTINCT FROM OLD.governance_user_id
    THEN RAISE EXCEPTION 'Person governance identity is immutable' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER directory_person_identity_guard BEFORE UPDATE ON directory_person
FOR EACH ROW EXECUTE FUNCTION directory_immutable_identity();

CREATE FUNCTION directory_audit_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Directory audit is append-only' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER directory_audit_guard BEFORE UPDATE OR DELETE ON directory_change_audit
FOR EACH ROW EXECUTE FUNCTION directory_audit_immutable();
