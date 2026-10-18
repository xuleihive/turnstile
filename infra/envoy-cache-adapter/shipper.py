from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import UUID

LOG_PATH = Path("/tmp/turnstile-envoy/usage.jsonl")
EVENT_HUB_RESOURCE = "https://eventhubs.azure.net/"


def _text(row: dict[str, Any], key: str, default: str = "unattributed") -> str:
    value = row.get(key)
    return str(value) if value not in (None, "", "-") else default


def _number(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return int(float(value)) if value not in (None, "", "-") else 0


def _exact_counter(row: dict[str, Any], key: str, *, optional: bool = False) -> int:
    value = row.get(key)
    if optional and value in (None, "", "-"):
        return 0
    if value is None or type(value) not in {int, float, str}:
        raise ValueError("Usage counter is not numeric")
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("Usage counter is not an integer")
        value = int(value)
    if not 0 <= value <= 2**53 - 1 or int(value) != value:
        raise ValueError("Usage counter exceeds exact metadata precision")
    return int(value)


def _execution_usage(row: dict[str, Any]) -> dict[str, Any]:
    from turnstile_core.domain.budget_completion import (
        exact_integer,
        exact_usd_amount,
        normalized_identity,
        usd_completion_charge,
        validate_completion,
    )

    encoded = row.get("budget_execution")
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= 16384:
        raise ValueError("Execution receipt header is invalid")
    receipts = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= 2:
        raise ValueError("Execution requires bounded admission receipts")
    seen: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ValueError("Execution receipt must be an object")
        identity = normalized_identity(receipt["identity"])
        UUID(receipt["epoch"])
        if exact_integer(receipt["reserved_tokens"]) == 0:
            raise ValueError("Execution receipt must reserve tokens")
        # The Observer ships only stdlib protocol modules, not the API's Pydantic models.
        fields = {"identity", "epoch", "reserved_tokens", "budget_mode"}
        mode = receipt.get("budget_mode", "tokens")
        if mode == "usd":
            fields.update({"reserved_usd", "price_snapshot"})
            exact_usd_amount(receipt["reserved_usd"])
            projection = receipt["price_snapshot"]
            if (not isinstance(projection, dict)
                    or projection.get("model_id") != identity["model_id"]):
                raise ValueError("USD receipt requires its admitted model price")
            usd_completion_charge(projection, {
                "input_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                "output_tokens": 0,
            })
        elif mode != "tokens":
            raise ValueError("Execution receipt budget mode is invalid")
        if set(receipt) - fields:
            raise ValueError("Execution receipt fields do not match the protocol")
        receipt["identity"] = identity
        scope = identity["scope"]
        if (scope["scope_type"] in seen or any(identity[field] != row.get(field) for field in (
                "gateway_profile_id", "native_gateway_id", "correlation_id", "model_id",
        )) or (scope["scope_type"] == "person" and scope["scope_id"] != row.get("user_id"))):
            raise ValueError("Execution receipt differs from trusted request metadata")
        seen.add(scope["scope_type"])
    gateway_profile_id = UUID(row["gateway_profile_id"])
    context_fields = {
        name: _text(row, name, "") for name in (
            "native_gateway_id", "native_gateway_instance_id", "apim_service_id",
            "apim_region", "api_revision",
        )
    }
    if not all(context_fields.values()):
        raise ValueError("Execution usage requires complete Gateway context")
    managed = _text(row, "gateway_is_managed", "").casefold()
    if managed not in {"true", "false"}:
        raise ValueError("Execution usage requires trusted Gateway management state")
    identity_parts = (
        str(gateway_profile_id), context_fields["apim_service_id"].strip().casefold(),
        context_fields["native_gateway_id"].strip().casefold(),
        _text(row, "correlation_id", "").strip().casefold(),
    )
    if not all(identity_parts):
        raise ValueError("Execution usage requires complete canonical Gateway identity")
    canonical = ".".join(base64.b64encode(value.encode()).decode() for value in identity_parts)
    usage_id = "turnstile-v2-" + hashlib.sha256(canonical.encode()).hexdigest()
    if row.get("usage_id") != usage_id:
        raise ValueError("Execution usage identity does not match its Gateway context")
    result: dict[str, Any] = {
        "event_schema_version": 2, "id": usage_id, "usage_id": usage_id,
        **context_fields, "gateway_is_managed": managed == "true",
        "budget_execution": receipts, "usage_complete": False, "usage_event_source": "observer",
        "gateway_profile_id": str(gateway_profile_id),
        "apim_subscription_id": _text(row, "apim_subscription_id", "") or None,
        "application_actor_type": _text(row, "application_actor_type", "") or None,
        "application_actor_id": _text(row, "application_actor_id", "") or None,
        "application_admission": "ok", "budget_admission": "ok",
        "input_tokens": None, "cached_tokens": None, "cache_write_tokens": None,
        "output_tokens": None, "tokens_consumed": None, "estimated": True,
        "ingest_error": "budget_usage_incomplete", "runtime_authoritative": False,
    }
    try:
        if (not 200 <= _exact_counter(row, "response_code") < 300
                or row.get("response_flags") not in {"", "-"}):
            return result
        api_format = _text(row, "api_format")
        streamed = "text/event-stream" in _text(row, "response_content_type", "").lower()
        if api_format == "anthropic_messages":
            terminal = (row.get("last_event_type") == "message_stop" if streamed else
                        row.get("response_type") == "message"
                        and row.get("stop_reason") not in (None, "", "-"))
        elif api_format == "openai_responses":
            terminal = row.get("response_status") == "completed" and (
                not streamed or row.get("last_event_type") == "response.completed"
            )
        else:
            terminal = api_format == "openai_chat"
        if not terminal:
            return result
        prompt = _exact_counter(row, "prompt_tokens")
        output = _exact_counter(row, "output_tokens")
        cache_key = ("cache_read_tokens" if row.get("cache_read_tokens") not in (None, "", "-")
                     else "cache_read_tokens_fallback")
        read = _exact_counter(row, cache_key, optional=api_format != "anthropic_messages")
        write = _exact_counter(
            row, "cache_write_tokens", optional=api_format != "anthropic_messages",
        )
        advertised_total = None
        if api_format != "anthropic_messages":
            advertised_total = _exact_counter(row, "total_tokens")
            if api_format == "openai_chat" and advertised_total != prompt + output:
                # Some Chat APIs exclude reasoning from completion_tokens but include it
                # in total_tokens. Never add it twice or infer an unexplained difference.
                reasoning = _exact_counter(row, "reasoning_tokens", optional=True)
                if advertised_total == prompt + output + reasoning:
                    output += reasoning
        cached = read + write
        input_tokens = prompt if api_format == "anthropic_messages" else prompt - cached
        total = input_tokens + cached + output
        if advertised_total is not None and advertised_total != total:
            return result
        evidence = validate_completion({
            "source": "observer", "observed_at": row["timestamp"],
            "input_tokens": input_tokens, "cached_tokens": cached, "cache_write_tokens": write,
            "output_tokens": output, "total_tokens": total,
        })
        result.update(
            input_tokens=evidence["input_tokens"], cached_tokens=evidence["cached_tokens"],
            cache_write_tokens=evidence["cache_write_tokens"],
            output_tokens=evidence["output_tokens"],
            tokens_consumed=total, estimated=False, ingest_error=None, usage_complete=True,
            runtime_authoritative=_text(row, "pool_runtime", "") not in {"", "unattributed"},
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return result


def normalize_access_row(row: dict[str, Any]) -> dict[str, Any] | None:
    request_id = _text(row, "request_id", "")
    execution = (_execution_usage(row)
                 if row.get("budget_execution") not in (None, "", "-") else None)
    if not request_id or (execution is None and _number(row, "response_code") >= 400):
        return None
    counts = row if execution is None else {}
    prompt_tokens = _number(counts, "prompt_tokens")
    output_tokens = _number(counts, "output_tokens")
    cache_read_tokens = _number(
        counts,
        "cache_read_tokens"
        if row.get("cache_read_tokens") not in (None, "", "-")
        else "cache_read_tokens_fallback",
    )
    cache_write_tokens = _number(counts, "cache_write_tokens")
    cached_tokens = cache_read_tokens + cache_write_tokens
    if execution is None and prompt_tokens + output_tokens + cached_tokens == 0:
        return None
    api_format = _text(row, "api_format", "openai_chat")
    input_tokens = (
        prompt_tokens
        if api_format == "anthropic_messages"
        else max(prompt_tokens - cached_tokens, 0)
    )
    pool_runtime = _text(row, "pool_runtime", "")
    if pool_runtime == "unattributed":
        pool_runtime = ""
    return {
        "id": request_id,
        "request_id": request_id,
        "correlation_id": _text(row, "correlation_id", request_id),
        "ts": _text(row, "timestamp"),
        "team": _text(row, "department"),
        "organization": _text(row, "organization"),
        "organization_id": _text(row, "organization_id"),
        "department": _text(row, "department"),
        "department_id": _text(row, "department_id"),
        "project": _text(row, "project"),
        "project_id": _text(row, "project_id"),
        "user": _text(row, "user"),
        "user_id": _text(row, "user_id"),
        "agent": _text(row, "agent"),
        "agent_id": _text(row, "agent_id"),
        "workflow": _text(row, "workflow"),
        "run_id": _text(row, "run_id", request_id),
        "turn_index": max(_number(row, "turn_index"), 1),
        "provider": _text(row, "provider", "microsoft_foundry"),
        "model": _text(row, "model"),
        "model_id": _text(row, "model_id"),
        "runtime": pool_runtime or _text(row, "runtime"),
        "runtime_authoritative": bool(pool_runtime),
        "request_source": _text(row, "request_source"),
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "latency_ms": _number(row, "latency_ms"),
        "status": str(_number(row, "response_code")),
        "status_code": _number(row, "response_code"),
        "estimated_cost": None,
        "error_message": None,
        "estimated": False,
        "ingest_source": "eventhub",
        "ingest_error": None,
        "budget_admission": None,
        "model_admission": None,
        **(execution or {}),
    }


class ManagedIdentityToken:
    def __init__(self, resource: str = EVENT_HUB_RESOURCE) -> None:
        self._token = ""
        self._expires_on = 0
        self._resource = resource

    def get(self) -> str:
        now = int(time.time())
        if self._token and self._expires_on - now > 300:
            return self._token
        endpoint = os.environ["IDENTITY_ENDPOINT"]
        separator = "&" if "?" in endpoint else "?"
        url = endpoint + separator + urlencode(
            {"resource": self._resource, "api-version": "2019-08-01"}
        )
        request = Request(
            url,
            headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]},
        )
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        self._token = str(payload["access_token"])
        self._expires_on = int(payload["expires_on"])
        return self._token


class EventHubSender:
    def __init__(self) -> None:
        namespace = os.environ["EVENT_HUB_FQDN"].strip()
        event_hub = quote(os.environ["EVENT_HUB_NAME"].strip(), safe="")
        self._url = f"https://{namespace}/{event_hub}/messages"
        self._tokens = ManagedIdentityToken()
        self._completion: Any = None
        if os.environ.get("BUDGET_COMPLETION_ENABLED", "false").lower() == "true":
            from turnstile_core.integrations.budget_completion import BudgetCompletionClient

            tokens = ManagedIdentityToken("https://storage.azure.com/")
            self._completion = BudgetCompletionClient(
                os.environ["LEDGER_TABLE_ENDPOINT"], os.environ["LEDGER_TABLE_NAME"], tokens.get,
            )

    def send(self, event: dict[str, Any]) -> None:
        if (self._completion is not None and event.get("budget_execution")
                and event.get("usage_complete") is True):
            try:
                self._completion.complete_event(event)
            except Exception as error:
                print(f"budget completion deferred ({type(error).__name__})", flush=True)
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._tokens.get()}",
                "Content-Type": "application/json; charset=utf-8",
                "BrokerProperties": json.dumps({"PartitionKey": event["id"]}),
            },
        )
        with urlopen(request, timeout=10) as response:
            if response.status not in (200, 201, 202):
                raise RuntimeError(f"Event Hub returned HTTP {response.status}")


def run() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(exist_ok=True)
    sender = EventHubSender()
    with LOG_PATH.open("r", encoding="utf-8") as source:
        while True:
            position = source.tell()
            line = source.readline()
            if not line:
                time.sleep(0.05)
                continue
            if not line.endswith("\n"):
                source.seek(position)
                time.sleep(0.05)
                continue
            try:
                row = json.loads(line)
                event = normalize_access_row(row)
                if event is None:
                    continue
                while True:
                    try:
                        sender.send(event)
                        print(f"sent exact usage for {event['id']}", flush=True)
                        break
                    except Exception as error:  # noqa: BLE001
                        print(
                            f"usage send failed ({type(error).__name__}); retrying",
                            flush=True,
                        )
                        time.sleep(1)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                print(f"skipped malformed access row ({type(error).__name__})", flush=True)


if __name__ == "__main__":
    run()
