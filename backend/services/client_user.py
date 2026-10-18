"""User-bound desktop bootstrap, live catalogs and delegated APIM context."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
import jwt
from fastapi import HTTPException

from turnstile_core.config import Settings
from turnstile_core.domain.budget_counting import (
    BudgetOperation,
    BudgetPublicationContracts,
    verified_budget_operations,
)
from turnstile_core.domain.ledger import LedgerScope
from turnstile_core.domain.menu_permissions import menu_permissions
from turnstile_core.domain.models import ModelPrice
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.budget_delegation_headers import encode_delegation_metadata
from turnstile_core.integrations.gateway_protocol import invocation_api_paths
from turnstile_core.persistence.repository import QueryRepository
from turnstile_core.security import CredentialCipher
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.budget_delegation import (
    BudgetDelegationError,
    prepare_budget_delegation,
)
from turnstile_core.services.budget_settlement import (
    BudgetScopeRepository,
    BudgetStatusRepository,
    execution_budget_view,
)
from turnstile_core.services.directory_catalog import catalog_for, directory_store

from .auth_service import SessionIdentity
from .client_download_permissions import require_plugin_download
from .client_downloads import RESERVED, SDK
from .client_icons import PluginIconUnavailable, load_plugin_icons
from .client_namespace import plugin_namespace
from .client_pricing import plugin_model_cost
from .client_telemetry import client_telemetry_headers
from .client_user_store import ClientUserStore
from .download_filename import plugin_filename
from .gateway_credentials import with_platform_gateway_credential

USER_PLUGIN_VERSION = "0.5.1"
CLIENT_ISSUER = "urn:turnstile:client-runtime"
CLIENT_AUDIENCE = "turnstile-client-runtime"
PERSONAL_BUDGET_REQUIRED = "暂未开通预算，请联系部门管理员。"


class ClientUserService:
    def __init__(
        self,
        store: ClientUserStore,
        repository: QueryRepository,
        applications: ApplicationAccessService,
        settings: Settings,
        *,
        enabled: bool | None = None,
        origin: str | None = None,
    ) -> None:
        self.store, self.repository, self.applications, self.settings = (
            store,
            repository,
            applications,
            settings,
        )
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("CLIENT_USER_ENABLED", "false").lower() == "true"
        )
        self.origin = str(origin or os.getenv("CLIENT_PLATFORM_ORIGIN", "")).rstrip("/")
        parsed = urlsplit(self.origin)
        if self.origin and (
            parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or (
                parsed.scheme != "https"
                and not (
                    not settings.production
                    and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "testserver"}
                )
            )
        ):
            raise ValueError("Use the exact trusted platform origin")
        self.cipher = CredentialCipher.from_settings(settings)
        raw = settings.credential_encryption_key
        key = raw.get_secret_value().encode() if raw else settings.credential_key_file.read_bytes()
        self.signing_key = hmac.digest(key, b"turnstile:client-context:v1", "sha256")
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def require_enabled(self) -> None:
        if not self.enabled or not self.origin:
            raise HTTPException(503, "User plugin is not enabled on this platform")

    def _binding(self, uid: str) -> str:
        message = f"{self.origin}|{UUID(uid)}".encode()
        return hmac.new(self.signing_key, b"bootstrap|" + message, hashlib.sha256).hexdigest()

    @staticmethod
    def package_filename(identity: SessionIdentity) -> str:
        return plugin_filename("aigateway-proxy-user", identity.name or identity.email, "user")

    def has_personal_budget(self, identity: SessionIdentity) -> bool:
        now = datetime.now(UTC)
        # Read configuration only: download eligibility does not depend on remaining usage.
        return bool(self.repository.list_token_budgets(
            date(now.year, now.month, 1), identity.effective_user_id,
        ))

    def require_personal_budget(self, identity: SessionIdentity) -> None:
        if not self.has_personal_budget(identity):
            raise HTTPException(403, PERSONAL_BUDGET_REQUIRED)

    def package(self, identity: SessionIdentity) -> bytes:
        require_plugin_download(identity, "user-plugin")
        self.require_enabled()
        self.require_personal_budget(identity)
        try:
            icons = load_plugin_icons()
        except PluginIconUnavailable as error:
            raise HTTPException(503, "插件图标资源尚未准备就绪") from error
        selected = self.default_subscription(identity)
        installation, grant_id = uuid4(), uuid4()
        access = "tsca_" + secrets.token_urlsafe(32)
        refresh = "tscr_" + secrets.token_urlsafe(32)
        selection = {k: selected[k] for k in ("application_id", "subscription_id", "gateway_id")}
        self.store.issue_download(
            identity.id, installation, identity.method, selection, access, refresh, grant_id
        )
        catalog = self.catalog({"id": grant_id, "selection": selection}, identity)
        provider_id = plugin_namespace(
            "user", identity.name or identity.email, self.origin, str(UUID(identity.id))
        )
        cfg = {
            "version": 4,
            "platformOrigin": self.origin,
            "clientId": "magpie-user",
            "userId": identity.id,
            "userName": identity.name or identity.email,
            "binding": self._binding(identity.id),
            "providerId": provider_id,
            "name": f"AIGateway User · {identity.name or identity.email}",
            "icon": icons.data_uri,
            "iconRevision": icons.revision,
            "installationId": str(installation),
            "selection": catalog["selection"],
            "catalog": catalog,
            "authorization": {
                "type": "oauth",
                "access": access,
                "refresh": refresh,
                "expires": int(time.time() * 1000) + 900_000,
                "accountId": identity.name or identity.email,
                "uid": identity.id,
                "installationId": str(installation),
            },
        }
        root = Path(__file__).with_name("client_user_plugin")
        files = {
            name: (root / name).read_bytes()
            for name in (
                "index.mjs", "helpers.mjs", "images.mjs", "native-images.mjs",
                "quota.mjs", "README.md",
            )
        }
        files["pricing.mjs"] = (root.parent / "client_plugin" / "pricing.mjs").read_bytes()
        files.update(icons.package_files())
        files["aigateway-user.config.json"] = json.dumps(cfg, ensure_ascii=False, indent=2).encode()
        files["package.json"] = json.dumps(
            {
                "name": f"opencode-aigateway-user-{provider_id.rsplit('-', 1)[-1]}-auth",
                "version": USER_PLUGIN_VERSION,
                "type": "module",
                "main": "./index.mjs",
                "description": "AIGateway-Proxy user authorization provider for magpie",
                "magpie": {"icon": icons.data_uri},
                "files": list(files),
            }
        ).encode()
        out = BytesIO()
        archive_root = self.package_filename(identity).removesuffix(".zip")
        with ZipFile(out, "w", compression=ZIP_DEFLATED) as z:
            for name, data in files.items():
                info = ZipInfo(f"{archive_root}/{name}")
                info.external_attr = 0o100600 << 16
                z.writestr(info, data, compress_type=ZIP_DEFLATED)
        return out.getvalue()

    def default_subscription(self, identity: SessionIdentity) -> dict[str, Any]:
        items = [x for x in self.subscriptions(identity, force=True) if x["models"]]
        gateways = self.repository.registry()["gateways"]
        for gateway in sorted(gateways, key=lambda g: not g.get("is_default", False)):
            configured = (gateway.get("config") or {}).get("client_user_default_subscriptions", {})
            if not isinstance(configured, dict):
                raise HTTPException(409, "The default subscription configuration is invalid")
            target = next(
                (
                    configured[k]
                    for k in (identity.id, identity.effective_user_id, identity.email)
                    if k in configured
                ),
                None,
            )
            if target:
                match = next(
                    (
                        x
                        for x in items
                        if x["subscription_id"] == str(target)
                        and x["gateway_id"] == str(gateway["id"])
                    ),
                    None,
                )
                if not match:
                    raise HTTPException(409, "The configured default subscription is unavailable")
                return match
        if not items:
            raise HTTPException(409, "No authorized default subscription with models is available")
        bound = self.platform_subscription()
        selected = next(
            (x for x in items if x["subscription_id"] == bound["subscription_id"]), None
        )
        if not selected:
            raise HTTPException(409, "The platform default subscription has no authorized models")
        return selected

    def platform_gateway_credential(self) -> tuple[dict[str, Any], str]:
        rows = self.repository.registry()
        models = [
            m
            for m in rows["models"]
            if m["enabled"] and "image_generation" not in m["capabilities"]
        ]
        # Match the console's default-available-model selection, not application ordering.
        model = next((m for m in models if m.get("is_default")), models[0] if models else None)
        route = (
            self.repository.invocation_route(UUID(str(model["runtime_id"])), UUID(str(model["id"])))
            if model
            else None
        )
        if not model:
            defaults = [g for g in rows["gateways"] if g.get("is_default")]
            if len(defaults) == 1:
                gateway = defaults[0]
                route = {
                    "gateway_id": gateway["id"],
                    "gateway_implementation": gateway["implementation"],
                    "gateway_auth_type": gateway["auth_type"],
                    "gateway_credential_ciphertext": gateway.get("credential_ciphertext"),
                }
        if not route:
            raise HTTPException(409, "The platform default invocation route is unavailable")
        candidates = [g for g in rows["gateways"] if str(g["id"]) == str(route.get("gateway_id"))]
        if len(candidates) != 1:
            raise HTTPException(409, "A unique default platform gateway is required")
        gateway = candidates[0]
        if not gateway["enabled"] or gateway["implementation"] != "apim":
            raise HTTPException(409, "The default platform gateway is unavailable")
        route["gateway_credential"] = self.cipher.decrypt(
            route.get("gateway_credential_ciphertext")
        )
        route = with_platform_gateway_credential(route, self.settings)
        if route.get("gateway_auth_type") != "api_key" or not route.get("gateway_credential"):
            raise HTTPException(409, "The platform default subscription credential is unavailable")
        return gateway, str(route["gateway_credential"])

    def platform_subscription(self) -> dict[str, Any]:
        gateway, credential = self.platform_gateway_credential()
        fingerprint = hmac.new(self.signing_key, credential.encode(), hashlib.sha256).hexdigest()
        cache_key = f"platform-subscription:{gateway['id']}:{fingerprint}"
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        matches = []
        for app in self.applications.applications().items:
            if (
                app.status != "active"
                or app.system_managed
                or str(app.gateway_profile_id) != str(gateway["id"])
            ):
                continue
            for sub in self.repository.list_gateway_application_subscriptions([app.id]):
                if (
                    sub["state"] != "active"
                    or not sub["scope_exists"]
                    or str(sub["apim_subscription_id"]).lower()
                    in (RESERVED - {"turnstile-dashboard"})
                    or str(sub["gateway_profile_id"]) != str(gateway["id"])
                ):
                    continue
                kind = self.applications.match_application_subscription_key(
                    app.id, UUID(str(sub["id"])), credential
                )
                if kind:
                    matches.append(
                        {
                            "application_id": str(app.id),
                            "subscription_id": str(sub["id"]),
                            "gateway_id": str(gateway["id"]),
                            "key_kind": kind,
                        }
                    )
        if len(matches) != 1:
            raise HTTPException(
                409, "The platform credential must match exactly one active subscription"
            )
        result = matches[0]
        with self._lock:
            self._cache[cache_key] = (time.monotonic() + 300, result)
        return result

    def subscription_credential(self, selected: dict[str, Any]) -> str:
        bound = self.platform_subscription()
        if selected["subscription_id"] == bound["subscription_id"]:
            return self.platform_gateway_credential()[1]
        return self.applications.reveal_application_subscription_key(
            UUID(selected["application_id"]), UUID(selected["subscription_id"]), "primary"
        ).value

    def create_device(
        self,
        uid: str,
        binding: str,
        installation: UUID,
        purpose: str,
    ) -> dict[str, Any]:
        self.require_enabled()
        if not hmac.compare_digest(self._binding(uid), binding):
            raise HTTPException(400, "Invalid personal bootstrap")
        device = secrets.token_urlsafe(32)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        code = code[:4] + "-" + code[4:]
        self.store.create_device(uid, installation, purpose, device, code)
        return {
            "device_code": device,
            "user_code": code,
            "expires_in": 300,
            "interval": 5,
            "verification_uri": self.origin + "/?client_authorization=" + code,
        }

    def identity(self, grant: dict[str, Any]) -> SessionIdentity:
        role = grant["role"]
        if role not in {"owner", "member"}:
            raise HTTPException(401, "Account no longer has a valid role")
        identity = SessionIdentity(
            id=str(grant["user_id"]),
            email=grant["email"],
            name=grant.get("display_name"),
            role=role,
            method=grant["method"],
            session_expires_at=grant["expires_at"],
            menu_permissions=menu_permissions(owner=role == "owner"),
        )
        if self.settings.directory_source == "database":
            assert self.settings.database_url
            from dataclasses import replace

            directory = directory_store(self.settings.database_url)
            principal = directory.principal(UUID(identity.id), identity.email, role)
            person = directory.linked_person(UUID(identity.id))
            identity = replace(
                identity,
                governance_user_id=str(person["governance_user_id"]) if person else None,
                directory_person_active=bool(
                    person
                    and person["status"] == "active"
                    and not person["manual_disabled"]
                    and not person["source_disabled"]
                ),
                directory_permission_revision=principal.permission_revision,
                menu_permissions=principal.menu_permissions,
                data_scope=principal.data_scope,
            )
        return identity

    def subscriptions(
        self,
        identity: SessionIdentity,
        *,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        personal_status = self._personal_execution_status(identity)
        key = f"directory:{identity.id}:{identity.role}:{identity.directory_permission_revision}"
        if personal_status is not None:
            key += f":{personal_status.get('budget_mode')}:{personal_status.get('policy_revision')}"
        with self._lock:
            cached = self._cache.get(key)
            if not force and cached and cached[0] > time.monotonic():
                return cast(list[dict[str, Any]], cached[1]["items"])
            items = self._build_subscriptions(identity, personal_status=personal_status)
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[key] = (time.monotonic() + 60, {"items": items})
            return items

    def _build_subscriptions(
        self, identity: SessionIdentity, *, personal_status: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.require_enabled()
        if self.settings.directory_source == "database" and (
            not identity.governance_user_id or identity.directory_person_active is not True
        ):
            raise HTTPException(409, "Link an active person before using the user plugin")
        rows = self.repository.registry()
        gateways = {str(x["id"]): x for x in rows["gateways"]}
        runtimes = {str(x["id"]): x for x in rows["runtimes"]}
        providers = {str(x["id"]): x for x in rows["providers"]}
        policies = self.repository.list_user_model_policies([identity.effective_user_id])
        policy = next(
            (p for p in policies if str(p["user_id"]) == identity.effective_user_id), None
        )
        permitted = {str(x) for x in policy["model_ids"]} if policy else None
        platform = self.platform_subscription()
        out = []
        for app in self.applications.applications().items:
            if app.status != "active" or app.system_managed:
                continue
            if (
                identity.role != "owner"
                and str(app.id) != platform["application_id"]
                and app.owner_id
                not in {
                    identity.id,
                    identity.email,
                    identity.effective_user_id,
                }
            ):
                continue
            g = gateways.get(str(app.gateway_profile_id))
            if not g or not g["enabled"] or g["implementation"] != "apim":
                continue
            base = str(
                g.get("base_url")
                or (
                    self.settings.apim_gateway_url if str(g["id"]) == platform["gateway_id"] else ""
                )
                or ""
            ).rstrip("/")
            uri, expected = urlsplit(base), urlsplit(self.settings.apim_gateway_url or "")
            if uri.scheme != "https" or uri.netloc != expected.netloc or uri.query or uri.fragment:
                continue
            publication = self.repository.effective_gateway_publication(app.gateway_profile_id)
            if not publication:
                continue
            money_contracts = None
            if personal_status and personal_status.get("budget_mode") == "usd":
                try:
                    money_contracts = BudgetPublicationContracts.model_validate(
                        publication.get("resource_manifest", {}).get("budget_contracts"),
                    ).model_contracts
                except ValueError:
                    raise HTTPException(503, "Personal USD model catalogue is not ready") from None
            spec = publication["desired_spec"]
            if not isinstance(spec, dict):
                spec = spec.model_dump(mode="json")
            published = {str(x["id"]) for x in spec.get("discovery_models", [])}
            published.update(
                str(b.get("model", {}).get("model_key", ""))
                for b in spec.get("bindings", [])
                if isinstance(b, dict)
            )
            models = []
            for m in rows["models"]:
                r, p = runtimes.get(str(m["runtime_id"])), providers.get(str(m["provider_id"]))
                if (
                    not m["enabled"]
                    or not r
                    or not r["enabled"]
                    or not p
                    or not p["enabled"]
                    or str(r.get("gateway_profile_id")) != str(app.gateway_profile_id)
                    or m["model_key"] not in published
                    or identity.role not in m["allowed_roles"]
                    or identity.role not in r["allowed_roles"]
                    or (
                        app.model_policy_configured
                        and UUID(str(m["id"])) not in app.allowed_model_ids
                    )
                    or (permitted is not None and str(m["id"]) not in permitted)
                    or (permitted is None and m.get("assignment_required"))
                ):
                    continue
                capabilities = list(m["capabilities"])
                if "image_generation" in capabilities:
                    protocol = "openai_images"
                elif "chat" in capabilities:
                    paths = invocation_api_paths(
                        {
                            "model_key": m["model_key"],
                            "upstream_model_id": m.get("upstream_model_id"),
                            "model_family_key": m.get("family_key"),
                            "provider_kind": p["provider_kind"],
                            "provider_endpoint_url": p.get("endpoint_url"),
                            "runtime_kind": r["runtime_kind"],
                            "runtime_config": r["config"],
                            "gateway_implementation": "apim",
                        },
                        tools="tools" in capabilities,
                    )
                    if not paths:
                        continue
                    chosen_protocol = next(
                        (p for p in paths if p.value == "openai_responses"), next(iter(paths))
                    )
                    protocol = chosen_protocol.value
                else:
                    continue
                if money_contracts is not None:
                    contract = money_contracts.get(str(m["model_key"]))
                    operations: dict[str, BudgetOperation] = {
                        "openai_responses": "responses", "openai_chat": "chat-completions",
                        "anthropic_messages": "anthropic-messages",
                        "openai_images": "images-generations",
                    }
                    operation = operations[protocol]
                    if (contract is None or str(contract.model_id) != str(m["id"])
                        or operation not in verified_budget_operations(contract)
                        or ModelPrice.from_registry(m) is None):
                        continue
                models.append(
                    {
                        "id": m["model_key"],
                        "name": m["display_name"],
                        "model_id": str(m["id"]),
                        "runtime_id": str(m["runtime_id"]),
                        "protocol": protocol,
                        "upstream_model_id": m.get("upstream_model_id") or m["model_key"],
                        "context_window": m.get("context_window"),
                        "capabilities": capabilities,
                    }
                )
            for sub in self.repository.list_gateway_application_subscriptions([app.id]):
                if (
                    sub["state"] != "active"
                    or not sub["scope_exists"]
                    or (
                        str(sub["apim_subscription_id"]).lower() in RESERVED
                        and str(sub["id"]) != platform["subscription_id"]
                    )
                    or str(sub["gateway_profile_id"]) != str(app.gateway_profile_id)
                ):
                    continue
                out.append(
                    {
                        "application_id": str(app.id),
                        "application_name": app.display_name,
                        "subscription_id": str(sub["id"]),
                        "subscription_name": sub["display_name"],
                        "apim_subscription_id": sub["apim_subscription_id"],
                        "gateway_id": str(app.gateway_profile_id),
                        "gateway_name": g["name"],
                        "base_url": base,
                        "models": models,
                    }
                )
        return out

    def selected(
        self,
        identity: SessionIdentity,
        selection: dict[str, Any],
        *,
        fresh: bool = False,
    ) -> dict[str, Any]:
        found = next(
            (
                x
                for x in self.subscriptions(identity, force=fresh)
                if all(
                    x[k] == str(selection.get(k, ""))
                    for k in ("application_id", "subscription_id", "gateway_id")
                )
            ),
            None,
        )
        if not found:
            raise HTTPException(403, "Subscription access was removed; sign in and import again")
        row = self.repository.get_gateway_application(UUID(found["application_id"]))
        if not row or row["status"] != "active" or row["system_managed"]:
            raise HTTPException(403, "Application access is no longer available")
        if (
            identity.role != "owner"
            and row.get("owner_id")
            not in {
                identity.id,
                identity.email,
                identity.effective_user_id,
            }
            and found["subscription_id"] != self.platform_subscription()["subscription_id"]
        ):
            raise HTTPException(403, "Application ownership changed")
        subscriptions = self.repository.list_gateway_application_subscriptions(
            [UUID(found["application_id"])]
        )
        if not any(
            str(s["id"]) == found["subscription_id"]
            and s["state"] == "active"
            and s["scope_exists"]
            for s in subscriptions
        ):
            raise HTTPException(403, "Subscription access is no longer available")
        return found

    def validate_model(
        self,
        identity: SessionIdentity,
        selected: dict[str, Any],
        model: dict[str, Any],
    ) -> None:
        route = self.repository.invocation_route(UUID(model["runtime_id"]), UUID(model["model_id"]))
        if not route:
            raise HTTPException(403, "The model route is no longer enabled")
        if (
            identity.role not in route["model_allowed_roles"]
            or identity.role not in route["runtime_allowed_roles"]
        ):
            raise HTTPException(403, "The model role is no longer authorized")
        app_policy = self.repository.list_gateway_application_model_access(
            [UUID(selected["application_id"])]
        )
        if app_policy and not any(str(p.get("model_id")) == model["model_id"] for p in app_policy):
            raise HTTPException(403, "Application model access was removed")
        policies = self.repository.list_user_model_policies([identity.effective_user_id])
        policy = next(
            (p for p in policies if str(p["user_id"]) == identity.effective_user_id), None
        )
        if (
            policy
            and model["model_id"] not in {str(x) for x in policy["model_ids"]}
            or not policy
            and route.get("assignment_required")
        ):
            raise HTTPException(403, "Personal model access is not granted")
        publication = self.repository.effective_gateway_publication(UUID(selected["gateway_id"]))
        spec = publication["desired_spec"] if publication else {}
        if not isinstance(spec, dict):
            spec = spec.model_dump(mode="json")
        ids = {m["id"] for m in spec.get("discovery_models", [])}
        ids.update(b.get("model", {}).get("model_key") for b in spec.get("bindings", []))
        if model["id"] not in ids:
            raise HTTPException(403, "The model is no longer published")

    def poll(self, device: str) -> dict[str, Any]:
        row = self.store.poll(device)
        if row["state"] == "consumed":
            return cast(
                dict[str, Any], json.loads(self.cipher.decrypt(row["result_cipher"]) or "{}")
            )
        if row["state"] != "approved":
            raise HTTPException(400, "authorization_pending")
        access, refresh = "tsca_" + secrets.token_urlsafe(32), "tscr_" + secrets.token_urlsafe(32)
        result = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 900,
            "token_type": "Bearer",
            "account_id": str(row["user_id"]),
        }
        if not self.store.issue(
            row["id"], access, refresh, self.cipher.encrypt(json.dumps(result)), uuid4()
        ):
            return self.poll(device)
        return result

    def refresh(self, refresh: str, request_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "access_token": "tsca_" + secrets.token_urlsafe(32),
            "refresh_token": "tscr_" + secrets.token_urlsafe(32),
            "expires_in": 900,
            "token_type": "Bearer",
        }
        encrypted = self.store.rotate(
            refresh,
            request_id,
            result["access_token"],
            result["refresh_token"],
            self.cipher.encrypt(json.dumps(result)),
        )
        if encrypted is None:
            raise HTTPException(400, "invalid_grant")
        return cast(dict[str, Any], json.loads(self.cipher.decrypt(encrypted) or "{}"))

    def catalog(
        self, grant: dict[str, Any], identity: SessionIdentity, *, fresh: bool = False
    ) -> dict[str, Any]:
        if not grant["selection"]:
            return {"models": {}, "images": [], "selection": None}
        selected = self.selected(identity, grant["selection"], fresh=fresh)
        prices = self.repository.model_prices()
        self.store.activate(grant["id"])
        models = {}
        images = []
        for m in selected["models"]:
            if m["protocol"] == "openai_images":
                images.append({**m, "cost": plugin_model_cost(prices.get(m["model_id"]))})
                continue
            models[m["id"]] = {
                "id": m["id"],
                "name": m["name"],
                "cost": plugin_model_cost(prices.get(m["model_id"])),
                "provider": {
                    "npm": SDK[m["protocol"]],
                    "api": self.origin + "/api/v1/client-runtime",
                },
                "limit": {"context": m["context_window"] or 32768, "output": 8192},
                "tool_call": "tools" in m["capabilities"],
                "reasoning": "reasoning" in m["capabilities"],
                "modalities": {
                    "input": ["text", "image"] if "vision" in m["capabilities"] else ["text"]
                },
            }
        return {
            "user": {"id": identity.id, "name": identity.name or identity.email},
            "models": models,
            "images": images,
            "aliases": {m["id"]: m["upstream_model_id"] for m in selected["models"]},
            "selection": {k: v for k, v in selected.items() if k not in {"models", "base_url"}},
        }

    def usage(
        self, grant: dict[str, Any], identity: SessionIdentity, *, fresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = f"usage:{grant['id']}:{identity.directory_permission_revision}"
        with self._lock:
            hit = self._cache.get(cache_key)
            if (not fresh and not self.settings.budget_execution_enabled
                and hit and hit[0] > time.monotonic()):
                return hit[1]
        now = datetime.now(UTC)
        start = date(now.year, now.month, 1)
        end = date(now.year + 1, 1, 1) if now.month == 12 else date(now.year, now.month + 1, 1)
        reset = datetime(end.year, end.month, 1, tzinfo=UTC).isoformat()
        windows = []
        budget: dict[str, Any] | None = None
        person = self.repository.person_budget_state(identity.effective_user_id, start, end)
        if person:
            execution = self._execution_budget(
                LedgerScope(
                    scope_type="person", scope_id=identity.effective_user_id, period_start=start,
                )
            )
            mode = person.get("budget_mode", "tokens")
            if mode not in {"tokens", "usd"}:
                raise HTTPException(503, "Personal budget unit is unsupported")
            if mode == "usd" and execution is None:
                raise HTTPException(503, "USD budget execution is not ready")
            if execution is not None and (
                execution.get("ledger_status") != "current" or execution.get("policy_pending")
                or execution.get("budget_mode", "tokens") != mode
            ):
                raise HTTPException(503, "Personal budget is unavailable or still synchronizing")
            current = execution or person
            enforced = bool(current.get("enforce", person["mode"] == "block"))
            if mode == "usd":
                try:
                    values = {key: Decimal(str(current[key])) for key in (
                        "usd_limit", "used_usd", "pending_reserved_usd",
                        "finalized_upper_bound_usd",
                    )}
                except (KeyError, ValueError, InvalidOperation):
                    raise HTTPException(
                        503, "Personal USD budget has no verified balance",
                    ) from None
                if (any(not value.is_finite() or value < 0 for value in values.values())
                    or values["usd_limit"] == 0):
                    raise HTTPException(503, "Personal USD budget has no verified balance")
                limit_value = values["usd_limit"]
                occupied = sum((values[key] for key in values if key != "usd_limit"), Decimal(0))
                available = max(limit_value - occupied, Decimal(0))
                budget = {
                    "mode": "usd", "currency": "USD", "period": f"{start:%Y-%m}",
                    "limit": format(limit_value, "f"), "confirmed": format(values["used_usd"], "f"),
                    "reserved": format(values["pending_reserved_usd"], "f"),
                    "upper_bound": format(values["finalized_upper_bound_usd"], "f"),
                    "available": format(available, "f"), "enforce": enforced,
                    "ledger_version": current.get("ledger_version"),
                }
                amount, limit = float(occupied), float(limit_value)
                name, unit = "个人月度 USD", "USD"
            else:
                limit = int(current["token_limit"])
                amount = (
                    int(current["used_tokens"]) + int(current.get("pending_reserved_tokens") or 0)
                    + int(current.get("finalized_upper_bound_tokens") or 0)
                )
                name, unit = "个人月度 Token", "tokens"
            windows.append(
                {
                    "name": name,
                    "used": min(100, amount / limit * 100),
                    "amount": amount,
                    "limit": limit,
                    "unit": unit,
                    "resetsAt": reset,
                    "span": (end - start).days * 86400,
                    "aside": not enforced,
                }
            )
            if budget is not None:
                windows[-1]["display"] = (
                    f"已结算 USD {budget['confirmed']} · 预留 {budget['reserved']}"
                    f" · 保守占用 {budget['upper_bound']}"
                )
        result = {
            "user": identity.name or identity.email,
            "plan": "AIGateway 用户授权",
            "windows": windows,
            "as_of": now.isoformat(),
            "cache_seconds": 60,
            "balance": "未配置额度" if not windows else "",
            "signIn": "kept",
        }
        if budget is not None:
            result["quota_contract_version"] = 2
            result["budget"] = budget
        with self._lock:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[cache_key] = (time.monotonic() + 60, result)
        return result

    def _execution_budget(self, scope: LedgerScope) -> dict[str, Any] | None:
        if not self.settings.budget_execution_enabled:
            return None
        if (not isinstance(self.repository, BudgetScopeRepository)
            or not isinstance(self.repository, BudgetStatusRepository)):
            raise HTTPException(503, "Personal budget requires a durable execution repository")
        if self.repository.active_budget_execution_rollout() is None:
            return None
        if not self.settings.ledger_table_endpoint:
            raise HTTPException(503, "Personal execution ledger is not configured")
        with AtomicTableLedger(
            self.settings.ledger_table_endpoint, self.settings.ledger_table_name, timeout=2,
        ) as store:
            return execution_budget_view(self.repository, store, scope)

    def _personal_execution_status(self, identity: SessionIdentity) -> dict[str, Any] | None:
        if (not self.settings.budget_execution_enabled
            or not isinstance(self.repository, BudgetStatusRepository)):
            return None
        try:
            return self.repository.budget_execution_status(LedgerScope(
                scope_type="person", scope_id=identity.effective_user_id,
                period_start=datetime.now(UTC).date().replace(day=1),
            ))
        except Exception:
            raise HTTPException(503, "Personal budget status is unavailable") from None

    def context(
        self, identity: SessionIdentity, selection: dict[str, Any], model: str, rid: str
    ) -> str:
        return jwt.encode(
            self._context_payload(identity, selection, model, rid), self.signing_key,
            algorithm="HS256",
        )

    def invocation_headers(
        self, identity: SessionIdentity, selection: dict[str, Any], model: str, rid: str,
        *, telemetry: Mapping[str, str] | None = None,
    ) -> httpx.Headers:
        payload = self._context_payload(identity, selection, model, rid)
        try:
            delegation = prepare_budget_delegation(
                self.repository, self.settings,
                gateway_id=UUID(str(selection["gateway_id"])), gateway_implementation="apim",
                gateway_url=str(selection["base_url"]), user_id=identity.effective_user_id,
            )
        except BudgetDelegationError as error:
            raise HTTPException(error.status_code, str(error)) from None
        if (delegation is None and self.settings.budget_execution_enabled
            and UUID(str(selection["gateway_id"])) in self.settings.budget_execution_target_ids):
            now = datetime.now(UTC).date()
            start = now.replace(day=1)
            end = date(start.year + 1, 1, 1) if start.month == 12 else date(
                start.year, start.month + 1, 1,
            )
            person = self.repository.person_budget_state(identity.effective_user_id, start, end)
            if person and person.get("budget_mode") == "usd":
                raise HTTPException(503, "Trusted USD budget execution is not ready")
        headers = {
            "Ocp-Apim-Subscription-Key": self.subscription_credential(selection),
            "x-request-id": rid, "x-request-source": "aigateway-user",
            "Content-Type": "application/json", "Accept-Encoding": "identity",
        }
        headers.update(client_telemetry_headers(telemetry or {}))
        if delegation is None:
            headers["x-turnstile-client-context"] = jwt.encode(
                payload, self.signing_key, algorithm="HS256",
            )
        else:
            if self.settings.budget_execution_enabled:
                status = self._personal_execution_status(identity)
                if (status is None or status.get("state") != "active"
                    or status["policy_revision"] != status["policy_applied_revision"]):
                    raise HTTPException(
                        503, "Personal budget is still initializing or synchronizing",
                    )
            headers["x-turnstile-delegation-token"] = "Bearer " + delegation
            for header, claim in (
                ("x-user-id", "preferred_username"), ("x-user-name", "user_name"),
                ("x-org-id", "organization_id"), ("x-org-name", "organization_name"),
                ("x-department-id", "department_id"), ("x-department-name", "department_name"),
            ):
                headers[header] = str(payload[claim])
        if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in headers.values()):
            raise HTTPException(409, "Invocation metadata contains invalid header characters")
        if delegation is not None:
            headers = encode_delegation_metadata(headers)
        return httpx.Headers([
            (key.encode("ascii"), value.encode("utf-8")) for key, value in headers.items()
        ])

    def _context_payload(
        self, identity: SessionIdentity, selection: dict[str, Any], model: str, rid: str,
    ) -> dict[str, Any]:
        if self.settings.directory_source == "database" and (
            not identity.governance_user_id or identity.directory_person_active is not True
        ):
            raise HTTPException(409, "Link an active person before model invocation")
        catalog = catalog_for(self.repository, self.settings)
        person = next((x for x in catalog.users if x.id == identity.effective_user_id), None)
        if not person:
            raise HTTPException(409, "Active invocation identity is unavailable")
        department = next((x for x in catalog.departments if x.id == person.parent_id), None)
        organization = next(
            (x for x in catalog.organizations if department and x.id == department.parent_id), None
        )
        if not department or not organization:
            raise HTTPException(409, "Complete the person's organization and department first")
        now = datetime.now(UTC)
        payload = {
            "iss": CLIENT_ISSUER,
            "aud": CLIENT_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(seconds=60),
            "jti": rid,
            "sub": identity.id,
            "tid": identity.id,
            "oid": identity.id,
            "preferred_username": identity.effective_user_id,
            "scp": "Model.Invoke",
            "roles": [department.id],
            "user_name": person.name,
            "organization_id": organization.id,
            "organization_name": organization.name,
            "department_id": department.id,
            "department_name": department.name,
            "model": model,
            "subscription": selection["apim_subscription_id"],
            "application_id": selection["application_id"],
        }
        return payload
