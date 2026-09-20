-- Give the organization and its departments somewhere to live that is not a Python literal.
--
-- `enterprise_catalog()` returns an organization called "Contoso Global" and five departments as
-- a hardcoded tuple. Every governance screen reads it, every budget hangs off it, and no
-- deployment can change any of it: an install cannot rename the organization to its own name,
-- cannot add the department it actually has, and cannot retire one it does not.
--
-- The seed below is exactly what that function returns today, so applying this migration changes
-- nothing that anyone can observe. The point is not the values -- it is that they stop being
-- values in a source file. "Contoso Global" is seeded because removing it would delete an
-- organization that existing budget rows already point at; an install renames it on the first
-- visit to the organization screen, which is the whole reason this table exists.
--
-- `id` is the join key. Budgets (token_budget.scope_id), usage (token_usage.department_id),
-- channels (gateway_application.department_id) and enforcement (department_enforcement) all
-- reference it as text with no foreign key, and the gateway policy and the Entra app role names
-- carry the same strings outside this database entirely. So an id is written once and never
-- changed: there is no route that updates it, and a department that should no longer be used is
-- retired rather than renamed into something else. The display name carries no such weight --
-- nothing joins on it -- so it is free to change at any time.
--
-- Deleting is not offered either. A department with a year of usage behind it cannot be removed
-- without orphaning that history, and a table that lets you do it once by accident is worse than
-- one that never lets you.

CREATE TABLE public.org_unit (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    unit_type text NOT NULL CHECK (unit_type = ANY (ARRAY['organization'::text, 'department'::text])),
    parent_id text REFERENCES public.org_unit(id) ON DELETE RESTRICT,
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 120),
    status text NOT NULL DEFAULT 'active' CHECK (status = ANY (ARRAY['active'::text, 'retired'::text])),
    created_by text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT org_unit_parent_check CHECK (
        (unit_type = 'organization' AND parent_id IS NULL)
        OR (unit_type = 'department' AND parent_id IS NOT NULL)
    )
);

COMMENT ON TABLE public.org_unit IS
    'The organization and its departments. Seeded from the catalog literal this replaces; ids are immutable because four tables and two systems outside this database join on them.';

CREATE INDEX org_unit_parent_idx ON public.org_unit (parent_id, status, display_name);

CREATE TABLE public.org_unit_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_id text NOT NULL REFERENCES public.org_unit(id) ON DELETE RESTRICT,
    operation text NOT NULL CHECK (operation = ANY (
        ARRAY['created'::text, 'renamed'::text, 'retired'::text, 'restored'::text])),
    before_state jsonb,
    after_state jsonb,
    actor text NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE INDEX org_unit_audit_unit_idx ON public.org_unit_audit (unit_id, created_at DESC, id);

-- Exactly the catalog literal, so a migrated install reads the same structure it read before.
INSERT INTO public.org_unit (id, unit_type, parent_id, display_name, created_by, updated_by)
VALUES
    ('org-contoso-global', 'organization', NULL, 'Contoso Global', 'migration', 'migration'),
    ('department-platform', 'department', 'org-contoso-global', 'AI Platform', 'migration', 'migration'),
    ('department-commerce', 'department', 'org-contoso-global', 'Commerce', 'migration', 'migration'),
    ('department-finance', 'department', 'org-contoso-global', 'Finance', 'migration', 'migration'),
    ('department-support', 'department', 'org-contoso-global', 'Customer Support', 'migration', 'migration'),
    ('department-security', 'department', 'org-contoso-global', 'Security', 'migration', 'migration');
