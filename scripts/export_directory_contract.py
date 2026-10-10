"""Generate the directory OpenAPI domain from its served request DTOs and routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from backend.api import app

PREFIX = "/api/v1/organization-management/"
ERROR = "../../openapi.yaml#/components/responses/ErrorResponse"


def export() -> dict[str, Any]:
    document = app.openapi()
    schemas: dict[str, Any] = {}

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            ref = result.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.removeprefix("#/components/schemas/")
                if name not in schemas:
                    schemas[name] = {}
                    schemas[name] = visit(document["components"]["schemas"][name])
                result["$ref"] = f"../../openapi.yaml#/components/schemas/{name}"
            return result
        return value

    paths = {}
    for path, operations in document["paths"].items():
        if not path.startswith(PREFIX):
            continue
        prepared = {}
        for method, original in operations.items():
            operation = {**original, "responses": dict(original["responses"])}
            operation["responses"].pop("422", None)
            operation = visit(operation)
            operation["tags"] = ["Organization Management"]
            operation["security"] = [{"sessionCookie": []}]
            operation["description"] = (
                "Persistent APIM organization directory. Owner manages organizations, account "
                "links, connections and reviewed sync jobs. A delegated Member is restricted "
                "to explicitly granted departments and capabilities. Writes enforce revisions; "
                "create retries use Idempotency-Key. No-store responses and safe errors never "
                "expose credentials or cursors. "
                "Sync does not grant login roles, model access or tenant-wide consent."
            )
            for status in ("401", "403", "404", "409", "422", "503", "default"):
                operation["responses"][status] = {"$ref": ERROR}
            prepared[method] = operation
        paths[path] = prepared
    schemas["ConnectionWrite"]["properties"]["credential_ref"]["writeOnly"] = True
    return {"paths": dict(sorted(paths.items())), "schemas": dict(sorted(schemas.items()))}


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "contracts/openapi"
    value = export()
    (root / "paths/organization-management.yaml").write_text(
        yaml.safe_dump({"paths": value["paths"]}, sort_keys=False),
        encoding="utf-8",
    )
    (root / "schemas/organization-management.yaml").write_text(
        yaml.safe_dump({"schemas": value["schemas"]}, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
