ALTER TABLE directory_person ADD COLUMN organization_id TEXT
    REFERENCES directory_organization(id) ON DELETE RESTRICT;
UPDATE directory_person p SET organization_id = m.organization_id
FROM directory_membership m WHERE m.person_id = p.id
    AND m.membership_kind = 'primary_department'
    AND m.valid_from <= now() AND (m.valid_to IS NULL OR m.valid_to > now());
CREATE INDEX directory_person_organization_idx ON directory_person(organization_id, status, id);

CREATE OR REPLACE FUNCTION directory_validate_membership() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target directory_unit; person_organization TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'directory-membership-stripe:' ||
        (hashtextextended(NEW.person_id::TEXT, 0) & 255)::TEXT, 0));
    SELECT organization_id INTO person_organization FROM directory_person
        WHERE id = NEW.person_id FOR UPDATE;
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
    IF NEW.valid_to IS NULL THEN
        IF person_organization IS NULL THEN
            UPDATE directory_person SET organization_id = target.organization_id
                WHERE id = NEW.person_id;
        ELSIF person_organization <> target.organization_id THEN
            RAISE EXCEPTION 'Membership must belong to the person organization'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION directory_person_organization_guard() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.organization_id IS NOT NULL AND NEW.organization_id IS DISTINCT FROM OLD.organization_id
    THEN RAISE EXCEPTION 'Person organization is immutable' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER directory_person_organization_immutable BEFORE UPDATE ON directory_person
FOR EACH ROW EXECUTE FUNCTION directory_person_organization_guard();

CREATE OR REPLACE VIEW directory_effective_menu_administrator AS
SELECT DISTINCT g.id, g.app_user_id, g.organization_id, g.unit_id, g.scope_kind,
    g.scope_kind || '_admin' AS menu_group
FROM directory_menu_administrator g
JOIN app_user a ON a.id = g.app_user_id AND a.enabled
JOIN directory_account_link l ON l.app_user_id = a.id
JOIN directory_person p ON p.id = l.person_id AND p.status = 'active' AND NOT p.manual_disabled
    AND p.organization_id = g.organization_id
JOIN directory_organization o ON o.id = g.organization_id AND o.status = 'active'
LEFT JOIN directory_unit u ON u.id = g.unit_id AND u.status = 'active'
WHERE g.valid_from <= now() AND (g.valid_to IS NULL OR g.valid_to > now())
    AND NOT EXISTS(SELECT 1 FROM directory_external_binding e
        WHERE e.person_id = p.id AND e.source_disabled)
    AND (
        g.scope_kind = 'organization'
        OR (g.scope_kind = 'department' AND u.kind = 'department' AND EXISTS(
            SELECT 1 FROM directory_membership m WHERE m.person_id = p.id
                AND m.unit_id = u.id AND m.membership_kind = 'primary_department'
                AND m.valid_from <= now() AND (m.valid_to IS NULL OR m.valid_to > now())
        ))
        OR (g.scope_kind = 'team' AND u.kind = 'team' AND EXISTS(
            SELECT 1 FROM directory_unit d WHERE d.id = u.parent_unit_id AND d.status = 'active'
        ) AND EXISTS(
            SELECT 1 FROM directory_membership tm WHERE tm.person_id = p.id
                AND tm.unit_id = u.id AND tm.membership_kind = 'team'
                AND tm.valid_from <= now() AND (tm.valid_to IS NULL OR tm.valid_to > now())
        ))
    );

-- Retain old records but stop future transfers after the workflow is retired.
UPDATE directory_transfer SET status = 'cancelled', completed_at = now(),
    conflict_code = 'workflow_retired' WHERE status IN ('scheduled', 'conflicted');
UPDATE directory_control_state SET permission_revision = permission_revision + 1;
