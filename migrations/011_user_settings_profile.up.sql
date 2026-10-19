CREATE TABLE app_user_avatar (
    app_user_id UUID PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL CHECK (media_type IN ('image/png', 'image/jpeg', 'image/webp')),
    image_bytes BYTEA NOT NULL CHECK (octet_length(image_bytes) BETWEEN 1 AND 65536),
    revision UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_security_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_user_id UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN (
        'display_name_updated', 'avatar_updated', 'avatar_removed',
        'password_changed', 'password_verification_failed'
    )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX account_security_event_user_action_time_idx
    ON account_security_event(app_user_id, action, created_at);

COMMENT ON TABLE app_user_avatar IS
    'Private normalized password-account avatars. Microsoft sessions use Graph photos.';
COMMENT ON TABLE account_security_event IS
    'Account changes and password-verification failures; never contains credentials or image data.';
