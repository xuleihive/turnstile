# ruff: noqa: E501
from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from xml.etree import ElementTree as ET

from ..domain.budget_delegation import BudgetDelegationTrust
from ..domain.image_profiles import ImageGenerationProfile
from .apim_control_plane_contract import PolicyCompilationError
from .apim_policy_components import (
  TEXT_CONDITION,
  parse_policy,
  serialize_policy,
  validate_parent_policy,
)
from .budget_delegation_headers import DELEGATION_ENCODING_HEADER, DELEGATION_METADATA_HEADERS

VERSION = 2

def _rejection_rule_condition(value: str, version: str) -> str:
    return f"""({value} is JObject && {value}["status_code"]?.Type == JTokenType.Integer
      && (({version}) == 1
        ? ((JObject){value}).Properties().Count() == 3
          && new[] {{ 400, 422 }}.Contains((int){value}["status_code"])
          && new[] {{ "error_type", "error_code" }}.All(ruleField => {value}[ruleField]?.Type == JTokenType.String
            && System.Text.RegularExpressions.Regex.IsMatch((string){value}[ruleField], "^[A-Za-z0-9_.-]{{1,128}}$"))
        : ({version}) == 2 && ((JObject){value}).Properties().Count() == 4
          && (int){value}["status_code"] >= 400 && (int){value}["status_code"] < 500
          && !new[] {{ 408, 499 }}.Contains((int){value}["status_code"])
          && new[] {{ "error_type", "error_code", "error_param" }}.Select(ruleField => {value}[ruleField])
            .All(fieldObject => fieldObject is JObject && fieldObject["state"]?.Type == JTokenType.String
              && (new[] {{ "absent", "null" }}.Contains((string)fieldObject["state"])
                ? ((JObject)fieldObject).Properties().Count() == 1
                : (string)fieldObject["state"] == "value" && ((JObject)fieldObject).Properties().Count() == 2
                  && fieldObject["value"]?.Type == JTokenType.String
                  && System.Text.RegularExpressions.Regex.IsMatch((string)fieldObject["value"], "^[A-Za-z0-9_.\\\\[\\\\]-]{{1,255}}$")))
          && new[] {{ "error_type", "error_code" }}.Any(ruleField => (string){value}[ruleField]["state"] == "value")))"""

REQUEST_ANALYSIS = """@{
    var result = new JObject(new JProperty("error", "budget_request_analysis_failed"));
    try {
      JToken parsed;
      var requestText = context.Request.Body.As<string>(preserveContent: true);
      try {
        parsed = JsonConvert.DeserializeObject<JToken>(requestText);
      } catch { result["error"] = "budget_request_invalid"; return result; }
      var body = parsed as JObject;
      if (body == null) { result["error"] = "budget_request_invalid"; return result; }
      var operation = context.Operation.Id;
      var anthropic = operation == "anthropic-messages";
      var chat = operation == "chat-completions";
      var responses = operation == "responses" || operation == "responses-compact";
      var images = operation == "images-generations";
      if (operation == "microsoft-decision") {
        var questions = body["questions"] as JObject;
        if (body.Property("state") == null || questions == null || questions.Count == 0
          || body.Properties().Any(property => !new[] { "model", "state", "questions" }.Contains(property.Name))) {
          result["error"] = "budget_request_invalid"; return result;
        }
        foreach (var entry in questions.Properties()) {
          var question = entry.Value as JObject;
          if (question == null || question["type"]?.Type != JTokenType.String
            || question["instructions"]?.Type != JTokenType.String || string.IsNullOrEmpty((string)question["instructions"])
            || question.Properties().Any(property => !new[] { "type", "instructions", "criteria" }.Contains(property.Name))) {
            result["error"] = "budget_request_invalid"; return result;
          }
          var kind = (string)question["type"];
          var criteria = question["criteria"];
          if (kind == "noul") {
            if (criteria != null && (!(criteria is JObject) || ((JObject)criteria).Properties().Any(property =>
              !new[] { "true", "false" }.Contains(property.Name) || property.Value.Type != JTokenType.String))) {
              result["error"] = "budget_request_invalid"; return result;
            }
          } else if (kind == "choice") {
            if (!(criteria is JObject) || ((JObject)criteria).Count < 2 || ((JObject)criteria).Properties().Any(property =>
              property.Value.Type != JTokenType.String || string.IsNullOrEmpty((string)property.Value))) {
              result["error"] = "budget_request_invalid"; return result;
            }
          } else if (kind == "score") {
            if (!(criteria is JArray) || ((JArray)criteria).Count < 2 || ((JArray)criteria).Any(value =>
              value.Type != JTokenType.String || string.IsNullOrEmpty((string)value))) {
              result["error"] = "budget_request_invalid"; return result;
            }
          } else { result["error"] = "budget_request_invalid"; return result; }
        }
        return new JObject(new JProperty("error", ""), new JProperty("version", 1),
          new JProperty("complete", true), new JProperty("remote_content", false),
          new JProperty("required_features", new JArray()), new JProperty("request_controls", new JArray()));
      }
      if (!anthropic && !chat && !responses && !images) { return result; }
      var features = new HashSet<string>();
      var controls = new HashSet<string>();
      bool complete = true, remote = false;
      var fields = anthropic
        ? new[] { "model", "messages", "max_tokens", "metadata", "service_tier", "stop_sequences", "stream", "system",
          "temperature", "thinking", "tool_choice", "tools", "top_k", "top_p", "cache_control", "output_config", "inference_geo" }
        : chat ? new[] { "model", "messages", "audio", "frequency_penalty", "function_call", "functions", "logit_bias",
          "logprobs", "max_completion_tokens", "max_tokens", "metadata", "modalities", "n", "best_of", "parallel_tool_calls",
          "prediction", "presence_penalty", "reasoning_effort", "response_format", "safety_identifier", "seed", "service_tier",
          "stop", "store", "stream", "stream_options", "temperature", "tool_choice", "tools", "top_logprobs", "top_p",
          "user", "verbosity", "prompt_cache_key", "prompt_cache_retention", "web_search_options" }
        : operation == "responses-compact" ? new[] { "model", "input", "instructions", "previous_response_id", "prompt_cache_key",
          "prompt_cache_options", "prompt_cache_retention", "service_tier" }
        : new[] { "model", "input", "instructions", "previous_response_id", "conversation", "prompt", "background", "include",
          "max_output_tokens", "max_tool_calls", "metadata", "parallel_tool_calls", "prompt_cache_key", "prompt_cache_options",
          "prompt_cache_retention", "reasoning", "safety_identifier", "service_tier", "store", "stream", "stream_options",
          "temperature", "text", "tool_choice", "tools", "top_logprobs", "top_p", "truncation", "user" };
      if (!images && body.Properties().Any(property => !fields.Contains(property.Name))) { complete = false; }
      var pending = new Queue<KeyValuePair<string, JToken>>();
      if (!images && body["stream"] != null && body["stream"].Type != JTokenType.Null
        && body["stream"].Type != JTokenType.Boolean) {
        result["error"] = "budget_request_invalid"; return result;
      }
      if (responses) {
        if (body["previous_response_id"] != null && body["previous_response_id"].Type != JTokenType.Null) {
          if (body["previous_response_id"].Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
          features.Add("conversation_state");
        }
        if (body["conversation"] != null && body["conversation"].Type != JTokenType.Null) {
          if (body["conversation"].Type != JTokenType.String && !(body["conversation"] is JObject)) {
            result["error"] = "budget_request_invalid"; return result;
          }
          features.Add("conversation_state"); remote = true;
        }
        if (body["prompt"] != null && body["prompt"].Type != JTokenType.Null) {
          var prompt = body["prompt"] as JObject;
          if (prompt == null || prompt["id"]?.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
          features.Add("conversation_state"); remote = true;
          if (prompt["variables"] != null && prompt["variables"].Type != JTokenType.Null) {
            var variables = prompt["variables"] as JObject;
            if (variables == null) { result["error"] = "budget_request_invalid"; return result; }
            foreach (var variable in variables.Properties()) {
              if (variable.Value.Type != JTokenType.String && variable.Value.Type != JTokenType.Date) {
                pending.Enqueue(new KeyValuePair<string, JToken>("response-block", variable.Value));
              }
            }
          }
        }
        if (operation == "responses" && body["background"] != null && body["background"].Type != JTokenType.Null) {
          if (body["background"].Type != JTokenType.Boolean) { result["error"] = "budget_request_invalid"; return result; }
          if ((bool)body["background"]) { features.Add("background"); }
        }
        pending.Enqueue(new KeyValuePair<string, JToken>("response-input", body["input"]));
        pending.Enqueue(new KeyValuePair<string, JToken>("response-tools", body["tools"]));
      } else if (anthropic || chat) {
        pending.Enqueue(new KeyValuePair<string, JToken>(anthropic ? "anthropic-messages" : "chat-messages", body["messages"]));
        pending.Enqueue(new KeyValuePair<string, JToken>(anthropic ? "anthropic-tools" : "chat-tools", body["tools"]));
        if (anthropic) { pending.Enqueue(new KeyValuePair<string, JToken>("anthropic-content", body["system"])); }
        if (chat && body["web_search_options"] != null && body["web_search_options"].Type != JTokenType.Null) {
          if (!(body["web_search_options"] is JObject)) { result["error"] = "budget_request_invalid"; return result; }
          features.Add("server_tools");
        }
      }
      while (pending.Count > 0) {
        var entry = pending.Dequeue();
        var node = entry.Value;
        var kind = entry.Key;
        if (kind == "required-text") {
          if (node == null || (node.Type != JTokenType.String && node.Type != JTokenType.Date)) {
            result["error"] = "budget_request_invalid"; result["error_path"] = kind; return result;
          }
          continue;
        }
        if (node == null) { continue; }
        var textNode = node.Type == JTokenType.String || node.Type == JTokenType.Date;
        result["error_path"] = kind;
        if (kind == "anthropic-messages" || kind == "chat-messages" || kind == "response-input"
          || kind.EndsWith("-content", StringComparison.Ordinal) || kind.EndsWith("-tools", StringComparison.Ordinal)
          || kind == "chat-calls") {
          if (kind == "response-input" && (textNode || node.Type == JTokenType.Null)) { continue; }
          if (kind.EndsWith("-content", StringComparison.Ordinal)
            && (textNode || (kind == "chat-content" && node.Type == JTokenType.Null))) { continue; }
          if (kind.EndsWith("-tools", StringComparison.Ordinal) && node.Type == JTokenType.Null) { continue; }
          var items = node as JArray;
          if (items == null) { result["error"] = "budget_request_invalid"; return result; }
          var childKind = kind == "anthropic-messages" ? "anthropic-message"
            : kind == "chat-messages" ? "chat-message" : kind == "response-input" ? "response-item"
            : kind == "chat-calls" ? "chat-call" : kind.Replace("-content", "-block").Replace("-tools", "-tool");
          foreach (var item in items) { pending.Enqueue(new KeyValuePair<string, JToken>(childKind, item)); }
          continue;
        }
        var value = node as JObject;
        if (value == null) { result["error"] = "budget_request_invalid"; return result; }
        if (kind == "anthropic-message" || kind == "chat-message") {
          pending.Enqueue(new KeyValuePair<string, JToken>(kind == "anthropic-message" ? "anthropic-content" : "chat-content", value["content"]));
          if (kind == "chat-message") {
            pending.Enqueue(new KeyValuePair<string, JToken>("chat-calls", value["tool_calls"]));
            if (value["audio"] != null && value["audio"].Type != JTokenType.Null) { features.Add("multimodal_input"); remote = true; }
          }
          continue;
        }
        var typeValue = value["type"];
        if (typeValue != null && typeValue.Type != JTokenType.Null
          && typeValue.Type != JTokenType.String && typeValue.Type != JTokenType.Date) { result["error"] = "budget_request_invalid"; return result; }
        var type = typeValue != null && typeValue.Type != JTokenType.Null ? (string)typeValue : "";
        if (kind.EndsWith("-tool", StringComparison.Ordinal)) {
          if (kind == "anthropic-tool" && (type == "" || type == "custom")) { continue; }
          if (kind == "anthropic-tool" && new[] { "bash_20250124", "text_editor_20250124",
            "text_editor_20250429", "text_editor_20250728", "memory_20250818" }.Contains(type)) { continue; }
          if (kind == "anthropic-tool" && new[] { "web_search_20250305", "web_search_20260209", "web_search_20260318",
            "web_fetch_20250910", "web_fetch_20260209", "web_fetch_20260309", "web_fetch_20260318",
            "code_execution_20250522", "code_execution_20250825", "code_execution_20260120", "code_execution_20260521",
            "tool_search_tool_regex_20251119", "tool_search_tool_bm25_20251119" }.Contains(type)) {
            features.Add("server_tools"); continue;
          }
          if ((kind == "chat-tool" || kind == "response-tool") && (type == "function" || type == "custom")) { continue; }
          if (kind == "response-tool" && type == "namespace") {
            if (!(value["tools"] is JArray)) { result["error"] = "budget_request_invalid"; return result; }
            pending.Enqueue(new KeyValuePair<string, JToken>("response-tools", value["tools"])); continue;
          }
          if (kind == "response-tool" && new[] { "computer", "computer_use_preview", "local_shell", "apply_patch" }.Contains(type)) { continue; }
          if (kind == "response-tool" && type == "tool_search") {
            var execution = value["execution"];
            if (execution != null && execution.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
            if ((string)execution == "client") { continue; }
            if ((string)execution == "server") { features.Add("server_tools"); continue; }
            complete = false; continue;
          }
          if (kind == "response-tool" && type == "shell") {
            var environment = value["environment"] as JObject;
            if (environment == null) { complete = false; continue; }
            if (environment["type"]?.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
            if ((string)environment["type"] == "local") { continue; }
            if (new[] { "container_auto", "container_reference" }.Contains((string)environment["type"])) {
              features.Add("server_tools"); continue;
            }
            complete = false; continue;
          }
          if (kind == "response-tool" && new[] { "web_search", "web_search_preview", "web_search_preview_2025_03_11",
            "file_search", "code_interpreter", "image_generation", "mcp" }.Contains(type)) {
            features.Add("server_tools"); continue;
          }
          complete = false; continue;
        }
        if (kind == "chat-call") {
          if (type != "function" && type != "custom") { complete = false; }
          continue;
        }
        if (kind == "response-item") {
          if (type == "compaction_trigger") {
            if (operation != "responses" || value.Properties().Any(property => property.Name != "type")
              || !controls.Add(type)) { result["error"] = "budget_request_invalid"; return result; }
            features.Add("compaction"); continue;
          }
          if (type == "message" || (type == "" && value["role"]?.Type == JTokenType.String)) {
            pending.Enqueue(new KeyValuePair<string, JToken>("response-content", value["content"])); continue;
          }
          if (new[] { "function_call", "custom_tool_call", "computer_call", "local_shell_call", "local_shell_call_output",
            "apply_patch_call", "apply_patch_call_output", "shell_call_output" }.Contains(type)) { continue; }
          if (type == "additional_tools") { pending.Enqueue(new KeyValuePair<string, JToken>("response-tools", value["tools"])); continue; }
          if (type == "tool_search_call" || type == "tool_search_output") {
            var execution = value["execution"];
            if (execution != null && execution.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
            if ((string)execution == "server") { features.Add("server_tools"); }
            else if ((string)execution != "client") { complete = false; }
            if (type == "tool_search_output") { pending.Enqueue(new KeyValuePair<string, JToken>("response-tools", value["tools"])); }
            continue;
          }
          if (type == "shell_call") {
            var environment = value["environment"] as JObject;
            if (environment == null) { complete = false; continue; }
            if (environment["type"]?.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
            if ((string)environment["type"] == "local") { continue; }
            if ((string)environment["type"] == "container_reference") { features.Add("server_tools"); continue; }
            complete = false; continue;
          }
          if (type == "function_call_output" || type == "custom_tool_call_output") {
            pending.Enqueue(new KeyValuePair<string, JToken>("response-content", value["output"])); continue;
          }
          if (type == "reasoning" || type == "compaction" || type == "context_compaction") {
            var encrypted = value["encrypted_content"];
            if (encrypted != null && encrypted.Type != JTokenType.Null && encrypted.Type != JTokenType.String) {
              result["error"] = "budget_request_invalid"; return result;
            }
            if (type != "reasoning" || (encrypted != null && encrypted.Type != JTokenType.Null)) { features.Add("opaque_state"); }
            continue;
          }
          if (type == "item_reference") { features.Add("conversation_state"); remote = true; continue; }
          if (type == "computer_call_output") { pending.Enqueue(new KeyValuePair<string, JToken>("response-block", value["output"])); continue; }
          if (new[] { "web_search_call", "file_search_call", "code_interpreter_call", "image_generation_call",
            "mcp_call", "mcp_list_tools", "mcp_approval_request", "mcp_approval_response" }.Contains(type)) {
            features.Add("server_tools"); complete = false; continue;
          }
          complete = false; continue;
        }
        if (kind == "anthropic-block") {
          if (type == "text" || type == "thinking") {
            pending.Enqueue(new KeyValuePair<string, JToken>("required-text", value[type == "text" ? "text" : "thinking"])); continue;
          }
          if (type == "tool_use") { continue; }
          if (type == "search_result") { pending.Enqueue(new KeyValuePair<string, JToken>("anthropic-content", value["content"])); continue; }
          if (type == "tool_result") { pending.Enqueue(new KeyValuePair<string, JToken>("anthropic-content", value["content"])); continue; }
          if (type == "redacted_thinking" || type == "compaction") { features.Add("opaque_state"); continue; }
          if (type == "server_tool_use") { features.Add("server_tools"); continue; }
          if (type == "image" || type == "document") {
            if (!(value["source"] is JObject)) { result["error"] = "budget_request_invalid"; return result; }
            features.Add("multimodal_input"); pending.Enqueue(new KeyValuePair<string, JToken>("anthropic-source", value["source"])); continue;
          }
          complete = false; continue;
        }
        if (kind == "anthropic-source") {
          if (type == "url") {
            if (value["url"]?.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
            remote = true; continue;
          }
          if (type == "file") { remote = true; pending.Enqueue(new KeyValuePair<string, JToken>("required-text", value["file_id"])); continue; }
          if (type == "base64" || type == "text") { pending.Enqueue(new KeyValuePair<string, JToken>("required-text", value["data"])); continue; }
          if (type == "content") { pending.Enqueue(new KeyValuePair<string, JToken>("anthropic-content", value["content"])); continue; }
          complete = false; continue;
        }
        if (kind == "chat-block") {
          if (type == "text" || type == "refusal") { pending.Enqueue(new KeyValuePair<string, JToken>("required-text", value[type])); continue; }
          if (type == "image_url") {
            features.Add("multimodal_input");
            var image = value["image_url"] as JObject;
            if (image == null || image["url"]?.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
            remote = remote || !((string)image["url"]).StartsWith("data:", StringComparison.OrdinalIgnoreCase); continue;
          }
          if (type == "input_audio") {
            var audio = value["input_audio"] as JObject;
            if (audio == null) { result["error"] = "budget_request_invalid"; return result; }
            pending.Enqueue(new KeyValuePair<string, JToken>("required-text", audio["data"]));
            features.Add("multimodal_input"); continue;
          }
          if (type == "file") {
            features.Add("multimodal_input");
            var file = value["file"] as JObject;
            if (file == null) { result["error"] = "budget_request_invalid"; return result; }
            remote = remote || (file["file_id"] != null && file["file_id"].Type != JTokenType.Null); continue;
          }
          complete = false; continue;
        }
        if (kind == "response-block") {
          if (type == "input_text" || type == "output_text" || type == "refusal") {
            pending.Enqueue(new KeyValuePair<string, JToken>("required-text", value[type == "refusal" ? "refusal" : "text"])); continue;
          }
          if (new[] { "input_image", "input_file", "computer_screenshot" }.Contains(type)) {
            features.Add("multimodal_input");
            if (type == "input_file" && value["file_data"] != null && value["file_data"].Type != JTokenType.Null) {
              pending.Enqueue(new KeyValuePair<string, JToken>("required-text", value["file_data"]));
            }
            foreach (var field in new[] { "image_url", "file_url", "file_id" }) {
              var reference = value[field];
              if (reference == null || reference.Type == JTokenType.Null) { continue; }
              if (reference.Type != JTokenType.String) { result["error"] = "budget_request_invalid"; return result; }
              remote = remote || field == "file_id" || !((string)reference).StartsWith("data:", StringComparison.OrdinalIgnoreCase);
            }
            continue;
          }
          complete = false; continue;
        }
        return result;
      }
      result["error"] = ""; result.Remove("error_path");
      result["version"] = 1; result["complete"] = complete; result["remote_content"] = remote;
      result["required_features"] = new JArray(features.OrderBy(feature => feature, StringComparer.Ordinal));
      result["request_controls"] = new JArray(controls.OrderBy(control => control, StringComparer.Ordinal));
      return result;
    } catch { return result; }
}"""

PLAN = """@{
  var result = new JObject(new JProperty("error", "budget_configuration_invalid"));
    try {
      var fence = context.Variables.GetValueOrDefault<string>("budgetExecutionFence", "");
      var config = JObject.Parse(fence);
      var validation = new[] { "prepared", "paused" }.Contains((string)config["mode"])
        && (string)config["validation_revision"] == context.Api.Revision;
      if (((string)config["mode"] != "active" && !validation) || (int?)config["protocol_version"] != 2) { return result; }
      var validationPerson = (string)config["validation_person_id"];
      var hasPerson = context.Variables.GetValueOrDefault<bool>("hasEmployeeToken", false);
      if (validation && (context.Subscription == null
        || (!string.IsNullOrEmpty(validationPerson)
          ? (string)config["mode"] != "paused" || !hasPerson
            || context.Variables.GetValueOrDefault<string>("employeeName", "") != validationPerson
          : hasPerson)
        || (string)context.Variables.GetValueOrDefault<JObject>("applicationMapState", new JObject())["applicationId"]
          != (string)config["validation_application_id"])) {
        result["error"] = "budget_protocol_validation_scope"; return result;
      }
      var epoch = Guid.Parse((string)config["epoch"]).ToString();
      if ((string)config["gateway_profile_id"] != "__GATEWAY_PROFILE_ID__") { return result; }
      var applicationMapping = context.Variables.GetValueOrDefault<JObject>("applicationMapState", new JObject());
      var modelKey = context.Variables.GetValueOrDefault<string>("selectedBudgetModelKey", "");
      if (string.IsNullOrWhiteSpace(modelKey)) { result["error"] = "budget_model_unattributed"; return result; }
      var contracts = config["model_contracts"] as JObject;
      var modelContract = contracts?[modelKey] as JObject;
      if (context.Variables.ContainsKey("selectedBudgetCountContract")) {
        var snapshot = JObject.Parse((string)context.Variables["selectedBudgetCountContract"]);
        if ((int?)snapshot["version"] != 1 || (string)snapshot["epoch"] != epoch
          || (string)snapshot["gateway_profile_id"] != "__GATEWAY_PROFILE_ID__"
          || (string)snapshot["model_key"] != modelKey || !(snapshot["contract"] is JObject)) { return result; }
        modelContract = (JObject)snapshot["contract"];
      }
      var operation = context.Operation.Id;
      var contract = (modelContract?["protocols"] as JObject)?[operation] as JObject ?? modelContract;
      JObject rejectionPolicy = null;
      var requestPolicy = contract?["request_policy"] as JObject;
      if (contract?["request_policy"] != null && requestPolicy == null) { return result; }
      if (requestPolicy != null) {
        if (requestPolicy["version"]?.Type != JTokenType.Integer || !new[] { 1, 2, 3 }.Contains((int)requestPolicy["version"])
          || ((int)requestPolicy["version"] >= 2 ? (string)requestPolicy["operation"] != operation : requestPolicy["operation"] != null)
          || requestPolicy["evidence_sha256"]?.Type != JTokenType.String
          || !System.Text.RegularExpressions.Regex.IsMatch((string)requestPolicy["evidence_sha256"], "^[0-9a-f]{64}$")
          || (requestPolicy["verified_at"]?.Type != JTokenType.String && requestPolicy["verified_at"]?.Type != JTokenType.Date)) { return result; }
        if ((int)requestPolicy["version"] == 3) {
          var fields = new[] { "version", "operation", "streaming_thinking_display", "output_requirements", "terminal_rejections", "evidence_sha256", "verified_at" };
          if (operation != "anthropic-messages" || !context.Variables.ContainsKey("selectedBudgetCountContract")
            || requestPolicy["streaming_thinking_display"]?.Type != JTokenType.String
            || (string)requestPolicy["streaming_thinking_display"] != "summarized"
            || requestPolicy.Properties().Any(property => !fields.Contains(property.Name))
            || (requestPolicy["output_requirements"] != null && (!(requestPolicy["output_requirements"] is JObject)
              || ((JObject)requestPolicy["output_requirements"]).Count != 0))) { return result; }
        } else if (requestPolicy["streaming_thinking_display"] != null) { return result; }
        var verified = requestPolicy["verified_at"].Type == JTokenType.Date
          ? new DateTimeOffset(requestPolicy["verified_at"].Value<DateTime>()) : DateTimeOffset.Parse((string)requestPolicy["verified_at"]);
        if (verified > new DateTimeOffset((DateTime)context.Variables["requestStarted"])) { return result; }
        var rules = requestPolicy["terminal_rejections"] as JArray;
        if (requestPolicy["terminal_rejections"] != null && rules == null) { return result; }
        if (rules != null && rules.Count > 0) {
          if (rules.Count > 16 || rules.Any(rule => !""" + _rejection_rule_condition("rule", 'Math.Min((int)requestPolicy["version"], 2)') + """)
            || rules.Where((rule, index) => rules.Take(index).Any(prior => JToken.DeepEquals(prior, rule))).Any()) { return result; }
          rejectionPolicy = new JObject(new JProperty("version", Math.Min((int)requestPolicy["version"], 2)), new JProperty("rules", rules.DeepClone()),
            new JProperty("evidence_sha256", requestPolicy["evidence_sha256"]), new JProperty("verified_at", requestPolicy["verified_at"]));
          if ((int)requestPolicy["version"] >= 2) { rejectionPolicy["operation"] = operation; }
        }
      }
      var algorithm = (string)contract?["algorithm"];
      var legacyEstimate = algorithm == "legacy_request_estimate_v1";
      var passthrough = algorithm == "request_total_ceiling_v1"
        || (legacyEstimate && (operation == "responses-compact" || operation == "images-generations" || operation == "microsoft-decision"));
      if (legacyEstimate && (contract["output_estimate"]?.Type != JTokenType.Integer
        || (long)contract["output_estimate"] <= 0)) { return result; }
      if (!legacyEstimate && !passthrough && (algorithm != "context_input_ceiling_v1"
        || contract?["input_ceiling"]?.Type != JTokenType.Integer
        || (long)contract["input_ceiling"] <= 0
        || contract?["output_ceiling"]?.Type != JTokenType.Integer
        || (long)contract["output_ceiling"] <= 0)) {
        result["error"] = "budget_count_bound_unavailable"; return result;
      }
      if (passthrough && !legacyEstimate && (contract["request_token_ceiling"]?.Type != JTokenType.Integer
        || (long)contract["request_token_ceiling"] <= 0 || (string)contract["output_field"] != "none")) {
        result["error"] = "budget_count_bound_unavailable"; return result;
      }
      var model = Guid.Parse((string)modelContract["model_id"]).ToString();
      var operations = contract["operations"] as JArray ?? new JArray("chat-completions", "responses", "anthropic-messages");
      if (!operations.Values<string>().Contains(operation)
        || (!passthrough && (operation == "responses-compact" || operation == "images-generations" || operation == "microsoft-decision"))) {
        result["error"] = "budget_count_bound_unavailable"; return result;
      }
      var supported = contract["supported_features"] as JArray ?? new JArray();
      var knownFeatures = new[] { "conversation_state", "opaque_state", "multimodal_input", "server_tools", "background", "compaction" };
      if (supported.Any(value => value.Type != JTokenType.String || !knownFeatures.Contains((string)value))) { return result; }
      result["error"] = "budget_request_analysis_failed";
      var analysis = context.Variables.GetValueOrDefault<JObject>("executionRequestAnalysis", null);
      if (analysis == null || analysis["error"]?.Type != JTokenType.String) { return result; }
      if ((string)analysis["error"] != "") {
        if ((string)analysis["error"] == "budget_request_invalid") { result["error"] = analysis["error"]; }
        result["error_path"] = analysis["error_path"]; return result;
      }
      if ((int?)analysis["version"] != 1 || analysis["complete"]?.Type != JTokenType.Boolean
        || analysis["remote_content"]?.Type != JTokenType.Boolean || !(analysis["required_features"] is JArray)
        || ((JArray)analysis["required_features"]).Any(value => value.Type != JTokenType.String || !knownFeatures.Contains((string)value))) { return result; }
      var controls = analysis["request_controls"] as JArray;
      if (controls == null || controls.Any(value => value.Type != JTokenType.String
        || (string)value != "compaction_trigger") || controls.Count > 1) { return result; }
      var requiredFeatures = ((JArray)analysis["required_features"]).Values<string>().ToArray();
      if (!legacyEstimate && ((bool?)analysis["complete"] != true
        || requiredFeatures.Any(feature => !supported.Values<string>().Contains(feature)))) {
        result["error"] = "budget_count_bound_unavailable"; return result;
      }
      result["error"] = "budget_plan_failed";
      var body = context.Request.Body.As<JObject>(preserveContent: true);
      if ((bool?)body["stream"] == true && !context.Variables.GetValueOrDefault<bool>("selectedBudgetObserver", false)) {
        result["error"] = "budget_stream_evidence_unavailable"; return result;
      }
      string normalizedBody = null;
      var thinking = body["thinking"] as JObject;
      if ((int?)requestPolicy?["version"] == 3 && body["stream"]?.Type == JTokenType.Boolean
        && (bool)body["stream"] && thinking?["type"]?.Type == JTokenType.String
        && (string)thinking["type"] == "adaptive" && thinking.Property("display") == null) {
        thinking["display"] = "summarized";
        normalizedBody = body.ToString(Newtonsoft.Json.Formatting.None);
      }
      var field = passthrough ? "" : operation == "responses" ? "max_output_tokens"
        : operation == "anthropic-messages" ? "max_tokens"
        : (string)contract["output_field"];
      if (!passthrough && field != "max_tokens" && field != "max_completion_tokens" && field != "max_output_tokens") {
        result["error"] = "budget_output_limit_unavailable"; return result;
      }
      if (!passthrough && body["max_tokens"] != null && body["max_completion_tokens"] != null) {
        result["error"] = "budget_output_limit_invalid"; return result;
      }
      var requested = passthrough
        ? (operation == "responses" && controls.Count > 0 ? body["max_output_tokens"] : null)
        : body[field];
      if (!passthrough && operation == "chat-completions") { requested = body["max_completion_tokens"] ?? body["max_tokens"]; }
      if (requested != null && (requested.Type != JTokenType.Integer || (long)requested <= 0)) {
        result["error"] = "budget_output_limit_invalid"; return result;
      }
      long output = passthrough ? 0L : requested == null
        ? legacyEstimate ? (long)contract["output_estimate"] : Math.Min(4096L, (long)contract["output_ceiling"])
        : (long)requested;
      long minimumOutput = 0L;
      foreach (var control in controls.Values<string>()) {
        if (requestPolicy == null) { result["error"] = "budget_output_limit_unavailable"; return result; }
        var requirements = requestPolicy["output_requirements"] as JObject;
        var requirement = requirements?[control] as JObject;
        if ((passthrough && algorithm != "request_total_ceiling_v1") || operation != "responses" || requirement == null) {
          result["error"] = "budget_output_limit_unavailable"; return result;
        }
        if (requirement.Properties().Any(property => property.Name != "minimum_output_tokens")
          || requirement["minimum_output_tokens"]?.Type != JTokenType.Integer
          || (long)requirement["minimum_output_tokens"] <= 0) {
          result["error"] = "budget_configuration_invalid"; return result;
        }
        minimumOutput = Math.Max(minimumOutput, (long)requirement["minimum_output_tokens"]);
      }
      if (requested == null && !passthrough) { output = Math.Max(output, minimumOutput); }
      if ((!passthrough && output < minimumOutput)
        || (passthrough && requested != null && (long)requested < minimumOutput)) {
        result["error"] = "budget_output_limit_invalid"; return result;
      }
      if (!passthrough && !legacyEstimate && output > (long)contract["output_ceiling"]) {
        result["error"] = "budget_output_limit_invalid"; return result;
      }
      long samples = 1L;
      if (operation == "chat-completions") {
        foreach (var sampleField in new[] { "n", "best_of" }) {
          var count = body[sampleField];
          if (count == null) { continue; }
          if (count.Type != JTokenType.Integer || (long)count <= 0) {
            result["error"] = "budget_output_limit_invalid"; return result;
          }
          samples = Math.Max(samples, (long)count);
        }
      }
      if (normalizedBody != null) {
        if (!string.IsNullOrEmpty(field)) { body[field] = output; }
        normalizedBody = body.ToString(Newtonsoft.Json.Formatting.None);
      }
      long bodyBytes = normalizedBody == null
        ? context.Request.Body.As<byte[]>(preserveContent: true).LongLength
        : Encoding.UTF8.GetByteCount(normalizedBody);
      long bound = legacyEstimate ? checked(bodyBytes + (passthrough ? (long)contract["output_estimate"] : checked(output * samples)))
        : passthrough ? checked((long)contract["request_token_ceiling"] * samples) : checked((long)contract["input_ceiling"] + checked(output * samples));
      if (normalizedBody != null) { result["normalized_body"] = normalizedBody; }
      if (!hasPerson && context.Subscription != null && (string)applicationMapping["status"] == "ok"
        && (string)applicationMapping["applicationType"] == "system") {
        result["error"] = ""; result["bypass"] = true; result["receipts"] = new JArray(); return result;
      }
      var now = (DateTime)context.Variables["requestStarted"];
      var scopes = new JArray();
      if (context.Variables.GetValueOrDefault<bool>("hasEmployeeToken", false)) {
        if (context.Variables.GetValueOrDefault<string>("modelAdmission", "") == "ledger_unavailable") {
          result["error"] = "budget_ledger_unavailable"; return result;
        }
        scopes.Add(new JObject(new JProperty("scope_type", "person"),
          new JProperty("scope_id", (string)context.Variables["employeeName"])));
      }
      if (context.Subscription != null) {
        if (context.Variables.GetValueOrDefault<string>("applicationAdmission", "") == "application_ledger_unavailable") {
          result["error"] = "budget_ledger_unavailable"; return result;
        }
        var mapping = context.Variables.GetValueOrDefault<JObject>("applicationMapState", new JObject());
        if ((string)mapping["status"] != "ok") { result["error"] = "budget_identity_unavailable"; return result; }
        if ((string)mapping["applicationType"] != "system") {
          scopes.Add(new JObject(new JProperty("scope_type", "application"),
            new JProperty("scope_id", Guid.Parse((string)mapping["applicationId"]).ToString())));
        }
      }
      if (scopes.Count == 0) { result["error"] = "budget_identity_unavailable"; return result; }
      var receipts = new JArray();
      var parts = new JArray("__GATEWAY_PROFILE_ID__", context.Deployment.Gateway.Id, context.RequestId.ToString());
      string key;
      using (var digest = System.Security.Cryptography.SHA256.Create()) {
        key = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(
          parts.ToString(Newtonsoft.Json.Formatting.None)))).Replace("-", "").ToLowerInvariant();
      }
      foreach (JObject scope in scopes) {
        scope["period_start"] = now.ToString("yyyy-MM-01");
        receipts.Add(new JObject(new JProperty("epoch", epoch), new JProperty("reserved_tokens", bound),
          new JProperty("identity", new JObject(new JProperty("scope", scope),
            new JProperty("gateway_profile_id", "__GATEWAY_PROFILE_ID__"),
            new JProperty("native_gateway_id", context.Deployment.Gateway.Id),
            new JProperty("correlation_id", context.RequestId.ToString()),
            new JProperty("model_id", model), new JProperty("admitted_at", now.ToString("o"))))));
      }
      result["error"] = ""; result["attempt_key"] = key; result["receipts"] = receipts;
      result["model_id"] = model;
      result["output_field"] = field; result["output_limit"] = output;
      if (rejectionPolicy != null) { result["rejection_policy"] = rejectionPolicy; }
      result["output_samples"] = samples;
      result["algorithm"] = algorithm;
      if (algorithm == "request_total_ceiling_v1") { result["request_token_ceiling"] = bound; }
      result["count_assurance"] = legacyEstimate ? "estimate" : "verified_upper_bound";
      result["contract_sha256"] = contract["evidence_sha256"];
      result["input_ceiling"] = contract["input_ceiling"];
      result["required_features"] = new JArray(requiredFeatures);
      result["request_controls"] = controls.DeepClone();
      result["request_analysis"] = analysis.DeepClone();
      result["body_bytes_estimate"] = bodyBytes;
      return result;
    } catch { return result; }
}"""

NORMALIZED_REQUEST_BODY = """@{
  var plan = (JObject)context.Variables["executionPlan"];
  return (string)plan["normalized_body"] ?? context.Request.Body.As<string>(preserveContent: true);
}"""

NATIVE_COUNT_PLAN = """@{
    var disabled = new JObject(new JProperty("enabled", false));
    try {
      var plan = (JObject)context.Variables["executionPlan"];
      var config = JObject.Parse((string)context.Variables["budgetExecutionFence"]);
      var model = context.Variables.ContainsKey("selectedBudgetCountContract")
        ? (JObject)JObject.Parse((string)context.Variables["selectedBudgetCountContract"])["contract"]
        : (JObject)config["model_contracts"][(string)context.Variables["selectedBudgetModelKey"]];
      var contract = (model["protocols"] as JObject)?[context.Operation.Id] as JObject ?? model;
      var counter = contract["native_counter"] as JObject;
      if (counter == null || (string)plan["algorithm"] != "context_input_ceiling_v1") { return disabled; }
      if ((plan["request_controls"] as JArray)?.Count > 0) { return disabled; }
      var kind = (string)counter["kind"];
      if ((kind != "anthropic_messages" || context.Operation.Id != "anthropic-messages")
        && (kind != "openai_responses" || context.Operation.Id != "responses")) { return disabled; }
      var supported = counter["supported_features"] as JArray ?? new JArray();
      if (((JArray)plan["required_features"]).Values<string>().Any(value => !supported.Values<string>().Contains(value)
        || value == "server_tools" || value == "background")) { return disabled; }
      var origin = new Uri(context.Variables.GetValueOrDefault<string>("selectedBudgetCounterOrigin", ""));
      var endpoint = new Uri((string)counter["endpoint"]);
      var suffix = kind == "anthropic_messages" ? "/messages/count_tokens" : "/responses/input_tokens";
      if (origin.Scheme != "https" || endpoint.Scheme != "https" || origin.Host != endpoint.Host
        || origin.Port != endpoint.Port || !string.IsNullOrEmpty(endpoint.UserInfo)
        || !string.IsNullOrEmpty(endpoint.Query) || !string.IsNullOrEmpty(endpoint.Fragment)
        || !endpoint.AbsolutePath.EndsWith(suffix, StringComparison.Ordinal)
        || !endpoint.AbsolutePath.StartsWith(origin.AbsolutePath.TrimEnd('/') + "/", StringComparison.Ordinal)) { return disabled; }
      var authentication = context.Variables.GetValueOrDefault<string>("selectedBudgetCounterAuth", "");
      if (!new[] { "managed_identity", "named_value_api_key", "named_value_bearer", "none" }.Contains(authentication)) { return disabled; }
      var request = context.Request.Body.As<JObject>(preserveContent: true);
      var analysis = plan["request_analysis"] as JObject;
      if (analysis == null || (bool?)analysis["complete"] != true
        || ((string)counter["assurance"] == "upper_bound" && (bool?)analysis["remote_content"] != false)) { return disabled; }
      var fields = kind == "anthropic_messages"
        ? new[] { "messages", "system", "tools", "tool_choice", "thinking", "output_config", "cache_control" }
        : new[] { "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "previous_response_id", "conversation", "text", "reasoning", "truncation" };
      var ignored = kind == "anthropic_messages" ? new[] { "model", "max_tokens", "stream", "temperature", "top_p", "top_k", "stop_sequences", "metadata" }
        : new[] { "model", "max_output_tokens", "stream", "temperature", "top_p", "store", "metadata", "include", "service_tier", "user", "safety_identifier", "prompt_cache_key", "prompt_cache_retention", "stream_options" };
      if (request.Properties().Any(property => !fields.Contains(property.Name) && !ignored.Contains(property.Name))) { return disabled; }
      var payload = new JObject(new JProperty("model", (string)context.Variables["selectedBudgetUpstreamModel"]));
      foreach (var field in fields) { if (request[field] != null) { payload[field] = request[field].DeepClone(); } }
      string fingerprint;
      using (var digest = System.Security.Cryptography.SHA256.Create()) {
        fingerprint = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(payload.ToString(Newtonsoft.Json.Formatting.None))))
          .Replace("-", "").ToLowerInvariant();
      }
      return new JObject(new JProperty("enabled", true), new JProperty("endpoint", endpoint.AbsoluteUri),
        new JProperty("payload", payload), new JProperty("assurance", counter["assurance"]),
        new JProperty("padding", counter["upper_bound_padding_tokens"] ?? new JValue(0)),
        new JProperty("fingerprint", fingerprint), new JProperty("kind", kind));
    } catch { return disabled; }
}"""

NATIVE_COUNT_APPLY = """@{
    var plan = (JObject)((JObject)context.Variables["executionPlan"]).DeepClone();
    plan["count_status"] = "unavailable";
    try {
      var response = context.Variables.GetValueOrDefault<IResponse>("executionCountResponse", null);
      if (response == null || response.StatusCode != 200) { return plan; }
      var value = response.Body.As<JObject>(preserveContent: true)["input_tokens"];
      if (value == null || value.Type != JTokenType.Integer || (long)value < 0) { return plan; }
      var count = (JObject)context.Variables["executionCountPlan"];
      plan["input_estimate"] = value; plan["count_status"] = "estimated";
      plan["count_fingerprint"] = count["fingerprint"];
      if ((string)count["assurance"] != "upper_bound") { return plan; }
      if (count["padding"]?.Type != JTokenType.Integer || (long)count["padding"] < 0) { return plan; }
      long input = Math.Min((long)plan["input_ceiling"], checked((long)value + (long)count["padding"]));
      long bound = checked(input + checked((long)plan["output_limit"] * (long)plan["output_samples"]));
      foreach (JObject receipt in (JArray)plan["receipts"]) { receipt["reserved_tokens"] = bound; }
      plan["input_ceiling"] = input; plan["algorithm"] = "provider_input_count_v1";
      plan["count_status"] = "verified_upper_bound";
      return plan;
    } catch { return plan; }
}"""

SCOPE_PARTITION = """@{
    var receipt = (JObject)context.Variables["executionReceipt"];
    var scope = (JObject)receipt["identity"]["scope"];
    string hash;
    using (var digest = System.Security.Cryptography.SHA256.Create()) {
      hash = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes((string)scope["scope_id"])))
        .Replace("-", "").ToLowerInvariant();
    }
    return "budget2|" + (string)scope["scope_type"] + "|" + hash + "|" + ((string)scope["period_start"]).Substring(0, 7);
}"""

USD_PRICE_PROJECTION = """@{
    var result = new JObject(new JProperty("error", "budget_price_unavailable"));
    try {
      var response = context.Variables.GetValueOrDefault<IResponse>("executionPriceProjectionRead", null);
      if (response == null || response.StatusCode != 200) { return result; }
      var entity = response.Body.As<JObject>(preserveContent: true);
      var etag = response.Headers.GetValueOrDefault("ETag", (string)entity["odata.etag"] ?? "");
      if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.Contains("\\r") || etag.Contains("\\n")) { return result; }
      var document = JObject.Parse((string)entity["Document"]);
      var plan = (JObject)context.Variables["executionPlan"];
      var schedules = document["rate_schedules"] as JArray;
      if ((int?)document["version"] != 1 || (string)document["model_id"] != (string)plan["model_id"]
        || !System.Text.RegularExpressions.Regex.IsMatch((string)document["price_sha256"] ?? "", "^[0-9a-f]{64}$")
        || document["price"] == null || document["price"].Type == JTokenType.Null
        || schedules == null || schedules.Count < 1 || schedules.Count > 32) { return result; }
      var updated = document["updated_at"].Type == JTokenType.Date
        ? new DateTimeOffset(document["updated_at"].Value<DateTime>())
        : DateTimeOffset.Parse((string)document["updated_at"]);
      var now = DateTimeOffset.UtcNow;
      if (updated > now) { return result; }
      long nextMinimum = 0;
      for (int index = 0; index < schedules.Count; index++) {
        var tier = schedules[index] as JObject;
        var fields = new[] { "min_input_tokens", "max_input_tokens", "input_rate_units",
          "cached_rate_units", "cache_write_rate_units", "output_rate_units" };
        if (tier == null || tier.Properties().Any(property => !fields.Contains(property.Name))
          || tier.Properties().Count() != fields.Length || tier["min_input_tokens"]?.Type != JTokenType.Integer
          || (long)tier["min_input_tokens"] != nextMinimum
          || new[] { "input_rate_units", "cached_rate_units", "cache_write_rate_units", "output_rate_units" }
            .Any(field => tier[field]?.Type != JTokenType.Integer || (long)tier[field] < 0)) { return result; }
        if (tier["max_input_tokens"] == null || tier["max_input_tokens"].Type == JTokenType.Null) {
          if (index != schedules.Count - 1) { return result; }
          nextMinimum = -1;
        } else {
          if (tier["max_input_tokens"].Type != JTokenType.Integer
            || (long)tier["max_input_tokens"] < nextMinimum || nextMinimum < 0) { return result; }
          nextMinimum = checked((long)tier["max_input_tokens"] + 1L);
          if (index == schedules.Count - 1) { return result; }
        }
      }
      if (nextMinimum != -1) { return result; }
      result["error"] = ""; result["document"] = document; result["etag"] = etag;
      return result;
    } catch { return result; }
}"""

USD_RESERVATION = """@{
    var result = new JObject(new JProperty("error", "budget_price_unavailable"));
    try {
      var projection = (JObject)context.Variables["executionPriceProjection"];
      if (!string.IsNullOrEmpty((string)projection["error"])) { return result; }
      var document = (JObject)projection["document"];
      var plan = (JObject)context.Variables["executionPlan"];
      decimal maxInputRate = 0m, maxOutputRate = 0m, maxAnyRate = 0m;
      foreach (JObject tier in (JArray)document["rate_schedules"]) {
        var rates = new[] { "input_rate_units", "cached_rate_units", "cache_write_rate_units", "output_rate_units" }
          .Select(field => (decimal)tier[field]).ToArray();
        maxInputRate = Math.Max(maxInputRate, Math.Max(rates[0], Math.Max(rates[1], rates[2])));
        maxOutputRate = Math.Max(maxOutputRate, rates[3]);
        maxAnyRate = Math.Max(maxAnyRate, rates.Max());
      }
      decimal amount;
      if ((string)plan["algorithm"] == "context_input_ceiling_v1"
        || (string)plan["algorithm"] == "provider_input_count_v1") {
        long input = checked((long)plan["input_ceiling"]);
        long output = checked((long)plan["output_limit"] * (long)plan["output_samples"]);
        amount = (input * maxInputRate + output * maxOutputRate) / 100000000000000m;
      } else if ((string)plan["algorithm"] == "request_total_ceiling_v1") {
        amount = (long)plan["request_token_ceiling"] * maxAnyRate / 100000000000000m;
      } else { result["error"] = "budget_count_bound_unavailable"; return result; }
      amount = Math.Ceiling(amount * 100000000000000m) / 100000000000000m;
      if (amount < 0m) { return result; }
      result["error"] = ""; result["reserved_usd"] = amount;
      result["price_snapshot"] = document.DeepClone();
      result["price_etag"] = (string)projection["etag"];
      return result;
    } catch { return result; }
}"""

RESERVATION = """@{
    var result = new JObject(new JProperty("error", "budget_ledger_unavailable"));
    try {
      var response = (IResponse)context.Variables["executionSummaryRead"];
      if (response == null || response.StatusCode != 200) { return result; }
      var entity = response.Body.As<JObject>(preserveContent: true);
      var state = JObject.Parse((string)entity["Document"]);
      var etag = response.Headers.GetValueOrDefault("ETag", (string)entity["odata.etag"] ?? "");
      if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.Contains("\\r") || etag.Contains("\\n")) { return result; }
      var receipt = (JObject)context.Variables["executionReceipt"];
      if ((int?)state["protocol_version"] != 2 || (string)state["epoch"] != (string)receipt["epoch"]
        || !JToken.DeepEquals(state["scope"], receipt["identity"]["scope"])) { return result; }
      var mode = (string)state["budget_mode"] ?? "tokens";
      if (mode != "tokens" && mode != "usd") { return result; }
      if (mode == "usd" && (state["enabled"]?.Type != JTokenType.Boolean || !(bool)state["enabled"]
        || state["enforce"]?.Type != JTokenType.Boolean)) {
        result["error"] = "budget_consumer_disabled"; return result;
      }
      JObject priceEvidence = null;
      string priceEtag = null;
      decimal requestedUsd = 0m;
      if (mode == "usd") {
        var usdReservation = context.Variables.GetValueOrDefault<JObject>("executionUsdReservation", null);
        if (usdReservation == null || !string.IsNullOrEmpty((string)usdReservation["error"])) {
          result["error"] = (string)usdReservation?["error"] ?? "budget_price_unavailable"; return result;
        }
        if (usdReservation["reserved_usd"]?.Type != JTokenType.Float
          && usdReservation["reserved_usd"]?.Type != JTokenType.Integer) { return result; }
        requestedUsd = usdReservation["reserved_usd"].Value<decimal>();
        priceEvidence = (JObject)usdReservation["price_snapshot"].DeepClone();
        priceEtag = (string)usdReservation["price_etag"];
        if (state["usd_limit"] == null || state["usd_limit"].Type == JTokenType.Null
          || requestedUsd < 0m) { return result; }
        var names = new[] { "usd_limit", "confirmed_usd", "active_reserved_usd", "upper_bound_usd" };
        var amounts = new decimal[names.Length];
        for (int index = 0; index < names.Length; index++) {
          var token = state[names[index]];
          if (token == null) { amounts[index] = 0m; }
          else if (token.Type == JTokenType.String) {
            var number = JToken.Parse((string)token);
            if (number.Type != JTokenType.Integer && number.Type != JTokenType.Float) { return result; }
            amounts[index] = JsonConvert.DeserializeObject<decimal>((string)token);
          } else if (token.Type == JTokenType.Integer || token.Type == JTokenType.Float) {
            amounts[index] = token.Value<decimal>();
          } else { return result; }
          if (amounts[index] < 0m) { return result; }
        }
        decimal limitUsd = amounts[0];
        decimal confirmedUsd = amounts[1];
        decimal reservedUsd = amounts[2];
        decimal upperBoundUsd = amounts[3];
        decimal availableUsd = checked(limitUsd - confirmedUsd - reservedUsd - upperBoundUsd);
        if ((bool)state["enforce"] && requestedUsd > availableUsd) {
          decimal unreservedUsd = checked(limitUsd - confirmedUsd - upperBoundUsd);
          result["error"] = unreservedUsd <= 0m ? "budget_exhausted"
            : requestedUsd <= unreservedUsd ? "budget_temporarily_reserved"
            : "budget_request_bound_exceeds_balance";
          return result;
        }
        state["active_reserved_usd"] = JsonConvert.SerializeObject(checked(reservedUsd + requestedUsd));
      }
      var counters = new[] { state["version"], state["token_limit"], state["confirmed_tokens"],
        state["active_reserved_tokens"], state["upper_bound_tokens"], receipt["reserved_tokens"] };
      if (counters.Any(value => value == null || value.Type != JTokenType.Integer || (long)value < 0)
        || state["enforce"]?.Type != JTokenType.Boolean || state["enabled"]?.Type != JTokenType.Boolean) { return result; }
      result["snapshot"] = state.DeepClone();
      if (!(bool)state["enabled"]) { result["error"] = "budget_consumer_disabled"; return result; }
      long requested = (long)receipt["reserved_tokens"];
      if (mode == "tokens") {
        long unreserved = checked((long)state["token_limit"] - (long)state["confirmed_tokens"] - (long)state["upper_bound_tokens"]);
        long available = checked(unreserved - (long)state["active_reserved_tokens"]);
        if ((bool)state["enforce"] && requested > available) {
          result["error"] = unreserved <= 0 ? "budget_exhausted"
            : requested <= unreserved ? "budget_temporarily_reserved" : "budget_request_bound_exceeds_balance";
          return result;
        }
        state["active_reserved_tokens"] = checked((long)state["active_reserved_tokens"] + requested);
      }
      state["version"] = checked((long)state["version"] + 1L);
      state["updated_at"] = DateTime.UtcNow.ToString("o");
      var attempt = new JObject(new JProperty("identity", receipt["identity"].DeepClone()),
        new JProperty("epoch", receipt["epoch"]), new JProperty("state", "reserved"),
        new JProperty("reserved_tokens", requested), new JProperty("actual_tokens", null),
        new JProperty("evidence_revision", 0), new JProperty("evidence_key", null),
        new JProperty("updated_at", state["updated_at"]));
      if (mode == "usd") {
        attempt["budget_mode"] = "usd";
        attempt["reserved_usd"] = JsonConvert.SerializeObject(requestedUsd);
        attempt["price_snapshot"] = priceEvidence.DeepClone();
        attempt["price_etag"] = priceEtag;
      }
      var rejectionPolicy = context.Variables.GetValueOrDefault<JObject>("executionPlan", null)?["rejection_policy"];
      if (rejectionPolicy != null) { attempt["rejection_policy"] = rejectionPolicy.DeepClone(); }
      result["error"] = ""; result["summary"] = state; result["attempt"] = attempt; result["etag"] = etag;
      return result;
    } catch { return result; }
}"""

BATCH = """@{
    var prepared = (JObject)context.Variables["executionReservation"];
    var partition = (string)context.Variables["executionPartition"];
    var attemptKey = (string)((JObject)context.Variables["executionPlan"])["attempt_key"];
    var batch = (string)context.Variables["executionBoundary"];
    var changeset = "change_" + context.RequestId.ToString("N");
    var root = "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__";
    var names = new[] { "S", "A|" + attemptKey };
    var documents = new[] { prepared["summary"], prepared["attempt"] };
    var etags = new[] { (string)prepared["etag"], (string)prepared["attempt_etag"] };
    var text = new StringBuilder();
    text.Append("--" + batch + "\\r\\nContent-Type: multipart/mixed; boundary=" + changeset + "\\r\\n\\r\\n");
    for (int index = 0; index < names.Length; index++) {
      var entity = new JObject(new JProperty("PartitionKey", partition), new JProperty("RowKey", names[index]),
        new JProperty("Document", documents[index].ToString(Newtonsoft.Json.Formatting.None)));
      text.Append("--" + changeset + "\\r\\nContent-Type: application/http\\r\\nContent-Transfer-Encoding: binary\\r\\nContent-ID: " + (index + 1).ToString() + "\\r\\n\\r\\n");
      bool update = !string.IsNullOrEmpty(etags[index]);
      var address = update ? root + "(PartitionKey=\\u0027" + Uri.EscapeDataString(partition.Replace("\\u0027", "\\u0027\\u0027")) + "\\u0027,RowKey=\\u0027" + Uri.EscapeDataString(names[index]) + "\\u0027)" : root;
      text.Append((update ? "PUT " : "POST ") + address + " HTTP/1.1\\r\\nContent-Type: application/json\\r\\nAccept: application/json;odata=nometadata\\r\\nPrefer: return-no-content\\r\\nDataServiceVersion: 3.0;\\r\\n");
      if (update) { text.Append("If-Match: " + etags[index] + "\\r\\n"); }
      text.Append("\\r\\n" + entity.ToString(Newtonsoft.Json.Formatting.None) + "\\r\\n");
    }
    text.Append("--" + changeset + "--\\r\\n--" + batch + "--\\r\\n");
    return text.ToString();
}"""

BATCH_RESULT = r"""@{
    try {
      var response = (IResponse)context.Variables["executionWrite"];
      if (response == null || response.StatusCode != 202) { return "unavailable"; }
      var contentType = response.Headers.GetValueOrDefault("Content-Type", "");
      if (!contentType.StartsWith("multipart/mixed", StringComparison.OrdinalIgnoreCase)) { return "unavailable"; }
      var body = response.Body.As<string>(preserveContent: true);
      var statuses = body.Split(new[] { "\n" }, StringSplitOptions.None)
        .Where(line => System.Text.RegularExpressions.Regex.IsMatch(line, @"^HTTP/1\.1 \d{3}[^\r\n]*\r?$"))
        .Select(line => line.Substring(9, 3)).ToArray();
      if (statuses.Any(value => value == "409" || value == "412")) { return "conflict"; }
      return statuses.Length == 2 && statuses.All(value => value == "204")
        ? "ok" : "unavailable";
    } catch { return "unavailable"; }
}"""

COMPLETE_USAGE = """@{
    var missing = new JObject(new JProperty("input_tokens", null), new JProperty("cached_tokens", null),
      new JProperty("output_tokens", null), new JProperty("cache_write_tokens", null),
      new JProperty("tokens_consumed", null), new JProperty("estimated", true),
      new JProperty("usage_complete", false), new JProperty("ingest_error", "usage_incomplete"));
    try {
      var body = context.Response.Body.As<JObject>(preserveContent: true);
      var error = body["error"] as JObject;
      if (new[] { "responses", "responses-compact", "chat-completions", "anthropic-messages", "images-generations", "microsoft-decision" }.Contains(context.Operation.Id)
        && context.Response.StatusCode >= 400 && context.Response.StatusCode < 500
        && !new[] { 408, 499 }.Contains(context.Response.StatusCode)
        && context.Response.Headers.GetValueOrDefault("Content-Type", "").Split(';')[0].Trim().ToLowerInvariant() == "application/json"
        && body.Properties().All(property => new[] { "error", "type", "request_id" }.Contains(property.Name)) && error != null
        && (body["type"] == null || (body["type"].Type == JTokenType.String && (string)body["type"] == "error"))
        && (body["request_id"] == null || body["request_id"].Type == JTokenType.String)
        && !error.Properties().Any(property => new[] { "usage", "output", "content", "partial_response" }.Contains(property.Name))) {
        var rule = new JObject(new JProperty("status_code", context.Response.StatusCode));
        var parts = new JArray(context.Operation.Id, context.Response.StatusCode);
        foreach (var errorField in new[] { "type", "code", "param" }) {
          var raw = error[errorField];
          var fieldState = raw == null ? "absent" : raw.Type == JTokenType.Null ? "null" : "value";
          var observed = new JObject(new JProperty("state", fieldState));
          if (fieldState == "value") { observed["value"] = raw.DeepClone(); }
          rule["error_" + errorField] = observed;
          parts.Add(fieldState); parts.Add(fieldState == "value" ? raw.DeepClone() : JValue.CreateNull());
        }
        if (""" + _rejection_rule_condition("rule", "2") + """) {
          rule["version"] = 2; rule["operation"] = context.Operation.Id;
          using (var digest = System.Security.Cryptography.SHA256.Create()) {
            rule["observation_sha256"] = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(parts.ToString(Newtonsoft.Json.Formatting.None))))
              .Replace("-", "").ToLowerInvariant();
          }
          missing["terminal_rejection"] = rule;
          missing["terminal_observed_at"] = DateTime.UtcNow.ToString("o");
          return missing;
        }
      }
      if (context.Operation.Id == "responses" && (body["status"]?.Type != JTokenType.String
        || !new[] { "completed", "incomplete", "failed", "cancelled" }.Contains((string)body["status"]))) { return missing; }
      var usage = body["usage"] as JObject;
      if (usage == null) { return missing; }
      bool anthropic = context.Operation.Id == "anthropic-messages";
      bool decision = context.Operation.Id == "microsoft-decision";
      bool chat = context.Operation.Id == "chat-completions";
      var inputValue = usage[chat ? "prompt_tokens" : "input_tokens"]
        ?? (decision ? usage["prompt_tokens"] : null);
      var outputValue = usage[chat ? "completion_tokens" : "output_tokens"]
        ?? (decision ? usage["completion_tokens"] : null);
      var read = anthropic ? usage["cache_read_input_tokens"]
        : (usage["prompt_tokens_details"] ?? usage["input_tokens_details"])?["cached_tokens"];
      var write = anthropic ? usage["cache_creation_input_tokens"] : usage["prompt_tokens_details"]?["cache_write_tokens"];
      var required = new[] { inputValue, outputValue };
      if (required.Any(value => value == null || value.Type != JTokenType.Integer || (long)value < 0)
        || (read != null && (read.Type != JTokenType.Integer || (long)read < 0))
        || (write != null && (write.Type != JTokenType.Integer || (long)write < 0))) { return missing; }
      long incoming = (long)inputValue, outgoing = (long)outputValue;
      long cached = checked((read == null ? 0L : (long)read) + (write == null ? 0L : (long)write));
      long total = checked(incoming + outgoing + (anthropic ? cached : 0L));
      if (!anthropic && (cached > incoming
        || (!decision || usage["total_tokens"] != null)
          && (usage["total_tokens"]?.Type != JTokenType.Integer
            || (long)usage["total_tokens"] != total))) { return missing; }
      if (anthropic && (read == null || write == null)) { return missing; }
      var result = new JObject(new JProperty("input_tokens", anthropic ? incoming : incoming - cached),
        new JProperty("cached_tokens", cached), new JProperty("cache_write_tokens", write == null ? 0L : (long)write),
        new JProperty("output_tokens", outgoing), new JProperty("tokens_consumed", total),
        new JProperty("usage_complete", true), new JProperty("estimated", false), new JProperty("ingest_error", null));
      var dispatch = context.Variables.GetValueOrDefault<JObject>("executionDispatch", null);
      var usdReceipts = (dispatch?["receipts"] as JArray ?? new JArray()).OfType<JObject>()
        .Where(receipt => (string)receipt["budget_mode"] == "usd").ToArray();
      if (usdReceipts.Length > 0) {
        var plan = (JObject)context.Variables["executionPlan"];
        var snapshots = usdReceipts
          .Select(item => item["price_snapshot"] as JObject).ToArray();
        if (snapshots.Length < 1 || snapshots.Length > 2 || snapshots.Any(priceValue => priceValue == null)
          || snapshots.Any(priceDocument => !JToken.DeepEquals(priceDocument, snapshots[0]))) { return missing; }
        var snapshot = snapshots[0];
        var schedules = snapshot["rate_schedules"] as JArray;
        if ((int?)snapshot["version"] != 1 || (string)snapshot["model_id"] != (string)plan["model_id"]
          || !System.Text.RegularExpressions.Regex.IsMatch((string)snapshot["price_sha256"] ?? "", "^[0-9a-f]{64}$")
          || schedules == null || schedules.Count < 1 || schedules.Count > 32) { return missing; }
        long nextMinimum = 0;
        JObject selectedTier = null;
        long billedInput = checked((long)result["input_tokens"] + (long)result["cached_tokens"]);
        for (int index = 0; index < schedules.Count; index++) {
          var tier = schedules[index] as JObject;
          var fields = new[] { "min_input_tokens", "max_input_tokens", "input_rate_units",
            "cached_rate_units", "cache_write_rate_units", "output_rate_units" };
          if (tier == null || tier.Properties().Any(property => !fields.Contains(property.Name))
            || tier.Properties().Count() != fields.Length || tier["min_input_tokens"]?.Type != JTokenType.Integer
            || (long)tier["min_input_tokens"] != nextMinimum
            || new[] { "input_rate_units", "cached_rate_units", "cache_write_rate_units", "output_rate_units" }
              .Any(field => tier[field]?.Type != JTokenType.Integer || (long)tier[field] < 0)) { return missing; }
          bool selected = billedInput >= nextMinimum;
          if (tier["max_input_tokens"] == null || tier["max_input_tokens"].Type == JTokenType.Null) {
            if (index != schedules.Count - 1) { return missing; }
            if (selected) { selectedTier = tier; }
            nextMinimum = -1;
          } else {
            if (tier["max_input_tokens"].Type != JTokenType.Integer
              || (long)tier["max_input_tokens"] < nextMinimum || nextMinimum < 0) { return missing; }
            if (selected && billedInput <= (long)tier["max_input_tokens"]) { selectedTier = tier; }
            nextMinimum = checked((long)tier["max_input_tokens"] + 1L);
            if (index == schedules.Count - 1) { return missing; }
          }
        }
        if (nextMinimum != -1 || selectedTier == null) { return missing; }
        decimal inputRate = selectedTier["input_rate_units"].Value<decimal>();
        decimal cachedRate = selectedTier["cached_rate_units"].Value<decimal>();
        decimal writeRate = selectedTier["cache_write_rate_units"].Value<decimal>();
        decimal outputRate = selectedTier["output_rate_units"].Value<decimal>();
        decimal cacheReads = (long)result["cached_tokens"] - (write == null ? 0L : (long)write);
        decimal cost = ((decimal)(long)result["input_tokens"] * inputRate
          + cacheReads * cachedRate + (decimal)(write == null ? 0L : (long)write) * writeRate
          + (decimal)outgoing * outputRate) / 100000000000000m;
        result["usd_cost"] = JsonConvert.SerializeObject(cost);
        result["budget_price_projection"] = snapshot.DeepClone();
      }
      return result;
    } catch { return missing; }
}"""

COMPLETION = """@{
    var result = new JObject(new JProperty("error", "budget_completion_unavailable"));
  try {
      var summaryRead = (IResponse)context.Variables["executionSummaryRead"];
      var attemptRead = (IResponse)context.Variables["executionAttemptRead"];
      if (summaryRead == null || summaryRead.StatusCode != 200
        || attemptRead == null || attemptRead.StatusCode != 200) { return result; }
      var summaryEntity = summaryRead.Body.As<JObject>(preserveContent: true);
      var attemptEntity = attemptRead.Body.As<JObject>(preserveContent: true);
      var state = JObject.Parse((string)summaryEntity["Document"]);
      var attempt = JObject.Parse((string)attemptEntity["Document"]);
      var receipt = JObject.Parse(((JObject)context.Variables["executionReceipt"]).ToString(Newtonsoft.Json.Formatting.None));
      var etag = summaryRead.Headers.GetValueOrDefault("ETag", (string)summaryEntity["odata.etag"] ?? "");
      var attemptEtag = attemptRead.Headers.GetValueOrDefault("ETag", (string)attemptEntity["odata.etag"] ?? "");
      if (new[] { etag, attemptEtag }.Any(value => string.IsNullOrWhiteSpace(value) || value == "*" || value.Contains("\\r") || value.Contains("\\n"))) { return result; }
      if ((int?)state["protocol_version"] != 2 || (string)state["epoch"] != (string)receipt["epoch"]
        || (string)attempt["epoch"] != (string)receipt["epoch"]
        || !JToken.DeepEquals(attempt["identity"], receipt["identity"])
        || !JToken.DeepEquals(state["scope"], receipt["identity"]["scope"])
        || !JToken.DeepEquals(attempt["reserved_tokens"], receipt["reserved_tokens"])) { return result; }
      var usage = (JObject)((JObject)context.Variables["usagePayload"]).DeepClone();
      var rejected = usage["terminal_rejection"] as JObject;
      bool rejection = (bool?)usage["usage_complete"] != true && rejected != null;
      JObject rejectionEvidence = null;
      if (rejection) {
        var policy = attempt["rejection_policy"] as JObject;
        var rules = policy?["rules"] as JArray;
        if (policy == null || policy["version"]?.Type != JTokenType.Integer || !new[] { 1, 2 }.Contains((int)policy["version"])
          || policy["evidence_sha256"]?.Type != JTokenType.String
          || !System.Text.RegularExpressions.Regex.IsMatch((string)policy["evidence_sha256"], "^[0-9a-f]{64}$")
          || rules == null || rules.Count == 0 || rules.Count > 16
          || rules.Any(rule => !""" + _rejection_rule_condition("rule", '(int)policy["version"]') + """)) { return result; }
        var matched = (JObject)rejected.DeepClone();
        if ((int?)rejected["version"] == 2) {
          matched.Remove("version"); matched.Remove("operation"); matched.Remove("observation_sha256");
          if (!""" + _rejection_rule_condition("matched", "2") + """ || rejected.Properties().Count() != 7
            || (string)rejected["operation"] != context.Operation.Id) { return result; }
          var parts = new JArray(context.Operation.Id, rejected["status_code"]);
          foreach (var errorField in new[] { "error_type", "error_code", "error_param" }) {
            parts.Add(rejected[errorField]["state"]); parts.Add(rejected[errorField]["value"] ?? JValue.CreateNull());
          }
          using (var digest = System.Security.Cryptography.SHA256.Create()) {
            var expectedDigest = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(parts.ToString(Newtonsoft.Json.Formatting.None))))
              .Replace("-", "").ToLowerInvariant();
            if ((string)rejected["observation_sha256"] != expectedDigest) { return result; }
          }
          if ((int)policy["version"] == 1) {
            if (new[] { "error_type", "error_code" }.Any(errorField => (string)rejected[errorField]["state"] != "value")) { return result; }
            matched = new JObject(new JProperty("status_code", rejected["status_code"]),
              new JProperty("error_type", rejected["error_type"]["value"]), new JProperty("error_code", rejected["error_code"]["value"]));
            rejected = matched;
          }
        } else if ((int)policy["version"] != 1) { return result; }
        if (((int)policy["version"] == 2 && (string)policy["operation"] != context.Operation.Id)
          || !""" + _rejection_rule_condition("matched", '(int)policy["version"]') + """
          || !rules.Any(rule => JToken.DeepEquals(rule, matched))) { return result; }
        var verified = policy["verified_at"].Type == JTokenType.Date
          ? new DateTimeOffset(policy["verified_at"].Value<DateTime>()) : DateTimeOffset.Parse((string)policy["verified_at"]);
        if (verified > new DateTimeOffset(receipt["identity"]["admitted_at"].Value<DateTime>())) { return result; }
        rejectionEvidence = (JObject)rejected.DeepClone();
        rejectionEvidence["policy_evidence_sha256"] = policy["evidence_sha256"];
        foreach (var field in new[] { "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "tokens_consumed" }) { usage[field] = 0L; }
      } else if ((bool?)usage["usage_complete"] != true || (bool?)usage["estimated"] != false
        || (usage["ingest_error"] != null && usage["ingest_error"].Type != JTokenType.Null)) { return result; }
      var mode = (string)state["budget_mode"] ?? "tokens";
      if (mode != "tokens" && mode != "usd" || ((string)attempt["budget_mode"] ?? "tokens") != mode) { return result; }
      decimal cost = 0m, priorUsd = 0m;
      if (mode == "usd") {
        var price = attempt["price_snapshot"] as JObject;
        if (price == null || (string)price["model_id"] != (string)receipt["identity"]["model_id"]
          || !System.Text.RegularExpressions.Regex.IsMatch((string)price["price_sha256"] ?? "", "^[0-9a-f]{64}$")
          || (int?)price["version"] != 1 || price["price"] == null
          || price["price"].Type == JTokenType.Null
          || attempt["reserved_usd"] == null || attempt["reserved_usd"].Type == JTokenType.Null) { return result; }
        if (rejection) {
          cost = 0m;
        } else {
          var schedules = price["rate_schedules"] as JArray;
          if (schedules == null || schedules.Count < 1 || schedules.Count > 32) { return result; }
          long billedInput = checked((long)usage["input_tokens"] + (long)usage["cached_tokens"]);
          long nextMinimum = 0;
          JObject selectedTier = null;
          for (int index = 0; index < schedules.Count; index++) {
            var tier = schedules[index] as JObject;
            var rateFields = new[] { "input_rate_units", "cached_rate_units", "cache_write_rate_units", "output_rate_units" };
            if (tier == null || tier.Properties().Any(property => !new[] { "min_input_tokens", "max_input_tokens" }.Concat(rateFields).Contains(property.Name))
              || tier.Properties().Count() != 6 || tier["min_input_tokens"]?.Type != JTokenType.Integer
              || (long)tier["min_input_tokens"] != nextMinimum
              || rateFields.Any(field => tier[field]?.Type != JTokenType.Integer || (long)tier[field] < 0)) { return result; }
            if (tier["max_input_tokens"] == null || tier["max_input_tokens"].Type == JTokenType.Null) {
              if (index != schedules.Count - 1) { return result; }
              if (billedInput >= nextMinimum) { selectedTier = tier; }
              nextMinimum = -1;
            } else {
              if (tier["max_input_tokens"].Type != JTokenType.Integer
                || (long)tier["max_input_tokens"] < nextMinimum || nextMinimum < 0) { return result; }
              if (billedInput >= nextMinimum && billedInput <= (long)tier["max_input_tokens"]) { selectedTier = tier; }
              nextMinimum = checked((long)tier["max_input_tokens"] + 1L);
              if (index == schedules.Count - 1) { return result; }
            }
          }
          if (nextMinimum != -1 || selectedTier == null) { return result; }
          decimal cacheReads = (long)usage["cached_tokens"] - (long)usage["cache_write_tokens"];
          cost = ((decimal)(long)usage["input_tokens"] * (decimal)selectedTier["input_rate_units"]
            + cacheReads * (decimal)selectedTier["cached_rate_units"]
            + (decimal)(long)usage["cache_write_tokens"] * (decimal)selectedTier["cache_write_rate_units"]
            + (decimal)(long)usage["output_tokens"] * (decimal)selectedTier["output_rate_units"])
            / 100000000000000m;
        }
        var prior = attempt["actual_usd"];
        if (prior != null && prior.Type != JTokenType.Null) {
          if (prior.Type == JTokenType.String) {
            var number = JToken.Parse((string)prior);
            if (number.Type != JTokenType.Integer && number.Type != JTokenType.Float) { return result; }
            priorUsd = JsonConvert.DeserializeObject<decimal>((string)prior);
          } else if (prior.Type == JTokenType.Float || prior.Type == JTokenType.Integer) {
            priorUsd = prior.Value<decimal>();
          } else { return result; }
          if (priorUsd < 0m) { return result; }
        }
      }
      var fields = new[] { "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "tokens_consumed" };
      var tokens = fields.Select(field => usage[field]).ToArray();
      var counters = new[] { state["confirmed_tokens"], state["active_reserved_tokens"], state["upper_bound_tokens"], state["version"], attempt["reserved_tokens"], attempt["evidence_revision"] }.Concat(tokens);
      if (counters.Any(value => value == null || value.Type != JTokenType.Integer || (long)value < 0)) { return result; }
      long actual = (long)usage["tokens_consumed"];
      if ((long)usage["cache_write_tokens"] > (long)usage["cached_tokens"]
        || checked((long)usage["input_tokens"] + (long)usage["cached_tokens"] + (long)usage["output_tokens"]) != actual) { return result; }
      var retained = attempt["completion_evidence"] as JObject;
      if (rejection && (string)attempt["state"] == "released"
        && attempt["resolution_evidence"] is JObject && (int?)attempt["evidence_rank"] == 2) {
        result["error"] = ""; result["already_settled"] = true; return result;
      }
      if (rejection && (string)attempt["state"] == "settled") {
        if ((int?)attempt["evidence_rank"] == 3) {
          result["error"] = ""; result["already_settled"] = true; return result;
        }
        if ((long?)attempt["actual_tokens"] != 0L) { result["error"] = "budget_completion_conflict"; return result; }
        var priorRejection = attempt["rejection_evidence"] as JObject;
        if (priorRejection != null) {
          if (!JToken.DeepEquals(priorRejection["rejection"], rejectionEvidence)) {
            result["error"] = "budget_completion_conflict"; return result;
          }
          result["error"] = ""; result["already_settled"] = true; return result;
        }
      }
      if (retained != null) {
        if ((string)attempt["state"] != "settled" || (int?)attempt["evidence_rank"] != 3
          || !JToken.DeepEquals(attempt["actual_tokens"], retained["total_tokens"])) { return result; }
        if (mode == "usd" && (attempt["actual_usd"] == null || priorUsd != cost)) {
          result["error"] = "budget_completion_conflict"; return result;
        }
        var retainedFields = new[] { "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "total_tokens" };
        for (int index = 0; index < fields.Length; index++) {
          if (!JToken.DeepEquals(retained[retainedFields[index]], usage[fields[index]])) {
            result["error"] = "budget_completion_conflict"; return result;
          }
        }
        result["error"] = ""; result["already_settled"] = true; return result;
      }
      var stage = (string)attempt["state"];
      if (!new[] { "reserved", "dispatching", "uncertain", "upper_bound", "cancelled", "settled", "released" }.Contains(stage)) { return result; }
      if (stage == "released" && (!(attempt["resolution_evidence"] is JObject)
        || (int?)attempt["evidence_rank"] != 2 || attempt["actual_tokens"]?.Type != JTokenType.Null)) { return result; }
      long previous = 0;
      if (stage == "settled") {
        if (attempt["actual_tokens"]?.Type != JTokenType.Integer || (long)attempt["actual_tokens"] < 0) { return result; }
        previous = (long)attempt["actual_tokens"];
        if (mode == "usd" && (int?)attempt["evidence_rank"] == 3 && priorUsd != cost) {
          result["error"] = "budget_completion_conflict"; return result;
        }
        if (((int?)attempt["evidence_rank"] ?? 3) == 3 && previous != actual) {
          result["error"] = "budget_completion_conflict"; return result;
        }
      }
      long reserved = (long)attempt["reserved_tokens"];
      long active = new[] { "reserved", "dispatching", "uncertain" }.Contains(stage) ? reserved : 0L;
      long upper = stage == "upper_bound" ? reserved : 0L;
      if (mode == "tokens") {
        state["confirmed_tokens"] = checked((long)state["confirmed_tokens"] + actual - previous);
        state["active_reserved_tokens"] = checked((long)state["active_reserved_tokens"] - active);
        state["upper_bound_tokens"] = checked((long)state["upper_bound_tokens"] - upper);
      } else {
        if (state["usd_limit"] == null || state["usd_limit"].Type == JTokenType.Null) { return result; }
        var names = new[] { "confirmed_usd", "active_reserved_usd", "upper_bound_usd", "usd_limit" };
        var amounts = new decimal[names.Length];
        for (int index = 0; index < names.Length; index++) {
          var token = state[names[index]];
          if (token == null) { amounts[index] = 0m; }
          else if (token.Type == JTokenType.String) {
            var number = JToken.Parse((string)token);
            if (number.Type != JTokenType.Integer && number.Type != JTokenType.Float) { return result; }
            amounts[index] = JsonConvert.DeserializeObject<decimal>((string)token);
          } else if (token.Type == JTokenType.Integer || token.Type == JTokenType.Float) {
            amounts[index] = token.Value<decimal>();
          } else { return result; }
          if (amounts[index] < 0m) { return result; }
        }
        var held = attempt["reserved_usd"];
        if (held.Type == JTokenType.String) { held = JToken.Parse((string)held); }
        if (held.Type != JTokenType.Integer && held.Type != JTokenType.Float) { return result; }
        decimal heldUsd = attempt["reserved_usd"].Type == JTokenType.String
          ? JsonConvert.DeserializeObject<decimal>((string)attempt["reserved_usd"])
          : attempt["reserved_usd"].Value<decimal>();
        if (heldUsd < 0m) { return result; }
        decimal activeUsd = new[] { "reserved", "dispatching", "uncertain" }.Contains(stage) ? heldUsd : 0m;
        decimal upperUsd = stage == "upper_bound" ? heldUsd : 0m;
        amounts[0] = checked(amounts[0] + cost - priorUsd);
        amounts[1] = checked(amounts[1] - activeUsd);
        amounts[2] = checked(amounts[2] - upperUsd);
        for (int index = 0; index < 3; index++) {
          if (amounts[index] < 0m) { return result; }
          state[names[index]] = JsonConvert.SerializeObject(amounts[index]);
        }
      }
      if (new[] { "confirmed_tokens", "active_reserved_tokens", "upper_bound_tokens" }
        .Any(field => (long)state[field] < 0)) { return result; }
      state["version"] = checked((long)state["version"] + 1L);
      var now = DateTime.UtcNow;
      var updated = new[] { new DateTimeOffset(now), new DateTimeOffset(state["updated_at"].Value<DateTime>()), new DateTimeOffset(attempt["updated_at"].Value<DateTime>()) }.Max();
      state["updated_at"] = updated.ToString("o");
      var evidence = new JObject(new JProperty("source", "apim"), new JProperty("observed_at", now.ToString("o")),
        new JProperty("input_tokens", usage["input_tokens"]), new JProperty("cached_tokens", usage["cached_tokens"]),
        new JProperty("cache_write_tokens", usage["cache_write_tokens"]), new JProperty("output_tokens", usage["output_tokens"]),
        new JProperty("total_tokens", actual));
      if (rejection) { evidence["rejection"] = rejectionEvidence; }
      string digestValue;
      using (var digest = System.Security.Cryptography.SHA256.Create()) {
        digestValue = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(new JArray(tokens).ToString(Newtonsoft.Json.Formatting.None))))
          .Replace("-", "").ToLowerInvariant();
      }
      attempt["state"] = "settled"; attempt["actual_tokens"] = actual;
      if (mode == "usd") { attempt["actual_usd"] = JsonConvert.SerializeObject(cost); }
      attempt["evidence_key"] = (rejection ? "rejection:" : "completion:") + digestValue; attempt["evidence_rank"] = rejection ? 2 : 3;
      attempt["evidence_revision"] = Math.Max(1L, (long)attempt["evidence_revision"]);
      attempt[rejection ? "rejection_evidence" : "completion_evidence"] = evidence; attempt["updated_at"] = state["updated_at"];
      result["error"] = ""; result["already_settled"] = false;
      result["summary"] = state; result["attempt"] = attempt;
      result["etag"] = etag; result["attempt_etag"] = attemptEtag;
      return result;
    } catch { return result; }
}"""


def _variable(parent: ET.Element, name: str, expression: str) -> ET.Element:
    return ET.SubElement(parent, "set-variable", {"name": name, "value": expression})


def _header(parent: ET.Element, name: str, value: str) -> None:
    ET.SubElement(
        ET.SubElement(
            parent,
            "set-header",
            {
                "name": name,
                "exists-action": "override",
            },
        ),
        "value",
    ).text = value


def _send(
    parent: ET.Element,
    response: str,
    method: str,
    address: str,
    *,
    body: str | None = None,
    etag: str | None = None,
    batch: bool = False,
) -> ET.Element:
    node = ET.SubElement(
        parent,
        "send-request",
        {
            "mode": "new",
            "response-variable-name": response,
            "timeout": "2",
            "ignore-error": "true",
        },
    )
    ET.SubElement(node, "set-url").text = address
    ET.SubElement(node, "set-method").text = method
    _header(node, "x-ms-version", "2020-12-06")
    _header(node, "DataServiceVersion", "3.0;")
    _header(node, "MaxDataServiceVersion", "3.0;NetFx")
    _header(node, "Accept", "application/json;odata=nometadata")
    if body is not None:
        _header(
            node,
            "Content-Type",
            '@("multipart/mixed; boundary=" + (string)context.Variables["executionBoundary"])'
            if batch
            else "application/json",
        )
        _header(node, "Prefer", "return-no-content")
        if etag is not None:
            _header(node, "If-Match", etag)
        ET.SubElement(node, "set-body").text = body
    ET.SubElement(
        node, "authentication-managed-identity", {"resource": "https://storage.azure.com/"}
    )
    return node


def _usd_price_read(parent: ET.Element) -> None:
    branch = ET.SubElement(
        ET.SubElement(parent, "choose"),
        "when",
        {
            "condition": """@{
      if (context.Variables.ContainsKey("executionUsdReservation")) { return false; }
      var response = context.Variables.GetValueOrDefault<IResponse>("executionSummaryRead", null);
      if (response == null || response.StatusCode != 200) { return false; }
      try {
        var entity = response.Body.As<JObject>(preserveContent: true);
        var summary = JObject.Parse((string)entity["Document"]);
        return (string)summary["budget_mode"] == "usd";
      } catch { return false; }
    }""",
        },
    )
    _send(
      branch,
      "executionPriceProjectionRead",
      "GET",
      """@{
      var plan = (JObject)context.Variables["executionPlan"];
      var model = Guid.Parse((string)plan["model_id"]).ToString();
      var partition = Uri.EscapeDataString("budget-price|" + model);
      return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey=\\u0027"
        + partition + "\\u0027,RowKey=\\u0027P\\u0027)";
    }""",
    )
    _variable(branch, "executionPriceProjection", USD_PRICE_PROJECTION)
    _variable(branch, "executionUsdReservation", USD_RESERVATION)


def _cancel_preparing(parent: ET.Element) -> None:
    branch = ET.SubElement(ET.SubElement(parent, "choose"), "when", {"condition":
        '@(context.Variables.ContainsKey("executionDispatch") && context.Variables.ContainsKey("executionCoordinatorPartition"))'})
    _variable(branch, "executionCancelUrl", '''@{
      return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey=\\u0027" + Uri.EscapeDataString(
        ((string)context.Variables["executionCoordinatorPartition"]).Replace("\\u0027", "\\u0027\\u0027")) + "\\u0027,RowKey=\\u0027" +
        Uri.EscapeDataString("D|" + (string)((JObject)context.Variables["executionPlan"])["attempt_key"]) + "\\u0027)";
    }''')
    _send(branch, "executionCancelRead", "GET", '@((string)context.Variables["executionCancelUrl"])')
    _variable(branch, "executionCancelEtag", '''@{
      try {
        var response = context.Variables.GetValueOrDefault<IResponse>("executionCancelRead", null);
        if (response == null || response.StatusCode != 200) { return ""; }
        var entity = response.Body.As<JObject>(preserveContent: true);
        var stored = JObject.Parse((string)entity["Document"]);
        var owner = JObject.Parse(((JObject)context.Variables["executionDispatch"]).ToString(Newtonsoft.Json.Formatting.None));
        if ((string)stored["state"] != "preparing" || !JToken.DeepEquals(stored, owner)) { return ""; }
        var etag = response.Headers.GetValueOrDefault("ETag", (string)entity["odata.etag"] ?? "");
        return string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.Contains("\\r") || etag.Contains("\\n") ? "" : etag;
      } catch { return ""; }
    }''')
    confirmed = ET.SubElement(ET.SubElement(branch, "choose"), "when", {"condition":
        '@(!string.IsNullOrEmpty((string)context.Variables["executionCancelEtag"]))'})
    _send(confirmed, "executionCancelWrite", "PUT", '@((string)context.Variables["executionCancelUrl"])',
          etag='@((string)context.Variables["executionCancelEtag"])', body='''@{
      var plan = (JObject)((JObject)context.Variables["executionDispatch"]).DeepClone();
      plan["state"] = "cancelled"; plan["updated_at"] = DateTime.UtcNow.ToString("o");
      return new JObject(new JProperty("PartitionKey", (string)context.Variables["executionCoordinatorPartition"]),
        new JProperty("RecoveryPending", true),
        new JProperty("RowKey", "D|" + (string)((JObject)context.Variables["executionPlan"])["attempt_key"]),
        new JProperty("Document", plan.ToString(Newtonsoft.Json.Formatting.None))).ToString(Newtonsoft.Json.Formatting.None);
    }''')


def _deny(
    parent: ET.Element, condition: str, code: str, *, cancel_preparing: bool = False,
) -> None:
    branch = ET.SubElement(ET.SubElement(parent, "choose"), "when", {"condition": condition})
    _variable(branch, "executionError", code)
    _variable(
        branch,
        "executionStatus",
        '''@{
      var code = (string)context.Variables["executionError"];
      if (code == "budget_temporarily_reserved") { return 429; }
      if (new[] { "budget_exhausted", "budget_consumer_disabled", "budget_delegation_invalid" }.Contains(code)) { return 403; }
      if (new[] { "budget_request_bound_exceeds_balance", "budget_output_limit_invalid", "budget_request_invalid" }.Contains(code)) { return 400; }
      if (new[] { "budget_request_analysis_failed", "budget_plan_failed" }.Contains(code)) { return 500; }
      return 503;
    }''',
    )
    _variable(
        branch,
        "executionDecision",
        """@{
      var prepared = context.Variables.GetValueOrDefault<JObject>("executionReservation", new JObject());
      var snapshot = prepared["snapshot"] as JObject ?? new JObject();
      var plan = context.Variables.GetValueOrDefault<JObject>("executionPlan", new JObject());
      var receipt = context.Variables.GetValueOrDefault<JObject>("executionReceipt", new JObject());
      return new JObject(new JProperty("code", (string)context.Variables["executionError"]), new JProperty("error_path", plan["error_path"]),
        new JProperty("limit", snapshot["token_limit"]), new JProperty("confirmed", snapshot["confirmed_tokens"]),
        new JProperty("active_reserved", snapshot["active_reserved_tokens"]), new JProperty("upper_bound", snapshot["upper_bound_tokens"]),
        new JProperty("request_bound", receipt["reserved_tokens"]), new JProperty("algorithm", plan["algorithm"]),
        new JProperty("ledger_version", snapshot["version"]), new JProperty("ledger_updated_at", snapshot["updated_at"]));
    }""",
    )
    event = ET.SubElement(
        branch,
        "log-to-eventhub",
        {
            "logger-id": "__LOGGER_ID__",
            "partition-key": '@(context.Variables.GetValueOrDefault<string>("telemetryUsageId", context.RequestId.ToString()))',
        },
    )
    event.text = """@{
      bool known = context.Variables.ContainsKey("telemetryUsageId");
      var id = context.Variables.GetValueOrDefault<string>("telemetryUsageId", context.RequestId.ToString());
      var status = (int)context.Variables["executionStatus"];
      var eventData = new JObject(new JProperty("id", id), new JProperty("event_schema_version", known ? 2 : 1),
        new JProperty("usage_id", known ? id : null), new JProperty("usage_event_source", "apim"),
        new JProperty("request_id", context.Variables.GetValueOrDefault<string>("callerRequestId", context.RequestId.ToString())),
        new JProperty("correlation_id", context.RequestId.ToString()), new JProperty("ts", DateTime.UtcNow.ToString("o")),
        new JProperty("turn_index", 1), new JProperty("latency_ms", (long)(DateTime.UtcNow - context.Variables.GetValueOrDefault<DateTime>("requestStarted", context.Variables.GetValueOrDefault<DateTime>("executionFenceStarted", DateTime.UtcNow))).TotalMilliseconds), new JProperty("status", status.ToString()),
        new JProperty("status_code", status), new JProperty("estimated", true),
        new JProperty("ingest_error", "budget_pre_dispatch_denial"), new JProperty("ingest_source", "eventhub"),
        new JProperty("budget_admission", (string)context.Variables["executionError"]),
        new JProperty("error_message", ((JObject)context.Variables["executionDecision"]).ToString(Newtonsoft.Json.Formatting.None)),
        new JProperty("provider", context.Variables.GetValueOrDefault<string>("providerName", "unattributed")),
        new JProperty("model", context.Variables.GetValueOrDefault<string>("telemetryModel", "unattributed")),
        new JProperty("model_id", context.Variables.GetValueOrDefault<string>("telemetryModelId", "unattributed")),
        new JProperty("user", context.Variables.GetValueOrDefault<string>("telemetryUser", "unattributed")),
        new JProperty("user_id", context.Variables.GetValueOrDefault<string>("telemetryUserId", "unattributed")),
        new JProperty("runtime", context.Variables.GetValueOrDefault<string>("telemetryRuntime", "unattributed")));
      var fields = new[] { "organization", "organization_id", "department", "department_id", "project", "project_id", "agent", "agent_id", "workflow", "run_id", "request_source" };
      var variables = new[] { "telemetryOrganization", "telemetryOrganizationId", "telemetryDepartment", "telemetryDepartmentId", "telemetryProject", "telemetryProjectId", "telemetryAgent", "telemetryAgentId", "telemetryWorkflow", "telemetryRunId", "telemetryRequestSource" };
      for (int index = 0; index < fields.Length; index++) {
        eventData[fields[index]] = context.Variables.GetValueOrDefault<string>(variables[index], "unattributed");
      }
      eventData["team"] = eventData["department"];
      if (known) {
        eventData["gateway_profile_id"] = "__GATEWAY_PROFILE_ID__";
        eventData["native_gateway_id"] = context.Deployment.Gateway.Id;
        eventData["gateway_is_managed"] = context.Deployment.Gateway.IsManaged;
        eventData["apim_service_id"] = context.Deployment.ServiceId;
        eventData["apim_region"] = string.IsNullOrWhiteSpace(context.Deployment.Region) ? "unattributed" : context.Deployment.Region;
        eventData["api_revision"] = context.Api.Revision;
      }
      var subscription = context.Variables.GetValueOrDefault<string>("telemetryApimSubscriptionId", "");
      if (!string.IsNullOrEmpty(subscription)) { eventData["apim_subscription_id"] = subscription; }
      return eventData.ToString(Newtonsoft.Json.Formatting.None);
    }"""
    if cancel_preparing:
      _cancel_preparing(branch)
    response = ET.SubElement(branch, "return-response")
    ET.SubElement(
        response,
        "set-status",
        {
            "code": '@((int)context.Variables["executionStatus"])',
            "reason": "Budget Admission Rejected",
        },
    )
    _header(response, "Content-Type", "application/json")
    _header(response, "x-correlation-id", "@(context.RequestId.ToString())")
    ET.SubElement(response, "set-body").text = """@{
      var code = (string)context.Variables["executionError"];
      return new JObject(new JProperty("type", "error"), new JProperty("error",
        new JObject(new JProperty("code", code), new JProperty("type", "budget_error"),
          new JProperty("message", code), new JProperty("request_id", context.RequestId.ToString()),
          new JProperty("budget", ((JObject)context.Variables["executionDecision"]).DeepClone()))))
        .ToString(Newtonsoft.Json.Formatting.None);
    }"""


def _native_count(parent: ET.Element) -> None:
    _variable(parent, "executionCountPlan", NATIVE_COUNT_PLAN)
    enabled = ET.SubElement(
        ET.SubElement(parent, "choose"),
        "when",
        {"condition": '@((bool)((JObject)context.Variables["executionCountPlan"])["enabled"])'},
    )
    managed = ET.SubElement(
        ET.SubElement(enabled, "choose"),
        "when",
        {
            "condition": '@((string)context.Variables["selectedBudgetCounterAuth"] == "managed_identity")'
        },
    )
    ET.SubElement(
        managed,
        "authentication-managed-identity",
        {
            "resource": '@((string)context.Variables["selectedBudgetCounterAudience"])',
            "output-token-variable-name": "executionCounterToken",
            "ignore-error": "true",
        },
    )
    choices = ET.SubElement(enabled, "choose")
    for mode, header in (
        ("managed_identity", "Authorization"),
        ("named_value_bearer", "Authorization"),
        ("named_value_api_key", "api-key"),
        ("named_value_api_key", "x-api-key"),
        ("none", ""),
    ):
        condition = f'(string)context.Variables["selectedBudgetCounterAuth"] == "{mode}"'
        if mode == "named_value_api_key":
            condition += (
                f' && (string)context.Variables["selectedBudgetCounterHeader"] == "{header}"'
            )
        if mode == "managed_identity":
            condition += ' && !string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("executionCounterToken", ""))'
        branch = ET.SubElement(choices, "when", {"condition": "@(" + condition + ")"})
        request = ET.SubElement(
            branch,
            "send-request",
            {
                "mode": "new",
                "response-variable-name": "executionCountResponse",
                "timeout": "2",
                "ignore-error": "true",
            },
        )
        ET.SubElement(
            request, "set-url"
        ).text = '@((string)((JObject)context.Variables["executionCountPlan"])["endpoint"])'
        ET.SubElement(request, "set-method").text = "POST"
        _header(request, "Content-Type", "application/json")
        _header(request, "Accept", "application/json")
        _header(request, "anthropic-version", "2023-06-01")
        if header:
            variable = (
                "executionCounterToken"
                if mode == "managed_identity"
                else "selectedBudgetCounterKey"
            )
            prefix = '"Bearer " + ' if header == "Authorization" else ""
            _header(request, header, f'@({prefix}(string)context.Variables["{variable}"])')
        ET.SubElement(
            request, "set-body"
        ).text = '@(((JObject)context.Variables["executionCountPlan"])["payload"].ToString(Newtonsoft.Json.Formatting.None))'
    _variable(enabled, "executionPlan", NATIVE_COUNT_APPLY)


def _admission() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root, "when", {"condition": '@((bool)context.Variables["isInferenceOperation"])'}
    )
    _variable(branch, "executionRequestAnalysis", REQUEST_ANALYSIS)
    _variable(branch, "executionPlan", PLAN)
    _variable(
        branch,
        "executionModelId",
        """@{
      var receipts = ((JObject)context.Variables["executionPlan"])["receipts"] as JArray;
      return receipts != null && receipts.Count > 0 ? (string)receipts[0]["identity"]["model_id"] : "unattributed";
    }""",
    )
    _deny(
        branch,
        '@(!string.IsNullOrEmpty((string)((JObject)context.Variables["executionPlan"])["error"]))',
        '@((string)((JObject)context.Variables["executionPlan"])["error"])',
    )
    normalized = ET.SubElement(
      ET.SubElement(branch, "choose"), "when",
      {"condition": '@(((JObject)context.Variables["executionPlan"])["normalized_body"]?.Type == JTokenType.String)'},
    )
    ET.SubElement(normalized, "set-body").text = NORMALIZED_REQUEST_BODY
    branch = ET.SubElement(
      ET.SubElement(branch, "choose"), "when",
      {"condition": '@((bool?)((JObject)context.Variables["executionPlan"])["bypass"] != true)'},
    )
    _native_count(branch)
    _variable(
        branch,
        "executionReceipt",
        '@((JObject)((JArray)((JObject)context.Variables["executionPlan"])["receipts"])[0])',
    )
    _variable(branch, "executionPartition", SCOPE_PARTITION)
    _variable(
        branch,
        "executionCoordinatorPartition",
        '@((string)context.Variables["executionPartition"])',
    )
    _variable(
        branch,
        "executionDispatch",
        """@{
      var plan = (JObject)context.Variables["executionPlan"];
      var now = (DateTime)context.Variables["requestStarted"];
      var counting = new JObject();
      foreach (var field in new[] { "algorithm", "count_assurance", "body_bytes_estimate", "contract_sha256", "required_features", "input_ceiling", "output_limit", "output_samples", "input_estimate", "count_status", "count_fingerprint" }) {
        if (plan[field] != null) { counting[field] = plan[field].DeepClone(); }
      }
      return new JObject(new JProperty("owner", Guid.NewGuid().ToString()),
        new JProperty("caller_request_id", (string)context.Variables["callerRequestId"]),
        new JProperty("recovery_complete", false),
        new JProperty("counting_basis", counting),
        new JProperty("receipts", plan["receipts"].DeepClone()), new JProperty("state", "preparing"),
        new JProperty("expires_at", now.AddSeconds(45).ToString("o")), new JProperty("updated_at", now.ToString("o")));
    }""",
    )
    initial_dispatch = '@(((JObject)context.Variables["executionDispatch"]).DeepClone())'
    _variable(branch, "executionDispatchInitial", initial_dispatch)
    _variable(branch, "executionUsdEvidence", "@(new JArray())")
    _send(
        branch,
        "executionDispatchWrite",
        "POST",
        "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__",
        body="""@{
      return new JObject(new JProperty("PartitionKey", (string)context.Variables["executionCoordinatorPartition"]),
        new JProperty("RecoveryPending", true),
        new JProperty("RowKey", "D|" + (string)((JObject)context.Variables["executionPlan"])["attempt_key"]),
        new JProperty("Document", ((JObject)context.Variables["executionDispatch"]).ToString(Newtonsoft.Json.Formatting.None)))
        .ToString(Newtonsoft.Json.Formatting.None);
    }""",
    )
    _deny(
        branch,
        '@(context.Variables.GetValueOrDefault<IResponse>("executionDispatchWrite", null) == null || ((IResponse)context.Variables["executionDispatchWrite"]).StatusCode != 204)',
        "budget_ledger_unavailable",
      cancel_preparing=True,
    )
    for index in range(2):
        scope_branch = ET.SubElement(
            ET.SubElement(branch, "choose"),
            "when",
            {
                "condition": f'@(((JArray)((JObject)context.Variables["executionPlan"])["receipts"]).Count > {index})',
            },
        )
        _variable(
            scope_branch,
            "executionReceipt",
            f'@((JObject)((JArray)((JObject)context.Variables["executionPlan"])["receipts"])[{index}])',
        )
        _variable(scope_branch, "executionPartition", SCOPE_PARTITION)
        _variable(scope_branch, "executionWriteResult", "unavailable")
        retry = ET.SubElement(
            scope_branch,
            "retry",
            {
            "condition": '@((string)context.Variables["executionWriteResult"] == "conflict" || (string)context.Variables["executionWriteResult"] == "pending")',
                "count": "2",
            "interval": "1",
                "first-fast-retry": "true",
            },
        )
        _send(
            retry,
            "executionSummaryRead",
            "GET",
            """@{
          return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey=\\u0027" + Uri.EscapeDataString(
            ((string)context.Variables["executionPartition"]).Replace("\\u0027", "\\u0027\\u0027")) + "\\u0027,RowKey=\\u0027S\\u0027)";
        }""",
        )
        _usd_price_read(retry)
        _variable(retry, "executionReservation", RESERVATION)
        _deny(
            retry,
          '@(!string.IsNullOrEmpty((string)((JObject)context.Variables["executionReservation"])["error"]) && (string)((JObject)context.Variables["executionReservation"])["error"] != "budget_temporarily_reserved")',
            '@((string)((JObject)context.Variables["executionReservation"])["error"])',
            cancel_preparing=True,
        )
        _variable(retry, "executionWriteResult", "pending")
        available = ET.SubElement(ET.SubElement(retry, "choose"), "when", {"condition":
          '@((string)((JObject)context.Variables["executionReservation"])["error"] == "")'})
        _variable(available, "executionBoundary", '@("batch_" + Guid.NewGuid().ToString("N"))')
        _send(
          available,
            "executionWrite",
            "POST",
            "__LEDGER_TABLE_ENDPOINT__/$batch",
            body=BATCH,
            batch=True,
        )
        _variable(available, "executionWriteResult", BATCH_RESULT)
        evidence = ET.SubElement(
            ET.SubElement(available, "choose"),
            "when",
            {"condition": '@((string)context.Variables["executionWriteResult"] == "ok" && (string)((JObject)context.Variables["executionReservation"])["attempt"]["budget_mode"] == "usd")'},
        )
        _variable(
            evidence,
            "executionUsdEvidenceAppend",
            """@{
          var evidence = (JArray)context.Variables["executionUsdEvidence"];
          var attempt = (JObject)((JObject)context.Variables["executionReservation"])["attempt"];
          evidence.Add(attempt.DeepClone());
          return true;
        }""",
        )
        _deny(
            scope_branch,
            '@((string)context.Variables["executionWriteResult"] != "ok")',
          '@((string)context.Variables["executionWriteResult"] == "pending" ? "budget_temporarily_reserved" : "budget_ledger_unavailable")',
            cancel_preparing=True,
        )
    _variable(
        branch,
        "executionDispatchEvidence",
        """@{
      var evidence = (JArray)context.Variables["executionUsdEvidence"];
      if (evidence.Count > 0) {
        var dispatch = (JObject)context.Variables["executionDispatch"];
        var plan = (JObject)context.Variables["executionPlan"];
        foreach (JObject attempt in evidence) {
          foreach (JArray receipts in new[] { dispatch["receipts"], plan["receipts"] }) {
            var receipt = receipts.OfType<JObject>().Single(candidate => JToken.DeepEquals(
              candidate["identity"], attempt["identity"]));
            receipt["budget_mode"] = "usd";
            receipt["reserved_usd"] = attempt["reserved_usd"].DeepClone();
            receipt["price_snapshot"] = attempt["price_snapshot"].DeepClone();
          }
        }
      }
      return true;
    }""",
    )
    _variable(
        branch,
        "executionDispatchUrl",
        """@{
      return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey=\\u0027" + Uri.EscapeDataString(
        ((string)context.Variables["executionCoordinatorPartition"]).Replace("\\u0027", "\\u0027\\u0027")) + "\\u0027,RowKey=\\u0027" +
        Uri.EscapeDataString("D|" + (string)((JObject)context.Variables["executionPlan"])["attempt_key"]) + "\\u0027)";
    }""",
    )
    _send(
        branch,
        "executionDispatchRead",
        "GET",
        '@((string)context.Variables["executionDispatchUrl"])',
    )
    _variable(
        branch,
        "executionDispatchEtag",
        """@{
      try {
        var response = (IResponse)context.Variables["executionDispatchRead"];
        if (response == null || response.StatusCode != 200) { return ""; }
        var entity = response.Body.As<JObject>(preserveContent: true);
        var stored = JObject.Parse((string)entity["Document"]);
        var initial = JObject.Parse(((JObject)context.Variables["executionDispatchInitial"])
          .ToString(Newtonsoft.Json.Formatting.None));
        if (!JToken.DeepEquals(stored, initial) || new DateTimeOffset(stored["expires_at"].Value<DateTime>()) <= DateTimeOffset.UtcNow) { return ""; }
        var etag = response.Headers.GetValueOrDefault("ETag", (string)entity["odata.etag"] ?? "");
        return etag == "*" || etag.Contains("\\r") || etag.Contains("\\n") ? "" : etag;
      } catch { return ""; }
    }""",
    )
    _deny(
        branch,
        '@(string.IsNullOrEmpty((string)context.Variables["executionDispatchEtag"]))',
        "budget_dispatch_unconfirmed",
        cancel_preparing=True,
    )
    _send(
        branch,
        "executionDispatchAuthorized",
        "PUT",
        '@((string)context.Variables["executionDispatchUrl"])',
        etag='@((string)context.Variables["executionDispatchEtag"])',
        body="""@{
      var plan = (JObject)((JObject)context.Variables["executionDispatch"]).DeepClone();
      plan["state"] = "dispatching"; plan["updated_at"] = DateTime.UtcNow.ToString("o");
      return new JObject(new JProperty("PartitionKey", (string)context.Variables["executionCoordinatorPartition"]),
        new JProperty("RecoveryPending", true),
        new JProperty("RowKey", "D|" + (string)((JObject)context.Variables["executionPlan"])["attempt_key"]),
        new JProperty("Document", plan.ToString(Newtonsoft.Json.Formatting.None))).ToString(Newtonsoft.Json.Formatting.None);
    }""",
    )
    _deny(
        branch,
        '@(context.Variables.GetValueOrDefault<IResponse>("executionDispatchAuthorized", null) == null || ((IResponse)context.Variables["executionDispatchAuthorized"]).StatusCode != 204)',
        "budget_dispatch_unconfirmed",
        cancel_preparing=True,
    )
    _variable(
        branch,
        "executionReceiptHeader",
        """@{
      return Convert.ToBase64String(Encoding.UTF8.GetBytes(
        ((JObject)context.Variables["executionPlan"])["receipts"].ToString(Newtonsoft.Json.Formatting.None)));
    }""",
    )
    _variable(branch, "telemetryModelId", '@((string)context.Variables["executionModelId"])')
    for scope_type, status_variable in (
      ("person", "ledgerStatus"), ("application", "applicationAdmission"),
    ):
      _variable(
        branch,
        status_variable,
        f'''@{{
      var receipts = (JArray)((JObject)context.Variables["executionPlan"])["receipts"];
      return receipts.Any(receipt => (string)receipt["identity"]["scope"]["scope_type"] == "{scope_type}")
      ? "ok" : context.Variables.GetValueOrDefault<string>("{status_variable}", "");
    }}''',
      )
    ET.SubElement(branch, "set-body").text = """@{
      var body = context.Request.Body.As<JObject>(preserveContent: true);
      var plan = (JObject)context.Variables["executionPlan"];
      if (!string.IsNullOrEmpty((string)plan["output_field"])) {
        if (context.Operation.Id == "chat-completions") { body.Remove("max_tokens"); body.Remove("max_completion_tokens"); }
        body[(string)plan["output_field"]] = plan["output_limit"];
      }
      return body.ToString(Newtonsoft.Json.Formatting.None);
    }"""
    return root


def _completion() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": '@(context.Variables.ContainsKey("executionReceiptHeader") && (!context.Variables.GetValueOrDefault<bool>("isStreamedResponse", false) || context.Response.Headers.GetValueOrDefault("Content-Type", "").Split(\';\')[0].Trim().ToLowerInvariant() == "application/json"))'
        },
    )
    ready = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@((bool?)((JObject)context.Variables["usagePayload"])["usage_complete"] == true || ((JObject)context.Variables["usagePayload"])["terminal_rejection"] is JObject)'
        },
    )
    scope = ET.SubElement(
      ET.SubElement(ready, "choose"),
      "when",
      {"condition": '@(((JArray)((JObject)context.Variables["executionPlan"])["receipts"]).Count > 0)'},
    )
    _variable(scope, "executionCompletionIndex", "@(0L)")
    loop = ET.SubElement(scope, "retry", {
      "condition": '@((long)context.Variables["executionCompletionIndex"] < Math.Min(2, ((JArray)((JObject)context.Variables["executionPlan"])["receipts"]).Count))',
      "count": "1", "interval": "0", "first-fast-retry": "true",
    })
    _variable(
      loop,
      "executionReceipt",
      '@((JObject)((JArray)((JObject)context.Variables["executionPlan"])["receipts"])[(int)(long)context.Variables["executionCompletionIndex"]])',
    )
    _variable(loop, "executionPartition", SCOPE_PARTITION)
    _variable(loop, "executionWriteResult", "unavailable")
    retry = ET.SubElement(
      loop,
      "retry",
      {
        "condition": '@((string)context.Variables["executionWriteResult"] == "conflict")',
        "count": "2",
        "interval": "0",
        "first-fast-retry": "true",
      },
    )
    for row, response in (("S", "executionSummaryRead"), ("A", "executionAttemptRead")):
      suffix = (
        '"S"'
        if row == "S"
        else '"A|" + (string)((JObject)context.Variables["executionPlan"])["attempt_key"]'
      )
      _send(
        retry,
        response,
        "GET",
        """@{
        return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey=\\u0027" + Uri.EscapeDataString(
        ((string)context.Variables["executionPartition"]).Replace("\\u0027", "\\u0027\\u0027")) + "\\u0027,RowKey=\\u0027" + Uri.EscapeDataString("""
        + suffix
        + """) + "\\u0027)";
      }""",
        )
    _variable(retry, "executionReservation", COMPLETION)
    write = ET.SubElement(
      ET.SubElement(retry, "choose"),
      "when",
      {
        "condition": '@((string)((JObject)context.Variables["executionReservation"])["error"] == "" && (bool?)((JObject)context.Variables["executionReservation"])["already_settled"] == false)'
      },
    )
    _variable(write, "executionBoundary", '@("batch_" + Guid.NewGuid().ToString("N"))')
    _send(
      write,
      "executionWrite",
      "POST",
      "__LEDGER_TABLE_ENDPOINT__/$batch",
      body=BATCH,
      batch=True,
    )
    _variable(write, "executionWriteResult", BATCH_RESULT)
    _variable(loop, "executionCompletionIndex", '@((long)context.Variables["executionCompletionIndex"] + 1L)')
    return root


def _delegation(inbound: ET.Element, trust: BudgetDelegationTrust | None) -> None:
    marker = inbound.find("set-variable[@name='hasEmployeeToken']")
    employee = inbound.find("./choose/when/validate-azure-ad-token")
    if marker is None or employee is None:
        raise PolicyCompilationError("Execution delegation requires canonical employee validation")
    parents = {child: parent for parent in inbound.iter() for child in parent}
    employee_parent = parents[employee]
    original = marker.get("value", "")
    declared = ET.Element("set-variable", {"name": "budgetDelegatedPerson", "value": "@(false)"})
    presented = ET.Element(
        "set-variable",
        {
            "name": "budgetDelegationPresented",
            "value": '@(!string.IsNullOrWhiteSpace(context.Request.Headers.GetValueOrDefault("x-turnstile-delegation-token", "")))',
        },
    )
    inbound.insert(list(inbound).index(marker), declared)
    inbound.insert(list(inbound).index(marker), presented)
    choose = ET.Element("choose")
    branch = ET.SubElement(
        choose, "when", {"condition": '@((bool)context.Variables["budgetDelegationPresented"])'}
    )
    if trust is None:
        _deny(branch, "@(true)", "budget_delegation_unavailable")
    else:
        _deny(
            branch,
            '@(context.Subscription == null || !string.IsNullOrWhiteSpace(context.Request.Headers.GetValueOrDefault("Authorization", "")))',
            "budget_delegation_invalid",
        )
        validation = ET.SubElement(
            branch,
            "validate-azure-ad-token",
            {
                "tenant-id": str(trust.tenant_id),
                "header-name": "x-turnstile-delegation-token",
                "output-token-variable-name": "budgetDelegationToken",
                "failed-validation-httpcode": "401",
                "failed-validation-error-message": "A registered backend identity is required",
            },
        )
        clients = ET.SubElement(validation, "client-application-ids")
        for client_id in trust.client_ids:
            ET.SubElement(clients, "application-id").text = str(client_id)
        ET.SubElement(ET.SubElement(validation, "audiences"), "audience").text = trust.audience
        claims = ET.SubElement(
            ET.SubElement(validation, "required-claims"), "claim", {"name": "oid", "match": "any"}
        )
        for object_id in trust.object_ids:
            ET.SubElement(claims, "value").text = str(object_id)
        encoding = ET.SubElement(ET.SubElement(branch, "choose"), "when", {
            "condition": '@(context.Request.Headers.GetValueOrDefault("'
            + DELEGATION_ENCODING_HEADER + '", "") == "percent-v1")',
        })
        for header in DELEGATION_METADATA_HEADERS:
            _header(
                encoding, header,
                '@(Uri.UnescapeDataString(context.Request.Headers.GetValueOrDefault("'
                + header + '", "")))',
            )
        _deny(
            branch,
            """@{
      var person = context.Request.Headers.GetValueOrDefault("x-user-id", "");
      return string.IsNullOrWhiteSpace(person) || person == "unattributed" || person.Length > 255
      || person.Any(character => char.IsControl(character));
    }""",
            "budget_delegation_invalid",
        )
        _variable(branch, "budgetDelegatedPerson", "@(true)")
    inbound.insert(list(inbound).index(marker), choose)
    marker.set(
        "value", '@((bool)context.Variables["budgetDelegatedPerson"] || ' + original[2:-1] + ")"
    )
    index = list(employee_parent).index(employee)
    employee_parent.remove(employee)
    normal = ET.Element("choose")
    ET.SubElement(
        normal, "when", {"condition": '@(!(bool)context.Variables["budgetDelegatedPerson"])'}
    ).append(employee)
    employee_parent.insert(index, normal)
    delegated_fields = {
        "employeeName": "x-user-id",
        "employeeDepartmentId": "x-department-id",
        "employeeDepartmentName": "x-department-name",
        "clientAgentName": "x-agent-name",
        "clientAgentId": "x-agent-id",
    }
    for variable in employee_parent.findall("set-variable"):
        value = variable.get("value", "")
        name = variable.get("name", "")
        if name in delegated_fields:
            value = value.replace(
                "@{",
                '@{\n if ((bool)context.Variables["budgetDelegatedPerson"]) { return context.Request.Headers.GetValueOrDefault("'
                + delegated_fields[name]
                + '", "unattributed"); }',
                1,
            )
        value = value.replace(
            'context.Variables["employeeToken"]',
            'context.Variables[(bool)context.Variables["budgetDelegatedPerson"] ? "budgetDelegationToken" : "employeeToken"]',
        )
        variable.set("value", value)
    for name in (
        "x-org-id", "x-org-name", "x-project-id", "x-project-name",
        "x-user-name", "x-model-id", "x-hive-model", "x-request-source",
    ):
        header_value = employee_parent.find(f"set-header[@name='{name}']/value")
        if header_value is None or header_value.text is None:
            raise PolicyCompilationError("Canonical employee attribution header is missing")
        original_value = header_value.text.strip()
        fallback = original_value[2:-1] if original_value.startswith("@(") else json.dumps(original_value)
        header_value.text = (
            '@((bool)context.Variables["budgetDelegatedPerson"] ? '
            f'context.Request.Headers.GetValueOrDefault("{name}", {fallback}) : {fallback})'
        )
    employee_limiter = employee_parent.find(".//llm-token-limit")
    if employee_limiter is not None:
      limiter_parents = {child: owner for owner in employee_parent.iter() for child in owner}
      limiter_parent = limiter_parents[employee_limiter]
      if limiter_parent.tag == "when" and limiter_parent.get("condition") == TEXT_CONDITION:
        employee_limiter = limiter_parents[limiter_parent]
        limiter_parent = limiter_parents[employee_limiter]
        position = list(limiter_parent).index(employee_limiter)
        limiter_parent.remove(employee_limiter)
        normal_limit = ET.Element("choose")
        ET.SubElement(
            normal_limit,
            "when",
            {"condition": '@(!(bool)context.Variables["budgetDelegatedPerson"])'},
        ).append(employee_limiter)
        limiter_parent.insert(position, normal_limit)
    inbound.append(
        ET.Element(
            "set-header", {"name": "x-turnstile-delegation-token", "exists-action": "delete"}
        )
    )
    inbound.append(ET.Element(
        "set-header", {"name": DELEGATION_ENCODING_HEADER, "exists-action": "delete"},
    ))


def compose_budget_execution_parent(
    source: str,
    canonical_parent: str,
    profiles: Sequence[ImageGenerationProfile] = (),
    *,
    enabled: bool = False,
    delegation: BudgetDelegationTrust | None = None,
) -> str:
    if not enabled:
        return source
    validate_parent_policy(source, canonical_parent, profiles)
    root = parse_policy(source)
    inbound = root.find("inbound")
    assert inbound is not None
    if inbound.find("set-variable[@name='telemetryUsageId']") is None:
        raise PolicyCompilationError(
            "Budget execution requires explicit schema-two Gateway adoption"
        )
    _delegation(inbound, delegation)
    inbound.insert(
        0, ET.Element("set-variable", {"name": "budgetExecutionVersion", "value": "@(2)"})
    )
    inbound.insert(
        0,
        ET.Element(
            "set-header", {"name": "x-turnstile-budget-execution", "exists-action": "delete"}
        ),
    )
    for name in (
        "ledgerConfigured",
        "ledgerEnforced",
        "applicationLedgerConfigured",
        "applicationLedgerEnforced",
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if len(nodes) != 1:
            raise PolicyCompilationError("Budget execution has an ambiguous legacy writer")
        original = nodes[0].attrib["value"]
        nodes[0].set(
          "value",
          '@((string)((JObject)context.Variables["applicationMapState"])["applicationType"]'
          ' == "system" && (' + original[2:-1] + '))'
          if name.startswith("application") else "@(false)",
        )
    for name, partition in (
        ("ledgerUrl", "ledgerPartition"),
        ("applicationLedgerUrl", "applicationLedgerPartition"),
    ):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if len(nodes) != 1:
            raise PolicyCompilationError("Budget execution has an ambiguous permission reader")
        legacy = (
          '(string)((JObject)context.Variables["applicationMapState"])["applicationType"]'
          ' == "system"'
          if name.startswith("application") else "false"
        )
        nodes[0].set(
            "value",
            f'''@{{
          var partition = ((string)context.Variables["{partition}"]).Replace("\\u0027", "\\u0027\\u0027");
          return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__()?$filter=" + Uri.EscapeDataString(
            "PartitionKey eq '" + partition + "'" + ({legacy} ? "" : " and RowKey eq 'M'"));
        }}''',
        )
    inbound.append(_admission())
    outbound = root.find("outbound")
    assert outbound is not None
    first_event = next(child for child in outbound if child.find(".//log-to-eventhub") is not None)
    outbound.insert(list(outbound).index(first_event) + 1, _completion())
    for node in root.findall("./outbound/choose/when/set-variable[@name='usagePayload']"):
        if "failed_request_has_no_usage" in node.get("value", ""):
            node.set("value", COMPLETE_USAGE)
        elif "stream_usage_breakdown_unavailable" in node.get("value", ""):
            parent = next(owner for owner in root.iter() if node in list(owner))
            parent.set(
                "condition",
                '@((bool)context.Variables["isStreamedResponse"] && context.Response.Headers.GetValueOrDefault("Content-Type", "").Split(\';\')[0].Trim().ToLowerInvariant() != "application/json")',
            )
    buffered = root.find("./outbound/choose/otherwise/set-variable[@name='usagePayload']")
    if buffered is None:
      buffered = root.find(
        "./outbound/choose/otherwise/choose/otherwise/set-variable[@name='usagePayload']"
      )
    if buffered is None:
        raise PolicyCompilationError("Budget execution requires an explicit buffered usage reader")
    buffered.set("value", COMPLETE_USAGE)
    for event in root.findall("./outbound//log-to-eventhub") + root.findall(
        "./on-error//log-to-eventhub"
    ):
        expression = event.text or ""
        anchors = (
            'new JProperty("event_schema_version", 2),',
            'new JProperty("event_schema_version", hasGatewayContext ? 2 : 1),',
        )
        found = [value for value in anchors if expression.count(value) == 1]
        if len(found) != 1:
            raise PolicyCompilationError(
                "Budget execution requires canonical schema-two outcome events"
            )
        anchor = found[0]
        expression = expression.replace(
            "@{",
            """@{
      var budgetReceipts = (JArray)(context.Variables.GetValueOrDefault<JObject>("executionPlan", null)?["receipts"] as JArray ?? new JArray()).DeepClone();
""",
            1,
        )
        event.text = expression.replace(
            anchor,
            anchor
            + """
            new JProperty("usd_cost", context.Variables.GetValueOrDefault<JObject>("usagePayload", null)?["usd_cost"] ?? JValue.CreateNull()),
            new JProperty("budget_price_projection", context.Variables.GetValueOrDefault<JObject>("usagePayload", null)?["budget_price_projection"] ?? JValue.CreateNull()),
        new JProperty("budget_execution", budgetReceipts),
        new JProperty("budget_terminal", context.Variables.ContainsKey("executionReceiptHeader")
          && context.Variables.GetValueOrDefault<JObject>("usagePayload", null)?["terminal_rejection"] is JObject
          ? (JToken)new JObject(new JProperty("source", "apim"),
              new JProperty("observed_at", ((JObject)context.Variables["usagePayload"])["terminal_observed_at"]),
              new JProperty("rejection", ((JObject)context.Variables["usagePayload"])["terminal_rejection"]))
          : JValue.CreateNull()),
        new JProperty("usage_complete", context.Variables.ContainsKey("usagePayload")
          && ((bool?)((JObject)context.Variables["usagePayload"])["usage_complete"] ?? false)),""",
        )
    return serialize_policy(root)


def budget_execution_fence(api_ids: Sequence[str]) -> ET.Element:
    if not api_ids or any(not value.strip() or ";" in value or "/" in value for value in api_ids):
        raise PolicyCompilationError("The shared fence requires exact Turnstile API identifiers")
    root = ET.Element("choose")
    allowed = json.dumps(list(dict.fromkeys(api_ids)), separators=(",", ":"))
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": "@(JArray.Parse("
            + json.dumps(allowed)
            + ").Values<string>().Contains(context.Api.Id.Split(';')[0]))"
        },
    )
    _variable(branch, "executionFenceStarted", "@(DateTime.UtcNow)")
    _send(
        branch,
        "budgetExecutionFenceRead",
        "GET",
        """@{
      string service;
      using (var digest = System.Security.Cryptography.SHA256.Create()) {
        service = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(context.Deployment.ServiceId.ToLowerInvariant())))
          .Replace("-", "").ToLowerInvariant();
      }
      return "__LEDGER_TABLE_ENDPOINT__/__LEDGER_TABLE_NAME__(PartitionKey=\\u0027" + Uri.EscapeDataString(
        "budget-protocol|" + service + "|" + context.Api.Id.Split(';')[0]) + "\\u0027,RowKey=\\u0027F\\u0027)";
    }""",
    )
    _variable(
        branch,
        "budgetExecutionFence",
        """@{
      try {
        var response = (IResponse)context.Variables["budgetExecutionFenceRead"];
        if (response == null || response.StatusCode != 200) { return ""; }
        var state = JObject.Parse((string)response.Body.As<JObject>(preserveContent: true)["Document"]);
        if ((int?)state["protocol_version"] != 2 || (string)state["api_id"] != context.Api.Id.Split(';')[0]
          || !string.Equals((string)state["apim_service_id"], context.Deployment.ServiceId, StringComparison.OrdinalIgnoreCase)) { return ""; }
        if ((string)state["mode"] == "paused") {
          var validationSubscription = (string)state["validation_subscription_id"];
          return !string.IsNullOrEmpty((string)state["validation_person_id"])
            && !string.IsNullOrEmpty(validationSubscription) && context.Subscription != null
            && context.Subscription.Id == validationSubscription
            && context.Api.Revision == (string)state["validation_revision"]
            && context.Variables.GetValueOrDefault<int>("budgetExecutionVersion", 0) == 2
            ? state.ToString(Newtonsoft.Json.Formatting.None) : "";
        }
        if ((string)state["mode"] == "prepared") {
          var validationSubscription = (string)state["validation_subscription_id"];
          if (!string.IsNullOrEmpty(validationSubscription) && context.Subscription != null
            && context.Subscription.Id == validationSubscription
            && context.Api.Revision != (string)state["validation_revision"]) { return ""; }
          return state.ToString(Newtonsoft.Json.Formatting.None);
        }
        if ((string)state["mode"] != "active") { return ""; }
        var gateways = state["gateways"] as JObject;
        var revisions = gateways?[context.Deployment.Gateway.Id] as JArray;
        return revisions != null && revisions.Count > 0
          && context.Variables.GetValueOrDefault<int>("budgetExecutionVersion", 0) == 2
          ? state.ToString(Newtonsoft.Json.Formatting.None) : "";
      } catch { return ""; }
    }""",
    )
    _deny(
        branch,
        '@(string.IsNullOrWhiteSpace((string)context.Variables["budgetExecutionFence"]))',
        "budget_protocol_fenced",
    )
    return deepcopy(root)
