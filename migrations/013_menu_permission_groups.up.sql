-- Local application menus are independent of account roles and APIM authorization.
-- Existing people, including legacy department administrators, default to ordinary users.
ALTER TABLE directory_person
    ADD COLUMN menu_permission_group TEXT NOT NULL DEFAULT 'user'
    CHECK (menu_permission_group IN
        ('user', 'organization_admin', 'department_admin', 'team_admin'));

COMMENT ON COLUMN directory_person.menu_permission_group IS
    'Local navigation group only. Does not grant Owner, department scope, models or APIM access.';
