from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from pydantic import SecretStr

from turnstile_core.domain.budget_delegation import (
    BudgetDelegationCheck,
    BudgetDelegationReadiness,
    BudgetExecutionScopeReadback,
    BudgetExecutionVerification,
    BudgetExecutionVerificationOptions,
    BudgetExecutionVerificationRequest,
)
from turnstile_core.domain.ledger import LedgerAttempt, LedgerAttemptIdentity, LedgerScope
from turnstile_core.domain.runtime_models import GatewayKind, ModelInvocationRequest
from turnstile_core.integrations.atomic_ledger import AtomicTableLedger
from turnstile_core.integrations.gateway import OpenAICompatibleGatewayAdapter
from turnstile_core.integrations.reconciliation import ManagedIdentityTokenProvider
from turnstile_core.services.application_access import ApplicationAccessService
from turnstile_core.services.budget_settlement import BudgetStatusRepository

from .gateway_credentials import with_platform_gateway_credential
from .runtime_service import ModelRuntimeService, budget_readiness_failure_headers

logger = logging.getLogger(__name__)


class BudgetDiagnostics:
    def __init__(
        self,
        runtime: ModelRuntimeService,
        *,
        store_factory: Callable[[], AtomicTableLedger] | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
        token_provider: Any = None,
    ) -> None:
        self.runtime = runtime
        self.repository = runtime._repository
        self.settings = runtime._settings
        self.store_factory = store_factory or (
            lambda: AtomicTableLedger(
                str(self.settings.ledger_table_endpoint),
                self.settings.ledger_table_name,
                timeout=2,
            )
        )
        self.client_factory = client_factory or (
            lambda: httpx.Client(timeout=8, follow_redirects=False)
        )
        self.tokens = token_provider or ManagedIdentityTokenProvider()

    def _route(self, gateway_id: UUID, model_id: UUID | None = None) -> dict[str, Any]:
        if (
            not self.settings.budget_execution_enabled
            or gateway_id not in self.settings.budget_execution_target_ids
            or gateway_id not in self.settings.budget_delegation_targets
            or not self.settings.ledger_table_endpoint
        ):
            raise HTTPException(status_code=409, detail="Budget delegation is not configured")
        registry = self.repository.registry()
        runtime_ids = {
            row["id"]
            for row in registry["runtimes"]
            if row["enabled"] and row.get("gateway_profile_id") == gateway_id
        }
        for model in registry["models"]:
            if (
                not model["enabled"]
                or model["runtime_id"] not in runtime_ids
                or "image_generation" in (model.get("capabilities") or [])
                or (model_id is not None and model["id"] != model_id)
            ):
                continue
            route = self.repository.invocation_route(model["runtime_id"], model["id"])
            if (
                route is not None
                and str(route.get("gateway_id")) == str(gateway_id)
                and route.get("gateway_implementation") == "apim"
            ):
                resolved = self.runtime._invocation_route(self._request(
                    route, "system-budget-diagnostic", str(uuid4()),
                ))
                if resolved is None:
                    raise HTTPException(
                        status_code=409, detail="The diagnostic route is unavailable"
                    )
                route = resolved
                self._endpoint(gateway_id, str(route.get("gateway_base_url") or ""))
                return route
        raise HTTPException(status_code=409, detail="An enabled registered text route is required")

    def _endpoint(self, gateway_id: UUID, endpoint: str) -> str:
        target = self.repository.get_apim_target(gateway_id)
        configuration = self.settings.budget_delegation_targets[gateway_id]
        parsed = urlsplit(endpoint)
        origins = {
            urlsplit(str(origin)).netloc.casefold() for origin in configuration.allowed_origins
        }
        if (
            target is None
            or parsed.scheme != "https"
            or parsed.netloc.casefold() not in origins
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/" + str(target["api_path"]).strip("/")
        ):
            raise HTTPException(status_code=503, detail="Budget verification origin is not trusted")
        return endpoint.rstrip("/")

    def _fence(self, store: AtomicTableLedger, gateway_id: UUID) -> dict[str, Any]:
        target = self.repository.get_apim_target(gateway_id)
        if target is None:
            raise HTTPException(status_code=409, detail="A registered budget target is required")
        service = str(target["service_name"]).lower()
        partition = (
            f"budget-protocol|{hashlib.sha256(service.encode()).hexdigest()}|{target['api_id']}"
        )
        entity = store.read_entity(partition, "F")
        fence = json.loads(entity["Document"]) if entity else {}
        if (
            not isinstance(fence, dict)
            or fence.get("protocol_version") != 2
            or fence.get("gateway_profile_id") != str(gateway_id)
            or fence.get("api_id") != target["api_id"]
            or str(fence.get("apim_service_id", "")).lower() != service
            or fence.get("mode") not in {"prepared", "paused", "active"}
            or not isinstance(fence.get("gateways"), dict)
        ):
            raise HTTPException(
                status_code=409, detail="An exact budget protocol fence is required"
            )
        return fence

    @staticmethod
    def _revision(fence: Mapping[str, Any], native: str) -> str:
        revisions = fence["gateways"].get(native)
        revision = fence.get("validation_revision") or (
            revisions[0] if isinstance(revisions, list) and revisions else None
        )
        if (
            not isinstance(revisions, list)
            or not isinstance(revision, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", revision)
            or revision not in revisions
        ):
            raise HTTPException(status_code=409, detail="An exact registered Revision is required")
        return revision

    def _subscription(
        self, gateway_id: UUID, person_id: str, fence: Mapping[str, Any]
    ) -> dict[str, Any]:
        if (
            fence.get("mode") != "paused"
            or fence.get("validation_person_id") != person_id
            or not isinstance(fence.get("validation_revision"), str)
        ):
            raise HTTPException(
                status_code=409, detail="An isolated two-scope candidate is required"
            )
        try:
            application_id = UUID(str(fence.get("validation_application_id")))
        except ValueError:
            raise HTTPException(
                status_code=409, detail="Candidate Application is invalid"
            ) from None
        application = self.repository.get_gateway_application(application_id)
        if (
            application is None
            or application["status"] != "active"
            or application["system_managed"]
            or str(application["gateway_profile_id"]) != str(gateway_id)
        ):
            raise HTTPException(
                status_code=409, detail="An active non-system Application is required"
            )
        subscriptions = [
            dict(item)
            for item in self.repository.list_gateway_application_subscriptions(
                [application_id],
            )
            if item["state"] == "active"
            and item["scope_exists"]
            and item["apim_subscription_id"] == fence.get("validation_subscription_id")
            and str(item["gateway_profile_id"]) == str(gateway_id)
        ]
        if len(subscriptions) != 1:
            raise HTTPException(status_code=409, detail="The isolated subscription is not ready")
        return subscriptions[0]

    def _request(
        self, route: Mapping[str, Any], person_id: str, request_id: str
    ) -> ModelInvocationRequest:
        metadata = {
            name: "unattributed"
            for name in (
                "organization_id",
                "organization",
                "department_id",
                "department",
                "project_id",
                "project",
                "agent_id",
                "agent",
                "workflow",
            )
        }
        return ModelInvocationRequest.model_validate(
            {
                "model_id": route["model_id"],
                "max_output_tokens": 32,
                "messages": [{"role": "user", "content": "Reply exactly OK."}],
                "metadata": {
                    **metadata,
                    "user_id": person_id,
                    "user": person_id,
                    "model_id": str(route["model_id"]),
                    "model": route["model_key"],
                    "runtime": route["runtime_name"],
                    "run_id": request_id,
                    "request_source": "budget-verification",
                },
            }
        )

    def readiness(self, gateway_id: UUID, person_id: str) -> BudgetDelegationReadiness:
        route = self._route(gateway_id)
        stage = "ledger_fence_read"
        try:
            with self.store_factory() as store:
                fence = self._fence(store, gateway_id)
            stage = "ledger_fence_validate"
            native = str(route.get("native_gateway_id") or "managed")
            revision = self._revision(fence, native)
            configuration = self.settings.budget_delegation_targets[gateway_id]
            destinations = [(native, self._endpoint(gateway_id, str(route["gateway_base_url"])))]
            stage = "managed_identity_token"
            valid = self.tokens.token(configuration.audience)
            paused = fence["mode"] == "paused"
            if paused:
                stage = "gateway_destinations"
                destinations = [
                    (
                        str(item["native_gateway_id"]),
                        self._endpoint(
                            gateway_id,
                            str(item.get("endpoint_url") or ""),
                        ),
                    )
                    for item in self.repository.list_gateway_deployments(gateway_id)
                    if item["native_gateway_id"] in fence["gateways"] and not item.get("retired_at")
                ]
                if {item[0] for item in destinations} != set(fence["gateways"]):
                    raise HTTPException(
                        status_code=409, detail="Every fenced Gateway must be present"
                    )
                cases = [
                    ("current_fenced", 503, valid, True, False),
                    ("candidate_fenced", 503, valid, True, True),
                ]
            else:
                alternate = (
                    "https://management.azure.com/"
                    if configuration.audience != "https://management.azure.com/"
                    else "https://storage.azure.com/"
                )
                cases = [
                    ("valid_identity", 200, valid, True, True),
                    ("wrong_audience", 401, self.tokens.token(alternate), True, True),
                    ("missing_person", 403, valid, False, True),
                ]
            checks = []
            with self.client_factory() as client:
                stage = "subscription_credential"
                decrypted = with_platform_gateway_credential(
                    self.runtime._decrypt_route(route), self.settings,
                )
                headers = OpenAICompatibleGatewayAdapter(GatewayKind.APIM, client)._auth_headers(
                    decrypted
                )
                if (
                    not decrypted.get("gateway_credential")
                    or not any(key.lower() == "ocp-apim-subscription-key" for key in headers)
                    or any(key.lower() == "authorization" for key in headers)
                ):
                    raise HTTPException(status_code=409, detail="An APIM subscription is required")
                stage = "gateway_probes"
                for gateway_native, endpoint in destinations:
                    self._revision(fence, gateway_native)
                    for case, expected, token, person, candidate in cases:
                        actual, error_code = None, None
                        try:
                            response = client.get(
                                endpoint + (f";rev={revision}" if candidate else "") + "/v1/models",
                                headers={
                                    **headers,
                                    "x-turnstile-delegation-token": "Bearer " + token,
                                    **({"x-user-id": person_id} if person else {}),
                                },
                                timeout=2 if paused else 8,
                                follow_redirects=False,
                            )
                            actual = response.status_code
                            if paused:
                                payload = response.json()
                                error = payload.get("error") if isinstance(payload, dict) else None
                                if (
                                    isinstance(error, dict)
                                    and error.get("code") == "budget_protocol_fenced"
                                ):
                                    error_code = "budget_protocol_fenced"
                        except (httpx.HTTPError, ValueError):
                            pass
                        checks.append(
                            BudgetDelegationCheck.model_validate(
                                {
                                    "case": case,
                                    "expected_status": expected,
                                    "actual_status": actual,
                                    "native_gateway_id": gateway_native,
                                    "expected_error_code": "budget_protocol_fenced"
                                    if paused
                                    else None,
                                    "actual_error_code": error_code,
                                }
                            )
                        )
            ready = bool(checks) and all(
                item.actual_status == item.expected_status
                and item.actual_error_code == item.expected_error_code
                for item in checks
            )
            verification = None
            if ready and paused and fence.get("validation_person_id") == person_id:
                try:
                    subscription = self._subscription(gateway_id, person_id, fence)
                    policy = self.runtime._person_model_policy(person_id)
                    models = []
                    for model in self.repository.registry()["models"]:
                        try:
                            candidate_route = self._route(gateway_id, model["id"])
                            if self.runtime._model_policy_allows(candidate_route, policy):
                                models.append(model["id"])
                        except HTTPException:
                            continue
                    verification = BudgetExecutionVerificationOptions(
                        application_id=UUID(str(subscription["application_id"])),
                        model_ids=models,
                    )
                except HTTPException:
                    pass
            return BudgetDelegationReadiness(
                gateway_profile_id=gateway_id,
                native_gateway_id=native,
                revision=revision,
                checked_at=datetime.now(UTC),
                ready=ready,
                checks=checks,
                protocol_mode=fence["mode"],
                verification=verification,
            )
        except HTTPException as error:
            error.headers = {
                **(error.headers or {}), **budget_readiness_failure_headers(stage, error),
            }
            raise
        except Exception as error:
            logger.warning(
                "Budget readiness unavailable stage=%s error_type=%s",
                stage, type(error).__name__,
            )
            raise HTTPException(
                status_code=503, detail="Budget readiness could not be verified",
                headers=budget_readiness_failure_headers(stage, error),
            ) from None

    def execute(
        self,
        gateway_id: UUID,
        write: BudgetExecutionVerificationRequest,
        person_id: str,
        applications: ApplicationAccessService,
    ) -> BudgetExecutionVerification:
        route = self._route(gateway_id, write.model_id)
        moment = datetime.now(UTC)
        period = moment.date().replace(day=1)
        scopes = [
            LedgerScope(scope_type="person", scope_id=person_id, period_start=period),
            LedgerScope(
                scope_type="application", scope_id=str(write.application_id), period_start=period
            ),
        ]
        with self.store_factory() as store:
            fence = self._fence(store, gateway_id)
            native = str(route.get("native_gateway_id") or "managed")
            revision = self._revision(fence, native)
            subscription = self._subscription(gateway_id, person_id, fence)
            if str(subscription["application_id"]) != str(write.application_id):
                raise HTTPException(
                    status_code=409, detail="Application does not match the candidate"
                )
            for protected_id in self.settings.budget_execution_target_ids:
                protected = self._fence(store, protected_id)
                if protected["mode"] != "paused" or protected.get("epoch") != fence.get("epoch"):
                    raise HTTPException(
                        status_code=409, detail="Every shared budget target must be fenced"
                    )
            if not isinstance(self.repository, BudgetStatusRepository):
                raise HTTPException(status_code=503, detail="Execution scope status is unavailable")
            before = []
            for scope in scopes:
                status = self.repository.budget_execution_status(scope)
                if (
                    status is None
                    or status["state"] != "active"
                    or str(status["epoch"]) != fence.get("epoch")
                ):
                    raise HTTPException(
                        status_code=409, detail="Both execution scopes must be ready"
                    )
                summary = store.read_summary(scope)
                if not summary.enabled or str(summary.epoch) != fence.get("epoch"):
                    raise HTTPException(status_code=409, detail="Both scope policies must be ready")
                before.append(summary)
            request_id = str(uuid4())
            request = self._request(route, person_id, request_id)
            self.runtime._assert_model_allowed(request, route, request_id)
            policies = self.repository.list_gateway_application_model_access([write.application_id])
            if policies and not any(
                str(item.get("model_id")) == str(write.model_id) for item in policies
            ):
                raise HTTPException(
                    status_code=403, detail="Model is not assigned to the Application"
                )
            credential = applications.reveal_application_subscription_key(
                write.application_id,
                UUID(str(subscription["id"])),
                "primary",
            )
            endpoint = self._endpoint(gateway_id, str(route["gateway_base_url"]))
            route = self.runtime._model_protocol_route(self.runtime._decrypt_route(route))
            route.update(
                gateway_base_url=endpoint + f";rev={revision}",
                gateway_auth_type="api_key",
                gateway_credential=credential.value,
                gateway_config={
                    **dict(route.get("gateway_config") or {}),
                    "header_name": "Ocp-Apim-Subscription-Key",
                },
                budget_delegation_token=SecretStr(
                    self.tokens.token(
                        self.settings.budget_delegation_targets[gateway_id].audience,
                    )
                ),
            )
            if self._fence(store, gateway_id) != fence:
                raise HTTPException(
                    status_code=409, detail="Candidate fence changed before dispatch"
                )
            invocation, actual_status, correlation = None, 200, None
            try:
                invocation = self.runtime._invoke_resolved(
                    request,
                    route,
                    request_id,
                    timeout_ms=8000,
                )
                correlation = invocation.correlation_id
            except HTTPException as error:
                actual_status = error.status_code
                correlation = (error.headers or {}).get("x-correlation-id")
            readbacks = []
            for summary in before:
                after, attempt = None, None
                try:
                    after = store.read_summary(summary.scope)
                    if correlation:
                        identity = LedgerAttemptIdentity(
                            scope=summary.scope,
                            gateway_profile_id=gateway_id,
                            native_gateway_id=native,
                            correlation_id=correlation,
                            model_id=str(write.model_id),
                            admitted_at=moment,
                        )
                        entity = store.read_entity(summary.scope.partition, identity.row_key)
                        if entity:
                            observed = LedgerAttempt.model_validate_json(entity["Document"])
                            if (
                                observed.identity.scope != summary.scope
                                or observed.epoch != summary.epoch
                                or observed.identity.attempt_key != identity.attempt_key
                                or observed.identity.model_id != str(write.model_id)
                            ):
                                raise ValueError("Execution receipt identity does not match")
                            attempt = observed
                except Exception as error:
                    logger.warning(
                        "Budget readback unavailable error_type=%s", type(error).__name__
                    )
                readbacks.append(
                    BudgetExecutionScopeReadback(before=summary, after=after, attempt=attempt)
                )
            usage = invocation.usage if invocation else None
            total = (
                usage.input_tokens + usage.cached_tokens + usage.output_tokens
                if usage and not usage.estimated
                else None
            )
            return BudgetExecutionVerification(
                gateway_profile_id=gateway_id,
                native_gateway_id=native,
                revision=revision,
                checked_at=datetime.now(UTC),
                request_id=request_id,
                actual_status=actual_status,
                invocation=invocation,
                scopes=readbacks,
                settled=total is not None
                and all(
                    item.attempt is not None
                    and item.attempt.state == "settled"
                    and item.attempt.actual_tokens == total
                    for item in readbacks
                ),
            )
