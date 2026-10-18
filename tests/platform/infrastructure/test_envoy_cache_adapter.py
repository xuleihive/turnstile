from __future__ import annotations

import base64
import importlib.util
import json
import re
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from uuid import UUID

import pytest
import yaml  # type: ignore[import-untyped]

from tests.backend.telemetry.test_atomic_ledger import EPOCH, identity
from turnstile_core.domain.gateway_targets import canonical_usage_id_v2
from turnstile_core.domain.ledger import BudgetPriceProjection, LedgerAdmissionReceipt
from turnstile_core.domain.models import ModelPrice, UsageEvent

ROOT = Path(__file__).parents[3]
ADAPTER = ROOT / "infra" / "envoy-cache-adapter"


def load_shipper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("envoy_cache_shipper", ADAPTER / "shipper.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def access_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": "2026-08-15T01:02:03.000Z",
        "request_id": "request-envoy-1",
        "correlation_id": "correlation-envoy-1",
        "organization": "Contoso",
        "organization_id": "org-1",
        "department": "Platform",
        "department_id": "department-1",
        "project": "FinOps",
        "project_id": "project-1",
        "agent": "Delivery Engineer",
        "agent_id": "agent-1",
        "user": "Alex",
        "user_id": "alex@example.com",
        "workflow": "interactive",
        "run_id": "run-1",
        "turn_index": "1",
        "provider": "microsoft_foundry",
        "model": "customer-luna-alias",
        "model_id": "model-1",
        "runtime": "Envoy Cache Experiment",
        "api_format": "openai_chat",
        "request_source": "agent-invocation-module",
        "prompt_tokens": 1200,
        "cache_read_tokens": 1000,
        "cache_write_tokens": 128,
        "output_tokens": 30,
        "response_code": 200,
        "latency_ms": 1234,
    }
    row.update(overrides)
    return row


def execution_row(**overrides: object) -> dict[str, object]:
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=2000)
    attempt = receipt.identity
    row = access_row(
        timestamp=attempt.admitted_at.isoformat(), user_id=attempt.scope.scope_id,
        model_id=attempt.model_id, correlation_id=attempt.correlation_id,
        gateway_profile_id=str(attempt.gateway_profile_id),
        native_gateway_id=attempt.native_gateway_id,
        native_gateway_instance_id=attempt.native_gateway_id,
        gateway_is_managed=str(attempt.native_gateway_id == "managed"),
        apim_service_id="unit-service", apim_region="unit-region", api_revision="1",
        usage_id=canonical_usage_id_v2(
            attempt.gateway_profile_id, "unit-service", attempt.native_gateway_id,
            attempt.correlation_id,
        ),
        budget_execution=base64.b64encode(json.dumps([receipt.model_dump(mode="json")]).encode()).decode(),
        response_content_type="text/event-stream", response_flags="-", total_tokens=1230,
    )
    row.update(overrides)
    return row


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
def test_observer_execution_requires_complete_protocol_usage(protocol: str) -> None:
    row = execution_row(api_format=protocol, response_status="completed",
                        last_event_type="message_stop" if protocol == "anthropic_messages"
                        else "response.completed")
    event = load_shipper().normalize_access_row(row)
    assert event is not None and event["usage_complete"] is True
    UsageEvent.model_validate(event)
    assert event["usage_event_source"] == "observer"
    assert event["tokens_consumed"] == (2358 if protocol == "anthropic_messages" else 1230)


@pytest.mark.parametrize("completion,total,reasoning,expected", [
    (1, 52, 43, 44),
    (44, 52, 43, 44),
    (1, 9, None, 1),
])
def test_chat_reasoning_is_counted_only_when_the_reported_total_requires_it(
    completion: int, total: int, reasoning: int | None, expected: int,
) -> None:
    event = load_shipper().normalize_access_row(execution_row(
        prompt_tokens=8, cache_read_tokens=0, cache_write_tokens=0,
        output_tokens=completion, total_tokens=total, reasoning_tokens=reasoning,
    ))
    assert event is not None and event["usage_complete"] is True
    assert event["input_tokens"] == 8 and event["output_tokens"] == expected
    assert event["tokens_consumed"] == total
    UsageEvent.model_validate(event)


@pytest.mark.parametrize("reasoning", [None, -1, True, 42.5, 42])
def test_chat_reasoning_never_fills_an_unproven_token_difference(reasoning: object) -> None:
    event = load_shipper().normalize_access_row(execution_row(
        prompt_tokens=8, cache_read_tokens=0, cache_write_tokens=0,
        output_tokens=1, total_tokens=52, reasoning_tokens=reasoning,
    ))
    assert event is not None and event["usage_complete"] is False
    assert event["tokens_consumed"] is None and event["estimated"] is True


def test_usd_chat_reasoning_uses_the_admitted_output_price() -> None:
    from turnstile_core.domain.budget_completion import usd_completion_charge

    model_id = UUID(int=4)
    admitted = identity(model_id=str(model_id))
    projection = BudgetPriceProjection.create(model_id, ModelPrice(
        input_price_per_million=2, cached_price_per_million=0.5,
        cache_write_price_per_million=0, output_price_per_million=6,
    ), admitted.admitted_at)
    receipt = LedgerAdmissionReceipt(identity=admitted, epoch=EPOCH, reserved_tokens=2000,
        budget_mode="usd", reserved_usd=Decimal("3"), price_snapshot=projection)
    encoded = base64.b64encode(json.dumps([receipt.model_dump(mode="json")]).encode()).decode()
    event = load_shipper().normalize_access_row(execution_row(
        model_id=str(model_id), budget_execution=encoded,
        prompt_tokens=8, cache_read_tokens=0, cache_write_tokens=0,
        output_tokens=1, total_tokens=52, reasoning_tokens=43,
    ))
    assert event is not None and event["usage_complete"] is True
    assert event["output_tokens"] == 44 and event["tokens_consumed"] == 52
    assert usd_completion_charge(projection.model_dump(mode="json"), event) == Decimal("0.000280")


@pytest.mark.parametrize("changes", [
    {"response_flags": "UC"}, {"response_flags": None}, {"response_code": 503},
    {"prompt_tokens": 12.5}, {"prompt_tokens": True}, {"output_tokens": None},
    {"cache_read_tokens": 2000}, {"total_tokens": 100}, {"total_tokens": "1230.5"},
    {"prompt_tokens": 2**53}, {"api_format": "openai_responses", "response_status": "incomplete"},
    {"api_format": "anthropic_messages", "last_event_type": "message_delta"},
    {"api_format": "anthropic_messages", "last_event_type": "message_stop",
     "cache_write_tokens": None},
])
def test_observer_execution_incomplete_or_interrupted_usage_preserves_receipt(
    changes: dict[str, object],
) -> None:
    event = load_shipper().normalize_access_row(execution_row(**changes))
    assert event is not None and len(event["budget_execution"]) == 1
    assert event["usage_complete"] is False and event["estimated"] is True
    assert event["input_tokens"] is None and event["tokens_consumed"] is None
    UsageEvent.model_validate(event)


@pytest.mark.parametrize("changes", [
    {"budget_execution": "not-base64"}, {"budget_execution": "A" * 16385},
    {"budget_execution": base64.b64encode(b"[]").decode()},
    {"correlation_id": "another-attempt"}, {"model_id": "another-model"},
    {"user_id": "another-user"}, {"native_gateway_id": "another-gateway"},
])
def test_observer_execution_rejects_malformed_or_mismatched_receipts(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        load_shipper().normalize_access_row(execution_row(**changes))


@pytest.mark.parametrize("changes", [
    {"usage_id": "wrong"}, {"usage_id": None}, {"apim_service_id": "wrong-service"},
    {"apim_region": None}, {"api_revision": None}, {"native_gateway_instance_id": None},
    {"gateway_is_managed": "unknown"},
])
def test_observer_rejects_unproven_schema_two_gateway_context(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        load_shipper().normalize_access_row(execution_row(**changes))


def test_observer_keeps_original_usd_admission_price_and_reserved_amount() -> None:
    model_id = UUID(int=4)
    price = ModelPrice(
        input_price_per_million=10, cached_price_per_million=1,
        cache_write_price_per_million=20, output_price_per_million=30,
    )
    projection = BudgetPriceProjection.create(model_id, price, identity().admitted_at)
    receipt = LedgerAdmissionReceipt(
        identity=identity(model_id=str(model_id)), epoch=EPOCH, reserved_tokens=2000,
        budget_mode="usd", reserved_usd=Decimal("0.01000000000000"), price_snapshot=projection,
    )
    encoded = base64.b64encode(json.dumps([receipt.model_dump(mode="json")]).encode()).decode()
    event = load_shipper().normalize_access_row(execution_row(
        model_id=str(model_id), budget_execution=encoded, response_status="completed",
        last_event_type="response.completed",
    ))
    assert event is not None
    parsed = UsageEvent.model_validate(event)
    actual = LedgerAdmissionReceipt.model_validate(parsed.budget_execution[0])
    assert actual == receipt
    assert actual.reserved_usd == Decimal("0.01000000000000")
    assert actual.price_snapshot == projection
    assert parsed.usage_complete is True


def test_observer_budget_headers_are_removed_before_upstream_and_only_fixed_usage_is_logged(
) -> None:
    template = yaml.safe_load((ADAPTER / "envoy.yaml.template").read_text())
    listener = template["static_resources"]["listeners"][0]
    config = listener["filter_chains"][0]["filters"][0]["typed_config"]
    filters = {item["name"]: item["typed_config"] for item in config["http_filters"]}
    rules = filters["envoy.filters.http.header_to_metadata"]["request_rules"]
    for name in ("budget-execution", "gateway-profile-id", "native-gateway-id",
                 "usage-id", "native-gateway-instance-id", "gateway-is-managed",
                 "apim-service-id", "apim-region", "api-revision",
                 "apim-subscription-id", "application-actor-type", "application-actor-id"):
        rule = next(rule for rule in rules if rule["header"] == "x-turnstile-" + name)
        assert rule["remove"] is True
    logged = config["access_log"][0]["typed_config"]["log_format"]["json_format"]
    assert logged["response_flags"] == "%RESPONSE_FLAGS%"
    assert "last_event_type" in logged and "budget_execution" in logged
    assert not {"body", "messages", "prompt", "tools", "authorization"}.intersection(logged)


def test_shipper_normalizes_exact_cache_buckets_without_double_counting() -> None:
    event = load_shipper().normalize_access_row(access_row())

    assert event is not None
    assert event["id"] == "request-envoy-1"
    assert event["correlation_id"] == "correlation-envoy-1"
    assert event["input_tokens"] == 72
    assert event["cached_tokens"] == 1128
    assert event["cache_write_tokens"] == 128
    assert event["output_tokens"] == 30
    assert event["estimated"] is False
    assert event["runtime_authoritative"] is False


@pytest.mark.parametrize("enabled,fails", [(False, False), (True, False), (True, True)])
def test_observer_completion_is_optional_and_failure_still_delivers_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    enabled: bool, fails: bool,
) -> None:
    module = load_shipper()
    monkeypatch.setenv("EVENT_HUB_FQDN", "unit.servicebus.windows.net")
    monkeypatch.setenv("EVENT_HUB_NAME", "usage")
    monkeypatch.setenv("BUDGET_COMPLETION_ENABLED", str(enabled).lower())
    monkeypatch.setenv("LEDGER_TABLE_ENDPOINT", "https://ledger.example.test")
    monkeypatch.setenv("LEDGER_TABLE_NAME", "TurnstileLedger")
    with (patch.object(module.ManagedIdentityToken, "get", return_value="unit-token"),
          patch.object(module, "urlopen") as send,
          patch("turnstile_core.integrations.budget_completion.BudgetCompletionClient") as client):
        send.return_value.__enter__.return_value.status = 201
        if fails:
            client.return_value.complete_event.side_effect = RuntimeError("private details")
        sender = module.EventHubSender()
        event = module.normalize_access_row(execution_row())
        sender.send(event)
        if enabled:
            client.return_value.complete_event.assert_called_once_with(event)
        else:
            client.assert_not_called()
        send.assert_called_once()
        assert "private details" not in capsys.readouterr().out


@pytest.mark.parametrize("nested", (None, "", "-"))
def test_shipper_uses_top_level_cache_only_when_nested_measurement_is_missing(
    nested: object,
) -> None:
    event = load_shipper().normalize_access_row(access_row(
        cache_read_tokens=nested, cache_read_tokens_fallback=1000,
    ))
    assert event is not None
    assert event["cached_tokens"] == 1128
    assert event["input_tokens"] == 72


@pytest.mark.parametrize("nested", (0, "0", 200))
def test_shipper_preserves_explicit_nested_cache_including_zero(nested: object) -> None:
    event = load_shipper().normalize_access_row(access_row(
        cache_read_tokens=nested, cache_read_tokens_fallback=1000,
    ))
    assert event is not None
    assert event["cached_tokens"] == int(str(nested)) + 128
    assert event["input_tokens"] + event["cached_tokens"] == 1200


@pytest.mark.parametrize("runtime", (None, "", "-", "unattributed", "pool-member-runtime"))
def test_only_exact_pool_runtime_metadata_is_authoritative(runtime: object) -> None:
    event = load_shipper().normalize_access_row(access_row(pool_runtime=runtime))
    assert event is not None
    assert event["runtime_authoritative"] is (runtime == "pool-member-runtime")
    if runtime == "pool-member-runtime":
        assert event["runtime"] == runtime
    else:
        assert event["runtime"] == "Envoy Cache Experiment"
    assert load_shipper().normalize_access_row(access_row(
        response_code=503, pool_runtime=runtime,
    )) is None


@pytest.mark.parametrize("content_type,allowed", [
    ("application/json", True),
    ("application/json; charset=utf-8", True),
    ("application/json;charset=UTF-8", True),
    ('Application/JSON ; Charset="utf-8"', True),
    ("text/event-stream", False),
    ("text/html", False),
    ("application/jsonp", False),
    ("", False),
])
def test_json_usage_metadata_accepts_json_media_type_parameters_only(
    content_type: str, allowed: bool,
) -> None:
    template = yaml.safe_load((ADAPTER / "envoy.yaml.template").read_text())
    filters = template["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0][
        "typed_config"
    ]["http_filters"]
    json_filter = next(item["typed_config"] for item in filters
                       if item["name"] == "envoy.filters.http.json_to_metadata")
    pattern = json_filter["response_rules"]["allow_content_types_regex"]["regex"]
    assert bool(re.fullmatch(pattern, content_type)) is allowed


def test_observer_keeps_fallback_cache_and_pool_identity_in_separate_metadata() -> None:
    template = yaml.safe_load((ADAPTER / "envoy.yaml.template").read_text(encoding="utf-8"))
    listener = template["static_resources"]["listeners"][0]
    config = listener["filter_chains"][0]["filters"][0]["typed_config"]
    filters = {item["name"]: item["typed_config"] for item in config["http_filters"]}
    request_rules = filters["envoy.filters.http.header_to_metadata"]["request_rules"]
    pool = next(rule for rule in request_rules if rule["header"] == "x-turnstile-pool-runtime")
    assert pool["on_header_present"]["key"] == "pool_runtime"
    assert pool["remove"] is True
    rules = filters["envoy.filters.http.json_to_metadata"]["response_rules"]["rules"]
    fallback = next(rule for rule in rules if rule["selectors"] == [
        {"key": "usage"}, {"key": "cached_tokens"},
    ])
    assert fallback["on_present"]["key"] == "cache_read_tokens_fallback"
    nested = next(rule for rule in rules if rule["selectors"] == [
        {"key": "usage"}, {"key": "prompt_tokens_details"}, {"key": "cached_tokens"},
    ])
    assert nested["on_present"]["key"] == "cache_read_tokens"


def test_shipper_skips_failed_or_usage_free_access_rows() -> None:
    normalize = load_shipper().normalize_access_row

    assert normalize(access_row(response_code=500)) is None
    assert normalize(
        access_row(
            prompt_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
        )
    ) is None


def test_shipper_keeps_anthropic_input_exclusive_of_cache_buckets() -> None:
    event = load_shipper().normalize_access_row(
        access_row(
            api_format="anthropic_messages",
            prompt_tokens=10,
            cache_read_tokens=2048,
            cache_write_tokens=256,
            output_tokens=20,
        )
    )

    assert event is not None
    assert event["input_tokens"] == 10
    assert event["cached_tokens"] == 2304
    assert event["cache_write_tokens"] == 256
    assert event["output_tokens"] == 20


def test_shipper_normalizes_responses_input_cache_without_double_counting() -> None:
    event = load_shipper().normalize_access_row(
        access_row(
            api_format="openai_responses",
            prompt_tokens=1200,
            cache_read_tokens=1000,
            cache_write_tokens=0,
            output_tokens=30,
        )
    )

    assert event is not None
    assert event["input_tokens"] == 200
    assert event["cached_tokens"] == 1000
    assert event["output_tokens"] == 30


def test_adapter_is_provider_neutral_and_does_not_log_prompts() -> None:
    envoy = (ADAPTER / "envoy.yaml.template").read_text(encoding="utf-8")
    entrypoint = (ADAPTER / "entrypoint.py").read_text(encoding="utf-8")
    apim = (ADAPTER / "apim.bicep").read_text(encoding="utf-8")

    assert "authorization" not in envoy.lower()
    assert "messages" not in envoy
    assert "prompt_tokens" in envoy
    assert "cache_write_tokens" in envoy
    assert "cache_creation_input_tokens" in envoy
    assert "cache_read_input_tokens" in envoy
    assert "envoy.filters.http.json_to_metadata" in envoy
    assert "max_event_size: 10485760" in envoy
    assert "{ key: usage }, { key: input_tokens_details }, { key: cached_tokens }" in envoy
    assert "{ key: response }, { key: usage }, { key: input_tokens }" in envoy
    assert (
        "{ key: response }, { key: usage }, { key: input_tokens_details }, "
        "{ key: cached_tokens }" in envoy
    )
    assert "{ key: response }, { key: usage }, { key: output_tokens }" in envoy
    assert envoy.count("{ key: usage }, { key: cache_creation_input_tokens }") == 3
    assert envoy.count("{ key: usage }, { key: cache_read_input_tokens }") == 3
    assert "envoy.filters.http.dynamic_forward_proxy" in envoy
    assert "envoy.clusters.dynamic_forward_proxy" in envoy
    assert "host_rewrite_header: x-turnstile-upstream-host" in envoy
    assert "header: x-turnstile-pool-runtime" in envoy
    assert envoy.index("header: x-hive-runtime") < envoy.index(
        "header: x-turnstile-pool-runtime"
    )
    assert "request_headers_to_remove:" in envoy
    for header in (
        "client-ip",
        "disguised-host",
        "was-default-hostname",
        "x-arr-log-id",
        "x-arr-ssl",
        "x-client-ip",
        "x-client-port",
        "x-forwarded-tlsversion",
        "x-original-url",
        "x-site-deployment-id",
        "x-waws-unencoded-url",
    ):
        assert f"- {header}" in envoy
    assert "PROVIDER_HOST" not in envoy
    assert "PROVIDER_HOST" not in entrypoint
    assert "Microsoft.ApiManagement/service/apis" not in apim
    assert "Microsoft.ApiManagement/service/subscriptions" not in apim
    assert "turnstile-envoy-adapter-key" in apim


def test_main_apim_has_an_explicit_all_model_observer_adoption_boundary() -> None:
    main = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
    integration = (ROOT / "infra/modules/apim-integration.bicep").read_text(
        encoding="utf-8"
    )
    parent_policy = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(
        encoding="utf-8"
    )
    assert "__USAGE_OBSERVER_HEADERS__" in parent_policy
    assert "finops-legacy-foundry-observer" in integration
    assert "finops-legacy-databricks-observer" in integration
    assert "selectedRoutingManaged" in integration
    assert "selectedModelKnown&quot;, false))" in integration
    assert "streamOptions[&quot;include_usage&quot;] = true" in integration
    assert "x-turnstile-upstream-host" in integration
    assert "x-adapter-key" in integration
    assert "mode: 'disabled'" in main
    assert "keyNamedValue: ''" in main
    assert "param apimUsageObserverLegacyRoutingEnabled bool = false" in main
