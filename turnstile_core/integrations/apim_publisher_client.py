# APIM response and policy literals are intentionally kept on protocol-shaped lines.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from html import unescape
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, quote, urlparse
from urllib.parse import unquote as url_unquote
from uuid import UUID
from xml.etree import ElementTree

import httpx

from ..config import Settings
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
    ApiFormat,
    AuthStrategy,
    GatewayModelBinding,
    GatewayPublication,
    GatewayReleaseDependencies,
    PublicationKind,
)
from ..domain.image_profiles import ImageGenerationProfile, validate_image_profile
from ..integrations.reconciliation import ManagedIdentityTokenProvider
from .apim_control_plane_contract import (
    AuthorizationRequiredError,
    BackendCircuitBreakerResource,
    BackendPoolResource,
    BackendResource,
    ImageProbeJournal,
    InfrastructureUpgradeRequiredError,
    NamedValueResource,
    OAuthCredentialResource,
    OperationResource,
    PolicyCompilationError,
    ReleaseGcPlanEvidence,
    RetryablePublicationError,
)
from .apim_policy_compiler import ApimPolicyCompiler
from .apim_policy_components import parent_readback_matches


class AzureApimPublisherClient:
    _MANAGEMENT_ORIGIN = "https://management.azure.com"
    _MANAGEMENT_RESOURCE = f"{_MANAGEMENT_ORIGIN}/"
    _API_VERSION = "2024-05-01"
    _AFFINITY_API_VERSION = "2025-09-01-preview"
    _MANAGEMENT_REQUEST_TIMEOUT_SECONDS = 30.0
    _CANDIDATE_PROBE_TIMEOUT_SECONDS = 120.0
    _APPLICATION_AUTH_PROBE_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        settings: Settings,
        token_provider: ManagedIdentityTokenProvider | None = None,
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
            "apim-probe-journal",
            default=None,
        )
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client or httpx.Client(
            timeout=self._MANAGEMENT_REQUEST_TIMEOUT_SECONDS
        )
        self._resource_id_base = (
            f"/subscriptions/{settings.azure_subscription_id}"
            f"/resourceGroups/{settings.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{settings.apim_service_name}"
        )
        self._base = f"{self._MANAGEMENT_ORIGIN}{self._resource_id_base}"

    def _headers(self) -> dict[str, str]:
        token = self._token_provider.token(self._MANAGEMENT_RESOURCE)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

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
        )
        if allow_not_found and response.status_code == 404:
            return response
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryablePublicationError(
                f"APIM management request returned HTTP {response.status_code}"
            )
        if response.is_error:
            diagnostic: dict[str, Any] = {
                "method": method,
                "path": resource_path,
                "status": response.status_code,
            }
            try:
                payload = response.json()
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                if isinstance(error, dict):
                    code = error.get("code")
                    if isinstance(code, str) and re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_.]{0,127}", code
                    ):
                        diagnostic["code"] = code
            except ValueError:
                pass
            request_id = response.headers.get("x-ms-request-id", "")
            if re.fullmatch(r"[a-fA-F0-9-]{1,128}", request_id):
                diagnostic["request_id"] = request_id
            raise PolicyCompilationError(
                "APIM management failed: " + json.dumps(diagnostic, sort_keys=True)
            )
        return response

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
                readback_properties = readback.json().get("properties")
                if not isinstance(readback_properties, Mapping) or not self._backend_identity_matches(
                    backend, readback_properties
                ):
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
                        "interval": condition.get("interval"),
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
                    "tripDuration": raw_rule.get("tripDuration"),
                    "acceptRetryAfter": bool(raw_rule.get("acceptRetryAfter", False)),
                }
            )
        return {"rules": sorted(normalized, key=lambda item: str(item.get("name")))}

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
            or self._settings.apim_principal_id and principal_id != self._settings.apim_principal_id
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
                "parameters": {"clientId": str(config.client_id), "clientSecret": credential.client_secret},
            }}, extra_headers=(
                {"If-None-Match": "*"} if authorization.status_code == 404
                else {"If-Match": authorization.headers.get("etag", "*")}
            ))
        access_path = authorization_path + f"/accessPolicies/{identity['objectId']}"
        access = self._request("GET", access_path, allow_not_found=True)
        if access.status_code == 404:
            self._request(
                "PUT", access_path, json_body={"properties": identity},
                extra_headers={"If-None-Match": "*"},
            )
        issues = self.oauth_credential_issues(credential)
        if issues:
            raise PolicyCompilationError("OAuth credential readback failed: " + ", ".join(issues))

    def ensure_named_value(self, named_value: NamedValueResource) -> None:
        path = f"/namedValues/{quote(named_value.id, safe='')}"
        observed = self._request("GET", path, allow_not_found=True)
        if observed.status_code != 404:
            properties = observed.json().get("properties") or {}
            observed_secret = (properties.get("keyVault") or {}).get("secretIdentifier")
            if (
                named_value.key_vault_secret_id is not None
                and observed_secret != named_value.key_vault_secret_id
            ):
                raise PolicyCompilationError(
                    f"Named value {named_value.id} points to a different Key Vault secret"
                )
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
            return
        if named_value.key_vault_secret_id is None and named_value.value is None:
            raise PolicyCompilationError(
                f"Named value {named_value.id} has no credential source"
            )
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
        )

    def ensure_operation(self, revision: str, operation: OperationResource) -> None:
        path = self._revision_path(revision) + "/operations/" + quote(operation.id, safe="")
        observed = self._request("GET", path, allow_not_found=True)
        if observed.status_code == 404:
            raise InfrastructureUpgradeRequiredError(
                f"APIM infrastructure upgrade required: operation {operation.id} is missing. "
                "Run scripts.deploy plan-upgrade and upgrade before publishing image models."
            )
        properties = observed.json().get("properties") or {}
        if (
            properties.get("method") != operation.method
            or properties.get("urlTemplate") != operation.path
            or properties.get("templateParameters", [])
        ):
            raise PolicyCompilationError("Operation route does not match its compiled contract")

    def put_operation_policy(self, revision: str, operation: str, policy: str) -> None:
        self._put_policy(
            f"/apis/{quote(self._settings.apim_api_id, safe='')};rev={quote(revision, safe='')}"
            f"/operations/{quote(operation, safe='')}/policies/policy",
            policy,
        )

    def _put_policy(self, path: str, policy: str) -> None:
        self._request(
            "PUT",
            path,
            json_body={"properties": {"format": "rawxml", "value": policy}},
            extra_headers={"If-Match": "*"},
        )

    def probe_revision(
        self,
        revision: str,
        publication: GatewayPublication,
        *,
        journal: ImageProbeJournal | None = None,
    ) -> None:
        token = self._probe_journal.set(journal)
        try:
            self._probe_revision(revision, publication)
        finally:
            self._probe_journal.reset(token)

    def _probe_heartbeat(self) -> None:
        journal = self._probe_journal.get()
        if journal is not None:
            journal.heartbeat()

    def _probe_revision(self, revision: str, publication: GatewayPublication) -> None:
        if not self._settings.apim_probe_subscription_key:
            raise RuntimeError("APIM_PROBE_SUBSCRIPTION_KEY is required for verification")
        base = str(self._settings.apim_gateway_url).rstrip("/")
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
            "x-hive-run-id": "gateway-publication-probe",
            "x-hive-turn-index": "1",
            "x-hive-model": "gateway-publication-probe",
            "x-hive-runtime": "gateway-publication-probe",
        }
        revision_path = f"{base};rev={quote(revision, safe='')}"
        base_revision = publication.resource_manifest.get("base_apim_revision")
        baseline_path = (
            f"{base};rev={quote(str(base_revision), safe='')}"
            if base_revision and str(base_revision) != revision
            else None
        )
        self._probe_heartbeat()
        models = self._client.get(f"{revision_path}/v1/models", headers=headers)
        if self._is_transient_probe_status(models.status_code):
            raise RetryablePublicationError(
                f"Candidate discovery returned HTTP {models.status_code}"
            )
        models.raise_for_status()
        discovered = {str(item.get("id")) for item in (models.json().get("data") or [])}
        expected = {item.id for item in publication.desired_spec.discovery_models}
        if not expected.issubset(discovered):
            raise RetryablePublicationError(
                "Candidate revision model discovery is incomplete"
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

        regression = next(
            (
                item
                for item in publication.desired_spec.discovery_models
                if item.api_format is not ApiFormat.OPENAI_IMAGES
                if item.id.casefold() == self._settings.apim_regression_model_key.casefold()
            ),
            next(
                (
                    item
                    for item in publication.desired_spec.discovery_models
                    if item.api_format is not ApiFormat.OPENAI_IMAGES
                ),
                None,
            ),
        )
        if regression is not None:
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
                self._probe_model(
                    revision_path,
                    headers,
                    regression.id,
                    api_format=regression.api_format,
                    max_tokens_field=str(
                        regression_binding.runtime_config.get(
                            "max_tokens_field", "max_completion_tokens"
                        ) if regression_binding is not None else "max_completion_tokens"
                    ),
                    baseline_base=(
                        baseline_path
                        if target_binding is None or regression_binding is not target_binding
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
            self._probe_removed_model(
                revision_path,
                headers,
                target.model_key,
                target.api_format,
            )
        else:
            binding = publication.desired_spec.bindings[-1]
            if (
                binding.runtime_config.get("apim_backend_pool") is None
                and binding.api_format is not ApiFormat.OPENAI_IMAGES
            ):
                self._probe_model(
                    revision_path,
                    headers,
                    binding.model.model_key,
                    api_format=binding.api_format,
                    max_tokens_field=str(
                        binding.runtime_config.get("max_tokens_field", "max_completion_tokens")
                    ),
                    retry_assignment_denial=binding is target_binding,
                    baseline_base=baseline_path if binding is not target_binding else None,
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
            ]
            for responses_binding in responses_bindings:
                if (
                    responses_binding.runtime_config.get("apim_backend_pool")
                    is not None
                ):
                    continue
                self._probe_responses_model(
                    revision_path,
                    headers,
                    responses_binding.model.model_key,
                    retry_assignment_denial=responses_binding is target_binding,
                    baseline_base=(
                        baseline_path if responses_binding is not target_binding else None
                    ),
                    authorization_required=(
                        responses_binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY
                        and isinstance(
                            responses_binding.runtime_config.get("authorization"), dict
                        )
                    ),
                )
                self._probe_responses_compact_model(
                    revision_path,
                    headers,
                    responses_binding.model.model_key,
                    retry_assignment_denial=responses_binding is target_binding,
                    baseline_base=(
                        baseline_path if responses_binding is not target_binding else None
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
            for pooled_binding in publication.desired_spec.bindings:
                pool = pooled_binding.backend_pool
                if pool is None:
                    continue
                if (
                    baseline_path is not None
                    and publication.base_release_id is not None
                    and pooled_binding.api_format not in baseline_pool_ids
                ):
                    baseline_pool_ids[pooled_binding.api_format] = self._pool_baseline_backend_ids(
                        str(base_revision), pooled_binding.api_format
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
                        **headers,
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
                        baseline_publication = publication.model_copy(
                            update={"id": publication.base_release_id}
                        )
                        affinity_binding = pooled_binding.model_copy(update={"runtime_config": {
                            **pooled_binding.runtime_config,
                            "apim_backend_pool_session_affinity": True,
                        }})
                        baseline_member_id = next((
                            candidate for candidate in (
                                pool_compiler.pool_member_backend_id(
                                    baseline_publication, affinity_binding, member
                                ),
                                ApimPolicyCompiler._pool_member_backend_id(
                                    baseline_publication, pooled_binding, member
                                ),
                            )
                            if candidate in baseline_pool_ids[pooled_binding.api_format]
                        ), None)
                        if baseline_member_id is not None:
                            baseline_member_headers = {
                                **headers, "x-turnstile-pool-member": baseline_member_id,
                            }
                    member_baseline = (
                        baseline_path if baseline_member_headers is not None
                        and pooled_binding is not target_binding else None
                    )
                    self._probe_model(
                        revision_path,
                        member_headers,
                        pooled_binding.model.model_key,
                        api_format=pooled_binding.api_format,
                        max_tokens_field=str(
                            pooled_binding.runtime_config.get(
                                "max_tokens_field", "max_completion_tokens"
                            )
                        ),
                        baseline_base=member_baseline,
                        baseline_headers=baseline_member_headers,
                        authorization_required=authorization_required,
                    )
                    if not supports_responses:
                        continue
                    self._probe_responses_model(
                        revision_path,
                        member_headers,
                        pooled_binding.model.model_key,
                        baseline_base=member_baseline,
                        baseline_headers=baseline_member_headers,
                        authorization_required=authorization_required,
                    )
                    self._probe_responses_compact_model(
                        revision_path,
                        member_headers,
                        pooled_binding.model.model_key,
                        baseline_base=member_baseline,
                        baseline_headers=baseline_member_headers,
                        authorization_required=authorization_required,
                    )

        for image_binding in publication.desired_spec.bindings:
            if image_binding.api_format is not ApiFormat.OPENAI_IMAGES:
                continue
            profile = validate_image_profile(image_binding.model.image_profile)
            journal = self._probe_journal.get()
            if journal is None:
                raise PolicyCompilationError(
                    "Image verification requires a persistent probe journal"
                )
            body = self._image_probe_body(image_binding.model.model_key, profile)
            journal.run(
                image_binding,
                revision,
                body,
                partial(
                    self._send_bound_image_probe,
                    revision_path,
                    headers,
                    image_binding,
                    profile,
                ),
            )

    def _send_bound_image_probe(
        self,
        base: str,
        headers: dict[str, str],
        binding: GatewayModelBinding,
        profile: ImageGenerationProfile,
        request_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        return self._probe_image_model(
            base,
            {
                **headers,
                "x-request-id": request_id,
                "x-model-id": model_id,
                "x-hive-model": model_id,
            },
            binding.model.model_key,
            profile=profile,
            authorization_required=binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY,
        )

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
            raise PolicyCompilationError(
                "Image probes require an explicit profile and persistent journal"
            )
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
        self._probe_heartbeat()
        response = self._client.post(
            f"{base}{path}",
            headers=headers,
            json=body,
            timeout=self._CANDIDATE_PROBE_TIMEOUT_SECONDS,
        )
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
        self,
        base: str,
        headers: dict[str, str],
        model: str,
        *,
        profile: ImageGenerationProfile,
        authorization_required: bool = False,
    ) -> dict[str, Any]:
        from .image_generation import generated_image, image_usage

        profile = validate_image_profile(profile)
        body = self._image_probe_body(model, profile)
        evidence: dict[str, Any] = {"image_validated": False}
        try:
            with self._client.stream(
                "POST",
                base + profile.public_path,
                headers=headers,
                json=body,
                timeout=profile.timeout_seconds,
            ) as response:
                evidence.update(
                    status_code=response.status_code,
                    correlation_id=response.headers.get("x-correlation-id"),
                )
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
            evidence.update(
                total_tokens=usage.input_tokens + usage.cached_tokens + usage.output_tokens,
                usage=usage.model_dump(mode="json"),
            )
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
                    "The image provider has not authorized APIM", billable_outcome=outcome
                ) from error
            raise PolicyCompilationError(
                "Image probe did not return a complete measured image; no automatic retry",
                billable_outcome=outcome,
            ) from error
        return evidence

    @staticmethod
    def _image_probe_body(model: str, profile: ImageGenerationProfile) -> dict[str, Any]:
        validate_image_profile(profile)
        return {
            "model": model,
            "prompt": "A blue ceramic cup on a white table.",
            "n": 1,
            "stream": False,
        }

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
        self._probe_heartbeat()
        response = self._client.post(
            f"{base}/responses",
            headers=headers,
            json=body,
            timeout=self._CANDIDATE_PROBE_TIMEOUT_SECONDS,
        )
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
        body = {"model": model, "input": "Reply only with OK."}
        self._probe_heartbeat()
        response = self._client.post(
            f"{base}/responses/compact",
            headers=headers,
            json=body,
            timeout=self._CANDIDATE_PROBE_TIMEOUT_SECONDS,
        )
        if self._matches_baseline_failure(
            response,
            baseline_url=f"{baseline_base}/responses/compact" if baseline_base else None,
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

    def _matches_baseline_failure(
        self,
        response: httpx.Response,
        *,
        baseline_url: str | None,
        headers: dict[str, str],
        body: dict[str, Any],
        label: str,
        model: str,
    ) -> bool:
        if (
            baseline_url is None
            or not 400 <= response.status_code < 500
            or self._is_transient_probe_status(response.status_code)
        ):
            return False
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
                "model": model,
                "prompt": "A blue cup.",
                "size": "1024x1024",
                "quality": "low",
                "n": 1,
                "output_format": "png",
                "stream": False,
            }
        elif api_format is ApiFormat.OPENAI_CHAT:
            body["max_completion_tokens"] = 8
        else:
            body["max_tokens"] = 8
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
            return error.get("type") == "model_not_assigned"
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
        value = self._policy_value(self._revision_path(revision) + "/policies/policy?format=rawxml")
        if value is None:
            raise PolicyCompilationError("The APIM revision parent policy is missing")
        return value

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
        backends = sorted(value for value in backend_ids if value not in pools)
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
                    f"{revision_path}/policies/policy"
                    + ("?format=rawxml" if expected_contract else "")
                )
                if parent is None:
                    issues.append("missing_parent_policy")
                else:
                    expected_parent_hash = (
                        manifest.get("parent_policy_sha256")
                        or manifest.get("base_policy_sha256")
                    )
                    if (
                        expected_parent_hash is not None or expected_contract is not None
                    ) and not parent_readback_matches(
                        parent,
                        expected_contract=expected_contract,
                        expected_raw=expected_parent_hash,
                    ):
                        issues.append("mismatched_parent_policy_sha256")
                operation_policies: list[str] = []
                managed_operations = self._managed_operation_ids(
                    include_images=bool(manifest.get("images_generations_operation"))
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
                    and expected_backend.session_cookie_name
                    else None
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
    ) -> ReleaseGcPlanEvidence:
        release_by_revision = {
            release.apim_revision: release
            for release in releases
            if release.apim_revision is not None
        }
        retained = [
            release for release in releases if str(release.id) in retained_release_ids
        ]
        protected_backend_ids: set[str] = set()
        protected_named_value_ids: set[str] = set()
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
        protected_revision_ids: set[str] = set()
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
            matched_release = (
                release_by_revision.get(revision) if is_managed_api else None
            )
            protect_policy = (
                not is_managed_api
                or revision == current_revision
                or matched_release is None
                or str(matched_release.id) in retained_release_ids
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
        # One listing resolves every subscription's ownerId to an email. Doing it per subscription
        # would be one call per key; an install with several hundred keys has far fewer users, and
        # most subscriptions have no owner at all.
        owner_emails: dict[str, str] = {}
        try:
            for user in self._list_all("/users"):
                raw_user_properties = user.get("properties")
                user_properties = (
                    raw_user_properties if isinstance(raw_user_properties, Mapping) else {}
                )
                email = str(user_properties.get("email") or "").strip()
                if email and "@" in email:
                    owner_emails[str(user.get("name") or "").casefold()] = email
        except Exception:
            # A gateway whose user list is not readable still discovers its subscriptions; those
            # keys simply arrive without an owner rather than failing the whole sync.
            owner_emails = {}

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
            owner_reference = str(properties.get("ownerId") or "").rsplit("/", 1)[-1]
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
                    owner_email=owner_emails.get(owner_reference.casefold()),
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
        staged = spec.provisioning_version == 2
        if staged:
            self._validate_application_admission_target(spec)
        product_path = f"/products/{quote(spec.scope_id, safe='')}"
        product = self._request("GET", product_path, allow_not_found=True)
        if product.status_code == 404:
            raise PolicyCompilationError(
                f"APIM Product {spec.scope_id} does not exist"
            )
        if staged:
            properties = product.json().get("properties") or {}
            if properties.get("state") != "published" or properties.get("subscriptionRequired") is not True:
                raise PolicyCompilationError("Application Product must be published and require subscriptions")
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
        expected_name = self._application_pending_name(primary_key, secondary_key) if staged else spec.display_name
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
        marker = hashlib.sha256(f"{primary_key}\n{secondary_key}".encode()).hexdigest()[:32]
        return f"Turnstile pending {marker}"

    def _validate_application_admission_target(
        self, spec: GatewayApplicationSubscriptionProvisionSpec
    ) -> None:
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
        if (
            partitions != [f'@("app-map|{spec.gateway_profile_id}")']
            or identities != [str(spec.gateway_profile_id)]
        ):
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
            if (
                properties.get("displayName") != self._application_pending_name(primary_key, secondary_key)
                or properties.get("state") != "suspended"
            ):
                raise PolicyCompilationError("Application subscription is not owned by this creation operation")
            etag = observed.headers.get("ETag")
            if not etag:
                raise RetryablePublicationError("APIM subscription ETag is unavailable")
            self._request(
                "PUT", f"{path}?notify=false", extra_headers={"If-Match": etag},
                json_body={"properties": {
                    "displayName": spec.display_name,
                    "scope": scope,
                    "state": "active",
                    "allowTracing": False,
                    "primaryKey": primary_key,
                    "secondaryKey": secondary_key,
                }},
            )
            readback = self._request("GET", path).json().get("properties") or {}
            if (
                readback.get("displayName") != spec.display_name or readback.get("state") != "active"
                or str(readback.get("scope", "")).casefold() != scope.casefold()
            ):
                raise RetryablePublicationError("APIM subscription activation is not yet visible")
        self._verify_application_subscription_authentication(primary_key)

    def _application_models_url(self) -> str:
        base = str(self._settings.apim_gateway_url).rstrip("/")
        parsed = urlparse(base)
        api_path = f"/apis/{quote(self._settings.apim_api_id, safe='')}"
        api = self._request("GET", api_path).json().get("properties") or {}
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path.strip("/") != str(api.get("path", "")).strip("/")
        ):
            raise PolicyCompilationError("Application authentication probe gateway API path does not match")
        operation_path = f"{api_path}/operations/{quote(self._settings.apim_models_operation_id, safe='')}"
        operation = self._request("GET", operation_path).json().get("properties") or {}
        if operation.get("method") != "GET" or operation.get("urlTemplate") != "/v1/models":
            raise PolicyCompilationError("Application authentication probe requires GET /v1/models")
        policy = self._policy_value(f"{operation_path}/policies/policy?format=rawxml")
        try:
            inbound = ElementTree.fromstring(policy or "").find("inbound")
        except ElementTree.ParseError as error:
            raise PolicyCompilationError("Application model catalog policy is not valid XML") from error
        if inbound is None or not len(inbound) or inbound[0].tag != "base":
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
        operation = next((
            item for item in self._list_all(f"{revision_path}/operations")
            if item.get("properties", {}).get("method") == "POST"
            and str(item.get("properties", {}).get("urlTemplate", "")).strip("/") == route
        ), None)
        if operation is None:
            return set()
        policy = self._policy_value(
            f"{revision_path}/operations/{quote(str(operation['name']), safe='')}"
            "/policies/policy?format=rawxml"
        )
        if policy is None:
            return set()
        return {
            backend_id for node in ElementTree.fromstring(policy).iter("set-backend-service")
            if (backend_id := node.get("backend-id")) is not None
        }

    def _policy_value(self, path: str) -> str | None:
        response = self._request("GET", path, allow_not_found=True)
        if response.status_code == 404:
            return None
        content_type = response.headers.get("content-type", "").casefold()
        if "json" in content_type:
            value = (response.json().get("properties") or {}).get("value")
        elif "format=rawxml" in path:
            value = response.text
        else:
            value = unescape(response.text)
        if not isinstance(value, str) or not value:
            raise PolicyCompilationError(f"APIM policy {path} returned no value")
        return value

    def _list_all(self, path: str) -> list[dict[str, Any]]:
        response = self._request("GET", path)
        rows: list[dict[str, Any]] = []
        while True:
            payload = response.json()
            values = payload.get("value")
            if not isinstance(values, list):
                raise PolicyCompilationError(f"APIM list {path} returned no value array")
            rows.extend(value for value in values if isinstance(value, dict))
            next_link = payload.get("nextLink")
            if not next_link:
                return rows
            if not isinstance(next_link, str):
                raise PolicyCompilationError("APIM returned an untrusted pagination URL")
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
