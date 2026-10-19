ALTER TABLE public.managed_model_price
    DROP CONSTRAINT managed_model_price_source_check,
    DROP CONSTRAINT managed_model_price_reference_required,
    DROP CONSTRAINT managed_model_price_sync_status_check,
    ALTER COLUMN price_reference TYPE TEXT,
    ADD COLUMN match_metadata JSONB,
    ADD COLUMN source_snapshot JSONB;

ALTER TABLE public.managed_model_price
    ADD CONSTRAINT managed_model_price_source_check
        CHECK (price_source IN ('manual', 'models_dev', 'azure_retail', 'anthropic')),
    ADD CONSTRAINT managed_model_price_reference_required
        CHECK (price_source IN ('manual', 'models_dev') OR price_reference IS NOT NULL),
    ADD CONSTRAINT managed_model_price_sync_status_check
        CHECK (price_sync_status IS NULL OR price_sync_status IN (
            'ok', 'unmapped', 'stale', 'review_needed', 'superseded',
            'ambiguous', 'unsupported', 'deferred'
        ));

-- Existing explicit choices are preserved. An absent row is resolved by the new reader,
-- not backfilled: a legacy price alone does not prove an explicit manual choice.
COMMENT ON TABLE public.managed_model_price IS
    'Explicit price source and accepted baseline. Absent row follows the default public catalog.';
