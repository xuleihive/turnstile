CREATE TABLE public.usage_export_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenant(id),
    creator_user_id UUID NOT NULL REFERENCES public.app_user(id),
    from_ts TIMESTAMPTZ NOT NULL,
    to_ts TIMESTAMPTZ NOT NULL,
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'ready', 'failed', 'expired')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    row_count BIGINT CHECK (row_count IS NULL OR row_count >= 0),
    blob_name TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    error_code TEXT,
    idempotency_key VARCHAR(128),
    request_hash CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_ts < to_ts),
    CHECK ((idempotency_key IS NULL) = (request_hash IS NULL))
);

CREATE UNIQUE INDEX usage_export_job_idempotency_idx
    ON public.usage_export_job (tenant_id, creator_user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX usage_export_job_active_user_idx
    ON public.usage_export_job (tenant_id, creator_user_id, status, created_at DESC)
    WHERE status IN ('queued', 'running');

CREATE INDEX usage_export_job_lease_idx
    ON public.usage_export_job (status, lease_until, created_at)
    WHERE status IN ('queued', 'running');

CREATE INDEX usage_export_job_retention_idx
    ON public.usage_export_job (expires_at)
    WHERE status IN ('ready', 'failed');
