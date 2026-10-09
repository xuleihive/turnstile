from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from ..domain.models import ModelIdentity
from ..domain.pricing import PriceConfigurationChanged
from ..domain.runtime_models import apply_databricks_adoption


class PostgreSqlRegistryRepositoryMixin:
    _identities: dict[str, ModelIdentity] | None
    _identities_read_at: float
    _identity_lock: threading.Lock

    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def registry(self) -> dict[str, Sequence[dict[str, Any]]]:
        with self._connection() as connection:
            gateways = connection.execute(
                "SELECT * FROM gateway_profile ORDER BY name"
            ).fetchall()
            providers = connection.execute(
                """SELECT provider.*,
                                    COALESCE(metadata.brand_key, 'generic') AS brand_key
                     FROM model_provider provider
                     LEFT JOIN model_provider_metadata metadata
                         ON metadata.provider_id = provider.id
                     ORDER BY provider.name"""
            ).fetchall()
            runtimes = connection.execute(
                """SELECT runtime.*, provider.name AS provider_name,
                                    gateway.name AS gateway_name,
                                    COALESCE(metadata.brand_key, 'generic') AS brand_key,
                                    price.price_discount_percent,
                                    adopted.value->'runtime_config' AS adopted_config
                     FROM model_runtime runtime
                     JOIN model_provider provider ON provider.id = runtime.provider_id
                     LEFT JOIN gateway_profile gateway ON gateway.id = runtime.gateway_profile_id
                     LEFT JOIN model_runtime_metadata metadata
                         ON metadata.runtime_id = runtime.id
                     LEFT JOIN model_runtime_price price
                         ON price.runtime_id = runtime.id
                     LEFT JOIN effective_gateway_release effective
                         ON effective.gateway_profile_id = runtime.gateway_profile_id
                     LEFT JOIN gateway_publication publication
                         ON publication.id = effective.publication_id
                     LEFT JOIN LATERAL (
                         SELECT value FROM jsonb_array_elements(
                             COALESCE(publication.desired_spec->'bindings', '[]'::jsonb)
                         ) binding
                         WHERE value->>'runtime_id' = runtime.id::text
                           AND value->>'provider_id' = provider.id::text
                           AND value->>'routing_managed' = 'true'
                           AND (
                               value->'runtime_config'->>'databricks_connection_adoption' = 'true'
                               OR value->>'auth_strategy' = 'oauth_client_credentials'
                           )
                         LIMIT 1
                     ) adopted ON TRUE
                     ORDER BY runtime.is_default DESC, runtime.name"""
            ).fetchall()
            runtimes = [
                apply_databricks_adoption(row, row.pop("adopted_config", None)) for row in runtimes
            ]
            models = connection.execute(
                """SELECT model.*, provider.name AS provider_name,
                                    runtime.name AS runtime_name,
                                    COALESCE(metadata.family_key, 'generic') AS family_key,
                                    COALESCE(metadata.upstream_model_id, model.model_key)
                                        AS upstream_model_id,
                                    COALESCE(metadata.assignment_required, FALSE)
                                        AS assignment_required,
                                    metadata.publication_id,
                                    binding.value->'model'->'image_profile' AS image_profile,
                                    -- A model with no price row was priced by hand, which is
                                    -- what 'manual' means, so the COALESCE is the whole
                                    -- backfill.
                                    COALESCE(price.price_source, 'models_dev') AS price_source,
                                    (price.model_id IS NOT NULL) AS price_source_configured,
                                    price.updated_at AS price_config_updated_at,
                                    price.match_metadata,
                                    price.source_snapshot,
                                    price.price_reference,
                                    price.list_input_cost_per_million,
                                    price.list_output_cost_per_million,
                                    price.list_cached_cost_per_million,
                                    price.list_cache_write_cost_per_million,
                                    price.price_discount_percent,
                                    price.price_synced_at,
                                    price.price_sync_status,
                                    price.price_sync_message,
                                    price.pending_list_price,
                                    -- Resolved here rather than in the caller so every reader
                                    -- sees the same answer to "which discount actually applied".
                                    COALESCE(
                                        price.price_discount_percent,
                                        runtime_price.price_discount_percent
                                    ) AS effective_discount_percent
                     FROM managed_model model
                     JOIN model_provider provider ON provider.id = model.provider_id
                     JOIN model_runtime runtime ON runtime.id = model.runtime_id
                     LEFT JOIN managed_model_price price ON price.model_id = model.id
                     LEFT JOIN model_runtime_price runtime_price
                         ON runtime_price.runtime_id = runtime.id
                     LEFT JOIN managed_model_metadata metadata
                         ON metadata.model_id = model.id
                     LEFT JOIN effective_gateway_release effective
                         ON effective.gateway_profile_id = runtime.gateway_profile_id
                     LEFT JOIN gateway_publication publication
                         ON publication.id = effective.publication_id
                     LEFT JOIN LATERAL (
                         SELECT value FROM jsonb_array_elements(
                             COALESCE(publication.desired_spec->'bindings', '[]'::jsonb)
                         ) binding
                         WHERE lower(value->'model'->>'model_key') = lower(model.model_key)
                         LIMIT 1
                     ) binding ON TRUE
                     ORDER BY model.is_default DESC, model.display_name"""
            ).fetchall()
        return {
            "gateways": cast(Sequence[dict[str, Any]], gateways),
            "providers": cast(Sequence[dict[str, Any]], providers),
            "runtimes": cast(Sequence[dict[str, Any]], runtimes),
            "models": cast(Sequence[dict[str, Any]], models),
        }

    # A sync reads every model, talks to a price feed over the network, then writes. Between the
    # read and the write someone can open the model and change how it is priced -- switch it back
    # to manual and type a rate, repoint it, change the discount. The plan carries what it was
    # made against and every write is guarded by it, so a result that is about to land on a
    # configuration that no longer exists lands nowhere instead.
    _PRICE_CONFIG_STILL_MATCHES = """
        (%(expected_updated_at)s::timestamptz IS NULL
            OR model.updated_at = %(expected_updated_at)s::timestamptz)
        AND NOT EXISTS (
            SELECT 1 FROM gateway_publication removing
            WHERE removing.publication_kind = 'model_remove'
              AND removing.status IN (
                'queued','validating','provisioning','building_revision',
                'verifying','promoting','rolling_back'
              )
              AND removing.desired_spec #>> '{removed_models,0,model_id}' = model.id::text
        )
        AND EXISTS (
            SELECT 1 FROM model_runtime runtime
            LEFT JOIN model_runtime_price runtime_price ON runtime_price.runtime_id = runtime.id
            LEFT JOIN managed_model_price guard ON guard.model_id = model.id
            WHERE runtime.id = model.runtime_id
              AND (
                (%(expected_configured)s AND guard.model_id IS NOT NULL
                  AND guard.price_source = %(expected_source)s
                  AND guard.price_reference IS NOT DISTINCT FROM %(expected_reference)s
                  AND (%(expected_price_updated_at)s::timestamptz IS NULL
                    OR guard.updated_at = %(expected_price_updated_at)s::timestamptz))
                OR (NOT %(expected_configured)s AND guard.model_id IS NULL)
              )
              AND COALESCE(guard.price_discount_percent, runtime_price.price_discount_percent)
                  IS NOT DISTINCT FROM %(expected_discount_percent)s::numeric
        )
    """

    def apply_model_price_sync(self, updates: Sequence[Mapping[str, Any]]) -> int:
        written = 0
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('model-pricing', 0))"
            )
            for update in updates:
                parameters = dict(update)
                parameters.setdefault("expected_configured", True)
                parameters.setdefault("expected_updated_at", None)
                parameters.setdefault("expected_price_updated_at", None)
                parameters.setdefault("price_source", parameters.get("expected_source"))
                parameters.setdefault("price_reference", parameters.get("expected_reference"))
                parameters["match_metadata"] = (
                    Jsonb(update["match_metadata"]) if update.get("match_metadata") else None
                )
                parameters["source_snapshot"] = (
                    Jsonb(update["source_snapshot"]) if update.get("source_snapshot") else None
                )
                parameters["pending_list_price"] = (
                    Jsonb(update["pending_list_price"])
                    if update.get("pending_list_price") is not None
                    else None
                )
                superseded = False
                if update.get("writes"):
                    # The charged rates live on managed_model and always have: they are what
                    # the billing path reads, and this job is only one of the ways they get
                    # set. Where they came from is the part that is new.
                    row = connection.execute(
                        f"""UPDATE managed_model model SET
                               input_cost_per_million = %(input_cost_per_million)s,
                               output_cost_per_million = %(output_cost_per_million)s,
                               cached_cost_per_million = %(cached_cost_per_million)s,
                               cache_write_cost_per_million =
                                   %(cache_write_cost_per_million)s,
                               updated_at = now()
                           WHERE model.id = %(model_id)s
                             AND {self._PRICE_CONFIG_STILL_MATCHES}
                           RETURNING model.id""",
                        parameters,
                    ).fetchone()
                    if row is None:
                        superseded = True
                    else:
                        written += 1

                if superseded:
                    # Nothing about the rates changed, so nothing about the baseline should
                    # either. Only the record of what happened, and only if the row is still
                    # the one that was planned for.
                    connection.execute(
                        """UPDATE managed_model_price SET
                               price_sync_status = 'superseded',
                               price_sync_message =
                                   '同步期间该模型的计价配置被改动，本次结果已作废，未写入',
                               price_synced_at = now(),
                               updated_at = now()
                           WHERE model_id = %(model_id)s
                             AND price_source = %(expected_source)s
                             AND price_reference IS NOT DISTINCT FROM %(expected_reference)s
                             AND (%(expected_price_updated_at)s::timestamptz IS NULL
                               OR updated_at = %(expected_price_updated_at)s::timestamptz)""",
                        parameters,
                    )
                    continue

                if update.get("writes"):
                    connection.execute(
                        """INSERT INTO managed_model_price (
                               model_id, price_source, price_reference
                           ) VALUES (%(model_id)s, %(price_source)s, %(price_reference)s)
                           ON CONFLICT (model_id) DO NOTHING""", parameters,
                    )

                # The outcome is recorded either way. On a skip the rates stay exactly as they
                # are and only the record changes, so the registry can say why a model was
                # passed over instead of going quiet. An UPDATE is enough: a model without a
                # price row is manual, and the planner never selects one.
                #
                # list_* moves only when the sync accepted the price. A figure held back for
                # review goes to pending_list_price, because a baseline that follows the price
                # it is meant to gate agrees with it on the second run.
                connection.execute(
                    f"""UPDATE managed_model_price price SET
                           price_source = CASE WHEN %(writes)s
                               THEN %(price_source)s ELSE price.price_source END,
                           price_reference = CASE WHEN %(writes)s
                               THEN %(price_reference)s ELSE price.price_reference END,
                           match_metadata = CASE WHEN %(writes)s
                               THEN %(match_metadata)s::jsonb ELSE price.match_metadata END,
                           source_snapshot = CASE WHEN %(writes)s
                               THEN %(source_snapshot)s::jsonb ELSE price.source_snapshot END,
                           list_input_cost_per_million = CASE WHEN %(writes)s
                               THEN COALESCE(%(list_input_cost_per_million)s,
                                             list_input_cost_per_million)
                               ELSE list_input_cost_per_million END,
                           list_output_cost_per_million = CASE WHEN %(writes)s
                               THEN COALESCE(%(list_output_cost_per_million)s,
                                             list_output_cost_per_million)
                               ELSE list_output_cost_per_million END,
                           list_cached_cost_per_million = CASE WHEN %(writes)s
                               THEN COALESCE(%(list_cached_cost_per_million)s,
                                             list_cached_cost_per_million)
                               ELSE list_cached_cost_per_million END,
                           list_cache_write_cost_per_million = CASE WHEN %(writes)s
                               THEN COALESCE(%(list_cache_write_cost_per_million)s,
                                             list_cache_write_cost_per_million)
                               ELSE list_cache_write_cost_per_million END,
                           -- Accepting the price clears the proposal; proposing one replaces
                           -- it; a run that could not read the source leaves the standing
                           -- proposal alone rather than forgetting what is waiting.
                           --
                           -- The cast is load-bearing. This parameter appears only inside
                           -- `IS NOT NULL` and a CASE branch, so when it is NULL -- which is
                           -- every ordinary sync -- PostgreSQL has nothing to infer a type
                           -- from and rejects the statement with AmbiguousParameter.
                           pending_list_price = CASE
                               WHEN %(writes)s THEN NULL
                               WHEN %(pending_list_price)s::jsonb IS NOT NULL
                                   THEN %(pending_list_price)s::jsonb
                               ELSE pending_list_price END,
                           price_sync_status = %(status)s,
                           price_sync_message = %(message)s,
                           price_synced_at = now(),
                           updated_at = now()
                       WHERE model_id = %(model_id)s
                         AND (%(writes)s OR EXISTS (
                           SELECT 1 FROM managed_model model
                           WHERE model.id = price.model_id
                             AND {self._PRICE_CONFIG_STILL_MATCHES}
                         ))""",
                    parameters,
                )
        self._invalidate_identities()
        return written

    def create_registry_item(self, kind: str, values: Mapping[str, Any]) -> dict[str, Any]:
        parameters = dict(values)
        if "config" in parameters:
            parameters["config"] = Jsonb(parameters["config"])
        with self._connection() as connection, connection.transaction():
            if kind == "gateway":
                if values["is_default"]:
                    connection.execute("UPDATE gateway_profile SET is_default = FALSE")
                row = connection.execute(
                    """INSERT INTO gateway_profile (
                               name, implementation, base_url, auth_type,
                               credential_ciphertext, credential_hint, enabled,
                               is_default, config
                           ) VALUES (%(name)s, %(implementation)s, %(base_url)s,
                               %(auth_type)s, %(credential_ciphertext)s,
                               %(credential_hint)s, %(enabled)s, %(is_default)s,
                               %(config)s) RETURNING *""",
                        parameters,
                ).fetchone()
            elif kind == "provider":
                row = connection.execute(
                    """INSERT INTO model_provider (
                               name, provider_kind, endpoint_url, auth_type,
                               credential_ciphertext, credential_hint, enabled, config
                           ) VALUES (%(name)s, %(provider_kind)s, %(endpoint_url)s,
                               %(auth_type)s, %(credential_ciphertext)s,
                               %(credential_hint)s, %(enabled)s, %(config)s)
                           RETURNING *""",
                        parameters,
                ).fetchone()
                connection.execute(
                    """INSERT INTO model_provider_metadata (provider_id, brand_key)
                       VALUES (%s, %s)""",
                    (row["id"], values.get("brand_key", "generic")),
                )
                row = {**row, "brand_key": values.get("brand_key", "generic")}
            elif kind == "runtime":
                if values["is_default"]:
                    connection.execute("UPDATE model_runtime SET is_default = FALSE")
                row = connection.execute(
                    """INSERT INTO model_runtime (
                               provider_id, gateway_profile_id, name, runtime_kind,
                               enabled, is_default, config, allowed_roles
                           ) VALUES (%(provider_id)s, %(gateway_profile_id)s,
                               %(name)s, %(runtime_kind)s, %(enabled)s,
                               %(is_default)s, %(config)s, %(allowed_roles)s)
                           RETURNING *""",
                        parameters,
                ).fetchone()
                connection.execute(
                    """INSERT INTO model_runtime_metadata (runtime_id, brand_key)
                       VALUES (%s, %s)""",
                    (row["id"], values.get("brand_key", "generic")),
                )
                discount = values.get("price_discount_percent")
                if discount is not None:
                    connection.execute(
                        """INSERT INTO model_runtime_price (
                               runtime_id, price_discount_percent
                           ) VALUES (%s, %s)""",
                        (row["id"], discount),
                    )
                row = {
                    **row,
                    "brand_key": values.get("brand_key", "generic"),
                    "price_discount_percent": discount,
                }
            elif kind == "model":
                if values["is_default"]:
                    connection.execute("UPDATE managed_model SET is_default = FALSE")
                    connection.execute("UPDATE model_runtime SET is_default = FALSE")
                    connection.execute(
                        "UPDATE model_runtime SET is_default = TRUE WHERE id = %s",
                        (parameters["runtime_id"],),
                    )
                row = connection.execute(
                    """INSERT INTO managed_model (
                               provider_id, runtime_id, model_key, display_name,
                               enabled, is_default, capabilities, context_window,
                               input_cost_per_million, output_cost_per_million,
                               cached_cost_per_million, cache_write_cost_per_million,
                               allowed_roles
                           ) VALUES (%(provider_id)s, %(runtime_id)s, %(model_key)s,
                               %(display_name)s, %(enabled)s, %(is_default)s,
                               %(capabilities)s, %(context_window)s,
                               %(input_cost_per_million)s, %(output_cost_per_million)s,
                               %(cached_cost_per_million)s, %(cache_write_cost_per_million)s,
                               %(allowed_roles)s) RETURNING *""",
                        parameters,
                ).fetchone()
                metadata = {
                    "family_key": values.get("family_key", "generic"),
                    "upstream_model_id": values.get("upstream_model_id")
                    or values["model_key"],
                    "assignment_required": values.get("assignment_required", False),
                    "publication_id": values.get("publication_id"),
                }
                connection.execute(
                    """INSERT INTO managed_model_metadata (
                           model_id, family_key, upstream_model_id,
                           assignment_required, publication_id
                       ) VALUES (%s, %s, %s, %s, %s)""",
                    (
                        row["id"],
                        metadata["family_key"],
                        metadata["upstream_model_id"],
                        metadata["assignment_required"],
                        metadata["publication_id"],
                    ),
                )
                price = self._write_price(connection, "model", row["id"], values)
                row = {**row, **metadata, **price}
            else:
                raise ValueError(f"Unsupported registry kind: {kind}")
        self._invalidate_identities()
        return cast(dict[str, Any], row)

    def create_connection(
        self,
        provider_id: UUID | None,
        provider_values: Mapping[str, Any] | None,
        runtime_values: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (provider_id is None) == (provider_values is None):
            raise ValueError("select an existing provider or one provider template")
        runtime_parameters = dict(runtime_values)
        runtime_config = dict(runtime_parameters["config"])
        runtime_parameters["config"] = Jsonb(runtime_config)
        with self._connection() as connection, connection.transaction():
            if runtime_config.get("workspace_url"):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"gateway-publication:{runtime_values['gateway_profile_id']}",),
                )
                in_flight = connection.execute(
                    """SELECT id FROM gateway_publication
                       WHERE gateway_profile_id = %s
                         AND status IN (
                             'queued', 'validating', 'provisioning', 'building_revision',
                             'verifying', 'awaiting_authorization', 'promoting', 'rolling_back'
                         ) LIMIT 1""",
                    (runtime_values["gateway_profile_id"],),
                ).fetchone()
                if in_flight is not None:
                    raise ValueError(
                        "Wait for the gateway publication before creating a connection"
                    )
            project_endpoint = str(runtime_config.get("project_endpoint") or "").rstrip("/")
            if project_endpoint:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (
                        "model-connection:"
                        f"{runtime_values['gateway_profile_id']}:"
                        f"{project_endpoint.casefold()}",
                    ),
                )
                duplicate = connection.execute(
                    """SELECT id
                       FROM model_runtime
                       WHERE gateway_profile_id = %s
                         AND runtime_kind = 'foundry'
                         AND lower(rtrim(config->>'project_endpoint', '/')) = lower(%s)
                       LIMIT 1""",
                    (runtime_values["gateway_profile_id"], project_endpoint),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("This connection already exists")
            workspace_url = str(runtime_config.get("workspace_url") or "").rstrip("/")
            if workspace_url:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (
                        f"model-connection:{runtime_values['gateway_profile_id']}:"
                        f"{workspace_url.casefold()}",
                    ),
                )
                duplicate = connection.execute(
                    """SELECT runtime.id
                       FROM model_runtime runtime
                       WHERE runtime.gateway_profile_id = %s
                         AND (
                             lower(rtrim(runtime.config->>'workspace_url', '/')) = lower(%s)
                             OR EXISTS (
                                 SELECT 1 FROM effective_gateway_release effective
                                 JOIN gateway_publication publication
                                     ON publication.id = effective.publication_id
                                 CROSS JOIN LATERAL jsonb_array_elements(
                                     COALESCE(publication.desired_spec->'bindings', '[]'::jsonb)
                                 ) binding
                                 WHERE effective.gateway_profile_id = runtime.gateway_profile_id
                                   AND binding->>'runtime_id' = runtime.id::text
                                   AND binding->'runtime_config'
                                       ->>'databricks_connection_adoption' = 'true'
                                   AND lower(rtrim(
                                       binding->'runtime_config'->>'workspace_url', '/'
                                   )) = lower(%s)
                             )
                         ) LIMIT 1""",
                    (runtime_values["gateway_profile_id"], workspace_url, workspace_url),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("This connection already exists")
            if provider_values is not None:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"model-provider:{provider_values['brand_key']}",),
                )
                provider = connection.execute(
                    """SELECT provider.*
                       FROM model_provider provider
                       JOIN model_provider_metadata metadata
                         ON metadata.provider_id = provider.id
                       WHERE metadata.brand_key = %s AND provider.enabled
                       ORDER BY provider.created_at
                       LIMIT 1""",
                    (provider_values["brand_key"],),
                ).fetchone()
                if provider is None:
                    parameters = dict(provider_values)
                    parameters["config"] = Jsonb(parameters["config"])
                    provider = connection.execute(
                        """INSERT INTO model_provider (
                                   name, provider_kind, endpoint_url, auth_type,
                                   credential_ciphertext, credential_hint, enabled, config
                               ) VALUES (%(name)s, %(provider_kind)s, %(endpoint_url)s,
                                   %(auth_type)s, %(credential_ciphertext)s,
                                   %(credential_hint)s, %(enabled)s, %(config)s)
                               RETURNING *""",
                        parameters,
                    ).fetchone()
                    connection.execute(
                        """INSERT INTO model_provider_metadata (provider_id, brand_key)
                           VALUES (%s, %s)""",
                        (provider["id"], provider_values["brand_key"]),
                    )
                provider_id = provider["id"]
            runtime_parameters["provider_id"] = provider_id
            row = connection.execute(
                """INSERT INTO model_runtime (
                           provider_id, gateway_profile_id, name, runtime_kind,
                           enabled, is_default, config, allowed_roles
                       ) VALUES (%(provider_id)s, %(gateway_profile_id)s,
                           %(name)s, %(runtime_kind)s, %(enabled)s,
                           %(is_default)s, %(config)s, %(allowed_roles)s)
                       RETURNING *""",
                runtime_parameters,
            ).fetchone()
            connection.execute(
                """INSERT INTO model_runtime_metadata (runtime_id, brand_key)
                   VALUES (%s, %s)""",
                (row["id"], runtime_values.get("brand_key", "generic")),
            )
        self._invalidate_identities()
        return cast(
            dict[str, Any],
            {**row, "brand_key": runtime_values.get("brand_key", "generic")},
        )

    def delete_runtime_if_empty(self, runtime_id: UUID) -> bool:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """DELETE FROM model_runtime runtime
                   WHERE runtime.id = %s
                     AND NOT EXISTS (
                         SELECT 1 FROM managed_model model
                         WHERE model.runtime_id = runtime.id
                     )
                   RETURNING runtime.id""",
                (runtime_id,),
            ).fetchone()
        if row is not None:
            self._invalidate_identities()
        return row is not None

    def delete_gateway_if_unused(self, gateway_id: UUID) -> bool:
        with self._connection() as connection, connection.transaction():
            row = connection.execute(
                """DELETE FROM gateway_profile gateway
                   WHERE gateway.id = %s
                     AND NOT gateway.is_default
                     AND NOT EXISTS (
                         SELECT 1 FROM model_runtime runtime
                         WHERE runtime.gateway_profile_id = gateway.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM gateway_publication publication
                         WHERE publication.gateway_profile_id = gateway.id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM effective_gateway_release release
                         WHERE release.gateway_profile_id = gateway.id
                     )
                   RETURNING gateway.id""",
                (gateway_id,),
            ).fetchone()
        return row is not None

    def _invalidate_identities(self) -> None:
        """Drop the cached identity map after a registry write.

        Without this an administrator could rename a model and still see the old name in a
        trace for a full cache window, which reads as the write having failed.
        """
        with self._identity_lock:
            self._identities = None
            self._identities_read_at = 0.0

    def update_registry_item(
        self, kind: str, item_id: UUID, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        table = {
            "gateway": "gateway_profile",
            "provider": "model_provider",
            "runtime": "model_runtime",
            "model": "managed_model",
        }.get(kind)
        if table is None:
            raise ValueError(f"Unsupported registry kind: {kind}")
        allowed = {
            "gateway": [
                "name",
                "implementation",
                "base_url",
                "auth_type",
                "credential_ciphertext",
                "credential_hint",
                "enabled",
                "is_default",
                "config",
            ],
            "provider": [
                "name",
                "provider_kind",
                "endpoint_url",
                "auth_type",
                "credential_ciphertext",
                "credential_hint",
                "enabled",
                "config",
            ],
            "runtime": [
                "provider_id",
                "gateway_profile_id",
                "name",
                "runtime_kind",
                "enabled",
                "is_default",
                "config",
                "allowed_roles",
            ],
            "model": [
                "provider_id",
                "runtime_id",
                "model_key",
                "display_name",
                "enabled",
                "is_default",
                "capabilities",
                "context_window",
                "input_cost_per_million",
                "output_cost_per_million",
                "cached_cost_per_million",
                "cache_write_cost_per_million",
                "allowed_roles",
            ],
        }[kind]
        assignments = [f"{column} = %({column})s" for column in allowed if column in values]
        metadata_fields = {
            "provider": {"brand_key"},
            "runtime": {"brand_key"},
            "model": {"family_key", "upstream_model_id", "assignment_required"},
        }.get(kind, set())
        metadata_values = {key: values[key] for key in metadata_fields if key in values}
        price_fields = {
            "runtime": {"price_discount_percent"},
            "model": {
                "price_source", "price_reference", "price_discount_percent",
                "list_input_cost_per_million", "list_output_cost_per_million",
                "list_cached_cost_per_million", "list_cache_write_cost_per_million",
                "match_metadata", "source_snapshot",
            },
        }.get(kind, set())
        price_values = {key: values[key] for key in price_fields if key in values}
        if not assignments and not metadata_values and not price_values:
            return None
        parameters = dict(values)
        if "config" in parameters:
            parameters["config"] = Jsonb(parameters["config"])
        parameters["id"] = item_id
        with self._connection() as connection, connection.transaction():
            if kind in {"model", "runtime"}:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('model-pricing', 0))"
                )
            if kind == "model" and values.get("pricing_expected_updated_at") is not None:
                guard = connection.execute(
                    "SELECT updated_at FROM managed_model WHERE id = %s FOR UPDATE",
                    (item_id,),
                ).fetchone()
                if guard is None or guard["updated_at"] != values["pricing_expected_updated_at"]:
                    raise PriceConfigurationChanged("模型在定价期间已修改，请刷新后重试")
            if kind == "model":
                removing = connection.execute(
                    """SELECT id FROM gateway_publication
                       WHERE publication_kind = 'model_remove'
                         AND status IN (
                           'queued', 'validating', 'provisioning', 'building_revision',
                           'verifying', 'promoting', 'rolling_back'
                         )
                         AND desired_spec #>> '{removed_models,0,model_id}' = %s""",
                    (str(item_id),),
                ).fetchone()
                if removing is not None:
                    raise ValueError("Model deletion is already in progress")
            if values.get("is_default") is True:
                connection.execute(f"UPDATE {table} SET is_default = FALSE")
                if kind == "runtime":
                    connection.execute("UPDATE managed_model SET is_default = FALSE")
                    connection.execute(
                        """UPDATE managed_model SET is_default = TRUE
                           WHERE id = (
                               SELECT id FROM managed_model
                               WHERE runtime_id = %s AND enabled
                               ORDER BY created_at LIMIT 1
                           )""",
                        (item_id,),
                    )
                elif kind == "model":
                    connection.execute("UPDATE model_runtime SET is_default = FALSE")
                    connection.execute(
                        "UPDATE model_runtime SET is_default = TRUE WHERE id = %s",
                        (parameters["runtime_id"],),
                    )
            if assignments:
                row = connection.execute(
                    f"UPDATE {table} SET {', '.join(assignments)}, updated_at = now() "
                    "WHERE id = %(id)s RETURNING *",
                    parameters,
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE id = %s", (item_id,)
                ).fetchone()
            if row is None:
                return None
            if kind == "provider" and metadata_values:
                brand_key = metadata_values.get("brand_key", "generic")
                connection.execute(
                    """INSERT INTO model_provider_metadata (provider_id, brand_key)
                       VALUES (%s, %s)
                       ON CONFLICT (provider_id) DO UPDATE SET
                         brand_key = EXCLUDED.brand_key""",
                    (item_id, brand_key),
                )
                row = {**row, "brand_key": brand_key}
            elif kind == "runtime" and metadata_values:
                brand_key = metadata_values.get("brand_key", "generic")
                connection.execute(
                    """INSERT INTO model_runtime_metadata (runtime_id, brand_key)
                       VALUES (%s, %s)
                       ON CONFLICT (runtime_id) DO UPDATE SET
                         brand_key = EXCLUDED.brand_key""",
                    (item_id, brand_key),
                )
                row = {**row, "brand_key": brand_key}
            elif kind == "model" and metadata_values:
                metadata = connection.execute(
                    """SELECT * FROM managed_model_metadata WHERE model_id = %s""",
                    (item_id,),
                ).fetchone()
                family_key = metadata_values.get(
                    "family_key", metadata["family_key"] if metadata else "generic"
                )
                upstream_model_id = metadata_values.get(
                    "upstream_model_id",
                    metadata["upstream_model_id"] if metadata else row["model_key"],
                )
                assignment_required = metadata_values.get(
                    "assignment_required",
                    metadata["assignment_required"] if metadata else False,
                )
                connection.execute(
                    """INSERT INTO managed_model_metadata (
                           model_id, family_key, upstream_model_id, assignment_required
                       ) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (model_id) DO UPDATE SET
                         family_key = EXCLUDED.family_key,
                         upstream_model_id = EXCLUDED.upstream_model_id,
                         assignment_required = EXCLUDED.assignment_required""",
                    (item_id, family_key, upstream_model_id, assignment_required),
                )
                row = {
                    **row,
                    "family_key": family_key,
                    "upstream_model_id": upstream_model_id,
                    "assignment_required": assignment_required,
                    "publication_id": metadata["publication_id"] if metadata else None,
                }
            # Outside the chain above: a price edit can arrive on its own, and pricing a
            # model is the one edit that is always on its own -- nothing else on the form
            # changes when someone picks a list price.
            if price_values:
                row = {**row, **self._write_price(connection, kind, item_id, price_values)}
        self._invalidate_identities()
        return cast(dict[str, Any] | None, row)

    @staticmethod
    def _write_price(
        connection: Any,
        kind: str,
        item_id: UUID,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Upsert the price row and return the fields as they now stand."""
        if kind == "runtime":
            discount = values.get("price_discount_percent")
            connection.execute(
                """INSERT INTO model_runtime_price (runtime_id, price_discount_percent)
                   VALUES (%s, %s)
                   ON CONFLICT (runtime_id) DO UPDATE SET
                     price_discount_percent = EXCLUDED.price_discount_percent,
                     updated_at = now()""",
                (item_id, discount),
            )
            return {"price_discount_percent": discount}

        current = connection.execute(
            "SELECT * FROM managed_model_price WHERE model_id = %s", (item_id,)
        ).fetchone()
        merged = {
            "price_source": values.get(
                "price_source", current["price_source"] if current else "models_dev"
            ),
            "price_reference": values.get(
                "price_reference", current["price_reference"] if current else None
            ),
            "price_discount_percent": values.get(
                "price_discount_percent",
                current["price_discount_percent"] if current else None,
            ),
        }
        # Going back to manual drops the reference with it. Keeping it would leave a model
        # that reads as "following gpt-4.1 mini" while charging whatever was typed.
        if merged["price_source"] == "manual":
            merged["price_reference"] = None
        baseline_fields = (
            "list_input_cost_per_million", "list_output_cost_per_million",
            "list_cached_cost_per_million", "list_cache_write_cost_per_million",
            "match_metadata", "source_snapshot",
        )
        reset = current is not None and any(
            merged[field] != current.get(field) for field in ("price_source", "price_reference")
        )
        for field in baseline_fields:
            merged[field] = values.get(
                field, None if reset else current.get(field) if current else None,
            )
        baseline = {
            field: Jsonb(merged[field]) if field in {"match_metadata", "source_snapshot"}
            and merged[field] is not None else merged[field] for field in baseline_fields
        }
        connection.execute(
            f"""INSERT INTO managed_model_price (
                   model_id, price_source, price_reference, price_discount_percent,
                   {", ".join(baseline_fields)}
               ) VALUES (%s, %s, %s, %s, {", ".join(["%s"] * len(baseline_fields))})
               ON CONFLICT (model_id) DO UPDATE SET
                 price_source = EXCLUDED.price_source,
                 price_reference = EXCLUDED.price_reference,
                 price_discount_percent = EXCLUDED.price_discount_percent,
                 {", ".join(f"{field} = EXCLUDED.{field}" for field in baseline_fields)},
                 pending_list_price = CASE WHEN %s
                     THEN NULL ELSE managed_model_price.pending_list_price END,
                 updated_at = now()""",
            (
                item_id,
                merged["price_source"],
                merged["price_reference"],
                merged["price_discount_percent"],
                *baseline.values(),
                reset or values.get("source_snapshot") is not None,
            ),
        )
        return merged
