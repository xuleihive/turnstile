"""Narrow, idempotent upgrade of subscription TPM from the application ledger."""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from .apim_control_plane_contract import PolicyCompilationError

TPM_EXPRESSION = '@((int)context.Variables["effectiveApplicationTpm"])'


def _tpm_variable(fallback: str) -> str:
    value = """@{
          var fallback = (long)FALLBACK;
          var state = context.Variables.GetValueOrDefault<JObject>("applicationLedgerState", null);
          var mapping = context.Variables.GetValueOrDefault<JObject>("applicationMapState", null);
          var mapped = mapping != null && (string)mapping["status"] == "ok";
          var configured = state != null && ((bool?)state["available"] ?? false);
          var requested = configured ? ((long?)state["tokensPerMinute"] ?? fallback) : fallback;
          return (int)(mapped && configured && requested > 0 && requested <= int.MaxValue
            ? requested : fallback);
        }""".replace("FALLBACK", fallback)
    return '<set-variable name="effectiveApplicationTpm" value="' + escape(
        value, {'"': "&quot;"},
    ) + '" />'


def compose_application_tpm_policy(source: str) -> str:
    # Azure's primary table endpoint ends in '/'; older bootstrap policies appended another.
    # Normalize only the known ledger origin, preserving policy sections and customer targets.
    mapping = re.search(r'<set-variable name="applicationMapUrl".*?/>', source, re.DOTALL)
    if mapping is None:
        raise PolicyCompilationError("Application TPM upgrade has no ledger mapping URL")
    endpoint = re.search(r'https://[^/\s&"<>]+\.table\.[^/\s&"<>]+', mapping.group())
    if endpoint:
        source = source.replace(endpoint.group() + "//", endpoint.group() + "/")
    if 'name="effectiveApplicationTpm"' in source:
        # Upgrade the reviewed early candidate, not arbitrary customer expressions.
        from .apim_policy_components import expression_tokens, parse_policy

        node = re.search(r'<set-variable name="effectiveApplicationTpm".*?/>', source, re.DOTALL)
        assert node is not None
        old_value = parse_policy(node.group()).attrib["value"]
        fallback_match = re.search(
            r"var fallback = \(long\)([0-9]+|__TOKENS_PER_MINUTE__);", old_value,
        )
        if fallback_match:
            variable = _tpm_variable(fallback_match[1])
            previous = parse_policy(variable).attrib["value"].replace(
                'var mapping = context.Variables.GetValueOrDefault<JObject>('
                '"applicationMapState", null);\n'
                '          var mapped = mapping != null && (string)mapping["status"] == "ok";',
                'var admission = context.Variables.GetValueOrDefault<string>('
                '"applicationAdmission", "");',
            ).replace('mapped && configured', 'admission == "ok" && configured')
            if expression_tokens(old_value) == expression_tokens(previous):
                source = source[:node.start()] + variable + source[node.end():]
        return source
    text = re.compile(r"<llm-token-limit\b[^>]*finops:subscription:[^>]*?/>", re.DOTALL)
    matches = list(text.finditer(source))
    if len(matches) != 1:
        raise PolicyCompilationError(
            "Application TPM upgrade requires one subscription token limit"
        )
    old = matches[0].group()
    rate = re.search(r'tokens-per-minute="([^"]+)"', old)
    if rate is None or not (rate[1].isdigit() or rate[1] == "__TOKENS_PER_MINUTE__"):
        raise PolicyCompilationError("Application TPM fallback must be a reviewed integer")
    fallback = rate[1]
    expression = escape(TPM_EXPRESSION, {'"': "&quot;"})
    updated = old.replace(rate.group(), f'tokens-per-minute="{expression}"')
    source = source[: matches[0].start()] + updated + source[matches[0].end() :]
    images = re.compile(r"<rate-limit-by-key\b[^>]*finops:images:subscription:[^>]*?/>", re.DOTALL)
    matches = list(images.finditer(source))
    if len(matches) != 1:
        raise PolicyCompilationError(
            "Application TPM upgrade requires one image subscription limit"
        )
    old = matches[0].group()
    rate = re.search(r'calls="([^"]+)"', old)
    if rate is None or rate[1] != fallback:
        raise PolicyCompilationError("Text and image subscription limits have different fallbacks")
    source = (
        source[: matches[0].start()]
        + old.replace(
            rate.group(),
            f'calls="{expression}"',
        )
        + source[matches[0].end() :]
    )
    anchor = re.search(r'<set-variable name="consumerId"[^>]*?/>', source)
    if anchor is None:
        raise PolicyCompilationError("Application TPM upgrade has no subscription boundary")
    variable = '\n        ' + _tpm_variable(fallback)
    return source[: anchor.end()] + variable + source[anchor.end() :]
