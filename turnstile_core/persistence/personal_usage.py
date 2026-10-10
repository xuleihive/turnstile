from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..domain.models import TokenUsageRecord
from .repository_support import UsageFilters

_PRICED = "(input_price_per_million IS NOT NULL AND output_price_per_million IS NOT NULL)"
_TOTALS_SQL = f"""
    count(*) AS requests,
    COALESCE(sum(input_tokens), 0) AS input_tokens,
    COALESCE(sum(output_tokens), 0) AS output_tokens,
    COALESCE(sum(cached_tokens - cache_write_tokens), 0) AS cache_read_tokens,
    COALESCE(sum(cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(sum(input_tokens + cached_tokens + output_tokens), 0) AS total_tokens,
    100.0 * count(*) FILTER (WHERE status_code >= 400) / NULLIF(count(*), 0) AS error_rate,
    avg(latency_ms) AS average_latency_ms,
    count(*) FILTER (WHERE {_PRICED}) AS priced_count,
    COALESCE(sum(estimated_cost) FILTER (WHERE {_PRICED}), 0) AS estimated_cost_usd
"""


def _totals(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    priced = int(result.pop("priced_count"))
    for key in (
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    ):
        result[key] = int(result[key])
    for key in ("error_rate", "average_latency_ms", "estimated_cost_usd"):
        result[key] = float(result[key]) if result[key] is not None else None
    result["cost_state"] = (
        "priced"
        if result["requests"] and priced == result["requests"]
        else "partial"
        if priced
        else "unpriced"
    )
    if not priced:
        result["estimated_cost_usd"] = None
    return result


class PostgreSqlPersonalUsageRepositoryMixin:
    def _connection(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def personal_usage(
        self, user_id: str, from_: datetime, to: datetime, interval: str, timezone: str
    ) -> dict[str, Any]:
        where = "WHERE user_id = %s AND usage_domain = 'apim' AND ts >= %s AND ts < %s"
        parameters = (user_id, from_, to)
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT {_TOTALS_SQL}, max(ts) AS last_observed_at FROM token_usage {where}",
                parameters,
            ).fetchone()
            points = connection.execute(
                f"""SELECT date_trunc(%s, ts AT TIME ZONE %s) AT TIME ZONE %s AS bucket_start,
                    {_TOTALS_SQL} FROM token_usage {where} GROUP BY 1 ORDER BY 1""",
                (interval, timezone, timezone, *parameters),
            ).fetchall()
            models = connection.execute(
                f"""SELECT model_id, max(model) AS model_name, {_TOTALS_SQL}
                    FROM token_usage {where} GROUP BY model_id
                    ORDER BY total_tokens DESC, model_id""",
                parameters,
            ).fetchall()
        last_observed_at = total.pop("last_observed_at")
        return {
            "totals": _totals(total),
            "points": [
                {"bucket_start": row.pop("bucket_start"), "totals": _totals(row)} for row in points
            ],
            "models": [
                {
                    "model_id": row.pop("model_id"),
                    "model_name": row.pop("model_name"),
                    "totals": _totals(row),
                }
                for row in models
            ],
            "last_observed_at": last_observed_at,
        }


class InMemoryPersonalUsageRepositoryMixin:
    _usage_between: Callable[[datetime, datetime, UsageFilters], Sequence[TokenUsageRecord]]

    @staticmethod
    def _personal_totals(records: Sequence[TokenUsageRecord]) -> dict[str, Any]:
        priced = [
            record
            for record in records
            if record.input_price_per_million is not None
            and record.output_price_per_million is not None
        ]
        return _totals(
            {
                "requests": len(records),
                "input_tokens": sum(row.input_tokens for row in records),
                "output_tokens": sum(row.output_tokens for row in records),
                "cache_read_tokens": sum(
                    row.cached_tokens - row.cache_write_tokens for row in records
                ),
                "cache_write_tokens": sum(row.cache_write_tokens for row in records),
                "total_tokens": sum(
                    row.input_tokens + row.cached_tokens + row.output_tokens for row in records
                ),
                "error_rate": (
                    100 * sum(row.status_code >= 400 for row in records) / len(records)
                    if records
                    else None
                ),
                "average_latency_ms": (
                    sum(row.latency_ms for row in records) / len(records) if records else None
                ),
                "estimated_cost_usd": sum(row.estimated_cost or 0 for row in priced),
                "priced_count": len(priced),
            }
        )

    def personal_usage(
        self, user_id: str, from_: datetime, to: datetime, interval: str, timezone: str
    ) -> dict[str, Any]:
        records = self._usage_between(from_, to, UsageFilters(user_id=user_id))
        buckets: dict[datetime, list[TokenUsageRecord]] = {}
        models: dict[str, list[TokenUsageRecord]] = {}
        zone = ZoneInfo(timezone)
        for record in records:
            local = record.ts.astimezone(zone)
            bucket = local.replace(minute=0, second=0, microsecond=0)
            if interval == "day":
                bucket = bucket.replace(hour=0)
            buckets.setdefault(bucket.astimezone(UTC), []).append(record)
            models.setdefault(record.model_id, []).append(record)
        return {
            "totals": self._personal_totals(records),
            "points": [
                {"bucket_start": bucket, "totals": self._personal_totals(rows)}
                for bucket, rows in sorted(buckets.items())
            ],
            "models": sorted(
                [
                    {
                        "model_id": key,
                        "model_name": rows[-1].model,
                        "totals": self._personal_totals(rows),
                    }
                    for key, rows in models.items()
                ],
                key=lambda row: (-row["totals"]["total_tokens"], row["model_id"]),
            ),
            "last_observed_at": max((row.ts for row in records), default=None),
        }
