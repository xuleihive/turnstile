from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.image_profiles import ImageGenerationLimits


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str | None = None
    data_backend: Literal["postgresql", "demo"] = "postgresql"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    model_coefficients_json: str = '{"default":1.0}'
    credential_encryption_key: SecretStr | None = None
    credential_key_file: Path = Path(".turnstile/credential.key")
    log_analytics_workspace_id: str | None = None
    reconciliation_enabled: bool = True
    reconciliation_lookback_hours: int = Field(default=24, ge=1, le=168)
    reconciliation_safety_lag_minutes: int = Field(default=6, ge=1, le=120)
    cache_read_backfill_hours: int = Field(default=24 * 30, ge=24, le=24 * 90)
    cache_read_overlap_hours: int = Field(default=24, ge=1, le=168)
    # Budget ledger projected into Table Storage so the APIM admission check never
    # depends on this application.
    ledger_sync_enabled: bool = False
    ledger_table_endpoint: str | None = None
    ledger_table_name: str = "TurnstileLedger"
    ledger_reservation_recovery_lag_minutes: int = Field(default=10, ge=6, le=120)
    ledger_reservation_finalization_lag_hours: int = Field(default=24, ge=1, le=168)
    management_api_key: SecretStr | None = None
    production: bool = False
    image_generation_enabled: bool = False
    image_generation_defaults: ImageGenerationLimits = Field(
        default_factory=lambda: ImageGenerationLimits(
            max_request_bytes=24576,
            max_response_bytes=16777216,
            output_reservation_tokens=8192,
            timeout_seconds=180,
        )
    )
    bootstrap_owner_email: str = ""
    bootstrap_owner_password_hash: SecretStr | None = None

    # --- APIM control plane --------------------------------------------------------
    # Disabled unless an operator explicitly provisions the isolated publisher Function
    # and grants its managed identity narrowly scoped APIM rights. None of these settings
    # is read on the inference path.
    control_plane_enabled: bool = False
    gateway_publication_worker_enabled: bool = False
    gateway_release_worker_enabled: bool = False
    gateway_application_provisioning_enabled: bool = False
    gateway_application_key_management_enabled: bool = False
    azure_subscription_id: str | None = None
    apim_resource_group: str | None = None
    apim_service_name: str | None = None
    apim_principal_id: str | None = None
    databricks_oauth_enabled: bool = False
    apim_api_id: str = "turnstile-llm"
    apim_product_id: str = Field(default="finops-ai-consumers", min_length=1, max_length=255)
    apim_chat_completions_operation_id: str = "chat-completions"
    apim_responses_operation_id: str = "responses"
    apim_responses_compact_operation_id: str = "responses-compact"
    apim_messages_operation_id: str = "anthropic-messages"
    apim_count_tokens_operation_id: str = "anthropic-count-tokens"
    apim_models_operation_id: str = "anthropic-models"
    apim_gateway_url: str | None = None
    apim_dashboard_subscription_id: str = "turnstile-dashboard"
    apim_dashboard_subscription_key: SecretStr | None = None
    apim_probe_subscription_key: SecretStr | None = None
    apim_probe_subscription_id: str = "turnstile-publisher-probe"
    apim_regression_model_key: str = ""
    apim_usage_observer_url: str | None = None
    apim_usage_observer_key_named_value: str | None = None
    control_plane_lease_seconds: int = Field(default=180, ge=30, le=900)
    # A normal publication advances through six leased steps. Thirty leaves propagation
    # headroom while still bounding a pathological crash loop.
    control_plane_max_attempts: int = Field(default=30, ge=6, le=60)
    gateway_release_retention_count: int = Field(default=20, ge=2, le=1000)
    gateway_release_retention_days: int = Field(default=180, ge=1, le=3650)
    gateway_failed_release_retention_days: int = Field(default=30, ge=1, le=3650)
    gateway_release_protected_labels: list[str] = Field(
        default_factory=lambda: ["milestone", "rollback"]
    )
    gateway_application_default_monthly_token_limit: int = Field(
        default=100_000, ge=1
    )
    gateway_application_default_tokens_per_minute: int = Field(
        default=100_000, ge=1
    )
    apim_subscription_agent_map: dict[str, dict[str, str]] = Field(
        default_factory=dict
    )

    # --- Sign-in -------------------------------------------------------------------
    # The client this deployment uses for Microsoft sign-in. A public identifier, not a
    # secret: it travels in every authorize URL. It is a default rather than a required
    # setting so a local checkout works without a .env, and overridable because test and
    # production may eventually not share a registration.
    #
    # There is no tenant id here, and that is not an omission. The app is registered
    # multi-tenant and the frontend authenticates against `/organizations`, so tokens are
    # issued by the *signer's* tenant rather than the one owning the registration --
    # `EntraTokenVerifier` checks the issuer against each token's own `tid`. A constant
    # here would have described only the registration's tenant and matched nobody else's.
    entra_client_id: str = ""
    # Which mail domains may sign in with Microsoft. Empty means "any domain in the
    # tenant", which is a much weaker rule than it looks -- the tenant can invite guests --
    # so it is deliberately not the default.
    entra_allowed_email_domains: list[str] = Field(
        default_factory=list
    )
    # Application sessions are role-bound because an Owner can change budgets, model
    # access and gateway credentials while a Member is primarily a reader/caller.
    member_session_ttl_hours: int = Field(default=24, ge=1, le=168)
    owner_session_ttl_hours: int = Field(default=12, ge=1, le=24)
    # `Secure` is dropped only when running without TLS, which locally is the difference
    # between a working cookie and a silently rejected one. It follows `production` rather
    # than being its own switch so a production deployment cannot forget to set it.
    session_cookie_name: str = "turnstile_session"

    delegated_invocation_tester_ids: list[str] = Field(default_factory=list)

    traffic_generation_budget_usd: float = Field(default=20.0, gt=0, le=20)
    traffic_max_requests: int = Field(default=500, ge=1, le=5000)
    traffic_max_output_tokens: int = Field(default=64, ge=1, le=512)
    traffic_price_ceiling_per_million_usd: float = Field(default=100.0, gt=0)
    migrations_dir: Path = Path("migrations")
    web_dist_dir: Path = Path("frontend/dist")

    @model_validator(mode="after")
    def require_usage_observer_for_control_plane(self) -> Settings:
        owner_email = self.bootstrap_owner_email.strip()
        owner_hash = self.bootstrap_owner_password_hash
        if bool(owner_email) != bool(owner_hash):
            raise ValueError(
                "BOOTSTRAP_OWNER_EMAIL and BOOTSTRAP_OWNER_PASSWORD_HASH "
                "must be configured together"
            )
        if owner_email and "@" not in owner_email:
            raise ValueError("BOOTSTRAP_OWNER_EMAIL must be an email address")
        observer_url = (self.apim_usage_observer_url or "").strip()
        observer_key = (self.apim_usage_observer_key_named_value or "").strip()
        if bool(observer_url) != bool(observer_key):
            raise ValueError(
                "APIM usage observer URL and key Named Value must be configured together"
            )
        if self.control_plane_enabled and not observer_url:
            raise ValueError("The enabled control plane requires an APIM usage observer")
        return self

    @field_validator("delegated_invocation_tester_ids")
    @classmethod
    def normalize_delegated_invocation_tester_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold() for value in values]
        if any(not value or "@" not in value for value in normalized):
            raise ValueError("delegated invocation tester IDs must be email addresses")
        if len(normalized) != len(set(normalized)):
            raise ValueError("delegated invocation tester IDs must be unique")
        return normalized

    def session_ttl_hours_for(self, role: str) -> int:
        return (
            self.owner_session_ttl_hours
            if role == "owner"
            else self.member_session_ttl_hours
        )

    @property
    def model_coefficients(self) -> dict[str, float]:
        values = json.loads(self.model_coefficients_json)
        if not isinstance(values, dict):
            raise ValueError("MODEL_COEFFICIENTS_JSON must be an object")
        return {str(key): float(value) for key, value in values.items()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
