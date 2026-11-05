from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from backend.services.assistant_tools import (
    AssistantTools,
    UsageRankingArguments,
    UsageSummaryArguments,
)
from turnstile_core.domain.models import TokenUsageRecord
from turnstile_core.persistence.in_memory import InMemoryRepository
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy, UsageFilters

START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _usage(entry_id: str, **overrides: Any) -> TokenUsageRecord:
    values: dict[str, Any] = {
        "id": entry_id,
        "request_id": entry_id,
        "correlation_id": entry_id,
        "ts": START,
        "team": "Platform",
        "department_id": "platform",
        "department": "Platform",
        "user": "analyst@example.test",
        "user_id": "analyst@example.test",
        "agent": "Analyst",
        "workflow": "analytics",
        "run_id": "analytics-run",
        "turn_index": 1,
        "provider": "test",
        "model_id": "model-a",
        "model": "Model A",
        "runtime": "Connection A",
        "input_tokens": 7,
        "cached_tokens": 5,
        "cache_write_tokens": 2,
        "output_tokens": 11,
        "et": 51.5,
        "et_coeff_m": 1,
        "latency_ms": 1,
        "status": "success",
        "status_code": 200,
        "estimated_cost": 0.125,
        "estimated": False,
        "ingest_source": "eventhub",
    }
    return TokenUsageRecord.model_validate({**values, **overrides})


def test_model_rename_uses_stable_identity_and_latest_snapshot() -> None:
    repository = InMemoryRepository()
    repository.write_token_usage(_usage("before", model="Zulu"))
    repository.write_token_usage(
        _usage("after", model="Alpha", ts=START + timedelta(seconds=1))
    )

    rows = repository.distribution(START, END, "model", UsageFilters(), 50)

    assert len(rows) == 1
    assert rows[0]["id"] == "model-a"
    assert rows[0]["name"] == "Alpha"
    assert rows[0]["total_requests"] == 2
    assert rows[0]["total_tokens"] == 46
    assert rows[0]["cache_read_tokens"] == 6
    assert rows[0]["estimated_cost"] == 0.25
    assert rows[0]["share_percent"] == 100


def test_executive_precision_does_not_round_before_display() -> None:
    repository = InMemoryRepository()
    for index, (latency, status) in enumerate(((1, 200), (1, 200), (2, 500))):
        repository.write_token_usage(
            _usage(f"precision-{index}", latency_ms=latency, status_code=status)
        )

    totals = repository.executive_overview(START, END, UsageFilters())["totals"]

    assert totals["average_latency_ms"] == pytest.approx(4 / 3)
    assert totals["error_rate"] == pytest.approx(100 / 3)
    assert totals["total_tokens"] == 69
    assert totals["cache_read_tokens"] == 9
    assert totals["estimated_cost"] == 0.375

    row = repository.distribution(START, END, "model", UsageFilters(), 50)[0]
    assert row["average_latency_ms"] == pytest.approx(4 / 3)
    assert row["error_rate"] == pytest.approx(100 / 3)
    summary = AssistantTools(repository, lambda: {}).usage_summary(
        UsageSummaryArguments(
            time_range="custom", start_date="2026-10-01", end_date="2026-10-01"
        ),
        "UTC",
        "en",
    )
    displayed = {item["name"]: item["value"] for item in summary.payload["totals"]}
    assert displayed == {
        "Total tokens": 69,
        "Requests": 3,
        "Estimated cost": 0.375,
        "Average latency": 1.3,
        "Error rate": 33.33,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_model_name_is_deterministic_for_ties_and_empty_snapshots(reverse: bool) -> None:
    repository = InMemoryRepository()
    records = [
        _usage("old", model="Zulu"),
        _usage("tie-a", model="z", ts=START + timedelta(seconds=1)),
        _usage("tie-b", model="\u00e9", ts=START + timedelta(seconds=1)),
        _usage("empty", model="", ts=START + timedelta(seconds=2)),
    ]
    for record in reversed(records) if reverse else records:
        repository.write_token_usage(record)
    before = [record.model_dump() for record in repository.usage_records]

    rows = repository.distribution(START, END, "model", UsageFilters(), 50)

    assert len(rows) == 1
    assert rows[0]["name"] == "\u00e9"
    assert rows[0]["total_tokens"] == 92
    assert rows[0]["cache_read_tokens"] == 12
    assert rows[0]["estimated_cost"] == 0.5
    assert [record.model_dump() for record in repository.usage_records] == before


def test_distinct_unregistered_model_ids_with_same_name_do_not_merge() -> None:
    repository = InMemoryRepository()
    repository.models = []
    for identity in ("model-a", "model-b"):
        repository.write_token_usage(_usage(identity, model_id=identity, model="Shared"))

    rows = repository.distribution(START, END, "model", UsageFilters(), 50)

    assert {(row["id"], row["name"]) for row in rows} == {
        ("model-a", "Shared"), ("model-b", "Shared")
    }
    assert all(row["total_tokens"] == 23 for row in rows)
    assert all(row["estimated_cost"] == 0.125 for row in rows)
    assert all(row["share_percent"] == 50 for row in rows)


@pytest.mark.parametrize("identity", ["", "unattributed", "unknown"])
def test_missing_model_identity_keeps_snapshot_groups_and_breakdowns(identity: str) -> None:
    repository = InMemoryRepository()
    for index, name in enumerate(("Alpha", "Beta", "Alpha")):
        repository.write_token_usage(
            _usage(str(index), model_id=identity, model=name)
        )

    rows = repository.distribution(
        START, END, "model", UsageFilters(), 50, split_by="department"
    )

    assert {(row["id"], row["name"]) for row in rows} == {
        (identity, "Alpha"), (identity, "Beta")
    }
    assert [row["total_tokens"] for row in rows] == [46, 23]
    assert [row["cache_read_tokens"] for row in rows] == [6, 3]
    assert [row["estimated_cost"] for row in rows] == [0.25, 0.125]
    assert [row["share_percent"] for row in rows] == [66.67, 33.33]
    assert all(row["breakdown"][0]["total_tokens"] == row["total_tokens"] for row in rows)
    parent = repository.distribution(
        START, END, "department", UsageFilters(), 50, split_by="model"
    )[0]
    assert {(row["id"], row["name"]) for row in parent["breakdown"]} == {
        (identity, "Alpha"), (identity, "Beta")
    }
    assert sum(row["total_tokens"] for row in parent["breakdown"]) == 69
    assert parent["estimated_cost"] == 0.375


@pytest.mark.parametrize("identity,expected", [("model-a", "model-a"), ("", "unattributed")])
def test_empty_model_snapshots_have_a_usable_label(identity: str, expected: str) -> None:
    repository = InMemoryRepository()
    repository.write_token_usage(_usage("empty", model_id=identity, model=""))

    rows = repository.distribution(START, END, "model", UsageFilters(), 50)

    assert rows[0]["id"] == identity
    assert rows[0]["name"] == expected
    assert rows[0]["total_tokens"] == 23
    assert rows[0]["estimated_cost"] == 0.125


@pytest.mark.parametrize("dimension,split_by", [
    ("model", "department"), ("department", "model"), ("model", "model")
])
def test_model_renames_aggregate_in_parent_and_child_groups(
    dimension: str, split_by: str
) -> None:
    repository = InMemoryRepository()
    repository.write_token_usage(_usage("before", model="Zulu"))
    repository.write_token_usage(
        _usage("after", model="Alpha", ts=START + timedelta(seconds=1))
    )
    repository.write_token_usage(_usage("other", model_id="model-b", model="Alpha"))

    rows = repository.distribution(
        START, END, dimension, UsageFilters(), 50, split_by=split_by
    )

    assert sum(row["total_tokens"] for row in rows) == 69
    assert sum(row["cache_read_tokens"] for row in rows) == 9
    assert sum(row["estimated_cost"] for row in rows) == 0.375
    assert all(
        sum(child["total_tokens"] for child in row["breakdown"]) == row["total_tokens"]
        for row in rows
    )
    models = rows if dimension == "model" else rows[0]["breakdown"]
    assert {(row["id"], row["name"], row["total_requests"]) for row in models} == {
        ("model-a", "Alpha", 2), ("model-b", "Alpha", 1)
    }


@pytest.mark.parametrize("dimension,identity_field,label_field", [
    ("organization", "organization_id", "organization"),
    ("department", "department_id", "department"),
    ("project", "project_id", "project"),
    ("agent", "agent_id", "agent"),
    ("user", "user_id", "user"),
    ("runtime", "runtime", "runtime"),
])
def test_other_dimension_snapshot_contracts_are_unchanged(
    dimension: str, identity_field: str, label_field: str
) -> None:
    repository = InMemoryRepository()
    for name in ("First", "Second"):
        repository.write_token_usage(
            _usage(name, **{identity_field: "same", label_field: name})
        )

    rows = repository.distribution(START, END, dimension, UsageFilters(), 50)

    assert {row["name"] for row in rows} == {"First", "Second"}
    assert sum(row["total_tokens"] for row in rows) == 46
    assert sum(row["estimated_cost"] for row in rows) == 0.25


def test_model_aggregation_preserves_window_filters_and_stored_usage() -> None:
    repository = InMemoryRepository()
    records = [
        _usage("inside-a", model="Zulu"),
        _usage("inside-b", model="Alpha", ts=END - timedelta(microseconds=1)),
        _usage("before", ts=START - timedelta(microseconds=1)),
        _usage("end", ts=END),
        _usage("filtered", department_id="other"),
        _usage("copilot", usage_domain="github_copilot"),
    ]
    for record in records:
        repository.write_token_usage(record)
    before = [record.model_dump() for record in repository.usage_records]

    rows = repository.distribution(
        START, END, "model", UsageFilters(department_id="platform"), 50
    )

    assert len(rows) == 1
    assert rows[0]["name"] == "Alpha"
    assert rows[0]["total_tokens"] == 46
    assert rows[0]["cache_read_tokens"] == 6
    assert rows[0]["estimated_cost"] == 0.25
    assert [record.model_dump() for record in repository.usage_records] == before


@pytest.mark.parametrize("registry_case", [
    "same_connection", "same_connection_name", "different_connections",
    "missing_model", "missing_runtime", "empty_registry",
])
def test_assistant_ranking_disambiguates_model_identity(
    registry_case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = InMemoryRepository()
    first, second = repository.models[:2]
    connection = repository.runtimes[1]
    if registry_case in {"same_connection_name", "different_connections"}:
        replica = {
            **connection,
            "id": uuid4(),
            "name": connection["name"] if registry_case == "same_connection_name" else "Replica",
        }
        repository.runtimes.append(replica)
        second["runtime_id"] = replica["id"]
    repository.write_token_usage(_usage("before", model_id=str(first["id"]), model="Old"))
    for index, model in enumerate((first, second)):
        repository.write_token_usage(
            _usage(
                f"shared-{index}", model_id=str(model["id"]), model="Shared",
                ts=START + timedelta(seconds=1),
            )
        )
    registry = repository.registry()
    if registry_case == "missing_model":
        registry["models"] = [row for row in registry["models"] if row["id"] != second["id"]]
    if registry_case in {"missing_runtime", "empty_registry"}:
        registry["runtimes"] = []
    if registry_case == "empty_registry":
        registry["models"] = []
    monkeypatch.setattr(repository, "registry", lambda: registry)

    outcome = AssistantTools(repository, lambda: {}).usage_ranking(
        UsageRankingArguments(
            dimension="model", time_range="custom",
            start_date="2026-10-01", end_date="2026-10-01",
        ),
        "UTC",
        "en",
    )

    names = [row["name"] for row in outcome.payload["rows"]]
    separator = " \u00b7 "
    if registry_case in {"same_connection", "same_connection_name"}:
        assert names == [
            f"Shared{separator}{connection['name']}{separator}[{model['id']}]"
            for model in (first, second)
        ]
    elif registry_case == "different_connections":
        assert names == [f"Shared{separator}{connection['name']}", f"Shared{separator}Replica"]
    elif registry_case == "missing_model":
        assert names == [
            f"Shared{separator}{connection['name']}", f"Shared{separator}[{second['id']}]"
        ]
    else:
        assert names == [f"Shared{separator}[{model['id']}]" for model in (first, second)]
    assert len(set(names)) == 2
    assert outcome.chart is not None
    assert outcome.chart.rows == outcome.payload["rows"]
    assert sum(row["total_tokens"] for row in outcome.chart.rows) == 69
    assert [row["share_percent"] for row in outcome.chart.rows] == [66.67, 33.33]


class _RecordingConnection:
    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.results = results
        self.calls: list[tuple[str, list[Any]]] = []

    def execute(self, query: str, parameters: Sequence[Any]) -> _RecordingConnection:
        self.calls.append((" ".join(query.split()), list(parameters)))
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.results[len(self.calls) - 1]


def _recording_repository(
    monkeypatch: pytest.MonkeyPatch, results: list[list[dict[str, Any]]]
) -> tuple[PostgreSqlOpsDbProxy, _RecordingConnection]:
    recording = _RecordingConnection(results)
    repository = object.__new__(PostgreSqlOpsDbProxy)

    @contextmanager
    def connection() -> Iterator[_RecordingConnection]:
        yield recording

    monkeypatch.setattr(repository, "_connection", connection)
    return repository, recording


@pytest.mark.parametrize("dimension,split_by", [
    ("model", None), ("model", "department"),
    ("department", "model"), ("model", "model"),
])
def test_sql_distribution_groups_model_identity_without_grouping_known_snapshot(
    dimension: str, split_by: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_id = "model-a" if dimension == "model" else "platform"
    parent = {"id": parent_id, "name": "Alpha", "total_tokens": 46}
    child = {"id": "model-a", "name": "Alpha", "total_tokens": 46, "total_requests": 2}
    results = [[parent]]
    if split_by is not None:
        results.append([{**child, "parent_id": parent_id, "parent_name": ""}])
    repository, recording = _recording_repository(monkeypatch, results)

    rows = repository.distribution(
        START, END, dimension, UsageFilters(department_id="platform"), 50, split_by
    )

    query, parameters = recording.calls[0]
    assert parameters == [START, END, "platform"]
    assert "usage.ts >= %s AND usage.ts < %s" in query
    assert "usage.usage_domain = 'apim'" in query
    assert "SUM(input_tokens + cached_tokens + output_tokens)::BIGINT AS total_tokens" in query
    assert "SUM(GREATEST(cached_tokens - cache_write_tokens, 0))::BIGINT" in query
    assert "SUM(estimated_cost)::DOUBLE PRECISION AS estimated_cost" in query
    assert "ROUND(100.0 * SUM(estimated_cost) / total.cost, 2)" in query
    for index, (query, parameters) in enumerate(recording.calls):
        has_model = dimension == "model" or (index == 1 and split_by == "model")
        if has_model:
            assert "IN ('', 'unattributed', 'unknown')" in query
            if (index == 0 and dimension == "model") or (index == 1 and split_by == "model"):
                assert "ARRAY_AGG(model ORDER BY ts DESC, model COLLATE \"C\" DESC)" in query
                assert "FILTER (WHERE model <> '')" in query
            group_clause = query.partition("GROUP BY ")[2].partition("ORDER BY ")[0]
            assert "COALESCE(model_id, '')" in group_clause
            assert "CASE WHEN" in group_clause
            assert "GROUP BY model_id, model" not in query
        if index == 1:
            assert parameters == [START, END, "platform", [parent_id]]
            if split_by == "department":
                assert "department AS name" in query
                assert "AS parent_name" in query
    assert len(recording.calls) == (1 if split_by is None else 2)
    assert rows[0]["total_tokens"] == 46
    if split_by is not None:
        assert rows[0]["breakdown"] == [child]


@pytest.mark.parametrize("identity", ["", "unattributed", "unknown"])
def test_sql_unknown_model_breakdown_is_attached_to_the_matching_snapshot(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parents = [
        {"id": identity, "name": "Alpha", "total_tokens": 46},
        {"id": identity, "name": "Beta", "total_tokens": 23},
    ]
    children = [
        {"parent_id": identity, "parent_name": name, "id": "platform",
         "name": "Platform", "total_tokens": tokens, "total_requests": requests}
        for name, tokens, requests in (("Alpha", 46, 2), ("Beta", 23, 1))
    ]
    repository, recording = _recording_repository(monkeypatch, [parents, children])

    rows = repository.distribution(
        START, END, "model", UsageFilters(), 50, split_by="department"
    )

    assert [row["breakdown"][0]["total_tokens"] for row in rows] == [46, 23]
    assert [row["breakdown"][0]["total_requests"] for row in rows] == [2, 1]
    assert all("parent_name" not in row["breakdown"][0] for row in rows)
    assert "AS parent_name" in recording.calls[1][0]