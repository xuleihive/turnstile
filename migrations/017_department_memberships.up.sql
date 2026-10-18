ALTER TABLE directory_membership DROP CONSTRAINT directory_membership_membership_kind_check;
ALTER TABLE directory_membership ADD CONSTRAINT directory_membership_membership_kind_check
    CHECK (membership_kind IN ('primary_department', 'department', 'team'));

CREATE OR REPLACE FUNCTION directory_validate_membership() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE target directory_unit; person_organization TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'directory-membership-stripe:' ||
        (hashtextextended(NEW.person_id::TEXT, 0) & 255)::TEXT, 0));
    SELECT organization_id INTO person_organization FROM directory_person
        WHERE id = NEW.person_id FOR UPDATE;
    SELECT * INTO target FROM directory_unit WHERE id = NEW.unit_id;
    IF (NEW.membership_kind IN ('primary_department', 'department') AND target.kind <> 'department')
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

CREATE INDEX directory_membership_department_idx
    ON directory_membership(unit_id, person_id) WHERE membership_kind = 'department';
