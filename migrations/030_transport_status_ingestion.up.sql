ALTER TABLE public.token_usage DROP CONSTRAINT token_usage_status_code_check;
ALTER TABLE public.token_usage ADD CONSTRAINT token_usage_status_code_check CHECK (
    status_code BETWEEN 100 AND 599 OR (
        status_code = 0 AND status = '0' AND estimated
        AND ingest_error IS NOT NULL AND usd_cost IS NULL
    )
) NOT VALID;

CREATE TABLE usage_ingestion_failure (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    body BYTEA NOT NULL,
    body_sha256 BYTEA GENERATED ALWAYS AS (sha256(body)) STORED UNIQUE,
    error_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION reject_usage_ingestion_failure_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Usage ingestion failure evidence is immutable';
END;
$$;

CREATE TRIGGER usage_ingestion_failure_immutable
BEFORE UPDATE OR DELETE ON usage_ingestion_failure
FOR EACH ROW EXECUTE FUNCTION reject_usage_ingestion_failure_mutation();
