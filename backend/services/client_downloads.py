"""Authorized, in-memory export of an existing application subscription."""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import HTTPException
from pydantic import Field

from turnstile_core.config import Settings
from turnstile_core.domain.models import StrictModel
from turnstile_core.domain.runtime_models import InvocationApiFormat
from turnstile_core.integrations.gateway_protocol import invocation_api_paths
from turnstile_core.persistence.repository import QueryRepository
from turnstile_core.services.application_access import ApplicationAccessService

from .auth_service import SessionIdentity
from .client_download_permissions import require_plugin_download
from .client_icons import PluginIconUnavailable, load_plugin_icons
from .client_namespace import plugin_namespace
from .client_pricing import plugin_model_cost
from .download_filename import plugin_filename

PLUGIN_VERSION = "0.2.0"
RESERVED = {"master", "turnstile-dashboard", "turnstile-publisher-probe"}
SDK = {
    "openai_chat": "@ai-sdk/openai-compatible",
    "openai_responses": "@ai-sdk/openai",
    "anthropic_messages": "@ai-sdk/anthropic",
}


class ClientModel(StrictModel):
    id: str
    name: str
    protocol: str
    context_window: int | None
    capabilities: list[str]
    cost: dict[str, float] | None = None
    upstream_model_id: str | None = None


class ClientSubscription(StrictModel):
    application_id: UUID
    application_name: str
    subscription_id: UUID
    subscription_name: str
    gateway_id: UUID
    gateway_name: str
    base_url: str
    models: list[ClientModel]


class ClientDownloadOptions(StrictModel):
    items: list[ClientSubscription]
    key_management_available: bool
    unavailable_reason: str | None = None
    plugin_version: str = PLUGIN_VERSION


class ClientDownloadRequest(StrictModel):
    application_id: UUID
    subscription_id: UUID
    gateway_id: UUID
    key_kind: str = Field(default="primary", pattern="^(primary|secondary)$")


@dataclass(frozen=True)
class ClientDownloadArchive:
    content: bytes
    filename: str


class ClientDownloadService:
    def __init__(
        self, repository: QueryRepository, applications: ApplicationAccessService,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.applications = applications
        self.settings = settings

    def options(self, identity: SessionIdentity) -> ClientDownloadOptions:
        require_plugin_download(identity, "subscription-plugin")
        applications = self.applications.applications()
        rows = self.repository.registry()
        prices = self.repository.model_prices()
        gateways = {str(row["id"]): row for row in rows["gateways"]}
        runtimes = {str(row["id"]): row for row in rows["runtimes"]}
        providers = {str(row["id"]): row for row in rows["providers"]}
        items: list[ClientSubscription] = []
        for application in applications.items:
            if application.system_managed or application.status != "active":
                continue
            if identity.role != "owner" and application.owner_id not in {
                identity.id, identity.email, identity.effective_user_id,
            }:
                continue
            gateway = gateways.get(str(application.gateway_profile_id))
            if not gateway or not gateway["enabled"] or gateway["implementation"] != "apim":
                continue
            base_url = str(gateway.get("base_url") or "").rstrip("/")
            url = urlsplit(base_url)
            target = urlsplit(self.settings.apim_gateway_url or "")
            # The existing key client targets one APIM service; never export its keys elsewhere.
            if (
                url.scheme != "https" or not url.hostname or url.netloc != target.netloc
                or url.username or url.password or url.query or url.fragment
            ):
                continue
            effective = self.repository.effective_gateway_publication(
                application.gateway_profile_id
            )
            if not effective:
                continue
            spec = effective["desired_spec"]
            if not isinstance(spec, dict):
                spec = spec.model_dump(mode="json")
            published = {str(model["id"]) for model in spec.get("discovery_models", [])}
            published.update(
                str(binding.get("model", {}).get("model_key", ""))
                for binding in spec.get("bindings", []) if isinstance(binding, dict)
            )
            models: list[ClientModel] = []
            for row in rows["models"]:
                runtime = runtimes.get(str(row["runtime_id"]))
                provider = providers.get(str(row["provider_id"]))
                if (
                    not row["enabled"] or not runtime or not runtime["enabled"]
                    or not provider or not provider["enabled"]
                    or str(runtime.get("gateway_profile_id")) != str(application.gateway_profile_id)
                    or row["model_key"] not in published
                    or not {"chat", "image_generation"}.intersection(row["capabilities"])
                    or (application.model_policy_configured
                        and UUID(str(row["id"])) not in application.allowed_model_ids)
                ):
                    continue
                route = {
                    "model_key": row["model_key"],
                    "upstream_model_id": row.get("upstream_model_id"),
                    "model_family_key": row.get("family_key"),
                    "provider_kind": provider["provider_kind"],
                    "provider_endpoint_url": provider.get("endpoint_url"),
                    "runtime_kind": runtime["runtime_kind"],
                    "runtime_config": runtime["config"],
                    "gateway_implementation": "apim",
                }
                if "image_generation" in row["capabilities"]:
                    protocol_name = "openai_images"
                else:
                    paths = invocation_api_paths(route, tools="tools" in row["capabilities"])
                    if not paths:
                        continue
                    protocol = (
                        InvocationApiFormat.OPENAI_RESPONSES
                        if InvocationApiFormat.OPENAI_RESPONSES in paths else next(iter(paths))
                    )
                    protocol_name = protocol.value
                models.append(ClientModel(
                    id=str(row["model_key"]), name=str(row["display_name"]),
                    protocol=protocol_name, context_window=row.get("context_window"),
                    capabilities=list(row["capabilities"]),
                    cost=plugin_model_cost(prices.get(str(row["id"]))),
                    upstream_model_id=row.get("upstream_model_id") or str(row["model_key"]),
                ))
            for subscription in self.repository.list_gateway_application_subscriptions(
                [application.id]
            ):
                if (
                    subscription["state"] != "active" or not subscription["scope_exists"]
                    or str(subscription["apim_subscription_id"]).casefold() in RESERVED
                    or str(subscription["gateway_profile_id"])
                    != str(application.gateway_profile_id)
                ):
                    continue
                items.append(ClientSubscription(
                    application_id=application.id, application_name=application.display_name,
                    subscription_id=subscription["id"],
                    subscription_name=str(subscription["display_name"]),
                    gateway_id=application.gateway_profile_id, gateway_name=str(gateway["name"]),
                    base_url=base_url, models=models,
                ))
        return ClientDownloadOptions(
            items=items, key_management_available=applications.key_management_available,
            unavailable_reason=applications.key_management_unavailable_reason,
        )

    def download(
        self, identity: SessionIdentity, request: ClientDownloadRequest,
    ) -> ClientDownloadArchive:
        require_plugin_download(identity, "subscription-plugin")
        try:
            icons = load_plugin_icons()
        except PluginIconUnavailable as error:
            raise HTTPException(503, "插件图标资源尚未准备就绪") from error
        option = next((item for item in self.options(identity).items if (
            item.application_id == request.application_id
            and item.subscription_id == request.subscription_id
            and item.gateway_id == request.gateway_id
        )), None)
        if option is None:
            raise HTTPException(404, "没有可导出的应用订阅或网关。")
        if not option.models:
            raise HTTPException(409, "该订阅没有已发布且获授权的模型。")
        key = self.applications.reveal_application_subscription_key(
            request.application_id, request.subscription_id,
            "primary" if request.key_kind == "primary" else "secondary",
        )
        provider_id = plugin_namespace(
            "subscription", option.subscription_name, str(option.gateway_id),
            str(option.subscription_id),
        )
        config = {
            "version": 1, "providerId": provider_id,
            "name": f"AIGateway Subscription · {option.subscription_name}",
            "accountId": option.subscription_name,
            "icon": icons.data_uri,
            "iconRevision": icons.revision,
            "applicationId": str(option.application_id),
            "subscriptionId": str(option.subscription_id), "gatewayId": str(option.gateway_id),
            "baseURL": option.base_url, "modelsPath": "/v1/models",
            "subscriptionKey": key.value, "keyKind": request.key_kind,
            "models": {model.id: {
                "id": model.id, "name": model.name,
                "cost": model.cost,
                "provider": {"npm": SDK[model.protocol], "api": option.base_url},
                "limit": {"context": model.context_window or 32768, "output": 8192},
                "tool_call": "tools" in model.capabilities,
                "reasoning": "reasoning" in model.capabilities,
                "modalities": {"input": ["text", "image"]
                               if "vision" in model.capabilities else ["text"]},
            } for model in option.models if model.protocol != "openai_images"},
            "images": [model.model_dump(mode="json") for model in option.models
                       if model.protocol == "openai_images"],
            "aliases": {model.id: model.upstream_model_id for model in option.models},
        }
        root = Path(__file__).resolve().parent / "client_plugin"
        files = {name: (root / name).read_bytes()
                 for name in ("index.mjs", "pricing.mjs", "image-sync.mjs", "README.md")}
        shared = root.parent / "client_user_plugin"
        files.update({name: (shared / name).read_bytes()
                      for name in ("helpers.mjs", "images.mjs", "native-images.mjs", "quota.mjs")})
        files.update(icons.package_files())
        files["aigateway.config.json"] = json.dumps(config, ensure_ascii=False, indent=2).encode()
        files["package.json"] = json.dumps({
            "name": f"opencode-aigateway-subscription-{provider_id.rsplit('-', 1)[-1]}-auth",
            "version": PLUGIN_VERSION, "type": "module", "main": "./index.mjs",
            "description": "AIGateway-Proxy provider for magpie",
            "keywords": ["opencode", "opencode-plugin", "aigateway"], "license": "MIT",
            "magpie": {"icon": icons.data_uri},
            "files": list(files),
        }, indent=2).encode()
        output = BytesIO()
        filename = plugin_filename("aigateway-proxy", option.subscription_name, "subscription")
        archive_root = filename.removesuffix(".zip")
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in files.items():
                info = ZipInfo(f"{archive_root}/{name}")
                info.external_attr = 0o100600 << 16
                archive.writestr(info, content, compress_type=ZIP_DEFLATED)
        return ClientDownloadArchive(
            content=output.getvalue(),
            filename=filename,
        )
