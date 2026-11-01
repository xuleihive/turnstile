from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid5
from xml.etree import ElementTree as ET

import httpx
from pydantic import HttpUrl, ValidationError

from ..config import Settings
from ..domain.gateway_targets import (
    ApimOnboardingConfiguration,
    ApimOnboardingPlan,
    ApimOnboardingRequest,
)
from ..domain.storage_routes import FixedProgramInstallation, document_digest
from .apim_control_plane_contract import (
    PUBLICATION_REQUIRED_ACTIONS,
    ManagementTokenProvider,
    PolicyCompilationError,
    RetryablePublicationError,
    missing_management_actions,
)
from .apim_onboarding_policy import onboarding_logger_id
from .apim_policy_components import (
    component_digest,
    encode_policy_xml,
    parse_policy,
    validate_policy_upload,
)
from .apim_publisher_client import AzureApimPublisherClient
from .apim_storage_routes import (
    FixedRouteBinding,
    FixedRouteProgram,
    fixed_operation_contracts,
    fixed_program_digest,
)
from .apim_target_client_factory import ApimTargetClientConfig

PLATFORM_GOVERNANCE_NAMED_VALUE = "turnstile-platform-governance"
PLATFORM_GOVERNANCE_TAG = "turnstile-platform-configuration"


class AzureApimPlatformConfigurationClient(AzureApimPublisherClient):
    def configuration(self) -> ApimOnboardingConfiguration:
        resource = f"/namedValues/{PLATFORM_GOVERNANCE_NAMED_VALUE}"
        response = self._request("GET", resource, allow_not_found=True)
        if response.status_code == 404:
            raise PolicyCompilationError(
                "The platform APIM governance configuration has not been provisioned"
            )
        value = response.json()
        properties = value.get("properties") or {}
        if (
            str(value.get("id", "")).casefold()
            != (self._resource_id_base + resource).casefold()
            or properties.get("displayName") != PLATFORM_GOVERNANCE_NAMED_VALUE
            or properties.get("secret") is not False
            or PLATFORM_GOVERNANCE_TAG not in properties.get("tags", [])
        ):
            raise PolicyCompilationError(
                "The platform governance configuration identity is invalid"
            )
        try:
            return ApimOnboardingConfiguration.model_validate_json(properties.get("value", ""))
        except (ValidationError, TypeError):
            raise PolicyCompilationError(
                "The platform governance configuration is invalid"
            ) from None

    def ensure_apim_dependency_roles(
        self,
        gateway_profile_id: UUID,
        principal_id: UUID,
        configuration: ApimOnboardingConfiguration,
    ) -> None:
        for scope, role in AzureApimOnboardingClient._dependency_roles(configuration):
            identifier = uuid5(
                gateway_profile_id,
                f"{scope.casefold()}:{principal_id}:{role}",
            )
            role_path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{identifier}"
            definition = (
                f"/subscriptions/{scope.split('/')[2]}/providers/Microsoft.Authorization/"
                f"roleDefinitions/{role}"
            )
            expected = {"principalId": str(principal_id), "roleDefinitionId": definition}
            current = self._client.request(
                "GET",
                f"{self._MANAGEMENT_ORIGIN}{role_path}",
                params={"api-version": "2022-04-01"},
                headers=self._headers(),
            )
            if current.status_code == 404:
                put_response = self._client.request(
                    "PUT",
                    f"{self._MANAGEMENT_ORIGIN}{role_path}",
                    params={"api-version": "2022-04-01"},
                    headers={**self._headers(), "If-None-Match": "*"},
                    json={
                        "properties": {
                            **expected,
                            "principalType": "ServicePrincipal",
                        }
                    },
                )
                if put_response.status_code == 409 and "RoleAssignmentExists" in put_response.text:
                    continue
                if put_response.status_code == 429 or put_response.status_code >= 500:
                    raise RetryablePublicationError(
                        "An initialization dependency is temporarily unavailable"
                    )
                if put_response.is_error:
                    raise PolicyCompilationError(
                        f"Initialization dependency returned HTTP {put_response.status_code}; "
                        "check the platform identity's scoped permissions"
                    )


def onboarding_configuration(request: ApimOnboardingRequest) -> ApimTargetClientConfig:
    settings = Settings.model_construct(
        apim_gateway_profile_id=request.gateway_profile_id,
        azure_subscription_id=str(request.subscription_id),
        apim_resource_group=request.resource_group,
        apim_service_name=request.service_name,
        apim_api_id=request.api_id,
        apim_gateway_url=f"https://{request.service_name.lower()}.azure-api.net/{request.api_path}",
        apim_probe_subscription_key=None,
        apim_probe_subscription_id=request.probe_subscription_id,
        apim_regression_model_key="",
        apim_usage_observer_url=None,
        apim_usage_observer_key_named_value=None,
        apim_subscription_agent_map={},
        apim_chat_completions_operation_id="chat-completions",
        apim_responses_operation_id="responses",
        apim_responses_compact_operation_id="responses-compact",
        apim_messages_operation_id="anthropic-messages",
        apim_count_tokens_operation_id="anthropic-count-tokens",
        apim_models_operation_id="anthropic-models",
    )
    return ApimTargetClientConfig(
        request.gateway_profile_id, "onboarding", True, settings,
    )


def onboarding_plan_digest(plan: ApimOnboardingPlan, configuration_digest: str) -> str:
    values = plan.model_dump(mode="json", exclude={"ready", "blockers", "confirmation_sha256"})
    values["configuration_sha256"] = configuration_digest
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AzureApimOnboardingClient(AzureApimPublisherClient):
    def __init__(
        self,
        request: ApimOnboardingRequest,
        *,
        token_provider: ManagementTokenProvider | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(onboarding_configuration(request).settings, token_provider, client)
        self.request = request

    @property
    def ownership_marker(self) -> str:
        return f"Turnstile managed APIM initialization v1: {self.request.gateway_profile_id}"

    @property
    def api_resource_path(self) -> str:
        return f"/apis/{quote(self.request.api_id, safe='')}"

    def _api_properties(self) -> dict[str, object]:
        return {
            "apiType": "http",
            "displayName": "Turnstile Model API",
            "description": self.ownership_marker,
            "path": self.request.api_path,
            "apiRevision": "1",
            "protocols": ["https"],
            "subscriptionRequired": True,
            "subscriptionKeyParameterNames": {
                "header": "Ocp-Apim-Subscription-Key",
                "query": "subscription-key",
            },
        }

    def _require_owned_api(self, value: Mapping[str, Any]) -> None:
        if (
            str(value.get("id", "")).casefold()
            != (self._resource_id_base + self.api_resource_path).casefold()
        ):
            raise PolicyCompilationError("APIM returned an unexpected API resource identity")
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            parameters = properties.get("subscriptionKeyParameterNames")
            if isinstance(parameters, Mapping) and parameters.get("bearer") is None:
                properties = {
                    **properties,
                    "subscriptionKeyParameterNames": {
                        name: parameter
                        for name, parameter in parameters.items()
                        if name != "bearer"
                    },
                }
        if (
            not isinstance(properties, Mapping)
            or any(
                properties.get(name) != expected
                for name, expected in self._api_properties().items()
                if name not in {"displayName", "apiType"}
            )
            or properties.get("type", properties.get("apiType", "http")) != "http"
            or properties.get("isCurrent", True) is not True
            or properties.get("serviceUrl")
        ):
            raise PolicyCompilationError(
                "The reserved API is unowned or its configuration has drifted"
            )

    def inspect(
        self,
        *,
        management_tenant_id: UUID,
        configuration_digest: str,
        configuration_blockers: tuple[str, ...] = (),
    ) -> ApimOnboardingPlan:
        if self.request.tenant_id != management_tenant_id:
            raise PolicyCompilationError(
                "The platform management identity cannot initialize an APIM in another tenant"
            )
        service = self._request("GET", "").json()
        if str(service.get("id", "")).casefold() != self.request.resource_id.casefold():
            raise PolicyCompilationError(
                "APIM service identity does not match the requested resource"
            )
        properties = service.get("properties") or {}
        origin = str(properties.get("gatewayUrl") or "").rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname != f"{self.request.service_name.lower()}.azure-api.net"
            or parsed.port not in {None, 443}
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise PolicyCompilationError(
                "APIM did not return its expected public managed Gateway origin"
            )
        tier = str((service.get("sku") or {}).get("name") or "")
        location = str(service.get("location") or "")
        if not tier or not location:
            raise PolicyCompilationError("APIM returned incomplete tier or location information")
        blockers = list(configuration_blockers)
        if properties.get("provisioningState") != "Succeeded":
            blockers.append("apim_not_ready")
        if tier.casefold() not in {
            "developer",
            "basic",
            "standard",
            "premium",
            "basicv2",
            "standardv2",
            "premiumv2",
        }:
            blockers.append("managed_gateway_tier_unsupported")
        for api in self._list_all("/apis"):
            api_id = self._resource_name(api)
            path = str((api.get("properties") or {}).get("path") or "").strip("/")
            if api_id.casefold() == self.request.api_id.casefold():
                try:
                    self._require_owned_api(api)
                except PolicyCompilationError:
                    blockers.append("dedicated_api_conflict")
            elif path.casefold() == self.request.api_path.casefold():
                blockers.append("dedicated_api_path_conflict")
        for subscription_id in (
            self.request.dashboard_subscription_id,
            self.request.probe_subscription_id,
        ):
            response = self._request(
                "GET", f"/subscriptions/{quote(subscription_id, safe='')}", allow_not_found=True
            )
            if response.status_code != 404:
                subscription = response.json()
                expected_scope = self._resource_id_base + self.api_resource_path
                values = subscription.get("properties") or {}
                if values.get("displayName") != f"Turnstile {subscription_id}" or str(
                    values.get("scope", "")
                ).casefold() not in {
                    expected_scope.casefold(),
                    self.api_resource_path.casefold(),
                }:
                    blockers.append("system_subscription_conflict")
        plan = ApimOnboardingPlan(
            request=self.request,
            gateway_profile_id=self.request.gateway_profile_id,
            service_name=self.request.service_name,
            service_tier=tier,
            location=location,
            managed_gateway_origin=HttpUrl(origin),
            api_id=self.request.api_id,
            api_path=self.request.api_path,
            dashboard_subscription_id=self.request.dashboard_subscription_id,
            probe_subscription_id=self.request.probe_subscription_id,
            ready=not blockers,
            blockers=sorted(set(blockers)),
            confirmation_sha256="0" * 64,
        )
        return plan.model_copy(
            update={"confirmation_sha256": onboarding_plan_digest(plan, configuration_digest)}
        )

    def ensure_api(self) -> None:
        existing = self._request("GET", self.api_resource_path, allow_not_found=True)
        if existing.status_code != 404:
            self._require_owned_api(existing.json())
            return
        self._request(
            "PUT",
            self.api_resource_path,
            json_body={"properties": self._api_properties()},
            extra_headers={"If-None-Match": "*"},
        )
        self._require_owned_api(self._request("GET", self.api_resource_path).json())

    def _dependency_request(
        self,
        method: str,
        resource_id: str,
        *,
        body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
        api_version: str = "2022-04-01",
    ) -> httpx.Response:
        response = self._client.request(
            method,
            f"{self._MANAGEMENT_ORIGIN}{resource_id}",
            params={"api-version": api_version},
            headers={
                **self._headers(),
                **({"If-None-Match": "*"} if method == "PUT" else {}),
            },
            json=body,
        )
        if response.status_code == 404 and allow_not_found:
            return response
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryablePublicationError(
                "An initialization dependency is temporarily unavailable"
            )
        if response.is_error:
            raise PolicyCompilationError(
                f"Initialization dependency returned HTTP {response.status_code}; "
                "check the platform identity's scoped permissions"
            )
        return response

    def _require_permissions(self, scope: str, required: set[str]) -> None:
        path = scope + "/providers/Microsoft.Authorization/permissions"
        value = self._dependency_request("GET", path, api_version="2022-04-01").json()
        try:
            missing = missing_management_actions(value, sorted(required))
        except ValueError as error:
            raise PolicyCompilationError(str(error)) from None
        if missing:
            raise PolicyCompilationError(
                "Worker authorization is missing at " + scope + ": " + ", ".join(missing)
            )

    @staticmethod
    def _dependency_roles(
        configuration: ApimOnboardingConfiguration,
    ) -> tuple[tuple[str, str], ...]:
        return (
            (configuration.event_hub_resource_id, "2b629674-e913-4c01-ae53-ef4638d8f975"),
            (configuration.ledger_table_resource_id, "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"),
            (configuration.route_table_resource_id, "76199698-9eea-4c19-bc75-cec21354c6b6"),
            (configuration.credential_vault_resource_id, "4633458b-17de-408a-b874-0445c86b69e6"),
        )

    def verify_initialization_permissions(self, configuration: ApimOnboardingConfiguration) -> None:
        service = self._request("GET", "").json()
        if str(service.get("id", "")).casefold() != self.request.resource_id.casefold():
            raise PolicyCompilationError("APIM identity changed during permission validation")
        required = {
            f"Microsoft.ApiManagement/service/{suffix}"
            for suffix in (
                "read",
                "apis/read",
                "apis/write",
                "apis/policies/read",
                "apis/policies/write",
                "apis/operations/read",
                "apis/operations/write",
                "apis/operations/policies/read",
                "apis/operations/policies/write",
                "loggers/read",
                "loggers/write",
                "subscriptions/read",
                "subscriptions/write",
                "subscriptions/listSecrets/action",
            )
        }
        if "SystemAssigned" not in str((service.get("identity") or {}).get("type", "")):
            required.add("Microsoft.ApiManagement/service/write")
        required.update(PUBLICATION_REQUIRED_ACTIONS)
        self._require_permissions(self.request.resource_id, required)
        logger_path = f"/loggers/{onboarding_logger_id(self.request)}"
        logger = self._request("GET", logger_path, allow_not_found=True)
        if logger.status_code == 404:
            self._require_permissions(
                self.request.resource_id,
                {
                    "Microsoft.ApiManagement/service/namedValues/read",
                    "Microsoft.ApiManagement/service/namedValues/listValue/action",
                },
            )
        else:
            self._require_owned_logger(
                logger.json(), logger_path, self._logger_properties(configuration),
            )
        for scope, _ in self._dependency_roles(configuration):
            self._require_permissions(
                scope,
                {
                    "Microsoft.Authorization/roleAssignments/read",
                    "Microsoft.Authorization/roleAssignments/write",
                },
            )

    def ensure_governance_dependencies(
        self,
        configuration: ApimOnboardingConfiguration,
    ) -> UUID:
        response = self._request("GET", "")
        service = response.json()
        if str(service.get("id", "")).casefold() != self.request.resource_id.casefold():
            raise PolicyCompilationError("APIM identity changed during initialization")
        identity = service.get("identity") or {}
        if "SystemAssigned" not in str(identity.get("type", "")):
            etag = response.headers.get("ETag") or service.get("etag")
            if not isinstance(etag, str) or not etag or etag == "*":
                raise PolicyCompilationError(
                    "APIM identity update requires a concrete resource ETag"
                )
            user_assigned = identity.get("userAssignedIdentities") or {}
            self._request(
                "PATCH",
                "",
                json_body={
                    "identity": {
                        "type": "SystemAssigned, UserAssigned"
                        if user_assigned
                        else "SystemAssigned",
                        **(
                            {"userAssignedIdentities": {name: {} for name in user_assigned}}
                            if user_assigned
                            else {}
                        ),
                    }
                },
                extra_headers={"If-Match": etag},
            )
            raise RetryablePublicationError("APIM managed identity is being initialized")
        try:
            principal_id = UUID(str(identity["principalId"]))
        except (KeyError, ValueError) as error:
            raise RetryablePublicationError("APIM managed identity is not yet available") from error
        for scope, role in self._dependency_roles(configuration):
            identifier = uuid5(
                self.request.gateway_profile_id,
                f"{scope.casefold()}:{principal_id}:{role}",
            )
            role_path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{identifier}"
            definition = (
                f"/subscriptions/{scope.split('/')[2]}/providers/Microsoft.Authorization/"
                f"roleDefinitions/{role}"
            )
            expected = {"principalId": str(principal_id), "roleDefinitionId": definition}
            current = self._dependency_request("GET", role_path, allow_not_found=True)
            if current.status_code == 404:
                self._dependency_request(
                    "PUT",
                    role_path,
                    body={
                        "properties": {
                            **expected,
                            "principalType": "ServicePrincipal",
                        }
                    },
                )
                current = self._dependency_request("GET", role_path)
            actual = current.json().get("properties") or {}
            if any(
                str(actual.get(key, "")).casefold() != value.casefold()
                for key, value in expected.items()
            ) or (
                str(actual.get("scope", "")).casefold() != scope.casefold()
                or actual.get("condition")
                or actual.get("conditionVersion")
            ):
                raise PolicyCompilationError(
                    "Initialization role assignment identity or scope differs"
                )
        properties = self._logger_properties(configuration)
        logger_path = f"/loggers/{onboarding_logger_id(self.request)}"
        logger = self._request("GET", logger_path, allow_not_found=True)
        if logger.status_code == 404:
            self._request(
                "PUT",
                logger_path,
                json_body={"properties": properties},
                extra_headers={"If-None-Match": "*"},
            )
            logger = self._request("GET", logger_path)
        self._require_owned_logger(logger.json(), logger_path, properties)
        return principal_id

    def _logger_properties(self, configuration: ApimOnboardingConfiguration) -> dict[str, object]:
        event_hub = configuration.event_hub_resource_id.split("/")
        return {
            "loggerType": "azureEventHub",
            "description": self.ownership_marker,
            "isBuffered": True,
            "resourceId": "/".join(event_hub[:-2]),
            "credentials": {
                "endpointAddress": f"{event_hub[8]}.servicebus.windows.net",
                "identityClientId": "systemAssigned",
                "name": event_hub[-1],
            },
        }

    def _require_owned_logger(
        self,
        value: Mapping[str, Any],
        logger_path: str,
        expected: Mapping[str, object],
    ) -> None:
        actual = value.get("properties")
        if (
            str(value.get("id", "")).casefold()
            != (self._resource_id_base + logger_path).casefold()
            or not isinstance(actual, Mapping)
            or any(
                actual.get(key) != expected_value
                for key, expected_value in expected.items()
                if key != "credentials"
            )
        ):
            raise PolicyCompilationError("Initialization Logger is unowned or has drifted")
        credentials = actual.get("credentials")
        expected_credentials = expected["credentials"]
        assert isinstance(expected_credentials, Mapping)
        if (
            not isinstance(credentials, Mapping)
            or set(credentials) != set(expected_credentials)
            or any(
                credentials.get(key) != expected_value
                for key, expected_value in expected_credentials.items()
                if key != "identityClientId"
            )
        ):
            raise PolicyCompilationError("Initialization Logger is unowned or has drifted")
        identity = credentials.get("identityClientId")
        if identity == expected_credentials["identityClientId"]:
            return
        reference = re.fullmatch(r"\{\{([A-Za-z0-9_.-]{1,256})\}\}", identity or "") if (
            isinstance(identity, str)
        ) else None
        if reference is None:
            raise PolicyCompilationError("Initialization Logger identity has drifted")
        reference_path = f"/namedValues/{quote(reference.group(1), safe='')}"
        response = self._request("GET", reference_path, allow_not_found=True)
        if response.status_code == 404:
            raise PolicyCompilationError("Initialization Logger identity reference is missing")
        named_value = response.json()
        properties = named_value.get("properties")
        if (
            str(named_value.get("id", "")).casefold()
            != (self._resource_id_base + reference_path).casefold()
            or not isinstance(properties, Mapping)
            or properties.get("keyVault")
            or not isinstance(properties.get("secret"), bool)
        ):
            raise PolicyCompilationError("Initialization Logger identity reference is invalid")
        resolved = properties.get("value")
        if properties["secret"]:
            resolved = self._request("POST", reference_path + "/listValue").json().get("value")
        if resolved != expected_credentials["identityClientId"]:
            raise PolicyCompilationError("Initialization Logger identity has drifted")

    def ensure_subscriptions(
        self,
        keys: Mapping[str, Mapping[str, str]],
        *,
        activate: bool = False,
    ) -> None:
        self._require_owned_api(self._request("GET", self.api_resource_path).json())
        for purpose, identifier in (
            ("dashboard", self.request.dashboard_subscription_id),
            ("probe", self.request.probe_subscription_id),
        ):
            pair = keys[purpose]
            primary, secondary = pair["primary"], pair["secondary"]
            if len(primary) < 32 or len(secondary) < 32 or primary == secondary:
                raise PolicyCompilationError("Initialization subscription credentials are invalid")
            path = f"/subscriptions/{identifier}"
            properties = {
                "displayName": f"Turnstile {identifier}",
                "scope": self._resource_id_base + self.api_resource_path,
                "state": "suspended",
                "allowTracing": False,
                "primaryKey": primary,
                "secondaryKey": secondary,
            }
            current = self._request("GET", path, allow_not_found=True)
            if current.status_code == 404:
                if activate:
                    raise PolicyCompilationError("A staged initialization subscription is missing")
                self._request(
                    "PUT",
                    path,
                    json_body={"properties": properties},
                    extra_headers={"If-None-Match": "*"},
                )
                current = self._request("GET", path)
            actual = current.json().get("properties") or {}
            if (
                actual.get("displayName") != properties["displayName"]
                or str(actual.get("scope", "")).casefold()
                not in {
                    str(properties["scope"]).casefold(),
                    self.api_resource_path.casefold(),
                }
                or actual.get("state") not in {"active", "suspended"}
                or actual.get("allowTracing") is not False
            ):
                raise PolicyCompilationError("A system subscription is unowned or has drifted")
            observed_keys = self._request("POST", path + "/listSecrets").json()
            if (
                observed_keys.get("primaryKey") != primary
                or observed_keys.get("secondaryKey") != secondary
            ):
                raise PolicyCompilationError(
                    "System subscription credentials changed; refusing to overwrite them"
                )
            if activate and actual.get("state") != "active":
                etag = current.headers.get("ETag")
                if not etag or etag == "*":
                    raise PolicyCompilationError("Subscription activation requires a concrete ETag")
                self._request(
                    "PUT",
                    path,
                    json_body={"properties": {**properties, "state": "active"}},
                    extra_headers={"If-Match": etag},
                )
                readback = self._request("GET", path).json().get("properties") or {}
                if readback.get("state") != "active":
                    raise RetryablePublicationError(
                        "System subscription activation has not propagated"
                    )

    def verify_managed_gateway(
        self,
        plan: ApimOnboardingPlan,
        keys: Mapping[str, Mapping[str, str]],
        *, fixed_installation: FixedProgramInstallation | None = None,
    ) -> None:
        endpoint = str(plan.managed_gateway_origin).rstrip("/") + f"/{plan.api_path}/v1/models"
        if fixed_installation is not None and (
            fixed_installation.program.scope.target_id != str(plan.gateway_profile_id)
            or fixed_installation.program.scope.api_id != plan.api_id
            or fixed_installation.program.scope.gateway_id != "managed"
            or fixed_installation.gateway_endpoint != endpoint.removesuffix("/v1/models")
            or fixed_installation.operation_contracts.get("models") != ("GET", "/v1/models")
        ):
            raise PolicyCompilationError("Initialized fixed catalog belongs to another target")
        if urlsplit(endpoint).hostname != f"{self.request.service_name.lower()}.azure-api.net":
            raise PolicyCompilationError("Initialization probe cannot leave its managed Gateway")
        expected_catalog: dict[str, Any] = {"object": "list", "data": []}
        if fixed_installation is not None:
            expected_catalog = {
                "data": [], "has_more": False, "first_id": None, "last_id": None,
            }
        for purpose in ("dashboard", "probe"):
            response = self._client.get(
                endpoint,
                headers={"Ocp-Apim-Subscription-Key": keys[purpose]["primary"]},
                timeout=10.0,
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise RetryablePublicationError(
                    f"Managed Gateway catalog validation returned HTTP {response.status_code}"
                )
            try:
                catalog = response.json()
            except ValueError as error:
                raise PolicyCompilationError(
                    "Managed Gateway catalog did not return JSON"
                ) from error
            if (
                catalog != expected_catalog
                or fixed_installation is None and (response.headers.get("x-turnstile-onboarding")
                != str(self.request.gateway_profile_id)
                or response.headers.get("x-turnstile-native-gateway") != "managed")
            ):
                raise PolicyCompilationError(
                    "Managed Gateway catalog identity or empty state differs"
                )
        for headers in (
            {},
            {"Ocp-Apim-Subscription-Key": "turnstile-invalid-onboarding-credential"},
        ):
            response = self._client.get(
                endpoint, headers=headers, timeout=10.0, follow_redirects=False
            )
            if response.status_code not in {401, 403}:
                raise PolicyCompilationError(
                    "Managed Gateway allowed an unauthenticated catalog read"
                )

    def ensure_catalog(self) -> None:
        self._require_owned_api(self._request("GET", self.api_resource_path).json())
        operation = f"{self.api_resource_path}/operations/anthropic-models"
        properties = {
            "displayName": "Models",
            "method": "GET",
            "urlTemplate": "/v1/models",
            "description": self.ownership_marker,
            "templateParameters": [],
            "responses": [{"statusCode": 200, "description": "No published models"}],
        }
        existing = self._request("GET", operation, allow_not_found=True)
        if existing.status_code == 404:
            self._request(
                "PUT",
                operation,
                json_body={"properties": properties},
                extra_headers={"If-None-Match": "*"},
            )
        else:
            current = existing.json().get("properties") or {}
            if any(
                current.get(name) != properties[name]
                for name in ("description", "method", "urlTemplate")
            ):
                raise PolicyCompilationError(
                    "The model catalog operation is unowned or has drifted"
                )
        root = ET.Element("policies")
        inbound = ET.SubElement(root, "inbound")
        ET.SubElement(inbound, "base")
        result = ET.SubElement(inbound, "return-response")
        ET.SubElement(result, "set-status", {"code": "200", "reason": "OK"})
        header = ET.SubElement(
            result, "set-header", {"name": "Content-Type", "exists-action": "override"}
        )
        ET.SubElement(header, "value").text = "application/json"
        for name, value in (
            ("x-turnstile-onboarding", str(self.request.gateway_profile_id)),
            ("x-turnstile-native-gateway", "@(context.Deployment.Gateway.Id)"),
        ):
            header = ET.SubElement(
                result,
                "set-header",
                {"name": name, "exists-action": "override"},
            )
            ET.SubElement(header, "value").text = value
        ET.SubElement(result, "set-body").text = '{"object":"list","data":[]}'
        for section in ("backend", "outbound", "on-error"):
            ET.SubElement(ET.SubElement(root, section), "base")
        self.ensure_policy(operation, ET.tostring(root, encoding="unicode"))

    def ensure_fixed_program(
        self, program: FixedRouteProgram, record_intent: Callable[[str, str], bool],
    ) -> None:
        binding = program.binding
        policies = dict(program.operation_policies)
        contracts = {name: (method, path) for name, method, path in program.operation_contracts}
        if (
            binding.scope.target_id != str(self.request.gateway_profile_id)
            or binding.scope.api_id != self.request.api_id
            or binding.scope.gateway_id != "managed"
            or binding.api_revision != str(self._api_properties()["apiRevision"])
            or binding.publisher_subscription_id != self.request.probe_subscription_id
            or contracts != fixed_operation_contracts(
                binding.models_path, binding.messages_path, binding.retired_operations,
                decision_enabled=binding.decision_enabled,
            )
            or set(policies) != set(contracts)
            or fixed_program_digest(
                program.parent_policy, policies, program.operation_contracts,
            ) != binding.program_sha256
        ):
            raise PolicyCompilationError("Initialization fixed program binding changed")
        for policy in (program.parent_policy, *policies.values()):
            validate_policy_upload(
                ET.tostring(parse_policy(policy), encoding="unicode"),
                gateway_profile_id=self.request.gateway_profile_id,
                api_id=self.request.api_id,
                api_revision=binding.api_revision,
                table_endpoint=binding.ledger_table_endpoint,
                table_name=binding.ledger_table_name,
            )
        self._require_owned_api(self._request("GET", self.api_resource_path).json())
        operations: dict[str, tuple[str, dict[str, object]]] = {}
        for name, (method, path) in contracts.items():
            resource = f"{self.api_resource_path}/operations/{quote(name, safe='')}"
            properties: dict[str, object] = {
                "displayName": name, "method": method, "urlTemplate": path,
                "description": self.ownership_marker, "templateParameters": [], "responses": [],
            }
            current = self._request("GET", resource, allow_not_found=True)
            if current.status_code != 404:
                actual = current.json().get("properties") or {}
                if (
                    str(current.json().get("id", "")).casefold()
                    != (self._resource_id_base + resource).casefold()
                    or any(actual.get(key) != properties[key] for key in (
                        "description", "method", "urlTemplate", "templateParameters",
                    ))
                ):
                    raise PolicyCompilationError("Initialization operation is unowned or changed")
            operations[name] = (resource, properties)
        for name, (resource, properties) in operations.items():
            current = self._request("GET", resource, allow_not_found=True)
            if current.status_code == 404:
                body = {"properties": properties}
                if not record_intent(resource, document_digest(body)):
                    raise RetryablePublicationError("Initialization operation outcome is unknown")
                self._request(
                    "PUT", resource, json_body=body, extra_headers={"If-None-Match": "*"},
                )
                actual = self._request("GET", resource).json().get("properties") or {}
                if any(actual.get(key) != properties[key] for key in (
                    "description", "method", "urlTemplate", "templateParameters",
                )):
                    raise PolicyCompilationError("Initialization operation readback differs")
            self.ensure_policy(
                resource, policies[name], record_intent=record_intent, fixed_binding=binding,
            )
        self.ensure_policy(
            self.api_resource_path, program.parent_policy,
            record_intent=record_intent, fixed_binding=binding,
        )

    def ensure_policy(
        self, owner_path: str, policy: str,
        *, record_intent: Callable[[str, str], bool] | None = None,
        fixed_binding: FixedRouteBinding | None = None,
    ) -> None:
        if not (
            owner_path == self.api_resource_path
            or owner_path.startswith(self.api_resource_path + "/operations/")
        ):
            raise PolicyCompilationError(
                "Initialization cannot modify a policy outside its own API"
            )
        encoded = encode_policy_xml(parse_policy(policy))
        validate_policy_upload(
            encoded, gateway_profile_id=self.request.gateway_profile_id,
            api_id=self.request.api_id, api_revision=str(self._api_properties()["apiRevision"]),
            table_endpoint=(fixed_binding.ledger_table_endpoint if fixed_binding is not None
                            else self._settings.ledger_table_endpoint),
            table_name=(fixed_binding.ledger_table_name if fixed_binding is not None
                        else self._settings.ledger_table_name),
        )
        path = owner_path + "/policies/policy"
        existing = self._policy_value(path)
        if existing is not None:
            observed = parse_policy(existing)
            expected = parse_policy(policy)
            if component_digest(observed, normalize_text_defaults=False) != component_digest(
                expected,
                normalize_text_defaults=False,
            ):
                raise PolicyCompilationError("An existing initialization policy has drifted")
            return
        body = {"properties": {"format": "xml", "value": encoded}}
        if record_intent is not None and not record_intent(path, document_digest(body)):
            raise RetryablePublicationError("Initialization policy outcome is unknown")
        self._request(
            "PUT", path, json_body=body, extra_headers={"If-None-Match": "*"},
        )
        readback = self._policy_value(path)
        if readback is None or component_digest(
            parse_policy(readback),
            normalize_text_defaults=False,
        ) != component_digest(parse_policy(policy), normalize_text_defaults=False):
            raise PolicyCompilationError("Initialization policy readback does not match")
