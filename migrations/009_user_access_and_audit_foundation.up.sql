CREATE TABLE public.tenant (
    id UUID PRIMARY KEY,
    code TEXT NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 160),
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX tenant_single_primary_idx
    ON public.tenant (is_primary) WHERE is_primary;

CREATE TABLE public.organization_unit (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES public.tenant(id) ON DELETE RESTRICT,
    parent_id UUID,
    code TEXT NOT NULL CHECK (length(btrim(code)) BETWEEN 1 AND 100),
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 160),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (parent_id, tenant_id)
        REFERENCES public.organization_unit(id, tenant_id) ON DELETE RESTRICT,
    CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE INDEX organization_unit_parent_idx
    ON public.organization_unit (tenant_id, parent_id, display_name);
CREATE UNIQUE INDEX organization_unit_sibling_code_idx
    ON public.organization_unit (tenant_id, COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), code);

CREATE TABLE public.app_role (
    code TEXT PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_-]{0,62}$'),
    display_name TEXT NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 100),
    is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.app_permission (
    code TEXT PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_.-]{0,126}$'),
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.app_role_permission (
    role_code TEXT NOT NULL REFERENCES public.app_role(code) ON DELETE RESTRICT,
    permission_code TEXT NOT NULL REFERENCES public.app_permission(code) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (role_code, permission_code)
);

INSERT INTO public.tenant (id, code, display_name, is_primary)
VALUES (
    '10000000-0000-4000-8000-000000000001',
    'default',
    'Default organization',
    TRUE
);

INSERT INTO public.organization_unit (id, tenant_id, parent_id, code, display_name)
VALUES (
    '10000000-0000-4000-8000-000000000002',
    '10000000-0000-4000-8000-000000000001',
    NULL,
    'root',
    'Organization'
);

INSERT INTO public.app_role (code, display_name, is_builtin)
VALUES ('owner', 'Owner', TRUE), ('member', 'Member', TRUE);

INSERT INTO public.app_permission (code, description)
VALUES
    ('platform.manage', 'Manage platform-wide settings and resources'),
    ('users.read', 'Read the user directory'),
    ('users.manage', 'Create and update users'),
    ('org.read', 'Read organization units'),
    ('org.manage', 'Create and update organization units'),
    ('roles.read', 'Read role and permission definitions'),
    ('applications.read.all', 'Read all applications'),
    ('applications.read.own', 'Read applications owned by the current user'),
    ('keys.manage.all', 'Manage all application subscription keys'),
    ('keys.manage.own', 'Manage keys for the current user applications'),
    ('usage.read.all', 'Read enterprise usage'),
    ('usage.read.own', 'Read usage owned by the current user'),
    ('reports.create.all', 'Create enterprise reports'),
    ('reports.create.own', 'Create reports for the current user'),
    ('security_audit.read', 'Read security audit events');

INSERT INTO public.app_role_permission (role_code, permission_code)
SELECT 'owner', code FROM public.app_permission;

INSERT INTO public.app_role_permission (role_code, permission_code)
VALUES
    ('member', 'applications.read.own'),
    ('member', 'keys.manage.own'),
    ('member', 'usage.read.own'),
    ('member', 'reports.create.own');

ALTER TABLE public.app_user
    DROP CONSTRAINT IF EXISTS app_user_role_check,
    ADD COLUMN tenant_id UUID,
    ADD COLUMN organization_unit_id UUID;

UPDATE public.app_user
SET tenant_id = '10000000-0000-4000-8000-000000000001';

ALTER TABLE public.app_user
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN tenant_id SET DEFAULT '10000000-0000-4000-8000-000000000001',
    ADD CONSTRAINT app_user_role_fkey
        FOREIGN KEY (role) REFERENCES public.app_role(code) ON UPDATE RESTRICT ON DELETE RESTRICT,
    ADD CONSTRAINT app_user_tenant_fkey
        FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE RESTRICT,
    ADD CONSTRAINT app_user_organization_unit_tenant_fkey
        FOREIGN KEY (organization_unit_id, tenant_id)
        REFERENCES public.organization_unit(id, tenant_id) ON DELETE RESTRICT;

CREATE INDEX app_user_tenant_role_enabled_idx
    ON public.app_user (tenant_id, role, enabled, email);
CREATE INDEX app_user_organization_unit_idx
    ON public.app_user (tenant_id, organization_unit_id) WHERE organization_unit_id IS NOT NULL;

ALTER TABLE public.gateway_application
    ADD COLUMN tenant_id UUID,
    ADD COLUMN owner_user_id UUID,
    ADD COLUMN organization_unit_id UUID;

UPDATE public.gateway_application
SET tenant_id = '10000000-0000-4000-8000-000000000001';

WITH unique_users AS (
    SELECT lower(btrim(email)) AS normalized_email, (array_agg(id))[1] AS user_id
    FROM public.app_user
    GROUP BY lower(btrim(email))
    HAVING count(*) = 1
)
UPDATE public.gateway_application AS application
SET owner_user_id = unique_users.user_id
FROM unique_users
WHERE lower(btrim(application.owner_id)) = unique_users.normalized_email;

ALTER TABLE public.gateway_application
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN tenant_id SET DEFAULT '10000000-0000-4000-8000-000000000001',
    ADD CONSTRAINT gateway_application_tenant_fkey
        FOREIGN KEY (tenant_id) REFERENCES public.tenant(id) ON DELETE RESTRICT,
    ADD CONSTRAINT gateway_application_owner_user_fkey
        FOREIGN KEY (owner_user_id) REFERENCES public.app_user(id) ON DELETE SET NULL,
    ADD CONSTRAINT gateway_application_organization_unit_tenant_fkey
        FOREIGN KEY (organization_unit_id, tenant_id)
        REFERENCES public.organization_unit(id, tenant_id) ON DELETE RESTRICT;

CREATE INDEX gateway_application_tenant_owner_idx
    ON public.gateway_application (tenant_id, owner_user_id, status);
CREATE INDEX gateway_application_organization_unit_idx
    ON public.gateway_application (tenant_id, organization_unit_id)
    WHERE organization_unit_id IS NOT NULL;

CREATE TABLE public.security_audit_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenant(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id UUID REFERENCES public.app_user(id) ON DELETE RESTRICT,
    actor_email TEXT,
    actor_role TEXT,
    action TEXT NOT NULL CHECK (length(btrim(action)) BETWEEN 1 AND 160),
    resource_type TEXT NOT NULL CHECK (length(btrim(resource_type)) BETWEEN 1 AND 100),
    resource_id TEXT,
    result TEXT NOT NULL CHECK (result IN ('success', 'failure', 'denied')),
    request_id TEXT,
    correlation_id TEXT,
    client_ip INET,
    user_agent TEXT,
    before_data JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(before_data) = 'object'),
    after_data JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(after_data) = 'object')
);

CREATE INDEX security_audit_event_tenant_time_idx
    ON public.security_audit_event (tenant_id, created_at DESC, id DESC);
CREATE INDEX security_audit_event_actor_time_idx
    ON public.security_audit_event (tenant_id, actor_user_id, created_at DESC)
    WHERE actor_user_id IS NOT NULL;
CREATE INDEX security_audit_event_action_time_idx
    ON public.security_audit_event (tenant_id, action, created_at DESC);
