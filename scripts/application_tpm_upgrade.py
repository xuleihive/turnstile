"""Plan a narrow parent-policy upgrade using the existing revision/rollback engine."""

from __future__ import annotations

from turnstile_core.integrations.apim_application_tpm import compose_application_tpm_policy
from turnstile_core.integrations.apim_policy_components import validate_parent_policy

from .apim_upgrade import GatewaySnapshot, ImageUpgradePlan, document_digest, policy_digest

VERSION = "application-tpm-v1"


def plan_application_tpm_upgrade(
    api_resource_id: str, snapshot: GatewaySnapshot, canonical: str,
) -> ImageUpgradePlan:
    validate_parent_policy(snapshot.parent_policy, canonical)
    parent = compose_application_tpm_policy(snapshot.parent_policy)
    validate_parent_policy(parent, canonical)
    identity = document_digest({
        "target": api_resource_id.casefold(), "source": snapshot.fingerprint(),
        "parent": policy_digest(parent), "version": VERSION,
    })
    return ImageUpgradePlan(
        api_resource_id=api_resource_id, source=snapshot, parent_policy=parent,
        create_operation=False, initialize_image_policy=False,
        revision=f"turnstile-tpm-{identity[:16]}", version=VERSION,
    )
