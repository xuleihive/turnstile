ALTER TABLE permission_policy ADD COLUMN download_groups JSONB NOT NULL DEFAULT
    '{"user":["user-plugin"],"organization_admin":["user-plugin"],"department_admin":["user-plugin"],"team_admin":["user-plugin"]}'::jsonb
    CHECK (jsonb_typeof(download_groups) = 'object');
