-- Preserve the earlier single-group field for old clients; arrays are now authoritative.
ALTER TABLE directory_person
    ADD COLUMN menu_permission_groups TEXT[] NOT NULL DEFAULT ARRAY['user']::TEXT[]
    CHECK (
        cardinality(menu_permission_groups) BETWEEN 1 AND 3
        AND menu_permission_groups <@ ARRAY[
            'user', 'organization_admin', 'department_admin', 'team_admin'
        ]::TEXT[]
        AND (NOT 'user' = ANY(menu_permission_groups) OR cardinality(menu_permission_groups) = 1)
    );

UPDATE directory_person SET menu_permission_groups = ARRAY[menu_permission_group];

COMMENT ON COLUMN directory_person.menu_permission_groups IS
    'Local navigation groups only; menu union never grants API data scope or APIM access.';
