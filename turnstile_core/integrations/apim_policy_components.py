from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree as ET

from ..domain.image_profiles import ImageGenerationProfile, validate_image_profile
from .apim_control_plane_contract import PolicyCompilationError

IMAGE_POLICY_VERSION = 2
IMAGE_CONDITION = '@((bool)context.Variables["isImagesOperation"])'
TEXT_CONDITION = '@(!(bool)context.Variables["isImagesOperation"])'
POOL_RUNTIME_HEADER = "x-turnstile-pool-runtime"
_TOKENS = re.compile(
    r'//[^\r\n]*|/\*.*?\*/|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]",
    re.DOTALL,
)
CACHE_READ_WITHOUT_FALLBACK = '?? (long?)usage?["cache_read_input_tokens"] ?? 0;'

CACHE_READ_WITH_FALLBACK = (
    '?? (long?)usage?["cache_read_input_tokens"]\n          ?? (long?)usage?["cached_tokens"] ?? 0;'
)

_BOUND_NAMES = ("maxOutputBound", "applicationMaxOutputBound")
_FIT_NAMES = ("budgetRequestCannotFit", "applicationBudgetCannotFit")
NATIVE_STREAM_OPERATIONS = frozenset({"chat-completions", "responses", "anthropic-messages"})
NATIVE_STREAM_REQUEST = '''@{ try {
    var body = context.Request.Body.As<JObject>(preserveContent: true);
    return body?["stream"]?.Type == JTokenType.Boolean && (bool)body["stream"];
} catch { return false; } }'''


def validate_native_stream_policy(source: str, operation_id: str) -> str:
    if operation_id not in NATIVE_STREAM_OPERATIONS:
        return source
    root = parse_policy(source)
    backend = root.find("backend")
    if backend is None or any(len(root.findall(f"./{section}/base")) != 1
                              for section in ("inbound", "outbound", "on-error")):
        raise PolicyCompilationError("Native stream policy lost mandatory parent guards")

    def immediate(node: ET.Element) -> bool:
        if node.tag == "forward-request":
            return not list(node) and node.get("buffer-response") == "false"
        if node.tag == "choose":
            children = list(node)
            return bool(children) and children[-1].tag == "otherwise" and all(
                child.tag == "when" for child in children[:-1]
            ) and all(len(child) == 1 and immediate(child[0]) for child in children)
        return node.tag in {"backend", "retry"} and len(node) == 1 and immediate(node[0])

    if immediate(backend):
        return source
    if len(backend) == 1 and backend[0].tag == "choose":
        branches = list(backend[0])
        if (len(branches) == 2 and branches[0].tag == "when" and branches[1].tag == "otherwise"
                and expression_tokens(branches[0].get("condition", ""))
                == expression_tokens(NATIVE_STREAM_REQUEST)
                and len(branches[0]) == 1 and immediate(branches[0][0])
                and len(branches[1]) == 1 and branches[1][0].tag == "forward-request"
                and not list(branches[1][0])
                and branches[1][0].get("buffer-response", "true") == "true"):
            return source
    raise PolicyCompilationError("Native stream forwarding must be explicit and unbuffered")


def preserve_attribute_whitespace(source: str) -> str:
    def preserve_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        if tag.startswith(("<!--", "<![CDATA[")):
            return tag
        return re.sub(
            r"""(["'])(.*?)\1""",
            lambda attribute: (
                attribute.group(0)
                .replace("\r", "&#13;")
                .replace("\n", "&#10;")
                .replace("\t", "&#9;")
            ),
            tag,
            flags=re.DOTALL,
        )

    return re.sub(
        r"""<!--.*?-->|<!\[CDATA\[.*?\]\]>|<[A-Za-z_][^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>""",
        preserve_tag,
        source,
        flags=re.DOTALL,
    )


def parse_policy(source: str) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.fromstring(preserve_attribute_whitespace(source), parser=parser)


def validate_executable_policy(source: str) -> str:
    root = parse_policy(source)
    unresolved = {
        match
        for node in root.iter()
        for value in (
            (*node.attrib.values(), node.text or "", node.tail or "")
            if isinstance(node.tag, str) else (node.tail or "",)
        )
        for match in re.findall(r"__[A-Z][A-Z0-9_]*__", value)
    }
    if unresolved:
        raise PolicyCompilationError(
            "Executable policy contains unbound parameters: " + ", ".join(sorted(unresolved))
        )
    return source


def validate_executable_parent_policy(source: str, *, gateway_profile_id: UUID) -> str:
    root = parse_policy(validate_executable_policy(source))
    target = str(gateway_profile_id)
    attribution = root.findall(".//set-variable[@name='telemetryGatewayProfileId']")
    if len(attribution) != 1 or attribution[0].get("value") != target:
        raise PolicyCompilationError("Executable policy attribution belongs to another Gateway")
    bindings = root.findall(".//set-variable[@name='availabilityBindingValid']")
    marker = root.find(".//set-variable[@name='availabilityProtocolVersion']")
    if marker is not None and not bindings:
        raise PolicyCompilationError("Executable availability policy has no target binding")
    if bindings:
        from .apim_model_availability import BINDING_VALID

        expected = expression_tokens(BINDING_VALID.replace("__GATEWAY_PROFILE_ID__", target))
        if any(expression_tokens(node.get("value", "")) != expected for node in bindings):
            raise PolicyCompilationError("Executable availability binding targets another Gateway")
    return source


def validate_policy_upload(
    source: str, *, gateway_profile_id: UUID | None, api_id: str, api_revision: str,
    table_endpoint: str | None, table_name: str | None, operation_id: str | None = None,
) -> str:
    root = parse_policy(validate_executable_policy(source))
    target = str(gateway_profile_id) if gateway_profile_id is not None else None
    expected = {
        "telemetryGatewayProfileId": target,
        "availabilityTargetId": target,
        "availabilityApiId": api_id,
        "availabilityApiRevision": api_revision,
    }
    availability_identity = any(
        root.find(f".//set-variable[@name='{name}']") is not None
        for name in ("availabilityTargetId", "availabilityApiId", "availabilityApiRevision")
    )
    for name, value in expected.items():
        nodes = root.findall(f".//set-variable[@name='{name}']")
        if not nodes and not (availability_identity and name.startswith("availability")):
            continue
        if value is None or len(nodes) != 1 or nodes[0].get("value") != value:
            raise PolicyCompilationError("Executable policy binding does not match " + name)
    guards = root.findall(".//set-variable[@name='availabilityBindingValid']")
    marker = root.find(".//set-variable[@name='availabilityProtocolVersion']")
    if guards or marker is not None:
        from .apim_model_availability import BINDING_VALID, STATE_URL

        if target is None or marker is None or len(guards) != 2:
            raise PolicyCompilationError("Executable availability binding is incomplete")
        condition = expression_tokens(BINDING_VALID.replace("__GATEWAY_PROFILE_ID__", target))
        if any(expression_tokens(node.get("value", "")) != condition for node in guards):
            raise PolicyCompilationError("Executable availability binding targets another Gateway")
        if not table_endpoint or not table_name:
            raise PolicyCompilationError("Executable availability storage binding is unconfigured")
        for name, value in (
            ("availabilityTableEndpoint", table_endpoint.rstrip("/")),
            ("availabilityTableName", table_name),
        ):
            nodes = root.findall(f".//set-variable[@name='{name}']")
            if len(nodes) != 1 or nodes[0].get("value") != value:
                raise PolicyCompilationError(
                    "Executable availability storage binding does not match " + name
                )
        state_url = expression_tokens(STATE_URL.replace(
            "__LEDGER_TABLE_ENDPOINT__", table_endpoint.rstrip("/"),
        ).replace("__LEDGER_TABLE_NAME__", table_name))
        reads = root.findall(".//send-request[@response-variable-name='availabilityRead']")
        if len(reads) != 2 or any(
            len(node.findall("set-url")) != 1
            or expression_tokens(node.findtext("set-url", "")) != state_url for node in reads
        ):
            raise PolicyCompilationError("Executable availability state URL binding differs")
    for node in root.findall(".//set-variable[@name='selectedBudgetCountContract']"):
        value = node.attrib.get("value", "")
        if value.startswith("@"):
            expected_contract = r'''@{
                var route = JObject.Parse((string)context.Variables["routeContent"]);
                return new JObject(new JProperty("version", 1),
                    new JProperty("epoch", route["budget_epoch"]),
                    new JProperty("gateway_profile_id", route["scope"]["target_id"]),
                    new JProperty("model_key", route["alias"]),
                    new JProperty("contract", route["budget"].DeepClone()))
                    .ToString(Newtonsoft.Json.Formatting.None);
            }'''
            abi = root.findall(".//set-variable[@name='storageRouteAbi']")
            if (len(abi) != 1 or abi[0].get("value") != "2"
                    or target is None or not availability_identity
                    or expression_tokens(value) != expression_tokens(expected_contract)):
                raise PolicyCompilationError("Executable route budget contract binding is invalid")
            continue
        try:
            snapshot = json.loads(value)
        except (KeyError, TypeError, ValueError):
            raise PolicyCompilationError("Executable budget contract binding is invalid") from None
        if (
            not isinstance(snapshot, dict) or target is None
            or snapshot.get("gateway_profile_id") != target
        ):
            raise PolicyCompilationError(
                "Executable budget contract binding targets another Gateway"
            )
    if operation_id is not None:
        validate_native_stream_policy(source, operation_id)
    return source


def policy_document_digest(source: str) -> str:
    canonical = ET.canonicalize(preserve_attribute_whitespace(source), with_comments=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialize_policy(root: ET.Element) -> str:
    fragments = re.split(
        r"(<!--.*?-->|<[^>]*>)", ET.tostring(root, encoding="unicode"), flags=re.DOTALL
    )

    def restore(tag: str) -> str:
        if tag.startswith("<!--"):
            return tag
        return re.sub(
            r"""(["'])(.*?)\1""",
            lambda attribute: (
                attribute.group(0)
                .replace("&#13;", "\r")
                .replace("&#10;", "\n")
                .replace("&#09;", "\t")
                .replace("&#9;", "\t")
            ),
            tag,
            flags=re.DOTALL,
        )

    return "".join(
        restore(part) if index % 2 else part.replace('"', "&quot;")
        for index, part in enumerate(fragments)
    )


@lru_cache(maxsize=2048)
def expression_token_spans(value: str) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (match.group(), match.start(), match.end())
        for match in _TOKENS.finditer(value)
        if not match.group().startswith(("//", "/*"))
    )


@lru_cache(maxsize=2048)
def expression_tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token, _, _ in expression_token_spans(value))


def upgrade_application_tpm_policy(source: str) -> str:
    root = parse_policy(source)
    limits = [node for node in root.iter("llm-token-limit")
              if "finops:subscription:" in node.get("counter-key", "")]
    if len(limits) != 1:
        raise PolicyCompilationError("Subscription token limit is missing or ambiguous")
    limit = limits[0]
    consumer = _variable(root, "consumerId")
    parents = {child: parent for parent in root.iter() for child in parent}
    parent = parents[consumer]
    ordered = list(parent.iter())
    if (
        parent.tag != "when"
        or limit not in ordered
        or ordered.index(consumer) >= ordered.index(limit)
    ):
        raise PolicyCompilationError("Subscription token limit is outside its identity branch")
    variables = root.findall(".//set-variable[@name='applicationTokensPerMinute']")
    attribute = '@((int)(long)context.Variables["applicationTokensPerMinute"])'
    legacy_attribute = '@((long)context.Variables["applicationTokensPerMinute"])'
    configured = limit.get("tokens-per-minute", "")
    if variables:
        if len(variables) != 1 or configured not in {attribute, legacy_attribute}:
            raise PolicyCompilationError("Application token limit upgrade is incomplete")
        if (
            parents[variables[0]] is not parent
            or not ordered.index(consumer) < ordered.index(variables[0]) < ordered.index(limit)
        ):
            raise PolicyCompilationError(
                "Application token rate must precede its subscription limit"
            )
        match = re.search(r"long\s+defaultRate\s*=\s*([0-9]+|__TOKENS_PER_MINUTE__)\s*;",
                          variables[0].get("value", ""))
        if match is None:
            raise PolicyCompilationError("Application token limit default is unavailable")
        configured = match[1]
    if configured != "__TOKENS_PER_MINUTE__" and (
        not configured.isdecimal() or not 0 < int(configured) <= 2_147_483_647
    ):
        raise PolicyCompilationError("Subscription token limit must retain a positive default")
    expression = '''@{
          long defaultRate = DEFAULT_RATE;
          if (context.Variables.GetValueOrDefault<string>("applicationAdmission", "") != "ok"
            || !context.Variables.ContainsKey("applicationLedgerState")) { return defaultRate; }
          var state = (JObject)context.Variables["applicationLedgerState"];
          if (!((bool?)state["available"] ?? false)
            || (string)state["budgetStatus"] != "ok") { return defaultRate; }
          long configured = (long?)state["tokensPerMinute"] ?? 0;
          return configured > 0 && configured <= int.MaxValue ? configured : defaultRate;
        }'''.replace("DEFAULT_RATE", configured)
    if variables:
        if expression_tokens(variables[0].get("value", "")) != expression_tokens(expression):
            raise PolicyCompilationError(
                "Application token limit differs from the reviewed contract"
            )
        if limit.get("tokens-per-minute") == legacy_attribute:
            limit.set("tokens-per-minute", attribute)
            return serialize_policy(root)
        return source
    parent.insert(list(parent).index(consumer) + 1, ET.Element(
        "set-variable", {"name":"applicationTokensPerMinute", "value":expression},
    ))
    limit.set("tokens-per-minute", attribute)
    return serialize_policy(root)


def component_digest(node: ET.Element, *, normalize_text_defaults: bool = True) -> str:
    def normalized(value: str) -> object:
        if not value.strip().startswith("@"):
            return value.strip()
        tokens = list(expression_tokens(value))
        for index in range(len(tokens) - 4):
            if normalize_text_defaults and tokens[index : index + 3] == ["long", "fallback", "="]:
                tokens[index + 3] = "__DEFAULT_MAX_OUTPUT_TOKENS__"
        return tokens

    def shape(element: ET.Element) -> object:
        return [
            element.tag,
            {key: normalized(value) for key, value in element.attrib.items()},
            normalized(element.text or ""),
            [shape(child) for child in element if isinstance(child.tag, str)],
        ]

    return hashlib.sha256(json.dumps(shape(node), sort_keys=True).encode()).hexdigest()


def _one(nodes: list[ET.Element], label: str) -> ET.Element:
    if len(nodes) != 1:
        raise PolicyCompilationError(f"Required policy component is missing or ambiguous: {label}")
    return nodes[0]


def _variable(root: ET.Element, name: str) -> ET.Element:
    return _one(root.findall(f".//set-variable[@name='{name}']"), name)


def _usage(root: ET.Element) -> ET.Element:
    return _one(
        root.findall("./outbound/choose/otherwise/set-variable[@name='usagePayload']"),
        "buffered usage",
    )


def _body(root: ET.Element) -> ET.Element:
    return _one(
        [node for node in root.iter("set-body") if "clamp" in expression_tokens(node.text or "")],
        "request normalization",
    )


def legacy_components(root: ET.Element) -> dict[str, ET.Element]:
    if root.find("inbound/set-variable[@name='imageGenerationPolicyVersion']") is not None:
        current = _current_components(root)
        inference = deepcopy(current["isInferenceOperation"])
        prefix = '@((bool)context.Variables["isImagesOperation"] || '
        value = inference.get("value", "")
        if not value.startswith(prefix) or not value.endswith(")"):
            raise PolicyCompilationError("Canonical image inference classification is invalid")
        inference.set("value", "@(" + value[len(prefix):-1] + ")")
        return {
            "isInferenceOperation": inference,
            **{
                name: _one(current[name].findall(
                    f"otherwise/set-variable[@name='{name}']",
                ), name)
                for name in (*_BOUND_NAMES, *_FIT_NAMES)
            },
            "request_body": _one(current["request_body"].findall("when/set-body"),
                                  "canonical text request normalization"),
            "usage": _one(current["usage"].findall(
                "otherwise/set-variable[@name='usagePayload']",
            ), "canonical text usage"),
        }
    return {
        **{
            name: _variable(root, name)
            for name in ("isInferenceOperation", *_BOUND_NAMES, *_FIT_NAMES)
        },
        "request_body": _body(root),
        "usage": _usage(root),
    }


def _image_usage() -> ET.Element:
    node = ET.Element("set-variable", {"name": "usagePayload"})
    node.set(
        "value",
        """@{
      try {
        var body = context.Response.Body.As<JObject>(preserveContent: true);
        var usage = body["usage"] as JObject;
        var incoming = usage?["input_tokens_details"] as JObject;
        var outgoing = usage?["output_tokens_details"] as JObject;
        var counters = new[] { usage?["input_tokens"], usage?["output_tokens"],
          usage?["total_tokens"], incoming?["text_tokens"], incoming?["image_tokens"],
          outgoing?["text_tokens"], outgoing?["image_tokens"] };
        if (counters.Any(token => token == null || token.Type != JTokenType.Integer
          || (long)token < 0)) { throw new Exception(); }
        long input = (long)usage["input_tokens"];
        long output = (long)usage["output_tokens"];
        var cachedValue = incoming?["cached_tokens"];
        if (cachedValue != null && (cachedValue.Type != JTokenType.Integer
          || (long)cachedValue < 0 || (long)cachedValue > input)) { throw new Exception(); }
        long cached = cachedValue == null ? 0L : (long)cachedValue;
        if ((long)incoming["image_tokens"] != 0 || (long)outgoing["text_tokens"] != 0
          || (long)incoming["text_tokens"] != input || (long)outgoing["image_tokens"] != output
          || (long)usage["total_tokens"] != input + output) { throw new Exception(); }
        return new JObject(new JProperty("input_tokens", input - cached),
          new JProperty("cached_tokens", cached), new JProperty("cache_write_tokens", 0),
          new JProperty("output_tokens", output), new JProperty("estimated", false),
          new JProperty("ingest_error", null));
      } catch {
        return new JObject(new JProperty("input_tokens", null),
          new JProperty("cached_tokens", null), new JProperty("cache_write_tokens", null),
          new JProperty("output_tokens", null), new JProperty("estimated", true),
          new JProperty("ingest_error", "usage_parse_failed"));
      }
    }""",
    )
    return node


def _branch(normal: ET.Element, image: ET.Element | None) -> ET.Element:
    choose = ET.Element("choose")
    if image is None:
        ET.SubElement(choose, "when", {"condition": TEXT_CONDITION}).append(deepcopy(normal))
    else:
        ET.SubElement(choose, "when", {"condition": IMAGE_CONDITION}).append(image)
        ET.SubElement(choose, "otherwise").append(deepcopy(normal))
    return choose


def _components(reference: dict[str, ET.Element]) -> dict[str, ET.Element]:
    inference = deepcopy(reference["isInferenceOperation"])
    original = inference.attrib["value"]
    inference.set(
        "value", '@((bool)context.Variables["isImagesOperation"] || ' + original[2:-1] + ")"
    )
    result = {"isInferenceOperation": inference}
    for name in _BOUND_NAMES:
        image = ET.Element(
            "set-variable",
            {"name": name, "value": '@((long)context.Variables["imageOutputBound"])'},
        )
        result[name] = _branch(reference[name], image)
    for name in _FIT_NAMES:
        prefix = "application" if name.startswith("application") else ""
        input_name = prefix + "InputBound" if prefix else "inputBound"
        remaining_name = prefix + "LedgerRemaining" if prefix else "ledgerRemaining"
        enforced_name = prefix + "LedgerEnforced" if prefix else "ledgerEnforced"
        expression = f'''@{{
          if (!(bool)context.Variables["{enforced_name}"]) {{ return false; }}
          long input = (long)context.Variables["{input_name}"];
          long remaining = (long)context.Variables["{remaining_name}"];
                    return input >= remaining
                        || (long)context.Variables["imageOutputBound"] > remaining - input;
        }}'''
        result[name] = _branch(
            reference[name], ET.Element("set-variable", {"name": name, "value": expression})
        )
    result["request_body"] = _branch(reference["request_body"], None)
    result["usage"] = _branch(reference["usage"], _image_usage())
    return result


def _parents(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _replace(root: ET.Element, old: ET.Element, new: ET.Element) -> None:
    parent = _parents(root)[old]
    new.tail = old.tail
    index = list(parent).index(old)
    parent.remove(old)
    parent.insert(index, new)


def _current_components(root: ET.Element) -> dict[str, ET.Element]:
    parents = _parents(root)
    result = {"isInferenceOperation": _variable(root, "isInferenceOperation")}
    for name in (*_BOUND_NAMES, *_FIT_NAMES):
        nodes = root.findall(f".//set-variable[@name='{name}']")
        owners = {
            parents.get(parent) for node in nodes if (parent := parents.get(node)) is not None
        }
        result[name] = _one([owner for owner in owners if owner is not None], name)
    body = _body(root)
    result["request_body"] = parents[parents[body]]
    result["usage"] = _one(root.findall("./outbound/choose/otherwise/choose"), "image usage")
    return result


def _materialize_defaults(reference: dict[str, ET.Element], actual: dict[str, ET.Element]) -> None:
    for name in _BOUND_NAMES:
        candidates = [
            node
            for node in actual[name].iter("set-variable")
            if "fallback" in expression_tokens(node.get("value", ""))
        ]
        value = _one(candidates, name).attrib["value"]
        tokens = expression_tokens(value)
        limits = [
            tokens[index + 3]
            for index in range(len(tokens) - 4)
            if tokens[index : index + 3] == ("long", "fallback", "=")
        ]
        if len(limits) != 1 or not (
            limits[0].isdigit() or limits[0] == "__DEFAULT_MAX_OUTPUT_TOKENS__"
        ):
            raise PolicyCompilationError("The existing text output default cannot be adopted")
        reference[name].set(
            "value",
            reference[name].attrib["value"].replace("__DEFAULT_MAX_OUTPUT_TOKENS__", limits[0]),
        )


def _matches_template_value(actual: str, expected: str, bindings: dict[str, str]) -> bool:
    if actual == expected:
        return True
    placeholders = re.split(r"(__[A-Z][A-Z0-9_]*__)", expected)
    pattern = ""
    names: list[str] = []
    for part in placeholders:
        if re.fullmatch(r"__[A-Z][A-Z0-9_]*__", part):
            if part in bindings:
                pattern += re.escape(bindings[part])
            elif part in names:
                pattern += f"(?P={part})"
            else:
                constraint = {
                    "__LEDGER_TABLE_ENDPOINT__": r"https://[A-Za-z0-9.-]+(?::[0-9]+)?",
                    "__LEDGER_TABLE_NAME__": r"[A-Za-z][A-Za-z0-9]{2,62}",
                }.get(part, ".+?")
                pattern += f"(?P<{part}>{constraint})"
                names.append(part)
        else:
            pattern += re.escape(part)
    match = re.fullmatch(pattern, actual)
    if match is None:
        return False
    bindings.update({name: match.group(name) for name in names})
    return True


def _matches_policy_component(
    actual: ET.Element, expected: ET.Element, bindings: dict[str, str],
) -> bool:
    if actual.tag != expected.tag or actual.attrib.keys() != expected.attrib.keys():
        return False
    values = [(actual.get(name, ""), value) for name, value in expected.attrib.items()]
    values.append(((actual.text or "").strip(), (expected.text or "").strip()))
    for actual_value, expected_value in values:
        if expected_value.startswith("@"):
            actual_tokens = expression_tokens(actual_value)
            expected_tokens = expression_tokens(expected_value)
            if expected.tag == "log-to-eventhub":
                actual_tokens = _telemetry_template_tokens(actual_tokens)
                expected_tokens = _telemetry_template_tokens(expected_tokens)
            if len(actual_tokens) != len(expected_tokens) or not all(
                _matches_template_value(observed, template, bindings)
                for observed, template in zip(actual_tokens, expected_tokens, strict=True)
            ):
                return False
        elif not _matches_template_value(actual_value, expected_value, bindings):
            return False
    actual_children = [child for child in actual if isinstance(child.tag, str)]
    expected_children = [child for child in expected if isinstance(child.tag, str)]
    return len(actual_children) == len(expected_children) and all(
        _matches_policy_component(observed, template, bindings)
        for observed, template in zip(actual_children, expected_children, strict=True)
    )


def _telemetry_template_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    fallback_prefix = expression_tokens(
        'new JProperty("runtime", context.Request.Headers.GetValueOrDefault("x-hive-runtime",'
    )
    result = list(tokens)
    for index in range(len(tokens) - len(fallback_prefix)):
        if tokens[index:index + len(fallback_prefix)] == fallback_prefix:
            fallback_index = index + len(fallback_prefix)
            if tokens[fallback_index].startswith('"'):
                result[fallback_index] = '"__LEGACY_RUNTIME_DISPLAY_DEFAULT__"'
    return tuple(result)


def _validate_budget_components(root: ET.Element, template: ET.Element) -> dict[str, str]:
    actual_parents = _parents(root)
    expected_parents = _parents(template)
    bindings: dict[str, str] = {}
    for variable_name in (
        "ledgerState", "applicationLedgerState", "applicationTokensPerMinute",
        "effectiveApplicationTpm",
    ):
        if (variable_name == "applicationTokensPerMinute"
            and root.find(".//set-variable[@name='telemetryUsageId']") is None
            and template.find(".//set-variable[@name='telemetryUsageId']") is None
            and not root.findall(f".//set-variable[@name='{variable_name}']")
            and not template.findall(f".//set-variable[@name='{variable_name}']")):
            continue
        actual = actual_parents[_variable(root, variable_name)]
        expected = expected_parents[_variable(template, variable_name)]
        if (
            not _matches_policy_component(actual, expected, bindings)
            or _conditional_ancestry(root, actual) != _conditional_ancestry(template, expected)
        ):
            raise PolicyCompilationError(
                f"Complete budget control flow or component integrity mismatch: {variable_name}"
            )
    return bindings


def _validate_outcome_components(
    root: ET.Element, template: ET.Element, bindings: dict[str, str],
) -> None:
    for section in ("backend", "outbound", "on-error"):
        actual = root.find(section)
        expected = template.find(section)
        if (
            actual is None
            or expected is None
            or not _matches_policy_component(actual, expected, bindings)
        ):
            raise PolicyCompilationError(
                f"Complete {section} control flow or component integrity mismatch"
            )


def _validate_control_flow(
    root: ET.Element, template: ET.Element, reference: dict[str, ET.Element]
) -> None:
    normalized = deepcopy(root)
    template = deepcopy(template)
    policies = [normalized]
    if template.find("inbound/set-variable[@name='imageGenerationPolicyVersion']") is not None:
        policies.append(template)
    else:
        _replace(template, _usage(template), deepcopy(reference["usage"]))
    for policy in policies:
        for name, node in _current_components(policy).items():
            _replace(policy, node, deepcopy(reference[name]))
        for tag in ("llm-token-limit", "llm-emit-token-metric"):
            for node in list(policy.iter(tag)):
                parents = _parents(policy)
                _replace(policy, parents[parents[node]], deepcopy(node))
        inbound = policy.find("inbound")
        assert inbound is not None
        for name in ("isImagesOperation", "imageGenerationPolicyVersion"):
            for node in list(inbound.findall(f"set-variable[@name='{name}']")):
                inbound.remove(node)
    bindings = _validate_budget_components(normalized, template)
    _validate_outcome_components(normalized, template, bindings)


def _conditional_ancestry(
    root: ET.Element, node: ET.Element,
) -> list[tuple[str, tuple[str, ...]]]:
    parents = _parents(root)
    result: list[tuple[str, tuple[str, ...]]] = []
    while node in parents:
        parent = parents[node]
        condition = expression_tokens(parent.get("condition", ""))
        if parent.tag == "otherwise":
            condition = tuple(token for branch in parents[parent].findall("when")
                              for token in expression_tokens(branch.get("condition", "")))
        result.append((parent.tag, condition))
        node = parent
    return result


def _validate_parent_boundaries(root: ET.Element, template: ET.Element) -> None:
    bindings: dict[str, str] = {}
    operation_owned = {
        "imageOutputBound", "imageRequestValid", "selectedModelKnown",
        "selectedRequiresAssignment", "selectedRoutingManaged",
    }
    if any(node.get("name") in operation_owned for node in root.iter("set-variable")):
        raise PolicyCompilationError(
            "Parent policy cannot override operation-owned admission facts"
        )
    names = (
        "callerRequestId", "isAnthropicOperation", "isResponsesCreateOperation",
        "isResponsesCompactOperation", "isResponsesOperation", "hasEmployeeToken",
    )
    for name in names:
        actual = _variable(root, name)
        expected = _variable(template, name)
        if not _matches_policy_component(actual, expected, bindings) or _conditional_ancestry(
            root, actual
        ) != _conditional_ancestry(template, expected):
            raise PolicyCompilationError(f"Parent operation contract mismatch: {name}")
    inference = _variable(root, "isInferenceOperation")
    expected_inference = _variable(template, "isInferenceOperation")
    if (
        root.find("inbound/set-variable[@name='imageGenerationPolicyVersion']") is not None
        and template.find("inbound/set-variable[@name='imageGenerationPolicyVersion']") is None
    ):
        expected_inference = _components(legacy_components(template))["isInferenceOperation"]
    if not _matches_policy_component(inference, expected_inference, bindings):
        raise PolicyCompilationError("Parent inference admission classification is invalid")

    actual_authentication = [node for node in root.iter("validate-azure-ad-token")
                             if node.get("header-name") != "x-turnstile-delegation-token"]
    expected_authentication = [node for node in template.iter("validate-azure-ad-token")
                               if node.get("header-name") != "x-turnstile-delegation-token"]
    if len(actual_authentication) != len(expected_authentication) or any(
        not _matches_policy_component(actual, expected, bindings)
        or _conditional_ancestry(root, actual) != _conditional_ancestry(template, expected)
        for actual, expected in zip(actual_authentication, expected_authentication, strict=True)
    ):
        raise PolicyCompilationError("Parent authentication validation is missing or changed")

    unmanaged = '!context.Variables.GetValueOrDefault<bool>("selectedRoutingManaged", false)'
    allowed_fallbacks = {
        expression_tokens(f"@({unmanaged})"),
        expression_tokens(f'@({unmanaged} && (bool)context.Variables["isAnthropicOperation"])'),
    }
    for backend in root.findall("./inbound//set-backend-service"):
        if not any(
            condition in allowed_fallbacks for _, condition in _conditional_ancestry(root, backend)
        ):
            raise PolicyCompilationError(
                "Legacy backend fallback requires explicit offline migration"
            )

    def unknown_model_guards(policy: ET.Element) -> list[ET.Element]:
        return [node for node in policy.iter("when")
                if '"selectedModelKnown"' in expression_tokens(node.get("condition", ""))]

    actual_guards = unknown_model_guards(root)
    expected_guards = unknown_model_guards(template)
    if len(actual_guards) != len(expected_guards) or not expected_guards:
        raise PolicyCompilationError("Parent unknown-model rejection is incomplete")
    for actual, expected in zip(actual_guards, expected_guards, strict=True):
        if not _matches_policy_component(actual, expected, bindings) or _conditional_ancestry(
            root, actual
        ) != _conditional_ancestry(template, expected):
            raise PolicyCompilationError("Parent unknown-model rejection integrity mismatch")

    actual_inbound = root.find("inbound")
    expected_inbound = template.find("inbound")
    assert actual_inbound is not None and expected_inbound is not None
    expected_snapshots = [node for node in expected_inbound.findall("set-variable")
                          if node.get("name", "").startswith("telemetry")]
    for expected in expected_snapshots:
        actual = _one(
            actual_inbound.findall(f"set-variable[@name='{expected.attrib['name']}']"),
            expected.attrib["name"],
        )
        if actual not in actual_inbound or not _matches_policy_component(
            actual, expected, bindings
        ):
            raise PolicyCompilationError("Parent attribution snapshot integrity mismatch")
    for expected in expected_inbound.iter("set-header"):
        if expected.get("exists-action") != "delete":
            continue
        name = expected.attrib["name"]
        actual = _one(
            root.findall(f"./inbound//set-header[@name='{name}'][@exists-action='delete']"), name
        )
        if not _matches_policy_component(actual, expected, bindings) or _conditional_ancestry(
            root, actual
        ) != _conditional_ancestry(template, expected):
            raise PolicyCompilationError(f"Parent header cleanup integrity mismatch: {name}")
        if name.startswith(
            ("x-hive-", "x-org-", "x-user-", "x-project-", "x-agent-", "x-department-")
        ) and (
            actual not in actual_inbound
            or any(
                list(actual_inbound).index(_one(
                    actual_inbound.findall(f"set-variable[@name='{snapshot.attrib['name']}']"),
                    snapshot.attrib["name"],
                ))
                > list(actual_inbound).index(actual)
                for snapshot in expected_snapshots
            )
        ):
            raise PolicyCompilationError("Attribution must be captured before header cleanup")


def validate_parent_policy(
    source: str, canonical_parent: str, profiles: Sequence[ImageGenerationProfile] = (),
) -> str:
    from .apim_application_tpm import compose_application_tpm_policy
    from .apim_directory_policy import strip_validated_directory_identity_policy

    def without_slots(value: str) -> str:
        return value.replace("__LEGACY_PROVIDER_ROUTING__", "").replace(
            "__USAGE_OBSERVER_HEADERS__", ""
        )

    root, template = (
        parse_policy(compose_application_tpm_policy(
            without_slots(strip_validated_directory_identity_policy(source))
        )),
        parse_policy(compose_application_tpm_policy(
            without_slots(strip_validated_directory_identity_policy(canonical_parent))
        )),
    )
    availability = root.find(".//set-variable[@name='availabilityProtocolVersion']")
    expected_availability = template.find(".//set-variable[@name='availabilityProtocolVersion']")
    if availability is not None or expected_availability is not None:
        from .apim_model_availability import without_availability_parent

        if availability is None or expected_availability is None:
            raise PolicyCompilationError(
                "Model availability requires a compatible canonical parent"
            )
        validate_parent_policy(
            serialize_policy(without_availability_parent(root)),
            serialize_policy(without_availability_parent(template)), profiles,
        )
        return source
    for policy in (root, template):
        if policy.tag != "policies" or [
            node.tag for node in policy if isinstance(node.tag, str)
        ] != [
            "inbound",
            "backend",
            "outbound",
            "on-error",
        ]:
            raise PolicyCompilationError("Parent policy sections are incomplete or out of order")
        for choose in policy.iter("choose"):
            branches = [child.tag for child in choose if isinstance(child.tag, str)]
            if (not branches or branches[0] != "when" or branches.count("otherwise") > 1
                    or any(tag not in {"when", "otherwise"} for tag in branches)
                    or ("otherwise" in branches and branches[-1] != "otherwise")):
                raise PolicyCompilationError(
                    "The parent policy has an invalid direct child under choose"
                )
    execution_marker = root.find("inbound/set-variable[@name='budgetExecutionVersion']")
    expected_execution_marker = template.find(
        "inbound/set-variable[@name='budgetExecutionVersion']"
    )
    if (execution_marker is None) != (expected_execution_marker is None):
        raise PolicyCompilationError("Budget execution requires a compatible canonical parent")
    _validate_parent_boundaries(root, template)
    actual_fence_reads = root.findall(
        ".//send-request[@response-variable-name='budgetExecutionFenceRead']"
    )
    expected_fence_reads = template.findall(
        ".//send-request[@response-variable-name='budgetExecutionFenceRead']"
    )
    if len(actual_fence_reads) != len(expected_fence_reads):
        raise PolicyCompilationError("Budget execution fence integrity mismatch")
    actual_parents, expected_parents = _parents(root), _parents(template)
    for actual_read, expected_read in zip(actual_fence_reads, expected_fence_reads, strict=True):
        actual_fence = actual_parents[actual_parents[actual_read]]
        expected_fence = expected_parents[expected_parents[expected_read]]
        inbound = root.find("inbound")
        base = inbound.find("base") if inbound is not None else None
        if (
            inbound is None or base is None or actual_fence not in inbound
            or list(inbound).index(actual_fence) != list(inbound).index(base) + 1
            or not _matches_policy_component(actual_fence, expected_fence, {})
        ):
            raise PolicyCompilationError("Budget execution fence integrity mismatch")
    if execution_marker is not None:
        actual_inbound = root.find("inbound")
        assert actual_inbound is not None
        expected_inbound = template.find("inbound")
        assert expected_inbound is not None
        observed_gate = _variable(root, "budgetDelegationPresented")
        expected_gate = _variable(template, "budgetDelegationPresented")
        observed_index = list(actual_inbound).index(observed_gate)
        expected_index = list(expected_inbound).index(expected_gate)
        if (
            not _matches_policy_component(observed_gate, expected_gate, {})
            or observed_index + 1 >= len(actual_inbound)
            or not _matches_policy_component(
                list(actual_inbound)[observed_index + 1],
                list(expected_inbound)[expected_index + 1], {},
            )
        ):
            raise PolicyCompilationError("Trusted budget delegation integrity mismatch")
        base = actual_inbound.find("base")
        if base is None or (
            list(actual_inbound).index(execution_marker) > list(actual_inbound).index(base)
        ):
            raise PolicyCompilationError(
                "The execution protocol marker must precede the shared fence"
            )
        expected = _variable(template, "executionModelId")
        actual = _variable(root, "executionModelId")
        actual_parent = _parents(root)[actual]
        expected_parent = _parents(template)[expected]
        if (
            not _matches_policy_component(actual_parent, expected_parent, {})
            or _conditional_ancestry(root, actual) != _conditional_ancestry(template, expected)
            or expression_tokens(execution_marker.get("value", "")) != ("@", "(", "2", ")")
        ):
            raise PolicyCompilationError(
                "Budget execution admission or dispatch integrity mismatch"
            )
        actual_execution = [
            node for node in root.iter("set-variable")
            if node.get("name", "").startswith(("execution", "budgetDelegat"))
        ]
        expected_execution = [
            node for node in template.iter("set-variable")
            if node.get("name", "").startswith(("execution", "budgetDelegat"))
        ]
        if len(actual_execution) != len(expected_execution) or any(
            not _matches_policy_component(observed, reference, {})
            or _conditional_ancestry(root, observed) != _conditional_ancestry(template, reference)
            for observed, reference in zip(actual_execution, expected_execution, strict=True)
        ):
            raise PolicyCompilationError(
                "Execution variables cannot be shadowed outside their protocol"
            )
    marker = root.find("inbound/set-variable[@name='imageGenerationPolicyVersion']")
    if marker is not None:
        if expression_tokens(marker.get("value", "")) != ("@", "(", "2", ")"):
            raise PolicyCompilationError("Legacy image policy requires explicit offline migration")
        normalized_source = compose_application_tpm_policy(
            strip_validated_directory_identity_policy(source)
        )
        normalized_canonical = compose_application_tpm_policy(
            strip_validated_directory_identity_policy(canonical_parent)
        )
        if compose_image_parent_policy(
            normalized_source, normalized_canonical, profiles,
        ) != normalized_source:
            raise PolicyCompilationError("Normal publication cannot migrate its parent policy")
    else:
        bindings = _validate_budget_components(root, template)
        _validate_outcome_components(root, template, bindings)
    return source


def validate_parent_readback(
    source: str, canonical_parent: str, profiles: Sequence[ImageGenerationProfile],
    *, expected_contract: object, expected_raw: object,
) -> None:
    validate_parent_policy(source, canonical_parent, profiles)
    if not parent_readback_matches(
        source, expected_contract=expected_contract, expected_raw=expected_raw
    ):
        raise PolicyCompilationError(
            "APIM parent readback does not match the recorded release contract"
        )


def parent_readback_matches(
    source: str, *, expected_contract: object, expected_raw: object
) -> bool:
    if isinstance(expected_contract, str):
        try:
            return (
                component_digest(parse_policy(source), normalize_text_defaults=False)
                == expected_contract
            )
        except ET.ParseError:
            return False
    return (
        isinstance(expected_raw, str)
        and hashlib.sha256(source.encode()).hexdigest() == expected_raw
    )


def _token_wrapper(node: ET.Element, burst_tokens: int) -> ET.Element:
    wrapper = ET.Element("choose")
    ET.SubElement(wrapper, "when", {"condition": TEXT_CONDITION}).append(deepcopy(node))
    if node.tag == "llm-token-limit":
        ET.SubElement(
            ET.SubElement(wrapper, "otherwise"),
            "rate-limit-by-key",
            {
                "calls": node.attrib["tokens-per-minute"],
                "renewal-period": "60",
                "counter-key": node.attrib["counter-key"].replace("finops:", "finops:images:"),
                "increment-count": str(burst_tokens),
                "retry-after-header-name": "Retry-After",
            },
        )
    return wrapper


def compose_image_parent_policy(
    source: str,
    canonical_parent: str,
    profiles: Sequence[ImageGenerationProfile],
    *,
    previous_profiles: Sequence[ImageGenerationProfile] | None = None,
) -> str:
    root = parse_policy(source)
    template = parse_policy(canonical_parent)
    inbound = root.find("inbound")
    if inbound is None:
        raise PolicyCompilationError("Image admission requires a parent inbound policy")
    marker = inbound.find("set-variable[@name='imageGenerationPolicyVersion']")
    markers = root.findall(".//set-variable[@name='imageGenerationPolicyVersion']")
    flags = root.findall(".//set-variable[@name='isImagesOperation']")
    if markers and (len(markers) != 1 or marker is not markers[0]):
        raise PolicyCompilationError("Image policy version must be defined exactly once in inbound")
    if marker is None and flags:
        raise PolicyCompilationError("Unversioned parent cannot define image admission state")
    version = 0 if marker is None else {"@(1)": 1, "@(2)": 2}.get(marker.get("value", ""), -1)
    if version < 0:
        raise PolicyCompilationError("Unsupported image admission policy version")
    burst_tokens = max(
        (validate_image_profile(profile).burst_reservation_tokens for profile in profiles),
        default=1,
    )
    previous_burst = (
        json.loads(Path(__file__).with_name("image_policy_v1.json").read_text())["burst_tokens"]
        if version == 1
        else max(
            (
                validate_image_profile(profile).burst_reservation_tokens
                for profile in (profiles if previous_profiles is None else previous_profiles)
            ),
            default=1,
        )
    )
    if (not profiles or previous_profiles is not None and not previous_profiles) and template.find(
        "inbound/set-variable[@name='imageGenerationPolicyVersion']",
    ) is not None:
        counts = {node.get("increment-count") for node in template.iter("rate-limit-by-key")
                  if node.get("counter-key", "").startswith('@($"finops:images:')}
        if len(counts) != 1:
            raise PolicyCompilationError("Canonical image burst contract is ambiguous")
        count = counts.pop()
        if count is None or not count.isdecimal() or int(count) <= 0:
            raise PolicyCompilationError("Canonical image burst contract is invalid")
        previous_burst = int(count)
        if not profiles:
            burst_tokens = previous_burst
    reference = legacy_components(template)
    actual = _current_components(root) if version == 2 else legacy_components(root)
    _materialize_defaults(reference, actual)
    legacy_hashes = json.loads(Path(__file__).with_name("image_policy_v1.json").read_text())
    if version == 1:
        if any(
            component_digest(node)
            not in (
                legacy_hashes[name]
                if isinstance(legacy_hashes[name], list)
                else [legacy_hashes[name]]
            )
            for name, node in actual.items()
        ):
            raise PolicyCompilationError("Legacy image policy guards are incomplete or changed")
        reference["usage"] = deepcopy(actual["usage"])
    elif version == 2:
        text_usage = actual["usage"].find("otherwise/set-variable[@name='usagePayload']")
        if text_usage is not None and component_digest(text_usage) in legacy_hashes["usage"]:
            reference["usage"] = deepcopy(text_usage)
    generated = _components(reference)
    if version != 1:
        expected = generated if version == 2 else reference
        if any(
            component_digest(node) != component_digest(expected[name])
            for name, node in actual.items()
        ):
            raise PolicyCompilationError("Image policy component integrity mismatch")
    image_flag = inbound.find("set-variable[@name='isImagesOperation']")
    flag = ET.Element(
        "set-variable",
        {"name": "isImagesOperation", "value": '@(context.Operation.Id == "images-generations")'},
    )
    if version:
        if (len(flags) != 1 or image_flag is not flags[0]
            or component_digest(image_flag) != component_digest(flag)):
            raise PolicyCompilationError("Image operation guard is missing or changed")
        if list(inbound).index(image_flag) >= list(inbound).index(actual["isInferenceOperation"]):
            raise PolicyCompilationError("Image operation guard must precede inference admission")
    else:
        inbound.insert(list(inbound).index(actual["isInferenceOperation"]), flag)
    for name, node in actual.items():
        if version != 2:
            _replace(root, node, generated[name])
    for tag in ("llm-token-limit", "llm-emit-token-metric"):
        nodes = list(root.iter(tag))
        if len(nodes) != len(list(template.iter(tag))):
            raise PolicyCompilationError("A governed text policy component is missing")
        for node in nodes:
            expected_wrapper = _token_wrapper(node, previous_burst)
            if version:
                parents = _parents(root)
                parent = parents.get(node)
                wrapper = parents.get(parent) if parent is not None else None
                if wrapper is None or component_digest(wrapper) != component_digest(
                    expected_wrapper
                ):
                    raise PolicyCompilationError("Image token policy isolation is incomplete")
                if burst_tokens != previous_burst:
                    _replace(root, wrapper, _token_wrapper(node, burst_tokens))
            else:
                _replace(root, node, _token_wrapper(node, burst_tokens))
    if not root.findall("./outbound/choose/when/log-to-eventhub"):
        raise PolicyCompilationError("Image telemetry sink is missing")
    _validate_control_flow(root, template, reference)
    if version == 2 and burst_tokens == previous_burst:
        return source
    if marker is not None:
        inbound.remove(marker)
    ET.SubElement(
        inbound, "set-variable", {"name": "imageGenerationPolicyVersion", "value": "@(2)"}
    )
    return serialize_policy(root)
