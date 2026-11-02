# ruff: noqa: E501
from __future__ import annotations

from xml.etree import ElementTree as ET

from .apim_policy_components import parse_policy

ROUTER_GLOBAL_POLICY_MARKER = "turnstileRouterEngineVersion"

# The native path cannot run Fallback nodes; name the node so callers know why it is rejected.
ROUTER_REJECTION_BODY = r'''@{
    var message = "";
    try {
      var entity = JObject.Parse((string)context.Variables["routerCachedEntityJson"]);
      var documents = new List<JObject> { JObject.Parse((string)entity["Document"]) };
      var alternates = (string)entity["AlternateDocuments"];
      if (!string.IsNullOrEmpty(alternates)) {
        foreach (var value in JArray.Parse(alternates)) {
          documents.Add(value is JObject ? (JObject)value : JObject.Parse((string)value));
        }
      }
      foreach (var item in documents) {
        if ((string)item["gateway_profile_id"] != "__GATEWAY_PROFILE_ID__") { continue; }
        var nodes = item["definition"]?["nodes"] as JArray;
        var node = nodes == null ? null : nodes.OfType<JObject>().FirstOrDefault(candidate =>
          candidate["fallback_on"] is JArray && ((JArray)candidate["fallback_on"]).Count > 0);
        if (node != null) {
          var name = (string)node["label"];
          if (string.IsNullOrWhiteSpace(name)) { name = (string)node["id"]; }
          message = "Router Fallback node '" + name + "' is not supported by the gateway; "
            + "remove the Fallback node and publish the Router again";
          break;
        }
      }
    } catch { }
    var error = new JObject(new JProperty("code", "router_configuration_unavailable"));
    if (message != "") { error["message"] = message; }
    return new JObject(new JProperty("error", error)).ToString(Newtonsoft.Json.Formatting.None);
  }'''


_ROUTER_PERSON_IDENTITY_XML = r"""string rawUserId = context.Request.Headers.GetValueOrDefault(&quot;x-user-id&quot;, &quot;&quot;).Trim();
                  string callerEmail = &quot;&quot;;
                  bool hasPersonAuth = false;
                  var delegHdr = context.Request.Headers.GetValueOrDefault(&quot;x-turnstile-delegation-token&quot;, &quot;&quot;).Trim();
                  var authHdr = context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;&quot;).Trim();
                  if (!string.IsNullOrEmpty(delegHdr)) {
                    hasPersonAuth = true;
                    string rawTok = delegHdr.StartsWith(&quot;Bearer &quot;, StringComparison.OrdinalIgnoreCase) ? delegHdr.Substring(7).Trim() : delegHdr;
                    if (rawTok.AsJwt() != null &amp;&amp; !string.IsNullOrEmpty(rawUserId) &amp;&amp; !string.Equals(rawUserId, &quot;unattributed&quot;, StringComparison.OrdinalIgnoreCase)) {
                      callerEmail = rawUserId.ToLowerInvariant();
                    }
                  } else if (authHdr.StartsWith(&quot;Bearer &quot;, StringComparison.OrdinalIgnoreCase)) {
                    var jwt = authHdr.Substring(7).Trim().AsJwt();
                    if (jwt != null) {
                      hasPersonAuth = true;
                      callerEmail = (jwt.Claims.GetValueOrDefault(&quot;preferred_username&quot;,
                        jwt.Claims.GetValueOrDefault(&quot;upn&quot;,
                          jwt.Claims.GetValueOrDefault(&quot;email&quot;, &quot;&quot;))) ?? &quot;&quot;).Trim().ToLowerInvariant();
                    }
                  }"""

_ROUTER_FALLBACK_PLAN_XML = r"""var fbEvents = st[&quot;fallback_events&quot;] as JArray ?? new JArray();
                  bool canFallback = fbEvents.Values&lt;string&gt;().Any(ev =&gt; string.Equals(ev, trigger, StringComparison.OrdinalIgnoreCase) || string.Equals(ev, &quot;error&quot;, StringComparison.OrdinalIgnoreCase));
                  var triedIds = new HashSet&lt;string&gt;(attempts.OfType&lt;JObject&gt;().Where(a =&gt; (string)a[&quot;attempt_role&quot;] == &quot;candidate&quot;).Select(a =&gt; (string)a[&quot;model_id&quot;] ?? &quot;&quot;), StringComparer.OrdinalIgnoreCase);
                  var nextCand = canFallback
                    ? ((JArray)st[&quot;eligible_candidates&quot;]).OfType&lt;JObject&gt;().FirstOrDefault(c =&gt; !triedIds.Contains((string)c[&quot;model_id&quot;] ?? &quot;&quot;))
                    : null;
                  if (nextCand != null &amp;&amp; !string.IsNullOrEmpty(context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerOriginalRequestBody&quot;, &quot;&quot;))) {
                    st[&quot;selected_candidate&quot;] = nextCand.DeepClone();
                    st[&quot;decision_basis&quot;] = &quot;fallback_success&quot;;
                    st[&quot;sticky_bound_by&quot;] = JValue.CreateNull();
                    st[&quot;sticky_key&quot;] = JValue.CreateNull();
                    st[&quot;needs_outbound_fallback&quot;] = true;
                  } else {
                    st[&quot;needs_outbound_fallback&quot;] = false;
                  }"""


def _router_engine_templates(*, gateway_profile_id: str | None = None) -> tuple[str, str]:
    native = gateway_profile_id is not None
    components = {
        "__ROUTER_PERSON_IDENTITY__": (
            'string rawUserId = &quot;&quot;; string callerEmail = &quot;&quot;; '
            'bool hasPersonAuth = false;' if native else _ROUTER_PERSON_IDENTITY_XML
        ),
        "__ROUTER_CANDIDATE_TARGET__": (
            ' if ((string)candDoc[&quot;gateway_profile_id&quot;] != &quot;'
            + str(gateway_profile_id) + '&quot;) { continue; }' if native else ""
        ),
        "__ROUTER_FALLBACK_PLAN__": (
            'st[&quot;needs_outbound_fallback&quot;] = false;'
            if native else _ROUTER_FALLBACK_PLAN_XML
        ),
    }
    inbound, outbound = _INBOUND_ROUTER_ENGINE_XML, _OUTBOUND_ROUTER_ENGINE_XML
    for parameter, component in components.items():
        inbound = inbound.replace(parameter, component)
        outbound = outbound.replace(parameter, component)
    return inbound, outbound


def fixed_router_operation_stages(
  ledger_table_endpoint: str, ledger_table_name: str, api_id: str, gateway_profile_id: str,
  *, logger_id: str = "__LOGGER_ID__", usage_identity: str = "__ROUTER_USAGE_IDENTITY__",
) -> tuple[ET.Element, ET.Element]:
  from copy import deepcopy

  from .apim_budget_execution import pre_dispatch_denial_event

  replacements = {
    "__LEDGER_TABLE_ENDPOINT__": ledger_table_endpoint.rstrip("/"),
    "__LEDGER_TABLE_NAME__": ledger_table_name, "__API_ID__": api_id,
    "__MISS__": "turnstile-router-miss",
  }
  inbound_xml, outbound_xml = _router_engine_templates(gateway_profile_id=gateway_profile_id)
  for name, value in replacements.items():
    inbound_xml = inbound_xml.replace(name, value)
    outbound_xml = outbound_xml.replace(name, value)
  inbound_root = ET.fromstring(inbound_xml)
  branch = next(node for node in inbound_root.iter("when")
          if node.find("set-variable[@name='routerRequestedModel']") is not None)
  inbound = ET.Element("choose")
  branch = deepcopy(branch)
  branch.set("condition", '@(context.Operation.Id == "chat-completions" && '
         'context.Request.Method == "POST" && '
         '!context.Request.Headers.ContainsKey("x-turnstile-router-subrequest"))')
  inbound.append(branch)
  for owner in branch.iter():
    for node in list(owner):
      if node.tag in {"cache-lookup-value", "cache-store-value"}:
        owner.remove(node)
  matched = next(node for node in branch.iter("when") if node.get("condition") ==
           '@((bool)context.Variables["routerMatched"])')
  for node in list(matched):
    if node.find(".//send-request[@response-variable-name='routerAnthropicHttp']") is not None:
      matched.remove(node)
  guard = ET.Element("choose")
  denied = ET.SubElement(guard, "when", {"condition": '@(context.Subscription == null || '
    'string.IsNullOrEmpty(context.Request.Headers.GetValueOrDefault('
    '"Ocp-Apim-Subscription-Key", "")) || '
    '!string.IsNullOrEmpty(context.Request.Headers.GetValueOrDefault("Authorization", "")) || '
    '!string.IsNullOrEmpty(context.Request.Headers.GetValueOrDefault('
    '"x-turnstile-delegation-token", "")))'})
  response = ET.SubElement(denied, "return-response")
  ET.SubElement(response, "set-status", {"code": "403", "reason": "Forbidden"})
  ET.SubElement(response, "set-body").text = (
    '{"error":{"code":"router_subscription_auth_required"}}'
  )
  matched.insert(0, guard)
  valid = ET.Element("set-variable", {"name": "routerNativeDocumentReady", "value": r'''@{
    try {
      var entity = JObject.Parse((string)context.Variables["routerCachedEntityJson"]);
      var document = (string)entity["Document"];
      if ((int?)entity["SchemaVersion"] != 2 || (string)entity["Status"] != "published"
        || (string)entity["PartitionKey"] != "model-router|v1"
        || (string)entity["RowKey"] != (string)context.Variables["routerLookupRowKey"]
        || string.IsNullOrEmpty(document) || Encoding.UTF8.GetByteCount(document) > 65536) { return false; }
      using (var sha = System.Security.Cryptography.SHA256.Create()) {
        var digest = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(document)))
          .Replace("-", "").ToLowerInvariant();
        if (digest != (string)entity["Sha256"]) { return false; }
      }
      var documents = new List<JObject> { JObject.Parse(document) };
      var alternates = (string)entity["AlternateDocuments"];
      if (!string.IsNullOrEmpty(alternates)) {
        foreach (var value in JArray.Parse(alternates)) {
          documents.Add(value is JObject ? (JObject)value : JObject.Parse((string)value));
        }
      }
      var found = false;
      foreach (var item in documents) {
        if ((string)item["gateway_profile_id"] != "__GATEWAY_PROFILE_ID__") { continue; }
        found = true;
        if ((string)item["status"] != "published" || (int?)item["router_version"] < 1
          || (string)item["gateway_profile_id"] != "__GATEWAY_PROFILE_ID__") { return false; }
        var nodes = item["definition"]?["nodes"] as JArray;
        if (nodes == null || nodes.OfType<JObject>().Any(node =>
          node["fallback_on"] is JArray && ((JArray)node["fallback_on"]).Count > 0)) { return false; }
        foreach (var candidate in ((JObject)item["candidates_by_id"]).Properties()) {
          if ((string)candidate.Value["profile"] != "openai-v1") { return false; }
        }
        foreach (var model in ((JObject)item["routing_models_by_id"]).Properties()) {
          if (!new[] { "chat_json", "system_one" }.Contains((string)model.Value["decision_protocol"])
            || (string)model.Value["auth_mode"] != "gateway_route") { return false; }
        }
      }
            return found;
    } catch { return false; }
  }'''.replace("__GATEWAY_PROFILE_ID__", gateway_profile_id)})
  matched.insert(1, valid)
  invalid = ET.Element("choose")
  rejected = ET.SubElement(invalid, "when", {
    "condition": '@(!(bool)context.Variables["routerNativeDocumentReady"])',
  })
  ET.SubElement(rejected, "set-variable", {
    "name": "telemetryUsageId", "value": usage_identity,
  })
  rejected.append(pre_dispatch_denial_event("503", {
    "ingest_error": '"router_pre_dispatch_denial"',
    "model_admission": '"router_configuration_unavailable"',
    "error_message": '"router_configuration_unavailable"',
    "request_id": '(string)context.Variables["routerCallerRequestId"]',
    "model": '(string)context.Variables["routerRequestedModel"]',
    "native_gateway_instance_id": (
      'known ? (string.IsNullOrWhiteSpace(context.Deployment.Gateway.InstanceId) '
      '? context.Deployment.Gateway.Id : context.Deployment.Gateway.InstanceId) : null'
    ),
    "apim_subscription_id": '(context.Subscription?.Id ?? "").Split(\'/\').Last()',
  }, logger_id=logger_id, gateway_profile_id=gateway_profile_id, request_source=(
    'context.Variables.GetValueOrDefault<string>("telemetryRequestSource", '
    'context.Request.Headers.GetValueOrDefault("x-request-source", "unattributed"))'
  )))
  response = ET.SubElement(rejected, "return-response")
  ET.SubElement(response, "set-status", {"code": "503", "reason": "Service Unavailable"})
  ET.SubElement(response, "set-body").text = ROUTER_REJECTION_BODY.replace(
    "__GATEWAY_PROFILE_ID__", gateway_profile_id,
  )
  matched.insert(2, invalid)
  decision = branch.find(".//send-request[@response-variable-name='routerDecisionHttp']")
  assert decision is not None
  destination = decision.find("set-url")
  assert destination is not None
  destination.text = '''@{
    var st = JObject.Parse((string)context.Variables["routerPlanState"]);
    var routingModel = ((JObject)st["routing_models_by_id"])[(string)st["routing_model_id"] ?? ""] as JObject;
    return "https://" + context.Request.OriginalUrl.Host + "/" + context.Api.Path.Trim('/')
      + ((string)routingModel?["decision_protocol"] == "system_one"
        ? "/providers/microsoft/v1/systemone" : "/chat/completions");
  }'''
  payload = decision.find("set-body")
  assert payload is not None and payload.text is not None
  system_one_model = 'new JProperty("model", upstreamModel),'
  if payload.text.count(system_one_model) != 1:
    raise ValueError("Fixed Router System One payload changed")
  payload.text = payload.text.replace(
    system_one_model, 'new JProperty("model", (string)rmInfo?["apim_alias"] ?? upstreamModel),',
  )
  for node in list(decision):
    if node.tag == "authentication-managed-identity":
      decision.remove(node)
  header = ET.Element("set-header", {
    "name": "Ocp-Apim-Subscription-Key", "exists-action": "override",
  })
  ET.SubElement(header, "value").text = (
    '@(context.Request.Headers.GetValueOrDefault("Ocp-Apim-Subscription-Key", ""))'
  )
  decision.insert(2, header)
  outbound = ET.fromstring(outbound_xml)
  failed = next(node for node in outbound.iter("when")
          if node.get("condition") == "@(context.Response.StatusCode >= 400)")
  attempt = failed.find("set-variable[@name='routerPlanState']")
  assert attempt is not None
  for node in list(failed):
    if node is not attempt:
      failed.remove(node)
  trace = outbound.find(".//send-request[@response-variable-name='routerOutboundTraceWrite']/set-body")
  assert trace is not None
  trace.text = '''@{
    var comp = JObject.Parse((string)context.Variables["routerOutboundCompleted"]);
    var trace = (JObject)comp["trace"];
    trace["native_usage_recorded"] = true;
    return new JObject(
      new JProperty("PartitionKey", "model-router-trace|v1"),
      new JProperty("RowKey", (string)context.Variables["routerCallerRequestId"]),
      new JProperty("SchemaVersion", 2),
      new JProperty("Document", trace.ToString(Newtonsoft.Json.Formatting.None))
    ).ToString(Newtonsoft.Json.Formatting.None);
  }'''
  requests = {node.get("response-variable-name") for node in inbound.iter("send-request")}
  if requests != {"routerLookupRead", "routerStickyTableResp", "routerDecisionHttp"}:
    raise ValueError("Fixed Router contains an unsupported request path")
  parents = {child: owner for owner in inbound.iter() for child in owner}
  for rejected in list(inbound.iter("return-response")):
    correlation = ET.Element("set-header", {"name": "x-request-id", "exists-action": "override"})
    ET.SubElement(correlation, "value").text = (
      '@(context.Variables.GetValueOrDefault<string>("routerCallerRequestId", '
      'context.RequestId.ToString()))'
    )
    rejected.insert(1, correlation)
    diagnostic = ET.Element("trace", {"source": "turnstile-native-router", "severity": "error"})
    ET.SubElement(diagnostic, "message").text = (
      '@("Native Router rejected request_id=" + '
      'context.Variables.GetValueOrDefault<string>("routerCallerRequestId", '
      'context.RequestId.ToString()))'
    )
    owner = parents[rejected]
    owner.insert(list(owner).index(rejected), diagnostic)
  return inbound, outbound


def build_router_global_service_policy(
    base_service_policy_xml: str,
    *,
    ledger_table_endpoint: str,
    ledger_table_name: str,
    api_id: str,
) -> str:
    if "<inbound>" not in base_service_policy_xml or "<outbound>" not in base_service_policy_xml:
        raise ValueError("Service policy must contain inbound and outbound sections")

    replacements = {
        "__LEDGER_TABLE_ENDPOINT__": ledger_table_endpoint.rstrip("/"),
        "__LEDGER_TABLE_NAME__": ledger_table_name,
        "__API_ID__": api_id,
    }
    inbound_xml, outbound_xml = _router_engine_templates()
    for key, val in replacements.items():
        inbound_xml = inbound_xml.replace(key, val)
        outbound_xml = outbound_xml.replace(key, val)

    try:
        root = parse_policy(base_service_policy_xml)
    except Exception:
        # When base_service_policy_xml is in APIM rawxml format (containing unescaped < or >
        # inside C# policy expressions), validate and serialize the router engine fragments
        # with standard XML attribute escaping so single-line C# string literals do not break.
        inbound_serialized = ET.tostring(
            ET.fromstring(inbound_xml), encoding="unicode"
        )
        outbound_serialized = ET.tostring(
            ET.fromstring(outbound_xml), encoding="unicode"
        )
        inbound_snippet = (
            inbound_serialized.removeprefix("<inbound>").removesuffix("</inbound>").strip()
        )
        outbound_snippet = (
            outbound_serialized.removeprefix("<outbound>").removesuffix("</outbound>").strip()
        )
        in_marker = f'<set-variable name="{ROUTER_GLOBAL_POLICY_MARKER}"'
        in_end = base_service_policy_xml.find("</inbound>")
        in_start = base_service_policy_xml.find(in_marker)
        if in_start < 0 or in_start > in_end:
            in_start = in_end
        out_marker = '<choose id="turnstile-router-engine">'
        out_end = base_service_policy_xml.find("</outbound>", in_end)
        out_start = base_service_policy_xml.find(out_marker, in_end)
        if out_start < 0 or out_start > out_end:
            out_start = out_end
        return (
            base_service_policy_xml[:in_start].rstrip()
            + "\n  "
            + inbound_snippet
            + "\n"
            + base_service_policy_xml[in_end:out_start].rstrip()
            + "\n  "
            + outbound_snippet
            + "\n"
            + base_service_policy_xml[out_end:]
        )

    inbound = root.find("inbound")
    outbound = root.find("outbound")
    if inbound is None or outbound is None:
        raise ValueError("Service policy must contain inbound and outbound sections")

    # Remove any previous router engine nodes if re-applying idempotently
    for parent in (inbound, outbound):
        for child in list(parent):
            if child.get("id") == "turnstile-router-engine" or (
                child.tag == "set-variable" and child.get("name") == ROUTER_GLOBAL_POLICY_MARKER
            ):
                parent.remove(child)

    inbound_engine = ET.fromstring(inbound_xml)
    for child in list(inbound_engine):
        inbound.append(child)

    outbound_engine = ET.fromstring(outbound_xml)
    for child in list(outbound_engine):
        outbound.append(child)

    return ET.tostring(root, encoding="unicode")


_INBOUND_ROUTER_ENGINE_XML = r"""<inbound>
  <set-variable name="turnstileRouterEngineVersion" value="@(1)" />
  <choose id="turnstile-router-engine">
    <when condition="@(context.Api.Id.Split(';')[0] == &quot;__API_ID__&quot;)">
      <!-- Support api-key or non-JWT Bearer subscription key header for OpenAI SDK compatibility -->
      <choose>
        <when condition="@(context.Subscription == null &amp;&amp; !context.Request.Headers.ContainsKey(&quot;x-turnstile-subkey-rewritten&quot;))">
          <set-variable name="extractedSubKey" value="@{
            var apiKey = context.Request.Headers.GetValueOrDefault(&quot;api-key&quot;, &quot;&quot;).Trim();
            if (!string.IsNullOrEmpty(apiKey)) { return apiKey; }
            var auth = context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;&quot;).Trim();
            if (auth.StartsWith(&quot;Bearer &quot;, StringComparison.OrdinalIgnoreCase)) {
              var token = auth.Substring(7).Trim();
              if (token.Length &gt;= 24 &amp;&amp; token.Length &lt;= 64 &amp;&amp; !token.Contains(&quot;.&quot;)) { return token; }
            }
            return &quot;&quot;;
          }" />
          <choose>
            <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;extractedSubKey&quot;]))">
              <send-request mode="new" response-variable-name="rewrittenSubKeyResp" timeout="120" ignore-error="true">
                <set-url>@{
                  return "https://" + context.Request.OriginalUrl.Host + context.Request.OriginalUrl.Path + context.Request.OriginalUrl.QueryString;
                }</set-url>
                <set-method>@(context.Request.Method)</set-method>
                <set-header name="Content-Type" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("Content-Type", "application/json"))</value>
                </set-header>
                <set-header name="Ocp-Apim-Subscription-Key" exists-action="override">
                  <value>@((string)context.Variables["extractedSubKey"])</value>
                </set-header>
                <set-header name="x-turnstile-subkey-rewritten" exists-action="override">
                  <value>1</value>
                </set-header>
                <set-header name="x-request-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-request-id", context.RequestId.ToString()))</value>
                </set-header>
                <set-header name="x-session-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-session-id", ""))</value>
                </set-header>
                <set-header name="x-interaction-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-interaction-id", ""))</value>
                </set-header>
                <set-header name="x-initiator" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-initiator", ""))</value>
                </set-header>
                <set-header name="x-session-ttl-seconds" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-session-ttl-seconds", ""))</value>
                </set-header>
                <set-header name="x-task-type" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-task-type", ""))</value>
                </set-header>
                <set-header name="x-turn-index" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-turn-index", ""))</value>
                </set-header>
                <set-header name="x-org-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-org-id", "unattributed"))</value>
                </set-header>
                <set-header name="x-org-name" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-org-name", "unattributed"))</value>
                </set-header>
                <set-header name="x-department-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-department-id", "unattributed"))</value>
                </set-header>
                <set-header name="x-department-name" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-department-name", "unattributed"))</value>
                </set-header>
                <set-header name="x-project-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-project-id", "unattributed"))</value>
                </set-header>
                <set-header name="x-project-name" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-project-name", "unattributed"))</value>
                </set-header>
                <set-header name="x-user-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-user-id", "unattributed"))</value>
                </set-header>
                <set-header name="x-user-name" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-user-name", "unattributed"))</value>
                </set-header>
                <set-header name="x-agent-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-agent-id", "unattributed"))</value>
                </set-header>
                <set-header name="x-agent-name" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-agent-name", "unattributed"))</value>
                </set-header>
                <set-header name="x-request-source" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-request-source", "unattributed"))</value>
                </set-header>
                <set-header name="x-hive-workflow" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-hive-workflow", "unattributed"))</value>
                </set-header>
                <set-header name="x-hive-run-id" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-hive-run-id", ""))</value>
                </set-header>
                <set-header name="x-hive-turn-index" exists-action="override">
                  <value>@(context.Request.Headers.GetValueOrDefault("x-hive-turn-index", ""))</value>
                </set-header>
                <set-body>@{
                  return context.Request.Method == "POST" ? context.Request.Body.As&lt;string&gt;(preserveContent: true) : "";
                }</set-body>
              </send-request>
              <return-response response-variable-name="rewrittenSubKeyResp" />
            </when>
          </choose>
        </when>
      </choose>
      <choose>
        <when condition="@(context.Subscription != null &amp;&amp; context.Request.Headers.ContainsKey(&quot;Authorization&quot;))">
          <choose>
            <when condition="@{
              var auth = context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;&quot;).Trim();
              if (auth.StartsWith(&quot;Bearer &quot;, StringComparison.OrdinalIgnoreCase)) {
                var token = auth.Substring(7).Trim();
                return !token.Contains(&quot;.&quot;);
              }
              return false;
            }">
              <set-header name="Authorization" exists-action="delete" />
            </when>
          </choose>
        </when>
      </choose>

      <!-- Branch 1: GET /v1/models (Expose published Model Routers + Models to consumers; keep probe catalog untouched) -->
      <choose>
        <when condition="@(context.Operation.Id == &quot;models&quot; &amp;&amp; context.Request.Method == &quot;GET&quot; &amp;&amp; (context.Subscription == null || !context.Subscription.Id.EndsWith(&quot;-publisher-probe&quot;, StringComparison.OrdinalIgnoreCase)) &amp;&amp; !context.Request.Headers.ContainsKey(&quot;x-turnstile-availability-probe&quot;) &amp;&amp; !context.Request.Headers.ContainsKey(&quot;x-turnstile-internal-catalog&quot;))">
          <send-request mode="new" response-variable-name="routerInternalCatalogResp" timeout="10" ignore-error="true">
            <set-url>@{
              return "https://" + context.Request.OriginalUrl.Host + "/" + context.Api.Path.Trim('/') + "/v1/models";
            }</set-url>
            <set-method>GET</set-method>
            <set-header name="x-turnstile-internal-catalog" exists-action="override">
              <value>1</value>
            </set-header>
            <set-header name="Ocp-Apim-Subscription-Key" exists-action="override">
              <value>@(context.Request.Headers.GetValueOrDefault("Ocp-Apim-Subscription-Key", ""))</value>
            </set-header>
            <set-header name="Authorization" exists-action="override">
              <value>@(context.Request.Headers.GetValueOrDefault("Authorization", ""))</value>
            </set-header>
            <set-header name="x-turnstile-delegation-token" exists-action="override">
              <value>@(context.Request.Headers.GetValueOrDefault("x-turnstile-delegation-token", ""))</value>
            </set-header>
            <set-header name="x-user-id" exists-action="override">
              <value>@(context.Request.Headers.GetValueOrDefault("x-user-id", ""))</value>
            </set-header>
          </send-request>
          <choose>
            <when condition="@(context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerInternalCatalogResp&quot;, null) != null &amp;&amp; ((IResponse)context.Variables[&quot;routerInternalCatalogResp&quot;]).StatusCode == 200)">
              <send-request mode="new" response-variable-name="routerCatalogTableResp" timeout="3" ignore-error="true">
                <set-url>@{
                  return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__()?$filter=" + Uri.EscapeDataString("PartitionKey eq 'model-router|v1'");
                }</set-url>
                <set-method>GET</set-method>
                <set-header name="Accept" exists-action="override">
                  <value>application/json;odata=nometadata</value>
                </set-header>
                <set-header name="x-ms-version" exists-action="override">
                  <value>2020-12-06</value>
                </set-header>
                <authentication-managed-identity resource="https://storage.azure.com/" />
              </send-request>
              <set-variable name="routerMergedCatalogBody" value="@{
                try {
                  var baseResp = (IResponse)context.Variables[&quot;routerInternalCatalogResp&quot;];
                  var baseObj = JObject.Parse(baseResp.Body.As&lt;string&gt;(preserveContent: true));
                  var baseData = baseObj[&quot;data&quot;] as JArray ?? new JArray();
                  var allowedBaseIds = new HashSet&lt;string&gt;(StringComparer.OrdinalIgnoreCase);
                  foreach (var mTok in baseData) {
                    var mObj = mTok as JObject;
                    if (mObj == null) { continue; }
                    if (!string.IsNullOrEmpty((string)mObj[&quot;model_id&quot;])) { allowedBaseIds.Add((string)mObj[&quot;model_id&quot;]); }
                    if (!string.IsNullOrEmpty((string)mObj[&quot;id&quot;])) { allowedBaseIds.Add((string)mObj[&quot;id&quot;]); }
                  }
                  string callerEmail = &quot;&quot;;
                  bool hasPersonAuth = false;
                  var delegHdr = context.Request.Headers.GetValueOrDefault(&quot;x-turnstile-delegation-token&quot;, &quot;&quot;).Trim();
                  var authHdr = context.Request.Headers.GetValueOrDefault(&quot;Authorization&quot;, &quot;&quot;).Trim();
                  if (!string.IsNullOrEmpty(delegHdr)) {
                    hasPersonAuth = true;
                    string rawTok = delegHdr.StartsWith(&quot;Bearer &quot;, StringComparison.OrdinalIgnoreCase) ? delegHdr.Substring(7).Trim() : delegHdr;
                    if (rawTok.AsJwt() != null) {
                      var rawUid = context.Request.Headers.GetValueOrDefault(&quot;x-user-id&quot;, &quot;&quot;).Trim().ToLowerInvariant();
                      if (!string.IsNullOrEmpty(rawUid) &amp;&amp; rawUid != &quot;unattributed&quot;) {
                        callerEmail = rawUid;
                      }
                    }
                  } else if (authHdr.StartsWith(&quot;Bearer &quot;, StringComparison.OrdinalIgnoreCase)) {
                    var jwt = authHdr.Substring(7).Trim().AsJwt();
                    if (jwt != null) {
                      hasPersonAuth = true;
                      callerEmail = (jwt.Claims.GetValueOrDefault(&quot;preferred_username&quot;,
                        jwt.Claims.GetValueOrDefault(&quot;upn&quot;,
                          jwt.Claims.GetValueOrDefault(&quot;email&quot;, &quot;&quot;))) ?? &quot;&quot;).Trim().ToLowerInvariant();
                    }
                  }
                  string rawSubId = context.Subscription != null ? (context.Subscription.Id ?? &quot;&quot;) : &quot;&quot;;
                  int subSlash = rawSubId.LastIndexOf('/');
                  string subKeyId = (subSlash &gt;= 0 ? rawSubId.Substring(subSlash + 1) : rawSubId).Trim().ToLowerInvariant();
                  var merged = new JArray();
                  var tableResp = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerCatalogTableResp&quot;, null);
                  if (tableResp != null &amp;&amp; tableResp.StatusCode == 200) {
                    var entities = JObject.Parse(tableResp.Body.As&lt;string&gt;(preserveContent: true))[&quot;value&quot;] as JArray ?? new JArray();
                    var routerItems = new List&lt;JObject&gt;();
                    var seenRouterIds = new HashSet&lt;string&gt;(StringComparer.OrdinalIgnoreCase);
                    foreach (var token in entities) {
                      var ent = token as JObject;
                      if (ent == null || (bool?)ent[&quot;PrimaryEntry&quot;] != true || (string)ent[&quot;Status&quot;] != &quot;published&quot;) { continue; }
                      var candidateDocs = new List&lt;JObject&gt;();
                      var docStr = (string)ent[&quot;Document&quot;];
                      if (!string.IsNullOrEmpty(docStr)) {
                        try { candidateDocs.Add(JObject.Parse(docStr)); } catch {}
                      }
                      var altStr = (string)ent[&quot;AlternateDocuments&quot;];
                      if (!string.IsNullOrEmpty(altStr)) {
                        try {
                          var altArr = JArray.Parse(altStr);
                          foreach (var altTok in altArr) {
                            var altObj = altTok as JObject;
                            if (altObj != null) { candidateDocs.Add(altObj); }
                            else if (altTok != null &amp;&amp; altTok.Type == JTokenType.String &amp;&amp; !string.IsNullOrEmpty((string)altTok)) {
                              candidateDocs.Add(JObject.Parse((string)altTok));
                            }
                          }
                        } catch {}
                      }
                      foreach (var rDoc in candidateDocs) {
                        string rid = ((string)rDoc[&quot;router_id&quot;] ?? &quot;&quot;).Trim();
                        if (!string.IsNullOrEmpty(rid) &amp;&amp; !seenRouterIds.Add(rid)) { continue; }
                        if (hasPersonAuth) {
                          if (string.IsNullOrEmpty(callerEmail)) { continue; }
                          var assignedArr = rDoc[&quot;assigned_user_ids&quot;] as JArray;
                          string ownerEmail = ((string)rDoc[&quot;owner_email&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
                          bool userAllowed = (!string.IsNullOrEmpty(ownerEmail) &amp;&amp; string.Equals(ownerEmail, callerEmail, StringComparison.OrdinalIgnoreCase))
                            || (assignedArr != null &amp;&amp; assignedArr.Values&lt;string&gt;().Any(u =&gt; string.Equals((u ?? &quot;&quot;).Trim(), callerEmail, StringComparison.OrdinalIgnoreCase)));
                          if (!userAllowed) { continue; }
                        } else {
                          var allowedSubs = rDoc[&quot;allowed_subscription_ids&quot;] as JArray;
                          if (allowedSubs == null || string.IsNullOrEmpty(subKeyId) || !allowedSubs.Values&lt;string&gt;().Any(s =&gt; string.Equals((s ?? &quot;&quot;).Trim(), subKeyId, StringComparison.OrdinalIgnoreCase))) {
                            continue;
                          }
                          if (allowedBaseIds.Count &gt; 0 &amp;&amp; rDoc[&quot;candidate_model_ids&quot;] is JArray candIdsArr &amp;&amp; candIdsArr.Count &gt; 0) {
                            var candsMap = rDoc[&quot;candidates_by_id&quot;] as JObject;
                            bool allCandsAllowed = true;
                            foreach (var cidTok in candIdsArr) {
                              string key = (string)cidTok ?? &quot;&quot;;
                              if (allowedBaseIds.Contains(key)) { continue; }
                              var cObj = candsMap != null ? (candsMap[key] as JObject) : null;
                              string alias = cObj != null ? ((string)cObj[&quot;apim_alias&quot;] ?? &quot;&quot;) : &quot;&quot;;
                              string mKey = cObj != null ? ((string)cObj[&quot;model_key&quot;] ?? &quot;&quot;) : &quot;&quot;;
                              if ((!string.IsNullOrEmpty(alias) &amp;&amp; allowedBaseIds.Contains(alias))
                                  || (!string.IsNullOrEmpty(mKey) &amp;&amp; allowedBaseIds.Contains(mKey))) {
                                continue;
                              }
                              allCandsAllowed = false;
                              break;
                            }
                            if (!allCandsAllowed) {
                              continue;
                            }
                          }
                        }
                        var item = new JObject(
                          new JProperty(&quot;id&quot;, (string)rDoc[&quot;router_name&quot;]),
                          new JProperty(&quot;object&quot;, &quot;model&quot;),
                          new JProperty(&quot;type&quot;, &quot;model_router&quot;),
                          new JProperty(&quot;created_at&quot;, (string)rDoc[&quot;published_at&quot;] ?? (string)rDoc[&quot;created_at&quot;]),
                          new JProperty(&quot;display_name&quot;, (string)rDoc[&quot;router_name&quot;]),
                          new JProperty(&quot;owned_by&quot;, &quot;turnstile-model-router&quot;),
                          new JProperty(&quot;router_id&quot;, (string)rDoc[&quot;router_id&quot;]),
                          new JProperty(&quot;router_version&quot;, (int?)rDoc[&quot;router_version&quot;] ?? 1),
                          new JProperty(&quot;description&quot;, (string)rDoc[&quot;description&quot;] ?? &quot;&quot;),
                          new JProperty(&quot;strategy_types&quot;, rDoc[&quot;strategy_types&quot;] ?? new JArray()),
                          new JProperty(&quot;condition_fields&quot;, rDoc[&quot;condition_fields&quot;] ?? new JArray()),
                          new JProperty(&quot;candidate_models&quot;, rDoc[&quot;candidate_models&quot;] ?? new JArray())
                        );
                        routerItems.Add(item);
                      }
                    }
                    foreach (var rItem in routerItems.OrderBy(x =&gt; (string)x[&quot;id&quot;], StringComparer.OrdinalIgnoreCase)) {
                      merged.Add(rItem);
                    }
                  }
                  foreach (var mToken in baseData) {
                    var mObj = mToken as JObject;
                    if (mObj == null) { continue; }
                    var clone = (JObject)mObj.DeepClone();
                    if (clone[&quot;object&quot;] == null) { clone[&quot;object&quot;] = &quot;model&quot;; }
                    if (clone[&quot;owned_by&quot;] == null) { clone[&quot;owned_by&quot;] = &quot;turnstile&quot;; }
                    merged.Add(clone);
                  }
                  var result = new JObject(
                    new JProperty(&quot;object&quot;, &quot;list&quot;),
                    new JProperty(&quot;data&quot;, merged),
                    new JProperty(&quot;has_more&quot;, false),
                    new JProperty(&quot;first_id&quot;, merged.Count == 0 ? JValue.CreateNull() : merged[0][&quot;id&quot;]),
                    new JProperty(&quot;last_id&quot;, merged.Count == 0 ? JValue.CreateNull() : merged[merged.Count - 1][&quot;id&quot;])
                  );
                  return result.ToString(Newtonsoft.Json.Formatting.None);
                } catch {
                  return &quot;&quot;;
                }
              }" />
              <choose>
                <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;routerMergedCatalogBody&quot;]))">
                  <return-response>
                    <set-status code="200" reason="OK" />
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-header name="Cache-Control" exists-action="override">
                      <value>no-store</value>
                    </set-header>
                    <set-header name="x-auth-method" exists-action="override">
                      <value>@(context.Subscription != null ? "subscription_key" : "entra")</value>
                    </set-header>
                    <set-header name="x-apim-subscription-id" exists-action="override">
                      <value>@(context.Subscription != null ? context.Subscription.Id : "")</value>
                    </set-header>
                    <set-body>@((string)context.Variables["routerMergedCatalogBody"])</set-body>
                  </return-response>
                </when>
              </choose>
            </when>
          </choose>
        </when>
      </choose>

      <!-- Branch 2: POST /chat/completions or /responses (Native APIM Model Router Execution) -->
      <choose>
        <when condition="@((context.Operation.Id == &quot;chat-completions&quot; || context.Operation.Id == &quot;responses&quot;) &amp;&amp; context.Request.Method == &quot;POST&quot; &amp;&amp; !context.Request.Headers.ContainsKey(&quot;x-turnstile-router-subrequest&quot;))">
          <set-variable name="routerCallerRequestId" value="@{
            var reqId = context.Request.Headers.GetValueOrDefault(&quot;x-request-id&quot;, &quot;&quot;).Trim();
            return string.IsNullOrEmpty(reqId) ? context.RequestId.ToString() : reqId;
          }" />
          <set-header name="x-request-id" exists-action="override">
            <value>@((string)context.Variables["routerCallerRequestId"])</value>
          </set-header>
          <set-variable name="routerRequestedModel" value="@{
            try {
              var b = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
              return ((string)b[&quot;model&quot;] ?? &quot;&quot;).Trim();
            } catch { return &quot;&quot;; }
          }" />
          <set-variable name="routerLookupRowKey" value="@{
            var m = (string)context.Variables[&quot;routerRequestedModel&quot;];
            if (string.IsNullOrEmpty(m)) { return &quot;&quot;; }
            using (var sha = System.Security.Cryptography.SHA256.Create()) {
              return &quot;router:&quot; + BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(m.ToLowerInvariant()))).Replace(&quot;-&quot;, &quot;&quot;).ToLowerInvariant();
            }
          }" />
          <choose>
            <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;routerLookupRowKey&quot;]))">
              <cache-lookup-value key="@(&quot;turnstile-router-def:&quot; + (string)context.Variables[&quot;routerLookupRowKey&quot;])" variable-name="routerCachedEntityJson" />
              <choose>
                <when condition="@(!context.Variables.ContainsKey(&quot;routerCachedEntityJson&quot;))">
                  <send-request mode="new" response-variable-name="routerLookupRead" timeout="2" ignore-error="true">
                    <set-url>@{
                      return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='model-router%7Cv1',RowKey='" + Uri.EscapeDataString((string)context.Variables["routerLookupRowKey"]) + "')";
                    }</set-url>
                    <set-method>GET</set-method>
                    <set-header name="Accept" exists-action="override">
                      <value>application/json;odata=nometadata</value>
                    </set-header>
                    <set-header name="x-ms-version" exists-action="override">
                      <value>2020-12-06</value>
                    </set-header>
                    <authentication-managed-identity resource="https://storage.azure.com/" />
                  </send-request>
                  <set-variable name="routerCachedEntityJson" value="@{
                    var r = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerLookupRead&quot;, null);
                    if (r == null) { return &quot;&quot;; }
                    if (r.StatusCode == 200) { return r.Body.As&lt;string&gt;(preserveContent: true); }
                    if (r.StatusCode == 404) { return &quot;__MISS__&quot;; }
                    return &quot;&quot;;
                  }" />
                  <choose>
                    <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;routerCachedEntityJson&quot;]))">
                      <cache-store-value key="@(&quot;turnstile-router-def:&quot; + (string)context.Variables[&quot;routerLookupRowKey&quot;])" value="@((string)context.Variables[&quot;routerCachedEntityJson&quot;])" duration="10" />
                    </when>
                  </choose>
                </when>
              </choose>
            </when>
          </choose>
          <set-variable name="routerMatched" value="@{
            var cached = context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerCachedEntityJson&quot;, &quot;&quot;);
            return !string.IsNullOrEmpty(cached) &amp;&amp; cached != &quot;__MISS__&quot;;
          }" />

          <!-- If NOT a Router, still strip router_context if present so upstream provider does not reject it -->
          <choose>
            <when condition="@(!(bool)context.Variables[&quot;routerMatched&quot;])">
              <set-body>@{
                try {
                  var b = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
                  if (b[&quot;router_context&quot;] != null) {
                    b.Remove(&quot;router_context&quot;);
                  }
                  return b.ToString(Newtonsoft.Json.Formatting.None);
                } catch {
                  return context.Request.Body.As&lt;string&gt;(preserveContent: true);
                }
              }</set-body>
            </when>
          </choose>

          <!-- If Matched a Published Model Router: Execute Graph, Sticky Lookup, Model-Based / Weighted / Priority / Cost-Guard -->
          <choose>
            <when condition="@((bool)context.Variables[&quot;routerMatched&quot;])">
              <set-variable name="routerPlanState" value="@{
                try {
                  var entity = JObject.Parse((string)context.Variables[&quot;routerCachedEntityJson&quot;]);
                  var routerDoc = JObject.Parse((string)entity[&quot;Document&quot;]);
                  var reqBody = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
                  var rCtx = reqBody[&quot;router_context&quot;] as JObject ?? new JObject();
                  var messages = (reqBody[&quot;messages&quot;] as JArray) ?? (reqBody[&quot;input&quot;] as JArray) ?? new JArray();

                  string lastUserPrompt = &quot;&quot;;
                  string firstUserPrompt = &quot;&quot;;
                  string prevUserPrompt = &quot;&quot;;
                  string prevAssistantPrompt = &quot;&quot;;
                  string pendingAssistantText = &quot;&quot;;
                  string lastNonSystemRole = &quot;&quot;;
                  bool lastIsToolContinuation = false;
                  int userMsgCount = 0;
                  bool hasToolMsgs = false;
                  bool hasUserRequestTag = false;
                  var sysParts = new List&lt;string&gt;();
                  var sdkPrefixMarkers = new string[] {
                    &quot;&lt;/system_notifications&gt;&quot;,
                    &quot;&lt;/custom_instructions&gt;&quot;,
                    &quot;&lt;/tools&gt;&quot;,
                    &quot;&lt;/environment_limitations&gt;&quot;,
                    &quot;&lt;/code_change_instructions&gt;&quot;
                  };
                  var sdkSuffixMarkers = new string[] {
                    &quot;&lt;memories&gt;&quot;,
                    &quot;&lt;session_search_context&gt;&quot;,
                    &quot;&lt;session_context&gt;&quot;,
                    &quot;&lt;task_completion&gt;&quot;,
                    &quot;Your goal is to deliver complete, working solutions.&quot;,
                    &quot;Use only tools exposed by this application.&quot;,
                    &quot;Current page context:&quot;,
                    &quot;Conversation history:&quot;
                  };

                  if (reqBody[&quot;instructions&quot;]?.Type == JTokenType.String) {
                    string rawInst = (string)reqBody[&quot;instructions&quot;] ?? &quot;&quot;;
                    int maxPre = -1;
                    foreach (var pm in sdkPrefixMarkers) {
                      int pIdx = rawInst.LastIndexOf(pm, StringComparison.Ordinal);
                      if (pIdx &gt;= 0 &amp;&amp; pIdx + pm.Length &gt; maxPre) { maxPre = pIdx + pm.Length; }
                    }
                    if (maxPre &gt;= 0) { rawInst = rawInst.Substring(maxPre); }
                    int minSuf = rawInst.Length;
                    foreach (var sm in sdkSuffixMarkers) {
                      int sIdx = rawInst.IndexOf(sm, StringComparison.Ordinal);
                      if (sIdx &gt;= 0 &amp;&amp; sIdx &lt; minSuf) { minSuf = sIdx; }
                    }
                    rawInst = rawInst.Substring(0, minSuf);
                    rawInst = System.Text.RegularExpressions.Regex.Replace(rawInst, &quot;&lt;context&gt;[\\s\\S]*?&lt;/context&gt;&quot;, &quot; &quot;, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                    rawInst = System.Text.RegularExpressions.Regex.Replace(rawInst, &quot;&lt;current_datetime&gt;[\\s\\S]*?&lt;/current_datetime&gt;&quot;, &quot; &quot;, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                    rawInst = System.Text.RegularExpressions.Regex.Replace(rawInst, &quot;\\s+&quot;, &quot; &quot;).Trim();
                    if (!string.IsNullOrEmpty(rawInst)) {
                      sysParts.Add(rawInst);
                    }
                  }

                  if (reqBody[&quot;input&quot;]?.Type == JTokenType.String) {
                    userMsgCount = 1;
                    firstUserPrompt = ((string)reqBody[&quot;input&quot;] ?? &quot;&quot;).Trim();
                    lastUserPrompt = firstUserPrompt;
                    lastNonSystemRole = &quot;user&quot;;
                  }

                  foreach (var mTok in messages) {
                    var mObj = mTok as JObject;
                    if (mObj == null) { continue; }
                    var role = ((string)mObj[&quot;role&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
                    if (role == &quot;system&quot; || role == &quot;developer&quot;) {
                      var rawSys = &quot;&quot;;
                      if (mObj[&quot;content&quot;]?.Type == JTokenType.String) {
                        rawSys = (string)mObj[&quot;content&quot;] ?? &quot;&quot;;
                      } else if (mObj[&quot;content&quot;] is JArray) {
                        rawSys = string.Join(&quot; &quot;, ((JArray)mObj[&quot;content&quot;]).OfType&lt;JObject&gt;()
                          .Where(p =&gt; (string)p[&quot;type&quot;] == null || (string)p[&quot;type&quot;] == &quot;text&quot; || (string)p[&quot;type&quot;] == &quot;input_text&quot;)
                          .Select(p =&gt; (string)p[&quot;text&quot;] ?? &quot;&quot;));
                      }
                      int maxPre = -1;
                      foreach (var pm in sdkPrefixMarkers) {
                        int pIdx = rawSys.LastIndexOf(pm, StringComparison.Ordinal);
                        if (pIdx &gt;= 0 &amp;&amp; pIdx + pm.Length &gt; maxPre) { maxPre = pIdx + pm.Length; }
                      }
                      if (maxPre &gt;= 0) { rawSys = rawSys.Substring(maxPre); }
                      int minSuf = rawSys.Length;
                      foreach (var sm in sdkSuffixMarkers) {
                        int sIdx = rawSys.IndexOf(sm, StringComparison.Ordinal);
                        if (sIdx &gt;= 0 &amp;&amp; sIdx &lt; minSuf) { minSuf = sIdx; }
                      }
                      rawSys = rawSys.Substring(0, minSuf);
                      rawSys = System.Text.RegularExpressions.Regex.Replace(rawSys, &quot;&lt;context&gt;[\\s\\S]*?&lt;/context&gt;&quot;, &quot; &quot;, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                      rawSys = System.Text.RegularExpressions.Regex.Replace(rawSys, &quot;&lt;current_datetime&gt;[\\s\\S]*?&lt;/current_datetime&gt;&quot;, &quot; &quot;, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                      rawSys = System.Text.RegularExpressions.Regex.Replace(rawSys, &quot;\\s+&quot;, &quot; &quot;).Trim();
                      if (!string.IsNullOrEmpty(rawSys)) {
                        sysParts.Add(rawSys);
                      }
                      continue;
                    }
                    if (!string.IsNullOrEmpty(role)) {
                      lastNonSystemRole = role;
                    }
                    string itemType = ((string)mObj[&quot;type&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
                    var itemBlocks = mObj[&quot;content&quot;] as JArray;
                    lastIsToolContinuation = itemType == &quot;function_call_output&quot; || itemType == &quot;function_call&quot;
                      || (role == &quot;user&quot; &amp;&amp; itemBlocks != null &amp;&amp; itemBlocks.Count &gt; 0
                        &amp;&amp; itemBlocks.All(block =&gt; block is JObject &amp;&amp; (string)block[&quot;type&quot;] == &quot;tool_result&quot;));
                    if (role == &quot;tool&quot; || role == &quot;assistant&quot;) {
                      hasToolMsgs = true;
                    }
                    if (role == &quot;assistant&quot;) {
                      var rawAsst = &quot;&quot;;
                      if (mObj[&quot;content&quot;]?.Type == JTokenType.String) {
                        rawAsst = (string)mObj[&quot;content&quot;] ?? &quot;&quot;;
                      } else if (mObj[&quot;content&quot;] is JArray) {
                        rawAsst = string.Join(&quot; &quot;, ((JArray)mObj[&quot;content&quot;]).OfType&lt;JObject&gt;()
                          .Where(p =&gt; (string)p[&quot;type&quot;] == null || (string)p[&quot;type&quot;] == &quot;text&quot; || (string)p[&quot;type&quot;] == &quot;output_text&quot;)
                          .Select(p =&gt; (string)p[&quot;text&quot;] ?? &quot;&quot;));
                      }
                      rawAsst = rawAsst.Trim();
                      if (!string.IsNullOrEmpty(rawAsst)) {
                        pendingAssistantText = rawAsst;
                      }
                    } else if (role == &quot;user&quot;) {
                      var rawContent = &quot;&quot;;
                      if (mObj[&quot;content&quot;]?.Type == JTokenType.String) {
                        rawContent = (string)mObj[&quot;content&quot;] ?? &quot;&quot;;
                      } else if (mObj[&quot;content&quot;] is JArray) {
                        rawContent = string.Join(&quot; &quot;, ((JArray)mObj[&quot;content&quot;]).OfType&lt;JObject&gt;()
                          .Where(p =&gt; (string)p[&quot;type&quot;] == null || (string)p[&quot;type&quot;] == &quot;text&quot; || (string)p[&quot;type&quot;] == &quot;input_text&quot;)
                          .Select(p =&gt; (string)p[&quot;text&quot;] ?? &quot;&quot;));
                      }
                      var extracted = rawContent;
                      int startTag = rawContent.LastIndexOf(&quot;&lt;userRequest&gt;&quot;, StringComparison.Ordinal);
                      int endTag = rawContent.LastIndexOf(&quot;&lt;/userRequest&gt;&quot;, StringComparison.Ordinal);
                      if (startTag &gt;= 0 &amp;&amp; endTag &gt; startTag + 13) {
                        hasUserRequestTag = true;
                        extracted = rawContent.Substring(startTag + 13, endTag - (startTag + 13)).Trim();
                      }
                      extracted = System.Text.RegularExpressions.Regex.Replace(extracted, &quot;&lt;current_datetime&gt;[\\s\\S]*?&lt;/current_datetime&gt;\\s*&quot;, &quot;&quot;, System.Text.RegularExpressions.RegexOptions.IgnoreCase).Trim();
                      int umStart = extracted.IndexOf(&quot;&lt;user_message&gt;&quot;, StringComparison.OrdinalIgnoreCase);
                      int umEnd = extracted.LastIndexOf(&quot;&lt;/user_message&gt;&quot;, StringComparison.OrdinalIgnoreCase);
                      if (umStart &gt;= 0 &amp;&amp; umEnd &gt; umStart + 14) {
                        string innerUm = extracted.Substring(umStart + 14, umEnd - (umStart + 14)).Trim();
                        if (innerUm.Length &gt; 600) {
                          string boundedUm = innerUm.Substring(0, 300).TrimEnd() + &quot; ... &quot; + innerUm.Substring(innerUm.Length - 150).TrimStart();
                          extracted = extracted.Substring(0, umStart + 14) + &quot;\n&quot; + boundedUm + &quot;\n&quot; + extracted.Substring(umEnd);
                        }
                      }
                      extracted = extracted.Trim();
                      if (!string.IsNullOrEmpty(extracted)) {
                        userMsgCount++;
                        if (userMsgCount == 1) {
                          firstUserPrompt = extracted;
                        } else {
                          prevUserPrompt = lastUserPrompt;
                          prevAssistantPrompt = pendingAssistantText;
                        }
                        lastUserPrompt = extracted;
                      }
                    }
                  }

                  string systemSummary = &quot;&quot;;
                  if (!hasUserRequestTag &amp;&amp; sysParts.Count &gt; 0) {
                    string mergedSys = string.Join(&quot; &quot;, sysParts).Trim();
                    systemSummary = mergedSys.Length &lt;= 600
                      ? mergedSys
                      : (mergedSys.Substring(0, 420).TrimEnd() + &quot; ... &quot; + mergedSys.Substring(mergedSys.Length - 140).TrimStart());
                  }

                  var constraintParts = new List&lt;string&gt;();
                  int? reqMaxTok = (int?)reqBody[&quot;max_completion_tokens&quot;] ?? (int?)reqBody[&quot;max_tokens&quot;] ?? (int?)reqBody[&quot;max_output_tokens&quot;];
                  if (reqMaxTok.HasValue &amp;&amp; reqMaxTok.Value &gt; 0 &amp;&amp; reqMaxTok.Value &lt;= 128) {
                    constraintParts.Add(&quot;max_output_tokens=&quot; + reqMaxTok.Value.ToString());
                  }
                  string respFmt = reqBody[&quot;response_format&quot;]?.Type == JTokenType.String
                    ? (string)reqBody[&quot;response_format&quot;]
                    : (string)reqBody[&quot;response_format&quot;]?[&quot;type&quot;];
                  if (!string.IsNullOrWhiteSpace(respFmt) &amp;&amp; !string.Equals(respFmt.Trim(), &quot;text&quot;, StringComparison.OrdinalIgnoreCase)) {
                    constraintParts.Add(&quot;response_format=&quot; + respFmt.Trim());
                  }
                  var reqTools = reqBody[&quot;tools&quot;] as JArray;
                  if (reqTools != null &amp;&amp; reqTools.Count &gt; 0) {
                    constraintParts.Add(&quot;tools=&quot; + reqTools.Count.ToString());
                  }
                  string requestConstraints = string.Join(&quot;, &quot;, constraintParts);

                  bool hasMultiTurn = userMsgCount &gt; 1 &amp;&amp; !string.IsNullOrEmpty(prevUserPrompt) &amp;&amp; !string.Equals(prevUserPrompt, lastUserPrompt, StringComparison.Ordinal);
                  string decisionInput = lastUserPrompt;
                  if (!string.IsNullOrEmpty(systemSummary) || !string.IsNullOrEmpty(requestConstraints) || hasMultiTurn) {
                    var sections = new List&lt;string&gt;();
                    if (!string.IsNullOrEmpty(systemSummary)) {
                      sections.Add(&quot;Task / System instruction:\n&quot; + systemSummary);
                    }
                    if (!string.IsNullOrEmpty(requestConstraints)) {
                      sections.Add(&quot;Request constraints: &quot; + requestConstraints);
                    }
                    sections.Add(&quot;Current user request:\n&quot; + lastUserPrompt);
                    if (hasMultiTurn) {
                      var sb = new StringBuilder();
                      sb.Append(&quot;Prior turn context (for reference only if the current request is a follow-up):\n&quot;);
                      if (userMsgCount &gt; 2 &amp;&amp; !string.IsNullOrEmpty(firstUserPrompt) &amp;&amp; !string.Equals(firstUserPrompt, prevUserPrompt, StringComparison.Ordinal)) {
                        string firstClean = firstUserPrompt.Replace(&quot;\r&quot;, &quot; &quot;).Replace(&quot;\n&quot;, &quot; &quot;).Trim();
                        sb.Append(&quot;- Initial topic: &quot;).Append(firstClean.Length &gt; 200 ? firstClean.Substring(0, 200) + &quot;...&quot; : firstClean).Append(&quot;\n&quot;);
                      }
                      string prevUserClean = prevUserPrompt.Replace(&quot;\r&quot;, &quot; &quot;).Replace(&quot;\n&quot;, &quot; &quot;).Trim();
                      sb.Append(&quot;- Previous user message: &quot;).Append(prevUserClean.Length &gt; 240 ? prevUserClean.Substring(0, 240) + &quot;...&quot; : prevUserClean);
                      if (!string.IsNullOrEmpty(prevAssistantPrompt)) {
                        string prevAsstClean = prevAssistantPrompt.Replace(&quot;\r&quot;, &quot; &quot;).Replace(&quot;\n&quot;, &quot; &quot;).Trim();
                        sb.Append(&quot;\n- Previous assistant reply: &quot;).Append(prevAsstClean.Length &gt; 240 ? prevAsstClean.Substring(0, 240) + &quot;...&quot; : prevAsstClean);
                      }
                      sections.Add(sb.ToString());
                    }
                    decisionInput = string.Join(&quot;\n\n&quot;, sections);
                  }

                  int promptLength = lastUserPrompt.Length;
                  string taskType = context.Request.Headers.GetValueOrDefault(&quot;x-task-type&quot;,
                    context.Request.Headers.GetValueOrDefault(&quot;x-hive-workflow&quot;, (string)rCtx[&quot;task_type&quot;] ?? &quot;&quot;)).Trim();
                  string initiator = context.Request.Headers.GetValueOrDefault(&quot;x-initiator&quot;, (string)rCtx[&quot;x_initiator&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
                  string interactionId = context.Request.Headers.GetValueOrDefault(&quot;x-interaction-id&quot;,
                    context.Request.Headers.GetValueOrDefault(&quot;x-conversation-id&quot;,
                      context.Request.Headers.GetValueOrDefault(&quot;x-copilot-interaction-id&quot;, (string)rCtx[&quot;interaction_id&quot;] ?? &quot;&quot;))).Trim();
                  string sessionId = context.Request.Headers.GetValueOrDefault(&quot;x-session-id&quot;, (string)rCtx[&quot;session_id&quot;] ?? &quot;&quot;).Trim();
                  __ROUTER_PERSON_IDENTITY__
                  string rawSubId = context.Subscription != null ? (context.Subscription.Id ?? &quot;&quot;) : &quot;&quot;;
                  int subSlash = rawSubId.LastIndexOf('/');
                  string subKeyId = (subSlash &gt;= 0 ? rawSubId.Substring(subSlash + 1) : rawSubId).Trim().ToLowerInvariant();
                  var candidateRouterDocs = new List&lt;JObject&gt;() { routerDoc };
                  var altDocsStr = (string)entity[&quot;AlternateDocuments&quot;];
                  if (!string.IsNullOrEmpty(altDocsStr)) {
                    try {
                      var altArr = JArray.Parse(altDocsStr);
                      foreach (var altTok in altArr) {
                        var altObj = altTok as JObject;
                        if (altObj != null) { candidateRouterDocs.Add(altObj); }
                        else if (altTok != null &amp;&amp; altTok.Type == JTokenType.String &amp;&amp; !string.IsNullOrEmpty((string)altTok)) {
                          candidateRouterDocs.Add(JObject.Parse((string)altTok));
                        }
                      }
                    } catch {}
                  }
                  JObject authorizedDoc = null;
                  foreach (var candDoc in candidateRouterDocs) {__ROUTER_CANDIDATE_TARGET__
                    if (hasPersonAuth) {
                      if (string.IsNullOrEmpty(callerEmail)) { continue; }
                      var assignedArr = candDoc[&quot;assigned_user_ids&quot;] as JArray;
                      string ownerEmail = ((string)candDoc[&quot;owner_email&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
                      bool userAllowed = (!string.IsNullOrEmpty(ownerEmail) &amp;&amp; string.Equals(ownerEmail, callerEmail, StringComparison.OrdinalIgnoreCase))
                        || (assignedArr != null &amp;&amp; assignedArr.Values&lt;string&gt;().Any(u =&gt; string.Equals((u ?? &quot;&quot;).Trim(), callerEmail, StringComparison.OrdinalIgnoreCase)));
                      if (userAllowed) {
                        authorizedDoc = candDoc;
                        break;
                      }
                    } else {
                      if (string.IsNullOrEmpty(subKeyId)) { continue; }
                      var allowedSubs = candDoc[&quot;allowed_subscription_ids&quot;] as JArray;
                      if (allowedSubs != null &amp;&amp; allowedSubs.Values&lt;string&gt;().Any(s =&gt; string.Equals((s ?? &quot;&quot;).Trim(), subKeyId, StringComparison.OrdinalIgnoreCase))) {
                        authorizedDoc = candDoc;
                        break;
                      }
                    }
                  }
                  if (authorizedDoc == null) {
                    return new JObject(new JProperty(&quot;access_denied&quot;, true)).ToString(Newtonsoft.Json.Formatting.None);
                  }
                  routerDoc = authorizedDoc;
                  string consumerKey = (!string.IsNullOrEmpty(callerEmail)
                    ? callerEmail
                    : (!string.IsNullOrEmpty(subKeyId) ? subKeyId : &quot;anonymous&quot;)).ToLowerInvariant();

                  int turnIndex = Math.Max(1, userMsgCount);
                  int parsedTurn;
                  var rawTurn = context.Request.Headers.GetValueOrDefault(&quot;x-turn-index&quot;,
                    context.Request.Headers.GetValueOrDefault(&quot;x-hive-turn-index&quot;, (string)rCtx[&quot;turn_index&quot;] ?? &quot;&quot;));
                  if (int.TryParse(rawTurn, out parsedTurn) &amp;&amp; parsedTurn &gt;= 1) { turnIndex = parsedTurn; }

                  int customTtlSeconds = 0;
                  int parsedTtl;
                  var rawTtl = context.Request.Headers.GetValueOrDefault(&quot;x-session-ttl-seconds&quot;, (string)rCtx[&quot;session_ttl_seconds&quot;] ?? &quot;&quot;);
                  if (int.TryParse(rawTtl, out parsedTtl)) {
                    customTtlSeconds = Math.Max(1, Math.Min(86400, parsedTtl));
                  }
                  int sessionTtlSeconds = customTtlSeconds &gt; 0 ? customTtlSeconds : 1800;
                  int interactionTtlSeconds = customTtlSeconds &gt; 0 ? customTtlSeconds : 300;
                  int ttlSeconds = !string.IsNullOrEmpty(sessionId) ? sessionTtlSeconds : interactionTtlSeconds;

                  string runId = !string.IsNullOrEmpty(interactionId) ? interactionId
                    : (!string.IsNullOrEmpty(sessionId) ? sessionId
                      : context.Request.Headers.GetValueOrDefault(&quot;x-run-id&quot;,
                        context.Request.Headers.GetValueOrDefault(&quot;x-hive-run-id&quot;, (string)context.Variables[&quot;routerCallerRequestId&quot;])));

                  /* Walk Router Graph (support any start node ID by matching type == start) */
                  var def = (JObject)routerDoc[&quot;definition&quot;];
                  var nodes = (JArray)def[&quot;nodes&quot;];
                  var edges = (JArray)def[&quot;edges&quot;];
                  var nodesById = nodes.OfType&lt;JObject&gt;().ToDictionary(n =&gt; (string)n[&quot;id&quot;], n =&gt; n);
                  var condEvals = new JArray();

                  var startNode = nodes.OfType&lt;JObject&gt;().FirstOrDefault(n =&gt; string.Equals((string)n[&quot;type&quot;], &quot;start&quot;, StringComparison.OrdinalIgnoreCase));
                  string curId = (string)startNode?[&quot;id&quot;] ?? &quot;start&quot;;
                  JObject candNode = null;
                  JObject stratNode = null;
                  for (int step = 0; step &lt; 20; step++) {
                    if (!nodesById.ContainsKey(curId)) { break; }
                    var curNode = nodesById[curId];
                    var nType = (string)curNode[&quot;type&quot;];
                    if (nType == &quot;candidate_models&quot;) { candNode = curNode; }
                    if (nType == &quot;strategy&quot; || nType == &quot;priority&quot;) { stratNode = curNode; break; }
                    var outEdges = edges.OfType&lt;JObject&gt;().Where(e =&gt; (string)e[&quot;source&quot;] == curId).ToList();
                    if (outEdges.Count == 0) { break; }
                    if (nType == &quot;condition&quot;) {
                      var cond = curNode[&quot;condition&quot;] as JObject;
                      string field = (string)cond?[&quot;field&quot;] ?? &quot;&quot;;
                      string op = (string)cond?[&quot;operator&quot;] ?? &quot;equals&quot;;
                      JToken expTok = cond?[&quot;value&quot;];
                      JToken actTok = JValue.CreateNull();
                      if (field == &quot;prompt_length&quot;) { actTok = new JValue(promptLength); }
                      else if (field == &quot;request_parameter&quot;) { actTok = new JValue(lastUserPrompt); }
                      else if (field == &quot;task_type&quot;) { actTok = new JValue(taskType); }
                      else if (field == &quot;user_id&quot;) { actTok = new JValue(consumerKey); }
                      else if (field == &quot;required_capability&quot;) { actTok = new JValue((reqBody[&quot;tools&quot;] as JArray)?.Count &gt; 0 ? &quot;tools&quot; : &quot;chat&quot;); }
                      else if (rCtx[field] != null) { actTok = rCtx[field]; }

                      bool matched = false;
                      string actStr = actTok.Type == JTokenType.Null ? &quot;&quot; : actTok.ToString();
                      string expStr = expTok == null || expTok.Type == JTokenType.Null ? &quot;&quot; : expTok.ToString();
                      double actNum, expNum;
                      if (op == &quot;less_than&quot; &amp;&amp; double.TryParse(actStr, out actNum) &amp;&amp; double.TryParse(expStr, out expNum)) {
                        matched = actNum &lt; expNum;
                      } else if (op == &quot;less_than_or_equal&quot; &amp;&amp; double.TryParse(actStr, out actNum) &amp;&amp; double.TryParse(expStr, out expNum)) {
                        matched = actNum &lt;= expNum;
                      } else if (op == &quot;greater_than&quot; &amp;&amp; double.TryParse(actStr, out actNum) &amp;&amp; double.TryParse(expStr, out expNum)) {
                        matched = actNum &gt; expNum;
                      } else if (op == &quot;greater_than_or_equal&quot; &amp;&amp; double.TryParse(actStr, out actNum) &amp;&amp; double.TryParse(expStr, out expNum)) {
                        matched = actNum &gt;= expNum;
                      } else if (op == &quot;equals&quot;) {
                        matched = string.Equals(actStr.Trim(), expStr.Trim(), StringComparison.OrdinalIgnoreCase);
                      } else if (op == &quot;not_equals&quot;) {
                        matched = !string.Equals(actStr.Trim(), expStr.Trim(), StringComparison.OrdinalIgnoreCase);
                      } else if (op == &quot;contains&quot;) {
                        matched = actStr.IndexOf(expStr, StringComparison.OrdinalIgnoreCase) &gt;= 0;
                      } else if (op == &quot;matches_any&quot; || op == &quot;in&quot;) {
                        var terms = (expTok is JArray)
                          ? ((JArray)expTok).Select(t =&gt; t.ToString().Trim()).Where(t =&gt; t.Length &gt; 0).ToList()
                          : expStr.Replace(&quot;;&quot;, &quot;,&quot;).Replace(&quot;|&quot;, &quot;,&quot;).Split(',').Select(t =&gt; t.Trim()).Where(t =&gt; t.Length &gt; 0).ToList();
                        if (op == &quot;matches_any&quot;) {
                          matched = terms.Any(t =&gt; actStr.IndexOf(t, StringComparison.OrdinalIgnoreCase) &gt;= 0);
                        } else {
                          matched = terms.Any(t =&gt; string.Equals(actStr, t, StringComparison.OrdinalIgnoreCase));
                        }
                      } else if (op == &quot;regex&quot;) {
                        matched = System.Text.RegularExpressions.Regex.IsMatch(actStr, expStr, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                      } else if (op == &quot;exists&quot;) {
                        matched = !string.IsNullOrWhiteSpace(actStr);
                      }
                      string branchLabel = matched ? &quot;Match&quot; : &quot;Else&quot;;
                      condEvals.Add(new JObject(
                        new JProperty(&quot;node_id&quot;, curId),
                        new JProperty(&quot;node_label&quot;, (string)curNode[&quot;label&quot;] ?? curId),
                        new JProperty(&quot;field&quot;, field),
                        new JProperty(&quot;operator&quot;, op),
                        new JProperty(&quot;expected&quot;, expTok),
                        new JProperty(&quot;actual&quot;, field == &quot;request_parameter&quot; &amp;&amp; actStr.Length &gt; 80 ? new JValue(actStr.Substring(0, 80) + &quot;...&quot;) : actTok),
                        new JProperty(&quot;matched&quot;, matched),
                        new JProperty(&quot;branch&quot;, branchLabel)
                      ));
                      var nextEdge = outEdges.FirstOrDefault(e =&gt; string.Equals(((string)e[&quot;label&quot;] ?? &quot;&quot;).Trim(), branchLabel, StringComparison.OrdinalIgnoreCase)) ?? outEdges[0];
                      curId = (string)nextEdge[&quot;target&quot;];
                    } else {
                      curId = (string)outEdges[0][&quot;target&quot;];
                    }
                  }
                  if (candNode == null) {
                    candNode = nodes.OfType&lt;JObject&gt;().FirstOrDefault(n =&gt; (string)n[&quot;type&quot;] == &quot;candidate_models&quot;);
                  }
                  if (stratNode == null) {
                    stratNode = nodes.OfType&lt;JObject&gt;().FirstOrDefault(n =&gt; (string)n[&quot;type&quot;] == &quot;strategy&quot; || (string)n[&quot;type&quot;] == &quot;priority&quot;);
                  }
                  var fbEvents = new JArray();
                  var fbSet = new HashSet&lt;string&gt;(StringComparer.OrdinalIgnoreCase);
                  var stratFb = stratNode?[&quot;fallback_on&quot;] as JArray;
                  if (stratFb != null) {
                    foreach (var ev in stratFb) {
                      if (fbSet.Add(ev.ToString())) { fbEvents.Add(ev.ToString()); }
                    }
                  }
                  if (stratNode != null) {
                    string sNodeId = (string)stratNode[&quot;id&quot;];
                    foreach (var e in edges.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;source&quot;] == sNodeId)) {
                      string tId = (string)e[&quot;target&quot;];
                      if (nodesById.ContainsKey(tId) &amp;&amp; (string)nodesById[tId][&quot;type&quot;] == &quot;fallback&quot;) {
                        var fOn = nodesById[tId][&quot;fallback_on&quot;] as JArray;
                        if (fOn != null) {
                          foreach (var ev in fOn) {
                            if (fbSet.Add(ev.ToString())) { fbEvents.Add(ev.ToString()); }
                          }
                        }
                      }
                    }
                  }
                  int timeoutMs = Math.Max(1000, Math.Min(120000, (int?)stratNode?[&quot;timeout_ms&quot;] ?? 120000));

                  /* Build candidate list for reached strategy */
                  var candsById = (JObject)routerDoc[&quot;candidates_by_id&quot;];
                  var rawCands = candNode?[&quot;candidates&quot;] as JArray ?? new JArray();
                  var modelConfigs = stratNode?[&quot;model_configs&quot;] as JArray ?? new JArray();
                  var cfgById = modelConfigs.OfType&lt;JObject&gt;().ToDictionary(c =&gt; (string)c[&quot;model_id&quot;], c =&gt; c);

                  var mergedCands = new List&lt;JObject&gt;();
                  int idx = 0;
                  foreach (var cTok in rawCands) {
                    idx++;
                    var cObj = cTok as JObject;
                    if (cObj == null || (bool?)cObj[&quot;enabled&quot;] == false) { continue; }
                    string mid = (string)cObj[&quot;model_id&quot;];
                    if (candsById[mid] == null) { continue; }
                    var mInfo = (JObject)candsById[mid].DeepClone();
                    var mCfg = cfgById.ContainsKey(mid) ? cfgById[mid] : cObj;
                    mInfo[&quot;priority&quot;] = (int?)mCfg[&quot;priority&quot;] ?? (int?)cObj[&quot;priority&quot;] ?? idx;
                    mInfo[&quot;weight&quot;] = (int?)mCfg[&quot;weight&quot;] ?? (int?)cObj[&quot;weight&quot;] ?? 100;
                    mInfo[&quot;max_cost_per_million&quot;] = mCfg[&quot;max_cost_per_million&quot;] ?? cObj[&quot;max_cost_per_million&quot;] ?? JValue.CreateNull();
                    mInfo[&quot;max_latency_ms&quot;] = mCfg[&quot;max_latency_ms&quot;] ?? cObj[&quot;max_latency_ms&quot;] ?? JValue.CreateNull();
                    mInfo[&quot;description&quot;] = (string)mCfg[&quot;description&quot;] ?? (string)cObj[&quot;description&quot;] ?? &quot;&quot;;
                    mergedCands.Add(mInfo);
                  }
                  mergedCands = mergedCands.OrderBy(c =&gt; (int)c[&quot;priority&quot;]).ThenByDescending(c =&gt; (int)c[&quot;weight&quot;]).ToList();

                  string strategyType = (string)stratNode?[&quot;strategy_type&quot;] ?? &quot;priority&quot;;
                  var attempts = new JArray();
                  var eligibleCands = new JArray();
                  int attNum = 0;
                  foreach (var mc in mergedCands) {
                    var mcCaps = mc[&quot;capabilities&quot;] as JArray;
                    if (mcCaps != null &amp;&amp; mcCaps.Any(x =&gt; (string)x == &quot;decision&quot;)) {
                      continue;
                    }
                    double? effCostOpt = (double?)mc[&quot;effective_cost_per_million&quot;];
                    if (effCostOpt.HasValue &amp;&amp; (mc[&quot;max_cost_per_million&quot;]?.Type == JTokenType.Float || mc[&quot;max_cost_per_million&quot;]?.Type == JTokenType.Integer)) {
                      double effCost = effCostOpt.Value;
                      double maxCost = (double)mc[&quot;max_cost_per_million&quot;];
                      if (effCost &gt; maxCost) {
                        attNum++;
                        attempts.Add(new JObject(
                          new JProperty(&quot;sequence&quot;, attNum),
                          new JProperty(&quot;attempt_number&quot;, attNum),
                          new JProperty(&quot;model_id&quot;, (string)mc[&quot;model_id&quot;]),
                          new JProperty(&quot;model_name&quot;, (string)mc[&quot;display_name&quot;]),
                          new JProperty(&quot;request_id&quot;, JValue.CreateNull()),
                          new JProperty(&quot;outcome&quot;, &quot;cost_limit_exceeded&quot;),
                          new JProperty(&quot;status_code&quot;, JValue.CreateNull()),
                          new JProperty(&quot;latency_ms&quot;, 0),
                          new JProperty(&quot;priority&quot;, (int?)mc[&quot;priority&quot;]),
                          new JProperty(&quot;weight&quot;, (int?)mc[&quot;weight&quot;]),
                          new JProperty(&quot;max_cost_per_million&quot;, mc[&quot;max_cost_per_million&quot;]),
                          new JProperty(&quot;reason&quot;, &quot;Candidate exceeds static cost ceiling ($&quot; + effCost.ToString(&quot;F2&quot;) + &quot;/M &gt; $&quot; + maxCost.ToString(&quot;F2&quot;) + &quot;/M)&quot;),
                          new JProperty(&quot;strategy_type&quot;, strategyType),
                          new JProperty(&quot;fallback_trigger&quot;, &quot;cost&quot;),
                          new JProperty(&quot;decision_basis&quot;, &quot;cost_limit_exceeded&quot;),
                          new JProperty(&quot;attempt_role&quot;, &quot;candidate&quot;),
                          new JProperty(&quot;condition_evaluations&quot;, attNum == 1 ? condEvals : new JArray())
                        ));
                        continue;
                      }
                    }
                    eligibleCands.Add(mc);
                  }

                  /* Compute Sticky Keys */
                  var lookupKeys = new JArray();
                  var saveKeys = new JArray();
                  using (var sha = System.Security.Cryptography.SHA256.Create()) {
                    if (!string.IsNullOrEmpty(interactionId)) {
                      string sk = &quot;interaction:&quot; + interactionId;
                      string rk = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(consumerKey + &quot;|&quot; + sk))).Replace(&quot;-&quot;, &quot;&quot;).ToLowerInvariant();
                      var entry = new JObject(new JProperty(&quot;tier&quot;, &quot;interaction&quot;), new JProperty(&quot;sticky_key&quot;, sk), new JProperty(&quot;row_key&quot;, rk), new JProperty(&quot;ttl_seconds&quot;, interactionTtlSeconds));
                      lookupKeys.Add(entry);
                      saveKeys.Add(entry);
                    }
                    if (!string.IsNullOrEmpty(sessionId)) {
                      string sk = &quot;session:&quot; + sessionId;
                      string rk = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(consumerKey + &quot;|&quot; + sk))).Replace(&quot;-&quot;, &quot;&quot;).ToLowerInvariant();
                      var entry = new JObject(new JProperty(&quot;tier&quot;, &quot;session&quot;), new JProperty(&quot;sticky_key&quot;, sk), new JProperty(&quot;row_key&quot;, rk), new JProperty(&quot;ttl_seconds&quot;, sessionTtlSeconds));
                      lookupKeys.Add(entry);
                      saveKeys.Add(entry);
                    }
                    if (string.IsNullOrEmpty(interactionId) &amp;&amp; string.IsNullOrEmpty(sessionId) &amp;&amp; !string.IsNullOrEmpty(firstUserPrompt)) {
                      string derivedSeed = string.IsNullOrEmpty(systemSummary) ? firstUserPrompt : (systemSummary + &quot;\n&quot; + firstUserPrompt);
                      string promptHash = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(derivedSeed))).Replace(&quot;-&quot;, &quot;&quot;).ToLowerInvariant();
                      string sk = &quot;derived-&quot; + promptHash.Substring(0, 32);
                      string rk = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(consumerKey + &quot;|&quot; + sk))).Replace(&quot;-&quot;, &quot;&quot;).ToLowerInvariant();
                      var entry = new JObject(new JProperty(&quot;tier&quot;, &quot;derived_interaction&quot;), new JProperty(&quot;sticky_key&quot;, sk), new JProperty(&quot;row_key&quot;, rk), new JProperty(&quot;ttl_seconds&quot;, interactionTtlSeconds));
                      saveKeys.Add(entry);
                      if (initiator == &quot;agent&quot; || userMsgCount &gt; 1 || hasToolMsgs || turnIndex &gt; 1) {
                        lookupKeys.Add(entry);
                      }
                    }
                  }

                  /* Pre-calculate non-model-based selection (priority or weighted) */
                  JObject defaultSelected = eligibleCands.Count &gt; 0 ? (JObject)eligibleCands[0] : null;
                  string defaultBasis = attempts.Count &gt; 0 ? &quot;fallback_success&quot; : &quot;highest_priority_success&quot;;
                  if (strategyType == &quot;weighted&quot; &amp;&amp; eligibleCands.Count &gt; 1) {
                    int totalWeight = eligibleCands.OfType&lt;JObject&gt;().Sum(c =&gt; Math.Max(1, (int)c[&quot;weight&quot;]));
                    using (var sha = System.Security.Cryptography.SHA256.Create()) {
                      var h = sha.ComputeHash(Encoding.UTF8.GetBytes((string)routerDoc[&quot;router_id&quot;] + &quot;:&quot; + (string)routerDoc[&quot;router_version&quot;] + &quot;:&quot; + consumerKey + &quot;:&quot; + runId));
                      uint bucket = (uint)((h[0] &lt;&lt; 24) | (h[1] &lt;&lt; 16) | (h[2] &lt;&lt; 8) | h[3]) % (uint)totalWeight;
                      uint running = 0;
                      foreach (JObject ec in eligibleCands) {
                        running += (uint)Math.Max(1, (int)ec[&quot;weight&quot;]);
                        if (bucket &lt; running) {
                          defaultSelected = ec;
                          break;
                        }
                      }
                    }
                    defaultBasis = &quot;weighted_selection_success&quot;;
                  }

                  var state = new JObject(
                    new JProperty(&quot;router_id&quot;, (string)routerDoc[&quot;router_id&quot;]),
                    new JProperty(&quot;router_name&quot;, (string)routerDoc[&quot;router_name&quot;]),
                    new JProperty(&quot;router_version&quot;, (int)routerDoc[&quot;router_version&quot;]),
                    new JProperty(&quot;consumer_key&quot;, consumerKey),
                    new JProperty(&quot;interaction_id&quot;, string.IsNullOrEmpty(interactionId) ? JValue.CreateNull() : new JValue(interactionId)),
                    new JProperty(&quot;session_id&quot;, string.IsNullOrEmpty(sessionId) ? JValue.CreateNull() : new JValue(sessionId)),
                    new JProperty(&quot;run_id&quot;, runId),
                    new JProperty(&quot;turn_index&quot;, turnIndex),
                    new JProperty(&quot;task_type&quot;, taskType),
                    new JProperty(&quot;ttl_seconds&quot;, ttlSeconds),
                    new JProperty(&quot;timeout_ms&quot;, timeoutMs),
                    new JProperty(&quot;fallback_events&quot;, fbEvents),
                    new JProperty(&quot;user_prompt&quot;, lastUserPrompt),
                    new JProperty(&quot;decision_input&quot;, decisionInput),
                    new JProperty(&quot;strategy_type&quot;, strategyType),
                    new JProperty(&quot;routing_model_id&quot;, stratNode?[&quot;routing_model_id&quot;] ?? JValue.CreateNull()),
                    new JProperty(&quot;decision_prompt&quot;, stratNode?[&quot;decision_prompt&quot;] ?? JValue.CreateNull()),
                    new JProperty(&quot;decision_reasoning_effort&quot;, stratNode?[&quot;decision_reasoning_effort&quot;] ?? JValue.CreateNull()),
                    new JProperty(&quot;max_prompt_chars&quot;, (int?)stratNode?[&quot;max_prompt_chars&quot;] ?? 4000),
                    new JProperty(&quot;condition_evaluations&quot;, condEvals),
                    new JProperty(&quot;attempts&quot;, attempts),
                    new JProperty(&quot;eligible_candidates&quot;, eligibleCands),
                    new JProperty(&quot;candidates_by_id&quot;, candsById),
                    new JProperty(&quot;routing_models_by_id&quot;, routerDoc[&quot;routing_models_by_id&quot;] ?? new JObject()),
                    new JProperty(&quot;lookup_keys&quot;, lookupKeys),
                    new JProperty(&quot;save_keys&quot;, saveKeys),
                    new JProperty(&quot;is_new_user_turn&quot;, initiator != &quot;agent&quot; &amp;&amp; lastNonSystemRole == &quot;user&quot; &amp;&amp; userMsgCount &gt; 1 &amp;&amp; !string.Equals(lastUserPrompt, prevUserPrompt, StringComparison.Ordinal)),
                    new JProperty(&quot;is_user_turn&quot;, initiator != &quot;agent&quot; &amp;&amp; lastNonSystemRole == &quot;user&quot; &amp;&amp; !lastIsToolContinuation),
                    new JProperty(&quot;selected_candidate&quot;, defaultSelected != null ? (JToken)defaultSelected : JValue.CreateNull()),
                    new JProperty(&quot;decision_basis&quot;, defaultBasis),
                    new JProperty(&quot;sticky_hit&quot;, false),
                    new JProperty(&quot;sticky_candidate&quot;, JValue.CreateNull()),
                    new JProperty(&quot;sticky_bound_by&quot;, JValue.CreateNull()),
                    new JProperty(&quot;sticky_key&quot;, JValue.CreateNull()),
                    new JProperty(&quot;decision_ms&quot;, 0L),
                    new JProperty(&quot;affinity_cookie&quot;, &quot;&quot;)
                  );
                  return state.ToString(Newtonsoft.Json.Formatting.None);
                } catch {
                  return &quot;&quot;;
                }
              }" />
              <choose>
                <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;routerPlanState&quot;]) &amp;&amp; (bool?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;access_denied&quot;] == true)">
                  <return-response>
                    <set-status code="403" reason="Forbidden" />
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-body>{"type":"error","error":{"type":"permission_error","code":"router_not_assigned","message":"Model router is not assigned to this user"}}</set-body>
                  </return-response>
                </when>
              </choose>

              <!-- Step 5.2: Sticky Session Binding Lookup -->
              <choose>
                <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;routerPlanState&quot;]) &amp;&amp; ((JArray)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;lookup_keys&quot;]).Count &gt; 0)">
                  <send-request mode="new" response-variable-name="routerStickyTableResp" timeout="2" ignore-error="true">
                    <set-url>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var lks = (JArray)st[&quot;lookup_keys&quot;];
                      var pk = &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;];
                      var rkClauses = lks.OfType&lt;JObject&gt;().Select(k =&gt; &quot;RowKey eq '&quot; + (string)k[&quot;row_key&quot;] + &quot;'&quot;);
                      var filter = &quot;PartitionKey eq '&quot; + pk + &quot;' and (&quot; + string.Join(&quot; or &quot;, rkClauses) + &quot;)&quot;;
                      return &quot;__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__()?$filter=&quot; + Uri.EscapeDataString(filter);
                    }</set-url>
                    <set-method>GET</set-method>
                    <set-header name="Accept" exists-action="override">
                      <value>application/json;odata=nometadata</value>
                    </set-header>
                    <set-header name="x-ms-version" exists-action="override">
                      <value>2020-12-06</value>
                    </set-header>
                    <authentication-managed-identity resource="https://storage.azure.com/" />
                  </send-request>
                  <set-variable name="routerPlanState" value="@{
                    try {
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var resp = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerStickyTableResp&quot;, null);
                      if (resp == null || resp.StatusCode != 200) { return st.ToString(Newtonsoft.Json.Formatting.None); }
                      var values = JObject.Parse(resp.Body.As&lt;string&gt;(preserveContent: true))[&quot;value&quot;] as JArray ?? new JArray();
                      if (values.Count == 0) { return st.ToString(Newtonsoft.Json.Formatting.None); }
                      long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                      long fallbackTtl = (long)st[&quot;ttl_seconds&quot;];
                      var candsById = (JObject)st[&quot;candidates_by_id&quot;];
                      var elig = (JArray)st[&quot;eligible_candidates&quot;];
                      var rawByRowKey = new Dictionary&lt;string, JObject&gt;(StringComparer.OrdinalIgnoreCase);
                      foreach (var vTok in values) {
                        var ent = vTok as JObject;
                        if (ent == null || ent[&quot;Document&quot;]?.Type != JTokenType.String) { continue; }
                        var bDoc = JObject.Parse((string)ent[&quot;Document&quot;]);
                        rawByRowKey[(string)ent[&quot;RowKey&quot;]] = bDoc;
                      }
                      foreach (JObject lk in (JArray)st[&quot;lookup_keys&quot;]) {
                        string rk = (string)lk[&quot;row_key&quot;];
                        if (!rawByRowKey.ContainsKey(rk)) { continue; }
                        var bDoc = rawByRowKey[rk];
                        long updatedEpoch = (long?)bDoc[&quot;updated_epoch&quot;] ?? 0L;
                        long keyTtl = (long?)lk[&quot;ttl_seconds&quot;] ?? fallbackTtl;
                        string mid = (string)bDoc[&quot;model_id&quot;] ?? &quot;&quot;;
                        if (now - updatedEpoch &lt;= keyTtl &amp;&amp; candsById[mid] != null) {
                          string tier = (string)lk[&quot;tier&quot;];
                          var boundElig = elig.OfType&lt;JObject&gt;().FirstOrDefault(x =&gt; (string)x[&quot;model_id&quot;] == mid);
                          var boundCand = (JObject)(boundElig ?? candsById[mid]).DeepClone();
                          double bCost = ((double?)boundCand[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)boundCand[&quot;output_cost_per_million&quot;] ?? 0.0);
                          bool isNewTurn = (bool?)st[&quot;is_new_user_turn&quot;] ?? false;
                          if (((bool?)st[&quot;is_user_turn&quot;] ?? false) &amp;&amp; boundElig == null &amp;&amp; elig.Count &gt; 0) {
                            bool higherBranch = elig.OfType&lt;JObject&gt;().Any(x =&gt; (((double?)x[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)x[&quot;output_cost_per_million&quot;] ?? 0.0)) &gt; bCost);
                            if (higherBranch) {
                              break;
                            }
                          }
                          bool canEscalate = false;
                          if ((string)st[&quot;strategy_type&quot;] == &quot;model_based&quot; &amp;&amp; isNewTurn &amp;&amp; boundElig != null &amp;&amp; elig.Count &gt; 1) {
                            var poolCosts = elig.OfType&lt;JObject&gt;().Select(x =&gt; ((double?)x[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)x[&quot;output_cost_per_million&quot;] ?? 0.0)).ToList();
                            bool sameCostWithDesc = poolCosts.Count &gt; 0 &amp;&amp; poolCosts.Max() == poolCosts.Min() &amp;&amp; elig.OfType&lt;JObject&gt;().Any(x =&gt; !string.IsNullOrWhiteSpace((string)x[&quot;description&quot;]));
                            canEscalate = poolCosts.Any(c =&gt; c &gt; bCost) || sameCostWithDesc;
                            if (canEscalate) {
                              var filteredElig = new JArray();
                              foreach (JObject ec in elig) {
                                string ecId = (string)ec[&quot;model_id&quot;];
                                double ecCost = ((double?)ec[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)ec[&quot;output_cost_per_million&quot;] ?? 0.0);
                                if (ecId == mid || ecCost &gt; bCost || sameCostWithDesc) {
                                  filteredElig.Add(ec);
                                }
                              }
                              if (!filteredElig.OfType&lt;JObject&gt;().Any(x =&gt; (string)x[&quot;model_id&quot;] == mid)) {
                                filteredElig.Insert(0, boundCand.DeepClone());
                              }
                              if (filteredElig.Count &gt; 1) {
                                st[&quot;eligible_candidates&quot;] = filteredElig;
                              }
                            }
                          }
                          st[&quot;sticky_bound_by&quot;] = tier;
                          st[&quot;sticky_key&quot;] = (string)lk[&quot;sticky_key&quot;];
                          st[&quot;selected_candidate&quot;] = boundCand;
                          st[&quot;decision_basis&quot;] = tier == &quot;session&quot; ? &quot;session_sticky&quot; : &quot;interaction_sticky&quot;;
                          st[&quot;affinity_cookie&quot;] = (string)bDoc[&quot;affinity_cookie&quot;] ?? &quot;&quot;;
                          if (canEscalate) {
                            st[&quot;sticky_hit&quot;] = false;
                            st[&quot;sticky_candidate&quot;] = boundCand.DeepClone();
                          } else {
                            st[&quot;sticky_hit&quot;] = true;
                            st[&quot;attempts&quot;] = new JArray();
                          }
                          break;
                        }
                      }
                      return st.ToString(Newtonsoft.Json.Formatting.None);
                    } catch {
                      return (string)context.Variables[&quot;routerPlanState&quot;];
                    }
                  }" />
                </when>
              </choose>

              <!-- Step 5.3: If NOT Sticky-Hit and Strategy is model_based, invoke Decision routing model -->
              <choose>
                <when condition="@{
                  if (string.IsNullOrEmpty((string)context.Variables[&quot;routerPlanState&quot;])) { return false; }
                  var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                  return !(bool)st[&quot;sticky_hit&quot;] &amp;&amp; (string)st[&quot;strategy_type&quot;] == &quot;model_based&quot; &amp;&amp; ((JArray)st[&quot;eligible_candidates&quot;]).Count &gt; 1;
                }">
                  <set-variable name="routerDecisionRequestId" value="@(Guid.NewGuid().ToString())" />
                  <set-variable name="routerDecisionStarted" value="@(DateTime.UtcNow)" />
                  <send-request mode="new" response-variable-name="routerDecisionHttp" timeout="15" ignore-error="true">
                    <set-url>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var rmById = (JObject)st[&quot;routing_models_by_id&quot;];
                      string rmId = (string)st[&quot;routing_model_id&quot;] ?? &quot;&quot;;
                      var rmInfo = rmById[rmId] as JObject;
                      if ((string)rmInfo?[&quot;auth_mode&quot;] == &quot;gateway_route&quot;) {
                        return &quot;https://&quot; + context.Request.OriginalUrl.Host + &quot;/&quot; + context.Api.Path.Trim('/') + &quot;/chat/completions&quot;;
                      }
                      string origin = (string)rmInfo?[&quot;endpoint_origin&quot;] ?? &quot;&quot;;
                      string proto = (string)rmInfo?[&quot;decision_protocol&quot;] ?? &quot;system_one&quot;;
                      string bPath = (string)rmInfo?[&quot;backend_path&quot;];
                      if (string.IsNullOrWhiteSpace(bPath)) {
                        bPath = proto == &quot;chat_json&quot; ? &quot;/openai/v1/chat/completions&quot; : &quot;/providers/microsoft/v1/systemone&quot;;
                      }
                      return origin.TrimEnd('/') + &quot;/&quot; + bPath.TrimStart('/');
                    }</set-url>
                    <set-method>POST</set-method>
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-header name="x-turnstile-router-subrequest" exists-action="override">
                      <value>1</value>
                    </set-header>
                    <set-header name="x-request-id" exists-action="override">
                      <value>@((string)context.Variables["routerDecisionRequestId"])</value>
                    </set-header>
                    <set-body>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var elig = (JArray)st[&quot;eligible_candidates&quot;];
                      var rmById = (JObject)st[&quot;routing_models_by_id&quot;];
                      string rmId = (string)st[&quot;routing_model_id&quot;] ?? &quot;&quot;;
                      var rmInfo = rmById[rmId] as JObject;
                      string upstreamModel = (string)rmInfo?[&quot;upstream_model&quot;] ?? rmId;
                      string proto = (string)rmInfo?[&quot;decision_protocol&quot;] ?? &quot;system_one&quot;;
                      string prompt = (string)st[&quot;decision_input&quot;] ?? (string)st[&quot;user_prompt&quot;] ?? &quot;&quot;;
                      var activeSticky = st[&quot;sticky_candidate&quot;] as JObject;
                      if (activeSticky != null) {
                        string activeName = (string)activeSticky[&quot;display_name&quot;] ?? (string)activeSticky[&quot;model_id&quot;] ?? &quot;&quot;;
                        string activeNote = &quot;Active conversation model: &quot; + activeName + &quot; (keep this model to preserve conversation prompt cache unless the current user request clearly requires a higher-capability candidate based on the candidate descriptions and unit costs)&quot;;
                        int ctxIdx = prompt.IndexOf(&quot;\n\nPrior turn context&quot;, StringComparison.Ordinal);
                        if (ctxIdx &gt;= 0) {
                          prompt = prompt.Substring(0, ctxIdx) + &quot;\n\n&quot; + activeNote + prompt.Substring(ctxIdx);
                        } else if (prompt.Contains(&quot;Current user request:\n&quot;)) {
                          prompt = prompt + &quot;\n\n&quot; + activeNote;
                        } else {
                          prompt = &quot;Current user request:\n&quot; + prompt + &quot;\n\n&quot; + activeNote;
                        }
                      }
                      int maxChars = (int?)st[&quot;max_prompt_chars&quot;] ?? 4000;
                      if (prompt.Length &gt; maxChars) {
                        string marker = &quot;\n...[middle omitted]...\n&quot;;
                        if (maxChars &gt; marker.Length + 20) {
                          int rem = maxChars - marker.Length;
                          int headLen = rem / 2;
                          int tailLen = rem - headLen;
                          prompt = prompt.Substring(0, headLen) + marker + prompt.Substring(prompt.Length - tailLen);
                        } else {
                          prompt = prompt.Substring(0, maxChars);
                        }
                      }
                      string instructions = (string)st[&quot;decision_prompt&quot;];
                      if (proto == &quot;chat_json&quot;) {
                        var catArr = new JArray();
                        foreach (JObject c in elig) {
                          catArr.Add(new JObject(
                            new JProperty(&quot;model_id&quot;, (string)c[&quot;model_id&quot;]),
                            new JProperty(&quot;display_name&quot;, (string)c[&quot;display_name&quot;]),
                            new JProperty(&quot;provider_name&quot;, (string)c[&quot;provider_name&quot;]),
                            new JProperty(&quot;description&quot;, (string)c[&quot;description&quot;] ?? &quot;&quot;),
                            new JProperty(&quot;input_cost_per_million&quot;, c[&quot;input_cost_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;output_cost_per_million&quot;, c[&quot;output_cost_per_million&quot;] ?? JValue.CreateNull())
                          ));
                        }
                        string catalogJson = catArr.ToString(Newtonsoft.Json.Formatting.None);
                        string jsonShapeNote = "Return only a JSON object with this shape: {\"model_id\":\"&lt;candidate UUID&gt;\",\"rationale\":\"&lt;one short sentence&gt;\"}. Never return a model outside the catalog.";
                        string sysPrompt;
                        if (string.IsNullOrWhiteSpace(instructions)) {
                          sysPrompt = "You are Turnstile's model routing classifier. Select the single best model from the candidate catalog for the user's request based on each candidate's description and unit cost.\n\nCandidate catalog:\n" + catalogJson + "\n\n" + jsonShapeNote;
                        } else {
                          sysPrompt = instructions.Contains("{catalog}") ? instructions.Replace("{catalog}", catalogJson) : (instructions + "\n\nCandidate catalog:\n" + catalogJson);
                          if (!sysPrompt.Contains("model_id") || instructions.IndexOf("json", StringComparison.OrdinalIgnoreCase) &lt; 0) {
                            sysPrompt = sysPrompt + "\n\n" + jsonShapeNote;
                          }
                        }
                        string reqMod = (string)rmInfo?[&quot;auth_mode&quot;] == &quot;gateway_route&quot; ? ((string)rmInfo?[&quot;apim_alias&quot;] ?? upstreamModel) : upstreamModel;
                        var chatPayload = new JObject(
                          new JProperty(&quot;model&quot;, reqMod),
                          new JProperty(&quot;messages&quot;, new JArray(
                            new JObject(new JProperty(&quot;role&quot;, &quot;system&quot;), new JProperty(&quot;content&quot;, sysPrompt)),
                            new JObject(new JProperty(&quot;role&quot;, &quot;user&quot;), new JProperty(&quot;content&quot;, prompt))
                          )),
                          new JProperty(&quot;response_format&quot;, new JObject(new JProperty(&quot;type&quot;, &quot;json_object&quot;))),
                          new JProperty(&quot;stream&quot;, false)
                        );
                        string maxTokField = (string)rmInfo?[&quot;max_tokens_field&quot;] ?? &quot;max_completion_tokens&quot;;
                        string effort = (string)st[&quot;decision_reasoning_effort&quot;];
                        var rmCaps = rmInfo?[&quot;capabilities&quot;] as JArray;
                        bool isReasoning = rmCaps != null &amp;&amp; rmCaps.Any(x =&gt; (string)x == &quot;reasoning&quot;);
                        int maxOutTok = (!string.IsNullOrEmpty(effort) &amp;&amp; effort != &quot;none&quot;) || isReasoning ? 1024 : 200;
                        chatPayload[maxTokField] = maxOutTok;
                        if (!string.IsNullOrEmpty(effort)) {
                          chatPayload[&quot;reasoning_effort&quot;] = effort;
                        }
                        return chatPayload.ToString(Newtonsoft.Json.Formatting.None);
                      }
                      if (string.IsNullOrWhiteSpace(instructions)) {
                        instructions = &quot;Select the single best candidate model for the user's request based on each candidate's description and unit cost.&quot;;
                      }
                      var criteria = new JObject();
                      foreach (JObject c in elig) {
                        string cid = (string)c[&quot;model_id&quot;];
                        double? cIn = (double?)c[&quot;input_cost_per_million&quot;];
                        double? cOut = (double?)c[&quot;output_cost_per_million&quot;];
                        string costPart = (cIn.HasValue &amp;&amp; cOut.HasValue)
                          ? (&quot;: cost=$&quot; + cIn.Value.ToString(&quot;F2&quot;) + &quot;/M input, $&quot; + cOut.Value.ToString(&quot;F2&quot;) + &quot;/M output. &quot;)
                          : &quot;: &quot;;
                        string desc = (string)c[&quot;display_name&quot;] + &quot; (&quot; + (string)c[&quot;provider_name&quot;] + &quot;)&quot;
                          + costPart
                          + (string)c[&quot;description&quot;];
                        criteria[cid] = desc;
                      }
                      var payload = new JObject(
                        new JProperty(&quot;model&quot;, upstreamModel),
                        new JProperty(&quot;state&quot;, prompt),
                        new JProperty(&quot;questions&quot;, new JObject(
                          new JProperty(&quot;route&quot;, new JObject(
                            new JProperty(&quot;type&quot;, &quot;choice&quot;),
                            new JProperty(&quot;instructions&quot;, instructions),
                            new JProperty(&quot;criteria&quot;, criteria)
                          ))
                        ))
                      );
                      return payload.ToString(Newtonsoft.Json.Formatting.None);
                    }</set-body>
                    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
                  </send-request>
                  <set-variable name="routerPlanState" value="@{
                    try {
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      long decMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;routerDecisionStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
                      st[&quot;decision_ms&quot;] = decMs;
                      var resp = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerDecisionHttp&quot;, null);
                      var rmById = (JObject)st[&quot;routing_models_by_id&quot;];
                      string rmId = (string)st[&quot;routing_model_id&quot;] ?? &quot;&quot;;
                      var rmInfo = rmById[rmId] as JObject;
                      string rmName = (string)rmInfo?[&quot;display_name&quot;] ?? rmId;
                      string rmProvider = (string)rmInfo?[&quot;provider_name&quot;] ?? &quot;&quot;;
                      string rmRuntime = (string)rmInfo?[&quot;runtime_name&quot;] ?? &quot;&quot;;
                      string proto = (string)rmInfo?[&quot;decision_protocol&quot;] ?? &quot;system_one&quot;;
                      double? rmInRate = (double?)rmInfo?[&quot;input_cost_per_million&quot;];
                      double? rmCachedRate = (double?)rmInfo?[&quot;cached_cost_per_million&quot;] ?? rmInRate;
                      double? rmCwRate = (double?)rmInfo?[&quot;cache_write_cost_per_million&quot;] ?? (proto == &quot;system_one&quot; ? 0.0 : rmCachedRate);
                      double? rmOutRate = (double?)rmInfo?[&quot;output_cost_per_million&quot;];
                      var candsById = (JObject)st[&quot;candidates_by_id&quot;];
                      var elig = (JArray)st[&quot;eligible_candidates&quot;];
                      var attempts = (JArray)st[&quot;attempts&quot;];
                      var condEvals = (JArray)st[&quot;condition_evaluations&quot;];
                      string decReqId = (string)context.Variables[&quot;routerDecisionRequestId&quot;];

                      if (resp != null &amp;&amp; resp.StatusCode == 200) {
                        var dBody = JObject.Parse(resp.Body.As&lt;string&gt;(preserveContent: true));
                        string chosenId = &quot;&quot;;
                        double? conf = null;
                        var probsDict = new JObject();
                        if (proto == &quot;chat_json&quot;) {
                          var msgTok = dBody[&quot;choices&quot;]?[0]?[&quot;message&quot;]?[&quot;content&quot;];
                          string rawContent = &quot;&quot;;
                          if (msgTok != null) {
                            if (msgTok.Type == JTokenType.String) {
                              rawContent = ((string)msgTok ?? &quot;&quot;).Trim();
                            } else if (msgTok is JArray) {
                              rawContent = string.Join(&quot;&quot;, ((JArray)msgTok).OfType&lt;JObject&gt;().Select(p =&gt; (string)p[&quot;text&quot;] ?? &quot;&quot;)).Trim();
                            }
                          }
                          int firstBrace = rawContent.IndexOf((char)123);
                          int lastBrace = rawContent.LastIndexOf((char)125);
                          if (firstBrace &gt;= 0 &amp;&amp; lastBrace &gt; firstBrace) {
                            rawContent = rawContent.Substring(firstBrace, lastBrace - firstBrace + 1);
                          }
                          if (!string.IsNullOrEmpty(rawContent)) {
                            try {
                              var parsedJson = JObject.Parse(rawContent);
                              string rawSel = ((string)parsedJson[&quot;model_id&quot;] ?? (string)parsedJson[&quot;model&quot;] ?? &quot;&quot;).Trim();
                              foreach (JObject ec in elig) {
                                string ecId = (string)ec[&quot;model_id&quot;] ?? &quot;&quot;;
                                if (string.Equals(ecId, rawSel, StringComparison.OrdinalIgnoreCase)
                                    || string.Equals((string)ec[&quot;display_name&quot;], rawSel, StringComparison.OrdinalIgnoreCase)
                                    || string.Equals((string)ec[&quot;model_key&quot;], rawSel, StringComparison.OrdinalIgnoreCase)
                                    || string.Equals((string)ec[&quot;apim_alias&quot;], rawSel, StringComparison.OrdinalIgnoreCase)) {
                                  chosenId = ecId;
                                  break;
                                }
                              }
                              double parsedConf;
                              if (parsedJson[&quot;confidence&quot;] != null &amp;&amp; double.TryParse(parsedJson[&quot;confidence&quot;].ToString(), out parsedConf) &amp;&amp; parsedConf &gt;= 0.0) {
                                conf = Math.Round(Math.Min(1.0, parsedConf), 4);
                              }
                              var probsObj = parsedJson[&quot;probabilities&quot;] as JObject;
                              if (probsObj != null) {
                                foreach (var prop in probsObj.Properties()) {
                                  if (prop.Name == &quot;__none__&quot; || prop.Value == null) { continue; }
                                  double pVal;
                                  if (!double.TryParse(prop.Value.ToString(), out pVal) || pVal &lt; 0.0) { continue; }
                                  string resolvedKey = &quot;&quot;;
                                  foreach (JObject ec in elig) {
                                    string ecId = (string)ec[&quot;model_id&quot;] ?? &quot;&quot;;
                                    if (string.Equals(ecId, prop.Name, StringComparison.OrdinalIgnoreCase)
                                        || string.Equals((string)ec[&quot;display_name&quot;], prop.Name, StringComparison.OrdinalIgnoreCase)
                                        || string.Equals((string)ec[&quot;model_key&quot;], prop.Name, StringComparison.OrdinalIgnoreCase)
                                        || string.Equals((string)ec[&quot;apim_alias&quot;], prop.Name, StringComparison.OrdinalIgnoreCase)) {
                                      resolvedKey = ecId;
                                      break;
                                    }
                                  }
                                  if (!string.IsNullOrEmpty(resolvedKey)) {
                                    probsDict[resolvedKey] = Math.Round(Math.Min(1.0, pVal), 4);
                                  }
                                }
                              }
                            } catch {}
                          }
                        } else {
                          var routeAns = dBody[&quot;answers&quot;]?[&quot;route&quot;] as JObject;
                          chosenId = (string)routeAns?[&quot;choice&quot;] ?? (string)routeAns?[&quot;answer&quot;] ?? &quot;&quot;;
                          conf = (double?)routeAns?[&quot;confidence&quot;];
                          var probsObj = routeAns?[&quot;probabilities&quot;] as JObject;
                          if (probsObj != null) {
                            foreach (var prop in probsObj.Properties()) {
                              probsDict[prop.Name] = (double)prop.Value;
                            }
                          }
                        }
                        var uObj = dBody[&quot;usage&quot;] as JObject ?? new JObject();
                        long rawInTok = (long?)uObj[&quot;input_tokens&quot;] ?? (long?)uObj[&quot;prompt_tokens&quot;] ?? 0L;
                        long outTok = (long?)uObj[&quot;output_tokens&quot;] ?? (long?)uObj[&quot;completion_tokens&quot;] ?? 0L;
                        long decCachedTok = (long?)uObj[&quot;cached_tokens&quot;] ?? (long?)(uObj[&quot;prompt_tokens_details&quot;] ?? uObj[&quot;input_tokens_details&quot;])?[&quot;cached_tokens&quot;] ?? 0L;
                        long decCwTok = (long?)uObj[&quot;cache_write_tokens&quot;] ?? (long?)uObj[&quot;prompt_tokens_details&quot;]?[&quot;cache_write_tokens&quot;] ?? 0L;
                        long uncachedInTok = Math.Max(0L, rawInTok - decCachedTok);
                        long totDecTok = (long?)uObj[&quot;total_tokens&quot;] ?? (rawInTok + outTok);
                        string rmTierKey = null;
                        var rmTiers = rmInfo?[&quot;context_price_tiers&quot;] as JArray;
                        if (rmTiers != null &amp;&amp; rmTiers.Count &gt; 0) {
                          JObject matchedRmTier = null;
                          foreach (JObject tObj in rmTiers.OfType&lt;JObject&gt;()) {
                            long minTok = (long?)tObj[&quot;min_input_tokens&quot;] ?? 0L;
                            long? maxTok = (long?)tObj[&quot;max_input_tokens&quot;];
                            if (rawInTok &gt;= minTok &amp;&amp; (!maxTok.HasValue || rawInTok &lt;= maxTok.Value)) {
                              matchedRmTier = tObj;
                              break;
                            }
                          }
                          if (matchedRmTier == null) { matchedRmTier = (JObject)rmTiers[rmTiers.Count - 1]; }
                          rmInRate = (double?)matchedRmTier[&quot;input_price_per_million&quot;];
                          rmOutRate = (double?)matchedRmTier[&quot;output_price_per_million&quot;];
                          rmCachedRate = (double?)matchedRmTier[&quot;cached_price_per_million&quot;] ?? rmInRate;
                          rmCwRate = (double?)matchedRmTier[&quot;cache_write_price_per_million&quot;] ?? rmCachedRate;
                          rmTierKey = (string)matchedRmTier[&quot;key&quot;];
                        }
                        double? decCost = (rmInRate.HasValue &amp;&amp; rmOutRate.HasValue)
                          ? (double?)Math.Round((uncachedInTok * rmInRate.Value + Math.Max(0L, decCachedTok - decCwTok) * (rmCachedRate ?? rmInRate.Value) + decCwTok * (rmCwRate ?? rmCachedRate ?? rmInRate.Value) + outTok * rmOutRate.Value) / 1000000.0, 8)
                          : null;
                        int seq = attempts.Count + 1;
                        bool validChoice = !string.IsNullOrEmpty(chosenId) &amp;&amp; candsById[chosenId] != null;

                        attempts.Add(new JObject(
                          new JProperty(&quot;sequence&quot;, seq),
                          new JProperty(&quot;attempt_number&quot;, seq),
                          new JProperty(&quot;model_id&quot;, rmId),
                          new JProperty(&quot;model_name&quot;, rmName),
                          new JProperty(&quot;provider_name&quot;, rmProvider),
                          new JProperty(&quot;runtime_name&quot;, rmRuntime),
                          new JProperty(&quot;request_id&quot;, decReqId),
                          new JProperty(&quot;outcome&quot;, validChoice ? &quot;succeeded&quot; : &quot;failed&quot;),
                          new JProperty(&quot;status_code&quot;, 200),
                          new JProperty(&quot;latency_ms&quot;, decMs),
                          new JProperty(&quot;reason&quot;, validChoice ? &quot;Routing model selected an authorized candidate&quot; : &quot;Routing model returned a model outside the candidate catalog&quot;),
                          new JProperty(&quot;strategy_type&quot;, &quot;model_based&quot;),
                          new JProperty(&quot;decision_basis&quot;, validChoice ? &quot;model_based_selection&quot; : &quot;model_based_fallback&quot;),
                          new JProperty(&quot;attempt_role&quot;, &quot;routing_decision&quot;),
                          new JProperty(&quot;decision_protocol&quot;, proto),
                          new JProperty(&quot;selected_candidate_model_id&quot;, validChoice ? (JToken)chosenId : JValue.CreateNull()),
                          new JProperty(&quot;decision_confidence&quot;, conf),
                          new JProperty(&quot;decision_probabilities&quot;, probsDict),
                          new JProperty(&quot;condition_evaluations&quot;, condEvals),
                          new JProperty(&quot;input_tokens&quot;, uncachedInTok),
                          new JProperty(&quot;cached_tokens&quot;, decCachedTok),
                          new JProperty(&quot;cache_write_tokens&quot;, decCwTok),
                          new JProperty(&quot;output_tokens&quot;, outTok),
                          new JProperty(&quot;total_tokens&quot;, totDecTok),
                          new JProperty(&quot;estimated_cost&quot;, decCost.HasValue ? (JToken)new JValue(decCost.Value) : JValue.CreateNull()),
                          new JProperty(&quot;input_price_per_million&quot;, rmInRate.HasValue ? (JToken)new JValue(rmInRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;cached_price_per_million&quot;, rmCachedRate.HasValue ? (JToken)new JValue(rmCachedRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;cache_write_price_per_million&quot;, rmCwRate.HasValue ? (JToken)new JValue(rmCwRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;output_price_per_million&quot;, rmOutRate.HasValue ? (JToken)new JValue(rmOutRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;price_context_tier&quot;, rmTierKey != null ? (JToken)new JValue(rmTierKey) : JValue.CreateNull())
                        ));
                        if (validChoice) {
                          var chosenElig = elig.OfType&lt;JObject&gt;().FirstOrDefault(x =&gt; (string)x[&quot;model_id&quot;] == chosenId);
                          var chosenCand = (JObject)(chosenElig ?? candsById[chosenId]).DeepClone();
                          var stickyCand = st[&quot;sticky_candidate&quot;] as JObject;
                          if (stickyCand != null) {
                            double bCost = ((double?)stickyCand[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)stickyCand[&quot;output_cost_per_million&quot;] ?? 0.0);
                            double tCost = ((double?)chosenCand[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)chosenCand[&quot;output_cost_per_million&quot;] ?? 0.0);
                            var poolCosts = elig.OfType&lt;JObject&gt;().Select(x =&gt; ((double?)x[&quot;input_cost_per_million&quot;] ?? 0.0) + ((double?)x[&quot;output_cost_per_million&quot;] ?? 0.0)).ToList();
                            bool sameCostWithDesc = poolCosts.Count &gt; 0 &amp;&amp; poolCosts.Max() == poolCosts.Min() &amp;&amp; elig.OfType&lt;JObject&gt;().Any(x =&gt; !string.IsNullOrWhiteSpace((string)x[&quot;description&quot;]));
                            bool escalated = (tCost &gt; bCost || sameCostWithDesc) &amp;&amp; (string)chosenCand[&quot;model_id&quot;] != (string)stickyCand[&quot;model_id&quot;];
                            if (escalated) {
                              st[&quot;selected_candidate&quot;] = chosenCand;
                              st[&quot;decision_basis&quot;] = &quot;model_based_selection&quot;;
                              st[&quot;sticky_bound_by&quot;] = JValue.CreateNull();
                              st[&quot;sticky_key&quot;] = JValue.CreateNull();
                              st[&quot;affinity_cookie&quot;] = &quot;&quot;;
                            } else {
                              st[&quot;selected_candidate&quot;] = stickyCand;
                              string sTier = (string)st[&quot;sticky_bound_by&quot;] ?? &quot;interaction&quot;;
                              st[&quot;decision_basis&quot;] = sTier == &quot;session&quot; ? &quot;session_sticky&quot; : &quot;interaction_sticky&quot;;
                            }
                          } else {
                            st[&quot;selected_candidate&quot;] = chosenCand;
                            st[&quot;decision_basis&quot;] = &quot;model_based_selection&quot;;
                          }
                        } else if (st[&quot;sticky_candidate&quot;] is JObject) {
                          st[&quot;selected_candidate&quot;] = st[&quot;sticky_candidate&quot;];
                          string sTier = (string)st[&quot;sticky_bound_by&quot;] ?? &quot;interaction&quot;;
                          st[&quot;decision_basis&quot;] = sTier == &quot;session&quot; ? &quot;session_sticky&quot; : &quot;interaction_sticky&quot;;
                        } else {
                          st[&quot;decision_basis&quot;] = &quot;model_based_fallback&quot;;
                        }
                      } else {
                        int seq = attempts.Count + 1;
                        attempts.Add(new JObject(
                          new JProperty(&quot;sequence&quot;, seq),
                          new JProperty(&quot;attempt_number&quot;, seq),
                          new JProperty(&quot;model_id&quot;, rmId),
                          new JProperty(&quot;model_name&quot;, rmName),
                          new JProperty(&quot;provider_name&quot;, rmProvider),
                          new JProperty(&quot;runtime_name&quot;, rmRuntime),
                          new JProperty(&quot;request_id&quot;, decReqId),
                          new JProperty(&quot;outcome&quot;, &quot;failed&quot;),
                          new JProperty(&quot;status_code&quot;, resp != null ? (JToken)resp.StatusCode : JValue.CreateNull()),
                          new JProperty(&quot;latency_ms&quot;, decMs),
                          new JProperty(&quot;reason&quot;, &quot;Routing model request failed&quot;),
                          new JProperty(&quot;strategy_type&quot;, &quot;model_based&quot;),
                          new JProperty(&quot;decision_basis&quot;, &quot;model_based_fallback&quot;),
                          new JProperty(&quot;attempt_role&quot;, &quot;routing_decision&quot;),
                          new JProperty(&quot;decision_protocol&quot;, proto),
                          new JProperty(&quot;condition_evaluations&quot;, condEvals)
                        ));
                        if (st[&quot;sticky_candidate&quot;] is JObject) {
                          st[&quot;selected_candidate&quot;] = st[&quot;sticky_candidate&quot;];
                          string sTier = (string)st[&quot;sticky_bound_by&quot;] ?? &quot;interaction&quot;;
                          st[&quot;decision_basis&quot;] = sTier == &quot;session&quot; ? &quot;session_sticky&quot; : &quot;interaction_sticky&quot;;
                        } else {
                          st[&quot;decision_basis&quot;] = &quot;model_based_fallback&quot;;
                        }
                      }
                      return st.ToString(Newtonsoft.Json.Formatting.None);
                    } catch {
                      return (string)context.Variables[&quot;routerPlanState&quot;];
                    }
                  }" />
                </when>
              </choose>

              <!-- Step 5.4: Ensure at least one eligible candidate exists before dispatch -->
              <choose>
                <when condition="@(string.IsNullOrEmpty((string)context.Variables[&quot;routerPlanState&quot;]) || JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;selected_candidate&quot;]?.Type != JTokenType.Object)">
                  <return-response>
                    <set-status code="409" reason="Conflict" />
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-body>{"type":"error","error":{"type":"conflict_error","code":"router_all_candidates_failed","message":"No eligible candidate models available for this router"}}</set-body>
                  </return-response>
                </when>
              </choose>

              <!-- Step 5.5: Dispatch to Winning Candidate (Cross-protocol Anthropic or Direct OpenAI rewrite) -->
              <set-variable name="routerOriginalRequestBody" value="@(context.Request.Body.As&lt;string&gt;(preserveContent: true))" />
              <set-variable name="routerInferenceStarted" value="@(DateTime.UtcNow)" />
              <choose>
                <when condition="@{
                  var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                  return (string)st[&quot;selected_candidate&quot;]?[&quot;profile&quot;] == &quot;anthropic-messages&quot; &amp;&amp; context.Operation.Id == &quot;chat-completions&quot;;
                }">
                  <retry condition="@((string)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;anthropic_status&quot;] == &quot;retry_anthropic&quot;)" count="3" interval="0" first-fast-retry="true">
                  <set-variable name="routerInferenceStarted" value="@(DateTime.UtcNow)" />
                  <send-request mode="new" response-variable-name="routerAnthropicHttp" timeout="@(Math.Max(1, Math.Min(120, (Math.Min((int?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;selected_candidate&quot;]?[&quot;max_latency_ms&quot;] ?? 120000, (int?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;timeout_ms&quot;] ?? 120000) + 999) / 1000)))" ignore-error="true">
                    <set-url>@{
                      return "https://" + context.Request.OriginalUrl.Host + "/" + context.Api.Path.Trim('/') + "/v1/messages";
                    }</set-url>
                    <set-method>POST</set-method>
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-header name="anthropic-version" exists-action="override">
                      <value>2023-06-01</value>
                    </set-header>
                    <set-header name="x-turnstile-router-subrequest" exists-action="override">
                      <value>1</value>
                    </set-header>
                    <set-header name="Ocp-Apim-Subscription-Key" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("Ocp-Apim-Subscription-Key", ""))</value>
                    </set-header>
                    <set-header name="Authorization" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("Authorization", ""))</value>
                    </set-header>
                    <set-header name="x-turnstile-delegation-token" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-turnstile-delegation-token", ""))</value>
                    </set-header>
                    <set-header name="x-request-id" exists-action="override">
                      <value>@((string)context.Variables["routerCallerRequestId"])</value>
                    </set-header>
                    <set-header name="x-hive-run-id" exists-action="override">
                      <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["run_id"])</value>
                    </set-header>
                    <set-header name="x-hive-turn-index" exists-action="override">
                      <value>@(((int)JObject.Parse((string)context.Variables["routerPlanState"])["turn_index"]).ToString())</value>
                    </set-header>
                    <set-header name="x-hive-model" exists-action="override">
                      <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["selected_candidate"]["apim_alias"])</value>
                    </set-header>
                    <set-header name="x-model-id" exists-action="override">
                      <value>@{
                        var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                        return !string.IsNullOrWhiteSpace(context.Request.Headers.GetValueOrDefault(&quot;x-turnstile-delegation-token&quot;, &quot;&quot;))
                          ? (&quot;router:&quot; + ((string)st[&quot;router_id&quot;]).ToLowerInvariant())
                          : (string)st[&quot;selected_candidate&quot;][&quot;model_id&quot;];
                      }</value>
                    </set-header>
                    <set-header name="x-hive-runtime" exists-action="override">
                      <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["selected_candidate"]["runtime_name"])</value>
                    </set-header>
                    <set-header name="x-org-id" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-org-id", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-org-name" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-org-name", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-department-id" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-department-id", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-department-name" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-department-name", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-project-id" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-project-id", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-project-name" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-project-name", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-user-id" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-user-id", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-user-name" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-user-name", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-agent-id" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-agent-id", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-agent-name" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-agent-name", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-request-source" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-request-source", "unattributed"))</value>
                    </set-header>
                    <set-header name="x-hive-workflow" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-hive-workflow", "unattributed"))</value>
                    </set-header>
                    <set-body>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var sel = (JObject)st[&quot;selected_candidate&quot;];
                      var reqBody = JObject.Parse((string)context.Variables[&quot;routerOriginalRequestBody&quot;]);
                      int requestedMaxTok = (int?)reqBody[&quot;max_completion_tokens&quot;] ?? (int?)reqBody[&quot;max_tokens&quot;] ?? 1024;
                      int maxTok = requestedMaxTok &gt; 0 ? requestedMaxTok : 1024;
                      var inMsgs = reqBody[&quot;messages&quot;] as JArray ?? new JArray();
                      var outMsgs = new JArray();
                      string sysText = &quot;&quot;;
                      foreach (var mTok in inMsgs) {
                        var mObj = mTok as JObject;
                        if (mObj == null) { continue; }
                        string role = (string)mObj[&quot;role&quot;] ?? &quot;user&quot;;
                        JToken rawC = mObj[&quot;content&quot;];
                        string textVal = &quot;&quot;;
                        var partBlocks = new JArray();
                        var cArrIn = rawC as JArray;
                        if (rawC == null || rawC.Type == JTokenType.Null) { textVal = &quot;&quot;; }
                        else if (rawC.Type == JTokenType.String) { textVal = (string)rawC; }
                        else if (cArrIn != null) {
                          textVal = string.Join(&quot;\n&quot;, cArrIn.OfType&lt;JObject&gt;().Select(p =&gt; (string)p[&quot;text&quot;] ?? (string)p[&quot;content&quot;] ?? &quot;&quot;).Where(s =&gt; !string.IsNullOrEmpty(s)));
                          foreach (var p in cArrIn.OfType&lt;JObject&gt;()) {
                            string pType = ((string)p[&quot;type&quot;] ?? &quot;text&quot;).Trim().ToLowerInvariant();
                            if (pType == &quot;text&quot;) {
                              string pText = (string)p[&quot;text&quot;] ?? (string)p[&quot;content&quot;] ?? &quot;&quot;;
                              if (!string.IsNullOrEmpty(pText)) {
                                partBlocks.Add(new JObject(new JProperty(&quot;type&quot;, &quot;text&quot;), new JProperty(&quot;text&quot;, pText)));
                              }
                            } else if (pType == &quot;image&quot; &amp;&amp; p[&quot;source&quot;] != null) {
                              partBlocks.Add(p.DeepClone());
                            } else if (pType == &quot;image_url&quot;) {
                              string imgUrl = ((string)p[&quot;image_url&quot;]?[&quot;url&quot;] ?? (string)p[&quot;image_url&quot;] ?? (string)p[&quot;url&quot;] ?? &quot;&quot;).Trim();
                              if (imgUrl.StartsWith(&quot;data:&quot;, StringComparison.OrdinalIgnoreCase) &amp;&amp; imgUrl.IndexOf(&quot;;base64,&quot;, StringComparison.OrdinalIgnoreCase) &gt; 5) {
                                int commaIdx = imgUrl.IndexOf(&apos;,&apos;);
                                int semiIdx = imgUrl.IndexOf(&apos;;&apos;);
                                string mediaType = semiIdx &gt; 5 ? imgUrl.Substring(5, semiIdx - 5) : &quot;image/png&quot;;
                                string base64Data = commaIdx &gt;= 0 ? imgUrl.Substring(commaIdx + 1) : &quot;&quot;;
                                if (!string.IsNullOrEmpty(base64Data)) {
                                  partBlocks.Add(new JObject(
                                    new JProperty(&quot;type&quot;, &quot;image&quot;),
                                    new JProperty(&quot;source&quot;, new JObject(
                                      new JProperty(&quot;type&quot;, &quot;base64&quot;),
                                      new JProperty(&quot;media_type&quot;, mediaType),
                                      new JProperty(&quot;data&quot;, base64Data)
                                    ))
                                  ));
                                }
                              } else if (!string.IsNullOrEmpty(imgUrl)) {
                                partBlocks.Add(new JObject(
                                  new JProperty(&quot;type&quot;, &quot;image&quot;),
                                  new JProperty(&quot;source&quot;, new JObject(
                                    new JProperty(&quot;type&quot;, &quot;url&quot;),
                                    new JProperty(&quot;url&quot;, imgUrl)
                                  ))
                                ));
                              }
                            }
                          }
                        } else { textVal = rawC.ToString(); }
                        if (role == &quot;system&quot; || role == &quot;developer&quot;) {
                          sysText = string.IsNullOrEmpty(sysText) ? textVal : (sysText + &quot;\n\n&quot; + textVal);
                        } else if (role == &quot;tool&quot;) {
                          outMsgs.Add(new JObject(
                            new JProperty(&quot;role&quot;, &quot;user&quot;),
                            new JProperty(&quot;content&quot;, new JArray(new JObject(
                              new JProperty(&quot;type&quot;, &quot;tool_result&quot;),
                              new JProperty(&quot;tool_use_id&quot;, (string)mObj[&quot;tool_call_id&quot;] ?? &quot;&quot;),
                              new JProperty(&quot;content&quot;, textVal)
                            )))
                          ));
                        } else if (role == &quot;assistant&quot; &amp;&amp; (mObj[&quot;tool_calls&quot;] as JArray) != null &amp;&amp; ((JArray)mObj[&quot;tool_calls&quot;]).Count &gt; 0) {
                          var contentBlocks = new JArray();
                          if (partBlocks.Count &gt; 0) {
                            foreach (var pb in partBlocks) { contentBlocks.Add(pb); }
                          } else if (!string.IsNullOrEmpty(textVal)) {
                            contentBlocks.Add(new JObject(new JProperty(&quot;type&quot;, &quot;text&quot;), new JProperty(&quot;text&quot;, textVal)));
                          }
                          foreach (var tcTok in (JArray)mObj[&quot;tool_calls&quot;]) {
                            var tcObj = tcTok as JObject;
                            if (tcObj == null) { continue; }
                            JToken parsedInput = new JObject();
                            string rawArgs = (string)tcObj[&quot;function&quot;]?[&quot;arguments&quot;] ?? &quot;{}&quot;;
                            try { parsedInput = JToken.Parse(string.IsNullOrWhiteSpace(rawArgs) ? &quot;{}&quot; : rawArgs); } catch { parsedInput = new JObject(); }
                            contentBlocks.Add(new JObject(
                              new JProperty(&quot;type&quot;, &quot;tool_use&quot;),
                              new JProperty(&quot;id&quot;, (string)tcObj[&quot;id&quot;] ?? (&quot;call_&quot; + Guid.NewGuid().ToString(&quot;N&quot;).Substring(0, 16))),
                              new JProperty(&quot;name&quot;, (string)tcObj[&quot;function&quot;]?[&quot;name&quot;] ?? &quot;&quot;),
                              new JProperty(&quot;input&quot;, parsedInput)
                            ));
                          }
                          outMsgs.Add(new JObject(
                            new JProperty(&quot;role&quot;, &quot;assistant&quot;),
                            new JProperty(&quot;content&quot;, contentBlocks)
                          ));
                        } else {
                          bool hasImageBlocks = partBlocks.OfType&lt;JObject&gt;().Any(blk =&gt; (string)blk[&quot;type&quot;] == &quot;image&quot;);
                          outMsgs.Add(new JObject(
                            new JProperty(&quot;role&quot;, role == &quot;assistant&quot; ? &quot;assistant&quot; : &quot;user&quot;),
                            new JProperty(&quot;content&quot;, hasImageBlocks ? (JToken)partBlocks : (JToken)textVal)
                          ));
                        }
                      }
                      var anth = new JObject(
                        new JProperty(&quot;model&quot;, (string)sel[&quot;apim_alias&quot;]),
                        new JProperty(&quot;max_tokens&quot;, maxTok),
                        new JProperty(&quot;messages&quot;, outMsgs)
                      );
                      if (!string.IsNullOrEmpty(sysText)) { anth[&quot;system&quot;] = sysText; }
                      var inTools = reqBody[&quot;tools&quot;] as JArray;
                      if (inTools != null &amp;&amp; inTools.Count &gt; 0) {
                        var anthTools = new JArray();
                        foreach (var tTok in inTools) {
                          var tObj = tTok as JObject;
                          if (tObj == null) { continue; }
                          var fn = tObj[&quot;function&quot;] as JObject;
                          if (fn != null) {
                            anthTools.Add(new JObject(
                              new JProperty(&quot;name&quot;, (string)fn[&quot;name&quot;] ?? &quot;&quot;),
                              new JProperty(&quot;description&quot;, (string)fn[&quot;description&quot;] ?? &quot;&quot;),
                              new JProperty(&quot;input_schema&quot;, fn[&quot;parameters&quot;]?.DeepClone() ?? new JObject(new JProperty(&quot;type&quot;, &quot;object&quot;), new JProperty(&quot;properties&quot;, new JObject())))
                            ));
                          } else if (tObj[&quot;name&quot;] != null &amp;&amp; tObj[&quot;input_schema&quot;] != null) {
                            anthTools.Add(tObj.DeepClone());
                          }
                        }
                        if (anthTools.Count &gt; 0) { anth[&quot;tools&quot;] = anthTools; }
                      }
                      JToken tcChoice = reqBody[&quot;tool_choice&quot;];
                      if (tcChoice != null &amp;&amp; tcChoice.Type != JTokenType.Null) {
                        if (tcChoice.Type == JTokenType.String) {
                          string tcMode = ((string)tcChoice ?? &quot;&quot;).Trim().ToLowerInvariant();
                          if (tcMode == &quot;auto&quot;) { anth[&quot;tool_choice&quot;] = new JObject(new JProperty(&quot;type&quot;, &quot;auto&quot;)); }
                          else if (tcMode == &quot;required&quot;) { anth[&quot;tool_choice&quot;] = new JObject(new JProperty(&quot;type&quot;, &quot;any&quot;)); }
                          else if (tcMode == &quot;none&quot;) { anth.Remove(&quot;tools&quot;); anth.Remove(&quot;tool_choice&quot;); }
                        } else {
                          var tcChoiceObj = tcChoice as JObject;
                          if (tcChoiceObj != null) {
                            string fnName = (string)tcChoiceObj[&quot;function&quot;]?[&quot;name&quot;] ?? (string)tcChoiceObj[&quot;name&quot;] ?? &quot;&quot;;
                            if (!string.IsNullOrEmpty(fnName)) {
                              anth[&quot;tool_choice&quot;] = new JObject(new JProperty(&quot;type&quot;, &quot;tool&quot;), new JProperty(&quot;name&quot;, fnName));
                            }
                          }
                        }
                      }
                      return anth.ToString(Newtonsoft.Json.Formatting.None);
                    }</set-body>
                  </send-request>
                  <set-variable name="routerPlanState" value="@{
                    try {
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var resp = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerAnthropicHttp&quot;, null);
                      if (resp != null &amp;&amp; resp.StatusCode &gt;= 200 &amp;&amp; resp.StatusCode &lt; 300) {
                        st[&quot;anthropic_status&quot;] = &quot;succeeded&quot;;
                        return st.ToString(Newtonsoft.Json.Formatting.None);
                      }
                      long infMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;routerInferenceStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
                      var sel = (JObject)st[&quot;selected_candidate&quot;];
                      var attempts = (JArray)st[&quot;attempts&quot;];
                      var condEvals = (JArray)st[&quot;condition_evaluations&quot;];
                      string reqId = (string)context.Variables[&quot;routerCallerRequestId&quot;];
                      int statusCode = resp != null ? resp.StatusCode : 504;
                      string outcome = resp == null || statusCode == 408 || statusCode == 504 ? &quot;timeout&quot; : (statusCode == 429 ? &quot;rate_limited&quot; : &quot;failed&quot;);
                      string trigger = resp == null || statusCode == 408 || statusCode == 504 ? &quot;timeout&quot; : (statusCode == 429 ? &quot;rate_limit&quot; : &quot;error&quot;);
                      int seq = attempts.Count + 1;
                      attempts.Add(new JObject(
                        new JProperty(&quot;sequence&quot;, seq),
                        new JProperty(&quot;attempt_number&quot;, seq),
                        new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                        new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                        new JProperty(&quot;request_id&quot;, reqId),
                        new JProperty(&quot;outcome&quot;, outcome),
                        new JProperty(&quot;status_code&quot;, resp != null ? (JToken)statusCode : JValue.CreateNull()),
                        new JProperty(&quot;latency_ms&quot;, infMs),
                        new JProperty(&quot;priority&quot;, (int?)sel[&quot;priority&quot;]),
                        new JProperty(&quot;weight&quot;, (int?)sel[&quot;weight&quot;]),
                        new JProperty(&quot;max_cost_per_million&quot;, sel[&quot;max_cost_per_million&quot;]),
                        new JProperty(&quot;reason&quot;, &quot;Candidate returned HTTP &quot; + statusCode.ToString()),
                        new JProperty(&quot;strategy_type&quot;, (string)st[&quot;strategy_type&quot;]),
                        new JProperty(&quot;fallback_trigger&quot;, trigger),
                        new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                        new JProperty(&quot;attempt_role&quot;, &quot;candidate&quot;),
                        new JProperty(&quot;condition_evaluations&quot;, seq == 1 ? condEvals : new JArray())
                      ));
                      var fbEvents = st[&quot;fallback_events&quot;] as JArray ?? new JArray();
                      bool canFallback = fbEvents.Values&lt;string&gt;().Any(ev =&gt; string.Equals(ev, trigger, StringComparison.OrdinalIgnoreCase) || string.Equals(ev, &quot;error&quot;, StringComparison.OrdinalIgnoreCase));
                      var triedIds = new HashSet&lt;string&gt;(attempts.OfType&lt;JObject&gt;().Where(a =&gt; (string)a[&quot;attempt_role&quot;] == &quot;candidate&quot;).Select(a =&gt; (string)a[&quot;model_id&quot;] ?? &quot;&quot;), StringComparer.OrdinalIgnoreCase);
                      var nextCand = canFallback
                        ? ((JArray)st[&quot;eligible_candidates&quot;]).OfType&lt;JObject&gt;().FirstOrDefault(c =&gt; !triedIds.Contains((string)c[&quot;model_id&quot;] ?? &quot;&quot;))
                        : null;
                      if (nextCand != null) {
                        st[&quot;selected_candidate&quot;] = nextCand.DeepClone();
                        st[&quot;decision_basis&quot;] = &quot;fallback_success&quot;;
                        st[&quot;sticky_bound_by&quot;] = JValue.CreateNull();
                        st[&quot;sticky_key&quot;] = JValue.CreateNull();
                        st[&quot;anthropic_status&quot;] = (string)nextCand[&quot;profile&quot;] == &quot;anthropic-messages&quot; ? &quot;retry_anthropic&quot; : &quot;fallback&quot;;
                      } else {
                        st[&quot;anthropic_status&quot;] = &quot;failed&quot;;
                        st[&quot;anthropic_error_code&quot;] = statusCode;
                        string errBody = resp != null ? resp.Body.As&lt;string&gt;(preserveContent: true) : &quot;&quot;;
                        if (string.IsNullOrWhiteSpace(errBody)) {
                          errBody = &quot;{\&quot;type\&quot;:\&quot;error\&quot;,\&quot;error\&quot;:{\&quot;type\&quot;:\&quot;api_error\&quot;,\&quot;code\&quot;:\&quot;upstream_error\&quot;,\&quot;message\&quot;:\&quot;Candidate request failed with HTTP &quot; + statusCode.ToString() + &quot;\&quot;}}&quot;;
                        }
                        st[&quot;anthropic_error_body&quot;] = errBody;
                        var usageRecs = new JArray();
                        foreach (JObject att in attempts) {
                          if ((string)att[&quot;outcome&quot;] != &quot;succeeded&quot; || (string)att[&quot;attempt_role&quot;] != &quot;routing_decision&quot;) { continue; }
                          string aReqId = (string)att[&quot;request_id&quot;] ?? reqId;
                          usageRecs.Add(new JObject(
                            new JProperty(&quot;id&quot;, aReqId),
                            new JProperty(&quot;request_id&quot;, aReqId),
                            new JProperty(&quot;correlation_id&quot;, aReqId),
                            new JProperty(&quot;ts&quot;, DateTime.UtcNow.ToString(&quot;o&quot;)),
                            new JProperty(&quot;organization_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;organization&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;department_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;department&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;team&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;project_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;project&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;agent_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;agent&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;request_source&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;&quot;)),
                            new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                            new JProperty(&quot;user&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-user-name&quot;, (string)st[&quot;consumer_key&quot;])),
                            new JProperty(&quot;workflow&quot;, &quot;model-router-decision&quot;),
                            new JProperty(&quot;run_id&quot;, (string)st[&quot;run_id&quot;]),
                            new JProperty(&quot;turn_index&quot;, (int)st[&quot;turn_index&quot;]),
                            new JProperty(&quot;provider&quot;, (string)att[&quot;provider_name&quot;] ?? &quot;&quot;),
                            new JProperty(&quot;model&quot;, (string)att[&quot;model_name&quot;]),
                            new JProperty(&quot;model_id&quot;, (string)att[&quot;model_id&quot;]),
                            new JProperty(&quot;runtime&quot;, (string)att[&quot;runtime_name&quot;] ?? &quot;&quot;),
                            new JProperty(&quot;input_tokens&quot;, (long?)att[&quot;input_tokens&quot;] ?? 0L),
                            new JProperty(&quot;cached_tokens&quot;, (long?)att[&quot;cached_tokens&quot;] ?? 0L),
                            new JProperty(&quot;cache_write_tokens&quot;, (long?)att[&quot;cache_write_tokens&quot;] ?? 0L),
                            new JProperty(&quot;output_tokens&quot;, (long?)att[&quot;output_tokens&quot;] ?? 0L),
                            new JProperty(&quot;latency_ms&quot;, (long?)att[&quot;latency_ms&quot;] ?? 1L),
                            new JProperty(&quot;status&quot;, &quot;200&quot;),
                            new JProperty(&quot;status_code&quot;, 200),
                            new JProperty(&quot;estimated_cost&quot;, att[&quot;estimated_cost&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;input_price_per_million&quot;, att[&quot;input_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;cached_price_per_million&quot;, att[&quot;cached_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;cache_write_price_per_million&quot;, att[&quot;cache_write_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;output_price_per_million&quot;, att[&quot;output_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;price_context_tier&quot;, att[&quot;price_context_tier&quot;] ?? JValue.CreateNull())
                          ));
                        }
                        st[&quot;failed_trace&quot;] = new JObject(
                          new JProperty(&quot;request_id&quot;, reqId),
                          new JProperty(&quot;correlation_id&quot;, context.RequestId.ToString()),
                          new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                          new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                          new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                          new JProperty(&quot;selected_model_id&quot;, (string)sel[&quot;model_id&quot;]),
                          new JProperty(&quot;selected_model_name&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;attempts&quot;, attempts),
                          new JProperty(&quot;usage_records&quot;, usageRecs)
                        );
                      }
                      return st.ToString(Newtonsoft.Json.Formatting.None);
                    } catch {
                      return (string)context.Variables[&quot;routerPlanState&quot;];
                    }
                  }" />
                  </retry>
                  <choose>
                    <when condition="@((string)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;anthropic_status&quot;] == &quot;failed&quot;)">
                      <send-request mode="new" response-variable-name="routerAnthropicFailedTraceWrite" timeout="2" ignore-error="true">
                        <set-url>@{
                          return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='model-router-trace%7Cv1',RowKey='" + Uri.EscapeDataString((string)context.Variables["routerCallerRequestId"]) + "')";
                        }</set-url>
                        <set-method>PUT</set-method>
                        <set-header name="Content-Type" exists-action="override">
                          <value>application/json</value>
                        </set-header>
                        <set-header name="Accept" exists-action="override">
                          <value>application/json;odata=nometadata</value>
                        </set-header>
                        <set-header name="x-ms-version" exists-action="override">
                          <value>2020-12-06</value>
                        </set-header>
                        <set-body>@{
                          var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                          var entity = new JObject(
                            new JProperty(&quot;PartitionKey&quot;, &quot;model-router-trace|v1&quot;),
                            new JProperty(&quot;RowKey&quot;, (string)context.Variables[&quot;routerCallerRequestId&quot;]),
                            new JProperty(&quot;SchemaVersion&quot;, 2),
                            new JProperty(&quot;Document&quot;, st[&quot;failed_trace&quot;].ToString(Newtonsoft.Json.Formatting.None))
                          );
                          return entity.ToString(Newtonsoft.Json.Formatting.None);
                        }</set-body>
                        <authentication-managed-identity resource="https://storage.azure.com/" />
                      </send-request>
                      <return-response>
                        <set-status code="@((int)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;anthropic_error_code&quot;])" reason="Error" />
                        <set-header name="Content-Type" exists-action="override">
                          <value>application/json</value>
                        </set-header>
                        <set-header name="x-request-id" exists-action="override">
                          <value>@((string)context.Variables["routerCallerRequestId"])</value>
                        </set-header>
                        <set-header name="x-router-id" exists-action="override">
                          <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["router_id"])</value>
                        </set-header>
                        <set-header name="x-router-name" exists-action="override">
                          <value>@{
                            var routerName = (string)JObject.Parse((string)context.Variables["routerPlanState"])["router_name"];
                            return routerName.Any(character =&gt; character &gt; 127) ? Uri.EscapeDataString(routerName) : routerName;
                          }</value>
                        </set-header>
                        <set-body>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["anthropic_error_body"])</set-body>
                      </return-response>
                    </when>
                    <when condition="@((string)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;anthropic_status&quot;] == &quot;succeeded&quot;)">
                      <set-variable name="routerAnthropicCompleted" value="@{
                        var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                        var sel = (JObject)st[&quot;selected_candidate&quot;];
                        var reqBody = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
                        bool wantStream = (bool?)reqBody[&quot;stream&quot;] == true;
                        var resp = (IResponse)context.Variables[&quot;routerAnthropicHttp&quot;];
                        long infMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;routerInferenceStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
                        var aBody = JObject.Parse(resp.Body.As&lt;string&gt;(preserveContent: true));
                        string textContent = &quot;&quot;;
                        var toolCalls = new JArray();
                        var cArr = aBody[&quot;content&quot;] as JArray;
                        if (cArr != null) {
                          textContent = string.Join(&quot;&quot;, cArr.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;type&quot;] == &quot;text&quot;).Select(x =&gt; (string)x[&quot;text&quot;] ?? &quot;&quot;));
                          if (string.IsNullOrEmpty(textContent)) {
                            textContent = string.Join(&quot;&quot;, cArr.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;type&quot;] == &quot;thinking&quot;).Select(x =&gt; (string)x[&quot;thinking&quot;] ?? &quot;&quot;));
                          }
                          int tcIdx = 0;
                          foreach (var bObj in cArr.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;type&quot;] == &quot;tool_use&quot;)) {
                            string argsJson = bObj[&quot;input&quot;] != null ? bObj[&quot;input&quot;].ToString(Newtonsoft.Json.Formatting.None) : &quot;{}&quot;;
                            toolCalls.Add(new JObject(
                              new JProperty(&quot;index&quot;, tcIdx),
                              new JProperty(&quot;id&quot;, (string)bObj[&quot;id&quot;] ?? (&quot;call_&quot; + tcIdx.ToString())),
                              new JProperty(&quot;type&quot;, &quot;function&quot;),
                              new JProperty(&quot;function&quot;, new JObject(
                                new JProperty(&quot;name&quot;, (string)bObj[&quot;name&quot;] ?? &quot;&quot;),
                                new JProperty(&quot;arguments&quot;, argsJson)
                              ))
                            ));
                            tcIdx++;
                          }
                        }
                        string stopReason = (string)aBody[&quot;stop_reason&quot;] ?? &quot;&quot;;
                        string finishReason = (stopReason == &quot;tool_use&quot; || toolCalls.Count &gt; 0) ? &quot;tool_calls&quot; : (stopReason == &quot;max_tokens&quot; ? &quot;length&quot; : &quot;stop&quot;);
                        var outMsgObj = new JObject(
                          new JProperty(&quot;role&quot;, &quot;assistant&quot;),
                          new JProperty(&quot;content&quot;, (toolCalls.Count &gt; 0 &amp;&amp; string.IsNullOrEmpty(textContent)) ? JValue.CreateNull() : (JToken)textContent)
                        );
                        if (toolCalls.Count &gt; 0) {
                          outMsgObj[&quot;tool_calls&quot;] = toolCalls;
                        }
                        var uObj = aBody[&quot;usage&quot;] as JObject ?? new JObject();
                        long inTok = (long?)uObj[&quot;input_tokens&quot;] ?? 0L;
                        long cacheReadTok = (long?)uObj[&quot;cache_read_input_tokens&quot;] ?? 0L;
                        long cacheWriteTok = (long?)uObj[&quot;cache_creation_input_tokens&quot;] ?? 0L;
                        long cachedTok = cacheReadTok + cacheWriteTok;
                        long outTok = (long?)uObj[&quot;output_tokens&quot;] ?? 0L;
                        long totalInTok = inTok + cachedTok;
                        long totalTok = totalInTok + outTok;
                        double? inRate = (double?)sel[&quot;input_cost_per_million&quot;];
                        double? outRate = (double?)sel[&quot;output_cost_per_million&quot;];
                        double? cRate = (double?)sel[&quot;cached_cost_per_million&quot;] ?? inRate;
                        double? cwRate = (double?)sel[&quot;cache_write_cost_per_million&quot;] ?? cRate;
                        string sTierKey = null;
                        var sTiers = sel[&quot;context_price_tiers&quot;] as JArray;
                        if (sTiers != null &amp;&amp; sTiers.Count &gt; 0) {
                          JObject matchedTier = null;
                          foreach (JObject tObj in sTiers.OfType&lt;JObject&gt;()) {
                            long minTok = (long?)tObj[&quot;min_input_tokens&quot;] ?? 0L;
                            long? maxTok = (long?)tObj[&quot;max_input_tokens&quot;];
                            if (totalInTok &gt;= minTok &amp;&amp; (!maxTok.HasValue || totalInTok &lt;= maxTok.Value)) {
                              matchedTier = tObj;
                              break;
                            }
                          }
                          if (matchedTier == null) { matchedTier = (JObject)sTiers[sTiers.Count - 1]; }
                          inRate = (double?)matchedTier[&quot;input_price_per_million&quot;];
                          outRate = (double?)matchedTier[&quot;output_price_per_million&quot;];
                          cRate = (double?)matchedTier[&quot;cached_price_per_million&quot;] ?? inRate;
                          cwRate = (double?)matchedTier[&quot;cache_write_price_per_million&quot;] ?? cRate;
                          sTierKey = (string)matchedTier[&quot;key&quot;];
                        }
                        double? mainCost = (inRate.HasValue &amp;&amp; outRate.HasValue)
                          ? (double?)Math.Round((inTok * inRate.Value + cacheReadTok * (cRate ?? inRate.Value) + cacheWriteTok * (cwRate ?? cRate ?? inRate.Value) + outTok * outRate.Value) / 1000000.0, 8)
                          : null;

                        var attempts = (JArray)st[&quot;attempts&quot;];
                        var condEvals = (JArray)st[&quot;condition_evaluations&quot;];
                        string reqId = (string)context.Variables[&quot;routerCallerRequestId&quot;];
                        int seq = attempts.Count + 1;
                        attempts.Add(new JObject(
                          new JProperty(&quot;sequence&quot;, seq),
                          new JProperty(&quot;attempt_number&quot;, seq),
                          new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                          new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;request_id&quot;, reqId),
                          new JProperty(&quot;outcome&quot;, &quot;succeeded&quot;),
                          new JProperty(&quot;status_code&quot;, resp.StatusCode),
                          new JProperty(&quot;latency_ms&quot;, infMs),
                          new JProperty(&quot;priority&quot;, (int?)sel[&quot;priority&quot;]),
                          new JProperty(&quot;weight&quot;, (int?)sel[&quot;weight&quot;]),
                          new JProperty(&quot;max_cost_per_million&quot;, sel[&quot;max_cost_per_million&quot;]),
                          new JProperty(&quot;reason&quot;, (string)st[&quot;sticky_bound_by&quot;] != null ? &quot;Reused sticky session binding&quot; : &quot;Selected candidate succeeded&quot;),
                          new JProperty(&quot;strategy_type&quot;, (string)st[&quot;strategy_type&quot;]),
                          new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                          new JProperty(&quot;attempt_role&quot;, &quot;candidate&quot;),
                          new JProperty(&quot;sticky_bound_by&quot;, st[&quot;sticky_bound_by&quot;]),
                          new JProperty(&quot;sticky_key&quot;, st[&quot;sticky_key&quot;]),
                          new JProperty(&quot;condition_evaluations&quot;, seq == 1 ? condEvals : new JArray()),
                          new JProperty(&quot;input_tokens&quot;, inTok),
                          new JProperty(&quot;cached_tokens&quot;, cachedTok),
                          new JProperty(&quot;cache_write_tokens&quot;, cacheWriteTok),
                          new JProperty(&quot;output_tokens&quot;, outTok),
                          new JProperty(&quot;total_tokens&quot;, totalTok),
                          new JProperty(&quot;estimated_cost&quot;, mainCost.HasValue ? (JToken)new JValue(mainCost.Value) : JValue.CreateNull()),
                          new JProperty(&quot;input_price_per_million&quot;, inRate.HasValue ? (JToken)new JValue(inRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;cached_price_per_million&quot;, cRate.HasValue ? (JToken)new JValue(cRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;cache_write_price_per_million&quot;, cwRate.HasValue ? (JToken)new JValue(cwRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;output_price_per_million&quot;, outRate.HasValue ? (JToken)new JValue(outRate.Value) : JValue.CreateNull()),
                          new JProperty(&quot;price_context_tier&quot;, sTierKey != null ? (JToken)new JValue(sTierKey) : JValue.CreateNull())
                        ));
                        double totalCost = attempts.OfType&lt;JObject&gt;().Sum(a =&gt; (double?)a[&quot;estimated_cost&quot;] ?? 0.0);
                        long totalLat = ((long)st[&quot;decision_ms&quot;]) + infMs;

                        var usageRecs = new JArray();
                        foreach (JObject att in attempts) {
                          if ((string)att[&quot;outcome&quot;] != &quot;succeeded&quot;) { continue; }
                          bool isDec = (string)att[&quot;attempt_role&quot;] == &quot;routing_decision&quot;;
                          if (!isDec) { continue; }
                          string aReqId = (string)att[&quot;request_id&quot;] ?? reqId;
                          usageRecs.Add(new JObject(
                            new JProperty(&quot;id&quot;, aReqId),
                            new JProperty(&quot;request_id&quot;, aReqId),
                            new JProperty(&quot;correlation_id&quot;, isDec ? aReqId : context.RequestId.ToString()),
                            new JProperty(&quot;ts&quot;, DateTime.UtcNow.ToString(&quot;o&quot;)),
                            new JProperty(&quot;organization_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;organization&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;department_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;department&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;team&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;project_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;project&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;agent_id&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;&quot;)),
                            new JProperty(&quot;agent&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;&quot;)),
                            new JProperty(&quot;request_source&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;&quot;)),
                            new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                            new JProperty(&quot;user&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-user-name&quot;, (string)st[&quot;consumer_key&quot;])),
                            new JProperty(&quot;workflow&quot;, isDec ? &quot;model-router-decision&quot; : (string)st[&quot;task_type&quot;]),
                            new JProperty(&quot;run_id&quot;, (string)st[&quot;run_id&quot;]),
                            new JProperty(&quot;turn_index&quot;, (int)st[&quot;turn_index&quot;]),
                            new JProperty(&quot;provider&quot;, !string.IsNullOrEmpty((string)att[&quot;provider_name&quot;]) ? (string)att[&quot;provider_name&quot;] : (string)sel[&quot;provider_name&quot;]),
                            new JProperty(&quot;model&quot;, (string)att[&quot;model_name&quot;]),
                            new JProperty(&quot;model_id&quot;, (string)att[&quot;model_id&quot;]),
                            new JProperty(&quot;runtime&quot;, !string.IsNullOrEmpty((string)att[&quot;runtime_name&quot;]) ? (string)att[&quot;runtime_name&quot;] : (string)sel[&quot;runtime_name&quot;]),
                            new JProperty(&quot;input_tokens&quot;, (long?)att[&quot;input_tokens&quot;] ?? 0L),
                            new JProperty(&quot;cached_tokens&quot;, (long?)att[&quot;cached_tokens&quot;] ?? 0L),
                            new JProperty(&quot;cache_write_tokens&quot;, (long?)att[&quot;cache_write_tokens&quot;] ?? 0L),
                            new JProperty(&quot;output_tokens&quot;, (long?)att[&quot;output_tokens&quot;] ?? 0L),
                            new JProperty(&quot;latency_ms&quot;, (long?)att[&quot;latency_ms&quot;] ?? 1L),
                            new JProperty(&quot;status&quot;, &quot;200&quot;),
                            new JProperty(&quot;status_code&quot;, 200),
                            new JProperty(&quot;estimated_cost&quot;, att[&quot;estimated_cost&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;input_price_per_million&quot;, att[&quot;input_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;cached_price_per_million&quot;, att[&quot;cached_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;cache_write_price_per_million&quot;, att[&quot;cache_write_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;output_price_per_million&quot;, att[&quot;output_price_per_million&quot;] ?? JValue.CreateNull()),
                            new JProperty(&quot;price_context_tier&quot;, att[&quot;price_context_tier&quot;] ?? JValue.CreateNull())
                          ));
                        }

                        var traceDoc = new JObject(
                          new JProperty(&quot;request_id&quot;, reqId),
                          new JProperty(&quot;correlation_id&quot;, context.RequestId.ToString()),
                          new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                          new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                          new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                          new JProperty(&quot;selected_model_id&quot;, (string)sel[&quot;model_id&quot;]),
                          new JProperty(&quot;selected_model_name&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;attempts&quot;, attempts),
                          new JProperty(&quot;usage_records&quot;, usageRecs)
                        );

                        var chatResp = new JObject(
                          new JProperty(&quot;id&quot;, reqId),
                          new JProperty(&quot;object&quot;, &quot;chat.completion&quot;),
                          new JProperty(&quot;created&quot;, (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds),
                          new JProperty(&quot;model&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;choices&quot;, new JArray(
                            new JObject(
                              new JProperty(&quot;index&quot;, 0),
                              new JProperty(&quot;message&quot;, outMsgObj),
                              new JProperty(&quot;finish_reason&quot;, finishReason)
                            )
                          )),
                          new JProperty(&quot;usage&quot;, new JObject(
                            new JProperty(&quot;prompt_tokens&quot;, inTok + cachedTok),
                            new JProperty(&quot;cached_tokens&quot;, cachedTok),
                            new JProperty(&quot;cache_write_tokens&quot;, cacheWriteTok),
                            new JProperty(&quot;completion_tokens&quot;, outTok),
                            new JProperty(&quot;total_tokens&quot;, totalTok)
                          )),
                          new JProperty(&quot;turnstile_routing&quot;, new JObject(
                            new JProperty(&quot;request_id&quot;, reqId),
                            new JProperty(&quot;correlation_id&quot;, context.RequestId.ToString()),
                            new JProperty(&quot;requested_model&quot;, (string)context.Variables[&quot;routerRequestedModel&quot;]),
                            new JProperty(&quot;routed&quot;, true),
                            new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                            new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                            new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                            new JProperty(&quot;selected_model_id&quot;, (string)sel[&quot;model_id&quot;]),
                            new JProperty(&quot;selected_model_name&quot;, (string)sel[&quot;display_name&quot;]),
                            new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                            new JProperty(&quot;sticky_bound_by&quot;, st[&quot;sticky_bound_by&quot;]),
                            new JProperty(&quot;sticky_key&quot;, st[&quot;sticky_key&quot;]),
                            new JProperty(&quot;session_id&quot;, st[&quot;session_id&quot;]),
                            new JProperty(&quot;interaction_id&quot;, st[&quot;interaction_id&quot;]),
                            new JProperty(&quot;auth_method&quot;, context.Subscription != null ? &quot;subscription_key&quot; : &quot;entra&quot;),
                            new JProperty(&quot;apim_subscription_id&quot;, context.Subscription != null ? context.Subscription.Id : null),
                            new JProperty(&quot;provider&quot;, (string)sel[&quot;provider_name&quot;]),
                            new JProperty(&quot;runtime&quot;, (string)sel[&quot;runtime_name&quot;]),
                            new JProperty(&quot;gateway&quot;, context.Request.OriginalUrl.Host),
                            new JProperty(&quot;latency_ms&quot;, totalLat),
                            new JProperty(&quot;estimated_cost&quot;, Math.Round(totalCost, 8)),
                            new JProperty(&quot;router_attempts&quot;, attempts)
                          ))
                        );
                        long createdEpoch = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                        var sseDelta1 = new JObject(
                          new JProperty(&quot;role&quot;, &quot;assistant&quot;),
                          new JProperty(&quot;content&quot;, (toolCalls.Count &gt; 0 &amp;&amp; string.IsNullOrEmpty(textContent)) ? JValue.CreateNull() : (JToken)textContent)
                        );
                        if (toolCalls.Count &gt; 0) {
                          sseDelta1[&quot;tool_calls&quot;] = toolCalls.DeepClone();
                        }
                        var sseChunk1 = new JObject(
                          new JProperty(&quot;id&quot;, reqId),
                          new JProperty(&quot;object&quot;, &quot;chat.completion.chunk&quot;),
                          new JProperty(&quot;created&quot;, createdEpoch),
                          new JProperty(&quot;model&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;choices&quot;, new JArray(
                            new JObject(
                              new JProperty(&quot;index&quot;, 0),
                              new JProperty(&quot;delta&quot;, sseDelta1),
                              new JProperty(&quot;finish_reason&quot;, JValue.CreateNull())
                            )
                          ))
                        );
                        var sseChunk2 = new JObject(
                          new JProperty(&quot;id&quot;, reqId),
                          new JProperty(&quot;object&quot;, &quot;chat.completion.chunk&quot;),
                          new JProperty(&quot;created&quot;, createdEpoch),
                          new JProperty(&quot;model&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;choices&quot;, new JArray(
                            new JObject(
                              new JProperty(&quot;index&quot;, 0),
                              new JProperty(&quot;delta&quot;, new JObject()),
                              new JProperty(&quot;finish_reason&quot;, finishReason)
                            )
                          )),
                          new JProperty(&quot;usage&quot;, chatResp[&quot;usage&quot;])
                        );
                        string sseBody = &quot;data: &quot; + sseChunk1.ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;
                          + &quot;data: &quot; + sseChunk2.ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;
                          + &quot;data: [DONE]\n\n&quot;;
                        return new JObject(
                          new JProperty(&quot;trace&quot;, traceDoc),
                          new JProperty(&quot;want_stream&quot;, wantStream),
                          new JProperty(&quot;sse_body&quot;, sseBody),
                          new JProperty(&quot;response&quot;, chatResp),
                          new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                          new JProperty(&quot;model_alias&quot;, (string)sel[&quot;apim_alias&quot;]),
                          new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                          new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                          new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                          new JProperty(&quot;decision_ms&quot;, (long)st[&quot;decision_ms&quot;]),
                          new JProperty(&quot;sticky_bound_by&quot;, st[&quot;sticky_bound_by&quot;])
                        ).ToString(Newtonsoft.Json.Formatting.None);
                      }" />
                      <send-request mode="new" response-variable-name="routerAnthropicTraceWrite" timeout="2" ignore-error="true">
                        <set-url>@{
                          return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='model-router-trace%7Cv1',RowKey='" + Uri.EscapeDataString((string)context.Variables["routerCallerRequestId"]) + "')";
                        }</set-url>
                        <set-method>PUT</set-method>
                        <set-header name="Content-Type" exists-action="override">
                          <value>application/json</value>
                        </set-header>
                        <set-header name="Accept" exists-action="override">
                          <value>application/json;odata=nometadata</value>
                        </set-header>
                        <set-header name="x-ms-version" exists-action="override">
                          <value>2020-12-06</value>
                        </set-header>
                        <set-body>@{
                          var comp = JObject.Parse((string)context.Variables[&quot;routerAnthropicCompleted&quot;]);
                          var entity = new JObject(
                            new JProperty(&quot;PartitionKey&quot;, &quot;model-router-trace|v1&quot;),
                            new JProperty(&quot;RowKey&quot;, (string)context.Variables[&quot;routerCallerRequestId&quot;]),
                            new JProperty(&quot;SchemaVersion&quot;, 2),
                            new JProperty(&quot;Document&quot;, comp[&quot;trace&quot;].ToString(Newtonsoft.Json.Formatting.None))
                          );
                          return entity.ToString(Newtonsoft.Json.Formatting.None);
                        }</set-body>
                        <authentication-managed-identity resource="https://storage.azure.com/" />
                      </send-request>
                      <!-- Persist sticky session bindings ONLY after Anthropic candidate succeeded -->
                      <choose>
                        <when condition="@(((JArray)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;save_keys&quot;]).Count &gt; 0)">
                          <send-request mode="new" response-variable-name="routerStickyWrite0" timeout="2" ignore-error="true">
                            <set-url>@{
                              var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                              var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[0];
                              var pk = &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;];
                              var rk = (string)sk[&quot;row_key&quot;];
                              return &quot;__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='&quot; + Uri.EscapeDataString(pk) + &quot;',RowKey='&quot; + Uri.EscapeDataString(rk) + &quot;')&quot;;
                            }</set-url>
                            <set-method>PUT</set-method>
                            <set-header name="Content-Type" exists-action="override">
                              <value>application/json</value>
                            </set-header>
                            <set-header name="Accept" exists-action="override">
                              <value>application/json;odata=nometadata</value>
                            </set-header>
                            <set-header name="x-ms-version" exists-action="override">
                              <value>2020-12-06</value>
                            </set-header>
                            <set-body>@{
                              var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                              var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[0];
                              var sel = (JObject)st[&quot;selected_candidate&quot;];
                              long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                              var doc = new JObject(
                                new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                                new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                                new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                                new JProperty(&quot;tier&quot;, (string)sk[&quot;tier&quot;]),
                                new JProperty(&quot;sticky_key&quot;, (string)sk[&quot;sticky_key&quot;]),
                                new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                                new JProperty(&quot;updated_epoch&quot;, now),
                                new JProperty(&quot;affinity_cookie&quot;, (string)st[&quot;affinity_cookie&quot;] ?? &quot;&quot;)
                              );
                              var entity = new JObject(
                                new JProperty(&quot;PartitionKey&quot;, &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;]),
                                new JProperty(&quot;RowKey&quot;, (string)sk[&quot;row_key&quot;]),
                                new JProperty(&quot;SchemaVersion&quot;, 2),
                                new JProperty(&quot;Document&quot;, doc.ToString(Newtonsoft.Json.Formatting.None))
                              );
                              return entity.ToString(Newtonsoft.Json.Formatting.None);
                            }</set-body>
                            <authentication-managed-identity resource="https://storage.azure.com/" />
                          </send-request>
                          <choose>
                            <when condition="@(((JArray)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;save_keys&quot;]).Count &gt; 1)">
                              <send-request mode="new" response-variable-name="routerStickyWrite1" timeout="2" ignore-error="true">
                                <set-url>@{
                                  var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                                  var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[1];
                                  var pk = &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;];
                                  var rk = (string)sk[&quot;row_key&quot;];
                                  return &quot;__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='&quot; + Uri.EscapeDataString(pk) + &quot;',RowKey='&quot; + Uri.EscapeDataString(rk) + &quot;')&quot;;
                                }</set-url>
                                <set-method>PUT</set-method>
                                <set-header name="Content-Type" exists-action="override">
                                  <value>application/json</value>
                                </set-header>
                                <set-header name="Accept" exists-action="override">
                                  <value>application/json;odata=nometadata</value>
                                </set-header>
                                <set-header name="x-ms-version" exists-action="override">
                                  <value>2020-12-06</value>
                                </set-header>
                                <set-body>@{
                                  var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                                  var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[1];
                                  var sel = (JObject)st[&quot;selected_candidate&quot;];
                                  long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                                  var doc = new JObject(
                                    new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                                    new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                                    new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                                    new JProperty(&quot;tier&quot;, (string)sk[&quot;tier&quot;]),
                                    new JProperty(&quot;sticky_key&quot;, (string)sk[&quot;sticky_key&quot;]),
                                    new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                                    new JProperty(&quot;updated_epoch&quot;, now),
                                    new JProperty(&quot;affinity_cookie&quot;, (string)st[&quot;affinity_cookie&quot;] ?? &quot;&quot;)
                                  );
                                  var entity = new JObject(
                                    new JProperty(&quot;PartitionKey&quot;, &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;]),
                                    new JProperty(&quot;RowKey&quot;, (string)sk[&quot;row_key&quot;]),
                                    new JProperty(&quot;SchemaVersion&quot;, 2),
                                    new JProperty(&quot;Document&quot;, doc.ToString(Newtonsoft.Json.Formatting.None))
                                  );
                                  return entity.ToString(Newtonsoft.Json.Formatting.None);
                                }</set-body>
                                <authentication-managed-identity resource="https://storage.azure.com/" />
                              </send-request>
                            </when>
                          </choose>
                        </when>
                      </choose>
                      <return-response>
                        <set-status code="200" reason="OK" />
                        <set-header name="Content-Type" exists-action="override">
                          <value>@((bool)JObject.Parse((string)context.Variables["routerAnthropicCompleted"])["want_stream"] ? "text/event-stream; charset=utf-8" : "application/json")</value>
                        </set-header>
                        <set-header name="x-request-id" exists-action="override">
                          <value>@((string)context.Variables["routerCallerRequestId"])</value>
                        </set-header>
                        <set-header name="x-routed-model" exists-action="override">
                          <value>@((string)JObject.Parse((string)context.Variables["routerAnthropicCompleted"])["model_alias"])</value>
                        </set-header>
                        <set-header name="x-router-id" exists-action="override">
                          <value>@((string)JObject.Parse((string)context.Variables["routerAnthropicCompleted"])["router_id"])</value>
                        </set-header>
                        <set-header name="x-router-name" exists-action="override">
                          <value>@{
                            var routerName = (string)JObject.Parse((string)context.Variables["routerAnthropicCompleted"])["router_name"];
                            return routerName.Any(character =&gt; character &gt; 127) ? Uri.EscapeDataString(routerName) : routerName;
                          }</value>
                        </set-header>
                        <set-header name="x-router-reason" exists-action="override">
                          <value>@((string)JObject.Parse((string)context.Variables["routerAnthropicCompleted"])["decision_basis"])</value>
                        </set-header>
                        <set-header name="x-router-decision-ms" exists-action="override">
                          <value>@(((long)JObject.Parse((string)context.Variables["routerAnthropicCompleted"])["decision_ms"]).ToString())</value>
                        </set-header>
                        <set-header name="x-auth-method" exists-action="override">
                          <value>@(context.Subscription != null ? "subscription_key" : "entra")</value>
                        </set-header>
                        <set-header name="x-apim-subscription-id" exists-action="override">
                          <value>@(context.Subscription != null ? context.Subscription.Id : "")</value>
                        </set-header>
                        <set-body>@{
                          var comp = JObject.Parse((string)context.Variables[&quot;routerAnthropicCompleted&quot;]);
                          return (bool)comp[&quot;want_stream&quot;]
                            ? (string)comp[&quot;sse_body&quot;]
                            : comp[&quot;response&quot;].ToString(Newtonsoft.Json.Formatting.None);
                        }</set-body>
                      </return-response>
                    </when>
                  </choose>
                </when>
              </choose>
              <!-- Standard OpenAI candidate (or fallback candidate after Anthropic failure): rewrite request body model to selected candidate's apim_alias -->
              <set-variable name="routerInferenceStarted" value="@(DateTime.UtcNow)" />
              <set-header name="x-hive-run-id" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["run_id"])</value>
              </set-header>
              <set-header name="x-hive-turn-index" exists-action="override">
                <value>@(((int)JObject.Parse((string)context.Variables["routerPlanState"])["turn_index"]).ToString())</value>
              </set-header>
              <set-header name="x-hive-model" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["selected_candidate"]["apim_alias"])</value>
              </set-header>
              <set-header name="x-model-id" exists-action="override">
                <value>@{
                  var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                  return !string.IsNullOrWhiteSpace(context.Request.Headers.GetValueOrDefault(&quot;x-turnstile-delegation-token&quot;, &quot;&quot;))
                    ? (&quot;router:&quot; + ((string)st[&quot;router_id&quot;]).ToLowerInvariant())
                    : (string)st[&quot;selected_candidate&quot;][&quot;model_id&quot;];
                }</value>
              </set-header>
              <set-header name="x-hive-runtime" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["selected_candidate"]["runtime_name"])</value>
              </set-header>
              <set-body>@{
                var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                var sel = (JObject)st[&quot;selected_candidate&quot;];
                var b = JObject.Parse((string)context.Variables[&quot;routerOriginalRequestBody&quot;]);
                b[&quot;model&quot;] = (string)sel[&quot;apim_alias&quot;];
                if (b[&quot;router_context&quot;] != null) {
                  b.Remove(&quot;router_context&quot;);
                }
                return b.ToString(Newtonsoft.Json.Formatting.None);
              }</set-body>
            </when>
          </choose>
        </when>
      </choose>
    </when>
  </choose>
</inbound>"""


_OUTBOUND_ROUTER_ENGINE_XML = r"""<outbound>
  <choose id="turnstile-router-engine">
    <when condition="@(context.Api.Id.Split(';')[0] == &quot;__API_ID__&quot; &amp;&amp; (context.Operation.Id == &quot;chat-completions&quot; || context.Operation.Id == &quot;responses&quot;) &amp;&amp; !context.Request.Headers.ContainsKey(&quot;x-turnstile-router-subrequest&quot;))">
      <choose>
        <when condition="@(context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;routerMatched&quot;, false) &amp;&amp; !string.IsNullOrEmpty(context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerPlanState&quot;, &quot;&quot;)))">
          <choose>
            <when condition="@(context.Response.StatusCode &gt;= 400)">
              <set-variable name="routerPlanState" value="@{
                try {
                  var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                  long infMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;routerInferenceStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
                  var sel = (JObject)st[&quot;selected_candidate&quot;];
                  var attempts = (JArray)st[&quot;attempts&quot;];
                  var condEvals = (JArray)st[&quot;condition_evaluations&quot;];
                  string reqId = context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerCallerRequestId&quot;, context.RequestId.ToString());
                  int statusCode = context.Response.StatusCode;
                  string outcome = statusCode == 408 || statusCode == 504 ? &quot;timeout&quot; : (statusCode == 429 ? &quot;rate_limited&quot; : &quot;failed&quot;);
                  string trigger = statusCode == 408 || statusCode == 504 ? &quot;timeout&quot; : (statusCode == 429 ? &quot;rate_limit&quot; : &quot;error&quot;);
                  int seq = attempts.Count + 1;
                  attempts.Add(new JObject(
                    new JProperty(&quot;sequence&quot;, seq),
                    new JProperty(&quot;attempt_number&quot;, seq),
                    new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                    new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                    new JProperty(&quot;request_id&quot;, reqId),
                    new JProperty(&quot;outcome&quot;, outcome),
                    new JProperty(&quot;status_code&quot;, statusCode),
                    new JProperty(&quot;latency_ms&quot;, infMs),
                    new JProperty(&quot;priority&quot;, (int?)sel[&quot;priority&quot;]),
                    new JProperty(&quot;weight&quot;, (int?)sel[&quot;weight&quot;]),
                    new JProperty(&quot;max_cost_per_million&quot;, sel[&quot;max_cost_per_million&quot;]),
                    new JProperty(&quot;reason&quot;, &quot;Candidate returned HTTP &quot; + statusCode.ToString()),
                    new JProperty(&quot;strategy_type&quot;, (string)st[&quot;strategy_type&quot;]),
                    new JProperty(&quot;fallback_trigger&quot;, trigger),
                    new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                    new JProperty(&quot;attempt_role&quot;, &quot;candidate&quot;),
                    new JProperty(&quot;condition_evaluations&quot;, seq == 1 ? condEvals : new JArray())
                  ));
                  __ROUTER_FALLBACK_PLAN__
                  return st.ToString(Newtonsoft.Json.Formatting.None);
                } catch {
                  return (string)context.Variables[&quot;routerPlanState&quot;];
                }
              }" />
              <choose>
                <when condition="@((bool?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;needs_outbound_fallback&quot;] == true)">
                  <retry condition="@((bool?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;needs_outbound_fallback&quot;] == true)" count="3" interval="0" first-fast-retry="true">
                  <set-variable name="routerInferenceStarted" value="@(DateTime.UtcNow)" />
                  <send-request mode="new" response-variable-name="routerFallbackHttp" timeout="@(Math.Max(1, Math.Min(120, (Math.Min((int?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;selected_candidate&quot;]?[&quot;max_latency_ms&quot;] ?? 120000, (int?)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;timeout_ms&quot;] ?? 120000) + 999) / 1000)))" ignore-error="true">
                    <set-url>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      if ((string)st[&quot;selected_candidate&quot;]?[&quot;profile&quot;] == &quot;anthropic-messages&quot;) {
                        return &quot;https://&quot; + context.Request.OriginalUrl.Host + &quot;/&quot; + context.Api.Path.Trim('/') + &quot;/v1/messages&quot;;
                      }
                      return &quot;https://&quot; + context.Request.OriginalUrl.Host + context.Request.OriginalUrl.Path + context.Request.OriginalUrl.QueryString;
                    }</set-url>
                    <set-method>POST</set-method>
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-header name="anthropic-version" exists-action="override">
                      <value>2023-06-01</value>
                    </set-header>
                    <set-header name="x-turnstile-router-subrequest" exists-action="override">
                      <value>1</value>
                    </set-header>
                    <set-header name="Ocp-Apim-Subscription-Key" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("Ocp-Apim-Subscription-Key", ""))</value>
                    </set-header>
                    <set-header name="Authorization" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("Authorization", ""))</value>
                    </set-header>
                    <set-header name="x-turnstile-delegation-token" exists-action="override">
                      <value>@(context.Request.Headers.GetValueOrDefault("x-turnstile-delegation-token", ""))</value>
                    </set-header>
                    <set-header name="x-request-id" exists-action="override">
                      <value>@((string)context.Variables["routerCallerRequestId"])</value>
                    </set-header>
                    <set-header name="x-hive-run-id" exists-action="override">
                      <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["run_id"])</value>
                    </set-header>
                    <set-header name="x-hive-turn-index" exists-action="override">
                      <value>@(((int)JObject.Parse((string)context.Variables["routerPlanState"])["turn_index"]).ToString())</value>
                    </set-header>
                    <set-header name="x-hive-model" exists-action="override">
                      <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["selected_candidate"]["apim_alias"])</value>
                    </set-header>
                    <set-header name="x-model-id" exists-action="override">
                      <value>@{
                        var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                        return !string.IsNullOrWhiteSpace(context.Request.Headers.GetValueOrDefault(&quot;x-turnstile-delegation-token&quot;, &quot;&quot;))
                          ? (&quot;router:&quot; + ((string)st[&quot;router_id&quot;]).ToLowerInvariant())
                          : (string)st[&quot;selected_candidate&quot;][&quot;model_id&quot;];
                      }</value>
                    </set-header>
                    <set-header name="x-hive-runtime" exists-action="override">
                      <value>@((string)JObject.Parse((string)context.Variables["routerPlanState"])["selected_candidate"]["runtime_name"])</value>
                    </set-header>
                    <set-body>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var sel = (JObject)st[&quot;selected_candidate&quot;];
                      var b = JObject.Parse((string)context.Variables[&quot;routerOriginalRequestBody&quot;]);
                      if ((string)sel[&quot;profile&quot;] == &quot;anthropic-messages&quot;) {
                        int requestedMaxTok = (int?)b[&quot;max_completion_tokens&quot;] ?? (int?)b[&quot;max_tokens&quot;] ?? 1024;
                        int maxTok = requestedMaxTok &gt; 0 ? requestedMaxTok : 1024;
                        var inMsgs = b[&quot;messages&quot;] as JArray ?? new JArray();
                        var outMsgs = new JArray();
                        string sysText = &quot;&quot;;
                        foreach (var mTok in inMsgs) {
                          var mObj = mTok as JObject;
                          if (mObj == null) { continue; }
                          string role = (string)mObj[&quot;role&quot;] ?? &quot;user&quot;;
                          JToken rawC = mObj[&quot;content&quot;];
                          string textVal = &quot;&quot;;
                          var partBlocks = new JArray();
                          var cArrIn = rawC as JArray;
                          if (rawC == null || rawC.Type == JTokenType.Null) { textVal = &quot;&quot;; }
                          else if (rawC.Type == JTokenType.String) { textVal = (string)rawC; }
                          else if (cArrIn != null) {
                            textVal = string.Join(&quot;\n&quot;, cArrIn.OfType&lt;JObject&gt;().Select(p =&gt; (string)p[&quot;text&quot;] ?? (string)p[&quot;content&quot;] ?? &quot;&quot;).Where(s =&gt; !string.IsNullOrEmpty(s)));
                            foreach (var p in cArrIn.OfType&lt;JObject&gt;()) {
                              string pType = ((string)p[&quot;type&quot;] ?? &quot;text&quot;).Trim().ToLowerInvariant();
                              if (pType == &quot;text&quot;) {
                                string pText = (string)p[&quot;text&quot;] ?? (string)p[&quot;content&quot;] ?? &quot;&quot;;
                                if (!string.IsNullOrEmpty(pText)) {
                                  partBlocks.Add(new JObject(new JProperty(&quot;type&quot;, &quot;text&quot;), new JProperty(&quot;text&quot;, pText)));
                                }
                              } else if (pType == &quot;image&quot; &amp;&amp; p[&quot;source&quot;] != null) {
                                partBlocks.Add(p.DeepClone());
                              } else if (pType == &quot;image_url&quot;) {
                                string imgUrl = ((string)p[&quot;image_url&quot;]?[&quot;url&quot;] ?? (string)p[&quot;image_url&quot;] ?? (string)p[&quot;url&quot;] ?? &quot;&quot;).Trim();
                                if (imgUrl.StartsWith(&quot;data:&quot;, StringComparison.OrdinalIgnoreCase) &amp;&amp; imgUrl.IndexOf(&quot;;base64,&quot;, StringComparison.OrdinalIgnoreCase) &gt; 5) {
                                  int commaIdx = imgUrl.IndexOf(&apos;,&apos;);
                                  int semiIdx = imgUrl.IndexOf(&apos;;&apos;);
                                  string mediaType = semiIdx &gt; 5 ? imgUrl.Substring(5, semiIdx - 5) : &quot;image/png&quot;;
                                  string base64Data = commaIdx &gt;= 0 ? imgUrl.Substring(commaIdx + 1) : &quot;&quot;;
                                  if (!string.IsNullOrEmpty(base64Data)) {
                                    partBlocks.Add(new JObject(
                                      new JProperty(&quot;type&quot;, &quot;image&quot;),
                                      new JProperty(&quot;source&quot;, new JObject(
                                        new JProperty(&quot;type&quot;, &quot;base64&quot;),
                                        new JProperty(&quot;media_type&quot;, mediaType),
                                        new JProperty(&quot;data&quot;, base64Data)
                                      ))
                                    ));
                                  }
                                } else if (!string.IsNullOrEmpty(imgUrl)) {
                                  partBlocks.Add(new JObject(
                                    new JProperty(&quot;type&quot;, &quot;image&quot;),
                                    new JProperty(&quot;source&quot;, new JObject(
                                      new JProperty(&quot;type&quot;, &quot;url&quot;),
                                      new JProperty(&quot;url&quot;, imgUrl)
                                    ))
                                  ));
                                }
                              }
                            }
                          } else { textVal = rawC.ToString(); }
                          if (role == &quot;system&quot; || role == &quot;developer&quot;) {
                            sysText = string.IsNullOrEmpty(sysText) ? textVal : (sysText + &quot;\n\n&quot; + textVal);
                          } else if (role == &quot;tool&quot;) {
                            outMsgs.Add(new JObject(
                              new JProperty(&quot;role&quot;, &quot;user&quot;),
                              new JProperty(&quot;content&quot;, new JArray(new JObject(
                                new JProperty(&quot;type&quot;, &quot;tool_result&quot;),
                                new JProperty(&quot;tool_use_id&quot;, (string)mObj[&quot;tool_call_id&quot;] ?? &quot;&quot;),
                                new JProperty(&quot;content&quot;, textVal)
                              )))
                            ));
                          } else if (role == &quot;assistant&quot; &amp;&amp; (mObj[&quot;tool_calls&quot;] as JArray) != null &amp;&amp; ((JArray)mObj[&quot;tool_calls&quot;]).Count &gt; 0) {
                            var contentBlocks = new JArray();
                            if (partBlocks.Count &gt; 0) {
                              foreach (var pb in partBlocks) { contentBlocks.Add(pb); }
                            } else if (!string.IsNullOrEmpty(textVal)) {
                              contentBlocks.Add(new JObject(new JProperty(&quot;type&quot;, &quot;text&quot;), new JProperty(&quot;text&quot;, textVal)));
                            }
                            foreach (var tcTok in (JArray)mObj[&quot;tool_calls&quot;]) {
                              var tcObj = tcTok as JObject;
                              if (tcObj == null) { continue; }
                              JToken parsedInput = new JObject();
                              string rawArgs = (string)tcObj[&quot;function&quot;]?[&quot;arguments&quot;] ?? &quot;{}&quot;;
                              try { parsedInput = JToken.Parse(string.IsNullOrWhiteSpace(rawArgs) ? &quot;{}&quot; : rawArgs); } catch { parsedInput = new JObject(); }
                              contentBlocks.Add(new JObject(
                                new JProperty(&quot;type&quot;, &quot;tool_use&quot;),
                                new JProperty(&quot;id&quot;, (string)tcObj[&quot;id&quot;] ?? (&quot;call_&quot; + Guid.NewGuid().ToString(&quot;N&quot;).Substring(0, 16))),
                                new JProperty(&quot;name&quot;, (string)tcObj[&quot;function&quot;]?[&quot;name&quot;] ?? &quot;&quot;),
                                new JProperty(&quot;input&quot;, parsedInput)
                              ));
                            }
                            outMsgs.Add(new JObject(
                              new JProperty(&quot;role&quot;, &quot;assistant&quot;),
                              new JProperty(&quot;content&quot;, contentBlocks)
                            ));
                          } else {
                            bool hasImageBlocks = partBlocks.OfType&lt;JObject&gt;().Any(blk =&gt; (string)blk[&quot;type&quot;] == &quot;image&quot;);
                            outMsgs.Add(new JObject(
                              new JProperty(&quot;role&quot;, role == &quot;assistant&quot; ? &quot;assistant&quot; : &quot;user&quot;),
                              new JProperty(&quot;content&quot;, hasImageBlocks ? (JToken)partBlocks : (JToken)textVal)
                            ));
                          }
                        }
                        var anth = new JObject(
                          new JProperty(&quot;model&quot;, (string)sel[&quot;apim_alias&quot;]),
                          new JProperty(&quot;max_tokens&quot;, maxTok),
                          new JProperty(&quot;messages&quot;, outMsgs)
                        );
                        if (!string.IsNullOrEmpty(sysText)) { anth[&quot;system&quot;] = sysText; }
                        var inTools = b[&quot;tools&quot;] as JArray;
                        if (inTools != null &amp;&amp; inTools.Count &gt; 0) {
                          var anthTools = new JArray();
                          foreach (var tTok in inTools) {
                            var tObj = tTok as JObject;
                            if (tObj == null) { continue; }
                            var fn = tObj[&quot;function&quot;] as JObject;
                            if (fn != null) {
                              anthTools.Add(new JObject(
                                new JProperty(&quot;name&quot;, (string)fn[&quot;name&quot;] ?? &quot;&quot;),
                                new JProperty(&quot;description&quot;, (string)fn[&quot;description&quot;] ?? &quot;&quot;),
                                new JProperty(&quot;input_schema&quot;, fn[&quot;parameters&quot;]?.DeepClone() ?? new JObject(new JProperty(&quot;type&quot;, &quot;object&quot;), new JProperty(&quot;properties&quot;, new JObject())))
                              ));
                            } else if (tObj[&quot;name&quot;] != null &amp;&amp; tObj[&quot;input_schema&quot;] != null) {
                              anthTools.Add(tObj.DeepClone());
                            }
                          }
                          if (anthTools.Count &gt; 0) { anth[&quot;tools&quot;] = anthTools; }
                        }
                        JToken tcChoice = b[&quot;tool_choice&quot;];
                        if (tcChoice != null &amp;&amp; tcChoice.Type != JTokenType.Null) {
                          if (tcChoice.Type == JTokenType.String) {
                            string tcMode = ((string)tcChoice ?? &quot;&quot;).Trim().ToLowerInvariant();
                            if (tcMode == &quot;auto&quot;) { anth[&quot;tool_choice&quot;] = new JObject(new JProperty(&quot;type&quot;, &quot;auto&quot;)); }
                            else if (tcMode == &quot;required&quot;) { anth[&quot;tool_choice&quot;] = new JObject(new JProperty(&quot;type&quot;, &quot;any&quot;)); }
                            else if (tcMode == &quot;none&quot;) { anth.Remove(&quot;tools&quot;); anth.Remove(&quot;tool_choice&quot;); }
                          } else {
                            var tcChoiceObj = tcChoice as JObject;
                            if (tcChoiceObj != null) {
                              string fnName = (string)tcChoiceObj[&quot;function&quot;]?[&quot;name&quot;] ?? (string)tcChoiceObj[&quot;name&quot;] ?? &quot;&quot;;
                              if (!string.IsNullOrEmpty(fnName)) {
                                anth[&quot;tool_choice&quot;] = new JObject(new JProperty(&quot;type&quot;, &quot;tool&quot;), new JProperty(&quot;name&quot;, fnName));
                              }
                            }
                          }
                        }
                        return anth.ToString(Newtonsoft.Json.Formatting.None);
                      }
                      b[&quot;model&quot;] = (string)sel[&quot;apim_alias&quot;];
                      if (b[&quot;router_context&quot;] != null) {
                        b.Remove(&quot;router_context&quot;);
                      }
                      return b.ToString(Newtonsoft.Json.Formatting.None);
                    }</set-body>
                  </send-request>
                  <set-variable name="routerPlanState" value="@{
                    try {
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var fbResp = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerFallbackHttp&quot;, null);
                      if (fbResp != null &amp;&amp; fbResp.StatusCode &lt; 400) {
                        st[&quot;needs_outbound_fallback&quot;] = false;
                        return st.ToString(Newtonsoft.Json.Formatting.None);
                      }
                      long infMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;routerInferenceStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
                      var sel = (JObject)st[&quot;selected_candidate&quot;];
                      var attempts = (JArray)st[&quot;attempts&quot;];
                      string reqId = context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerCallerRequestId&quot;, context.RequestId.ToString());
                      int fbStat = fbResp != null ? fbResp.StatusCode : 504;
                      string outcome = fbStat == 408 || fbStat == 504 ? &quot;timeout&quot; : (fbStat == 429 ? &quot;rate_limited&quot; : &quot;failed&quot;);
                      string trigger = fbStat == 408 || fbStat == 504 ? &quot;timeout&quot; : (fbStat == 429 ? &quot;rate_limit&quot; : &quot;error&quot;);
                      int seq = attempts.Count + 1;
                      attempts.Add(new JObject(
                        new JProperty(&quot;sequence&quot;, seq),
                        new JProperty(&quot;attempt_number&quot;, seq),
                        new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                        new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                        new JProperty(&quot;request_id&quot;, reqId),
                        new JProperty(&quot;outcome&quot;, outcome),
                        new JProperty(&quot;status_code&quot;, fbResp != null ? (JToken)fbStat : JValue.CreateNull()),
                        new JProperty(&quot;latency_ms&quot;, infMs),
                        new JProperty(&quot;priority&quot;, (int?)sel[&quot;priority&quot;]),
                        new JProperty(&quot;weight&quot;, (int?)sel[&quot;weight&quot;]),
                        new JProperty(&quot;max_cost_per_million&quot;, sel[&quot;max_cost_per_million&quot;]),
                        new JProperty(&quot;reason&quot;, &quot;Fallback candidate returned HTTP &quot; + fbStat.ToString()),
                        new JProperty(&quot;strategy_type&quot;, (string)st[&quot;strategy_type&quot;]),
                        new JProperty(&quot;fallback_trigger&quot;, trigger),
                        new JProperty(&quot;decision_basis&quot;, &quot;fallback_failed&quot;),
                        new JProperty(&quot;attempt_role&quot;, &quot;candidate&quot;),
                        new JProperty(&quot;condition_evaluations&quot;, new JArray())
                      ));
                      var fbEvents = st[&quot;fallback_events&quot;] as JArray ?? new JArray();
                      bool canFallback = fbEvents.Values&lt;string&gt;().Any(ev =&gt; string.Equals(ev, trigger, StringComparison.OrdinalIgnoreCase) || string.Equals(ev, &quot;error&quot;, StringComparison.OrdinalIgnoreCase));
                      var triedIds = new HashSet&lt;string&gt;(attempts.OfType&lt;JObject&gt;().Where(a =&gt; (string)a[&quot;attempt_role&quot;] == &quot;candidate&quot;).Select(a =&gt; (string)a[&quot;model_id&quot;] ?? &quot;&quot;), StringComparer.OrdinalIgnoreCase);
                      var nextCand = canFallback
                        ? ((JArray)st[&quot;eligible_candidates&quot;]).OfType&lt;JObject&gt;().FirstOrDefault(c =&gt; !triedIds.Contains((string)c[&quot;model_id&quot;] ?? &quot;&quot;))
                        : null;
                      if (nextCand != null) {
                        st[&quot;selected_candidate&quot;] = nextCand.DeepClone();
                        st[&quot;decision_basis&quot;] = &quot;fallback_success&quot;;
                        st[&quot;needs_outbound_fallback&quot;] = true;
                      } else {
                        st[&quot;needs_outbound_fallback&quot;] = false;
                      }
                      return st.ToString(Newtonsoft.Json.Formatting.None);
                    } catch {
                      return (string)context.Variables[&quot;routerPlanState&quot;];
                    }
                  }" />
                  </retry>
                  <choose>
                    <when condition="@(context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerFallbackHttp&quot;, null) != null &amp;&amp; ((IResponse)context.Variables[&quot;routerFallbackHttp&quot;]).StatusCode &lt; 400)">
                      <set-status code="@(((IResponse)context.Variables[&quot;routerFallbackHttp&quot;]).StatusCode)" reason="OK" />
                      <set-header name="Content-Type" exists-action="override">
                        <value>@(((IResponse)context.Variables["routerFallbackHttp"]).Headers.GetValueOrDefault("Content-Type", "application/json"))</value>
                      </set-header>
                      <set-body>@{
                        var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                        var sel = (JObject)st[&quot;selected_candidate&quot;];
                        string rawFb = ((IResponse)context.Variables[&quot;routerFallbackHttp&quot;]).Body.As&lt;string&gt;(preserveContent: true);
                        if ((string)sel[&quot;profile&quot;] == &quot;anthropic-messages&quot;) {
                          try {
                            var aBody = JObject.Parse(rawFb);
                            string textContent = &quot;&quot;;
                            var toolCalls = new JArray();
                            var cArr = aBody[&quot;content&quot;] as JArray;
                            if (cArr != null) {
                              textContent = string.Join(&quot;&quot;, cArr.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;type&quot;] == &quot;text&quot;).Select(x =&gt; (string)x[&quot;text&quot;] ?? &quot;&quot;));
                              if (string.IsNullOrEmpty(textContent)) {
                                textContent = string.Join(&quot;&quot;, cArr.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;type&quot;] == &quot;thinking&quot;).Select(x =&gt; (string)x[&quot;thinking&quot;] ?? &quot;&quot;));
                              }
                              int tcIdx = 0;
                              foreach (var bObj in cArr.OfType&lt;JObject&gt;().Where(x =&gt; (string)x[&quot;type&quot;] == &quot;tool_use&quot;)) {
                                string argsJson = bObj[&quot;input&quot;] != null ? bObj[&quot;input&quot;].ToString(Newtonsoft.Json.Formatting.None) : &quot;{}&quot;;
                                toolCalls.Add(new JObject(
                                  new JProperty(&quot;index&quot;, tcIdx),
                                  new JProperty(&quot;id&quot;, (string)bObj[&quot;id&quot;] ?? (&quot;call_&quot; + tcIdx.ToString())),
                                  new JProperty(&quot;type&quot;, &quot;function&quot;),
                                  new JProperty(&quot;function&quot;, new JObject(
                                    new JProperty(&quot;name&quot;, (string)bObj[&quot;name&quot;] ?? &quot;&quot;),
                                    new JProperty(&quot;arguments&quot;, argsJson)
                                  ))
                                ));
                                tcIdx++;
                              }
                            }
                            string stopReason = (string)aBody[&quot;stop_reason&quot;] ?? &quot;&quot;;
                            string finishReason = (stopReason == &quot;tool_use&quot; || toolCalls.Count &gt; 0) ? &quot;tool_calls&quot; : (stopReason == &quot;max_tokens&quot; ? &quot;length&quot; : &quot;stop&quot;);
                            var outMsgObj = new JObject(
                              new JProperty(&quot;role&quot;, &quot;assistant&quot;),
                              new JProperty(&quot;content&quot;, (toolCalls.Count &gt; 0 &amp;&amp; string.IsNullOrEmpty(textContent)) ? JValue.CreateNull() : (JToken)textContent)
                            );
                            if (toolCalls.Count &gt; 0) {
                              outMsgObj[&quot;tool_calls&quot;] = toolCalls;
                            }
                            var uObj = aBody[&quot;usage&quot;] as JObject ?? new JObject();
                            long inTok = (long?)uObj[&quot;input_tokens&quot;] ?? 0L;
                            long crTok = (long?)uObj[&quot;cache_read_input_tokens&quot;] ?? 0L;
                            long cwTok = (long?)uObj[&quot;cache_creation_input_tokens&quot;] ?? 0L;
                            long outTok = (long?)uObj[&quot;output_tokens&quot;] ?? 0L;
                            long cachedTok = crTok + cwTok;
                            var converted = new JObject(
                              new JProperty(&quot;id&quot;, (string)context.Variables[&quot;routerCallerRequestId&quot;]),
                              new JProperty(&quot;object&quot;, &quot;chat.completion&quot;),
                              new JProperty(&quot;created&quot;, (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds),
                              new JProperty(&quot;model&quot;, (string)sel[&quot;display_name&quot;]),
                              new JProperty(&quot;choices&quot;, new JArray(
                                new JObject(
                                  new JProperty(&quot;index&quot;, 0),
                                  new JProperty(&quot;message&quot;, outMsgObj),
                                  new JProperty(&quot;finish_reason&quot;, finishReason)
                                )
                              )),
                              new JProperty(&quot;usage&quot;, new JObject(
                                new JProperty(&quot;prompt_tokens&quot;, inTok + cachedTok),
                                new JProperty(&quot;cached_tokens&quot;, cachedTok),
                                new JProperty(&quot;cache_write_tokens&quot;, cwTok),
                                new JProperty(&quot;completion_tokens&quot;, outTok),
                                new JProperty(&quot;total_tokens&quot;, inTok + cachedTok + outTok)
                              ))
                            );
                            return converted.ToString(Newtonsoft.Json.Formatting.None);
                          } catch {
                            return rawFb;
                          }
                        }
                        return rawFb;
                      }</set-body>
                    </when>
                  </choose>
                </when>
              </choose>
            </when>
          </choose>
          <set-variable name="routerOutboundCompleted" value="@{
            try {
              var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
              var sel = (JObject)st[&quot;selected_candidate&quot;];
              long infMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;routerInferenceStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
              string reqId = context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerCallerRequestId&quot;, context.RequestId.ToString());
              int finalStatus = context.Response.StatusCode;
              var fbResp = context.Variables.GetValueOrDefault&lt;IResponse&gt;(&quot;routerFallbackHttp&quot;, null);
              if (fbResp != null &amp;&amp; fbResp.StatusCode &lt; 400) {
                finalStatus = fbResp.StatusCode;
              }
              bool isSuccess = finalStatus &lt; 400;
              string ct = context.Response.Headers.GetValueOrDefault(&quot;Content-Type&quot;, &quot;&quot;);
              bool isStream = ct.IndexOf(&quot;text/event-stream&quot;, StringComparison.OrdinalIgnoreCase) &gt;= 0;
              JObject rBody = null;
              if (isSuccess &amp;&amp; !isStream) {
                try { rBody = context.Response.Body.As&lt;JObject&gt;(preserveContent: true); } catch { rBody = null; }
              }
              bool hasJsonBody = isSuccess &amp;&amp; rBody != null;

              long? uncachedInTok = null;
              long? cachedTok = null;
              long? cwTok = null;
              long? compTok = null;
              long? totalTok = null;
              double? mainCost = null;

              double? inRate = (double?)sel[&quot;input_cost_per_million&quot;];
              double? outRate = (double?)sel[&quot;output_cost_per_million&quot;];
              double? cRate = (double?)sel[&quot;cached_cost_per_million&quot;] ?? inRate;
              double? cwRate = (double?)sel[&quot;cache_write_cost_per_million&quot;] ?? cRate;
              string sTierKey = null;
              var sTiers = sel[&quot;context_price_tiers&quot;] as JArray;

              if (hasJsonBody) {
                var uObj = rBody[&quot;usage&quot;] as JObject ?? new JObject();
                long promptTok = (long?)uObj[&quot;prompt_tokens&quot;] ?? (long?)uObj[&quot;input_tokens&quot;] ?? 0L;
                long outT = (long?)uObj[&quot;completion_tokens&quot;] ?? (long?)uObj[&quot;output_tokens&quot;] ?? 0L;
                long cT = (long?)uObj[&quot;cached_tokens&quot;] ?? (long?)(uObj[&quot;prompt_tokens_details&quot;] ?? uObj[&quot;input_tokens_details&quot;])?[&quot;cached_tokens&quot;] ?? 0L;
                long cwT = (long?)uObj[&quot;cache_write_tokens&quot;] ?? (long?)uObj[&quot;prompt_tokens_details&quot;]?[&quot;cache_write_tokens&quot;] ?? 0L;
                long totT = (long?)uObj[&quot;total_tokens&quot;] ?? (promptTok + outT);
                long uncachedT = Math.Max(0L, promptTok - cT);
                uncachedInTok = uncachedT;
                cachedTok = cT;
                cwTok = cwT;
                compTok = outT;
                totalTok = totT;
                if (sTiers != null &amp;&amp; sTiers.Count &gt; 0) {
                  JObject matchedTier = null;
                  foreach (JObject tObj in sTiers.OfType&lt;JObject&gt;()) {
                    long minTok = (long?)tObj[&quot;min_input_tokens&quot;] ?? 0L;
                    long? maxTok = (long?)tObj[&quot;max_input_tokens&quot;];
                    if (promptTok &gt;= minTok &amp;&amp; (!maxTok.HasValue || promptTok &lt;= maxTok.Value)) {
                      matchedTier = tObj;
                      break;
                    }
                  }
                  if (matchedTier == null) { matchedTier = (JObject)sTiers[sTiers.Count - 1]; }
                  inRate = (double?)matchedTier[&quot;input_price_per_million&quot;];
                  outRate = (double?)matchedTier[&quot;output_price_per_million&quot;];
                  cRate = (double?)matchedTier[&quot;cached_price_per_million&quot;] ?? inRate;
                  cwRate = (double?)matchedTier[&quot;cache_write_price_per_million&quot;] ?? cRate;
                  sTierKey = (string)matchedTier[&quot;key&quot;];
                }
                if (inRate.HasValue &amp;&amp; outRate.HasValue) {
                  mainCost = Math.Round((uncachedT * inRate.Value + Math.Max(0L, cT - cwT) * (cRate ?? inRate.Value) + cwT * (cwRate ?? cRate ?? inRate.Value) + outT * outRate.Value) / 1000000.0, 8);
                }
              } else if (sTiers != null &amp;&amp; sTiers.Count &gt; 0) {
                inRate = null;
                outRate = null;
                cRate = null;
                cwRate = null;
              }

              var attempts = (JArray)st[&quot;attempts&quot;];
              var condEvals = (JArray)st[&quot;condition_evaluations&quot;];
              if (isSuccess) {
                int seq = attempts.Count + 1;
                attempts.Add(new JObject(
                  new JProperty(&quot;sequence&quot;, seq),
                  new JProperty(&quot;attempt_number&quot;, seq),
                  new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                  new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                  new JProperty(&quot;request_id&quot;, reqId),
                  new JProperty(&quot;outcome&quot;, &quot;succeeded&quot;),
                  new JProperty(&quot;status_code&quot;, finalStatus),
                  new JProperty(&quot;latency_ms&quot;, infMs),
                  new JProperty(&quot;priority&quot;, (int?)sel[&quot;priority&quot;]),
                  new JProperty(&quot;weight&quot;, (int?)sel[&quot;weight&quot;]),
                  new JProperty(&quot;max_cost_per_million&quot;, sel[&quot;max_cost_per_million&quot;]),
                  new JProperty(&quot;reason&quot;, st[&quot;sticky_bound_by&quot;]?.Type == JTokenType.String ? &quot;Reused sticky session binding&quot; : &quot;Selected candidate succeeded&quot;),
                  new JProperty(&quot;strategy_type&quot;, (string)st[&quot;strategy_type&quot;]),
                  new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                  new JProperty(&quot;attempt_role&quot;, &quot;candidate&quot;),
                  new JProperty(&quot;sticky_bound_by&quot;, st[&quot;sticky_bound_by&quot;]),
                  new JProperty(&quot;sticky_key&quot;, st[&quot;sticky_key&quot;]),
                  new JProperty(&quot;condition_evaluations&quot;, seq == 1 ? condEvals : new JArray()),
                  new JProperty(&quot;input_tokens&quot;, uncachedInTok.HasValue ? (JToken)new JValue(uncachedInTok.Value) : JValue.CreateNull()),
                  new JProperty(&quot;cached_tokens&quot;, cachedTok.HasValue ? (JToken)new JValue(cachedTok.Value) : JValue.CreateNull()),
                  new JProperty(&quot;cache_write_tokens&quot;, cwTok.HasValue ? (JToken)new JValue(cwTok.Value) : JValue.CreateNull()),
                  new JProperty(&quot;output_tokens&quot;, compTok.HasValue ? (JToken)new JValue(compTok.Value) : JValue.CreateNull()),
                  new JProperty(&quot;total_tokens&quot;, totalTok.HasValue ? (JToken)new JValue(totalTok.Value) : JValue.CreateNull()),
                  new JProperty(&quot;estimated_cost&quot;, mainCost.HasValue ? (JToken)new JValue(mainCost.Value) : JValue.CreateNull()),
                  new JProperty(&quot;input_price_per_million&quot;, inRate.HasValue ? (JToken)new JValue(inRate.Value) : JValue.CreateNull()),
                  new JProperty(&quot;cached_price_per_million&quot;, cRate.HasValue ? (JToken)new JValue(cRate.Value) : JValue.CreateNull()),
                  new JProperty(&quot;cache_write_price_per_million&quot;, cwRate.HasValue ? (JToken)new JValue(cwRate.Value) : JValue.CreateNull()),
                  new JProperty(&quot;output_price_per_million&quot;, outRate.HasValue ? (JToken)new JValue(outRate.Value) : JValue.CreateNull()),
                  new JProperty(&quot;price_context_tier&quot;, sTierKey != null ? (JToken)new JValue(sTierKey) : JValue.CreateNull())
                ));
              }

              double totalCost = attempts.OfType&lt;JObject&gt;().Sum(a =&gt; (double?)a[&quot;estimated_cost&quot;] ?? 0.0);
              long totalLat = ((long)st[&quot;decision_ms&quot;]) + infMs;

              if (hasJsonBody) {
                var uObj = rBody[&quot;usage&quot;] as JObject ?? new JObject();
                uObj[&quot;cached_tokens&quot;] = cachedTok ?? 0L;
                uObj[&quot;cache_write_tokens&quot;] = cwTok ?? 0L;
                rBody[&quot;id&quot;] = reqId;
                rBody[&quot;model&quot;] = (string)sel[&quot;display_name&quot;];
                rBody[&quot;turnstile_routing&quot;] = new JObject(
                  new JProperty(&quot;request_id&quot;, reqId),
                  new JProperty(&quot;correlation_id&quot;, context.RequestId.ToString()),
                  new JProperty(&quot;requested_model&quot;, (string)context.Variables[&quot;routerRequestedModel&quot;]),
                  new JProperty(&quot;routed&quot;, true),
                  new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                  new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                  new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                  new JProperty(&quot;selected_model_id&quot;, (string)sel[&quot;model_id&quot;]),
                  new JProperty(&quot;selected_model_name&quot;, (string)sel[&quot;display_name&quot;]),
                  new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                  new JProperty(&quot;sticky_bound_by&quot;, st[&quot;sticky_bound_by&quot;]),
                  new JProperty(&quot;sticky_key&quot;, st[&quot;sticky_key&quot;]),
                  new JProperty(&quot;session_id&quot;, st[&quot;session_id&quot;]),
                  new JProperty(&quot;interaction_id&quot;, st[&quot;interaction_id&quot;]),
                  new JProperty(&quot;auth_method&quot;, context.Subscription != null ? &quot;subscription_key&quot; : &quot;entra&quot;),
                  new JProperty(&quot;apim_subscription_id&quot;, context.Subscription != null ? context.Subscription.Id : null),
                  new JProperty(&quot;provider&quot;, (string)sel[&quot;provider_name&quot;]),
                  new JProperty(&quot;runtime&quot;, (string)sel[&quot;runtime_name&quot;]),
                  new JProperty(&quot;gateway&quot;, context.Request.OriginalUrl.Host),
                  new JProperty(&quot;latency_ms&quot;, totalLat),
                  new JProperty(&quot;estimated_cost&quot;, Math.Round(totalCost, 8)),
                  new JProperty(&quot;router_attempts&quot;, attempts)
                );
              }

              var usageRecs = new JArray();
              foreach (JObject att in attempts) {
                if ((string)att[&quot;outcome&quot;] != &quot;succeeded&quot;) { continue; }
                bool isDec = (string)att[&quot;attempt_role&quot;] == &quot;routing_decision&quot;;
                if (!isDec) { continue; }
                string aReqId = (string)att[&quot;request_id&quot;] ?? reqId;
                usageRecs.Add(new JObject(
                  new JProperty(&quot;id&quot;, aReqId),
                  new JProperty(&quot;request_id&quot;, aReqId),
                  new JProperty(&quot;correlation_id&quot;, isDec ? aReqId : context.RequestId.ToString()),
                  new JProperty(&quot;ts&quot;, DateTime.UtcNow.ToString(&quot;o&quot;)),
                  new JProperty(&quot;organization_id&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryOrganizationId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;&quot;))),
                  new JProperty(&quot;organization&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryOrganization&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;&quot;))),
                  new JProperty(&quot;department_id&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryDepartmentId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;&quot;))),
                  new JProperty(&quot;department&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryDepartment&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;&quot;))),
                  new JProperty(&quot;team&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryDepartment&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;&quot;))),
                  new JProperty(&quot;project_id&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryProjectId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;&quot;))),
                  new JProperty(&quot;project&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryProject&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;&quot;))),
                  new JProperty(&quot;agent_id&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryAgentId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;&quot;))),
                  new JProperty(&quot;agent&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryAgent&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;&quot;))),
                  new JProperty(&quot;request_source&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryRequestSource&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;&quot;))),
                  new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                  new JProperty(&quot;user&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryUser&quot;, (string)st[&quot;consumer_key&quot;])),
                  new JProperty(&quot;workflow&quot;, isDec ? &quot;model-router-decision&quot; : (string)st[&quot;task_type&quot;]),
                  new JProperty(&quot;run_id&quot;, (string)st[&quot;run_id&quot;]),
                  new JProperty(&quot;turn_index&quot;, (int)st[&quot;turn_index&quot;]),
                  new JProperty(&quot;provider&quot;, !string.IsNullOrEmpty((string)att[&quot;provider_name&quot;]) ? (string)att[&quot;provider_name&quot;] : (string)sel[&quot;provider_name&quot;]),
                  new JProperty(&quot;model&quot;, (string)att[&quot;model_name&quot;]),
                  new JProperty(&quot;model_id&quot;, (string)att[&quot;model_id&quot;]),
                  new JProperty(&quot;runtime&quot;, !string.IsNullOrEmpty((string)att[&quot;runtime_name&quot;]) ? (string)att[&quot;runtime_name&quot;] : (string)sel[&quot;runtime_name&quot;]),
                  new JProperty(&quot;input_tokens&quot;, (long?)att[&quot;input_tokens&quot;] ?? 0L),
                  new JProperty(&quot;cached_tokens&quot;, (long?)att[&quot;cached_tokens&quot;] ?? 0L),
                  new JProperty(&quot;cache_write_tokens&quot;, (long?)att[&quot;cache_write_tokens&quot;] ?? 0L),
                  new JProperty(&quot;output_tokens&quot;, (long?)att[&quot;output_tokens&quot;] ?? 0L),
                  new JProperty(&quot;latency_ms&quot;, (long?)att[&quot;latency_ms&quot;] ?? 1L),
                  new JProperty(&quot;status&quot;, &quot;200&quot;),
                  new JProperty(&quot;status_code&quot;, 200),
                  new JProperty(&quot;estimated_cost&quot;, att[&quot;estimated_cost&quot;] ?? JValue.CreateNull()),
                  new JProperty(&quot;input_price_per_million&quot;, att[&quot;input_price_per_million&quot;] ?? JValue.CreateNull()),
                  new JProperty(&quot;cached_price_per_million&quot;, att[&quot;cached_price_per_million&quot;] ?? JValue.CreateNull()),
                  new JProperty(&quot;cache_write_price_per_million&quot;, att[&quot;cache_write_price_per_million&quot;] ?? JValue.CreateNull()),
                  new JProperty(&quot;output_price_per_million&quot;, att[&quot;output_price_per_million&quot;] ?? JValue.CreateNull()),
                  new JProperty(&quot;price_context_tier&quot;, att[&quot;price_context_tier&quot;] ?? JValue.CreateNull())
                ));
              }

              var traceDoc = new JObject(
                new JProperty(&quot;request_id&quot;, reqId),
                new JProperty(&quot;correlation_id&quot;, context.RequestId.ToString()),
                new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                new JProperty(&quot;selected_model_id&quot;, (string)sel[&quot;model_id&quot;]),
                new JProperty(&quot;selected_model_name&quot;, (string)sel[&quot;display_name&quot;]),
                new JProperty(&quot;attempts&quot;, attempts),
                new JProperty(&quot;usage_records&quot;, usageRecs)
              );

              return new JObject(
                new JProperty(&quot;succeeded&quot;, isSuccess),
                new JProperty(&quot;trace&quot;, traceDoc),
                new JProperty(&quot;has_json_body&quot;, hasJsonBody),
                new JProperty(&quot;body&quot;, hasJsonBody ? (JToken)rBody : JValue.CreateNull()),
                new JProperty(&quot;model_name&quot;, (string)sel[&quot;display_name&quot;]),
                new JProperty(&quot;model_alias&quot;, (string)sel[&quot;apim_alias&quot;]),
                new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                new JProperty(&quot;router_name&quot;, (string)st[&quot;router_name&quot;]),
                new JProperty(&quot;decision_basis&quot;, (string)st[&quot;decision_basis&quot;]),
                new JProperty(&quot;decision_ms&quot;, (long)st[&quot;decision_ms&quot;]),
                new JProperty(&quot;sticky_bound_by&quot;, (string)st[&quot;sticky_bound_by&quot;] ?? &quot;&quot;)
              ).ToString(Newtonsoft.Json.Formatting.None);
            } catch {
              return &quot;&quot;;
            }
          }" />
          <choose>
            <when condition="@(!string.IsNullOrEmpty((string)context.Variables[&quot;routerOutboundCompleted&quot;]))">
              <set-header name="x-request-id" exists-action="override">
                <value>@((string)context.Variables["routerCallerRequestId"])</value>
              </set-header>
              <set-header name="x-routed-model" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerOutboundCompleted"])["model_alias"])</value>
              </set-header>
              <set-header name="x-router-id" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerOutboundCompleted"])["router_id"])</value>
              </set-header>
              <set-header name="x-router-name" exists-action="override">
                <value>@{
                  var routerName = (string)JObject.Parse((string)context.Variables["routerOutboundCompleted"])["router_name"];
                  return routerName.Any(character =&gt; character &gt; 127) ? Uri.EscapeDataString(routerName) : routerName;
                }</value>
              </set-header>
              <set-header name="x-router-reason" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerOutboundCompleted"])["decision_basis"])</value>
              </set-header>
              <set-header name="x-router-decision-ms" exists-action="override">
                <value>@(((long)JObject.Parse((string)context.Variables["routerOutboundCompleted"])["decision_ms"]).ToString())</value>
              </set-header>
              <set-header name="x-router-sticky-bound-by" exists-action="override">
                <value>@((string)JObject.Parse((string)context.Variables["routerOutboundCompleted"])["sticky_bound_by"])</value>
              </set-header>
              <set-header name="x-auth-method" exists-action="override">
                <value>@(context.Subscription != null ? "subscription_key" : "entra")</value>
              </set-header>
              <set-header name="x-apim-subscription-id" exists-action="override">
                <value>@(context.Subscription != null ? context.Subscription.Id : "")</value>
              </set-header>
              <choose>
                <when condition="@((bool)JObject.Parse((string)context.Variables[&quot;routerOutboundCompleted&quot;])[&quot;has_json_body&quot;])">
                  <set-body>@(JObject.Parse((string)context.Variables["routerOutboundCompleted"])["body"].ToString(Newtonsoft.Json.Formatting.None))</set-body>
                </when>
              </choose>
              <send-request mode="new" response-variable-name="routerOutboundTraceWrite" timeout="2" ignore-error="true">
                <set-url>@{
                  return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='model-router-trace%7Cv1',RowKey='" + Uri.EscapeDataString((string)context.Variables["routerCallerRequestId"]) + "')";
                }</set-url>
                <set-method>PUT</set-method>
                <set-header name="Content-Type" exists-action="override">
                  <value>application/json</value>
                </set-header>
                <set-header name="Accept" exists-action="override">
                  <value>application/json;odata=nometadata</value>
                </set-header>
                <set-header name="x-ms-version" exists-action="override">
                  <value>2020-12-06</value>
                </set-header>
                <set-body>@{
                  var comp = JObject.Parse((string)context.Variables[&quot;routerOutboundCompleted&quot;]);
                  var entity = new JObject(
                    new JProperty(&quot;PartitionKey&quot;, &quot;model-router-trace|v1&quot;),
                    new JProperty(&quot;RowKey&quot;, (string)context.Variables[&quot;routerCallerRequestId&quot;]),
                    new JProperty(&quot;SchemaVersion&quot;, 2),
                    new JProperty(&quot;Document&quot;, comp[&quot;trace&quot;].ToString(Newtonsoft.Json.Formatting.None))
                  );
                  return entity.ToString(Newtonsoft.Json.Formatting.None);
                }</set-body>
                <authentication-managed-identity resource="https://storage.azure.com/" />
              </send-request>
              <choose>
                <when condition="@((bool)JObject.Parse((string)context.Variables[&quot;routerOutboundCompleted&quot;])[&quot;succeeded&quot;] &amp;&amp; ((JArray)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;save_keys&quot;]).Count &gt; 0)">
                  <send-request mode="new" response-variable-name="routerOutboundStickyRefresh0" timeout="2" ignore-error="true">
                    <set-url>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[0];
                      var pk = &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;];
                      var rk = (string)sk[&quot;row_key&quot;];
                      return &quot;__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='&quot; + Uri.EscapeDataString(pk) + &quot;',RowKey='&quot; + Uri.EscapeDataString(rk) + &quot;')&quot;;
                    }</set-url>
                    <set-method>PUT</set-method>
                    <set-header name="Content-Type" exists-action="override">
                      <value>application/json</value>
                    </set-header>
                    <set-header name="Accept" exists-action="override">
                      <value>application/json;odata=nometadata</value>
                    </set-header>
                    <set-header name="x-ms-version" exists-action="override">
                      <value>2020-12-06</value>
                    </set-header>
                    <set-body>@{
                      var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                      var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[0];
                      var sel = (JObject)st[&quot;selected_candidate&quot;];
                      long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                      var doc = new JObject(
                        new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                        new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                        new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                        new JProperty(&quot;tier&quot;, (string)sk[&quot;tier&quot;]),
                        new JProperty(&quot;sticky_key&quot;, (string)sk[&quot;sticky_key&quot;]),
                        new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                        new JProperty(&quot;updated_epoch&quot;, now),
                        new JProperty(&quot;affinity_cookie&quot;, (string)st[&quot;affinity_cookie&quot;] ?? &quot;&quot;)
                      );
                      var entity = new JObject(
                        new JProperty(&quot;PartitionKey&quot;, &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;]),
                        new JProperty(&quot;RowKey&quot;, (string)sk[&quot;row_key&quot;]),
                        new JProperty(&quot;SchemaVersion&quot;, 2),
                        new JProperty(&quot;Document&quot;, doc.ToString(Newtonsoft.Json.Formatting.None))
                      );
                      return entity.ToString(Newtonsoft.Json.Formatting.None);
                    }</set-body>
                    <authentication-managed-identity resource="https://storage.azure.com/" />
                  </send-request>
                  <choose>
                    <when condition="@(((JArray)JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;])[&quot;save_keys&quot;]).Count &gt; 1)">
                      <send-request mode="new" response-variable-name="routerOutboundStickyRefresh1" timeout="2" ignore-error="true">
                        <set-url>@{
                          var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                          var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[1];
                          var pk = &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;];
                          var rk = (string)sk[&quot;row_key&quot;];
                          return &quot;__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey='&quot; + Uri.EscapeDataString(pk) + &quot;',RowKey='&quot; + Uri.EscapeDataString(rk) + &quot;')&quot;;
                        }</set-url>
                        <set-method>PUT</set-method>
                        <set-header name="Content-Type" exists-action="override">
                          <value>application/json</value>
                        </set-header>
                        <set-header name="Accept" exists-action="override">
                          <value>application/json;odata=nometadata</value>
                        </set-header>
                        <set-header name="x-ms-version" exists-action="override">
                          <value>2020-12-06</value>
                        </set-header>
                        <set-body>@{
                          var st = JObject.Parse((string)context.Variables[&quot;routerPlanState&quot;]);
                          var sk = (JObject)((JArray)st[&quot;save_keys&quot;])[1];
                          var sel = (JObject)st[&quot;selected_candidate&quot;];
                          long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                          var doc = new JObject(
                            new JProperty(&quot;router_id&quot;, (string)st[&quot;router_id&quot;]),
                            new JProperty(&quot;router_version&quot;, (int)st[&quot;router_version&quot;]),
                            new JProperty(&quot;user_id&quot;, (string)st[&quot;consumer_key&quot;]),
                            new JProperty(&quot;tier&quot;, (string)sk[&quot;tier&quot;]),
                            new JProperty(&quot;sticky_key&quot;, (string)sk[&quot;sticky_key&quot;]),
                            new JProperty(&quot;model_id&quot;, (string)sel[&quot;model_id&quot;]),
                            new JProperty(&quot;updated_epoch&quot;, now),
                            new JProperty(&quot;affinity_cookie&quot;, (string)st[&quot;affinity_cookie&quot;] ?? &quot;&quot;)
                          );
                          var entity = new JObject(
                            new JProperty(&quot;PartitionKey&quot;, &quot;model-router-sticky|&quot; + (string)st[&quot;router_id&quot;]),
                            new JProperty(&quot;RowKey&quot;, (string)sk[&quot;row_key&quot;]),
                            new JProperty(&quot;SchemaVersion&quot;, 2),
                            new JProperty(&quot;Document&quot;, doc.ToString(Newtonsoft.Json.Formatting.None))
                          );
                          return entity.ToString(Newtonsoft.Json.Formatting.None);
                        }</set-body>
                        <authentication-managed-identity resource="https://storage.azure.com/" />
                      </send-request>
                    </when>
                  </choose>
                </when>
              </choose>
            </when>
          </choose>
        </when>
        <otherwise>
          <!-- Direct Model Call (non-router) on /chat/completions: uniform client headers on every successful response, including streams; turnstile_routing only for JSON bodies -->
          <choose>
            <when condition="@(context.Response.StatusCode &lt; 400 &amp;&amp; (context.Subscription == null || !context.Subscription.Id.EndsWith(&quot;-publisher-probe&quot;, StringComparison.OrdinalIgnoreCase)) &amp;&amp; context.Operation.Id == &quot;chat-completions&quot;)">
              <set-header name="x-request-id" exists-action="override">
                <value>@(context.Variables.GetValueOrDefault&lt;string&gt;("routerCallerRequestId", context.RequestId.ToString()))</value>
              </set-header>
              <set-header name="x-routed-model" exists-action="override">
                <value>@(context.Variables.GetValueOrDefault&lt;string&gt;("routerRequestedModel", ""))</value>
              </set-header>
              <set-header name="x-auth-method" exists-action="override">
                <value>@(context.Subscription != null ? "subscription_key" : "entra")</value>
              </set-header>
              <set-header name="x-apim-subscription-id" exists-action="override">
                <value>@(context.Subscription != null ? context.Subscription.Id : "")</value>
              </set-header>
            </when>
          </choose>
          <choose>
            <when condition="@(context.Response.StatusCode &lt; 400 &amp;&amp; (context.Subscription == null || !context.Subscription.Id.EndsWith(&quot;-publisher-probe&quot;, StringComparison.OrdinalIgnoreCase)) &amp;&amp; context.Operation.Id == &quot;chat-completions&quot; &amp;&amp; context.Response.Headers.GetValueOrDefault(&quot;Content-Type&quot;, &quot;&quot;).StartsWith(&quot;application/json&quot;, StringComparison.OrdinalIgnoreCase))">
              <set-body>@{
                try {
                  var rBody = context.Response.Body.As&lt;JObject&gt;(preserveContent: true);
                  string reqId = context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerCallerRequestId&quot;, context.RequestId.ToString());
                  string reqModel = context.Variables.GetValueOrDefault&lt;string&gt;(&quot;routerRequestedModel&quot;, (string)rBody[&quot;model&quot;] ?? &quot;&quot;);
                  var uObj = rBody[&quot;usage&quot;] as JObject ?? new JObject();
                  long cachedTok = (long?)uObj[&quot;cached_tokens&quot;] ?? (long?)(uObj[&quot;prompt_tokens_details&quot;] ?? uObj[&quot;input_tokens_details&quot;])?[&quot;cached_tokens&quot;] ?? 0L;
                  long cwTok = (long?)uObj[&quot;cache_write_tokens&quot;] ?? (long?)uObj[&quot;prompt_tokens_details&quot;]?[&quot;cache_write_tokens&quot;] ?? 0L;
                  uObj[&quot;cached_tokens&quot;] = cachedTok;
                  uObj[&quot;cache_write_tokens&quot;] = cwTok;
                  rBody[&quot;id&quot;] = reqId;
                  rBody[&quot;model&quot;] = reqModel;
                  long latMs = Math.Max(1L, (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault&lt;DateTime&gt;(&quot;requestStarted&quot;, DateTime.UtcNow)).TotalMilliseconds);
                  rBody[&quot;turnstile_routing&quot;] = new JObject(
                    new JProperty(&quot;request_id&quot;, reqId),
                    new JProperty(&quot;correlation_id&quot;, context.RequestId.ToString()),
                    new JProperty(&quot;requested_model&quot;, reqModel),
                    new JProperty(&quot;routed&quot;, false),
                    new JProperty(&quot;router_id&quot;, JValue.CreateNull()),
                    new JProperty(&quot;router_name&quot;, JValue.CreateNull()),
                    new JProperty(&quot;router_version&quot;, JValue.CreateNull()),
                    new JProperty(&quot;selected_model_id&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;availabilityModelId&quot;, &quot;&quot;)),
                    new JProperty(&quot;selected_model_name&quot;, reqModel),
                    new JProperty(&quot;decision_basis&quot;, &quot;direct_model&quot;),
                    new JProperty(&quot;sticky_bound_by&quot;, JValue.CreateNull()),
                    new JProperty(&quot;auth_method&quot;, context.Subscription != null ? &quot;subscription_key&quot; : &quot;entra&quot;),
                    new JProperty(&quot;apim_subscription_id&quot;, context.Subscription != null ? context.Subscription.Id : null),
                    new JProperty(&quot;provider&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;selectedProviderName&quot;, &quot;&quot;)),
                    new JProperty(&quot;runtime&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;selectedRuntimeName&quot;, &quot;&quot;)),
                    new JProperty(&quot;gateway&quot;, context.Request.OriginalUrl.Host),
                    new JProperty(&quot;latency_ms&quot;, latMs),
                    new JProperty(&quot;router_attempts&quot;, new JArray())
                  );
                  return rBody.ToString(Newtonsoft.Json.Formatting.None);
                } catch {
                  return context.Response.Body.As&lt;string&gt;(preserveContent: true);
                }
              }</set-body>
            </when>
          </choose>
        </otherwise>
      </choose>
    </when>
  </choose>
</outbound>"""
