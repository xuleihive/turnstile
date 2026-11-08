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


def _identity(entry: CatalogEntry, key: str) -> str:
    value = entry.metadata.get(key)
    return value if isinstance(value, str) else ""


def name_tokens(value: str) -> tuple[str, ...]:
    value = value.split("·", 1)[0].strip().lower()
    value = re.sub(r"-foundry-[0-9a-f]{8}$", "", value)
    return tuple(re.findall(r"[a-z]+|\d+", value.rsplit("/", 1)[-1]))


def entry_names(entry: CatalogEntry) -> tuple[tuple[str, ...], ...]:
    identity = (
        _identity(entry, "model_id")
        or _identity(entry, "canonical_model_id")
        or entry.label
    )
    return (name_tokens(identity),) if identity else ()


@dataclass(frozen=True)
class MatchResult:
    entry: CatalogEntry | None
    status: Literal["matched", "ambiguous", "unmapped"]
    candidates: tuple[CatalogEntry, ...] = ()
    method: Literal["exact", "ai"] = "exact"
    price_basis: Literal["deployment", "origin_reference"] = "deployment"
    requires_ai: bool = False


def _choose_provider(
    entries: Sequence[CatalogEntry],
    brand: str,
    kind: str,
) -> tuple[CatalogEntry | None, Literal["deployment", "origin_reference"]]:
    for provider in _PROVIDERS.get(brand, _PROVIDERS.get(kind, ())):
        found = [entry for entry in entries if _identity(entry, "provider_id") == provider]
        if len(found) == 1:
            return found[0], "deployment"
        if len(found) > 1:
            return None, "deployment"
    origins = [
        entry
        for entry in entries
        if _identity(entry, "canonical_model_id")
        and _identity(entry, "provider_id")
        == _identity(entry, "canonical_model_id").partition("/")[0]
    ]
    return (origins[0], "origin_reference") if len(origins) == 1 else (None, "deployment")


def _evidence(names: Sequence[tuple[str, ...]], entry: CatalogEntry) -> bool:
    for name in names:
        for candidate in entry_names(entry):
            digits = tuple(word for word in name if word.isdigit())
            other_digits = tuple(word for word in candidate if word.isdigit())
            words = set(name) - _VARIANTS - set(digits) - {"fw"}
            other_words = set(candidate) - _VARIANTS - set(other_digits)
            if (
                digits
                and digits == other_digits
                and set(name) & _VARIANTS == set(candidate) & _VARIANTS
                and words
                and words == other_words
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
    normalized = [name_tokens(name) for name in names if name.strip()][:1]
    if not normalized:
        return MatchResult(None, "unmapped")
    eligible = [
        entry
        for entry in entries
        if entry.priced
        and entry.complete
        and entry.text_to_image == (operation == "image_generation")
    ]
    for name in normalized:
        exact = [entry for entry in eligible if name in entry_names(entry)]
        if exact:
            selected, basis = _choose_provider(exact, brand, kind)
            return (
                MatchResult(selected, "matched", price_basis=basis)
                if selected
                else MatchResult(None, "ambiguous", tuple(exact[:12]))
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
        return MatchResult(None, "unmapped")
    if judge is None:
        return MatchResult(None, "ambiguous", candidates, requires_ai=True)
    prompt = json.dumps(
        {
            "names": list(names),
            "candidates": [
                {
                    "candidate_id": entry.reference,
                    "name": entry.label,
                    "model_id": _identity(entry, "model_id"),
                    "canonical_model_id": _identity(entry, "canonical_model_id"),
                    "provider_id": _identity(entry, "provider_id"),
                }
                for entry in candidates
            ],
        },
        ensure_ascii=True,
    )
    if len(prompt) > 16000:
        return MatchResult(None, "ambiguous", candidates)
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
            entry for entry in candidates if entry.reference == result.get("candidate_id")
        )
        if not _evidence(normalized, selected):
            raise ValueError("Match lacks independent model evidence")
        canonical = _identity(selected, "canonical_model_id")
        equivalents = [
            entry
            for entry in eligible
            if canonical and _identity(entry, "canonical_model_id") == canonical
        ]
        preferred, basis = _choose_provider(equivalents or [selected], brand, kind)
        if preferred is None or preferred not in candidates or not _evidence(normalized, preferred):
            raise ValueError("Provider ambiguity remains")
        return MatchResult(preferred, "matched", method="ai", price_basis=basis)
    except (ValueError, TypeError, StopIteration):
        return MatchResult(None, "ambiguous", candidates)
