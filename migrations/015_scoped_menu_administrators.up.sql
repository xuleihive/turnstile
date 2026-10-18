-- Administrator appointments control menus, not department data grants or APIM admission.
CREATE TABLE directory_menu_administrator (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_user_id UUID NOT NULL REFERENCES app_user(id),
    organization_id TEXT NOT NULL REFERENCES directory_organization(id),
    unit_id TEXT REFERENCES directory_unit(id),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('organization', 'department', 'team')),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    granted_by TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    CHECK ((scope_kind = 'organization') = (unit_id IS NULL)),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE UNIQUE INDEX directory_menu_administrator_active_idx
    ON directory_menu_administrator(
        scope_kind, organization_id, COALESCE(unit_id, ''), app_user_id
    ) WHERE valid_to IS NULL;
CREATE INDEX directory_menu_administrator_account_idx
    ON directory_menu_administrator(app_user_id) WHERE valid_to IS NULL;

CREATE FUNCTION directory_validate_menu_administrator() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.unit_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM directory_unit u WHERE u.id = NEW.unit_id
            AND u.organization_id = NEW.organization_id AND u.kind = NEW.scope_kind
    ) THEN
        RAISE EXCEPTION 'Administrator scope hierarchy is invalid' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER directory_menu_administrator_hierarchy
    BEFORE INSERT OR UPDATE ON directory_menu_administrator
    FOR EACH ROW EXECUTE FUNCTION directory_validate_menu_administrator();

CREATE VIEW directory_effective_menu_administrator AS
SELECT DISTINCT g.id, g.app_user_id, g.organization_id, g.unit_id, g.scope_kind,
    g.scope_kind || '_admin' AS menu_group
FROM directory_menu_administrator g
JOIN app_user a ON a.id = g.app_user_id AND a.enabled
JOIN directory_account_link l ON l.app_user_id = a.id
JOIN directory_person p ON p.id = l.person_id AND p.status = 'active' AND NOT p.manual_disabled
JOIN directory_membership m ON m.person_id = p.id AND m.membership_kind = 'primary_department'
    AND m.organization_id = g.organization_id
    AND m.valid_from <= now() AND (m.valid_to IS NULL OR m.valid_to > now())
JOIN directory_unit d ON d.id = m.unit_id AND d.kind = 'department' AND d.status = 'active'
JOIN directory_organization o ON o.id = g.organization_id AND o.status = 'active'
LEFT JOIN directory_unit u ON u.id = g.unit_id AND u.status = 'active'
WHERE g.valid_from <= now() AND (g.valid_to IS NULL OR g.valid_to > now())
    AND NOT EXISTS(SELECT 1 FROM directory_external_binding e
        WHERE e.person_id = p.id AND e.source_disabled)
    AND (
        g.scope_kind = 'organization'
        OR (g.scope_kind = 'department' AND u.id = d.id)
        OR (g.scope_kind = 'team' AND u.parent_unit_id = d.id AND EXISTS(
            SELECT 1 FROM directory_membership tm WHERE tm.person_id = p.id
                AND tm.unit_id = u.id AND tm.membership_kind = 'team'
                AND tm.valid_from <= now() AND (tm.valid_to IS NULL OR tm.valid_to > now())
        ))
    );

-- Recover only genuine, active legacy department appointments. The old team panel
-- reused a department ID; neither team membership nor that UI can prove a team appointment.
INSERT INTO directory_menu_administrator(
    app_user_id, organization_id, unit_id, scope_kind, granted_by, source
)
SELECT DISTINCT ON (g.app_user_id, u.id)
    g.app_user_id, u.organization_id, u.id, 'department', g.granted_by, 'migration_015'
FROM directory_department_grant g
JOIN directory_unit u ON u.id = g.department_id AND u.kind = 'department' AND u.status = 'active'
JOIN directory_organization o ON o.id = u.organization_id AND o.status = 'active'
JOIN app_user a ON a.id = g.app_user_id AND a.enabled
JOIN directory_account_link l ON l.app_user_id = a.id
JOIN directory_person p ON p.id = l.person_id AND p.status = 'active' AND NOT p.manual_disabled
JOIN directory_membership m ON m.person_id = p.id AND m.unit_id = u.id
    AND m.membership_kind = 'primary_department'
    AND m.valid_from <= now() AND (m.valid_to IS NULL OR m.valid_to > now())
WHERE g.valid_from <= now() AND (g.valid_to IS NULL OR g.valid_to > now())
    AND NOT EXISTS(SELECT 1 FROM directory_external_binding e
        WHERE e.person_id = p.id AND e.source_disabled)
ORDER BY g.app_user_id, u.id, g.valid_from DESC, g.id;

UPDATE directory_control_state SET permission_revision = permission_revision + 1;
