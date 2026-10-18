from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from scripts.prepare_budget_execution import CutoverReadback, render_candidate
from tests.backend.telemetry.test_atomic_ledger import EPOCH, NOW, completion, identity, summary
from turnstile_core.domain.budget_delegation import BudgetDelegationTrust
from turnstile_core.domain.ledger import LedgerAdmissionReceipt, complete_ledger, reserve_ledger
from turnstile_core.integrations import apim_budget_execution as compiler
from turnstile_core.integrations import apim_model_availability as availability
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
}
sealed class Operation { public string Id; }
sealed class Subscription { public string Id; }
sealed class Api { public string Id = "unit-turnstile"; public string Revision = "candidate"; }
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
        Response = new Response(new JObject(new JProperty("status", 200),
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
    expressions.update(value for name, value in vars(availability).items()
                       if name.isupper() and isinstance(value, str) and value.startswith("@"))
    assembled = ET.Element("inbound")
    compiler._native_count(assembled)
    compiler._deny(assembled, "@(true)", "budget_request_invalid", cancel_preparing=True)
    assembled.append(compiler._admission())
    assembled.append(compiler._completion())
    assembled.append(compiler.budget_execution_fence(["unit-turnstile"]))
    delegated = parse_policy(compiler.compose_budget_execution_parent(
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(),
        (ROOT / "infra/policies/foundry-finops-policy.xml").read_text(), enabled=True,
        delegation=BudgetDelegationTrust(
            tenant_id=EPOCH, audience="api://unit", client_ids=(EPOCH,), object_ids=(EPOCH,),
        ),
    ))
    for node in delegated.findall(".//set-header"):
        value = node.findtext("value", "")
        if "Uri.UnescapeDataString" in value:
            expressions.add(value)
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
    {"IssuedEpoch": "999999999999"}, {"Lifetime": 180}, {"ETag": "*"},
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


@pytest.mark.parametrize("samples", [1, 3])
def test_whole_request_bound_reaches_usd_reservation_without_rewriting_output(
    compiled_expressions: tuple[Path, str], tmp_path: Path, samples: int,
) -> None:
    case = planned_case({
        "model": "unit-model", "messages": [{"role": "user", "content": "hello"}],
        "n": samples, "max_completion_tokens": 17,
    }, operation="chat-completions")
    fence = json.loads(case["variables"]["budgetExecutionFence"])
    contract = fence["model_contracts"]["unit-model"]
    contract.update(algorithm="request_total_ceiling_v1", output_field="none",
                    request_token_ceiling=10000)
    case["variables"]["budgetExecutionFence"] = json.dumps(fence)
    plan = run_cases(compiled_expressions, tmp_path, [case])[0]
    assert plan["error"] == ""
    assert plan["request_token_ceiling"] == 10000 * samples
    assert plan["receipts"][0]["reserved_tokens"] == 10000 * samples
    assert plan["output_field"] == ""
    assert "normalized_body" not in plan
    result = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(compiler.USD_RESERVATION),
        "variables": {
            "executionPlan": plan,
            "executionPriceProjection": {"error": "", "etag": 'W/"unit"', "document": {
                "rate_schedules": [{"input_rate_units": 200000000, "cached_rate_units": 0,
                                    "cache_write_rate_units": 0, "output_rate_units": 400000000}],
            }},
        },
    }])[0]
    assert result["error"] == ""
    assert result["reserved_usd"] == pytest.approx(0.04 * samples)


def test_usd_reservation_refuses_unverified_legacy_estimates(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
) -> None:
    result = run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(compiler.USD_RESERVATION),
        "variables": {
            "executionPlan": {"algorithm": "legacy_request_estimate_v1",
                              "body_bytes_estimate": 10, "output_limit": 32, "output_samples": 1},
            "executionPriceProjection": {"error": "", "etag": 'W/"unit"', "document": {
                "rate_schedules": [{"input_rate_units": 200000000, "cached_rate_units": 0,
                                    "cache_write_rate_units": 0, "output_rate_units": 400000000}],
            }},
        },
    }])[0]
    assert result["error"] == "budget_count_bound_unavailable"
    assert "reserved_usd" not in result


def test_delegated_unicode_metadata_decodes_exactly_once(
    compiled_expressions: tuple[Path, str], tmp_path: Path,
) -> None:
    expression = ('@(Uri.UnescapeDataString(context.Request.Headers.GetValueOrDefault('
                  '"x-user-name", "")))')
    assert run_cases(compiled_expressions, tmp_path, [{
        "body": "{}", "expression": expression_id(expression), "variables": {},
        "headers": {"x-user-name": "%E7%A0%94%E5%8F%91%20%2520"},
    }]) == ["研发 %20"]


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
