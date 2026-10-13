from __future__ import annotations

from typing import Literal
from urllib.parse import quote

import httpx

from ..config import Settings
from .apim_control_plane_contract import PolicyCompilationError
from .reconciliation import ManagedIdentityTokenProvider


class AzureApimSubscriptionKeyClient:
    _MANAGEMENT_ORIGIN = "https://management.azure.com"
    _MANAGEMENT_RESOURCE = f"{_MANAGEMENT_ORIGIN}/"
    _API_VERSION = "2024-05-01"

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
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"APIM key-management settings are missing: {', '.join(missing)}"
            )
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client or httpx.Client(timeout=30.0)
        self._base = (
            f"{self._MANAGEMENT_ORIGIN}/subscriptions/{settings.azure_subscription_id}"
            f"/resourceGroups/{settings.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{settings.apim_service_name}"
        )

    def _post(self, apim_subscription_id: str, action: str) -> httpx.Response:
        token = self._token_provider.token(self._MANAGEMENT_RESOURCE)
        response = self._client.post(
            f"{self._base}/subscriptions/"
            f"{quote(apim_subscription_id, safe='')}/{action}",
            params={"api-version": self._API_VERSION},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response

    def cancel_application_subscription(self, apim_subscription_id: str) -> None:
        token = self._token_provider.token(self._MANAGEMENT_RESOURCE)
        response = self._client.delete(
            f"{self._base}/subscriptions/{quote(apim_subscription_id, safe='')}",
            params={"api-version": self._API_VERSION},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()

    def application_subscription_keys(
        self, apim_subscription_id: str
    ) -> tuple[str, str]:
        payload = self._post(apim_subscription_id, "listSecrets").json()
        primary_key = payload.get("primaryKey")
        secondary_key = payload.get("secondaryKey")
        if not isinstance(primary_key, str) or not primary_key:
            raise PolicyCompilationError("APIM primary subscription key is unavailable")
        if not isinstance(secondary_key, str) or not secondary_key:
            raise PolicyCompilationError("APIM secondary subscription key is unavailable")
        return primary_key, secondary_key

    def regenerate_application_subscription_key(
        self,
        apim_subscription_id: str,
        key_kind: Literal["primary", "secondary"],
    ) -> None:
        action = (
            "regeneratePrimaryKey"
            if key_kind == "primary"
            else "regenerateSecondaryKey"
        )
        self._post(apim_subscription_id, action)
