from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from ..domain.pricing import PriceConfigurationChanged
from ..domain.runtime_models import apply_databricks_adoption


class InMemoryRegistryRepositoryMixin:
    gateways: list[dict[str, Any]]
    providers: list[dict[str, Any]]
    runtimes: list[dict[str, Any]]
    models: list[dict[str, Any]]
    gateway_publications: list[dict[str, Any]]
    effective_gateway_releases: dict[UUID, UUID]

    def _published_image_profile(self, model: Mapping[str, Any]) -> object:
        runtime = next((row for row in self.runtimes if row["id"] == model["runtime_id"]), None)
        gateway_id = runtime.get("gateway_profile_id") if runtime else None
        if gateway_id is None:
            return None
        publication_id = self.effective_gateway_releases.get(gateway_id)
        publication = next(
            (row for row in self.gateway_publications if row["id"] == publication_id), None
        )
        for binding in publication["desired_spec"]["bindings"] if publication else []:
            if binding["model"]["model_key"].casefold() == model["model_key"].casefold():
                return binding["model"].get("image_profile")
        return None

    def _published_databricks_runtime(self, runtime: dict[str, Any]) -> dict[str, Any]:
        gateway_id = runtime.get("gateway_profile_id")
        publication_id = self.effective_gateway_releases.get(gateway_id) if gateway_id else None
        publication = next(
            (row for row in self.gateway_publications if row["id"] == publication_id), None,
        )
        for binding in publication["desired_spec"]["bindings"] if publication else []:
            if (
                str(binding.get("runtime_id")) == str(runtime["id"])
                and str(binding.get("provider_id")) == str(runtime["provider_id"])
                and binding.get("routing_managed") is True
                and (
                    (binding.get("runtime_config") or {}).get("databricks_connection_adoption")
                    is True or binding.get("auth_strategy") == "oauth_client_credentials"
                )
            ):
                return apply_databricks_adoption(runtime, binding["runtime_config"])
        return runtime

    def _effective_discount(self, model: dict[str, Any]) -> float | None:
        """The discount that actually applies: the model's own, else its connection's.

        The SQL repository resolves this in the query. Resolving it here too keeps the two
        implementations answering the same question -- without it the fake charges list price
        for every model whose discount is set on the connection, and the tests would agree
        with it.
        """
        own = model.get("price_discount_percent")
        if own is not None:
            return cast(float, own)
        runtime = next(
            (row for row in self.runtimes if row["id"] == model.get("runtime_id")), None
        )
        return cast("float | None", runtime.get("price_discount_percent") if runtime else None)

    def registry(self) -> dict[str, Sequence[dict[str, Any]]]:
        return {
            "gateways": self.gateways,
            "providers": self.providers,
            "runtimes": [self._published_databricks_runtime(row) for row in self.runtimes],
            "models": [
                {
                    **model,
                    "price_source": model.get("price_source") or "models_dev",
                    "price_source_configured": model.get(
                        "price_source_configured", "price_source" in model,
                    ),
                    "image_profile": self._published_image_profile(model),
                    "effective_discount_percent": self._effective_discount(model),
                }
                for model in self.models
            ],
        }

    def apply_model_price_sync(self, updates: Sequence[Mapping[str, Any]]) -> int:
        charged = (
            "input_cost_per_million",
            "output_cost_per_million",
            "cached_cost_per_million",
            "cache_write_cost_per_million",
        )
        listed = (
            "list_input_cost_per_million",
            "list_output_cost_per_million",
            "list_cached_cost_per_million",
            "list_cache_write_cost_per_million",
        )
        now = datetime.now(UTC)
        written = 0
        by_id = {model.get("id"): model for model in self.models}
        for update in updates:
            model = by_id.get(update.get("model_id"))
            if model is None:
                continue

            # Same guard the SQL repository applies. Without it here, every test that reaches a
            # sync through the fake would agree that a stale result may land on a model whose
            # pricing changed underneath it -- and the database would disagree in production.
            if update.get("expected_source") is not None:
                current = (
                    str(model.get("price_source") or "models_dev"),
                    model.get("price_reference"),
                    self._effective_discount(model),
                )
                planned = (
                    str(update.get("expected_source")),
                    update.get("expected_reference"),
                    update.get("expected_discount_percent"),
                )
                configured = model.get("price_source_configured", "price_source" in model)
                version_changed = (
                    update.get("expected_updated_at") is not None
                    and model.get("updated_at") != update["expected_updated_at"]
                )
                price_version_changed = (
                    update.get("expected_price_updated_at") is not None
                    and model.get("price_config_updated_at") != update["expected_price_updated_at"]
                )
                if current != planned or configured != update.get("expected_configured", True) \
                        or version_changed or price_version_changed:
                    if current == planned:
                        model["price_sync_status"] = "superseded"
                    continue
            if not update.get("writes") and not model.get(
                "price_source_configured", "price_source" in model,
            ):
                continue

            if update.get("writes"):
                # The accepted baseline moves only when the price is accepted.
                for column in listed:
                    if update.get(column) is not None:
                        model[column] = update[column]
                for column in charged:
                    model[column] = update.get(column)
                model["pending_list_price"] = None
                model["updated_at"] = now
                model["price_source"] = update.get("price_source") or update.get("expected_source")
                model["price_reference"] = update.get("price_reference") \
                    or update.get("expected_reference")
                model["price_source_configured"] = True
                model["price_config_updated_at"] = now
                model["match_metadata"] = update.get("match_metadata")
                model["source_snapshot"] = update.get("source_snapshot")
                written += 1
            elif update.get("pending_list_price") is not None:
                model["pending_list_price"] = update["pending_list_price"]

            model["price_sync_status"] = update.get("status")
            model["price_sync_message"] = update.get("message")
            model["price_synced_at"] = now
            model["price_config_updated_at"] = now
        return written

    def create_registry_item(self, kind: str, values: Mapping[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        row = dict(values)
        if kind in {"provider", "runtime"}:
            row.setdefault("brand_key", "generic")
        if kind == "model":
            row.setdefault("family_key", "generic")
            row.setdefault("upstream_model_id", row.get("model_key"))
            row.setdefault("assignment_required", False)
            row.setdefault("publication_id", None)
            row.setdefault("price_source_configured", "price_source" in values)
        row.setdefault("id", uuid4())
        row.update(created_at=now, updated_at=now)
        target = self._registry_target(kind)
        if row.get("is_default"):
            for item in target:
                item["is_default"] = False
            if kind == "model":
                for runtime in self.runtimes:
                    runtime["is_default"] = runtime["id"] == row["runtime_id"]
        if kind == "runtime":
            provider = self._require(self.providers, row["provider_id"], "provider")
            gateway = self._find(self.gateways, row.get("gateway_profile_id"))
            row.update(
                provider_name=provider["name"],
                gateway_name=gateway["name"] if gateway else None,
                health_status="unknown",
                health_message=None,
                last_checked_at=None,
            )
        elif kind == "model":
            provider = self._require(self.providers, row["provider_id"], "provider")
            runtime = self._require(self.runtimes, row["runtime_id"], "runtime")
            if runtime["provider_id"] != provider["id"]:
                raise ValueError("The selected runtime does not belong to the provider")
            row.update(provider_name=provider["name"], runtime_name=runtime["name"])
        target.append(row)
        return row

    def create_connection(
        self,
        provider_id: UUID | None,
        provider_values: Mapping[str, Any] | None,
        runtime_values: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (provider_id is None) == (provider_values is None):
            raise ValueError("select an existing provider or one provider template")
        runtime_config = dict(runtime_values.get("config") or {})
        if runtime_config.get("workspace_url") and any(
            row["gateway_profile_id"] == runtime_values["gateway_profile_id"]
            and row["status"] in {
                "queued", "validating", "provisioning", "building_revision",
                "verifying", "awaiting_authorization", "promoting", "rolling_back",
            }
            for row in self.gateway_publications
        ):
            raise ValueError("Wait for the gateway publication before creating a connection")
        project_endpoint = str(runtime_config.get("project_endpoint") or "").rstrip("/")
        if project_endpoint and any(
            runtime.get("gateway_profile_id") == runtime_values["gateway_profile_id"]
            and runtime.get("runtime_kind") == "foundry"
            and str((runtime.get("config") or {}).get("project_endpoint") or "")
            .rstrip("/")
            .casefold()
            == project_endpoint.casefold()
            for runtime in self.runtimes
        ):
            raise ValueError("This connection already exists")
        workspace_url = str(runtime_config.get("workspace_url") or "").rstrip("/")
        if workspace_url and any(
            runtime.get("gateway_profile_id") == runtime_values["gateway_profile_id"]
            and str((runtime.get("config") or {}).get("workspace_url") or "")
            .rstrip("/").casefold() == workspace_url.casefold()
            for runtime in self.registry()["runtimes"]
        ):
            raise ValueError("This connection already exists")
        provider_count = len(self.providers)
        if provider_values is not None:
            provider = next(
                (
                    row
                    for row in self.providers
                    if row["enabled"]
                    and row.get("brand_key") == provider_values["brand_key"]
                ),
                None,
            )
            if provider is None:
                provider = self.create_registry_item("provider", provider_values)
            provider_id = provider["id"]
        assert provider_id is not None
        self._require(self.providers, provider_id, "provider")
        try:
            return self.create_registry_item(
                "runtime",
                {**runtime_values, "provider_id": provider_id},
            )
        except Exception:
            del self.providers[provider_count:]
            raise

    def update_registry_item(
        self, kind: str, item_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        target = self._registry_target(kind)
        row = self._find(target, item_id)
        if row is None:
            return None
        if kind == "model" and values.get("pricing_expected_updated_at") is not None \
                and row["updated_at"] != values["pricing_expected_updated_at"]:
            raise PriceConfigurationChanged("模型在定价期间已修改，请刷新后重试")
        if kind == "model" and any(
            publication["publication_kind"] == "model_remove"
            and publication["status"] in {
                "queued",
                "validating",
                "provisioning",
                "building_revision",
                "verifying",
                "promoting",
                "rolling_back",
            }
            and str(publication["desired_spec"]["removed_models"][0]["model_id"])
            == str(item_id)
            for publication in self.gateway_publications
        ):
            raise ValueError("Model deletion is already in progress")
        if values.get("is_default") is True:
            for item in target:
                item["is_default"] = False
            if kind == "runtime":
                for model in self.models:
                    model["is_default"] = False
                candidate = next(
                    (
                        model
                        for model in self.models
                        if model["runtime_id"] == item_id and model["enabled"]
                    ),
                    None,
                )
                if candidate is not None:
                    candidate["is_default"] = True
            elif kind == "model":
                for runtime in self.runtimes:
                    runtime["is_default"] = runtime["id"] == values["runtime_id"]
        pricing_changed = kind == "model" and any(
            field in values and values[field] != row.get(field)
            for field in ("price_source", "price_reference")
        )
        row.update({
            key: value for key, value in values.items() if key != "pricing_expected_updated_at"
        })
        if kind == "model" and (pricing_changed or values.get("source_snapshot") is not None):
            row["pending_list_price"] = None
        if pricing_changed:
            for field in (
                "list_input_cost_per_million", "list_output_cost_per_million",
                "list_cached_cost_per_million", "list_cache_write_cost_per_million",
                "match_metadata", "source_snapshot",
            ):
                if field not in values:
                    row[field] = None
        if kind == "model" and "price_source" in values:
            row["price_source_configured"] = True
            row["price_config_updated_at"] = datetime.now(UTC)
            if values["price_source"] == "manual":
                row["price_reference"] = None
        row["updated_at"] = datetime.now(UTC)
        if kind == "runtime":
            provider = self._require(self.providers, row["provider_id"], "provider")
            gateway = self._find(self.gateways, row.get("gateway_profile_id"))
            row.update(
                provider_name=provider["name"],
                gateway_name=gateway["name"] if gateway else None,
            )
        elif kind == "model":
            provider = self._require(self.providers, row["provider_id"], "provider")
            runtime = self._require(self.runtimes, row["runtime_id"], "runtime")
            if runtime["provider_id"] != provider["id"]:
                raise ValueError("The selected runtime does not belong to the provider")
            row.update(provider_name=provider["name"], runtime_name=runtime["name"])
        return row

    def delete_runtime_if_empty(self, runtime_id: UUID) -> bool:
        runtime = self._find(self.runtimes, runtime_id)
        if runtime is None or any(
            model["runtime_id"] == runtime_id for model in self.models
        ):
            return False
        self.runtimes.remove(runtime)
        return True

    def delete_gateway_if_unused(self, gateway_id: UUID) -> bool:
        gateway = self._find(self.gateways, gateway_id)
        if (
            gateway is None
            or gateway["is_default"]
            or any(runtime.get("gateway_profile_id") == gateway_id for runtime in self.runtimes)
            or any(
                publication["gateway_profile_id"] == gateway_id
                for publication in self.gateway_publications
            )
            or gateway_id in self.effective_gateway_releases
        ):
            return False
        self.gateways.remove(gateway)
        return True

    def _registry_target(self, kind: str) -> list[dict[str, Any]]:
        targets = {
            "gateway": self.gateways,
            "provider": self.providers,
            "runtime": self.runtimes,
            "model": self.models,
        }
        if kind not in targets:
            raise ValueError(f"Unsupported registry kind: {kind}")
        return targets[kind]

    @staticmethod
    def _find(items: list[dict[str, Any]], item_id: Any) -> dict[str, Any] | None:
        return next((item for item in items if item["id"] == item_id), None)

    @classmethod
    def _require(
        cls, items: list[dict[str, Any]], item_id: Any, label: str
    ) -> dict[str, Any]:
        item = cls._find(items, item_id)
        if item is None:
            raise ValueError(f"Unknown {label}: {item_id}")
        return item
