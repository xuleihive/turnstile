from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from ..config import get_settings
from ..domain.enterprise import enterprise_catalog
from ..domain.model_router import ModelRouter
from ..domain.models import ModelPrice, TokenUsageRecord
from .reconciliation import ManagedIdentityTokenProvider
from .table_batch import TableBatchConflict, conditional_batch, validate_batch_response

logger = logging.getLogger(__name__)

ROUTER_CATALOG_PARTITION = "model-router|v1"
ROUTER_PREPARED_PARTITION = "model-router-prepared|v1"
ROUTER_STICKY_PARTITION_PREFIX = "model-router-sticky|"
ROUTER_TRACE_PARTITION = "model-router-trace|v1"

_SYNC_LOCK = threading.RLock()
_LAST_TRACE_SYNC_MONO = 0.0
_SYNCED_TRACE_IDS: set[str] = set()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def router_row_key(name_or_id: str) -> str:
    return "router:" + _sha256_hex(name_or_id.strip().lower())


def _endpoint_origin(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlsplit(str(url).strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _resolve_model_pricing(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_tiers = row.get("context_price_tiers") or []
    if raw_tiers:
        price = ModelPrice.from_registry(row)
        if price is not None and price.context_tiers:
            resolved = price.for_input_tokens(0)
            in_cost = float(resolved.input_price_per_million)
            cached_cost = float(resolved.cached_price_per_million)
            cw_cost = float(resolved.cache_write_price_per_million)
            out_cost = float(resolved.output_price_per_million)
            return {
                "input_cost_per_million": in_cost,
                "cached_cost_per_million": cached_cost,
                "cache_write_cost_per_million": cw_cost,
                "output_cost_per_million": out_cost,
                "effective_cost_per_million": max(in_cost, out_cost),
                "context_price_tiers": [
                    tier.model_dump(mode="json") for tier in price.context_tiers
                ],
            }
    raw_in = row.get("input_cost_per_million")
    raw_out = row.get("output_cost_per_million")
    if raw_in is None and raw_out is None:
        return {
            "input_cost_per_million": None,
            "cached_cost_per_million": None,
            "cache_write_cost_per_million": None,
            "output_cost_per_million": None,
            "effective_cost_per_million": None,
            "context_price_tiers": [],
        }
    in_cost = float(raw_in) if raw_in is not None else 0.0
    out_cost = float(raw_out) if raw_out is not None else 0.0
    raw_cached = row.get("cached_cost_per_million")
    cached_cost = float(raw_cached) if raw_cached is not None else in_cost
    raw_cw = row.get("cache_write_cost_per_million")
    is_decision = "decision" in (row.get("capabilities") or [])
    cw_cost = (
        float(raw_cw)
        if raw_cw is not None
        else (0.0 if is_decision else cached_cost)
    )
    return {
        "input_cost_per_million": in_cost,
        "cached_cost_per_million": cached_cost,
        "cache_write_cost_per_million": cw_cost,
        "output_cost_per_million": out_cost,
        "effective_cost_per_million": max(in_cost, out_cost),
        "context_price_tiers": [],
    }


def build_router_table_document(
    router: ModelRouter,
    registry: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    assigned_user_ids: Sequence[str] = (),
    allowed_subscription_ids: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    if router.status != "published":
        return None
    models_by_id = {
        str(row["id"]): row for row in registry.get("models", []) if row.get("enabled")
    }
    providers_by_id = {
        str(row["id"]): row for row in registry.get("providers", [])
    }
    runtimes_by_id = {
        str(row["id"]): row for row in registry.get("runtimes", [])
    }
    default_provider_name = (
        str(registry["providers"][0]["name"])
        if registry.get("providers")
        else ""
    )
    default_runtime_name = (
        str(registry["runtimes"][0]["name"])
        if registry.get("runtimes")
        else ""
    )

    candidates_by_id: dict[str, dict[str, Any]] = {}
    routing_models_by_id: dict[str, dict[str, Any]] = {}
    candidate_names: list[str] = []
    strategy_types: list[str] = []
    condition_fields: list[str] = []

    for node in router.definition.nodes:
        if node.type == "condition" and node.condition is not None:
            field_name = str(node.condition.field)
            if field_name not in condition_fields:
                condition_fields.append(field_name)
        elif node.type in {"strategy", "priority"}:
            eff_strategy = str(
                node.strategy_type or ("priority" if node.type == "priority" else "")
            )
            if eff_strategy and eff_strategy not in strategy_types:
                strategy_types.append(eff_strategy)
            if node.routing_model_id is not None:
                rm_id = str(node.routing_model_id)
                rm_row = models_by_id.get(rm_id)
                if rm_row is None:
                    continue
                prov = providers_by_id.get(str(rm_row.get("provider_id"))) or {}
                rt = runtimes_by_id.get(str(rm_row.get("runtime_id"))) or {}
                raw_cfg = rt.get("config")
                rt_cfg: Mapping[str, Any] = (
                    raw_cfg if isinstance(raw_cfg, Mapping) else {}
                )
                rm_pricing = _resolve_model_pricing(rm_row)
                upstream_origin = _endpoint_origin(
                    str(
                        rt_cfg.get("project_endpoint")
                        or rt_cfg.get("backend_url")
                        or rt_cfg.get("inference_endpoint")
                        or rt.get("endpoint_url")
                        or prov.get("endpoint_url")
                        or ""
                    )
                )
                upstream_model = str(
                    rm_row.get("upstream_model_id")
                    or rm_row.get("upstream_model")
                    or rm_row.get("deployment_name")
                    or str(rm_row["display_name"]).split("·")[0].strip()
                    or rm_row.get("model_key")
                )
                rm_caps = [str(c) for c in (rm_row.get("capabilities") or [])]
                is_system_one = "decision" in rm_caps
                raw_auth = str(
                    rt_cfg.get("auth_strategy") or rt_cfg.get("auth_mode") or ""
                ).strip().lower()
                auth_mode = (
                    "gateway_route" if rt_cfg.get("control_plane_managed") is True else
                    "managed_identity"
                    if is_system_one or raw_auth in {"", "managed_identity"}
                    else "gateway_route"
                )
                rm_backend_path = (
                    "/providers/microsoft/v1/systemone"
                    if is_system_one
                    else str(rt_cfg.get("backend_path") or "/openai/v1/chat/completions")
                ).replace("{upstream_model_id}", upstream_model)
                routing_models_by_id[rm_id] = {
                    "model_id": rm_id,
                    "apim_alias": str(rm_row.get("model_key") or rm_row["display_name"]),
                    "model_key": str(rm_row.get("model_key") or rm_row["display_name"]),
                    "upstream_model": upstream_model,
                    "endpoint_origin": upstream_origin,
                    "backend_path": rm_backend_path,
                    "auth_mode": auth_mode,
                    "decision_protocol": (
                        "system_one" if is_system_one else "chat_json"
                    ),
                    "capabilities": rm_caps,
                    "max_tokens_field": str(
                        rt_cfg.get("max_tokens_field") or "max_completion_tokens"
                    ),
                    "display_name": str(rm_row["display_name"]),
                    "provider_name": str(prov.get("name") or default_provider_name),
                    "runtime_name": str(rt.get("name") or default_runtime_name),
                    "input_cost_per_million": rm_pricing["input_cost_per_million"],
                    "cached_cost_per_million": rm_pricing["cached_cost_per_million"],
                    "cache_write_cost_per_million": rm_pricing["cache_write_cost_per_million"],
                    "output_cost_per_million": rm_pricing["output_cost_per_million"],
                    "context_price_tiers": rm_pricing["context_price_tiers"],
                }
        elif node.type == "candidate_models":
            if not node.candidates:
                continue
            for cand in node.candidates:
                if not getattr(cand, "enabled", True):
                    continue
                cid = str(cand.model_id)
                m_row = models_by_id.get(cid)
                if m_row is None:
                    continue
                prov = providers_by_id.get(str(m_row.get("provider_id"))) or {}
                rt = runtimes_by_id.get(str(m_row.get("runtime_id"))) or {}
                cand_pricing = _resolve_model_pricing(m_row)
                model_key = str(m_row.get("model_key") or m_row["display_name"])
                display_name = str(m_row["display_name"])
                family_key = str(m_row.get("family_key") or "").lower()
                is_anthropic = (
                    "claude" in model_key.lower()
                    or "anthropic" in family_key
                    or "claude" in display_name.lower()
                )
                profile = "anthropic-messages" if is_anthropic else "openai-v1"
                candidates_by_id[cid] = {
                    "model_id": cid,
                    "apim_alias": model_key,
                    "model_key": model_key,
                    "display_name": display_name,
                    "provider_name": str(prov.get("name") or default_provider_name),
                    "runtime_name": str(rt.get("name") or default_runtime_name),
                    "profile": profile,
                    "capabilities": list(m_row.get("capabilities") or []),
                    "context_window": int(m_row.get("context_window") or 128000),
                    "input_cost_per_million": cand_pricing["input_cost_per_million"],
                    "cached_cost_per_million": cand_pricing["cached_cost_per_million"],
                    "cache_write_cost_per_million": cand_pricing["cache_write_cost_per_million"],
                    "output_cost_per_million": cand_pricing["output_cost_per_million"],
                    "effective_cost_per_million": cand_pricing["effective_cost_per_million"],
                    "context_price_tiers": cand_pricing["context_price_tiers"],
                }
                if display_name not in candidate_names:
                    candidate_names.append(display_name)

    if not candidates_by_id:
        return None

    created_iso = (
        (router.published_at or router.updated_at).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if (router.published_at or router.updated_at)
        else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    owner_email = (
        str(router.created_by or router.updated_at and router.updated_by or "")
        .strip()
        .lower()
    )
    normalized_assignees = sorted(
        {
            str(uid).strip().lower()
            for uid in assigned_user_ids
            if str(uid).strip()
        }
        | ({owner_email} if owner_email else set())
    )
    doc_payload: dict[str, Any] = {
        "router_id": str(router.id),
        "router_name": router.name,
        "router_version": router.version,
        "status": "published",
        "description": router.description or "",
        "owner_email": owner_email,
        "assigned_user_ids": normalized_assignees,
        "created_at": created_iso,
        "published_at": created_iso,
        "strategy_types": strategy_types,
        "condition_fields": condition_fields,
        "candidate_models": candidate_names,
        "candidate_model_ids": list(candidates_by_id.keys()),
        "candidates_by_id": candidates_by_id,
        "routing_models_by_id": routing_models_by_id,
        "definition": router.definition.model_dump(mode="json"),
        "allowed_subscription_ids": sorted(
            {
                str(sid).strip().lower()
                for sid in (allowed_subscription_ids or ())
                if str(sid).strip()
            }
        ),
    }
    model_ids = set(candidates_by_id) | set(routing_models_by_id)
    gateway_ids = {str(runtimes_by_id.get(str(models_by_id[model_id].get("runtime_id")), {}).get(
        "gateway_profile_id", "",
    )) for model_id in model_ids}
    doc_payload["gateway_profile_id"] = next(iter(gateway_ids)) if len(gateway_ids) == 1 else ""
    return doc_payload


def _resolve_allowed_subscription_ids_for_router(
    router: ModelRouter,
    assigned_user_ids: Sequence[str],
    repository: Any,
) -> list[str]:
    list_apps = getattr(repository, "list_gateway_applications", None)
    list_subs = getattr(repository, "list_gateway_application_subscriptions", None)
    list_access = getattr(repository, "list_gateway_application_model_access", None)
    if not (callable(list_apps) and callable(list_subs)):
        return []
    try:
        apps = list(list_apps())
        owner_email = (
            str(router.created_by or router.updated_at and router.updated_by or "")
            .strip()
            .lower()
        )
        authorized_users = {
            str(uid).strip().lower()
            for uid in assigned_user_ids
            if str(uid).strip()
        } | ({owner_email} if owner_email else set())
        cand_ids: set[str] = set()
        for node in router.definition.nodes:
            if node.type == "candidate_models":
                for cand in node.candidates:
                    cand_ids.add(str(cand.model_id).lower())
        active_apps = [
            a
            for a in apps
            if a.get("status") == "active" and not a.get("system_managed")
        ]
        app_uuids = [UUID(str(a["id"])) for a in active_apps]
        subs = list(list_subs(app_uuids)) if app_uuids else []
        access_by_app: dict[str, set[str]] = {}
        configured_apps: set[str] = set()
        if callable(list_access) and app_uuids:
            for row in list_access(app_uuids):
                aid = str(row["application_id"]).lower()
                configured_apps.add(aid)
                if row.get("model_id") is not None:
                    access_by_app.setdefault(aid, set()).add(
                        str(row["model_id"]).lower()
                    )
        allowed_app_ids: set[str] = set()
        for app in active_apps:
            aid = str(app["id"]).lower()
            raw_owner = str(app.get("owner_id") or "").strip().lower()
            raw_creator = str(app.get("created_by") or "").strip().lower()
            app_owner = raw_owner or (raw_creator if "@" in raw_creator else "")
            if app_owner and app_owner not in authorized_users:
                continue
            if aid in configured_apps:
                allowed_models = access_by_app.get(aid, set())
                if not cand_ids or not (cand_ids <= allowed_models):
                    continue
            allowed_app_ids.add(aid)
        allowed_subs: list[str] = []
        for sub in subs:
            if (
                sub.get("state") == "active"
                and sub.get("scope_exists", True)
                and str(sub.get("application_id") or "").lower() in allowed_app_ids
            ):
                apim_sub = str(sub.get("apim_subscription_id") or "").strip().lower()
                if apim_sub and apim_sub not in allowed_subs:
                    allowed_subs.append(apim_sub)
        return sorted(allowed_subs)
    except Exception:
        logger.warning(
            "Failed to resolve allowed_subscription_ids for router %s; failing closed",
            router.id,
            exc_info=True,
        )
        return []


class RouterTableProjector:
    def __init__(
        self,
        table_endpoint: str,
        table_name: str,
        *,
        token_provider: ManagedIdentityTokenProvider | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._endpoint = table_endpoint.rstrip("/")
        self._table = table_name
        self._tokens = token_provider or ManagedIdentityTokenProvider()
        self._timeout = timeout

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        token = self._tokens.token("https://storage.azure.com/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=minimalmetadata",
            "x-ms-version": "2020-12-06",
            "DataServiceVersion": "3.0;",
            "MaxDataServiceVersion": "3.0;NetFx",
        }
        if write:
            headers["Content-Type"] = "application/json"
        return headers

    def _entity_url(self, partition_key: str, row_key: str) -> str:
        pk = quote(partition_key.replace("'", "''"), safe="")
        rk = quote(row_key.replace("'", "''"), safe="")
        return f"{self._endpoint}/{self._table}(PartitionKey='{pk}',RowKey='{rk}')"

    @staticmethod
    def _extract_etag(
        response: httpx.Response, entity: Mapping[str, Any] | None = None
    ) -> str:
        etag = (
            response.headers.get("etag")
            or response.headers.get("ETag")
            or (entity.get("odata.etag") if isinstance(entity, Mapping) else None)
            or "*"
        )
        return str(etag).strip() or "*"

    @staticmethod
    def _parse_entity_documents(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
        docs_by_id: dict[str, dict[str, Any]] = {}
        raw_primary = entity.get("Document")
        if isinstance(raw_primary, str) and raw_primary.strip():
            try:
                parsed = json.loads(raw_primary)
                if isinstance(parsed, dict) and parsed.get("router_id"):
                    docs_by_id[str(parsed["router_id"]).strip().lower()] = parsed
            except Exception:
                pass
        raw_alt = entity.get("AlternateDocuments")
        if isinstance(raw_alt, str) and raw_alt.strip():
            try:
                alt_list = json.loads(raw_alt)
                if isinstance(alt_list, list):
                    for item in alt_list:
                        parsed_item = (
                            json.loads(item)
                            if isinstance(item, str)
                            else item
                            if isinstance(item, dict)
                            else None
                        )
                        if (
                            isinstance(parsed_item, dict)
                            and parsed_item.get("router_id")
                        ):
                            rid = str(parsed_item["router_id"]).strip().lower()
                            docs_by_id.setdefault(rid, parsed_item)
            except Exception:
                pass
        return list(docs_by_id.values())

    def prepare_router(
        self,
        router: ModelRouter,
        registry: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        assigned_user_ids: Sequence[str] = (),
        allowed_subscription_ids: Sequence[str] | None = None,
    ) -> bool:
        document = build_router_table_document(
            router, registry, assigned_user_ids=assigned_user_ids,
            allowed_subscription_ids=allowed_subscription_ids,
        )
        if document is None:
            return False
        serialized = json.dumps(document, separators=(",", ":"), sort_keys=True)
        payload: dict[str, Any] = {
            "PartitionKey": ROUTER_PREPARED_PARTITION,
            "RowKey": router_row_key(str(router.id)), "Status": "prepared",
            "RouterId": str(router.id), "RouterVersion": router.version,
            "Document": serialized, "Sha256": _sha256_hex(serialized),
        }
        with httpx.Client(timeout=self._timeout) as client:
            response = client.put(
                self._entity_url(ROUTER_PREPARED_PARTITION, payload["RowKey"]),
                headers=self._headers(write=True), json=payload,
            )
            response.raise_for_status()
        return True

    def upsert_router(
        self,
        router: ModelRouter,
        registry: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        assigned_user_ids: Sequence[str] = (),
        allowed_subscription_ids: Sequence[str] | None = None,
    ) -> bool:
        doc = build_router_table_document(
            router,
            registry,
            assigned_user_ids=assigned_user_ids,
            allowed_subscription_ids=allowed_subscription_ids,
        )
        if doc is None:
            self.delete_router(router.id, router.name)
            return False
        doc_json = json.dumps(doc, separators=(",", ":"), sort_keys=True)
        doc_sha = _sha256_hex(doc_json)
        name_rk = router_row_key(router.name)
        id_rk = router_row_key(str(router.id))
        target_id = str(router.id).strip().lower()
        with _SYNC_LOCK, httpx.Client(timeout=self._timeout) as client:
            id_payload: dict[str, Any] = {
                "PartitionKey": ROUTER_CATALOG_PARTITION,
                "RowKey": id_rk,
                "SchemaVersion": 2,
                "PrimaryEntry": False,
                "RouterId": str(router.id),
                "RouterName": router.name,
                "RouterVersion": router.version,
                "Status": "published",
                "Document": doc_json,
                "Sha256": doc_sha,
            }
            for attempt in range(3):
                existing: dict[str, tuple[dict[str, Any], str | None]] = {}
                for row_key in dict.fromkeys((id_rk, name_rk)):
                    response = client.get(
                        self._entity_url(ROUTER_CATALOG_PARTITION, row_key),
                        headers=self._headers(),
                    )
                    if response.status_code == 404:
                        existing[row_key] = ({}, None)
                        continue
                    response.raise_for_status()
                    entity = response.json()
                    for current in self._parse_entity_documents(entity):
                        if (str(current.get("router_id", "")).lower() == target_id
                                and int(current["router_version"]) > router.version):
                            return False
                    existing[row_key] = (entity, self._extract_etag(response, entity))
                other_docs: list[dict[str, Any]] = []
                if existing[name_rk][0]:
                    for existing_doc in self._parse_entity_documents(existing[name_rk][0]):
                        existing_id = (
                            str(existing_doc.get("router_id") or "").strip().lower()
                        )
                        if not existing_id or existing_id == target_id:
                            continue
                        other_docs.append(existing_doc)

                name_payload: dict[str, Any] = {
                    "PartitionKey": ROUTER_CATALOG_PARTITION,
                    "RowKey": name_rk,
                    "SchemaVersion": 2,
                    "PrimaryEntry": True,
                    "RouterId": str(router.id),
                    "RouterName": router.name,
                    "RouterVersion": router.version,
                    "Status": "published",
                    "Document": doc_json,
                    "Sha256": doc_sha,
                }
                if other_docs:
                    name_payload["AlternateDocuments"] = json.dumps(
                        other_docs, separators=(",", ":"), sort_keys=True
                    )
                payloads = {id_rk: id_payload, name_rk: name_payload}
                content_type, body = conditional_batch(
                    self._endpoint, self._table, ROUTER_CATALOG_PARTITION,
                    [(row_key, payload, existing[row_key][1])
                     for row_key, payload in payloads.items()],
                )
                response = client.post(
                    self._endpoint + "/$batch",
                    headers={**self._headers(write=True), "Content-Type": content_type},
                    content=body,
                )
                response.raise_for_status()
                try:
                    validate_batch_response(
                        response.status_code, response.headers.get("Content-Type", ""),
                        response.content, len(payloads),
                    )
                except TableBatchConflict:
                    if attempt < 2:
                        continue
                    raise
                return True
        return True

    def delete_router(self, router_id: UUID | str, router_name: str | None = None) -> None:
        target_id = str(router_id).strip().lower()
        id_rk = router_row_key(str(router_id))
        with _SYNC_LOCK, httpx.Client(timeout=self._timeout) as client:
            resp = client.delete(
                self._entity_url(ROUTER_CATALOG_PARTITION, id_rk),
                headers={**self._headers(), "If-Match": "*"},
            )
            if resp.status_code not in {200, 204, 404}:
                resp.raise_for_status()
            if not router_name:
                return
            name_rk = router_row_key(router_name)
            for attempt in range(3):
                get_resp = client.get(
                    self._entity_url(ROUTER_CATALOG_PARTITION, name_rk),
                    headers=self._headers(),
                )
                if get_resp.status_code == 404:
                    return
                if get_resp.status_code != 200:
                    get_resp.raise_for_status()
                entity = get_resp.json()
                etag = self._extract_etag(get_resp, entity)
                existing_docs = self._parse_entity_documents(entity)
                remaining_docs = [
                    d
                    for d in existing_docs
                    if str(d.get("router_id") or "").strip().lower() != target_id
                ]
                entity_router_id = str(entity.get("RouterId") or "").strip().lower()
                if remaining_docs:
                    if (
                        len(remaining_docs) != len(existing_docs)
                        or entity_router_id == target_id
                    ):
                        primary_doc = remaining_docs[0]
                        alt_docs = remaining_docs[1:]
                        primary_json = json.dumps(
                            primary_doc, separators=(",", ":"), sort_keys=True
                        )
                        replacement: dict[str, Any] = {
                            "PartitionKey": ROUTER_CATALOG_PARTITION,
                            "RowKey": name_rk,
                            "SchemaVersion": 2,
                            "PrimaryEntry": True,
                            "RouterId": str(primary_doc["router_id"]),
                            "RouterName": str(
                                primary_doc.get("router_name") or router_name
                            ),
                            "RouterVersion": int(
                                primary_doc.get("router_version") or 1
                            ),
                            "Status": "published",
                            "Document": primary_json,
                            "Sha256": _sha256_hex(primary_json),
                        }
                        if alt_docs:
                            replacement["AlternateDocuments"] = json.dumps(
                                alt_docs, separators=(",", ":"), sort_keys=True
                            )
                        put_resp = client.put(
                            self._entity_url(ROUTER_CATALOG_PARTITION, name_rk),
                            headers={**self._headers(write=True), "If-Match": etag},
                            json=replacement,
                        )
                        if put_resp.status_code in {409, 412} and attempt < 2:
                            continue
                        put_resp.raise_for_status()
                    return
                if entity_router_id and entity_router_id != target_id:
                    return
                del_resp = client.delete(
                    self._entity_url(ROUTER_CATALOG_PARTITION, name_rk),
                    headers={**self._headers(), "If-Match": etag},
                )
                if del_resp.status_code in {409, 412} and attempt < 2:
                    continue
                if del_resp.status_code not in {200, 204, 404}:
                    del_resp.raise_for_status()
                return

    def sync_all_published_routers(
        self, repository: Any, *, gateway_profile_id: UUID | None = None,
    ) -> int:
        registry = repository.registry()
        published_rows = list(repository.list_published_model_routers())
        projected = 0
        list_assignees = getattr(repository, "list_model_router_assignees", None)
        for row in published_rows:
            router = ModelRouter.model_validate(row)
            assignees = (
                list(list_assignees(router.id))
                if callable(list_assignees)
                else []
            )
            allowed_subs = _resolve_allowed_subscription_ids_for_router(
                router, assignees, repository
            )
            if gateway_profile_id is not None:
                document = build_router_table_document(router, registry)
                if (document is None
                    or document.get("gateway_profile_id") != str(gateway_profile_id)):
                    continue

            def project(
                current: dict[str, Any],
                assigned: Sequence[str] = assignees,
                subscriptions: Sequence[str] = allowed_subs,
            ) -> bool:
                return self.upsert_router(
                    ModelRouter.model_validate(current), registry,
                    assigned_user_ids=assigned, allowed_subscription_ids=subscriptions,
                )

            if repository.project_published_model_router(
                router.id, router.version, project,
            ):
                projected += 1
        return projected

    def _sync_entity_doc(
        self,
        rk: str,
        raw_doc: str,
        repository: Any,
        *,
        default_org: Any,
        default_dept: Any,
        default_proj: Any,
        default_agent: Any,
        default_provider: str,
        default_runtime: str,
    ) -> bool:
        try:
            doc = json.loads(raw_doc)
            req_id = str(doc["request_id"])
            router_id = UUID(str(doc["router_id"]))
            router_name = str(doc["router_name"])
            router_version = int(doc["router_version"])
            selected_model_id = (
                UUID(str(doc["selected_model_id"]))
                if doc.get("selected_model_id")
                else None
            )
            selected_model_name = (
                str(doc["selected_model_name"])
                if doc.get("selected_model_name")
                else None
            )
            raw_attempts = list(doc.get("attempts") or [])
            attempts: list[dict[str, Any]] = []
            for idx, raw_att in enumerate(raw_attempts, start=1):
                if not isinstance(raw_att, Mapping):
                    continue
                att = dict(raw_att)
                att.setdefault("sequence", int(att.get("attempt_number") or idx))
                att.setdefault("attempt_number", int(att["sequence"]))
                role = str(att.get("attempt_role") or "candidate")
                if role == "candidate_invocation":
                    role = "candidate"
                att["attempt_role"] = role
                outcome = str(att.get("outcome") or "succeeded")
                if outcome == "skipped":
                    outcome = "cost_limit_exceeded"
                att["outcome"] = outcome
                if att.get("decision_basis") == "policy_filtered":
                    att["decision_basis"] = "cost_limit_exceeded"
                probs = att.get("decision_probabilities")
                if isinstance(probs, list):
                    att["decision_probabilities"] = {
                        str(p["model_id"]): float(p["probability"])
                        for p in probs
                        if isinstance(p, Mapping)
                        and p.get("model_id") is not None
                        and p.get("probability") is not None
                    }
                attempts.append(att)
            repository.save_model_router_invocation(
                req_id,
                router_id=router_id,
                router_name=router_name,
                router_version=router_version,
                selected_model_id=selected_model_id,
                selected_model_name=selected_model_name,
                attempts=attempts,
            )
            for usage_item in (() if doc.get("native_usage_recorded") is True
                               else doc.get("usage_records") or []):
                if str(usage_item.get("request_id") or "") == req_id:
                    continue
                ts_val = (
                    datetime.fromisoformat(str(usage_item["ts"]).replace("Z", "+00:00"))
                    if usage_item.get("ts")
                    else datetime.now(UTC)
                )
                repository.write_token_usage(
                    TokenUsageRecord(
                        id=str(usage_item.get("id") or usage_item["request_id"]),
                        request_id=str(usage_item["request_id"]),
                        correlation_id=str(
                            usage_item.get("correlation_id") or usage_item["request_id"]
                        ),
                        ts=ts_val,
                        team=str(usage_item.get("team") or default_dept.name),
                        organization=str(
                            usage_item.get("organization") or default_org.name
                        ),
                        organization_id=str(
                            usage_item.get("organization_id") or default_org.id
                        ),
                        department=str(
                            usage_item.get("department") or default_dept.name
                        ),
                        department_id=str(
                            usage_item.get("department_id") or default_dept.id
                        ),
                        project=str(usage_item.get("project") or default_proj.name),
                        project_id=str(
                            usage_item.get("project_id") or default_proj.id
                        ),
                        user=str(usage_item.get("user") or "unattributed"),
                        user_id=str(usage_item.get("user_id") or "unattributed"),
                        agent=str(
                            usage_item.get("agent") or default_agent.name
                        ),
                        agent_id=str(
                            usage_item.get("agent_id") or default_agent.id
                        ),
                        workflow=str(
                            usage_item.get("workflow") or f"{default_proj.id}-interactive"
                        ),
                        run_id=str(
                            usage_item.get("run_id") or usage_item["request_id"]
                        ),
                        turn_index=int(usage_item.get("turn_index") or 1),
                        provider=str(
                            usage_item.get("provider") or default_provider
                        ),
                        model=str(usage_item["model"]),
                        model_id=str(usage_item["model_id"]),
                        runtime=str(
                            usage_item.get("runtime") or default_runtime
                        ),
                        request_source=str(
                            usage_item.get("request_source") or "apim-gateway"
                        ),
                        usage_domain="apim",
                        input_tokens=int(usage_item.get("input_tokens") or 0),
                        cached_tokens=int(usage_item.get("cached_tokens") or 0),
                        cache_write_tokens=int(
                            usage_item.get("cache_write_tokens") or 0
                        ),
                        output_tokens=int(usage_item.get("output_tokens") or 0),
                        et=float(usage_item.get("et") or 0.0001),
                        et_coeff_m=1.0,
                        latency_ms=int(usage_item.get("latency_ms") or 0),
                        status=str(usage_item.get("status") or "200"),
                        status_code=int(usage_item.get("status_code") or 200),
                        estimated_cost=float(
                            usage_item.get("estimated_cost") or 0.0
                        ),
                        usd_cost=usage_item.get("estimated_cost"),
                        input_price_per_million=usage_item.get(
                            "input_price_per_million"
                        ),
                        cached_price_per_million=usage_item.get(
                            "cached_price_per_million"
                        ),
                        cache_write_price_per_million=usage_item.get(
                            "cache_write_price_per_million"
                        ),
                        output_price_per_million=usage_item.get(
                            "output_price_per_million"
                        ),
                        price_context_tier=(
                            str(usage_item["price_context_tier"])
                            if usage_item.get("price_context_tier") is not None
                            else None
                        ),
                        estimated=False,
                        ingest_source="gateway",
                    )
                )
            _SYNCED_TRACE_IDS.add(rk)
            return True
        except Exception:
            logger.debug("Skipping malformed router trace entity %s", rk, exc_info=True)
            return False

    def _delete_trace_entity(self, client: httpx.Client, rk: str) -> None:
        try:
            client.delete(
                self._entity_url(ROUTER_TRACE_PARTITION, rk),
                headers={**self._headers(), "If-Match": "*"},
            )
        except Exception:
            logger.debug("Could not delete synced router trace entity %s", rk, exc_info=True)

    def sync_single_trace_to_repository(self, request_id: str, repository: Any) -> bool:
        rk = request_id.strip()
        if not rk:
            return False
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(
                self._entity_url(ROUTER_TRACE_PARTITION, rk),
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return False
            entity = resp.json()
            raw_doc = entity.get("Document")
            if not isinstance(raw_doc, str) or not raw_doc:
                return False
            catalog = enterprise_catalog()
            registry = repository.registry()
            ok = self._sync_entity_doc(
                rk,
                raw_doc,
                repository,
                default_org=catalog.organizations[0],
                default_dept=catalog.departments[0],
                default_proj=catalog.projects[0],
                default_agent=catalog.agents[0],
                default_provider=(
                    str(registry["providers"][0]["name"])
                    if registry.get("providers")
                    else "unattributed"
                ),
                default_runtime=(
                    str(registry["runtimes"][0]["name"])
                    if registry.get("runtimes")
                    else "unattributed"
                ),
            )
            if ok:
                self._delete_trace_entity(client, rk)
            return ok

    def sync_traces_to_repository(self, repository: Any) -> int:
        synced = 0
        entities: list[dict[str, Any]] = []
        filter_q = quote(f"PartitionKey eq '{ROUTER_TRACE_PARTITION}'")
        next_partition: str | None = None
        next_row: str | None = None
        with httpx.Client(timeout=self._timeout) as client:
            for _ in range(10):
                url = f"{self._endpoint}/{self._table}()?$filter={filter_q}&$top=200"
                if next_partition:
                    url += "&NextPartitionKey=" + quote(next_partition, safe="")
                if next_row:
                    url += "&NextRowKey=" + quote(next_row, safe="")
                resp = client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    break
                page_items = resp.json().get("value", [])
                if isinstance(page_items, list):
                    entities.extend(page_items)
                next_partition = (
                    resp.headers.get("x-ms-continuation-NextPartitionKey")
                    or resp.headers.get("x-ms-continuation-nextpartitionkey")
                )
                next_row = (
                    resp.headers.get("x-ms-continuation-NextRowKey")
                    or resp.headers.get("x-ms-continuation-nextrowkey")
                )
                if not next_row and not next_partition:
                    break

            if not entities:
                return 0

            catalog = enterprise_catalog()
            default_org = catalog.organizations[0]
            default_dept = catalog.departments[0]
            default_proj = catalog.projects[0]
            default_agent = catalog.agents[0]
            registry = repository.registry()
            default_provider = (
                str(registry["providers"][0]["name"])
                if registry.get("providers")
                else "unattributed"
            )
            default_runtime = (
                str(registry["runtimes"][0]["name"])
                if registry.get("runtimes")
                else "unattributed"
            )

            for entity in entities:
                rk = str(entity.get("RowKey") or "")
                if not rk:
                    continue
                if rk in _SYNCED_TRACE_IDS:
                    self._delete_trace_entity(client, rk)
                    continue
                raw_doc = entity.get("Document")
                if not isinstance(raw_doc, str) or not raw_doc:
                    continue
                if self._sync_entity_doc(
                    rk,
                    raw_doc,
                    repository,
                    default_org=default_org,
                    default_dept=default_dept,
                    default_proj=default_proj,
                    default_agent=default_agent,
                    default_provider=default_provider,
                    default_runtime=default_runtime,
                ):
                    synced += 1
                    self._delete_trace_entity(client, rk)
        return synced


def project_router_to_table_if_configured(
    router: ModelRouter | None,
    repository: Any,
    *,
    deleted_id: UUID | None = None,
    deleted_name: str | None = None,
    required: bool = False,
    use_published_snapshot: bool = True,
    prepare_only: bool = False,
) -> None:
    settings = get_settings()
    if not settings.ledger_table_endpoint or not settings.ledger_table_name:
        if required:
            raise RuntimeError("Native Router projection storage is unavailable")
        return
    try:
        projector = RouterTableProjector(
            settings.ledger_table_endpoint,
            settings.ledger_table_name,
        )
        if deleted_id is not None:
            projector.delete_router(deleted_id, deleted_name)
        elif router is not None:
            if router.status == "published":
                published_row = (repository.get_published_model_router(router.id)
                                 if use_published_snapshot else None)
                effective = (
                    ModelRouter.model_validate(published_row)
                    if published_row is not None
                    else router
                )
                list_assignees = getattr(
                    repository, "list_model_router_assignees", None
                )
                assignees = (
                    list(list_assignees(effective.id))
                    if callable(list_assignees)
                    else []
                )
                allowed_subs = _resolve_allowed_subscription_ids_for_router(
                    effective, assignees, repository
                )
                registry = repository.registry()
                if prepare_only:
                    projected = projector.prepare_router(
                        effective, registry, assigned_user_ids=assignees,
                        allowed_subscription_ids=allowed_subs,
                    )
                else:
                    projected = repository.project_published_model_router(
                        effective.id, effective.version, lambda current: projector.upsert_router(
                            ModelRouter.model_validate(current), registry,
                            assigned_user_ids=assignees, allowed_subscription_ids=allowed_subs,
                        ),
                    )
                if required and not projected:
                    raise RuntimeError("Native Router projection is incomplete")
            else:
                projector.delete_router(router.id, router.name)
    except Exception:
        if required:
            raise
        logger.warning("Router Table Storage projection deferred", exc_info=True)


def sync_apim_router_traces_if_configured(
    repository: Any,
    *,
    force: bool = False,
    request_id: str | None = None,
) -> None:
    global _LAST_TRACE_SYNC_MONO
    settings = get_settings()
    if not settings.ledger_table_endpoint or not settings.ledger_table_name:
        return
    if request_id and request_id.strip():
        try:
            RouterTableProjector(
                settings.ledger_table_endpoint,
                settings.ledger_table_name,
            ).sync_single_trace_to_repository(request_id.strip(), repository)
        except Exception:
            logger.debug("Single APIM router trace sync skipped", exc_info=True)
    now = time.monotonic()
    if not force and now - _LAST_TRACE_SYNC_MONO < 1.0:
        return
    with _SYNC_LOCK:
        if not force and time.monotonic() - _LAST_TRACE_SYNC_MONO < 1.0:
            return
        _LAST_TRACE_SYNC_MONO = time.monotonic()
        try:
            RouterTableProjector(
                settings.ledger_table_endpoint,
                settings.ledger_table_name,
            ).sync_traces_to_repository(repository)
        except Exception:
            logger.debug("APIM router trace sync skipped", exc_info=True)
