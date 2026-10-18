"""Initialize only the new consumer's admission rows before its subscription is activated."""

from datetime import UTC, datetime
from uuid import UUID

from ..domain.application_access import MAX_TOKENS_PER_MINUTE, application_organization
from ..integrations.apim_control_plane_contract import (
    PolicyCompilationError,
    RetryablePublicationError,
)
from ..integrations.ledger import (
    APPLICATION_RATE_LIMIT_ROW_KEY,
    MODEL_PROPERTY_LIMIT,
    LedgerSyncService,
    TableStorageLedger,
    application_map_partition_key,
    application_partition_key,
    application_rate_limit_partition_key,
    model_access_value,
)
from ..persistence.repository import QueryRepository
from .directory_catalog import catalog_for


def read_application_rate_limit(
    store: TableStorageLedger, application_id: UUID,
) -> int | None:
    current = store.read_versioned_entity(
        application_rate_limit_partition_key(application_id),
        APPLICATION_RATE_LIMIT_ROW_KEY,
    )
    if current is None:
        return None
    value = current[0].get("TokensPerMinute")
    if type(value) is not int or not 0 < value <= MAX_TOKENS_PER_MINUTE:
        raise ValueError("Application rate limit configuration is invalid")
    return value


def project_application_rate_limit(
    repository: QueryRepository, store: TableStorageLedger,
    application_id: UUID,
) -> int | None:
    partition = application_rate_limit_partition_key(application_id)
    for _ in range(3):
        current = store.read_versioned_entity(partition, APPLICATION_RATE_LIMIT_ROW_KEY)
        period = datetime.now(UTC).date().replace(day=1)
        budget = repository.get_effective_gateway_application_budget(application_id, period)
        if budget is None:
            return None
        if str(budget["application_id"]) != str(application_id):
            raise ValueError("Application rate limit identity is ambiguous")
        rate = budget["tokens_per_minute"]
        if type(rate) is not int or not 0 < rate <= MAX_TOKENS_PER_MINUTE:
            raise ValueError("Application rate limit configuration is invalid")
        if store.replace_if_match(
            partition, APPLICATION_RATE_LIMIT_ROW_KEY, {"TokensPerMinute": rate},
            current[1] if current is not None else None,
        ) and read_application_rate_limit(store, application_id) == rate:
            latest = repository.get_effective_gateway_application_budget(
                application_id, datetime.now(UTC).date().replace(day=1),
            )
            if latest == budget:
                return rate
    raise RetryablePublicationError("Application rate limit projection is pending")


def prepare_application_ledger(
    repository: QueryRepository,
    store: TableStorageLedger,
    gateway_id: UUID,
    application_id: UUID,
) -> None:
    now = datetime.now(UTC)
    service = LedgerSyncService(repository, store)
    snapshot = next(
        (item for item in service.application_snapshot(now)
         if item.application_id == str(application_id)),
        None,
    )
    mappings = [
        item for item in repository.gateway_application_ledger_mappings()
        if str(item["application_id"]) == str(application_id)
        and str(item["gateway_profile_id"]) == str(gateway_id)
    ]
    if snapshot is None or not mappings:
        raise RetryablePublicationError("Application budget or attribution is not ready")
    if len(mappings) != 1:
        raise PolicyCompilationError("Application creation must have exactly one subscription")
    mapping = mappings[0]
    if (mapping["application_status"] != "active"
            or mapping["subscription_state"] != "active"
            or not mapping["scope_exists"]):
        raise PolicyCompilationError("Application was disabled or its subscription scope changed")
    policy = next(
        (item for item in service.application_model_access()
         if item.application_id == str(application_id)),
        None,
    )
    partition = application_partition_key(application_id, now.strftime("%Y-%m"))
    quota = {
        "Limit": snapshot.token_limit,
        "TokensPerMinute": snapshot.tokens_per_minute,
        "Enforce": snapshot.enforce,
    }
    model_identifiers = model_access_value(policy.identifiers) if policy else ""
    if len(model_identifiers) > MODEL_PROPERTY_LIMIT:
        raise PolicyCompilationError("Application model access exceeds the ledger property limit")
    access = {"Configured": policy is not None, "Models": model_identifiers}
    catalog = catalog_for(repository, include_inactive=True)
    organization = application_organization(dict(mapping.get("tags") or {}), catalog=catalog)
    department = next((item for item in catalog.departments
                       if item.id == mapping.get("department_id")), None)
    identity = {
        "ApplicationId": str(application_id),
        "ApplicationSlug": str(mapping["application_slug"]),
        "ApplicationName": str(mapping["application_name"]),
        "ApplicationType": str(mapping["application_type"]),
        "ApplicationStatus": str(mapping["application_status"]),
        "OrganizationId": organization.organization_id or "unattributed",
        "OrganizationName": organization.organization_name or "unattributed",
        "DepartmentId": str(mapping.get("department_id") or "unattributed"),
        "DepartmentName": department.name if department is not None else "unattributed",
        "SubscriptionState": str(mapping["subscription_state"]),
        "ScopeExists": bool(mapping["scope_exists"]),
    }
    if snapshot.budget_mode == "tokens":
        store.upsert(partition, "Q", quota)
        store.insert_if_missing(partition, "C", {"ConfirmedUsed": snapshot.confirmed_tokens})
    store.upsert(partition, "M", access)
    map_partition = application_map_partition_key(gateway_id)
    subscription_id = str(mapping["apim_subscription_id"])
    store.upsert(map_partition, subscription_id, identity)
    if project_application_rate_limit(repository, store, application_id) is None:
        raise RetryablePublicationError("Application rate limit is not ready")
    expected_rows = (
        (("Q", quota), ("M", access))
        if snapshot.budget_mode == "tokens" else (("M", access),)
    )
    for key, expected in expected_rows:
        actual = store.read_entity(partition, key)
        if actual is None or any(actual.get(field) != value for field, value in expected.items()):
            raise RetryablePublicationError("Application admission ledger readback is incomplete")
    confirmed = store.read_entity(partition, "C")
    actual_mapping = store.read_entity(map_partition, subscription_id)
    confirmed_missing = snapshot.budget_mode == "tokens" and (
        confirmed is None or int(confirmed.get("ConfirmedUsed", -1)) < 0
    )
    if confirmed_missing or actual_mapping is None or any(
        actual_mapping.get(field) != value for field, value in identity.items()
    ):
        raise RetryablePublicationError("Application attribution ledger readback is incomplete")
