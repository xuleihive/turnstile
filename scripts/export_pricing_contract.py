"""Export the pricing DTOs and their reachable schemas without duplicating their definitions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.api import app

ROOTS = (
    "PriceSource", "PricePreviewRequest", "PricePreviewResponse", "PriceSyncRequest",
    "PriceSyncResponse", "PriceCatalogModelsResponse", "PriceCatalogOptionsResponse",
)


def export() -> dict[str, Any]:
    source = app.openapi()["components"]["schemas"]
    schemas: dict[str, Any] = {}

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if ref := result.get("$ref"):
                name = ref.removeprefix("#/components/schemas/")
                include(name)
                result["$ref"] = f"#/schemas/{name}"
            return result
        return value

    def include(name: str) -> None:
        if name not in schemas:
            schemas[name] = {}
            schemas[name] = visit(source[name])

    for name in ROOTS:
        include(name)
    return {"schemas": dict(sorted(schemas.items()))}


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "contracts/openapi/schemas/pricing.json"
    destination.write_text(json.dumps(export(), indent=2) + "\n", encoding="utf-8")
