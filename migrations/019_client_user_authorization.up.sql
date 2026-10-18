-- Desktop user grants are independent of browser sessions and APIM subscription keys.
CREATE TABLE client_device_authorization (
    id UUID PRIMARY KEY,
    device_digest TEXT NOT NULL UNIQUE,
    code_digest TEXT NOT NULL UNIQUE,
    user_id UUID NOT NULL REFERENCES app_user(id),
    installation_id UUID NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('login','import','reauth')),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','denied','consumed')),
    selection JSONB,
    authenticated_method TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_poll_at TIMESTAMPTZ,
    result_cipher BYTEA,
    result_until TIMESTAMPTZ
);
CREATE INDEX client_device_user_created ON client_device_authorization(user_id,created_at);
CREATE INDEX client_device_expiry ON client_device_authorization(expires_at);

CREATE TABLE client_user_grant (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_user(id),
    installation_id UUID NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('password','entra')),
    access_digest TEXT NOT NULL UNIQUE,
    refresh_digest TEXT NOT NULL UNIQUE,
    previous_refresh_digest TEXT,
    refresh_request_id TEXT,
    refresh_result_cipher BYTEA,
    refresh_result_until TIMESTAMPTZ,
    image_digest TEXT UNIQUE,
    image_expires_at TIMESTAMPTZ,
    access_expires_at TIMESTAMPTZ NOT NULL,
    refresh_expires_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    selection JSONB,
    expected_generation BIGINT NOT NULL DEFAULT 0,
    activated BOOLEAN NOT NULL DEFAULT FALSE,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX client_grant_installation ON client_user_grant(user_id,installation_id);
CREATE INDEX client_grant_expiry ON client_user_grant(expires_at);
CREATE TABLE client_user_installation (
    user_id UUID NOT NULL REFERENCES app_user(id),
    installation_id UUID NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    active_grant UUID REFERENCES client_user_grant(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(user_id,installation_id)
);
