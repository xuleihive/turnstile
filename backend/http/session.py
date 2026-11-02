from __future__ import annotations

import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, HTTPException, Request

from turnstile_core.config import Settings, get_settings
from turnstile_core.integrations.apim_target_client_factory import (
    AzureApimSubscriptionKeyClientResolver,
    apim_target_client_config_resolver,
)
from turnstile_core.persistence.auth_store import AuthStore
from turnstile_core.security import CredentialCipher

from ..services.auth_service import hash_session_token
from .dependencies import get_repository

_MODEL_GATEWAY_PATHS = frozenset({
    "/v1/models",
    "/v1/chat/completions",
    "/chat/completions",
})
_SUBSCRIPTION_KEY_CACHE_LOCK = threading.RLock()
_VERIFIED_SUBSCRIPTION_KEYS: dict[str, tuple[datetime, dict[str, Any]]] = {}


def _is_model_gateway_path(path: str) -> bool:
    return path.startswith("/api/v1/model-gateway/") or path in _MODEL_GATEWAY_PATHS


def _hash_subscription_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def cache_verified_subscription_key(
    raw_key: str,
    *,
    application_id: UUID | str | None = None,
    apim_subscription_id: str | None = None,
    gateway_profile_id: UUID | str | None = None,
    ttl_seconds: int = 900,
) -> None:
    cleaned = raw_key.strip()
    if not cleaned:
        return
    key_hash = _hash_subscription_key(cleaned)
    expires_at = datetime.now(UTC) + timedelta(seconds=max(1, ttl_seconds))
    meta = {
        "application_id": str(application_id) if application_id else None,
        "apim_subscription_id": str(apim_subscription_id) if apim_subscription_id else None,
        "gateway_profile_id": str(gateway_profile_id) if gateway_profile_id else None,
    }
    with _SUBSCRIPTION_KEY_CACHE_LOCK:
        _VERIFIED_SUBSCRIPTION_KEYS[key_hash] = (expires_at, meta)


def _lookup_cached_subscription_key(raw_key: str) -> dict[str, Any] | None:
    key_hash = _hash_subscription_key(raw_key)
    now = datetime.now(UTC)
    with _SUBSCRIPTION_KEY_CACHE_LOCK:
        entry = _VERIFIED_SUBSCRIPTION_KEYS.get(key_hash)
        if entry is None:
            return None
        expires_at, meta = entry
        if expires_at <= now:
            _VERIFIED_SUBSCRIPTION_KEYS.pop(key_hash, None)
            return None
        return dict(meta)


@lru_cache
def get_auth_store() -> AuthStore:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for authentication")
    return AuthStore(settings.database_url)


Store = Annotated[AuthStore, Depends(get_auth_store)]
Config = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class SessionIdentity:
    id: str
    email: str
    name: str | None
    role: Literal["owner", "member"]
    method: Literal["password", "entra", "subscription_key"]
    session_expires_at: datetime
    application_id: str | None = None
    apim_subscription_id: str | None = None
    allowed_model_ids: frozenset[UUID] | None = None


def _resolve_subscription_key_meta(
    raw_key: str,
    request: Request,
    repository: Any,
    settings: Settings,
) -> dict[str, Any] | None:
    cached = _lookup_cached_subscription_key(raw_key)
    if cached is not None:
        return cached

    apps = list(repository.list_gateway_applications())
    app_by_id = {str(a["id"]): a for a in apps}
    app_ids = [UUID(str(a["id"])) for a in apps]
    subs = list(repository.list_gateway_application_subscriptions(app_ids)) if app_ids else []

    for sub in subs:
        for field in ("primary_key", "secondary_key", "subscription_key"):
            candidate = sub.get(field)
            if (
                isinstance(candidate, str)
                and candidate
                and hmac.compare_digest(candidate.strip(), raw_key)
            ):
                meta: dict[str, str | None] = {
                    "application_id": str(sub["application_id"]),
                    "apim_subscription_id": str(sub["apim_subscription_id"]),
                    "gateway_profile_id": str(sub["gateway_profile_id"]),
                }
                cache_verified_subscription_key(
                    raw_key,
                    application_id=meta["application_id"],
                    apim_subscription_id=meta["apim_subscription_id"],
                    gateway_profile_id=meta["gateway_profile_id"],
                )
                return meta

    for secret_setting, suffix_hint in (
        (settings.apim_dashboard_subscription_key, "-dashboard"),
        (settings.apim_probe_subscription_key, "-publisher-probe"),
    ):
        if secret_setting is not None:
            secret_val = secret_setting.get_secret_value().strip()
            if secret_val and hmac.compare_digest(secret_val, raw_key):
                matched_sub = next(
                    (
                        s for s in subs
                        if str(s.get("apim_subscription_id", "")).endswith(suffix_hint)
                    ),
                    None,
                )
                meta = {
                    "application_id": (
                        str(matched_sub["application_id"]) if matched_sub else None
                    ),
                    "apim_subscription_id": (
                        str(matched_sub["apim_subscription_id"])
                        if matched_sub
                        else suffix_hint.lstrip("-")
                    ),
                    "gateway_profile_id": (
                        str(matched_sub["gateway_profile_id"]) if matched_sub else None
                    ),
                }
                cache_verified_subscription_key(
                    raw_key,
                    application_id=meta["application_id"],
                    apim_subscription_id=meta["apim_subscription_id"],
                    gateway_profile_id=meta["gateway_profile_id"],
                )
                return meta

    try:
        cipher = CredentialCipher.from_settings(settings)
        gateways = repository.registry().get("gateways") or []
        for gw in gateways:
            ciphertext = gw.get("credential_ciphertext")
            if not ciphertext:
                continue
            decrypted = cipher.decrypt(ciphertext)
            if (
                isinstance(decrypted, str)
                and decrypted
                and hmac.compare_digest(decrypted.strip(), raw_key)
            ):
                gw_id = str(gw["id"])
                matched_sub = next(
                    (
                        s for s in subs
                        if str(s.get("gateway_profile_id")) == gw_id
                        and str(s.get("apim_subscription_id", "")).endswith("-dashboard")
                    ),
                    next((s for s in subs if str(s.get("gateway_profile_id")) == gw_id), None),
                )
                meta = {
                    "application_id": (
                        str(matched_sub["application_id"]) if matched_sub else None
                    ),
                    "apim_subscription_id": (
                        str(matched_sub["apim_subscription_id"])
                        if matched_sub
                        else "dashboard"
                    ),
                    "gateway_profile_id": gw_id,
                }
                cache_verified_subscription_key(
                    raw_key,
                    application_id=meta["application_id"],
                    apim_subscription_id=meta["apim_subscription_id"],
                    gateway_profile_id=meta["gateway_profile_id"],
                )
                return meta
    except Exception:
        pass

    key_settings_available = all(
        (
            settings.azure_subscription_id,
            settings.apim_resource_group,
            settings.apim_service_name,
        )
    ) or settings.apim_target_registry_enabled
    if not (
        settings.gateway_application_key_management_enabled
        and key_settings_available
        and subs
    ):
        return None

    active_subs = [
        s for s in subs
        if s.get("state") == "active"
        and app_by_id.get(str(s["application_id"]), {}).get("status") == "active"
        and not app_by_id.get(str(s["application_id"]), {}).get("system_managed")
    ]
    hint_sub_id = (request.headers.get("x-apim-subscription-id") or "").strip().casefold()
    hint_app_id = (request.headers.get("x-application-id") or "").strip().casefold()
    active_subs.sort(
        key=lambda s: (
            0 if (
                (hint_sub_id and str(s.get("apim_subscription_id", "")).casefold() == hint_sub_id)
                or (hint_app_id and str(s.get("application_id", "")).casefold() == hint_app_id)
            ) else 1,
            str(s.get("apim_subscription_id", "")),
        )
    )
    try:
        resolver = AzureApimSubscriptionKeyClientResolver(
            apim_target_client_config_resolver(repository, settings)
        )

        def _fetch_sub_keys(sub_row: dict[str, Any]) -> tuple[dict[str, Any], str, str] | None:
            try:
                client = resolver.resolve(UUID(str(sub_row["gateway_profile_id"])))
                p_key, s_key = client.application_subscription_keys(
                    str(sub_row["apim_subscription_id"])
                )
                return sub_row, p_key, s_key
            except Exception:
                return None

        batch_size = 8
        for start in range(0, len(active_subs), batch_size):
            batch = active_subs[start : start + batch_size]
            with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                futures = [pool.submit(_fetch_sub_keys, sub_row) for sub_row in batch]
                for fut in as_completed(futures):
                    res = fut.result()
                    if res is None:
                        continue
                    sub_row, p_key, s_key = res
                    sub_app_id = str(sub_row["application_id"])
                    sub_apim_id = str(sub_row["apim_subscription_id"])
                    sub_gw_id = str(sub_row["gateway_profile_id"])
                    cache_verified_subscription_key(
                        p_key,
                        application_id=sub_app_id,
                        apim_subscription_id=sub_apim_id,
                        gateway_profile_id=sub_gw_id,
                    )
                    cache_verified_subscription_key(
                        s_key,
                        application_id=sub_app_id,
                        apim_subscription_id=sub_apim_id,
                        gateway_profile_id=sub_gw_id,
                    )
            found = _lookup_cached_subscription_key(raw_key)
            if found is not None:
                return found
    except Exception:
        pass
    return _lookup_cached_subscription_key(raw_key)


def _authenticate_subscription_key(
    raw_key: str,
    request: Request,
    settings: Settings,
) -> SessionIdentity:
    repo_provider = request.app.dependency_overrides.get(get_repository, get_repository)
    repository = repo_provider()
    meta = _resolve_subscription_key_meta(raw_key, request, repository, settings)
    if meta is None:
        raise HTTPException(status_code=401, detail="Invalid Ocp-Apim-Subscription-Key")

    app_id_str = meta.get("application_id")
    apim_sub_id = meta.get("apim_subscription_id")
    app_row: dict[str, Any] | None = None
    allowed_model_ids: frozenset[UUID] | None = None
    if app_id_str:
        try:
            app_uuid = UUID(str(app_id_str))
            app_row = repository.get_gateway_application(app_uuid)
            if app_row is not None and app_row.get("status") in {"suspended", "retired"}:
                raise HTTPException(
                    status_code=403,
                    detail="Application subscription is not active",
                )
            access_rows = list(repository.list_gateway_application_model_access([app_uuid]))
            if access_rows:
                allowed_model_ids = frozenset(
                    UUID(str(r["model_id"]))
                    for r in access_rows
                    if r.get("model_id") is not None
                )
        except HTTPException:
            raise
        except Exception:
            pass

    identity_id = str(
        app_id_str
        or uuid5(NAMESPACE_URL, f"turnstile-subscription:{apim_sub_id or 'default'}")
    )
    raw_owner = str((app_row or {}).get("owner_id") or "").strip()
    raw_creator = str((app_row or {}).get("created_by") or "").strip()
    owner_email = raw_owner or (raw_creator if "@" in raw_creator else "")
    effective_email = (
        owner_email
        or f"{apim_sub_id or 'application'}@applications.turnstile.local"
    )
    effective_name = str(
        (app_row or {}).get("display_name") or apim_sub_id or effective_email
    )
    return SessionIdentity(
        id=identity_id,
        email=effective_email,
        name=effective_name,
        role="member",
        method="subscription_key",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
        application_id=app_id_str,
        apim_subscription_id=apim_sub_id,
        allowed_model_ids=allowed_model_ids,
    )


def require_authenticated_session(
    request: Request,
    store: Store,
    settings: Config,
) -> SessionIdentity:
    is_gateway = _is_model_gateway_path(request.url.path)
    explicit_sub_key = (
        request.headers.get("ocp-apim-subscription-key")
        or request.headers.get("api-key")
        or ""
    ).strip()
    if is_gateway and explicit_sub_key:
        return _authenticate_subscription_key(explicit_sub_key, request, settings)

    session = request.cookies.get(settings.session_cookie_name)
    bearer_token = ""
    if not session:
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            bearer_token = auth_header[7:].strip()
            session = bearer_token
    if not session:
        raise HTTPException(status_code=401, detail="未登录。")
    owner = store.session_owner(hash_session_token(session))
    if not owner:
        if is_gateway and bearer_token:
            return _authenticate_subscription_key(bearer_token, request, settings)
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
    role = str(owner["role"])
    method = str(owner["method"])
    if role not in {"owner", "member"} or method not in {"password", "entra"}:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录。")
    return SessionIdentity(
        id=str(owner["id"]),
        email=str(owner["email"]),
        name=owner.get("display_name"),
        role=role,  # type: ignore[arg-type]
        method=method,  # type: ignore[arg-type]
        session_expires_at=owner["expires_at"],
    )


CurrentSession = Annotated[SessionIdentity, Depends(require_authenticated_session)]


def require_owner_session(identity: CurrentSession) -> SessionIdentity:
    if identity.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role is required")
    return identity


OwnerSession = Annotated[SessionIdentity, Depends(require_owner_session)]


def require_allowed_write_origin(request: Request, settings: Config) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if _is_model_gateway_path(request.url.path) and (
        request.headers.get("ocp-apim-subscription-key")
        or request.headers.get("api-key")
    ):
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    normalized = origin.rstrip("/")
    allowed = {value.rstrip("/") for value in settings.cors_origins}
    same_host = urlsplit(normalized).netloc == request.headers.get("host")
    if normalized not in allowed and not same_host:
        raise HTTPException(status_code=403, detail="Writes from this origin are not allowed.")
