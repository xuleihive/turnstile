# Shared fixtures and builders for the control-plane test modules.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import Request
from pydantic import HttpUrl, SecretStr

from turnstile_core.config import Settings
from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationSubscriptionProvisionSpec,
)
from turnstile_core.domain.control_plane import (
    GatewayPublication,
    GatewayPublicationCreate,
    GatewayReleaseDependencies,
    GatewayReleaseRetentionPolicy,
    ModelCreateTarget,
    RuntimeTarget,
)
from turnstile_core.domain.runtime_models import ProviderTarget
from turnstile_core.integrations.apim_control_plane import (
    BackendResource,
    NamedValueResource,
    ReleaseGcPlanEvidence,
)
from turnstile_core.integrations.apim_control_plane_contract import (
    ImageProbeJournal,
    OAuthCredentialResource,
    OperationResource,
)
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.control_plane import (
    GatewayControlPlaneService as BaseGatewayControlPlaneService,
)
from turnstile_core.services.gateway_publication_worker import (
    GatewayPublicationWorker as BaseGatewayPublicationWorker,
)

APIM_ID = UUID("10000000-0000-4000-8000-000000000001")

ROOT = Path(__file__).resolve().parents[3]

TEST_CIPHER = CredentialCipher(Fernet.generate_key())

def _claim_for_test(repository: InMemoryRepository, publication_id: UUID, actor: str) -> None:
    owned = next(
        (
            item
            for item in repository.gateway_publication_outbox
            if item["publication_id"] == publication_id
            and item["status"] == "leased"
            and item["lease_owner"] == actor
            and item["lease_expires_at"] > datetime.now(UTC)
        ),
        None,
    )
    if owned is None:
        claimed = repository.claim_gateway_publication(actor, 180)
        assert claimed is not None and claimed["id"] == publication_id


def transition_publication(
    repository: InMemoryRepository,
    publication_id: UUID,
    expected_status: str,
    status: str,
    updates: Mapping[str, Any],
    actor: str,
) -> dict[str, Any] | None:
    if expected_status not in {"failed", "awaiting_authorization"}:
        _claim_for_test(repository, publication_id, actor)
    return repository.transition_gateway_publication(
        publication_id, expected_status, status, updates, actor
    )


def activate_publication(
    repository: InMemoryRepository, publication_id: UUID, actor: str
) -> dict[str, Any]:
    _claim_for_test(repository, publication_id, actor)
    return repository.activate_gateway_publication(publication_id, actor)


class GatewayControlPlaneService(BaseGatewayControlPlaneService):
    def __init__(
        self,
        repository: InMemoryRepository,
        cipher: CredentialCipher | None = None,
        apim_principal_id: str | None = None,
        retention_policy: GatewayReleaseRetentionPolicy | None = None,
        release_worker_enabled: bool = True,
        application_provisioning_enabled: bool = True,
        application_default_token_limit: int = 100_000,
        application_default_tokens_per_minute: int = 100_000,
        application_product_id: str = "finops-ai-consumers",
        dashboard_subscription_id: str = "turnstile-dashboard",
        probe_subscription_id: str = "turnstile-publisher-probe",
        databricks_oauth_enabled: bool = False,
    ) -> None:
        super().__init__(
            repository,
            cipher or TEST_CIPHER,
            apim_principal_id=apim_principal_id,
            retention_policy=retention_policy,
            release_worker_enabled=release_worker_enabled,
            application_provisioning_enabled=application_provisioning_enabled,
            application_default_token_limit=application_default_token_limit,
            application_default_tokens_per_minute=application_default_tokens_per_minute,
            application_product_id=application_product_id,
            dashboard_subscription_id=dashboard_subscription_id,
            probe_subscription_id=probe_subscription_id,
            databricks_oauth_enabled=databricks_oauth_enabled,
        )

class GatewayPublicationWorker(BaseGatewayPublicationWorker):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("cipher", TEST_CIPHER)
        super().__init__(*args, **kwargs)

class StubTokenProvider:
    def token(self, resource: str) -> str:
        assert resource == "https://management.azure.com/"
        return "managed-identity-token"

def publisher_settings() -> Settings:
    return Settings(
        azure_subscription_id="00000000-0000-0000-0000-000000000001",
        apim_resource_group="rg-finops",
        apim_service_name="apim-finops",
        apim_gateway_url="https://gateway.example.com/turnstile/llm",
    )

class PublicationAuthStore:
    def __init__(self, role: str) -> None:
        self.role = role

    def session_owner(self, token_sha256: str) -> dict[str, object] | None:
        assert len(token_sha256) == 64
        return {
            "email": "owner@example.com",
            "role": self.role,
        }

def publication_request(origin: str = "http://localhost:5173") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/model-management/publications",
            "headers": [
                (b"cookie", b"turnstile_session=session-token"),
                (b"origin", origin.encode()),
                (b"host", b"localhost:8000"),
            ],
        }
    )

class FakeApimClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.current: str | None = "1"
        self.policy = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
        self.backends: dict[str, BackendResource] = {}
        self.named_values: list[NamedValueResource] = []
        self.oauth_credentials: list[OAuthCredentialResource] = []
        self.api_policies: dict[str, str] = {}
        self.operation_policies: dict[str, str] = {}
        self.probe_counts: dict[str, int] = {}
        self.fail_probe_at: dict[str, int] = {}
        self.last_gc_retained_ids: set[str] = set()

    def ensure_backend(self, backend: BackendResource) -> None:
        self.backends[backend.id] = backend
        self.calls.append(("backend", backend.id))

    def ensure_named_value(self, named_value: NamedValueResource) -> None:
        self.named_values.append(named_value)
        self.calls.append(("named-value", named_value.id))

    def ensure_oauth_credential(self, credential: OAuthCredentialResource) -> None:
        self.oauth_credentials.append(credential)
        self.calls.append(("oauth-credential", credential.config.provider_id))

    def ensure_revision(self, revision: str, description: str) -> None:
        del description
        self.calls.append(("revision", revision))

    def put_api_policy(self, revision: str, policy: str) -> None:
        assert "selectedProviderName" in policy
        self.api_policies[revision] = policy
        self.calls.append(("api-policy", revision))

    def put_operation_policy(self, revision: str, operation: str, policy: str) -> None:
        assert policy.startswith("<policies>")
        self.operation_policies[operation] = policy
        self.calls.append((operation, revision))

    def ensure_operation(self, revision: str, operation: OperationResource) -> None:
        self.calls.append(("operation", operation.id))

    def revision_api_policy(self, revision: str) -> str:
        return self.api_policies.get(revision, self.policy)

    def probe_revision(
        self,
        revision: str,
        publication: object,
        *,
        journal: ImageProbeJournal | None = None,
    ) -> None:
        if journal is not None and isinstance(publication, GatewayPublication):
            for binding in publication.desired_spec.bindings:
                if binding.api_format == "openai_images":
                    journal.run(
                        binding,
                        revision,
                        {"model": binding.model.model_key},
                        lambda request_id, model_id: {
                            "total_tokens": 30,
                            "correlation_id": "unit-probe-" + request_id,
                            "image_validated": True,
                        },
                    )
        self.probe_counts[revision] = self.probe_counts.get(revision, 0) + 1
        if self.probe_counts[revision] == self.fail_probe_at.get(revision):
            raise RuntimeError("Injected post-promotion probe failure")
        self.calls.append(("probe", revision))

    def promote_revision(self, revision: str, release_name: str) -> None:
        del release_name
        self.calls.append(("promote", revision))
        self.current = revision

    def current_revision(self) -> str | None:
        return self.current

    def current_api_policy(self) -> tuple[str, str]:
        self.calls.append(("current-policy", self.current or ""))
        return (
            self.current or "1",
            self.policy,
        )

    def discover_gateway_applications(
        self, gateway_profile_id: UUID
    ) -> GatewayApplicationDiscovery:
        raise AssertionError(
            f"unexpected application discovery for {gateway_profile_id}"
        )

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None:
        del spec, primary_key, secondary_key
        raise AssertionError("unexpected Application subscription provisioning")

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None:
        del spec, primary_key, secondary_key
        raise AssertionError("unexpected Application subscription activation")

    def inspect_revision_dependencies(
        self, publication: GatewayPublication
    ) -> GatewayReleaseDependencies:
        manifest = publication.resource_manifest
        raw_backends = manifest.get("backends")
        raw_named_values = manifest.get("named_values")
        backend_ids = (
            [str(value) for value in raw_backends]
            if isinstance(raw_backends, list)
            else []
        )
        named_value_ids = (
            [str(value) for value in raw_named_values]
            if isinstance(raw_named_values, list)
            else []
        )
        return GatewayReleaseDependencies(
            apim_revision=publication.apim_revision,
            parent_policy_sha256=str(
                manifest.get("parent_policy_sha256")
                or manifest["base_policy_sha256"]
            ),
            compiled_policy_sha256=publication.policy_sha256,
            backends=sorted(
                value for value in backend_ids if not value.startswith("turnstile-pool-")
            ),
            backend_pools=sorted(
                value for value in backend_ids if value.startswith("turnstile-pool-")
            ),
            named_values=sorted(named_value_ids),
            recorded_complete=True,
            live_status="healthy",
            issues=[],
        )

    def plan_release_garbage_collection(
        self,
        releases: Sequence[GatewayPublication],
        retained_release_ids: set[str],
    ) -> ReleaseGcPlanEvidence:
        self.last_gc_retained_ids = retained_release_ids
        candidates: tuple[dict[str, object], ...] = tuple(
            {
                "resource_type": "api_revision",
                "resource_id": release.apim_revision,
                "reasons": ["release_not_retained"],
            }
            for release in releases
            if str(release.id) not in retained_release_ids
            and release.apim_revision is not None
        )
        return ReleaseGcPlanEvidence(
            current_non_release_references={"policy_surface_count": 1},
            candidates=candidates,
            reference_graph_sha256="a" * 64,
        )

def bedrock_publication(alias: str = "claude-sonnet-4-6-bedrock") -> GatewayPublicationCreate:
    return GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(template="amazon_bedrock"),
        runtime=RuntimeTarget(
            bedrock_runtime_url=HttpUrl(
                "https://bedrock-runtime.ap-southeast-2.amazonaws.com"
            ),
            api_key=SecretStr("bedrock-api-key"),
        ),
        model=ModelCreateTarget(
            model_key=alias,
            display_name="Claude Sonnet 4.6 - Amazon Bedrock",
            upstream_model_id="au.anthropic.claude-sonnet-4-6",
        ),
    )

def foundry_publication(
    deployment_name: str = "gpt-5-mini-deployment",
) -> GatewayPublicationCreate:
    return GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(template="microsoft_foundry"),
        runtime=RuntimeTarget(
            foundry_project_endpoint=HttpUrl(
                "https://contoso-ai.services.ai.azure.com/api/projects/finops"
            ),
        ),
        model=ModelCreateTarget(
            deployment_name=deployment_name,
        ),
    )

def external_tenant_foundry_publication(
    repository: InMemoryRepository,
    *,
    account: str = "admin-8382-resource",
    project: str = "admin-8382",
    deployment: str = "gpt-5.6-sol",
) -> GatewayPublicationCreate:
    provider = next(
        item
        for item in repository.providers
        if item["brand_key"] == "microsoft_foundry"
    )
    return GatewayPublicationCreate(
        gateway_profile_id=APIM_ID,
        provider=ProviderTarget(existing_id=provider["id"]),
        runtime=RuntimeTarget(
            foundry_project_endpoint=HttpUrl(
                f"https://{account}.services.ai.azure.com/api/projects/{project}"
            ),
            foundry_inference_endpoint=HttpUrl(
                f"https://{account}.openai.azure.com/openai/v1"
            ),
            api_key=SecretStr("external-foundry-key"),
        ),
        model=ModelCreateTarget(deployment_name=deployment),
    )
