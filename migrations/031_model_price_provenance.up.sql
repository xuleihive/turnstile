ALTER TABLE public.managed_model_price
    DROP CONSTRAINT managed_model_price_source_check,
    ADD CONSTRAINT managed_model_price_source_check
        CHECK (price_source IN ('manual', 'azure_retail', 'anthropic', 'models_dev')),
    ADD COLUMN price_source_snapshot JSONB;