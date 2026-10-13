ALTER TABLE public.gateway_application_audit
    DROP CONSTRAINT gateway_application_audit_operation_check,
    ADD CONSTRAINT gateway_application_audit_operation_check CHECK (operation IN (
        'created', 'adopted', 'updated', 'budget_updated', 'models_updated',
        'subscription_synced', 'subscription_created', 'subscription_cancelled',
        'owner_changed', 'suspended', 'resumed', 'retired'
    ));
