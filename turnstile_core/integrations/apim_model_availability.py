from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any
from xml.etree import ElementTree as ET

from ..domain.control_plane import GatewayPublication
from ..domain.model_availability import (
    ModelAvailabilityPolicyAdoption,
    ModelAvailabilityPublication,
)
from .apim_control_plane_contract import PolicyCompilationError
from .apim_policy_components import (
    _matches_policy_component,
    component_digest,
    expression_tokens,
    parse_policy,
    serialize_policy,
)

AVAILABILITY_PROBE_PREFIX = "availability-probe-"

RESOLVE_MODEL = r"""@{
  try {
    var body = context.Request.Body.As<JObject>(preserveContent: true);
    if (body["model"] == null || body["model"].Type != JTokenType.String) { return ""; }
    var alias = ((string)body["model"]).Trim();
    var routes = JArray.Parse((string)context.Variables["availabilityRoutes"]);
    var matches = routes.OfType<JObject>().Where(route =>
      string.Equals((string)route["alias"], alias, StringComparison.OrdinalIgnoreCase)
      && (route["gateways"].Type == JTokenType.Null || ((JArray)route["gateways"]).Any(
        gateway => string.Equals((string)gateway, context.Deployment.Gateway.Id,
          StringComparison.OrdinalIgnoreCase)))).ToArray();
    if (matches.Length != 1) { return ""; }
    Guid modelId;
    var value = (string)matches[0]["model_id"];
    return Guid.TryParseExact(value, "D", out modelId) && modelId.ToString() == value ? value : "";
  } catch { return ""; }
}"""

BINDING_VALID = r"""@{
  Guid modelId;
  var value = context.Variables.GetValueOrDefault<string>("availabilityModelId", "");
  return Guid.TryParseExact(value, "D", out modelId) && modelId.ToString() == value
        && context.Variables.GetValueOrDefault<string>("availabilityTargetId", "")
            == "__GATEWAY_PROFILE_ID__"
        && context.Variables.GetValueOrDefault<string>("availabilityApiId", "")
            == context.Api.Id.Split(';')[0]
        && context.Variables.GetValueOrDefault<string>("availabilityApiRevision", "")
            == context.Api.Revision;
}"""

STATE_URL = r"""@{
  return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='"
    + Uri.EscapeDataString("model-availability|v1") + "',RowKey='"
    + Uri.EscapeDataString((string)context.Variables["availabilityModelId"]) + "')";
}"""

READ_STATE = r"""@{
  try {
    var reply = (IResponse)context.Variables["availabilityRead"];
    if (reply == null || reply.StatusCode != 200) { throw new Exception(); }
    var etag = reply.Headers.GetValueOrDefault("ETag", "");
    if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.IndexOf('\r') >= 0
      || etag.IndexOf('\n') >= 0) { throw new Exception(); }
    var text = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(text) > 4096) { throw new Exception(); }
    var entity = JObject.Parse(text);
    var modelId = (string)context.Variables["availabilityModelId"];
    if (entity["PartitionKey"]?.Type != JTokenType.String
      || (string)entity["PartitionKey"] != "model-availability|v1"
      || entity["RowKey"]?.Type != JTokenType.String || (string)entity["RowKey"] != modelId
      || entity["ModelId"]?.Type != JTokenType.String || (string)entity["ModelId"] != modelId
      || entity["SchemaVersion"]?.Type != JTokenType.Integer || (long)entity["SchemaVersion"] != 1
      || entity["Revision"]?.Type != JTokenType.Integer || (long)entity["Revision"] < 1
      || (long)entity["Revision"] > 2147483647 || entity["Enabled"]?.Type != JTokenType.Boolean
      || entity["Lifecycle"]?.Type != JTokenType.String) { throw new Exception(); }
    var lifecycle = (string)entity["Lifecycle"];
        if (lifecycle != "active" && lifecycle != "staged" && lifecycle != "deleted") {
            throw new Exception();
        }
    if (lifecycle == "deleted" && (bool)entity["Enabled"]) { throw new Exception(); }
    return new JObject(new JProperty("outcome", (bool)entity["Enabled"] ? "enabled" : "disabled"),
      new JProperty("revision", (int)entity["Revision"])).ToString(Newtonsoft.Json.Formatting.None);
  } catch {
    return "{\"outcome\":\"unavailable\"}";
  }
}"""

PROBE_NONCE = r"""@{
    try {
        var nonce = context.Request.Headers.GetValueOrDefault("x-turnstile-availability-probe", "");
        Guid parsed;
        if (nonce.Length != 32 || !Guid.TryParseExact(nonce, "N", out parsed)
            || parsed.ToString("N") != nonce) { return ""; }
        var kind = context.Variables.GetValueOrDefault<string>("availabilitySurfaceKind", "");
        if (context.Request.Method == "GET" && kind == "catalog") { return nonce; }
        if (context.Request.Method != "HEAD" || (kind != "inference" && kind != "catalog")
            || !context.Operation.Id.StartsWith("availability-probe-", StringComparison.Ordinal)) {
            return "";
        }
        return nonce;
    } catch { return ""; }
}"""

NODE_ROW_KEY = r"""@{
    var identity = new JArray(context.Api.Revision, context.Deployment.Gateway.Id,
        context.Deployment.Gateway.InstanceId).ToString(Newtonsoft.Json.Formatting.None);
    using (var algorithm = System.Security.Cryptography.SHA256.Create()) {
        return BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(identity)))
            .Replace("-", "").ToLowerInvariant();
    }
}"""

NODE_URL = r"""@{
    var partition = "model-availability-nodes|v1|"
        + (string)context.Variables["availabilityTargetId"] + "|" + context.Api.Id.Split(';')[0];
    return ((string)context.Variables["availabilityTableEndpoint"]).TrimEnd('/') + "/"
        + (string)context.Variables["availabilityTableName"] + "(PartitionKey='"
        + Uri.EscapeDataString(partition) + "',RowKey='"
        + Uri.EscapeDataString((string)context.Variables["availabilityNodeKey"]) + "')";
}"""

READ_NODE = r"""@{
    try {
        var reply = (IResponse)context.Variables["availabilityNodeRead"];
        if (reply == null || reply.StatusCode != 200) { return false; }
        var etag = reply.Headers.GetValueOrDefault("ETag", "");
        if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.IndexOf('\r') >= 0
            || etag.IndexOf('\n') >= 0) { return false; }
        var text = reply.Body.As<string>(preserveContent: true);
        if (Encoding.UTF8.GetByteCount(text) > 4096) { return false; }
        var entity = JObject.Parse(text);
        var expected = new Dictionary<string, string> {
            { "PartitionKey", "model-availability-nodes|v1|"
                + (string)context.Variables["availabilityTargetId"]
                + "|" + context.Api.Id.Split(';')[0] },
            { "RowKey", (string)context.Variables["availabilityNodeKey"] },
            { "TargetId", (string)context.Variables["availabilityTargetId"] },
            { "ApiId", context.Api.Id.Split(';')[0] }, { "ApiRevision", context.Api.Revision },
            { "NativeGatewayId", context.Deployment.Gateway.Id },
            { "InstanceId", context.Deployment.Gateway.InstanceId },
            { "CapabilityDigest", (string)context.Variables["availabilityCapabilityDigest"] }
        };
        foreach (var field in expected) {
            if (entity[field.Key]?.Type != JTokenType.String
                || (string)entity[field.Key] != field.Value) {
                return false;
            }
        }
        if (entity["SchemaVersion"]?.Type != JTokenType.Integer
            || (long)entity["SchemaVersion"] != 1
            || entity["IssuedEpoch"]?.Type != JTokenType.String
            || entity["ExpiresEpoch"]?.Type != JTokenType.String) { return false; }
        long issued, expires;
        var issueText = (string)entity["IssuedEpoch"];
        var expiryText = (string)entity["ExpiresEpoch"];
        if (issueText.Length > 12 || expiryText.Length > 12
            || !long.TryParse(issueText, out issued) || !long.TryParse(expiryText, out expires)
            || JsonConvert.SerializeObject(issued) != issueText
            || JsonConvert.SerializeObject(expires) != expiryText) {
            return false;
        }
        var now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc))
            .TotalSeconds;
        return issued <= now && expires > issued;
    } catch { return false; }
}"""

STORE_IDENTITY = r"""@{
    var identity = ((string)context.Variables["availabilityTableEndpoint"]).TrimEnd('/')
        + "|" + (string)context.Variables["availabilityTableName"];
    using (var algorithm = System.Security.Cryptography.SHA256.Create()) {
        return BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(identity)))
            .Replace("-", "").ToLowerInvariant();
    }
}"""

CATALOG_URL = r"""@{
  var page = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
  var url = (string)context.Variables["availabilityTableEndpoint"] + "/"
    + (string)context.Variables["availabilityTableName"] + "()?$filter="
    + Uri.EscapeDataString("PartitionKey eq 'model-availability|v1'")
    + "&$top=1000&$select=PartitionKey,RowKey,SchemaVersion,ModelId,Revision,Enabled,Lifecycle";
  if (page["next_partition"] != null && page["next_partition"].Type == JTokenType.String) {
    url += "&NextPartitionKey=" + Uri.EscapeDataString((string)page["next_partition"]);
  }
  if (page["next_row"] != null && page["next_row"].Type == JTokenType.String) {
    url += "&NextRowKey=" + Uri.EscapeDataString((string)page["next_row"]);
  }
  return url;
}"""

CATALOG_PAGE = r"""@{
  try {
    var previous = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
    if ((bool?)previous["ok"] != true || (int)previous["pages"] >= 4) { throw new Exception(); }
    var reply = (IResponse)context.Variables["availabilityCatalogRead"];
    if (reply == null || reply.StatusCode != 200) { throw new Exception(); }
    var text = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(text) > 524288) { throw new Exception(); }
    var values = JObject.Parse(text)["value"] as JArray;
    if (values == null || values.Count > 1000) { throw new Exception(); }
    var states = (JObject)previous["states"];
    foreach (var token in values) {
      var entity = token as JObject;
      Guid modelId;
      if (entity == null || entity["PartitionKey"]?.Type != JTokenType.String
        || (string)entity["PartitionKey"] != "model-availability|v1"
        || entity["RowKey"]?.Type != JTokenType.String
        || entity["ModelId"]?.Type != JTokenType.String
        || (string)entity["RowKey"] != (string)entity["ModelId"]
        || !Guid.TryParseExact((string)entity["ModelId"], "D", out modelId)
        || modelId.ToString() != (string)entity["ModelId"]
        || entity["SchemaVersion"]?.Type != JTokenType.Integer || (long)entity["SchemaVersion"] != 1
        || entity["Revision"]?.Type != JTokenType.Integer || (long)entity["Revision"] < 1
        || (long)entity["Revision"] > 2147483647 || entity["Enabled"]?.Type != JTokenType.Boolean
        || entity["Lifecycle"]?.Type != JTokenType.String) { throw new Exception(); }
      var lifecycle = (string)entity["Lifecycle"];
            if (lifecycle != "active" && lifecycle != "staged" && lifecycle != "deleted") {
                throw new Exception();
            }
      if (lifecycle == "deleted" && (bool)entity["Enabled"]) { throw new Exception(); }
      var key = modelId.ToString();
      if (states.Property(key) != null) { throw new Exception(); }
      states[key] = new JObject(new JProperty("revision", (int)entity["Revision"]),
        new JProperty("enabled", (bool)entity["Enabled"]));
    }
        var nextPartition = reply.Headers.GetValueOrDefault(
            "x-ms-continuation-NextPartitionKey", "");
    var nextRow = reply.Headers.GetValueOrDefault("x-ms-continuation-NextRowKey", "");
    if (nextPartition.Length > 1024 || nextRow.Length > 1024
            || string.IsNullOrEmpty(nextPartition) && !string.IsNullOrEmpty(nextRow)) {
            throw new Exception();
        }
        return new JObject(new JProperty("ok", true),
            new JProperty("pages", (int)previous["pages"] + 1),
      new JProperty("states", states),
      new JProperty("next_partition", string.IsNullOrEmpty(nextPartition) ? null : nextPartition),
      new JProperty("next_row", string.IsNullOrEmpty(nextRow) ? null : nextRow)
    ).ToString(Newtonsoft.Json.Formatting.None);
  } catch { return "{\"ok\":false}"; }
}"""

FILTER_CATALOG = r"""@{
  try {
    var page = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
    if ((bool?)page["ok"] != true || page["next_partition"]?.Type != JTokenType.Null
            || page["next_row"]?.Type != JTokenType.Null || (int)page["pages"] < 1) {
            throw new Exception();
        }
    var states = (JObject)page["states"];
    var models = JArray.Parse((string)context.Variables["availabilityCatalogScope"]);
    if (models.Count > 500) { throw new Exception(); }
    var data = new JArray();
    var aliases = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    var revisions = new Dictionary<string, string>(StringComparer.Ordinal);
    foreach (JObject model in models) {
      var id = (string)model["logical_model_id"];
      var observed = states[id] as JObject;
      if (observed == null || !aliases.Add((string)model["id"])) { throw new Exception(); }
      var enabled = (bool)observed["enabled"];
        revisions[id] = id + ":" + JsonConvert.SerializeObject((int)observed["revision"])
        + ":" + (enabled ? "1" : "0");
      if (enabled) {
        var entry = (JObject)model.DeepClone();
        entry.Remove("logical_model_id");
        data.Add(entry);
      }
    }
    string digest;
    using (var algorithm = System.Security.Cryptography.SHA256.Create()) {
      digest = BitConverter.ToString(algorithm.ComputeHash(Encoding.UTF8.GetBytes(
                string.Join("|", revisions.OrderBy(item => item.Key, StringComparer.Ordinal)
                    .Select(item => item.Value))))).Replace("-", "").ToLowerInvariant();
    }
    var body = new JObject(new JProperty("data", data), new JProperty("has_more", false),
      new JProperty("first_id", data.Count == 0 ? JValue.CreateNull() : data[0]["id"]),
      new JProperty("last_id", data.Count == 0 ? JValue.CreateNull() : data[data.Count - 1]["id"]));
    return new JObject(new JProperty("ok", true), new JProperty("body", body),
      new JProperty("digest", digest)).ToString(Newtonsoft.Json.Formatting.None);
  } catch { return "{\"ok\":false}"; }
}"""


RESOLVE_FIXED_MODEL = r'''@{
    try {
        var alias = (string)context.Variables["availabilityFixedModelAlias"];
        var routes = JArray.Parse((string)context.Variables["availabilityRoutes"]);
        var matches = routes.OfType<JObject>().Where(route =>
            string.Equals((string)route["alias"], alias, StringComparison.Ordinal)
            && (route["gateways"].Type == JTokenType.Null || ((JArray)route["gateways"]).Any(
                gateway => string.Equals((string)gateway, context.Deployment.Gateway.Id,
                    StringComparison.OrdinalIgnoreCase)))).ToArray();
        if (matches.Length != 1) { return ""; }
        Guid modelId;
        var value = (string)matches[0]["model_id"];
                return Guid.TryParseExact(value, "D", out modelId) && modelId.ToString() == value
                    ? value : "";
    } catch { return ""; }
}'''

INHERITED_OPERATION_POLICY = (
        "<policies><inbound><base /></inbound><backend><base /></backend>"
        "<outbound><base /></outbound><on-error><base /></on-error></policies>"
)


def _variable(parent: ET.Element, name: str, expression: str) -> ET.Element:
    return ET.SubElement(parent, "set-variable", {"name": name, "value": expression})


def availability_kernel_digest() -> str:
    return hashlib.sha256(
        "|".join(
            (
                RESOLVE_MODEL,
                RESOLVE_FIXED_MODEL,
                BINDING_VALID,
                READ_STATE,
                PROBE_NONCE,
                NODE_ROW_KEY,
                NODE_URL,
                READ_NODE,
                STORE_IDENTITY,
                CATALOG_URL,
                CATALOG_PAGE,
                FILTER_CATALOG,
                component_digest(admission_guard()),
                component_digest(availability_probe_guard()),
            )
        ).encode()
    ).hexdigest()


def availability_capability_digest(
    snapshot: ModelAvailabilityPublication | ModelAvailabilityPolicyAdoption,
) -> str:
    source = json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256((availability_kernel_digest() + source).encode()).hexdigest()


def availability_operation_digest(root: ET.Element) -> str:
    source = deepcopy(root)
    inbound = source.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Availability operation inbound is missing")
    for marker in inbound.findall("set-variable[@name='availabilitySurfaceDigest']"):
        inbound.remove(marker)
    return component_digest(source)


def _header(parent: ET.Element, name: str, value: str) -> None:
    node = ET.Element("set-header", {"name": name, "exists-action": "override"})
    body = parent.find("set-body") if parent.tag == "return-response" else None
    if body is None:
        parent.append(node)
    else:
        parent.insert(list(parent).index(body), node)
    ET.SubElement(node, "value").text = value


def _storage_read(parent: ET.Element, name: str, url: str) -> None:
    request = ET.SubElement(
        parent,
        "send-request",
        {
            "mode": "new",
            "response-variable-name": name,
            "timeout": "2",
            "ignore-error": "true",
        },
    )
    ET.SubElement(request, "set-url").text = url
    ET.SubElement(request, "set-method").text = "GET"
    _header(request, "x-ms-version", "2020-12-06")
    _header(request, "Accept", "application/json;odata=nometadata")
    _header(request, "Cache-Control", "no-cache")
    ET.SubElement(
        request,
        "authentication-managed-identity",
        {
            "resource": "https://storage.azure.com/",
        },
    )


def availability_denial(*, disabled: bool = False) -> ET.Element:
    response = ET.Element("return-response")
    code = "model_disabled" if disabled else "model_availability_unavailable"
    ET.SubElement(
        response,
        "set-status",
        {
            "code": "403" if disabled else "503",
            "reason": "Forbidden" if disabled else "Service Unavailable",
        },
    )
    _header(response, "Content-Type", "application/json")
    _header(response, "Cache-Control", "no-store")
    _header(
        response,
        "x-request-id",
        '@(context.Variables.GetValueOrDefault<string>("callerRequestId", '
        "context.RequestId.ToString()))",
    )
    _header(response, "x-correlation-id", "@(context.RequestId.ToString())")
    if not disabled:
        _header(response, "Retry-After", "1")
    ET.SubElement(response, "set-body").text = (
        '{"type":"error","error":{"type":"'
        + ("permission_error" if disabled else "api_error")
        + '","code":"'
        + code
        + '","param":"model","message":"'
        + (
            "The requested model is disabled"
            if disabled
            else "Model availability could not be verified"
        )
        + '"}}'
    )
    return response


def node_admission_guard() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                "@(!context.Deployment.Gateway.IsManaged && string.IsNullOrEmpty("
                'context.Variables.GetValueOrDefault<string>("availabilityProbeNonce", "")))'
            )
        },
    )
    _variable(branch, "availabilityNodeKey", NODE_ROW_KEY)
    _storage_read(branch, "availabilityNodeRead", NODE_URL)
    _variable(branch, "availabilityNodeAllowed", READ_NODE)
    denied = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {"condition": '@(!(bool)context.Variables["availabilityNodeAllowed"])'},
    )
    denied.append(availability_denial())
    return root


def availability_probe_guard() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                '@((context.Variables.GetValueOrDefault<string>("availabilitySurfaceKind", "")'
                ' == "inference" || context.Variables.GetValueOrDefault<string>('
                '"availabilitySurfaceKind", "") == "catalog") && !string.IsNullOrEmpty('
                'context.Variables.GetValueOrDefault<string>("availabilityProbeNonce", "")))'
            )
        },
    )
    response = availability_denial()
    for header, value in (
        ("version", "1"),
        ("probe", '@((string)context.Variables["availabilityProbeNonce"])'),
        ("kernel", '@((string)context.Variables["availabilityKernelDigest"])'),
        ("capability", '@((string)context.Variables["availabilityCapabilityDigest"])'),
        ("surface", '@((string)context.Variables["availabilitySurfaceDigest"])'),
        ("target", '@((string)context.Variables["availabilityTargetId"])'),
        ("revision", "@(context.Api.Revision)"),
        ("gateway", "@(context.Deployment.Gateway.Id)"),
        ("instance", "@(context.Deployment.Gateway.InstanceId)"),
        ("store", STORE_IDENTITY),
    ):
        _header(response, "x-turnstile-availability-" + header, value)
    branch.append(response)
    invalid_method = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                '@(context.Request.Method == "HEAD" || ('
                'context.Variables.GetValueOrDefault<string>("availabilitySurfaceKind", "")'
                ' == "inference" && context.Request.Method != "POST"))'
            )
        },
    )
    denied = availability_denial()
    status = denied.find("set-status")
    assert status is not None
    status.set("code", "405")
    status.set("reason", "Method Not Allowed")
    _header(denied, "Allow", "POST")
    invalid_method.append(denied)
    return root


def admission_guard(
    *, native_gateway_scope: bool = False, logger_id: str | None = None,
) -> ET.Element:
    from .apim_budget_execution import pre_dispatch_denial_event

    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": (
                '@(context.Request.Method == "POST" || context.Operation.Id == "chat-completions"'
                ' || context.Operation.Id == "responses"'
                ' || context.Operation.Id == "responses-compact"'
                ' || context.Operation.Id == "anthropic-messages"'
                ' || context.Operation.Id == "anthropic-count-tokens"'
                ' || context.Operation.Id == "images-generations")'
            )
        },
    )
    _variable(branch, "availabilityBindingValid", BINDING_VALID)
    invalid = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@(!(bool)context.Variables["availabilityBindingValid"])',
        },
    )
    invalid.append(availability_denial())
    _storage_read(branch, "availabilityRead", STATE_URL)
    _variable(branch, "availabilityDecision", READ_STATE)
    outcomes = ET.SubElement(branch, "choose")
    for outcome, disabled in (("disabled", True), ("unavailable", False)):
        rejected = ET.SubElement(
            outcomes,
            "when",
            {
                "condition": (
                    "@((string)JObject.Parse((string)context.Variables"
                    '["availabilityDecision"])["outcome"]'
                    f' == "{outcome}")'
                )
            },
        )
        trace = ET.SubElement(
            rejected,
            "trace",
            {
                "source": "turnstile-model-availability",
                "severity": "information",
            },
        )
        ET.SubElement(trace, "message").text = (
            "model_disabled" if disabled else "model_availability_unavailable"
        )
        ET.SubElement(trace, "metadata", {"name": "provider_dispatched", "value": "false"})
        ET.SubElement(
            trace,
            "metadata",
            {
                "name": "correlation_id",
                "value": "@(context.RequestId.ToString())",
            },
        )
        if disabled and logger_id is not None:
            business = ET.SubElement(ET.SubElement(rejected, "choose"), "when", {
                "condition": (
                    '@(context.Variables.GetValueOrDefault<bool>('
                    '"isInferenceOperation", false))'
                ),
            })
            business.append(pre_dispatch_denial_event("403", {
                "ingest_error": '"model_availability_pre_dispatch_denial"',
                "model_admission": '"model_disabled"', "error_message": '"model_disabled"',
                "model": (
                    'context.Variables.GetValueOrDefault<string>("availabilityFixedModelAlias", '
                    'context.Variables.GetValueOrDefault<string>("telemetryModel", "unattributed"))'
                ),
                "model_id": '(string)context.Variables["availabilityModelId"]',
                "native_gateway_instance_id": (
                    'known ? (string.IsNullOrWhiteSpace(context.Deployment.Gateway.InstanceId) '
                    '? context.Deployment.Gateway.Id : '
                    'context.Deployment.Gateway.InstanceId) : null'
                ),
                "apim_subscription_id": '(context.Subscription?.Id ?? "").Split(\'/\').Last()',
            }, logger_id=logger_id, request_source=(
                'context.Variables.GetValueOrDefault<string>("telemetryRequestSource", '
                'context.Request.Headers.GetValueOrDefault("x-request-source", "unattributed"))'
            )))
        rejected.append(availability_denial(disabled=disabled))
    if not native_gateway_scope:
        branch.append(node_admission_guard())
    return root


def _legacy_authenticated_branches(root: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    inbound = root.find("inbound")
    if inbound is None:
        return []
    condition = expression_tokens('@((bool)context.Variables["hasEmployeeToken"])')
    for choice in inbound.findall("choose"):
        branches = [node for node in choice if isinstance(node.tag, str)]
        if [branch.tag for branch in branches] != ["when", "otherwise"] or (
            expression_tokens(branches[0].get("condition", "")) != condition
        ):
            continue
        employee = [node for node in branches[0] if isinstance(node.tag, str)]
        subscription = [node for node in branches[1] if isinstance(node.tag, str)]
        if not employee or employee[0].tag not in {"validate-azure-ad-token", "validate-jwt"}:
            return []
        if not subscription or subscription[0].tag != "choose":
            return []
        checks = list(subscription[0])
        if len(checks) != 1 or checks[0].tag != "when" or (
            expression_tokens(checks[0].get("condition", ""))
            != expression_tokens("@(context.Subscription == null)")
        ):
            return []
        children = [node for node in checks[0] if isinstance(node.tag, str)]
        if len(children) != 1 or children[0].tag != "return-response":
            return []
        status = children[0].find("set-status")
        if status is None or status.get("code") != "401":
            return []
        return [(branches[0], employee[0]), (branches[1], subscription[0])]
    return []


def _admission_locations(root: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    locations = []
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Availability requires a parent inbound policy")
    for parent in inbound:
        if parent.tag != "choose":
            continue
        for branch in parent:
            for node in branch:
                condition = node.find("when") if node.tag == "choose" else None
                if condition is not None and "selectedModelKnown" in condition.get("condition", ""):
                    locations.append((branch, node))
    legacy_markers = root.findall(".//set-variable[@name='availabilityLegacyAuthenticated']")
    if legacy_markers:
        if locations or len(legacy_markers) != 2:
            raise PolicyCompilationError("Legacy availability authentication is ambiguous")
        branches = _legacy_authenticated_branches(root)
        if len(branches) != 2:
            raise PolicyCompilationError("Legacy authentication cannot be verified")
        for branch, authenticated in branches:
            marker = branch.find("set-variable[@name='availabilityLegacyAuthenticated']")
            if marker is None or marker not in legacy_markers or marker.get("value") != "@(true)":
                raise PolicyCompilationError("Legacy authentication marker is invalid")
            preceding = [node for node in list(branch)[:list(branch).index(marker)]
                         if isinstance(node.tag, str)]
            if preceding != [authenticated] and (
                len(preceding) != 2 or preceding[0] is not authenticated or (
                    component_digest(preceding[1]) != component_digest(availability_probe_guard())
                )
            ):
                raise PolicyCompilationError("Legacy availability must follow authentication")
            locations.append((branch, marker))
    if len(locations) != 2:
        raise PolicyCompilationError("Availability requires both authenticated admission paths")
    for parent, anchor in locations:
        earlier = list(parent)[: list(parent).index(anchor)]
        if any(
            child.tag in {"llm-token-limit", "rate-limit-by-key", "send-request"}
            for node in earlier
            for child in node.iter()
        ):
            raise PolicyCompilationError("Availability must precede counting and reservations")
    return locations


def compose_availability_parent(source: str) -> str:
    from .apim_budget_execution import eventhub_logger_id

    root = parse_policy(source)
    if any(
        node.tag in {"cache-lookup", "cache-store", "cache-lookup-value", "cache-store-value"}
        for node in root.iter()
    ):
        raise PolicyCompilationError("Availability requires uncached admission and discovery")
    if root.find(".//set-variable[@name='availabilityProtocolVersion']") is not None:
        validate_availability_parent(root)
        return source
    inbound = root.find("inbound")
    assert inbound is not None
    if not any(
        "selectedModelKnown" in node.get("condition", "")
        for node in inbound.iter("when")
    ):
        legacy = _legacy_authenticated_branches(root)
        if len(legacy) != 2:
            raise PolicyCompilationError("Availability requires both authenticated admission paths")
        for branch, authenticated in legacy:
            branch.insert(list(branch).index(authenticated) + 1, ET.Element("set-variable", {
                "name": "availabilityLegacyAuthenticated", "value": "@(true)",
            }))
    logger_id = eventhub_logger_id(root) if root.findall(".//log-to-eventhub") else None
    for parent, anchor in _admission_locations(root):
        parent.insert(list(parent).index(anchor), availability_probe_guard())
        parent.insert(list(parent).index(anchor) + 1, admission_guard(
            logger_id=logger_id,
        ))
    inbound.insert(
        0,
        ET.Element(
            "set-variable",
            {
                "name": "availabilityProtocolVersion",
                "value": "@(1)",
            },
        ),
    )
    inbound.insert(
        1,
        ET.Element(
            "set-variable",
            {
                "name": "availabilityTableEndpoint",
                "value": "__LEDGER_TABLE_ENDPOINT__",
            },
        ),
    )
    inbound.insert(
        2,
        ET.Element(
            "set-variable",
            {
                "name": "availabilityTableName",
                "value": "__LEDGER_TABLE_NAME__",
            },
        ),
    )
    inbound.insert(
        3,
        ET.Element(
            "set-variable",
            {
                "name": "availabilityKernelDigest",
                "value": availability_kernel_digest(),
            },
        ),
    )
    inbound.insert(
        4,
        ET.Element(
            "set-variable",
            {
                "name": "availabilityProbeNonce",
                "value": PROBE_NONCE,
            },
        ),
    )
    validate_availability_parent(root)
    return serialize_policy(root)


def validate_availability_parent(root: ET.Element) -> None:
    from .apim_budget_execution import eventhub_logger_id

    markers = root.findall(".//set-variable[@name='availabilityProtocolVersion']")
    if (
        len(markers) != 1
        or markers[0] is not root.find("inbound/set-variable[@name='availabilityProtocolVersion']")
        or markers[0].get("value") != "@(1)"
    ):
        raise PolicyCompilationError("Availability capability marker is missing or incompatible")
    bindings: dict[str, str] = {}
    for name, expected in (
        ("availabilityKernelDigest", availability_kernel_digest()),
        ("availabilityProbeNonce", PROBE_NONCE),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if (
            len(nodes) != 1
            or nodes[0] is not root.find(f"inbound/set-variable[@name='{name}']")
            or not _matches_policy_component(
                nodes[0],
                ET.Element("set-variable", {"name": name, "value": expected}),
                bindings,
            )
        ):
            raise PolicyCompilationError("Availability node protocol integrity changed")
    for name, value in (
        ("availabilityTableEndpoint", "__LEDGER_TABLE_ENDPOINT__"),
        ("availabilityTableName", "__LEDGER_TABLE_NAME__"),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if len(nodes) != 1 or nodes[0] is not root.find(f"inbound/set-variable[@name='{name}']"):
            raise PolicyCompilationError("Availability store configuration is ambiguous")
        expected_store = ET.Element("set-variable", {"name": name, "value": value})
        if not _matches_policy_component(nodes[0], expected_store, bindings):
            raise PolicyCompilationError("Availability store configuration is invalid")
    guards = root.findall(".//set-variable[@name='availabilityBindingValid']")
    if len(guards) != 2:
        raise PolicyCompilationError("Availability guards are incomplete or duplicated")
    locations = _admission_locations(root)
    parents = {child: parent for parent in root.iter() for child in parent}
    branches = {parents[parent] for parent, _ in locations}
    inbound = root.find("inbound")
    assert inbound is not None
    if len(branches) != 1:
        raise PolicyCompilationError("Availability admission paths are not exhaustive")
    authentication = next(iter(branches))
    if authentication not in list(inbound) or [
        node.tag for node in authentication if isinstance(node.tag, str)
    ] != ["when", "otherwise"]:
        raise PolicyCompilationError("Availability admission paths are not exhaustive")
    common_prefix = list(inbound)[: list(inbound).index(authentication)]
    for node in (
        child for item in common_prefix if not is_read_only_budget_fence(item)
        for child in item.iter()
    ):
        if node.tag in {
            "llm-token-limit",
            "rate-limit",
            "rate-limit-by-key",
            "quota",
            "quota-by-key",
            "send-request",
            "forward-request",
            "include-fragment",
        }:
            raise PolicyCompilationError("Work cannot precede model availability admission")
        if node.tag == "return-response" and not is_pre_admission_denial(root, node):
            raise PolicyCompilationError("A response can bypass model availability")
    expected_names = {
        "availabilityProtocolVersion",
        "availabilityTableEndpoint",
        "availabilityTableName",
        "availabilityBindingValid",
        "availabilityDecision",
        "availabilityKernelDigest",
        "availabilityProbeNonce",
        "availabilityNodeKey",
        "availabilityNodeAllowed",
        "availabilityLegacyAuthenticated",
    }
    if any(
        node.get("name", "").startswith("availability") and node.get("name") not in expected_names
        for node in root.iter("set-variable")
    ):
        raise PolicyCompilationError("Parent cannot shadow operation availability identity")
    if len(root.findall(".//set-variable[@name='availabilityDecision']")) != 2:
        raise PolicyCompilationError("Parent availability decisions are ambiguous")
    logger_id = eventhub_logger_id(root) if root.findall(".//log-to-eventhub") else None
    for parent, anchor in locations:
        probe_index = list(parent).index(anchor) - 1
        if probe_index < 0 or not _matches_policy_component(
            parent[probe_index],
            availability_probe_guard(),
            bindings,
        ):
            raise PolicyCompilationError("Availability probe integrity or placement changed")
        index = list(parent).index(anchor) + 1
        if index >= len(parent) or not (
            _matches_policy_component(
                parent[index], admission_guard(logger_id=logger_id), bindings,
            ) or logger_id is not None and _matches_policy_component(
                parent[index], admission_guard(), bindings,
            )
        ):
            raise PolicyCompilationError("Availability guard integrity or placement changed")


def is_read_only_budget_fence(node: ET.Element) -> bool:
    from .apim_budget_execution import budget_execution_fence

    branches = list(node)
    if node.tag != "choose" or len(branches) != 1 or branches[0].tag != "when":
        return False
    prefix = expression_tokens("@(JArray.Parse(")
    suffix = expression_tokens(
        ").Values<string>().Contains(context.Api.Id.Split(';')[0]))"
    )
    tokens = expression_tokens(branches[0].get("condition", ""))
    if len(tokens) != len(prefix) + 1 + len(suffix) or (
        tokens[:len(prefix)] != prefix or tokens[-len(suffix):] != suffix
    ):
        return False
    try:
        api_ids = json.loads(json.loads(tokens[len(prefix)]))
        if not isinstance(api_ids, list) or not api_ids or any(
            not isinstance(value, str) for value in api_ids
        ):
            return False
        expected = budget_execution_fence(api_ids)
    except (ValueError, TypeError):
        return False
    return _matches_policy_component(node, expected, {})


def is_pre_admission_denial(root: ET.Element, response: ET.Element) -> bool:
    status = response.find("set-status")
    code = status.get("code", "") if status is not None else ""
    if code.isdigit():
        return 400 <= int(code) <= 599
    from .apim_budget_execution import _deny

    parent = next((parent for parent in root.iter() if response in list(parent)), None)
    if parent is None or parent.tag != "when":
        return False
    error = parent.find("set-variable[@name='executionError']")
    if error is None:
        return False
    reference = ET.Element("inbound")
    _deny(reference, parent.get("condition", ""), error.get("value", ""))
    expected = reference.find("choose/when")
    if expected is not None and _matches_policy_component(parent, expected, {}):
        return True
    if expression_tokens(code) != expression_tokens('@((int)context.Variables["executionStatus"])'):
        return False
    statuses = [node for node in parent.iter("set-variable")
                if node.get("name") == "executionStatus"]
    if len(statuses) != 1 or statuses[0] not in list(parent) or (
        list(parent).index(statuses[0]) >= list(parent).index(response)
    ):
        return False
    status_node = reference.find("choose/when/set-variable[@name='executionStatus']")
    assert status_node is not None
    legacy_status = '''@(
        (string)context.Variables["executionError"] == "budget_temporarily_reserved" ? 429
        : (string)context.Variables["executionError"] == "budget_exhausted"
          || (string)context.Variables["executionError"] == "budget_consumer_disabled"
          || (string)context.Variables["executionError"] == "budget_delegation_invalid" ? 403
        : (string)context.Variables["executionError"] == "budget_request_bound_exceeds_balance"
          || (string)context.Variables["executionError"] == "budget_output_limit_invalid"
          ? 400 : 503)'''
    initial_status = (
        '@((string)context.Variables["executionError"] == "budget_temporarily_reserved" ? 429'
        ' : (string)context.Variables["executionError"] == "budget_exhausted"'
        ' || (string)context.Variables["executionError"] == "budget_consumer_disabled" ? 403'
        ' : (string)context.Variables["executionError"] == "budget_request_bound_exceeds_balance"'
        ' || (string)context.Variables["executionError"] == "budget_output_limit_invalid"'
        ' ? 400 : 503)'
    )
    return expression_tokens(statuses[0].get("value", "")) in {
        expression_tokens(status_node.get("value", "")), expression_tokens(legacy_status),
        expression_tokens(initial_status),
    }


def without_availability_parent(root: ET.Element) -> ET.Element:
    validate_availability_parent(root)
    result = deepcopy(root)
    for parent, anchor in _admission_locations(result):
        parent.remove(parent[list(parent).index(anchor) + 1])
        parent.remove(parent[list(parent).index(anchor) - 1])
        if anchor.get("name") == "availabilityLegacyAuthenticated":
            parent.remove(anchor)
    inbound = result.find("inbound")
    assert inbound is not None
    marker = inbound.find("set-variable[@name='availabilityProtocolVersion']")
    assert marker is not None
    inbound.remove(marker)
    for name in (
        "availabilityTableEndpoint",
        "availabilityTableName",
        "availabilityKernelDigest",
        "availabilityProbeNonce",
    ):
        node = inbound.find(f"set-variable[@name='{name}']")
        assert node is not None
        inbound.remove(node)
    return result


def catalog_read() -> list[ET.Element]:
    root = ET.Element("inbound")
    _variable(root, "availabilityCatalogPage", '{"ok":true,"pages":0,"states":{}}')
    retry = ET.SubElement(
        root,
        "retry",
        {
            "count": "3",
            "interval": "0",
            "first-fast-retry": "true",
            "condition": (
                "@((bool?)JObject.Parse((string)context.Variables"
                '["availabilityCatalogPage"])["ok"] == true'
                " && JObject.Parse((string)context.Variables"
                '["availabilityCatalogPage"])["next_partition"]'
                "?.Type == JTokenType.String)"
            ),
        },
    )
    _storage_read(retry, "availabilityCatalogRead", CATALOG_URL)
    _variable(retry, "availabilityCatalogPage", CATALOG_PAGE)
    return list(root)


def retired_availability_denial() -> ET.Element:
    response = availability_denial(disabled=True)
    _header(response, "x-turnstile-model-retired", "1")
    for name, value in (
        ("probe", '@(context.Request.Headers.GetValueOrDefault('
                  '"x-turnstile-availability-probe", ""))'),
        ("api", "@(context.Api.Id.Split(';')[0])"),
        ("revision", "@(context.Api.Revision)"),
        ("gateway", "@(context.Deployment.Gateway.Id)"),
        ("instance", "@(context.Deployment.Gateway.InstanceId)"),
    ):
        _header(response, "x-turnstile-retired-" + name, value)
    return response


def is_retired_availability_operation(root: ET.Element) -> bool:
    inbound = root.find("inbound")
    if inbound is None:
        return False
    children = [node for node in inbound if isinstance(node.tag, str)]
    return bool(children) and component_digest(children[0]) == component_digest(
        retired_availability_denial()
    )


def protocol_fenced_availability_denial() -> ET.Element:
    response = availability_denial()
    body = response.find("set-body")
    assert body is not None
    body.text = (
        '{"type":"error","error":{"type":"api_error",'
        '"code":"budget_protocol_fenced","message":"This historical revision remains fenced"}}'
    )
    _header(response, "x-turnstile-protocol-fenced", "1")
    for name, value in (
        ("probe", '@(context.Request.Headers.GetValueOrDefault('
                  '"x-turnstile-availability-probe", ""))'),
        ("api", "@(context.Api.Id.Split(';')[0])"),
        ("revision", "@(context.Api.Revision)"),
        ("gateway", "@(context.Deployment.Gateway.Id)"),
        ("instance", "@(context.Deployment.Gateway.InstanceId)"),
    ):
        _header(response, "x-turnstile-protocol-fence-" + name, value)
    return response


def is_protocol_fenced_availability_operation(root: ET.Element) -> bool:
    inbound = root.find("inbound")
    if inbound is None:
        return False
    children = [node for node in inbound if isinstance(node.tag, str)]
    return bool(children) and component_digest(children[0]) == component_digest(
        protocol_fenced_availability_denial()
    )


def compile_protocol_fenced_availability_operation(source: str) -> str:
    root = parse_policy(source)
    if not is_protocol_fenced_availability_operation(root):
        inbound = root.find("inbound")
        if inbound is None:
            raise PolicyCompilationError("Protocol-fenced operation inbound is missing")
        inbound.insert(0, protocol_fenced_availability_denial())
    return serialize_policy(root)


def compile_protocol_fenced_probe(source: str) -> str:
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None or len(inbound.findall("base")) != 1:
        raise PolicyCompilationError("Historical probe requires exactly one operation base")
    base = inbound.find("base")
    assert base is not None
    prefix = list(inbound)[:list(inbound).index(base)]
    if not any(node.tag == "set-variable" and node.get("name") in {
        "imageRequestValid", "dynamicImageModel",
    }
               for node in prefix):
        return source
    processing = [node for node in prefix if not (
        node.tag == "set-variable" and node.get("name", "").startswith("availability")
    )]
    if not processing:
        return source
    inbound.remove(base)
    inbound.insert(list(inbound).index(processing[0]), base)
    return serialize_policy(root)


def compile_availability_operation(
    source: str,
    publication: GatewayPublication,
    snapshot: ModelAvailabilityPublication,
    *,
    catalog: bool = False,
) -> str:
    snapshot.validate_publication(publication)
    bindings = {
        binding.model.model_key.casefold(): binding for binding in publication.desired_spec.bindings
    }
    routes = []
    for alias, model_id in snapshot.models.items():
        binding = bindings.get(alias.casefold())
        routes.append(
            {
                "alias": alias,
                "model_id": str(model_id),
                "gateways": [
                    deployment.native_gateway_id for deployment in binding.serving_deployments
                ]
                if binding and binding.deployment_scope.mode.value == "selected"
                else None,
            }
        )
    return _compile_availability_operation(source, snapshot, routes, catalog=catalog)


def compile_availability_adoption(
    source: str,
    adoption: ModelAvailabilityPolicyAdoption,
    operation_id: str,
    *,
    catalog: bool = False,
) -> str:
    adoption.validate_source(operation_id, source)
    if operation_id in adoption.retired_operations:
        if catalog:
            raise PolicyCompilationError("A catalog cannot be a retired fixed route")
        root = parse_policy(source)
        inbound = root.find("inbound")
        if inbound is None:
            raise PolicyCompilationError("Retired route inbound policy is missing")
        inbound.insert(0, retired_availability_denial())
        return serialize_policy(root)
    routes = [
        {"alias": alias, "model_id": str(model_id), "gateways": adoption.serving_gateways[alias]}
        for alias, model_id in adoption.models.items()
    ]
    fixed_model = adoption.fixed_operations.get(operation_id)
    if fixed_model is not None:
        if catalog:
            raise PolicyCompilationError("A catalog cannot use a fixed inference identity")
        routes = [route for route in routes if route["alias"] == fixed_model]
    return _compile_availability_operation(
        source, adoption, routes, catalog=catalog, fixed_model_alias=fixed_model,
    )


def _compile_availability_operation(
    source: str,
    snapshot: ModelAvailabilityPublication | ModelAvailabilityPolicyAdoption,
    routes: Sequence[dict[str, Any]],
    *,
    catalog: bool,
    fixed_model_alias: str | None = None,
) -> str:
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None or len(inbound.findall("base")) != 1:
        raise PolicyCompilationError("Availability requires exactly one operation base policy")
    base = inbound.find("base")
    assert base is not None
    nodes = ET.Element("inbound")
    for name, value in (
        ("availabilityTargetId", str(snapshot.target_id)),
        ("availabilityApiId", snapshot.api_id),
        ("availabilityApiRevision", snapshot.revision),
        ("availabilitySurfaceKind", "catalog" if catalog else "inference"),
        ("availabilityCapabilityDigest", availability_capability_digest(snapshot)),
    ):
        _variable(nodes, name, value)
    if not catalog:
        _variable(nodes, "availabilityRoutes", json.dumps(routes, separators=(",", ":")))
        if fixed_model_alias is not None:
            _variable(nodes, "availabilityFixedModelAlias", fixed_model_alias)
        _variable(nodes, "availabilityModelId",
                  RESOLVE_FIXED_MODEL if fixed_model_alias is not None else RESOLVE_MODEL)
    for index, node in enumerate(nodes):
        inbound.insert(index, node)
    ready = ET.Element("choose")
    missing = ET.SubElement(
        ready,
        "when",
        {
            "condition": (
                '@(context.Variables.GetValueOrDefault<int>("availabilityProtocolVersion", 0) != 1)'
            )
        },
    )
    missing.append(availability_denial())
    inbound.insert(list(inbound).index(base) + 1, ready)
    inbound.insert(
        list(inbound).index(base) + 2,
        ET.Element(
            "set-header",
            {
                "name": "x-turnstile-availability-probe",
                "exists-action": "delete",
            },
        ),
    )
    if catalog:
        inbound.insert(list(inbound).index(base) + 3, node_admission_guard())
        responses = list(root.iter("return-response"))
        for response in responses:
            status = response.find("set-status")
            body = response.find("set-body")
            if status is None or status.get("code") != "200" or body is None:
                continue
            payload = json.loads(body.text or "{}")
            models = payload.get("data")
            if not isinstance(models, list):
                raise PolicyCompilationError("Published catalog response is invalid")
            for model in models:
                if model["id"] not in snapshot.models:
                    raise PolicyCompilationError("Published catalog identity is missing")
                model["logical_model_id"] = str(snapshot.models[model["id"]])
            parent = next(parent for parent in root.iter() if response in list(parent))
            insertion = list(parent).index(response)
            generated = ET.Element("inbound")
            _variable(
                generated, "availabilityCatalogScope", json.dumps(models, separators=(",", ":"))
            )
            generated.extend(catalog_read())
            _variable(generated, "availabilityCatalogResult", FILTER_CATALOG)
            failed = ET.SubElement(
                ET.SubElement(generated, "choose"),
                "when",
                {
                    "condition": (
                        "@((bool?)JObject.Parse((string)context.Variables"
                        '["availabilityCatalogResult"])["ok"] != true)'
                    )
                },
            )
            failed.append(availability_denial())
            for offset, node in enumerate(generated):
                parent.insert(insertion + offset, node)
            body.text = (
                '@(JObject.Parse((string)context.Variables["availabilityCatalogResult"])["body"]'
                ".ToString(Newtonsoft.Json.Formatting.None))"
            )
            _header(response, "Cache-Control", "no-store, max-age=0")
            _header(response, "Pragma", "no-cache")
            _header(response, "x-turnstile-availability-version", "1")
            for header, variable in (
                ("probe", "availabilityProbeNonce"),
                ("kernel", "availabilityKernelDigest"),
                ("capability", "availabilityCapabilityDigest"),
                ("surface", "availabilitySurfaceDigest"),
            ):
                _header(
                    response,
                    "x-turnstile-availability-" + header,
                    f'@((string)context.Variables["{variable}"])',
                )
            _header(response, "x-turnstile-availability-store", STORE_IDENTITY)
            _header(
                response,
                "x-turnstile-availability-digest",
                (
                    "@((string)JObject.Parse((string)context.Variables"
                    '["availabilityCatalogResult"])["digest"])'
                ),
            )
            _header(response, "x-turnstile-availability-target", str(snapshot.target_id))
            _header(response, "x-turnstile-availability-revision", snapshot.revision)
            _header(
                response, "x-turnstile-availability-gateway", "@(context.Deployment.Gateway.Id)"
            )
            _header(
                response,
                "x-turnstile-availability-instance",
                "@(context.Deployment.Gateway.InstanceId)",
            )
    marker = ET.Element(
        "set-variable",
        {
            "name": "availabilitySurfaceDigest",
            "value": availability_operation_digest(root),
        },
    )
    inbound.insert(0, marker)
    return serialize_policy(root)
