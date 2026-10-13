from __future__ import annotations

import argparse
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def path_domain(item: dict[str, Any]) -> str:
    tags = {
        str(tag)
        for method, operation in item.items()
        if method.lower() in HTTP_METHODS and isinstance(operation, dict)
        for tag in operation.get("tags", ["Untagged"])
    }
    if "GitHub Copilot" in tags:
        return "github-copilot"
    if "Authentication" in tags:
        return "authentication"
    if "Assistant" in tags:
        return "assistant"
    if "Model Control Plane" in tags:
        return "model-platform"
    if "Budgets" in tags:
        return "budgets"
    return "observability"


def pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def rewrite_component_refs(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("#/components/"):
        return f"../../openapi.yaml{value}"
    if isinstance(value, list):
        return [rewrite_component_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        if key == "$ref" and isinstance(item, str) and item.startswith("#/components/"):
            rewritten[key] = f"../../openapi.yaml{item}"
        else:
            rewritten[key] = rewrite_component_refs(item)
    return rewritten


def dump_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            value,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        ),
        encoding="utf-8",
    )


def split_contract(source: Path, target: Path) -> None:
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    paths = dict(document.get("paths") or {})
    components = dict(document.get("components") or {})
    schemas = dict(components.get("schemas") or {})
    if not paths or not schemas:
        raise RuntimeError("The source contract must contain inline paths and schemas")
    if any(set(item) == {"$ref"} for item in paths.values() if isinstance(item, dict)):
        raise RuntimeError("The source contract is already split")

    output_root = target.parent / "openapi"
    if output_root.exists():
        shutil.rmtree(output_root)

    paths_by_domain: dict[str, dict[str, Any]] = defaultdict(dict)
    path_domains: dict[str, str] = {}
    for route, item in paths.items():
        domain = path_domain(item)
        path_domains[route] = domain
        paths_by_domain[domain][route] = rewrite_component_refs(item)

    schema_domains: dict[str, set[str]] = defaultdict(set)

    def walk(value: Any, domain: str, seen: set[str]) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, domain, seen)
            return
        if not isinstance(value, dict):
            return
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            if ref in seen:
                return
            seen.add(ref)
            _, _, kind, name = ref.split("/", 3)
            collection = components.get(kind) or {}
            target = collection.get(name)
            if kind == "schemas":
                schema_domains[name].add(domain)
            if target is not None:
                walk(target, domain, seen)
        for key, item in value.items():
            if key != "$ref":
                walk(item, domain, seen)

    for route, item in paths.items():
        walk(item, path_domains[route], set())
    for kind in ("parameters", "responses"):
        walk(components.get(kind) or {}, "common", set())
    event_contracts = document.get("x-event-contracts")
    if event_contracts is not None:
        walk(event_contracts, "events", set())

    schemas_by_domain: dict[str, dict[str, Any]] = defaultdict(dict)
    for name, schema in schemas.items():
        owners = schema_domains.get(name, set())
        domain = next(iter(owners)) if len(owners) == 1 and "common" not in owners else "common"
        schemas_by_domain[domain][name] = rewrite_component_refs(schema)

    for domain, domain_paths in sorted(paths_by_domain.items()):
        dump_yaml(output_root / "paths" / f"{domain}.yaml", {"paths": domain_paths})
    for domain, domain_schemas in sorted(schemas_by_domain.items()):
        dump_yaml(output_root / "schemas" / f"{domain}.yaml", {"schemas": domain_schemas})

    common_components = {
        kind: rewrite_component_refs(components.get(kind) or {})
        for kind in ("parameters", "responses", "securitySchemes")
        if components.get(kind)
    }
    dump_yaml(output_root / "components" / "common.yaml", common_components)
    if event_contracts is not None:
        dump_yaml(
            output_root / "events" / "eventhub.yaml",
            {"eventContracts": rewrite_component_refs(event_contracts)},
        )

    extracted_keys = {"paths", "components", "x-event-contracts"}
    root = {
        key: value for key, value in document.items() if key not in extracted_keys
    }
    root["paths"] = {
        route: {
            "$ref": f"./openapi/paths/{path_domains[route]}.yaml#/paths/{pointer(route)}"
        }
        for route in paths
    }
    root_components: dict[str, Any] = {}
    for kind in ("securitySchemes", "parameters", "responses"):
        values = components.get(kind) or {}
        if values:
            root_components[kind] = {
                name: {"$ref": f"./openapi/components/common.yaml#/{kind}/{name}"}
                for name in values
            }
    owner_by_schema = {
        name: domain for domain, values in schemas_by_domain.items() for name in values
    }
    root_components["schemas"] = {
        name: {
            "$ref": f"./openapi/schemas/{owner_by_schema[name]}.yaml#/schemas/{name}"
        }
        for name in schemas
    }
    root["components"] = root_components
    if event_contracts is not None:
        # Redocly resolves external refs in OpenAPI-defined locations but does not traverse
        # arbitrary extension values. Keep the extension inline so bundling stays equivalent;
        # the generated event file remains the domain mirror used for focused review.
        root["x-event-contracts"] = event_contracts
    dump_yaml(target, root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Split the canonical OpenAPI contract by domain")
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("contracts/openapi.yaml"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    split_contract(args.source, args.output or args.source)


if __name__ == "__main__":
    main()
