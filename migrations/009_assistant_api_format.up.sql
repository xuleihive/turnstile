ALTER TABLE public.assistant_setting
    ADD COLUMN api_format TEXT,
    ADD CONSTRAINT assistant_setting_api_format_check
        CHECK (api_format IN ('openai_chat', 'openai_responses', 'anthropic_messages'));

COMMENT ON COLUMN public.assistant_setting.api_format IS
    'Optional assistant API protocol. NULL selects the model-compatible protocol automatically.';
