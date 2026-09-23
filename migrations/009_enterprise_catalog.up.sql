-- The organization catalog: organizations and departments configured by an Owner or by
-- an integration, replacing the seeded demonstration catalog.
--
-- An empty table keeps the seeded catalog, so an existing deployment is unchanged until
-- something writes here. Writes replace the whole catalog in one transaction, so readers
-- never see half of an update.
--
-- `external_ref` records where an entity came from in the writer's own system -- for
-- example the Microsoft Entra group whose members form a business unit -- and
-- `attributes` carries small descriptive values such as a tier. Neither is interpreted by
-- Turnstile; both are returned as written.
CREATE TABLE IF NOT EXISTS enterprise_entity (
    entity_type text NOT NULL CHECK (entity_type IN ('organization', 'department')),
    entity_id text NOT NULL CHECK (length(entity_id) BETWEEN 1 AND 200),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    parent_id text,
    is_default boolean NOT NULL DEFAULT false,
    external_ref text CHECK (external_ref IS NULL OR length(external_ref) <= 400),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    position integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    PRIMARY KEY (entity_type, entity_id),
    CHECK ((entity_type = 'organization') = (parent_id IS NULL)),
    CHECK (entity_type = 'department' OR NOT is_default)
);

-- At most one default department, where Owners are listed before they generate traffic.
CREATE UNIQUE INDEX IF NOT EXISTS enterprise_entity_one_default
    ON enterprise_entity ((true)) WHERE is_default;
