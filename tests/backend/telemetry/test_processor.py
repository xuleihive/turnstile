from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tests.backend.model_platform.control_plane_support import (
    GatewayControlPlaneService,
    StubTokenProvider,
    foundry_publication,
    publisher_settings,
)
from tests.backend.model_platform.test_platform_invocation_trace import invocation_attempt
from turnstile_core.domain.application_access import (
    GatewayApplicationDiscovery,
    GatewayApplicationDiscoveryItem,
    UsageApplicationAttribution,
)
from turnstile_core.domain.budget_completion import normalize_terminal_rejection
from turnstile_core.domain.control_plane import publication_model_id
from turnstile_core.domain.gateway_targets import (
    UsageGatewayAttribution,
    UsageGatewayAttributionSource,
    canonical_usage_id_v2,
)
from turnstile_core.domain.ledger import (
    BudgetPriceProjection,
    LedgerAdmissionReceipt,
    LedgerAttemptIdentity,
    LedgerScope,
)
from turnstile_core.domain.models import (
    ModelIdentity,
    ModelPrice,
    ModelPriceTier,
    TokenUsageRecord,
    UsageEvent,
)
from turnstile_core.ingestion.processor import (
    CoefficientResolver,
    UsageProcessor,
    calculate_et,
)
from turnstile_core.integrations.apim_publisher_client import AzureApimPublisherClient
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
from turnstile_core.persistence.repository_budget_execution import PostgreSqlBudgetExecutionMixin
from turnstile_core.services.application_access import ApplicationAccessService

GATEWAY_ID = UUID("10000000-0000-4000-8000-000000000001")


class RecordingRepository:
    def __init__(
        self,
        prices: dict[str, ModelPrice] | None = None,
        identities: dict[str, ModelIdentity] | None = None,
    ) -> None:
        self.records: list[TokenUsageRecord] = []
        self.execution_writes: list[dict[str, Any]] = []
        self.gateway_attributions: list[UsageGatewayAttribution] = []
        self.prices = prices or {}
        self.identities = identities or {}
        self.price_reads = 0
        self.identity_reads = 0

    def write_token_usage(
        self, record: TokenUsageRecord, application: object | None = None, *,
        gateway: UsageGatewayAttribution | None = None,
        terminal_observation: dict[str, Any] | None = None,
        execution_receipts: Sequence[LedgerAdmissionReceipt] = (),
        usage_complete: bool = False, gateway_profile_id: UUID | None = None,
        native_gateway_id: str | None = None, evidence_source: str = "apim",
    ) -> None:
        assert application is None
        self.records.append(record)
        if gateway is not None:
            self.gateway_attributions.append(gateway)
        if execution_receipts:
            self.execution_writes.append({
                "receipts": execution_receipts, "complete": usage_complete,
                "gateway_profile_id": gateway_profile_id, "native_gateway_id": native_gateway_id,
                "evidence_source": evidence_source,
                "terminal_observation": terminal_observation,
            })

    def model_prices(self) -> dict[str, ModelPrice]:
        self.price_reads += 1
        return self.prices

    def model_identities(self) -> dict[str, ModelIdentity]:
        self.identity_reads += 1
        return self.identities

    def get_gateway_publication(self, publication_id: UUID) -> None:
        del publication_id
        return None

    def gateway_application_attribution_map(
        self, gateway_profile_id: UUID
    ) -> list[dict[str, object]]:
        del gateway_profile_id
        return []

    def platform_request_person_recorded(self, request_id: str, user_id: str) -> bool:
        del request_id, user_id
        return False


def event(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "usage-1",
        "ts": datetime(2026, 7, 17, tzinfo=UTC).isoformat(),
        "team": "platform",
        "user": "alex",
        "agent": "delivery-engineer",
        "workflow": "pull-request-review",
        "run_id": "run-1",
        "turn_index": 1,
        "provider": "aoai",
        "model": "gpt-4.1-mini",
        "input_tokens": 1000,
        "cached_tokens": 200,
        "output_tokens": 100,
        "latency_ms": 1200,
        "status": "success",
        "ingest_source": "eventhub",
    }
    value.update(overrides)
    return value


def test_calculate_et() -> None:
    assert calculate_et(1000, 200, 100, 0.5) == 710


def schema_two_event(**overrides: object) -> dict[str, object]:
    correlation_id = "unit-correlation"
    service_id = (
        "/subscriptions/unit/resourceGroups/unit/providers/Microsoft.ApiManagement/service/unit"
    )
    usage_id = canonical_usage_id_v2(GATEWAY_ID, service_id, "managed", correlation_id)
    value = event(
        id=usage_id, usage_id=usage_id, event_schema_version=2, correlation_id=correlation_id,
        gateway_profile_id=str(GATEWAY_ID), native_gateway_id="managed",
        native_gateway_instance_id="managed", gateway_is_managed=True,
        apim_service_id=service_id, apim_region="Unit Region", api_revision="1",
    )
    value.update(overrides)
    return value


@pytest.mark.parametrize("changes", [
    {"usage_id": None}, {"id": "different"}, {"correlation_id": None},
    {"native_gateway_id": None}, {"gateway_is_managed": False},
    {"native_gateway_instance_id": "other"}, {"api_revision": None},
])
def test_gateway_schema_two_rejects_incomplete_or_conflicting_identity(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        UsageEvent.model_validate(schema_two_event(**changes))


def test_gateway_schema_two_canonical_identity_is_target_and_native_scoped() -> None:
    value = schema_two_event()
    parsed = UsageEvent.model_validate(value)
    assert parsed.usage_id == value["id"]
    assert canonical_usage_id_v2(GATEWAY_ID, " SERVICE ", " Managed ", " CORRELATION ") == (
        canonical_usage_id_v2(GATEWAY_ID, "service", "managed", "correlation")
    )
    assert canonical_usage_id_v2(GATEWAY_ID, "service", "managed", "correlation") != (
        canonical_usage_id_v2(GATEWAY_ID, "service", "unit-shgw", "correlation")
    )
    assert UsageEvent.model_validate(event()).event_schema_version == 1


def test_gateway_sidecar_and_exact_usd_share_postgres_usage_transaction() -> None:
    raw = schema_two_event()
    price = ModelPrice(input_price_per_million=10, cached_price_per_million=1,
                       cache_write_price_per_million=20, output_price_per_million=30)
    record = UsageProcessor(RecordingRepository(prices={"gpt-4.1-mini": price}),
                            CoefficientResolver({})).normalize(raw)
    assert record is not None and record.usd_cost == Decimal("0.0132")
    attribution = UsageGatewayAttribution.model_validate({
        "usage_id": record.id, "apim_target_id": GATEWAY_ID, "native_gateway_id": "managed",
        "is_managed": True, "apim_service_id": raw["apim_service_id"],
        "apim_region": raw["apim_region"], "api_revision": "1",
        "attribution_source": "eventhub", "observed_at": record.ts,
    })
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with patch.object(repository, "_connection") as connect, \
            patch.object(repository, "_write_usage_gateway_attribution") as sidecar:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = []
        repository.write_token_usage(record, gateway=attribution)
        connection.transaction.assert_called_once()
        sidecar.assert_called_once_with(connection, attribution)
        upsert = next(call for call in connection.execute.call_args_list
                      if "INSERT INTO token_usage AS existing" in call.args[0])
        assert upsert.args[1]["usd_cost"] == Decimal("0.0132")
        assert "ELSE existing.usd_cost END" in upsert.args[0]


def test_gateway_sidecar_failure_restores_in_memory_usage_and_receipt_state() -> None:
    record = UsageProcessor(RecordingRepository(), CoefficientResolver({})).normalize(
        schema_two_event(),
    )
    assert record is not None
    attribution = UsageGatewayAttribution.model_validate({
        "usage_id": record.id, "apim_target_id": GATEWAY_ID, "native_gateway_id": "managed",
        "is_managed": True, "apim_service_id": "unit-service", "apim_region": "unit",
        "api_revision": "1", "attribution_source": "eventhub", "observed_at": record.ts,
    })
    repository = InMemoryRepository()
    with pytest.raises(ValueError, match="Unknown APIM target"):
        repository.write_token_usage(record, gateway=attribution)
    assert not repository.usage_records
    assert not repository.usage_gateway_attributions
    assert not repository.budget_evidence_received_at


def test_gateway_schema_two_reader_rejects_wrong_digest_and_passes_real_identity() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({}))
    wrong_id = "turnstile-v2-" + "0" * 64
    assert processor.process(schema_two_event(id=wrong_id, usage_id=wrong_id)) is False
    assert not repository.records and not repository.gateway_attributions
    assert processor.process(schema_two_event()) is True
    attribution = repository.gateway_attributions[0]
    assert attribution.usage_id == repository.records[0].id == schema_two_event()["id"]
    assert attribution.apim_target_id == GATEWAY_ID
    assert attribution.native_gateway_id == "managed" and attribution.is_managed is True
    assert attribution.native_instance_id == "managed"
    assert processor.process(event(id="legacy", correlation_id="legacy")) is True
    assert len(repository.gateway_attributions) == 1


def execution_event(**overrides: object) -> dict[str, object]:
    receipt = LedgerAdmissionReceipt(
        identity=LedgerAttemptIdentity(
            scope=LedgerScope(scope_type="person", scope_id="unit@example.com",
                              period_start=datetime(2026, 7, 1, tzinfo=UTC).date()),
            gateway_profile_id=GATEWAY_ID, native_gateway_id="managed",
            correlation_id="unit-correlation", model_id="unit-model",
            admitted_at=datetime(2026, 7, 17, tzinfo=UTC),
        ), epoch=UUID(int=1), reserved_tokens=2000,
    )
    result = schema_two_event(
        user_id="unit@example.com", model_id="unit-model", model="unit-model",
        correlation_id="unit-correlation", gateway_profile_id=str(GATEWAY_ID),
        native_gateway_id="managed", budget_execution=[receipt.model_dump(mode="json")],
        usage_complete=True, tokens_consumed=1300, cache_write_tokens=50,
    )
    result.update(overrides)
    return result


@pytest.mark.parametrize("source", ["apim", "observer"])
def test_execution_usage_wakeup_runs_only_after_durable_write(source: str) -> None:
    repository = RecordingRepository()
    completed = []

    def settle(usage_id: str) -> None:
        assert repository.records and repository.execution_writes
        completed.append(usage_id)

    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True,
                               settle_committed_usage=settle)
    assert processor.process(execution_event(usage_event_source=source))
    assert completed == [schema_two_event()["id"]]
    assert repository.execution_writes[0]["complete"] is True
    assert repository.execution_writes[0]["evidence_source"] == source


@pytest.mark.parametrize("changes", [
    {"output_tokens": None}, {"cache_write_tokens": None}, {"tokens_consumed": 999},
    {"input_tokens": True}, {"cache_write_tokens": 201}, {"estimated": True},
    {"usage_complete": False}, {"ingest_error": "unit_unavailable"},
])
def test_execution_incomplete_usage_never_becomes_strong_evidence(changes: dict[str, Any]) -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    assert processor.process(execution_event(**changes))
    assert repository.execution_writes[0]["complete"] is False


def test_execution_disabled_reader_and_in_memory_repository_refuse_receipts() -> None:
    repository = RecordingRepository()
    with pytest.raises(RuntimeError, match="enabled schema-2 reader"):
        UsageProcessor(repository, CoefficientResolver({})).process(execution_event())
    assert not repository.records
    memory = InMemoryRepository()
    with pytest.raises(RuntimeError, match="durable PostgreSQL"):
        UsageProcessor(memory, CoefficientResolver({}), execution_enabled=True).process(
            execution_event()
        )
    assert not memory.usage_records


@pytest.mark.parametrize("changes", [
    {"ingest_source": "copilot_cli"}, {"request_source": "copilot-assistant"},
    {"usage_event_source": "backfill"},
])
def test_execution_rejects_untrusted_event_attribution_before_write(
    changes: dict[str, Any],
) -> None:
    repository = RecordingRepository()
    with pytest.raises(ValueError, match="trusted gateway attribution"):
        UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True).process(
            execution_event(**changes)
        )
    assert not repository.records


@pytest.mark.parametrize("changes", [{"native_gateway_id": None}, {"gateway_profile_id": None}])
def test_execution_missing_schema_two_attribution_is_skipped_before_write(
    changes: dict[str, Any],
) -> None:
    repository = RecordingRepository()
    assert UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True).process(
        execution_event(**changes),
    ) is False
    assert not repository.records


def test_execution_terminal_reader_retains_unknown_usage_and_observation() -> None:
    rejection = normalize_terminal_rejection(
        "microsoft-decision", 400, "application/json", {"error": {
            "type": "invalid_request_error", "code": "question_invalid", "param": None,
        }},
    )
    assert rejection is not None
    observed = {"source": "apim", "observed_at": datetime(2026, 7, 17, tzinfo=UTC).isoformat(),
                "rejection": rejection}
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
    assert processor.process(execution_event(
        input_tokens=None, cached_tokens=None, cache_write_tokens=None, output_tokens=None,
        tokens_consumed=None, estimated=True, usage_complete=False, status_code=400,
        status="400", budget_terminal=observed,
    )) is True
    assert repository.records[0].usd_cost is None and repository.records[0].estimated is True
    assert repository.execution_writes[0]["complete"] is False
    assert repository.execution_writes[0]["terminal_observation"] == observed


def test_execution_usd_admission_replay_cannot_change_reserved_amount() -> None:
    model_id = UUID(int=77)
    price = ModelPrice(input_price_per_million=10, cached_price_per_million=1,
                       cache_write_price_per_million=20, output_price_per_million=30)
    projection = BudgetPriceProjection.create(model_id, price, datetime(2026, 7, 17, tzinfo=UTC))
    raw = execution_event(model_id=str(model_id))
    receipts = cast(list[dict[str, Any]], raw["budget_execution"])
    receipt = LedgerAdmissionReceipt.model_validate({
        **receipts[0], "identity": {**receipts[0]["identity"], "model_id": str(model_id)},
        "budget_mode": "usd", "reserved_usd": "0.5",
        "price_snapshot": projection.model_dump(mode="json"),
    })
    record = UsageProcessor(RecordingRepository(), CoefficientResolver({})).normalize({
        **raw, "budget_execution": [receipt.model_dump(mode="json")],
    })
    assert record is not None
    connection = MagicMock()
    connection.execute.return_value.fetchone.side_effect = [
        {"epoch": receipt.epoch, "state": "active", "admitted_after": receipt.identity.admitted_at},
        {"identity": receipt.identity.model_dump(mode="json"), "epoch": receipt.epoch,
         "reserved_tokens": receipt.reserved_tokens, "budget_mode": "usd",
         "reserved_usd": Decimal("0.6"), "price_snapshot": projection.model_dump(mode="json")},
    ]
    gateway = UsageGatewayAttribution(
        usage_id=record.id, apim_target_id=GATEWAY_ID, native_gateway_id="managed",
        native_instance_id="managed", is_managed=True, apim_service_id="unit-service",
        apim_region="unit-region", api_revision="1",
        attribution_source=UsageGatewayAttributionSource.EVENT_HUB,
        observed_at=record.ts,
    )
    with pytest.raises(ValueError, match="admission is immutable"):
        PostgreSqlBudgetExecutionMixin._write_execution_receipts(
            connection, record, record.id, None, gateway, [receipt], True,
        )
    assert not any("INSERT INTO budget_execution_attempt" in call.args[0]
                   for call in connection.execute.call_args_list)


def test_usd_normalization_preserves_admitted_price_instead_of_current_registry() -> None:
    model_id = UUID(int=77)
    price = ModelPrice(input_price_per_million=10, cached_price_per_million=1,
                       cache_write_price_per_million=20, output_price_per_million=30)
    projection = BudgetPriceProjection.create(model_id, price, datetime(2026, 7, 17, tzinfo=UTC))
    raw = schema_two_event(model_id=str(model_id), model="unit-priced-model", input_tokens=100,
                           cached_tokens=200, cache_write_tokens=50, output_tokens=100,
                           tokens_consumed=400, usage_complete=True, usd_cost="0.00515",
                           budget_price_projection=projection.model_dump(mode="json"))
    repository = RecordingRepository(prices={str(model_id): ModelPrice(
        input_price_per_million=999, cached_price_per_million=999,
        cache_write_price_per_million=999, output_price_per_million=999,
    )})
    record = UsageProcessor(repository, CoefficientResolver({})).normalize(raw)
    assert record is not None
    assert record.id == raw["usage_id"] and record.usd_cost == Decimal("0.00515")
    assert record.usd_price_sha256 == projection.price_sha256
    assert record.usd_price_updated_at == projection.updated_at
    assert record.input_price_per_million == 10
    with pytest.raises(ValueError, match="admission price snapshot"):
        UsageProcessor(repository, CoefficientResolver({})).normalize(
            {**raw, "usd_cost": "0.00516"},
        )


@pytest.mark.parametrize("changes", [{"output_tokens": None}, {"input_tokens": True},
                                    {"tokens_consumed": 999}, {"estimated": True}])
def test_usd_unknown_or_contradictory_usage_keeps_amount_null(changes: dict[str, Any]) -> None:
    repository = RecordingRepository(prices={"gpt-4.1-mini": ModelPrice(
        input_price_per_million=10, cached_price_per_million=1,
        cache_write_price_per_million=20, output_price_per_million=30,
    )})
    record = UsageProcessor(repository, CoefficientResolver({})).normalize(event(**changes))
    assert record is not None and record.usd_cost is None


def test_execution_failed_wakeup_keeps_committed_usage_for_outbox_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = RecordingRepository()
    wakeup = MagicMock(side_effect=RuntimeError("private test details"))
    processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True,
                               settle_committed_usage=wakeup)
    assert processor.process(execution_event())
    assert len(repository.records) == 1
    assert "RuntimeError" in caplog.text and "private test details" not in caplog.text


def test_postgres_execution_receipts_share_usage_transaction_and_retained_id() -> None:
    record = UsageProcessor(RecordingRepository(), CoefficientResolver({})).normalize(
        event(user_id="unit@example.com", correlation_id="unit-correlation")
    )
    assert record is not None
    receipt = LedgerAdmissionReceipt(
        identity=LedgerAttemptIdentity(
            scope=LedgerScope(scope_type="person", scope_id=record.user_id,
                              period_start=record.ts.date().replace(day=1)),
            gateway_profile_id=GATEWAY_ID, native_gateway_id="managed",
            correlation_id=record.correlation_id, model_id=record.model_id,
            admitted_at=record.ts,
        ), epoch=UUID(int=1), reserved_tokens=2000,
    )
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [{
        "id": "retained-usage-id", "correlation_id": record.correlation_id,
        "usage_domain": "apim", "user_id": record.user_id,
    }]
    context = MagicMock()
    context.__enter__.return_value = connection
    repository = PostgreSqlOpsDbProxy("postgresql://unused", pool_min_size=0)
    with (
        patch.object(repository, "_connection", return_value=context),
        patch.object(repository, "_write_execution_receipts",
                     side_effect=ValueError("receipt rejected")) as write,
        pytest.raises(ValueError, match="receipt rejected"),
    ):
        repository.write_token_usage(
            record, execution_receipts=[receipt], usage_complete=True,
            gateway_profile_id=GATEWAY_ID, native_gateway_id="managed",
        )
    assert write.call_args.args[:3] == (connection, record, "retained-usage-id")
    assert context.__exit__.call_args.args[0] is ValueError
    assert connection.commit.call_count == 0


def register_publication_probe(repository: InMemoryRepository) -> None:
    ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=GATEWAY_ID,
            discovered_at=datetime(2026, 9, 18, tzinfo=UTC),
            items=[GatewayApplicationDiscoveryItem(
                apim_subscription_id="unit-publisher-probe", display_name="Unit Publisher",
                state="active", scope_type="api", scope_id="unit-api", scope_exists=True,
                application_type="system", system_managed=True,
            ), GatewayApplicationDiscoveryItem(
                apim_subscription_id="unit-service", display_name="Unit Service",
                state="active", scope_type="api", scope_id="unit-api", scope_exists=True,
                application_type="service", system_managed=False,
            )],
        ),
        "application-sync-worker",
    )


@pytest.mark.parametrize("delayed", [False, True])
def test_publication_probe_uses_frozen_identity_and_price_before_activation(delayed: bool) -> None:
    repository = InMemoryRepository()
    register_publication_probe(repository)
    request = foundry_publication()
    request.provider = request.provider.model_copy(update={
        "existing_id": next(item["id"] for item in repository.providers
                            if item["brand_key"] == "microsoft_foundry"),
        "template": None,
    })
    request.model = request.model.model_copy(update={
        "input_cost_per_million": 0.25, "cached_cost_per_million": 0.025,
        "cache_write_cost_per_million": 0.25, "output_cost_per_million": 2.0,
    })
    publication = GatewayControlPlaneService(
        repository, apim_principal_id=str(UUID(int=90)),
    ).publish(request, "owner@example.com")
    binding = publication.desired_spec.bindings[-1]
    model_id = str(publication_model_id(publication.id, binding.model.model_key))
    assert model_id not in repository.model_prices()
    requests: list[httpx.Request] = []

    def handler(outbound: httpx.Request) -> httpx.Response:
        requests.append(outbound)
        if outbound.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": item.id} for item in publication.desired_spec.discovery_models
            ]})
        assert outbound.method == "POST"
        if outbound.url.path.endswith("/responses/compact"):
            return httpx.Response(200, json={
                "object": "response.compaction", "output": [],
                "usage": {"input_tokens": 11, "output_tokens": 10, "total_tokens": 21},
            })
        if outbound.url.path.endswith("/responses"):
            return httpx.Response(200, json={"output": []})
        if outbound.url.path.endswith("/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    settings = publisher_settings().model_copy(update={
        "apim_probe_subscription_key": SecretStr("unit-probe-key"),
    })
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = AzureApimPublisherClient(settings, cast(Any, StubTokenProvider()), transport)
        client.probe_revision("unit-pricing-review", publication)
    probes = [outbound for outbound in requests if outbound.method == "POST"
              and json.loads(outbound.content)["model"] == binding.model.model_key]
    assert probes
    if delayed:
        repository.gateway_publications[-1]["status"] = "superseded"
        current_model = dict(repository.models[0])
        current_model.update(
            id=UUID(model_id), model_key=binding.model.model_key, display_name="Later name",
            input_cost_per_million=99, output_cost_per_million=99,
        )
        repository.models.append(current_model)
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    headers = probes[0].headers
    payload = event(
        request_source=headers["x-request-source"], user_id=headers["x-user-id"],
        gateway_profile_id=str(GATEWAY_ID), apim_subscription_id="unit-publisher-probe",
        run_id=headers["x-hive-run-id"], model_id=headers["x-model-id"],
        model=headers["x-hive-model"],
        input_tokens=11, cached_tokens=0, output_tokens=10, estimated_cost=99,
    )
    record = processor.normalize(payload)
    assert record is not None
    assert record.model_id == model_id
    assert record.model == binding.model.display_name
    assert record.input_price_per_million == 0.25
    assert record.output_price_per_million == 2.0
    assert record.estimated_cost == 0.00002275
    assert record.ingest_error is None
    for index, probe in enumerate(probes):
        observed = processor.normalize({
            **payload, "id": f"probe-{index}",
            "run_id": probe.headers["x-hive-run-id"],
            "model_id": probe.headers["x-model-id"], "model": probe.headers["x-hive-model"],
        })
        assert observed is not None and observed.model_id == model_id
        assert observed.estimated_cost == 0.00002275
        assert observed.ingest_error is None
    for overrides in (
        {"model_id": str(UUID(int=981))},
        {"model": "another-model"},
        {"run_id": str(UUID(int=982))},
        {"apim_subscription_id": "unregistered-subscription"},
        {"apim_subscription_id": None},
        {"gateway_profile_id": None},
        {"apim_subscription_id": "unit-service", "application_actor_type": "service"},
    ):
        rejected = processor.normalize({**payload, **overrides})
        assert rejected is not None
        assert rejected.input_price_per_million is None
        assert rejected.estimated_cost == 0
        assert rejected.ingest_error == "publication_probe_snapshot_unavailable"


def test_publication_probe_resolves_the_selected_existing_binding_not_the_first() -> None:
    repository = InMemoryRepository()
    register_publication_probe(repository)
    request = foundry_publication()
    request.provider = request.provider.model_copy(update={
        "existing_id": next(item["id"] for item in repository.providers
                            if item["brand_key"] == "microsoft_foundry"),
        "template": None,
    })
    publication = GatewayControlPlaneService(
        repository, apim_principal_id=str(UUID(int=90)),
    ).publish(request, "owner@example.com")
    template = publication.desired_spec.bindings[-1]
    bindings = [template.model_copy(update={
        "model": template.model.model_copy(update={
            "model_key": model["model_key"], "display_name": model["display_name"],
            "input_cost_per_million": rate, "output_cost_per_million": rate * 2,
        }),
    }) for rate, model in enumerate(repository.models[:2], start=1)]
    repository.gateway_publications[-1].update(
        publication_kind="route_reconcile",
        desired_spec={
            **publication.desired_spec.model_dump(mode="json"),
            "bindings": [binding.model_dump(mode="json") for binding in bindings],
            "discovery_models": [{
                "id": binding.model.model_key, "display_name": binding.model.display_name,
                "api_format": binding.api_format.value,
            } for binding in bindings],
        },
    )
    selected = bindings[-1]
    identity = repository.model_identities()[selected.model.model_key]
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(
        request_source="gateway-publication-probe", user_id="system-gateway-publication",
        gateway_profile_id=str(GATEWAY_ID), apim_subscription_id="unit-publisher-probe",
        run_id=str(publication.id), model_id=identity.model_id, model=selected.model.model_key,
        input_tokens=1000, cached_tokens=0, output_tokens=100,
    ))
    assert record is not None
    assert record.model_id == identity.model_id
    assert record.model == selected.model.display_name
    assert record.input_price_per_million == 2
    assert record.output_price_per_million == 4
    assert record.estimated_cost == 0.0024
    assert record.ingest_error is None


@pytest.mark.parametrize("priced,recorded_cost,available", [
    (False, 0, False), (True, 0, True), (False, 0.123, True),
])
def test_usage_cost_availability_and_details_belong_to_the_recorded_snapshot(
    priced: bool, recorded_cost: float, available: bool,
) -> None:
    from turnstile_core.domain.models import UsageRequestDetail

    repository = InMemoryRepository()
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event())
    assert record is not None
    record = record.model_copy(update={
        "estimated_cost": recorded_cost,
        "input_price_per_million": 0 if priced else None,
        "cached_price_per_million": 0 if priced else None,
        "cache_write_price_per_million": 0 if priced else None,
        "output_price_per_million": 0 if priced else None,
    })
    repository.write_token_usage(record)
    detail = repository.get_usage_request(record.correlation_id)
    assert detail is not None and detail["cost_available"] is available
    parsed = UsageRequestDetail.model_validate(detail)
    assert parsed.estimated_cost == recorded_cost
    assert parsed.input_price_per_million == record.input_price_per_million
    assert parsed.price_context_snapshot == record.price_context_snapshot
    repository.models.clear()
    assert repository.get_usage_request(record.correlation_id) == detail


@pytest.mark.parametrize(
    "fields",
    [
        {"input_tokens": True},
        {"input_tokens": "1000"},
        {"input_tokens": 1000.0},
        {"cache_write_tokens": 201},
        {"tokens_consumed": 9},
    ],
)
def test_invalid_usage_cannot_claim_exact_evidence(fields: dict[str, object]) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(**fields))
    assert record is not None
    assert record.estimated
    assert record.ingest_error == "usage_validation_failed"


def test_coefficient_snapshot_is_written() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"aoai/gpt-4.1-mini": 0.35}))
    assert processor.process(event()) is True
    assert repository.records[0].et_coeff_m == 0.35
    assert repository.records[0].et == 497
    assert repository.records[0].request_id == "usage-1"
    assert repository.records[0].correlation_id == "usage-1"
    assert repository.records[0].status_code == 200


def test_enterprise_attribution_and_failure_fields_are_preserved() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    raw = event(
        request_id="request-42",
        correlation_id="apim-42",
        organization="Contoso",
        organization_id="org-1",
        department="Platform",
        department_id="department-1",
        project="FinOps",
        project_id="project-1",
        agent_id="agent-1",
        user_id="user-1",
        model_id="deployment-1",
        request_source="agent-console",
        status="429",
        status_code=429,
        estimated_cost=0.0123,
        error_message="rate limited",
    )
    assert processor.process(raw) is True
    record = repository.records[0]
    assert record.organization_id == "org-1"
    assert record.department_id == "department-1"
    assert record.project_id == "project-1"
    assert record.agent_id == "agent-1"
    assert record.user_id == "user-1"
    assert record.model_id == "deployment-1"
    assert record.request_source == "agent-console"
    assert record.usage_domain == "apim"
    assert record.status_code == 429
    assert record.estimated_cost == 0.0123
    assert record.error_message == "rate limited"


def test_apim_subscription_is_resolved_to_immutable_application_attribution() -> None:
    repository = InMemoryRepository()
    ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=GATEWAY_ID,
            discovered_at=datetime(2026, 8, 26, tzinfo=UTC),
            items=[
                GatewayApplicationDiscoveryItem(
                    apim_subscription_id="outline-assistant",
                    display_name="Outline Assistant",
                    state="active",
                    scope_type="product",
                    scope_id="finops-applications",
                    scope_exists=True,
                    application_type="service",
                    system_managed=False,
                )
            ],
        ),
        "application-sync-worker",
    )
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(
        event(
            id="application-caller-request",
            request_id="application-caller-request",
            correlation_id="application-attempt",
            gateway_profile_id=str(GATEWAY_ID),
            apim_subscription_id="outline-assistant",
            application_actor_type="service",
            application_actor_id="service:outline-assistant",
            application_admission="ok",
        )
    )

    attribution = repository.usage_application_attributions["application-attempt"]
    assert attribution.application_name_snapshot == "Outline Assistant"
    assert attribution.actor_type == "service"
    assert attribution.actor_id == "service:outline-assistant"
    assert attribution.application_admission == "ok"


def _service_application_repository() -> InMemoryRepository:
    repository = InMemoryRepository()
    ApplicationAccessService(repository, sync_available=True).sync_discovery(
        GatewayApplicationDiscovery(
            gateway_profile_id=GATEWAY_ID,
            discovered_at=datetime(2026, 8, 26, tzinfo=UTC),
            items=[GatewayApplicationDiscoveryItem(
                apim_subscription_id="outline-assistant", display_name="Outline Assistant",
                state="active", scope_type="product", scope_id="finops-applications",
                scope_exists=True, application_type="service", system_managed=False,
            )],
        ),
        "application-sync-worker",
    )
    return repository


def _application_event(request_id: str, **overrides: object) -> dict[str, object]:
    return event(**{
        "id": request_id, "request_id": request_id, "correlation_id": request_id + "-attempt",
        "gateway_profile_id": str(GATEWAY_ID), "apim_subscription_id": "outline-assistant",
        "application_actor_type": "service",
        "application_actor_id": "service:outline-assistant",
        "user_id": "ceo@contoso.com", "user": "CEO", **overrides,
    })


def test_application_key_cannot_name_a_person_or_spend_their_budget() -> None:
    repository = _service_application_repository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(_application_event("spoofed"))

    record = next(row for row in repository.usage_records if row.request_id == "spoofed")
    assert (record.user_id, record.user) == ("unattributed", "unattributed")
    assert repository.usage_application_attributions["spoofed-attempt"].person_id is None
    assert repository.budget_scope_confirmed_tokens(
        "person", "ceo@contoso.com", date(2026, 7, 1), date(2026, 8, 1),
    ) == 0


def test_verified_people_keep_attribution_on_application_traffic() -> None:
    repository = _service_application_repository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    attempt = invocation_attempt("platform-call")
    repository.begin_platform_invocation(attempt.model_copy(update={
        "metadata": attempt.metadata.model_copy(update={"user_id": "ceo@contoso.com"}),
    }))

    for request_id, overrides in (
        ("delegated", {"application_actor_type": "person",
                       "application_actor_id": "ceo@contoso.com"}),
        ("platform-call", {}),
    ):
        assert processor.process(_application_event(request_id, **overrides))
        record = next(row for row in repository.usage_records if row.request_id == request_id)
        assert (record.user_id, record.user) == ("ceo@contoso.com", "CEO")
    assert processor.process(_application_event("platform-call", user_id="cfo@contoso.com",
                                                correlation_id="other-person-attempt"))
    assert repository.usage_records[-1].user_id == "unattributed"


def test_copilot_assistant_events_are_classified_outside_the_apim_domain() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event(request_source="copilot-assistant")) is True
    assert repository.records[0].usage_domain == "github_copilot"


def test_stream_without_breakdown_is_persisted_without_inventing_a_split() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    raw = event(input_tokens=None, cached_tokens=None, output_tokens=None, tokens_consumed=1000)
    assert processor.process(raw) is True
    record = repository.records[0]
    assert record.input_tokens == 0
    assert record.cached_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated is True
    assert record.ingest_error == "stream_usage_breakdown_unavailable"


def test_apim_retries_keep_distinct_attempts_for_one_request() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    for correlation_id, status_code in (("attempt-first", 200), ("attempt-second", 429)):
        assert processor.process(
            event(
                id="caller-request",
                request_id="caller-request",
                correlation_id=correlation_id,
                status=str(status_code),
                status_code=status_code,
            )
        )

    assert [record.id for record in repository.usage_records] == [
        "attempt-first",
        "attempt-second",
    ]
    assert {record.request_id for record in repository.usage_records} == {"caller-request"}
    assert [record.status_code for record in repository.usage_records] == [200, 429]


def test_apim_redelivery_upgrades_one_attempt_despite_different_event_ids() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    identity = {"request_id": "caller-request", "correlation_id": "gateway-attempt"}

    assert processor.process(
        event(
            **identity,
            id="gateway-event",
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            budget_admission="ok",
        )
    )
    assert processor.process(event(**identity, id="observer-event", input_tokens=72))
    assert processor.process(event(**identity, id="redelivered-event", input_tokens=999))

    assert len(repository.usage_records) == 1
    record = repository.usage_records[0]
    assert record.id == "gateway-attempt"
    assert record.request_id == "caller-request"
    assert record.correlation_id == "gateway-attempt"
    assert record.estimated is False
    assert record.input_tokens == 72
    assert record.budget_admission == "ok"


@pytest.mark.parametrize("estimated", [False, True])
def test_apim_redelivery_preserves_pre_upgrade_identity(estimated: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    identity = {"request_id": "legacy-caller", "correlation_id": "legacy-attempt"}
    legacy = processor.normalize(event(**identity, id="legacy-caller", input_tokens=100))
    assert legacy is not None
    repository.write_token_usage(
        legacy.model_copy(update={"id": "legacy-caller", "estimated": estimated})
    )

    assert processor.process(event(**identity, id="replayed-event", input_tokens=72))
    assert processor.process(event(**identity, id="second-delivery", input_tokens=999))

    assert len(repository.usage_records) == 1
    stored = repository.usage_records[0]
    assert stored.id == "legacy-caller"
    assert stored.request_id == "legacy-caller"
    assert stored.correlation_id == "legacy-attempt"
    assert stored.estimated is False
    assert stored.input_tokens == (72 if estimated else 100)


@pytest.mark.parametrize("existing_id", [None, "legacy-caller"])
def test_postgres_reuses_attempt_identity_for_usage_and_attribution(
    existing_id: str | None,
) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(id="caller", correlation_id="gateway-attempt"))
    assert record is not None
    repository = object.__new__(PostgreSqlOpsDbProxy)
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = (
        [
            {
                "id": existing_id,
                "correlation_id": record.correlation_id,
                "usage_domain": "apim",
                "user_id": record.user_id,
            }
        ]
        if existing_id is not None
        else []
    )
    application = cast(UsageApplicationAttribution, MagicMock(application_id=UUID(int=88)))
    expected_id = existing_id or record.id
    with (
        patch.object(repository, "_connection") as connect,
        patch.object(repository, "_write_usage_application_attribution") as attribute,
    ):
        connect.return_value.__enter__.return_value = connection
        repository.write_token_usage(record, application)
        attribute.assert_called_once_with(connection, expected_id, application)

    assert connection.execute.call_count == 5
    person_lock, application_lock, lock, lookup, write = connection.execute.call_args_list
    assert person_lock.args[1] == (
        f"budget-reservation:person:{record.user_id}:{record.correlation_id}",
    )
    assert application_lock.args[1] == (
        f"budget-reservation:application:{application.application_id}:{record.correlation_id}",
    )
    assert lock.args[1] == (f"apim-attempt:{record.correlation_id}",)
    assert "LIMIT 2 FOR UPDATE" in lookup.args[0]
    assert lookup.args[1] == (record.correlation_id, record.id)
    assert write.args[1]["id"] == expected_id
    assert write.args[1]["request_id"] == record.request_id
    assert write.args[1]["correlation_id"] == record.correlation_id
    assert record.id == "gateway-attempt"


@pytest.mark.parametrize("observer_first", (False, True))
def test_runtime_authority_wins_both_event_orders_without_repricing(observer_first: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    logical = event(id="runtime-order", runtime="Logical primary", input_tokens=120)
    measured = event(
        id="runtime-order", runtime="Measured secondary", input_tokens=240,
        runtime_authoritative=True,
    )
    first, second = (measured, logical) if observer_first else (logical, measured)
    assert processor.process(first)
    before = repository.usage_records[-1].model_dump()
    assert processor.process(second)
    assert repository.usage_records[-1].model_dump() == {**before, "runtime": "Measured secondary"}
    assert len(repository.usage_records) == 1
    assert "runtime_authoritative" not in repository.usage_records[-1].model_dump()


@pytest.mark.parametrize("has_admission", (False, True))
def test_runtime_only_correction_keeps_every_other_exact_field(has_admission: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    logical = event(
        id="runtime-only", runtime="Logical primary", input_tokens=123,
        cache_write_tokens=50, estimated_cost=1.25,
        budget_admission="ok" if has_admission else None,
        model_admission="ok" if has_admission else None,
    )
    observed = event(
        id="runtime-only", runtime="Measured secondary", runtime_authoritative=True,
        model="different-unpriced-model", input_tokens=456, cached_tokens=600,
        cache_write_tokens=80, output_tokens=900, latency_ms=9999, estimated_cost=99.0,
    )
    assert processor.process(logical)
    expected = {**repository.usage_records[-1].model_dump(), "runtime": "Measured secondary"}
    for incoming in (observed, observed, logical):
        assert processor.process(incoming)
        assert repository.usage_records[-1].model_dump() == expected
    assert len(repository.usage_records) == 1


@pytest.mark.parametrize("overrides", (
    {"estimated": True}, {"input_tokens": None}, {"runtime": "unattributed"},
    {"runtime": ""}, {"ingest_source": "copilot_cli"}, {"ingest_source": "gateway"},
    {"request_source": "copilot-assistant-tools"},
))
def test_runtime_authority_rejects_unmeasured_or_non_apim_sources(
    overrides: dict[str, object],
) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    raw = event(runtime="Measured runtime", runtime_authoritative=True)
    raw.update(overrides)
    record = processor.normalize(raw)
    assert record is not None
    assert record.runtime_authoritative is False


@pytest.mark.parametrize("authoritative", (False, True))
def test_postgres_runtime_authority_is_a_transient_bound_parameter(authoritative: bool) -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(
        runtime="Measured runtime", runtime_authoritative=authoritative,
        request_id="caller", correlation_id="attempt",
    ))
    assert record is not None
    repository = object.__new__(PostgreSqlOpsDbProxy)
    with patch.object(repository, "_connection") as connect:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchall.return_value = [
            {
                "id": "legacy-row",
                "correlation_id": record.correlation_id,
                "usage_domain": "apim",
                "user_id": record.user_id,
            }
        ]
        repository.write_token_usage(record)
    statement, values = connection.execute.call_args.args
    assert values["id"] == "legacy-row"
    assert values["runtime_authoritative"] is authoritative
    assert values["runtime"] == record.runtime
    assert "runtime_authoritative" not in record.model_dump()
    assert "runtime_authoritative" not in statement.split(") VALUES", 1)[0]
    assert "existing.runtime IS DISTINCT FROM EXCLUDED.runtime" in statement
    assert "input_tokens = CASE WHEN existing.estimated" in statement
    assert connection.execute.call_count == 4


@pytest.mark.parametrize("failure", ["ambiguous", "identity", "attribution"])
def test_identity_failure_preserves_usage_attribution_and_receipt(failure: str) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(correlation_id="conflict", estimated=True))
    assert record is not None
    attribution = UsageApplicationAttribution(
        application_id=GATEWAY_ID,
        application_subscription_id=GATEWAY_ID,
        application_name_snapshot="Unit",
        apim_subscription_id="unit",
        actor_type="service",
        actor_id="service:unit",
    )
    repository.usage_records.append(record)
    repository.usage_application_attributions[record.id] = attribution
    incoming = record.model_copy(update={"estimated": False, "input_tokens": 90})
    if failure == "ambiguous":
        repository.usage_records.append(record.model_copy(update={"id": "duplicate"}))
    elif failure == "identity":
        incoming = incoming.model_copy(update={"user_id": "other-person"})
    else:
        attribution = attribution.model_copy(update={"application_id": UUID(int=2)})
    before = (list(repository.usage_records), dict(repository.usage_application_attributions))
    with pytest.raises(ValueError):
        repository.write_token_usage(incoming, attribution)
    assert (repository.usage_records, repository.usage_application_attributions) == before
    assert repository.budget_evidence_received_at == {}
    assert repository.budget_evidence_conflicts == set()


@pytest.mark.parametrize("legacy", [False, True])
def test_receipt_time_is_registered_once_after_identity_validation(legacy: bool) -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    record = processor.normalize(event(correlation_id="receipt"))
    assert record is not None
    initial = record.model_copy(update={"id": "legacy"}) if legacy else record
    received = datetime(2026, 9, 12, 12, tzinfo=UTC)
    with patch("turnstile_core.persistence.in_memory.datetime", wraps=datetime) as clock:
        clock.now.return_value = received
        repository.write_token_usage(initial)
        clock.now.return_value = datetime(2026, 9, 12, 13, tzinfo=UTC)
        repository.write_token_usage(record)
    assert repository.budget_evidence_received_at == {"usage:" + initial.id: received}
    assert repository.usage_records == [initial]


def test_copilot_identity_and_missing_correlation_fallbacks_are_preserved() -> None:
    processor = UsageProcessor(RecordingRepository(), CoefficientResolver({"default": 1.0}))
    cases: tuple[tuple[dict[str, object], str, str], ...] = (
        ({"request_id": "caller-request"}, "caller-request", "apim"),
        ({}, "source-event", "apim"),
        (
            {"ingest_source": "copilot_cli", "correlation_id": "shared-correlation"},
            "source-event",
            "github_copilot",
        ),
        (
            {"request_source": "copilot-assistant-tools", "correlation_id": "shared-correlation"},
            "source-event",
            "github_copilot",
        ),
    )
    for overrides, expected_id, expected_domain in cases:
        record = processor.normalize(event(id="source-event", **overrides))
        assert record is not None
        assert record.id == expected_id
        assert record.usage_domain == expected_domain


def test_exact_adapter_event_upgrades_only_the_estimated_placeholder() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(
        event(
            id="shared-request",
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            tokens_consumed=1000,
            budget_admission="ok",
            model_admission="ok",
        )
    )
    assert processor.process(
        event(
            id="shared-request",
            input_tokens=72,
            cached_tokens=1128,
            cache_write_tokens=128,
            output_tokens=30,
            estimated=False,
            ingest_source="eventhub",
        )
    )
    assert processor.process(
        event(
            id="shared-request",
            input_tokens=999,
            cached_tokens=0,
            output_tokens=999,
            estimated=False,
        )
    )

    matching = [record for record in repository.usage_records if record.id == "shared-request"]
    assert len(matching) == 1
    assert matching[0].input_tokens == 72
    assert matching[0].cached_tokens == 1128
    assert matching[0].cache_write_tokens == 128
    assert matching[0].output_tokens == 30
    assert matching[0].estimated is False
    assert matching[0].budget_admission == "ok"
    assert matching[0].model_admission == "ok"


def test_unattributed_budget_protocol_fenced_probe_is_skipped_and_filtered() -> None:
    from turnstile_core.persistence.repository import UsageFilters

    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    fenced_probe = event(
        id="fenced-probe-1",
        request_id="fenced-probe-1",
        correlation_id="fenced-probe-1",
        team="unattributed",
        organization="unattributed",
        organization_id="unattributed",
        department="unattributed",
        department_id="unattributed",
        project="unattributed",
        project_id="unattributed",
        user="unattributed",
        user_id="unattributed",
        agent="unattributed",
        agent_id="unattributed",
        workflow="unattributed",
        run_id="unattributed",
        provider="unattributed",
        model="unattributed",
        model_id="unattributed",
        runtime="unattributed",
        request_source="unattributed",
        input_tokens=None,
        cached_tokens=None,
        output_tokens=None,
        status="503",
        status_code=503,
        estimated=True,
        ingest_source="eventhub",
        ingest_error="budget_pre_dispatch_denial",
        budget_admission="budget_protocol_fenced",
    )
    assert processor.process(fenced_probe) is False
    assert repository.usage_records == []

    # Even if a historical row already exists in storage, observability queries exclude it.
    historical = processor.normalize(fenced_probe)
    assert historical is not None
    repository.usage_records.append(historical)
    attributed_denial = processor.normalize(
        {
            **fenced_probe,
            "id": "attributed-denial-1",
            "request_id": "attributed-denial-1",
            "correlation_id": "attributed-denial-1",
            "user_id": "user@example.com",
            "user": "user@example.com",
        }
    )
    assert attributed_denial is not None
    assert processor.process(
        {
            **fenced_probe,
            "id": "attributed-denial-1",
            "request_id": "attributed-denial-1",
            "correlation_id": "attributed-denial-1",
            "user_id": "user@example.com",
            "user": "user@example.com",
        }
    ) is True
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)
    listed = repository.list_usage_requests(start, end, UsageFilters(), 50)
    assert [row["request_id"] for row in listed] == ["attributed-denial-1"]
    overview = repository.executive_overview(start, end, UsageFilters())
    assert overview["totals"]["total_requests"] == 1
    sql, params = PostgreSqlOpsDbProxy._filter_sql(UsageFilters())
    assert "budget_pre_dispatch_denial" in sql
    assert "budget_protocol_fenced" in sql
    assert params == []


def test_later_apim_exact_event_adds_admission_without_replacing_observer_usage() -> None:
    repository = InMemoryRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(
        event(
            id="exact-first",
            input_tokens=72,
            cached_tokens=1128,
            cache_write_tokens=128,
            output_tokens=30,
            estimated=False,
            ingest_source="eventhub",
        )
    )
    assert processor.process(
        event(
            id="exact-first",
            input_tokens=999,
            cached_tokens=0,
            output_tokens=999,
            estimated=False,
            budget_admission="ok",
            model_admission="ok",
        )
    )

    matching = [record for record in repository.usage_records if record.id == "exact-first"]
    assert len(matching) == 1
    assert matching[0].input_tokens == 72
    assert matching[0].cached_tokens == 1128
    assert matching[0].cache_write_tokens == 128
    assert matching[0].output_tokens == 30
    assert matching[0].budget_admission == "ok"
    assert matching[0].model_admission == "ok"


def test_postgres_upsert_preserves_exact_observer_usage_when_admission_arrives() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "turnstile_core/persistence/repository.py"
    ).read_text(encoding="utf-8")

    assert "input_tokens = CASE WHEN existing.estimated" in source
    assert "THEN EXCLUDED.input_tokens ELSE existing.input_tokens END" in source
    assert "runtime = CASE WHEN existing.estimated" in source
    assert "THEN EXCLUDED.runtime ELSE existing.runtime END" in source
    assert "existing.budget_admission, EXCLUDED.budget_admission" in source
    assert "existing.model_admission, EXCLUDED.model_admission" in source


def test_malformed_event_does_not_raise_or_write() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process({"id": "broken"}) is False
    assert repository.records == []


def test_missing_usage_is_persisted_and_flagged() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert (
        processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None)) is True
    )
    record = repository.records[0]
    assert record.input_tokens == 0
    assert record.cached_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated is True
    assert record.ingest_error == "usage_unavailable"


def sonnet_prices() -> dict[str, ModelPrice]:
    return {
        "gpt-4.1-mini": ModelPrice(
            input_price_per_million=3.0,
            cached_price_per_million=0.3,
            cache_write_price_per_million=3.75,
            output_price_per_million=15.0,
        )
    }


def context_price() -> ModelPrice:
    return ModelPrice(
        input_price_per_million=0, cached_price_per_million=0,
        cache_write_price_per_million=0, output_price_per_million=0,
        context_tiers=[
            ModelPriceTier(
                key="short", min_input_tokens=0, max_input_tokens=1200,
                input_price_per_million=3, cached_price_per_million=0.3,
                cache_write_price_per_million=3.75, output_price_per_million=15,
            ),
            ModelPriceTier(
                key="long", min_input_tokens=1201,
                input_price_per_million=6, cached_price_per_million=0.6,
                cache_write_price_per_million=7.5, output_price_per_million=22.5,
            ),
        ],
    )


@pytest.mark.parametrize("input_tokens,cached_tokens,key", [
    (0, 0, "short"), (1000, 200, "short"), (1001, 200, "long"),
    (0, 1201, "long"), (1201, 0, "long"),
])
def test_context_price_selects_by_total_input_and_prices_the_whole_request(
    input_tokens: int, cached_tokens: int, key: str,
) -> None:
    price = context_price()
    selected = price.for_input_tokens(input_tokens + cached_tokens)
    assert selected.selected_tier == key
    writes = min(100, cached_tokens)
    output_tokens = 9999
    expected = (
        input_tokens * selected.input_price_per_million
        + (cached_tokens - writes) * selected.cached_price_per_million
        + writes * selected.cache_write_price_per_million
        + output_tokens * selected.output_price_per_million
    ) / 1_000_000
    assert price.cost(input_tokens, cached_tokens, output_tokens, writes) == round(expected, 8)
    assert price.selected_tier is None


@pytest.mark.parametrize("minimum,maximum", [(1200, None), (1202, None), (1201, 9000)])
def test_context_price_rejects_overlapping_missing_or_unbounded_ranges(
    minimum: int, maximum: int | None,
) -> None:
    data = context_price().model_dump()
    data["context_tiers"][1].update(min_input_tokens=minimum, max_input_tokens=maximum)
    with pytest.raises(ValueError):
        ModelPrice.model_validate(data)


@pytest.mark.parametrize("input_tokens,key,rate", [(1000, "short", 3), (1001, "long", 6)])
def test_context_price_ingestion_snapshots_selected_rates_and_complete_schedule(
    input_tokens: int, key: str, rate: float,
) -> None:
    price = context_price()
    repository = RecordingRepository({"gpt-4.1-mini": price})
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(event(input_tokens=input_tokens, cache_write_tokens=150)) is True
    record = repository.records[0]
    assert record.price_context_tier == key
    assert record.input_price_per_million == rate
    assert record.price_context_snapshot == price.model_dump(mode="json")
    assert record.estimated_cost == price.cost(input_tokens, 200, 100, 150)
    price.context_tiers[0].input_price_per_million = 999
    assert record.price_context_snapshot["context_tiers"][0]["input_price_per_million"] == 3


def test_context_price_ingestion_does_not_guess_a_tier_for_missing_input_usage() -> None:
    price = context_price()
    repository = RecordingRepository({"gpt-4.1-mini": price})
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    record = repository.records[0]
    assert record.price_context_tier is None
    assert record.input_price_per_million is None
    assert record.estimated_cost == 0
    assert record.price_context_snapshot == price.model_dump(mode="json")
    assert record.estimated is True


def test_context_price_reconciliation_uses_original_rules_after_registry_price_changes() -> None:
    from turnstile_core.domain.models import ReconciledUsage

    repository = InMemoryRepository()
    prices = context_price()
    recording = RecordingRepository({"gpt-4.1-mini": prices})
    processor = UsageProcessor(recording, CoefficientResolver({"default": 1.0}))
    processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    original = recording.records[0]
    repository.write_token_usage(original)
    prices.context_tiers[1].input_price_per_million = 999
    item = ReconciledUsage(
        correlation_id=original.correlation_id, input_tokens=1001, cached_tokens=200,
        output_tokens=100,
    )
    assert repository.apply_reconciled_usage([item]) == 1
    saved = repository.usage_records[-1]
    assert saved.price_context_tier == "long"
    assert saved.input_price_per_million == 6
    assert saved.estimated_cost == context_price().cost(1001, 200, 100)
    assert saved.price_context_snapshot == original.price_context_snapshot
    assert repository.apply_reconciled_usage([item]) == 0


def test_context_price_late_formal_usage_preserves_first_price_schedule() -> None:
    repository = InMemoryRepository()
    recording = RecordingRepository({"gpt-4.1-mini": context_price()})
    processor = UsageProcessor(recording, CoefficientResolver({"default": 1.0}))
    processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    original = recording.records[0]
    repository.write_token_usage(original)
    recording.prices["gpt-4.1-mini"].context_tiers[1].input_price_per_million = 999
    processor.process(event(input_tokens=1001, cached_tokens=200, output_tokens=100))
    repository.write_token_usage(recording.records[-1])
    saved = repository.usage_records[-1]
    assert saved.price_context_tier == "long"
    assert saved.input_price_per_million == 6
    assert saved.price_context_snapshot == original.price_context_snapshot


@pytest.mark.parametrize("new_pricing", ["flat", "tiered"])
@pytest.mark.parametrize("originally_priced", [False, True])
@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_late_formal_usage_keeps_original_flat_prices(
    new_pricing: str, originally_priced: bool, backend: str,
) -> None:
    original_price = ModelPrice(
        input_price_per_million=2, cached_price_per_million=0.2,
        cache_write_price_per_million=2.5, output_price_per_million=8,
    )
    recording = RecordingRepository(
        {"gpt-4.1-mini": original_price} if originally_priced else {},
    )
    processor = UsageProcessor(recording, CoefficientResolver({"default": 1.0}))
    processor.process(event(input_tokens=None, cached_tokens=None, output_tokens=None))
    original = recording.records[-1]
    recording.prices["gpt-4.1-mini"] = (
        context_price() if new_pricing == "tiered"
        else original_price.model_copy(update={"input_price_per_million": 999})
    )
    UsageProcessor(recording, CoefficientResolver({"default": 1.0})).process(
        event(input_tokens=1001, cached_tokens=200, cache_write_tokens=50, output_tokens=100),
    )
    if backend == "memory":
        repository = InMemoryRepository()
        repository.write_token_usage(original)
        repository.write_token_usage(recording.records[-1])
        saved = repository.usage_records[-1].model_dump()
    else:
        proxy = PostgreSqlOpsDbProxy("postgresql://nobody@127.0.0.1:1/unused")
        with patch.object(proxy, "_connection") as connection_context:
            connection = connection_context.return_value.__enter__.return_value
            connection.execute.return_value.fetchall.return_value = [original.model_dump()]
            proxy.write_token_usage(recording.records[-1])
            upsert = next(call for call in connection.execute.call_args_list
                          if "INSERT INTO token_usage AS existing" in call.args[0])
            saved = upsert.args[1]
            assert "price_context_snapshot = existing.price_context_snapshot" in upsert.args[0]
    assert not saved["estimated"]
    assert saved.get("price_context_snapshot") is None
    assert saved.get("price_context_tier") is None
    assert saved["input_price_per_million"] == (2 if originally_priced else None)
    assert saved["cached_price_per_million"] == (0.2 if originally_priced else None)
    assert saved["cache_write_price_per_million"] == (2.5 if originally_priced else None)
    assert saved["output_price_per_million"] == (8 if originally_priced else None)
    expected = original_price.cost(1001, 200, 100, 50) if originally_priced else 0
    assert saved["estimated_cost"] == expected


def test_cost_is_priced_at_ingestion_with_the_cached_rate() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    # 1000 input at 3.00, 200 cached at 0.30, 100 output at 15.00.
    assert record.estimated_cost == round((1000 * 3.0 + 200 * 0.3 + 100 * 15.0) / 1_000_000, 8)
    assert record.input_price_per_million == 3.0
    assert record.cached_price_per_million == 0.3
    assert record.output_price_per_million == 15.0


def test_cache_writes_are_priced_above_cache_reads() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    # 200 cached tokens of which 150 were written, leaving 50 reads.
    assert processor.process(event(cache_write_tokens=150)) is True

    record = repository.records[0]
    assert record.cache_write_tokens == 150
    assert record.cache_write_price_per_million == 3.75
    assert record.estimated_cost == round(
        (1000 * 3.0 + 50 * 0.3 + 150 * 3.75 + 100 * 15.0) / 1_000_000, 8
    )


def test_an_unreported_split_prices_every_cached_token_as_a_read() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    assert record.cache_write_tokens == 0
    assert record.estimated_cost == round((1000 * 3.0 + 200 * 0.3 + 100 * 15.0) / 1_000_000, 8)


def test_a_write_count_above_the_cache_bucket_is_clamped() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    # The database constrains writes to the cache bucket, so a malformed event must not exceed it.
    assert processor.process(event(cache_write_tokens=9999)) is True

    record = repository.records[0]
    assert record.cache_write_tokens == 200
    assert record.estimated_cost == round((1000 * 3.0 + 200 * 3.75 + 100 * 15.0) / 1_000_000, 8)


def test_registry_id_takes_priority_over_the_model_key() -> None:
    prices = sonnet_prices()
    prices["deployment-1"] = ModelPrice(
        input_price_per_million=1.0,
        cached_price_per_million=0.5,
        cache_write_price_per_million=1.25,
        output_price_per_million=6.0,
    )
    repository = RecordingRepository(prices)
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event(model_id="deployment-1")) is True

    assert repository.records[0].input_price_per_million == 1.0


def test_unpriced_model_records_no_cost_and_no_prices() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    assert record.estimated_cost == 0
    assert record.input_price_per_million is None
    assert record.cached_price_per_million is None
    assert record.output_price_per_million is None


def test_registry_is_read_once_per_batch_not_per_event() -> None:
    repository = RecordingRepository(sonnet_prices())
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    for index in range(5):
        assert processor.process(event(id=f"usage-{index}")) is True

    assert repository.price_reads == 1
    assert repository.identity_reads == 1


def test_registry_uuid_and_model_key_collapse_onto_one_identity() -> None:
    identity = ModelIdentity(model_id="307940d3", display_name="Claude Sonnet 5")
    identities = {"307940d3": identity, "databricks-claude-sonnet-5": identity}
    repository = RecordingRepository(identities=identities)
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    # The dashboard BFF sends the registry UUID; an employee desktop client sends the model key.
    assert processor.process(event(id="bff", model_id="307940d3", model="gpt-name-drift")) is True
    assert processor.process(
        event(
            id="desktop",
            model_id="databricks-claude-sonnet-5",
            model="databricks-claude-sonnet-5",
        )
    ) is True

    assert [(item.model_id, item.model) for item in repository.records] == [
        ("307940d3", "Claude Sonnet 5"),
        ("307940d3", "Claude Sonnet 5"),
    ]


def test_a_model_missing_from_the_registry_keeps_the_caller_identifiers() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event(model_id="unknown-1", model="mystery-model")) is True

    record = repository.records[0]
    assert record.model_id == "unknown-1"
    assert record.model == "mystery-model"


def test_a_failing_registry_read_records_usage_without_a_cost() -> None:
    class FailingRepository(RecordingRepository):
        def model_prices(self) -> dict[str, ModelPrice]:
            raise RuntimeError("registry unavailable")

    repository = FailingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))

    assert processor.process(event()) is True

    record = repository.records[0]
    assert record.input_tokens == 1000
    assert record.estimated_cost == 0
    assert record.input_price_per_million is None


def test_failed_request_without_usage_is_persisted() -> None:
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(
        event(
            input_tokens=None,
            cached_tokens=None,
            output_tokens=None,
            status="502",
            status_code=502,
            error_message="upstream unavailable",
        )
    ) is True
    record = repository.records[0]
    assert record.total_tokens if hasattr(record, "total_tokens") else 0 == 0
    assert record.status_code == 502
    assert record.ingest_error == "failed_request_has_no_usage"
