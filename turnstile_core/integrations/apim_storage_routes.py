# ruff: noqa: E501

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import time
from typing import Literal, Protocol
from urllib.parse import quote
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import httpx
from pydantic import Field, SecretStr, model_validator

from ..domain.image_profiles import (
    DEFAULT_IMAGE_GENERATION_LIMITS,
    ImageGenerationLimits,
    ImageGenerationProfile,
    create_image_profile,
)
from ..domain.storage_routes import (
    ActiveRouteHead,
    Digest,
    FixedProgramInstallation,
    FixedProgramUpgrade,
    Identifier,
    ModelRouteHead,
    ProgramCapabilityProof,
    ProgramIdentity,
    RouteModel,
    RouteProfile,
    RouteScope,
    canonical_document,
    document_digest,
    text_digest,
)
from .apim_control_plane_contract import PolicyCompilationError
from .apim_policy_components import (
    NATIVE_STREAM_REQUEST,
    component_digest,
    encode_policy_xml,
    expression_token_spans,
    expression_tokens,
    parse_policy,
    policy_document_digest,
    serialize_policy,
    validate_executable_policy,
    validate_parent_policy,
    validate_policy_upload,
)


class FixedProgramReader(Protocol):
    def current_revision(self) -> str | None: ...

    def revision_api_policy(self, revision: str) -> str: ...

    def route_program_operations(self, revision: str) -> Mapping[str, tuple[str, str, str]]: ...


class FixedProgramVerifier:
    def __init__(
        self, installation: FixedProgramInstallation, management: FixedProgramReader,
        subscription_key: SecretStr, client: httpx.Client,
        *, clock: Callable[[], int] = lambda: int(time()),
        expected_current_revision: str | None = None,
    ) -> None:
        self.installation = FixedProgramInstallation.model_validate(installation)
        if expected_current_revision is not None and not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,80}", expected_current_revision,
        ):
            raise PolicyCompilationError("Fixed route expected current revision is invalid")
        self._expected_current_revision = (
            expected_current_revision or installation.program.api_revision
        )
        self._management = management
        self._subscription_key = subscription_key
        self._client = client
        self._clock = clock
        self._verified_parent: str | None = None

    def require_deployment_pool_reader(self) -> None:
        if self._verified_parent is None or not deployment_pool_reader_supported(
            self._verified_parent,
        ):
            raise PolicyCompilationError(
                "Installed API parent policy requires a deployment Pool reader upgrade"
            )

    @classmethod
    def native_installation(
        cls, installation: FixedProgramInstallation, native_gateway_id: str, endpoint: str,
        management: FixedProgramReader, subscription_key: SecretStr, client: httpx.Client,
    ) -> FixedProgramInstallation:
        if not installation.native_gateway_scope or native_gateway_id.casefold() == "managed":
            raise PolicyCompilationError("Native Gateway binding is unavailable")
        nonce = uuid4().hex
        response = client.request(
            "HEAD", endpoint.rstrip("/") + ";rev=" + quote(installation.program.api_revision, safe="")
            + "/_turnstile/program",
            headers={"Ocp-Apim-Subscription-Key": subscription_key.get_secret_value(),
                     "x-turnstile-route-probe": nonce, "Cache-Control": "no-cache"},
            timeout=10.0, follow_redirects=False,
        )
        expected = {"abi": "2", "nonce": nonce, "target": installation.program.scope.target_id,
                    "api": installation.program.scope.api_id,
                    "revision": installation.program.api_revision, "gateway": native_gateway_id,
                    "program": installation.program.program_sha256,
                    "trust": installation.program.trust_binding_sha256}
        instance = response.headers.get("x-turnstile-route-instance", "")
        if (response.status_code != 204 or response.content or not instance
                or instance.casefold() == "managed" or any(
                    response.headers.get("x-turnstile-route-" + name) != value
                    for name, value in expected.items())):
            raise PolicyCompilationError("Native Gateway authenticated binding changed")
        native = installation.for_native_gateway(native_gateway_id, instance, endpoint.rstrip("/"))
        cls(native, management, subscription_key, client).verify()
        return native

    def verify(self) -> ProgramCapabilityProof:
        self._verified_parent = None
        installed = self.installation
        program = installed.program
        started = self._clock()
        models = installed.operation_contracts.get("models")
        messages = installed.operation_contracts.get("anthropic-messages")
        if models is None or models[0] != "GET" or messages is None or messages[0] != "POST":
            raise PolicyCompilationError("Fixed route public protocol contract is missing")
        contracts = fixed_operation_contracts(
            models[1], messages[1], installed.retired_operations,
            decision_enabled=installed.decision_enabled,
        )
        if installed.operation_contracts != contracts:
            raise PolicyCompilationError("Fixed route program installation is incomplete")
        if self._management.current_revision() != self._expected_current_revision:
            raise PolicyCompilationError("Fixed route program revision changed")
        parent = self._management.revision_api_policy(program.api_revision)
        if fixed_policy_digest(parent) != installed.parent_policy_sha256:
            raise PolicyCompilationError("Fixed route program parent changed")
        origins = parse_policy(parent).findall(".//set-variable[@name='routeVaultOrigins']")
        expected_origins = json.dumps(installed.trusted_vault_origins, separators=(",", ":"))
        if len(origins) != 1 or origins[0].get("value") != expected_origins:
            raise PolicyCompilationError("Fixed route trusted Vault origins binding changed")
        for capability, enabled in {
            "OAuth": installed.oauth_enabled, "Pool": installed.pool_enabled,
            "Decision": installed.decision_enabled,
            "Router": installed.router_enabled,
            "ModelAdditions": installed.model_additions_enabled,
            "NativeGatewayScope": installed.native_gateway_scope,
        }.items():
            flags = parse_policy(parent).findall(
                ".//set-variable[@name='route" + capability + "Enabled']",
            )
            expected = "@(true)" if enabled else "@(false)"
            if enabled and not flags or flags and (
                len(flags) != 1 or flags[0].get("value") != expected
            ):
                raise PolicyCompilationError(
                    "Fixed route " + capability + " program capability changed",
                )
        adapters = parse_policy(parent).findall(".//set-variable[@name='routeRequestAdapters']")
        expected_adapters = json.dumps(installed.supported_adapters, separators=(",", ":"))
        if installed.supported_adapters and not adapters or adapters and (
            len(adapters) != 1 or adapters[0].get("value") != expected_adapters
        ):
            raise PolicyCompilationError("Fixed route request adapter capability changed")
        legacy = parse_policy(parent).findall(".//set-variable[@name='routeLegacyProgram']")
        if installed.model_additions_enabled and (
            len(legacy) != 1 or legacy[0].get("value") != (installed.legacy_program_sha256 or "")
        ):
            raise PolicyCompilationError("Fixed route legacy program binding changed")
        predecessors = parse_policy(parent).findall(".//set-variable[@name='routePredecessorPrograms']")
        if (installed.predecessor_program_sha256 or predecessors) and (
            len(predecessors) != 1 or predecessors[0].get("value")
            != json.dumps(installed.predecessor_program_sha256, separators=(",", ":"))
        ):
            raise PolicyCompilationError("Fixed route predecessor program binding changed")
        operations = self._management.route_program_operations(program.api_revision)
        if set(operations) != set(installed.operation_contracts):
            raise PolicyCompilationError("Fixed route program operation set changed")
        for name, (method, path, policy) in operations.items():
            if (
                (method, path) != installed.operation_contracts[name]
                or fixed_policy_digest(policy) != installed.operation_policies_sha256[name]
                or name in installed.retired_operations
                and fixed_policy_digest(policy) != fixed_policy_digest(
                    compact_fixed_policy(retired_operation()),
                )
            ):
                raise PolicyCompilationError("Fixed route program operation changed")
        digest = fixed_program_digest(
            parent, {name: operation[2] for name, operation in operations.items()},
            tuple((name, *contract) for name, contract in contracts.items()),
        )
        if digest != program.program_sha256:
            raise PolicyCompilationError("Fixed route program complete digest changed")
        self._probe(configuration=None)
        checked_at = self._clock()
        if type(checked_at) is not int or checked_at < started:
            raise PolicyCompilationError(
                "Fixed route program verification exceeded its time window",
            )
        # Management evidence read from `started` backs a proof for at most 150 seconds: a
        # 30-second verification budget plus the 120-second proof lifetime. Slow management
        # reads shorten the proof instead of failing it, so the evidence age bound is unchanged.
        expires_epoch = min(checked_at + 120, started + 150)
        if checked_at >= expires_epoch:
            raise PolicyCompilationError(
                "Fixed route program verification exceeded its time window after "
                f"{checked_at - started} of 150 seconds",
            )
        self._verified_parent = parent
        return ProgramCapabilityProof(
            proof_id=str(uuid4()), identity=program,
            supported_profiles=installed.supported_profiles,
            issued_epoch=checked_at, expires_epoch=expires_epoch,
        )

    def confirm(self, head: ActiveRouteHead) -> None:
        if (
            head.approval.scope != self.installation.program.scope
            or head.approval.required_program_sha256 not in {
                self.installation.program.program_sha256, self.installation.legacy_program_sha256,
                *self.installation.predecessor_program_sha256,
            }
        ):
            raise PolicyCompilationError("Fixed route configuration belongs to another program")
        self._probe(configuration=head)

    def confirm_model(self, model: ModelRouteHead) -> None:
        head = model.head
        if (not self.installation.model_additions_enabled
                or head.approval.scope != self.installation.program.scope
                or head.approval.required_program_sha256 not in {
                    self.installation.program.program_sha256,
                    self.installation.legacy_program_sha256,
                    *self.installation.predecessor_program_sha256,
                }):
            raise PolicyCompilationError("Model route belongs to another installed program")
        self._probe(configuration=head, model_alias=model.alias)

    def _probe(
        self, *, configuration: ActiveRouteHead | None, model_alias: str | None = None,
    ) -> None:
        installed = self.installation
        program = installed.program
        nonce = uuid4().hex
        endpoint = installed.gateway_endpoint + ";rev=" + quote(program.api_revision, safe="")
        endpoint += (
            "/_turnstile/configuration" if configuration is not None else "/_turnstile/program"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self._subscription_key.get_secret_value(),
            "x-turnstile-route-probe": nonce, "Cache-Control": "no-cache",
        }
        if model_alias is not None:
            headers["x-turnstile-route-model"] = model_alias
        response = self._client.request(
            "HEAD", endpoint, headers=headers, timeout=10.0, follow_redirects=False,
        )
        expected = {
            "abi": "2", "nonce": nonce, "target": program.scope.target_id,
            "api": program.scope.api_id, "revision": program.api_revision,
            "gateway": program.scope.gateway_id, "instance": program.instance_id,
            "program": program.program_sha256, "trust": program.trust_binding_sha256,
        }
        if configuration is not None:
            expected.update(
                version=configuration.approval.route_version,
                generation=str(configuration.approval.generation),
                epoch=configuration.approval.budget_epoch,
            )
        if model_alias is not None:
            expected["model"] = model_alias.casefold()
        if response.status_code != 204 or response.content or any(
            response.headers.get("x-turnstile-route-" + name) != value
            for name, value in expected.items()
        ):
            raise PolicyCompilationError("Fixed route authenticated probe identity changed")


FULL_PROGRAM_PROFILES: tuple[RouteProfile, ...] = (
    "openai-v1", "anthropic-messages", "openai-images", "microsoft-decision",
)


class FixedRouteBinding(RouteModel):
    scope: RouteScope
    api_revision: Identifier
    table_endpoint: str
    table_name: str
    ledger_table_endpoint: str
    ledger_table_name: str
    trust_binding_sha256: Digest
    program_sha256: Digest = "0" * 64
    gateway_kind: Literal["managed"] = "managed"
    trusted_vault_origins: tuple[str, ...] = ()
    observer_key_named_value: Identifier | None = None
    publisher_subscription_id: Identifier = "finops-publisher-probe"
    supported_profiles: tuple[
        Literal["openai-v1", "anthropic-messages", "openai-images", "microsoft-decision"], ...
    ] = (
        "openai-v1",
        "anthropic-messages",
    )
    image_limits: ImageGenerationLimits | None = None
    oauth_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    pool_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    router_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    decision_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    model_additions_enabled: bool = Field(default=False, exclude_if=lambda value: value is False)
    native_gateway_scope: bool = Field(default=False, exclude_if=lambda value: value is False)
    legacy_program_sha256: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    predecessor_program_sha256: tuple[Digest, ...] = Field(
        default=(), max_length=32, exclude_if=lambda value: not value,
    )
    supported_adapters: tuple[Literal["bedrock-anthropic-v1"], ...] = Field(
        default=(), max_length=1, exclude_if=lambda value: not value,
    )
    models_path: Literal["/models", "/v1/models"] = Field(
        default="/models", exclude_if=lambda value: value == "/models",
    )
    messages_path: Literal["/messages", "/v1/messages"] = Field(
        default="/messages", exclude_if=lambda value: value == "/messages",
    )
    retired_operations: dict[Identifier, tuple[Literal["POST"], str]] = Field(
        default_factory=dict, max_length=16, exclude_if=lambda value: not value,
    )
    cache_seconds: int = Field(default=300, strict=True, ge=1, le=300)

    @model_validator(mode="after")
    def validate_storage(self) -> FixedRouteBinding:
        if self.legacy_program_sha256 is not None and not self.model_additions_enabled:
            raise ValueError("MODEL_ADDITION_LEGACY_PROGRAM_REQUIRES_CAPABILITY")
        if self.predecessor_program_sha256 and (
            not self.model_additions_enabled
            or len(set(self.predecessor_program_sha256)) != len(self.predecessor_program_sha256)
            or self.program_sha256 in self.predecessor_program_sha256
            or self.legacy_program_sha256 in self.predecessor_program_sha256
        ):
            raise ValueError("ROUTE_PREDECESSOR_PROGRAM_BINDING_INVALID")
        if (
            not re.fullmatch(
                r"https://[a-z0-9]{3,24}\.table\.core\.windows\.net", self.table_endpoint
            )
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", self.table_name)
            or self.table_name.casefold() == "turnstileledger"
            or self.scope.gateway_id != "managed"
            or not re.fullmatch(
                r"https://[a-z0-9]{3,24}\.table\.core\.windows\.net", self.ledger_table_endpoint
            )
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", self.ledger_table_name)
            or (
                self.table_endpoint == self.ledger_table_endpoint
                and self.table_name.casefold() == self.ledger_table_name.casefold()
            )
        ):
            raise ValueError("FIXED_ROUTE_PRIMARY_STORE_REQUIRED")
        if len(set(self.trusted_vault_origins)) != len(self.trusted_vault_origins) or any(
            not re.fullmatch(r"https://[a-z0-9-]{3,24}\.vault\.azure\.net", value)
            for value in self.trusted_vault_origins
        ):
            raise ValueError("FIXED_ROUTE_VAULT_BINDING_INVALID")
        if not self.supported_profiles or len(set(self.supported_profiles)) != len(
            self.supported_profiles
        ):
            raise ValueError("FIXED_ROUTE_PROFILE_BINDING_INVALID")
        if ("openai-images" in self.supported_profiles) != (self.image_limits is not None):
            raise ValueError("FIXED_ROUTE_IMAGE_CAPACITY_REQUIRED")
        if ("microsoft-decision" in self.supported_profiles) != self.decision_enabled:
            raise ValueError("FIXED_ROUTE_DECISION_CAPABILITY_REQUIRED")
        fixed_operation_contracts(
            self.models_path, self.messages_path, self.retired_operations,
            decision_enabled=self.decision_enabled,
        )
        return self


def image_program_profiles(binding: FixedRouteBinding) -> tuple[ImageGenerationProfile, ...]:
    return (
        (create_image_profile(binding.image_limits, "fixed-route-capacity"),)
        if binding.image_limits is not None else ()
    )


def _exact_fields(variable: str, fields: str, optional: str = "") -> str:
    if optional:
        required = ",".join('"' + name + '"' for name in fields.split())
        allowed = ",".join('"' + name + '"' for name in (fields + " " + optional).split())
        return (
            f"if ({variable} == null || new [] {{{required}}}.Any(name => {variable}[name] == null) "
            f"|| {variable}.Properties().Any(property => !new [] {{{allowed}}}.Contains(property.Name))) "
            '{ return ""; }'
        )
    names = ",".join('"' + name + '"' for name in fields.split())
    return (
        f"if ({variable} == null || {variable}.Properties().Count() != {len(fields.split())} "
        f"|| {variable}.Properties().Any(property => !new [] {{{names}}}.Contains(property.Name))) "
        '{ return ""; }'
    )


def _strict_json_object(document: str, result: str) -> str:
    return r'''
        var __RESULT__Encoded = System.Text.RegularExpressions.Regex.Replace(
            __DOCUMENT__, @"""(?:[^""\\]|\\.)*""", match => match.Value.Insert(1, "~"));
        var __RESULT__Source = JObject.Parse(__RESULT__Encoded);
        var __RESULT__ = new JObject();
        var __RESULT__Queue = new Queue<Tuple<JContainer, JContainer, int>>();
        __RESULT__Queue.Enqueue(Tuple.Create((JContainer)__RESULT__Source, (JContainer)__RESULT__, 0));
        var __RESULT__Nodes = 0;
        while (__RESULT__Queue.Count > 0) {
            var item = __RESULT__Queue.Dequeue();
            if (item.Item3 > 32 || ++__RESULT__Nodes > 32768) { throw new Exception(); }
            var rawFields = item.Item1 is JObject
                ? ((JObject)item.Item1).Properties().Select(property => new KeyValuePair<string, JToken>(property.Name.Substring(1), property.Value))
                : ((JArray)item.Item1).Select(value => new KeyValuePair<string, JToken>("", value));
            foreach (var field in rawFields) {
                JToken child = field.Value is JObject ? (JToken)new JObject()
                    : field.Value is JArray ? (JToken)new JArray()
                    : field.Value.Type == JTokenType.String ? new JValue(((string)field.Value).Substring(1))
                    : field.Value.DeepClone();
                if (item.Item2 is JObject) { ((JObject)item.Item2).Add(field.Key, child); }
                else { ((JArray)item.Item2).Add(child); }
                if (child is JContainer) {
                    __RESULT__Queue.Enqueue(Tuple.Create((JContainer)field.Value, (JContainer)child, item.Item3 + 1));
                }
            }
        }
        if (__RESULT__.ToString(Newtonsoft.Json.Formatting.None) != __DOCUMENT__) { throw new Exception(); }
    '''.replace("__DOCUMENT__", document).replace("__RESULT__", result)


def _entity_document(
    response: str, partition: str, row: str, limit: int, *, response_expression: bool = False,
) -> str:
    return (
        r'''
        var response = context.Variables.GetValueOrDefault<IResponse>('''
        + (response if response_expression else json.dumps(response))
        + r""", null);
        if (response == null || response.StatusCode != 200) { return ""; }
        var etag = response.Headers.GetValueOrDefault("ETag", "");
        if (string.IsNullOrWhiteSpace(etag) || etag == "*" || etag.IndexOf('\r') >= 0
            || etag.IndexOf('\n') >= 0) { return ""; }
        var envelope = response.Body.As<string>(preserveContent: true);
        if (Encoding.UTF8.GetByteCount(envelope) > """
        + str(limit + 2048)
        + r""") { return ""; }
        var entity = JObject.Parse(envelope);
        var fields = new [] {"PartitionKey","RowKey","SchemaVersion","Document","Sha256",
            "Timestamp","odata.etag","odata.metadata","odata.id","odata.editLink"};
        if (entity.Properties().Any(property => !fields.Contains(property.Name))
            || (string)entity["PartitionKey"] != """
        + partition
        + r"""
            || (string)entity["RowKey"] != """
        + row
        + r"""
            || entity["SchemaVersion"]?.Type != JTokenType.Integer
            || (int)entity["SchemaVersion"] != 2 || entity["Document"]?.Type != JTokenType.String) { return ""; }
        var document = (string)entity["Document"];
        if (Encoding.UTF8.GetByteCount(document) > """
        + str(limit)
        + r"""
            || Encoding.Unicode.GetByteCount(document) > 32768) { return ""; }
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            var actual = BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(document)))
                .Replace("-", "").ToLowerInvariant();
            if (actual != (string)entity["Sha256"]) { return ""; }
        }
    """
        + _strict_json_object("document", "parsed")
    )


SCOPE_CHECK = r"""
        var scope = parsed["scope"] as JObject;
        if (!JToken.DeepEquals(scope, JObject.Parse((string)context.Variables["routeScope"]))) { return ""; }
"""

SELECT_ROUTE_BACKEND = r'''@{ try {
    var connection = JObject.Parse((string)context.Variables["routeContent"])["connection"];
    var backend = (string)connection["backend_id"];
    if (string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeCandidateId", ""))) {
        return backend;
    }
    var selected = context.Variables.GetValueOrDefault<string>("routeCandidateMember", "");
    var pool = connection["pool"] as JObject;
    if (pool == null) { return string.IsNullOrEmpty(selected) ? backend : ""; }
    var members = pool["members"] as JArray;
    return members != null && members.Any(member =>
        string.Equals((string)member["backend_id"], selected, StringComparison.Ordinal)) ? selected : "";
} catch { return ""; } }'''


READ_FIXED_HEAD = (
    "@{ try {"
    + _entity_document(
        '!string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeCandidateId", "")) ? "routeCandidateRead" : context.Variables.GetValueOrDefault<bool>("routeModelHeadSelected", false) ? "routeModelHeadRead" : "routeHeadRead"',
        '(string)context.Variables["routePartition"]',
        '(!string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeCandidateId", "")) ? "candidate:" + (string)context.Variables["routeCandidateId"] : context.Variables.GetValueOrDefault<bool>("routeModelHeadSelected", false) ? "model:" + (string)context.Variables["routeAliasHash"] : "active")',
        8192,
        response_expression=True,
    )
    + r'''
        var candidateId = context.Variables.GetValueOrDefault<string>("routeCandidateId", "");
        var modelSelected = context.Variables.GetValueOrDefault<bool>("routeModelHeadSelected", false);
        if (modelSelected && !string.IsNullOrEmpty(candidateId)) { return ""; }
        if (modelSelected) {
            if (!context.Variables.GetValueOrDefault<bool>("routeModelAdditionsEnabled", false)) { return ""; }
'''
    + _exact_fields(
        "parsed", "version publication_id alias model_id display_name created_at content_sha256 head",
    )
    + r'''
            if (parsed["version"]?.Type != JTokenType.Integer || (int)parsed["version"] != 1
                || parsed["alias"]?.Type != JTokenType.String
                || !string.Equals((string)parsed["alias"], (string)context.Variables["routeAlias"], StringComparison.OrdinalIgnoreCase)
                || parsed["display_name"]?.Type != JTokenType.String
                || string.IsNullOrEmpty((string)parsed["display_name"])
                || ((string)parsed["display_name"]).Length > 255
                || parsed["created_at"]?.Type != JTokenType.String
                || parsed["content_sha256"]?.Type != JTokenType.String
                || !System.Text.RegularExpressions.Regex.IsMatch((string)parsed["content_sha256"], @"\A[0-9a-f]{64}\z")) { return ""; }
            foreach (var field in new [] {"publication_id", "model_id"}) {
                Guid identifier;
                if (parsed[field]?.Type != JTokenType.String || !Guid.TryParseExact((string)parsed[field], "D", out identifier)
                    || identifier.ToString() != (string)parsed[field]) { return ""; }
            }
            var modelHead = parsed["head"] as JObject;
            if (modelHead == null || modelHead["approval"]?["entry_count"]?.Type != JTokenType.Integer
                || (long)modelHead["approval"]["entry_count"] != 1) { return ""; }
            var catalog = new JObject(new JProperty((string)parsed["alias"], new JObject(
                new JProperty("created_at", parsed["created_at"]),
                new JProperty("display_name", parsed["display_name"]),
                new JProperty("id", parsed["alias"]),
                new JProperty("logical_model_id", parsed["model_id"]),
                new JProperty("type", "model")))).ToString(Newtonsoft.Json.Formatting.None);
            using (var digest = System.Security.Cryptography.SHA256.Create()) {
                if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(catalog))).Replace("-", "").ToLowerInvariant()
                    != (string)modelHead["approval"]["catalog_sha256"]) { return ""; }
            }
            parsed = modelHead;
            document = modelHead.ToString(Newtonsoft.Json.Formatting.None);
        }
        if (!string.IsNullOrEmpty(candidateId)) {
'''
    + _exact_fields("parsed", "probe_id publication_id request_id subscription_id alias operation body_sha256 allow_dispatch head issued_epoch expires_epoch", "member_backend_id")
    + r'''
            if (parsed.Property("member_backend_id") != null && (
                parsed["member_backend_id"]?.Type != JTokenType.String
                || !System.Text.RegularExpressions.Regex.IsMatch((string)parsed["member_backend_id"], @"\A[a-zA-Z0-9._-]{1,180}\z"))) { return ""; }
            var candidateNow = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
            foreach (var field in new [] {"probe_id", "publication_id", "request_id"}) {
                Guid identifier;
                if (parsed[field]?.Type != JTokenType.String || !Guid.TryParseExact((string)parsed[field], "D", out identifier)
                    || identifier.ToString() != (string)parsed[field]) { return ""; }
            }
            if (context.Subscription == null || context.Subscription.Id != (string)context.Variables["routePublisherSubscription"]
                || parsed["allow_dispatch"]?.Type != JTokenType.Boolean
                || context.Subscription.Id != (string)parsed["subscription_id"] || context.Request.Method != "POST"
                || candidateId != (string)parsed["probe_id"]
                || context.Request.Headers.GetValueOrDefault("x-turnstile-route-candidate", "") != candidateId
                || context.Request.Headers.GetValueOrDefault("x-request-id", "") != (string)parsed["request_id"]
                || context.Operation.Id != (string)parsed["operation"]
                || !new [] {"chat-completions", "responses", "responses-compact", "anthropic-messages", "anthropic-count-tokens", "images-generations", "microsoft-decision"}.Contains((string)parsed["operation"])
                || parsed["issued_epoch"]?.Type != JTokenType.Integer || parsed["expires_epoch"]?.Type != JTokenType.Integer
                || (long)parsed["issued_epoch"] < 0 || candidateNow < (long)parsed["issued_epoch"]
                || candidateNow >= (long)parsed["expires_epoch"]
                || (long)parsed["expires_epoch"] - (long)parsed["issued_epoch"] > 120) { return ""; }
            var body = context.Request.Body.As<JObject>(preserveContent: true);
            if (body["model"]?.Type != JTokenType.String || parsed["alias"]?.Type != JTokenType.String
                || !string.Equals(((string)body["model"]).Trim(), (string)parsed["alias"], StringComparison.OrdinalIgnoreCase)) { return ""; }
            if (parsed["body_sha256"]?.Type != JTokenType.String) { return ""; }
            using (var digest = System.Security.Cryptography.SHA256.Create()) {
                var actual = BitConverter.ToString(digest.ComputeHash(context.Request.Body.As<byte[]>(preserveContent: true)))
                    .Replace("-", "").ToLowerInvariant();
                if (actual != (string)parsed["body_sha256"]) { return ""; }
            }
            var selected = parsed["head"] as JObject;
            if (selected == null || (long)parsed["issued_epoch"] < (long)selected["activated_epoch"]
                || (long)parsed["expires_epoch"] > (long)selected["approval"]["activate_before_epoch"]) { return ""; }
            parsed = selected;
            document = selected.ToString(Newtonsoft.Json.Formatting.None);
        }
        if (Encoding.UTF8.GetByteCount(document) > 4096) { return ""; }
'''
    + _exact_fields("parsed", "state approval activated_epoch")
    + r"""
        var approval = parsed["approval"] as JObject;
"""
    + _exact_fields(
        "approval",
        "approval_id scope route_version generation manifest_sha256 dependency_manifest_sha256 catalog_sha256 entry_count required_program_sha256 required_profiles budget_epoch issued_epoch activate_before_epoch",
    )
    + r"""
        var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
        if ((string)parsed["state"] != "active"
            || !JToken.DeepEquals(approval["scope"], JObject.Parse((string)context.Variables["routeScope"]))
            || ((string)approval["required_program_sha256"] != (string)context.Variables["routeProgram"]
                && (!string.IsNullOrEmpty(candidateId)
                    || ((string)approval["required_program_sha256"] != context.Variables.GetValueOrDefault<string>("routeLegacyProgram", "")
                        && !JArray.Parse(context.Variables.GetValueOrDefault<string>("routePredecessorPrograms", "[]"))
                            .Values<string>().Contains((string)approval["required_program_sha256"]))))
            || (string)approval["budget_epoch"] != (string)context.Variables["routeBudgetEpoch"]
            || context.Api.Id.Split(';')[0] != (string)approval["scope"]["api_id"]
            || context.Api.Revision != (string)context.Variables["routeApiRevision"]
            || context.Deployment.Gateway.Id != (string)approval["scope"]["gateway_id"]
            || parsed["activated_epoch"]?.Type != JTokenType.Integer) { return ""; }
        foreach (var field in new [] {"issued_epoch","activate_before_epoch","generation","entry_count"}) {
            if (approval[field]?.Type != JTokenType.Integer || (long)approval[field] < 0
                || (long)approval[field] > 9007199254740991L) { return ""; }
        }
        foreach (var field in new [] {"approval_id","route_version","budget_epoch"}) {
            Guid identity;
            if (approval[field]?.Type != JTokenType.String || !Guid.TryParseExact((string)approval[field], "D", out identity)
                || identity.ToString() != (string)approval[field]) { return ""; }
        }
        foreach (var field in new [] {"manifest_sha256","dependency_manifest_sha256","catalog_sha256","required_program_sha256"}) {
            if (approval[field]?.Type != JTokenType.String || !System.Text.RegularExpressions.Regex.IsMatch(
                (string)approval[field], @"\A[0-9a-f]{64}\z")) { return ""; }
        }
        var profiles = approval["required_profiles"] as JArray;
        if (profiles == null || profiles.Any(value => value.Type != JTokenType.String
            || !new [] {"openai-v1","anthropic-messages","openai-images","microsoft-decision"}.Contains((string)value))
            || profiles.Any(value => !JArray.Parse((string)context.Variables["routeProfiles"]).Values<string>().Contains((string)value))
            || profiles.Values<string>().Distinct().Count() != profiles.Count
            || (long)approval["generation"] < 1 || (long)approval["entry_count"] > 10000
            || (long)approval["activate_before_epoch"] <= (long)approval["issued_epoch"]
            || (long)approval["activate_before_epoch"] - (long)approval["issued_epoch"] > 600
            || (long)parsed["activated_epoch"] < (long)approval["issued_epoch"]
            || (long)parsed["activated_epoch"] >= (long)approval["activate_before_epoch"]
            || (long)parsed["activated_epoch"] > now) { return ""; }
        return document;
    } catch { return ""; }
}"""
)

READ_CANDIDATE_PROBE = READ_FIXED_HEAD

PROOF_ROW = r"""@{
    var value = new JArray(context.Api.Revision, context.Deployment.Gateway.Id,
        context.Deployment.Gateway.InstanceId).ToString(Newtonsoft.Json.Formatting.None);
    using (var digest = System.Security.Cryptography.SHA256.Create()) {
        return "proof:" + BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(value)))
            .Replace("-", "").ToLowerInvariant();
    }
}"""

READ_FIXED_PROOF = (
    "@{ try {"
    + _entity_document(
        "routeProofRead",
        '(string)context.Variables["routePartition"]',
        '(string)context.Variables["routeProofRow"]',
        8192,
    )
    + _exact_fields("parsed", "proof_id identity supported_profiles issued_epoch expires_epoch")
    + r"""
        var identity = parsed["identity"] as JObject;
"""
    + _exact_fields(
        "identity", "scope api_revision instance_id program_sha256 trust_binding_sha256"
    )
    + r"""
        var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
        Guid proofId;
        if (!Guid.TryParseExact((string)parsed["proof_id"], "D", out proofId)
            || proofId.ToString() != (string)parsed["proof_id"]
            || !JToken.DeepEquals(identity["scope"], JObject.Parse((string)context.Variables["routeScope"]))
            || (string)identity["api_revision"] != context.Api.Revision
            || (string)identity["instance_id"] != context.Deployment.Gateway.InstanceId
            || (string)identity["program_sha256"] != (string)context.Variables["routeProgram"]
            || (string)identity["trust_binding_sha256"] != (string)context.Variables["routeTrust"]
            || parsed["issued_epoch"]?.Type != JTokenType.Integer || parsed["expires_epoch"]?.Type != JTokenType.Integer
            || (long)parsed["issued_epoch"] < 0 || (long)parsed["issued_epoch"] > now
            || (long)parsed["expires_epoch"] <= (long)parsed["issued_epoch"]) { return ""; }
        var supported = parsed["supported_profiles"] as JArray;
        var required = (JArray)JObject.Parse((string)context.Variables["routeHead"])["approval"]["required_profiles"];
        if (supported == null || supported.Count == 0 || supported.Any(value => value.Type != JTokenType.String)
            || supported.Values<string>().Distinct().Count() != supported.Count
            || required.Any(value => !supported.Values<string>().Contains((string)value))) { return ""; }
        return document;
    } catch { return ""; }
}"""
)

READ_FIXED_MEMBER = (
    "@{ try {"
    + _entity_document(
        "routeMemberRead",
        '(string)context.Variables["routePartition"] + ":" + (string)JObject.Parse((string)context.Variables["routeHead"])["approval"]["route_version"]',
        '(string)context.Variables["routeAliasHash"]',
        24576,
    )
    + _exact_fields(
        "parsed",
        "scope route_version alias model_id content_sha256 connection_approval approval_sha256",
    )
    + SCOPE_CHECK
    + r"""
        var head = JObject.Parse((string)context.Variables["routeHead"]);
        if (context.Variables.GetValueOrDefault<bool>("routeModelHeadSelected", false)) {
            var modelResponse = (IResponse)context.Variables["routeModelHeadRead"];
            var model = JObject.Parse((string)modelResponse.Body.As<JObject>(preserveContent: true)["Document"]);
            if ((string)parsed["model_id"] != (string)model["model_id"]
                || (string)parsed["content_sha256"] != (string)model["content_sha256"]) { return ""; }
        }
        if ((string)parsed["route_version"] != (string)head["approval"]["route_version"]
            || !string.Equals((string)parsed["alias"], (string)context.Variables["routeAlias"], StringComparison.OrdinalIgnoreCase)) { return ""; }
        Guid modelId;
        if (!Guid.TryParseExact((string)parsed["model_id"], "D", out modelId)
            || modelId.ToString() != (string)parsed["model_id"]) { return ""; }
        foreach (var field in new [] {"content_sha256","approval_sha256"}) {
            if (parsed[field]?.Type != JTokenType.String || !System.Text.RegularExpressions.Regex.IsMatch(
                (string)parsed[field], @"\A[0-9a-f]{64}\z")) { return ""; }
        }
        var approval = parsed["connection_approval"] as JObject;
"""
    + _exact_fields(
        "approval", "approval_id connection state evidence_sha256 issued_epoch expires_epoch"
    )
    + r"""
        var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
        Guid approvalId;
        if (approval["approval_id"]?.Type != JTokenType.String
            || !Guid.TryParseExact((string)approval["approval_id"], "D", out approvalId)
            || approvalId.ToString() != (string)approval["approval_id"]
            || approval["evidence_sha256"]?.Type != JTokenType.String
            || !System.Text.RegularExpressions.Regex.IsMatch((string)approval["evidence_sha256"], @"\A[0-9a-f]{64}\z")) { return ""; }
        if ((string)approval["state"] != "approved" || approval["issued_epoch"]?.Type != JTokenType.Integer
            || approval["expires_epoch"]?.Type != JTokenType.Integer || (long)approval["issued_epoch"] < 0
            || (long)approval["issued_epoch"] > now
            || (long)approval["expires_epoch"] <= (long)approval["issued_epoch"]
            || (long)approval["expires_epoch"] - (long)approval["issued_epoch"] > 86400) { return ""; }
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            var approved = approval.ToString(Newtonsoft.Json.Formatting.None);
            if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(approved)))
                .Replace("-", "").ToLowerInvariant() != (string)parsed["approval_sha256"]) { return ""; }
        }
        return document;
    } catch { return ""; }
}"""
)

READ_FIXED_MEMBER_METADATA = READ_FIXED_MEMBER

READ_RENEWED_APPROVAL = (
    "@{ try {"
    + r'''
        if (!context.Variables.GetValueOrDefault<bool>("routeModelAdditionsEnabled", false)) { return ""; }
        if (context.Variables.GetValueOrDefault<IResponse>("routeApprovalRead", null)?.StatusCode == 404) {
            return (string)context.Variables["routeMember"];
        }
'''
    + _entity_document(
        "routeApprovalRead", '(string)context.Variables["routePartition"]',
        '(string)context.Variables["routeApprovalRow"]', 24576,
    )
    + _exact_fields(
        "parsed", "version scope content_sha256 original_approval_sha256 required_program_sha256 budget_epoch approval",
    )
    + SCOPE_CHECK
    + r'''
        var member = JObject.Parse((string)context.Variables["routeMember"]);
        var original = member["connection_approval"];
        if (parsed["version"]?.Type != JTokenType.Integer || (int)parsed["version"] != 1
            || (string)parsed["content_sha256"] != (string)member["content_sha256"]
            || (string)parsed["original_approval_sha256"] != (string)member["approval_sha256"]
            || parsed["required_program_sha256"]?.Type != JTokenType.String
            || !System.Text.RegularExpressions.Regex.IsMatch(
                (string)parsed["required_program_sha256"], @"\A[0-9a-f]{64}\z")
            || (string)parsed["budget_epoch"] != (string)context.Variables["routeBudgetEpoch"]) { return ""; }
        var approval = parsed["approval"] as JObject;
'''
    + _exact_fields("approval", "approval_id connection state evidence_sha256 issued_epoch expires_epoch")
    + r'''
        var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
        Guid identifier;
        if (approval["approval_id"]?.Type != JTokenType.String
            || !Guid.TryParseExact((string)approval["approval_id"], "D", out identifier)
            || identifier.ToString() != (string)approval["approval_id"]
            || !JToken.DeepEquals(approval["connection"], original["connection"])
            || (string)approval["state"] != "approved"
            || approval["evidence_sha256"]?.Type != JTokenType.String
            || !System.Text.RegularExpressions.Regex.IsMatch((string)approval["evidence_sha256"], @"\A[0-9a-f]{64}\z")
            || approval["issued_epoch"]?.Type != JTokenType.Integer || approval["expires_epoch"]?.Type != JTokenType.Integer
            || (long)approval["issued_epoch"] < (long)original["issued_epoch"]
            || (long)approval["issued_epoch"] > now
            || (long)approval["expires_epoch"] <= (long)approval["issued_epoch"]
            || (long)approval["expires_epoch"] <= (long)original["expires_epoch"]
            || (long)approval["expires_epoch"] - (long)approval["issued_epoch"] > 86400) { return ""; }
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            member["approval_sha256"] = BitConverter.ToString(digest.ComputeHash(
                Encoding.UTF8.GetBytes(approval.ToString(Newtonsoft.Json.Formatting.None))))
                .Replace("-", "").ToLowerInvariant();
        }
        member["connection_approval"] = approval;
        return member.ToString(Newtonsoft.Json.Formatting.None);
    } catch { return ""; }
}'''
)

READ_FIXED_CONTENT = (
    r"""@{ try {
        var member = JObject.Parse((string)context.Variables["routeMember"]);
        var document = context.Variables.GetValueOrDefault<string>("routeCachedContent", "");
        if (string.IsNullOrEmpty(document)) {
"""
    + _entity_document(
        "routeContentRead",
        '(string)context.Variables["routePartition"] + ":content"',
        '(string)member["content_sha256"]',
        16384,
    )
    .replace("var document =", "document =")
    .replace("parsed", "cold")
    + r"""
        }
        if (Encoding.UTF8.GetByteCount(document) > 16384 || Encoding.Unicode.GetByteCount(document) > 32768) { return ""; }
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(document)))
                .Replace("-", "").ToLowerInvariant() != (string)member["content_sha256"]) { return ""; }
        }
"""
    + _strict_json_object("document", "parsed")
    + _exact_fields(
        "parsed",
        "scope model_id alias display_name created_at upstream_model profile provider_name runtime_name context_window assignment_required streaming_mode operations connection budget_epoch budget",
        "image_profile request_adapter",
    )
    + SCOPE_CHECK
    + r"""
        if ((string)parsed["model_id"] != (string)member["model_id"] || (string)parsed["alias"] != (string)member["alias"]
            || (string)parsed["budget_epoch"] != (string)context.Variables["routeBudgetEpoch"]
            || (string)parsed["budget"]?["model_id"] != (string)member["model_id"]
            || parsed["context_window"]?.Type != JTokenType.Integer || (long)parsed["context_window"] < 0
            || (long)parsed["context_window"] > 9007199254740991L
            || parsed["assignment_required"]?.Type != JTokenType.Boolean
            || !new [] {"native","buffered"}.Contains((string)parsed["streaming_mode"])) { return ""; }
        foreach (var field in new [] {"alias","upstream_model","provider_name","runtime_name"}) {
            if (parsed[field]?.Type != JTokenType.String || string.IsNullOrWhiteSpace((string)parsed[field])
                || ((string)parsed[field]).Length > 255) { return ""; }
        }
        var operations = parsed["operations"] as JArray;
        var profiles = new Dictionary<string,string[]> {
            {"openai-v1", new [] {"chat-completions","responses","responses-compact"}},
            {"anthropic-messages", new [] {"anthropic-messages"}},
            {"openai-images", new [] {"images-generations"}},
            {"microsoft-decision", new [] {"microsoft-decision"}}
        };
        var profile = (string)parsed["profile"] ?? "";
        if (profile == "microsoft-decision" && !context.Variables.GetValueOrDefault<bool>("routeDecisionEnabled", false)) { return ""; }
        var image = parsed["image_profile"] as JObject;
        if (profile == "openai-images") {
            var limits = JObject.Parse(context.Variables.GetValueOrDefault<string>("routeImageLimits", "{}"));
            if (image == null || (string)image["protocol"] != "openai_images"
                || (string)image["provider_model"] != (string)parsed["upstream_model"]
                || (string)image["backend_path"] != "/openai/v1/images/generations"
                || (string)image["api_version"] != "preview") { return ""; }
            foreach (var field in new [] {"max_request_bytes","max_response_bytes","output_reservation_tokens","timeout_seconds"}) {
                if (image[field]?.Type != JTokenType.Integer || (long)image[field] < 1
                    || limits[field]?.Type != JTokenType.Integer || (long)image[field] > (long)limits[field]) { return ""; }
            }
        } else if (parsed["image_profile"] != null) { return ""; }
        var routeOperation = context.Operation.Id == "anthropic-count-tokens"
            ? "anthropic-messages" : context.Operation.Id;
        if (!profiles.ContainsKey(profile) || operations == null || operations.Count == 0
            || operations.Any(value => value.Type != JTokenType.String || !profiles[profile].Contains((string)value))
            || operations.Values<string>().Distinct().Count() != operations.Count
            || !operations.Values<string>().Contains(routeOperation)
            || !((JArray)JObject.Parse((string)context.Variables["routeHead"])["approval"]["required_profiles"])
                .Values<string>().Contains(profile)) { return ""; }
        var connection = parsed["connection"] as JObject;
"""
    + _exact_fields(
        "connection",
        "scope revision_id backend_id origin path protocol_paths dependency_sha256 credential observer count",
        "public_count_path pool",
    )
    + r"""
        if (!JToken.DeepEquals(connection, member["connection_approval"]["connection"])
            || !JToken.DeepEquals(connection["scope"], parsed["scope"])
            || connection["observer"]?.Type != JTokenType.Boolean) { return ""; }
        if (connection["public_count_path"] != null && (
            connection["public_count_path"].Type != JTokenType.String || profile != "anthropic-messages"
            || !((string)connection["public_count_path"]).EndsWith("/messages/count_tokens", StringComparison.Ordinal))) { return ""; }
        foreach (var field in new [] {"revision_id", "backend_id", "origin", "path", "dependency_sha256"}) {
            if (connection[field]?.Type != JTokenType.String) { return ""; }
        }
        Guid revision;
        if (!Guid.TryParseExact((string)connection["revision_id"], "D", out revision)
            || revision.ToString() != (string)connection["revision_id"]
            || !System.Text.RegularExpressions.Regex.IsMatch((string)connection["backend_id"] ?? "", @"\A[a-zA-Z0-9._-]{1,180}\z")
            || !System.Text.RegularExpressions.Regex.IsMatch((string)connection["dependency_sha256"] ?? "", @"\A[0-9a-f]{64}\z")) { return ""; }
        var origin = new Uri((string)connection["origin"]);
        if (origin.Scheme != "https" || origin.Port != 443 || !string.IsNullOrEmpty(origin.UserInfo)
            || origin.AbsolutePath != "/" || !string.IsNullOrEmpty(origin.Query) || !string.IsNullOrEmpty(origin.Fragment)) { return ""; }
        var paths = connection["protocol_paths"] as JObject;
        if (paths == null || paths.Properties().Any(property => property.Value.Type != JTokenType.String
            || !new [] {"chat-completions", "responses", "responses-compact", "anthropic-messages", "images-generations", "microsoft-decision"}.Contains(property.Name))
            || (operations.Count > 1 && operations.Any(value => paths[(string)value]?.Type != JTokenType.String))) { return ""; }
        if (parsed["request_adapter"] != null && (
            parsed["request_adapter"].Type != JTokenType.String
            || (string)parsed["request_adapter"] != "bedrock-anthropic-v1"
            || !JArray.Parse(context.Variables.GetValueOrDefault<string>("routeRequestAdapters", "[]"))
                .Values<string>().Contains((string)parsed["request_adapter"])
            || profile != "anthropic-messages" || (string)parsed["streaming_mode"] != "buffered"
            || operations.Count != 1 || (string)operations[0] != "anthropic-messages"
            || (string)connection["path"] != "/model/" + (string)parsed["upstream_model"] + "/invoke"
            || ((string)paths["anthropic-messages"] ?? (string)connection["path"]) != (string)connection["path"]
            || connection["public_count_path"] != null)) { return ""; }
        foreach (var path in paths.Properties().Select(property => (string)property.Value)
            .Concat(new [] {(string)connection["path"]})
            .Concat(connection["public_count_path"] == null ? new string[0] : new [] {(string)connection["public_count_path"]})) {
            if (path.Length > 1000 || !path.StartsWith("/", StringComparison.Ordinal) || (path.Length > 1 && path[1] == (char)47)
                || path.Contains("?") || path.Contains("#") || path.Contains("%") || path.IndexOf((char)92) >= 0
                || path.Split('/').Contains("..")
                || path.Any(character => char.IsWhiteSpace(character) || char.IsControl(character) || character > 127)) { return ""; }
        }
        if (connection["pool"] != null) {
            var pool = connection["pool"] as JObject;
"""
    + _exact_fields("pool", "members rate_limit session_cookie_name credential_versions", "header_values configuration_sha256")
    + r"""
            if (!context.Variables.GetValueOrDefault<bool>("routePoolEnabled", false)
                || profile == "openai-images"
                || !new [] {"none", "managed_identity"}.Contains((string)connection["credential"]?["mode"])
                || !JToken.DeepEquals(pool["rate_limit"], JObject.Parse(@"{""backend_timeout_seconds"":120,""circuit_breaker"":{""accept_retry_after"":true,""error_reasons"":[""BackendConnectionFailure"",""Timeout""],""failure_count"":1,""interval_seconds"":60,""status_code_ranges"":[{""maximum"":429,""minimum"":429},{""maximum"":408,""minimum"":408},{""maximum"":599,""minimum"":500}],""trip_duration_seconds"":60},""first_fast_retry"":true,""max_attempts_per_request"":2,""retry_interval_seconds"":1}"))) { return ""; }
            var poolMembers = pool["members"] as JArray;
            if (pool["configuration_sha256"] != null && (
                pool["configuration_sha256"].Type != JTokenType.String
                || !System.Text.RegularExpressions.Regex.IsMatch((string)pool["configuration_sha256"], @"\A[0-9a-f]{64}\z"))) { return ""; }
            var backendIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var weights = new Dictionary<long,long>();
            if (poolMembers == null || poolMembers.Count < 2 || poolMembers.Count > 30) { return ""; }
            foreach (var value in poolMembers) {
                var poolMember = value as JObject;
"""
    + _exact_fields("poolMember", "runtime_id backend_id dependency_sha256 priority weight")
    + r"""
                Guid runtimeId;
                if (poolMember["runtime_id"]?.Type != JTokenType.String
                    || !Guid.TryParseExact((string)poolMember["runtime_id"], "D", out runtimeId)
                    || runtimeId.ToString() != (string)poolMember["runtime_id"]
                    || poolMember["backend_id"]?.Type != JTokenType.String
                    || !System.Text.RegularExpressions.Regex.IsMatch((string)poolMember["backend_id"], @"\A[a-zA-Z0-9._-]{1,180}\z")
                    || !backendIds.Add((string)poolMember["backend_id"])
                    || string.Equals((string)poolMember["backend_id"], (string)connection["backend_id"], StringComparison.OrdinalIgnoreCase)
                    || poolMember["dependency_sha256"]?.Type != JTokenType.String
                    || !System.Text.RegularExpressions.Regex.IsMatch((string)poolMember["dependency_sha256"], @"\A[0-9a-f]{64}\z")
                    || poolMember["priority"]?.Type != JTokenType.Integer || poolMember["weight"]?.Type != JTokenType.Integer
                    || (long)poolMember["priority"] < 0 || (long)poolMember["priority"] > 100
                    || (long)poolMember["weight"] < 0 || (long)poolMember["weight"] > 100) { return ""; }
                var priority = (long)poolMember["priority"];
                if (!weights.ContainsKey(priority)) { weights[priority] = 0; }
                weights[priority] += (long)poolMember["weight"];
            }
            if (weights.Values.Any(weight => weight == 0)) { return ""; }
            if (pool["session_cookie_name"]?.Type != JTokenType.Null) {
                string cookie;
                using (var digest = System.Security.Cryptography.SHA256.Create()) {
                    cookie = "TurnstileAffinity-" + BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(
                        (string)scope["target_id"] + ":" + (string)parsed["alias"])))
                        .Replace("-", "").ToLowerInvariant().Substring(0, 16);
                }
                if (pool["session_cookie_name"]?.Type != JTokenType.String || (string)pool["session_cookie_name"] != cookie) { return ""; }
            }
            var versions = pool["credential_versions"] as JObject;
            if (versions == null || versions.Count > 30) { return ""; }
            if (pool["header_values"] != null) {
                var headers = pool["header_values"] as JObject;
                if (headers == null || headers.Count > 90) { return ""; }
                foreach (var header in headers.Properties()) {
                    if (!System.Text.RegularExpressions.Regex.IsMatch(header.Name, @"\A[a-zA-Z0-9._-]{1,180}\z")
                        || versions[header.Name] != null || header.Value.Type != JTokenType.String
                        || ((string)header.Value).Length == 0 || ((string)header.Value).Length > 255
                        || new [] {"@(", "@{", "{{", "}}"}.Any(marker => ((string)header.Value).Contains(marker))
                        || ((string)header.Value).Any(character => character < 32)) { return ""; }
                }
            }
            var origins = JArray.Parse(context.Variables.GetValueOrDefault<string>("routeVaultOrigins", "[]"));
            foreach (var version in versions.Properties()) {
                if (!System.Text.RegularExpressions.Regex.IsMatch(version.Name, @"\A[a-zA-Z0-9._-]{1,180}\z")
                    || version.Value.Type != JTokenType.String) { return ""; }
                var secretUri = new Uri((string)version.Value);
                if (secretUri.Scheme != "https" || secretUri.Port != 443 || !string.IsNullOrEmpty(secretUri.UserInfo)
                    || !string.IsNullOrEmpty(secretUri.Query) || !string.IsNullOrEmpty(secretUri.Fragment)
                    || !origins.Values<string>().Contains(secretUri.GetLeftPart(UriPartial.Authority))
                    || !System.Text.RegularExpressions.Regex.IsMatch(secretUri.Host, @"\A[a-z0-9-]{3,24}\.vault\.azure\.net\z")
                    || !System.Text.RegularExpressions.Regex.IsMatch(secretUri.AbsolutePath, @"\A/secrets/[a-zA-Z0-9-]{1,127}/[0-9a-f]{32}\z")) { return ""; }
            }
        }
        foreach (var kind in new [] {"credential", "count"}) {
            if (kind == "count" && connection[kind]?.Type == JTokenType.Null) { continue; }
            var counter = connection[kind] as JObject;
            if (kind == "credential") {
"""
    + _exact_fields("counter", "mode header secret_id audience", "oauth")
    + r"""
            } else {
"""
    + _exact_fields("counter", "mode origin header secret_id audience endpoints", "oauth")
    + r"""
            }
            foreach (var field in new [] {"mode","header","secret_id","audience"}) {
                if (counter[field]?.Type != JTokenType.String) { return ""; }
            }
            if (kind == "count") {
                if (counter["origin"]?.Type != JTokenType.String || (string)counter["origin"] != (string)connection["origin"]) { return ""; }
                var endpoints = counter["endpoints"] as JArray;
                if (endpoints == null || endpoints.Count == 0 || endpoints.Count > 5
                    || endpoints.Any(value => value.Type != JTokenType.String)
                    || endpoints.Values<string>().Distinct().Count() != endpoints.Count) { return ""; }
                foreach (string endpoint in endpoints) {
                    if (!endpoint.StartsWith("/", StringComparison.Ordinal) || (endpoint.Length > 1 && endpoint[1] == (char)47)
                        || endpoint.Contains("?") || endpoint.Contains("#") || endpoint.IndexOf((char)92) >= 0
                        || endpoint.Contains("%") || endpoint.Split('/').Contains("..")
                        || endpoint.Any(character => char.IsWhiteSpace(character) || char.IsControl(character) || character > 127)
                        || (!endpoint.EndsWith("/messages/count_tokens", StringComparison.Ordinal)
                            && !endpoint.EndsWith("/responses/input_tokens", StringComparison.Ordinal))) { return ""; }
                }
            }
            var mode = (string)counter["mode"];
            var header = (string)counter["header"];
            var secret = (string)counter["secret_id"];
            var audience = (string)counter["audience"];
            if (mode != "oauth_client_credentials" && counter["oauth"] != null) { return ""; }
            if (mode == "oauth_client_credentials") {
                var oauth = counter["oauth"] as JObject;
"""
    + _exact_fields("oauth", "provider_id authorization_id client_id token_url scopes")
    + r"""
                Guid clientId;
                if (oauth.Properties().Any(property => property.Value.Type != JTokenType.String)
                    || !context.Variables.GetValueOrDefault<bool>("routeOAuthEnabled", false)
                    || context.Deployment.Gateway.Id != "managed" || (string)scope["gateway_id"] != "managed"
                    || header != "Authorization" || secret != "" || audience != ""
                    || (string)oauth["authorization_id"] != "connection" || (string)oauth["scopes"] != "all-apis"
                    || !System.Text.RegularExpressions.Regex.IsMatch((string)oauth["provider_id"], @"\Aturnstile-oauth-[0-9a-f]{32}\z")
                    || !Guid.TryParseExact((string)oauth["client_id"], "D", out clientId)
                    || clientId.ToString() != (string)oauth["client_id"]
                    || !origin.Host.EndsWith(".azuredatabricks.net", StringComparison.Ordinal)
                    || (string)oauth["token_url"] != "https://" + origin.Host + "/oidc/v1/token"
                    || (string)oauth["token_url"] != (string)connection["origin"] + "/oidc/v1/token") { return ""; }
            } else if (mode == "none") {
                if (header != "" || secret != "" || audience != "") { return ""; }
            } else if (mode == "managed_identity") {
                if (header != "Authorization" || secret != "") { return ""; }
                Guid resourceId;
                if (Guid.TryParseExact(audience, "D", out resourceId)) {
                    if (resourceId.ToString() != audience) { return ""; }
                } else {
                    var resource = new Uri(audience);
                    if (resource.Scheme != "https" || resource.Port != 443 || resource.AbsolutePath != "/"
                        || audience.Any(character => char.IsWhiteSpace(character) || character > 127)
                        || !string.IsNullOrEmpty(resource.UserInfo) || !string.IsNullOrEmpty(resource.Query)
                        || !string.IsNullOrEmpty(resource.Fragment)) { return ""; }
                }
            } else if (mode == "key_vault_api_key" || mode == "key_vault_bearer") {
                var vault = new Uri(secret);
                var allowed = JArray.Parse(context.Variables.GetValueOrDefault<string>("routeVaultOrigins", "[]"));
                if (audience != "" || vault.Scheme != "https" || vault.Port != 443 || !string.IsNullOrEmpty(vault.UserInfo)
                    || !string.IsNullOrEmpty(vault.Query) || !string.IsNullOrEmpty(vault.Fragment)
                    || !allowed.Values<string>().Contains(vault.GetLeftPart(UriPartial.Authority))
                    || !System.Text.RegularExpressions.Regex.IsMatch(vault.Host, @"\A[a-z0-9-]{3,24}\.vault\.azure\.net\z")
                    || !System.Text.RegularExpressions.Regex.IsMatch(vault.AbsolutePath, @"\A/secrets/[a-zA-Z0-9-]{1,127}/[0-9a-f]{32}\z")
                    || (mode == "key_vault_api_key" && !new [] {"api-key","x-api-key"}.Contains(header))
                    || (mode == "key_vault_bearer" && header != "Authorization")) { return ""; }
            } else { return ""; }
        }
        var budget = parsed["budget"] as JObject;
        var protocols = budget?["protocols"] as JObject;
        if (budget == null || protocols == null || protocols.Properties().Any(property =>
            !new [] {"chat-completions", "responses", "responses-compact", "anthropic-messages", "images-generations", "microsoft-decision"}.Contains(property.Name)
            || property.Value.Type != JTokenType.Object)) { return ""; }
        foreach (string operation in operations) {
            var contract = protocols[operation] as JObject ?? budget;
            var supported = contract["operations"] as JArray;
            if (supported == null || supported.Any(value => value.Type != JTokenType.String)
                || !supported.Values<string>().Contains(operation)) { return ""; }
            if (contract["native_counter"]?.Type == JTokenType.Null) { continue; }
            var native = contract["native_counter"] as JObject;
            var counter = connection["count"] as JObject;
            if (native == null || counter == null || native["endpoint"]?.Type != JTokenType.String
                || !((JArray)counter["endpoints"]).Values<string>().Any(endpoint =>
                    (string)native["endpoint"] == (string)counter["origin"] + endpoint)) { return ""; }
        }
        return document;
    } catch { return ""; }
}"""
)


def deployment_pool_reader_supported(
    parent_policy: str,
) -> bool:
    readers = [
        node.get("value", "")
        for node in parse_policy(parent_policy).iter("set-variable")
        if node.get("name") == "routeContent"
    ]
    expected = expression_tokens(READ_FIXED_CONTENT)
    return bool(readers) and all(expression_tokens(reader) == expected for reader in readers)


def variable(parent: ET.Element, name: str, value: str) -> ET.Element:
    return ET.SubElement(parent, "set-variable", {"name": name, "value": value})


def deny(parent: ET.Element, condition: str, code: str = "route_configuration_unavailable") -> None:
    branch = ET.SubElement(ET.SubElement(parent, "choose"), "when", {"condition": condition})
    response = ET.SubElement(branch, "return-response")
    ET.SubElement(response, "set-status", {"code": "503", "reason": "Service Unavailable"})
    for name, value in (("Content-Type", "application/json"), ("Cache-Control", "no-store")):
        header = ET.SubElement(response, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(header, "value").text = value
    ET.SubElement(response, "set-body").text = canonical_document({"error": {"code": code}})


def table_read(parent: ET.Element, name: str, partition: str, row: str) -> None:
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
    ET.SubElement(request, "set-url").text = (
        '@((string)context.Variables["routeStore"] + "/" + (string)context.Variables["routeTable"]'
        ' + "(PartitionKey=\u0027" + Uri.EscapeDataString(' + partition + ")"
        ' + "\u0027,RowKey=\u0027" + Uri.EscapeDataString(' + row + ') + "\u0027)")'
    )
    ET.SubElement(request, "set-method").text = "GET"
    for header, value in (
        ("Accept", "application/json;odata=nometadata"),
        ("x-ms-version", "2020-12-06"),
    ):
        node = ET.SubElement(request, "set-header", {"name": header, "exists-action": "override"})
        ET.SubElement(node, "value").text = value
    ET.SubElement(
        request, "authentication-managed-identity", {"resource": "https://storage.azure.com/"}
    )


def fixed_reader(binding: FixedRouteBinding) -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root,
        "when",
        {
            "condition": '@((bool)context.Variables["isInferenceOperation"] || context.Operation.Id == "anthropic-count-tokens" || context.Operation.Id == "models" || context.Operation.Id == "storage-route-program-probe" || context.Operation.Id == "storage-route-configuration-probe")'
        },
    )
    scope_value = canonical_document(binding.scope)
    partition_value = binding.scope.partition
    if binding.native_gateway_scope:
        scope_value = "@{\n" + (
            "var scope = JObject.Parse(" + json.dumps(scope_value, ensure_ascii=True) + ");\n"
            "var gateway = context.Deployment.Gateway.Id;\n"
            'if (string.IsNullOrEmpty(gateway) || gateway.Length > 80 '
            '|| !System.Text.RegularExpressions.Regex.IsMatch(gateway, @"^[A-Za-z0-9][A-Za-z0-9_.-]*$")) { return ""; }\n'
            'scope["gateway_id"] = gateway;\n'
            "return scope.ToString(Newtonsoft.Json.Formatting.None);\n}"
        )
        partition_value = r'''@{
            var scope = (string)context.Variables["routeScope"];
            if (string.IsNullOrEmpty(scope)) { return ""; }
            using (var sha = System.Security.Cryptography.SHA256.Create()) {
                return "route-v2:" + BitConverter.ToString(sha.ComputeHash(
                    System.Text.Encoding.UTF8.GetBytes(scope))).Replace("-", "").ToLowerInvariant();
            }
        }'''
    for name, value in {
        "routeScope": scope_value,
        "routePartition": partition_value,
        "routeStore": binding.table_endpoint,
        "routeTable": binding.table_name,
        "routeProgram": binding.program_sha256,
        "routeTrust": binding.trust_binding_sha256,
        "routeApiRevision": binding.api_revision,
        "routeVaultOrigins": json.dumps(binding.trusted_vault_origins, separators=(",", ":")),
        "routeProfiles": json.dumps(binding.supported_profiles, separators=(",", ":")),
        "routePublisherSubscription": binding.publisher_subscription_id,
        "routeImageLimits": canonical_document(binding.image_limits) if binding.image_limits else "{}",
        "routeOAuthEnabled": "@(true)" if binding.oauth_enabled else "@(false)",
        "routePoolEnabled": "@(true)" if binding.pool_enabled else "@(false)",
        "routeRequestAdapters": json.dumps(binding.supported_adapters, separators=(",", ":")),
    }.items():
        variable(branch, name, value)
    if binding.router_enabled:
        variable(branch, "routeRouterEnabled", "@(true)")
    if binding.native_gateway_scope:
        variable(branch, "routeNativeGatewayScopeEnabled", "@(true)")
    if binding.decision_enabled:
        variable(branch, "routeDecisionEnabled", "@(true)")
    program_probe = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@(context.Operation.Id == "storage-route-program-probe")',
        },
    )
    fixed_probe(program_probe, configuration=False)
    variable(
        branch,
        "routeBudgetEpoch",
        '@((string)JObject.Parse((string)context.Variables["budgetExecutionFence"])["epoch"])',
    )
    variable(branch, "routeProofRow", PROOF_ROW)
    variable(branch, "routeCandidateId", r'''@{
        var candidate = context.Request.Headers.GetValueOrDefault("x-turnstile-route-candidate", "");
        return candidate;
    }''')
    candidate_branch = ET.SubElement(ET.SubElement(branch, "choose"), "when", {
        "condition": '@(!string.IsNullOrEmpty((string)context.Variables["routeCandidateId"]))',
    })
    deny(candidate_branch, r'''@{
        Guid identifier;
        var value = (string)context.Variables["routeCandidateId"];
        return context.Subscription == null
            || context.Subscription.Id != (string)context.Variables["routePublisherSubscription"]
            || context.Request.Method != "POST"
            || (!(bool)context.Variables["isInferenceOperation"] && context.Operation.Id != "anthropic-count-tokens")
            || !Guid.TryParseExact(value, "D", out identifier) || identifier.ToString() != value;
    }''', "route_candidate_forbidden")
    table_read(
        candidate_branch, "routeCandidateRead", '(string)context.Variables["routePartition"]',
        '"candidate:" + (string)context.Variables["routeCandidateId"]',
    )
    if binding.model_additions_enabled:
        variable(branch, "routeModelAdditionsEnabled", "@(true)")
        variable(branch, "routeLegacyProgram", binding.legacy_program_sha256 or "")
        if binding.predecessor_program_sha256:
            variable(branch, "routePredecessorPrograms", json.dumps(
                binding.predecessor_program_sha256, separators=(",", ":"),
            ))
        variable(branch, "routeModelProbe", '@(context.Operation.Id == "storage-route-configuration-probe" && context.Request.Headers.ContainsKey("x-turnstile-route-model"))')
        model_lookup = ET.SubElement(ET.SubElement(branch, "choose"), "when", {
            "condition": '@(string.IsNullOrEmpty((string)context.Variables["routeCandidateId"]) && ((bool)context.Variables["isInferenceOperation"] || context.Operation.Id == "anthropic-count-tokens" || (bool)context.Variables["routeModelProbe"]))',
        })
        deny(model_lookup, '@((bool)context.Variables["routeModelProbe"] && (context.Request.Method != "HEAD" || context.Subscription == null || context.Subscription.Id != (string)context.Variables["routePublisherSubscription"]))', "route_model_probe_forbidden")
        variable(model_lookup, "routeAlias", r'''@{ try {
            var alias = (bool)context.Variables["routeModelProbe"]
                ? context.Request.Headers.GetValueOrDefault("x-turnstile-route-model", "")
                : (string)context.Request.Body.As<JObject>(preserveContent: true)["model"];
            alias = (alias ?? "").Trim();
            return System.Text.RegularExpressions.Regex.IsMatch(alias, @"\A[a-zA-Z0-9._:-]{1,255}\z")
                ? alias.ToLowerInvariant() : "";
        } catch { return ""; } }''')
        deny(model_lookup, '@(string.IsNullOrEmpty((string)context.Variables["routeAlias"]))')
        variable(model_lookup, "routeAliasHash", r'''@{
            using (var digest = System.Security.Cryptography.SHA256.Create()) {
                return BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes((string)context.Variables["routeAlias"])))
                    .Replace("-", "").ToLowerInvariant();
            }
        }''')
        table_read(model_lookup, "routeModelHeadRead", '(string)context.Variables["routePartition"]',
                   '"model:" + (string)context.Variables["routeAliasHash"]')
        deny(model_lookup, '@(context.Variables.GetValueOrDefault<IResponse>("routeModelHeadRead", null) == null || (((IResponse)context.Variables["routeModelHeadRead"]).StatusCode != 200 && ((IResponse)context.Variables["routeModelHeadRead"]).StatusCode != 404))')
        variable(model_lookup, "routeModelHeadSelected", '@(((IResponse)context.Variables["routeModelHeadRead"]).StatusCode == 200)')
        deny(model_lookup, '@((bool)context.Variables["routeModelProbe"] && !(bool)context.Variables["routeModelHeadSelected"])')
    for response, partition, row, name, expression in (
        (
            "routeHeadRead",
            '(string)context.Variables["routePartition"]',
            '"active"',
            "routeHead",
            READ_FIXED_HEAD,
        ),
        (
            "routeProofRead",
            '(string)context.Variables["routePartition"]',
            '(string)context.Variables["routeProofRow"]',
            "routeProof",
            READ_FIXED_PROOF,
        ),
    ):
        read_parent = branch
        if response == "routeHeadRead":
            read_parent = ET.SubElement(ET.SubElement(branch, "choose"), "when", {
                "condition": '@(string.IsNullOrEmpty((string)context.Variables["routeCandidateId"]) && !context.Variables.GetValueOrDefault<bool>("routeModelHeadSelected", false))',
            })
        table_read(read_parent, response, partition, row)
        variable(branch, name, expression)
        deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["' + name + '"]))')
    variable(branch, "routeCandidateExpires", r'''@{
        if (string.IsNullOrEmpty((string)context.Variables["routeCandidateId"])) { return 9007199254740991L; }
        var response = (IResponse)context.Variables["routeCandidateRead"];
        return (long)JObject.Parse((string)response.Body.As<JObject>(preserveContent: true)["Document"])["expires_epoch"];
    }''')
    variable(branch, "routeCandidateAllowsDispatch", r'''@{
        if (string.IsNullOrEmpty((string)context.Variables["routeCandidateId"])) { return true; }
        var response = (IResponse)context.Variables["routeCandidateRead"];
        return (bool)JObject.Parse((string)response.Body.As<JObject>(preserveContent: true)["Document"])["allow_dispatch"];
    }''')
    variable(branch, "routeCandidateMember", r'''@{
        if (string.IsNullOrEmpty((string)context.Variables["routeCandidateId"])) { return ""; }
        var response = (IResponse)context.Variables["routeCandidateRead"];
        return (string)JObject.Parse((string)response.Body.As<JObject>(preserveContent: true)["Document"])["member_backend_id"] ?? "";
    }''')
    configuration_probe = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@(context.Operation.Id == "storage-route-configuration-probe")',
        },
    )
    fixed_probe(configuration_probe, configuration=True)
    if binding.model_additions_enabled:
        model_response = configuration_probe.find("return-response")
        if model_response is None:
            raise PolicyCompilationError("Model probe confirmation response is missing")
        header_value(model_response, "x-turnstile-route-model", '@(context.Variables.GetValueOrDefault<string>("routeAlias", ""))')
    ET.SubElement(branch, "set-header", {
        "name": "x-turnstile-route-candidate", "exists-action": "delete",
    })
    ET.SubElement(branch, "set-header", {
        "name": "x-turnstile-pool-member", "exists-action": "delete",
    })
    ET.SubElement(branch, "set-header", {
        "name": "x-turnstile-pool-deployment", "exists-action": "delete",
    })
    branch.append(fixed_catalog(model_additions_enabled=binding.model_additions_enabled))
    branch = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@((bool)context.Variables["isInferenceOperation"] || context.Operation.Id == "anthropic-count-tokens")',
        },
    )
    variable(
        branch,
        "routeAlias",
        r"""@{ try {
        var request = context.Request.Body.As<JObject>(preserveContent: true);
        if (request["model"]?.Type != JTokenType.String) { return ""; }
        var alias = ((string)request["model"]).Trim();
        return System.Text.RegularExpressions.Regex.IsMatch(alias, @"\A[a-zA-Z0-9._:-]{1,255}\z")
            ? alias.ToLowerInvariant() : "";
    } catch { return ""; } }""",
    )
    deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeAlias"]))')
    variable(
        branch,
        "routeAliasHash",
        r"""@{ using(var digest = System.Security.Cryptography.SHA256.Create()) {
        return BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes((string)context.Variables["routeAlias"])))
            .Replace("-", "").ToLowerInvariant(); } }""",
    )
    table_read(
        branch,
        "routeMemberRead",
        '(string)context.Variables["routePartition"] + ":" + (string)JObject.Parse((string)context.Variables["routeHead"])["approval"]["route_version"]',
        '(string)context.Variables["routeAliasHash"]',
    )
    variable(branch, "routeMember", (
        READ_FIXED_MEMBER_METADATA if binding.model_additions_enabled else READ_FIXED_MEMBER
    ))
    deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeMember"]))')
    if binding.model_additions_enabled:
        variable(branch, "routeApprovalRow", r'''@{
            var member = JObject.Parse((string)context.Variables["routeMember"]);
            using (var digest = System.Security.Cryptography.SHA256.Create()) {
                return "approval:" + BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(
                    (string)member["content_sha256"] + (string)member["approval_sha256"])))
                    .Replace("-", "").ToLowerInvariant();
            }
        }''')
        table_read(branch, "routeApprovalRead", '(string)context.Variables["routePartition"]',
                   '(string)context.Variables["routeApprovalRow"]')
        variable(branch, "routeMember", READ_RENEWED_APPROVAL)
        deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeMember"]))')
    variable(
        branch,
        "routeCacheKey",
        '@((string)context.Variables["routeStore"] + ":" + (string)context.Variables["routeTable"] + ":" + (string)context.Variables["routePartition"] + ":" + (string)context.Variables["routeProgram"] + ":" + (string)JObject.Parse((string)context.Variables["routeMember"])["content_sha256"])',
    )
    ET.SubElement(
        branch,
        "cache-lookup-value",
        {
            "key": '@((string)context.Variables["routeCacheKey"])',
            "variable-name": "routeCachedContent",
            "default-value": "",
            "caching-type": "internal",
        },
    )
    cold = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": '@(string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeCachedContent", "")))'
        },
    )
    table_read(
        cold,
        "routeContentRead",
        '(string)context.Variables["routePartition"] + ":content"',
        '(string)JObject.Parse((string)context.Variables["routeMember"])["content_sha256"]',
    )
    variable(branch, "routeContent", READ_FIXED_CONTENT)
    deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeContent"]))')
    variable(branch, "routeSelectedBackend", SELECT_ROUTE_BACKEND)
    deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeSelectedBackend"]))')
    ET.SubElement(
        branch,
        "cache-store-value",
        {
            "key": '@((string)context.Variables["routeCacheKey"])',
            "value": '@((string)context.Variables["routeContent"])',
            "duration": str(binding.cache_seconds),
            "caching-type": "internal",
        },
    )
    return root


def reader_expressions(binding: FixedRouteBinding) -> set[str]:
    roots = [fixed_reader(binding), route_metadata(binding), count_credential_stage()]
    roots.extend(
        parse_policy(fixed_operation(operation, binding)) for operation in FIXED_OPERATIONS
        if operation != "microsoft-decision" or binding.decision_enabled
    )
    return {
        value
        for root in roots
        for node in root.iter()
        for value in [*node.attrib.values(), node.text or ""]
        if value.startswith("@")
    }


def route_metadata(binding: FixedRouteBinding) -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(
        root, "when", {"condition": '@((bool)context.Variables["isInferenceOperation"] || context.Operation.Id == "anthropic-count-tokens")'}
    )
    route = 'JObject.Parse((string)context.Variables["routeContent"])'
    for name, field in (
        ("selectedBudgetModelKey", "alias"),
        ("selectedProviderName", "provider_name"),
        ("selectedRuntimeName", "runtime_name"),
        ("selectedBudgetUpstreamModel", "upstream_model"),
        ("availabilityModelId", "model_id"),
    ):
        variable(branch, name, f'@((string){route}["{field}"])')
    for name, value in (
        ("selectedModelKnown", "@(true)"),
        ("selectedRoutingManaged", "@(true)"),
        ("selectedContextWindow", f'@((long){route}["context_window"])'),
        ("selectedRequiresAssignment", f'@((bool){route}["assignment_required"])'),
        ("selectedBudgetObserver", f'@((bool){route}["connection"]["observer"])'),
        ("providerName", '@((string)context.Variables["selectedProviderName"])'),
        ("availabilityTargetId", binding.scope.target_id),
        ("availabilityApiId", binding.scope.api_id),
        ("availabilityApiRevision", binding.api_revision),
    ):
        variable(branch, name, value)
    images = ET.SubElement(ET.SubElement(branch, "choose"), "when", {
        "condition": '@(context.Operation.Id == "images-generations")',
    })
    capacity = ET.SubElement(ET.SubElement(images, "choose"), "when", {
        "condition": f'@(context.Request.Body.As<byte[]>(preserveContent: true).Length > (long){route}["image_profile"]["max_request_bytes"])',
    })
    response = ET.SubElement(capacity, "return-response")
    ET.SubElement(response, "set-status", {"code": "413", "reason": "Payload Too Large"})
    variable(images, "imageOutputBound", f'@((long){route}["image_profile"]["output_reservation_tokens"])')
    variable(
        branch,
        "selectedBudgetCountContract",
        r"""@{
        var route = JObject.Parse((string)context.Variables["routeContent"]);
        return new JObject(new JProperty("version", 1), new JProperty("epoch", route["budget_epoch"]),
            new JProperty("gateway_profile_id", route["scope"]["target_id"]), new JProperty("model_key", route["alias"]),
            new JProperty("contract", route["budget"].DeepClone())).ToString(Newtonsoft.Json.Formatting.None);
    }""",
    )
    for name, field in (
        ("selectedBudgetCounterOrigin", "origin"),
        ("selectedBudgetCounterHeader", "header"),
        ("selectedBudgetCounterAudience", "audience"),
    ):
        variable(branch, name, f'@((string)({route}["connection"]["count"] as JObject)?["{field}"] ?? "")')
    variable(
        branch,
        "selectedBudgetCounterAuth",
        r"""@{
        var counter = JObject.Parse((string)context.Variables["routeContent"])["connection"]["count"] as JObject;
        var mode = (string)counter?["mode"] ?? "";
        return mode == "key_vault_api_key" ? "named_value_api_key"
            : mode == "key_vault_bearer" || mode == "oauth_client_credentials" ? "named_value_bearer" : mode;
    }""",
    )
    header = ET.SubElement(
        branch, "set-header", {"name": "x-hive-runtime", "exists-action": "override"}
    )
    ET.SubElement(header, "value").text = '@((string)context.Variables["selectedRuntimeName"])'
    variable(branch, "routeRequestedStream", NATIVE_STREAM_REQUEST)
    buffered = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": f'@((bool)context.Variables["isInferenceOperation"] && context.Operation.Id != "images-generations" && context.Operation.Id != "microsoft-decision" && (string){route}["streaming_mode"] == "buffered")',
        },
    )
    ET.SubElement(buffered, "set-body").text = r"""@{
        var body = context.Request.Body.As<JObject>(preserveContent: true);
        body["stream"] = false;
        return body.ToString(Newtonsoft.Json.Formatting.None);
    }"""
    return root


READ_FIXED_SECRET = r"""@{ try {
    var response = context.Variables.GetValueOrDefault<IResponse>("routeCredentialRead", null);
    if (response == null || response.StatusCode != 200) { return ""; }
    var document = response.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(document) > 32768) { return ""; }
    var secret = JObject.Parse(document);
    var attributes = secret["attributes"] as JObject;
    if (secret["id"]?.Type != JTokenType.String
        || (string)secret["id"] != (string)context.Variables["routeCredentialSecretId"]
        || attributes == null || attributes["enabled"]?.Type != JTokenType.Boolean
        || !(bool)attributes["enabled"] || secret["value"]?.Type != JTokenType.String) { return ""; }
    var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
    foreach (var field in new [] {"nbf", "exp"}) {
        if (attributes[field] == null || attributes[field].Type == JTokenType.Null) { continue; }
        if (attributes[field].Type != JTokenType.Integer || (long)attributes[field] < 0
            || (field == "nbf" && now < (long)attributes[field])
            || (field == "exp" && now >= (long)attributes[field])) { return ""; }
    }
    var value = (string)secret["value"];
    if (string.IsNullOrWhiteSpace(value) || value.Length > 8192
        || value.Any(character => char.IsControl(character))) { return ""; }
    return value;
} catch { return ""; } }"""


def header_value(parent: ET.Element, name: str, value: str) -> None:
    header = ET.SubElement(parent, "set-header", {"name": name, "exists-action": "override"})
    ET.SubElement(header, "value").text = value


def credential_read(parent: ET.Element, kind: str, result: str) -> None:
    expression = (
        'JObject.Parse((string)context.Variables["routeContent"])["connection"]["' + kind + '"]'
    )
    branch = ET.SubElement(
        ET.SubElement(parent, "choose"),
        "when",
        {
            "condition": "@((string)"
            + expression
            + '?["mode"] == "key_vault_api_key" || (string)'
            + expression
            + '?["mode"] == "key_vault_bearer")'
        },
    )
    variable(branch, "routeCredentialSecretId", "@((string)" + expression + '["secret_id"])')
    versioned_secret_read(branch, result)


def versioned_secret_read(branch: ET.Element, result: str) -> None:
    request = ET.SubElement(
        branch,
        "send-request",
        {
            "mode": "new",
            "response-variable-name": "routeCredentialRead",
            "timeout": "2",
            "ignore-error": "true",
        },
    )
    ET.SubElement(
        request, "set-url"
    ).text = '@((string)context.Variables["routeCredentialSecretId"] + "?api-version=7.4")'
    ET.SubElement(request, "set-method").text = "GET"
    header_value(request, "Accept", "application/json")
    ET.SubElement(
        request, "authentication-managed-identity", {"resource": "https://vault.azure.net"}
    )
    variable(branch, result, READ_FIXED_SECRET)
    variable(
        branch,
        result + "Expires",
        r"""@{
        try {
            var response = (IResponse)context.Variables["routeCredentialRead"];
            var attributes = response.Body.As<JObject>(preserveContent: true)["attributes"];
            return attributes["exp"]?.Type == JTokenType.Integer ? (long)attributes["exp"] : 9007199254740991L;
        } catch { return 0L; }
    }""",
    )
    deny(
        branch,
        '@(string.IsNullOrEmpty((string)context.Variables["' + result + '"]))',
        "route_credential_unavailable",
    )


def oauth_credential_read(parent: ET.Element, kind: str, result: str) -> None:
    credential = f'JObject.Parse((string)context.Variables["routeContent"])["connection"]["{kind}"]'
    branch = ET.SubElement(ET.SubElement(parent, "choose"), "when", {
        "condition": f'@((string)({credential} as JObject)?["mode"] == "oauth_client_credentials")',
    })
    context_name = result + "Authorization"
    ET.SubElement(branch, "get-authorization-context", {
        "provider-id": f'@((string){credential}["oauth"]["provider_id"])',
        "authorization-id": f'@((string){credential}["oauth"]["authorization_id"])',
        "context-variable-name": context_name, "identity-type": "managed", "ignore-error": "true",
    })
    variable(branch, result, (
        f'@((context.Variables.GetValueOrDefault<Authorization>("{context_name}", null))'
        '?.AccessToken ?? "")'
    ))
    deny(
        branch,
        f'@(string.IsNullOrWhiteSpace(context.Variables.GetValueOrDefault<string>("{result}", "")))',
        "route_credential_unavailable",
    )


def pool_credential_read(parent: ET.Element) -> None:
    pool = 'JObject.Parse((string)context.Variables["routeContent"])["connection"]["pool"]'
    branch = ET.SubElement(ET.SubElement(parent, "choose"), "when", {
        "condition": f'@({pool} is JObject && ((JObject){pool}["credential_versions"]).Count > 0)',
    })
    variable(branch, "routePoolCredentialVersions", f'''@{{
        var versions = (JObject){pool}["credential_versions"];
        return new JArray(versions.Properties().Select(property => (string)property.Value)
            .Distinct().OrderBy(value => value)).ToString(Newtonsoft.Json.Formatting.None);
    }}''')
    variable(branch, "routePoolCredentialIndex", "@(0L)")
    variable(branch, "routePoolKeysExpires", "@(9007199254740991L)")
    loop = ET.SubElement(branch, "retry", {
        "count": "29", "interval": "0", "first-fast-retry": "true",
        "condition": '@((long)context.Variables["routePoolCredentialIndex"] < JArray.Parse((string)context.Variables["routePoolCredentialVersions"]).Count)',
    })
    variable(loop, "routeCredentialSecretId", '@((string)JArray.Parse((string)context.Variables["routePoolCredentialVersions"])[(int)(long)context.Variables["routePoolCredentialIndex"]])')
    versioned_secret_read(loop, "routePoolKey")
    variable(loop, "routePoolKeysExpires", '@(Math.Min((long)context.Variables["routePoolKeysExpires"], (long)context.Variables["routePoolKeyExpires"]))')
    variable(loop, "routePoolKey", "")
    variable(loop, "routePoolCredentialIndex", '@((long)context.Variables["routePoolCredentialIndex"] + 1L)')
    deny(branch, '@((long)context.Variables["routePoolCredentialIndex"] != JArray.Parse((string)context.Variables["routePoolCredentialVersions"]).Count)', "route_pool_credentials_incomplete")
    branch.append(route_dispatch_guard())


def count_credential_stage() -> ET.Element:
    holder = ET.Element("inbound")
    deny(holder, '@(!context.Variables.GetValueOrDefault<bool>("routeCandidateAllowsDispatch", true))', "route_disabled_probe_changed")
    root = holder[0]
    branch = ET.SubElement(
        root,
        "when",
        {"condition": '@((bool)((JObject)context.Variables["executionCountPlan"])["enabled"])'},
    )
    credential_read(branch, "count", "selectedBudgetCounterKey")
    oauth_credential_read(branch, "count", "selectedBudgetCounterKey")
    branch.append(route_dispatch_guard())
    return root


ROUTE_DISPATCH_READY = r"""@{ try {
    var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
    var proof = JObject.Parse((string)context.Variables["routeProof"]);
    if ((long)proof["expires_epoch"] <= (long)proof["issued_epoch"]) { return false; }
    foreach (var name in new [] {"routeProviderKeyExpires", "selectedBudgetCounterKeyExpires", "routeCandidateExpires", "routePoolKeysExpires"}) {
        if (context.Variables.ContainsKey(name) && now >= (long)context.Variables[name]) { return false; }
    }
    return true;
} catch { return false; } }"""


def route_dispatch_guard() -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(root, "when", {"condition": "@(true)"})
    variable(branch, "routeDispatchReady", ROUTE_DISPATCH_READY)
    deny(branch, '@(!(bool)context.Variables["routeDispatchReady"])', "route_authorization_expired")
    return root


def refresh_fixed_connection_approval_policy(
    source: str, binding: FixedRouteBinding | None = None,
) -> str:
    root = parse_policy(source)
    for guard in root.findall(".//set-variable[@name='routeDispatchReady']"):
        guard.set("value", ROUTE_DISPATCH_READY)
    if binding is not None:
        inbound = root.find("inbound")
        marker = root.find("inbound/set-variable[@name='storageRouteAbi']")
        if inbound is None or marker is None or marker.get("value") != "2":
            raise PolicyCompilationError("Connection approval refresh requires a fixed program")
        position = list(inbound).index(marker)
        if len(inbound) <= position + 2:
            raise PolicyCompilationError("Connection approval fixed reader is unavailable")
        inbound[position + 1] = fixed_reader(binding)
        inbound[position + 2] = route_metadata(binding)
        candidate = serialize_policy(root)
        validate_policy_upload(
            candidate, gateway_profile_id=UUID(binding.scope.target_id),
            api_id=binding.scope.api_id, api_revision=binding.api_revision,
            table_endpoint=binding.ledger_table_endpoint, table_name=binding.ledger_table_name,
        )
        return candidate
    return serialize_policy(root)


def provider_credential_stage(binding: FixedRouteBinding) -> ET.Element:
    root = ET.Element("choose")
    branch = ET.SubElement(root, "when", {"condition": "@(true)"})
    deny(branch, '@(!context.Variables.GetValueOrDefault<bool>("routeCandidateAllowsDispatch", true))', "route_disabled_probe_changed")
    branch.append(route_dispatch_guard())
    route = 'JObject.Parse((string)context.Variables["routeContent"])'
    if binding.observer_key_named_value is None:
        deny(branch, f'@((bool){route}["connection"]["observer"])', "route_observer_unconfigured")
    credential_read(branch, "credential", "routeProviderKey")
    oauth_credential_read(branch, "credential", "routeProviderKey")
    if binding.pool_enabled:
        pool_credential_read(branch)
    managed = ET.SubElement(
        ET.SubElement(branch, "choose"),
        "when",
        {
            "condition": f'@((string){route}["connection"]["credential"]["mode"] == "managed_identity")',
        },
    )
    ET.SubElement(
        managed,
        "authentication-managed-identity",
        {
            "resource": f'@((string){route}["connection"]["credential"]["audience"])',
            "output-token-variable-name": "routeProviderToken",
            "ignore-error": "true",
        },
    )
    deny(
        managed,
        '@(string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeProviderToken", "")))',
        "route_credential_unavailable",
    )
    return root


FIXED_OPERATIONS: Mapping[str, tuple[str, str]] = {
    "chat-completions": ("POST", "/chat/completions"),
    "responses": ("POST", "/responses"),
    "responses-compact": ("POST", "/responses/compact"),
    "anthropic-messages": ("POST", "/messages"),
    "anthropic-count-tokens": ("POST", "/messages/count_tokens"),
    "images-generations": ("POST", "/images/generations"),
    "microsoft-decision": ("POST", "/providers/microsoft/v1/systemone"),
}

REWRITE_FIXED_MODEL = r"""@{
    var route = JObject.Parse((string)context.Variables["routeContent"]);
    var body = context.Request.Body.As<JObject>(preserveContent: true);
    if ((string)route["request_adapter"] == "bedrock-anthropic-v1") {
        body.Remove("model"); body.Remove("stream"); body.Remove("context_management");
        body["anthropic_version"] = "bedrock-2023-05-31";
        return body.ToString(Newtonsoft.Json.Formatting.None);
    }
    body["model"] = (string)route["upstream_model"];
    if (context.Operation.Id == "chat-completions" && (bool?)body["stream"] == true) {
        var options = body["stream_options"] as JObject ?? new JObject();
        options["include_usage"] = true;
        body["stream_options"] = options;
    }
    return body.ToString(Newtonsoft.Json.Formatting.None);
}"""

POOL_RATE_LIMITED_BODY = canonical_document({"type": "error", "error": {
    "type": "rate_limit_error", "code": "rate_limit_exceeded", "param": None,
    "message": "Every deployment for this model is rate limited; retry after Retry-After",
}})


def observer_headers(parent: ET.Element) -> None:
    for name, value in (
        ("x-turnstile-correlation-id", "@(context.RequestId.ToString())"),
        ("x-turnstile-event-schema-version", "2"),
        ("x-provider-name", '@((string)context.Variables["selectedProviderName"])'),
        ("x-hive-runtime", '@((string)context.Variables["selectedRuntimeName"])'),
        (
            "x-turnstile-api-format",
            '@(context.Operation.Id == "anthropic-messages" ? "anthropic_messages" : context.Operation.Id == "chat-completions" ? "openai_chat" : "openai_responses")',
        ),
        (
            "x-turnstile-gateway-is-managed",
            '@(((bool)context.Variables["telemetryGatewayIsManaged"]).ToString().ToLowerInvariant())',
        ),
        (
            "x-turnstile-upstream-host",
            '@(new Uri((string)JObject.Parse((string)context.Variables["routeContent"])["connection"]["origin"]).Host)',
        ),
    ):
        header_value(parent, name, value)
    for name, variable_name in {
        "x-request-id": "callerRequestId",
        "x-org-name": "telemetryOrganization",
        "x-org-id": "telemetryOrganizationId",
        "x-department-name": "telemetryDepartment",
        "x-department-id": "telemetryDepartmentId",
        "x-project-name": "telemetryProject",
        "x-project-id": "telemetryProjectId",
        "x-agent-name": "telemetryAgent",
        "x-agent-id": "telemetryAgentId",
        "x-user-name": "telemetryUser",
        "x-user-id": "telemetryUserId",
        "x-hive-workflow": "telemetryWorkflow",
        "x-hive-run-id": "telemetryRunId",
        "x-hive-turn-index": "telemetryTurnIndex",
        "x-hive-model": "telemetryModel",
        "x-model-id": "executionModelId",
        "x-request-source": "telemetryRequestSource",
        "x-turnstile-usage-id": "telemetryUsageId",
        "x-turnstile-gateway-profile-id": "telemetryGatewayProfileId",
        "x-turnstile-native-gateway-id": "telemetryNativeGatewayId",
        "x-turnstile-native-gateway-instance-id": "telemetryNativeGatewayInstanceId",
        "x-turnstile-apim-service-id": "telemetryApimServiceId",
        "x-turnstile-apim-region": "telemetryApimRegion",
        "x-turnstile-api-revision": "telemetryApiRevision",
        "x-turnstile-budget-execution": "executionReceiptHeader",
        "x-turnstile-apim-subscription-id": "telemetryApimSubscriptionId",
        "x-turnstile-application-actor-type": "telemetryApplicationActorType",
        "x-turnstile-application-actor-id": "telemetryApplicationActorId",
    }.items():
        header_value(
            parent, name, f'@(context.Variables.GetValueOrDefault<string>("{variable_name}", ""))'
        )


def fixed_operation(
    operation: str, binding: FixedRouteBinding, *, logger_id: str = "__LOGGER_ID__",
    usage_identity: str = "__ROUTER_USAGE_IDENTITY__",
) -> str:
    if operation not in FIXED_OPERATIONS or (
        operation == "microsoft-decision" and not binding.decision_enabled
    ):
        raise PolicyCompilationError("Unsupported fixed route operation")
    root = ET.Element("policies")
    inbound = ET.SubElement(root, "inbound")
    router_outbound = None
    if binding.router_enabled and operation == "chat-completions":
        from .apim_router_global_policy import fixed_router_operation_stages

        router_inbound, router_outbound = fixed_router_operation_stages(
            binding.ledger_table_endpoint, binding.ledger_table_name, binding.scope.api_id,
            binding.scope.target_id, logger_id=logger_id, usage_identity=usage_identity,
        )
        variable(inbound, "turnstileRouterEngineVersion", "@(2)")
        inbound.append(router_inbound)
    ET.SubElement(inbound, "base")
    if operation == "images-generations" and binding.image_limits is None:
        deny(inbound, "@(true)", "route_image_capacity_unavailable")
    deny(
        inbound,
        '@(string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeContent", "")))',
    )
    inbound.append(route_dispatch_guard())
    route = 'JObject.Parse((string)context.Variables["routeContent"])'
    if operation == "anthropic-count-tokens":
        from ..domain.control_plane import COUNT_TOKENS_REJECTIONS

        unavailable = ET.SubElement(ET.SubElement(inbound, "choose"), "when", {
            "condition": f'@(string.IsNullOrEmpty((string){route}["connection"]["public_count_path"]))',
        })
        rejection = COUNT_TOKENS_REJECTIONS["unavailable"]
        response = ET.SubElement(unavailable, "return-response")
        ET.SubElement(response, "set-status", {
            "code": str(rejection.status_code), "reason": rejection.reason,
        })
        header_value(response, "Content-Type", "application/json")
        header_value(response, "Cache-Control", "no-store")
        header_value(response, "x-turnstile-route-candidate", '@(context.Variables.GetValueOrDefault<string>("routeCandidateId", ""))')
        header_value(response, "x-turnstile-route-request", '@(context.Variables.GetValueOrDefault<string>("callerRequestId", ""))')
        header_value(response, "x-turnstile-route-version", '@((string)JObject.Parse((string)context.Variables["routeHead"])["approval"]["route_version"])')
        if binding.pool_enabled:
            header_value(response, "x-turnstile-route-member", '@(context.Variables.GetValueOrDefault<string>("routeCandidateMember", ""))')
        ET.SubElement(response, "set-body").text = canonical_document(rejection.payload())
        inbound.append(provider_credential_stage(binding))
    auth = route + '["connection"]["credential"]'
    for name in (
        "Authorization",
        "api-key",
        "x-api-key",
        "Ocp-Apim-Subscription-Key",
        "x-adapter-key",
    ):
        ET.SubElement(inbound, "set-header", {"name": name, "exists-action": "delete"})
    managed = ET.SubElement(
        ET.SubElement(inbound, "choose"),
        "when",
        {"condition": f'@((string){auth}["mode"] == "managed_identity")'},
    )
    header_value(
        managed, "Authorization", '@("Bearer " + (string)context.Variables["routeProviderToken"])'
    )
    choices = ET.SubElement(inbound, "choose")
    for mode, header in (
        ("key_vault_api_key", "api-key"),
        ("key_vault_api_key", "x-api-key"),
        ("key_vault_bearer", "Authorization"),
        ("oauth_client_credentials", "Authorization"),
    ):
        branch = ET.SubElement(
            choices,
            "when",
            {
                "condition": f'@((string){auth}["mode"] == "{mode}" && (string){auth}["header"] == "{header}")'
            },
        )
        prefix = '"Bearer " + ' if header == "Authorization" else ""
        header_value(
            branch, header, "@(" + prefix + '(string)context.Variables["routeProviderKey"])'
        )
    ET.SubElement(
        inbound,
        "set-backend-service",
        {"backend-id": '@((string)context.Variables["routeSelectedBackend"])'},
    )
    ET.SubElement(
        inbound,
        "rewrite-uri",
        {
            "template": (
                f'@((string){route}["connection"]["public_count_path"])'
                if operation == "anthropic-count-tokens" else
                f'@((string){route}["connection"]["protocol_paths"][context.Operation.Id] ?? (string){route}["connection"]["path"])'
            ),
            "copy-unmatched-params": "false",
        },
    )
    headers = ET.SubElement(inbound, "choose")
    observer = ET.SubElement(
        headers, "when", {"condition": f'@((bool){route}["connection"]["observer"])'}
    )
    if binding.observer_key_named_value is None:
        deny(observer, "@(true)", "route_observer_unconfigured")
    else:
        header_value(observer, "x-adapter-key", "{{" + binding.observer_key_named_value + "}}")
        observer_headers(observer)
    direct = ET.SubElement(headers, "when", {
        "condition": f'@({route}["connection"]["pool"] == null)',
    }) if binding.pool_enabled else ET.SubElement(headers, "otherwise")
    header_value(direct, "Host", f'@(new Uri((string){route}["connection"]["origin"]).Host)')
    if operation in {"anthropic-messages", "anthropic-count-tokens"}:
        header_value(inbound, "anthropic-version", "2023-06-01")
    if operation == "anthropic-count-tokens":
        header_value(inbound, "x-turnstile-api-format", "anthropic_count_tokens")
    header_value(inbound, "Accept-Encoding", "identity")
    if operation == "images-generations":
        version = ET.SubElement(inbound, "set-query-parameter", {
            "name": "api-version", "exists-action": "override",
        })
        ET.SubElement(version, "value").text = "preview"
    ET.SubElement(inbound, "set-body").text = REWRITE_FIXED_MODEL
    backend = ET.SubElement(root, "backend")
    destination = backend
    if binding.pool_enabled and operation != "images-generations":
        choices = ET.SubElement(backend, "choose")
        pooled = ET.SubElement(choices, "when", {
            "condition": f'@({route}["connection"]["pool"] is JObject)',
        })
        # A 429 consumed no tokens, so a budgeted request retries within its one reservation.
        retry_node = ET.SubElement(pooled, "retry", {
            "condition": "@(context.Response != null && context.Response.StatusCode == 429)",
            "count": "1",
            "interval": "1",
            "first-fast-retry": "true",
        })
        # APIM keeps the previous member's backend headers; only some members set these.
        for name in ("x-turnstile-pool-deployment", "api-key", "x-api-key"):
            ET.SubElement(retry_node, "set-header", {"name": name, "exists-action": "delete"})
        ET.SubElement(retry_node, "forward-request", {
            "timeout": "120", "buffer-request-body": "true",
            "buffer-response": "true" if operation == "microsoft-decision" else "false",
            "fail-on-error-status-code": "false",
        })
        destination = ET.SubElement(choices, "otherwise")
    ET.SubElement(
        destination,
        "forward-request",
        {
            "buffer-request-body": "true",
            "buffer-response": (
                "true" if operation in {"images-generations", "microsoft-decision"} else "false"
            ),
            "fail-on-error-status-code": "false",
            **({"timeout": str(binding.image_limits.timeout_seconds) if binding.image_limits else "1"}
               if operation == "images-generations" else {}),
        },
    )
    outbound = ET.SubElement(root, "outbound")
    ET.SubElement(outbound, "base")
    if router_outbound is not None:
        outbound.extend(list(router_outbound))
    if binding.pool_enabled and operation != "images-generations":
        # The provider's 429 body names the member deployment and region.
        limited = ET.SubElement(ET.SubElement(outbound, "choose"), "when", {
            "condition": f'@(context.Response.StatusCode == 429 && {route}["connection"]["pool"] is JObject)',
        })
        header_value(limited, "Content-Type", "application/json")
        ET.SubElement(limited, "set-body").text = POOL_RATE_LIMITED_BODY
    if operation == "anthropic-messages":
        from .apim_policy_compiler import ApimPolicyCompiler

        buffered = ET.SubElement(ET.SubElement(outbound, "choose"), "when", {
            "condition": '@(context.Variables.GetValueOrDefault<bool>("routeRequestedStream", false) && (string)JObject.Parse((string)context.Variables["routeContent"])["streaming_mode"] == "buffered" && context.Response.StatusCode < 400)',
        })
        adapter = parse_policy(
            "<policies>" + ApimPolicyCompiler._buffered_sse_body().replace(
                "dynamicRequestedModel", "routeAlias",
            ) + "</policies>",
        )
        buffered.extend(list(adapter))
    for name in (
        "x-turnstile-route-candidate", "x-turnstile-route-request", "x-turnstile-route-version",
    ) + (("x-turnstile-route-member",) if binding.pool_enabled else ()):
        ET.SubElement(outbound, "set-header", {"name": name, "exists-action": "delete"})
    candidate = ET.SubElement(ET.SubElement(outbound, "choose"), "when", {
        "condition": '@(!string.IsNullOrEmpty(context.Variables.GetValueOrDefault<string>("routeCandidateId", "")))',
    })
    header_value(candidate, "x-turnstile-route-candidate", '@((string)context.Variables["routeCandidateId"])')
    header_value(candidate, "x-turnstile-route-request", '@((string)context.Variables["callerRequestId"])')
    header_value(candidate, "x-turnstile-route-version", '@((string)JObject.Parse((string)context.Variables["routeHead"])["approval"]["route_version"])')
    if binding.pool_enabled:
        header_value(candidate, "x-turnstile-route-member", '@(context.Variables.GetValueOrDefault<string>("routeCandidateMember", ""))')
    ET.SubElement(ET.SubElement(root, "on-error"), "base")
    return serialize_policy(root)


CATALOG_PAGE_URL = r"""@{
    var page = JObject.Parse((string)context.Variables["routeCatalogPage"]);
    var head = JObject.Parse((string)context.Variables["routeHead"]);
    var partition = (string)context.Variables["routePartition"] + ":" + (string)head["approval"]["route_version"] + ":catalog";
    var url = (string)context.Variables["routeStore"] + "/" + (string)context.Variables["routeTable"]
        + "()?$filter=" + Uri.EscapeDataString("PartitionKey eq '" + partition + "'") + "&$top=1000";
    if (page["next_partition"]?.Type == JTokenType.String) {
        url += "&NextPartitionKey=" + Uri.EscapeDataString((string)page["next_partition"]);
    }
    if (page["next_row"]?.Type == JTokenType.String) {
        url += "&NextRowKey=" + Uri.EscapeDataString((string)page["next_row"]);
    }
    return url;
}"""

READ_CATALOG_PAGE = r"""@{ try {
    var page = JObject.Parse((string)context.Variables["routeCatalogPage"]);
    var reply = context.Variables.GetValueOrDefault<IResponse>("routeCatalogRead", null);
    if ((bool?)page["ok"] != true || (int)page["pages"] >= 10 || reply == null || reply.StatusCode != 200) { return "{\"ok\":false}"; }
    var body = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(body) > 1048576) { return "{\"ok\":false}"; }
    var entities = JObject.Parse(body)["value"] as JArray;
    if (entities == null || entities.Count > 1000) { return "{\"ok\":false}"; }
    var head = JObject.Parse((string)context.Variables["routeHead"]);
    var partition = (string)context.Variables["routePartition"] + ":" + (string)head["approval"]["route_version"] + ":catalog";
    var items = (JObject)page["items"];
    var aliases = new HashSet<string>(items.Properties().Select(property => property.Name), StringComparer.OrdinalIgnoreCase);
    foreach (var token in entities) {
        var entity = token as JObject;
        if (entity == null || (string)entity["PartitionKey"] != partition
            || entity["SchemaVersion"]?.Type != JTokenType.Integer || (int)entity["SchemaVersion"] != 2
            || entity["Document"]?.Type != JTokenType.String || entity["Sha256"]?.Type != JTokenType.String
            || entity["RowKey"]?.Type != JTokenType.String) { return "{\"ok\":false}"; }
        var document = (string)entity["Document"];
        if (Encoding.UTF8.GetByteCount(document) > 4096) { return "{\"ok\":false}"; }
        var model = new JObject(JsonConvert.DeserializeObject<Dictionary<string, string>>(document)
            .Select(field => new JProperty(field.Key, field.Value)));
        var names = new [] {"id","type","display_name","created_at","logical_model_id"};
        if (model.Properties().Count() != 5 || model.Properties().Any(property => !names.Contains(property.Name))
            || model.ToString(Newtonsoft.Json.Formatting.None) != document || (string)model["type"] != "model"
            || model["id"]?.Type != JTokenType.String || !System.Text.RegularExpressions.Regex.IsMatch((string)model["id"], @"\A[a-zA-Z0-9._:-]{1,255}\z")
            || model["display_name"]?.Type != JTokenType.String || string.IsNullOrWhiteSpace((string)model["display_name"])
            || ((string)model["display_name"]).Length > 255 || !aliases.Add((string)model["id"])) { return "{\"ok\":false}"; }
        Guid modelId;
        if (model["logical_model_id"]?.Type != JTokenType.String || !Guid.TryParseExact((string)model["logical_model_id"], "D", out modelId)
            || modelId.ToString() != (string)model["logical_model_id"]
            || (model["created_at"]?.Type != JTokenType.Date && model["created_at"]?.Type != JTokenType.String)) { return "{\"ok\":false}"; }
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(document))).Replace("-", "").ToLowerInvariant()
                != (string)entity["Sha256"] || BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(((string)model["id"]).ToLowerInvariant())))
                    .Replace("-", "").ToLowerInvariant() != (string)entity["RowKey"]) { return "{\"ok\":false}"; }
        }
        items[(string)model["id"]] = document;
    }
    if (items.Properties().Count() > (int)head["approval"]["entry_count"]
        || Encoding.UTF8.GetByteCount(items.ToString(Newtonsoft.Json.Formatting.None)) > 2097152) { return "{\"ok\":false}"; }
    var nextPartition = reply.Headers.GetValueOrDefault("x-ms-continuation-NextPartitionKey", "");
    var nextRow = reply.Headers.GetValueOrDefault("x-ms-continuation-NextRowKey", "");
    if (nextPartition.Length > 1024 || nextRow.Length > 1024 || (nextPartition == "" && nextRow != "")
        || (nextPartition != "" && nextPartition == (string)page["next_partition"] && nextRow == (string)page["next_row"])) { return "{\"ok\":false}"; }
    return new JObject(new JProperty("ok", true), new JProperty("pages", (int)page["pages"] + 1),
        new JProperty("items", items), new JProperty("next_partition", nextPartition == "" ? null : nextPartition),
        new JProperty("next_row", nextRow == "" ? null : nextRow)).ToString(Newtonsoft.Json.Formatting.None);
} catch { return "{\"ok\":false}"; } }"""

CHECK_CATALOG = r"""@{ try {
    var page = JObject.Parse((string)context.Variables["routeCatalogPage"]);
    var approval = JObject.Parse((string)context.Variables["routeHead"])["approval"];
    var items = page["items"] as JObject;
    if ((bool?)page["ok"] != true || page["next_partition"]?.Type != JTokenType.Null
        || page["next_row"]?.Type != JTokenType.Null || (int)page["pages"] < 1 || (int)page["pages"] > 10
        || items == null || items.Properties().Count() != (int)approval["entry_count"]) { return ""; }
    if (items.Properties().Any(property => property.Value.Type != JTokenType.String)) { return ""; }
    var sorted = new JObject(items.Properties().OrderBy(property => property.Name, StringComparer.Ordinal)
        .Select(property => new JProperty(property.Name, new JRaw((string)property.Value))));
    using (var digest = System.Security.Cryptography.SHA256.Create()) {
        if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(sorted.ToString(Newtonsoft.Json.Formatting.None))))
            .Replace("-", "").ToLowerInvariant() != (string)approval["catalog_sha256"]) { return ""; }
    }
    return new JArray(sorted.Properties().Select(property => property.Value)).ToString(Newtonsoft.Json.Formatting.None);
} catch { return ""; } }"""

FILTER_FIXED_CATALOG = r"""@{ try {
    var page = JObject.Parse((string)context.Variables["availabilityCatalogPage"]);
    if ((bool?)page["ok"] != true || page["next_partition"]?.Type != JTokenType.Null
        || page["next_row"]?.Type != JTokenType.Null || (int)page["pages"] < 1 || (int)page["pages"] > 4) { return ""; }
    var states = (JObject)page["states"];
    var entries = new JArray(JsonConvert.DeserializeObject<List<Dictionary<string, string>>>(
        (string)context.Variables["routeCatalogEntries"]).Select(entry => new JObject(
            entry.Select(field => new JProperty(field.Key, field.Value)))));
    var data = new JArray();
    foreach (JObject entry in entries) {
        var state = states[(string)entry["logical_model_id"]] as JObject;
        if (state == null || state["enabled"]?.Type != JTokenType.Boolean) { return ""; }
        if (!(bool)state["enabled"]) { continue; }
        var item = (JObject)entry.DeepClone();
        item.Remove("logical_model_id");
        data.Add(item);
    }
    return new JObject(new JProperty("data", data), new JProperty("has_more", false),
        new JProperty("first_id", data.Count == 0 ? JValue.CreateNull() : data[0]["id"]),
        new JProperty("last_id", data.Count == 0 ? JValue.CreateNull() : data[data.Count - 1]["id"]))
        .ToString(Newtonsoft.Json.Formatting.None);
} catch { return ""; } }"""


MODEL_CATALOG_URL = r'''@{
    var page = JObject.Parse((string)context.Variables["modelCatalogPage"]);
    var filter = "PartitionKey eq '" + (string)context.Variables["routePartition"]
        + "' and RowKey ge 'model:' and RowKey lt 'model;'";
    var url = (string)context.Variables["routeStore"] + "/" + (string)context.Variables["routeTable"]
        + "()?$filter=" + Uri.EscapeDataString(filter) + "&$top=1000";
    if (page["next_partition"]?.Type == JTokenType.String) {
        url += "&NextPartitionKey=" + Uri.EscapeDataString((string)page["next_partition"]);
    }
    if (page["next_row"]?.Type == JTokenType.String) {
        url += "&NextRowKey=" + Uri.EscapeDataString((string)page["next_row"]);
    }
    return url;
}'''

READ_MODEL_CATALOG_PAGE = (
    r'''@{ try {
    if (!context.Variables.GetValueOrDefault<bool>("routeModelAdditionsEnabled", false)) { return "{\"ok\":false}"; }
    var page = JObject.Parse((string)context.Variables["modelCatalogPage"]);
    var reply = context.Variables.GetValueOrDefault<IResponse>("modelCatalogRead", null);
    if ((bool?)page["ok"] != true || (int)page["pages"] >= 10 || reply == null || reply.StatusCode != 200) { return "{\"ok\":false}"; }
    var body = reply.Body.As<string>(preserveContent: true);
    if (Encoding.UTF8.GetByteCount(body) > 1048576) { return "{\"ok\":false}"; }
    var entities = JObject.Parse(body)["value"] as JArray;
    if (entities == null || entities.Count > 1000) { return "{\"ok\":false}"; }
    var items = (JObject)page["items"];
    var aliases = new HashSet<string>(items.Properties().Select(property => property.Name), StringComparer.OrdinalIgnoreCase);
    var now = (long)(DateTime.UtcNow - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
    foreach (var token in entities) {
        var entity = token as JObject;
        if (entity == null || (string)entity["PartitionKey"] != (string)context.Variables["routePartition"]
            || entity["SchemaVersion"]?.Type != JTokenType.Integer || (int)entity["SchemaVersion"] != 2
            || entity["Document"]?.Type != JTokenType.String || entity["Sha256"]?.Type != JTokenType.String
            || entity["RowKey"]?.Type != JTokenType.String) { return "{\"ok\":false}"; }
        var document = (string)entity["Document"];
        if (Encoding.UTF8.GetByteCount(document) > 4096 || Encoding.Unicode.GetByteCount(document) > 32768) { return "{\"ok\":false}"; }
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(document))).Replace("-", "").ToLowerInvariant()
                != (string)entity["Sha256"]) { return "{\"ok\":false}"; }
        }
'''
    + _strict_json_object("document", "model")
    + _exact_fields(
        "model", "version publication_id alias model_id display_name created_at content_sha256 head",
    ).replace('return "";', 'return "{\\\"ok\\\":false}";')
    + r'''
        if (model["version"]?.Type != JTokenType.Integer || (int)model["version"] != 1
            || model["alias"]?.Type != JTokenType.String
            || !System.Text.RegularExpressions.Regex.IsMatch((string)model["alias"], @"\A[a-zA-Z0-9._:-]{1,255}\z")
            || !aliases.Add((string)model["alias"]) || model["display_name"]?.Type != JTokenType.String
            || string.IsNullOrWhiteSpace((string)model["display_name"]) || ((string)model["display_name"]).Length > 255
            || model["created_at"]?.Type != JTokenType.String) { return "{\"ok\":false}"; }
        foreach (var field in new [] {"publication_id", "model_id"}) {
            Guid identifier;
            if (model[field]?.Type != JTokenType.String || !Guid.TryParseExact((string)model[field], "D", out identifier)
                || identifier.ToString() != (string)model[field]) { return "{\"ok\":false}"; }
        }
        var head = model["head"] as JObject;
'''
    + _exact_fields("head", "state approval activated_epoch")
    + r'''
        var approval = head["approval"] as JObject;
'''
    + _exact_fields("approval", "approval_id scope route_version generation manifest_sha256 dependency_manifest_sha256 catalog_sha256 entry_count required_program_sha256 required_profiles budget_epoch issued_epoch activate_before_epoch")
    + r'''
        if ((string)head["state"] != "active"
            || !JToken.DeepEquals(approval["scope"], JObject.Parse((string)context.Variables["routeScope"]))
            || ((string)approval["required_program_sha256"] != (string)context.Variables["routeProgram"]
                && (string)approval["required_program_sha256"] != context.Variables.GetValueOrDefault<string>("routeLegacyProgram", "")
                && !JArray.Parse(context.Variables.GetValueOrDefault<string>("routePredecessorPrograms", "[]"))
                    .Values<string>().Contains((string)approval["required_program_sha256"]))
            || (string)approval["budget_epoch"] != (string)context.Variables["routeBudgetEpoch"]
            || head["activated_epoch"]?.Type != JTokenType.Integer) { return "{\"ok\":false}"; }
        foreach (var field in new [] {"approval_id", "route_version", "budget_epoch"}) {
            Guid identifier;
            if (approval[field]?.Type != JTokenType.String || !Guid.TryParseExact((string)approval[field], "D", out identifier)
                || identifier.ToString() != (string)approval[field]) { return "{\"ok\":false}"; }
        }
        foreach (var field in new [] {"issued_epoch", "activate_before_epoch", "entry_count", "generation"}) {
            if (approval[field]?.Type != JTokenType.Integer || (long)approval[field] < 0 || (long)approval[field] > 9007199254740991L) { return "{\"ok\":false}"; }
        }
        foreach (var value in new [] {approval["manifest_sha256"], approval["dependency_manifest_sha256"], approval["catalog_sha256"], model["content_sha256"]}) {
            if (value?.Type != JTokenType.String || !System.Text.RegularExpressions.Regex.IsMatch((string)value, @"\A[0-9a-f]{64}\z")) { return "{\"ok\":false}"; }
        }
        var profiles = approval["required_profiles"] as JArray;
        if (profiles == null || profiles.Count != 1 || profiles[0].Type != JTokenType.String
            || !JArray.Parse((string)context.Variables["routeProfiles"]).Values<string>().Contains((string)profiles[0])
            || (long)approval["entry_count"] != 1 || (long)approval["generation"] != 1
            || (long)approval["activate_before_epoch"] <= (long)approval["issued_epoch"]
            || (long)approval["activate_before_epoch"] - (long)approval["issued_epoch"] > 600
            || (long)head["activated_epoch"] < (long)approval["issued_epoch"]
            || (long)head["activated_epoch"] >= (long)approval["activate_before_epoch"]
            || (long)head["activated_epoch"] > now) { return "{\"ok\":false}"; }
        var entry = new JObject(new JProperty("created_at", model["created_at"]),
            new JProperty("display_name", model["display_name"]), new JProperty("id", model["alias"]),
            new JProperty("logical_model_id", model["model_id"]), new JProperty("type", "model"));
        var catalog = new JObject(new JProperty((string)model["alias"], entry));
        using (var digest = System.Security.Cryptography.SHA256.Create()) {
            if (BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(catalog.ToString(Newtonsoft.Json.Formatting.None))))
                .Replace("-", "").ToLowerInvariant() != (string)approval["catalog_sha256"]
                || "model:" + BitConverter.ToString(digest.ComputeHash(Encoding.UTF8.GetBytes(((string)model["alias"]).ToLowerInvariant())))
                    .Replace("-", "").ToLowerInvariant() != (string)entity["RowKey"]) { return "{\"ok\":false}"; }
        }
        items[(string)model["alias"]] = entry.ToString(Newtonsoft.Json.Formatting.None);
    }
    if (items.Properties().Count() > 10000 || Encoding.UTF8.GetByteCount(items.ToString(Newtonsoft.Json.Formatting.None)) > 2097152) { return "{\"ok\":false}"; }
    var nextPartition = reply.Headers.GetValueOrDefault("x-ms-continuation-NextPartitionKey", "");
    var nextRow = reply.Headers.GetValueOrDefault("x-ms-continuation-NextRowKey", "");
    if (nextPartition.Length > 1024 || nextRow.Length > 1024 || (nextPartition == "" && nextRow != "")
        || (nextPartition != "" && nextPartition == (string)page["next_partition"] && nextRow == (string)page["next_row"])) { return "{\"ok\":false}"; }
    return new JObject(new JProperty("ok", true), new JProperty("pages", (int)page["pages"] + 1),
        new JProperty("items", items), new JProperty("next_partition", nextPartition == "" ? null : nextPartition),
        new JProperty("next_row", nextRow == "" ? null : nextRow)).ToString(Newtonsoft.Json.Formatting.None);
} catch { return "{\"ok\":false}"; } }'''
).replace('return "";', r'return "{\"ok\":false}";')

MERGE_MODEL_CATALOG = r'''@{ try {
    var entries = JsonConvert.DeserializeObject<List<Dictionary<string, string>>>(
        (string)context.Variables["routeCatalogEntries"]);
    var page = JObject.Parse((string)context.Variables["modelCatalogPage"]);
    if ((bool?)page["ok"] != true || page["next_partition"]?.Type != JTokenType.Null
        || page["next_row"]?.Type != JTokenType.Null || (int)page["pages"] < 1 || (int)page["pages"] > 10) { return ""; }
    var aliases = new HashSet<string>(entries.Select(entry => entry["id"]), StringComparer.OrdinalIgnoreCase);
    foreach (var item in ((JObject)page["items"]).Properties()) {
        if (!aliases.Add(item.Name) || item.Value.Type != JTokenType.String) { return ""; }
        entries.Add(JsonConvert.DeserializeObject<Dictionary<string, string>>((string)item.Value));
    }
    if (entries.Count > 10000) { return ""; }
    return JsonConvert.SerializeObject(entries.OrderBy(entry => entry["id"], StringComparer.Ordinal));
} catch { return ""; } }'''


def fixed_catalog(*, model_additions_enabled: bool = False) -> ET.Element:
    from .apim_model_availability import CATALOG_PAGE, CATALOG_URL

    root = ET.Element("choose")
    branch = ET.SubElement(root, "when", {"condition": '@(context.Operation.Id == "models")'})
    deny(branch, '@(context.Request.Method != "GET")')
    variable(branch, "routeCatalogPage", '{"ok":true,"pages":0,"items":{}}')
    pages = ET.SubElement(
        branch,
        "retry",
        {
            "count": "9",
            "interval": "0",
            "first-fast-retry": "true",
            "condition": '@((bool?)JObject.Parse((string)context.Variables["routeCatalogPage"])["ok"] == true && JObject.Parse((string)context.Variables["routeCatalogPage"])["next_partition"]?.Type == JTokenType.String)',
        },
    )
    for parent, response_name, url, variable_name, expression in (
        (pages, "routeCatalogRead", CATALOG_PAGE_URL, "routeCatalogPage", READ_CATALOG_PAGE),
    ):
        query_read(parent, response_name, url)
        variable(parent, variable_name, expression)
    variable(branch, "routeCatalogEntries", CHECK_CATALOG)
    deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeCatalogEntries"]))')
    if model_additions_enabled:
        variable(branch, "modelCatalogPage", '{"ok":true,"pages":0,"items":{}}')
        model_pages = ET.SubElement(branch, "retry", {
            "count": "9", "interval": "0", "first-fast-retry": "true",
            "condition": '@((bool?)JObject.Parse((string)context.Variables["modelCatalogPage"])["ok"] == true && JObject.Parse((string)context.Variables["modelCatalogPage"])["next_partition"]?.Type == JTokenType.String)',
        })
        query_read(model_pages, "modelCatalogRead", MODEL_CATALOG_URL)
        variable(model_pages, "modelCatalogPage", READ_MODEL_CATALOG_PAGE)
        variable(branch, "routeCatalogEntries", MERGE_MODEL_CATALOG)
        deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeCatalogEntries"]))')
    variable(branch, "availabilityCatalogPage", '{"ok":true,"pages":0,"states":{}}')
    states = ET.SubElement(
        branch,
        "retry",
        {
            "count": "3",
            "interval": "0",
            "first-fast-retry": "true",
            "condition": '@((bool?)JObject.Parse((string)context.Variables["availabilityCatalogPage"])["ok"] == true && JObject.Parse((string)context.Variables["availabilityCatalogPage"])["next_partition"]?.Type == JTokenType.String)',
        },
    )
    query_read(states, "availabilityCatalogRead", CATALOG_URL)
    variable(states, "availabilityCatalogPage", CATALOG_PAGE)
    variable(branch, "routeCatalogResult", FILTER_FIXED_CATALOG)
    deny(branch, '@(string.IsNullOrEmpty((string)context.Variables["routeCatalogResult"]))')
    response = ET.SubElement(branch, "return-response")
    ET.SubElement(response, "set-status", {"code": "200", "reason": "OK"})
    header_value(response, "Content-Type", "application/json")
    header_value(response, "Cache-Control", "no-store")
    ET.SubElement(response, "set-body").text = '@((string)context.Variables["routeCatalogResult"])'
    return root


def query_read(parent: ET.Element, name: str, url: str) -> None:
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
    header_value(request, "Accept", "application/json;odata=nometadata")
    header_value(request, "x-ms-version", "2020-12-06")
    ET.SubElement(
        request, "authentication-managed-identity", {"resource": "https://storage.azure.com/"}
    )


def fixed_probe(parent: ET.Element, *, configuration: bool) -> None:
    variable(
        parent,
        "routeProbeNonce",
        r"""@{
        var nonce = context.Request.Headers.GetValueOrDefault("x-turnstile-route-probe", "");
        Guid parsed;
        return context.Request.Method == "HEAD" && Guid.TryParseExact(nonce, "N", out parsed)
            && parsed.ToString("N") == nonce ? nonce : "";
    }""",
    )
    deny(parent, '@(string.IsNullOrEmpty((string)context.Variables["routeProbeNonce"]))')
    response = ET.SubElement(parent, "return-response")
    ET.SubElement(response, "set-status", {"code": "204", "reason": "No Content"})
    for name, value in (
        ("abi", "2"),
        ("nonce", '@((string)context.Variables["routeProbeNonce"])'),
        ("program", '@((string)context.Variables["routeProgram"])'),
        ("trust", '@((string)context.Variables["routeTrust"])'),
        (
            "target",
            '@((string)JObject.Parse((string)context.Variables["routeScope"])["target_id"])',
        ),
        ("api", "@(context.Api.Id.Split(';')[0])"),
        ("revision", "@(context.Api.Revision)"),
        ("gateway", "@(context.Deployment.Gateway.Id)"),
        ("instance", "@(context.Deployment.Gateway.InstanceId)"),
    ):
        header_value(response, "x-turnstile-route-" + name, value)
    header_value(response, "Cache-Control", "no-store")
    if configuration:
        for name, field in (
            ("version", "route_version"),
            ("generation", "generation"),
            ("epoch", "budget_epoch"),
        ):
            header_value(
                response,
                "x-turnstile-route-" + name,
                f'@(JObject.Parse((string)context.Variables["routeHead"])["approval"]["{field}"].ToString())',
            )


def _baseline_root(source: str, binding: FixedRouteBinding | None = None) -> ET.Element:
    from .apim_budget_execution import eventhub_logger_id
    from .apim_model_availability import (
        _admission_locations,
        admission_guard,
        validate_availability_parent,
    )

    root = parse_policy(source)
    if [node.tag for node in root if isinstance(node.tag, str)] != [
        "inbound",
        "backend",
        "outbound",
        "on-error",
    ]:
        raise PolicyCompilationError("Fixed route ABI requires the complete parent policy")
    validate_availability_parent(root)
    if root.find(".//set-variable[@name='executionStatus']") is None:
        raise PolicyCompilationError("Fixed route ABI requires active budget execution")
    if root.find(".//set-variable[@name='storageRouteAbi']") is not None:
        raise PolicyCompilationError("Fixed route ABI cannot be composed twice")
    if binding is not None and binding.native_gateway_scope:
        for branch, anchor in _admission_locations(root):
            guard = serialize_policy(admission_guard(
                native_gateway_scope=True, logger_id=eventhub_logger_id(root),
            ))
            for parameter, value in (
                ("__GATEWAY_PROFILE_ID__", binding.scope.target_id),
                ("__LEDGER_TABLE_ENDPOINT__", binding.ledger_table_endpoint),
                ("__LEDGER_TABLE_NAME__", binding.ledger_table_name),
            ):
                guard = guard.replace(parameter, value)
            branch[list(branch).index(anchor) + 1] = parse_policy(guard)
    return root


def upgrade_fixed_program_parent(
    source: str, binding: FixedRouteBinding, canonical_parent: str,
    previous_document_sha256: str,
) -> str:
    from copy import deepcopy

    from .apim_budget_execution import _admission, _completion, budget_execution_fence
    from .apim_model_availability import _admission_locations, admission_guard
    from .apim_policy_components import compose_image_parent_policy

    if policy_document_digest(source) != previous_document_sha256:
        raise PolicyCompilationError("Fixed program upgrade source policy changed")
    root = _baseline_root(source)
    inbound = root.find("inbound")
    assert inbound is not None
    loggers = {node.get("logger-id", "") for node in root.iter("log-to-eventhub")}
    if len(loggers) != 1 or not next(iter(loggers)):
        raise PolicyCompilationError("Fixed program upgrade logger identity is ambiguous")
    for branch, anchor in _admission_locations(root):
        branch[list(branch).index(anchor) + 1] = admission_guard(
            logger_id=next(iter(loggers)),
        )
    markers = root.findall(".//set-variable[@name='executionModelId']")
    if len(markers) != 1:
        raise PolicyCompilationError("Fixed program upgrade admission is ambiguous")
    parent = next(node for node in root.iter() if markers[0] in list(node))
    container = next(node for node in root.iter() if parent in list(node))
    admission = _admission().find("when")
    if admission is None or parent.tag != "when" or container.tag != "choose":
        raise PolicyCompilationError("Fixed program upgrade admission shape changed")
    position = list(container).index(parent)
    container.remove(parent)
    container.insert(position, deepcopy(admission))
    completions = [node for node in root.findall("./outbound//when")
                   if "executionReceiptHeader" in node.get("condition", "")
                   and node.find(".//set-variable[@name='executionReservation']") is not None]
    if len(completions) != 1:
        raise PolicyCompilationError("Fixed program upgrade completion is ambiguous")
    completion_parent = next(node for node in root.iter() if completions[0] in list(node))
    completion_index = list(completion_parent).index(completions[0])
    completion = _completion().find("when")
    if completion is None:
        raise PolicyCompilationError("Fixed program upgrade completion is missing")
    completion_parent.remove(completions[0])
    completion_parent.insert(completion_index, deepcopy(completion))
    outcome_template = parse_policy(
        compose_image_parent_policy(
            canonical_parent, canonical_parent, image_program_profiles(binding),
        ) if root.find("inbound/set-variable[@name='imageGenerationPolicyVersion']") is not None
        else canonical_parent
    )
    usage_nodes = root.findall("./outbound//set-variable[@name='usagePayload']")
    expected_usage = outcome_template.findall("./outbound//set-variable[@name='usagePayload']")
    if len(usage_nodes) != len(expected_usage):
        raise PolicyCompilationError("Fixed program upgrade usage reader is ambiguous")
    parents = {child: owner for owner in root.iter() for child in owner}
    expected_parents = {child: owner for owner in outcome_template.iter() for child in owner}
    mapping_nodes = root.findall(".//set-variable[@name='applicationMapState']")
    expected_mapping = outcome_template.findall(".//set-variable[@name='applicationMapState']")
    if len(mapping_nodes) != 1 or len(expected_mapping) != 1:
        raise PolicyCompilationError("Fixed program upgrade subscription mapping is ambiguous")
    mapping_nodes[0].set("value", expected_mapping[0].attrib["value"])
    application_states = root.findall(".//set-variable[@name='applicationLedgerState']")
    expected_states = outcome_template.findall(".//set-variable[@name='applicationLedgerState']")
    if len(application_states) != 1 or len(expected_states) != 1:
        raise PolicyCompilationError("Fixed program upgrade subscription attribution is ambiguous")
    actual_branch = parents[application_states[0]]
    expected_branch = expected_parents[expected_states[0]]
    attribution_headers = {"x-org-id", "x-org-name", "x-department-id", "x-department-name"}
    for node in list(actual_branch):
        if node.tag == "set-header" and node.get("name") in attribution_headers:
            actual_branch.remove(node)
    for index, node in enumerate(expected_branch):
        if node.tag == "set-header" and node.get("name") in attribution_headers:
            actual_branch.insert(index, deepcopy(node))
    for marker, subject in (
        ('"modelAdmission"', "model"), ('"applicationDenialReason"', "application"),
    ):
        denials = [
            [node for node in policy.iter("when")
             if marker in node.get("condition", "") and node.find("return-response") is not None]
            for policy in (root, outcome_template)
        ]
        if [len(nodes) for nodes in denials] != [1, 1]:
            raise PolicyCompilationError(f"Fixed program upgrade {subject} denial is ambiguous")
        actual_denial, expected_denial = denials[0][0], denials[1][0]
        denial_response = actual_denial.find("return-response")
        expected_response = expected_denial.find("return-response")
        assert denial_response is not None and expected_response is not None
        actual_denial[list(actual_denial).index(denial_response)] = deepcopy(expected_response)
    for node, expected_node in zip(usage_nodes, expected_usage, strict=True):
        node.set("value", expected_node.attrib["value"])
        if "stream_usage_breakdown_unavailable" in node.get("value", ""):
            parents[node].set("condition", expected_parents[expected_node].attrib["condition"])
    events = root.findall("./outbound//log-to-eventhub") + root.findall(
        "./on-error//log-to-eventhub"
    )
    expected_events = outcome_template.findall("./outbound//log-to-eventhub") + outcome_template.findall(
        "./on-error//log-to-eventhub"
    )
    if len(events) != len(expected_events):
        raise PolicyCompilationError("Fixed program upgrade outcome events are ambiguous")
    for event, expected_event in zip(events, expected_events, strict=True):
        event.text = expected_event.text
    if not root.findall(".//send-request[@response-variable-name='budgetExecutionFenceRead']"):
        base = inbound.find("base")
        if base is None:
            raise PolicyCompilationError("Fixed program upgrade requires inherited protections")
        inbound.insert(list(inbound).index(base) + 1, budget_execution_fence([binding.scope.api_id]))
    values = {
        "__LOGGER_ID__": next(iter(loggers)),
        "__GATEWAY_PROFILE_ID__": binding.scope.target_id,
        "__LEDGER_TABLE_ENDPOINT__": binding.ledger_table_endpoint,
        "__LEDGER_TABLE_NAME__": binding.ledger_table_name,
    }
    for node in root.iter():
        for name, value in node.attrib.items():
            for parameter, replacement in values.items():
                value = value.replace(parameter, replacement)
            node.set(name, value)
        if node.text:
            for parameter, replacement in values.items():
                node.text = node.text.replace(parameter, replacement)
    candidate = serialize_policy(root)
    validate_parent_policy(candidate, canonical_parent, image_program_profiles(binding))
    validate_policy_upload(
        candidate, gateway_profile_id=UUID(binding.scope.target_id),
        api_id=binding.scope.api_id, api_revision=binding.api_revision,
        table_endpoint=binding.ledger_table_endpoint, table_name=binding.ledger_table_name,
    )
    return candidate


def prepare_budget_delegation_upgrade(
    installed: FixedProgramInstallation, publication_id: str, candidate: str,
    operations: dict[str, tuple[str, str, str]],
    *, enable_pool: bool = False, enable_router: bool = False,
    enable_decision: bool = False, image_limits: ImageGenerationLimits | None = None,
) -> tuple[FixedProgramUpgrade, str]:
    contracts = fixed_operation_contracts(
        installed.operation_contracts["models"][1],
        installed.operation_contracts["anthropic-messages"][1],
        installed.retired_operations,
        decision_enabled=installed.decision_enabled or enable_decision,
    )
    capabilities: dict[str, object] = {}
    if enable_decision or image_limits is not None:
        capabilities = {
            "supported_profiles": (
                *installed.supported_profiles,
                *(("openai-images",) if image_limits is not None else ()),
                *(("microsoft-decision",) if enable_decision else ()),
            ),
            "image_limits": image_limits or installed.image_limits,
            "decision_enabled": installed.decision_enabled or enable_decision,
            "operation_contracts": contracts,
        }
    root = parse_policy(compact_fixed_policy(candidate))
    marker = root.find(".//set-variable[@name='routeProgram']")
    if marker is None:
        raise RuntimeError("Budget delegation program identity is missing")
    predecessor = installed.program.program_sha256
    legacy = installed.legacy_program_sha256 or predecessor
    predecessors = tuple(value for value in (
        *installed.predecessor_program_sha256, predecessor,
    ) if value != legacy)
    owner = next(node for node in root.iter() if marker in list(node))
    history = owner.find("set-variable[@name='routeLegacyProgram']")
    if history is None:
        history = ET.Element("set-variable", {"name": "routeLegacyProgram"})
        owner.insert(list(owner).index(marker), history)
    history.set("value", legacy)
    # Match fixed_reader: predecessors follow the legacy marker and are omitted when empty.
    for node in owner.findall("set-variable[@name='routePredecessorPrograms']"):
        owner.remove(node)
    if predecessors:
        owner.insert(list(owner).index(history) + 1, ET.Element("set-variable", {
            "name": "routePredecessorPrograms",
            "value": json.dumps(predecessors, separators=(",", ":")),
        }))
    digest = fixed_program_digest(
        serialize_policy(root), {name: operation[2] for name, operation in operations.items()},
        tuple((name, *contract) for name, contract in contracts.items()),
    )
    marker.set("value", digest)
    candidate = serialize_policy(root)
    upgrade = FixedProgramUpgrade(
        previous_publication_id=publication_id,
        previous_installation_sha256=document_digest(installed),
        installation=installed.model_copy(update={
            "program": installed.program.model_copy(update={"program_sha256": digest}),
            "parent_policy_sha256": fixed_policy_digest(candidate),
            "legacy_program_sha256": legacy,
            "predecessor_program_sha256": predecessors,
            "operation_policies_sha256": {
                name: fixed_policy_digest(operation[2]) for name, operation in operations.items()
            },
            **({"pool_enabled": True} if enable_pool or enable_router else {}),
            **({"router_enabled": True} if enable_router else {}),
            **capabilities,
        }),
    )
    upgrade.require_previous(publication_id, installed)
    return upgrade, candidate


def prepare_capability_upgrade(
    installed: FixedProgramInstallation, publication_id: str, parent: str,
    operations: Mapping[str, tuple[str, str, str]], ledger_endpoint: str, ledger_table: str,
    observer_key: str | None, *, enable_decision: bool, enable_images: bool,
) -> tuple[FixedProgramUpgrade, str, dict[str, str]]:
    # Other operations keep their installed bytes; the cutover checks them against the live API.
    from .apim_budget_execution import eventhub_logger_id, usage_identity_expression

    decision = enable_decision and not installed.decision_enabled
    images = enable_images and installed.image_limits is None
    if not (decision or images):
        raise PolicyCompilationError("Program capabilities are already enabled")
    previous = installed_route_binding(installed, ledger_endpoint, ledger_table, observer_key)
    enabled = FixedRouteBinding.model_validate_json(previous.model_copy(update={
        "supported_profiles": (
            *previous.supported_profiles,
            *(("openai-images",) if images else ()),
            *(("microsoft-decision",) if decision else ()),
        ),
        "image_limits": DEFAULT_IMAGE_GENERATION_LIMITS if images else previous.image_limits,
        "decision_enabled": previous.decision_enabled or decision,
    }).model_dump_json())
    root = parse_policy(parent)
    bursts = {node.get("increment-count") for node in root.iter("rate-limit-by-key")
              if node.get("counter-key", "").startswith('@($"finops:images:')}
    if images and bursts != {str(DEFAULT_IMAGE_GENERATION_LIMITS.burst_reservation_tokens)}:
        raise PolicyCompilationError("Installed image burst cap differs from the image capacity")
    if {name: operation[:2] for name, operation in operations.items()} != dict(
        installed.operation_contracts,
    ):
        raise PolicyCompilationError("Installed program operations differ from its approval")
    logger_id, usage_identity = eventhub_logger_id(root), usage_identity_expression(root)
    for name in set(FIXED_OPERATIONS) & set(operations):
        if component_digest(parse_policy(operations[name][2]), normalize_text_defaults=False) != (
            component_digest(parse_policy(fixed_operation(
                name, previous, logger_id=logger_id, usage_identity=usage_identity,
            )), normalize_text_defaults=False)
        ):
            raise PolicyCompilationError(
                "Installed program is not current; wait for the Control-plane refresh",
            )
    policies = {name: operation[2] for name, operation in operations.items()}
    for name in (("images-generations",) if images else ()) + (
        ("microsoft-decision",) if decision else ()
    ):
        policies[name] = compact_fixed_policy(fixed_operation(
            name, enabled, logger_id=logger_id, usage_identity=usage_identity,
        ))
    baseline = serialize_policy(_without_fixed_route_parent(parent, previous))
    contracts = fixed_operation_contracts(
        enabled.models_path, enabled.messages_path, enabled.retired_operations,
        decision_enabled=enabled.decision_enabled,
    )
    upgrade, candidate = prepare_budget_delegation_upgrade(
        installed, publication_id,
        compact_fixed_policy(compose_fixed_route_parent(baseline, enabled)),
        {name: (method, path, policies[name]) for name, (method, path) in contracts.items()},
        enable_decision=decision,
        image_limits=DEFAULT_IMAGE_GENERATION_LIMITS if images else None,
    )
    return upgrade, candidate, policies


def refresh_fixed_budget_execution_policy(
    source: str, binding: FixedRouteBinding, canonical_parent: str,
) -> str:
    from .apim_budget_execution import eventhub_logger_id
    from .apim_model_availability import _admission_locations, admission_guard

    current = refresh_fixed_connection_approval_policy(source, binding)
    root = _without_fixed_route_parent(current, binding)
    if binding.native_gateway_scope:
        guards = [serialize_policy(admission_guard(
            native_gateway_scope=native, logger_id=eventhub_logger_id(root),
        ))
                  for native in (True, False)]
        legacy = serialize_policy(admission_guard(native_gateway_scope=True))
        for parameter, value in (
            ("__GATEWAY_PROFILE_ID__", binding.scope.target_id),
            ("__LEDGER_TABLE_ENDPOINT__", binding.ledger_table_endpoint),
            ("__LEDGER_TABLE_NAME__", binding.ledger_table_name),
        ):
            guards = [guard.replace(parameter, value) for guard in guards]
            legacy = legacy.replace(parameter, value)
        for parent, anchor in _admission_locations(root):
            index = list(parent).index(anchor) + 1
            if component_digest(parent[index]) not in {
                component_digest(parse_policy(guards[0])), component_digest(parse_policy(legacy)),
            }:
                raise PolicyCompilationError("Fixed native availability guard changed")
            parent[index] = parse_policy(guards[1])
    for claim in root.findall(".//validate-azure-ad-token/required-claims/claim[@name='scp']"):
        if claim.get("separator") == "":
            claim.set("separator", " ")
    baseline = serialize_policy(root)
    upgraded = upgrade_fixed_program_parent(
        baseline, binding, canonical_parent, policy_document_digest(baseline),
    )
    return compose_fixed_route_parent(upgraded, binding)


DECISION_TOKEN_BOUND = r'''@{
    var snapshot = JObject.Parse((string)context.Variables["selectedBudgetCountContract"]);
    var model = (JObject)snapshot["contract"];
    var contract = (model["protocols"] as JObject)?["microsoft-decision"] as JObject ?? model;
    long bound;
    if ((string)contract["algorithm"] == "legacy_request_estimate_v1") {
        bound = checked(context.Request.Body.As<byte[]>(preserveContent: true).LongLength
            + (long)contract["output_estimate"]);
    } else if ((string)contract["algorithm"] == "request_total_ceiling_v1") {
        bound = (long)contract["request_token_ceiling"];
    } else { throw new Exception("Decision counting contract is unavailable"); }
    if (bound <= 0) { throw new Exception("Decision counting contract is invalid"); }
    return checked((int)bound);
}'''


def _decision_token_wrapper(node: ET.Element) -> ET.Element:
    from copy import deepcopy

    wrapper = ET.Element("choose")
    ET.SubElement(wrapper, "when", {
        "condition": '@(context.Operation.Id != "microsoft-decision")',
    }).append(deepcopy(node))
    if node.tag == "llm-token-limit":
        counter = node.attrib["counter-key"]
        ET.SubElement(ET.SubElement(wrapper, "otherwise"), "rate-limit-by-key", {
            "calls": node.attrib["tokens-per-minute"], "renewal-period": "60",
            "counter-key": '@("finops:decision:" + (' + counter[2:-1] + '))',
            "increment-count": DECISION_TOKEN_BOUND, "retry-after-header-name": "Retry-After",
        })
    return wrapper


def _compose_decision_parent(root: ET.Element) -> None:
    inference = root.find("./inbound/set-variable[@name='isInferenceOperation']")
    if inference is None:
        raise PolicyCompilationError("Decision inference admission is missing")
    value = inference.attrib["value"]
    inference.set("value", '@((' + value[2:-1] + ') || context.Operation.Id == "microsoft-decision")')
    parents = {child: owner for owner in root.iter() for child in owner}
    for node in (*root.iter("llm-token-limit"), *root.iter("llm-emit-token-metric")):
        parent = parents[node]
        parent[list(parent).index(node)] = _decision_token_wrapper(node)


def _without_decision_parent(root: ET.Element) -> None:
    from copy import deepcopy

    inference = root.find("./inbound/set-variable[@name='isInferenceOperation']")
    if inference is None:
        raise PolicyCompilationError("Decision inference admission has changed")
    value = inference.attrib["value"]
    tokens = list(expression_token_spans(value))
    suffix = (
        ")", "|", "|", "context", ".", "Operation", ".", "Id", "=", "=",
        '"microsoft-decision"', ")",
    )
    if (tuple(token for token, _, _ in tokens[:3]) != ("@", "(", "(")
            or tuple(token for token, _, _ in tokens[-len(suffix):]) != suffix):
        raise PolicyCompilationError("Decision inference admission has changed")
    inference.set("value", "@(" + value[tokens[2][2]:tokens[-len(suffix)][1]] + ")")
    parents = {child: owner for owner in root.iter() for child in owner}
    for node in (*root.iter("llm-token-limit"), *root.iter("llm-emit-token-metric")):
        branch = parents[node]
        wrapper = parents[branch]
        if component_digest(wrapper) != component_digest(_decision_token_wrapper(node)):
            raise PolicyCompilationError("Decision token policy isolation has changed")
        owner = parents[wrapper]
        owner[list(owner).index(wrapper)] = deepcopy(node)


def compose_fixed_route_parent(source: str, binding: FixedRouteBinding) -> str:
    from .apim_model_availability import _admission_locations

    root = _baseline_root(source, binding)
    if binding.decision_enabled:
        _compose_decision_parent(root)
    inbound = root.find("inbound")
    assert inbound is not None
    locations = _admission_locations(root)
    choices = [node for node in inbound if all(branch in list(node) for branch, _ in locations)]
    if len(choices) != 1:
        raise PolicyCompilationError("Fixed route authentication branches are ambiguous")
    original = choices[0]
    resumed = ET.Element("choose")
    for branch, anchor in locations:
        suffix = ET.SubElement(resumed, branch.tag, dict(branch.attrib))
        boundary = list(branch).index(anchor) - 1
        for node in list(branch)[boundary:]:
            branch.remove(node)
            suffix.append(node)
    index = list(inbound).index(original) + 1
    for offset, node in enumerate(
        (
            ET.Element("set-variable", {"name": "storageRouteAbi", "value": "2"}),
            fixed_reader(binding),
            route_metadata(binding),
            resumed,
        )
    ):
        inbound.insert(index + offset, node)
    count_plans = root.findall(".//set-variable[@name='executionCountPlan']")
    if len(count_plans) != 1:
        raise PolicyCompilationError("Fixed route native counter location is ambiguous")
    count_parent = next(node for node in root.iter() if count_plans[0] in list(node))
    count_parent.insert(list(count_parent).index(count_plans[0]) + 1, count_credential_stage())
    plans = root.findall(".//set-variable[@name='executionPlan']")
    if not plans:
        raise PolicyCompilationError("Fixed route budget plan location is missing")
    plan_parent = next(node for node in root.iter() if plans[0] in list(node))
    plan_parent.insert(list(plan_parent).index(plans[0]) + 3, provider_credential_stage(binding))
    authorized = root.find(".//send-request[@response-variable-name='executionDispatchAuthorized']")
    if authorized is None:
        raise PolicyCompilationError("Fixed route dispatch authorization is missing")
    dispatch_parent = next(node for node in root.iter() if authorized in list(node))
    dispatch_parent.insert(list(dispatch_parent).index(authorized), route_dispatch_guard())
    result = serialize_policy(root)
    validate_fixed_route_parent(result, source, binding, executable=False)
    return result


def _without_fixed_route_parent(
    candidate: str,
    binding: FixedRouteBinding,
    *,
    executable: bool = True,
) -> ET.Element:
    from .apim_model_availability import _admission_locations

    root = parse_policy(validate_executable_policy(candidate) if executable else candidate)
    count_plans = root.findall(".//set-variable[@name='executionCountPlan']")
    if len(count_plans) != 1:
        raise PolicyCompilationError("Fixed route native counter location is ambiguous")
    count_parent = next(node for node in root.iter() if count_plans[0] in list(node))
    count_index = list(count_parent).index(count_plans[0]) + 1
    if len(count_parent) <= count_index or component_digest(
        count_parent[count_index]
    ) != component_digest(count_credential_stage()):
        raise PolicyCompilationError(
            "Fixed route native counter credentials are missing or reordered"
        )
    count_parent.remove(count_parent[count_index])
    plans = root.findall(".//set-variable[@name='executionPlan']")
    if not plans:
        raise PolicyCompilationError("Fixed route budget plan location is missing")
    plan_parent = next(node for node in root.iter() if plans[0] in list(node))
    provider_index = list(plan_parent).index(plans[0]) + 3
    if len(plan_parent) <= provider_index or component_digest(
        plan_parent[provider_index]
    ) != component_digest(provider_credential_stage(binding)):
        raise PolicyCompilationError("Fixed route model credentials are missing or reordered")
    plan_parent.remove(plan_parent[provider_index])
    authorized = root.find(".//send-request[@response-variable-name='executionDispatchAuthorized']")
    if authorized is None:
        raise PolicyCompilationError("Fixed route dispatch authorization is missing")
    dispatch_parent = next(node for node in root.iter() if authorized in list(node))
    dispatch_index = list(dispatch_parent).index(authorized) - 1
    if dispatch_index < 0 or component_digest(dispatch_parent[dispatch_index]) != component_digest(
        route_dispatch_guard()
    ):
        raise PolicyCompilationError("Fixed route dispatch freshness guard is missing or reordered")
    dispatch_parent.remove(dispatch_parent[dispatch_index])
    inbound = root.find("inbound")
    assert inbound is not None
    markers = root.findall(".//set-variable[@name='storageRouteAbi']")
    if len(markers) != 1 or markers[0] not in list(inbound) or markers[0].get("value") != "2":
        raise PolicyCompilationError("Fixed route ABI marker is missing or ambiguous")
    index = list(inbound).index(markers[0])
    if index < 1 or len(inbound) <= index + 3:
        raise PolicyCompilationError("Fixed route ABI admission stages are incomplete")
    prefix, reader, metadata, resumed = (
        inbound[index - 1],
        inbound[index + 1],
        inbound[index + 2],
        inbound[index + 3],
    )
    if (
        component_digest(reader) != component_digest(fixed_reader(binding))
        or component_digest(metadata) != component_digest(route_metadata(binding))
        or prefix.tag != "choose"
        or resumed.tag != "choose"
        or len(prefix) != 2
        or len(resumed) != 2
    ):
        raise PolicyCompilationError("Fixed route ABI reader, metadata or admission order differs")
    for before, after in zip(prefix, resumed, strict=True):
        if before.tag != after.tag or before.attrib != after.attrib:
            raise PolicyCompilationError("Fixed route authentication branch changed")
        before.extend(list(after))
    for node in (markers[0], reader, metadata, resumed):
        inbound.remove(node)
    if binding.decision_enabled:
        _without_decision_parent(root)
    _admission_locations(root)
    if len(parse_policy(candidate).findall(".//cache-lookup-value")) != 1:
        raise PolicyCompilationError("Fixed route ABI requires exactly one immutable-content cache")
    return root


def installed_route_binding(
    installation: FixedProgramInstallation, ledger_endpoint: str | None, ledger_table: str,
    observer_key: str | None,
) -> FixedRouteBinding:
    values = installation.model_dump(mode="python", include={
        "publisher_subscription_id", "supported_profiles", "image_limits", "oauth_enabled",
        "pool_enabled", "decision_enabled", "model_additions_enabled", "native_gateway_scope",
        "router_enabled",
        "legacy_program_sha256", "predecessor_program_sha256", "supported_adapters",
        "trusted_vault_origins", "retired_operations",
    })
    return FixedRouteBinding.model_validate({
        **values, "scope": installation.program.scope,
        "api_revision": installation.program.api_revision,
        "table_endpoint": installation.storage_endpoint, "table_name": installation.storage_table,
        "ledger_table_endpoint": ledger_endpoint, "ledger_table_name": ledger_table,
        "trust_binding_sha256": installation.program.trust_binding_sha256,
        "program_sha256": installation.program.program_sha256,
        "observer_key_named_value": observer_key,
        "models_path": installation.operation_contracts["models"][1],
        "messages_path": installation.operation_contracts["anthropic-messages"][1],
    })


def upgrade_fixed_pool_program(
    source: str, binding: FixedRouteBinding, *, enable_router: bool = False,
) -> FixedRouteProgram:
    from copy import deepcopy

    from .apim_budget_execution import eventhub_logger_id, usage_identity_expression

    root = parse_policy(source)
    identities = root.findall(".//set-variable[@name='routeProgram']")
    if len(identities) != 1:
        raise PolicyCompilationError("Program upgrade identity is ambiguous")
    reader = next(node for node in root.iter() if identities[0] in list(node))
    expected_reader = fixed_reader(binding).find("when")
    assert expected_reader is not None
    history = {"routeLegacyProgram", "routePredecessorPrograms"}
    for name in history:
        expected = expected_reader.find(f"set-variable[@name='{name}']")
        observed = reader.findall(f"set-variable[@name='{name}']")
        if expected is not None:
            if len(observed) != 1 or observed[0].attrib != expected.attrib:
                raise PolicyCompilationError("Program upgrade predecessor history changed")
        elif observed and (len(observed) != 1 or observed[0].get("value") != "[]"):
            raise PolicyCompilationError("Program upgrade predecessor history changed")
        for node in observed:
            reader.remove(node)
    for index, node in enumerate(expected_reader):
        if node.tag == "set-variable" and node.get("name") in history:
            reader.insert(index, deepcopy(node))
    source = serialize_policy(root)
    baseline = _without_fixed_route_parent(source, binding)
    enabled = binding.model_copy(update={
        "pool_enabled": True, **({"router_enabled": True} if enable_router else {}),
    })
    root = parse_policy(source)
    inbound = root.find("inbound")
    assert inbound is not None
    marker = inbound.find("set-variable[@name='storageRouteAbi']")
    assert marker is not None
    index = list(inbound).index(marker)
    inbound[index + 1] = fixed_reader(enabled)
    inbound[index + 2] = route_metadata(enabled)
    plan = root.find(".//set-variable[@name='executionPlan']")
    assert plan is not None
    owner = next(node for node in root.iter() if plan in list(node))
    owner[list(owner).index(plan) + 3] = provider_credential_stage(enabled)
    contracts = tuple((name, *contract) for name, contract in fixed_operation_contracts(
        binding.models_path, binding.messages_path, binding.retired_operations,
        decision_enabled=binding.decision_enabled,
    ).items())
    operations = {
        name: compact_fixed_policy(
            fixed_operation(name, enabled, logger_id=eventhub_logger_id(root),
                            usage_identity=usage_identity_expression(root))
            if name in FIXED_OPERATIONS else
            retired_operation() if name in binding.retired_operations else auxiliary_operation()
        ) for name, _, _ in contracts
    }
    digest = fixed_program_digest(
        compact_fixed_policy(serialize_policy(root)), operations, contracts,
    )
    identity = root.find(".//set-variable[@name='routeProgram']")
    assert identity is not None
    identity.set("value", digest)
    enabled = enabled.model_copy(update={"program_sha256": digest})
    parent = compact_fixed_policy(serialize_policy(root))
    restored = _without_fixed_route_parent(parent, enabled)
    if component_digest(restored, normalize_text_defaults=False) != component_digest(
        baseline, normalize_text_defaults=False,
    ):
        raise PolicyCompilationError("Pool upgrade changed protected authentication or accounting")
    return FixedRouteProgram(
        enabled, text_digest(serialize_policy(baseline)), parent, tuple(operations.items()), contracts,
    )


def validate_fixed_route_parent(
    candidate: str,
    baseline: str,
    binding: FixedRouteBinding,
    *,
    executable: bool = True,
) -> str:
    expected = _baseline_root(baseline, binding)
    root = _without_fixed_route_parent(candidate, binding, executable=executable)
    if component_digest(root, normalize_text_defaults=False) != component_digest(
        expected, normalize_text_defaults=False
    ):
        raise PolicyCompilationError(
            "Fixed route ABI changed protected authentication or accounting"
        )
    return candidate


AUXILIARY_OPERATIONS: Mapping[str, tuple[str, str]] = {
    "models": ("GET", "/models"),
    "storage-route-program-probe": ("HEAD", "/_turnstile/program"),
    "storage-route-configuration-probe": ("HEAD", "/_turnstile/configuration"),
}


def fixed_operation_contracts(
    models_path: str = "/models", messages_path: str = "/messages",
    retired_operations: Mapping[str, tuple[str, str]] | None = None,
    *, decision_enabled: bool = False,
) -> dict[str, tuple[str, str]]:
    if models_path not in {"/models", "/v1/models"}:
        raise PolicyCompilationError("Fixed route catalog path is unsupported")
    if messages_path not in {"/messages", "/v1/messages"}:
        raise PolicyCompilationError("Fixed route Messages path is unsupported")
    contracts = {**FIXED_OPERATIONS, **AUXILIARY_OPERATIONS}
    if not decision_enabled:
        contracts.pop("microsoft-decision")
    contracts["models"] = ("GET", models_path)
    contracts["anthropic-messages"] = ("POST", messages_path)
    contracts["anthropic-count-tokens"] = ("POST", messages_path + "/count_tokens")
    for name, (method, path) in sorted((retired_operations or {}).items()):
        if (name in contracts or method != "POST"
                or not re.fullmatch(r"/[A-Za-z0-9._/-]{1,999}", path)
                or "//" in path or ".." in path.split("/")
                or (method, path) in contracts.values()):
            raise PolicyCompilationError("Fixed retired operation contract is invalid")
        contracts[name] = (method, path)
    return contracts


PLATFORM_POLICY_BYTES = 512 * 1024
MAX_POLICY_BYTES = PLATFORM_POLICY_BYTES - 32 * 1024


def compact_fixed_policy(source: str) -> str:
    root = parse_policy(source)
    before = component_digest(root, normalize_text_defaults=False)

    def compact(value: str) -> str:
        if not value.strip():
            return ""
        if not value.startswith("@"):
            return value
        pieces: list[str] = []
        previous_end = 0
        previous_token = ""
        preserve_line_end = False
        delimiters = {"{", "}", "(", ")", "[", "]", ";", ","}
        operators = {"=", "!", "<", ">", "+", "-", "*", "/", "%", "&", "|", "^", "?", ":"}
        for token, start, end in expression_token_spans(value):
            if start > previous_end and pieces:
                newline = preserve_line_end and "\n" in value[previous_end:start]
                if newline:
                    pieces.append("\n")
                    preserve_line_end = False
                elif (
                    previous_token not in delimiters and token not in delimiters
                    and not (token == "." and previous_token.isidentifier())
                    and not (previous_token == "." and token.isidentifier())
                    and not (previous_token in {"return", "case"}
                             and token.startswith(('"', '@"', "'")))
                    and not (token in operators and (
                        previous_token.isidentifier() or previous_token.isdecimal()
                        or previous_token.startswith(('"', '@"', "'"))
                    ))
                    and not (previous_token in operators and (
                        token.isidentifier() or token.isdecimal()
                        or token.startswith(('"', '@"', "'"))
                    ))
                ):
                    pieces.append(" ")
            pieces.append(token)
            preserve_line_end = preserve_line_end or "//" in token
            previous_end = end
            previous_token = token
        return "".join(pieces)

    for node in root.iter():
        for child in list(node):
            if not isinstance(child.tag, str):
                if child.tail and child.tail.strip():
                    raise PolicyCompilationError("Fixed policy has significant comment tail")
                node.remove(child)
        for name, value in node.attrib.items():
            node.set(name, compact(value) if value.startswith("@") else value)
        if node.text:
            node.text = compact(node.text)
        if node.tail:
            node.tail = compact(node.tail)
    candidate = encode_policy_xml(root)
    if component_digest(parse_policy(candidate), normalize_text_defaults=False) != before:
        raise PolicyCompilationError("Fixed policy compaction changed executable tokens")
    return candidate


def auxiliary_operation() -> str:
    root = ET.Element("policies")
    inbound = ET.SubElement(root, "inbound")
    ET.SubElement(inbound, "base")
    deny(inbound, "@(true)")
    ET.SubElement(root, "backend")
    ET.SubElement(ET.SubElement(root, "outbound"), "base")
    ET.SubElement(ET.SubElement(root, "on-error"), "base")
    return serialize_policy(root)


def retired_operation() -> str:
    from .apim_model_availability import retired_availability_denial

    root = ET.Element("policies")
    ET.SubElement(root, "inbound").append(retired_availability_denial())
    ET.SubElement(root, "backend")
    ET.SubElement(ET.SubElement(root, "outbound"), "base")
    ET.SubElement(ET.SubElement(root, "on-error"), "base")
    return serialize_policy(root)


@dataclass(frozen=True)
class FixedRouteProgram:
    binding: FixedRouteBinding
    baseline_sha256: str
    parent_policy: str
    operation_policies: tuple[tuple[str, str], ...]
    operation_contracts: tuple[tuple[str, str, str], ...]

    def installation(
        self, target_identity_sha256: str, gateway_endpoint: str,
    ) -> FixedProgramInstallation:
        return FixedProgramInstallation(
            program=ProgramIdentity(
                scope=self.binding.scope, api_revision=self.binding.api_revision,
                instance_id=self.binding.scope.gateway_id,
                program_sha256=self.binding.program_sha256,
                trust_binding_sha256=self.binding.trust_binding_sha256,
            ),
            target_identity_sha256=target_identity_sha256,
            storage_endpoint=self.binding.table_endpoint,
            storage_table=self.binding.table_name,
            gateway_endpoint=gateway_endpoint,
            publisher_subscription_id=self.binding.publisher_subscription_id,
            parent_policy_sha256=fixed_policy_digest(self.parent_policy),
            operation_policies_sha256={
                name: fixed_policy_digest(policy) for name, policy in self.operation_policies
            },
            operation_contracts={
                name: (method, path) for name, method, path in self.operation_contracts
            },
            retired_operations=self.binding.retired_operations,
            supported_profiles=self.binding.supported_profiles,
            credential_vault_origin=(self.binding.trusted_vault_origins[0]
                                     if self.binding.trusted_vault_origins else None),
            trusted_vault_origins=self.binding.trusted_vault_origins,
            image_limits=self.binding.image_limits,
            oauth_enabled=self.binding.oauth_enabled,
            pool_enabled=self.binding.pool_enabled,
            router_enabled=self.binding.router_enabled,
            decision_enabled=self.binding.decision_enabled,
            model_additions_enabled=self.binding.model_additions_enabled,
            native_gateway_scope=self.binding.native_gateway_scope,
            legacy_program_sha256=self.binding.legacy_program_sha256,
            predecessor_program_sha256=self.binding.predecessor_program_sha256,
            supported_adapters=self.binding.supported_adapters,
        )

    @property
    def artifact_sha256(self) -> str:
        return text_digest(
            canonical_document(
                {
                    "binding": self.binding.model_dump(mode="json"),
                    "baseline": self.baseline_sha256,
                    "parent": text_digest(self.parent_policy),
                    "operations": {
                        name: text_digest(policy) for name, policy in self.operation_policies
                    },
                    "contracts": self.operation_contracts,
                }
            )
        )


def fixed_policy_digest(source: str) -> str:
    root = parse_policy(source)
    for node in root.iter():
        if len(node) and node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return policy_document_digest(serialize_policy(root))


def fixed_program_digest(
    parent: str, operations: Mapping[str, str], contracts: tuple[tuple[str, str, str], ...]
) -> str:
    root = parse_policy(parent)
    markers = root.findall(".//set-variable[@name='routeProgram']")
    if len(markers) != 1:
        raise PolicyCompilationError("Fixed route program identity is ambiguous")
    markers[0].set("value", "0" * 64)
    return text_digest(
        canonical_document(
            {
                "parent": fixed_policy_digest(serialize_policy(root)),
                "operations": {
                    name: fixed_policy_digest(policy) for name, policy in operations.items()
                },
                "contracts": contracts,
            }
        )
    )


def compile_fixed_route_program(
    baseline: str,
    binding: FixedRouteBinding,
    *,
    canonical_parent: str,
) -> FixedRouteProgram:
    from .apim_budget_execution import eventhub_logger_id, usage_identity_expression

    binding = FixedRouteBinding.model_validate_json(binding.model_dump_json(), strict=True)
    validate_parent_policy(baseline, canonical_parent, image_program_profiles(binding))
    baseline_root = _baseline_root(baseline)
    if len(baseline_root.findall(".//set-variable[@name='budgetExecutionFence']")) != 1:
        raise PolicyCompilationError("Fixed route program requires an explicit budget fence")
    validate_policy_upload(
        baseline,
        gateway_profile_id=UUID(binding.scope.target_id),
        api_id=binding.scope.api_id,
        api_revision=binding.api_revision,
        table_endpoint=binding.ledger_table_endpoint,
        table_name=binding.ledger_table_name,
    )
    operations = {
        name: compact_fixed_policy(fixed_operation(
            name, binding, logger_id=eventhub_logger_id(baseline_root),
            usage_identity=usage_identity_expression(baseline_root),
        )) for name in FIXED_OPERATIONS
        if name != "microsoft-decision" or binding.decision_enabled
    }
    operations.update(
        {name: compact_fixed_policy(auxiliary_operation()) for name in AUXILIARY_OPERATIONS}
    )
    operations.update({
        name: compact_fixed_policy(retired_operation()) for name in sorted(binding.retired_operations)
    })
    contracts = tuple(
        (name, method, path)
        for name, (method, path) in fixed_operation_contracts(
            binding.models_path, binding.messages_path, binding.retired_operations,
            decision_enabled=binding.decision_enabled,
        ).items()
    )
    neutral = binding.model_copy(update={"program_sha256": "0" * 64})
    parent = compact_fixed_policy(compose_fixed_route_parent(baseline, neutral))
    program = fixed_program_digest(parent, operations, contracts)
    bound = binding.model_copy(update={"program_sha256": program})
    candidate = FixedRouteProgram(
        bound,
        text_digest(baseline),
        compact_fixed_policy(compose_fixed_route_parent(baseline, bound)),
        tuple(operations.items()),
        contracts,
    )
    validate_fixed_route_program(candidate, baseline, bound, canonical_parent=canonical_parent)
    return candidate


def validate_fixed_route_program(
    candidate: FixedRouteProgram,
    baseline: str,
    expected: FixedRouteBinding,
    *,
    canonical_parent: str,
) -> None:
    from .apim_budget_execution import eventhub_logger_id, usage_identity_expression

    validate_parent_policy(baseline, canonical_parent, image_program_profiles(expected))
    if candidate.binding != expected or candidate.baseline_sha256 != text_digest(baseline):
        raise PolicyCompilationError("Fixed program source or target binding changed")
    validate_policy_upload(
        baseline,
        gateway_profile_id=UUID(expected.scope.target_id),
        api_id=expected.scope.api_id,
        api_revision=expected.api_revision,
        table_endpoint=expected.ledger_table_endpoint,
        table_name=expected.ledger_table_name,
    )
    validate_fixed_route_parent(candidate.parent_policy, baseline, expected)
    contracts = tuple(
        (name, method, path)
        for name, (method, path) in fixed_operation_contracts(
            expected.models_path, expected.messages_path, expected.retired_operations,
            decision_enabled=expected.decision_enabled,
        ).items()
    )
    operations = dict(candidate.operation_policies)
    if (
        len(operations) != len(candidate.operation_policies)
        or set(operations) != {name for name, _, _ in contracts}
        or candidate.operation_contracts != contracts
    ):
        raise PolicyCompilationError("Fixed program operation set is incomplete or changed")
    baseline_root = parse_policy(baseline)
    for name, policy in operations.items():
        expected_policy = (
            fixed_operation(name, expected, logger_id=eventhub_logger_id(baseline_root),
                            usage_identity=usage_identity_expression(baseline_root))
            if name in FIXED_OPERATIONS
            else retired_operation() if name in expected.retired_operations else auxiliary_operation()
        )
        validate_executable_policy(policy)
        from .apim_policy_components import validate_native_stream_policy

        validate_native_stream_policy(policy, name)
        if component_digest(
            parse_policy(policy), normalize_text_defaults=False
        ) != component_digest(parse_policy(expected_policy), normalize_text_defaults=False):
            raise PolicyCompilationError("Fixed program operation integrity changed")
    for policy in (candidate.parent_policy, *operations.values()):
        if len(policy.encode()) > MAX_POLICY_BYTES:
            raise PolicyCompilationError("Fixed program exceeds the per-policy byte limit")
    if (
        fixed_program_digest(candidate.parent_policy, operations, contracts)
        != expected.program_sha256
    ):
        raise PolicyCompilationError("Fixed program complete digest differs")
