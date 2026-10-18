CREATE TABLE permission_policy (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    groups JSONB NOT NULL CHECK (jsonb_typeof(groups) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL
);
CREATE TABLE permission_policy_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revision BIGINT NOT NULL UNIQUE,
    before_value JSONB NOT NULL,
    after_value JSONB NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by TEXT NOT NULL
);
INSERT INTO permission_policy(groups, updated_by) VALUES (
    '{
      "user":["user-settings","finops-invoke"],
      "team_admin":["user-settings","finops-invoke","finops-overview","finops-analytics","finops-trends","finops-requests","assistant","pinned-report","copilot-requests","models","organization-management"],
      "department_admin":["user-settings","finops-invoke","finops-overview","finops-analytics","finops-trends","finops-requests","assistant","pinned-report","copilot-requests","finops-governance","budgets","models","organization-management"],
      "organization_admin":["user-settings","finops-invoke","finops-overview","finops-analytics","finops-trends","finops-requests","assistant","pinned-report","copilot-requests","finops-governance","budgets","models","organization-management"]
    }'::jsonb, 'migration_018'
);
CREATE FUNCTION permission_audit_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Permission policy audit is immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER permission_policy_audit_immutable
    BEFORE UPDATE OR DELETE ON permission_policy_audit
    FOR EACH ROW EXECUTE FUNCTION permission_audit_immutable();

-- Additional department and cross-department team members can hold exact appointments.
CREATE OR REPLACE VIEW directory_effective_menu_administrator AS
SELECT DISTINCT g.id,g.app_user_id,g.organization_id,g.unit_id,g.scope_kind,
    g.scope_kind||'_admin' AS menu_group
FROM directory_menu_administrator g
JOIN app_user a ON a.id=g.app_user_id AND a.enabled
JOIN directory_account_link l ON l.app_user_id=a.id
JOIN directory_person p ON p.id=l.person_id AND p.status='active' AND NOT p.manual_disabled
JOIN directory_organization o ON o.id=g.organization_id AND o.status='active'
LEFT JOIN directory_unit u ON u.id=g.unit_id AND u.status='active'
WHERE p.organization_id=g.organization_id
 AND g.valid_from<=now() AND (g.valid_to IS NULL OR g.valid_to>now())
 AND NOT EXISTS(SELECT 1 FROM directory_external_binding e WHERE e.person_id=p.id AND e.source_disabled)
 AND (g.scope_kind='organization' OR (
   u.kind=g.scope_kind
   AND (g.scope_kind<>'team' OR EXISTS(
     SELECT 1 FROM directory_unit d WHERE d.id=u.parent_unit_id AND d.status='active'))
   AND EXISTS(SELECT 1 FROM directory_membership m WHERE m.person_id=p.id AND m.unit_id=u.id
     AND m.valid_from<=now() AND (m.valid_to IS NULL OR m.valid_to>now())
     AND ((g.scope_kind='department' AND m.membership_kind IN ('primary_department','department'))
       OR (g.scope_kind='team' AND m.membership_kind='team')))
 ));
UPDATE directory_control_state SET permission_revision=permission_revision+1;
