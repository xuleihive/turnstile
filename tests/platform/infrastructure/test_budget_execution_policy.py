from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import UUID
from xml.etree import ElementTree as ET

import pytest

from scripts.prepare_budget_execution import CutoverReadback, render_candidate
from tests.backend.persistence.test_price_sync_concurrency import (
    pricing_postgres as pricing_postgres,
)
from tests.backend.telemetry.test_atomic_ledger import EPOCH, NOW, completion, identity, summary
from tests.platform.api.test_model_router_api import router_repository as router_repository
from tests.platform.infrastructure.test_envoy_cache_adapter import execution_row, load_shipper
from turnstile_core.domain.gateway_targets import canonical_usage_id_v2
from turnstile_core.domain.ledger import (
    BudgetPriceProjection,
    LedgerAdmissionReceipt,
    LedgerAttempt,
    LedgerCharge,
    LedgerProtocolConflict,
    LedgerScope,
    LedgerSummary,
    complete_ledger,
    reserve_ledger,
    settle_ledger,
)
from turnstile_core.domain.models import ModelPrice
from turnstile_core.integrations import apim_budget_execution as compiler
from turnstile_core.integrations import apim_model_availability as availability
from turnstile_core.integrations import apim_storage_routes as storage_routes
from turnstile_core.integrations.apim_control_plane_contract import PolicyCompilationError
from turnstile_core.integrations.apim_policy_components import parse_policy

ROOT = Path(__file__).resolve().parents[3]


def test_budget_events_supply_public_reader_identity_on_success_and_error() -> None:
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    root = parse_policy(compiler.compose_budget_execution_parent(source, source, enabled=True))
    native = root.find("./inbound/set-variable[@name='telemetryNativeGatewayId']")
    assert native is not None and native.get("value") == "@(context.Deployment.Gateway.Id)"
    canonical = root.find("./inbound/set-variable[@name='telemetryUsageId']")
    assert canonical is not None and "turnstile-v2-" in canonical.get("value", "")
    assert "context.Request.Headers" not in canonical.get("value", "")
    for section in ("outbound", "on-error"):
        events = root.findall(f"./{section}//log-to-eventhub")
        assert events
        for event in events:
            expression = event.text or ""
            assert 'context.Variables["telemetryNativeGatewayId"]' in expression
            assert 'new JProperty("usage_event_source", "apim")' in expression
            assert 'new JProperty("correlation_id", context.RequestId.ToString())' in expression
            assert "turnstile-v2-" not in expression


def test_confirmed_admission_binds_observer_model_to_immutable_receipt() -> None:
    admission = compiler._admission()
    trusted = admission.find(".//set-variable[@name='telemetryModelId']")
    header = admission.find(".//set-variable[@name='executionReceiptHeader']")
    assert trusted is not None and header is not None
    assert trusted.attrib["value"] == '@((string)context.Variables["executionModelId"])'
    parents = {child: node for node in admission.iter() for child in node}
    assert parents[trusted] is parents[header]
    assert list(parents[trusted]).index(trusted) > list(parents[header]).index(header)


HARNESS = r"""
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
sealed class Values : Dictionary<string, object> {
  public T GetValueOrDefault<T>(string key, T fallback) {
    object value; return TryGetValue(key, out value) ? (T)value : fallback;
  }
}
sealed class Headers : Dictionary<string, string> {
  public Headers() : base(StringComparer.OrdinalIgnoreCase) {}
  public string GetValueOrDefault(string key, string fallback) {
    string value; return TryGetValue(key, out value) ? value : fallback;
  }
}
sealed class Body {
  readonly string text;
  public Body(string value) { text = value; }
  public T As<T>(bool preserveContent = false) {
    if (typeof(T) == typeof(string)) { return (T)(object)text; }
    if (typeof(T) == typeof(byte[])) { return (T)(object)Encoding.UTF8.GetBytes(text); }
    if (typeof(T) == typeof(JObject)) { return (T)(object)JObject.Parse(text); }
    throw new Exception("unsupported body type");
  }
}
interface IResponse { int StatusCode {get;} Headers Headers {get;} Body Body {get;} }
sealed class Response : IResponse {
  public int StatusCode {get;set;}
  public Headers Headers {get;set;}
  public Body Body {get;set;}
  public Response(JObject source) {
    StatusCode = (int)source["status"]; Headers = new Headers();
    foreach (var item in ((JObject)source["headers"]).Properties()) {
      Headers[item.Name] = (string)item.Value;
    }
    Body = new Body((string)source["body"]);
  }
}
sealed class Request {
    public Body Body; public Headers Headers = new Headers(); public string Method = "POST";
    public Uri OriginalUrl = new Uri("https://unit.azure-api.net/turnstile/chat/completions");
}
sealed class Operation { public string Id; }
sealed class Subscription { public string Id; }
sealed class Api {
  public string Id = "unit-turnstile"; public string Revision = "candidate";
  public string Path = "turnstile";
}
sealed class Gateway {
    public string Id = "managed"; public bool IsManaged = true;
    public string InstanceId = "managed";
}
sealed class Deployment {
  public Gateway Gateway = new Gateway();
  public string ServiceId = "unit-service"; public string Region = "unit-region";
}
sealed class Context {
  public Values Variables = new Values(); public Request Request; public IResponse Response;
  public Operation Operation; public Api Api = new Api();
  public Deployment Deployment = new Deployment(); public Subscription Subscription;
  public Guid RequestId = Guid.Parse("11111111-2222-3333-4444-555555555555");
  public Context(JObject source) {
        Request = new Request {
            Body = new Body((string)source["body"] ?? "{}"),
            Method = (string)source["method"] ?? "POST"
        };
        Deployment.Gateway.Id = (string)source["gateway"] ?? "managed";
        Deployment.Gateway.InstanceId = (string)source["instance"] ?? "managed";
        Deployment.Gateway.IsManaged = Deployment.Gateway.Id == "managed";
        Response = new Response(new JObject(
            new JProperty("status", (int?)source["response_status"] ?? 200),
            new JProperty("headers", new JObject()),
            new JProperty("body", (string)source["response"] ?? "{}")));
    Operation = new Operation { Id = (string)source["operation"] ?? "responses" };
    Subscription = (bool?)source["subscription"] == true
      ? new Subscription { Id = "unit-subscription" } : null;
    if (source["headers"] is JObject) {
      foreach (var item in ((JObject)source["headers"]).Properties()) {
        Request.Headers[item.Name] = (string)item.Value;
      }
    }
    foreach (var item in ((JObject)source["variables"]).Properties()) {
      var value = item.Value;
      Variables[item.Name] = item.Name == "requestStarted"
        ? (object)DateTime.Parse((string)value).ToUniversalTime()
                : item.Name == "budgetExecutionVersion"
                    || item.Name == "availabilityProtocolVersion"
                    ? (object)(int)value
        : value is JObject && value["__response"] != null ? new Response((JObject)value)
        : value is JValue ? ((JValue)value).Value : value;
    }
  }
}
static class Expressions {
__METHODS__
  public static object Run(string name, Context context) {
    switch(name) { __SWITCH__ default: throw new Exception("unknown expression"); }
  }
}
static class Program {
  static int Main(string[] args) {
    var results = new JArray();
    foreach (JObject item in JArray.Parse(File.ReadAllText(args[0]))) {
      try {
        var context = new Context(item);
        if (item["steps"] is JArray) {
          foreach (JObject step in (JArray)item["steps"]) {
            context.Variables[(string)step["variable"]] = Expressions.Run(
              (string)step["expression"], context);
          }
        }
        results.Add(new JObject(new JProperty("value", JToken.FromObject(
          Expressions.Run((string)item["expression"], context))),
          new JProperty("body_after", context.Request.Body.As<string>(true))));
      } catch (Exception error) {
        results.Add(new JObject(new JProperty("exception", error.GetType().Name)));
      }
    }
    Console.WriteLine(results.ToString(Formatting.None)); return 0;
  }
}
"""


def expression_id(expression: str) -> str:
    return hashlib.sha256(expression.encode()).hexdigest()[:20]


def text_usage_expression() -> str:
    root = parse_policy((ROOT / "infra/policies/foundry-finops-policy.xml").read_text())
    node = root.find(
        "./outbound/choose/otherwise/choose/otherwise/set-variable[@name='usagePayload']",
    )
    assert node is not None
    return node.attrib["value"]


def responses_observer_event(
    receipt: LedgerAdmissionReceipt, native: dict[str, Any], streamed: bool,
) -> dict[str, Any]:
    request = receipt.identity
    event = load_shipper().normalize_access_row(execution_row(
        request_id=request.correlation_id, correlation_id=request.correlation_id,
        usage_id=canonical_usage_id_v2(
            request.gateway_profile_id, "unit-service", request.native_gateway_id,
            request.correlation_id,
        ),
        model_id=request.model_id, user_id=request.scope.scope_id, api_format="openai_responses",
        budget_execution=base64.b64encode(json.dumps(
            [receipt.model_dump(mode="json")],
        ).encode()).decode(),
        response_content_type="text/event-stream" if streamed else "application/json",
        response_status="completed", last_event_type="response.completed",
        prompt_tokens=native["input_tokens"], output_tokens=native["output_tokens"],
        total_tokens=native["total_tokens"],
        cache_read_tokens=native["input_tokens_details"]["cached_tokens"],
        cache_write_tokens=native["input_tokens_details"]["cache_write_tokens"],
    ))
    assert event is not None
    return cast(dict[str, Any], event)


def application_tpm_expressions() -> dict[str, str]:
    source = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    composed = compiler.compose_budget_execution_parent(source, source, enabled=True)
    root = parse_policy(composed.replace("__TOKENS_PER_MINUTE__", "100000"))
    names = {
        "applicationLedgerUrl", "applicationLedgerState", "applicationTokensPerMinute",
        "applicationRateLimitPartition", "applicationRateLimitUrl",
    }
    return {node.attrib["name"]: node.attrib["value"]
            for node in root.iter("set-variable") if node.get("name") in names}


def application_attribution_expressions() -> dict[str, str]:
    root = parse_policy((ROOT / "infra/policies/foundry-finops-policy.xml").read_text())
    return {
        node.attrib["name"]: node.findtext("value", "")
        for node in root.iter("set-header")
        if "applicationMapState" in node.findtext("value", "")
        and node.get("name") in {"x-org-id", "x-org-name", "x-department-id", "x-department-name"}
    }


def native_router_expressions() -> dict[str, str]:
    from turnstile_core.integrations.apim_router_global_policy import (
        build_router_global_service_policy,
        fixed_router_operation_stages,
    )

    inbound, outbound = fixed_router_operation_stages(
        "https://unitstore.table.core.windows.net", "Ledger", "unit-turnstile",
        "90000000-0000-4000-8000-000000000002",
    )
    result = {}
    for node in inbound.iter("set-variable"):
        if node.get("name") in {"routerNativeDocumentReady", "routerPlanState"}:
            name = node.attrib["name"]
            result[name if name not in result else "routerDecisionResult"] = node.attrib["value"]
    decision = inbound.find(
        ".//send-request[@response-variable-name='routerDecisionHttp']/set-body",
    )
    assert decision is not None
    result["routerDecisionBody"] = decision.text or ""
    result["routerDecisionUrl"] = inbound.findtext(
        ".//send-request[@response-variable-name='routerDecisionHttp']/set-url", "",
    )
    rewrite = next(node for node in inbound.iter("set-body")
                   if 'context.Variables["routerOriginalRequestBody"]' in (node.text or ""))
    result["routerCandidateBody"] = rewrite.text or ""
    rejection = next(
        (node.text or "" for node in inbound.iter("set-body")
         if "router_configuration_unavailable" in (node.text or "")), "",
    )
    if rejection.startswith("@"):
        result["routerRejectionBody"] = rejection
    result["routerStickyState"] = next(
        node.attrib["value"] for node in inbound.iter("set-variable")
        if node.get("name") == "routerPlanState"
        and "routerStickyTableResp" in node.attrib["value"]
    )
    for node in outbound.iter("set-variable"):
        name = node.attrib["name"]
        if name in {"routerPlanState", "routerOutboundCompleted"}:
            key = "routerFailedAttempt" if name == "routerPlanState" else name
            result[key] = node.attrib["value"]
    result["routerNameHeader"] = next(
        node.findtext("value", "") for node in outbound.iter("set-header")
        if node.get("name") == "x-router-name"
    )
    legacy = parse_policy(build_router_global_service_policy(
        "<policies><inbound></inbound><outbound></outbound></policies>",
        ledger_table_endpoint="https://unitstore.table.core.windows.net",
        ledger_table_name="Ledger", api_id="unit-turnstile",
    ))
    for state_variable in ("routerPlanState", "routerAnthropicCompleted"):
        result[f"{state_variable}NameHeader"] = next(
            node.findtext("value", "") for node in legacy.iter("set-header")
            if node.get("name") == "x-router-name"
            and state_variable in node.findtext("value", "")
        )
    result["directModelResponseBody"] = next(
        node.text or "" for node in legacy.iter("set-body")
        if 'new JProperty("decision_basis", "direct_model")' in (node.text or "")
    )
    trace = outbound.find(
        ".//send-request[@response-variable-name='routerOutboundTraceWrite']/set-body",
    )
    assert trace is not None
    result["routerTraceBody"] = trace.text or ""
    return result


def business_denial_expressions() -> dict[str, str]:
    from turnstile_core.integrations.apim_router_global_policy import fixed_router_operation_stages

    source = parse_policy((ROOT / "infra/policies/foundry-finops-policy.xml").read_text())
    identity = source.find("./inbound/set-variable[@name='telemetryUsageId']")
    assert identity is not None
    model = availability.admission_guard(logger_id="__LOGGER_ID__")
    router, _ = fixed_router_operation_stages(
        "https://unitstore.table.core.windows.net", "Ledger", "unit-turnstile",
        "90000000-0000-4000-8000-000000000002",
        usage_identity=identity.attrib["value"],
    )
    result = {"usageIdentity": identity.attrib["value"]}
    for name, guard in (("model_disabled", model), ("router_configuration_unavailable", router)):
        event = guard.find(".//log-to-eventhub")
        assert event is not None
        result[name] = event.text or ""
        if name == "model_disabled":
            owner = next(node for node in guard.iter() if event in list(node))
            result["availabilityEventAllowed"] = owner.attrib["condition"]
    inference = source.find("./inbound/set-variable[@name='isInferenceOperation']")
    assert inference is not None
    result["isInferenceOperation"] = inference.attrib["value"]
    denial = next(
        node for node in source.iter("return-response")
        if node.find("set-header[@name='x-application-budget-remaining-tokens']") is not None
    )
    body = denial.findtext("set-body", "")
    if body.startswith("@"):
        result["applicationDenialBody"] = body
    return result


def test_model_disabled_denial_emits_before_early_return() -> None:
    guard = availability.admission_guard(logger_id="__LOGGER_ID__")
    response = next(node for node in guard.iter("return-response")
                    if node.find("set-status[@code='403']") is not None)
    owners = {child: owner for owner in guard.iter() for child in owner}
    denied = owners[response]
    events = denied.findall(".//log-to-eventhub")
    assert len(events) == 1
    sender = next(node for node in denied if events[0] in node.iter())
    assert list(denied).index(sender) < list(denied).index(response)
    assert events[0].attrib["logger-id"] == "__LOGGER_ID__"
    assert '"correlation_id"' in (events[0].text or "")
    assert '"prompt_tokens"' not in (events[0].text or "")


def test_native_router_configuration_denial_emits_before_early_return() -> None:
    from turnstile_core.integrations.apim_router_global_policy import fixed_router_operation_stages

    guard, _ = fixed_router_operation_stages(
        "https://unitstore.table.core.windows.net", "Ledger", "unit-turnstile",
        "90000000-0000-4000-8000-000000000002",
    )
    response = next(node for node in guard.iter("return-response")
                    if '"router_configuration_unavailable"' in (node.findtext("set-body") or ""))
    owners = {child: owner for owner in guard.iter() for child in owner}
    denied = owners[response]
    events = denied.findall(".//log-to-eventhub")
    assert len(events) == 1
    sender = next(node for node in denied if events[0] in node.iter())
    assert list(denied).index(sender) < list(denied).index(response)
    assert events[0].attrib["logger-id"] == "__LOGGER_ID__"
    assert '"correlation_id"' in (events[0].text or "")
    assert '"prompt_tokens"' not in (events[0].text or "")


@pytest.mark.parametrize("surface,status_code", [
    ("model_disabled", 403), ("router_configuration_unavailable", 503),
])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("gateway", ["managed", "unit-self-hosted"])
@pytest.mark.parametrize("request_source", [None, "unit-client", "copilot-assistant-tool"])
def test_business_denial_event_is_ingested_without_usage_or_sensitive_content(
    compiled_expressions: tuple[Path, str], tmp_path: Path, surface: str,
    status_code: int, known: bool, gateway: str, request_source: str | None,
) -> None:
    from tests.backend.telemetry.test_processor import RecordingRepository
    from turnstile_core.domain.gateway_targets import canonical_usage_id_v2
    from turnstile_core.domain.models import UsageEvent
    from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor

    target = UUID("90000000-0000-4000-8000-000000000002")
    correlation = "11111111-2222-3333-4444-555555555555"
    request_id = "22222222-3333-4444-5555-666666666666"
    usage_id = canonical_usage_id_v2(target, "unit-service", gateway, correlation)
    variables: dict[str, Any] = {
        "callerRequestId": request_id, "routerCallerRequestId": request_id,
        "availabilityModelId": "90000000-0000-4000-8000-000000000003",
        "availabilityFixedModelAlias": "unit-model", "routerRequestedModel": "unit-router",
    }
    if known:
        variables["telemetryUsageId"] = usage_id
    expressions = business_denial_expressions()
    has_identity = known or surface == "router_configuration_unavailable"
    payload = json.loads(run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(expressions[surface]),
        "body": json.dumps({"messages": [{"role": "user", "content": "unit-private-prompt"}]}),
        "headers": {"Ocp-Apim-Subscription-Key": "unit-private-key",
                    "x-user-id": "forged-person@example.test",
                    **({"x-request-source": request_source} if request_source else {})},
        "operation": "chat-completions", "subscription": True,
        "gateway": gateway, "instance": gateway, "variables": variables,
        "steps": [{"variable": "telemetryUsageId",
               "expression": expression_id(expressions["usageIdentity"])}]
        if surface == "router_configuration_unavailable" else [],
    }])[0])
    assert payload["request_id"] == request_id
    assert payload["correlation_id"] == correlation
    assert payload["status_code"] == status_code
    assert payload["event_schema_version"] == (2 if has_identity else 1)
    assert payload["model_admission"] == surface
    assert payload["model"] == ("unit-model" if surface == "model_disabled" else "unit-router")
    if has_identity:
        assert payload["id"] == usage_id
        assert payload["gateway_profile_id"] == str(target)
        assert payload["native_gateway_id"] == gateway
        assert payload["native_gateway_instance_id"] == gateway
        assert payload["gateway_is_managed"] is (gateway == "managed")
    else:
        assert all(payload.get(name) is None for name in (
            "gateway_profile_id", "native_gateway_id", "native_gateway_instance_id",
            "gateway_is_managed", "apim_service_id", "apim_region", "api_revision",
        ))
    assert payload["apim_subscription_id"] == "unit-subscription"
    assert payload["user_id"] == "unattributed"
    assert payload["request_source"] == (request_source or "unattributed")
    assert not {"input_tokens", "cached_tokens", "output_tokens", "tokens_consumed",
                "budget_execution", "budget_terminal", "usd_cost"} & payload.keys()
    assert "unit-private-prompt" not in json.dumps(payload)
    assert "unit-private-key" not in json.dumps(payload)
    assert "forged-person@example.test" not in json.dumps(payload)
    parsed = UsageEvent.model_validate(payload)
    assert parsed.budget_execution == []
    assert parsed.usage_complete is False
    repository = RecordingRepository()
    processor = UsageProcessor(repository, CoefficientResolver({"default": 1.0}))
    assert processor.process(payload) is True
    assert len(repository.records) == 1
    record = repository.records[0]
    assert record.request_id == request_id
    assert record.status_code == status_code
    assert record.model_admission == surface
    assert record.usage_domain == (
        "github_copilot" if request_source == "copilot-assistant-tool" else "apim"
    )
    assert record.estimated is True
    assert record.usd_cost is None
    assert repository.execution_writes == []
    if has_identity:
        assert repository.gateway_attributions[0].apim_target_id == target
    else:
        assert repository.gateway_attributions == []


@pytest.mark.parametrize("operation,allowed", [
    ("chat-completions", True), ("responses", True), ("responses-compact", True),
    ("anthropic-messages", True), ("images-generations", True),
    ("anthropic-count-tokens", False), ("models", False), ("unknown", False),
])
def test_business_denial_event_preserves_non_inference_exclusions(
    compiled_expressions: tuple[Path, str], tmp_path: Path, operation: str, allowed: bool,
) -> None:
    expressions = business_denial_expressions()
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(expressions["availabilityEventAllowed"]),
        "body": "{}", "operation": operation,
        "variables": {"isImagesOperation": operation == "images-generations",
                      "isResponsesOperation": operation in {"responses", "responses-compact"}},
        "steps": [{"variable": "isInferenceOperation",
                   "expression": expression_id(expressions["isInferenceOperation"])}],
    }])[0]
    assert result is allowed
    assert availability.availability_probe_guard().find(".//log-to-eventhub") is None


@pytest.mark.parametrize("fault", [
    None, "digest", "target", "unpublished", "direct-provider", "fallback",
    "prepared", "multiple-gateways", "system-one", "unknown-protocol",
])
def test_native_router_definition_proof_executes_before_paid_routing(
    compiled_expressions: tuple[Path, str], tmp_path: Path, fault: str | None,
) -> None:
    from turnstile_core.integrations.router_table_projection import router_row_key

    document = {
        "status": "draft" if fault == "unpublished" else "published", "router_version": 1,
        "gateway_profile_id": "foreign" if fault == "target"
        else None if fault == "multiple-gateways"
        else "90000000-0000-4000-8000-000000000002",
        "candidates_by_id": {"candidate": {"profile": "openai-v1"}},
        "definition": {"nodes": [{"fallback_on": ["timeout"] if fault == "fallback" else []}]},
        "routing_models_by_id": {"decision": {
            "decision_protocol": {"system-one": "system_one", "unknown-protocol": "unit"}.get(
                fault or "", "chat_json",
            ),
            "auth_mode": "managed_identity" if fault == "direct-provider" else "gateway_route",
        }},
    }
    serialized = json.dumps(document, separators=(",", ":"), sort_keys=True)
    entity = {
        "PartitionKey": "model-router-prepared|v1" if fault == "prepared" else "model-router|v1",
        "RowKey": router_row_key("unit-router"),
        "SchemaVersion": 2, "Status": "prepared" if fault == "prepared" else "published",
        "Document": serialized,
        "Sha256": ("0" * 64 if fault == "digest"
               else hashlib.sha256(serialized.encode()).hexdigest()),
    }
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(native_router_expressions()["routerNativeDocumentReady"]),
        "body": "{}", "subscription": True, "variables": {
            "routerCachedEntityJson": json.dumps(entity),
            "routerLookupRowKey": router_row_key("unit-router"),
        },
    }])[0]
    assert result is (fault in {None, "system-one"})


_CODE_MODEL, _GENERAL_MODEL = str(UUID(int=911)), str(UUID(int=912))


def _native_router_choice(
    compiled_expressions: tuple[Path, str], tmp_path: Path, body: dict[str, Any],
    *, sticky_cost: float,
) -> dict[str, Any]:
    """Plan a request on a Router whose code branch excludes the session's sticky model."""
    def branch(name: str, model: str, y: int) -> list[dict[str, Any]]:
        return [
            {"id": f"pool-{name}", "type": "candidate_models", "label": name,
             "position": {"x": 420, "y": y}, "candidates": [{"model_id": model}]},
            {"id": f"strat-{name}", "type": "strategy", "label": name,
             "position": {"x": 620, "y": y}, "strategy_type": "priority",
             "model_configs": [{"model_id": model, "priority": 1, "weight": 100}]},
        ]

    document = {
        "router_id": str(UUID(int=904)), "router_name": "unit-router", "router_version": 1,
        "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
        "status": "published", "owner_email": "offline@example.test",
        "assigned_user_ids": [], "allowed_subscription_ids": ["unit-subscription"],
        "definition": {"schema_version": 2, "nodes": [
            {"id": "start", "type": "start", "label": "Start", "position": {"x": 0, "y": 0}},
            {"id": "task", "type": "condition", "label": "Task", "position": {"x": 200, "y": 0},
             "condition": {"field": "task_type", "operator": "equals", "value": "code"}},
            *branch("code", _CODE_MODEL, 80), *branch("general", _GENERAL_MODEL, 240),
            {"id": "output", "type": "output", "label": "Output", "position": {"x": 840, "y": 0}},
        ], "edges": [
            {"id": "e1", "source": "start", "target": "task"},
            {"id": "e2", "source": "task", "target": "pool-code", "label": "Match"},
            {"id": "e3", "source": "task", "target": "pool-general", "label": "Else"},
            {"id": "e4", "source": "pool-code", "target": "strat-code"},
            {"id": "e5", "source": "pool-general", "target": "strat-general"},
            {"id": "e6", "source": "strat-code", "target": "output"},
            {"id": "e7", "source": "strat-general", "target": "output"},
        ]},
        "candidates_by_id": {model_id: {
            "model_id": model_id, "apim_alias": alias, "display_name": alias,
            "provider_name": "Unit", "runtime_name": "Unit", "profile": "openai-v1",
            "capabilities": [], "context_window": 16384,
            "input_cost_per_million": cost, "output_cost_per_million": cost,
            "effective_cost_per_million": cost,
        } for model_id, alias, cost in (
            (_CODE_MODEL, "code-model", 1.0), (_GENERAL_MODEL, "general-model", sticky_cost),
        )},
        "routing_models_by_id": {},
    }
    text = json.dumps({**body, "model": "unit-router",
                       "router_context": {"task_type": "code", "session_id": "s1"}})
    plan_expression = expression_id(native_router_expressions()["routerPlanState"])
    case: dict[str, Any] = {
        "body": text, "subscription": True, "operation": "chat-completions",
        "expression": plan_expression, "variables": {
            "routerCachedEntityJson": json.dumps({"Document": json.dumps(document)}),
            "routerOriginalRequestBody": text,
            "routerCallerRequestId": str(UUID(int=906)),
            "routerRequestedModel": "unit-router",
            "routerDecisionRequestId": str(UUID(int=905)),
        },
    }
    plan = json.loads(run_cases(compiled_expressions, tmp_path, [case])[0])
    session = next(key for key in plan["lookup_keys"] if key["tier"] == "session")
    binding = {"model_id": _GENERAL_MODEL, "updated_epoch": int(datetime.now(UTC).timestamp())}
    sticky = {**case, "expression": expression_id(native_router_expressions()["routerStickyState"]),
              "steps": [{"variable": "routerPlanState", "expression": plan_expression}],
              "variables": {**case["variables"], "routerStickyTableResp": {
                  "__response": True, "status": 200, "headers": {}, "body": json.dumps({
                      "value": [{"RowKey": session["row_key"], "Document": json.dumps(binding)}],
                  })}}}
    return dict(json.loads(run_cases(compiled_expressions, tmp_path, [sticky])[0]))


# The code branch is pricier than the sticky model. [False] one user message, which the
# old rule never treated as a new turn; [True] a multi-turn conversation.
@pytest.mark.parametrize("multi_turn", [False, True])
def test_native_router_branch_takes_precedence_over_sticky_model_on_user_turns(
    compiled_expressions: tuple[Path, str], tmp_path: Path, multi_turn: bool,
) -> None:
    earlier = [{"role": "user", "content": "Summarize the news"},
               {"role": "assistant", "content": "Here is the summary."}] if multi_turn else []
    result = _native_router_choice(
        compiled_expressions, tmp_path,
        {"messages": [*earlier, {"role": "user", "content": "Refactor queue.ts"}]},
        sticky_cost=0.5,
    )
    assert result["selected_candidate"]["model_id"] == _CODE_MODEL
    assert result["decision_basis"] == "highest_priority_success"


@pytest.mark.parametrize("multi_turn", [False, True])
def test_native_router_user_turns_keep_the_sticky_model_when_the_branch_is_cheaper(
    compiled_expressions: tuple[Path, str], tmp_path: Path, multi_turn: bool,
) -> None:
    earlier = [{"role": "user", "content": "Summarize the news"},
               {"role": "assistant", "content": "Here is the summary."}] if multi_turn else []
    result = _native_router_choice(
        compiled_expressions, tmp_path,
        {"messages": [*earlier, {"role": "user", "content": "Refactor queue.ts"}]},
        sticky_cost=35.0,
    )
    assert result["selected_candidate"]["model_id"] == _GENERAL_MODEL
    assert result["decision_basis"] == "session_sticky"


# [False] Anthropic Messages tool_result turn; [True] Responses function_call_output item.
@pytest.mark.parametrize("responses", [False, True])
def test_native_router_tool_continuations_keep_the_sticky_model(
    compiled_expressions: tuple[Path, str], tmp_path: Path, responses: bool,
) -> None:
    if responses:
        field, text_type = "input", "input_text"
        history: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "input_text", "text": "Refactor queue.ts"}]},
            {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "export class Q {}"},
        ]
    else:
        field, text_type = "messages", "text"
        history = [
            {"role": "user", "content": "Refactor queue.ts"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "read", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "export class Q {}"}]},
        ]
    continued = _native_router_choice(
        compiled_expressions, tmp_path, {field: history}, sticky_cost=0.5,
    )
    assert continued["selected_candidate"]["model_id"] == _GENERAL_MODEL
    assert continued["decision_basis"] == "session_sticky"
    fresh = _native_router_choice(compiled_expressions, tmp_path, {field: [
        *history, {"role": "user", "content": [{"type": text_type, "text": "Now add tests"}]},
    ]}, sticky_cost=0.5)
    assert fresh["selected_candidate"]["model_id"] == _CODE_MODEL


@pytest.mark.parametrize("fallback", [False, True])
def test_native_router_rejection_names_the_unsupported_fallback_node(
    compiled_expressions: tuple[Path, str], tmp_path: Path, fallback: bool,
) -> None:
    document = {
        "status": "published", "router_version": 1,
        "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
        "definition": {"nodes": [
            {"id": "start", "label": "Start", "fallback_on": []},
            {"id": "backup", "label": "Backup \"tier\"",
             "fallback_on": ["timeout"] if fallback else []},
        ]},
    }
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(native_router_expressions()["routerRejectionBody"]),
        "body": "{}", "subscription": True, "variables": {
            "routerCachedEntityJson": json.dumps({"Document": json.dumps(document)}),
        },
    }])[0]
    error = json.loads(result)["error"]
    assert error["code"] == "router_configuration_unavailable"
    assert error.get("message") == (
        "Router Fallback node 'Backup \"tier\"' is not supported by the gateway; "
        "remove the Fallback node and publish the Router again" if fallback else None
    )


@pytest.mark.parametrize("reason,kind,param", [
    ("application_model_not_assigned", "permission_error", "model"),
    ("application_monthly_token_budget_exhausted", "insufficient_quota", None),
])
def test_application_denial_uses_the_standard_error_envelope_and_request_id(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    reason: str, kind: str, param: str | None,
) -> None:
    source = parse_policy((ROOT / "infra/policies/foundry-finops-policy.xml").read_text())
    denial = next(
        node for node in source.iter("return-response")
        if node.find("set-header[@name='x-application-budget-remaining-tokens']") is not None
    )
    assert denial.findtext("set-header[@name='x-request-id']/value") == (
        '@((string)context.Variables["callerRequestId"])'
    )
    body = json.loads(run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(business_denial_expressions()["applicationDenialBody"]),
        "body": "{}", "variables": {"applicationDenialReason": reason},
    }])[0])
    assert body["type"] == "error"
    assert body["error"]["type"] == kind and body["error"]["code"] == reason
    assert body["error"]["param"] == param and body["error"]["message"]


@pytest.mark.parametrize("prompt", [
    "Pick the cheaper model.\n{catalog}", "Answer with model_id only.",
])
def test_native_router_custom_decision_prompt_always_asks_for_json(
    compiled_expressions: tuple[Path, str], tmp_path: Path, prompt: str,
) -> None:
    from tests.platform.api.test_model_router_api import model_based_definition

    first, second, decision = (UUID(int=value) for value in (901, 902, 903))
    definition = model_based_definition(decision, first, second)
    definition["nodes"] = [node for node in definition["nodes"] if node["type"] != "fallback"]
    definition["edges"] = [edge for edge in definition["edges"]
                           if edge["source"] != "fallback" and edge["target"] != "fallback"]
    next(node for node in definition["nodes"]
         if node["type"] == "strategy")["decision_prompt"] = prompt
    document = {
        "router_id": str(UUID(int=904)), "router_name": "unit-router", "router_version": 1,
        "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
        "status": "published", "owner_email": "offline@example.test",
        "assigned_user_ids": [], "allowed_subscription_ids": ["unit-subscription"],
        "definition": definition,
        "candidates_by_id": {str(model_id): {
            "model_id": str(model_id), "apim_alias": alias, "display_name": alias,
            "provider_name": "Unit", "runtime_name": "Unit", "profile": "openai-v1",
            "capabilities": [], "context_window": 16384,
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
            "effective_cost_per_million": 2.0,
        } for model_id, alias in ((first, "candidate-a"), (second, "candidate-b"))},
        "routing_models_by_id": {str(decision): {
            "model_id": str(decision), "apim_alias": "decision-alias",
            "upstream_model": "decision-deployment", "display_name": "Decision",
            "provider_name": "Unit", "runtime_name": "Unit",
            "decision_protocol": "chat_json", "auth_mode": "gateway_route",
            "capabilities": [], "max_tokens_field": "max_completion_tokens",
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
        }},
    }
    body = json.dumps({"model": "unit-router",
                       "messages": [{"role": "user", "content": "Select a candidate"}]})
    expressions = native_router_expressions()
    request = run_cases(compiled_expressions, tmp_path, [{
        "body": body, "subscription": True, "operation": "chat-completions",
        "expression": expression_id(expressions["routerDecisionBody"]),
        "variables": {
            "routerCachedEntityJson": json.dumps({"Document": json.dumps(document)}),
            "routerOriginalRequestBody": body,
            "routerCallerRequestId": str(UUID(int=906)),
            "routerRequestedModel": "unit-router",
            "routerDecisionRequestId": str(UUID(int=905)),
        },
        "steps": [{"variable": "routerPlanState",
                   "expression": expression_id(expressions["routerPlanState"])}],
    }])[0]
    decision_body = json.loads(request)
    assert decision_body["response_format"] == {"type": "json_object"}
    system = decision_body["messages"][0]["content"]
    assert system.startswith(prompt.split("\n")[0])
    assert "json" in system.casefold()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status_code,outcome", [
    (200, "succeeded"), (429, "rate_limited"), (504, "timeout"),
])
def test_native_router_model_decision_rewrites_only_to_an_eligible_gateway_alias(
    compiled_expressions: tuple[Path, str], tmp_path: Path, stream: bool,
    status_code: int, outcome: str,
) -> None:
    from tests.platform.api.test_model_router_api import model_based_definition

    first, second, decision = (UUID(int=value) for value in (901, 902, 903))
    definition = model_based_definition(decision, first, second)
    definition["nodes"] = [node for node in definition["nodes"] if node["type"] != "fallback"]
    definition["edges"] = [edge for edge in definition["edges"]
                           if edge["source"] != "fallback" and edge["target"] != "fallback"]
    candidates = {str(model_id): {
        "model_id": str(model_id), "apim_alias": alias, "display_name": alias,
        "provider_name": "Unit", "runtime_name": "Unit", "profile": "openai-v1",
        "capabilities": [], "context_window": 16384,
        "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
        "effective_cost_per_million": 2.0,
    } for model_id, alias in ((first, "candidate-a"), (second, "candidate-b"))}
    document = {
        "router_id": str(UUID(int=904)), "router_name": "unit-router", "router_version": 1,
        "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
        "status": "published", "owner_email": "offline@example.test",
        "assigned_user_ids": [], "allowed_subscription_ids": ["unit-subscription"],
        "definition": definition,
        "candidates_by_id": candidates,
        "routing_models_by_id": {str(decision): {
            "model_id": str(decision), "apim_alias": "decision-alias",
            "upstream_model": "provider-deployment-not-a-public-alias",
            "display_name": "Decision", "provider_name": "Unit", "runtime_name": "Unit",
            "decision_protocol": "chat_json", "auth_mode": "gateway_route",
            "capabilities": [], "max_tokens_field": "max_completion_tokens",
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
        }},
    }
    body = json.dumps({
        "model": "unit-router", "messages": [{"role": "user", "content": "Select candidate B"}],
        "stream": stream, "router_context": {"task_type": "unit"},
    })
    expressions = native_router_expressions()
    variables = {
        "routerCachedEntityJson": json.dumps({"Document": json.dumps(document)}),
        "routerOriginalRequestBody": body,
        "routerCallerRequestId": str(UUID(int=906)),
        "routerRequestedModel": "unit-router",
        "routerDecisionRequestId": str(UUID(int=905)),
        "routerDecisionHttp": {
            "__response": True, "status": 200, "headers": {}, "body": json.dumps({
                "choices": [{"message": {"content": json.dumps({
                    "model_id": str(second), "rationale": "Eligible candidate B",
                })}}], "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
            }),
        },
    }
    case: dict[str, Any] = {"body": body, "subscription": True, "operation": "chat-completions",
            "variables": variables, "steps": [{
                "variable": "routerPlanState",
                "expression": expression_id(expressions["routerPlanState"]),
            }]}
    request, routed = run_cases(compiled_expressions, tmp_path, [
        {**case, "expression": expression_id(expressions["routerDecisionBody"])},
        {**case, "steps": [*case["steps"], {
            "variable": "routerPlanState",
            "expression": expression_id(expressions["routerDecisionResult"]),
        }], "expression": expression_id(expressions["routerCandidateBody"])},
    ])
    decision_body = json.loads(request)
    assert decision_body["model"] == "decision-alias" and decision_body["stream"] is False
    result = json.loads(routed)
    assert result["model"] == "candidate-b" and result["stream"] is stream
    assert "router_context" not in result
    assert result["messages"] == json.loads(body)["messages"]
    completion_steps = [*case["steps"], {
        "variable": "routerPlanState",
        "expression": expression_id(expressions["routerDecisionResult"]),
    }]
    if status_code >= 400 and "routerFailedAttempt" in expressions:
        completion_steps.append({
            "variable": "routerPlanState",
            "expression": expression_id(expressions["routerFailedAttempt"]),
        })
    completion_case = {**case, "response_status": status_code, "steps": [*completion_steps, {
        "variable": "routerOutboundCompleted",
        "expression": expression_id(expressions["routerOutboundCompleted"]),
    }], "expression": expression_id(expressions["routerTraceBody"])}
    entity = json.loads(run_cases(compiled_expressions, tmp_path, [completion_case])[0])
    trace = json.loads(entity["Document"])
    assert trace["native_usage_recorded"] is True
    assert trace["selected_model_id"] == str(second)
    assert len(trace["attempts"]) == 2
    decision_attempt, candidate_attempt = trace["attempts"]
    assert decision_attempt["attempt_role"] == "routing_decision"
    assert decision_attempt["outcome"] == "succeeded"
    assert candidate_attempt["attempt_role"] == "candidate"
    assert candidate_attempt["model_id"] == str(second)
    assert candidate_attempt["model_name"] == "candidate-b"
    assert candidate_attempt["status_code"] == status_code
    assert candidate_attempt["outcome"] == outcome
    assert candidate_attempt["sequence"] == 2


@pytest.mark.parametrize("operation,details_field,prompt_field,output_field", [
    ("chat-completions", "prompt_tokens_details", "prompt_tokens", "completion_tokens"),
    ("responses", "input_tokens_details", "input_tokens", "output_tokens"),
])
@pytest.mark.parametrize("read,write", [(0, 4520), (1000, 3520), (4520, 0), (0, 0)])
@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("attempt_role", ["candidate", "routing_decision"])
def test_native_router_usage_counts_cache_writes_once(
    compiled_expressions: tuple[Path, str], tmp_path: Path, operation: str,
    details_field: str, prompt_field: str, output_field: str, read: int, write: int,
    normalized: bool, attempt_role: str,
) -> None:
    from tests.platform.api.test_model_router_api import model_based_definition

    first, second, decision = (UUID(int=value) for value in (901, 902, 903))
    definition = model_based_definition(decision, first, second)
    definition["nodes"] = [node for node in definition["nodes"] if node["type"] != "fallback"]
    definition["edges"] = [edge for edge in definition["edges"]
                           if edge["source"] != "fallback" and edge["target"] != "fallback"]
    document = {
        "router_id": str(UUID(int=904)), "router_name": "unit-router", "router_version": 1,
        "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
        "status": "published", "owner_email": "offline@example.test",
        "assigned_user_ids": [], "allowed_subscription_ids": ["unit-subscription"],
        "definition": definition,
        "candidates_by_id": {str(model_id): {
            "model_id": str(model_id), "apim_alias": alias, "display_name": alias,
            "provider_name": "Unit", "runtime_name": "Unit", "profile": "openai-v1",
            "capabilities": [], "context_window": 16384,
            "input_cost_per_million": 1.25, "cached_cost_per_million": 0.125,
            "cache_write_cost_per_million": 1.5, "output_cost_per_million": 10.0,
            "effective_cost_per_million": 10.0,
        } for model_id, alias in ((first, "candidate-a"), (second, "candidate-b"))},
        "routing_models_by_id": {str(decision): {
            "model_id": str(decision), "apim_alias": "decision-alias",
            "upstream_model": "decision-deployment", "display_name": "Decision",
            "provider_name": "Unit", "runtime_name": "Unit",
            "decision_protocol": "chat_json", "auth_mode": "gateway_route",
            "capabilities": [], "max_tokens_field": "max_completion_tokens",
            "input_cost_per_million": 1.25, "cached_cost_per_million": 0.125,
            "cache_write_cost_per_million": 1.5, "output_cost_per_million": 10.0,
        }},
    }
    body = json.dumps({"model": "unit-router",
                       "messages": [{"role": "user", "content": "Select candidate B"}]})
    expressions = native_router_expressions()
    steps = [{"variable": "routerPlanState",
              "expression": expression_id(expressions[name])}
             for name in ("routerPlanState", "routerDecisionResult")]
    usage = {
        prompt_field: 4523, details_field: {"cached_tokens": read, "cache_write_tokens": write},
        output_field: 5, "total_tokens": 4528,
        **({"cached_tokens": read + write, "cache_write_tokens": write} if normalized else {}),
    }
    other_usage = {prompt_field: 11, output_field: 2, "total_tokens": 13}
    response = json.dumps({"model": "candidate-b", "usage": (
        usage if attempt_role == "candidate" else other_usage
    )})
    case: dict[str, Any] = {
        "body": body, "subscription": True, "operation": operation, "response": response,
        "variables": {
            "routerCachedEntityJson": json.dumps({"Document": json.dumps(document)}),
            "routerOriginalRequestBody": body,
            "routerCallerRequestId": str(UUID(int=906)),
            "routerRequestedModel": "unit-router",
            "routerDecisionRequestId": str(UUID(int=905)),
            "routerDecisionHttp": {
                "__response": True, "status": 200, "headers": {}, "body": json.dumps({
                    "choices": [{"message": {"content": json.dumps({
                        "model_id": str(second), "rationale": "Eligible candidate B",
                    })}}],
                    "usage": usage if attempt_role == "routing_decision" else other_usage,
                }),
            },
        },
        "steps": steps,
        "expression": expression_id(expressions["routerOutboundCompleted"]),
    }
    completed, stored = run_cases(compiled_expressions, tmp_path, [case, {
        **case,
        "steps": [*steps, {"variable": "routerOutboundCompleted",
                           "expression": expression_id(expressions["routerOutboundCompleted"])}],
        "expression": expression_id(expressions["routerTraceBody"]),
    }])
    trace = json.loads(json.loads(stored)["Document"])
    attempt = next(item for item in trace["attempts"] if item["attempt_role"] == attempt_role)
    assert (attempt["input_tokens"], attempt["cached_tokens"],
            attempt["cache_write_tokens"], attempt["output_tokens"]) == (
        4523 - read - write, read + write, write, 5,
    )
    assert attempt["input_tokens"] + attempt["cached_tokens"] + attempt["output_tokens"] == 4528
    assert attempt["estimated_cost"] == round(
        ((4523 - read - write) * 1.25 + read * 0.125 + write * 1.5 + 5 * 10.0) / 1_000_000, 8,
    )
    completion_result = json.loads(completed)
    if attempt_role == "candidate":
        assert completion_result["body"]["usage"] == {
            **usage, "cached_tokens": read + write, "cache_write_tokens": write,
        }
    else:
        decision_usage = trace["usage_records"][0]
        for field in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens",
                      "estimated_cost"):
            assert decision_usage[field] == attempt[field]


@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("read,write", [(0, 4520), (1000, 3520), (4520, 0), (0, 0)])
def test_direct_model_client_usage_normalizes_cache_writes_once(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    normalized: bool, read: int, write: int,
) -> None:
    usage = {
        "prompt_tokens": 4523, "completion_tokens": 5, "total_tokens": 4528,
        "prompt_tokens_details": {"cached_tokens": read, "cache_write_tokens": write},
        **({"cached_tokens": read + write, "cache_write_tokens": write} if normalized else {}),
    }
    response = {"id": "unit-provider-request", "model": "unit-model", "usage": usage}
    expression = native_router_expressions()["directModelResponseBody"]
    case = {
        "body": "{}", "operation": "chat-completions", "response": json.dumps(response),
        "variables": {"routerCallerRequestId": str(UUID(int=906)),
                      "routerRequestedModel": "unit-model"},
        "expression": expression_id(expression),
    }
    rewritten = json.loads(run_cases(compiled_expressions, tmp_path, [case])[0])
    assert rewritten["usage"] == {
        **usage, "cached_tokens": read + write, "cache_write_tokens": write,
    }
    assert rewritten["usage"]["cached_tokens"] == read + write
    assert rewritten["usage"]["prompt_tokens"] == 4523
    assert rewritten["usage"]["total_tokens"] == 4528
    repeated = json.loads(run_cases(compiled_expressions, tmp_path, [{
        **case, "response": json.dumps(rewritten),
    }])[0])
    assert repeated["usage"] == rewritten["usage"]


@pytest.mark.parametrize("stream", [False, True])
def test_native_router_system_one_decision_uses_the_gateway_decision_operation(
    compiled_expressions: tuple[Path, str], tmp_path: Path, stream: bool,
) -> None:
    from tests.platform.api.test_model_router_api import model_based_definition

    first, second, decision = (UUID(int=value) for value in (901, 902, 903))
    definition = model_based_definition(decision, first, second)
    definition["nodes"] = [node for node in definition["nodes"] if node["type"] != "fallback"]
    definition["edges"] = [edge for edge in definition["edges"]
                           if edge["source"] != "fallback" and edge["target"] != "fallback"]
    document: dict[str, Any] = {
        "router_id": str(UUID(int=904)), "router_name": "unit-router", "router_version": 1,
        "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
        "status": "published", "owner_email": "offline@example.test",
        "assigned_user_ids": [], "allowed_subscription_ids": ["unit-subscription"],
        "definition": definition,
        "candidates_by_id": {str(model_id): {
            "model_id": str(model_id), "apim_alias": alias, "display_name": alias,
            "provider_name": "Unit", "runtime_name": "Unit", "profile": "openai-v1",
            "capabilities": [], "context_window": 16384,
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
            "effective_cost_per_million": 2.0,
        } for model_id, alias in ((first, "candidate-a"), (second, "candidate-b"))},
        "routing_models_by_id": {str(decision): {
            "model_id": str(decision), "apim_alias": "decision-1",
            "upstream_model": "Decision-1-deployment", "display_name": "Decision-1",
            "provider_name": "Microsoft Foundry", "runtime_name": "Unit",
            "decision_protocol": "system_one", "auth_mode": "gateway_route",
            "capabilities": ["decision"], "input_cost_per_million": 0.042,
            "output_cost_per_million": 0.0,
        }},
    }
    body = json.dumps({
        "model": "unit-router", "messages": [{"role": "user", "content": "Prove a theorem"}],
        "stream": stream,
    })
    expressions = native_router_expressions()
    case: dict[str, Any] = {"body": body, "subscription": True, "operation": "chat-completions",
            "variables": {
                "routerCachedEntityJson": json.dumps({"Document": json.dumps(document)}),
                "routerOriginalRequestBody": body,
                "routerCallerRequestId": str(UUID(int=906)),
                "routerRequestedModel": "unit-router",
                "routerDecisionRequestId": str(UUID(int=905)),
                "routerDecisionHttp": {
                    "__response": True, "status": 200, "headers": {}, "body": json.dumps({
                        "answers": {"route": {
                            "choice": str(second), "confidence": 0.86, "type": "choice",
                            "probabilities": {str(first): 0.14, str(second): 0.86},
                        }},
                        "usage": {"input_tokens": 92, "output_tokens": 3},
                    }),
                },
            }, "steps": [{
                "variable": "routerPlanState",
                "expression": expression_id(expressions["routerPlanState"]),
            }]}
    decided = [*case["steps"], {
        "variable": "routerPlanState",
        "expression": expression_id(expressions["routerDecisionResult"]),
    }]
    url, request, routed, stored = run_cases(compiled_expressions, tmp_path, [
        {**case, "expression": expression_id(expressions["routerDecisionUrl"])},
        {**case, "expression": expression_id(expressions["routerDecisionBody"])},
        {**case, "steps": decided, "expression": expression_id(expressions["routerCandidateBody"])},
        {**case, "steps": [*decided, {
            "variable": "routerOutboundCompleted",
            "expression": expression_id(expressions["routerOutboundCompleted"]),
        }], "expression": expression_id(expressions["routerTraceBody"])},
    ])
    assert url == "https://unit.azure-api.net/turnstile/providers/microsoft/v1/systemone"
    decision_body = json.loads(request)
    assert set(decision_body) == {"model", "state", "questions"}
    assert decision_body["model"] == "decision-1"
    route = decision_body["questions"]["route"]
    assert route["type"] == "choice" and set(route["criteria"]) == {str(first), str(second)}
    assert analyze(compiled_expressions, tmp_path, "microsoft-decision", decision_body)[
        "error"] == ""
    assert json.loads(routed)["model"] == "candidate-b"
    trace = json.loads(json.loads(stored)["Document"])
    assert trace["native_usage_recorded"] is True
    assert trace["selected_model_id"] == str(second)
    decision_attempt = trace["attempts"][0]
    assert decision_attempt["attempt_role"] == "routing_decision"
    assert decision_attempt["decision_protocol"] == "system_one"
    assert decision_attempt["outcome"] == "succeeded"
    assert decision_attempt["request_id"] == str(UUID(int=905))
    assert decision_attempt["decision_probabilities"] == {str(first): 0.14, str(second): 0.86}
    chat = {**document["routing_models_by_id"][str(decision)], "decision_protocol": "chat_json"}
    chat_document = {**document, "routing_models_by_id": {str(decision): chat}}
    chat_url = run_cases(compiled_expressions, tmp_path, [{
        **case, "variables": {**case["variables"], "routerCachedEntityJson": json.dumps({
            "Document": json.dumps(chat_document),
        })}, "expression": expression_id(expressions["routerDecisionUrl"]),
    }])[0]
    assert chat_url == "https://unit.azure-api.net/turnstile/chat/completions"


@pytest.mark.parametrize("state_variable,expression_name", [
    ("routerOutboundCompleted", "routerNameHeader"),
    ("routerPlanState", "routerPlanStateNameHeader"),
    ("routerAnthropicCompleted", "routerAnthropicCompletedNameHeader"),
])
def test_native_router_name_header_is_ascii_for_any_router_name(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    state_variable: str, expression_name: str,
) -> None:
    expression = native_router_expressions()[expression_name]
    cases = [
        ("Unified Router 1", "Unified Router 1"),
        ("统一条件路由", "%E7%BB%9F%E4%B8%80%E6%9D%A1%E4%BB%B6%E8%B7%AF%E7%94%B1"),
        ("50% router", "50% router"), ("50%20 router", "50%20 router"),
        ("Printable ~% /?+;=,:\"", "Printable ~% /?+;=,:\""),
        ("Router\r\nx-injected: yes", "Router%0D%0Ax-injected%3A%20yes"),
        ("Router café \U0001f600", "Router%20caf%C3%A9%20%F0%9F%98%80"),
        ("路由 %\t名", "%E8%B7%AF%E7%94%B1%20%25%09%E5%90%8D"),
        *[(f"Router{chr(codepoint)}name", f"Router%{codepoint:02X}name")
          for codepoint in (*range(0x20), 0x7F)],
    ]

    values = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(expression),
        "variables": {state_variable: json.dumps({"router_name": name})},
    } for name, _ in cases])

    assert values == [header for _, header in cases]
    assert all(" " <= character <= "~" for value in values for character in value)


def test_native_router_keeps_other_owner_name_access_after_stale_backfill(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    router_repository: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.platform.api.test_model_router_api import same_name_router_backfill_scenario

    before, after, second_id = same_name_router_backfill_scenario(router_repository, monkeypatch)
    expressions = native_router_expressions()
    body = json.dumps({
        "model": "Shared owner backfill", "messages": [{"role": "user", "content": "Hello"}],
    })
    cases = [{
        "expression": expression_id(expressions["routerPlanState"]),
        "body": body, "subscription": True, "operation": "chat-completions", "variables": {
            "routerCachedEntityJson": json.dumps(entity),
            "routerOriginalRequestBody": body,
            "routerRequestedModel": "Shared owner backfill",
            "routerCallerRequestId": str(UUID(int=907)),
        },
    } for entity in (before, after)]
    for result in run_cases(compiled_expressions, tmp_path, cases):
        planned = json.loads(result)
        assert not planned.get("access_denied", False)
        assert planned["router_id"] == str(second_id)
        assert planned["selected_candidate"]["model_id"]


@pytest.mark.parametrize("mode,version,subscription,person,allowed", [
    ("active", 2, False, False, True), ("active", 1, False, False, False),
    ("paused", 2, False, True, False), ("paused", 2, True, False, False),
    ("paused", 2, True, True, True), ("prepared", 1, False, False, True),
    ("prepared", 2, True, False, True), ("unknown", 2, False, False, False),
])
def test_shared_fence_executes_version_and_validation_scope_gates(
    compiled_expressions: tuple[Path, str], tmp_path: Path, mode: str, version: int,
    subscription: bool, person: bool, allowed: bool,
) -> None:
    node = compiler.budget_execution_fence(["unit-turnstile"]).find(
        ".//set-variable[@name='budgetExecutionFence']"
    )
    assert node is not None
    document = {
        "protocol_version": 2, "mode": mode, "api_id": "unit-turnstile",
        "apim_service_id": "unit-service", "gateways": {"managed": ["candidate"]},
        "validation_revision": "candidate", "validation_subscription_id": "unit-subscription",
        "validation_person_id": "unit-person" if person else None,
    }
    result = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "subscription": subscription,
        "expression": expression_id(node.attrib["value"]),
        "variables": {"budgetExecutionVersion": version, "budgetExecutionFenceRead": {
            "__response": True, "status": 200, "headers": {},
            "body": json.dumps({"Document": json.dumps(document)}),
        }},
    }])[0]
    assert bool(result) is allowed
    if allowed:
        assert json.loads(result) == document


@pytest.fixture(scope="module")
def compiled_expressions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    executable = shutil.which("dotnet")
    assert executable is not None, "The policy execution gate requires the .NET SDK"
    directory = tmp_path_factory.mktemp("budget-policy-csharp")
    expressions = {
        value
        for name, value in vars(compiler).items()
        if name.isupper() and isinstance(value, str) and value.startswith("@")
    }
    expressions.update(application_tpm_expressions().values())
    expressions.update(application_attribution_expressions().values())
    expressions.update(native_router_expressions().values())
    expressions.update(business_denial_expressions().values())
    expressions.add(text_usage_expression())
    expressions.update(value for name, value in vars(availability).items()
                       if name.isupper() and isinstance(value, str) and value.startswith("@"))
    expressions.add(storage_routes.READ_FIXED_CONTENT)
    assembled = ET.Element("inbound")
    compiler._native_count(assembled)
    compiler._deny(assembled, "@(true)", "budget_request_invalid", cancel_preparing=True)
    assembled.append(compiler._admission())
    assembled.append(compiler._completion())
    assembled.append(compiler.budget_execution_fence(["unit-turnstile"]))
    for node in assembled.iter():
        for expression in [*node.attrib.values(), node.text or ""]:
            if expression.strip().startswith("@"):
                expressions.add(expression.strip())
    methods, branches = [], []
    for expression in sorted(expressions):
        name = "Expression_" + expression_id(expression)
        body = expression[2:-1] if expression.startswith("@{") else "return " + expression[1:] + ";"
        body = body.replace("__GATEWAY_PROFILE_ID__", "90000000-0000-4000-8000-000000000002")
        methods.append(f"public static object {name}(Context context) {{ {body} }}")
        branches.append(f'case "{expression_id(expression)}": return {name}(context);')
    (directory / "Program.cs").write_text(
        HARNESS.replace("__METHODS__", "\n".join(methods)).replace(
            "__SWITCH__", "\n".join(branches)
        )
    )
    (directory / "NuGet.Config").write_text(
        "<configuration><packageSources><clear /></packageSources></configuration>"
    )
    (directory / "BudgetPolicy.csproj").write_text("""<Project Sdk="Microsoft.NET.Sdk">
      <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework>
        <LangVersion>7.3</LangVersion><ImplicitUsings>disable</ImplicitUsings><Nullable>disable</Nullable>
      </PropertyGroup><ItemGroup><Reference Include="Newtonsoft.Json">
        <HintPath>$(MSBuildSDKsPath)/../Newtonsoft.Json.dll</HintPath>
      </Reference></ItemGroup></Project>""")
    result = subprocess.run(
        [
            executable,
            "build",
            str(directory / "BudgetPolicy.csproj"),
            "--nologo",
            "--verbosity",
            "quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return directory / "bin/Debug/net8.0/BudgetPolicy.dll", executable


def run_cases(
    compiled: tuple[Path, str],
    tmp_path: Path,
    cases: list[dict[str, Any]],
) -> list[Any]:
    assembly, executable = compiled
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    result = subprocess.run(
        [executable, str(assembly), str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    results: list[dict[str, Any]] = json.loads(result.stdout)
    assert all("exception" not in result for result in results), results
    for case, evaluated in zip(cases, results, strict=True):
        assert evaluated["body_after"] == case["body"]
    return [result["value"] for result in results]


@pytest.mark.parametrize("change,status,outcome", [
    ({}, 200, "disabled"), ({"Enabled": True}, 200, "enabled"),
    ({"Enabled": "true"}, 200, "unavailable"), ({"Revision": True}, 200, "unavailable"),
    ({"Revision": 0}, 200, "unavailable"), ({"SchemaVersion": True}, 200, "unavailable"),
    ({"ModelId": "other"}, 200, "unavailable"), ({"RowKey": "other"}, 200, "unavailable"),
    ({"Lifecycle": "deleted", "Enabled": True}, 200, "unavailable"),
    ({}, 404, "unavailable"), ({}, 503, "unavailable"),
])
def test_availability_state_expression_rejects_unknown_or_wrong_identity(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    change: dict[str, Any], status: int, outcome: str,
) -> None:
    model_id = "00000000-0000-0000-0000-000000000002"
    entity = {"PartitionKey": "model-availability|v1", "RowKey": model_id,
              "ModelId": model_id, "SchemaVersion": 1, "Revision": 2,
              "Lifecycle": "active", "Enabled": False, **change}
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.READ_STATE), "body": "{}",
        "variables": {"availabilityModelId": model_id, "availabilityRead": {
            "__response": True, "status": status, "headers": {"ETag": '"unit"'},
            "body": json.dumps(entity),
        }},
    }])[0]
    assert json.loads(result)["outcome"] == outcome


@pytest.mark.parametrize("method,operation,kind,accepted", [
    ("POST", "chat-completions", "inference", False),
    ("HEAD", "chat-completions", "inference", False),
    ("HEAD", "availability-probe-chat-completions", "inference", True),
    ("HEAD", "availability-probe-models", "catalog", True),
    ("HEAD", "models", "catalog", False),
    ("GET", "models", "catalog", True),
])
def test_availability_probe_cannot_bypass_normal_inference(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    method: str, operation: str, kind: str, accepted: bool,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.PROBE_NONCE), "body": "{}", "method": method,
        "operation": operation, "headers": {"x-turnstile-availability-probe": nonce},
        "variables": {"availabilitySurfaceKind": kind},
    }])[0]
    assert result == (nonce if accepted else "")


@pytest.mark.parametrize("enabled", [False, True])
def test_availability_catalog_filters_actual_state_and_preserves_model_shape(
    compiled_expressions: tuple[Path, str], tmp_path: Path, enabled: bool,
) -> None:
    model_id = "00000000-0000-0000-0000-000000000002"
    result = json.loads(run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.FILTER_CATALOG), "body": "{}",
        "variables": {
            "availabilityCatalogPage": json.dumps({"ok": True, "pages": 1,
                "states": {model_id: {"enabled": enabled, "revision": 2}},
                "next_partition": None, "next_row": None}),
            "availabilityCatalogScope": json.dumps([
                {"id": "unit-model", "owned_by": "unit-provider", "logical_model_id": model_id},
            ]),
        },
    }])[0])
    assert result["ok"] is True
    assert result["body"]["data"] == (
        [{"id": "unit-model", "owned_by": "unit-provider"}] if enabled else []
    )
    expected = f"{model_id}:2:{1 if enabled else 0}"
    assert result["digest"] == hashlib.sha256(expected.encode()).hexdigest()


@pytest.mark.parametrize("change", [
    {}, {"InstanceId": "other"}, {"CapabilityDigest": "other"}, {"ApiRevision": "other"},
    {"SchemaVersion": True}, {"IssuedEpoch": "01"}, {"ExpiresEpoch": "1"},
    {"IssuedEpoch": "999999999999"}, {"Lifetime": -1}, {"ETag": "*"},
])
def test_availability_native_node_lease_requires_exact_identity_and_short_lifetime(
    compiled_expressions: tuple[Path, str], tmp_path: Path, change: dict[str, Any],
) -> None:
    now = int(datetime.now(UTC).timestamp())
    target = "90000000-0000-4000-8000-000000000002"
    key = hashlib.sha256(b'["candidate","edge","node-one"]').hexdigest()
    entity = {
        "PartitionKey": f"model-availability-nodes|v1|{target}|unit-turnstile",
        "RowKey": key, "TargetId": target, "ApiId": "unit-turnstile", "ApiRevision": "candidate",
        "NativeGatewayId": "edge", "InstanceId": "node-one", "CapabilityDigest": "a" * 64,
        "SchemaVersion": 1, "IssuedEpoch": str(now - 1),
        "ExpiresEpoch": str(now + change.get("Lifetime", 60)), **change,
    }
    actual = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.READ_NODE), "body": "{}",
        "gateway": "edge", "instance": "node-one",
        "variables": {"availabilityTargetId": target, "availabilityNodeKey": key,
            "availabilityCapabilityDigest": "a" * 64, "availabilityNodeRead": {
                "__response": True, "status": 200,
                "headers": {"ETag": change.get("ETag", '"unit"')}, "body": json.dumps(entity),
            }},
    }])[0]
    assert actual is (not change)


@pytest.mark.parametrize("gateway,routes,body,expected", [
    ("managed", None, {"model": " UNIT "}, True),
    ("edge", ["edge"], {"model": "unit"}, True),
    ("managed", ["edge"], {"model": "unit"}, False),
    ("managed", None, {"model": 1}, False),
    ("managed", None, {"model": "other"}, False),
])
def test_availability_native_alias_resolution_honors_serving_scope(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    gateway: str, routes: list[str] | None, body: dict[str, Any], expected: bool,
) -> None:
    model_id = "00000000-0000-0000-0000-000000000002"
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.RESOLVE_MODEL), "body": json.dumps(body),
        "gateway": gateway, "variables": {"availabilityRoutes": json.dumps([
            {"alias": "unit", "model_id": model_id, "gateways": routes},
        ])},
    }])[0]
    assert result == (model_id if expected else "")


@pytest.mark.parametrize("gateway,routes,alias,expected", [
    ("managed", None, "unit", True),
    ("edge", ["edge"], "unit", True),
    ("managed", ["edge"], "unit", False),
    ("edge", ["edge"], "UNIT", False),
    ("edge", ["edge"], "other", False),
])
def test_availability_fixed_alias_does_not_read_the_request_body(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    gateway: str, routes: list[str] | None, alias: str, expected: bool,
) -> None:
    model_id = "00000000-0000-0000-0000-000000000002"
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.RESOLVE_FIXED_MODEL),
        "body": '{"model":"different-body-model"}', "gateway": gateway,
        "variables": {
            "availabilityFixedModelAlias": alias,
            "availabilityRoutes": json.dumps([
                {"alias": "unit", "model_id": model_id, "gateways": routes},
            ]),
        },
    }])[0]
    assert result == (model_id if expected else "")


@pytest.mark.parametrize("failure", ["duplicate", "foreign", "boolean", "revision", "continuation"])
def test_availability_native_catalog_pages_reject_invalid_rows(
    compiled_expressions: tuple[Path, str], tmp_path: Path, failure: str,
) -> None:
    model_id = "00000000-0000-0000-0000-000000000002"
    entity = {"PartitionKey": "model-availability|v1", "RowKey": model_id, "ModelId": model_id,
              "SchemaVersion": 1, "Revision": 2, "Enabled": False, "Lifecycle": "active"}
    if failure == "foreign":
        entity["PartitionKey"] = "other"
    if failure == "boolean":
        entity["Enabled"] = "false"
    if failure == "revision":
        entity["Revision"] = 2147483648
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.CATALOG_PAGE), "body": "{}",
        "variables": {
            "availabilityCatalogPage": json.dumps({"ok": True, "pages": 0, "states": {}}),
            "availabilityCatalogRead": {"__response": True, "status": 200,
                "headers": {"x-ms-continuation-NextRowKey": "next"}
                if failure == "continuation" else {},
                "body": json.dumps({
                    "value": [entity, entity] if failure == "duplicate" else [entity],
                })},
        },
    }])[0]
    assert json.loads(result) == {"ok": False}


@pytest.mark.parametrize("failure", ["incomplete", "missing", "failed"])
def test_availability_native_catalog_never_returns_partial_success(
    compiled_expressions: tuple[Path, str], tmp_path: Path, failure: str,
) -> None:
    model_id = "00000000-0000-0000-0000-000000000002"
    page = {"ok": failure != "failed", "pages": 4,
            "next_partition": "next" if failure == "incomplete" else None, "next_row": None,
            "states": {} if failure == "missing" else {model_id: {"revision": 2, "enabled": False}}}
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(availability.FILTER_CATALOG), "body": "{}",
        "variables": {"availabilityCatalogPage": json.dumps(page),
                      "availabilityCatalogScope": json.dumps([
                          {"id": "unit", "logical_model_id": model_id},
                      ])},
    }])[0]
    assert json.loads(result) == {"ok": False}


def test_execution_retains_configured_public_application_tpm(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
) -> None:
    expressions = application_tpm_expressions()
    values = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(expressions["applicationTokensPerMinute"]),
        "variables": {
            "applicationAdmission": "ok",
            "applicationRateLimitPartition": "app-tpm|unit-application",
            "applicationRateLimitRead": {
                "__response": True, "status": 200, "headers": {"ETag": 'W/"unit"'},
                "body": json.dumps({
                    "PartitionKey": "app-tpm|unit-application", "RowKey": "T",
                    "TokensPerMinute": 4321,
                }),
            },
        },
    }, {
        "body": "{}", "expression": expression_id(expressions["applicationLedgerUrl"]),
        "variables": {"applicationLedgerPartition": "unit-app|2026-09",
                      "applicationMapState": {"applicationType": "service"}},
    }, {
        "body": "{}", "expression": expression_id(expressions["applicationRateLimitUrl"]),
        "variables": {"applicationRateLimitPartition": "app-tpm|unit-application"},
    }])
    assert values[0] == 4321
    from urllib.parse import unquote

    query = unquote(values[1])
    assert "RowKey eq 'M'" in query and "RowKey eq 'Q'" not in query
    assert "RowKey eq 'C'" not in query and "RowKey eq 'R" not in query
    assert values[2] == (
        "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__"
        "(PartitionKey='app-tpm%7Cunit-application',RowKey='T')"
    )


@pytest.mark.parametrize("status,etag,changes", [
    (404, 'W/"unit"', {}), (200, "", {}), (200, "*", {}),
    (200, 'W/"unit"', {"PartitionKey": "foreign-application"}),
    (200, 'W/"unit"', {"RowKey": "Q"}),
    (200, 'W/"unit"', {"TokensPerMinute": True}),
    (200, 'W/"unit"', {"TokensPerMinute": 1.5}),
    (200, 'W/"unit"', {"TokensPerMinute": 0}),
    (200, 'W/"unit"', {"TokensPerMinute": -1}),
    (200, 'W/"unit"', {"TokensPerMinute": 2**31}),
])
def test_execution_tpm_rejects_unproven_or_invalid_rate_without_a_default(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    status: int, etag: str, changes: dict[str, object],
) -> None:
    expression = application_tpm_expressions()["applicationTokensPerMinute"]
    row = {"PartitionKey": "app-tpm|unit-application", "RowKey": "T",
           "TokensPerMinute": 4321, **changes}
    result = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(expression),
        "variables": {
            "applicationRateLimitPartition": "app-tpm|unit-application",
            "applicationRateLimitRead": {
                "__response": True, "status": status, "headers": {"ETag": etag},
                "body": json.dumps(row),
            },
        },
    }])[0]
    assert result == 0


@pytest.mark.parametrize("person", [True, False])
def test_application_traffic_keeps_validated_person_organization(
    compiled_expressions: tuple[Path, str], tmp_path: Path, person: bool,
) -> None:
    expressions = application_attribution_expressions()
    person_headers = {
        "x-org-id": "org-person", "x-org-name": "Person Org",
        "x-department-id": "department-person", "x-department-name": "Person Department",
    }
    registered = {
        "organizationId": "org-application", "organizationName": "Application Org",
        "departmentId": "department-application", "departmentName": "Application Department",
    }
    values = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(expression), "headers": person_headers,
        "variables": {"hasEmployeeToken": person, "applicationMapState": registered},
    } for expression in expressions.values()])
    assert dict(zip(expressions, values, strict=True)) == (
        person_headers if person else dict(zip(person_headers, registered.values(), strict=True))
    )


def analyze(
    compiled: tuple[Path, str],
    tmp_path: Path,
    operation: str,
    body: Any,
) -> dict[str, Any]:
    analysis: dict[str, Any] = run_cases(
        compiled,
        tmp_path,
        [
            {
                "expression": expression_id(compiler.REQUEST_ANALYSIS),
                "operation": operation,
                "body": json.dumps(body),
                "variables": {},
            }
        ],
    )[0]
    return analysis


@pytest.mark.parametrize("operation", ["anthropic-messages", "chat-completions", "responses"])
@pytest.mark.parametrize(
    "business",
    [
        {"type": ["string", "null"]},
        {"type": {"nested": [1, 2]}},
        {"type": "image", "source": {"type": "url", "url": "https://business.example/file"}},
        {"encrypted_content": "business-secret", "url": "https://business.example"},
        {"nested": [{"input": {"type": "web_search"}}]},
        {"text": '{"type":"image","url":"https://business.example"}'},
        {"background": "transparent"},
        {"conversation": "business-data"},
    ],
)
def test_business_json_is_not_protocol_content(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    operation: str,
    business: dict[str, Any],
) -> None:
    if operation == "anthropic-messages":
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "unit", "name": "unit", "input": business},
                    ],
                }
            ],
            "tools": [{"name": "unit", "input_schema": business}],
            "metadata": business,
        }
    elif operation == "chat-completions":
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": json.dumps(business),
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "unit", "arguments": json.dumps(business)},
                        }
                    ],
                }
            ],
            "tools": [{"type": "function", "function": {"name": "unit", "parameters": business}}],
            "metadata": business,
        }
    else:
        body = {
            "input": [{"type": "function_call", "arguments": json.dumps(business)}],
            "tools": [{"type": "function", "name": "unit", "parameters": business}],
            "metadata": business,
        }
    result = analyze(compiled_expressions, tmp_path, operation, body)
    assert result == {
        "error": "",
        "version": 1,
        "complete": True,
        "remote_content": False,
        "required_features": [],
        "request_controls": [],
    }


@pytest.mark.parametrize(
    "operation,body,features,remote",
    [
        (
            "anthropic-messages",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "unit",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "url",
                                            "url": "https://content.example/image",
                                        },
                                    },
                                ],
                            },
                        ],
                    }
                ]
            },
            ["multimodal_input"],
            True,
        ),
        (
            "chat-completions",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,unit"},
                            },
                        ],
                    }
                ]
            },
            ["multimodal_input"],
            False,
        ),
        (
            "responses",
            {
                "input": [
                    {
                        "type": "function_call_output",
                        "output": [
                            {"type": "input_file", "file_id": "file-unit"},
                        ],
                    }
                ]
            },
            ["multimodal_input"],
            True,
        ),
        (
            "responses",
            {"input": [{"type": "reasoning", "encrypted_content": "opaque"}]},
            ["opaque_state"],
            False,
        ),
        (
            "responses",
            {"input": [{"type": "item_reference", "id": "unit"}]},
            ["conversation_state"],
            True,
        ),
        ("responses", {"input": "unit", "background": True}, ["background"], False),
        (
            "responses",
            {"input": "unit", "tools": [{"type": "web_search"}]},
            ["server_tools"],
            False,
        ),
        (
            "images-generations",
            {"prompt": "unit", "background": "transparent", "size": "auto"},
            [],
            False,
        ),
    ],
)
def test_real_protocol_features_remain_visible(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    operation: str,
    body: dict[str, Any],
    features: list[str],
    remote: bool,
) -> None:
    result = analyze(compiled_expressions, tmp_path, operation, body)
    assert result["error"] == "" and result["complete"] is True
    assert result["required_features"] == features and result["remote_content"] is remote


@pytest.mark.parametrize(
    "operation,body",
    [
        ("responses", {"input": "unit", "future_option": {"anything": True}}),
        ("responses-compact", {"input": "unit", "background": "transparent"}),
        ("anthropic-messages", {"messages": [{"role": "user", "content": [{"type": "future"}]}]}),
        ("chat-completions", {"messages": [], "tools": [{"type": "future_tool"}]}),
    ],
)
def test_unknown_structure_is_incomplete_not_plain_text(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    operation: str,
    body: dict[str, Any],
) -> None:
    result = analyze(compiled_expressions, tmp_path, operation, body)
    assert result["error"] == "" and result["complete"] is False


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"messages": {}},
        {"messages": [{"content": [{"type": ["text"]}]}]},
        {"messages": [{"content": [{"type": {"secret": "value"}}]}]},
        {"messages": [{"content": [{"type": "text", "text": {"secret": "value"}}]}]},
        {"messages": [], "stream": "true"},
    ],
)
def test_invalid_protocol_reports_only_fixed_structure_categories(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    body: Any,
) -> None:
    result = analyze(compiled_expressions, tmp_path, "anthropic-messages", body)
    assert result["error"] == "budget_request_invalid"
    assert result.get("error_path") in {
        None,
        "anthropic-messages",
        "anthropic-block",
        "required-text",
    }
    assert "secret" not in json.dumps(result) and "value" not in json.dumps(result)


def planned_case(
    body: dict[str, Any], operation: str = "anthropic-messages", *, legacy: bool = False
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "model_id": "90000000-0000-4000-8000-000000000003",
        "algorithm": "legacy_request_estimate_v1" if legacy else "context_input_ceiling_v1",
        "input_ceiling": 10000,
        "output_ceiling": 4096,
        "output_estimate": 4096,
        "output_field": "max_tokens",
        "operations": [operation],
        "evidence_sha256": "a" * 64,
        "supported_features": ["multimodal_input", "opaque_state", "conversation_state"],
        "native_counter": {
            "kind": "anthropic_messages",
            "assurance": "upper_bound",
            "endpoint": "https://count.example/v1/messages/count_tokens",
            "supported_features": ["multimodal_input"],
            "upper_bound_padding_tokens": 8,
        },
    }
    return {
        "expression": expression_id(compiler.PLAN),
        "body": json.dumps(body),
        "operation": operation,
        "subscription": True,
        "variables": {
            "budgetExecutionFence": json.dumps(
                {
                    "mode": "active",
                    "protocol_version": 2,
                    "gateway_profile_id": "90000000-0000-4000-8000-000000000002",
                    "epoch": "90000000-0000-4000-8000-000000000001",
                    "model_contracts": {"unit-model": contract},
                }
            ),
            "selectedBudgetModelKey": "unit-model",
            "selectedBudgetObserver": True,
            "selectedBudgetUpstreamModel": "unit-upstream",
            "selectedBudgetCounterOrigin": "https://count.example/v1",
            "selectedBudgetCounterAuth": "managed_identity",
            "requestStarted": "2026-09-22T00:00:00Z",
            "applicationMapState": {
                "status": "ok",
                "applicationType": "service",
                "applicationId": "90000000-0000-4000-8000-000000000004",
            },
        },
        "steps": [
            {
                "expression": expression_id(compiler.REQUEST_ANALYSIS),
                "variable": "executionRequestAnalysis",
            }
        ],
    }


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
def test_plan_uses_shared_analysis_and_keeps_complete_byte_estimate(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    legacy: bool,
    unknown: bool,
) -> None:
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": "unit"}],
        "max_tokens": 32,
        "tools": [
            {
                "name": "unit",
                "input_schema": {
                    "type": ["object", "null"],
                    "url": "https://business.example",
                    "data": "x" * 1024,
                },
            }
        ],
    }
    if unknown:
        body["unknown_extension"] = True
    case = planned_case(body, legacy=legacy)
    result = run_cases(compiled_expressions, tmp_path, [case])[0]
    if unknown and not legacy:
        assert result["error"] == "budget_count_bound_unavailable"
        return
    assert result["error"] == ""
    assert result["body_bytes_estimate"] == len(case["body"].encode())
    assert result["receipts"][0]["reserved_tokens"] == (
        len(case["body"].encode()) + 32 if legacy else 10032
    )
    assert result["request_analysis"]["complete"] is not unknown
    assert result["count_assurance"] == ("estimate" if legacy else "verified_upper_bound")


@pytest.mark.parametrize("remote", [False, True])
def test_native_count_uses_protocol_remote_state_not_business_urls(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    remote: bool,
) -> None:
    content: list[dict[str, Any]] = [
        {
            "type": "tool_use",
            "input": {
                "type": ["object"],
                "url": "https://business.example",
                "encrypted_content": "data",
            },
        }
    ]
    if remote:
        content.append(
            {
                "type": "tool_result",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://content.example/image"},
                    }
                ],
            }
        )
    case = planned_case({"messages": [{"role": "assistant", "content": content}], "max_tokens": 32})
    case["steps"].append({"expression": expression_id(compiler.PLAN), "variable": "executionPlan"})
    case["expression"] = expression_id(compiler.NATIVE_COUNT_PLAN)
    result = run_cases(compiled_expressions, tmp_path, [case])[0]
    assert result["enabled"] is not remote
    if not remote:
        assert result["payload"]["messages"][0]["content"] == content
        assert result["payload"]["model"] == "unit-upstream"
        assert "max_tokens" not in result["payload"]


@pytest.mark.parametrize(
    "assurance,count,expected",
    [
        ("estimate", 7, 10032),
        ("upper_bound", 7, 47),
        ("upper_bound", "7", 10032),
        ("upper_bound", -1, 10032),
        ("upper_bound", True, 10032),
    ],
)
def test_native_count_apply_never_shrinks_from_estimate_or_invalid_response(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    assurance: str,
    count: Any,
    expected: int,
) -> None:
    case = planned_case({"messages": [{"role": "user", "content": "unit"}], "max_tokens": 32})
    case["steps"].append({"expression": expression_id(compiler.PLAN), "variable": "executionPlan"})
    case["variables"].update(
        {
            "executionCountPlan": {"assurance": assurance, "padding": 8, "fingerprint": "unit"},
            "executionCountResponse": {
                "__response": True,
                "status": 200,
                "headers": {},
                "body": json.dumps({"input_tokens": count}),
            },
        }
    )
    case["expression"] = expression_id(compiler.NATIVE_COUNT_APPLY)
    result = run_cases(compiled_expressions, tmp_path, [case])[0]
    assert result["receipts"][0]["reserved_tokens"] == expected
    assert result["output_limit"] == 32 and result["output_samples"] == 1


@pytest.mark.parametrize(
    "code,status",
    [
        ("budget_request_invalid", 400),
        ("budget_request_analysis_failed", 500),
        ("budget_plan_failed", 500),
        ("budget_configuration_invalid", 503),
        ("budget_identity_unavailable", 503),
        ("budget_ledger_unavailable", 503),
        ("budget_temporarily_reserved", 429),
        ("budget_exhausted", 403),
        ("budget_output_limit_invalid", 400),
    ],
)
def test_error_owner_preserves_status_contract(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    code: str,
    status: int,
) -> None:
    owner = compiler._admission().find(".//set-variable[@name='executionStatus']")
    assert owner is not None
    result = run_cases(
        compiled_expressions,
        tmp_path,
        [
            {
                "expression": expression_id(owner.attrib["value"]),
                "body": "{}",
                "variables": {"executionError": code},
            }
        ],
    )[0]
    assert result == status


@pytest.mark.parametrize("header_count,fault,accepted", [
    (0, None, True), (90, None, True), (91, None, False),
    (0, "separate-runtime", True), (0, "duplicate-backend", False),
    (0, "duplicate-backend-case", False), (0, "invalid-runtime", False),
    (0, "pool-disabled", False),
    (90, "configuration-sha", True), (0, "invalid-configuration-sha", False),
])
def test_fixed_content_csharp_accepts_deployments_not_unique_runtimes(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    header_count: int, fault: str | None, accepted: bool,
) -> None:
    from tests.backend.model_platform.test_storage_route_contracts import route_fixture
    from turnstile_core.domain.storage_routes import canonical_document

    route, _ = route_fixture()
    document = route.model_dump(mode="json")
    members = [
        {"runtime_id": str(UUID(int=80)), "backend_id": "unit-member-" + str(index),
         "dependency_sha256": "a" * 64, "priority": 0, "weight": 1}
        for index in range(2)
    ]
    if fault == "separate-runtime":
        members[1]["runtime_id"] = str(UUID(int=81))
    elif fault == "duplicate-backend":
        members[1]["backend_id"] = members[0]["backend_id"]
    elif fault == "duplicate-backend-case":
        members[1]["backend_id"] = str(members[0]["backend_id"]).upper()
    elif fault == "invalid-runtime":
        members[1]["runtime_id"] = "not-a-uuid"
    document["connection"]["pool"] = {
        "members": members,
        "rate_limit": {
            "max_attempts_per_request": 2, "retry_interval_seconds": 1,
            "first_fast_retry": True, "backend_timeout_seconds": 120,
            "circuit_breaker": {
                "failure_count": 1, "interval_seconds": 60, "trip_duration_seconds": 60,
                "accept_retry_after": True,
                "status_code_ranges": [
                    {"minimum": 429, "maximum": 429},
                    {"minimum": 408, "maximum": 408},
                    {"minimum": 500, "maximum": 599},
                ],
                "error_reasons": ["BackendConnectionFailure", "Timeout"],
            },
        },
        "session_cookie_name": None, "credential_versions": {},
        "header_values": {"unit-header-" + str(index): "value" for index in range(header_count)},
    }
    if fault in {"configuration-sha", "invalid-configuration-sha"}:
        document["connection"]["pool"]["configuration_sha256"] = (
            "a" * 64 if fault == "configuration-sha" else "invalid"
        )
    content = canonical_document(document)
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(storage_routes.READ_FIXED_CONTENT), "body": "{}",
        "operation": "chat-completions",
        "variables": {
            "routeCachedContent": content,
            "routeScope": json.dumps(document["scope"]),
            "routeBudgetEpoch": document["budget_epoch"],
            "routePoolEnabled": fault != "pool-disabled",
            "routeHead": json.dumps({"approval": {"required_profiles": [document["profile"]]}}),
            "routeMember": json.dumps({
                "model_id": document["model_id"], "alias": document["alias"],
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "connection_approval": {"connection": document["connection"]},
            }),
        },
    }])[0]
    if accepted:
        assert json.loads(result) == document
    else:
        assert result == ""


def stored_response(document: dict[str, Any], etag: str = 'W/"unit"') -> dict[str, Any]:
    return {
        "__response": True,
        "status": 200,
        "headers": {"ETag": etag},
        "body": json.dumps({"Document": json.dumps(document)}),
    }


@pytest.mark.parametrize("actual", [0, 100, 1200])
def test_csharp_reservation_and_completion_match_domain_math(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    actual: int,
) -> None:
    initial = summary()
    expected, attempt = reserve_ledger(initial, identity(), 500, NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    base = {
        "body": "{}",
        "variables": {
            "executionSummaryRead": stored_response(initial.model_dump(mode="json")),
            "executionReceipt": receipt.model_dump(mode="json"),
        },
    }
    reserved = run_cases(
        compiled_expressions,
        tmp_path,
        [
            {
                **base,
                "expression": expression_id(compiler.RESERVATION),
            }
        ],
    )[0]
    assert reserved["error"] == ""
    for field in ("confirmed_tokens", "active_reserved_tokens", "upper_bound_tokens", "version"):
        assert reserved["summary"][field] == getattr(expected, field)
    expected_settled, expected_attempt = complete_ledger(
        expected,
        attempt,
        receipt,
        completion(actual),
        NOW,
    )
    case: dict[str, Any] = {
        "body": "{}",
        "expression": expression_id(compiler.COMPLETION),
        "variables": {
            "executionSummaryRead": stored_response(expected.model_dump(mode="json")),
            "executionAttemptRead": stored_response(attempt.model_dump(mode="json")),
            "executionReceipt": receipt.model_dump(mode="json"),
            "usagePayload": {
                "input_tokens": actual,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "tokens_consumed": actual,
                "usage_complete": True,
                "estimated": False,
                "ingest_error": None,
            },
        },
    }
    result = run_cases(compiled_expressions, tmp_path, [case])[0]
    assert result["error"] == "" and result["already_settled"] is False
    for field in ("confirmed_tokens", "active_reserved_tokens", "upper_bound_tokens", "version"):
        assert result["summary"][field] == getattr(expected_settled, field)
    assert result["attempt"]["actual_tokens"] == expected_attempt.actual_tokens
    case["variables"]["executionSummaryRead"] = stored_response(result["summary"])
    case["variables"]["executionAttemptRead"] = stored_response(result["attempt"])
    assert run_cases(compiled_expressions, tmp_path, [case])[0]["already_settled"] is True


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([204, 204], "ok"),
        ([204], "unavailable"),
        ([204, 412], "conflict"),
        ([409], "conflict"),
        ([204, 500], "unavailable"),
        ([], "unavailable"),
    ],
)
def test_batch_requires_both_successful_financial_writes(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
    statuses: list[int],
    expected: str,
) -> None:
    result = run_cases(
        compiled_expressions,
        tmp_path,
        [
            {
                "body": "{}",
                "expression": expression_id(compiler.BATCH_RESULT),
                "variables": {
                    "executionWrite": {
                        "__response": True,
                        "status": 202,
                        "headers": {"Content-Type": "multipart/mixed; boundary=unit"},
                        "body": "\r\n".join(f"HTTP/1.1 {status} Result" for status in statuses),
                    }
                },
            }
        ],
    )[0]
    assert result == expected


def test_native_counter_keeps_authentication_and_exact_plan_apply() -> None:
    root = ET.Element("inbound")
    compiler._native_count(root)
    plans = root.findall(".//set-variable[@name='executionPlan']")
    assert len(plans) == 1 and plans[0].get("value") == compiler.NATIVE_COUNT_APPLY
    requests = root.findall(".//send-request")
    assert len(requests) == 5
    assert all(request.findtext("set-method") == "POST" for request in requests)
    assert all(request.get("timeout") == "2" for request in requests)
    assert root.find(".//authentication-managed-identity") is not None
    for request in requests:
        for header in request.findall("set-header"):
            if header.get("name") in {"api-key", "x-api-key"}:
                assert "Bearer" not in (header.findtext("value") or "")


def test_analysis_precedes_every_reservation_and_dispatch_authorization() -> None:
    root = compiler._admission()
    nodes = list(root.iter())
    analysis = root.find(".//set-variable[@name='executionRequestAnalysis']")
    plans = root.findall(".//set-variable[@name='executionPlan']")
    assert analysis is not None and len(plans) == 2
    assert analysis.get("value") == compiler.REQUEST_ANALYSIS
    assert plans[0].get("value") == compiler.PLAN
    assert plans[1].get("value") == compiler.NATIVE_COUNT_APPLY
    assert nodes.index(analysis) < nodes.index(plans[0]) < nodes.index(plans[1])
    writes = root.findall(".//send-request")
    assert all(nodes.index(request) > nodes.index(plans[0]) for request in writes)
    authorization = root.find(
        ".//send-request[@response-variable-name='executionDispatchAuthorized']"
    )
    header = root.find(".//set-variable[@name='executionReceiptHeader']")
    assert authorization is not None and header is not None
    assert nodes.index(authorization) < nodes.index(header)
    assert authorization.find("set-header[@name='If-Match']") is not None
    assert len(root.findall(".//set-variable[@name='executionReservation']")) == 2


def test_execution_composition_preserves_public_identity_limits_and_routing() -> None:
    template = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    assert compiler.compose_budget_execution_parent(template, template) == template
    original = parse_policy(template)
    candidate = parse_policy(
        compiler.compose_budget_execution_parent(template, template, enabled=True)
    )
    assert (
        len(candidate.findall(".//llm-token-limit"))
        == len(original.findall(".//llm-token-limit"))
        == 2
    )
    for before, after in zip(
        original.findall(".//llm-token-limit"), candidate.findall(".//llm-token-limit"), strict=True
    ):
        assert after.attrib == before.attrib
    assert [ET.tostring(node) for node in candidate.findall(".//validate-azure-ad-token")] == [
        ET.tostring(node) for node in original.findall(".//validate-azure-ad-token")
    ]
    assert candidate.find(".//set-variable[@name='executionRequestAnalysis']") is not None
    enforcement = candidate.find(".//set-variable[@name='ledgerEnforced']")
    event = candidate.find("./outbound//log-to-eventhub")
    header = candidate.find("./inbound/set-header[@name='x-turnstile-budget-execution']")
    assert enforcement is not None and enforcement.get("value") == "@(false)"
    assert event is not None and event.text and "budget_execution" in event.text
    assert header is not None and header.get("exists-action") == "delete"
    with pytest.raises(PolicyCompilationError):
        compiler.compose_budget_execution_parent(
            template.replace('header-name="Authorization"', 'header-name="untrusted"'),
            template,
            enabled=True,
        )


def test_execution_composition_settles_image_responses() -> None:
    from turnstile_core.integrations.apim_policy_components import (
        _image_usage,
        serialize_policy,
        validate_parent_policy,
    )

    template = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    composed = compiler.compose_budget_execution_parent(template, template, enabled=True)
    root = parse_policy(composed)
    image, text = (root.find(
        f"./outbound/choose/otherwise/choose/{branch}/set-variable[@name='usagePayload']",
    ) for branch in ("when", "otherwise"))
    assert image is not None and image.get("value") == compiler.COMPLETE_IMAGE_USAGE
    assert text is not None and text.get("value") == compiler.COMPLETE_USAGE
    image.set("value", _image_usage().attrib["value"])
    validate_parent_policy(serialize_policy(root), composed)


def image_response(change: dict[str, int]) -> str:
    cached = change.get("cached")
    usage = {
        "input_tokens": 20, "output_tokens": 229, "total_tokens": 249 + change.get("total", 0),
        "input_tokens_details": {
            "text_tokens": 20 - change.get("input_image", 0),
            "image_tokens": change.get("input_image", 0),
            **({"cached_tokens": cached} if cached is not None else {}),
        },
        "output_tokens_details": {
            "text_tokens": change.get("output_text", 0),
            "image_tokens": 229 - change.get("output_text", 0),
        },
    }
    return json.dumps({"created": 1, "data": [{"b64_json": "A" * 2_000_000}], "usage": usage})


@pytest.mark.parametrize("change,complete", [
    ({}, True), ({"cached": 5}, True), ({"input_image": 3}, False),
    ({"output_text": 4}, False), ({"total": 1}, False),
])
def test_image_completion_usage_settles_only_exact_text_to_image_usage(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    change: dict[str, int], complete: bool,
) -> None:
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(compiler.COMPLETE_IMAGE_USAGE), "body": "{}",
        "operation": "images-generations", "response": image_response(change), "variables": {},
    }])[0]
    assert result["usage_complete"] is complete
    if complete:
        cached = change.get("cached", 0)
        assert (result["input_tokens"], result["cached_tokens"], result["cache_write_tokens"],
                result["output_tokens"], result["tokens_consumed"]) == (
            20 - cached, cached, 0, 229, 249,
        )
    else:
        assert result["ingest_error"] == "usage_parse_failed"


@pytest.mark.parametrize("operation", ["responses", "responses-compact", "chat-completions"])
@pytest.mark.parametrize("reader", ["template", "fixed_program"])
@pytest.mark.parametrize("details,expected", [
    ({"cache_write_tokens": 2115, "cached_tokens": 0}, (3, 2115, 2115, 5, 2123)),
    ({"cache_write_tokens": 0, "cached_tokens": 2115}, (3, 2115, 0, 5, 2123)),
    ({"cache_write_tokens": 1115, "cached_tokens": 1000}, (3, 2115, 1115, 5, 2123)),
    ({}, (2118, 0, 0, 5, 2123)),
])
def test_responses_completion_preserves_cache_buckets(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    operation: str, reader: str, details: dict[str, int], expected: tuple[int, int, int, int, int],
) -> None:
    chat = operation == "chat-completions"
    response = json.dumps({"status": "completed", "usage": {
        "prompt_tokens" if chat else "input_tokens": 2118,
        "prompt_tokens_details" if chat else "input_tokens_details": details,
        "completion_tokens" if chat else "output_tokens": 5, "total_tokens": 2123,
    }})
    model_id = UUID(int=4)
    projection = BudgetPriceProjection.create(model_id, ModelPrice(
        input_price_per_million=10, cached_price_per_million=1,
        cache_write_price_per_million=20, output_price_per_million=30,
    ), NOW)
    receipt = LedgerAdmissionReceipt(
        identity=identity(model_id=str(model_id)), epoch=EPOCH, reserved_tokens=3000,
        budget_mode="usd", reserved_usd=Decimal("1"), price_snapshot=projection,
    )
    expression = compiler.COMPLETE_USAGE if reader == "fixed_program" else text_usage_expression()
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(expression), "body": "{}",
        "operation": operation, "response": response,
        "variables": {
            "isResponsesOperation": not chat,
            "executionDispatch": {"receipts": [receipt.model_dump(mode="json")]},
            "executionPlan": {"model_id": str(model_id)},
        },
    }])[0]
    assert result["estimated"] is False
    assert tuple(result[field] for field in (
        "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens",
    )) == expected[:4]
    assert result["input_tokens"] + result["cached_tokens"] + result["output_tokens"] == 2123
    if reader == "fixed_program":
        assert result["usage_complete"] is True and result["tokens_consumed"] == expected[4]
        expected_usd = (Decimal(expected[0]) * Decimal("10")
                        + Decimal(expected[1] - expected[2]) * Decimal("1")
                        + Decimal(expected[2]) * Decimal("20")
                        + Decimal(expected[3]) * Decimal("30")) / Decimal(1_000_000)
        assert Decimal(result["usd_cost"]) == expected_usd
        assert result["budget_price_projection"] == projection.model_dump(mode="json")


@pytest.mark.parametrize("details", [
    {"cached_tokens": 2115, "cache_write_tokens": 2115},
    {"cached_tokens": 0, "cache_write_tokens": 2119},
    {"cached_tokens": 0, "cache_write_tokens": -1},
    {"cached_tokens": 0, "cache_write_tokens": "2115"},
    {"cached_tokens": 0, "cache_write_tokens": None},
])
def test_responses_completion_rejects_inconsistent_cache_buckets(
    compiled_expressions: tuple[Path, str], tmp_path: Path, details: dict[str, Any],
) -> None:
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(compiler.COMPLETE_USAGE), "body": "{}",
        "operation": "responses", "response": json.dumps({"status": "completed", "usage": {
            "input_tokens": 2118, "input_tokens_details": details,
            "output_tokens": 5, "total_tokens": 2123,
        }}), "variables": {},
    }])[0]
    assert result["usage_complete"] is False and result["estimated"] is True
    assert result["input_tokens"] is None and result["cached_tokens"] is None


@pytest.mark.parametrize("scope_type", ["person", "application"])
def test_responses_cache_usage_settles_original_price_once(
    compiled_expressions: tuple[Path, str], tmp_path: Path, scope_type: str,
) -> None:
    scope = LedgerScope.model_validate({
        "scope_type": scope_type,
        "scope_id": "unit-cache-budget" if scope_type == "person" else str(UUID(int=8)),
        "period_start": identity().scope.period_start,
    })
    current = summary(
        limit=0, scope=scope, budget_mode="usd", usd_limit=Decimal("5"),
        confirmed_usd=Decimal("0.004196000"),
    )
    model_id = UUID(int=4)
    projection = BudgetPriceProjection.create(model_id, ModelPrice(
        input_price_per_million=1.25, cached_price_per_million=0.125,
        cache_write_price_per_million=0.125, output_price_per_million=10,
    ), NOW)
    expected = (Decimal(3) * Decimal("1.25") + Decimal(2115) * Decimal("0.125")
                + Decimal(5) * Decimal("10")) / Decimal(1_000_000)
    assert expected == Decimal("0.000318125")

    for streamed in (False, True):
        request = identity(f"cache-{scope_type}-{streamed}", scope=scope, model_id=str(model_id))
        current, attempt = reserve_ledger(
            current, request, 3000, current.updated_at,
            reserved_usd=Decimal("0.01"), price_snapshot=projection,
        )
        receipt = LedgerAdmissionReceipt(
            identity=request, epoch=EPOCH, reserved_tokens=3000, budget_mode="usd",
            reserved_usd=Decimal("0.01"), price_snapshot=projection,
        )
        native = {"input_tokens": 2118, "output_tokens": 5, "total_tokens": 2123,
                  "input_tokens_details": {
                      "cached_tokens": 2115 if streamed else 0,
                      "cache_write_tokens": 0 if streamed else 2115,
                  }}
        response = json.dumps({"status": "completed", "usage": native})
        if streamed:
            response = "event: response.completed\ndata: " + json.dumps({
                "type": "response.completed", "response": {"status": "completed", "usage": native},
            }) + "\n\n"
        variables = {
            "executionDispatch": {"receipts": [receipt.model_dump(mode="json")]},
            "executionPlan": {"model_id": str(model_id)},
        }
        usage = run_cases(compiled_expressions, tmp_path, [{
            "expression": expression_id(compiler.COMPLETE_USAGE), "body": "{}",
            "operation": "responses", "response": response, "variables": variables,
        }])[0]
        if streamed:
            assert usage["usage_complete"] is False
        else:
            assert usage["usage_complete"] is True
            assert Decimal(usage["usd_cost"]) == expected
            completed = run_cases(compiled_expressions, tmp_path, [{
                "expression": expression_id(compiler.COMPLETION), "body": "{}",
                "operation": "responses", "variables": {
                    "usagePayload": usage, "executionReceipt": receipt.model_dump(mode="json"),
                    "executionSummaryRead": stored_response(current.model_dump(mode="json")),
                    "executionAttemptRead": stored_response(attempt.model_dump(mode="json")),
                },
            }])[0]
            assert completed["error"] == "" and completed["already_settled"] is False
            current = LedgerSummary.model_validate(completed["summary"])
            attempt = LedgerAttempt.model_validate(completed["attempt"])
            assert attempt.actual_usd == expected
            assert attempt.completion_evidence is not None

        observed = responses_observer_event(receipt, native, streamed)
        assert observed["usage_complete"] is True
        assert (observed["input_tokens"], observed["cached_tokens"], observed["output_tokens"]) == (
            3, 2115, 5,
        )
        assert observed["cache_write_tokens"] == (0 if streamed else 2115)
        formal = LedgerCharge(
            identity=request, epoch=EPOCH, revision=1, evidence_rank=3,
            evidence_key="observer:" + observed["id"], total_tokens=2123,
            budget_mode="usd", actual_usd=expected, price_snapshot=projection,
        )
        previous = current
        current, attempt = settle_ledger(current, attempt, formal, current.updated_at)
        if not streamed:
            assert current is previous
        duplicate, duplicate_attempt = settle_ledger(current, attempt, formal, current.updated_at)
        assert duplicate is current and duplicate_attempt is attempt
        assert attempt.actual_usd == expected and attempt.actual_tokens == 2123
        assert current.active_reserved_usd == 0
        with pytest.raises(LedgerProtocolConflict, match="conflicting amount"):
            settle_ledger(current, attempt, formal.model_copy(update={
                "actual_usd": expected + Decimal("0.00000000000001"),
            }), current.updated_at)
        with pytest.raises(LedgerProtocolConflict, match="admitted price snapshot"):
            settle_ledger(current, attempt, formal.model_copy(update={
                "price_snapshot": projection.model_copy(update={"price_sha256": "f" * 64}),
            }), current.updated_at)

    assert current.confirmed_usd == Decimal("0.004832250")
    assert current.confirmed_usd - Decimal("0.004196000") == Decimal("0.000636250")


@pytest.mark.parametrize("case_name,cache_read,cache_write", [
    ("json_write", 0, 2115), ("json_read", 2115, 0), ("json_uncached", 0, 0),
    ("sse_write", 0, 2115), ("sse_read", 2115, 0), ("sse_uncached", 0, 0),
    ("old_json", 0, 2115),
])
def test_responses_formal_tasks_apply_once_without_hiding_conflicts(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
    pricing_postgres: tuple[str, Event], case_name: str, cache_read: int, cache_write: int,
) -> None:
    from unittest.mock import Mock
    from uuid import uuid4

    from turnstile_core.domain.gateway_targets import gateway_deployment_id_for
    from turnstile_core.ingestion.processor import CoefficientResolver, UsageProcessor
    from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy
    from turnstile_core.services.budget_settlement import BudgetSettlementService

    scope = identity().scope.model_copy(update={"scope_id": "cache-" + uuid4().hex})
    request = identity("formal-" + uuid4().hex, scope=scope, model_id=str(UUID(int=4)))
    projection = BudgetPriceProjection.create(UUID(int=4), ModelPrice(
        input_price_per_million=1.25, cached_price_per_million=0.125,
        cache_write_price_per_million=0.125, output_price_per_million=10,
    ), NOW)
    current, attempt = reserve_ledger(
        summary(limit=0, scope=scope, budget_mode="usd", usd_limit=Decimal("5")),
        request, 3000, NOW, reserved_usd=Decimal("0.01"), price_snapshot=projection,
    )
    receipt = LedgerAdmissionReceipt(
        identity=request, epoch=EPOCH, reserved_tokens=3000, budget_mode="usd",
        reserved_usd=Decimal("0.01"), price_snapshot=projection,
    )
    streamed = case_name.startswith("sse")
    native = {"input_tokens": 2118, "output_tokens": 5, "total_tokens": 2123,
              "input_tokens_details": {
                  "cached_tokens": cache_read, "cache_write_tokens": cache_write,
              }}
    if not streamed:
        usage = run_cases(compiled_expressions, tmp_path, [{
            "expression": expression_id(compiler.COMPLETE_USAGE), "body": "{}",
            "operation": "responses",
            "response": json.dumps({"status": "completed", "usage": native}),
            "variables": {},
        }])[0]
        if case_name == "old_json":
            usage.update(input_tokens=2118, cached_tokens=0, cache_write_tokens=0)
        current, attempt = complete_ledger(current, attempt, receipt, completion(
            2123, input_tokens=usage["input_tokens"], cached_tokens=usage["cached_tokens"],
            cache_write_tokens=usage["cache_write_tokens"], output_tokens=usage["output_tokens"],
        ), NOW)
    before = current
    observed = responses_observer_event(receipt, native, streamed)
    expected = (Decimal(2118 - cache_read - cache_write) * Decimal("1.25")
                + Decimal(cache_read + cache_write) * Decimal("0.125")
                + Decimal(5) * Decimal("10")) / Decimal(1_000_000)
    info, _waiting = pricing_postgres
    repository = PostgreSqlOpsDbProxy(info)
    try:
        with repository._connection() as connection:
            connection.execute(
                "INSERT INTO gateway_profile(id,name,implementation) VALUES(%s,%s,'apim') "
                "ON CONFLICT DO NOTHING", (request.gateway_profile_id, "Unit cache accounting"),
            )
            connection.execute(
                """INSERT INTO apim_target_metadata (
                   gateway_profile_id, arm_resource_id, tenant_id, subscription_id, resource_group,
                   service_name, api_id, api_path, service_tier, tier_family, service_location,
                   ownership_mode
                   ) VALUES (%s,%s,%s,%s,'unit','unit-service','unit','unit','Developer',
                             'classic','unit','turnstile_managed') ON CONFLICT DO NOTHING""",
                (
                    request.gateway_profile_id,
                    "/subscriptions/" + str(EPOCH) + "/resourceGroups/unit/"
                    "providers/Microsoft.ApiManagement/service/unit-service", EPOCH, EPOCH,
                ),
            )
            connection.execute(
                """INSERT INTO gateway_deployment (
                   id,apim_target_id,native_gateway_id,kind,display_name,hosting_mode,lifecycle_ownership
                   ) VALUES (%s,%s,'managed','managed','Unit','azure_managed','turnstile_iac')
                   ON CONFLICT DO NOTHING""",
                (
                    gateway_deployment_id_for(request.gateway_profile_id, "managed"),
                    request.gateway_profile_id,
                ),
            )
            connection.execute(
                """INSERT INTO budget_execution_scope (
                   scope_type,scope_id,period_start,epoch,state,admitted_after,legacy_fence_verified_at
                   ) VALUES ('person',%s,%s,%s,'active',%s,%s)""",
                (scope.scope_id, scope.period_start, EPOCH, NOW, NOW),
            )
        processor = UsageProcessor(repository, CoefficientResolver({}), execution_enabled=True)
        assert processor.process(observed) is True
        with repository._connection() as connection:
            queued = connection.execute(
                "SELECT * FROM budget_settlement_task WHERE scope_id=%s", (scope.scope_id,),
            ).fetchone()
        assert queued is not None
        assert queued["actual_usd"] == expected
        assert queued["price_snapshot"] == projection.model_dump(mode="json")
        assert (
            queued["evidence_rank"], queued["revision"], queued["applied_revision"],
        ) == (3, 1, 0)

        def apply(charge: LedgerCharge) -> tuple[LedgerSummary, LedgerAttempt]:
            nonlocal current, attempt
            current, attempt = settle_ledger(current, attempt, charge, datetime.now(UTC))
            return current, attempt

        ledger = Mock()
        ledger.settle.side_effect = apply
        service = BudgetSettlementService(repository, ledger)
        assert service.drain(observed["id"], limit=1) == int(case_name != "old_json")
        with repository._connection() as connection:
            applied = connection.execute(
                "SELECT * FROM budget_settlement_task WHERE id=%s", (queued["id"],),
            ).fetchone()
        assert applied is not None and applied["attempts"] == 1
        if case_name == "old_json":
            assert current is before and current.confirmed_usd == Decimal("0.002697500")
            assert applied["applied_revision"] == 0 and applied["applied_at"] is None
            assert applied["last_error"] == "LedgerProtocolConflict"
        else:
            assert applied["applied_revision"] == 1 and applied["applied_at"] is not None
            assert applied["last_error"] is None
            assert current.confirmed_usd == expected and current.active_reserved_usd == 0
            assert processor.process(observed) is True
            assert service.drain(observed["id"], limit=1) == 0
            with repository._connection() as connection:
                repeated = connection.execute(
                    "SELECT revision,applied_revision,attempts FROM budget_settlement_task "
                    "WHERE id=%s",
                    (queued["id"],),
                ).fetchone()
            assert repeated is not None
            assert (
                repeated["revision"], repeated["applied_revision"], repeated["attempts"],
            ) == (1, 1, 1)
            duplicate, duplicate_attempt = settle_ledger(
                current, attempt, ledger.settle.call_args.args[0], datetime.now(UTC),
            )
            assert duplicate is current and duplicate_attempt is attempt
            assert current.confirmed_usd == expected
    finally:
        repository.close()


def test_image_response_settles_its_ledger_reservation(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
) -> None:
    initial = summary()
    reserved, attempt = reserve_ledger(initial, identity(), 500, NOW)
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=500)
    expected, expected_attempt = complete_ledger(reserved, attempt, receipt, completion(249), NOW)
    result = run_cases(compiled_expressions, tmp_path, [{
        "expression": expression_id(compiler.COMPLETION), "body": "{}",
        "operation": "images-generations", "response": image_response({}),
        "variables": {
            "executionSummaryRead": stored_response(reserved.model_dump(mode="json")),
            "executionAttemptRead": stored_response(attempt.model_dump(mode="json")),
            "executionReceipt": receipt.model_dump(mode="json"),
        },
        "steps": [{"variable": "usagePayload",
                   "expression": expression_id(compiler.COMPLETE_IMAGE_USAGE)}],
    }])[0]
    assert result["error"] == "" and result["already_settled"] is False
    assert result["attempt"]["actual_tokens"] == expected_attempt.actual_tokens == 249
    for field in ("confirmed_tokens", "active_reserved_tokens", "upper_bound_tokens"):
        assert result["summary"][field] == getattr(expected, field)


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "not_fenced",
        "active",
        "upper",
        "wrong_balance",
        "stale",
        "duplicate",
        "wrong_epoch",
    ],
)
def test_cutover_requires_fresh_drained_unchanged_balances(defect: str | None) -> None:
    state = summary(confirmed_tokens=400)
    scope = {
        "summary": state.model_dump(mode="json"),
        "legacy_active_reservations": int(defect == "active"),
        "legacy_upper_bound_rows": int(defect == "upper"),
        "legacy_confirmed_tokens": 401 if defect == "wrong_balance" else 400,
        "legacy_snapshot_sha256": "a" * 64,
        "observed_at": NOW,
    }
    readback = CutoverReadback.model_validate(
        {
            "observed_at": NOW - timedelta(seconds=31) if defect == "stale" else NOW,
            "old_admission_fenced": defect != "not_fenced",
            "old_inflight_drained": True,
            "all_gateway_revisions_verified": True,
            "compatible_readers_verified": True,
            "scopes": [scope, scope] if defect == "duplicate" else [scope],
        }
    )
    if defect is None:
        assert readback.summaries(EPOCH, NOW) == [state]
    else:
        with pytest.raises(ValueError):
            readback.summaries(
                identity().gateway_profile_id if defect == "wrong_epoch" else EPOCH, NOW
            )


def test_offline_candidate_uses_public_template_without_enabling_it() -> None:
    template = (ROOT / "infra/policies/foundry-finops-policy.xml").read_text()
    rendered = parse_policy(render_candidate(template))
    assert rendered.find(".//set-variable[@name='executionRequestAnalysis']") is not None
    assert rendered.find(".//set-variable[@name='budgetExecutionFence']") is None


def test_dispatch_lease_uses_fresh_admission_timestamp_after_slow_request_upload(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
) -> None:
    from turnstile_core.domain.ledger import LedgerDispatch

    admission = compiler._admission()
    dispatch_node = admission.find(".//set-variable[@name='executionDispatch']")
    etag_node = admission.find(".//set-variable[@name='executionDispatchEtag']")
    assert dispatch_node is not None and etag_node is not None
    case = planned_case({"messages": [{"role": "user", "content": "unit"}], "max_tokens": 32})
    case["variables"]["requestStarted"] = (
        datetime.now(UTC) - timedelta(seconds=900)
    ).isoformat()
    case["variables"]["callerRequestId"] = "caller-slow-upload"
    case["steps"].append({"expression": expression_id(compiler.PLAN), "variable": "executionPlan"})
    case["expression"] = expression_id(dispatch_node.attrib["value"])
    dispatch_raw = run_cases(compiled_expressions, tmp_path, [case])[0]
    validated = LedgerDispatch.model_validate(dispatch_raw)
    assert validated.expires_at - validated.receipts[0].identity.admitted_at == timedelta(
        seconds=45
    )
    etag_case = {
        "body": "{}",
        "expression": expression_id(etag_node.attrib["value"]),
        "variables": {
            "executionDispatchInitial": dispatch_raw,
            "executionDispatchRead": stored_response(dispatch_raw, etag='W/"fresh-dispatch"'),
        },
    }
    assert run_cases(compiled_expressions, tmp_path, [etag_case])[0] == 'W/"fresh-dispatch"'


def test_multimodal_reservation_bounds_base64_and_url_without_byte_inflation(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
) -> None:
    uncompressed_data_url = "data:image/png;base64," + ("A" * 1_049_696)
    compressed_data_url = "data:image/png;base64," + ("B" * 1_988)
    remote_url = "https://cdn.example/image.png"

    def make_chat_body(url: str) -> dict[str, Any]:
        return {
            "model": "unit-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
        }

    cases = []
    for url in (uncompressed_data_url, compressed_data_url, remote_url):
        case = planned_case(make_chat_body(url), operation="chat-completions", legacy=True)
        Fence = json.loads(case["variables"]["budgetExecutionFence"])
        Fence["model_contracts"]["unit-model"]["output_field"] = "max_tokens"
        case["variables"]["budgetExecutionFence"] = json.dumps(Fence)
        cases.append(case)

    for url in (uncompressed_data_url, compressed_data_url, remote_url):
        case = planned_case(make_chat_body(url), operation="chat-completions", legacy=False)
        Fence = json.loads(case["variables"]["budgetExecutionFence"])
        Fence["model_contracts"]["unit-model"]["multimodal_item_ceiling"] = 1700
        case["variables"]["budgetExecutionFence"] = json.dumps(Fence)
        cases.append(case)

    results = run_cases(compiled_expressions, tmp_path, cases)
    legacy_uncompressed, legacy_compressed, legacy_url = results[:3]
    verified_uncompressed, verified_compressed, verified_url = results[3:]

    # 1. Both uncompressed (~1.05MB) and compressed (~2KB) inline Base64 images produce
    # identical non-media byte estimates and identical total reservations (~8,357 tokens),
    # well within the 862,562 available balance from incident ecce1223-4255-458e-b21d-ec6d5154ec7e.
    assert legacy_uncompressed["error"] == ""
    assert legacy_compressed["error"] == ""
    assert legacy_uncompressed["body_bytes_estimate"] > 1_000_000
    assert legacy_compressed["body_bytes_estimate"] < 3_000
    assert (
        legacy_uncompressed["non_media_body_bytes_estimate"]
        == legacy_compressed["non_media_body_bytes_estimate"]
    )
    assert legacy_uncompressed["media_reserve_tokens"] == 4096
    assert (
        legacy_uncompressed["receipts"][0]["reserved_tokens"]
        == legacy_compressed["receipts"][0]["reserved_tokens"]
    )
    assert legacy_uncompressed["receipts"][0]["reserved_tokens"] < 10_000
    assert legacy_uncompressed["inline_media_count"] == 1
    assert legacy_uncompressed["remote_media_count"] == 0
    assert legacy_uncompressed["media_sources"] == ["data_url"]

    # 2. Switching to a short URL still reserves media_reserve_tokens (4,096) and cannot
    # bypass multimodal reservation with a 29-byte URL string.
    assert legacy_url["error"] == ""
    assert legacy_url["inline_media_count"] == 0
    assert legacy_url["remote_media_count"] == 1
    assert legacy_url["media_sources"] == ["url"]
    assert legacy_url["media_reserve_tokens"] == 4096
    assert (
        legacy_url["receipts"][0]["reserved_tokens"]
        == legacy_uncompressed["receipts"][0]["reserved_tokens"]
    )

    # 3. Verified context_input_ceiling_v1 with multimodal_item_ceiling = 1700 bounds all three
    # to non_media_body_bytes_estimate + 1700 + 4096 with verified_upper_bound assurance.
    for verified in (verified_uncompressed, verified_compressed, verified_url):
        assert verified["error"] == ""
        assert verified["count_assurance"] == "verified_upper_bound"
        assert verified["media_reserve_tokens"] == 1700
        assert (
            verified["receipts"][0]["reserved_tokens"]
            == verified["non_media_body_bytes_estimate"] + 1700 + 4096
        )


def test_hard_budget_rejects_legacy_estimate_and_denial_includes_media_diagnostics(
    compiled_expressions: tuple[Path, str],
    tmp_path: Path,
) -> None:
    from turnstile_core.domain.ledger import LedgerAdmissionDenied

    # 1. Python LedgerSummary with require_verified_bound=True rejects estimate assurance
    hard_summary = summary().model_copy(update={"require_verified_bound": True})
    with pytest.raises(LedgerAdmissionDenied) as exc_info:
        reserve_ledger(hard_summary, identity(), 500, NOW, count_assurance="estimate")
    assert exc_info.value.code == "budget_count_bound_unavailable"

    # 2. C# RESERVATION with require_verified_bound=True rejects estimate assurance
    receipt = LedgerAdmissionReceipt(identity=identity(), epoch=EPOCH, reserved_tokens=8357)
    res_case = {
        "body": "{}",
        "expression": expression_id(compiler.RESERVATION),
        "variables": {
            "executionSummaryRead": stored_response(
                {**hard_summary.model_dump(mode="json"), "require_verified_bound": True}
            ),
            "executionReceipt": receipt.model_dump(mode="json"),
            "executionPlan": {"count_assurance": "estimate"},
        },
    }
    res_result = run_cases(compiled_expressions, tmp_path, [res_case])[0]
    assert res_result["error"] == "budget_count_bound_unavailable"

    # 3. C# executionDecision on denial includes reservation breakdown without leaking base64 data
    secret_payload = "SECRET_IMAGE_PAYLOAD_" + ("Z" * 512)
    chat_body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + secret_payload},
                    }
                ],
            }
        ],
        "max_tokens": 2048,
    }
    plan_case = planned_case(chat_body, operation="chat-completions", legacy=True)
    fence = json.loads(plan_case["variables"]["budgetExecutionFence"])
    fence["model_contracts"]["unit-model"]["output_field"] = "max_tokens"
    plan_case["variables"]["budgetExecutionFence"] = json.dumps(fence)
    plan_result = run_cases(compiled_expressions, tmp_path, [plan_case])[0]

    decision_node = compiler._admission().find(".//set-variable[@name='executionDecision']")
    assert decision_node is not None
    tight_summary = summary(
        token_limit=1_000_000, confirmed_tokens=995_000, upper_bound_tokens=0
    )
    decision_case = {
        "body": json.dumps(chat_body),
        "operation": "chat-completions",
        "expression": expression_id(decision_node.attrib["value"]),
        "variables": {
            "executionError": "budget_request_bound_exceeds_balance",
            "executionPlan": plan_result,
            "executionReceipt": plan_result["receipts"][0],
            "executionReservation": {"snapshot": tight_summary.model_dump(mode="json")},
        },
    }
    decision = run_cases(compiled_expressions, tmp_path, [decision_case])[0]
    assert decision["code"] == "budget_request_bound_exceeds_balance"
    assert decision["count_assurance"] == "estimate"
    assert decision["inline_media_count"] == 1
    assert decision["inline_media_bytes"] == len("data:image/png;base64," + secret_payload)
    assert decision["media_sources"] == ["data_url"]
    assert decision["media_reserve_tokens"] == 4096
    assert decision["output_reserve_tokens"] == 2048
    assert (
        decision["request_bound"]
        == decision["input_reserve_tokens"] + decision["output_reserve_tokens"]
    )
    assert secret_payload not in json.dumps(decision)
    assert secret_payload not in json.dumps(plan_result)

