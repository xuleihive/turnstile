from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.ledger import (
    MODEL_PROPERTY_LIMIT,
    LedgerSyncService,
    TableStorageLedger,
    application_map_partition_key,
    application_partition_key,
    model_access_value,
)
from ..persistence.repository import QueryRepository


def prepare_application_ledger(
    repository: QueryRepository,
    store: TableStorageLedger,
    gateway_id: UUID,
    application_id: UUID,
) -> None:
    now = datetime.now(UTC)
    service = LedgerSyncService(repository, store)
    snapshot = next((
        item for item in service.application_snapshot(now)
        if item.application_id == str(application_id)
    ), None)
    mappings = [
        item for item in repository.gateway_application_ledger_mappings()
        if str(item["application_id"]) == str(application_id)
        and str(item["gateway_profile_id"]) == str(gateway_id)
    ]
    if snapshot is None or not mappings:
        raise RetryablePublicationError("Application budget or attribution is not ready")
    active_mappings = [
        mapping
        for mapping in mappings
        if mapping["application_status"] == "active"
        and mapping["subscription_state"] == "active"
        and mapping["scope_exists"]
    ]
    if not active_mappings:
        raise PolicyCompilationError("Application was disabled or its subscription scope changed")
    policy = next((
        item for item in service.application_model_access()
        if item.application_id == str(application_id)
    ), None)
    identifiers = model_access_value(policy.identifiers) if policy else ""
    if len(identifiers) > MODEL_PROPERTY_LIMIT:
        raise PolicyCompilationError("Application model access exceeds the ledger property limit")
    partition = application_partition_key(application_id, now.strftime("%Y-%m"))
    quota = {
        "Limit": snapshot.token_limit,
        "TokensPerMinute": snapshot.tokens_per_minute,
        "Enforce": snapshot.enforce,
    }
    access = {"Configured": policy is not None, "Models": identifiers}
    map_partition = application_map_partition_key(gateway_id)
    store.upsert(partition, "Q", quota)
    store.insert_if_missing(partition, "C", {"ConfirmedUsed": snapshot.confirmed_tokens})
    store.upsert(partition, "M", access)
    expected_mappings: dict[str, dict[str, object]] = {}
    for item in mappings:
        subscription_id = str(item["apim_subscription_id"])
        identity: dict[str, object] = {
            "ApplicationId": str(application_id),
            "ApplicationSlug": str(item["application_slug"]),
            "ApplicationName": str(item["application_name"]),
            "ApplicationType": str(item["application_type"]),
            "ApplicationStatus": str(item["application_status"]),
            "SubscriptionState": str(item["subscription_state"]),
            "ScopeExists": bool(item["scope_exists"]),
            "OwnerUserId": str(item.get("owner_user_id") or ""),
            "OwnerEmail": str(item.get("owner_email") or ""),
            "OwnerDisplayName": str(item.get("owner_display_name") or ""),
            "OwnerEnabled": bool(item.get("owner_enabled", False)),
            "OwnerTenantId": str(item.get("owner_tenant_id") or ""),
            "TenantId": str(item.get("tenant_id") or ""),
            "TenantName": str(item.get("tenant_name") or ""),
            "TenantEnabled": bool(item.get("tenant_enabled", False)),
            "OrganizationUnitId": str(item.get("organization_unit_id") or ""),
            "OrganizationUnitName": str(item.get("organization_unit_name") or ""),
            "OrganizationUnitEnabled": bool(item.get("organization_unit_enabled", False)),
        }
        expected_mappings[subscription_id] = identity
        store.upsert(map_partition, subscription_id, identity)
    for key, expected in (("Q", quota), ("M", access)):
        actual = store.read_entity(partition, key)
        if actual is None or any(actual.get(field) != value for field, value in expected.items()):
            raise RetryablePublicationError("Application admission ledger readback is incomplete")
    confirmed = store.read_entity(partition, "C")
    if (
        confirmed is None
        or int(confirmed.get("ConfirmedUsed", -1)) < 0
        or any(
            (actual_mapping := store.read_entity(map_partition, subscription_id)) is None
            or any(actual_mapping.get(field) != value for field, value in expected.items())
            for subscription_id, expected in expected_mappings.items()
        )
    ):
        raise RetryablePublicationError("Application attribution ledger readback is incomplete")
