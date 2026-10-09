from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException

from turnstile_core.config import Settings
from turnstile_core.domain.billable_requests import BillableBudgetExceeded, BillableRequestPlan
from turnstile_core.domain.images import ImageInvocationRequest, ImageInvocationResponse
from turnstile_core.domain.models import ModelPrice, TokenUsageRecord
from turnstile_core.domain.runtime_models import (
    FOUNDRY_INFERENCE_RESOURCE,
    FOUNDRY_INFERENCE_ROLE_ID,
    BrandKey,
    ConnectionAuthMode,
    GatewayKind,
    GatewayProfile,
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
from turnstile_core.integrations.gateway import GatewayInvocationError, GatewayRouter, elapsed_ms
from turnstile_core.integrations.gateway_protocol import model_protocol_route
from turnstile_core.persistence.repository import QueryRepository
from turnstile_core.pricing.catalog import CompositeCatalog, build_default_catalog
from turnstile_core.pricing.sync import plan_price_sync
from turnstile_core.security import CredentialCipher, credential_hint

logger = logging.getLogger(__name__)


class ModelRuntimeService:
    _MODEL_ROUTING_FIELDS = ("provider_id", "runtime_id", "model_key", "upstream_model_id")

    def __init__(
        self,
        repository: QueryRepository,
        settings: Settings,
        router: GatewayRouter | None = None,
        price_catalog: CompositeCatalog | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._cipher = CredentialCipher.from_settings(settings)
        self._router = router or GatewayRouter()
        # Injectable so a test never reaches a vendor's website.
        self._price_catalog = price_catalog or build_default_catalog()

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
                    "expected_source": update.expected_source,
                    "expected_reference": update.expected_reference,
                    "expected_discount_percent": update.expected_discount_percent,
                }
                for update in summary.updates
            ]
        )
        # The plan intended `summary.written` writes and the repository performed `written`. The
        # difference is exactly the rows whose pricing changed while the sync was talking to the
        # price feed, so the guard refused them. Reported rather than absorbed: "considered 45,
        # updated 44" with nothing saying why is the kind of arithmetic nobody chases.
        superseded = max(summary.written - written, 0)
        details = [
            PriceSyncDetail(
                model_id=update.model_id,
                model_key=update.model_key,
                status=update.status,
                message=update.message,
            )
            for update in summary.updates
            if update.status is not PriceSyncStatus.OK
        ]
        refreshed = self.registry()
        if superseded:
            planned = {update.model_id for update in summary.updates if update.writes}
            details.extend(
                PriceSyncDetail(
                    model_id=model.id,
                    model_key=model.model_key,
                    status=PriceSyncStatus.SUPERSEDED,
                    message=model.price_sync_message,
                )
                for model in refreshed.models
                if model.id in planned
                and model.price_sync_status is PriceSyncStatus.SUPERSEDED
            )
        return PriceSyncResponse(
            considered=len(summary.updates),
            updated=written,
            unmapped=summary.unmapped,
            review_needed=summary.review_needed,
            stale=summary.stale,
            superseded=superseded,
            details=details,
            registry=refreshed,
        )

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
        return RegistryResponse(
            backend_pool_session_affinity_supported=True,
            databricks_connections_supported=True,
            databricks_oauth_supported=self._settings.databricks_oauth_enabled,
            image_generation_supported=self._settings.image_generation_enabled,
            image_configuration_defaults=self._settings.image_generation_defaults,
            gateways=[
                GatewayProfile.model_validate(self._public_secret(row)) for row in rows["gateways"]
            ],
            providers=[Provider.model_validate(self._public_secret(row)) for row in providers],
            runtimes=[Runtime.model_validate(row) for row in runtimes],
            models=[
                ManagedModel.model_validate(row)
                for row in rows["models"]
                if row["runtime_id"] in runtime_ids
                and row["provider_id"] in provider_ids
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
                    ModelVendorKey.GENERIC, write.model_vendor,
                }:
                    raise HTTPException(
                        status_code=409,
                        detail="The selected model vendor does not match the provider",
                    )
                openai_vendor = write.model_vendor or configured_vendor
        else:
            assert write.provider.template is not None
            template = write.provider.template
            brand = {
                "amazon_bedrock": BrandKey.AMAZON_BEDROCK,
                "microsoft_foundry": BrandKey.MICROSOFT_FOUNDRY,
                "azure_databricks": BrandKey.AZURE_DATABRICKS,
                "openai_compatible": {
                    ModelVendorKey.OPENAI: BrandKey.OPENAI,
                    ModelVendorKey.ANTHROPIC: BrandKey.ANTHROPIC,
                }.get(openai_vendor, BrandKey.GENERIC),
            }[template]
            provider_kind = {
                "amazon_bedrock": ProviderKind.ANTHROPIC,
                "microsoft_foundry": ProviderKind.MICROSOFT_FOUNDRY,
                "openai_compatible": ProviderKind.OPENAI_COMPATIBLE,
                "azure_databricks": ProviderKind.ANTHROPIC,
            }[template]
            provider_id = None
            provider_name = (
                "Microsoft Foundry"
                if template == "microsoft_foundry"
                else "Amazon Bedrock"
                if template == "amazon_bedrock"
                else "Azure Databricks"
                if template == "azure_databricks"
                else model_vendor_label(openai_vendor)
            )
            provider_config: dict[str, Any] = {"hosting_platform": template}
            if template == "openai_compatible":
                provider_config["model_vendor"] = openai_vendor.value
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
            values = self._openai_connection_values(write, registry["runtimes"], openai_vendor)
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
            named_value_name = f"finops-foundry-key-{uuid4().hex[:20]}"
            auth_strategy = "named_value_api_key"
            managed_identity_resource = None
            config.update(
                inference_endpoint=inference_endpoint,
                credential_kind="api_key",
                credential_provisioned=False,
            )
        else:
            if not self._settings.apim_principal_id:
                raise HTTPException(
                    status_code=409,
                    detail="APIM managed identity is not configured for Foundry onboarding",
                )
            config["authorization"] = {
                "kind": "azure_rbac",
                "principal_id": self._settings.apim_principal_id,
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
                    "finops-bedrock-"
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
        if not self._settings.apim_principal_id:
            raise HTTPException(
                status_code=409,
                detail="APIM managed identity is not configured for Databricks onboarding",
            )
        workspace = databricks_workspace_url(str(write.databricks_workspace_url))
        config = databricks_connection_config(workspace, self._settings.apim_principal_id)
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
        runtimes: Any,
        model_vendor: ModelVendorKey,
    ) -> dict[str, Any]:
        if write.openai_base_url is None:
            raise HTTPException(status_code=400, detail="OpenAI-compatible Base URL is missing")
        try:
            base_url, backend_url, backend_path = openai_compatible_endpoint_values(
                str(write.openai_base_url)
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        self._ensure_unique_connection(runtimes, write.gateway_profile_id, "base_url", base_url)
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
                "named_value_name": f"turnstile-openai-{uuid4().hex}",
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
            ("image_generation" in write.capabilities)
            != ("image_generation" in (current.get("capabilities") or []))
        ):
            raise HTTPException(
                status_code=409, detail="Changing model operation requires a publication"
            )
        price_fields = (
            "runtime_id", "price_source", "price_reference", "price_discount_percent",
            "input_cost_per_million", "output_cost_per_million",
            "cached_cost_per_million", "cache_write_cost_per_million",
        )
        current_model = ManagedModel.model_validate(current) if current is not None else None
        if write.price_source is not PriceSource.MANUAL and (
            current_model is None
            or any(getattr(current_model, field) != getattr(write, field) for field in price_fields)
        ):
            try:
                entry = self._price_catalog.lookup(write.price_reference or "")
            except Exception as error:  # noqa: BLE001
                raise HTTPException(
                    status_code=503, detail="The selected price source is unavailable"
                ) from error
            if (
                entry is None
                or not entry.complete
                or entry.input_per_million is None
                or entry.output_per_million is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The selected catalog price is incomplete; existing prices are unchanged"
                    ),
                )
        self._save("model", write.model_dump(mode="python"), item_id)
        return self.registry()

    def check_runtime(self, runtime_id: UUID) -> RuntimeHealth:
        route = self._repository.invocation_route(runtime_id, None)
        if route is None:
            raise HTTPException(
                status_code=409, detail="Runtime has no enabled model or is disabled"
            )
        route = self._decrypt_route(route)
        result = self._router.adapter(route).check(route)
        self._repository.update_runtime_health(
            runtime_id, result.status.value, result.message, result.checked_at
        )
        return result

    def _assert_model_allowed(
        self,
        request: ModelInvocationRequest | ImageInvocationRequest,
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
        if not user_id or user_id == "system-runtime-health-check":
            return
        policies = self._repository.list_user_model_policies([user_id])
        policy = next((row for row in policies if str(row["user_id"]) == user_id), None)
        if policy is None:
            # Legacy models preserve the compatibility rule that an unconfigured person is
            # unrestricted. Dynamically published models opt out: promotion and a Table
            # Storage policy write cannot be one transaction, so requiring an explicit
            # assignment keeps a newly routable alias closed during that boundary.
            if route.get("assignment_required"):
                self._deny(
                    request,
                    route,
                    "model_not_assigned",
                    request_id,
                    model_admission="denied",
                )
            return
        allowed = {str(value) for value in (policy.get("model_ids") or [])}
        if str(request.model_id) in allowed:
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
        request: ModelInvocationRequest | ImageInvocationRequest,
        route: dict[str, Any],
        request_id: str,
    ) -> None:
        """Enforce the monthly allowance on the same path, for the same reason.

        The employee path reserves against a Table Storage ledger because APIM cannot ask
        this application anything. Here the allowance and the usage are both one query
        away, so the check is a direct comparison instead. It is intentionally not a
        reservation: telemetry for a console call lands seconds later, so a burst can
        overshoot slightly. That is the accepted cost of not putting a write on the
        request path for a human-driven, low-volume surface.

        A department in audit mode is not blocked, and a person with no budget row is not
        blocked either — the same two rules the ledger admission already follows.
        """
        user_id = request.metadata.user_id
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
        if str(state["mode"]) != "block":
            return
        if int(state["used_tokens"]) < int(state["token_limit"]):
            return
        self._deny(
            request,
            route,
            "budget_exceeded",
            request_id,
            budget_admission="denied",
        )

    def _deny(
        self,
        request: ModelInvocationRequest | ImageInvocationRequest,
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

    def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        request_id: str | None = None,
        timeout_ms: int | None = None,
        enforce_user_model_access: bool = True,
    ) -> ModelInvocationResponse:
        request_id = request_id or str(uuid4())
        route = self._repository.invocation_route(request.runtime_id, request.model_id)
        if route is None:
            raise HTTPException(
                status_code=404, detail="No enabled model route matches the request"
            )
        if "image_generation" in (route.get("model_capabilities") or []):
            raise HTTPException(
                status_code=409, detail="Image models require the image generation endpoint"
            )
        if (
            route.get("gateway_implementation") == GatewayKind.APIM
            and not route.get("gateway_base_url")
            and self._settings.apim_gateway_url
        ):
            route["gateway_base_url"] = self._settings.apim_gateway_url
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
        if enforce_user_model_access:
            self._assert_model_allowed(request, route, request_id)
        self._assert_budget_available(request, route, request_id)
        route = self._decrypt_route(route)
        if (
            route.get("gateway_implementation") == GatewayKind.APIM
            and not route.get("gateway_credential")
            and self._settings.apim_dashboard_subscription_key
        ):
            route["gateway_credential"] = (
                self._settings.apim_dashboard_subscription_key.get_secret_value()
            )
            route["gateway_auth_type"] = "api_key"
        if timeout_ms is not None:
            runtime_config = dict(route.get("runtime_config") or {})
            configured_timeout = float(runtime_config.get("timeout_seconds", 120))
            runtime_config["timeout_seconds"] = min(
                configured_timeout,
                timeout_ms / 1_000,
            )
            route["runtime_config"] = runtime_config
        route["request_id"] = request_id
        started = time.monotonic()
        try:
            result = self._router.adapter(route).invoke(request, route)
        except GatewayInvocationError as error:
            headers = dict(error.headers)
            headers.setdefault("x-request-id", request_id)
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
                headers=headers,
            ) from error

        latency_ms = elapsed_ms(started)
        correlation_id = result.correlation_id or request_id
        estimated_cost = self._estimated_cost(route, result.usage)
        response = ModelInvocationResponse(
            request_id=request_id,
            correlation_id=correlation_id,
            content=result.content,
            tool_calls=list(result.tool_calls) or None,
            provider=str(route["provider_name"]),
            runtime=str(route["runtime_name"]),
            model=str(route["model_key"]),
            gateway=result.gateway,
            latency_ms=latency_ms,
            usage=result.usage,
            estimated_cost=estimated_cost,
        )
        response._responses_output = result.responses_output
        response._anthropic_content = result.anthropic_content
        return response

    def generate_image(
        self, request: ImageInvocationRequest, *, role: str
    ) -> ImageInvocationResponse:
        if not self._settings.image_generation_enabled:
            raise HTTPException(status_code=503, detail="Image generation is not enabled")
        request_id = str(uuid4())
        route = self._repository.invocation_route(request.runtime_id, request.model_id)
        if route is None:
            raise HTTPException(status_code=404, detail="No enabled image model route matches")
        if role not in (route.get("model_allowed_roles") or []) or role not in (
            route.get("runtime_allowed_roles") or []
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
            reserved = request.reservation_tokens(str(route["model_key"]), profile)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        self._assert_model_allowed(request, route, request_id)
        self._assert_budget_available(request, route, request_id)
        route = self._decrypt_route(route)
        if not route.get("gateway_base_url") and self._settings.apim_gateway_url:
            route["gateway_base_url"] = self._settings.apim_gateway_url
        if not route.get("gateway_credential") and self._settings.apim_dashboard_subscription_key:
            route["gateway_credential"] = (
                self._settings.apim_dashboard_subscription_key.get_secret_value()
            )
            route["gateway_auth_type"] = "api_key"
        now = datetime.now(UTC)
        try:
            attempt = self._repository.begin_billable_request(
                BillableRequestPlan(
                    operation_key="invocation:" + request_id,
                    plan_sha256=hashlib.sha256(
                        json.dumps(
                            {
                                "request": request.model_dump(mode="json"),
                                "profile": profile.model_dump(mode="json"),
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                    scope_type="person",
                    scope_id=request.metadata.user_id,
                    period_start=date(now.year, now.month, 1),
                    model_id=str(route["model_id"]),
                    model_key=str(route["model_key"]),
                    reserved_tokens=reserved,
                )
            )
        except BillableBudgetExceeded:
            self._deny(
                request, route, "image_budget_insufficient", request_id, budget_admission="denied"
            )
        request_id = str(attempt.id)
        route["request_id"] = request_id
        started = time.monotonic()
        try:
            result = self._router.generate_image(request, route)
        except GatewayInvocationError as error:
            usage = error.usage
            actual = (
                usage.input_tokens + usage.cached_tokens + usage.output_tokens
                if usage is not None and not usage.estimated
                else None
            )
            self._repository.finish_billable_request(
                attempt.id,
                actual_tokens=actual,
                correlation_id=error.headers.get("x-correlation-id"),
                evidence={
                    "status_code": error.status_code,
                    "outcome": "response_invalid" if actual is not None else "uncertain",
                    "usage": usage.model_dump(mode="json")
                    if actual is not None and usage
                    else None,
                },
            )
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
                headers={**error.headers, "x-request-id": request_id},
            ) from error
        actual = (
            result.usage.input_tokens + result.usage.cached_tokens + result.usage.output_tokens
            if result.usage is not None
            else None
        )
        self._repository.finish_billable_request(
            attempt.id,
            actual_tokens=actual,
            correlation_id=result.correlation_id,
            evidence={
                "status_code": 200,
                "usage": result.usage.model_dump(mode="json") if result.usage else None,
            },
        )
        return ImageInvocationResponse(
            request_id=request_id,
            correlation_id=result.correlation_id or request_id,
            provider=str(route["provider_name"]),
            runtime=str(route["runtime_name"]),
            model=str(route["model_key"]),
            gateway=result.gateway,
            latency_ms=elapsed_ms(started),
            data=[result.image],
            size=result.size,
            quality=result.quality,
            output_format=result.output_format,
            usage=result.usage,
            estimated_cost=self._estimated_cost(route, result.usage),
        )

    @staticmethod
    def _estimated_cost(
        route: dict[str, Any], usage: InvocationUsage | None
    ) -> float | None:
        input_price = route.get("input_cost_per_million")
        output_price = route.get("output_cost_per_million")
        if usage is None or input_price is None or output_price is None:
            return None
        cached_price = route.get("cached_cost_per_million")
        if cached_price is None:
            cached_price = input_price
        cache_write_price = route.get("cache_write_cost_per_million")
        if cache_write_price is None:
            cache_write_price = cached_price
        return ModelPrice(
            input_price_per_million=float(input_price),
            cached_price_per_million=float(cached_price),
            cache_write_price_per_million=float(cache_write_price),
            output_price_per_million=float(output_price),
        ).cost(
            usage.input_tokens,
            usage.cached_tokens,
            usage.output_tokens,
            usage.cache_write_tokens,
        )

    @staticmethod
    def _model_protocol_route(route: dict[str, Any]) -> dict[str, Any]:
        return model_protocol_route(route)

    def _save(self, kind: str, values: dict[str, Any], item_id: UUID | None) -> None:
        try:
            if item_id is None:
                self._repository.create_registry_item(kind, values)
            elif self._repository.update_registry_item(kind, item_id, values) is None:
                raise HTTPException(status_code=404, detail=f"Unknown {kind}: {item_id}")
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
        result = dict(route)
        result["provider_credential"] = self._cipher.decrypt(
            result.pop("provider_credential_ciphertext", None)
        )
        result["gateway_credential"] = self._cipher.decrypt(
            result.pop("gateway_credential_ciphertext", None)
        )
        return result
