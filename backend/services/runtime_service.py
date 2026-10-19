from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, NoReturn, cast, overload
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import SecretStr

from turnstile_core.config import Settings
from turnstile_core.domain.application_access import application_id_for
from turnstile_core.domain.budget_delegation import (
    BudgetDelegationCheck,
    BudgetDelegationReadiness,
    BudgetExecutionScopeReadback,
    BudgetExecutionVerification,
    BudgetExecutionVerificationOptions,
    BudgetExecutionVerificationRequest,
)
from turnstile_core.domain.gateway_targets import ApimTargetTopologyList, validate_gateway_endpoint
from turnstile_core.domain.images import ImageInvocationRequest, ImageInvocationResponse
from turnstile_core.domain.ledger import (
    BudgetPriceProjection,
    LedgerAttempt,
    LedgerAttemptIdentity,
    LedgerScope,
)
from turnstile_core.domain.model_availability import ModelAvailabilityConflict
from turnstile_core.domain.models import ModelPrice, TokenUsageRecord
from turnstile_core.domain.runtime_models import (
    FOUNDRY_INFERENCE_RESOURCE,
    FOUNDRY_INFERENCE_ROLE_ID,
    BrandKey,
    ConnectionAuthMode,
    DecisionInvocationRequest,
    DecisionInvocationResponse,
    GatewayKind,
    GatewayProfile,
    GatewayProfileNameWrite,
    GatewayProfileWrite,
    InvocationUsage,
    ManagedModel,
    ManagedModelWrite,
    ModelConnectionCreate,
    ModelConnectionUpdate,
    ModelFamilyKey,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ModelVendorKey,
    OAuthClientCredentialsConfig,
    PlatformInvocationAttempt,
    PlatformInvocationCompletion,
    PriceCatalogModel,
    PriceCatalogModelsResponse,
    PriceCatalogOption,
    PriceCatalogOptionsResponse,
    PriceSource,
    PriceSyncDetail,
    PriceSyncResponse,
    PriceSyncStatus,
    Provider,
    ProviderKind,
    ProviderWrite,
    RegistryResponse,
    Runtime,
    RuntimeCheckPlan,
    RuntimeCheckSelection,
    RuntimeHealth,
    RuntimeKind,
    RuntimeWrite,
    databricks_connection_config,
    databricks_runtime_name,
    databricks_workspace_url,
    foundry_runtime_name,
    model_vendor_label,
    openai_compatible_endpoint_values,
    openai_compatible_runtime_name,
)
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.gateway import (
    GatewayInvocationError,
    GatewayRouter,
    MicrosoftDecisionGatewayAdapter,
    OpenAICompatibleGatewayAdapter,
    elapsed_ms,
)
from turnstile_core.integrations.gateway_protocol import model_protocol_route
from turnstile_core.integrations.reconciliation import ManagedIdentityTokenProvider
from turnstile_core.persistence.repository import QueryRepository
from turnstile_core.pricing.catalog import (
    CompositeCatalog,
    build_default_catalog,
    parse_context_tier,
)
from turnstile_core.pricing.context import (
    context_price_reference,
    model_price_from_values,
    resolve_context_prices,
)
from turnstile_core.pricing.sync import plan_price_sync
from turnstile_core.security import CredentialCipher, credential_hint
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.budget_settlement import BudgetScopeRepository, BudgetStatusRepository

from .runtime_diagnostics import RuntimeDiagnostics

logger = logging.getLogger(__name__)


class ModelRuntimeService:
    _MODEL_ROUTING_FIELDS = ("provider_id", "runtime_id", "model_key", "upstream_model_id")

    def __init__(
        self,
        repository: QueryRepository,
        settings: Settings,
        router: GatewayRouter | None = None,
        price_catalog: CompositeCatalog | None = None,
        topology_reader: Callable[
            [Mapping[str, Sequence[dict[str, Any]]]], ApimTargetTopologyList
        ] | None = None,
        project_budget_price: Callable[[UUID, BudgetPriceProjection], None] | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._cipher = CredentialCipher.from_settings(settings)
        self._router = router or GatewayRouter()
        self._price_catalog = price_catalog or build_default_catalog()
        self._topology_reader = topology_reader
        self._project_budget_price = project_budget_price

    def _project_accepted_model_price(self, model_id: UUID) -> None:
        if self._project_budget_price is None:
            return
        try:
            observed_at = datetime.now(UTC)
            row = next((
                model for model in self._repository.registry()["models"]
                if model["id"] == model_id
            ), None)
            if row is None:
                return
            price = ModelPrice.from_registry(row)
            projection = BudgetPriceProjection.create(model_id, price, observed_at)
            self._project_budget_price(model_id, projection)
        except Exception as error:
            logger.warning(
                "Accepted price awaits USD budget projection model_id=%s error_type=%s",
                model_id, type(error).__name__,
            )

    def authorize(self, role: str, authorization: str | None, *, manage: bool) -> None:
        if manage:
            configured = self._settings.management_api_key
            if configured is not None:
                expected = f"Bearer {configured.get_secret_value()}"
                if authorization is None or not hmac.compare_digest(authorization, expected):
                    raise HTTPException(status_code=401, detail="Invalid management credential")
            elif self._settings.production:
                raise HTTPException(
                    status_code=503, detail="Management authentication is not configured"
                )
        allowed = {"owner", "admin"} if manage else {"owner", "admin", "member", "service"}
        if role not in allowed:
            raise HTTPException(status_code=403, detail="Role is not allowed for this operation")

    def authorize_model_update(
        self,
        item_id: UUID,
        write: ManagedModelWrite,
        role: str,
        authorization: str | None,
    ) -> None:
        current = next(
            (
                model
                for model in self._repository.registry()["models"]
                if model["id"] == item_id
            ),
            None,
        )
        if current is None:
            raise HTTPException(status_code=404, detail=f"Unknown model: {item_id}")
        incoming = write.model_dump(mode="python")
        changes_routing_identity = any(
            str(current.get(field) or "") != str(incoming.get(field) or "")
            for field in self._MODEL_ROUTING_FIELDS
        )
        self.authorize(role, authorization, manage=changes_routing_identity)

    def registry(self) -> RegistryResponse:
        rows = self._repository.registry()
        github_provider_ids, copilot_runtime_ids = self._copilot_registry_ids(rows)
        providers = [
            row for row in rows["providers"] if row["id"] not in github_provider_ids
        ]
        provider_ids = {row["id"] for row in providers}
        runtimes = [
            row for row in rows["runtimes"] if row["id"] not in copilot_runtime_ids
        ]
        runtime_ids = {row["id"] for row in runtimes}
        diagnostics = RuntimeDiagnostics(self._repository, self._settings).project_results(runtimes)
        return RegistryResponse(
            runtime_diagnostics_supported=True,
            failed_publication_archive_supported=True,
            context_pricing_supported=True,
            context_price_rules_supported=True,
            backend_pool_session_affinity_supported=True,
            databricks_connections_supported=True,
            databricks_oauth_supported=self._settings.databricks_oauth_enabled,
            decision_supported=True,
            image_generation_supported=True,
            image_configuration_defaults=self._settings.image_generation_defaults,
            apim_target_topology_available=(
                self._settings.apim_target_registry_enabled
            ),
            apim_target_topology=(
                self._topology_reader(rows)
                if self._settings.apim_target_registry_enabled and self._topology_reader
                else None
            ),
            explicit_gateway_selection_supported=self._settings.apim_target_registry_enabled,
            self_hosted_gateway_enabled=self._settings.self_hosted_gateway_enabled,
            multi_apim_target_rollout_enabled=(
                self._settings.multi_apim_target_rollout_enabled
            ),
            gateways=[
                GatewayProfile.model_validate(self._public_secret(row))
                for row in rows["gateways"]
            ],
            providers=[
                Provider.model_validate(self._public_secret(row))
                for row in providers
            ],
            runtimes=[Runtime.model_validate({**row, "diagnostic": diagnostics.get(row["id"])})
                      for row in runtimes],
            models=[
                ManagedModel.model_validate(row)
                for row in rows["models"]
                if row["runtime_id"] in runtime_ids and row["provider_id"] in provider_ids
                and row.get("family_key") != ModelFamilyKey.COPILOT
            ],
        )

    @staticmethod
    def _copilot_registry_ids(rows: dict[str, Any]) -> tuple[set[UUID], set[UUID]]:
        github_provider_ids = {
            row["id"]
            for row in rows["providers"]
            if row["provider_kind"] == ProviderKind.GITHUB
            or row.get("brand_key") == BrandKey.GITHUB
        }
        copilot_runtime_ids = {
            row["id"]
            for row in rows["runtimes"]
            if row["runtime_kind"] == RuntimeKind.COPILOT_CLI
            or row.get("brand_key") == BrandKey.GITHUB
            or row["provider_id"] in github_provider_ids
        }
        return github_provider_ids, copilot_runtime_ids

    def save_gateway(
        self, write: GatewayProfileWrite, item_id: UUID | None = None
    ) -> RegistryResponse:
        values = self._secret_values(write, creating=item_id is None)
        self._save("gateway", values, item_id)
        return self.registry()

    def rename_gateway(
        self, gateway_id: UUID, write: GatewayProfileNameWrite, *, include_registry: bool = True,
    ) -> RegistryResponse | None:
        registry = self.registry() if include_registry else None
        saved = self._save("gateway", {"name": write.name}, gateway_id)
        if registry is None:
            return None
        for gateway in registry.gateways:
            if gateway.id == gateway_id:
                gateway.name = write.name
                gateway.updated_at = saved["updated_at"]
        for runtime in registry.runtimes:
            if runtime.gateway_profile_id == gateway_id:
                runtime.gateway_name = write.name
        return registry

    def delete_gateway(self, gateway_id: UUID) -> RegistryResponse:
        registry = self._repository.registry()
        gateway = next(
            (row for row in registry["gateways"] if row["id"] == gateway_id),
            None,
        )
        if gateway is None:
            raise HTTPException(status_code=404, detail="Gateway not found")
        if gateway.get("is_default"):
            raise HTTPException(
                status_code=409,
                detail="Select another default gateway before deleting this one",
            )
        if any(
            runtime.get("gateway_profile_id") == gateway_id
            for runtime in registry["runtimes"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Remove every connection from this gateway before deleting it",
            )
        if self._repository.list_gateway_publications(gateway_id, 1):
            raise HTTPException(
                status_code=409,
                detail="A gateway with publication history cannot be deleted",
            )
        if not self._repository.delete_gateway_if_unused(gateway_id):
            raise HTTPException(
                status_code=409,
                detail="The gateway changed and could not be deleted",
            )
        return self.registry()

    def save_provider(
        self, write: ProviderWrite, item_id: UUID | None = None
    ) -> RegistryResponse:
        registry = self._repository.registry()
        github_provider_ids, _ = self._copilot_registry_ids(registry)
        if (
            write.provider_kind is ProviderKind.GITHUB
            or write.brand_key is BrandKey.GITHUB
            or item_id in github_provider_ids
        ):
            raise HTTPException(
                status_code=409,
                detail="GitHub Copilot is managed through its separate data source",
            )
        values = self._secret_values(write, creating=item_id is None)
        self._save("provider", values, item_id)
        return self.registry()

    def save_runtime(self, write: RuntimeWrite, item_id: UUID | None = None) -> RegistryResponse:
        registry = self._repository.registry()
        current = next((row for row in registry["runtimes"] if row["id"] == item_id), None)
        if write.brand_key is BrandKey.AZURE_DATABRICKS or (
            current is not None and current.get("brand_key") == BrandKey.AZURE_DATABRICKS
        ):
            raise HTTPException(
                status_code=409,
                detail="Use Databricks connection metadata or publication workflows",
            )
        github_provider_ids, copilot_runtime_ids = self._copilot_registry_ids(registry)
        if (
            write.runtime_kind is RuntimeKind.COPILOT_CLI
            or write.brand_key is BrandKey.GITHUB
            or write.provider_id in github_provider_ids
            or item_id in copilot_runtime_ids
        ):
            raise HTTPException(
                status_code=409,
                detail="GitHub Copilot is managed through its separate data source",
            )
        self._save("runtime", write.model_dump(mode="python"), item_id)
        return self.registry()

    def save_connection(self, write: ModelConnectionCreate) -> RegistryResponse:
        registry = self._repository.registry()
        gateway = next(
            (
                row
                for row in registry["gateways"]
                if row["id"] == write.gateway_profile_id
            ),
            None,
        )
        if gateway is None or not gateway["enabled"] or gateway["implementation"] != "apim":
            raise HTTPException(status_code=409, detail="The selected APIM gateway is unavailable")
        provider_values: dict[str, Any] | None = None
        openai_vendor = write.model_vendor or ModelVendorKey.GENERIC
        if write.provider.existing_id is not None:
            provider = next(
                (
                    row
                    for row in registry["providers"]
                    if row["id"] == write.provider.existing_id
                ),
                None,
            )
            if provider is None or not provider["enabled"]:
                raise HTTPException(
                    status_code=409,
                    detail="The selected provider is unavailable",
                )
            brand = BrandKey(provider.get("brand_key", BrandKey.GENERIC))
            provider_kind = ProviderKind(provider["provider_kind"])
            provider_id = provider["id"]
            if provider_kind is ProviderKind.OPENAI_COMPATIBLE:
                configured_vendor = ModelVendorKey(
                    str((provider.get("config") or {}).get("model_vendor", "generic"))
                )
                if write.model_vendor is not None and configured_vendor not in {
                    ModelVendorKey.GENERIC,
                    write.model_vendor,
                }:
                    raise HTTPException(
                        status_code=409,
                        detail="The selected model vendor does not match the provider",
                    )
                openai_vendor = write.model_vendor or configured_vendor
        else:
            assert write.provider.template is not None
            model_vendor = openai_vendor
            brand = {
                "amazon_bedrock": BrandKey.AMAZON_BEDROCK,
                "microsoft_foundry": BrandKey.MICROSOFT_FOUNDRY,
                "azure_databricks": BrandKey.AZURE_DATABRICKS,
                "openai_compatible": {
                    ModelVendorKey.OPENAI: BrandKey.OPENAI,
                    ModelVendorKey.ANTHROPIC: BrandKey.ANTHROPIC,
                }.get(model_vendor, BrandKey.GENERIC),
            }[write.provider.template]
            provider_kind = (
                ProviderKind.ANTHROPIC
                if write.provider.template in {"amazon_bedrock", "azure_databricks"}
                else ProviderKind.MICROSOFT_FOUNDRY
                if write.provider.template == "microsoft_foundry"
                else ProviderKind.OPENAI_COMPATIBLE
            )
            provider_id = None
            provider_name = (
                "Microsoft Foundry"
                if write.provider.template == "microsoft_foundry"
                else "Amazon Bedrock"
                if write.provider.template == "amazon_bedrock"
                else "Azure Databricks"
                if write.provider.template == "azure_databricks"
                else model_vendor_label(model_vendor)
            )
            provider_config: dict[str, Any] = {
                "hosting_platform": write.provider.template,
            }
            if write.provider.template == "openai_compatible":
                provider_config["model_vendor"] = model_vendor.value
            provider_values = {
                "name": provider_name,
                "provider_kind": provider_kind,
                "endpoint_url": None,
                "auth_type": "none",
                "credential_ciphertext": None,
                "credential_hint": None,
                "enabled": True,
                "brand_key": brand,
                "config": provider_config,
            }
        if brand is BrandKey.MICROSOFT_FOUNDRY:
            values = self._foundry_connection_values(write, registry["runtimes"])
        elif brand is BrandKey.AMAZON_BEDROCK:
            values = self._bedrock_connection_values(write, registry["runtimes"])
        elif brand is BrandKey.AZURE_DATABRICKS:
            values = self._databricks_connection_values(write, registry["runtimes"])
        elif provider_kind is ProviderKind.OPENAI_COMPATIBLE:
            values = self._openai_connection_values(
                write,
                registry["runtimes"],
                openai_vendor,
            )
        else:
            raise HTTPException(
                status_code=409,
                detail="This provider does not support managed connection onboarding",
            )
        try:
            runtime_brand = brand
            if provider_kind is ProviderKind.OPENAI_COMPATIBLE:
                runtime_brand = {
                    ModelVendorKey.OPENAI: BrandKey.OPENAI,
                    ModelVendorKey.ANTHROPIC: BrandKey.ANTHROPIC,
                }.get(openai_vendor, BrandKey.GENERIC)
            self._repository.create_connection(
                provider_id,
                provider_values,
                {
                "gateway_profile_id": gateway["id"],
                "enabled": True,
                "is_default": False,
                "brand_key": runtime_brand,
                "allowed_roles": ["owner", "admin", "member"],
                **values,
            },
            )
        except UniqueViolation as error:
            constraint = error.diag.constraint_name
            logging.getLogger(__name__).error(
                "Connection save rejected exception=UniqueViolation sqlstate=%s constraint=%s",
                error.sqlstate, constraint,
            )
            message = (
                "Connection display names are still restricted by the database schema; "
                "the runtime UUID identity migration must be applied"
                if constraint == "model_runtime_name_key"
                else "The connection conflicts with an existing record"
            )
            raise HTTPException(status_code=409, detail=message) from error
        except ForeignKeyViolation as error:
            raise HTTPException(
                status_code=409,
                detail="The selected APIM or provider changed; refresh before saving",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return self.registry()

    def update_connection(
        self, runtime_id: UUID, write: ModelConnectionUpdate
    ) -> RegistryResponse:
        current = next(
            (
                runtime
                for runtime in self._repository.registry()["runtimes"]
                if runtime["id"] == runtime_id
            ),
            None,
        )
        if current is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        values = write.model_dump(mode="python", exclude={"is_default"})
        if bool(current.get("is_default")) != write.is_default:
            values["is_default"] = write.is_default
        self._save("runtime", values, runtime_id)
        return self.registry()

    def delete_connection(self, runtime_id: UUID) -> RegistryResponse:
        registry = self._repository.registry()
        runtime = next(
            (row for row in registry["runtimes"] if row["id"] == runtime_id),
            None,
        )
        if runtime is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        config = dict(runtime.get("config") or {})
        if not config.get("control_plane_managed") or runtime.get("gateway_profile_id") is None:
            raise HTTPException(
                status_code=409,
                detail="Only control-plane-managed connections can be deleted",
            )
        if runtime.get("is_default"):
            raise HTTPException(
                status_code=409,
                detail="Select another default connection before deleting this one",
            )
        if any(model["runtime_id"] == runtime_id for model in registry["models"]):
            raise HTTPException(
                status_code=409,
                detail="Remove every model from this connection before deleting it",
            )
        active_statuses = {
            "queued",
            "validating",
            "provisioning",
            "building_revision",
            "verifying",
            "awaiting_authorization",
            "promoting",
            "rolling_back",
        }
        for publication in self._repository.list_gateway_publications(
            runtime["gateway_profile_id"],
            100,
        ):
            if publication["status"] not in active_statuses:
                continue
            bindings = dict(publication["desired_spec"]).get("bindings", [])
            if any(str(binding.get("runtime_id")) == str(runtime_id) for binding in bindings):
                raise HTTPException(
                    status_code=409,
                    detail="A publication is still using this connection",
                )
        if not self._repository.delete_runtime_if_empty(runtime_id):
            raise HTTPException(
                status_code=409,
                detail="The connection changed and could not be deleted",
            )
        return self.registry()

    def _connection_principal_id(self, gateway_profile_id: UUID) -> str | None:
        if not self._settings.apim_target_registry_enabled:
            return self._settings.apim_principal_id
        target = self._repository.get_apim_target(gateway_profile_id)
        if target is None or not target.get("principal_id"):
            raise HTTPException(
                status_code=409,
                detail="The selected APIM target has no managed identity",
            )
        return str(target["principal_id"])

    def _foundry_connection_values(
        self,
        write: ModelConnectionCreate,
        runtimes: list[dict[str, Any]] | Any,
    ) -> dict[str, Any]:
        if write.foundry_project_endpoint is None or write.auth_mode is None:
            raise HTTPException(status_code=400, detail="Foundry connection fields are missing")
        endpoint = urlsplit(str(write.foundry_project_endpoint))
        host = (endpoint.hostname or "").casefold()
        suffix = ".services.ai.azure.com"
        account = host.removesuffix(suffix) if host.endswith(suffix) else ""
        path_match = re.fullmatch(
            r"/api/projects/(?P<project>[A-Za-z0-9._-]+)/?",
            endpoint.path,
        )
        if (
            endpoint.scheme != "https"
            or not account
            or path_match is None
            or endpoint.query
            or endpoint.fragment
            or endpoint.username is not None
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "The Foundry project endpoint must use "
                    "https://<account>.services.ai.azure.com/api/projects/<project>"
                ),
            )
        project = unquote(path_match.group("project"))
        project_endpoint = f"https://{endpoint.netloc}/api/projects/{project}"
        backend_url = project_endpoint
        auth_strategy = "managed_identity"
        named_value_name = None
        managed_identity_resource: str | None = FOUNDRY_INFERENCE_RESOURCE
        config: dict[str, Any] = {
            "project_endpoint": project_endpoint,
            "max_tokens_field": "max_completion_tokens",
            "supports_temperature": False,
        }
        if write.auth_mode is ConnectionAuthMode.API_KEY:
            assert write.foundry_inference_endpoint is not None
            inference = urlsplit(str(write.foundry_inference_endpoint))
            inference_host = (inference.hostname or "").casefold()
            if (
                inference.scheme != "https"
                or inference_host
                not in {
                    f"{account}.openai.azure.com",
                    f"{account}.services.ai.azure.com",
                }
                or inference.path.rstrip("/") != "/openai/v1"
                or inference.query
                or inference.fragment
                or inference.username is not None
            ):
                raise HTTPException(
                    status_code=400,
                    detail="The Foundry inference endpoint must use the same account",
                )
            inference_endpoint = str(write.foundry_inference_endpoint).rstrip("/")
            backend_url = f"{inference.scheme}://{inference.netloc}"
            named_value_name = f"turnstile-foundry-key-{uuid4().hex[:20]}"
            auth_strategy = "named_value_api_key"
            managed_identity_resource = None
            config.update(
                inference_endpoint=inference_endpoint,
                credential_kind="api_key",
                credential_provisioned=False,
            )
        else:
            principal_id = self._connection_principal_id(write.gateway_profile_id)
            if not principal_id:
                raise HTTPException(
                    status_code=409,
                    detail="APIM managed identity is not configured for Foundry onboarding",
                )
            config["authorization"] = {
                "kind": "azure_rbac",
                "principal_id": principal_id,
                "resource_endpoint": f"https://{endpoint.netloc}",
                "role_id": FOUNDRY_INFERENCE_ROLE_ID,
                "role_name": "Cognitive Services User",
            }
        self._ensure_unique_connection(
            runtimes,
            write.gateway_profile_id,
            "project_endpoint",
            project_endpoint,
        )
        return {
            "name": foundry_runtime_name(account, project),
            "runtime_kind": "foundry",
            "config": {
                **config,
                "path": "/chat/completions",
                "api_format": "openai_chat",
                "streaming_mode": "native",
                "control_plane_managed": True,
                "backend_url": backend_url,
                "backend_path": "/openai/v1/chat/completions",
                "auth_strategy": auth_strategy,
                "named_value_name": named_value_name,
                "key_vault_secret_id": None,
                "managed_identity_resource": managed_identity_resource,
            },
        }

    def _bedrock_connection_values(
        self,
        write: ModelConnectionCreate,
        runtimes: list[dict[str, Any]] | Any,
    ) -> dict[str, Any]:
        if write.bedrock_runtime_url is None:
            raise HTTPException(status_code=400, detail="Bedrock Runtime URL is missing")
        endpoint = urlsplit(str(write.bedrock_runtime_url))
        host = (endpoint.hostname or "").casefold()
        prefix = "bedrock-runtime."
        suffix = ".amazonaws.com"
        if (
            endpoint.scheme != "https"
            or not host.startswith(prefix)
            or not host.endswith(suffix)
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise HTTPException(status_code=400, detail="Enter a regional Bedrock Runtime URL")
        region = host.removeprefix(prefix).removesuffix(suffix)
        backend_url = f"{endpoint.scheme}://{endpoint.netloc}"
        self._ensure_unique_connection(
            runtimes,
            write.gateway_profile_id,
            "backend_url",
            backend_url,
        )
        scope = f"{write.gateway_profile_id}:{backend_url}"
        return {
            "name": f"Amazon Bedrock Claude ({region}) via APIM",
            "runtime_kind": "openai_compatible",
            "config": {
                "region": region,
                "path": "/v1/messages",
                "api_format": "anthropic_messages",
                "streaming_mode": "buffered",
                "control_plane_managed": True,
                "backend_url": backend_url,
                "backend_path": "/model/{upstream_model_id}/invoke",
                "auth_strategy": "named_value_bearer",
                "named_value_name": (
                    "turnstile-bedrock-"
                    + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
                ),
                "credential_kind": "api_key",
                "credential_provisioned": False,
                "key_vault_secret_id": None,
                "managed_identity_resource": None,
            },
        }

    def _databricks_connection_values(
        self,
        write: ModelConnectionCreate,
        runtimes: list[dict[str, Any]] | Any,
    ) -> dict[str, Any]:
        if write.databricks_workspace_url is None:
            raise HTTPException(status_code=400, detail="Databricks Workspace URL is missing")
        principal_id = self._connection_principal_id(write.gateway_profile_id)
        if not principal_id:
            raise HTTPException(
                status_code=409,
                detail="APIM managed identity is not configured for Databricks onboarding",
            )
        workspace = databricks_workspace_url(str(write.databricks_workspace_url))
        config = databricks_connection_config(workspace, principal_id)
        if write.auth_mode is ConnectionAuthMode.OAUTH_M2M:
            if not self._settings.databricks_oauth_enabled:
                raise HTTPException(
                    status_code=409,
                    detail="Databricks OAuth M2M is not enabled in this environment",
                )
            assert write.oauth_client_id is not None
            oauth = OAuthClientCredentialsConfig.model_validate({
                "provider_id": f"turnstile-oauth-{uuid4().hex}",
                "client_id": write.oauth_client_id,
                "token_url": f"{workspace}/oidc/v1/token",
            })
            config.update(
                auth_strategy="oauth_client_credentials",
                managed_identity_resource=None,
                credential_kind="databricks_oauth_m2m",
                credential_provisioned=False,
                oauth=oauth.model_dump(mode="json"),
                authorization={
                    "kind": "databricks_oauth",
                    "client_id": str(write.oauth_client_id),
                    "resource_endpoint": workspace,
                    "role_name": "CAN_QUERY",
                },
            )
        self._ensure_unique_connection(
            runtimes, write.gateway_profile_id, "workspace_url", workspace,
        )
        return {
            "name": databricks_runtime_name(workspace),
            "runtime_kind": "openai_compatible",
            "config": config,
        }

    def _openai_connection_values(
        self,
        write: ModelConnectionCreate,
        runtimes: list[dict[str, Any]] | Any,
        model_vendor: ModelVendorKey,
    ) -> dict[str, Any]:
        if write.openai_base_url is None:
            raise HTTPException(
                status_code=400,
                detail="OpenAI-compatible Base URL is missing",
            )
        try:
            base_url, backend_url, backend_path = openai_compatible_endpoint_values(
                str(write.openai_base_url)
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        self._ensure_unique_connection(
            runtimes,
            write.gateway_profile_id,
            "base_url",
            base_url,
        )
        scope = f"{write.gateway_profile_id}:{base_url.casefold()}"
        return {
            "name": openai_compatible_runtime_name(base_url, model_vendor),
            "runtime_kind": "openai_compatible",
            "config": {
                "base_url": base_url,
                "model_vendor": model_vendor.value,
                "path": "/chat/completions",
                "api_format": "openai_chat",
                "streaming_mode": "native",
                "control_plane_managed": True,
                "backend_url": backend_url,
                "backend_path": backend_path,
                "auth_strategy": "named_value_bearer",
                "named_value_name": (
                    "turnstile-openai-"
                    + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
                ),
                "credential_kind": "api_key",
                "credential_provisioned": False,
                "key_vault_secret_id": None,
                "managed_identity_resource": None,
                "max_tokens_field": "max_tokens",
                "supports_temperature": True,
            },
        }

    @staticmethod
    def _ensure_unique_connection(
        runtimes: Any,
        gateway_id: UUID,
        config_key: str,
        expected: str,
    ) -> None:
        if any(
            runtime.get("gateway_profile_id") == gateway_id
            and str((runtime.get("config") or {}).get(config_key, "")).rstrip("/").casefold()
            == expected.rstrip("/").casefold()
            for runtime in runtimes
        ):
            raise HTTPException(status_code=409, detail="This connection already exists")

    def save_model(
        self, write: ManagedModelWrite, item_id: UUID | None = None
    ) -> RegistryResponse:
        if "decision" in write.capabilities and item_id is None:
            raise HTTPException(
                status_code=409, detail="Publish Decision models through the control plane",
            )
        if "image_generation" in write.capabilities:
            if item_id is None:
                raise HTTPException(
                    status_code=409, detail="Publish image models through the control plane"
                )
            if any(
                rate is None
                for rate in (
                    write.input_cost_per_million,
                    write.cached_cost_per_million,
                    write.output_cost_per_million,
                )
            ):
                raise HTTPException(status_code=422, detail="Image prices cannot be omitted")
        registry = self._repository.registry()
        github_provider_ids, copilot_runtime_ids = self._copilot_registry_ids(registry)
        current = next(
            (row for row in registry["models"] if row["id"] == item_id),
            None,
        )
        if (
            write.family_key is ModelFamilyKey.COPILOT
            or write.provider_id in github_provider_ids
            or write.runtime_id in copilot_runtime_ids
            or current is not None
            and (
                current["provider_id"] in github_provider_ids
                or current["runtime_id"] in copilot_runtime_ids
                or current.get("family_key") == ModelFamilyKey.COPILOT
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="GitHub Copilot is managed through its separate data source",
            )
        if current is not None and (
            any((operation in write.capabilities)
                != (operation in (current.get("capabilities") or []))
                for operation in ("image_generation", "decision"))
        ):
            raise HTTPException(
                status_code=409, detail="Changing model operation requires a publication"
            )
        if (
            current is not None
            and "enabled" in write.model_fields_set
            and write.enabled != current["enabled"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Change model availability through the versioned availability command",
            )
        price_fields = (
            "runtime_id", "price_source", "price_discount_percent", "context_price_references",
            "context_price_rule",
            "input_cost_per_million", "output_cost_per_million",
            "cached_cost_per_million", "cache_write_cost_per_million",
        )
        current_model = ManagedModel.model_validate(current) if current is not None else None
        pricing_changed = (
            current_model is None
            or write.context_price_refresh
            or any(getattr(current_model, field) != getattr(write, field) for field in price_fields)
            or (not write.context_price_references
                and current_model.price_reference != write.price_reference)
        )
        values = write.model_dump(mode="python")
        if item_id is not None and "enabled" not in write.model_fields_set:
            values.pop("enabled", None)
        values.pop("context_price_references", None)
        values.pop("context_price_refresh", None)
        values.pop("context_price_rule", None)
        discount = write.price_discount_percent
        if write.price_source is not PriceSource.MANUAL:
            runtime = next(
                (row for row in registry["runtimes"] if row["id"] == write.runtime_id), None,
            )
            if runtime is None:
                raise HTTPException(status_code=422, detail="Unknown pricing connection")
            if discount is None:
                discount = runtime.get("price_discount_percent")
            if discount is not None:
                discount = float(discount)
        if write.context_price_references:
            try:
                if pricing_changed:
                    tiers = resolve_context_prices(
                        self._price_catalog, write.price_source,
                        write.context_price_references, discount,
                        rule=write.context_price_rule,
                        previous_tiers=current_model.context_price_tiers if current_model else (),
                    )
                else:
                    tiers = current_model.context_price_tiers if current_model else []
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except Exception as error:
                raise HTTPException(
                    status_code=503, detail="The selected price source is unavailable",
                ) from error
            values["context_price_tiers"] = [tier.model_dump(mode="json") for tier in tiers]
            values["price_reference"] = context_price_reference(
                write.price_source, [tier.reference for tier in tiers],
            )
            for column in ("input_cost_per_million", "output_cost_per_million",
                           "cached_cost_per_million", "cache_write_cost_per_million"):
                values[column] = None
        elif write.price_source is not PriceSource.MANUAL and pricing_changed:
            try:
                entry = self._price_catalog.lookup(write.price_reference or "")
            except Exception as error:  # noqa: BLE001
                raise HTTPException(
                    status_code=503, detail="The selected price source is unavailable"
                ) from error
            if (
                entry is None
                or not entry.complete
                or entry.source is not write.price_source
                or entry.reference != write.price_reference
                or entry.input_per_million is None
                or entry.output_per_million is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The selected catalog price is incomplete; existing prices are unchanged"
                    ),
                )
            if entry.context_tier is not None or parse_context_tier(entry.label) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Select the complete context price schedule, not one tier",
                )
            charged = entry.discounted(discount)
            for column, bucket in (
                ("input_cost_per_million", "input_per_million"),
                ("output_cost_per_million", "output_per_million"),
                ("cached_cost_per_million", "cached_per_million"),
                ("cache_write_cost_per_million", "cache_write_per_million"),
            ):
                values[column] = getattr(charged, bucket)
                values[f"list_{column}"] = getattr(entry, bucket)
        if pricing_changed:
            for column in (
                "list_input_cost_per_million", "list_output_cost_per_million",
                "list_cached_cost_per_million", "list_cache_write_cost_per_million",
            ):
                values.setdefault(column, None)
            values.update(
                pending_list_price=None, pending_context_price_tiers=None,
                price_sync_status="ok" if write.price_source is not PriceSource.MANUAL else None,
                price_sync_message=None,
                price_synced_at=datetime.now(UTC)
                if write.price_source is not PriceSource.MANUAL else None,
            )
        if not write.context_price_references:
            values["context_price_tiers"] = []
        saved = self._save("model", values, item_id)
        self._project_accepted_model_price(UUID(str(saved["id"])))
        return self.registry()

    def runtime_check_plan(self, runtime_id: UUID) -> RuntimeCheckPlan:
        return RuntimeDiagnostics(self._repository, self._settings).plan(runtime_id)

    def check_runtime(
        self, runtime_id: UUID, selection: RuntimeCheckSelection, *, actor: str,
    ) -> RuntimeHealth:
        return RuntimeDiagnostics(self._repository, self._settings).check(
            runtime_id, selection, actor,
        )

    def check_budget_delegation(
        self,
        gateway_id: UUID,
        person_id: str,
    ) -> BudgetDelegationReadiness:
        configuration = self._settings.budget_delegation_targets.get(gateway_id)
        if (
            not self._settings.budget_execution_enabled
            or gateway_id not in self._settings.budget_execution_target_ids
            or configuration is None
            or not self._settings.ledger_table_endpoint
        ):
            raise HTTPException(status_code=409, detail="Budget delegation is not configured")
        target = self._repository.get_apim_target(gateway_id)
        registry = self._repository.registry()
        runtimes = {
            runtime["id"]
            for runtime in registry["runtimes"]
            if runtime.get("gateway_profile_id") == gateway_id and runtime["enabled"]
        }
        route = None
        for model in registry["models"]:
            if not model["enabled"] or model.get("runtime_id") not in runtimes:
                continue
            selected = self._repository.invocation_route(model["runtime_id"], model["id"])
            if selected is not None and str(selected.get("gateway_id")) == str(gateway_id):
                route = selected
                break
        if (
            target is None
            or route is None
            or route.get("gateway_implementation") != GatewayKind.APIM
        ):
            raise HTTPException(status_code=409, detail="A registered APIM route is required")
        destination = urlsplit(str(route.get("gateway_base_url") or ""))
        origins = {urlsplit(str(origin)).netloc.lower() for origin in configuration.allowed_origins}
        if (
            destination.scheme != "https"
            or destination.netloc.lower() not in origins
            or destination.username
            or destination.password
            or destination.query
            or destination.fragment
            or destination.path.rstrip("/") != "/" + str(target["api_path"]).strip("/")
        ):
            raise HTTPException(status_code=503, detail="Budget delegation origin is not trusted")
        service_hash = hashlib.sha256(str(target["service_name"]).lower().encode()).hexdigest()
        partition = f"budget-protocol|{service_hash}|{target['api_id']}"
        try:
            with AtomicTableLedger(
                self._settings.ledger_table_endpoint,
                self._settings.ledger_table_name,
                timeout=2,
            ) as store:
                entity = store.read_entity(partition, "F")
            fence = json.loads(entity["Document"]) if entity is not None else {}
            mode = fence.get("mode")
            native = str(route.get("native_gateway_id") or "managed")
            revisions = fence.get("gateways", {}).get(native, [])
            revision = fence.get("validation_revision") or (
                revisions[0] if isinstance(revisions, list) and revisions else None
            )
            if (
                fence.get("protocol_version") != 2
                or mode not in {"prepared", "paused", "active"}
                or fence.get("gateway_profile_id") != str(gateway_id)
                or fence.get("api_id") != target["api_id"]
                or str(fence.get("apim_service_id", "")).lower()
                != str(target["service_name"]).lower()
                or not isinstance(revision, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision)
                or revision not in revisions
            ):
                raise ValueError("An exact registered protocol revision is required")
            decrypted = self._decrypt_route(route)
            with httpx.Client(timeout=8, follow_redirects=False) as client:
                headers = OpenAICompatibleGatewayAdapter(GatewayKind.APIM, client)._auth_headers(
                    decrypted,
                )
                if not any(name.lower() == "ocp-apim-subscription-key" for name in headers) or any(
                    name.lower() == "authorization" for name in headers
                ):
                    raise ValueError("A configured APIM subscription credential is required")
                provider = ManagedIdentityTokenProvider()
                valid_token = provider.token(configuration.audience)
                destinations = [(native, destination.geturl().rstrip("/"))]
                cases: tuple[tuple[str, int, str, bool, bool], ...]
                if mode == "paused":
                    destinations = []
                    for deployment in self._repository.list_gateway_deployments(gateway_id):
                        gateway_native = str(deployment["native_gateway_id"])
                        if gateway_native not in fence["gateways"]:
                            continue
                        endpoint = urlsplit(str(deployment["endpoint_url"]))
                        api_path = "/" + str(target["api_path"]).strip("/")
                        if (
                            endpoint.scheme != "https"
                            or endpoint.netloc.lower() not in origins
                            or endpoint.username
                            or endpoint.password
                            or endpoint.query
                            or endpoint.fragment
                            or endpoint.path.rstrip("/") not in {"", api_path}
                        ):
                            raise ValueError("A registered fenced Gateway origin is not trusted")
                        destinations.append(
                            (gateway_native, endpoint._replace(path=api_path).geturl())
                        )
                    if {value[0] for value in destinations} != set(fence["gateways"]):
                        raise ValueError(
                            "Every fenced native Gateway must have a registered endpoint"
                        )
                    cases = (
                        ("current_fenced", 503, valid_token, True, False),
                        ("candidate_fenced", 503, valid_token, True, True),
                    )
                else:
                    alternate = (
                        "https://management.azure.com/"
                        if configuration.audience != "https://management.azure.com/"
                        else "https://storage.azure.com/"
                    )
                    wrong_token = provider.token(alternate)
                    cases = (
                        ("valid_identity", 200, valid_token, True, True),
                        ("wrong_audience", 401, wrong_token, True, True),
                        ("missing_person", 403, valid_token, False, True),
                    )
                checks: list[BudgetDelegationCheck] = []
                for gateway_native, base in destinations:
                    for case, expected, token, include_person, explicit_revision in cases:
                        status = None
                        actual_code = None
                        expected_code = "budget_protocol_fenced" if mode == "paused" else None
                        address = (
                            base + (f";rev={revision}" if explicit_revision else "") + "/v1/models"
                        )
                        try:
                            response = client.get(
                                address,
                                headers={
                                    **headers,
                                    "x-turnstile-delegation-token": "Bearer " + token,
                                    **({"x-user-id": person_id} if include_person else {}),
                                },
                                timeout=2 if mode == "paused" else 8,
                            )
                            status = response.status_code
                            if mode == "paused":
                                try:
                                    body = response.json()
                                    error = body.get("error") if isinstance(body, dict) else None
                                    if (
                                        isinstance(error, dict)
                                        and error.get("code") == expected_code
                                    ):
                                        actual_code = expected_code
                                except ValueError:
                                    pass
                        except httpx.HTTPError:
                            pass
                        checks.append(
                            BudgetDelegationCheck.model_validate(
                                {
                                    "case": case,
                                    "expected_status": expected,
                                    "actual_status": status,
                                    "native_gateway_id": gateway_native,
                                    "expected_error_code": expected_code,
                                    "actual_error_code": actual_code,
                                }
                            )
                        )
        except Exception as error:
            logger.warning(
                "Budget delegation readiness unavailable error_type=%s",
                type(error).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Budget delegation readiness could not be verified",
            ) from None
        ready = bool(checks) and all(
            check.actual_status == check.expected_status
            and (
                check.expected_error_code is None
                or check.actual_error_code == check.expected_error_code
            )
            for check in checks
        )
        logger.info(
            "Budget delegation readiness gateway=%s revision=%s ready=%s",
            gateway_id,
            revision,
            ready,
        )
        return BudgetDelegationReadiness(
            gateway_profile_id=gateway_id,
            native_gateway_id=native,
            revision=revision,
            checked_at=datetime.now(UTC),
            ready=ready,
            checks=checks,
            protocol_mode=mode,
            verification=self._budget_verification_options(gateway_id, person_id, fence, registry)
            if ready else None,
        )

    def _person_model_policy(self, person_id: str) -> set[str] | None:
        policies = self._repository.list_user_model_policies([person_id])
        policy = next((row for row in policies if str(row["user_id"]) == person_id), None)
        return (
            {str(value) for value in (policy.get("model_ids") or [])}
            if policy is not None else None
        )

    @staticmethod
    def _model_policy_allows(route: dict[str, Any], policy: set[str] | None) -> bool:
        return (
            str(route["model_id"]) in policy
            if policy is not None else not route.get("assignment_required", False)
        )

    def _budget_verification_options(
        self, gateway_id: UUID, person_id: str, fence: dict[str, Any], registry: dict[str, Any],
    ) -> BudgetExecutionVerificationOptions | None:
        if fence.get("mode") != "paused" or fence.get("validation_person_id") != person_id:
            return None
        try:
            application_id = UUID(str(fence.get("validation_application_id")))
        except ValueError:
            return None
        application = self._repository.get_gateway_application(application_id)
        if (
            application is None or application["status"] != "active"
            or application["system_managed"]
            or str(application["gateway_profile_id"]) != str(gateway_id)
        ):
            return None
        subscriptions = self._repository.list_gateway_application_subscriptions([application_id])
        if sum(
            item["state"] == "active" and item["scope_exists"]
            and item["apim_subscription_id"] == fence.get("validation_subscription_id")
            and str(item["gateway_profile_id"]) == str(gateway_id)
            for item in subscriptions
        ) != 1:
            return None
        policy = self._person_model_policy(person_id)
        models = []
        for model in registry["models"]:
            if not model["enabled"] or any(operation in (model.get("capabilities") or [])
                                           for operation in ("image_generation", "decision")):
                continue
            route = self._repository.invocation_route(model["runtime_id"], model["id"])
            if (
                route is not None and str(route.get("gateway_id")) == str(gateway_id)
                and self._model_policy_allows(route, policy)
            ):
                models.append(UUID(str(model["id"])))
        return BudgetExecutionVerificationOptions(application_id=application_id, model_ids=models)

    def check_budget_execution(
        self,
        gateway_id: UUID,
        write: BudgetExecutionVerificationRequest,
        person_id: str,
        applications: ApplicationAccessService,
    ) -> BudgetExecutionVerification:
        configuration = self._settings.budget_delegation_targets.get(gateway_id)
        route = self._repository.invocation_route(None, write.model_id)
        target = self._repository.get_apim_target(gateway_id)
        if (
            not self._settings.budget_execution_enabled
            or gateway_id not in self._settings.budget_execution_target_ids
            or configuration is None
            or not self._settings.ledger_table_endpoint
            or route is None
            or target is None
            or str(route.get("gateway_id")) != str(gateway_id)
            or route.get("gateway_implementation") != GatewayKind.APIM
                 or any(operation in (route.get("model_capabilities") or [])
                     for operation in ("image_generation", "decision"))
        ):
            raise HTTPException(
                status_code=409, detail="An enabled text execution route is required"
            )
        destination = urlsplit(str(route.get("gateway_base_url") or ""))
        origins = {urlsplit(str(origin)).netloc.lower() for origin in configuration.allowed_origins}
        if (
            destination.scheme != "https"
            or destination.netloc.lower() not in origins
            or destination.username
            or destination.password
            or destination.query
            or destination.fragment
            or destination.path.rstrip("/") != "/" + str(target["api_path"]).strip("/")
        ):
            raise HTTPException(status_code=503, detail="Budget verification origin is not trusted")
        native = str(route.get("native_gateway_id") or "managed")
        service_hash = hashlib.sha256(str(target["service_name"]).lower().encode()).hexdigest()
        partition = f"budget-protocol|{service_hash}|{target['api_id']}"
        moment = datetime.now(UTC)
        scopes = [
            LedgerScope(
                scope_type="person", scope_id=person_id, period_start=moment.date().replace(day=1)
            ),
            LedgerScope(
                scope_type="application",
                scope_id=str(write.application_id),
                period_start=moment.date().replace(day=1),
            ),
        ]
        with AtomicTableLedger(
            self._settings.ledger_table_endpoint,
            self._settings.ledger_table_name,
            timeout=2,
        ) as store:
            entity = store.read_entity(partition, "F")
            fence = json.loads(entity["Document"]) if entity else {}
            revision = fence.get("validation_revision")
            if (
                fence.get("protocol_version") != 2
                or fence.get("mode") != "paused"
                or fence.get("gateway_profile_id") != str(gateway_id)
                or fence.get("api_id") != target["api_id"]
                or str(fence.get("apim_service_id", "")).lower()
                != str(target["service_name"]).lower()
                or fence.get("validation_application_id") != str(write.application_id)
                or fence.get("validation_person_id") != person_id
                or not isinstance(revision, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision)
                or revision not in fence.get("gateways", {}).get(native, [])
            ):
                raise HTTPException(
                    status_code=409, detail="An exact isolated two-scope candidate is required"
                )
            for protected_id in self._settings.budget_execution_target_ids:
                protected = self._repository.get_apim_target(protected_id)
                if protected is None:
                    raise HTTPException(
                        status_code=409, detail="Every shared budget target must be fenced"
                    )
                protected_hash = hashlib.sha256(
                    str(protected["service_name"]).lower().encode()
                ).hexdigest()
                entity = store.read_entity(
                    f"budget-protocol|{protected_hash}|{protected['api_id']}", "F"
                )
                state = json.loads(entity["Document"]) if entity else {}
                if (
                    state.get("protocol_version") != 2
                    or state.get("mode") != "paused"
                    or state.get("epoch") != fence["epoch"]
                    or state.get("gateway_profile_id") != str(protected_id)
                    or state.get("api_id") != protected["api_id"]
                    or str(state.get("apim_service_id", "")).lower()
                    != str(protected["service_name"]).lower()
                ):
                    raise HTTPException(
                        status_code=409, detail="Every shared budget target must be fenced"
                    )
            if not isinstance(self._repository, BudgetStatusRepository):
                raise HTTPException(status_code=503, detail="Execution scope status is unavailable")
            before = []
            for scope in scopes:
                scope_status = self._repository.budget_execution_status(scope)
                if (
                    scope_status is None
                    or scope_status["state"] != "active"
                    or str(scope_status["epoch"]) != fence["epoch"]
                ):
                    raise HTTPException(
                        status_code=409, detail="Both execution scopes must be ready"
                    )
                summary = store.read_summary(scope)
                if str(summary.epoch) != fence["epoch"] or not summary.enabled:
                    raise HTTPException(
                        status_code=409, detail="Both execution scope policies must be ready"
                    )
                before.append(summary)
            application = self._repository.get_gateway_application(write.application_id)
            if (
                application is None
                or str(application["gateway_profile_id"]) != str(gateway_id)
                or application["status"] != "active"
                or application["system_managed"]
            ):
                raise HTTPException(
                    status_code=409, detail="An active non-system Application is required"
                )
            subscriptions = [
                item
                for item in self._repository.list_gateway_application_subscriptions(
                    [write.application_id]
                )
                if item["state"] == "active"
                and item["scope_exists"]
                and item["apim_subscription_id"] == fence.get("validation_subscription_id")
                and str(item["gateway_profile_id"]) == str(gateway_id)
            ]
            if len(subscriptions) != 1:
                raise HTTPException(
                    status_code=409, detail="The isolated subscription is not ready"
                )
            request_id = str(uuid4())
            metadata = {
                name: "unattributed"
                for name in (
                    "organization_id",
                    "organization",
                    "department_id",
                    "department",
                    "project_id",
                    "project",
                    "agent_id",
                    "agent",
                    "workflow",
                )
            }
            request = ModelInvocationRequest.model_validate(
                {
                    "model_id": write.model_id,
                    "max_output_tokens": 32,
                    "messages": [{"role": "user", "content": "Reply exactly OK."}],
                    "metadata": {
                        **metadata,
                        "user_id": person_id,
                        "user": person_id,
                        "model_id": str(write.model_id),
                        "model": str(route["model_key"]),
                        "runtime": str(route["runtime_name"]),
                        "run_id": request_id,
                        "request_source": "budget-verification",
                    },
                }
            )
            self._assert_model_allowed(request, route, request_id)
            credential = applications.reveal_application_subscription_key(
                write.application_id,
                subscriptions[0]["id"],
                "primary",
            )
            route = self._model_protocol_route(self._decrypt_route(route))
            route.update(
                gateway_base_url=destination.geturl().rstrip("/") + f";rev={revision}",
                gateway_auth_type="api_key",
                gateway_credential=credential.value,
                gateway_config={
                    **dict(route.get("gateway_config") or {}),
                    "header_name": "Ocp-Apim-Subscription-Key",
                },
                budget_delegation_token=SecretStr(
                    ManagedIdentityTokenProvider().token(configuration.audience)
                ),
            )
            invocation = None
            actual_status = 200
            correlation = None
            try:
                invocation = self._invoke_resolved(request, route, request_id, timeout_ms=8_000)
                correlation = invocation.correlation_id
            except HTTPException as error:
                actual_status = error.status_code
                correlation = (error.headers or {}).get("x-correlation-id")
            readbacks = []
            for summary in before:
                after = None
                attempt = None
                try:
                    after = store.read_summary(summary.scope)
                    if correlation:
                        identity = LedgerAttemptIdentity(
                            scope=summary.scope,
                            gateway_profile_id=gateway_id,
                            native_gateway_id=native,
                            correlation_id=correlation,
                            model_id=str(write.model_id),
                            admitted_at=moment,
                        )
                        entity = store.read_entity(summary.scope.partition, identity.row_key)
                        if entity is not None:
                            observed = LedgerAttempt.model_validate_json(entity["Document"])
                            if (
                                observed.identity.scope != summary.scope
                                or observed.epoch != summary.epoch
                                or observed.identity.attempt_key != identity.attempt_key
                                or observed.identity.model_id != str(write.model_id)
                            ):
                                raise ValueError("Execution receipt identity does not match")
                            attempt = observed
                except Exception as error:
                    logger.warning(
                        "Execution verification readback unavailable error_type=%s",
                        type(error).__name__,
                    )
                readbacks.append(
                    BudgetExecutionScopeReadback(before=summary, after=after, attempt=attempt)
                )
            usage = invocation.usage if invocation is not None else None
            total = (
                None
                if usage is None or usage.estimated
                else (
                    usage.input_tokens + usage.cached_tokens + usage.output_tokens
                )
            )
            return BudgetExecutionVerification(
                gateway_profile_id=gateway_id,
                native_gateway_id=native,
                revision=revision,
                checked_at=datetime.now(UTC),
                request_id=request_id,
                actual_status=actual_status,
                invocation=invocation,
                scopes=readbacks,
                settled=total is not None
                and all(
                    item.attempt is not None
                    and item.attempt.state == "settled"
                    and item.attempt.actual_tokens == total
                    for item in readbacks
                ),
            )

    def _assert_model_allowed(
        self,
        request: ModelInvocationRequest | ImageInvocationRequest | DecisionInvocationRequest,
        route: dict[str, Any],
        request_id: str,
    ) -> None:
        """Enforce the person's model policy on the path where we are the caller.

        The APIM ledger check only covers employee tokens, because that is the only path
        where the identity is unforgeable. On this path the identity comes from our own
        UI, so the trustworthy place to enforce is here, before the outbound call: the
        policy is in PostgreSQL next to us, which is also exact rather than eventually
        consistent. This is not the removed policy-snapshot endpoint — APIM still never
        calls back into this application, so nothing we operate sits on the employee
        inference path.
        """
        user_id = request.metadata.user_id
        if not user_id:
            return
        if self._model_policy_allows(route, self._person_model_policy(user_id)):
            return
        self._deny(
            request,
            route,
            "model_not_assigned",
            request_id,
            model_admission="denied",
        )

    def _assert_budget_available(
        self,
        request: ModelInvocationRequest | ImageInvocationRequest | DecisionInvocationRequest,
        route: dict[str, Any],
        request_id: str,
    ) -> None:
        """Enforce the monthly allowance on the same path, for the same reason.

        The employee path reserves against a Table Storage ledger because APIM cannot ask
        this application anything. Here the allowance and the usage are both one query
        away, so the check is a direct comparison instead. It is intentionally not a
        reservation for text calls: telemetry lands seconds later, so a text burst can
        overshoot slightly. Image generation additionally performs atomic billable-attempt
        admission after this preflight and retains conservative unknown-outcome holds.

        A department in audit mode is not blocked, and a person with no budget row is not
        blocked either — the same two rules the ledger admission already follows.
        """
        user_id = request.metadata.user_id
        if isinstance(route.get("budget_delegation_token"), SecretStr):
            return
        if not user_id:
            return
        now = datetime.now(UTC)
        period_start = date(now.year, now.month, 1)
        period_end = (
            date(now.year + 1, 1, 1) if now.month == 12 else date(now.year, now.month + 1, 1)
        )
        state = self._repository.person_budget_state(user_id, period_start, period_end)
        if state is None:
            return
        if state.get("budget_mode", "tokens") == "usd":
            raise HTTPException(
                status_code=503,
                detail="USD budgets require verified atomic APIM admission",
            )
        if str(state["mode"]) != "block":
            return
        if isinstance(request, ImageInvocationRequest):
            remaining = int(state["token_limit"]) - int(state["used_tokens"])
            if remaining < request.reservation_tokens(
                str(route["model_key"]), request.validate_profile(route.get("image_profile"))
            ):
                self._deny(
                    request, route, "image_budget_insufficient", request_id,
                    budget_admission="denied",
                )
        if int(state["used_tokens"]) < int(state["token_limit"]):
            return
        self._deny(
            request,
            route,
            "budget_exceeded",
            request_id,
            budget_admission="denied",
        )

    def _prepare_budget_delegation(
        self, request: ModelInvocationRequest | ImageInvocationRequest | DecisionInvocationRequest,
        route: dict[str, Any],
    ) -> None:
        target = route.get("gateway_id")
        if target is None or not self._settings.budget_execution_enabled:
            return
        target_id = UUID(str(target))
        if target_id not in self._settings.budget_execution_target_ids:
            return
        try:
            if not isinstance(self._repository, BudgetScopeRepository):
                raise TypeError("Budget delegation requires a durable rollout")
            rollout = self._repository.active_budget_execution_rollout()
        except Exception as error:
            logger.warning("Budget rollout unavailable error_type=%s", type(error).__name__)
            raise HTTPException(
                status_code=503, detail="Budget rollout state is unavailable",
            ) from None
        if rollout is None or target_id not in rollout["gateway_profile_ids"]:
            return
        configuration = self._settings.budget_delegation_targets.get(target_id)
        if configuration is None or route.get("gateway_implementation") != GatewayKind.APIM:
            raise HTTPException(
                status_code=503, detail="Trusted budget delegation is not configured",
            )
        destination = urlsplit(str(route.get("gateway_base_url") or ""))
        origins = {urlsplit(str(value)).netloc.lower() for value in configuration.allowed_origins}
        if (
            destination.scheme != "https" or destination.netloc.lower() not in origins
            or destination.username or destination.password
            or destination.query or destination.fragment
        ):
            raise HTTPException(
                status_code=503,
                detail="Budget delegation target does not match its trusted origin",
            )
        if not request.metadata.user_id.strip() or request.metadata.user_id == "unattributed":
            raise HTTPException(
                status_code=403, detail="Budget delegation requires a verified person",
            )
        try:
            token = ManagedIdentityTokenProvider().token(configuration.audience)
        except Exception as error:
            logger.warning("Budget delegation unavailable error_type=%s", type(error).__name__)
            raise HTTPException(
                status_code=503, detail="Budget delegation identity is unavailable",
            ) from None
        route["budget_delegation_token"] = SecretStr(token)

    def _deny(
        self,
        request: ModelInvocationRequest | ImageInvocationRequest | DecisionInvocationRequest,
        route: dict[str, Any],
        reason: str,
        request_id: str,
        *,
        budget_admission: str | None = None,
        model_admission: str | None = None,
    ) -> NoReturn:
        """Reject the call and leave a trace of it.

        A denial that nobody can see is the worse of the two failure modes: it still stops
        the person working but the dashboard cannot explain why. The APIM policy learned
        this the hard way and emits its denial before returning, so this path does the
        same. Usage is zero and `estimated` is false because a rejected request's zero is
        exact, not missing.

        The model and runtime come from the resolved route, never from the caller's
        metadata. Those metadata fields are display values a client asserts, and the
        console asserted registry UUIDs for both: three denial rows landed with a bare
        runtime UUID, which then rendered as its own bar beside the same runtime's real
        name in the runtime distribution. That is the split identity migrations 013 and
        018 collapsed for the model and the organization, arriving through the client
        instead of through a second column. The route is the routing decision this
        service just made, so it is authoritative and costs no extra query.

        The provider stays `unattributed` on purpose: the runtime is a decision we made,
        but no provider was ever contacted.
        """
        metadata = request.metadata
        try:
            self._repository.write_token_usage(
                TokenUsageRecord(
                    id=request_id,
                    request_id=request_id,
                    correlation_id=request_id,
                    ts=datetime.now(UTC),
                    team=metadata.department or "unattributed",
                    organization=metadata.organization,
                    organization_id=metadata.organization_id,
                    department=metadata.department,
                    department_id=metadata.department_id,
                    project=metadata.project,
                    project_id=metadata.project_id,
                    user=metadata.user,
                    user_id=metadata.user_id,
                    agent=metadata.agent,
                    agent_id=metadata.agent_id,
                    workflow=metadata.workflow,
                    run_id=metadata.run_id,
                    turn_index=metadata.turn_index,
                    provider="unattributed",
                    model=str(route["display_name"]),
                    model_id=str(route["model_id"]),
                    runtime=str(route["runtime_name"]),
                    request_source=metadata.request_source,
                    usage_domain=metadata.usage_domain,
                    input_tokens=0,
                    cached_tokens=0,
                    output_tokens=0,
                    et=0.0,
                    # The column is NOT NULL CHECK (et_coeff_m > 0), so a denial cannot
                    # store zero even though its ET is zero by construction. The value is
                    # the catalog default, which is what a real request of this shape
                    # would have been priced with.
                    et_coeff_m=self._settings.model_coefficients.get("default", 1.0),
                    latency_ms=0,
                    status="403",
                    status_code=403,
                    estimated_cost=0.0,
                    error_message=f"API policy denied: {reason}",
                    estimated=False,
                    ingest_source="policy",
                    budget_admission=budget_admission,
                    model_admission=model_admission,
                )
            )
        except Exception:  # noqa: BLE001 - the denial itself must not depend on telemetry
            # Loud on purpose: this except exists so a telemetry failure cannot stop the
            # denial, and it once hid a CHECK violation that silently dropped every trace.
            logger.exception("Denial trace could not be written for %s", reason)
        raise HTTPException(
            status_code=403,
            detail=reason,
            headers={"x-request-id": request_id},
        )

    def _invocation_route(
        self, request: ModelInvocationRequest | ImageInvocationRequest | DecisionInvocationRequest,
    ) -> dict[str, Any] | None:
        if request.model_id is not None:
            current = next((model for model in self._repository.registry()["models"]
                            if model["id"] == request.model_id), None)
            if current is not None and current["enabled"] is False:
                raise HTTPException(status_code=403, detail={
                    "code": "model_disabled", "message": "The requested model is disabled",
                })
        explicit = (
            request.gateway_profile_id is not None or request.gateway_deployment_id is not None
        )
        if explicit and not self._settings.apim_target_registry_enabled:
            raise HTTPException(
                status_code=503, detail="Explicit APIM Gateway selection is not enabled"
            )
        if explicit:
            route = self._repository.invocation_route(
                request.runtime_id, request.model_id,
                gateway_profile_id=request.gateway_profile_id,
                gateway_deployment_id=request.gateway_deployment_id,
            )
        else:
            route = self._repository.invocation_route(request.runtime_id, request.model_id)
        if route is not None:
            if explicit and (
                str(route.get("gateway_id")) != str(request.gateway_profile_id)
                or str(route.get("gateway_deployment_id")) != str(request.gateway_deployment_id)
                or route.get("gateway_implementation") != GatewayKind.APIM
            ):
                raise HTTPException(
                    status_code=409, detail="Resolved Gateway does not match selection"
                )
            if (
                route.get("gateway_deployment_id") is not None
                and route.get("native_gateway_id") != "managed"
                and not self._settings.self_hosted_gateway_enabled
            ):
                raise HTTPException(status_code=503, detail="Self-hosted Gateway is not enabled")
            self._validate_invocation_endpoint(route)
        return route

    def _validate_invocation_endpoint(self, route: Mapping[str, Any]) -> None:
        if route.get("gateway_implementation") != GatewayKind.APIM:
            return
        try:
            endpoint = urlsplit(str(route.get("gateway_base_url") or ""))
            if (
                endpoint.scheme != "https" or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.port not in (None, 443)
            ):
                raise ValueError("Gateway endpoint is not trusted")
            deployment_id = route.get("gateway_deployment_id")
            if deployment_id is None:
                if route.get("gateway_target") or route.get("native_gateway_id") not in (
                    None,
                    "managed",
                ):
                    raise ValueError("Registered Gateway deployment is missing")
                return
            target = route.get("gateway_target")
            deployment = route.get("gateway_deployment")
            if not isinstance(target, Mapping) or not isinstance(deployment, Mapping) or (
                str(target.get("gateway_profile_id")) != str(route.get("gateway_id"))
                or str(deployment.get("id")) != str(deployment_id)
                or deployment.get("native_gateway_id") != route.get("native_gateway_id")
                or str(deployment.get("endpoint_url")).rstrip("/")
                != str(route.get("gateway_base_url")).rstrip("/")
            ):
                raise ValueError("Gateway route does not match its deployment")
            if (
                deployment.get("kind") == "self_hosted"
                and not self._settings.self_hosted_gateway_enabled
            ):
                raise ValueError("Self-hosted Gateway is not enabled")
            validate_gateway_endpoint(
                target, deployment, route.get("gateway_inventory"),
                trusted_endpoint=self._settings.self_hosted_gateway_trusted_endpoints.get(
                    UUID(str(deployment_id)),
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=503, detail="Gateway route is not verified") from error

    def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        request_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> ModelInvocationResponse:
        request_id = request_id or str(uuid4())
        route = self._invocation_route(request)
        if route is None:
            raise HTTPException(
                status_code=404, detail="No enabled model route matches the request"
            )
        route = self._model_protocol_route(route)
        if "decision" in (route.get("model_capabilities") or []):
            raise HTTPException(status_code=409, detail="Use the Decision endpoint for this model")
        if "image_generation" in (route.get("model_capabilities") or []):
            raise HTTPException(
                status_code=409, detail="Use the image generation endpoint for this model"
            )
        try:
            route = model_protocol_route(
                route, request.api_format,
                tools=bool(request.tools) or any(
                    message.tool_calls or message.role == "tool" for message in request.messages
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if self._settings.production and route.get("gateway_implementation") != GatewayKind.APIM:
            raise HTTPException(
                status_code=409,
                detail="Production model invocations must use Azure API Management",
            )
        self._assert_model_allowed(request, route, request_id)
        self._prepare_budget_delegation(request, route)
        self._assert_budget_available(request, route, request_id)
        route = self._decrypt_route(route)
        return self._invoke_resolved(request, route, request_id, timeout_ms=timeout_ms)

    def invoke_decision(
        self, request: DecisionInvocationRequest, *, request_id: str | None = None,
    ) -> DecisionInvocationResponse:
        request_id = request_id or str(uuid4())
        route = self._invocation_route(request)
        if route is None:
            raise HTTPException(status_code=404, detail="No enabled Decision route matches")
        route = self._model_protocol_route(route)
        if "decision" not in (route.get("model_capabilities") or []):
            raise HTTPException(status_code=409, detail="This model does not support Decision")
        if route.get("gateway_implementation") != GatewayKind.APIM:
            raise HTTPException(status_code=409, detail="Decision invocation requires APIM")
        self._assert_model_allowed(request, route, request_id)
        self._prepare_budget_delegation(request, route)
        self._assert_budget_available(request, route, request_id)
        return self._invoke_resolved(request, self._decrypt_route(route), request_id)

    @overload
    def _invoke_resolved(
        self, request: ModelInvocationRequest, route: dict[str, Any], request_id: str,
        *, timeout_ms: int | None = None,
    ) -> ModelInvocationResponse: ...

    @overload
    def _invoke_resolved(
        self, request: DecisionInvocationRequest, route: dict[str, Any], request_id: str,
        *, timeout_ms: int | None = None,
    ) -> DecisionInvocationResponse: ...

    def _invoke_resolved(
        self, request: ModelInvocationRequest | DecisionInvocationRequest,
        route: dict[str, Any], request_id: str,
        *, timeout_ms: int | None = None,
    ) -> ModelInvocationResponse | DecisionInvocationResponse:
        if timeout_ms is not None:
            runtime_config = dict(route.get("runtime_config") or {})
            configured_timeout = float(runtime_config.get("timeout_seconds", 120))
            runtime_config["timeout_seconds"] = min(
                configured_timeout,
                timeout_ms / 1_000,
            )
            route["runtime_config"] = runtime_config
        route["request_id"] = request_id
        target = route.get("gateway_target") or {}
        subscription_id = target.get("dashboard_subscription_id")
        target_id = UUID(str(route["gateway_id"])) if route.get("gateway_id") else None
        attempt = PlatformInvocationAttempt(
            request_id=request_id, started_at=datetime.now(UTC),
            metadata=request.metadata.model_copy(update={
                "model_id": str(route["model_id"]), "model": str(route["display_name"]),
                "runtime": str(route["runtime_name"]),
            }),
            model_id=UUID(str(route["model_id"])), model_key=str(route["model_key"]),
            model_name=str(route["display_name"]), runtime_id=UUID(str(route["runtime_id"])),
            runtime_name=str(route["runtime_name"]), provider_name=str(route["provider_name"]),
            apim_target_id=target_id, gateway_deployment_id=route.get("gateway_deployment_id"),
            native_gateway_id=route.get("native_gateway_id"),
            apim_service_id=target.get("arm_resource_id"),
            application_id=application_id_for(target_id, subscription_id)
            if target_id is not None and subscription_id else None,
        )
        try:
            self._repository.begin_platform_invocation(attempt)
        except Exception as error:
            logger.error(
                "Invocation recording unavailable request_id=%s error_type=%s",
                request_id,
                type(error).__name__,
            )
            raise HTTPException(
                status_code=503, detail="Invocation recording is unavailable; no request was sent",
                headers={"x-request-id": request_id},
            ) from None
        started = time.monotonic()
        try:
            adapter = self._router.adapter(route)
            if isinstance(request, DecisionInvocationRequest):
                if not isinstance(adapter, MicrosoftDecisionGatewayAdapter):
                    raise GatewayInvocationError("Decision route protocol differs", status_code=409)
                result = adapter.invoke(request, route)
            else:
                result = adapter.invoke(request, route)
        except GatewayInvocationError as error:
            headers = dict(error.headers)
            upstream_request_id = headers.get("x-request-id") or headers.get("request-id")
            if upstream_request_id:
                headers["x-upstream-request-id"] = upstream_request_id
            headers["x-request-id"] = request_id
            recorded = self._finish_invocation(PlatformInvocationCompletion.model_validate({
                "request_id": request_id, "completed_at": datetime.now(UTC), "outcome": "failed",
                "status_code": error.status_code, "latency_ms": elapsed_ms(started),
                "gateway_correlation_id": headers.get("x-correlation-id"),
                "upstream_request_id": upstream_request_id or headers.get("x-ms-request-id")
                or headers.get("apim-request-id"), "failure_kind": error.failure_kind,
            }))
            headers["x-platform-trace-state"] = "recorded" if recorded else "pending"
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
                headers=headers,
            ) from error
        except Exception as error:
            self._finish_invocation(PlatformInvocationCompletion(
                request_id=request_id, completed_at=datetime.now(UTC), outcome="failed",
                status_code=502, latency_ms=elapsed_ms(started), failure_kind="internal_error",
            ))
            logger.error(
                "Invocation failed request_id=%s error_type=%s", request_id, type(error).__name__
            )
            raise HTTPException(
                status_code=502, detail="Gateway invocation failed",
                headers={"x-request-id": request_id},
            ) from None

        latency_ms = elapsed_ms(started)
        self._finish_invocation(PlatformInvocationCompletion(
            request_id=request_id, completed_at=datetime.now(UTC), outcome="succeeded",
            status_code=200, latency_ms=latency_ms, gateway_correlation_id=result.correlation_id,
            upstream_request_id=result.upstream_request_id,
        ))
        correlation_id = result.correlation_id
        estimated_cost = self._estimated_cost(route, result.usage)
        values: dict[str, Any] = dict(
            request_id=request_id,
            correlation_id=correlation_id,
            upstream_request_id=result.upstream_request_id,
            provider=str(route["provider_name"]),
            runtime=str(route["runtime_name"]),
            model=str(route["model_key"]),
            gateway=result.gateway,
            gateway_profile_id=route.get("gateway_id"),
            gateway_deployment_id=route.get("gateway_deployment_id"),
            native_gateway_id=route.get("native_gateway_id"),
            latency_ms=latency_ms,
            usage=result.usage,
            estimated_cost=estimated_cost,
        )
        if isinstance(request, DecisionInvocationRequest):
            return DecisionInvocationResponse(
                **values, answers=cast(dict[str, Any], result.answers),
            )
        response = ModelInvocationResponse(
            **values, content=result.content, tool_calls=list(result.tool_calls) or None,
        )
        response._responses_output = result.responses_output
        response._anthropic_content = result.anthropic_content
        return response

    def _finish_invocation(self, completion: PlatformInvocationCompletion) -> bool:
        try:
            self._repository.finish_platform_invocation(completion)
        except Exception as error:
            logger.error(
                "Invocation outcome pending request_id=%s error_type=%s",
                completion.request_id, type(error).__name__,
            )
            return False
        return True

    def generate_image(
        self, request: ImageInvocationRequest, *, role: str
    ) -> ImageInvocationResponse:
        import hashlib
        import json

        from turnstile_core.domain.billable_requests import (
            BillableBudgetExceeded,
            BillableRequestPlan,
        )

        request_id = str(uuid4())
        route = self._invocation_route(request)
        if route is None:
            raise HTTPException(status_code=404, detail="No enabled image model route matches")
        if (
            role not in (route.get("model_allowed_roles") or [])
            or role not in (route.get("runtime_allowed_roles") or [])
        ):
            raise HTTPException(status_code=403, detail="Role cannot use this image model")
        if "image_generation" not in (route.get("model_capabilities") or []):
            raise HTTPException(status_code=409, detail="This model does not generate images")
        if route.get("provider_kind") != ProviderKind.MICROSOFT_FOUNDRY:
            raise HTTPException(status_code=409, detail="Image generation requires Foundry")
        if route.get("gateway_implementation") != GatewayKind.APIM:
            raise HTTPException(status_code=409, detail="Image generation requires APIM")
        try:
            profile = request.validate_profile(route.get("image_profile"))
            reserved_tokens = request.reservation_tokens(str(route["model_key"]), profile)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        self._assert_model_allowed(request, route, request_id)
        self._prepare_budget_delegation(request, route)
        self._assert_budget_available(request, route, request_id)
        route = self._decrypt_route(route)
        now = datetime.now(UTC)
        try:
            attempt = self._repository.begin_billable_request(BillableRequestPlan(
                operation_key="invocation:" + request_id,
                plan_sha256=hashlib.sha256(json.dumps({
                    "request": request.model_dump(mode="json"),
                    "profile": profile.model_dump(mode="json"),
                }, sort_keys=True).encode()).hexdigest(),
                scope_type="person", scope_id=request.metadata.user_id,
                period_start=date(now.year, now.month, 1),
                model_id=str(route["model_id"]), model_key=str(route["model_key"]),
                reserved_tokens=reserved_tokens,
                admission_authority="apim_execution"
                if isinstance(route.get("budget_delegation_token"), SecretStr) else "postgresql",
            ))
        except BillableBudgetExceeded:
            self._deny(request, route, "image_budget_insufficient", request_id,
                       budget_admission="denied")
        request_id = str(attempt.id)
        route["request_id"] = request_id
        started = time.monotonic()
        try:
            result = self._router.generate_image(request, route)
        except GatewayInvocationError as error:
            measured_usage = error.usage
            actual_tokens = (
                measured_usage.input_tokens + measured_usage.cached_tokens
                + measured_usage.output_tokens
                if measured_usage is not None and not measured_usage.estimated else None
            )
            self._repository.finish_billable_request(
                attempt.id, actual_tokens=actual_tokens,
                correlation_id=error.headers.get("x-correlation-id"),
                evidence={"status_code": error.status_code,
                          "gateway_profile_id": str(route.get("gateway_id", "")),
                          "native_gateway_id": route.get("native_gateway_id") or "managed",
                          "outcome": (
                              "response_invalid" if actual_tokens is not None else "uncertain"
                          ),
                          "usage": measured_usage.model_dump(mode="json")
                          if actual_tokens is not None and measured_usage is not None else None},
            )
            headers = {**error.headers, "x-request-id": request_id}
            raise HTTPException(
                status_code=error.status_code, detail=str(error), headers=headers
            ) from error
        actual_tokens = (
            result.usage.input_tokens + result.usage.cached_tokens + result.usage.output_tokens
            if result.usage is not None else None
        )
        self._repository.finish_billable_request(
            attempt.id, actual_tokens=actual_tokens, correlation_id=result.correlation_id,
            evidence={"status_code": 200,
                      "gateway_profile_id": str(route.get("gateway_id", "")),
                      "native_gateway_id": route.get("native_gateway_id") or "managed",
                      "usage": result.usage.model_dump(mode="json") if result.usage else None},
        )
        return ImageInvocationResponse(
            request_id=request_id,
            correlation_id=result.correlation_id or request_id,
            provider=str(route["provider_name"]), runtime=str(route["runtime_name"]),
            model=str(route["model_key"]), gateway=result.gateway,
            gateway_profile_id=route.get("gateway_id"),
            gateway_deployment_id=route.get("gateway_deployment_id"),
            native_gateway_id=route.get("native_gateway_id"),
            latency_ms=elapsed_ms(started), data=[result.image],
            size=result.size, quality=result.quality, output_format=result.output_format,
            usage=result.usage, estimated_cost=self._estimated_cost(route, result.usage),
        )

    @staticmethod
    def _estimated_cost(
        route: dict[str, Any], usage: InvocationUsage | None
    ) -> float | None:
        price = model_price_from_values(route)
        if usage is None or price is None or price.context_tiers and usage.estimated:
            return None
        return price.cost(
            usage.input_tokens,
            usage.cached_tokens,
            usage.output_tokens,
            usage.cache_write_tokens,
        )

    @staticmethod
    def _model_protocol_route(route: dict[str, Any]) -> dict[str, Any]:
        if "decision" in (route.get("model_capabilities") or []):
            return {**route, "runtime_config": {
                **dict(route.get("runtime_config") or {}),
                "path": "/providers/microsoft/v1/systemone", "api_format": "microsoft_decision",
            }}
        return model_protocol_route(route)

    def _save(self, kind: str, values: dict[str, Any], item_id: UUID | None) -> dict[str, Any]:
        try:
            if item_id is None:
                return self._repository.create_registry_item(kind, values)
            saved = self._repository.update_registry_item(kind, item_id, values)
            if saved is None:
                raise HTTPException(status_code=404, detail=f"Unknown {kind}: {item_id}")
            return saved
        except ModelAvailabilityConflict as error:
            raise HTTPException(status_code=409, detail={
                "code": "model_availability_conflict", "message": str(error),
            }) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    def _secret_values(
        self, write: GatewayProfileWrite | ProviderWrite, *, creating: bool
    ) -> dict[str, Any]:
        values = write.model_dump(mode="python", exclude={"credential"})
        credential = write.credential
        if credential is not None:
            plaintext = credential.get_secret_value()
            values["credential_ciphertext"] = self._cipher.encrypt(plaintext)
            values["credential_hint"] = credential_hint(plaintext)
        elif creating:
            values["credential_ciphertext"] = None
            values["credential_hint"] = None
        return values

    @staticmethod
    def _public_secret(row: dict[str, Any]) -> dict[str, Any]:
        public = dict(row)
        encrypted = public.pop("credential_ciphertext", None)
        public["credential"] = None
        public["credential_configured"] = encrypted is not None
        return public

    def _decrypt_route(self, route: dict[str, Any]) -> dict[str, Any]:
        self._validate_invocation_endpoint(route)
        result = dict(route)
        result["provider_credential"] = self._cipher.decrypt(
            result.pop("provider_credential_ciphertext", None)
        )
        result["gateway_credential"] = self._cipher.decrypt(
            result.pop("gateway_credential_ciphertext", None)
        )
        return result

    def price_catalog_models(self, query: str, *, limit: int = 60) -> PriceCatalogModelsResponse:
        """The models a person can point a registry model at, one row each.

        Deliberately a search over names rather than an automatic match: the caller picks, and
        the pick is stored. Matching on the registry alias would be wrong about as often as
        names are ambiguous, and nothing downstream would notice.
        """
        try:
            found = self._price_catalog.search_models(query, limit=limit)
        except Exception as error:  # noqa: BLE001 - an unreachable vendor is not a server fault
            raise HTTPException(
                status_code=503, detail=f"价目表暂时读取不到：{type(error).__name__}"
            ) from error
        return PriceCatalogModelsResponse(
            models=[
                PriceCatalogModel(
                    key=model.key,
                    label=model.label,
                    product=model.product,
                    source=model.source,
                    context_tier=model.resolved_context_tier,
                    text_to_image=model.text_to_image,
                )
                for model in found.models
            ],
            unavailable=list(found.unavailable),
        )

    def price_catalog_options(self, model_key: str) -> PriceCatalogOptionsResponse:
        try:
            found = self._price_catalog.options(model_key)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail=f"价目表暂时读取不到：{type(error).__name__}"
            ) from error
        if found is None:
            raise HTTPException(status_code=404, detail="价目表中没有这个模型")
        return PriceCatalogOptionsResponse(
            model_entry=PriceCatalogModel(
                key=found.model.key,
                label=found.model.label,
                product=found.model.product,
                source=found.model.source,
                context_tier=found.model.resolved_context_tier,
                text_to_image=found.model.text_to_image,
            ),
            options=[
                PriceCatalogOption(
                    reference=option.reference,
                    deployment=option.deployment,
                    regions=list(option.regions),
                    references_by_region=dict(option.references_by_region),
                    region_required=option.region_required,
                    input_per_million=option.entry.input_per_million,
                    output_per_million=option.entry.output_per_million,
                    cached_per_million=option.entry.cached_per_million,
                    cache_write_per_million=option.entry.cache_write_per_million,
                )
                for option in found.options
            ],
            unreadable=list(found.unreadable),
            other_meters=list(found.other_meters),
            note=found.note,
            complete=found.complete,
        )

    def sync_prices(self, only: Sequence[UUID] | None = None) -> PriceSyncResponse:
        registry = self.registry()
        summary = plan_price_sync(registry.models, self._price_catalog, only=only)
        written = self._repository.apply_model_price_sync(
            [
                {
                    "model_id": update.model_id,
                    "writes": update.writes,
                    "status": update.status.value,
                    "message": update.message,
                    "input_cost_per_million": update.input_cost_per_million,
                    "output_cost_per_million": update.output_cost_per_million,
                    "cached_cost_per_million": update.cached_cost_per_million,
                    "cache_write_cost_per_million": update.cache_write_cost_per_million,
                    "list_input_cost_per_million": update.list_input_cost_per_million,
                    "list_output_cost_per_million": update.list_output_cost_per_million,
                    "list_cached_cost_per_million": update.list_cached_cost_per_million,
                    "list_cache_write_cost_per_million": (
                        update.list_cache_write_cost_per_million
                    ),
                    "pending_list_price": update.pending_list_price,
                    "context_price_tiers": update.context_price_tiers,
                    "pending_context_price_tiers": update.pending_context_price_tiers,
                    "expected_context_price_tiers": update.expected_context_price_tiers,
                    "expected_source": update.expected_source,
                    "expected_reference": update.expected_reference,
                    "expected_discount_percent": update.expected_discount_percent,
                    "expected_list_prices": update.expected_list_prices,
                }
                for update in summary.updates
            ]
        )
        refreshed = self.registry()
        current_models = {model.id: model for model in refreshed.models}
        outcomes = []
        for update in summary.updates:
            current = current_models.get(update.model_id)
            superseded = current is None or current.price_sync_status is PriceSyncStatus.SUPERSEDED
            if update.writes and not superseded:
                self._project_accepted_model_price(update.model_id)
            outcomes.append(PriceSyncDetail(
                model_id=update.model_id, model_key=update.model_key,
                status=PriceSyncStatus.SUPERSEDED if superseded else update.status,
                message=current.price_sync_message if superseded and current else update.message,
            ))
        return PriceSyncResponse(
            considered=len(summary.updates),
            updated=written,
            unmapped=sum(item.status is PriceSyncStatus.UNMAPPED for item in outcomes),
            review_needed=sum(item.status is PriceSyncStatus.REVIEW_NEEDED for item in outcomes),
            stale=sum(item.status is PriceSyncStatus.STALE for item in outcomes),
            superseded=sum(item.status is PriceSyncStatus.SUPERSEDED for item in outcomes),
            details=[item for item in outcomes if item.status is not PriceSyncStatus.OK],
            registry=refreshed,
        )
