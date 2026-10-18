"""Cheap, fail-closed budget capabilities from installed publication evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from turnstile_core.config import Settings
from turnstile_core.domain.budget_counting import (
    BudgetPublicationContracts,
    verified_budget_operations,
)
from turnstile_core.domain.budget_delegation import BudgetCapabilities
from turnstile_core.domain.control_plane import GatewayPublication
from turnstile_core.persistence.repository_contract import QueryRepository
from turnstile_core.services.budget_settlement import BudgetScopeRepository


def budget_capabilities(repository: QueryRepository, settings: Settings) -> BudgetCapabilities:
    def unavailable(reason: str, message: str) -> BudgetCapabilities:
        return BudgetCapabilities(usd_available=False, reason_code=reason, message=message)

    if not settings.budget_execution_enabled:
        return unavailable(
            "execution_disabled", "金额预算尚未启用，请先完成预算执行协议安装与验证。",
        )
    if not isinstance(repository, BudgetScopeRepository):
        return unavailable("repository_unavailable", "当前数据源不支持金额预算执行。")
    try:
        rollout = repository.active_budget_execution_rollout()
        if rollout is None:
            return unavailable("rollout_unavailable", "金额预算执行尚未初始化。")
        period = datetime.now(UTC).date().replace(day=1)
        if rollout["prepared_period"] != period:
            return unavailable("period_unprepared", "当前预算周期尚未完成额度初始化。")
        targets = set(settings.budget_execution_target_ids)
        if not targets or not targets <= set(rollout["gateway_profile_ids"]):
            return unavailable("target_unverified", "金额预算网关范围尚未验证。")
        if (not targets <= settings.budget_delegation_targets.keys()
            or not targets <= settings.budget_delegation_trust.keys()):
            return unavailable("delegation_unconfigured", "可信后端委托尚未配置。")
        for target in targets:
            if repository.get_apim_target(target) is None:
                return unavailable("target_unregistered", "金额预算网关尚未注册。")
            raw = repository.effective_gateway_publication(target)
            if raw is None:
                return unavailable("publication_unavailable", "金额预算发布证据尚不可用。")
            publication = GatewayPublication.model_validate(raw)
            if (publication.status != "active" or not publication.apim_revision
                or not publication.policy_sha256):
                return unavailable("publication_unavailable", "金额预算发布证据尚不可用。")
            contracts = BudgetPublicationContracts.model_validate(
                publication.resource_manifest.get("budget_contracts"),
            )
            contracts.validate_publication(publication)
            if contracts.epoch != rollout["epoch"]:
                return unavailable("epoch_mismatch", "金额预算发布批次尚未同步。")
            if not any(verified_budget_operations(c) for c in contracts.model_contracts.values()):
                return unavailable(
                    "counting_unverified", "模型计数上界尚未验证，不能启用金额预算。",
                )
    except Exception:
        return unavailable("evidence_unavailable", "暂时无法验证金额预算状态，请稍后刷新。")
    return BudgetCapabilities(
        usd_available=True, reason_code="ready", message="金额预算协议已就绪。",
    )
