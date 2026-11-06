from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import ceil, floor
from typing import Any

SCOPE_COLUMNS = {
    "organization": "organization_id",
    "department": "department_id",
    "project": "project_id",
    "agent": "agent_id",
    "model": "model_id",
    "user": "user_id",
}


def _value(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name)


def _timestamp(record: Any) -> datetime:
    value = _value(record, "ts")
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _percentile(values: Sequence[float], percentage: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    position = (len(ordered) - 1) * percentage / 100
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _scoped_records(records: Sequence[Any], rule: Mapping[str, Any]) -> list[Any]:
    if rule["scope_type"] == "global":
        return list(records)
    column = SCOPE_COLUMNS[str(rule["scope_type"])]
    return [record for record in records if _value(record, column) == rule["scope_id"]]


def _signal(
    rule: Mapping[str, Any],
    suffix: str,
    detected_at: datetime,
    dimension: str,
    dimension_id: str,
    dimension_name: str,
    actual: float,
    threshold: float,
    description: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    rule_id = str(rule["id"])
    return {
        "id": f"{rule_id}:{suffix}",
        "detected_at": detected_at,
        "rule_id": rule_id,
        "severity": rule["severity"],
        "title": rule["name"],
        "description": rule["description"] or description,
        "dimension": dimension,
        "dimension_id": dimension_id,
        "dimension_name": dimension_name,
        "request_id": request_id,
        "actual_value": round(actual, 8),
        "threshold_value": round(threshold, 8),
    }


def evaluate_anomaly_rules(
    records: Sequence[Any],
    rules: Sequence[Mapping[str, Any]],
    detected_at: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    for rule in rules:
        if not rule["enabled"]:
            continue
        scoped = _scoped_records(records, rule)
        metric = rule["metric"]
        minimum = int(rule["minimum_sample_size"])
        configured_threshold = float(rule["threshold_value"])

        if metric == "error_rate_percent":
            if len(scoped) < minimum:
                continue
            failed = sum(not 100 <= int(_value(record, "status_code")) < 400 for record in scoped)
            actual = 100 * failed / len(scoped)
            if actual >= configured_threshold:
                anomalies.append(
                    _signal(
                        rule,
                        "scope",
                        detected_at,
                        "scope",
                        str(rule["scope_id"] or "current-filter"),
                        "当前筛选范围",
                        actual,
                        configured_threshold,
                        f"{failed} / {len(scoped)} 次调用失败。",
                    )
                )
            continue

        if metric in {"request_latency_ms", "request_cost_usd"}:
            value_field = "latency_ms" if metric == "request_latency_ms" else "estimated_cost"
            candidates = [
                (record, float(_value(record, value_field)))
                for record in scoped
                if metric != "request_cost_usd" or float(_value(record, value_field)) > 0
            ]
            if len(candidates) < minimum:
                continue
            threshold = configured_threshold
            if rule["threshold_mode"] == "percentile":
                threshold = _percentile(
                    [value for _, value in candidates], configured_threshold
                )
            for record, actual in candidates:
                if actual < threshold:
                    continue
                request_id = str(_value(record, "request_id"))
                model_name = str(_value(record, "model"))
                description = (
                    "单次请求延迟达到规则阈值。"
                    if metric == "request_latency_ms"
                    else "单次请求成本达到规则阈值。"
                )
                anomalies.append(
                    _signal(
                        rule,
                        request_id,
                        _timestamp(record),
                        "request",
                        request_id,
                        model_name,
                        actual,
                        threshold,
                        description,
                        request_id,
                    )
                )
            continue

        agent_counts: dict[tuple[str, str], int] = {}
        for record in scoped:
            key = (str(_value(record, "agent_id")), str(_value(record, "agent")))
            agent_counts[key] = agent_counts.get(key, 0) + 1
        if len(agent_counts) < minimum:
            continue
        threshold = configured_threshold
        if rule["threshold_mode"] == "percentile":
            threshold = _percentile(
                [float(value) for value in agent_counts.values()], configured_threshold
            )
        for (agent_id, agent_name), count in agent_counts.items():
            if count < threshold:
                continue
            anomalies.append(
                _signal(
                    rule,
                    agent_id,
                    detected_at,
                    "agent",
                    agent_id,
                    agent_name,
                    float(count),
                    threshold,
                    "Agent 调用次数达到规则阈值。",
                )
            )

    return sorted(
        anomalies,
        key=lambda item: (item["detected_at"], item["id"]),
        reverse=True,
    )[:limit]