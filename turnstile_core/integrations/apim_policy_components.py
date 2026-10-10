from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from copy import deepcopy
from xml.etree import ElementTree as ET

from ..domain.image_profiles import ImageGenerationProfile, validate_image_profile
from .apim_control_plane_contract import PolicyCompilationError
from .apim_image_policy import IMAGE_OPERATION_ID, IMAGE_POLICY_VERSION

IMAGE_CONDITION = '@((bool)context.Variables["isImagesOperation"])'
TEXT_CONDITION = '@(!(bool)context.Variables["isImagesOperation"])'
# The v1.1 parent contract grew two elements that have nothing to do with images.
# An installed v1.0 policy lacks both, so the upgrade has to adopt them here as well;
# otherwise validate_parent_policy rejects every real v1.0 gateway.
POOL_MEMBER_HEADER = "x-turnstile-pool-member"
POOL_RUNTIME_HEADER = "x-turnstile-pool-runtime"
CACHE_READ_WITHOUT_FALLBACK = '?? (long?)usage?["cache_read_input_tokens"] ?? 0;'
CACHE_READ_WITH_FALLBACK = (
    '?? (long?)usage?["cache_read_input_tokens"]\n          ?? (long?)usage?["cached_tokens"] ?? 0;'
)
_TOKENS = re.compile(
    r'//[^\r\n]*|/\*.*?\*/|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]",
    re.DOTALL,
)


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


def expression_tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in _TOKENS.findall(value) if not token.startswith(("//", "/*")))


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
        raise PolicyCompilationError(f"Policy component is missing or ambiguous: {label}")
    return nodes[0]


def _variable(root: ET.Element, name: str) -> ET.Element:
    return _one(root.findall(f".//set-variable[@name='{name}']"), name)


def _replace(root: ET.Element, old: ET.Element, new: ET.Element) -> None:
    parents = {child: parent for parent in root.iter() for child in parent}
    parent = parents[old]
    index = list(parent).index(old)
    new.tail = old.tail
    parent.remove(old)
    parent.insert(index, new)


def _branch(normal: ET.Element, image: ET.Element | None) -> ET.Element:
    result = ET.Element("choose")
    if image is None:
        ET.SubElement(result, "when", {"condition": TEXT_CONDITION}).append(deepcopy(normal))
    else:
        ET.SubElement(result, "when", {"condition": IMAGE_CONDITION}).append(image)
        ET.SubElement(result, "otherwise").append(deepcopy(normal))
    return result


def _image_usage() -> ET.Element:
    return ET.Element(
        "set-variable",
        {
            "name": "usagePayload",
            "value": """@{
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
        },
    )


def adopt_pool_runtime_scrub(inbound: ET.Element) -> None:
    """Adopt the pool runtime scrub that pairs with the pool member scrub.

    Cross-region routing pins a session to one region through this header, so a
    caller must never supply it and the parent policy drops it on the way in. The
    line arrived with that routing, not with images, and a v1.0 policy has neither.
    """
    if inbound.find(f"set-header[@name='{POOL_RUNTIME_HEADER}']") is not None:
        return
    member = inbound.find(f"set-header[@name='{POOL_MEMBER_HEADER}']")
    if member is None:
        raise PolicyCompilationError(
            "The source parent policy does not scrub the pool member header"
        )
    inbound.insert(
        list(inbound).index(member) + 1,
        ET.Element("set-header", {"name": POOL_RUNTIME_HEADER, "exists-action": "delete"}),
    )


def adopt_cached_tokens_fallback(root: ET.Element) -> None:
    """Adopt the top-level usage.cached_tokens fallback.

    OpenAI-compatible upstreams report cached reads as usage.cached_tokens, neither
    nested under prompt_tokens_details nor named cache_read_input_tokens. Without
    this fallback their cached reads are recorded as zero.
    """
    for node in root.iter("set-variable"):
        if node.get("name") != "usagePayload":
            continue
        value = node.get("value", "")
        if CACHE_READ_WITHOUT_FALLBACK in value:
            node.set(
                "value",
                value.replace(CACHE_READ_WITHOUT_FALLBACK, CACHE_READ_WITH_FALLBACK),
            )


def compose_image_parent_policy(source: str, profiles: Sequence[ImageGenerationProfile]) -> str:
    root = parse_policy(source)
    inbound = root.find("inbound")
    if inbound is None or root.findall(".//set-variable[@name='imageGenerationPolicyVersion']"):
        raise PolicyCompilationError("Only the unversioned public source template can be adapted")
    inference = _variable(root, "isInferenceOperation")
    inbound.insert(
        list(inbound).index(inference),
        ET.Element(
            "set-variable",
            {
                "name": "isImagesOperation",
                "value": f'@(context.Operation.Id == "{IMAGE_OPERATION_ID}")',
            },
        ),
    )
    inference.set(
        "value",
        '@((bool)context.Variables["isImagesOperation"] || '
        + inference.attrib["value"][2:-1]
        + ")",
    )
    for name in ("maxOutputBound", "applicationMaxOutputBound"):
        normal = _variable(root, name)
        image = ET.Element(
            "set-variable",
            {"name": name, "value": '@((long)context.Variables["imageOutputBound"])'},
        )
        _replace(root, normal, _branch(normal, image))
    for name, prefix in (
        ("budgetRequestCannotFit", ""),
        ("applicationBudgetCannotFit", "application"),
    ):
        normal = _variable(root, name)
        input_name = "applicationInputBound" if prefix else "inputBound"
        remaining = "applicationLedgerRemaining" if prefix else "ledgerRemaining"
        enforced = "applicationLedgerEnforced" if prefix else "ledgerEnforced"
        image = ET.Element(
            "set-variable",
            {
                "name": name,
                "value": f'''@{{
          if (!(bool)context.Variables["{enforced}"]) {{ return false; }}
          long input = (long)context.Variables["{input_name}"];
          long remaining = (long)context.Variables["{remaining}"];
          return input >= remaining
              || (long)context.Variables["imageOutputBound"] > remaining - input;
        }}''',
            },
        )
        _replace(root, normal, _branch(normal, image))
    body = _one(
        [node for node in root.iter("set-body") if "clamp" in expression_tokens(node.text or "")],
        "text request normalization",
    )
    _replace(root, body, _branch(body, None))
    usage = _one(
        root.findall("./outbound/choose/otherwise/set-variable[@name='usagePayload']"), "usage"
    )
    _replace(root, usage, _branch(usage, _image_usage()))
    burst = max(
        (validate_image_profile(profile).burst_reservation_tokens for profile in profiles),
        default=1,
    )
    for tag in ("llm-token-limit", "llm-emit-token-metric"):
        for node in list(root.iter(tag)):
            wrapper = _branch(node, None)
            if tag == "llm-token-limit":
                ET.SubElement(
                    ET.SubElement(wrapper, "otherwise"),
                    "rate-limit-by-key",
                    {
                        "calls": node.attrib["tokens-per-minute"],
                        "renewal-period": "60",
                        "counter-key": node.attrib["counter-key"].replace(
                            "finops:", "finops:images:"
                        ),
                        "increment-count": str(burst),
                        "retry-after-header-name": "Retry-After",
                    },
                )
            _replace(root, node, wrapper)
    adopt_pool_runtime_scrub(inbound)
    adopt_cached_tokens_fallback(root)
    ET.SubElement(
        inbound,
        "set-variable",
        {"name": "imageGenerationPolicyVersion", "value": f"@({IMAGE_POLICY_VERSION})"},
    )
    return serialize_policy(root)


def _matches_value(actual: str, expected: str, bindings: dict[str, str]) -> bool:
    if actual == expected:
        return True
    pattern = ""
    names: list[str] = []
    for part in re.split(r"(__[A-Z][A-Z0-9_]*__)", expected):
        if re.fullmatch(r"__[A-Z][A-Z0-9_]*__", part):
            if part in bindings:
                pattern += re.escape(bindings[part])
            elif part in names:
                pattern += f"(?P={part})"
            else:
                pattern += f"(?P<{part}>.+?)"
                names.append(part)
        else:
            pattern += re.escape(part)
    match = re.fullmatch(pattern, actual)
    if match is None:
        return False
    bindings.update({name: match.group(name) for name in names})
    return True


def _matches_component(actual: ET.Element, expected: ET.Element, bindings: dict[str, str]) -> bool:
    if actual.tag != expected.tag or actual.attrib.keys() != expected.attrib.keys():
        return False
    values = [(actual.get(name, ""), value) for name, value in expected.attrib.items()]
    values.append(((actual.text or "").strip(), (expected.text or "").strip()))
    for actual_value, expected_value in values:
        if expected_value.startswith("@"):
            observed, template = expression_tokens(actual_value), expression_tokens(expected_value)
            if len(observed) != len(template) or not all(
                _matches_value(value, reference, bindings)
                for value, reference in zip(observed, template, strict=True)
            ):
                return False
        elif not _matches_value(actual_value, expected_value, bindings):
            return False
    observed_children = [child for child in actual if isinstance(child.tag, str)]
    template_children = [child for child in expected if isinstance(child.tag, str)]
    return len(observed_children) == len(template_children) and all(
        _matches_component(child, reference, bindings)
        for child, reference in zip(observed_children, template_children, strict=True)
    )


def validate_parent_policy(
    source: str,
    canonical_parent: str,
    profiles: Sequence[ImageGenerationProfile] = (),
) -> str:
    from .apim_directory_policy import strip_validated_directory_identity_policy

    def without_slots(value: str) -> str:
        return value.replace("__LEGACY_PROVIDER_ROUTING__", "").replace(
            "__USAGE_OBSERVER_HEADERS__", ""
        )

    root, template = (
        parse_policy(without_slots(strip_validated_directory_identity_policy(source))),
        parse_policy(without_slots(strip_validated_directory_identity_policy(canonical_parent))),
    )
    for policy in (root, template):
        if policy.tag != "policies" or [
            node.tag for node in policy if isinstance(node.tag, str)
        ] != ["inbound", "backend", "outbound", "on-error"]:
            raise PolicyCompilationError("Parent policy sections are incomplete or out of order")
        for choose in policy.iter("choose"):
            branches = [child.tag for child in choose if isinstance(child.tag, str)]
            if (
                not branches
                or branches[0] != "when"
                or branches.count("otherwise") > 1
                or any(tag not in {"when", "otherwise"} for tag in branches)
                or ("otherwise" in branches and branches[-1] != "otherwise")
            ):
                raise PolicyCompilationError("Parent policy choose branches are invalid")
    if not _matches_component(root, template, {}):
        raise PolicyCompilationError("Parent policy differs from the reviewed public contract")
    marker = root.find("./inbound/set-variable[@name='imageGenerationPolicyVersion']")
    if profiles and marker is None:
        raise PolicyCompilationError("Image publication requires an image-ready parent policy")
    if marker is not None:
        if len(
            root.findall(".//set-variable[@name='imageGenerationPolicyVersion']")
        ) != 1 or expression_tokens(marker.get("value", "")) != ("@", "(", "2", ")"):
            raise PolicyCompilationError("Image policy version is invalid")
        flag = _variable(root, "isImagesOperation")
        if expression_tokens(flag.get("value", "")) != expression_tokens(
            f'@(context.Operation.Id == "{IMAGE_OPERATION_ID}")'
        ):
            raise PolicyCompilationError("Image operation classification is invalid")
        if any(
            node.get("name")
            in {
                "imageOutputBound",
                "selectedModelKnown",
                "selectedRoutingManaged",
                "selectedRequiresAssignment",
            }
            for node in root.iter("set-variable")
        ):
            raise PolicyCompilationError("Parent cannot overwrite operation-owned facts")
        for profile in profiles:
            validate_image_profile(profile)
        for rate in root.iter("rate-limit-by-key"):
            if "images:" in rate.get("counter-key", "") and int(
                rate.get("increment-count", "0")
            ) < max((profile.burst_reservation_tokens for profile in profiles), default=1):
                raise PolicyCompilationError("Image burst limits exceed the reviewed parent policy")
    return source


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


def validate_parent_readback(
    source: str,
    canonical_parent: str,
    profiles: Sequence[ImageGenerationProfile],
    *,
    expected_contract: object,
    expected_raw: object,
) -> None:
    validate_parent_policy(source, canonical_parent, profiles)
    if not parent_readback_matches(
        source, expected_contract=expected_contract, expected_raw=expected_raw
    ):
        raise PolicyCompilationError(
            "APIM parent readback differs from the recorded release contract"
        )
