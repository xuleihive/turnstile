"""Conservative name matching; model inference chooses candidates, never prices."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from turnstile_core.pricing.catalog import CatalogEntry

_VARIANTS = {
    "mini",
    "nano",
    "pro",
    "flash",
    "thinking",
    "instruct",
    "opus",
    "sonnet",
    "haiku",
    "lite",
    "preview",
    "astra",
    "sol",
    "sunburst",
    "flare",
    "instant",
    "turbo",
}
_PROVIDERS = {
    "microsoft_foundry": ("azure", "azure-cognitive-services"),
    "azure_openai": ("azure",),
    "openai": ("openai",),
    "anthropic": ("anthropic",),
    "amazon_bedrock": ("amazon-bedrock",),
    "xai": ("xai",),
    "kimi": ("moonshotai",),
    "deepseek": ("deepseek",),
}


def reference_price_basis(entry: CatalogEntry, brand: str, kind: str) -> str:
    serving = _PROVIDERS.get(brand, _PROVIDERS.get(kind, ()))
    return "deployment" if entry.provider_id in serving else "origin_reference"


def name_tokens(value: str) -> tuple[str, ...]:
    value = value.split("·", 1)[0].strip().lower()
    value = re.sub(r"-foundry-[0-9a-f]{8}$", "", value)
    return tuple(re.findall(r"[a-z]+|\d+", value.rsplit("/", 1)[-1]))


def entry_names(entry: CatalogEntry) -> tuple[tuple[str, ...], ...]:
    return tuple(
        name_tokens(value)
        for value in (
            entry.model_id or "",
            entry.label,
            entry.canonical_model_id or "",
        )
        if value
    )


@dataclass(frozen=True)
class MatchResult:
    entry: CatalogEntry | None
    status: str
    candidates: tuple[CatalogEntry, ...] = ()
    method: str = "exact"
    price_basis: str = "deployment"
    reason: str | None = None
    requires_ai: bool = False


def _choose_provider(
    entries: Sequence[CatalogEntry],
    brand: str,
    kind: str,
) -> tuple[CatalogEntry | None, str]:
    for provider in _PROVIDERS.get(brand, _PROVIDERS.get(kind, ())):
        found = [entry for entry in entries if entry.provider_id == provider]
        if len(found) == 1:
            return found[0], "deployment"
        if len(found) > 1:
            return None, "deployment"
    origins = [
        entry
        for entry in entries
        if entry.canonical_model_id
        and entry.provider_id == entry.canonical_model_id.partition("/")[0]
    ]
    if len(origins) == 1:
        return origins[0], "origin_reference"
    return None, "deployment"


def _evidence(names: Sequence[tuple[str, ...]], entry: CatalogEntry) -> bool:
    for name in names:
        for candidate in entry_names(entry):
            digits = tuple(word for word in name if word.isdigit())
            other_digits = tuple(word for word in candidate if word.isdigit())
            variants = set(name) & _VARIANTS
            other_variants = set(candidate) & _VARIANTS
            words = set(name) - _VARIANTS - set(digits) - {"fw"}
            other_words = set(candidate) - _VARIANTS - set(other_digits)
            if (
                digits
                and digits == other_digits
                and variants == other_variants
                and words
                and words <= other_words
            ):
                return True
    return False


def match_model(
    entries: Sequence[CatalogEntry],
    names: Sequence[str],
    *,
    brand: str,
    kind: str,
    operation: Literal["chat", "image_generation"] = "chat",
    judge: Callable[[str], str] | None = None,
) -> MatchResult:
    normalized = [name_tokens(name) for name in names if name.strip()]
    if not normalized:
        return MatchResult(None, "unmapped", reason="请输入模型或部署名称")
    eligible = [
        entry for entry in entries
        if not entry.unsupported and entry.priced and entry.operation == operation
    ]
    # Higher-priority identity names win; a generic display name must not broaden an exact ID.
    for name in normalized:
        exact = [entry for entry in eligible if name in entry_names(entry)]
        if exact:
            selected, basis = _choose_provider(exact, brand, kind)
            if selected:
                return MatchResult(selected, "matched", price_basis=basis)
            return MatchResult(
                None,
                "ambiguous",
                tuple(exact[:12]),
                reason="同名模型有多个无法区分的提供方报价",
            )
    scored: list[tuple[int, str, CatalogEntry]] = []
    for entry in eligible:
        score = max(
            len(set(name) & set(candidate))
            for name in normalized
            for candidate in entry_names(entry)
        )
        if score >= 2:
            scored.append((-score, entry.reference, entry))
    scored.sort(key=lambda item: item[:2])
    candidates = tuple(item[2] for item in scored[:12])
    if not candidates:
        return MatchResult(None, "unmapped", reason="公网目录中没有适用的模型候选")
    if judge is None:
        return MatchResult(
            None,
            "ambiguous",
            candidates,
            reason="无法确定型号，需默认模型判断或人工选择",
            requires_ai=True,
        )
    prompt = json.dumps(
        {
            "names": list(names),
            "candidates": [
                {
                    "candidate_id": entry.reference,
                    "name": entry.label,
                    "model_id": entry.model_id,
                    "canonical_model_id": entry.canonical_model_id,
                    "provider_id": entry.provider_id,
                }
                for entry in candidates
            ],
        },
        ensure_ascii=True,
    )
    try:
        answer = judge(prompt).strip()
        if len(answer) > 8000:
            raise ValueError("Oversized match answer")
        if answer.startswith("```"):
            answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer)
        result = json.loads(answer)
        if not isinstance(result, dict) or result.get("decision") != "match":
            raise ValueError("Model declined a match")
        selected = next(
            (entry for entry in candidates if entry.reference == result.get("candidate_id")),
            None,
        )
        if selected is None or not _evidence(normalized, selected):
            raise ValueError("Match lacks independently verifiable model evidence")
        equivalents = [
            entry
            for entry in eligible
            if selected.canonical_model_id
            and entry.canonical_model_id == selected.canonical_model_id
        ] or [selected]
        preferred, basis = _choose_provider(equivalents, brand, kind)
        if preferred is None:
            raise ValueError("Provider ambiguity remains")
        return MatchResult(
            preferred,
            "matched",
            method="ai",
            price_basis=basis,
            reason=str(result.get("reason") or "默认模型辅助匹配")[:500],
        )
    except (ValueError, TypeError, StopIteration):
        return MatchResult(
            None,
            "ambiguous",
            candidates,
            reason="默认模型未能提供可验证匹配，请人工确认",
        )
