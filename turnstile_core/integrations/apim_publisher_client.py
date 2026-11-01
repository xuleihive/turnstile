# APIM response and policy literals are intentionally kept on protocol-shaped lines.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from html import unescape
from threading import Event, Lock
from time import monotonic
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from urllib.parse import unquote as url_unquote
from uuid import UUID
from xml.etree import ElementTree

import httpx
from pydantic import HttpUrl, SecretStr, ValidationError

from ..domain.application_access import (
    ApplicationScopeType,
    ApplicationSubscriptionState,
    ApplicationType,
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    GatewayApplicationSubscriptionProvisionSpec,
)
from ..domain.billable_requests import BillableRequestOutcome
from ..domain.control_plane import (
    COUNT_TOKENS_REJECTIONS,
    PUBLICATION_OBSERVER_ACTIONS,
    ApiFormat,
    AuthStrategy,
    DiscoveryModel,
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
    PublicationKind,
    publication_model_id,
)
from ..domain.gateway_targets import (
    ApimTargetArmInventory,
    ApimTargetCapabilitySnapshot,
    ApimTierFamily,
    CapabilitySupport,
    GatewayArmInventory,
    GatewayCertificateAuthorityInventory,
    GatewayHostnameConfigurationInventory,
    self_hosted_gateway_capability_for_tier,
)
from ..domain.image_profiles import ImageGenerationProfile, validate_image_profile
from ..domain.model_availability import (
    AVAILABILITY_CONFIRMATION_PARTITION,
    ModelAvailabilityConfirmationPlan,
    ModelAvailabilityCoverage,
    ModelAvailabilityNodeEnrollment,
    ModelAvailabilityStagedProgram,
    ModelAvailabilityState,
    ModelAvailabilityTargetResult,
    ModelAvailabilityUnavailable,
)
from ..domain.storage_routes import ConnectionRevision, ProgramCapabilityProof, document_digest
from ..integrations.reconciliation import ManagedIdentityTokenProvider
from .apim_control_plane_contract import (
    PUBLICATION_OAUTH_ACTIONS,
    PUBLICATION_RECOVERY_ACTIONS,
    PUBLICATION_REQUIRED_ACTIONS,
    ApimClientSettings,
    AuthorizationRequiredError,
    BackendCircuitBreakerResource,
    BackendPoolMemberResource,
    BackendPoolResource,
    BackendResource,
    CompiledGatewayRelease,
    CredentialSourceMissingError,
    GatewayEndpointProbeResult,
    ImageProbeJournal,
    InfrastructureUpgradeRequiredError,
    ManagementTokenProvider,
    NamedValueResource,
    OAuthCredentialResource,
    OperationResource,
    PolicyCompilationError,
    ProbeRequestLimitExceeded,
    ProbeVerificationDeferred,
    ProbeVerificationJournal,
    PublisherPermissionError,
    ReleaseGcPlanEvidence,
    RetryablePublicationError,
    missing_management_actions,
)
from .apim_policy_compiler import ApimPolicyCompiler
from .apim_policy_components import (
    component_digest,
    parent_readback_matches,
    parse_policy,
    validate_policy_upload,
)
from .apim_self_hosted_gateway import AzureSelfHostedGatewayManagementMixin
from .apim_storage_routes import FixedProgramVerifier
from .ledger import TableStorageLedger
from .model_availability_verifier import ModelAvailabilityGatewayVerifier


class _AvailabilityManagementReadPacer:
    def __init__(self) -> None:
        self._lock = Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            now = monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + 1 / 16
        if slot > now:
            Event().wait(slot - now)


_availability_management_reads = _AvailabilityManagementReadPacer()


class AzureApimPublisherClient(AzureSelfHostedGatewayManagementMixin):
    _MANAGEMENT_ORIGIN = "https://management.azure.com"
    _MANAGEMENT_RESOURCE = f"{_MANAGEMENT_ORIGIN}/"
    _API_VERSION = "2024-05-01"
    _AFFINITY_API_VERSION = "2025-09-01-preview"
    _MANAGEMENT_REQUEST_TIMEOUT_SECONDS = 30.0
    _CANDIDATE_PROBE_TIMEOUT_SECONDS = 120.0
    _CANDIDATE_TRANSIENT_ATTEMPTS = 3
    _APPLICATION_AUTH_PROBE_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
    settings: ApimClientSettings,
        token_provider: ManagementTokenProvider | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        required = {
            "AZURE_SUBSCRIPTION_ID": settings.azure_subscription_id,
            "APIM_RESOURCE_GROUP": settings.apim_resource_group,
            "APIM_SERVICE_NAME": settings.apim_service_name,
            "APIM_GATEWAY_URL": settings.apim_gateway_url,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Control-plane settings are missing: {', '.join(missing)}")
        self._settings = settings
        self._probe_journal: ContextVar[ImageProbeJournal | None] = ContextVar(
            "apim-probe-journal", default=None,
        )
        self._probe_verification: ContextVar[ProbeVerificationJournal | None] = ContextVar(
            "apim-probe-verification", default=None,
        )
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client or httpx.Client(
            timeout=self._MANAGEMENT_REQUEST_TIMEOUT_SECONDS
        )
        self._owns_client = client is None
        self._resource_id_base = (
            f"/subscriptions/{settings.azure_subscription_id}"
            f"/resourceGroups/{settings.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{settings.apim_service_name}"
        )
        self._base = f"{self._MANAGEMENT_ORIGIN}{self._resource_id_base}"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        token = self._token_provider.token(self._MANAGEMENT_RESOURCE)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def verify_publication_permissions(
        self, compiled: CompiledGatewayRelease, *, recovery_only: bool = False,
    ) -> None:
        required = PUBLICATION_RECOVERY_ACTIONS if recovery_only else PUBLICATION_REQUIRED_ACTIONS + (
            PUBLICATION_OAUTH_ACTIONS if compiled.oauth_credentials else ()
        )
        request_id = None
        try:
            response = self._client.get(
                self._base + "/providers/Microsoft.Authorization/permissions",
                params={"api-version": "2022-04-01"},
                headers=self._headers(), timeout=10.0, follow_redirects=False,
            )
            correlation = response.headers.get("x-ms-request-id", "")
            if re.fullmatch(r"[a-fA-F0-9-]{36}", correlation):
                request_id = str(UUID(correlation))
            response.raise_for_status()
            missing = missing_management_actions(response.json(), required)
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
            raise PublisherPermissionError(request_id=request_id) from None
        if missing:
            raise PublisherPermissionError(missing_actions=missing, request_id=request_id)

    def verify_observer_permissions(self, named_value_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", named_value_id):
            raise PolicyCompilationError("Observer credential source identity is invalid")
        path = "/namedValues/" + quote(named_value_id, safe="")
        request_id = None
        try:
            response = self._client.get(
                self._base + path + "/providers/Microsoft.Authorization/permissions",
                params={"api-version": "2022-04-01"}, headers=self._headers(),
                timeout=10.0, follow_redirects=False,
            )
            supplied_id = response.headers.get("x-ms-request-id", "")
            if re.fullmatch(r"[a-fA-F0-9-]{36}", supplied_id):
                request_id = str(UUID(supplied_id))
            response.raise_for_status()
            missing = missing_management_actions(response.json(), PUBLICATION_OBSERVER_ACTIONS)
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
            raise PublisherPermissionError(request_id=request_id) from None
        if missing:
            raise PublisherPermissionError(missing_actions=missing, request_id=request_id)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
        api_version: str | None = None,
    ) -> httpx.Response:
        headers = self._headers()
        headers.update(extra_headers or {})
        resource_path, separator, raw_query = path.partition("?")
        params = (
            dict(parse_qsl(raw_query, keep_blank_values=True))
            if separator
            else {}
        )
        params["api-version"] = api_version or self._API_VERSION
        response = self._client.request(
            method,
            f"{self._base}{resource_path}",
            params=params,
            headers=headers,
            json=json_body,
            timeout=self._MANAGEMENT_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if allow_not_found and response.status_code == 404:
            return response
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryablePublicationError(
                f"APIM management request returned HTTP {response.status_code}"
            ) from httpx.HTTPStatusError(
                "APIM management request is temporarily unavailable",
                request=response.request, response=response,
            )
        if response.is_error:
            diagnostic: dict[str, Any] = {"method": method, "path": resource_path, "status": response.status_code}
            try:
                payload = response.json()
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                if isinstance(error, dict):
                    code = error.get("code")
                    if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", code):
                        diagnostic["code"] = code
                    details = error.get("details", [])
                    if isinstance(details, list):
                        diagnostic["locations"] = [
                            {name: detail[name] for name in ("code", "target")
                             if isinstance(detail.get(name), str)
                             and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\[\]-]{0,127}", detail[name])}
                            for detail in details[:8] if isinstance(detail, dict)
                        ]
            except ValueError:
                pass
            request_id = response.headers.get("x-ms-request-id", "")
            if re.fullmatch(r"[a-fA-F0-9-]{1,128}", request_id):
                diagnostic["request_id"] = request_id
            if json_body and resource_path.endswith("/operations/" + resource_path.rsplit("/", 1)[-1]):
                diagnostic["request_sha256"] = hashlib.sha256(
                    json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            raise PolicyCompilationError("APIM management failed: " + json.dumps(diagnostic, sort_keys=True))
        return response

    def discover_apim_target_inventory(
        self, gateway_profile_id: UUID
    ) -> ApimTargetArmInventory:
        discovered_at = datetime.now(UTC)
        service = self._request("GET", "").json()
        service_id = str(service.get("id") or "")
        if service_id.casefold() != self._resource_id_base.casefold():
            raise PolicyCompilationError(
                "APIM service inventory returned another resource identity"
            )
        sku = service.get("sku")
        if not isinstance(sku, Mapping):
            raise PolicyCompilationError("APIM service inventory returned no SKU")
        service_tier = str(sku.get("name") or "").strip()
        service_location = str(service.get("location") or "").strip()
        if not service_tier or not service_location:
            raise PolicyCompilationError(
                "APIM service inventory returned incomplete tier or location"
            )
        tier_family = (
            ApimTierFamily.V2
            if service_tier.casefold().endswith("v2")
            else ApimTierFamily.CLASSIC
        )
        self_hosted_support = self_hosted_gateway_capability_for_tier(service_tier)
        identity = service.get("identity")
        principal_value = (
            identity.get("principalId") if isinstance(identity, Mapping) else None
        )
        try:
            principal_id = UUID(str(principal_value)) if principal_value else None
            subscription_id = UUID(str(self._settings.azure_subscription_id))
        except ValueError as error:
            raise PolicyCompilationError(
                "APIM service inventory returned an invalid identity"
            ) from error

        api = self._request(
            "GET", f"/apis/{quote(self._settings.apim_api_id, safe='')}"
        ).json()
        api_path = self._resource_path(api)
        if api_path.casefold() != (
            f"/apis/{self._settings.apim_api_id}".casefold()
        ):
            raise PolicyCompilationError("APIM inventory returned another API identity")
        api_properties = api.get("properties")
        if not isinstance(api_properties, Mapping):
            raise PolicyCompilationError("APIM inventory returned no API properties")
        public_api_path = str(api_properties.get("path") or "").strip("/")
        api_revision = str(api_properties.get("apiRevision") or "").strip()
        if not public_api_path or not api_revision:
            raise PolicyCompilationError(
                "APIM inventory returned incomplete API path or Revision"
            )

        gateways: list[GatewayArmInventory] = []
        native_gateways = (
            self._list_all("/gateways")
            if self_hosted_support is CapabilitySupport.SUPPORTED else []
        )
        for gateway in native_gateways:
            native_gateway_id = self._resource_name(gateway)
            gateway_path = self._resource_path(gateway)
            expected_path = f"/gateways/{native_gateway_id}"
            if gateway_path.casefold() != expected_path.casefold():
                raise PolicyCompilationError(
                    "APIM Gateway inventory returned an unexpected resource path"
                )
            properties = gateway.get("properties")
            if not isinstance(properties, Mapping):
                properties = {}
            location_data = properties.get("locationData")
            if not isinstance(location_data, Mapping):
                location_data = {}
            encoded_gateway = quote(native_gateway_id, safe="")
            associated_api_ids: list[str] = []
            for value in self._list_all(f"/gateways/{encoded_gateway}/apis"):
                associated_path = self._resource_path(value)
                expected_prefix = f"/gateways/{native_gateway_id}/apis/"
                if not associated_path.casefold().startswith(
                    expected_prefix.casefold()
                ):
                    raise PolicyCompilationError(
                        "APIM Gateway API association returned another Gateway"
                    )
                associated_api_ids.append(self._resource_name(value))
            hostname_configurations = tuple(
                sorted(
                    (
                        self._hostname_inventory(native_gateway_id, value)
                        for value in self._list_all(
                            f"/gateways/{encoded_gateway}/hostnameConfigurations"
                        )
                    ),
                    key=lambda value: value.name.casefold(),
                )
            )
            certificate_authorities = tuple(
                sorted(
                    (
                        self._certificate_authority_inventory(
                            native_gateway_id, value
                        )
                        for value in self._list_all(
                            f"/gateways/{encoded_gateway}/certificateAuthorities"
                        )
                    ),
                    key=lambda value: value.name.casefold(),
                )
            )
            gateways.append(
                GatewayArmInventory(
                    native_gateway_id=native_gateway_id,
                    arm_resource_id=service_id + gateway_path,
                    description=self._optional_string(properties.get("description")),
                    location_name=self._optional_string(location_data.get("name")),
                    city=self._optional_string(location_data.get("city")),
                    district=self._optional_string(location_data.get("district")),
                    country_or_region=self._optional_string(
                        location_data.get("countryOrRegion")
                    ),
                    associated_api_ids=tuple(
                        sorted(set(associated_api_ids), key=str.casefold)
                    ),
                    hostname_configurations=hostname_configurations,
                    certificate_authorities=certificate_authorities,
                )
            )
        gateways.sort(key=lambda value: value.native_gateway_id.casefold())
        return ApimTargetArmInventory(
            gateway_profile_id=gateway_profile_id,
            arm_resource_id=service_id,
            subscription_id=subscription_id,
            resource_group=str(self._settings.apim_resource_group),
            service_name=str(self._settings.apim_service_name),
            api_id=self._settings.apim_api_id,
            api_path=public_api_path,
            api_revision=api_revision,
            service_tier=service_tier,
            tier_family=tier_family,
            service_location=service_location,
            principal_id=principal_id,
            managed_gateway_url=HttpUrl(str(self._settings.apim_gateway_url)),
            capabilities=ApimTargetCapabilitySnapshot(
                self_hosted_gateway=self_hosted_support,
                source_api_version=self._API_VERSION,
                observed_at=discovered_at,
            ),
            gateways=tuple(gateways),
            source_api_version=self._API_VERSION,
            discovered_at=discovered_at,
        )

    def _hostname_inventory(
        self, native_gateway_id: str, value: Mapping[str, Any]
    ) -> GatewayHostnameConfigurationInventory:
        resource_path = self._resource_path(value)
        expected_prefix = (
            f"/gateways/{native_gateway_id}/hostnameConfigurations/"
        )
        if not resource_path.casefold().startswith(expected_prefix.casefold()):
            raise PolicyCompilationError(
                "APIM Gateway hostname inventory returned another Gateway"
            )
        properties = value.get("properties")
        if not isinstance(properties, Mapping):
            raise PolicyCompilationError(
                "APIM Gateway hostname inventory returned no properties"
            )
        hostname = str(properties.get("hostname") or "").strip()
        if not hostname:
            raise PolicyCompilationError(
                "APIM Gateway hostname inventory returned no hostname"
            )
        certificate_id = self._optional_string(properties.get("certificateId"))
        if certificate_id is not None:
            certificate_path = self._resource_path({"id": certificate_id})
            if not certificate_path.casefold().startswith("/certificates/"):
                raise PolicyCompilationError(
                    "APIM Gateway hostname returned an unexpected certificate identity"
                )
        return GatewayHostnameConfigurationInventory(
            configuration_id=self._resource_id_base + resource_path,
            name=self._resource_name(value),
            hostname=hostname,
            certificate_id=certificate_id,
            negotiate_client_certificate=bool(
                properties.get("negotiateClientCertificate", False)
            ),
        )

    def _certificate_authority_inventory(
        self, native_gateway_id: str, value: Mapping[str, Any]
    ) -> GatewayCertificateAuthorityInventory:
        resource_path = self._resource_path(value)
        expected_prefix = (
            f"/gateways/{native_gateway_id}/certificateAuthorities/"
        )
        if not resource_path.casefold().startswith(expected_prefix.casefold()):
            raise PolicyCompilationError(
                "APIM Gateway certificate authority returned another Gateway"
            )
        properties = value.get("properties")
        if not isinstance(properties, Mapping):
            raise PolicyCompilationError(
                "APIM Gateway certificate authority returned no properties"
            )
        return GatewayCertificateAuthorityInventory(
            authority_id=self._resource_id_base + resource_path,
            name=self._resource_name(value),
            is_trusted=bool(properties.get("isTrusted", False)),
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    def ensure_backend(self, backend: BackendResource) -> None:
        path = f"/backends/{quote(backend.id, safe='')}"
        api_version = (
            self._AFFINITY_API_VERSION
            if isinstance(backend, BackendPoolResource) and backend.session_cookie_name
            else None
        )
        observed = self._request("GET", path, allow_not_found=True, api_version=api_version)
        if observed.status_code != 404:
            observed_properties = observed.json().get("properties") or {}
            if not isinstance(observed_properties, Mapping) or not self._backend_identity_matches(
                backend, observed_properties
            ):
                raise PolicyCompilationError(
                    f"Immutable APIM backend {backend.id} exists with different settings"
                )
            return
        if isinstance(backend, BackendPoolResource):
            pool_properties: dict[str, object] = {"services": self._pool_services(backend)}
            if backend.session_cookie_name:
                pool_properties["sessionAffinity"] = {
                    "sessionId": {"source": "cookie", "name": backend.session_cookie_name}
                }
            self._request(
                "PUT",
                path,
                api_version=api_version,
                json_body={
                    "properties": {
                        "title": backend.title,
                        "description": "Turnstile-managed immutable deployment pool.",
                        "type": "Pool",
                        "pool": pool_properties,
                    }
                },
            )
            if backend.session_cookie_name:
                readback = self._request("GET", path, api_version=api_version)
                readback_properties = readback.json().get("properties") or {}
                if not self._backend_identity_matches(backend, readback_properties):
                    raise RetryablePublicationError("APIM Pool session affinity readback differs")
            return
        properties: dict[str, object] = {
            "title": backend.title,
            "description": "Turnstile-managed immutable gateway backend.",
            "type": "Single",
            "protocol": "http",
            "url": backend.url,
            "tls": {
                "validateCertificateChain": True,
                "validateCertificateName": True,
            },
        }
        if backend.headers:
            properties["credentials"] = {
                "header": {name: [value] for name, value in backend.headers}
            }
        circuit_breaker = self._circuit_breaker_properties(backend.circuit_breaker)
        if circuit_breaker is not None:
            properties["circuitBreaker"] = circuit_breaker
        self._request(
            "PUT",
            path,
            json_body={"properties": properties},
        )

    def _backend_identity_matches(
        self,
        backend: BackendResource,
        observed_properties: Mapping[str, Any],
    ) -> bool:
        if isinstance(backend, BackendPoolResource):
            observed_pool = observed_properties.get("pool")
            observed_services = (
                observed_pool.get("services") or []
                if isinstance(observed_pool, Mapping)
                else []
            )
            expected_services = self._pool_services(backend)
            normalized_observed = {
                (
                    str(item.get("id", "")).casefold(),
                    int(item.get("priority", 0)),
                    int(item.get("weight", 0)),
                )
                for item in observed_services
                if isinstance(item, Mapping)
            }
            normalized_expected = {
                (
                    str(item["id"]).casefold(),
                    int(str(item["priority"])),
                    int(str(item["weight"])),
                )
                for item in expected_services
            }
            observed_affinity = (
                observed_pool.get("sessionAffinity") if isinstance(observed_pool, Mapping) else None
            )
            expected_affinity = (
                {"sessionId": {"source": "cookie", "name": backend.session_cookie_name}}
                if backend.session_cookie_name else None
            )
            return (
                observed_properties.get("type") == "Pool"
                and normalized_observed == normalized_expected
                and (observed_affinity or None) == expected_affinity
            )
        tls = observed_properties.get("tls")
        observed_tls = tls if isinstance(tls, Mapping) else {}
        credentials = observed_properties.get("credentials")
        observed_headers = (
            credentials.get("header") or {}
            if isinstance(credentials, Mapping)
            else {}
        )
        if not isinstance(observed_headers, Mapping):
            return False
        expected_headers = {name: [value] for name, value in backend.headers}
        headers_match = observed_headers.keys() == expected_headers.keys() and all(
            observed_headers[name] is None
            or observed_headers[name] == expected_value
            for name, expected_value in expected_headers.items()
        )
        return (
            str(observed_properties.get("url", "")).rstrip("/")
            == backend.url.rstrip("/")
            and observed_properties.get("type", "Single") == "Single"
            and observed_tls.get("validateCertificateChain") is True
            and observed_tls.get("validateCertificateName") is True
            and headers_match
            and self._normalized_circuit_breaker(
                observed_properties.get("circuitBreaker")
            )
            == self._normalized_circuit_breaker(
                self._circuit_breaker_properties(backend.circuit_breaker)
            )
        )

    def pool_deployment_catalog(self, binding: GatewayModelBinding) -> dict[str, Any]:
        from ..security import CredentialCipher
        from .gateway_connection_probe import foundry_deployment_catalog
        from .route_credential_vault import RouteCredentialVault

        def headers() -> dict[str, str]:
            if binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY:
                return {"Authorization": "Bearer " + self._token_provider.token(
                    "https://ai.azure.com",
                )}
            if binding.key_vault_secret_id is not None:
                encryption_key = self._settings.credential_encryption_key
                if encryption_key is None:
                    raise PolicyCompilationError("Deployment credential vault is unavailable")
                secret_id = str(binding.key_vault_secret_id)
                address = urlparse(secret_id)
                vault = RouteCredentialVault(
                    address.scheme + "://" + address.netloc,
                    CredentialCipher(encryption_key.get_secret_value().encode("utf-8")),
                    self._token_provider, self._client,
                )
                return {"api-key": vault.verify(secret_id).get_secret_value()}
            raise PolicyCompilationError("Deployment metadata credential is unavailable")

        return foundry_deployment_catalog(
            self._client, str(binding.runtime_config.get("project_endpoint") or ""),
            str(binding.backend_url), binding.model.upstream_model_id, headers,
        )

    def validate_pool_deployments(
        self,
        publication: GatewayPublication,
        *,
        runtimes: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        from ..domain.control_plane import pool_member_bindings
        from .gateway_connection_probe import (
            foundry_model_identity,
            verify_pool_deployment_observer,
        )
        from .route_credential_vault import credential_bindings, route_credential_source

        catalogs: dict[tuple[str, str], dict[str, Any]] = {}
        observer_verified = False
        credentials = credential_bindings(publication.resource_manifest)
        for binding in publication.desired_spec.bindings:
            pool = binding.backend_pool
            if pool is None or binding.provider_brand_key.value != "microsoft_foundry" or not any(
                member.model_id is not None or (
                    member.upstream_model_id is not None
                    and member.upstream_model_id != binding.model.upstream_model_id
                )
                for member in pool.members
            ):
                continue
            requires_rewriter = any(
                member.upstream_model_id is not None
                and member.upstream_model_id != binding.model.upstream_model_id
                for member in pool.members
            )
            if requires_rewriter and not observer_verified:
                observer = self._settings.apim_usage_observer_url
                key_name = self._settings.apim_usage_observer_key_named_value
                if not observer or not key_name:
                    raise PolicyCompilationError("Deployment-specific pools require the Observer")
                self.verify_observer_permissions(key_name)
                verify_pool_deployment_observer(
                    self._client, str(observer),
                    self.route_credential_value(key_name).get_secret_value(),
                )
                observer_verified = True
            identities = set()
            for source in pool_member_bindings(binding):
                secret_id = credentials.get(route_credential_source(source))
                if source.key_vault_secret_id is None and secret_id is not None:
                    source = source.model_copy(update={"key_vault_secret_id": secret_id})
                if not source.runtime_config.get("project_endpoint") and source.runtime_id:
                    matched_runtime = next(
                        (item for item in runtimes if item.get("id") == source.runtime_id),
                        None,
                    )
                    runtime_project = (
                        (matched_runtime.get("config") or {}).get("project_endpoint")
                        if matched_runtime else None
                    )
                    if runtime_project:
                        source = source.model_copy(update={"runtime_config": {
                            **source.runtime_config,
                            "project_endpoint": str(runtime_project).rstrip("/"),
                        }})
                key = (str(source.runtime_id), str(source.runtime_config.get("project_endpoint")))
                if key not in catalogs:
                    catalogs[key] = self.pool_deployment_catalog(source)
                identities.add(foundry_model_identity(
                    catalogs[key].get(source.model.upstream_model_id),
                ))
            if len(identities) != 1:
                raise PolicyCompilationError("Pool deployments must use the same model and version")

    def _pool_services(
        self,
        backend: BackendPoolResource,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": f"{self._resource_id_base}/backends/{member.backend_id}",
                "priority": member.priority + 1,
                "weight": member.weight,
            }
            for member in backend.members
        ]

    @staticmethod
    def _circuit_breaker_properties(
        breaker: BackendCircuitBreakerResource | None,
    ) -> dict[str, object] | None:
        if breaker is None:
            return None
        return {
            "rules": [
                {
                    "name": "deployment-fault-isolation",
                    "failureCondition": {
                        "count": breaker.failure_count,
                        "interval": f"PT{breaker.interval_seconds}S",
                        "statusCodeRanges": [
                            {"min": minimum, "max": maximum}
                            for minimum, maximum in breaker.status_code_ranges
                        ],
                        "errorReasons": list(breaker.error_reasons),
                    },
                    "tripDuration": f"PT{breaker.trip_duration_seconds}S",
                    "acceptRetryAfter": breaker.accept_retry_after,
                }
            ]
        }

    @staticmethod
    def _normalized_circuit_breaker(value: object) -> object:
        if not isinstance(value, dict) or not value:
            return None
        rules = value.get("rules")
        if not isinstance(rules, list):
            return value
        normalized = []
        for raw_rule in rules:
            if not isinstance(raw_rule, dict):
                normalized.append(raw_rule)
                continue
            condition = raw_rule.get("failureCondition") or {}
            ranges = condition.get("statusCodeRanges") or []
            normalized.append(
                {
                    "name": raw_rule.get("name"),
                    "failureCondition": {
                        "count": int(condition.get("count", 0)),
                        "interval": AzureApimPublisherClient._normalized_duration(
                            condition.get("interval")
                        ),
                        "statusCodeRanges": sorted(
                            (
                                {
                                    "min": int(item.get("min", 0)),
                                    "max": int(item.get("max", 0)),
                                }
                                for item in ranges
                                if isinstance(item, dict)
                            ),
                            key=lambda item: (item["min"], item["max"]),
                        ),
                    },
                    "tripDuration": AzureApimPublisherClient._normalized_duration(
                        raw_rule.get("tripDuration")
                    ),
                    "acceptRetryAfter": bool(raw_rule.get("acceptRetryAfter", False)),
                }
            )
        return {"rules": sorted(normalized, key=lambda item: str(item.get("name")))}

    @staticmethod
    def _normalized_duration(value: object) -> object:
        if not isinstance(value, str):
            return value
        match = re.fullmatch(
            r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
            r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
            value,
        )
        if match is None or not any(match.groupdict().values()):
            return value
        parts = {name: int(raw or 0) for name, raw in match.groupdict().items()}
        return (
            parts["days"] * 86_400
            + parts["hours"] * 3_600
            + parts["minutes"] * 60
            + parts["seconds"]
        )

    @staticmethod
    def _oauth_provider_properties(credential: OAuthCredentialResource) -> dict[str, Any]:
        config = credential.config
        return {
            "displayName": f"Turnstile {config.provider_id}",
            "identityProvider": "oauth2",
            "oauth2": {"grantTypes": {"clientCredentials": {
                "tokenUrl": str(config.token_url), "scopes": config.scopes,
            }}},
        }

    @classmethod
    def _oauth_provider_matches(
        cls, credential: OAuthCredentialResource, properties: Mapping[str, Any],
    ) -> bool:
        expected = cls._oauth_provider_properties(credential)
        oauth = properties.get("oauth2") or {}
        grants = oauth.get("grantTypes") or {}
        settings = grants.get("clientCredentials") or {}
        return (
            properties.get("displayName") == expected["displayName"]
            and properties.get("identityProvider") == "oauth2"
            and not grants.get("authorizationCode")
            and settings.get("tokenUrl") == str(credential.config.token_url)
            and settings.get("scopes") == credential.config.scopes
        )

    def _oauth_identity(self) -> dict[str, str]:
        identity = self._request("GET", "").json().get("identity") or {}
        principal_id, tenant_id = identity.get("principalId"), identity.get("tenantId")
        if (
            not principal_id or not tenant_id
            or self._settings.apim_principal_id
            and principal_id != self._settings.apim_principal_id
        ):
            raise PolicyCompilationError("Credential Manager requires the configured APIM system identity")
        return {"objectId": str(UUID(principal_id)), "tenantId": str(UUID(tenant_id))}

    def oauth_credential_issues(self, credential: OAuthCredentialResource) -> list[str]:
        config = credential.config
        provider_path = f"/authorizationProviders/{config.provider_id}"
        authorization_path = provider_path + f"/authorizations/{config.authorization_id}"
        issues = []
        provider = self._request("GET", provider_path, allow_not_found=True)
        if provider.status_code == 404:
            return [f"missing_oauth_provider:{config.provider_id}"]
        if not self._oauth_provider_matches(credential, provider.json().get("properties") or {}):
            issues.append(f"mismatched_oauth_provider:{config.provider_id}")
        authorization = self._request("GET", authorization_path, allow_not_found=True)
        if authorization.status_code == 404:
            issues.append(f"missing_oauth_connection:{config.provider_id}")
        else:
            properties = authorization.json().get("properties") or {}
            if (
                properties.get("authorizationType") != "OAuth2"
                or properties.get("oauth2grantType") != "ClientCredentials"
                or (properties.get("parameters") or {}).get("clientId") != str(config.client_id)
                or properties.get("status") != "Connected"
            ):
                issues.append(f"mismatched_oauth_connection:{config.provider_id}")
        identity = self._oauth_identity()
        access = self._request(
            "GET", authorization_path + f"/accessPolicies/{identity['objectId']}",
            allow_not_found=True,
        )
        if access.status_code == 404:
            issues.append(f"missing_oauth_access_policy:{config.provider_id}")
        else:
            properties = access.json().get("properties") or {}
            if any(properties.get(key) != value for key, value in identity.items()) or properties.get("appIds"):
                issues.append(f"mismatched_oauth_access_policy:{config.provider_id}")
        policies = self._list_all(authorization_path + "/accessPolicies")
        if len(policies) != 1 or any(
            any((policy.get("properties") or {}).get(key) != value for key, value in identity.items())
            or (policy.get("properties") or {}).get("appIds")
            for policy in policies
        ):
            issues.append(f"mismatched_oauth_access_policy_set:{config.provider_id}")
        return issues

    def ensure_oauth_credential(self, credential: OAuthCredentialResource) -> None:
        if not self._settings.databricks_oauth_enabled:
            raise PolicyCompilationError("Databricks OAuth M2M is not enabled on the Publisher")
        config = credential.config
        identity = self._oauth_identity()
        provider_path = f"/authorizationProviders/{config.provider_id}"
        authorization_path = provider_path + f"/authorizations/{config.authorization_id}"
        provider = self._request("GET", provider_path, allow_not_found=True)
        if provider.status_code == 404:
            if credential.client_secret is None:
                raise PolicyCompilationError("OAuth credential has no secret source")
            self._request("PUT", provider_path, json_body={
                "properties": self._oauth_provider_properties(credential),
            }, extra_headers={"If-None-Match": "*"})
        elif not self._oauth_provider_matches(credential, provider.json().get("properties") or {}):
            raise PolicyCompilationError("OAuth provider belongs to a different connection")
        authorization = self._request("GET", authorization_path, allow_not_found=True)
        properties = authorization.json().get("properties") or {}
        if authorization.status_code != 404 and (
            properties.get("authorizationType") != "OAuth2"
            or properties.get("oauth2grantType") != "ClientCredentials"
            or (properties.get("parameters") or {}).get("clientId") != str(config.client_id)
        ):
            raise PolicyCompilationError("OAuth authorization belongs to a different client")
        if authorization.status_code == 404 or properties.get("status") != "Connected":
            if credential.client_secret is None:
                raise PolicyCompilationError("OAuth credential requires a new client secret")
            self._request("PUT", authorization_path, json_body={"properties": {
                "authorizationType": "OAuth2", "oauth2grantType": "ClientCredentials",
                "parameters": {
                    "clientId": str(config.client_id), "clientSecret": credential.client_secret,
                },
            }}, extra_headers=(
                {"If-None-Match": "*"} if authorization.status_code == 404
                else {"If-Match": authorization.headers.get("etag", "*")}
            ))
        access_path = authorization_path + f"/accessPolicies/{identity['objectId']}"
        access = self._request("GET", access_path, allow_not_found=True)
        if access.status_code == 404:
            self._request("PUT", access_path, json_body={"properties": identity},
                          extra_headers={"If-None-Match": "*"})
        issues = self.oauth_credential_issues(credential)
        if issues:
            raise PolicyCompilationError("OAuth credential readback failed: " + ", ".join(issues))

    def ensure_named_value(self, named_value: NamedValueResource) -> None:
        path = f"/namedValues/{quote(named_value.id, safe='')}"
        observed = self._request("GET", path, allow_not_found=True)
        if observed.status_code != 404:
            properties = observed.json().get("properties") or {}
            self._validate_named_value(named_value, properties)
            observed_secret = (properties.get("keyVault") or {}).get("secretIdentifier")
            if named_value.value is not None:
                if observed_secret is not None:
                    raise PolicyCompilationError(
                        f"Named value {named_value.id} is Key Vault-backed"
                    )
                expected_tag = (
                    self._publication_tag(named_value.owner_publication_id)
                    if named_value.owner_publication_id
                    else None
                )
                observed_tags = {
                    str(tag) for tag in (properties.get("tags") or [])
                }
                if expected_tag is None or expected_tag not in observed_tags:
                    raise PolicyCompilationError(
                        f"Named value {named_value.id} belongs to another publication"
                    )
                self._request(
                    "PUT",
                    path,
                    json_body={
                        "properties": {
                            "displayName": named_value.id,
                            "secret": True,
                            "value": named_value.value,
                            "tags": sorted(observed_tags),
                        }
                    },
                )
                self._read_named_value(named_value, path)
            return
        if named_value.key_vault_secret_id is None and named_value.value is None:
            raise CredentialSourceMissingError(named_value.id)
        write_properties: dict[str, object] = {
            "displayName": named_value.id,
            "secret": True,
        }
        if named_value.owner_publication_id:
            write_properties["tags"] = [
                self._publication_tag(named_value.owner_publication_id)
            ]
        if named_value.key_vault_secret_id is not None:
            write_properties["keyVault"] = {
                "secretIdentifier": named_value.key_vault_secret_id
            }
        else:
            write_properties["value"] = named_value.value
        self._request(
            "PUT",
            path,
            json_body={"properties": write_properties},
        )
        self._read_named_value(named_value, path)

    def _read_named_value(self, named_value: NamedValueResource, path: str) -> None:
        observed = self._request("GET", path, allow_not_found=True)
        if observed.status_code == 404:
            raise RetryablePublicationError("APIM credential resource is not yet readable")
        self._validate_named_value(named_value, observed.json().get("properties") or {})

    @staticmethod
    def _validate_named_value(named_value: NamedValueResource, properties: Mapping[str, Any]) -> None:
        if properties.get("provisioningState") not in {None, "Succeeded"}:
            raise RetryablePublicationError("APIM credential resource is not ready")
        if properties.get("secret") is not True:
            raise PolicyCompilationError("APIM credential resource is not marked secret")
        observed_secret = (properties.get("keyVault") or {}).get("secretIdentifier")
        if observed_secret != named_value.key_vault_secret_id:
            raise PolicyCompilationError("APIM credential resource has a different credential source")

    @staticmethod
    def _publication_tag(publication_id: str) -> str:
        normalized_id = re.sub(r"[^A-Za-z0-9._]", "_", publication_id)
        return f"turnstile_publication_{normalized_id}"

    def ensure_revision(self, revision: str, description: str) -> None:
        encoded_api = quote(self._settings.apim_api_id, safe="")
        encoded_revision = quote(revision, safe="")
        revision_path = f"/apis/{encoded_api};rev={encoded_revision}"
        observed = self._request("GET", revision_path, allow_not_found=True)
        if observed.status_code != 404:
            state = (observed.json().get("properties") or {}).get("provisioningState")
            if state not in {None, "Succeeded"}:
                raise RetryablePublicationError(f"APIM revision provisioning state is {state}")
            return
        current = self._request("GET", f"/apis/{encoded_api}").json()
        current_properties = current.get("properties") or {}
        properties = {
            "path": current_properties["path"],
            "sourceApiId": current["id"],
            "apiRevisionDescription": description,
        }
        if current_properties.get("serviceUrl") is not None:
            properties["serviceUrl"] = current_properties["serviceUrl"]
        self._request(
            "PUT",
            revision_path,
            json_body={"properties": properties},
        )

    def put_api_policy(self, revision: str, policy: str) -> None:
        self._put_policy(
            f"/apis/{quote(self._settings.apim_api_id, safe='')};rev={quote(revision, safe='')}/policies/policy",
            policy,
            revision=revision,
        )

    def ensure_operation(
        self, revision: str, operation: OperationResource, *, create_only: bool = False,
    ) -> None:
        path = self._revision_path(revision) + "/operations/" + quote(operation.id, safe="")
        observed = self._request("GET", path, allow_not_found=True)
        if create_only and observed.status_code != 404:
            raise PolicyCompilationError("New Operation already exists or cannot be verified")
        if observed.status_code == 404:
            if not create_only:
                raise InfrastructureUpgradeRequiredError(
                    f"APIM infrastructure upgrade required: operation {operation.id} is missing. "
                    "Run scripts.deploy plan-upgrade and upgrade before publishing image models."
                )
            self._request("PUT", path, json_body={"properties": {
                "displayName": operation.display_name, "method": operation.method,
                "urlTemplate": operation.path, "templateParameters": [],
            }}, extra_headers={"If-None-Match": "*"} if create_only else None)
            observed = self._request("GET", path)
        properties = observed.json().get("properties") or {}
        if (properties.get("method") != operation.method or properties.get("urlTemplate") != operation.path
                or properties.get("templateParameters", [])):
            raise PolicyCompilationError("Operation route does not match its compiled contract")

    def put_operation_policy(
        self, revision: str, operation: str, policy: str, *, create_only: bool = False,
    ) -> None:
        self._put_policy(
            f"/apis/{quote(self._settings.apim_api_id, safe='')};rev={quote(revision, safe='')}"
            f"/operations/{quote(operation, safe='')}/policies/policy",
            policy,
            revision=revision,
            operation=operation,
            create_only=create_only,
        )

    def _put_policy(
        self, path: str, policy: str, *, revision: str, operation: str | None = None,
        create_only: bool = False,
    ) -> None:
        encoded = ElementTree.tostring(parse_policy(policy), encoding="unicode")
        validate_policy_upload(
            encoded, gateway_profile_id=self._settings.apim_gateway_profile_id,
            api_id=self._settings.apim_api_id, api_revision=revision,
            table_endpoint=self._settings.ledger_table_endpoint,
            table_name=self._settings.ledger_table_name,
            operation_id=operation,
        )
        expected = component_digest(parse_policy(encoded), normalize_text_defaults=False)
        observed, actual = self._read_policy(path, allow_not_found=True)
        if create_only and observed.status_code != 404:
            raise PolicyCompilationError("New Operation policy already exists or cannot be verified")
        if observed.status_code == 404:
            headers = {"If-None-Match": "*"}
        else:
            etag = observed.headers.get("ETag", "").strip()
            if observed.status_code != 200 or not etag or etag == "*" or "\r" in etag or "\n" in etag:
                raise PolicyCompilationError("APIM policy requires a concrete ETag before update")
            if component_digest(parse_policy(actual), normalize_text_defaults=False) == expected:
                return
            headers = {"If-Match": etag}
        self._request(
            "PUT",
            path,
            json_body={"properties": {"format": "xml", "value": encoded}},
            extra_headers=headers,
        )
        readback, actual = self._read_policy(path)
        if readback.status_code != 200:
            raise PolicyCompilationError("APIM policy readback is unavailable")
        validate_policy_upload(
            actual, gateway_profile_id=self._settings.apim_gateway_profile_id,
            api_id=self._settings.apim_api_id, api_revision=revision,
            table_endpoint=self._settings.ledger_table_endpoint,
            table_name=self._settings.ledger_table_name,
            operation_id=operation,
        )
        if component_digest(parse_policy(actual), normalize_text_defaults=False) != expected:
            raise PolicyCompilationError("APIM policy readback differs from the validated artifact")

    @staticmethod
    def _policy_response_value(response: httpx.Response) -> str:
        try:
            if "json" in response.headers.get("content-type", "").casefold():
                value = response.json()["properties"]["value"]
            else:
                value = response.text
                if value.lstrip().startswith("&lt;"):
                    value = unescape(value)
            if not isinstance(value, str) or not value:
                raise ValueError
            parse_policy(value)
        except ElementTree.ParseError as error:
            raise PolicyCompilationError("APIM policy response has no valid XML value") from error
        except (KeyError, TypeError, ValueError):
            raise PolicyCompilationError("APIM policy response has no valid XML value") from None
        return value

    def _read_policy(
        self, path: str, *, allow_not_found: bool = False,
        before_request: Callable[[], None] | None = None, record_probe_request: bool = False,
    ) -> tuple[httpx.Response, str]:
        location = urlparse(path)
        for representation in ("rawxml", "xml"):
            query = {**dict(parse_qsl(location.query)), "format": representation}
            address = location._replace(query=urlencode(query)).geturl()
            if record_probe_request:
                self._record_probe_request("GET", address)
            if before_request is not None:
                before_request()
            response = self._request("GET", address, allow_not_found=allow_not_found)
            if response.status_code == 404:
                return response, ""
            try:
                return response, self._policy_response_value(response)
            except PolicyCompilationError as error:
                if representation == "rawxml" and isinstance(error.__cause__, ElementTree.ParseError):
                    continue
                raise
        raise PolicyCompilationError(f"APIM policy {path} returned no XML representation")

    def probe_revision(
        self, revision: str, publication: GatewayPublication, *, journal: ImageProbeJournal | None = None,
        baseline_publication: GatewayPublication | None = None,
    ) -> tuple[GatewayEndpointProbeResult, ...]:
        token = self._probe_journal.set(journal)
        verification_token = self._probe_verification.set(
            cast(ProbeVerificationJournal | None, getattr(journal, "verification", None)),
        )
        try:
            return self._probe_revision(
                revision, publication, baseline_publication=baseline_publication,
            )
        finally:
            self._probe_verification.reset(verification_token)
            self._probe_journal.reset(token)

    def _probe_disabled_model(
        self, base: str, headers: dict[str, str], binding: GatewayModelBinding,
    ) -> None:
        endpoint = {
            ApiFormat.OPENAI_CHAT: "/chat/completions",
            ApiFormat.ANTHROPIC_MESSAGES: "/v1/messages",
            ApiFormat.OPENAI_IMAGES: "/images/generations",
        }[binding.api_format]
        url = base + endpoint
        self._record_probe_request("POST", url)
        response = self._client.post(url, headers=headers, json={
            "model": binding.model.model_key,
        }, timeout=10)
        try:
            error = response.json()["error"]
            valid = response.status_code == 403 and error["code"] == "model_disabled"
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise PolicyCompilationError("Disabled model was not rejected before dispatch")

    def _probe_heartbeat(self) -> None:
        journal = self._probe_journal.get()
        if journal is not None:
            journal.heartbeat()

    def _record_probe_request(self, method: str, endpoint: str) -> None:
        journal = self._probe_journal.get()
        if journal is not None:
            journal.record_request(method, endpoint)

    def _probe_revision(
        self, revision: str, publication: GatewayPublication,
        *, baseline_publication: GatewayPublication | None = None,
    ) -> tuple[GatewayEndpointProbeResult, ...]:
        if baseline_publication is not None and (
            baseline_publication.id != publication.base_release_id
            or baseline_publication.gateway_profile_id != publication.gateway_profile_id
        ):
            raise PolicyCompilationError("Count verification baseline does not match the publication")
        if not self._settings.apim_probe_subscription_key:
            raise RuntimeError("APIM_PROBE_SUBSCRIPTION_KEY is required for verification")
        headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": (
                self._settings.apim_probe_subscription_key.get_secret_value()
            ),
            "x-request-id": "gateway-publication-probe",
            "x-org-id": "system-gateway-publication",
            "x-org-name": "Turnstile Control Plane",
            "x-department-id": "system-gateway-publication",
            "x-department-name": "Turnstile Control Plane",
            "x-project-id": "system-gateway-publication",
            "x-project-name": "Turnstile Control Plane",
            "x-agent-id": "system-gateway-publication",
            "x-agent-name": "Turnstile Publisher",
            "x-model-id": "gateway-publication-probe",
            "x-runtime-id": "gateway-publication-probe",
            "x-user-id": "system-gateway-publication",
            "x-user-name": "Turnstile Publisher",
            "x-request-source": "gateway-publication-probe",
            "x-hive-organization": "Turnstile Control Plane",
            "x-hive-department": "Turnstile Control Plane",
            "x-hive-project": "Turnstile Control Plane",
            "x-hive-agent": "Turnstile Publisher",
            "x-hive-user": "Turnstile Publisher",
            "x-hive-workflow": "gateway-publication-probe",
            "x-hive-run-id": str(publication.id),
            "x-hive-turn-index": "1",
            "x-hive-model": "gateway-publication-probe",
            "x-hive-runtime": "gateway-publication-probe",
        }
        deployments = publication.desired_spec.gateway_deployments
        if not deployments:
            self._probe_revision_endpoint(
                str(self._settings.apim_gateway_url).rstrip("/"),
                revision,
                publication,
                headers,
                native_gateway_id=None,
                baseline_publication=baseline_publication,
            )
            return ()
        results: list[GatewayEndpointProbeResult] = []
        for deployment in deployments:
            self._probe_heartbeat()
            required = deployment.release_gate.value == "required"
            try:
                self._probe_revision_endpoint(
                    str(deployment.endpoint_url).rstrip("/"),
                    revision,
                    publication,
                    headers,
                    native_gateway_id=deployment.native_gateway_id,
                    baseline_publication=baseline_publication,
                )
            except (ProbeRequestLimitExceeded, ProbeVerificationDeferred):
                raise
            except Exception as error:
                if required:
                    raise
                results.append(
                    GatewayEndpointProbeResult(
                        deployment_id=deployment.deployment_id,
                        native_gateway_id=deployment.native_gateway_id,
                        required=False,
                        status="optional_pending",
                        error_code=type(error).__name__,
                        error_message=str(error).replace("\n", " ")[:1000],
                    )
                )
            else:
                results.append(
                    GatewayEndpointProbeResult(
                        deployment_id=deployment.deployment_id,
                        native_gateway_id=deployment.native_gateway_id,
                        required=required,
                        status="succeeded",
                    )
                )
        return tuple(results)

    def _probe_revision_endpoint(
        self,
        base: str,
        revision: str,
        publication: GatewayPublication,
        headers: dict[str, str],
        *,
        native_gateway_id: str | None,
        baseline_publication: GatewayPublication | None = None,
    ) -> None:
        verification = self._probe_verification.get()
        checks = self._probe_revision_checks(
            base, revision, publication, headers, native_gateway_id=native_gateway_id,
            baseline_publication=baseline_publication,
        )
        for step_index, check in enumerate(checks):
            if verification is None:
                check()
            else:
                key = hashlib.sha256(json.dumps(
                    [base, revision, native_gateway_id, step_index], separators=(",", ":"),
                ).encode()).hexdigest()
                verification.run_step(key, check)

    def _probe_revision_checks(
        self,
        base: str,
        revision: str,
        publication: GatewayPublication,
        headers: dict[str, str],
        *,
        native_gateway_id: str | None,
        baseline_publication: GatewayPublication | None = None,
    ) -> Iterator[Callable[[], None]]:
        revision_path = f"{base};rev={quote(revision, safe='')}"
        base_revision = publication.resource_manifest.get("base_apim_revision")
        baseline_path = (
            f"{base};rev={quote(str(base_revision), safe='')}"
            if base_revision and str(base_revision) != revision
            else None
        )
        self._record_probe_request("GET", f"{revision_path}/v1/models")
        models = self._client.get(f"{revision_path}/v1/models", headers=headers)
        if self._is_transient_probe_status(models.status_code):
            raise RetryablePublicationError(
                f"Candidate discovery returned HTTP {models.status_code}"
            )
        models.raise_for_status()
        discovered = {str(item.get("id")) for item in (models.json().get("data") or [])}
        expected_models = self._discovery_models_for_gateway(
            publication,
            native_gateway_id,
        )
        availability_states = publication.resource_manifest.get("availability_probe_states")
        disabled_aliases: set[str] = set()
        if publication.resource_manifest.get("model_availability") is not None:
            if not isinstance(availability_states, dict) or any(
                type(availability_states.get(item.id)) is not bool
                for item in publication.desired_spec.discovery_models
            ):
                raise PolicyCompilationError("Publication availability state is unverified")
            disabled_aliases = {item.id for item in expected_models
                                if availability_states[item.id] is False}
            expected_models = [item for item in expected_models if item.id not in disabled_aliases]
        expected = {item.id for item in expected_models}
        if not expected.issubset(discovered):
            raise RetryablePublicationError(
                "Candidate revision model discovery is incomplete"
            )
        out_of_scope = {
            item.id for item in publication.desired_spec.discovery_models
        } - expected
        if discovered & out_of_scope:
            raise RetryablePublicationError(
                "Candidate revision advertises a model outside this Gateway scope"
            )
        removed = publication.desired_spec.removed_models
        removed_aliases = {item.model_key for item in removed}
        target_binding = (
            publication.desired_spec.bindings[-1]
            if publication.publication_kind
            in {PublicationKind.MODEL_ADD, PublicationKind.CREDENTIAL_ROTATION}
            else None
        )
        if discovered & removed_aliases:
            raise RetryablePublicationError(
                "Candidate revision still advertises the removed model"
            )

        regression_model_key = (self._settings.apim_regression_model_key or "").casefold()
        regression = next(
            (
                item
                for item in expected_models
                if item.api_format is not ApiFormat.OPENAI_IMAGES
                if item.id.casefold() == regression_model_key
            ),
            next((item for item in expected_models
                if item.api_format is not ApiFormat.OPENAI_IMAGES), None),
        )
        latest_binding = (
            publication.desired_spec.bindings[-1]
            if publication.publication_kind is not PublicationKind.MODEL_REMOVE
            and publication.desired_spec.bindings
            else None
        )
        latest_is_regression = (
            regression is not None
            and latest_binding is not None
            and latest_binding.model.model_key.casefold() == regression.id.casefold()
            and self._binding_serves_gateway(latest_binding, native_gateway_id)
        )
        if regression is not None and not latest_is_regression:
            regression_binding = next(
                (
                    binding
                    for binding in publication.desired_spec.bindings
                    if binding.model.model_key.casefold() == regression.id.casefold()
                ),
                None,
            )
            if (
                regression_binding is None
                or regression_binding.runtime_config.get("apim_backend_pool") is None
            ):
                yield partial(self._probe_model,
                    revision_path,
                    self._model_probe_headers(publication, headers, regression.id),
                    regression.id,
                    api_format=regression.api_format,
                    max_tokens_field=str(
                        regression_binding.runtime_config.get(
                            "max_tokens_field", "max_completion_tokens"
                        )
                        if regression_binding is not None
                        else "max_completion_tokens"
                    ),
                    baseline_base=(
                        baseline_path
                        if target_binding is None
                        or regression_binding is not target_binding
                        else None
                    ),
                    authorization_required=(
                        regression_binding is not None
                        and regression_binding.auth_strategy
                        is AuthStrategy.MANAGED_IDENTITY
                        and isinstance(
                            regression_binding.runtime_config.get("authorization"),
                            dict,
                        )
                    ),
                )
        if publication.publication_kind is PublicationKind.MODEL_REMOVE:
            target = removed[0]
            removed_headers = self._model_probe_headers(publication, headers, target.model_key)
            yield partial(self._probe_removed_model,
                revision_path,
                removed_headers,
                target.model_key,
                target.api_format,
            )
            if target.api_format is ApiFormat.ANTHROPIC_MESSAGES:
                yield partial(self._probe_count_tokens_model,
                    revision_path,
                    removed_headers,
                    target.model_key,
                    expected="unpublished",
                )
        else:
            binding = publication.desired_spec.bindings[-1]
            binding_headers = self._model_probe_headers(publication, headers, binding.model.model_key)
            binding_served = self._binding_serves_gateway(
                binding,
                native_gateway_id,
            )
            if binding.model.model_key in disabled_aliases:
                yield partial(self._probe_disabled_model, revision_path, binding_headers, binding)
            elif not binding_served:
                yield partial(self._probe_removed_model,
                    revision_path,
                    binding_headers,
                    binding.model.model_key,
                    binding.api_format,
                )
            elif (
                binding.runtime_config.get("apim_backend_pool") is None
                and binding.api_format is not ApiFormat.OPENAI_IMAGES
            ):
                yield partial(self._probe_model,
                    revision_path,
                    binding_headers,
                    binding.model.model_key,
                    api_format=binding.api_format,
                    max_tokens_field=str(
                        binding.runtime_config.get(
                            "max_tokens_field", "max_completion_tokens"
                        )
                    ),
                    retry_assignment_denial=binding is target_binding,
                    baseline_base=(
                        baseline_path if binding is not target_binding else None
                    ),
                    authorization_required=(
                        binding.auth_strategy in {
                            AuthStrategy.MANAGED_IDENTITY, AuthStrategy.OAUTH_CLIENT_CREDENTIALS,
                        }
                        and isinstance(
                            binding.runtime_config.get("authorization"), dict
                        )
                    ),
                )
            responses_bindings = [
                item
                for item in publication.desired_spec.bindings
                if ApimPolicyCompiler._responses_backend_path(item) is not None
                and self._binding_serves_gateway(item, native_gateway_id)
                and item.model.model_key not in disabled_aliases
            ]
            for responses_binding in responses_bindings:
                if (
                    responses_binding.runtime_config.get("apim_backend_pool")
                    is not None
                ):
                    continue
                responses_headers = self._model_probe_headers(
                    publication, headers, responses_binding.model.model_key,
                )
                yield partial(self._probe_responses_model,
                    revision_path,
                    responses_headers,
                    responses_binding.model.model_key,
                    retry_assignment_denial=responses_binding is target_binding,
                    baseline_base=(
                        baseline_path
                        if responses_binding is not target_binding
                        else None
                    ),
                    authorization_required=(
                        responses_binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY
                        and isinstance(
                            responses_binding.runtime_config.get("authorization"), dict
                        )
                    ),
                )
                yield partial(self._probe_responses_compact_model,
                    revision_path,
                    responses_headers,
                    responses_binding.model.model_key,
                    retry_assignment_denial=responses_binding is target_binding,
                    baseline_base=(
                        baseline_path
                        if responses_binding is not target_binding
                        else None
                    ),
                    authorization_required=(
                        responses_binding.auth_strategy
                        is AuthStrategy.MANAGED_IDENTITY
                        and isinstance(
                            responses_binding.runtime_config.get("authorization"),
                            dict,
                        )
                    ),
                )
            pool_compiler = ApimPolicyCompiler(
                self._settings.apim_probe_subscription_id,
                self._settings.apim_usage_observer_url,
                self._settings.apim_usage_observer_key_named_value,
            )
            baseline_pool_ids: dict[ApiFormat, set[str]] = {}
            for count_binding in publication.desired_spec.bindings:
                if count_binding.model.model_key in disabled_aliases:
                    continue
                if count_binding.api_format is not ApiFormat.ANTHROPIC_MESSAGES:
                    continue
                serves_gateway = self._binding_serves_gateway(
                    count_binding,
                    native_gateway_id,
                )
                count_operation = count_binding.count_tokens_operation
                if (
                    serves_gateway
                    and count_operation.support == "supported"
                    and count_binding.backend_pool is not None
                ):
                    continue
                yield partial(self._probe_count_tokens_model,
                    revision_path,
                    self._model_probe_headers(publication, headers, count_binding.model.model_key),
                    count_binding.model.model_key,
                    expected=(
                        "unpublished"
                        if not serves_gateway
                        else "available"
                        if count_operation.support == "supported"
                        else "unavailable"
                    ),
                    retry_assignment_denial=count_binding is binding,
                    baseline_base=baseline_path,
                    baseline_revision=str(base_revision) if baseline_path else None,
                    baseline_publication=self._unchanged_count_baseline(
                        baseline_publication, count_binding, target_binding,
                    ),
                    authorization_required=(
                        count_binding.auth_strategy
                        is AuthStrategy.MANAGED_IDENTITY
                        and isinstance(
                            count_binding.runtime_config.get("authorization"),
                            dict,
                        )
                    ),
                )
            for pooled_binding in publication.desired_spec.bindings:
                if pooled_binding.model.model_key in disabled_aliases:
                    continue
                if not self._binding_serves_gateway(
                    pooled_binding,
                    native_gateway_id,
                ):
                    continue
                pool = pooled_binding.backend_pool
                if pool is None:
                    continue
                if (
                    baseline_path is not None
                    and publication.base_release_id is not None
                    and pooled_binding.api_format not in baseline_pool_ids
                ):
                    baseline_pool_ids[pooled_binding.api_format] = (
                        self._pool_baseline_backend_ids(
                            str(base_revision), pooled_binding.api_format
                        )
                    )
                supports_responses = (
                    ApimPolicyCompiler._responses_backend_path(pooled_binding)
                    is not None
                )
                authorization_required = (
                    pooled_binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY
                    and isinstance(
                        pooled_binding.runtime_config.get("authorization"), dict
                    )
                )
                for member in pool.members:
                    member_headers = {
                        **self._model_probe_headers(publication, headers, pooled_binding.model.model_key),
                        "x-turnstile-pool-member": (
                            pool_compiler.pool_member_backend_id(
                                publication,
                                pooled_binding,
                                member,
                            )
                        ),
                    }
                    baseline_member_headers = None
                    if baseline_path is not None and publication.base_release_id is not None:
                        baseline_selector_publication = publication.model_copy(
                            update={"id": publication.base_release_id}
                        )
                        affinity_binding = pooled_binding.model_copy(update={
                            "runtime_config": {
                                **pooled_binding.runtime_config,
                                "apim_backend_pool_session_affinity": True,
                            },
                        })
                        baseline_member_id = next(
                            (
                                candidate
                                for candidate in (
                                    pool_compiler.pool_member_backend_id(
                                        baseline_selector_publication, affinity_binding, member
                                    ),
                                    ApimPolicyCompiler._pool_member_backend_id(
                                        baseline_selector_publication, pooled_binding, member
                                    ),
                                )
                                if candidate in baseline_pool_ids[pooled_binding.api_format]
                            ),
                            None,
                        )
                        if baseline_member_id is not None:
                            baseline_member_headers = {
                                **self._model_probe_headers(publication, headers, pooled_binding.model.model_key),
                                "x-turnstile-pool-member": baseline_member_id,
                            }
                    member_baseline_path = (
                        baseline_path if baseline_member_headers is not None else None
                    )
                    yield partial(self._probe_model,
                        revision_path,
                        member_headers,
                        pooled_binding.model.model_key,
                        api_format=pooled_binding.api_format,
                        max_tokens_field=str(
                            pooled_binding.runtime_config.get(
                                "max_tokens_field", "max_completion_tokens"
                            )
                        ),
                        baseline_base=(
                            member_baseline_path
                            if pooled_binding is not target_binding
                            else None
                        ),
                        baseline_headers=baseline_member_headers,
                        authorization_required=authorization_required,
                    )
                    if pooled_binding.count_tokens_operation.support == "supported":
                        yield partial(self._probe_count_tokens_model,
                            revision_path,
                            member_headers,
                            pooled_binding.model.model_key,
                            expected="available",
                            retry_assignment_denial=pooled_binding is target_binding,
                            baseline_base=member_baseline_path,
                            baseline_headers=baseline_member_headers,
                            baseline_revision=str(base_revision) if member_baseline_path else None,
                            baseline_publication=self._unchanged_count_baseline(
                                baseline_publication, pooled_binding, target_binding,
                            ),
                            authorization_required=authorization_required,
                        )
                    if not supports_responses:
                        continue
                    yield partial(self._probe_responses_model,
                        revision_path,
                        member_headers,
                        pooled_binding.model.model_key,
                        baseline_base=(
                            member_baseline_path
                            if pooled_binding is not target_binding
                            else None
                        ),
                        baseline_headers=baseline_member_headers,
                        authorization_required=authorization_required,
                    )
                    yield partial(self._probe_responses_compact_model,
                        revision_path,
                        member_headers,
                        pooled_binding.model.model_key,
                        baseline_base=(
                            member_baseline_path
                            if pooled_binding is not target_binding
                            else None
                        ),
                        baseline_headers=baseline_member_headers,
                        authorization_required=authorization_required,
                    )
            for image_binding in publication.desired_spec.bindings:
                if (
                    image_binding.api_format is ApiFormat.OPENAI_IMAGES
                    and self._binding_serves_gateway(image_binding, native_gateway_id)
                ):
                    from ..domain.image_profiles import validate_image_profile

                    profile = validate_image_profile(image_binding.model.image_profile)
                    journal = self._probe_journal.get()
                    if journal is None:
                        raise PolicyCompilationError("Image verification requires a persistent probe journal")
                    body = self._image_probe_body(image_binding.model.model_key, profile)
                    yield partial(journal.run,
                        image_binding, revision, body, partial(
                            self._send_bound_image_probe, revision_path,
                            self._model_probe_headers(publication, headers, image_binding.model.model_key),
                            image_binding, profile,
                        ),
                        gateway_endpoint=revision_path if native_gateway_id is not None else None,
                    )

    @staticmethod
    def _model_probe_headers(
        publication: GatewayPublication, headers: dict[str, str], model_key: str,
    ) -> dict[str, str]:
        binding = next((item for item in publication.desired_spec.bindings
                        if item.model.model_key.casefold() == model_key.casefold()), None)
        removed = next((item for item in publication.desired_spec.removed_models
                        if item.model_key.casefold() == model_key.casefold()), None)
        model_id = str(binding.logical_model_id) if binding and binding.logical_model_id else model_key
        if removed is not None:
            model_id = str(removed.model_id)
        elif (binding is not None and binding.logical_model_id is None
              and publication.publication_kind is PublicationKind.MODEL_ADD
              and binding is publication.desired_spec.bindings[-1]):
            model_id = str(publication_model_id(publication.id, model_key))
        model_headers = {
            **headers, "x-model-id": model_id, "x-hive-model": model_key,
            "x-hive-run-id": str(publication.id),
        }
        model_headers.pop("x-hive-runtime", None)
        model_headers.pop("x-runtime-id", None)
        if binding is not None and binding.runtime_id is not None:
            model_headers["x-runtime-id"] = str(binding.runtime_id)
        return model_headers

    def _send_bound_image_probe(
        self, base: str, headers: dict[str, str], binding: GatewayModelBinding,
        profile: ImageGenerationProfile, request_id: str, model_id: str,
    ) -> dict[str, Any]:
        return self._probe_image_model(
            base, {**headers, "x-request-id": request_id, "x-model-id": model_id,
                   "x-hive-model": model_id}, binding.model.model_key,
            authorization_required=binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY,
            profile=profile,
        )

    @staticmethod
    def _binding_serves_gateway(
        binding: GatewayModelBinding,
        native_gateway_id: str | None,
    ) -> bool:
        if native_gateway_id is None or binding.deployment_scope.mode.value == "all":
            return True
        return any(
            deployment.native_gateway_id.casefold() == native_gateway_id.casefold()
            for deployment in binding.serving_deployments
        )

    @classmethod
    def _discovery_models_for_gateway(
        cls,
        publication: GatewayPublication,
        native_gateway_id: str | None,
    ) -> list[DiscoveryModel]:
        binding_by_alias = {
            binding.model.model_key.casefold(): binding
            for binding in publication.desired_spec.bindings
        }
        return [
            model
            for model in publication.desired_spec.discovery_models
            if (
                (binding := binding_by_alias.get(model.id.casefold())) is None
                or cls._binding_serves_gateway(binding, native_gateway_id)
            )
        ]

    def _verification_timeout(self, maximum: float) -> float:
        verification = self._probe_verification.get()
        return verification.request_timeout(maximum) if verification is not None else maximum

    def _post_text_probe(
        self, url: str, headers: dict[str, str], body: dict[str, Any],
    ) -> httpx.Response:
        for attempt in range(self._CANDIDATE_TRANSIENT_ATTEMPTS):
            self._record_probe_request("POST", url)
            response = self._client.post(
                url, headers=headers, json=body,
                timeout=self._verification_timeout(self._CANDIDATE_PROBE_TIMEOUT_SECONDS),
            )
            if (
                response.status_code not in {500, 502, 503, 504}
                or response.headers.get("Retry-After") is not None
                or attempt + 1 == self._CANDIDATE_TRANSIENT_ATTEMPTS
            ):
                return response
        raise RuntimeError("Text probe attempt limit must be positive")

    def _probe_model(
        self,
        base: str,
        headers: dict[str, str],
        model: str,
        *,
        api_format: ApiFormat = ApiFormat.ANTHROPIC_MESSAGES,
        max_tokens_field: str = "max_completion_tokens",
        retry_assignment_denial: bool = False,
        baseline_base: str | None = None,
        baseline_headers: dict[str, str] | None = None,
        authorization_required: bool = False,
    ) -> None:
        if api_format is ApiFormat.OPENAI_IMAGES:
            raise PolicyCompilationError("Image probes require an explicit profile and persistent journal")
        path = "/v1/messages"
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": "Reply only with OK."}],
        }
        if api_format is ApiFormat.OPENAI_CHAT:
            path = "/chat/completions"
            if max_tokens_field not in {"max_tokens", "max_completion_tokens"}:
                raise RuntimeError("Candidate model probe uses an unsupported token field")
            body[max_tokens_field] = 8
        else:
            body["max_tokens"] = 8
        response = self._post_text_probe(f"{base}{path}", headers, body)
        if self._matches_baseline_failure(
            response,
            baseline_url=f"{baseline_base}{path}" if baseline_base else None,
            headers=baseline_headers or headers,
            body=body,
            label="model",
            model=model,
        ):
            return
        if self._is_transient_probe_status(response.status_code):
            raise RetryablePublicationError(
                f"Candidate model probe returned HTTP {response.status_code}: {model}"
            )
        if (
            retry_assignment_denial
            and response.status_code == 403
            and self._is_model_assignment_denial(response)
        ):
            raise RetryablePublicationError(
                f"Candidate model assignment policy has not propagated: {model}"
            )
        if authorization_required and response.status_code in {401, 403}:
            raise AuthorizationRequiredError(
                "The provider has not authorized the APIM managed identity"
            )
        response.raise_for_status()
        payload = response.json()
        response_field = "choices" if api_format is ApiFormat.OPENAI_CHAT else "content"
        if not isinstance(payload.get(response_field), list):
            raise RuntimeError(f"Candidate model probe returned an invalid response: {model}")

    def _probe_image_model(
        self, base: str, headers: dict[str, str], model: str, *,
        authorization_required: bool = False,
        profile: ImageGenerationProfile,
    ) -> dict[str, Any]:
        from .image_generation import generated_image, image_usage

        profile = validate_image_profile(profile)
        body = self._image_probe_body(model, profile)
        evidence: dict[str, Any] = {"image_validated": False}
        self._record_probe_request("POST", base + profile.public_path)
        try:
            with self._client.stream(
                "POST", base + profile.public_path, headers=headers, json=body,
                timeout=self._verification_timeout(profile.timeout_seconds),
            ) as response:
                evidence.update(status_code=response.status_code,
                                correlation_id=response.headers.get("x-correlation-id"))
                if authorization_required and response.status_code in {401, 403}:
                    raise AuthorizationRequiredError("The image provider has not authorized APIM")
                if response.status_code != 200:
                    raise PolicyCompilationError(
                        f"Image probe failed with HTTP {response.status_code}; it was not retried"
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > profile.max_response_bytes:
                        raise ValueError("Image probe exceeded its response limit")
                    content.extend(chunk)
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("Image probe response must be an object")
            usage = image_usage(payload)
            if usage is None:
                raise ValueError("Image probe did not return exact modality usage")
            evidence.update(total_tokens=usage.input_tokens + usage.cached_tokens + usage.output_tokens,
                            usage=usage.model_dump(mode="json"))
            generated_image(payload)
            evidence["image_validated"] = True
        except (AuthorizationRequiredError, httpx.HTTPError, ValueError, TypeError) as error:
            outcome = BillableRequestOutcome(
                actual_tokens=evidence.get("total_tokens"),
                correlation_id=evidence.get("correlation_id"),
                evidence=evidence,
            )
            if isinstance(error, AuthorizationRequiredError):
                raise AuthorizationRequiredError(
                    "The image provider has not authorized APIM", billable_outcome=outcome,
                ) from error
            raise PolicyCompilationError(
                "Image probe did not return a complete measured image; no automatic retry",
                billable_outcome=outcome,
            ) from error
        return evidence

    @staticmethod
    def _image_probe_body(model: str, profile: ImageGenerationProfile) -> dict[str, Any]:
        validate_image_profile(profile)
        return {"model": model, "prompt": "A blue ceramic cup on a white table.",
            "n": 1, "stream": False}

    def _probe_responses_model(
        self,
        base: str,
        headers: dict[str, str],
        model: str,
        *,
        retry_assignment_denial: bool = False,
        baseline_base: str | None = None,
        baseline_headers: dict[str, str] | None = None,
        authorization_required: bool = False,
    ) -> None:
        body = {
            "model": model,
            "input": "Reply only with OK.",
            "max_output_tokens": 16,
            "store": False,
            "stream": False,
        }
        response = self._post_text_probe(f"{base}/responses", headers, body)
        if self._matches_baseline_failure(
            response,
            baseline_url=f"{baseline_base}/responses" if baseline_base else None,
            headers=baseline_headers or headers,
            body=body,
            label="Responses",
            model=model,
        ):
            return
        if self._is_transient_probe_status(response.status_code):
            raise RetryablePublicationError(
                f"Candidate Responses probe returned HTTP {response.status_code}: {model}"
            )
        if (
            retry_assignment_denial
            and response.status_code == 403
            and self._is_model_assignment_denial(response)
        ):
            raise RetryablePublicationError(
                f"Candidate Responses model assignment policy has not propagated: {model}"
            )
        if authorization_required and response.status_code in {401, 403}:
            raise AuthorizationRequiredError(
                "The provider has not authorized the APIM managed identity"
            )
        response.raise_for_status()
        if not isinstance(response.json().get("output"), list):
            raise RuntimeError(
                f"Candidate Responses probe returned an invalid response: {model}"
            )

    @staticmethod
    def _unchanged_count_baseline(
        baseline: GatewayPublication | None,
        binding: GatewayModelBinding,
        target: GatewayModelBinding | None,
    ) -> GatewayPublication | None:
        if baseline is None or binding is target:
            return None
        return baseline if any(previous == binding for previous in baseline.desired_spec.bindings) else None

    def _count_baseline_policy_matches(
        self, baseline: GatewayPublication, revision: str, headers: dict[str, str],
    ) -> bool:
        compiler = ApimPolicyCompiler(
            self._settings.apim_probe_subscription_id,
            self._settings.apim_usage_observer_url,
            self._settings.apim_usage_observer_key_named_value,
        )
        expected = parse_policy(compiler.compile(baseline).count_tokens_policy)
        selector = headers.get("x-turnstile-pool-member")
        if selector is not None and selector not in {
            node.get("backend-id") for node in expected.iter("set-backend-service")
        }:
            return False
        observed = self._policy_value(
            f"{self._revision_path(revision)}/operations/"
            f"{quote(self._settings.apim_count_tokens_operation_id, safe='')}/policies/policy",
            record_probe_request=True,
        )
        return observed is not None and component_digest(
            parse_policy(observed), normalize_text_defaults=False,
        ) == component_digest(expected, normalize_text_defaults=False)

    def _probe_count_tokens_model(
        self,
        base: str,
        headers: dict[str, str],
        model: str,
        *,
        expected: Literal["available", "unavailable", "unpublished"],
        retry_assignment_denial: bool = False,
        baseline_base: str | None = None,
        baseline_revision: str | None = None,
        baseline_publication: GatewayPublication | None = None,
        baseline_headers: dict[str, str] | None = None,
        authorization_required: bool = False,
    ) -> None:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply only with OK."}],
        }
        response = self._post_text_probe(
            f"{base}/v1/messages/count_tokens", headers, body,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        rejection = COUNT_TOKENS_REJECTIONS.get(expected)
        if (
            rejection is not None
            and response.headers.get("Content-Type", "").partition(";")[0].strip().casefold()
            == "application/json"
            and rejection.matches(response.status_code, payload)
        ):
            return
        if self._is_transient_probe_status(response.status_code):
            raise RetryablePublicationError(
                f"Candidate count-tokens probe returned HTTP "
                f"{response.status_code}: {model}"
            )
        if expected == "unpublished":
            raise RuntimeError(
                f"Candidate count-tokens probe did not reject an unpublished model: {model}"
            )
        if expected == "unavailable":
            raise RuntimeError(
                f"Candidate count-tokens probe did not preserve unavailable status: {model}"
            )
        if (
            baseline_base is not None
            and baseline_revision is not None
            and baseline_publication is not None
            and 400 <= response.status_code < 500
            and self._structured_error_identity(response) is not None
            and self._count_baseline_policy_matches(
                baseline_publication, baseline_revision, baseline_headers or headers,
            )
            and self._matches_baseline_failure(
                response,
                baseline_url=f"{baseline_base}/v1/messages/count_tokens",
                headers=baseline_headers or headers,
                body=body, label="count-tokens", model=model,
                require_matching_error=True,
            )
        ):
            return
        if (
            retry_assignment_denial
            and response.status_code == 403
            and self._is_model_assignment_denial(response)
        ):
            raise RetryablePublicationError(
                f"Candidate count-tokens assignment policy has not propagated: {model}"
            )
        if authorization_required and response.status_code in {401, 403}:
            raise AuthorizationRequiredError(
                "The provider has not authorized the APIM managed identity"
            )
        response.raise_for_status()
        input_tokens = payload.get("input_tokens") if isinstance(payload, dict) else None
        if (
            response.status_code != 200
            or type(input_tokens) is not int or input_tokens < 0 or input_tokens > 2**53 - 1
        ):
            raise RuntimeError(
                f"Candidate count-tokens probe returned an invalid response: {model}"
            )

    def _probe_responses_compact_model(
        self,
        base: str,
        headers: dict[str, str],
        model: str,
        *,
        retry_assignment_denial: bool = False,
        baseline_base: str | None = None,
        baseline_headers: dict[str, str] | None = None,
        authorization_required: bool = False,
    ) -> None:
        body = {
            "model": model,
            "input": "Reply only with OK.",
        }
        response = self._post_text_probe(f"{base}/responses/compact", headers, body)
        if self._matches_baseline_failure(
            response,
            baseline_url=(
                f"{baseline_base}/responses/compact" if baseline_base else None
            ),
            headers=baseline_headers or headers,
            body=body,
            label="Responses compact",
            model=model,
        ):
            return
        if self._is_transient_probe_status(response.status_code):
            raise RetryablePublicationError(
                f"Candidate Responses compact probe returned HTTP "
                f"{response.status_code}: {model}"
            )
        if (
            retry_assignment_denial
            and response.status_code == 403
            and self._is_model_assignment_denial(response)
        ):
            raise RetryablePublicationError(
                f"Candidate Responses compact model assignment policy has not "
                f"propagated: {model}"
            )
        if authorization_required and response.status_code in {401, 403}:
            raise AuthorizationRequiredError(
                "The provider has not authorized the APIM managed identity"
            )
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage")
        if (
            payload.get("object") != "response.compaction"
            or not isinstance(payload.get("output"), list)
            or not isinstance(usage, dict)
            or not all(
                isinstance(usage.get(field), int)
                for field in ("input_tokens", "output_tokens", "total_tokens")
            )
        ):
            raise RuntimeError(
                f"Candidate Responses compact probe returned an invalid response: {model}"
            )

    @staticmethod
    def _structured_error_identity(response: httpx.Response) -> tuple[str | None, str | None] | None:
        if response.headers.get("Content-Type", "").partition(";")[0].strip().casefold() != "application/json":
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None
        kind, code = error.get("type"), error.get("code")
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (kind, code)):
            return None
        if kind is None and code is None:
            return None
        return kind, code

    def _matches_baseline_failure(
        self,
        response: httpx.Response,
        *,
        baseline_url: str | None,
        headers: dict[str, str],
        body: dict[str, Any],
        label: str,
        model: str,
        require_matching_error: bool = False,
    ) -> bool:
        if (
            baseline_url is None
            or response.status_code < 400
            or response.status_code >= 500
            or self._is_transient_probe_status(response.status_code)
        ):
            return False
        self._record_probe_request("POST", baseline_url)
        baseline = self._client.post(
            baseline_url,
            headers=headers,
            json=body,
            timeout=self._CANDIDATE_PROBE_TIMEOUT_SECONDS,
        )
        if self._is_transient_probe_status(baseline.status_code):
            raise RetryablePublicationError(
                f"Current {label} baseline returned HTTP {baseline.status_code}: {model}"
            )
        if baseline.status_code == response.status_code:
            if require_matching_error and (
                self._structured_error_identity(response) is None
                or self._structured_error_identity(response) != self._structured_error_identity(baseline)
            ):
                raise RetryablePublicationError(
                    f"Current and candidate {label} error contracts differ: {model}"
                )
            return True
        if baseline.status_code < 400:
            raise RetryablePublicationError(
                f"Candidate {label} regressed from HTTP {baseline.status_code} "
                f"to {response.status_code}: {model}"
            )
        raise RetryablePublicationError(
            f"Current and candidate {label} failures differ "
            f"({baseline.status_code} vs {response.status_code}): {model}"
        )

    def _probe_removed_model(
        self,
        base: str,
        headers: dict[str, str],
        model: str,
        api_format: ApiFormat,
    ) -> None:
        path = (
            "/chat/completions"
            if api_format is ApiFormat.OPENAI_CHAT
            else "/images/generations"
            if api_format is ApiFormat.OPENAI_IMAGES
            else "/v1/messages"
        )
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": "Reply only with OK."}],
        }
        if api_format is ApiFormat.OPENAI_IMAGES:
            body = {
                "model": model, "prompt": "A blue cup.", "size": "1024x1024",
                "quality": "low", "n": 1, "output_format": "png", "stream": False,
            }
        elif api_format is ApiFormat.OPENAI_CHAT:
            body["max_completion_tokens"] = 8
        else:
            body["max_tokens"] = 8
        self._record_probe_request("POST", f"{base}{path}")
        response = self._client.post(
            f"{base}{path}",
            headers=headers,
            json=body,
            timeout=self._CANDIDATE_PROBE_TIMEOUT_SECONDS,
        )
        if api_format is ApiFormat.OPENAI_IMAGES:
            if self._is_image_model_not_published(response):
                return
        elif response.status_code == 400 and "not published" in response.text.casefold():
            if api_format is ApiFormat.OPENAI_CHAT:
                probes = (
                    (
                        "Responses",
                        "/responses",
                        {
                            "model": model,
                            "input": "Reply only with OK.",
                            "max_output_tokens": 16,
                            "store": False,
                            "stream": False,
                        },
                    ),
                    (
                        "Responses compact",
                        "/responses/compact",
                        {"model": model, "input": "Reply only with OK."},
                    ),
                )
                for probe_name, probe_path, probe_body in probes:
                    self._record_probe_request("POST", f"{base}{probe_path}")
                    probe = self._client.post(
                        f"{base}{probe_path}",
                        headers=headers,
                        json=probe_body,
                        timeout=self._CANDIDATE_PROBE_TIMEOUT_SECONDS,
                    )
                    if probe.status_code == 400 and any(
                        marker in probe.text.casefold()
                        for marker in ("not published", "does not support")
                    ):
                        continue
                    if self._is_transient_probe_status(probe.status_code):
                        raise RetryablePublicationError(
                            f"Candidate {probe_name} removal probe returned HTTP "
                            f"{probe.status_code}: {model}"
                        )
                    raise RuntimeError(
                        f"Candidate {probe_name} revision still accepts the removed "
                        f"model: {model} (HTTP {probe.status_code})"
                    )
            return
        if self._is_transient_probe_status(response.status_code):
            raise RetryablePublicationError(
                f"Candidate removal probe returned HTTP {response.status_code}: {model}"
            )
        raise RuntimeError(
            f"Candidate revision still accepts the removed model: {model} "
            f"(HTTP {response.status_code})"
        )

    @staticmethod
    def _is_transient_probe_status(status_code: int) -> bool:
        return status_code in {404, 408, 409, 425, 429} or status_code >= 500

    @staticmethod
    def _is_image_model_not_published(response: httpx.Response) -> bool:
        if response.status_code != 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("code")
        return isinstance(error, str) and error == "image_model_not_published"

    @staticmethod
    def _is_model_assignment_denial(response: httpx.Response) -> bool:
        try:
            error = response.json().get("error")
        except (json.JSONDecodeError, TypeError, AttributeError):
            return False
        if isinstance(error, str):
            return error == "model_not_assigned"
        if isinstance(error, dict):
            return "model_not_assigned" in (error.get("code"), error.get("type"))
        return False

    def promote_revision(self, revision: str, release_name: str) -> None:
        self._request(
            "PUT",
            f"/apis/{quote(self._settings.apim_api_id, safe='')}/releases/"
            f"{quote(release_name, safe='')}",
            json_body={
                "properties": {
                    "apiId": (f"{self._base}/apis/{self._settings.apim_api_id};rev={revision}"),
                    "notes": f"Turnstile gateway publication {release_name}",
                }
            },
            extra_headers={"If-Match": "*"},
        )

    def promote_fixed_revision(
        self, revision: str, release_name: str, expected_current: str,
    ) -> None:
        path = f"/apis/{quote(self._settings.apim_api_id, safe='')}"
        before = self._request("GET", path)
        etag = before.headers.get("ETag")
        if (before.json().get("properties", {}).get("apiRevision") != expected_current
                or not etag or etag.strip() == "*" or any(value in etag for value in "\r\n")):
            raise PolicyCompilationError("Fixed program promotion current revision changed")
        release_path = path + "/releases/" + quote(release_name, safe="")
        if self._request("GET", release_path, allow_not_found=True).status_code != 404:
            raise PolicyCompilationError("Fixed program promotion release already exists")
        current = self._request("GET", path)
        if (current.headers.get("ETag") != etag
                or current.json().get("properties", {}).get("apiRevision") != expected_current):
            raise PolicyCompilationError("Fixed program promotion current revision changed")
        self._request(
            "PUT", release_path,
            json_body={"properties": {
                "apiId": f"{self._base}/apis/{self._settings.apim_api_id};rev={revision}",
                "notes": "Turnstile fixed program migration " + release_name,
            }},
            extra_headers={"If-None-Match": "*"},
        )

    def current_revision(self) -> str | None:
        payload = self._request("GET", f"/apis/{quote(self._settings.apim_api_id, safe='')}").json()
        value = (payload.get("properties") or {}).get("apiRevision")
        return str(value) if value is not None else None

    def current_api_policy(self) -> tuple[str, str]:
        revision = self.current_revision()
        if not revision:
            raise RuntimeError("APIM did not report a current revision")
        return revision, self.revision_api_policy(revision)

    def revision_api_policy(self, revision: str) -> str:
        value = self._audit_parent_policy(
            self._revision_path(revision) + "/policies/policy", lambda: None,
        )
        if value is None:
            raise PolicyCompilationError("The APIM revision parent policy is missing")
        return value

    def route_credential_value(self, named_value_id: str) -> SecretStr:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", named_value_id):
            raise PolicyCompilationError("Route credential source identity is invalid")
        path = "/namedValues/" + quote(named_value_id, safe="")
        observed = self._request("GET", path)
        if observed.status_code != 200:
            raise PolicyCompilationError("Route credential source read was rejected")
        properties = observed.json().get("properties")
        if not isinstance(properties, dict):
            raise PolicyCompilationError("Route credential source is invalid")
        self._validate_named_value(NamedValueResource(named_value_id, None), properties)
        response = self._request("POST", path + "/listValue")
        if response.status_code != 200:
            raise PolicyCompilationError("Route credential value read was rejected")
        try:
            value = response.json()["value"]
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 25600:
                raise ValueError("invalid credential")
        except (ValueError, KeyError, TypeError):
            raise PolicyCompilationError("Route credential value is unavailable") from None
        return SecretStr(value)

    def prepare_route_oauth(
        self, credential: OAuthCredentialResource, checkpoint: Callable[[dict[str, Any]], None],
    ) -> None:
        from ..domain.runtime_models import OAuthClientCredentialsConfig
        from ..domain.storage_routes import canonical_document, text_digest

        if not self._settings.databricks_oauth_enabled:
            raise PolicyCompilationError("Fixed route OAuth is not enabled on this Publisher")
        config = OAuthClientCredentialsConfig.model_validate_json(credential.config.model_dump_json())
        if not re.fullmatch(r"turnstile-oauth-[a-f0-9]{32}", config.provider_id):
            raise PolicyCompilationError("Fixed route OAuth provider identity is invalid")
        if credential.client_secret is not None and (
            not credential.client_secret or len(credential.client_secret.encode()) > 25600
        ):
            raise PolicyCompilationError("Fixed route OAuth secret source is invalid")
        identity = self._oauth_identity()
        provider_path = f"/authorizationProviders/{config.provider_id}"
        authorization_path = provider_path + "/authorizations/connection"
        stages = [
            (provider_path, self._oauth_provider_properties(credential)),
            (authorization_path, {
                "authorizationType": "OAuth2", "oauth2grantType": "ClientCredentials",
                "parameters": {"clientId": str(config.client_id),
                               "clientSecret": credential.client_secret},
            }),
            (authorization_path + "/accessPolicies/" + identity["objectId"], identity),
        ]
        for index, (path, properties) in enumerate(stages):
            observed = self._request("GET", path, allow_not_found=True)
            if observed.status_code == 404:
                if credential.client_secret is None:
                    raise PolicyCompilationError("Fixed route OAuth has no approved secret source")
                body = canonical_document({"properties": properties})
                headers = {**self._headers(), "If-None-Match": "*"}
                checkpoint({
                    "version": 1, "method": "PUT", "resource_id": self._resource_id_base + path,
                    "api_version": self._API_VERSION, "if_none_match": "*",
                    "configuration_sha256": text_digest(config.model_dump_json()),
                    "body": body, "body_sha256": text_digest(body),
                })
                try:
                    response = self._client.request(
                        "PUT", self._base + path, params={"api-version": self._API_VERSION},
                        headers=headers, content=body.encode("utf-8"),
                        timeout=10.0, follow_redirects=False,
                    )
                except httpx.HTTPError:
                    response = None
                if (
                    response is not None and response.status_code < 500
                    and response.status_code not in {200, 201, 202, 204, 409, 412, 429}
                ):
                    raise PolicyCompilationError("Fixed route OAuth creation was rejected")
                observed = self._request("GET", path, allow_not_found=True)
                if observed.status_code == 404:
                    raise RetryablePublicationError("Fixed route OAuth creation requires read-only recovery")
            actual = observed.json().get("properties")
            if not isinstance(actual, dict):
                raise PolicyCompilationError("Fixed route OAuth readback is invalid")
            if index == 0:
                if not self._oauth_provider_matches(credential, actual):
                    raise PolicyCompilationError("Fixed route OAuth provider belongs to another source")
            elif index == 1:
                if (
                    actual.get("authorizationType") != "OAuth2"
                    or actual.get("oauth2grantType") != "ClientCredentials"
                    or (actual.get("parameters") or {}).get("clientId") != str(config.client_id)
                ):
                    raise PolicyCompilationError("Fixed route OAuth authorization belongs to another client")
                if actual.get("status") != "Connected":
                    raise RetryablePublicationError("Fixed route OAuth connection is not yet confirmed")
            elif any(actual.get(key) != value for key, value in identity.items()) or actual.get("appIds"):
                raise PolicyCompilationError("Fixed route OAuth access policy differs from its identity")
        if self.oauth_credential_issues(credential):
            raise PolicyCompilationError("Fixed route OAuth dependency readback is not current")

    def route_backend_properties(self, backend: BackendResource) -> dict[str, Any]:
        if isinstance(backend, BackendPoolResource):
            pool: dict[str, Any] = {"services": self._pool_services(backend)}
            if backend.session_cookie_name is not None:
                pool["sessionAffinity"] = {
                    "sessionId": {"source": "cookie", "name": backend.session_cookie_name},
                }
            return {"type": "Pool", "pool": pool}
        ApimPolicyCompiler._validated_backend_url(backend.url)
        if len({name.casefold() for name, _ in backend.headers}) != len(backend.headers):
            raise PolicyCompilationError("Fixed route Backend repeats a credential header")
        return {
            "type": "Single", "protocol": "http", "url": backend.url,
            "tls": {"validateCertificateChain": True, "validateCertificateName": True},
            "credentials": {"header": {name: [value] for name, value in backend.headers}}
            if backend.headers else {},
            "circuitBreaker": self._circuit_breaker_properties(backend.circuit_breaker),
        }

    @staticmethod
    def _pool_resource_values(raw: Any, pool: bool) -> dict[str, Any]:
        from copy import deepcopy

        if not isinstance(raw, dict):
            raise PolicyCompilationError("Fixed route Pool resource readback is invalid")
        if pool:
            raw = {name: value for name, value in raw.items()
                   if name not in {"url", "protocol"} or value is not None}
        allowed = {"title", "description", "type", "pool"} if pool else {
            "title", "description", "type", "protocol", "url", "tls", "credentials", "circuitBreaker",
        }
        if set(raw) - allowed:
            raise PolicyCompilationError("Fixed route Pool resource has unsupported properties")
        if pool:
            return {"type": raw.get("type"), "pool": raw.get("pool")}
        breaker = deepcopy(raw.get("circuitBreaker"))
        if isinstance(breaker, dict) and isinstance(breaker.get("rules"), list):
            for rule in breaker["rules"]:
                if not isinstance(rule, dict):
                    continue
                for container, field in (
                    (rule, "tripDuration"), (rule.get("failureCondition"), "interval"),
                ):
                    if isinstance(container, dict) and field in container:
                        seconds = AzureApimPublisherClient._normalized_duration(container[field])
                        if type(seconds) is int:
                            container[field] = f"PT{seconds}S"
        return {
            "type": raw.get("type", "Single"), "protocol": raw.get("protocol"), "url": raw.get("url"),
            "tls": raw.get("tls"), "credentials": raw.get("credentials") or {},
            "circuitBreaker": breaker,
        }

    def _route_pool_resource(
        self, connection: ConnectionRevision,
    ) -> BackendPoolResource:
        pool = connection.pool
        if (pool is None or connection.scope.target_id != str(self._settings.apim_gateway_profile_id)
                or connection.scope.api_id != self._settings.apim_api_id):
            raise PolicyCompilationError("Fixed route Pool target is invalid")
        return BackendPoolResource(
            id=connection.backend_id, title="Turnstile route Pool", url="",
            members=tuple(BackendPoolMemberResource(
                member.backend_id, member.priority, member.weight,
            ) for member in pool.members),
            session_cookie_name=pool.session_cookie_name,
        )

    def _prepare_pool_resource(
        self, path: str, properties: Mapping[str, Any], connection: ConnectionRevision,
        checkpoint: Callable[[dict[str, Any]], None], *, api_version: str, allow_create: bool,
    ) -> tuple[Mapping[str, Any], bool]:
        from ..domain.storage_routes import canonical_document, text_digest

        response = self._request("GET", path, allow_not_found=True, api_version=api_version)
        created = response.status_code == 404
        if created:
            if not allow_create:
                raise RetryablePublicationError("Fixed route Pool preparation slice completed")
            body = canonical_document({"properties": properties})
            headers = {**self._headers(), "If-None-Match": "*"}
            checkpoint({
                "version": 1, "method": "PUT", "resource_id": self._resource_id_base + path,
                "api_version": api_version, "if_none_match": "*",
                "connection_sha256": document_digest(connection), "body": body,
                "body_sha256": text_digest(body),
            })
            try:
                result = self._client.request(
                    "PUT", self._base + path, params={"api-version": api_version},
                    headers=headers, content=body.encode("utf-8"), timeout=10.0,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                result = None
            if result is not None and result.status_code < 500 and result.status_code not in {
                200, 201, 202, 204, 409, 412, 429,
            }:
                raise PolicyCompilationError("Fixed route Pool resource creation was rejected")
            response = self._request("GET", path, allow_not_found=True, api_version=api_version)
        if response.status_code != 200:
            raise RetryablePublicationError("Fixed route Pool resource requires read-only recovery")
        raw = response.json().get("properties")
        if not isinstance(raw, dict):
            raise PolicyCompilationError("Fixed route Pool resource readback is invalid")
        return raw, created

    def prepare_route_pool(
        self, connection: ConnectionRevision, backends: Sequence[BackendResource],
        checkpoint: Callable[[dict[str, Any]], None],
    ) -> None:
        from ..domain.storage_routes import canonical_document

        connection = ConnectionRevision.model_validate_json(canonical_document(connection))
        native_pool = self._route_pool_resource(connection)
        assert connection.pool is not None
        pool = connection.pool
        by_id = {backend.id: backend for backend in backends}
        if len(by_id) != len(backends) or set(by_id) != {member.backend_id for member in pool.members}:
            raise PolicyCompilationError("Fixed route Pool member set changed")
        properties = self.route_backend_properties(native_pool)
        if document_digest(properties) != connection.dependency_sha256:
            raise PolicyCompilationError("Fixed route Pool dependency changed")
        member_documents = {}
        for member in pool.members:
            backend = by_id[member.backend_id]
            if isinstance(backend, BackendPoolResource):
                raise PolicyCompilationError("Fixed route Pool cannot contain nested pools")
            values = self.route_backend_properties(backend)
            if document_digest(values) != member.dependency_sha256:
                raise PolicyCompilationError("Fixed route Pool member dependency changed")
            for name, value in backend.headers:
                if name.casefold() in {"authorization", "api-key", "x-api-key"} and not any(
                    value == ("Bearer " if name.casefold() == "authorization" else "") + "{{" + key + "}}"
                    for key in pool.credential_versions
                ):
                    raise PolicyCompilationError("Fixed route Pool member credential is not versioned")
            member_documents[member.backend_id] = values
        created = 0
        for name, value in pool.header_values.items():
            values = {"displayName": name, "secret": False, "value": value}
            raw, wrote = self._prepare_pool_resource(
                "/namedValues/" + quote(name, safe=""), values, connection, checkpoint,
                api_version=self._API_VERSION, allow_create=created < 4,
            )
            self._verify_route_header_value(name, value, raw)
            created += int(wrote)
        for name, secret_id in pool.credential_versions.items():
            values = {"displayName": name, "secret": True, "keyVault": {"secretIdentifier": secret_id}}
            raw, wrote = self._prepare_pool_resource(
                "/namedValues/" + quote(name, safe=""), values, connection, checkpoint,
                api_version=self._API_VERSION, allow_create=created < 4,
            )
            observed = (raw.get("keyVault") or {}).get("secretIdentifier")
            if raw.get("secret") is not True and (wrote or observed == secret_id):
                # APIM marks a Key Vault reference secret only when its asynchronous creation ends.
                raise RetryablePublicationError("APIM credential resource is not yet ready")
            self._validate_named_value(NamedValueResource(name, secret_id), raw)
            created += int(wrote)
        for member in pool.members:
            raw, wrote = self._prepare_pool_resource(
                "/backends/" + quote(member.backend_id, safe=""), member_documents[member.backend_id],
                connection, checkpoint, api_version=self._API_VERSION, allow_create=created < 4,
            )
            if document_digest(self._pool_resource_values(raw, False)) != member.dependency_sha256:
                raise PolicyCompilationError("Fixed route Pool member readback changed")
            created += int(wrote)
        raw, _ = self._prepare_pool_resource(
            "/backends/" + quote(connection.backend_id, safe=""), properties, connection, checkpoint,
            api_version=self._AFFINITY_API_VERSION if pool.session_cookie_name else self._API_VERSION,
            allow_create=created < 4,
        )
        if document_digest(self._pool_resource_values(raw, True)) != connection.dependency_sha256:
            raise PolicyCompilationError("Fixed route Pool readback changed")

    def verify_route_pool(self, connection: ConnectionRevision) -> str:
        self.verify_route_pool_slice(connection, 0, maximum=151)
        return connection.dependency_sha256

    def verify_route_pool_slice(
        self, connection: ConnectionRevision, cursor: int, *, maximum: int = 4,
    ) -> int:
        connection = ConnectionRevision.model_validate(connection)
        native_pool = self._route_pool_resource(connection)
        expected = self.route_backend_properties(native_pool)
        if document_digest(expected) != connection.dependency_sha256:
            raise PolicyCompilationError("Fixed route Pool definition changed")
        assert connection.pool is not None
        pool = connection.pool
        versions = sorted(pool.credential_versions.items())
        headers = sorted(pool.header_values.items())
        total = 1 + len(pool.members) + len(versions) + len(headers)
        if (type(cursor) is not int or not 0 <= cursor <= total
            or type(maximum) is not int or not 1 <= maximum <= 151):
            raise PolicyCompilationError("Fixed route Pool readback cursor is invalid")
        end = min(total, cursor + maximum)
        for index in range(cursor, end):
            if index == 0:
                raw = self._request(
                    "GET", "/backends/" + quote(connection.backend_id, safe=""),
                    api_version=self._AFFINITY_API_VERSION
                    if pool.session_cookie_name else self._API_VERSION,
                ).json().get("properties")
                if document_digest(self._pool_resource_values(raw, True)) != (
                    connection.dependency_sha256
                ):
                    raise PolicyCompilationError("Fixed route Pool dependency readback changed")
            elif index <= len(pool.members):
                member = pool.members[index - 1]
                raw = self._request(
                    "GET", "/backends/" + quote(member.backend_id, safe=""),
                ).json().get("properties")
                if document_digest(self._pool_resource_values(raw, False)) != member.dependency_sha256:
                    raise PolicyCompilationError("Fixed route Pool member readback changed")
            elif index <= len(pool.members) + len(versions):
                name, secret_id = versions[index - len(pool.members) - 1]
                raw = self._request(
                    "GET", "/namedValues/" + quote(name, safe=""),
                ).json().get("properties")
                if not isinstance(raw, Mapping):
                    raise PolicyCompilationError("Fixed route Pool credential readback is invalid")
                self._validate_named_value(NamedValueResource(name, secret_id), raw)
            else:
                name, value = headers[index - len(pool.members) - len(versions) - 1]
                raw = self._request(
                    "GET", "/namedValues/" + quote(name, safe=""),
                ).json().get("properties")
                self._verify_route_header_value(name, value, raw)
        return end

    @staticmethod
    def _verify_route_header_value(name: str, value: str, raw: Any) -> None:
        if (not isinstance(raw, dict) or raw.get("displayName") != name
                or raw.get("secret") is not False or raw.get("value") != value
                or raw.get("keyVault") is not None
                or raw.get("provisioningState", "Succeeded") != "Succeeded"
                or set(raw) - {
                    "displayName", "secret", "value", "tags", "keyVault", "provisioningState",
                }):
            raise PolicyCompilationError("Fixed route public metadata readback changed")

    def _verify_route_oauth(self, connection: ConnectionRevision) -> None:
        checked: set[str] = set()
        for credential in (connection.credential, connection.count):
            if credential is None or credential.oauth is None:
                continue
            if not self._settings.databricks_oauth_enabled:
                raise PolicyCompilationError("Fixed route OAuth is not enabled on this Publisher")
            identity = credential.oauth.model_dump_json()
            if identity in checked:
                continue
            if self.oauth_credential_issues(OAuthCredentialResource(config=credential.oauth)):
                raise PolicyCompilationError("Fixed route OAuth dependency is not current")
            checked.add(identity)

    def prepare_route_connection(
        self, connection: ConnectionRevision, checkpoint: Callable[[dict[str, Any]], None],
        *, recovery_only: bool = False,
    ) -> str:
        from ..domain.storage_routes import canonical_document, text_digest

        connection = ConnectionRevision.model_validate_json(canonical_document(connection))
        self._verify_route_oauth(connection)
        origin = connection.origin
        credentials: dict[str, Any] = {}
        if connection.observer:
            origin = self._settings.apim_usage_observer_url or ""
            reference = self._settings.apim_usage_observer_key_named_value
            if not origin or not reference:
                raise PolicyCompilationError("Fixed route Observer binding is unavailable")
            credentials = {"header": {
                "x-adapter-key": ["{{" + reference + "}}"],
            }}
        properties = {
            "type": "Single", "protocol": "http", "url": origin,
            "tls": {"validateCertificateChain": True, "validateCertificateName": True},
            "credentials": credentials, "circuitBreaker": None,
        }
        if document_digest(properties) != connection.dependency_sha256:
            raise PolicyCompilationError("Fixed route Backend preparation contract changed")
        path = "/backends/" + quote(connection.backend_id, safe="")
        observed = self._request("GET", path, allow_not_found=True)
        if observed.status_code != 404:
            return self.verify_route_connection(connection)
        if recovery_only:
            raise RetryablePublicationError("Route Backend creation requires read-only recovery")
        body = canonical_document({"properties": properties})
        headers = {**self._headers(), "If-None-Match": "*"}
        checkpoint({
            "version": 1, "method": "PUT", "resource_id": self._resource_id_base + path,
            "api_version": self._API_VERSION, "if_none_match": "*",
            "connection_sha256": document_digest(connection),
            "body": body, "body_sha256": text_digest(body),
        })
        try:
            response = self._client.request(
                "PUT", self._base + path, params={"api-version": self._API_VERSION},
                headers=headers, content=body.encode("utf-8"),
                timeout=10.0, follow_redirects=False,
            )
        except httpx.HTTPError:
            response = None
        if (
            response is not None and response.status_code < 500
            and response.status_code not in {200, 201, 202, 204, 409, 412, 429}
        ):
            raise PolicyCompilationError("Fixed route Backend creation was rejected")
        observed = self._request("GET", path, allow_not_found=True)
        if observed.status_code == 404:
            raise RetryablePublicationError("Route Backend creation requires read-only recovery")
        return self.verify_route_connection(connection)

    def verify_route_connection(self, connection: ConnectionRevision) -> str:
        connection = ConnectionRevision.model_validate(connection)
        if connection.pool is not None:
            return self.verify_route_pool(connection)
        self._verify_route_oauth(connection)
        response = self._request("GET", "/backends/" + quote(connection.backend_id, safe=""))
        raw = response.json().get("properties")
        if not isinstance(raw, dict) or set(raw) - {
            "title", "description", "url", "protocol", "type", "tls", "credentials", "circuitBreaker",
        }:
            raise PolicyCompilationError("Fixed route Backend properties are incomplete or unsupported")
        values = {
            "type": raw.get("type", "Single"), "protocol": raw.get("protocol"),
            "url": raw.get("url"), "tls": raw.get("tls"),
            "credentials": raw.get("credentials") or {}, "circuitBreaker": raw.get("circuitBreaker"),
        }
        expected_url = connection.origin
        if connection.observer:
            expected_url = self._settings.apim_usage_observer_url or ""
            if not expected_url or not self._settings.apim_usage_observer_key_named_value:
                raise PolicyCompilationError("Fixed route Observer binding is unavailable")
        credentials = values["credentials"]
        if not isinstance(credentials, dict):
            raise PolicyCompilationError("Fixed route Backend credentials changed")
        allowed_headers = {
            "x-adapter-key": ["{{" + str(self._settings.apim_usage_observer_key_named_value) + "}}"],
        } if connection.observer else {}
        if (
            values["type"] != "Single" or values["protocol"] != "http"
            or str(values["url"]).rstrip("/") != expected_url.rstrip("/")
            or values["tls"] != {"validateCertificateChain": True, "validateCertificateName": True}
            or values["circuitBreaker"] is not None
            or (credentials and credentials != {"header": allowed_headers})
            or document_digest(values) != connection.dependency_sha256
        ):
            raise PolicyCompilationError("Fixed route Backend differs from its approved dependency")
        return connection.dependency_sha256

    def route_program_operations(self, revision: str) -> dict[str, tuple[str, str, str]]:
        path = self._revision_path(revision) + "/operations"
        prefix = self._resource_id_base + url_unquote(path) + "/"
        result: dict[str, tuple[str, str, str]] = {}
        for operation in self._list_all(path, max_items=30, max_pages=3):
            name = operation.get("name")
            properties = operation.get("properties")
            if (
                not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", name)
                or name in result or operation.get("id") != prefix + name
                or not isinstance(properties, dict)
                or properties.get("method") not in {"GET", "POST", "HEAD"}
                or not isinstance(properties.get("urlTemplate"), str)
            ):
                raise PolicyCompilationError("Fixed program operation inventory changed")
            response, policy = self._read_policy(
                path + "/" + quote(name, safe="") + "/policies/policy",
                allow_not_found=True,
            )
            if response.status_code == 404:
                raise PolicyCompilationError("Fixed program operation policy is missing")
            result[name] = (properties["method"], properties["urlTemplate"], policy)
        return result

    def inspect_revision_dependencies(
        self, publication: GatewayPublication
    ) -> GatewayReleaseDependencies:
        manifest = publication.resource_manifest
        raw_backends = manifest.get("backends")
        raw_named_values = manifest.get("named_values")
        raw_oauth = manifest.get("oauth_credentials")
        backend_ids = (
            sorted(str(value) for value in raw_backends)
            if isinstance(raw_backends, list)
            else []
        )
        named_value_ids = (
            sorted(str(value) for value in raw_named_values)
            if isinstance(raw_named_values, list)
            else []
        )
        issues: list[str] = []
        compiled = ApimPolicyCompiler(
            self._settings.apim_probe_subscription_id,
            self._settings.apim_usage_observer_url,
            self._settings.apim_usage_observer_key_named_value,
        ).compile(publication)
        expected_pool_members = {
            backend.id: {member.backend_id for member in backend.members}
            for backend in compiled.backends
            if isinstance(backend, BackendPoolResource)
        }
        pools = sorted(value for value in backend_ids if value in expected_pool_members)
        backends = sorted(value for value in backend_ids if value not in expected_pool_members)
        expected_backends = {backend.id: backend for backend in compiled.backends}
        expected_named_values = {value.id: value for value in compiled.named_values}
        expected_oauth = {value.config.provider_id: value for value in compiled.oauth_credentials}
        oauth_ids = sorted(str(value) for value in raw_oauth) if isinstance(raw_oauth, list) else []
        if set(oauth_ids) != set(expected_oauth):
            issues.append("missing_recorded_oauth_dependencies")
        for credential in compiled.oauth_credentials:
            issues.extend(self.oauth_credential_issues(credential))
        revision = publication.apim_revision
        if not revision:
            issues.append("missing_recorded_apim_revision")
        else:
            revision_path = self._revision_path(revision)
            observed = self._request("GET", revision_path, allow_not_found=True)
            if observed.status_code == 404:
                issues.append("missing_apim_revision")
            else:
                expected_contract = manifest.get("parent_policy_contract_sha256")
                parent = self._policy_value(
                    f"{revision_path}/policies/policy" + ("?format=rawxml" if expected_contract else "")
                )
                if parent is None:
                    issues.append("missing_parent_policy")
                else:
                    expected_parent_hash = (
                        manifest.get("parent_policy_sha256")
                        or manifest.get("base_policy_sha256")
                    )
                    if (
                        (expected_parent_hash is not None or expected_contract is not None)
                        and not parent_readback_matches(
                            parent, expected_contract=expected_contract, expected_raw=expected_parent_hash,
                        )
                    ):
                        issues.append("mismatched_parent_policy_sha256")
                operation_policies: list[str] = []
                managed_operations = (
                    *self._managed_operation_ids(
                        include_images=bool(manifest.get("images_generations_operation"))
                    ),
                    *(operation_id for operation_id, _ in compiled.availability_probe_policies),
                )
                for operation_id in managed_operations:
                    operation_path = (
                        f"{revision_path}/operations/{quote(operation_id, safe='')}"
                    )
                    operation = self._request(
                        "GET", operation_path, allow_not_found=True
                    )
                    if operation.status_code == 404:
                        issues.append(f"missing_operation:{operation_id}")
                        continue
                    policy = self._policy_value(f"{operation_path}/policies/policy")
                    if policy is None:
                        issues.append(f"missing_operation_policy:{operation_id}")
                    else:
                        operation_policies.append(policy)
                if len(operation_policies) == len(managed_operations):
                    observed_policy_hash = hashlib.sha256(
                        "\n".join(operation_policies).encode("utf-8")
                    ).hexdigest()
                    if (
                        publication.policy_sha256 is not None
                        and observed_policy_hash != publication.policy_sha256
                    ):
                        expected_policies = (
                            compiled.chat_completions_policy,
                            compiled.responses_policy,
                            compiled.responses_compact_policy,
                            compiled.messages_policy,
                            compiled.count_tokens_policy,
                            compiled.models_policy,
                            *((compiled.images_generations_policy,)
                              if compiled.images_generations_policy is not None else ()),
                            *(source for _, source in compiled.availability_probe_policies),
                        )
                        matches_availability_compilation = (
                            bool(compiled.availability_probe_policies)
                            and compiled.policy_sha256 == publication.policy_sha256
                            and len(operation_policies) == len(expected_policies)
                            and all(
                                component_digest(parse_policy(actual), normalize_text_defaults=False)
                                == component_digest(parse_policy(expected), normalize_text_defaults=False)
                                for actual, expected in zip(operation_policies, expected_policies, strict=True)
                            )
                        )
                        if not matches_availability_compilation:
                            issues.append("mismatched_compiled_policy_sha256")

        observed_backend_types: dict[str, str] = {}
        for backend_id in backend_ids:
            expected_backend = expected_backends.get(backend_id)
            response = self._request(
                "GET",
                f"/backends/{quote(backend_id, safe='')}",
                allow_not_found=True,
                api_version=(
                    self._AFFINITY_API_VERSION
                    if isinstance(expected_backend, BackendPoolResource)
                    and expected_backend.session_cookie_name else None
                ),
            )
            if response.status_code == 404:
                issues.append(f"missing_backend:{backend_id}")
                continue
            properties = response.json().get("properties") or {}
            if (
                expected_backend is None
                or not isinstance(properties, Mapping)
                or not self._backend_identity_matches(expected_backend, properties)
            ):
                issues.append(f"mismatched_backend_identity:{backend_id}")
                continue
            observed_backend_types[backend_id] = str(properties.get("type", "Single"))
            if backend_id in pools:
                if observed_backend_types[backend_id] != "Pool":
                    issues.append(f"mismatched_backend_pool_type:{backend_id}")
                    continue
                for member in (properties.get("pool") or {}).get("services") or []:
                    member_id = str(member.get("id", "")).rsplit("/", 1)[-1]
                    if member_id not in backend_ids:
                        issues.append(
                            f"mismatched_unrecorded_pool_member:{backend_id}:{member_id}"
                        )
                observed_members = {
                    str(member.get("id", "")).rsplit("/", 1)[-1]
                    for member in (properties.get("pool") or {}).get("services") or []
                }
                if observed_members != expected_pool_members.get(backend_id, set()):
                    issues.append(f"mismatched_backend_pool_members:{backend_id}")
        for named_value_id in named_value_ids:
            response = self._request(
                "GET",
                f"/namedValues/{quote(named_value_id, safe='')}",
                allow_not_found=True,
            )
            if response.status_code == 404:
                issues.append(f"missing_named_value:{named_value_id}")
                continue
            properties = response.json().get("properties") or {}
            expected_named_value = expected_named_values.get(named_value_id)
            if not isinstance(properties, Mapping) or expected_named_value is None:
                issues.append(f"mismatched_named_value_identity:{named_value_id}")
                continue
            key_vault = properties.get("keyVault")
            observed_secret_id = (
                key_vault.get("secretIdentifier")
                if isinstance(key_vault, Mapping)
                else None
            )
            if (
                properties.get("secret") is not True
                or observed_secret_id != expected_named_value.key_vault_secret_id
            ):
                issues.append(f"mismatched_named_value_identity:{named_value_id}")
        observer_named_value = self._settings.apim_usage_observer_key_named_value
        if observer_named_value and observer_named_value not in named_value_ids:
            response = self._request(
                "GET",
                f"/namedValues/{quote(observer_named_value, safe='')}",
                allow_not_found=True,
            )
            if response.status_code == 404:
                issues.append("missing_required_observer_named_value")
            else:
                properties = response.json().get("properties") or {}
                if not isinstance(properties, Mapping) or properties.get("secret") is not True:
                    issues.append("mismatched_required_observer_named_value")

        recorded_complete = all(
            value is not None
            for value in (
                publication.apim_revision,
                publication.policy_sha256,
                manifest.get("parent_policy_sha256")
                or manifest.get("base_policy_sha256"),
                raw_backends if isinstance(raw_backends, list) else None,
                raw_named_values if isinstance(raw_named_values, list) else None,
            )
        )
        if expected_oauth and set(oauth_ids) != set(expected_oauth):
            recorded_complete = False
        if not recorded_complete:
            issues.append("recorded_dependencies_incomplete")
        live_status: Literal["healthy", "missing", "mismatched"] = (
            "healthy"
            if not issues
            else "missing"
            if any(value.startswith("missing_") for value in issues)
            else "mismatched"
        )
        return GatewayReleaseDependencies(
            apim_revision=publication.apim_revision,
            parent_policy_sha256=(
                str(
                    manifest.get("parent_policy_sha256")
                    or manifest["base_policy_sha256"]
                )
                if manifest.get("parent_policy_sha256") is not None
                or manifest.get("base_policy_sha256") is not None
                else None
            ),
            compiled_policy_sha256=publication.policy_sha256,
            backends=backends,
            backend_pools=pools,
            named_values=named_value_ids,
            oauth_credentials=oauth_ids,
            recorded_complete=recorded_complete,
            live_status=live_status,
            issues=issues,
        )

    def plan_release_garbage_collection(
        self,
        releases: Sequence[GatewayPublication],
        retained_release_ids: set[str],
        *, fixed_dependencies: Mapping[str, GatewayReleaseDependencies] | None = None,
    ) -> ReleaseGcPlanEvidence:
        release_by_revision: dict[str, list[GatewayPublication]] = {}
        for release in releases:
            if release.apim_revision is not None:
                release_by_revision.setdefault(release.apim_revision, []).append(release)
        retained = [
            release for release in releases if str(release.id) in retained_release_ids
        ]
        protected_backend_ids: set[str] = set()
        protected_named_value_ids: set[str] = set()
        fixed_revision_ids: set[str] = set()
        fixed_configurations: list[tuple[str, str | None, str | None]] = []
        for release in releases:
            if not any(field in release.resource_manifest for field in (
                "fixed_route_program", "route_configuration", "fixed_route_snapshot",
                "route_preparation", "route_dependencies", "route_rollback",
            )):
                continue
            dependencies = (fixed_dependencies or {}).get(str(release.id))
            if (
                dependencies is None or not dependencies.recorded_complete
                or dependencies.apim_revision != release.apim_revision
                or dependencies.compiled_policy_sha256 != release.policy_sha256
            ):
                raise PolicyCompilationError(
                    "Fixed route GC requires complete confirmed configuration evidence",
                )
            if dependencies.apim_revision is None:
                raise PolicyCompilationError("Fixed route GC program identity is missing")
            fixed_revision_ids.add(dependencies.apim_revision)
            protected_backend_ids.update(dependencies.backends)
            protected_named_value_ids.update(dependencies.named_values)
            fixed_configurations.append((
                str(release.id), dependencies.route_configuration_sha256,
                dependencies.route_head_sha256,
            ))
        for release in retained:
            manifest = release.resource_manifest
            protected_backend_ids.update(
                self._string_list(manifest.get("backends"))
            )
            protected_named_value_ids.update(
                self._string_list(manifest.get("named_values"))
            )

        api_rows = self._list_all(
            "/apis?includeRevisions=true&expandApiVersionSet=true"
        )
        policy_evidence: list[tuple[str, str]] = []
        current_revision = self.current_revision()
        protected_revision_ids: set[str] = set(fixed_revision_ids)
        all_revision_ids: set[str] = set()
        revision_paths: dict[str, str] = {}
        for api in api_rows:
            resource_path = self._resource_path(api)
            raw_properties = api.get("properties")
            properties = raw_properties if isinstance(raw_properties, Mapping) else {}
            revision = str(properties.get("apiRevision") or "")
            is_managed_api = self._api_name(resource_path) == self._settings.apim_api_id
            if is_managed_api and revision:
                all_revision_ids.add(revision)
                revision_paths[revision] = resource_path
            matched_releases = (
                release_by_revision.get(revision, []) if is_managed_api else []
            )
            protect_policy = (
                not is_managed_api
                or revision == current_revision
                or not matched_releases or revision in fixed_revision_ids
                or any(str(matched.id) in retained_release_ids for matched in matched_releases)
            )
            if protect_policy and revision:
                protected_revision_ids.add(revision)
            if protect_policy:
                self._collect_api_policy_evidence(resource_path, policy_evidence)

        release_asset_references: list[tuple[str, str]] = []
        release_assets = self._list_all(
            f"/apis/{quote(self._settings.apim_api_id, safe='')}/releases"
        )
        for release_asset in release_assets:
            release_asset_path = self._resource_path(release_asset)
            raw_properties = release_asset.get("properties")
            properties = raw_properties if isinstance(raw_properties, Mapping) else {}
            if not properties.get("apiId"):
                detail = self._request("GET", release_asset_path).json()
                raw_properties = detail.get("properties")
                properties = (
                    raw_properties if isinstance(raw_properties, Mapping) else {}
                )
            api_id = str(properties.get("apiId") or "")
            if not api_id:
                raise PolicyCompilationError(
                    "APIM Release asset did not identify its API"
                )
            api_path = self._resource_path({"id": api_id})
            api_reference = url_unquote(api_path.removeprefix("/apis/"))
            api_name, separator, revision = api_reference.partition(";rev=")
            if (
                not api_path.startswith("/apis/")
                or "/" in api_reference
                or api_name != self._settings.apim_api_id
                or (separator and not revision)
            ):
                raise PolicyCompilationError(
                    "APIM Release asset references an unexpected API"
                )
            release_asset_references.append(
                (self._resource_name(release_asset), api_reference)
            )
            if separator:
                revision_path = revision_paths.get(revision)
                if revision_path is None:
                    raise PolicyCompilationError(
                        "APIM Release asset references a Revision absent from the API inventory"
                    )
                protected_revision_ids.add(revision)
                self._collect_api_policy_evidence(revision_path, policy_evidence)

        global_policy = self._policy_value("/policies/policy")
        if global_policy is not None:
            policy_evidence.append(("global", global_policy))
        product_rows = self._list_all("/products")
        for product in product_rows:
            product_path = self._resource_path(product)
            policy = self._policy_value(f"{product_path}/policies/policy")
            if policy is not None:
                policy_evidence.append((product_path, policy))
            for product_api in self._list_all(f"{product_path}/apis"):
                product_api_path = self._resource_path(product_api)
                if self._api_name(product_api_path) != self._settings.apim_api_id:
                    continue
                product_api_properties = product_api.get("properties")
                product_revision = (
                    str(product_api_properties.get("apiRevision") or "")
                    if isinstance(product_api_properties, Mapping)
                    else ""
                )
                if not product_revision and ";rev=" in product_api_path:
                    product_revision = product_api_path.rsplit(";rev=", 1)[-1]
                if not product_revision:
                    raise PolicyCompilationError(
                        "APIM Product API link did not identify an API Revision"
                    )
                revision_path = revision_paths.get(product_revision)
                if revision_path is None:
                    raise PolicyCompilationError(
                        "APIM Product references a Revision absent from the API inventory"
                    )
                protected_revision_ids.add(product_revision)
                self._collect_api_policy_evidence(
                    revision_path, policy_evidence
                )
        fragment_rows = self._list_all("/policyFragments")
        for fragment in fragment_rows:
            fragment_path = self._resource_path(fragment)
            fragment_detail = self._request("GET", fragment_path).json()
            value = (fragment_detail.get("properties") or {}).get("value")
            if isinstance(value, str):
                policy_evidence.append((fragment_path, value))

        backend_rows = self._list_all("/backends")
        backend_types: dict[str, str] = {}
        backend_documents: dict[str, dict[str, Any]] = {}
        pool_members: dict[str, set[str]] = {}
        for backend in backend_rows:
            backend_id = self._resource_name(backend)
            backend_documents[backend_id] = backend
            properties = backend.get("properties") or {}
            backend_types[backend_id] = str(properties.get("type", "Single"))
            if backend_types[backend_id] == "Pool":
                pool_members[backend_id] = {
                    str(value.get("id", "")).rsplit("/", 1)[-1]
                    for value in (properties.get("pool") or {}).get("services") or []
                }
        named_value_rows = self._list_all("/namedValues")
        all_named_value_ids = {self._resource_name(value) for value in named_value_rows}

        policy_text = "\n".join(value for _, value in policy_evidence)
        protected_backend_ids.update(
            backend_id for backend_id in backend_types if backend_id in policy_text
        )
        protected_named_value_ids.update(
            value for value in all_named_value_ids if value in policy_text
        )
        changed = True
        while changed:
            changed = False
            for pool_id, members in pool_members.items():
                if pool_id not in protected_backend_ids:
                    continue
                before = len(protected_backend_ids)
                protected_backend_ids.update(members)
                changed = changed or len(protected_backend_ids) != before
        for backend_id in protected_backend_ids:
            backend_document = backend_documents.get(backend_id)
            if backend_document is None:
                continue
            backend_text = json.dumps(
                backend_document.get("properties") or {},
                sort_keys=True,
            )
            protected_named_value_ids.update(
                value for value in all_named_value_ids if value in backend_text
            )

        expired = [
            release for release in releases if str(release.id) not in retained_release_ids
        ]
        candidates: list[dict[str, object]] = []
        for release in expired:
            if (
                release.apim_revision
                and release.apim_revision != current_revision
                and release.apim_revision in all_revision_ids
                and release.apim_revision not in protected_revision_ids
            ):
                candidates.append(
                    {
                        "resource_type": "api_revision",
                        "resource_id": release.apim_revision,
                        "reasons": ["release_not_retained", "revision_not_current"],
                    }
                )
            for backend_id in self._string_list(
                release.resource_manifest.get("backends")
            ):
                value = str(backend_id)
                if value not in protected_backend_ids and value in backend_types:
                    candidates.append(
                        {
                            "resource_type": (
                                "backend_pool"
                                if backend_types[value] == "Pool"
                                else "backend"
                            ),
                            "resource_id": value,
                            "reasons": ["zero_retained_or_current_references"],
                        }
                    )
            for named_value_id in self._string_list(
                release.resource_manifest.get("named_values")
            ):
                value = str(named_value_id)
                if (
                    value not in protected_named_value_ids
                    and value in all_named_value_ids
                ):
                    candidates.append(
                        {
                            "resource_type": "named_value",
                            "resource_id": value,
                            "reasons": ["zero_retained_or_current_references"],
                        }
                    )
        unique_candidates = {
            (str(value["resource_type"]), str(value["resource_id"])): value
            for value in candidates
        }
        ordered_candidates = tuple(
            unique_candidates[key] for key in sorted(unique_candidates)
        )
        graph = {
            "apis": sorted(all_revision_ids),
            "protected_revisions": sorted(protected_revision_ids),
            "protected_backends": sorted(protected_backend_ids),
            "protected_named_values": sorted(protected_named_value_ids),
            **({"fixed_configurations_retained": sorted(fixed_configurations)}
               if fixed_configurations else {}),
            "policy_hashes": sorted(
                (
                    name,
                    hashlib.sha256(value.encode("utf-8")).hexdigest(),
                )
                for name, value in policy_evidence
            ),
            "pool_members": {
                key: sorted(value) for key, value in sorted(pool_members.items())
            },
            "release_assets": sorted(release_asset_references),
            "candidates": list(ordered_candidates),
        }
        graph_hash = hashlib.sha256(
            json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ReleaseGcPlanEvidence(
            current_non_release_references={
                "protected_revision_ids": sorted(protected_revision_ids),
                "protected_backend_ids": sorted(protected_backend_ids),
                "protected_named_value_ids": sorted(protected_named_value_ids),
                "policy_surface_count": len(policy_evidence),
                "product_count": len(product_rows),
                "policy_fragment_count": len(fragment_rows),
                "release_asset_count": len(release_assets),
            },
            candidates=ordered_candidates,
            reference_graph_sha256=graph_hash,
        )

    def discover_gateway_applications(
        self, gateway_profile_id: UUID
    ) -> GatewayApplicationDiscovery:
        api_rows = self._list_all(
            "/apis?includeRevisions=true&expandApiVersionSet=true"
        )
        api_ids = {
            self._api_name(self._resource_path(value))
            for value in api_rows
        }
        product_ids = {
            self._resource_name(value) for value in self._list_all("/products")
        }
        system_ids = {"master", self._settings.apim_probe_subscription_id}
        agent_ids = {
            value.casefold()
            for value in self._settings.apim_subscription_agent_map
        }
        items: list[GatewayApplicationDiscoveryItem] = []
        for subscription in self._list_all("/subscriptions"):
            apim_subscription_id = self._resource_name(subscription)
            raw_properties = subscription.get("properties")
            properties = raw_properties if isinstance(raw_properties, Mapping) else {}
            display_name = str(properties.get("displayName") or apim_subscription_id)
            raw_state = str(properties.get("state") or "suspended")
            state = {
                "active": "active",
                "suspended": "suspended",
                "submitted": "suspended",
                "cancelled": "cancelled",
                "rejected": "cancelled",
                "expired": "cancelled",
            }.get(raw_state)
            if state is None:
                raise PolicyCompilationError(
                    f"APIM subscription {apim_subscription_id} returned an invalid state"
                )
            scope = str(properties.get("scope") or "")
            if not scope:
                raise PolicyCompilationError(
                    f"APIM subscription {apim_subscription_id} returned no scope"
                )
            scope_path = self._resource_path({"id": scope}).rstrip("/")
            if "/products/" in scope_path:
                scope_type = "product"
                scope_id = url_unquote(scope_path.rsplit("/products/", 1)[-1])
                scope_exists = scope_id in product_ids
            elif "/apis/" in scope_path:
                scope_type = "api"
                raw_api_id = url_unquote(scope_path.rsplit("/apis/", 1)[-1])
                scope_id = raw_api_id.split(";rev=", 1)[0]
                scope_exists = scope_id in api_ids
            elif scope_path in {"", "/apis"}:
                scope_type = "service"
                scope_id = self._settings.apim_service_name or "apim-service"
                scope_exists = True
            else:
                raise PolicyCompilationError(
                    f"APIM subscription {apim_subscription_id} has an unsupported scope"
                )
            system_managed = apim_subscription_id in system_ids
            application_type: ApplicationType = (
                "system"
                if system_managed
                else "agent"
                if apim_subscription_id.casefold() in agent_ids
                else "service"
            )
            items.append(
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id=apim_subscription_id,
                    display_name=display_name,
                    state=cast(ApplicationSubscriptionState, state),
                    scope_type=cast(ApplicationScopeType, scope_type),
                    scope_id=scope_id,
                    scope_exists=scope_exists,
                    application_type=application_type,
                    system_managed=system_managed,
                )
            )
        return GatewayApplicationDiscovery(
            gateway_profile_id=gateway_profile_id,
            items=sorted(items, key=lambda item: item.apim_subscription_id),
            discovered_at=datetime.now(UTC),
        )

    def ensure_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None:
        if spec.provisioning_version == 2:
            self._validate_application_admission_target(spec)
        product_path = f"/products/{quote(spec.scope_id, safe='')}"
        product = self._request("GET", product_path, allow_not_found=True)
        if product.status_code == 404:
            raise PolicyCompilationError(
                f"APIM Product {spec.scope_id} does not exist"
            )
        if spec.provisioning_version == 2:
            properties = product.json().get("properties") or {}
            if properties.get("state") != "published" or properties.get("subscriptionRequired") is not True:
                raise PolicyCompilationError("Application Product must be published and require subscriptions")
            # Product Api exposes Check Entity Exists (HEAD), not an entity GET.
            membership = self._request(
                "HEAD", f"{product_path}/apis/{quote(self._settings.apim_api_id, safe='')}",
                allow_not_found=True,
            )
            if membership.status_code == 404:
                raise PolicyCompilationError("Application Product does not include the managed gateway API")
        subscription_path = (
            f"/subscriptions/{quote(spec.apim_subscription_id, safe='')}"
        )
        scope = f"{self._resource_id_base}/products/{spec.scope_id}"
        staged = spec.provisioning_version == 2
        expected_name = (
            self._application_pending_name(primary_key, secondary_key) if staged else spec.display_name
        )
        expected_state = "suspended" if staged else "active"
        observed = self._request("GET", subscription_path, allow_not_found=True)
        if observed.status_code != 404:
            raw_properties = observed.json().get("properties")
            properties = (
                raw_properties if isinstance(raw_properties, Mapping) else {}
            )
            if (
                str(properties.get("displayName") or "") == expected_name
                and str(properties.get("scope") or "").casefold()
                == scope.casefold()
                and str(properties.get("state") or "") == expected_state
            ):
                return
            raise PolicyCompilationError(
                f"APIM subscription {spec.apim_subscription_id} already exists"
            )
        created = self._request(
            "PUT",
            f"{subscription_path}?notify=false",
            json_body={
                "properties": {
                    "displayName": expected_name,
                    "scope": scope,
                    "state": expected_state,
                    "allowTracing": False,
                    "primaryKey": primary_key,
                    "secondaryKey": secondary_key,
                }
            },
        )
        raw_properties = created.json().get("properties")
        properties = raw_properties if isinstance(raw_properties, Mapping) else {}
        if (
            str(properties.get("displayName") or "") != expected_name
            or str(properties.get("scope") or "").casefold() != scope.casefold()
            or str(properties.get("state") or "") != expected_state
        ):
            raise RetryablePublicationError(
                "APIM subscription readback did not match the requested Application"
            )

    @staticmethod
    def _application_pending_name(primary_key: str, secondary_key: str) -> str:
        # Distinguish retries from an unrelated, same-name subscription without reading keys.
        marker = hashlib.sha256(f"{primary_key}\n{secondary_key}".encode()).hexdigest()[:32]
        return f"Turnstile pending {marker}"

    def _validate_application_admission_target(
        self, spec: GatewayApplicationSubscriptionProvisionSpec
    ) -> None:
        # A profile selector is not a dispatcher. Verify this worker's live API uses the
        # selected profile's ledger partition before making any subscription write.
        policy = self._policy_value(
            f"/apis/{quote(self._settings.apim_api_id, safe='')}/policies/policy?format=rawxml"
        )
        if policy is None:
            raise PolicyCompilationError("Managed gateway Application admission policy is unavailable")
        try:
            root = ElementTree.fromstring(policy)
        except ElementTree.ParseError as error:
            raise PolicyCompilationError("Managed gateway policy is not valid XML") from error
        partitions = [node.get("value") for node in root.findall(
            ".//set-variable[@name='applicationMapPartition']"
        )]
        identities = [node.get("value") for node in root.findall(
            ".//set-variable[@name='telemetryGatewayProfileId']"
        )]
        if (partitions != [f'@("app-map|{spec.gateway_profile_id}")']
                or identities != [str(spec.gateway_profile_id)]):
            raise PolicyCompilationError(
                "The selected gateway is not served by this worker's Application admission policy"
            )

    def activate_application_subscription(
        self,
        spec: GatewayApplicationSubscriptionProvisionSpec,
        primary_key: str,
        secondary_key: str,
    ) -> None:
        self._validate_application_admission_target(spec)
        path = f"/subscriptions/{quote(spec.apim_subscription_id, safe='')}"
        scope = f"{self._resource_id_base}/products/{spec.scope_id}"
        observed = self._request("GET", path)
        properties = observed.json().get("properties") or {}
        if str(properties.get("scope", "")).casefold() != scope.casefold():
            raise PolicyCompilationError("Application subscription scope changed before activation")
        if not (properties.get("displayName") == spec.display_name and properties.get("state") == "active"):
            if (properties.get("displayName") != self._application_pending_name(primary_key, secondary_key)
                    or properties.get("state") != "suspended"):
                raise PolicyCompilationError("Application subscription is not owned by this creation operation")
            etag = observed.headers.get("ETag")
            if not etag:
                raise RetryablePublicationError("APIM subscription ETag is unavailable")
            self._request(
                "PUT", f"{path}?notify=false",
                extra_headers={"If-Match": etag},
                json_body={"properties": {
                    "displayName": spec.display_name, "scope": scope, "state": "active",
                    "allowTracing": False, "primaryKey": primary_key, "secondaryKey": secondary_key,
                }},
            )
            readback = self._request("GET", path).json().get("properties") or {}
            if (readback.get("displayName") != spec.display_name or readback.get("state") != "active"
                    or str(readback.get("scope", "")).casefold() != scope.casefold()):
                raise RetryablePublicationError("APIM subscription activation is not yet visible")
        self._verify_application_subscription_authentication(primary_key)

    def _application_models_url(self) -> str:
        base = str(self._settings.apim_gateway_url).rstrip("/")
        parsed = urlparse(base)
        api_path = f"/apis/{quote(self._settings.apim_api_id, safe='')}"
        api = self._request("GET", api_path).json().get("properties") or {}
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path.strip("/") != str(api.get("path", "")).strip("/")):
            raise PolicyCompilationError("Application authentication probe gateway API path does not match")
        operations = self._request("GET", f"{api_path}/operations").json()
        catalogs = [item for item in operations.get("value", [])
                    if (item.get("properties") or {}).get("method") == "GET"
                    and (item.get("properties") or {}).get("urlTemplate") == "/v1/models"]
        if operations.get("nextLink") or len(catalogs) != 1:
            raise PolicyCompilationError("Application authentication probe requires GET /v1/models")
        operation_path = f"{api_path}/operations/{quote(catalogs[0]['name'], safe='')}"
        policy = self._policy_value(f"{operation_path}/policies/policy?format=rawxml")
        try:
            inbound = ElementTree.fromstring(policy or "").find("inbound")
        except ElementTree.ParseError as error:
            raise PolicyCompilationError("Application model catalog policy is not valid XML") from error
        bases = inbound.findall("base") if inbound is not None else []
        identity_variables = {
            "availabilitySurfaceDigest", "availabilityTargetId", "availabilityApiId",
            "availabilityApiRevision", "availabilitySurfaceKind", "availabilityCapabilityDigest",
        }
        if inbound is None or len(bases) != 1 or any(
            node.tag != "set-variable" or node.get("name") not in identity_variables
            for node in list(inbound)[:list(inbound).index(bases[0])]
        ):
            raise PolicyCompilationError("Application model catalog must inherit authentication before returning")
        return f"{base}/v1/models"

    @staticmethod
    def _application_auth_diagnostic(category: str, response: httpx.Response | None = None) -> str:
        correlation_id = None
        if response is not None:
            for name in ("apim-request-id", "x-correlation-id", "x-request-id"):
                value = response.headers.get(name, "")
                if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
                    correlation_id = value
                    break
        return "Application data-plane authentication: " + json.dumps({
            "category": category,
            "status_code": response.status_code if response is not None else None,
            "correlation_id": correlation_id,
        }, sort_keys=True)

    def _verify_application_subscription_authentication(self, primary_key: str) -> None:
        address = self._application_models_url()
        for authenticated in (True, False):
            headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
            if authenticated:
                headers["Ocp-Apim-Subscription-Key"] = primary_key
            request = httpx.Request(
                "GET", address, headers=headers,
                extensions={"timeout": httpx.Timeout(self._APPLICATION_AUTH_PROBE_TIMEOUT_SECONDS).as_dict()},
            )
            try:
                response = self._client.send(request, auth=None, follow_redirects=False, stream=True)
                try:
                    if authenticated and response.status_code != 200:
                        raise RetryablePublicationError(self._application_auth_diagnostic("subscription_not_ready", response))
                    if not authenticated:
                        if response.status_code == 200:
                            raise PolicyCompilationError(self._application_auth_diagnostic("anonymous_catalog_access", response))
                        if response.status_code not in {401, 403}:
                            raise RetryablePublicationError(self._application_auth_diagnostic("authentication_control_inconclusive", response))
                finally:
                    response.close()
            except httpx.TransportError as error:
                category = "timeout" if isinstance(error, httpx.TimeoutException) else "transport_error"
                raise RetryablePublicationError(self._application_auth_diagnostic(category)) from None

    def _managed_operation_ids(self, *, include_images: bool = False) -> tuple[str, ...]:
        return (
            self._settings.apim_chat_completions_operation_id,
            self._settings.apim_responses_operation_id,
            self._settings.apim_responses_compact_operation_id,
            self._settings.apim_messages_operation_id,
            self._settings.apim_count_tokens_operation_id,
            self._settings.apim_models_operation_id,
            *(("images-generations",) if include_images else ()),
        )

    @staticmethod
    def _string_list(value: object) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    def _revision_path(self, revision: str) -> str:
        return (
            f"/apis/{quote(self._settings.apim_api_id, safe='')};rev="
            f"{quote(revision, safe='')}"
        )

    def _pool_baseline_backend_ids(self, revision: str, api_format: ApiFormat) -> set[str]:
        revision_path = self._revision_path(revision)
        route = "chat/completions" if api_format is ApiFormat.OPENAI_CHAT else "v1/messages"
        operation = next(
            (
                item
                for item in self._list_all(f"{revision_path}/operations")
                if item.get("properties", {}).get("method") == "POST"
                and str(item.get("properties", {}).get("urlTemplate", "")).strip("/") == route
            ),
            None,
        )
        if operation is None:
            return set()
        policy = self._policy_value(
            f"{revision_path}/operations/{quote(str(operation['name']), safe='')}"
            "/policies/policy?format=rawxml"
        )
        if policy is None:
            return set()
        return {
            backend_id
            for node in ElementTree.fromstring(policy).iter("set-backend-service")
            if (backend_id := node.get("backend-id")) is not None
        }

    def _audit_parent_policy(
        self, path: str, before_request: Callable[[], None],
    ) -> str | None:
        before_request()
        before = self._request("HEAD", path, allow_not_found=True)
        if before.status_code == 404:
            return None
        etag = before.headers.get("ETag", "")
        if not re.fullmatch(r'"[A-Za-z0-9+/=]{1,160}"', etag):
            raise PolicyCompilationError("APIM parent audit version is invalid")
        before_request()
        response = self._request("GET", path.removesuffix("/policy") + "?format=xml")
        payload = response.json()
        rows = payload.get("value") if isinstance(payload, dict) else None
        if (not isinstance(rows, list) or len(rows) != 1
                or payload.get("nextLink") not in (None, "") or not isinstance(rows[0], dict)):
            raise PolicyCompilationError("APIM parent audit inventory is incomplete")
        row = rows[0]
        properties = row.get("properties")
        if (row.get("id") != self._resource_id_base + url_unquote(path)
                or row.get("name") != "policy" or not isinstance(properties, dict)
                or properties.get("format") != "xml"
                or not isinstance(properties.get("value"), str)
                or not properties["value"] or len(properties["value"].encode()) > 1048576):
            raise PolicyCompilationError("APIM parent audit identity is invalid")
        try:
            parse_policy(properties["value"])
        except (ValueError, ElementTree.ParseError):
            raise PolicyCompilationError("APIM parent audit XML is invalid") from None
        _, policy = self._read_policy(path, before_request=before_request)
        if len(policy.encode()) > 1048576:
            raise PolicyCompilationError("APIM parent audit XML exceeds its size limit")
        before_request()
        after = self._request("HEAD", path)
        if after.headers.get("ETag") != etag:
            raise PolicyCompilationError("APIM parent audit version changed")
        return policy

    def _policy_value(
        self, path: str, *, record_probe_request: bool = False,
        before_request: Callable[[], None] | None = None,
    ) -> str | None:
        location = urlparse(path)
        api_audit = before_request is not None and location.path.startswith("/apis/")
        if (api_audit and before_request is not None and ";rev=" in location.path
                and location.path.endswith("/policies/policy")
                and "/operations/" not in location.path):
            return self._audit_parent_policy(location.path, before_request)
        response, value = self._read_policy(
            path, allow_not_found=True, before_request=before_request,
            record_probe_request=record_probe_request,
        )
        return None if response.status_code == 404 else value

    def _staged_availability_programs(
        self, store: TableStorageLedger, target_id: UUID,
        table_endpoint: str, table_name: str,
    ) -> tuple[ModelAvailabilityStagedProgram, ...]:
        key = ModelAvailabilityConfirmationPlan.context_key(
            self._resource_id_base, self._settings.apim_api_id,
            str(self._settings.apim_gateway_url), table_endpoint, table_name,
        )
        stored = store.read_versioned_entity(
            f"{AVAILABILITY_CONFIRMATION_PARTITION}|{target_id}", key,
        )
        if stored is None:
            return ()
        plan = ModelAvailabilityConfirmationPlan.from_entity(stored[0])
        if plan.row_key != key or plan.coverage.target_id != target_id:
            raise ModelAvailabilityUnavailable("Staged program approval belongs to another target")
        return plan.staged_programs

    def _verify_staged_availability_program(
        self, staged: ModelAvailabilityStagedProgram,
    ) -> ProgramCapabilityProof:
        key = self._settings.apim_probe_subscription_key
        if key is None or staged.installation.publisher_subscription_id != (
            self._settings.apim_probe_subscription_id
        ):
            raise ModelAvailabilityUnavailable("Staged program probe identity changed")
        return FixedProgramVerifier(
            staged.installation, self, key, self._client,
            expected_current_revision=staged.expected_current_revision,
        ).verify()

    def verify_model_availability(
        self, target_id: UUID, inventory_digest: str,
        deployments: Sequence[Mapping[str, Any]], table_endpoint: str, table_name: str,
        state_reader: Callable[[UUID], ModelAvailabilityState | None],
        *, node_enrollment_reader: Callable[[], Sequence[ModelAvailabilityNodeEnrollment]] | None = None,
        state_batch_reader: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]]
        | None = None,
        before_proof: Callable[[], None],
        staged_programs: tuple[ModelAvailabilityStagedProgram, ...] | None = None,
    ) -> ModelAvailabilityCoverage:
        def list_resources(path: str) -> list[dict[str, Any]]:
            return self._list_all(path, max_items=2000, max_pages=20,
                                  before_request=_availability_management_reads.wait)

        def read_policy(path: str) -> str | None:
            return self._policy_value(path, before_request=_availability_management_reads.wait)

        with TableStorageLedger(
            table_endpoint, table_name, token_provider=self._token_provider, timeout=2,
        ) as store:
            verifier = ModelAvailabilityGatewayVerifier(
                target_id=target_id, api_id=self._settings.apim_api_id,
                gateway_url=str(self._settings.apim_gateway_url),
                table_endpoint=table_endpoint, table_name=table_name,
                key=self._settings.apim_probe_subscription_key,
                list_resources=list_resources,
                read_policy=read_policy, client=self._client, state_reader=state_reader,
                node_enrollment_reader=node_enrollment_reader, node_store=store,
                resource_id=self._resource_id_base,
                state_batch_reader=state_batch_reader,
                before_proof=before_proof,
                staged_programs=staged_programs if staged_programs is not None else (
                    self._staged_availability_programs(
                    store, target_id, table_endpoint, table_name,
                    )
                ),
                staged_program_verifier=self._verify_staged_availability_program,
            )
            coverage = verifier.verify(inventory_digest, deployments)
            plan = verifier.confirmation_plan
            if plan is None:
                raise ModelAvailabilityUnavailable("Verified confirmation plan is missing")
            self._save_availability_plan(store, plan, full_audit=True)
            return coverage

    @staticmethod
    def _save_availability_plan(
        store: TableStorageLedger, plan: ModelAvailabilityConfirmationPlan,
        *, full_audit: bool = False,
    ) -> None:
        plan = ModelAvailabilityConfirmationPlan.model_validate(plan.model_dump())
        current = store.read_versioned_entity(plan.partition_key, plan.row_key)
        if current is not None:
            try:
                previous = ModelAvailabilityConfirmationPlan.from_entity(current[0])
            except (ValidationError, ModelAvailabilityUnavailable):
                if not full_audit:
                    raise ModelAvailabilityUnavailable("Invalid previous confirmation requires a full audit") from None
                previous = None
            if (
                previous is not None and previous.management_verified_at is not None
                and (plan.management_verified_at is None
                     or previous.management_verified_at > plan.management_verified_at)
            ) or (
                previous is not None and previous.management_verified_at == plan.management_verified_at
                and previous.coverage.verified_at > plan.coverage.verified_at
            ):
                raise ModelAvailabilityUnavailable("Confirmation plan advanced during audit")
        if not store.replace_if_match(
            plan.partition_key, plan.row_key, plan.entity(), current[1] if current else None,
        ):
            raise ModelAvailabilityUnavailable("Confirmation plan write lost its version")
        observed = store.read_versioned_entity(plan.partition_key, plan.row_key)
        if observed is None or ModelAvailabilityConfirmationPlan.from_entity(observed[0]) != plan:
            raise ModelAvailabilityUnavailable("Confirmation plan changed after audit")

    def renew_model_availability(
        self, target_id: UUID, inventory_digest: str, deployments: Sequence[Mapping[str, Any]],
        table_endpoint: str, table_name: str,
        read_states: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]],
        *, node_enrollment_reader: Callable[[], Sequence[ModelAvailabilityNodeEnrollment]],
    ) -> ModelAvailabilityCoverage:
        key = ModelAvailabilityConfirmationPlan.context_key(
            self._resource_id_base, self._settings.apim_api_id,
            str(self._settings.apim_gateway_url), table_endpoint, table_name,
        )
        with TableStorageLedger(
            table_endpoint, table_name, token_provider=self._token_provider, timeout=2,
        ) as store:
            stored = store.read_versioned_entity(f"{AVAILABILITY_CONFIRMATION_PARTITION}|{target_id}", key)
            if stored is None:
                raise ModelAvailabilityUnavailable("Verified confirmation plan is unavailable")
            plan = ModelAvailabilityConfirmationPlan.from_entity(stored[0])
            verifier = ModelAvailabilityGatewayVerifier(
                resource_id=self._resource_id_base, target_id=target_id,
                api_id=self._settings.apim_api_id, gateway_url=str(self._settings.apim_gateway_url),
                table_endpoint=table_endpoint, table_name=table_name,
                key=self._settings.apim_probe_subscription_key,
                list_resources=lambda _: [], read_policy=lambda _: None,
                client=self._client, state_reader=lambda model_id: read_states([model_id]).get(model_id),
                node_enrollment_reader=node_enrollment_reader, node_store=store,
                staged_program_verifier=self._verify_staged_availability_program,
            )
            coverage = verifier.renew(plan, inventory_digest, deployments, read_states)
            renewed = verifier.confirmation_plan
            if renewed is None:
                raise ModelAvailabilityUnavailable("Renewed confirmation plan is missing")
            self._save_availability_plan(store, renewed)
            return coverage

    def confirm_model_availability(
        self, target_id: UUID, projected: ModelAvailabilityState, coverage: ModelAvailabilityCoverage,
        deployments: Sequence[Mapping[str, Any]], table_endpoint: str, table_name: str,
        read_states: Callable[[Sequence[UUID]], dict[UUID, ModelAvailabilityState]],
        *, node_enrollment_reader: Callable[[], Sequence[ModelAvailabilityNodeEnrollment]],
    ) -> ModelAvailabilityTargetResult:
        if coverage.target_id != target_id or not coverage.current(datetime.now(UTC), coverage.inventory_digest):
            raise ModelAvailabilityUnavailable("Availability coverage is stale or belongs to another target")
        key = ModelAvailabilityConfirmationPlan.context_key(
            self._resource_id_base, self._settings.apim_api_id,
            str(self._settings.apim_gateway_url), table_endpoint, table_name,
        )
        with TableStorageLedger(
            table_endpoint, table_name, token_provider=self._token_provider, timeout=2,
        ) as store:
            stored = store.read_versioned_entity(f"{AVAILABILITY_CONFIRMATION_PARTITION}|{target_id}", key)
            if stored is None:
                raise ModelAvailabilityUnavailable("Verified confirmation plan is unavailable")
            plan = ModelAvailabilityConfirmationPlan.from_entity(stored[0])
            if (
                plan.coverage.policy_digest != coverage.policy_digest
                or plan.coverage.inventory_digest != coverage.inventory_digest
                or set(plan.coverage.serving_surfaces) != set(coverage.serving_surfaces)
            ):
                raise ModelAvailabilityUnavailable("Confirmation plan does not match current coverage")
            return ModelAvailabilityGatewayVerifier(
                resource_id=self._resource_id_base, target_id=target_id,
                api_id=self._settings.apim_api_id, gateway_url=str(self._settings.apim_gateway_url),
                table_endpoint=table_endpoint, table_name=table_name,
                key=self._settings.apim_probe_subscription_key,
                list_resources=lambda _: [], read_policy=lambda _: None,
                client=self._client, state_reader=lambda model_id: read_states([model_id]).get(model_id),
                node_enrollment_reader=node_enrollment_reader, node_store=store,
                staged_program_verifier=self._verify_staged_availability_program,
            ).confirm(plan, projected, coverage.inventory_digest, deployments, read_states)

    def _list_all(
        self, path: str, *, max_items: int | None = None, max_pages: int | None = None,
        before_request: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        if before_request is not None:
            before_request()
        response = self._request("GET", path)
        rows: list[dict[str, Any]] = []
        pages = 0
        seen_links: set[str] = set()
        while True:
            pages += 1
            payload = response.json()
            values = payload.get("value")
            if not isinstance(values, list):
                raise PolicyCompilationError(f"APIM list {path} returned no value array")
            rows.extend(value for value in values if isinstance(value, dict))
            if max_items is not None and (len(rows) > max_items or len(values) != sum(
                isinstance(value, dict) for value in values
            )):
                raise PolicyCompilationError("APIM bounded inventory is incomplete or too large")
            next_link = payload.get("nextLink")
            if not next_link:
                return rows
            if not isinstance(next_link, str):
                raise PolicyCompilationError("APIM returned an untrusted pagination URL")
            if (max_pages is not None and pages >= max_pages) or next_link in seen_links:
                raise PolicyCompilationError("APIM bounded pagination did not complete")
            seen_links.add(next_link)
            parsed = urlparse(next_link)
            try:
                port = parsed.port
            except ValueError as error:
                raise PolicyCompilationError(
                    "APIM returned an untrusted pagination URL"
                ) from error
            decoded_path = url_unquote(parsed.path)
            path_segments = decoded_path.split("/")
            if (
                parsed.scheme != "https"
                or parsed.hostname != "management.azure.com"
                or port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or any(segment in {".", ".."} for segment in path_segments)
                or not decoded_path.casefold().startswith(
                    f"{self._resource_id_base}/".casefold()
                )
            ):
                raise PolicyCompilationError("APIM returned an untrusted pagination URL")
            if before_request is not None:
                before_request()
            response = self._client.get(
                next_link,
                headers=self._headers(),
                timeout=self._MANAGEMENT_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryablePublicationError(
                    f"APIM pagination returned HTTP {response.status_code}"
                )
            response.raise_for_status()

    def _resource_path(self, value: Mapping[str, Any]) -> str:
        resource_id = str(value.get("id", ""))
        if not resource_id.startswith(self._resource_id_base):
            raise PolicyCompilationError("APIM returned a resource outside the service")
        return resource_id[len(self._resource_id_base) :]

    @staticmethod
    def _resource_name(value: Mapping[str, Any]) -> str:
        resource_id = str(value.get("id", ""))
        if not resource_id:
            raise PolicyCompilationError("APIM returned a resource without an ID")
        return resource_id.rsplit("/", 1)[-1]

    @staticmethod
    def _api_name(resource_path: str) -> str:
        return resource_path.split("/apis/", 1)[-1].split(";rev=", 1)[0]

    def _collect_api_policy_evidence(
        self, resource_path: str, evidence: list[tuple[str, str]]
    ) -> None:
        policy = self._policy_value(f"{resource_path}/policies/policy")
        if policy is not None:
            evidence.append((resource_path, policy))
        for operation in self._list_all(f"{resource_path}/operations"):
            operation_path = self._resource_path(operation)
            operation_policy = self._policy_value(
                f"{operation_path}/policies/policy"
            )
            if operation_policy is not None:
                evidence.append((operation_path, operation_policy))
