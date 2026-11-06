from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api import app, runtime_service
from backend.http.dependencies import get_repository
from backend.http.session import SessionIdentity, require_authenticated_session
from backend.services.model_router_service import (
    ModelRouterExecutionService,
    ModelRouterService,
)
from turnstile_core.domain.assistant_models import (
    AssistantReply,
    AssistantRouterProposal,
    AssistantRouterProposalModel,
)
from turnstile_core.domain.model_router import ModelRouterCondition, ModelRouterDefinition
from turnstile_core.domain.runtime_models import (
    DecisionInvocationRequest,
    DecisionInvocationResponse,
    InvocationUsage,
    ModelInvocationRequest,
    ModelInvocationResponse,
)
from turnstile_core.persistence.in_memory import InMemoryRepository

client = TestClient(app)
OWNER_ID = UUID("00000000-0000-4000-8000-000000000101")
MEMBER_ID = UUID("00000000-0000-4000-8000-000000000102")


def identity(id_: UUID, email: str, role: str = "owner") -> SessionIdentity:
    return SessionIdentity(
        id=str(id_),
        email=email,
        name=email.split("@")[0],
        role=role,  # type: ignore[arg-type]
        method="password",
        session_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def router_repository() -> Iterator[tuple[InMemoryRepository, dict[str, SessionIdentity]]]:
    repository = InMemoryRepository()
    current = {"identity": identity(OWNER_ID, "owner@contoso.com")}
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[require_authenticated_session] = lambda: current["identity"]
    yield repository, current
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(require_authenticated_session, None)


def test_router_list_keeps_missing_model_display_metadata_without_changing_candidates(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    model = next(item for item in repository.models if item["id"] == model_id)
    created = client.post("/api/v1/model-routers", json={
        "name": "Retained model display", "description": "",
        "definition": valid_definition(model["id"]),
    })
    assert created.status_code == 201
    definition = created.json()["definition"]
    reference = {
        "id": str(model["id"]), "display_name": model["display_name"],
        "model_key": model["model_key"], "family_key": "openai",
        "provider_name": "Microsoft Foundry", "status": "removed",
    }
    resolve = Mock(return_value=[reference])
    monkeypatch.setattr(repository, "model_router_model_references", resolve, raising=False)
    repository.models = [item for item in repository.models if item["id"] != model["id"]]

    response = client.get("/api/v1/model-routers")

    assert response.status_code == 200
    assert response.json()["model_references"] == [reference]
    assert response.json()["items"][0]["definition"] == definition
    resolve.assert_called_once_with([model["id"]])
    catalog = client.get("/api/v1/model-routers/catalog")
    assert catalog.status_code == 200
    assert str(model["id"]) not in catalog.json()["model_ids"]


def valid_definition(model_id: UUID) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "default_model_id": str(model_id),
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "position": {"x": 40, "y": 160},
            },
            {
                "id": "candidates",
                "type": "candidate_models",
                "label": "Candidate Models",
                "position": {"x": 300, "y": 160},
                "candidates": [
                    {
                        "model_id": str(model_id),
                        "priority": 1,
                        "weight": 100,
                        "enabled": True,
                    }
                ],
            },
            {
                "id": "strategy",
                "type": "strategy",
                "label": "Strategy",
                "position": {"x": 500, "y": 160},
                "strategy_type": "priority",
            },
            {
                "id": "output",
                "type": "output",
                "label": "Output",
                "position": {"x": 700, "y": 160},
            },
        ],
        "edges": [
            {"id": "start-candidates", "source": "start", "target": "candidates"},
            {"id": "candidates-strategy", "source": "candidates", "target": "strategy"},
            {"id": "strategy-output", "source": "strategy", "target": "output"},
        ],
    }


def fallback_definition(first_model_id: UUID, second_model_id: UUID) -> dict[str, Any]:
    definition = valid_definition(first_model_id)
    definition["nodes"][1]["candidates"] = [
        {
            "model_id": str(first_model_id),
            "priority": 1,
            "weight": 100,
            "enabled": True,
        },
        {
            "model_id": str(second_model_id),
            "priority": 2,
            "weight": 100,
            "enabled": True,
        },
    ]
    definition["nodes"].insert(
        3,
        {
            "id": "fallback",
            "type": "fallback",
            "label": "Fallback",
            "position": {"x": 500, "y": 320},
            "fallback_on": ["rate_limit"],
        },
    )
    definition["edges"] = [
        {"id": "start-candidates", "source": "start", "target": "candidates"},
        {
            "id": "strategy-output",
            "source": "strategy",
            "target": "output",
            "label": "正常输出",
        },
        {
            "id": "strategy-fallback",
            "source": "strategy",
            "target": "fallback",
            "label": "异常策略",
        },
        {"id": "candidates-strategy", "source": "candidates", "target": "strategy"},
    ]
    return definition


def model_based_definition(
    routing_model_id: UUID,
    first_model_id: UUID,
    second_model_id: UUID,
) -> dict[str, Any]:
    definition = fallback_definition(first_model_id, second_model_id)
    strategy = next(
        node for node in definition["nodes"] if node["type"] == "strategy"
    )
    strategy["strategy_type"] = "model_based"
    strategy["routing_model_id"] = str(routing_model_id)
    return definition


def described_model_based_definition(
    routing_model_id: UUID,
    first_model_id: UUID,
    second_model_id: UUID,
) -> dict[str, Any]:
    definition = model_based_definition(
        routing_model_id,
        first_model_id,
        second_model_id,
    )
    strategy = next(
        node for node in definition["nodes"] if node["type"] == "strategy"
    )
    strategy["decision_prompt"] = (
        "Choose one authorized candidate and return JSON only.\n{catalog}"
    )
    strategy["max_prompt_chars"] = 64
    candidates = next(
        node for node in definition["nodes"] if node["type"] == "candidate_models"
    )["candidates"]
    candidates[0]["description"] = "Fast, economical default."
    candidates[1]["description"] = ""
    return definition


def legacy_fallback_definition(
    first_model_id: UUID, second_model_id: UUID
) -> dict[str, Any]:
    definition = fallback_definition(first_model_id, second_model_id)
    definition["nodes"] = [
        node for node in definition["nodes"] if node["type"] != "strategy"
    ]
    definition["nodes"].insert(
        2,
        {
            "id": "priority",
            "type": "priority",
            "label": "Priority",
            "position": {"x": 450, "y": 160},
        },
    )
    definition["edges"] = [
        {"id": "start-candidates", "source": "start", "target": "candidates"},
        {"id": "candidates-priority", "source": "candidates", "target": "priority"},
        {"id": "priority-fallback", "source": "priority", "target": "fallback"},
        {"id": "fallback-output", "source": "fallback", "target": "output"},
    ]
    return definition


def invocation_payload(router_id: str, user_id: str = "owner@contoso.com") -> dict[str, Any]:
    return {
        "router_id": router_id,
        "metadata": {
            "organization_id": "org-contoso-global",
            "organization": "Contoso Global",
            "department_id": "department-platform",
            "department": "AI Platform",
            "project_id": "project-finops",
            "project": "Model FinOps",
            "agent_id": "agent-console",
            "agent": "Invocation Console",
            "user_id": user_id,
            "user": user_id.split("@")[0],
            "workflow": "router-test",
            "model_id": "router",
            "model": "router",
            "runtime": "router",
            "request_source": "router-test",
            "run_id": "router-test",
            "turn_index": 1,
        },
        "messages": [{"role": "user", "content": "route this request"}],
    }


def enabled_model_ids(repository: InMemoryRepository) -> list[UUID]:
    return [UUID(str(model["id"])) for model in repository.models if model["enabled"]]


def persist_router_proposal(
    repository: InMemoryRepository,
    *,
    conversation_id: UUID,
    model_id: UUID,
    owner_id: str = "owner@contoso.com",
) -> AssistantRouterProposal:
    definition = ModelRouterDefinition.model_validate(valid_definition(model_id))
    proposal = AssistantRouterProposal(
        id=UUID("00000000-0000-4000-8000-000000000931"),
        name="Assistant Confirmed Router",
        description="Created from a persisted assistant proposal.",
        strategy_type="priority",
        models=[
            AssistantRouterProposalModel(
                model_id=model_id,
                display_name="Proposal model",
                provider_name="Test provider",
                capabilities=["chat"],
                branch="primary",
                priority=1,
                weight=100,
            )
        ],
        definition=definition,
    )
    reply = AssistantReply(
        conversation_id=str(conversation_id),
        message="Review this Router proposal.",
        charts=[],
        router_proposals=[proposal],
        steps=[],
        latency_ms=1,
        model="test-model",
        total_tokens=1,
    )
    repository.append_conversation_turn(
        conversation_id=conversation_id,
        owner_id=owner_id,
        title="Router proposal",
        question="Create a Router",
        reply=reply.model_dump(mode="json"),
    )
    return proposal


def test_execution_plan_scopes_fallback_to_the_reached_candidate_pool() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000911")
    second_model = UUID("00000000-0000-4000-8000-000000000912")
    current = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    )
    legacy = ModelRouterDefinition.model_validate(
        legacy_fallback_definition(first_model, second_model)
    )
    unrelated = current.model_copy(
        update={
            "edges": [edge for edge in current.edges if edge.target != "fallback"]
        }
    )

    current_candidates, current_events, current_strategy = (
        ModelRouterExecutionService._execution_plan(current, {})
    )
    legacy_candidates, legacy_events, legacy_strategy = (
        ModelRouterExecutionService._execution_plan(legacy, {})
    )
    _, unrelated_events, _ = ModelRouterExecutionService._execution_plan(unrelated, {})

    assert [candidate.model_id for candidate in current_candidates] == [
        first_model,
        second_model,
    ]
    assert current_events == {"rate_limit"}
    assert current_strategy == "priority"
    assert [candidate.model_id for candidate in legacy_candidates] == [
        first_model,
        second_model,
    ]
    assert legacy_events == {"rate_limit"}
    assert legacy_strategy == "priority"
    assert unrelated_events == set()


def test_execution_plan_never_bypasses_the_graph_with_default_model() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000913")
    definition = valid_definition(model_id)
    definition["edges"] = [
        {"id": "start-output", "source": "start", "target": "output"}
    ]

    with pytest.raises(
        ValueError, match="selected Router path must reach exactly one Strategy"
    ):
        ModelRouterExecutionService._execution_plan(
            ModelRouterDefinition.model_validate(definition), {}
        )


def test_v2_fallback_timeout_migrates_to_owning_strategy() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000914")
    second_model = UUID("00000000-0000-4000-8000-000000000915")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    ).model_dump(mode="json")
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    strategy["timeout_ms"] = None
    fallback["timeout_ms"] = 1_750

    migrated = ModelRouterDefinition.model_validate(definition)
    migrated_strategy = next(node for node in migrated.nodes if node.type == "strategy")
    migrated_fallback = next(node for node in migrated.nodes if node.type == "fallback")

    assert migrated.schema_version == 2
    assert migrated_strategy.timeout_ms == 1_750
    assert migrated_fallback.timeout_ms is None


def test_v2_shared_fallback_timeout_migrates_to_every_owning_strategy() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000907")
    second_model = UUID("00000000-0000-4000-8000-000000000908")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    ).model_dump(mode="json")
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    strategy["timeout_ms"] = None
    definition["nodes"].append({**strategy, "id": "strategy-2", "label": "Strategy 2"})
    definition["edges"].append(
        {
            "id": "strategy-2-fallback",
            "source": "strategy-2",
            "target": "fallback",
        }
    )
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    fallback["timeout_ms"] = 1_750

    migrated = ModelRouterDefinition.model_validate(definition)

    assert {
        node.timeout_ms for node in migrated.nodes if node.type == "strategy"
    } == {1_750}
    assert next(node for node in migrated.nodes if node.type == "fallback").timeout_ms is None


def test_v2_unowned_fallback_timeout_remains_readable() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000905")
    second_model = UUID("00000000-0000-4000-8000-000000000906")
    definition = fallback_definition(first_model, second_model)
    definition["edges"] = [
        edge for edge in definition["edges"] if edge["target"] != "fallback"
    ]
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    fallback["timeout_ms"] = 1_750

    migrated = ModelRouterDefinition.model_validate(definition)

    assert next(node for node in migrated.nodes if node.type == "fallback").timeout_ms == 1_750


def test_validation_rejects_quality_fallback() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000916")
    second_model = UUID("00000000-0000-4000-8000-000000000917")
    definition = fallback_definition(first_model, second_model)
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    fallback["fallback_on"] = ["quality"]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "fallback_quality_unsupported" in {
        issue.code for issue in validation.issues
    }


def test_validation_rejects_redundant_start_branches() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000919")
    definition = valid_definition(model_id)
    definition["edges"].append(
        {"id": "start-strategy", "source": "start", "target": "strategy"},
    )

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert validation.valid is False
    assert "start_branches_ambiguous" in {issue.code for issue in validation.issues}
    single = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(valid_definition(model_id))
    )
    assert "start_branches_ambiguous" not in {issue.code for issue in single.issues}


def test_validation_rejects_extra_condition_branch() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000918")
    definition = valid_definition(model_id)
    definition["nodes"].insert(
        1,
        {
            "id": "condition",
            "type": "condition",
            "label": "Condition",
            "position": {"x": 180, "y": 160},
            "condition": {"field": "task_type", "operator": "equals", "value": "chat"},
        },
    )
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {"id": "condition-match", "source": "condition", "target": "candidates", "label": "Match"},
        {"id": "condition-else", "source": "condition", "target": "candidates", "label": "Else"},
        {"id": "condition-extra", "source": "condition", "target": "candidates", "label": "Other"},
        *[edge for edge in definition["edges"] if edge["source"] != "start"],
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "condition_branches_ambiguous" in {
        issue.code for issue in validation.issues
    }


def test_validation_checks_else_branch_of_exists_condition_without_sampling() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000910")
    definition = valid_definition(model_id)
    definition["nodes"].insert(
        1,
        {
            "id": "condition",
            "type": "condition",
            "label": "Condition",
            "position": {"x": 180, "y": 160},
            "condition": {
                "field": "request_parameter",
                "operator": "exists",
                "value": None,
            },
        },
    )
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {
            "id": "condition-match",
            "source": "condition",
            "target": "candidates",
            "label": "Match",
        },
        {
            "id": "condition-else",
            "source": "condition",
            "target": "output",
            "label": "Else",
        },
        *[edge for edge in definition["edges"] if edge["source"] != "start"],
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "strategy_path_ambiguous" in {
        issue.code for issue in validation.issues
    }


def test_validation_rejects_multiple_strategies_on_one_path() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000919")
    second_model = UUID("00000000-0000-4000-8000-000000000920")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    ).model_dump(mode="json")
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    definition["nodes"].append({**strategy, "id": "strategy-2", "label": "Strategy 2"})
    definition["edges"].extend(
        [
            {
                "id": "candidates-strategy-2",
                "source": "candidates",
                "target": "strategy-2",
            },
            {
                "id": "strategy-2-output",
                "source": "strategy-2",
                "target": "output",
            },
        ]
    )
    parsed = ModelRouterDefinition.model_validate(definition)

    with pytest.raises(
        ValueError, match="selected Router path must reach exactly one Strategy"
    ):
        ModelRouterExecutionService._execution_plan(parsed, {})
    assert "strategy_path_ambiguous" in {
        issue.code for issue in ModelRouterService.validate_definition(parsed).issues
    }


def test_validation_allows_one_strategy_on_each_exclusive_condition_branch() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000903")
    second_model = UUID("00000000-0000-4000-8000-000000000904")
    definition = ModelRouterDefinition.model_validate(
        valid_definition(first_model)
    ).model_dump(mode="json")
    first_candidates = next(
        node for node in definition["nodes"] if node["type"] == "candidate_models"
    )
    first_strategy = next(
        node for node in definition["nodes"] if node["type"] == "strategy"
    )
    definition["nodes"].extend(
        [
            {
                "id": "condition",
                "type": "condition",
                "label": "Condition",
                "position": {"x": 180, "y": 160},
                "condition": {
                    "field": "task_type",
                    "operator": "equals",
                    "value": "chat",
                },
            },
            {
                **first_candidates,
                "id": "candidates-2",
                "candidates": [{"model_id": str(second_model)}],
            },
            {
                **first_strategy,
                "id": "strategy-2",
                "model_configs": [
                    {
                        "model_id": str(second_model),
                        "priority": 1,
                        "weight": 100,
                        "max_latency_ms": None,
                        "max_cost_per_million": None,
                        "description": "",
                    }
                ],
            },
        ]
    )
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {
            "id": "condition-match",
            "source": "condition",
            "target": "candidates",
            "label": "Match",
        },
        {
            "id": "condition-else",
            "source": "condition",
            "target": "candidates-2",
            "label": "Else",
        },
        {"id": "candidates-strategy", "source": "candidates", "target": "strategy"},
        {"id": "strategy-output", "source": "strategy", "target": "output"},
        {
            "id": "candidates-2-strategy-2",
            "source": "candidates-2",
            "target": "strategy-2",
        },
        {"id": "strategy-2-output", "source": "strategy-2", "target": "output"},
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert validation.valid is True
    assert validation.issues == []


def test_strategy_changes_candidate_order_and_blocks_unavailable_model_routing() -> None:
    first_model = UUID("00000000-0000-4000-8000-000000000921")
    second_model = UUID("00000000-0000-4000-8000-000000000922")
    definition = ModelRouterDefinition.model_validate(
        fallback_definition(first_model, second_model)
    )
    candidate_node = next(
        node for node in definition.nodes if node.type == "candidate_models"
    )
    strategy = next(node for node in definition.nodes if node.type == "strategy")

    assert definition.schema_version == 2
    assert candidate_node.candidates[0].model_dump() == {"model_id": first_model}
    assert [config.priority for config in strategy.model_configs] == [1, 2]
    assert [config.weight for config in strategy.model_configs] == [100, 100]
    strategy.model_configs[0].priority = 1
    strategy.model_configs[0].weight = 10
    strategy.model_configs[1].priority = 2
    strategy.model_configs[1].weight = 90

    priority_candidates, _, priority_type = ModelRouterExecutionService._execution_plan(
        definition, {}
    )
    strategy.strategy_type = "weighted"
    weighted_candidates, _, weighted_type = ModelRouterExecutionService._execution_plan(
        definition, {}
    )
    strategy.strategy_type = "model_based"
    validation = ModelRouterService.validate_definition(definition)
    bypass_definition = fallback_definition(first_model, second_model)
    bypass_definition["edges"].append(
        {
            "id": "candidate-output-bypass",
            "source": "candidates",
            "target": "output",
        }
    )
    bypass_validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(bypass_definition)
    )

    assert priority_type == "priority"
    assert [candidate.model_id for candidate in priority_candidates] == [
        first_model,
        second_model,
    ]
    assert weighted_type == "weighted"
    assert [candidate.model_id for candidate in weighted_candidates] == [
        second_model,
        first_model,
    ]
    assert "strategy_routing_model_missing" in {
        issue.code for issue in validation.issues
    }
    strategy.routing_model_id = first_model
    configured_validation = ModelRouterService.validate_definition(definition)
    assert "strategy_model_based_unavailable" not in {
        issue.code for issue in configured_validation.issues
    }
    assert "strategy_routing_model_missing" not in {
        issue.code for issue in configured_validation.issues
    }
    assert "candidate_strategy_bypass" in {
        issue.code for issue in bypass_validation.issues
    }


def test_condition_requires_match_and_else_branches() -> None:
    model_id = UUID("00000000-0000-4000-8000-000000000923")
    definition = valid_definition(model_id)
    condition = {
        "id": "condition",
        "type": "condition",
        "label": "Condition",
        "position": {"x": 180, "y": 160},
        "condition": {"field": "task_type", "operator": "equals", "value": "chat"},
    }
    definition["nodes"].insert(1, condition)
    definition["edges"] = [
        {"id": "start-condition", "source": "start", "target": "condition"},
        {
            "id": "condition-candidates",
            "source": "condition",
            "target": "candidates",
            "label": "Match",
        },
        *[edge for edge in definition["edges"] if edge["source"] != "start"],
    ]

    validation = ModelRouterService.validate_definition(
        ModelRouterDefinition.model_validate(definition)
    )

    assert "condition_branches_incomplete" in {
        issue.code for issue in validation.issues
    }

    execution_candidates, _, _ = ModelRouterExecutionService._execution_plan(
        ModelRouterDefinition.model_validate(definition),
        {"task_type": "does-not-match"},
    )
    assert [candidate.model_id for candidate in execution_candidates] == [model_id]


def test_condition_matching_uses_real_prompt_context_case_insensitively() -> None:
    payload = invocation_payload("00000000-0000-4000-8000-000000000924")
    payload["router_context"] = {
        "user_id": "spoofed@contoso.com",
        "request_parameter": "client supplied noise",
        "required_capability": "spoofed",
    }
    payload["messages"] = [
        {
            "role": "user",
            "content": "<userRequest>Please DEBUG this failure</userRequest>",
        }
    ]
    request = ModelInvocationRequest.model_validate(payload)

    context = ModelRouterExecutionService._routing_context(request)
    matched = ModelRouterExecutionService._condition_matches(
        ModelRouterCondition(
            field="request_parameter",
            operator="contains",
            value="debug",
        ),
        context,
    )
    length_matched = ModelRouterExecutionService._condition_matches(
        ModelRouterCondition(
            field="prompt_length",
            operator="greater_than_or_equal",
            value=len("Please DEBUG this failure"),
        ),
        context,
    )

    assert context["user_id"] == "owner@contoso.com"
    assert context["request_parameter"] == "Please DEBUG this failure"
    assert context["prompt_length"] == len("Please DEBUG this failure")
    assert context["required_capability"] == "chat"
    assert matched is True
    assert length_matched is True


def test_owner_confirms_persisted_assistant_router_proposal(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, current = router_repository
    model_id = enabled_model_ids(repository)[0]
    conversation_id = UUID("00000000-0000-4000-8000-000000000930")
    proposal = persist_router_proposal(
        repository,
        conversation_id=conversation_id,
        model_id=model_id,
    )
    path = (
        f"/api/v1/assistant/conversations/{conversation_id}"
        f"/router-proposals/{proposal.id}/confirm"
    )

    response = client.post(path)

    assert response.status_code == 201
    created = response.json()
    assert created["name"] == proposal.name
    assert created["status"] == "draft"
    assert created["version"] == 0
    assert created["published_at"] is None
    assert created["definition"] == proposal.definition.model_dump(mode="json")
    assert len(repository.model_routers) == 1
    assert repository.model_router_revisions == []
    assert repository.user_model_router_access == {}
    assert repository.model_router_invocations == {}
    repeated = client.post(path)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created["id"]
    assert len(repository.model_routers) == 1

    repository.model_routers[0]["description"] = "Edited after Assistant creation."
    assert client.post(path).status_code == 409

    current["identity"] = identity(MEMBER_ID, "member@contoso.com", "member")
    assert client.post(path).status_code == 403


def test_router_proposal_confirmation_is_scoped_to_its_conversation_owner(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    conversation_id = UUID("00000000-0000-4000-8000-000000000932")
    proposal = persist_router_proposal(
        repository,
        conversation_id=conversation_id,
        model_id=model_id,
        owner_id="someone.else@contoso.com",
    )

    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}"
        f"/router-proposals/{proposal.id}/confirm",
        json={},
    )

    assert response.status_code == 404
    assert repository.model_routers == []


def test_model_router_draft_publish_revision_and_duplicate_lifecycle(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    created = client.post(
        "/api/v1/model-routers",
        json={
            "name": "客服问答路由",
            "description": "低成本优先，失败后回退",
            "definition": valid_definition(model_id),
        },
    )
    assert created.status_code == 201
    router = created.json()
    assert router["status"] == "draft"
    assert router["version"] == 0
    assert router["has_unpublished_changes"] is True

    listed = client.get("/api/v1/model-routers")
    validated = client.post(f"/api/v1/model-routers/{router['id']}/validate")
    published = client.post(f"/api/v1/model-routers/{router['id']}/publish")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [router["id"]]
    assert validated.json() == {"valid": True, "issues": []}
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["version"] == 1
    assert published.json()["has_unpublished_changes"] is False

    updated_definition = valid_definition(model_id)
    updated_definition["nodes"][1]["position"]["y"] = 240
    updated = client.put(
        f"/api/v1/model-routers/{router['id']}",
        json={
            "name": "客服问答路由",
            "description": "已调整画布",
            "definition": updated_definition,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "published"
    assert updated.json()["version"] == 1
    assert updated.json()["has_unpublished_changes"] is True

    republished = client.post(f"/api/v1/model-routers/{router['id']}/publish")
    duplicated = client.post(
        f"/api/v1/model-routers/{router['id']}/duplicate",
        json={"name": "客服问答路由副本"},
    )
    disabled = client.post(f"/api/v1/model-routers/{router['id']}/disable")

    assert republished.json()["version"] == 2
    assert len(repository.model_router_revisions) == 2
    assert duplicated.status_code == 201
    assert duplicated.json()["status"] == "draft"
    assert duplicated.json()["version"] == 0
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert client.delete(
        f"/api/v1/model-routers/{duplicated.json()['id']}"
    ).status_code == 204
    assert client.delete(f"/api/v1/model-routers/{router['id']}").status_code == 204
    assert client.get(f"/api/v1/model-routers/{router['id']}").status_code == 404
    assert len(repository.model_router_revisions) == 2
    recreated = client.post(
        "/api/v1/model-routers",
        json={"name": "客服问答路由", "definition": valid_definition(model_id)},
    )
    assert recreated.status_code == 201


def test_invalid_draft_can_be_saved_but_cannot_be_published(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    definition = valid_definition(model_id)
    definition["default_model_id"] = None
    definition["edges"].append(
        {"id": "output-start", "source": "output", "target": "start"}
    )
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "待完善路由", "definition": definition},
    )
    assert created.status_code == 201

    router_id = created.json()["id"]
    validation = client.post(f"/api/v1/model-routers/{router_id}/validate")
    publish = client.post(f"/api/v1/model-routers/{router_id}/publish")

    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert {issue["code"] for issue in validation.json()["issues"]} >= {
        "start_has_input",
        "output_has_exit",
        "cycle",
    }
    assert publish.status_code == 409
    assert repository.model_router_revisions == []


def test_model_router_is_user_scoped_and_rejects_unassigned_candidates(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, current = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Owner router", "definition": valid_definition(first_model)},
    )
    assert created.status_code == 201

    current["identity"] = identity(MEMBER_ID, "member@contoso.com", "member")
    repository.user_model_policies["member@contoso.com"] = {
        "user_id": "member@contoso.com",
        "model_ids": [first_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }

    assert client.get("/api/v1/model-routers").json() == {"items": [], "model_references": []}
    assert client.get("/api/v1/model-routers/catalog").json() == {
        "model_ids": [str(first_model)]
    }
    assert client.get(
        f"/api/v1/model-routers/{created.json()['id']}"
    ).status_code == 404
    rejected = client.post(
        "/api/v1/model-routers",
        json={"name": "Unauthorized", "definition": valid_definition(second_model)},
    )
    allowed = client.post(
        "/api/v1/model-routers",
        json={"name": "Member router", "definition": valid_definition(first_model)},
    )
    assert rejected.status_code == 409
    assert allowed.status_code == 201


def test_router_names_are_unique_per_user_case_insensitively(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    payload = {"name": "Code Router", "definition": valid_definition(model_id)}
    assert client.post("/api/v1/model-routers", json=payload).status_code == 201
    payload["name"] = "code router"
    response = client.post("/api/v1/model-routers", json=payload)
    assert response.status_code == 409


def test_router_without_callable_candidates_is_denied_with_a_trace(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Retired Router", "definition": valid_definition(model_id)},
    )
    assert created.status_code == 201
    router_id = created.json()["id"]
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    next(model for model in repository.models if model["id"] == model_id)["enabled"] = False

    denied = client.post("/api/v1/model-gateway/invoke", json=invocation_payload(router_id))

    assert denied.status_code == 409
    assert denied.json()["detail"] == "Model router has no callable candidates"
    [trace] = repository.usage_records
    assert denied.headers["x-request-id"] == trace.request_id
    assert (trace.status_code, trace.model, trace.model_admission) == (
        409, "Retired Router", "router_no_callable_candidates",
    )
    assert (trace.input_tokens, trace.output_tokens, trace.estimated_cost) == (0, 0, 0.0)


def test_published_assigned_router_falls_back_and_persists_attempt_trace(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    attempts: list[tuple[UUID, str, str]] = []

    class FallbackRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            attempts.append((request.model_id, request.metadata.user_id, request_id))
            if request.model_id == first_model:
                raise HTTPException(status_code=429, detail="provider rate limited")
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content="ROUTER_FALLBACK_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=37,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = FallbackRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Fallback Router",
                "definition": fallback_definition(first_model, second_model),
            },
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assignment = {
            "user_id": "owner@contoso.com",
            "assigned": True,
        }
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment", json=assignment
        ).status_code == 409
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        # Creator (owner@contoso.com) can already see and call their own published router
        assert client.get("/api/v1/model-routers/assigned").json()["items"][0][
            "id"
        ] == router_id
        # Unassigned user (member@contoso.com) gets 403, not a silent fallback to gpt-5.6-luna
        assert client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id, "member@contoso.com"),
        ).status_code == 403

        assigned = client.put(
            f"/api/v1/model-routers/{router_id}/assignment", json=assignment
        )
        assert assigned.status_code == 200
        assert client.get("/api/v1/model-routers/assigned").json()["items"][0][
            "id"
        ] == router_id
        assert client.get(
            f"/api/v1/model-routers/{router_id}/assignees"
        ).json() == ["owner@contoso.com"]

        invoked = client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "ROUTER_FALLBACK_OK"
    assert response["router_id"] == router_id
    assert response["router_version"] == 1
    assert response["selected_model_id"] == str(second_model)
    assert [attempt[0] for attempt in attempts] == [first_model, second_model]
    assert {attempt[1] for attempt in attempts} == {"owner@contoso.com"}
    assert attempts[0][2] != attempts[1][2]
    assert [item["sequence"] for item in response["router_attempts"]] == [1, 2]
    assert [item["outcome"] for item in response["router_attempts"]] == [
        "failed",
        "succeeded",
    ]
    final_trace = repository.model_router_invocations[response["request_id"]]
    assert final_trace["router_name"] == "Fallback Router"
    assert final_trace["selected_model_id"] == second_model
    assert final_trace["router_attempts"] == response["router_attempts"]


def test_router_passes_strategy_timeout_and_falls_back_on_504(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    definition = fallback_definition(first_model, second_model)
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    strategy["timeout_ms"] = 1_250
    fallback["fallback_on"] = ["timeout"]
    calls: list[tuple[UUID, int | None]] = []

    class TimeoutRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append((request.model_id, timeout_ms))
            if request.model_id == first_model:
                raise HTTPException(status_code=504, detail="provider timed out")
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content="TIMEOUT_FALLBACK_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=21,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = TimeoutRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Timeout Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "TIMEOUT_FALLBACK_OK"
    assert calls == [(first_model, 1_250), (second_model, 1_250)]
    assert response["router_attempts"][0]["status_code"] == 504
    assert response["router_attempts"][0]["fallback_trigger"] == "timeout"


def test_router_does_not_retry_after_a_slow_success(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    definition = fallback_definition(first_model, second_model)
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    fallback = next(node for node in definition["nodes"] if node["type"] == "fallback")
    strategy["timeout_ms"] = 25
    fallback["fallback_on"] = ["timeout"]
    calls: list[tuple[UUID, int | None]] = []

    class SlowSuccessRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append((request.model_id, timeout_ms))
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content="SLOW_SUCCESS_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=10_000,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = SlowSuccessRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Slow Success Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke", json=invocation_payload(router_id)
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "SLOW_SUCCESS_OK"
    assert response["selected_model_id"] == str(first_model)
    assert calls == [(first_model, 25)]
    assert response["router_attempts"][0]["outcome"] == "succeeded"
    assert response["router_attempts"][0]["latency_ms"] == 10_000


def test_model_based_router_uses_real_user_request_and_selected_candidate(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]
    calls: list[ModelInvocationRequest] = []

    class ModelBasedRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append(request)
            if request.model_id == routing_model:
                assert timeout_ms == 5_000
                assert request.response_format == "json_object"
                assert request.messages[-1].content == "choose the strongest coding model"
                decision_system = request.messages[0].content or ""
                assert str(first_model) in decision_system
                assert str(second_model) in decision_system
                content = json.dumps(
                    {
                        "model_id": str(second_model),
                        "rationale": "The request needs stronger coding reasoning.",
                    }
                )
            else:
                assert request.model_id == second_model
                content = "MODEL_BASED_ROUTER_OK"
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=23,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = ModelBasedRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Model-based Router",
                "definition": model_based_definition(
                    routing_model,
                    first_model,
                    second_model,
                ),
            },
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        payload = invocation_payload(router_id)
        payload["messages"] = [
            {
                "role": "user",
                "content": (
                    "<context>noisy workspace state</context>"
                    "<userRequest>choose the strongest coding model</userRequest>"
                ),
            }
        ]
        invoked = client.post("/api/v1/model-gateway/invoke", json=payload)
        payload["metadata"]["turn_index"] = 2
        payload["messages"] = [
            {
                "role": "user",
                "content": "a different follow-up that must keep the first decision",
            }
        ]
        repeated = client.post("/api/v1/model-gateway/invoke", json=payload)
        bound = next(model for model in repository.models if model["id"] == second_model)
        bound["enabled"] = False
        blocked = client.post("/api/v1/model-gateway/invoke", json=payload)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "model_disabled"
        assert len(calls) == 3
        bound["enabled"] = True
        resumed = client.post("/api/v1/model-gateway/invoke", json=payload)
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    assert repeated.status_code == 200
    response = invoked.json()
    assert response["content"] == "MODEL_BASED_ROUTER_OK"
    assert response["selected_model_id"] == str(second_model)
    assert [request.model_id for request in calls] == [
        routing_model,
        second_model,
        second_model,
        second_model,
    ]
    assert [attempt["attempt_role"] for attempt in response["router_attempts"]] == [
        "routing_decision",
        "candidate",
    ]
    assert response["router_attempts"][0]["decision_basis"] == "model_based_selection"
    assert response["router_attempts"][0]["selected_candidate_model_id"] == str(
        second_model
    )
    decision = response["router_attempts"][0]
    assert decision["decision_system"].startswith(
        "You are Turnstile's model routing classifier."
    )
    assert decision["decision_input_chars"] == len(
        "choose the strongest coding model"
    )
    assert decision["decision_input_sha256"] == hashlib.sha256(
        b"choose the strongest coding model"
    ).hexdigest()
    assert decision["decision_response_sha256"] == hashlib.sha256(
        json.dumps(
            {
                "model_id": str(second_model),
                "rationale": "The request needs stronger coding reasoning.",
            }
        ).encode()
    ).hexdigest()
    assert "decision_input" not in decision
    assert "raw_response" not in decision
    assert "rationale" not in decision
    assert "stronger coding reasoning" not in str(decision)
    assert repeated.json()["selected_model_id"] == str(second_model)
    assert repeated.json()["router_attempts"][0]["decision_basis"] == "interaction_sticky"
    assert resumed.status_code == 200
    assert resumed.json()["selected_model_id"] == str(second_model)
    assert resumed.json()["router_attempts"][0]["decision_basis"] == "interaction_sticky"


def test_model_router_decision_preview_uses_unsaved_prompt_and_descriptions(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]
    definition = described_model_based_definition(
        routing_model,
        first_model,
        second_model,
    )

    response = client.post(
        "/api/v1/model-routers/decision-preview",
        json={
            "definition": definition,
            "strategy_node_id": "strategy",
            "sample_prompt": "x" * 100,
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["system"].startswith(
        "Choose one authorized candidate and return JSON only."
    )
    assert "{catalog}" not in preview["system"]
    assert "Fast, economical default." in preview["system"]
    assert str(first_model) in preview["system"]
    assert str(second_model) in preview["system"]
    assert preview["sample_truncated"] is True
    assert len(preview["user"]) == 64
    assert preview["routing_model_id"] == str(routing_model)
    assert preview["models_without_description"] == [str(second_model)]
    assert preview["is_default_prompt"] is False
    assert preview["has_placeholder"] is True


def test_model_based_router_narrows_catalog_and_falls_back_from_invalid_choice(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, excluded_model = enabled_model_ids(repository)[:3]
    calls: list[UUID] = []

    class NarrowedRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            assert request_id is not None
            calls.append(request.model_id)
            if request.model_id == routing_model:
                catalog = request.messages[0].content or ""
                assert str(first_model) in catalog
                assert str(excluded_model) not in catalog
                content = json.dumps(
                    {
                        "model_id": str(excluded_model),
                        "rationale": "Attempted an excluded model.",
                    }
                )
                usage = InvocationUsage(
                    input_tokens=9,
                    cached_tokens=0,
                    output_tokens=3,
                    estimated=False,
                )
            else:
                assert request.model_id == first_model
                content = "MODEL_BASED_FALLBACK_OK"
                usage = None
            return ModelInvocationResponse(
                request_id=request_id,
                correlation_id=request_id,
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=19,
                usage=usage,
            )

    app.dependency_overrides[runtime_service] = NarrowedRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Narrowed Model-based Router",
                "definition": model_based_definition(
                    routing_model,
                    first_model,
                    excluded_model,
                ),
            },
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        for row in repository.models:
            if row["id"] == excluded_model:
                row["enabled"] = False
        invoked = client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id),
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    assert response["content"] == "MODEL_BASED_FALLBACK_OK"
    assert calls == [routing_model, first_model]
    assert response["router_attempts"][0]["outcome"] == "failed"
    assert response["router_attempts"][0]["decision_basis"] == "model_based_fallback"
    assert response["router_attempts"][0]["total_tokens"] == 12
    assert response["router_attempts"][0]["latency_ms"] == 19
    assert response["router_attempts"][1]["decision_basis"] == "model_based_fallback"


def test_model_based_router_does_not_persist_provider_error_detail(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]

    class FailedDecisionRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            assert request.model_id is not None
            if request.model_id == routing_model:
                raise HTTPException(
                    status_code=429,
                    detail="provider echoed sensitive request content",
                    headers={"x-request-id": request_id or "decision-request"},
                )
            assert request.model_id == first_model
            return ModelInvocationResponse(
                request_id=request_id or "candidate-request",
                correlation_id=request_id or "candidate-request",
                content="PRIVACY_SAFE_FALLBACK_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=17,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = FailedDecisionRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Privacy-safe Model-based Router",
                "definition": model_based_definition(
                    routing_model,
                    first_model,
                    second_model,
                ),
            },
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id),
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    response = invoked.json()
    decision = response["router_attempts"][0]
    assert response["content"] == "PRIVACY_SAFE_FALLBACK_OK"
    assert decision["status_code"] == 429
    assert decision["reason"] == "Routing model request failed"
    assert "sensitive request content" not in str(response)


def test_model_based_prompt_truncation_keeps_both_ends() -> None:
    prompt = "BEGIN-" + ("x" * 8_000) + "-END"

    truncated, was_truncated = ModelRouterExecutionService._truncate_decision_prompt(
        prompt
    )

    assert was_truncated is True
    assert len(truncated) == 4_000
    assert truncated.startswith("BEGIN-")
    assert truncated.endswith("-END")
    assert "[middle omitted]" in truncated


def test_republish_allows_candidates_not_directly_assigned_to_existing_assignees(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    user_id = "member@contoso.com"
    repository.user_model_policies[user_id] = {
        "user_id": user_id,
        "model_ids": [first_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Stable assignment", "definition": valid_definition(first_model)},
    )
    router_id = created.json()["id"]
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    assert client.put(
        f"/api/v1/model-routers/{router_id}/assignment",
        json={"user_id": user_id, "assigned": True},
    ).status_code == 200

    assert client.put(
        f"/api/v1/model-routers/{router_id}",
        json={"name": "Stable assignment", "definition": valid_definition(second_model)},
    ).status_code == 200
    republished = client.post(f"/api/v1/model-routers/{router_id}/publish")
    assigned = client.get(
        "/api/v1/model-routers/assigned", params={"user_id": user_id}
    ).json()["items"]

    assert republished.status_code == 200
    assert assigned[0]["version"] == 2
    assert assigned[0]["definition"]["default_model_id"] == str(second_model)


def test_system_one_decision_model_routes_via_native_choice_with_probabilities(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    decision_model_id, first_model, second_model = enabled_model_ids(repository)[:3]
    decision_row = next(
        model for model in repository.models if model["id"] == decision_model_id
    )
    decision_row["display_name"] = "Microsoft-Decision-1"
    decision_row["model_key"] = "Microsoft-Decision-1"
    decision_row["capabilities"] = ["decision"]

    definition = described_model_based_definition(
        decision_model_id,
        first_model,
        second_model,
    )
    preview_res = client.post(
        "/api/v1/model-routers/decision-preview",
        json={
            "definition": definition,
            "strategy_node_id": "strategy",
            "sample_prompt": "Prove Dijkstra fails on negative edges",
        },
    )
    assert preview_res.status_code == 200
    preview = preview_res.json()
    assert preview["decision_protocol"] == "system_one"
    system_payload = json.loads(preview["system"])
    assert system_payload["questions"]["route"]["type"] == "choice"
    assert str(first_model) in system_payload["questions"]["route"]["criteria"]
    assert str(second_model) in system_payload["questions"]["route"]["criteria"]

    decision_calls: list[DecisionInvocationRequest] = []
    chat_calls: list[ModelInvocationRequest] = []

    class SystemOneRuntimeService:
        def invoke_decision(
            self,
            request: DecisionInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> DecisionInvocationResponse:
            decision_calls.append(request)
            assert request.model_id == decision_model_id
            assert timeout_ms == 5_000
            return DecisionInvocationResponse(
                request_id=request_id or "sysone-req",
                correlation_id="sysone-corr",
                answers={
                    "route": {
                        "choice": str(second_model),
                        "probabilities": {
                            str(first_model): 0.14,
                            str(second_model): 0.86,
                        },
                    }
                },
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=310,
                usage=InvocationUsage(
                    input_tokens=180,
                    cached_tokens=0,
                    output_tokens=0,
                    estimated=False,
                ),
                estimated_cost=0.000008,
            )

        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            chat_calls.append(request)
            assert request.model_id == second_model
            return ModelInvocationResponse(
                request_id=request_id or "chat-req",
                correlation_id=request_id or "chat-req",
                content="SYSTEM_ONE_ROUTED_OK",
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=42,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = SystemOneRuntimeService
    try:
        trial_res = client.post(
            "/api/v1/model-routers/decision-trial",
            json={
                "definition": definition,
                "strategy_node_id": "strategy",
                "sample_prompt": "Prove Dijkstra fails on negative edges",
            },
        )
        assert trial_res.status_code == 200
        trial = trial_res.json()
        assert trial["decision_protocol"] == "system_one"
        assert trial["selected_model_id"] == str(second_model)
        assert trial["confidence"] == 0.86
        assert trial["latency_ms"] == 310
        assert [item["model_id"] for item in trial["probabilities"]] == [
            str(second_model),
            str(first_model),
        ]

        created = client.post(
            "/api/v1/model-routers",
            json={"name": "System One Router", "definition": definition},
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        invoked = client.post(
            "/api/v1/model-gateway/invoke",
            json=invocation_payload(router_id),
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert invoked.status_code == 200
    body = invoked.json()
    assert body["content"] == "SYSTEM_ONE_ROUTED_OK"
    assert body["selected_model_id"] == str(second_model)
    assert len(decision_calls) == 2
    assert len(chat_calls) == 1
    decision_attempt = body["router_attempts"][0]
    assert decision_attempt["decision_protocol"] == "system_one"
    assert decision_attempt["decision_confidence"] == 0.86
    assert decision_attempt["decision_probabilities"] == {
        str(first_model): 0.14,
        str(second_model): 0.86,
    }
    assert "rationale" not in decision_attempt


def test_matches_any_condition_and_reasoning_effort_and_implicit_agent_stickiness(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, first_model, second_model = enabled_model_ids(repository)[:3]

    condition = ModelRouterCondition(
        field="request_parameter",
        operator="matches_any",
        value="证明, 算法, theorem, prove",
    )
    assert (
        ModelRouterExecutionService._condition_matches(
            condition,
            {"request_parameter": "Please PROVE this graph property"},
        )
        is True
    )
    assert (
        ModelRouterExecutionService._condition_matches(
            condition,
            {"request_parameter": "请帮我写一段快排算法"},
        )
        is True
    )
    assert (
        ModelRouterExecutionService._condition_matches(
            condition,
            {"request_parameter": "Translate hello to French"},
        )
        is False
    )

    definition = model_based_definition(routing_model, first_model, second_model)
    strategy = next(node for node in definition["nodes"] if node["type"] == "strategy")
    strategy["decision_reasoning_effort"] = "low"
    seen_requests: list[ModelInvocationRequest] = []

    class ReasoningDecisionRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            seen_requests.append(request)
            if request.model_id == routing_model:
                assert request.reasoning_effort == "low"
                assert request.max_output_tokens == 1_024
                content = json.dumps(
                    {
                        "model_id": str(second_model),
                        "rationale": "Complex agent loop",
                    }
                )
            else:
                content = "AGENT_LOOP_OK"
            return ModelInvocationResponse(
                request_id=request_id or "req",
                correlation_id=request_id or "req",
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=25,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = ReasoningDecisionRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Agent Sticky Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200

        turn1 = invocation_payload(router_id)
        turn1["metadata"]["run_id"] = "ephemeral-turn-1"
        turn1["messages"] = [{"role": "user", "content": "Fix the race condition in queue.ts"}]
        first_res = client.post("/api/v1/model-gateway/invoke", json=turn1)

        turn2 = invocation_payload(router_id)
        turn2["metadata"]["run_id"] = "ephemeral-turn-2"
        turn2["metadata"]["turn_index"] = 2
        turn2["messages"] = [
            {"role": "user", "content": "Fix the race condition in queue.ts"},
            {
                "role": "assistant",
                "content": "Let me read queue.ts first.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"queue.ts"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "export class AsyncQueue {}",
            },
        ]
        second_res = client.post("/api/v1/model-gateway/invoke", json=turn2)
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert first_res.status_code == 200
    assert second_res.status_code == 200
    assert [req.model_id for req in seen_requests] == [
        routing_model,
        second_model,
        second_model,
    ]
    assert second_res.json()["selected_model_id"] == str(second_model)
    assert second_res.json()["router_attempts"][0]["decision_basis"] == "interaction_sticky"


def test_chat_completions_and_invoke_treat_router_like_a_model(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    definition = {
        "schema_version": 2,
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "position": {"x": 40, "y": 160},
            },
            {
                "id": "cond-task",
                "type": "condition",
                "label": "Task Type Check",
                "position": {"x": 220, "y": 160},
                "condition": {
                    "field": "task_type",
                    "operator": "equals",
                    "value": "code",
                },
            },
            {
                "id": "pool-code",
                "type": "candidate_models",
                "label": "Code Models",
                "position": {"x": 420, "y": 80},
                "candidates": [{"model_id": str(first_model)}],
            },
            {
                "id": "strat-code",
                "type": "strategy",
                "label": "Code Strategy",
                "position": {"x": 620, "y": 80},
                "strategy_type": "priority",
                "model_configs": [{"model_id": str(first_model), "priority": 1, "weight": 100}],
            },
            {
                "id": "pool-general",
                "type": "candidate_models",
                "label": "General Models",
                "position": {"x": 420, "y": 240},
                "candidates": [{"model_id": str(second_model)}],
            },
            {
                "id": "strat-general",
                "type": "strategy",
                "label": "General Strategy",
                "position": {"x": 620, "y": 240},
                "strategy_type": "priority",
                "model_configs": [{"model_id": str(second_model), "priority": 1, "weight": 100}],
            },
            {
                "id": "output",
                "type": "output",
                "label": "Output",
                "position": {"x": 840, "y": 160},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "cond-task"},
            {"id": "e2", "source": "cond-task", "target": "pool-code", "label": "Match"},
            {"id": "e3", "source": "cond-task", "target": "pool-general", "label": "Else"},
            {"id": "e4", "source": "pool-code", "target": "strat-code"},
            {"id": "e5", "source": "pool-general", "target": "strat-general"},
            {"id": "e6", "source": "strat-code", "target": "output"},
            {"id": "e7", "source": "strat-general", "target": "output"},
        ],
    }

    class EchoRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            del timeout_ms
            model = next(
                item for item in repository.models if item["id"] == request.model_id
            )
            return ModelInvocationResponse(
                request_id=request_id or "req-direct",
                correlation_id="corr-unified",
                content=f"SERVED_BY:{model['display_name']}",
                provider="Azure OpenAI",
                runtime=str(model["runtime_name"]),
                model=str(model["display_name"]),
                gateway="APIM",
                latency_ms=42,
                usage=InvocationUsage(
                    input_tokens=20,
                    cached_tokens=0,
                    cache_write_tokens=0,
                    output_tokens=10,
                    estimated=False,
                ),
                estimated_cost=0.0002,
            )

    app.dependency_overrides[runtime_service] = EchoRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Unified Condition Router", "definition": definition},
        )
        router_id = created.json()["id"]
        assert client.post(
            f"/api/v1/model-routers/{router_id}/publish"
        ).status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200

        by_name = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Unified Condition Router",
                "messages": [{"role": "user", "content": "Hello via router name"}],
                "router_context": {"task_type": "code"},
            },
        )
        first_model_row = next(
            item for item in repository.models if item["id"] == first_model
        )
        direct_model = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": str(first_model_row["model_key"]),
                "messages": [{"role": "user", "content": "Hello via model key"}],
            },
        )
        invoke_as_model_id = invocation_payload(router_id)
        invoke_as_model_id["model_id"] = invoke_as_model_id.pop("router_id")
        invoke_as_model_id["router_context"] = {"task_type": "general"}
        by_model_id = client.post(
            "/api/v1/model-gateway/invoke",
            json=invoke_as_model_id,
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    assert by_name.status_code == 200
    chat_body = by_name.json()
    assert chat_body["object"] == "chat.completion"
    assert chat_body["choices"][0]["message"]["content"].startswith("SERVED_BY:")
    assert chat_body["turnstile_routing"]["routed"] is True
    assert chat_body["turnstile_routing"]["router_name"] == "Unified Condition Router"
    assert chat_body["turnstile_routing"]["selected_model_id"] == str(first_model)
    cond_eval = chat_body["turnstile_routing"]["router_attempts"][0][
        "condition_evaluations"
    ][0]
    assert cond_eval["field"] == "task_type"
    assert cond_eval["expected"] == "code"
    assert cond_eval["actual"] == "code"
    assert cond_eval["matched"] is True
    assert cond_eval["branch"] == "Match"

    assert direct_model.status_code == 200
    assert direct_model.json()["turnstile_routing"]["routed"] is False
    assert direct_model.json()["model"] == str(first_model_row["display_name"])

    assert by_model_id.status_code == 200
    assert by_model_id.json()["router_id"] == router_id
    assert by_model_id.json()["selected_model_id"] == str(second_model)

    app.dependency_overrides[runtime_service] = EchoRuntimeService
    try:
        sticky_within_ttl = client.post(
            "/api/v1/model-gateway/chat/completions",
            headers={"x-session-id": "sess-ttl-check"},
            json={
                "model": "Unified Condition Router",
                "messages": [{"role": "user", "content": "Turn 1 on code branch"}],
                "router_context": {"task_type": "code"},
            },
        )
        assert (
            sticky_within_ttl.json()["turnstile_routing"]["selected_model_id"]
            == str(first_model)
        )
        reused = client.post(
            "/api/v1/model-gateway/chat/completions",
            headers={"x-session-id": "sess-ttl-check"},
            json={
                "model": "Unified Condition Router",
                "messages": [{"role": "user", "content": "Turn 2 within 30m"}],
                "router_context": {"task_type": "code"},
            },
        )
        assert (
            reused.json()["turnstile_routing"]["selected_model_id"]
            == str(first_model)
        )
        assert (
            reused.json()["turnstile_routing"]["decision_basis"]
            == "session_sticky"
        )
        # 2. Once idle for > 30 minutes, the binding expires and the branch routes afresh
        for row in repository.model_router_run_bindings.values():
            row["updated_at"] = datetime.now(UTC) - timedelta(minutes=31)
        expired_reroute = client.post(
            "/api/v1/model-gateway/chat/completions",
            headers={"x-session-id": "sess-ttl-check"},
            json={
                "model": "Unified Condition Router",
                "messages": [{"role": "user", "content": "Turn 3 after 31m idle"}],
                "router_context": {"task_type": "code"},
            },
        )
        assert (
            expired_reroute.json()["turnstile_routing"]["selected_model_id"]
            == str(first_model)
        )
        assert (
            expired_reroute.json()["turnstile_routing"]["decision_basis"]
            == "highest_priority_success"
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)


def test_subscription_key_exposes_routers_in_models_and_invokes_chat_completions() -> None:
    from backend.http.session import get_auth_store
    from tests.platform.api.api_support import StubAuthStore

    repository = InMemoryRepository()
    first_model, second_model = enabled_model_ids(repository)[:2]
    gateway_id = UUID(str(repository.gateways[0]["id"]))
    app_id = UUID("00000000-0000-4000-8000-000000000701")
    sub_id = UUID("00000000-0000-4000-8000-000000000702")
    now = datetime.now(UTC)
    repository.gateway_applications.append(
        {
            "id": app_id,
            "gateway_profile_id": gateway_id,
            "slug": "enterprise-assistant",
            "display_name": "Enterprise Assistant",
            "description": None,
            "owner_id": None,
            "department_id": None,
            "application_type": "agent",
            "status": "active",
            "system_managed": False,
            "created_by": "owner@contoso.com",
            "created_at": now,
            "updated_by": "owner@contoso.com",
            "updated_at": now,
        }
    )
    test_sub_key = "sub-key-live-test-0123456789abcdef"
    repository.gateway_application_subscriptions.append(
        {
            "id": sub_id,
            "application_id": app_id,
            "gateway_profile_id": gateway_id,
            "apim_subscription_id": "enterprise-assistant",
            "display_name": "Enterprise Assistant",
            "scope_type": "api",
            "scope_id": "/apis/turnstile-llm",
            "state": "active",
            "scope_exists": True,
            "source": "discovered",
            "discovered_at": now,
            "last_synced_at": now,
            "primary_key": test_sub_key,
        }
    )
    definition = valid_definition(first_model)

    class EchoRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            del timeout_ms
            model = next(
                item for item in repository.models if item["id"] == request.model_id
            )
            return ModelInvocationResponse(
                request_id=request_id or "req-sub-key",
                correlation_id="corr-sub-key",
                content=f"SUB_KEY_SERVED:{model['display_name']}",
                provider="Microsoft Foundry",
                runtime=str(model["runtime_name"]),
                model=str(model["display_name"]),
                gateway="Azure API Management",
                latency_ms=19,
                usage=InvocationUsage(
                    input_tokens=12,
                    cached_tokens=0,
                    cache_write_tokens=0,
                    output_tokens=8,
                    estimated=False,
                ),
                estimated_cost=0.0001,
            )

    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_auth_store] = StubAuthStore
    app.dependency_overrides[runtime_service] = EchoRuntimeService
    try:
        # Create and publish router using owner session
        owner_override = identity(OWNER_ID, "owner@contoso.com")
        app.dependency_overrides[require_authenticated_session] = lambda: owner_override
        created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Exposed Subscription Router",
                "description": "Router exposed via /v1/models",
                "definition": definition,
            },
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert (
            client.post(f"/api/v1/model-routers/{router_id}/publish").status_code
            == 200
        )
        # Remove session override so require_authenticated_session uses Ocp-Apim-Subscription-Key
        app.dependency_overrides.pop(require_authenticated_session, None)
        client.cookies.clear()

        # 1. Invalid subscription key is rejected with 401
        bad_models = client.get(
            "/v1/models",
            headers={"Ocp-Apim-Subscription-Key": "invalid-key-000"},
        )
        assert bad_models.status_code == 401

        # 2. Valid Ocp-Apim-Subscription-Key on /v1/models lists both router and models
        for path in (
            "/v1/models",
            "/api/v1/model-gateway/models",
            "/api/v1/model-gateway/v1/models",
        ):
            models_resp = client.get(
                path,
                headers={"Ocp-Apim-Subscription-Key": test_sub_key},
            )
            assert models_resp.status_code == 200, path
            assert models_resp.headers["x-auth-method"] == "subscription_key"
            assert models_resp.headers["x-apim-subscription-id"] == "enterprise-assistant"
            payload = models_resp.json()
            assert payload["object"] == "list"
            router_items = [
                item for item in payload["data"] if item["type"] == "model_router"
            ]
            direct_items = [
                item for item in payload["data"] if item["type"] == "model"
            ]
            assert len(router_items) == 1
            assert router_items[0]["id"] == "Exposed Subscription Router"
            assert router_items[0]["router_id"] == router_id
            assert router_items[0]["object"] == "model"
            assert len(direct_items) >= 1

        # 3. Call /v1/chat/completions and /api/v1/model-gateway/chat/completions with key
        for path in ("/v1/chat/completions", "/api/v1/model-gateway/chat/completions"):
            chat_resp = client.post(
                path,
                headers={"Ocp-Apim-Subscription-Key": test_sub_key},
                json={
                    "model": "Exposed Subscription Router",
                    "messages": [{"role": "user", "content": "Hello with sub key"}],
                },
            )
            assert chat_resp.status_code == 200, path
            assert chat_resp.headers["x-auth-method"] == "subscription_key"
            assert chat_resp.headers["x-apim-subscription-id"] == "enterprise-assistant"
            chat_json = chat_resp.json()
            assert chat_json["turnstile_routing"]["routed"] is True
            assert chat_json["turnstile_routing"]["auth_method"] == "subscription_key"
            assert (
                chat_json["turnstile_routing"]["apim_subscription_id"]
                == "enterprise-assistant"
            )
            assert chat_json["choices"][0]["message"]["content"].startswith(
                "SUB_KEY_SERVED:"
            )

        # 4. Also works when subscription key is passed as Authorization: Bearer <sub_key>
        bearer_resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {test_sub_key}"},
            json={
                "model": "Exposed Subscription Router",
                "messages": [{"role": "user", "content": "Hello via OpenAI SDK Bearer"}],
            },
        )
        assert bearer_resp.status_code == 200
        assert bearer_resp.json()["turnstile_routing"]["auth_method"] == "subscription_key"
    finally:
        app.dependency_overrides.pop(get_repository, None)
        app.dependency_overrides.pop(get_auth_store, None)
        app.dependency_overrides.pop(runtime_service, None)
        app.dependency_overrides.pop(require_authenticated_session, None)


def test_fixed_router_component_keeps_native_admission_and_no_replay() -> None:
    from turnstile_core.integrations.apim_router_global_policy import fixed_router_operation_stages

    inbound, outbound = fixed_router_operation_stages(
        "https://unitstore.table.core.windows.net", "Ledger", "unit-api", str(OWNER_ID),
    )
    requests = {node.get("response-variable-name") for node in inbound.iter("send-request")}
    assert requests == {"routerLookupRead", "routerStickyTableResp", "routerDecisionHttp"}
    decision = inbound.find(".//send-request[@response-variable-name='routerDecisionHttp']")
    assert decision is not None
    assert decision.find("authentication-managed-identity") is None
    assert decision.find("set-header[@name='Ocp-Apim-Subscription-Key']") is not None
    assert "context.Request.OriginalUrl.Host" in decision.findtext("set-url", "")
    assert inbound.find(".//cache-lookup-value") is None
    assert outbound.find(".//send-request[@response-variable-name='routerFallbackHttp']") is None
    proof = inbound.find(".//set-variable[@name='routerNativeDocumentReady']")
    assert proof is not None
    assert "Sha256" in proof.get("value", "") and str(OWNER_ID) in proof.get("value", "")
    trace = outbound.find(
        ".//send-request[@response-variable-name='routerOutboundTraceWrite']/set-body",
    )
    assert trace is not None and 'trace["native_usage_recorded"] = true' in (trace.text or "")


def test_direct_model_streams_expose_the_trace_request_id() -> None:
    from turnstile_core.integrations.apim_router_global_policy import fixed_router_operation_stages

    _, outbound = fixed_router_operation_stages(
        "https://unitstore.table.core.windows.net", "Ledger", "unit-api", str(OWNER_ID),
    )
    owners = {child: owner for owner in outbound.iter() for child in owner}

    def conditions(node: Any) -> str:
        found = []
        while node in owners:
            node = owners[node]
            found.append(node.get("condition", ""))
        return " ".join(found)

    header = next(
        node for node in outbound.iter("set-header")
        if node.get("name") == "x-request-id" and "context.RequestId" in node.findtext("value", "")
    )
    body = next(
        node for node in outbound.iter("set-body") if "turnstile_routing" in (node.text or "")
    )
    assert "routerCallerRequestId" in header.findtext("value", "")
    assert "chat-completions" in conditions(header)
    assert "application/json" not in conditions(header)
    assert "application/json" in conditions(body)


@pytest.mark.parametrize("completed_variable", [
    "routerOutboundCompleted", "routerAnthropicCompleted",
])
def test_routed_model_header_uses_the_selected_ascii_gateway_alias(completed_variable: str) -> None:
    import xml.etree.ElementTree as ET

    from turnstile_core.integrations.apim_router_global_policy import (
        build_router_global_service_policy,
        fixed_router_operation_stages,
    )

    if completed_variable == "routerAnthropicCompleted":
        outbound = ET.fromstring(build_router_global_service_policy(
            "<policies><inbound></inbound><outbound></outbound></policies>",
            ledger_table_endpoint="https://unitstore.table.core.windows.net",
            ledger_table_name="Ledger", api_id="unit-api",
        ))
    else:
        _, outbound = fixed_router_operation_stages(
            "https://unitstore.table.core.windows.net", "Ledger", "unit-api", str(OWNER_ID),
        )
    completed = next(
        node.get("value", "") for node in outbound.iter("set-variable")
        if node.get("name") == completed_variable
    )
    routed = next(
        node.findtext("value", "") for node in outbound.iter("set-header")
        if node.get("name") == "x-routed-model"
        and completed_variable in node.findtext("value", "")
    )

    # Display names such as "gpt-5.4-mini · Microsoft Foundry" are not valid ASCII header values.
    assert 'new JProperty("model_alias", (string)sel["apim_alias"])' in completed
    assert routed == (
        f'@((string)JObject.Parse((string)context.Variables["{completed_variable}"])'
        '["model_alias"])'
    )


@pytest.mark.parametrize("router_name,header", [
    ("统一条件路由", "%E7%BB%9F%E4%B8%80%E6%9D%A1%E4%BB%B6%E8%B7%AF%E7%94%B1"),
    ("Unified Router 1", "Unified Router 1"), ("50% router", "50% router"),
    ("50%20 router", "50%20 router"),
    ("Printable ~% /?+;=,:\"", "Printable ~% /?+;=,:\""),
    ("Router\r\nx-injected: yes", "Router%0D%0Ax-injected%3A%20yes"),
    ("Router café \U0001f600", "Router%20caf%C3%A9%20%F0%9F%98%80"),
    ("路由 %\t名", "%E8%B7%AF%E7%94%B1%20%25%09%E5%90%8D"),
    *[(f"Router{chr(codepoint)}name", f"Router%{codepoint:02X}name")
      for codepoint in (*range(0x20), 0x7F)],
])
@pytest.mark.parametrize("path", [
    "/api/v1/model-gateway/chat/completions", "/api/v1/model-gateway/v1/chat/completions",
    "/v1/chat/completions", "/chat/completions",
])
def test_routed_chat_headers_carry_non_latin_router_and_model_names(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    router_name: str, header: str, path: str,
) -> None:
    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    model = next(item for item in repository.models if item["id"] == model_id)
    model["display_name"] = "统一模型 · Foundry"

    class KeyedRuntimeService:
        def invoke(
            self, request: ModelInvocationRequest, *, request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            del timeout_ms
            assert request.model_id == model_id
            return ModelInvocationResponse(
                request_id=request_id or "req-routed", correlation_id="corr-routed",
                content="ok", provider="Azure OpenAI", runtime=str(model["runtime_name"]),
                model=str(model["model_key"]), gateway="APIM", latency_ms=1,
                usage=InvocationUsage(
                    input_tokens=1, cached_tokens=0, cache_write_tokens=0, output_tokens=1,
                    estimated=False,
                ),
                estimated_cost=0.0,
            )

    app.dependency_overrides[runtime_service] = KeyedRuntimeService
    try:
        router_id = client.post(
            "/api/v1/model-routers",
            json={"name": router_name, "definition": valid_definition(model_id)},
        ).json()["id"]
        assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        response = client.post(
            path,
            json={"model": router_name, "messages": [{"role": "user", "content": "Hello"}]},
        )
    finally:
        app.dependency_overrides.pop(runtime_service, None)

    # The model was already invoked and billed; the headers must not turn it into a 500.
    assert response.status_code == 200
    assert response.headers["x-router-name"] == header
    assert all(" " <= character <= "~" for character in response.headers["x-router-name"])
    assert response.headers["x-routed-model"] == model["model_key"]
    body = response.json()
    assert body["turnstile_routing"]["router_name"] == router_name
    assert body["turnstile_routing"]["requested_model"] == router_name
    assert body["turnstile_routing"]["router_id"] == router_id
    assert body["turnstile_routing"]["selected_model_id"] == str(model_id)
    assert body["turnstile_routing"]["selected_model_name"] == model["display_name"]
    assert body["model"] == model["display_name"]
    assert client.get(f"/api/v1/model-routers/{router_id}").json()["name"] == router_name


def router_table_transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    from email import policy
    from email.parser import BytesParser
    from types import SimpleNamespace
    from urllib.parse import unquote

    import httpx

    from tests.backend.model_platform.test_storage_route_projection import batch_result
    from turnstile_core.config import Settings

    state = SimpleNamespace(entities={}, etags={}, requests=[], fail_name_read=False,
                            fail_batch=False, fail_prepare=False, sequence=0)

    def handle(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.path == "/$batch":
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + request.headers["Content-Type"].encode()
                + b"\r\n\r\n" + request.content,
            )
            mutations = []
            for part in message.walk():
                if part.get_content_type() != "application/http":
                    continue
                raw = part.get_payload(decode=True)
                assert isinstance(raw, bytes)
                head, body = raw.split(b"\r\n\r\n", 1)
                _, headers = head.split(b"\r\n", 1)
                values = json.loads(body)
                mutation_key = (values["PartitionKey"], values["RowKey"])
                parsed_headers = BytesParser(policy=policy.default).parsebytes(headers)
                etag = parsed_headers.get("If-Match")
                if etag and state.etags.get(mutation_key) != etag:
                    return batch_result(412)
                if not etag and mutation_key in state.entities:
                    return batch_result(409)
                mutations.append((mutation_key, values))
            if state.fail_batch:
                return batch_result(503)
            for mutation_key, values in mutations:
                state.sequence += 1
                state.entities[mutation_key] = values
                state.etags[mutation_key] = f'W/"{state.sequence}"'
            return batch_result(operations=len(mutations))
        address = unquote(str(request.url))
        key: tuple[str, str] | None = None
        if "PartitionKey='" in address:
            key = (address.split("PartitionKey='", 1)[1].split("'", 1)[0],
                   address.split("RowKey='", 1)[1].split("'", 1)[0])
        if request.method == "GET":
            if key is None:
                return httpx.Response(200, json={"value": list(state.entities.values())})
            if state.fail_name_read and key[1] == state.name_key:
                return httpx.Response(503)
            if key not in state.entities:
                return httpx.Response(404)
            return httpx.Response(200, json=state.entities[key],
                                  headers={"ETag": state.etags[key]})
        if request.method in {"PUT", "POST"}:
            values = json.loads(request.content)
            key = (values["PartitionKey"], values["RowKey"])
            if state.fail_prepare and values.get("Status") == "prepared":
                return httpx.Response(503)
            state.sequence += 1
            state.entities[key] = values
            state.etags[key] = f'W/"{state.sequence}"'
            return httpx.Response(204, headers={"ETag": state.etags[key]})
        if request.method == "DELETE":
            state.entities.pop(key, None)
            return httpx.Response(204)
        raise AssertionError(f"Unexpected Table request: {request.method} {request.url}")

    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: original_client(
        transport=httpx.MockTransport(handle),
    ))
    monkeypatch.setattr(
        "turnstile_core.integrations.router_table_projection.ManagedIdentityTokenProvider",
        lambda: SimpleNamespace(token=lambda _: "offline-table-token"),
    )
    settings = Settings.model_construct(
        ledger_table_endpoint="https://unitstore.table.core.windows.net",
        ledger_table_name="Ledger",
    )
    monkeypatch.setattr("turnstile_core.config.get_settings", lambda: settings)
    monkeypatch.setattr("turnstile_core.integrations.router_table_projection.get_settings",
                        lambda: settings)
    return state


def test_router_failed_sql_commit_never_activates_table_documents(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.integrations.router_table_projection import ROUTER_CATALOG_PARTITION

    repository, _ = router_repository
    table = router_table_transport(monkeypatch)
    created = client.post("/api/v1/model-routers", json={
        "name": "Commit boundary", "definition": valid_definition(enabled_model_ids(repository)[0]),
    })
    assert created.status_code == 201
    router_id = UUID(created.json()["id"])
    publish = repository.publish_model_router

    def fail_commit(*args: Any, before_record: Any, **kwargs: Any) -> Any:
        def prepare(candidate: dict[str, Any]) -> None:
            before_record(candidate)
            raise RuntimeError("Injected SQL commit failure")
        return publish(*args, before_record=prepare, **kwargs)

    monkeypatch.setattr(repository, "publish_model_router", fail_commit)
    with pytest.raises(RuntimeError, match="SQL commit failure"):
        client.post(f"/api/v1/model-routers/{router_id}/publish")
    current = client.get(f"/api/v1/model-routers/{router_id}").json()
    assert current["status"] == "draft" and current["version"] == 0
    assert repository.model_router_revisions == []
    assert table.entities
    assert all(key[0] != ROUTER_CATALOG_PARTITION for key in table.entities)
    assert all(entity["Status"] == "prepared" for entity in table.entities.values())


@pytest.mark.parametrize("fault", ["prepare", "name-read", "batch"])
@pytest.mark.parametrize("previously_published", [False, True])
def test_router_table_activation_failure_preserves_previous_entries(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch, fault: str, previously_published: bool,
) -> None:
    import httpx

    from turnstile_core.integrations.router_table_projection import (
        ROUTER_CATALOG_PARTITION,
        router_row_key,
    )

    repository, _ = router_repository
    table = router_table_transport(monkeypatch)
    created = client.post("/api/v1/model-routers", json={
        "name": "Atomic activation",
        "definition": valid_definition(enabled_model_ids(repository)[0]),
    })
    router_id = UUID(created.json()["id"])
    if previously_published:
        assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    before = client.get(f"/api/v1/model-routers/{router_id}").json()
    active_before = {key: value.copy() for key, value in table.entities.items()
                     if key[0] == ROUTER_CATALOG_PARTITION}
    table.name_key = router_row_key("Atomic activation")
    table.fail_prepare = fault == "prepare"
    table.fail_name_read = fault == "name-read"
    table.fail_batch = fault == "batch"
    if fault == "prepare":
        with pytest.raises(httpx.HTTPStatusError):
            client.post(f"/api/v1/model-routers/{router_id}/publish")
        assert client.get(f"/api/v1/model-routers/{router_id}").json() == before
    else:
        response = client.post(f"/api/v1/model-routers/{router_id}/publish")
        assert response.status_code == 200
        assert response.json()["version"] == before["version"] + 1
    assert {key: value for key, value in table.entities.items()
            if key[0] == ROUTER_CATALOG_PARTITION} == active_before
    if fault != "prepare":
        from turnstile_core.integrations.router_table_projection import RouterTableProjector

        table.fail_name_read = table.fail_batch = False
        model_id = enabled_model_ids(repository)[0]
        model = next(row for row in repository.models if row["id"] == model_id)
        runtime = next(row for row in repository.runtimes if row["id"] == model["runtime_id"])
        projector = RouterTableProjector("https://unitstore.table.core.windows.net", "Ledger")
        assert projector.sync_all_published_routers(
            repository, gateway_profile_id=runtime["gateway_profile_id"],
        ) == 1
        active = [value for key, value in table.entities.items()
                  if key[0] == ROUTER_CATALOG_PARTITION]
        assert len(active) == 2
        assert all(value["RouterVersion"] == before["version"] + 1 for value in active)


def test_router_backfill_cannot_overwrite_a_newer_active_version(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.integrations.router_table_projection import (
        ROUTER_CATALOG_PARTITION,
        RouterTableProjector,
    )

    repository, _ = router_repository
    table = router_table_transport(monkeypatch)
    created = client.post("/api/v1/model-routers", json={
        "name": "Monotonic activation",
        "definition": valid_definition(enabled_model_ids(repository)[0]),
    })
    router_id = UUID(created.json()["id"])
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    previous = repository.list_published_model_routers()
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    active_before = {key: value.copy() for key, value in table.entities.items()
                     if key[0] == ROUTER_CATALOG_PARTITION}
    assert len(active_before) == 2
    assert all(value["RouterVersion"] == 2 for value in active_before.values())
    monkeypatch.setattr(repository, "list_published_model_routers", lambda: previous)
    projector = RouterTableProjector("https://unitstore.table.core.windows.net", "Ledger")
    from turnstile_core.domain.model_router import ModelRouter

    assert not projector.upsert_router(
        ModelRouter.model_validate(previous[0]), repository.registry(),
    )
    assert projector.sync_all_published_routers(repository) == 0
    current = repository.get_published_model_router(router_id)
    assert current is not None and current["version"] == 2
    assert {key: value for key, value in table.entities.items()
            if key[0] == ROUTER_CATALOG_PARTITION} == active_before


def same_name_router_backfill_scenario(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any], UUID]:
    from tests.backend.telemetry.test_atomic_ledger import TARGET
    from turnstile_core.integrations.router_table_projection import (
        ROUTER_CATALOG_PARTITION,
        RouterTableProjector,
        router_row_key,
    )

    repository, current = router_repository
    table = router_table_transport(monkeypatch)
    model_id = enabled_model_ids(repository)[0]
    model = next(row for row in repository.models if row["id"] == model_id)
    runtime = next(row for row in repository.runtimes if row["id"] == model["runtime_id"])
    runtime["gateway_profile_id"] = TARGET
    model["family_key"] = "gpt"
    model["display_name"] = "Shared candidate"
    monkeypatch.setattr(
        "turnstile_core.integrations.router_table_projection."
        "_resolve_allowed_subscription_ids_for_router",
        lambda router, *_: (["unit-subscription"]
                   if router.created_by == "second-owner@contoso.com" else ["owner-a"]),
    )
    payload: dict[str, Any] = {
        "name": "Shared owner backfill", "definition": valid_definition(model_id),
    }
    first = client.post("/api/v1/model-routers", json=payload)
    assert first.status_code == 201
    assert client.post(f"/api/v1/model-routers/{first.json()['id']}/publish").status_code == 200
    snapshot = repository.list_published_model_routers()
    current["identity"] = identity(MEMBER_ID, "second-owner@contoso.com")
    second = client.post("/api/v1/model-routers", json=payload)
    assert second.status_code == 201
    second_id = UUID(second.json()["id"])
    assert client.post(f"/api/v1/model-routers/{second_id}/publish").status_code == 200
    key = (ROUTER_CATALOG_PARTITION, router_row_key(payload["name"]))
    before = table.entities[key].copy()
    monkeypatch.setattr(repository, "list_published_model_routers", lambda: snapshot)
    projector = RouterTableProjector("https://unitstore.table.core.windows.net", "Ledger")
    assert projector.sync_all_published_routers(repository, gateway_profile_id=TARGET) == 1
    assert repository.get_published_model_router(second_id) is not None
    assert (ROUTER_CATALOG_PARTITION, router_row_key(str(second_id))) in table.entities
    return before, table.entities[key], second_id


def test_router_stale_backfill_preserves_other_owner_name_entry(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from turnstile_core.integrations.router_table_projection import RouterTableProjector

    before, after, second_id = same_name_router_backfill_scenario(router_repository, monkeypatch)
    for entity in (before, after):
        documents = RouterTableProjector._parse_entity_documents(entity)
        assert len(documents) == 2
        assert str(second_id) in {document["router_id"] for document in documents}


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_router_stale_backfill_cannot_reactivate_revoked_router(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    from turnstile_core.integrations.router_table_projection import (
        ROUTER_CATALOG_PARTITION,
        RouterTableProjector,
    )

    repository, _ = router_repository
    table = router_table_transport(monkeypatch)
    model_id = enabled_model_ids(repository)[0]
    model = next(row for row in repository.models if row["id"] == model_id)
    runtime = next(row for row in repository.runtimes if row["id"] == model["runtime_id"])
    created = client.post("/api/v1/model-routers", json={
        "name": "Revocation race", "definition": valid_definition(model_id),
    })
    router_id = UUID(created.json()["id"])
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    snapshot = repository.list_published_model_routers()
    list_published = repository.list_published_model_routers
    if action == "disable":
        assert client.post(f"/api/v1/model-routers/{router_id}/disable").status_code == 200
    else:
        assert client.delete(f"/api/v1/model-routers/{router_id}").status_code == 204
    assert list_published() == []
    assert all(key[0] != ROUTER_CATALOG_PARTITION for key in table.entities)
    monkeypatch.setattr(repository, "list_published_model_routers", lambda: snapshot)
    projector = RouterTableProjector("https://unitstore.table.core.windows.net", "Ledger")
    assert projector.sync_all_published_routers(
        repository, gateway_profile_id=runtime["gateway_profile_id"],
    ) == 0
    monkeypatch.setattr(repository, "list_published_model_routers", list_published)
    assert projector.sync_all_published_routers(
        repository, gateway_profile_id=runtime["gateway_profile_id"],
    ) == 0
    assert all(key[0] != ROUTER_CATALOG_PARTITION for key in table.entities)


def test_native_router_trace_does_not_duplicate_formal_usage() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from turnstile_core.integrations.router_table_projection import RouterTableProjector

    repository = Mock()
    projector = RouterTableProjector("https://unitstore.table.core.windows.net", "Ledger")
    record: dict[str, Any] = {
        "request_id": "native-router-accounted-request", "router_id": str(OWNER_ID),
        "router_name": "Native test", "router_version": 1,
        "native_usage_recorded": True, "attempts": [],
        "usage_records": [{"request_id": "already-formal-decision", "input_tokens": 9}],
    }
    metadata = SimpleNamespace(id="unit", name="Unit")
    for _ in range(2):
        assert projector._sync_entity_doc(
            record["request_id"], json.dumps(record), repository,
            default_org=metadata, default_dept=metadata, default_proj=metadata,
            default_agent=metadata, default_provider="Unit", default_runtime="Unit",
        )
    assert repository.save_model_router_invocation.call_count == 2
    repository.write_token_usage.assert_not_called()


@pytest.mark.parametrize("state", [
    "missing-capability", "expired-proof", "unsupported-fallback", "ready",
    "projection-error-draft", "projection-error-published",
    "projection-error-fallback",
    "multiple-gateways",
])
def test_fixed_native_router_ui_publication_preserves_legacy_contract(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    from time import time
    from unittest.mock import Mock

    from turnstile_core.config import Settings
    from turnstile_core.domain.storage_routes import (
        FixedProgramInstallation,
        ProgramCapabilityProof,
        ProgramIdentity,
        RouteScope,
    )

    repository, _ = router_repository
    first, second = enabled_model_ids(repository)[:2]
    model = next(row for row in repository.models if row["id"] == first)
    runtime = next(row for row in repository.runtimes if row["id"] == model["runtime_id"])
    target = runtime["gateway_profile_id"]
    installed = FixedProgramInstallation(
        program=ProgramIdentity(
            scope=RouteScope(target_id=str(target), api_id="unit-api", gateway_id="managed"),
            api_revision="1", instance_id="managed", program_sha256="a" * 64,
            trust_binding_sha256="b" * 64,
        ),
        target_identity_sha256="c" * 64,
        storage_endpoint="https://unitstore.table.core.windows.net", storage_table="Routes",
        gateway_endpoint="https://unit.azure-api.net/turnstile", publisher_subscription_id="unit",
        parent_policy_sha256="d" * 64, operation_policies_sha256={"models": "e" * 64},
        operation_contracts={"models": ("GET", "/models")}, supported_profiles=("openai-v1",),
        router_enabled=state != "missing-capability",
    )
    monkeypatch.setattr(repository, "effective_gateway_publication", lambda _: {
        "id": OWNER_ID, "status": "active",
        "resource_manifest": {"fixed_route_program": installed.model_dump(mode="json")},
    })
    for row in repository.models:
        if row["id"] in {first, second}:
            row["family_key"] = "gpt"
            row["capabilities"] = ["chat"]
            selected = next(item for item in repository.runtimes if item["id"] == row["runtime_id"])
            selected["gateway_profile_id"] = target
            selected["config"].update(control_plane_managed=True, api_format="openai_chat")
    if state == "multiple-gateways":
        from uuid import uuid4

        second_model = next(row for row in repository.models if row["id"] == second)
        second_runtime = next(row for row in repository.runtimes
                              if row["id"] == second_model["runtime_id"])
        separate_runtime = {
            **second_runtime, "id": uuid4(), "gateway_profile_id": uuid4(),
        }
        repository.runtimes.append(separate_runtime)
        second_model["runtime_id"] = separate_runtime["id"]
    now = int(time())
    proof = ProgramCapabilityProof(
        proof_id=str(UUID(int=912)), identity=installed.program, supported_profiles=("openai-v1",),
        issued_epoch=now - 121 if state == "expired-proof" else now - 1,
        expires_epoch=now - 1 if state == "expired-proof" else now + 119,
    )
    projection = Mock()
    projection.read_program_proof.return_value = (proof, 'W/"proof"')
    monkeypatch.setattr(
        "turnstile_core.integrations.storage_route_projection.TableRouteProjection",
        Mock(return_value=projection),
    )
    settings = Settings.model_construct(
        ledger_table_endpoint="https://unitstore.table.core.windows.net",
        ledger_table_name="Ledger",
    )
    monkeypatch.setattr(
        "turnstile_core.integrations.router_table_projection.get_settings", lambda: settings,
    )
    monkeypatch.setattr("turnstile_core.config.get_settings", lambda: settings)
    projected = Mock()
    projected.prepare_router.return_value = True
    projected.upsert_router.return_value = True
    monkeypatch.setattr("turnstile_core.integrations.router_table_projection.RouterTableProjector",
                        Mock(return_value=projected))
    definition = (fallback_definition(first, second) if state in {
        "unsupported-fallback", "projection-error-fallback",
    }
                  else model_based_definition(first, first, second))
    if state not in {"unsupported-fallback", "projection-error-fallback"}:
        definition["nodes"] = [node for node in definition["nodes"] if node["type"] != "fallback"]
        definition["edges"] = [edge for edge in definition["edges"]
                               if edge["source"] != "fallback" and edge["target"] != "fallback"]
    created = client.post("/api/v1/model-routers", json={
        "name": "Missing native program", "definition": definition,
    })
    assert created.status_code == 201
    router_id = created.json()["id"]
    if state.startswith("projection-error"):
        if state == "projection-error-published":
            initial = client.post(f"/api/v1/model-routers/{router_id}/publish")
            assert initial.status_code == 200
        before = client.get(f"/api/v1/model-routers/{router_id}").json()
        revisions = len(repository.model_router_revisions)
        projected.prepare_router.side_effect = RuntimeError("Injected Router projection failure")
        for _ in range(2):
            with pytest.raises(RuntimeError, match="Router projection failure"):
                client.post(f"/api/v1/model-routers/{router_id}/publish")
            after = client.get(f"/api/v1/model-routers/{router_id}").json()
            assert after == before
            assert len(repository.model_router_revisions) == revisions
        return
    response = client.post(f"/api/v1/model-routers/{router_id}/publish")
    if state in {
        "ready", "unsupported-fallback", "multiple-gateways", "missing-capability", "expired-proof",
    }:
        assert response.status_code == 200 and response.json()["status"] == "published"
        projected.upsert_router.assert_called_once()
        if state == "ready":
            republished = client.post(f"/api/v1/model-routers/{router_id}/publish")
            assert republished.status_code == 200
            assert republished.json()["version"] == 2
            assert projected.upsert_router.call_count == 2
            assert projected.upsert_router.call_args.args[0].version == 2
    projection.read_program_proof.assert_not_called()


@pytest.mark.parametrize("present", [False, True])
def test_router_postgresql_projection_holds_current_published_row_lock(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch, present: bool,
) -> None:
    from unittest.mock import MagicMock

    from turnstile_core.persistence.repository_router import PostgreSqlRouterRepositoryMixin

    repository, _ = router_repository
    created = client.post("/api/v1/model-routers", json={
        "name": "Projection lock", "definition": valid_definition(enabled_model_ids(repository)[0]),
    })
    router_id = UUID(created.json()["id"])
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
    raw = repository.model_routers[0]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = raw if present else None
    transaction = connection.transaction.return_value
    postgres = PostgreSqlRouterRepositoryMixin()
    monkeypatch.setattr(postgres, "_connection", lambda: connection)

    def project(current: dict[str, Any]) -> bool:
        transaction.__enter__.assert_called_once()
        transaction.__exit__.assert_not_called()
        assert current["id"] == router_id and current["version"] == 1
        return True

    writer = Mock(side_effect=project)
    assert postgres.project_published_model_router(router_id, 1, writer) is present
    assert writer.call_count == int(present)
    sql, parameters = connection.execute.call_args.args
    assert parameters == (router_id, 1)
    assert "FOR UPDATE" in sql and "version = %s" in sql
    assert "status = 'published'" in sql and "deleted_at IS NULL" in sql
    transaction.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("projection_fails", [False, True])
def test_router_postgresql_publish_projects_before_transaction_exit(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch, projection_fails: bool,
) -> None:
    from unittest.mock import MagicMock

    from turnstile_core.persistence.repository_router import PostgreSqlRouterRepositoryMixin

    repository, _ = router_repository
    model_id = enabled_model_ids(repository)[0]
    created = client.post("/api/v1/model-routers", json={
        "name": "Transactional Router", "definition": valid_definition(model_id),
    })
    assert created.status_code == 201
    current = repository.model_routers[0]
    pending = {
        **current, "status": "published", "version": 1,
        "published_config": current["draft_config"], "published_at": datetime.now(UTC),
    }
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.side_effect = [current, pending]
    transaction = connection.transaction.return_value
    postgres = PostgreSqlRouterRepositoryMixin()
    monkeypatch.setattr(postgres, "_connection", lambda: connection)

    def project(candidate: dict[str, Any]) -> None:
        transaction.__enter__.assert_called_once()
        transaction.__exit__.assert_not_called()
        assert candidate["status"] == "published"
        assert candidate["version"] == 1
        if projection_fails:
            raise RuntimeError("Injected Router projection failure")

    if projection_fails:
        with pytest.raises(RuntimeError, match="Router projection failure"):
            postgres.publish_model_router(
                current["id"], OWNER_ID, "owner@contoso.com", before_record=project,
            )
        assert transaction.__exit__.call_args.args[0] is RuntimeError
    else:
        published = postgres.publish_model_router(
            current["id"], OWNER_ID, "owner@contoso.com", before_record=project,
        )
        assert published is not None and published["version"] == 1
        transaction.__exit__.assert_called_once_with(None, None, None)
    assert "FOR UPDATE" in connection.execute.call_args_list[0].args[0]


def test_router_table_projection_and_apim_global_policy_are_decoupled_and_parameterized(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    import json as json_mod

    import httpx

    from turnstile_core.domain.model_router import ModelRouter
    from turnstile_core.integrations.apim_router_global_policy import (
        build_router_global_service_policy,
    )
    from turnstile_core.integrations.router_table_projection import (
        ROUTER_CATALOG_PARTITION,
        ROUTER_TRACE_PARTITION,
        RouterTableProjector,
        build_router_table_document,
        router_row_key,
    )

    repository, _ = router_repository
    first_model, second_model = enabled_model_ids(repository)[:2]
    definition = fallback_definition(first_model, second_model)
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Table Projected Router", "definition": definition},
    )
    assert created.status_code == 201
    router_id = created.json()["id"]
    published_resp = client.post(f"/api/v1/model-routers/{router_id}/publish")
    assert published_resp.status_code == 200
    router_obj = ModelRouter.model_validate(published_resp.json())

    doc = build_router_table_document(router_obj, repository.registry())
    assert doc is not None
    assert doc["router_id"] == router_id
    assert doc["router_name"] == "Table Projected Router"
    assert str(first_model) in doc["candidates_by_id"]
    assert str(second_model) in doc["candidates_by_id"]

    # Verify model_based projection distinguishes chat_json vs system_one routing models
    mb_def = model_based_definition(first_model, first_model, second_model)
    mb_created = client.post(
        "/api/v1/model-routers",
        json={"name": "Chat JSON Routing Model Router", "definition": mb_def},
    )
    assert mb_created.status_code == 201
    mb_pub = client.post(f"/api/v1/model-routers/{mb_created.json()['id']}/publish")
    assert mb_pub.status_code == 200
    mb_router = ModelRouter.model_validate(mb_pub.json())
    mb_doc = build_router_table_document(mb_router, repository.registry())
    assert mb_doc is not None
    rm_entry = mb_doc["routing_models_by_id"][str(first_model)]
    assert rm_entry["decision_protocol"] == "chat_json"
    assert isinstance(rm_entry["capabilities"], list)
    assert rm_entry["max_tokens_field"] in {"max_completion_tokens", "max_tokens"}
    assert rm_entry["backend_path"].endswith("/chat/completions")
    assert "cached_cost_per_million" in rm_entry
    assert "cache_write_cost_per_million" in rm_entry

    reg_with_decision = dict(repository.registry())
    reg_with_decision["models"] = [
        {**row, "capabilities": ["decision"]}
        if str(row["id"]) == str(first_model)
        else row
        for row in reg_with_decision["models"]
    ]
    sys1_doc = build_router_table_document(mb_router, reg_with_decision)
    assert sys1_doc is not None
    assert (
        sys1_doc["routing_models_by_id"][str(first_model)]["decision_protocol"]
        == "system_one"
    )
    assert (
        sys1_doc["routing_models_by_id"][str(first_model)]["backend_path"]
        == "/providers/microsoft/v1/systemone"
    )

    # Verify APIM global policy builder is 100% parameterized and idempotent
    base_xml = (
        "<policies><inbound></inbound><backend><forward-request /></backend>"
        "<outbound></outbound><on-error></on-error></policies>"
    )
    compiled_once = build_router_global_service_policy(
        base_xml,
        ledger_table_endpoint="https://example-storage.table.core.windows.net",
        ledger_table_name="ExampleLedgerTable",
        api_id="example-llm-api",
    )
    compiled_twice = build_router_global_service_policy(
        compiled_once,
        ledger_table_endpoint="https://example-storage.table.core.windows.net",
        ledger_table_name="ExampleLedgerTable",
        api_id="example-llm-api",
    )
    assert compiled_once == compiled_twice
    assert "https://example-storage.table.core.windows.net/ExampleLedgerTable" in compiled_once
    assert "example-llm-api" in compiled_once
    assert "cache-lookup-value" in compiled_once
    assert "cache-store-value" in compiled_once
    assert (
        "interactionTtlSeconds = customTtlSeconds &gt; 0 ? customTtlSeconds : 300;"
        in compiled_once
    )
    assert (
        "sessionTtlSeconds = customTtlSeconds &gt; 0 ? customTtlSeconds : 1800;"
        in compiled_once
    )
    assert "__LEDGER_TABLE_ENDPOINT__" not in compiled_once
    assert "__LEDGER_TABLE_NAME__" not in compiled_once
    assert "__API_ID__" not in compiled_once
    assert "/openai/v1/chat/completions" in compiled_once
    assert "/providers/microsoft/v1/systemone" in compiled_once
    assert "proto == &quot;chat_json&quot;" in compiled_once

    from backend.services.model_router_service import ModelRouterExecutionService
    from turnstile_core.domain.runtime_models import ChatMessage

    single_msg = [
        ChatMessage(role="system", content="Huge system prompt that must be ignored"),
        ChatMessage(
            role="user",
            content="<context>noise</context><userRequest>Solve this integral</userRequest>",
        ),
    ]
    assert (
        ModelRouterExecutionService._extract_decision_prompt(single_msg)
        == "Solve this integral"
    )

    multi_msg = [
        ChatMessage(role="system", content="You are a distributed systems architect."),
        ChatMessage(
            role="user",
            content="Design a Raft consensus state machine with leader lease",
        ),
        ChatMessage(
            role="assistant",
            content=(
                "Here are two designs: Option 1 uses heartbeats; "
                "Option 2 uses monotonic quorum leases."
            ),
        ),
        ChatMessage(
            role="tool",
            tool_call_id="call_1",
            content="raw tool dump that must be ignored",
        ),
        ChatMessage(role="user", content="Implement Option 2 in Rust"),
    ]
    extracted_multi = ModelRouterExecutionService._extract_decision_prompt(multi_msg)
    assert extracted_multi.startswith(
        "Task / System instruction:\nYou are a distributed systems architect.\n\n"
        "Current user request:\nImplement Option 2 in Rust"
    )
    assert "Design a Raft consensus state machine" in extracted_multi
    assert "Option 2 uses monotonic quorum leases" in extracted_multi
    assert "raw tool dump" not in extracted_multi

    # Verify streaming and non-streaming trace sync normalizes legacy and new attempt shapes
    trace_payload = {
        "request_id": "req-stream-101",
        "correlation_id": "corr-stream-202",
        "router_id": router_id,
        "router_name": "Table Projected Router",
        "router_version": 1,
        "selected_model_id": str(second_model),
        "selected_model_name": doc["candidates_by_id"][str(second_model)]["display_name"],
        "attempts": [
            {
                "attempt_number": 1,
                "model_id": str(first_model),
                "model_name": doc["candidates_by_id"][str(first_model)]["display_name"],
                "outcome": "skipped",
                "decision_basis": "policy_filtered",
                "attempt_role": "candidate_invocation",
                "reason": "Candidate exceeds static cost ceiling ($25.00/M > $4.00/M)",
            },
            {
                "attempt_number": 2,
                "model_id": str(second_model),
                "model_name": doc["candidates_by_id"][str(second_model)]["display_name"],
                "request_id": "req-stream-101",
                "outcome": "succeeded",
                "status_code": 200,
                "latency_ms": 420,
                "attempt_role": "candidate_invocation",
                "decision_basis": "fallback_success",
            },
        ],
        "usage_records": [],
    }

    class FakeTokenProvider:
        def token(self, scope: str = "https://storage.azure.com/") -> str:
            del scope
            return "fake-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "model-router-trace" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "PartitionKey": ROUTER_TRACE_PARTITION,
                            "RowKey": "req-stream-101",
                            "Document": json_mod.dumps(trace_payload),
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})

    projector = RouterTableProjector(
        "https://example-storage.table.core.windows.net",
        "ExampleLedgerTable",
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )
    orig_client = httpx.Client

    def mock_client(*args: object, **kwargs: object) -> httpx.Client:
        del args, kwargs
        return orig_client(transport=httpx.MockTransport(handler))

    httpx.Client = mock_client  # type: ignore[misc,assignment]
    try:
        synced = projector.sync_traces_to_repository(repository)
    finally:
        httpx.Client = orig_client  # type: ignore[misc]

    assert synced == 1
    assert "req-stream-101" in repository.model_router_invocations
    assert "corr-stream-202" not in repository.model_router_invocations
    synced_trace = repository.model_router_invocations["req-stream-101"]
    assert synced_trace["router_attempts"][0]["sequence"] == 1
    assert synced_trace["router_attempts"][0]["outcome"] == "cost_limit_exceeded"
    assert synced_trace["router_attempts"][0]["attempt_role"] == "candidate"
    assert synced_trace["router_attempts"][1]["sequence"] == 2
    assert synced_trace["router_attempts"][1]["outcome"] == "succeeded"
    assert synced_trace["router_attempts"][1]["attempt_role"] == "candidate"
    assert router_row_key("Table Projected Router").startswith("router:")
    assert ROUTER_CATALOG_PARTITION == "model-router|v1"


def test_model_router_paginated_people_and_bulk_assignments(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    first_model = enabled_model_ids(repository)[0]
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Scalable Access Router", "definition": valid_definition(first_model)},
    )
    assert created.status_code == 201
    router_id = created.json()["id"]
    assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200

    # 1. Paginated people endpoint includes signed-in owner and department summaries
    first_page = client.get(
        f"/api/v1/model-routers/{router_id}/people",
        params={"offset": 0, "limit": 5},
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert body["router_id"] == router_id
    assert body["limit"] == 5
    assert len(body["items"]) <= 5
    assert body["total"] >= len(body["items"])
    assert len(body["departments"]) >= 1
    assert any(item["id"] == "owner@contoso.com" for item in body["items"])

    # 2. Bulk assign an entire department + explicit user override in 1 request
    target_dept = body["departments"][0]["id"]
    bulk_res = client.put(
        f"/api/v1/model-routers/{router_id}/assignments",
        json={
            "department_assignments": [
                {"department_id": target_dept, "assigned": True},
            ],
            "changes": [
                {"user_id": "owner@contoso.com", "assigned": False},
            ],
        },
    )
    assert bulk_res.status_code == 200
    bulk_body = bulk_res.json()
    assert bulk_body["updated_count"] >= 1
    assert bulk_body["assigned_count"] >= 1

    assignees = client.get(f"/api/v1/model-routers/{router_id}/assignees").json()
    assert "owner@contoso.com" not in assignees

    # 3. Search filter + all_matching_assigned clear
    filtered = client.get(
        f"/api/v1/model-routers/{router_id}/people",
        params={"department_id": target_dept, "offset": 0, "limit": 50},
    ).json()
    assert filtered["matching_assigned_count"] == filtered["total"] - 1

    clear_res = client.put(
        f"/api/v1/model-routers/{router_id}/assignments",
        json={
            "all_matching_assigned": False,
            "department_id": "all",
        },
    )
    assert clear_res.status_code == 200
    assert clear_res.json()["assigned_count"] == 0


def test_multi_turn_escalation_from_light_to_strong_and_cache_ratchet(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    # index 0: gpt-5.6-luna ($7/M, light), index 1: gpt-5.3-chat ($35/M, strong), index 2: gpt-5.4
    light_model, strong_model, routing_model = enabled_model_ids(repository)[:3]
    definition = model_based_definition(routing_model, strong_model, light_model)
    seen_calls: list[UUID] = []
    decision_inputs: list[str] = []

    class EscalationRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            del timeout_ms
            assert request.model_id is not None
            seen_calls.append(request.model_id)
            if request.model_id == routing_model:
                raw_input = request.messages[-1].content or ""
                decision_inputs.append(raw_input)
                user_text = raw_input.lower()
                first_line = (
                    user_text.splitlines()[1]
                    if user_text.startswith("current user request:")
                    else user_text
                )
                target = (
                    strong_model
                    if "refactor" in first_line or "distributed" in first_line
                    else light_model
                )
                content = json.dumps({"model_id": str(target), "rationale": "Complexity check"})
            else:
                content = f"SERVED_BY:{request.model_id}"
            return ModelInvocationResponse(
                request_id=request_id or "req-esc",
                correlation_id=request_id or "req-esc",
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=20,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = EscalationRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Ratchet Escalation Router", "definition": definition},
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200

        # 1. Turn 1: "Tell me a joke" -> routes to light_model
        t1 = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Ratchet Escalation Router",
                "messages": [{"role": "user", "content": "Tell me a joke"}],
            },
        )
        assert t1.status_code == 200
        assert t1.json()["turnstile_routing"]["selected_model_id"] == str(light_model)
        assert seen_calls == [routing_model, light_model]

        # 2. Turn 1b: Tool-call loop on light_model stays interaction_sticky
        t1_tool = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Ratchet Escalation Router",
                "messages": [
                    {"role": "user", "content": "Tell me a joke"},
                    {
                        "role": "assistant",
                        "content": "Checking joke database",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "get_joke", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "c1", "content": "Joke #42"},
                ],
            },
        )
        assert t1_tool.status_code == 200
        assert t1_tool.json()["turnstile_routing"]["selected_model_id"] == str(light_model)
        assert t1_tool.json()["turnstile_routing"]["decision_basis"] == "interaction_sticky"
        assert seen_calls == [routing_model, light_model, light_model]

        # 3. Turn 2: Complex coding follow-up in the same conversation escalates to strong_model
        t2 = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Ratchet Escalation Router",
                "messages": [
                    {"role": "user", "content": "Tell me a joke"},
                    {"role": "assistant", "content": "Why did the developer go broke?"},
                    {
                        "role": "user",
                        "content": "Now refactor this distributed consensus engine in Rust",
                    },
                ],
            },
        )
        assert t2.status_code == 200
        assert t2.json()["turnstile_routing"]["selected_model_id"] == str(strong_model)
        assert t2.json()["turnstile_routing"]["decision_basis"] == "model_based_selection"
        assert decision_inputs[0] == "Tell me a joke"
        assert decision_inputs[1].startswith(
            "Current user request:\nNow refactor this distributed consensus engine in Rust"
        )
        assert "Active conversation model:" in decision_inputs[1]
        assert "- Previous user message: Tell me a joke" in decision_inputs[1]
        assert "- Previous assistant reply: Why did the developer go broke?" in decision_inputs[1]
        assert seen_calls == [
            routing_model,
            light_model,
            light_model,
            routing_model,
            strong_model,
        ]

        # 4. Turn 3: Short follow-up within 5m stays locked on strong_model (preserves cache)
        t3 = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Ratchet Escalation Router",
                "messages": [
                    {"role": "user", "content": "Tell me a joke"},
                    {"role": "assistant", "content": "Why did the developer go broke?"},
                    {
                        "role": "user",
                        "content": "Now refactor this distributed consensus engine in Rust",
                    },
                    {"role": "assistant", "content": "Here is the refactored Rust code..."},
                    {"role": "user", "content": "Rename the timeout variable"},
                ],
            },
        )
        assert t3.status_code == 200
        assert t3.json()["turnstile_routing"]["selected_model_id"] == str(strong_model)
        assert t3.json()["turnstile_routing"]["decision_basis"] == "interaction_sticky"
        assert seen_calls == [
            routing_model,
            light_model,
            light_model,
            routing_model,
            strong_model,
            strong_model,
        ]

        # 5. Turn 4: After > 5m inactivity, a simple joke question routes back to light_model
        for row in repository.model_router_run_bindings.values():
            row["updated_at"] = datetime.now(UTC) - timedelta(minutes=6)
        t4 = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Ratchet Escalation Router",
                "messages": [
                    {"role": "user", "content": "Tell me a joke"},
                    {"role": "assistant", "content": "Why did the developer go broke?"},
                    {
                        "role": "user",
                        "content": "Now refactor this distributed consensus engine in Rust",
                    },
                    {"role": "assistant", "content": "Here is the refactored Rust code..."},
                    {"role": "user", "content": "Tell me another short joke"},
                ],
            },
        )
        assert t4.status_code == 200
        assert t4.json()["turnstile_routing"]["selected_model_id"] == str(light_model)
        assert t4.json()["turnstile_routing"]["decision_basis"] == "model_based_selection"
    finally:
        app.dependency_overrides.pop(runtime_service, None)


def test_model_router_extracts_system_task_instruction_and_request_constraints(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    repository, _ = router_repository
    routing_model, strong_model, light_model = enabled_model_ids(repository)[:3]
    for model in repository.models:
        if model["id"] == strong_model:
            model["input_cost_per_million"] = 5.0
            model["output_cost_per_million"] = 25.0
        elif model["id"] == light_model:
            model["input_cost_per_million"] = 0.0
            model["output_cost_per_million"] = 0.0

    definition = model_based_definition(routing_model, strong_model, light_model)
    captured_decision_inputs: list[str] = []

    class TaskAwareRuntimeService:
        def invoke(
            self,
            request: ModelInvocationRequest,
            *,
            request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            del timeout_ms
            assert request.model_id is not None
            if request.model_id == routing_model:
                raw_input = request.messages[-1].content or ""
                captured_decision_inputs.append(raw_input)
                task_section = raw_input.split("Current user request:\n", 1)[0].lower()
                target = (
                    light_model
                    if "concise 3-5 word title" in task_section
                    or "max_output_tokens=64" in task_section
                    else strong_model
                )
                content = json.dumps({"model_id": str(target), "rationale": "Task instruction"})
            else:
                content = f"SERVED_BY:{request.model_id}"
            return ModelInvocationResponse(
                request_id=request_id or "req-task",
                correlation_id=request_id or "req-task",
                content=content,
                provider="Microsoft Foundry",
                runtime=request.metadata.runtime,
                model=request.metadata.model,
                gateway="Azure API Management",
                latency_ms=15,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = TaskAwareRuntimeService
    try:
        created = client.post(
            "/api/v1/model-routers",
            json={"name": "Task Instruction Router", "definition": definition},
        )
        assert created.status_code == 201
        router_id = created.json()["id"]
        assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200

        # 1. Body generation: system prompt specifies deep report writing, user has raw notes
        body_res = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Task Instruction Router",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "<context>noisy ide block</context>"
                            "You are a technical weekly report writer. Transform raw meeting "
                            "notes into a structured English engineering report and "
                            "architecture analysis."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "Meeting notes: 今天同步了语音网关改造和自动评测进展。",
                    },
                ],
            },
        )
        assert body_res.status_code == 200
        assert body_res.json()["turnstile_routing"]["selected_model_id"] == str(strong_model)

        # 2. Title generation: system prompt specifies short title + max_completion_tokens=64,
        # while user message contains long technical architecture prose
        title_res = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Task Instruction Router",
                "max_completion_tokens": 64,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Generate a concise 3-5 word title for the following technical notes."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "# Foundry Voice Agent 2.0 Architecture & MCP Integration\n"
                            "We redesigned the distributed streaming architecture and end-to-end "
                            "OpenTelemetry trace pipeline across Azure API Management."
                        ),
                    },
                ],
            },
        )
        assert title_res.status_code == 200
        assert title_res.json()["turnstile_routing"]["selected_model_id"] == str(light_model)

        assert len(captured_decision_inputs) == 2
        assert captured_decision_inputs[0].startswith(
            "Task / System instruction:\nYou are a technical weekly report writer."
        )
        assert "noisy ide block" not in captured_decision_inputs[0]
        assert captured_decision_inputs[1].startswith(
            "Task / System instruction:\n"
            "Generate a concise 3-5 word title for the following technical notes."
        )
        assert "Request constraints: max_output_tokens=64" in captured_decision_inputs[1]

        # 3. Verify @github/copilot-sdk customize-mode envelope and <user_message> wrapper
        sdk_wrapped_title_res = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "Task Instruction Router",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the GitHub Copilot CLI, a terminal assistant built by "
                            "GitHub. You help with software engineering tasks.\n"
                            "<environment_limitations>sandbox</environment_limitations>\n"
                            "<tools><code_search_tools>search</code_search_tools></tools>\n"
                            "<system_notifications>notify</system_notifications>\n\n"
                            "Generate a concise 3-5 word title from one user message.\n\n"
                            "<session_context>Session folder: /tmp</session_context>\n"
                            "Your goal is to deliver complete, working solutions.\n"
                            "<task_completion>verify</task_completion>"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "<current_datetime>2026-10-22T05:13:12Z</current_datetime>\n\n"
                            "Create a title for this single user message:\n"
                            "<user_message>\n"
                            + ("Distributed consensus & MCP integration architecture. " * 25)
                            + "\n</user_message>"
                        ),
                    },
                ],
            },
        )
        assert sdk_wrapped_title_res.status_code == 200
        assert (
            sdk_wrapped_title_res.json()["turnstile_routing"]["selected_model_id"]
            == str(light_model)
        )
        assert captured_decision_inputs[2].startswith(
            "Task / System instruction:\n"
            "Generate a concise 3-5 word title from one user message."
        )
        assert "GitHub Copilot CLI" not in captured_decision_inputs[2]
        assert "<current_datetime>" not in captured_decision_inputs[2]
    finally:
        app.dependency_overrides.pop(runtime_service, None)


def test_model_router_context_price_tiers_and_attempt_price_enrichment(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    from backend.services.model_router_service import ModelRouterExecutionService
    from turnstile_core.domain.model_router import ModelRouter, ModelRouterStrategyModel
    from turnstile_core.integrations.router_table_projection import (
        build_router_table_document,
    )
    from turnstile_core.persistence.repository_support import enrich_router_attempts

    repository, _ = router_repository
    routing_model, tiered_model, unpriced_model = enabled_model_ids(repository)[:3]
    for model in repository.models:
        if model["id"] == tiered_model:
            model["input_cost_per_million"] = None
            model["output_cost_per_million"] = None
            model["cached_cost_per_million"] = None
            model["cache_write_cost_per_million"] = None
            model["context_price_tiers"] = [
                {
                    "key": "short",
                    "min_input_tokens": 0,
                    "max_input_tokens": 272000,
                    "input_price_per_million": 0.2,
                    "cached_price_per_million": 0.02,
                    "cache_write_price_per_million": 0.25,
                    "output_price_per_million": 1.2,
                },
                {
                    "key": "long",
                    "min_input_tokens": 272001,
                    "max_input_tokens": None,
                    "input_price_per_million": 0.4,
                    "cached_price_per_million": 0.04,
                    "cache_write_price_per_million": 0.5,
                    "output_price_per_million": 1.8,
                },
            ]
        elif model["id"] == unpriced_model:
            model["input_cost_per_million"] = None
            model["output_cost_per_million"] = None
            model["cached_cost_per_million"] = None
            model["cache_write_cost_per_million"] = None
            model["context_price_tiers"] = []

    definition = model_based_definition(routing_model, tiered_model, unpriced_model)
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "Context Tiered Router", "definition": definition},
    )
    assert created.status_code == 201
    router_id = created.json()["id"]
    published = client.post(f"/api/v1/model-routers/{router_id}/publish")
    assert published.status_code == 200
    router_obj = ModelRouter.model_validate(published.json())

    doc = build_router_table_document(router_obj, repository.registry())
    assert doc is not None
    tiered_entry = doc["candidates_by_id"][str(tiered_model)]
    assert tiered_entry["input_cost_per_million"] == 0.2
    assert tiered_entry["cached_cost_per_million"] == 0.02
    assert tiered_entry["cache_write_cost_per_million"] == 0.25
    assert tiered_entry["output_cost_per_million"] == 1.2
    assert tiered_entry["effective_cost_per_million"] == 1.2
    assert len(tiered_entry["context_price_tiers"]) == 2

    unpriced_entry = doc["candidates_by_id"][str(unpriced_model)]
    assert unpriced_entry["input_cost_per_million"] is None
    assert unpriced_entry["output_cost_per_million"] is None
    assert unpriced_entry["effective_cost_per_million"] is None
    assert unpriced_entry["context_price_tiers"] == []

    models_map = {UUID(str(m["id"])): m for m in repository.models}
    assert ModelRouterExecutionService._candidate_effective_cost(models_map[tiered_model]) == 1.4
    assert ModelRouterExecutionService._over_cost_limit(models_map[tiered_model], 0.5) is True
    assert ModelRouterExecutionService._over_cost_limit(models_map[tiered_model], 1.5) is False
    assert ModelRouterExecutionService._over_cost_limit(models_map[unpriced_model], 0.5) is False

    catalog = ModelRouterExecutionService._decision_catalog(
        [
            ModelRouterStrategyModel(model_id=tiered_model),
            ModelRouterStrategyModel(model_id=unpriced_model),
        ],
        models_map,
    )
    assert catalog[0]["input_cost_per_million"] == 0.2
    assert catalog[0]["output_cost_per_million"] == 1.2
    assert catalog[1]["input_cost_per_million"] is None
    assert catalog[1]["output_cost_per_million"] is None

    # Verify enrich_router_attempts replaces bogus 0.0 rates with historical tiered snapshot,
    # preserves legitimate 0.0 rates, and clears bogus 0.0 rates when historical usage is unpriced.
    enriched = enrich_router_attempts(
        [
            {
                "sequence": 1,
                "attempt_role": "routing_decision",
                "model_id": str(routing_model),
                "model_name": "Decision-1",
                "request_id": "req-dec-1",
                "outcome": "succeeded",
                "input_tokens": 444,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 1,
                "total_tokens": 445,
                "estimated_cost": 0.00001678,
                "input_price_per_million": 0.0378,
                "cached_price_per_million": 0.0378,
                "cache_write_price_per_million": 0.0,
                "output_price_per_million": 0.0,
            },
            {
                "sequence": 2,
                "attempt_role": "candidate",
                "model_id": str(tiered_model),
                "model_name": "gpt-5.6-luna",
                "request_id": "verify-73898f55-a18d-4752-a16f-874397429822",
                "outcome": "succeeded",
                "input_tokens": 112,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 32,
                "total_tokens": 144,
                "estimated_cost": 0.0,
                "input_price_per_million": 0.0,
                "cached_price_per_million": 0.0,
                "cache_write_price_per_million": 0.0,
                "output_price_per_million": 0.0,
            },
            {
                "sequence": 3,
                "attempt_role": "candidate",
                "model_id": str(unpriced_model),
                "model_name": "unpriced-model",
                "request_id": "req-unpriced",
                "outcome": "succeeded",
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "estimated_cost": 0.0,
                "input_price_per_million": 0.0,
                "cached_price_per_million": 0.0,
                "cache_write_price_per_million": 0.0,
                "output_price_per_million": 0.0,
            },
            {
                "sequence": 4,
                "attempt_role": "candidate",
                "model_id": str(routing_model),
                "model_name": "free-model",
                "request_id": "req-free",
                "outcome": "succeeded",
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "estimated_cost": 0.0,
                "input_price_per_million": 0.0,
                "cached_price_per_million": 0.0,
                "cache_write_price_per_million": 0.0,
                "output_price_per_million": 0.0,
            },
        ],
        {
            "verify-73898f55-a18d-4752-a16f-874397429822": {
                "input_tokens": 112,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 32,
                "total_tokens": 144,
                "estimated_cost": 0.0000608,
                "input_price_per_million": 0.2,
                "cached_price_per_million": 0.02,
                "cache_write_price_per_million": 0.25,
                "output_price_per_million": 1.2,
                "price_context_tier": "short",
            },
            "req-unpriced": {
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "estimated_cost": 0.0,
                "input_price_per_million": None,
                "cached_price_per_million": None,
                "cache_write_price_per_million": None,
                "output_price_per_million": None,
            },
            "req-free": {
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "estimated_cost": 0.0,
                "input_price_per_million": 0.0,
                "cached_price_per_million": 0.0,
                "cache_write_price_per_million": 0.0,
                "output_price_per_million": 0.0,
            },
        },
    )
    assert enriched[1]["estimated_cost"] == 0.0000608
    assert enriched[1]["input_price_per_million"] == 0.2
    assert enriched[1]["cached_price_per_million"] == 0.02
    assert enriched[1]["cache_write_price_per_million"] == 0.25
    assert enriched[1]["output_price_per_million"] == 1.2
    assert enriched[1]["price_context_tier"] == "short"

    assert enriched[2]["input_price_per_million"] is None
    assert enriched[2]["output_price_per_million"] is None

    assert enriched[3]["input_price_per_million"] == 0.0
    assert enriched[3]["output_price_per_million"] == 0.0


def test_audit_fixes_start_node_partial_candidates_sub_key_bounds_and_trace_pagination(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
) -> None:
    import json as json_mod

    import httpx

    from backend.http.session import get_auth_store
    from tests.platform.api.api_support import StubAuthStore
    from turnstile_core.domain.model_router import ModelRouter
    from turnstile_core.integrations.apim_router_global_policy import (
        build_router_global_service_policy,
    )
    from turnstile_core.integrations.ledger import LedgerSyncService
    from turnstile_core.integrations.router_table_projection import (
        ROUTER_TRACE_PARTITION,
        RouterTableProjector,
        build_router_table_document,
    )

    repository, _ = router_repository
    first_model, second_model, third_model = enabled_model_ids(repository)[:3]

    # 1. UI-created start node ID ("node-<uuid>") + partial candidate availability projection
    ui_definition = {
        "schema_version": 2,
        "default_model_id": str(first_model),
        "nodes": [
            {
                "id": "node-2dd2ed60-1111-4000-8000-000000000001",
                "type": "start",
                "label": "Start",
                "position": {"x": 40, "y": 160},
            },
            {
                "id": "node-pool-1",
                "type": "candidate_models",
                "label": "Candidate Models",
                "position": {"x": 240, "y": 160},
                "candidates": [
                    {"model_id": str(first_model)},
                    {"model_id": str(second_model)},
                ],
            },
            {
                "id": "node-strat-1",
                "type": "strategy",
                "label": "Strategy",
                "position": {"x": 460, "y": 160},
                "strategy_type": "priority",
                "model_configs": [
                    {"model_id": str(first_model), "priority": 1, "weight": 100},
                    {"model_id": str(second_model), "priority": 2, "weight": 100},
                ],
            },
            {
                "id": "node-out-1",
                "type": "output",
                "label": "Output",
                "position": {"x": 680, "y": 160},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "node-2dd2ed60-1111-4000-8000-000000000001",
                "target": "node-pool-1",
            },
            {"id": "e2", "source": "node-pool-1", "target": "node-strat-1"},
            {"id": "e3", "source": "node-strat-1", "target": "node-out-1"},
        ],
    }
    created = client.post(
        "/api/v1/model-routers",
        json={"name": "UI Start Node Router", "definition": ui_definition},
    )
    assert created.status_code == 201
    router_id = created.json()["id"]
    published = client.post(f"/api/v1/model-routers/{router_id}/publish")
    assert published.status_code == 200
    assert (
        client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "member@contoso.com", "assigned": True},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code
        == 200
    )

    # Disable second_model; router projection must still succeed with first_model
    for row in repository.models:
        if row["id"] == second_model:
            row["enabled"] = False
    try:
        router_obj = ModelRouter.model_validate(published.json())
        doc = build_router_table_document(
            router_obj,
            repository.registry(),
            assigned_user_ids=["member@contoso.com"],
        )
        assert doc is not None
        assert str(first_model) in doc["candidates_by_id"]
        assert str(second_model) not in doc["candidates_by_id"]
        assert doc["owner_email"] == "owner@contoso.com"
        assert doc["assigned_user_ids"] == ["member@contoso.com", "owner@contoso.com"]
        assert doc["candidate_model_ids"] == [str(first_model)]
    finally:
        for row in repository.models:
            if row["id"] == second_model:
                row["enabled"] = True

    # 2. Direct model access ("M" row) does NOT include router candidate models,
    #    but DOES include router:<router_id> for assigned users and creators.
    repository.user_model_policies["member@contoso.com"] = {
        "user_id": "member@contoso.com",
        "model_ids": [third_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }
    direct_access = LedgerSyncService(
        repository, None  # type: ignore[arg-type]
    ).model_access()
    member_access = next(
        row for row in direct_access if row.user_id == "member@contoso.com"
    )
    assert str(third_model) in member_access.identifiers
    assert f"router:{str(router_id).lower()}" in member_access.identifiers
    assert str(first_model) not in member_access.identifiers

    # 3. APIM Global Policy locates start node dynamically by type == "start",
    #    enforces strict person/subscription router authorization without empty callerEmail bypass,
    #    and supports multi-step & anthropic-messages fallback.
    base_xml = (
        "<policies><inbound></inbound><backend><forward-request /></backend>"
        "<outbound></outbound><on-error></on-error></policies>"
    )
    policy_xml = build_router_global_service_policy(
        base_xml,
        ledger_table_endpoint="https://example-storage.table.core.windows.net",
        ledger_table_name="ExampleLedgerTable",
        api_id="example-llm-api",
    )
    assert (
        "FirstOrDefault(n =&gt; string.Equals((string)n[&quot;type&quot;], &quot;start&quot;"
        in policy_xml
    )
    assert "router_not_assigned" in policy_xml
    assert "needs_outbound_fallback" in policy_xml
    assert "allowed_subscription_ids" in policy_xml
    assert "hasPersonAuth" in policy_xml
    assert "rawTok.AsJwt() != null" in policy_xml
    assert "retry_anthropic" in policy_xml
    assert "count=\"3\"" in policy_xml

    # 3b. Unassigned user calling /api/v1/model-gateway/chat/completions with a published router
    #     gets 403 (never silently falls back to gpt-5.6-luna), and unknown model gets 404.
    unassigned_user = "outsider@contoso.com"
    app.dependency_overrides[require_authenticated_session] = lambda: identity(
        MEMBER_ID, unassigned_user, role="member"
    )
    try:
        unassigned_chat = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "UI Start Node Router",
                "messages": [{"role": "user", "content": "Should be 403"}],
            },
        )
        assert unassigned_chat.status_code == 403

        unknown_chat = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": "non-existent-model-or-router-xyz",
                "messages": [{"role": "user", "content": "Should be 404"}],
            },
        )
        assert unknown_chat.status_code == 404
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: identity(
            OWNER_ID, "owner@contoso.com"
        )

    # 4. Restricted application subscription key cannot bypass allowed_model_ids via router
    gateway_id = UUID(str(repository.gateways[0]["id"]))
    app_id = UUID("00000000-0000-4000-8000-000000000791")
    sub_id = UUID("00000000-0000-4000-8000-000000000792")
    now = datetime.now(UTC)
    repository.gateway_applications.append(
        {
            "id": app_id,
            "gateway_profile_id": gateway_id,
            "slug": "restricted-app",
            "display_name": "Restricted App",
            "description": None,
            "owner_id": None,
            "department_id": None,
            "application_type": "agent",
            "status": "active",
            "system_managed": False,
            "created_by": "owner@contoso.com",
            "created_at": now,
            "updated_by": "owner@contoso.com",
            "updated_at": now,
        }
    )
    repository.gateway_application_model_policies[app_id] = {
        "application_id": app_id,
        "updated_at": now,
        "updated_by": "owner@contoso.com",
    }
    repository.gateway_application_model_access[app_id] = {third_model}
    restricted_key = "sub-key-restricted-app-999"
    repository.gateway_application_subscriptions.append(
        {
            "id": sub_id,
            "application_id": app_id,
            "gateway_profile_id": gateway_id,
            "apim_subscription_id": "restricted-app",
            "display_name": "Restricted App",
            "scope_type": "api",
            "scope_id": "/apis/turnstile-llm",
            "state": "active",
            "scope_exists": True,
            "source": "discovered",
            "discovered_at": now,
            "last_synced_at": now,
            "primary_key": restricted_key,
        }
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_auth_store] = StubAuthStore
    app.dependency_overrides.pop(require_authenticated_session, None)
    client.cookies.clear()
    try:
        # /v1/models hides routers whose candidates are outside allowed_model_ids
        models_resp = client.get(
            "/v1/models",
            headers={"Ocp-Apim-Subscription-Key": restricted_key},
        )
        assert models_resp.status_code == 200
        router_ids = [
            item["router_id"]
            for item in models_resp.json()["data"]
            if item["type"] == "model_router"
        ]
        assert router_id not in router_ids

        # Calling the router with the restricted subscription key returns 403 model_not_allowed
        denied_chat = client.post(
            "/v1/chat/completions",
            headers={"Ocp-Apim-Subscription-Key": restricted_key},
            json={
                "model": "UI Start Node Router",
                "messages": [{"role": "user", "content": "Attempt restricted router"}],
            },
        )
        assert denied_chat.status_code == 403
        assert (
            denied_chat.json()["detail"]
            == "Model is not permitted for this application subscription"
        )

        # Subscription key has role="member" and cannot call owner-only management endpoints
        owner_only = client.post(
            "/api/v1/model-routers",
            headers={"Ocp-Apim-Subscription-Key": restricted_key},
            json={"name": "SubKey Created", "definition": ui_definition},
        )
        assert owner_only.status_code in {401, 403}
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: identity(
            OWNER_ID, "owner@contoso.com"
        )

    # 5. Trace sync paginates NextPartitionKey/NextRowKey, forwards price_context_tier,
    # and deletes synced rows
    deleted_row_keys: list[str] = []
    get_calls: list[str] = []

    class FakeTokenProvider:
        def token(self, scope: str = "https://storage.azure.com/") -> str:
            del scope
            return "fake-token"

    def page_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "GET" and "model-router-trace" in url_str:
            get_calls.append(url_str)
            if "NextRowKey=req-page-2" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "PartitionKey": ROUTER_TRACE_PARTITION,
                                "RowKey": "req-page-2",
                                "Document": json_mod.dumps(
                                    {
                                        "request_id": "req-page-2",
                                        "router_id": router_id,
                                        "router_name": "UI Start Node Router",
                                        "router_version": 1,
                                        "selected_model_id": str(first_model),
                                        "selected_model_name": "gpt-5.6-luna",
                                        "attempts": [],
                                        "usage_records": [
                                            {
                                                "request_id": "dec-page-2",
                                                "user_id": "owner@contoso.com",
                                                "model": "gpt-5.6-luna",
                                                "model_id": str(first_model),
                                                "input_tokens": 100,
                                                "output_tokens": 10,
                                                "estimated_cost": 0.000032,
                                                "input_price_per_million": 0.2,
                                                "output_price_per_million": 1.2,
                                                "price_context_tier": "short",
                                            }
                                        ],
                                    }
                                ),
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                headers={
                    "x-ms-continuation-NextPartitionKey": ROUTER_TRACE_PARTITION,
                    "x-ms-continuation-NextRowKey": "req-page-2",
                },
                json={
                    "value": [
                        {
                            "PartitionKey": ROUTER_TRACE_PARTITION,
                            "RowKey": "req-page-1",
                            "Document": json_mod.dumps(
                                {
                                    "request_id": "req-page-1",
                                    "router_id": router_id,
                                    "router_name": "UI Start Node Router",
                                    "router_version": 1,
                                    "selected_model_id": str(first_model),
                                    "selected_model_name": "gpt-5.6-luna",
                                    "attempts": [],
                                    "usage_records": [],
                                }
                            ),
                        }
                    ]
                },
            )
        if request.method == "DELETE" and "model-router-trace" in url_str:
            deleted_row_keys.append(url_str)
            return httpx.Response(204)
        return httpx.Response(200, json={"value": []})

    projector = RouterTableProjector(
        "https://example-storage.table.core.windows.net",
        "ExampleLedgerTable",
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )
    orig_client = httpx.Client

    def mock_client(*args: object, **kwargs: object) -> httpx.Client:
        del args, kwargs
        return orig_client(transport=httpx.MockTransport(page_handler))

    httpx.Client = mock_client  # type: ignore[misc,assignment]
    try:
        synced_count = projector.sync_traces_to_repository(repository)
    finally:
        httpx.Client = orig_client  # type: ignore[misc]

    assert synced_count == 2
    assert len(get_calls) == 2
    assert len(deleted_row_keys) == 2
    assert "req-page-1" in repository.model_router_invocations
    assert "req-page-2" in repository.model_router_invocations
    dec_usage = repository.get_usage_request("dec-page-2")
    assert dec_usage is not None
    assert dec_usage["price_context_tier"] == "short"


def test_router_security_audit_spoofing_failclosed_and_same_name_isolation(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as json_mod

    import httpx

    from backend.http.dependencies import get_repository
    from backend.http.session import get_auth_store
    from tests.platform.api.api_support import StubAuthStore
    from turnstile_core.domain.model_router import ModelRouter
    from turnstile_core.integrations.apim_router_global_policy import (
        build_router_global_service_policy,
    )
    from turnstile_core.integrations.router_table_projection import (
        RouterTableProjector,
        _resolve_allowed_subscription_ids_for_router,
        build_router_table_document,
        router_row_key,
    )

    repository, _ = router_repository
    enabled_models = [row for row in repository.models if row["enabled"]]
    first_model = UUID(str(enabled_models[0]["id"]))
    second_model = UUID(str(enabled_models[1]["id"]))
    third_model = UUID(str(enabled_models[2]["id"]))

    definition = {
        "schema_version": 2,
        "default_model_id": str(first_model),
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "position": {"x": 40, "y": 160},
            },
            {
                "id": "pool",
                "type": "candidate_models",
                "label": "Pool",
                "position": {"x": 240, "y": 160},
                "candidates": [{"model_id": str(first_model)}],
            },
            {
                "id": "strat",
                "type": "strategy",
                "label": "Priority",
                "position": {"x": 460, "y": 160},
                "strategy_type": "priority",
                "model_configs": [
                    {"model_id": str(first_model), "priority": 1, "weight": 100}
                ],
            },
            {
                "id": "out",
                "type": "output",
                "label": "Output",
                "position": {"x": 680, "y": 160},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "pool"},
            {"id": "e2", "source": "pool", "target": "strat"},
            {"id": "e3", "source": "strat", "target": "out"},
        ],
    }

    created_resp = client.post(
        "/api/v1/model-routers",
        json={"name": "Shared Team Router", "definition": definition},
    )
    assert created_resp.status_code == 201
    owner_router_id = UUID(created_resp.json()["id"])
    pub_resp = client.post(f"/api/v1/model-routers/{owner_router_id}/publish")
    assert pub_resp.status_code == 200
    owner_router = ModelRouter.model_validate(pub_resp.json())

    # 1. Audit #1: Client-supplied _router_authorized_id cannot bypass model policy
    repository.user_model_policies["restricted@contoso.com"] = {
        "user_id": "restricted@contoso.com",
        "model_ids": [third_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }
    app.dependency_overrides[require_authenticated_session] = lambda: identity(
        MEMBER_ID, "restricted@contoso.com", role="member"
    )
    try:
        spoof_chat = client.post(
            "/api/v1/model-gateway/chat/completions",
            json={
                "model": str(enabled_models[0]["model_key"]),
                "messages": [{"role": "user", "content": "Spoof attempt"}],
                "router_context": {
                    "_router_authorized_id": str(owner_router_id),
                    "subscription_key_authenticated": True,
                },
            },
        )
        assert spoof_chat.status_code == 403

        spoof_invoke = client.post(
            "/api/v1/model-gateway/invoke",
            json={
                "runtime_id": str(enabled_models[0]["runtime_id"]),
                "model_id": str(first_model),
                "messages": [{"role": "user", "content": "Spoof invoke attempt"}],
                "router_context": {
                    "_router_authorized_id": str(owner_router_id),
                    "subscription_key_authenticated": True,
                },
                "metadata": {
                    "organization_id": "org-1",
                    "organization": "Org 1",
                    "department_id": "dept-1",
                    "department": "Dept 1",
                    "project_id": "proj-1",
                    "project": "Proj 1",
                    "user_id": "restricted@contoso.com",
                    "user": "Restricted User",
                    "agent_id": "agent-1",
                    "agent": "Agent 1",
                    "workflow": "test",
                    "run_id": "run-spoof-1",
                    "turn_index": 1,
                    "model_id": str(first_model),
                    "model": str(enabled_models[0]["model_key"]),
                    "runtime": "foundry",
                    "request_source": "sdk",
                },
            },
        )
        assert spoof_invoke.status_code == 403
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: identity(
            OWNER_ID, "owner@contoso.com"
        )

    # 2. Audit #2: _resolve_allowed_subscription_ids_for_router fails closed on error,
    #    and discovered shared APIM applications (no user email owner) are supported.
    class BrokenAppsRepo:
        def list_gateway_applications(self) -> list[dict[str, object]]:
            raise RuntimeError("DB transient failure")

        def list_gateway_application_subscriptions(
            self, app_ids: object
        ) -> list[dict[str, object]]:
            del app_ids
            return []

    assert (
        _resolve_allowed_subscription_ids_for_router(
            owner_router, [], BrokenAppsRepo()
        )
        == []
    )
    built_doc = build_router_table_document(
        owner_router, repository.registry(), allowed_subscription_ids=None
    )
    assert built_doc is not None
    assert built_doc["allowed_subscription_ids"] == []

    policy_xml = build_router_global_service_policy(
        "<policies><inbound></inbound><backend><forward-request /></backend>"
        "<outbound></outbound><on-error></on-error></policies>",
        ledger_table_endpoint="https://example-storage.table.core.windows.net",
        ledger_table_name="ExampleLedgerTable",
        api_id="example-llm-api",
    )
    assert "allowedSubs == null" in policy_xml
    assert "AlternateDocuments" in policy_xml
    assert 'set-header name="Authorization" exists-action="delete"' in policy_xml

    # Shared discovered APIM app (owner_id=None, created_by=64-hex service actor)
    gateway_id = UUID(str(repository.gateways[0]["id"]))
    shared_app_id = UUID("00000000-0000-4000-8000-000000000891")
    shared_sub_id = UUID("00000000-0000-4000-8000-000000000892")
    shared_key = "sub-key-outline-shared-891"
    now = datetime.now(UTC)
    repository.gateway_applications.append(
        {
            "id": shared_app_id,
            "gateway_profile_id": gateway_id,
            "slug": "outline-assistant",
            "display_name": "Outline Assistant",
            "description": None,
            "owner_id": None,
            "department_id": None,
            "application_type": "agent",
            "status": "active",
            "system_managed": False,
            "created_by": "7f42edb5530de70da9b89a8149416e8709b38354cbd42803cb88875c24122024",
            "created_at": now,
            "updated_by": "7f42edb5530de70da9b89a8149416e8709b38354cbd42803cb88875c24122024",
            "updated_at": now,
        }
    )
    repository.gateway_application_model_policies[shared_app_id] = {
        "application_id": shared_app_id,
        "updated_at": now,
        "updated_by": "owner@contoso.com",
    }
    repository.gateway_application_model_access[shared_app_id] = {
        first_model,
        second_model,
    }
    repository.gateway_application_subscriptions.append(
        {
            "id": shared_sub_id,
            "application_id": shared_app_id,
            "gateway_profile_id": gateway_id,
            "apim_subscription_id": "outline-assistant",
            "display_name": "Outline Assistant",
            "scope_type": "api",
            "scope_id": "/apis/turnstile-llm",
            "state": "active",
            "scope_exists": True,
            "source": "discovered",
            "discovered_at": now,
            "last_synced_at": now,
            "primary_key": shared_key,
        }
    )
    resolved_subs = _resolve_allowed_subscription_ids_for_router(
        owner_router, [], repository
    )
    assert "outline-assistant" in resolved_subs

    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_auth_store] = StubAuthStore
    app.dependency_overrides.pop(require_authenticated_session, None)
    client.cookies.clear()
    try:
        shared_models = client.get(
            "/v1/models",
            headers={"Ocp-Apim-Subscription-Key": shared_key},
        )
        assert shared_models.status_code == 200
        shared_router_ids = [
            item["router_id"]
            for item in shared_models.json()["data"]
            if item["type"] == "model_router"
        ]
        assert str(owner_router_id) in shared_router_ids
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: identity(
            OWNER_ID, "owner@contoso.com"
        )

    # 3. Audit #3: Cross-owner same-name routers preserve and isolate Table entities
    second_owner_id = UUID("00000000-0000-4000-8000-000000000777")
    app.dependency_overrides[require_authenticated_session] = lambda: identity(
        second_owner_id, "second-owner@contoso.com"
    )
    try:
        second_created = client.post(
            "/api/v1/model-routers",
            json={
                "name": "Shared Team Router",
                "definition": {
                    **definition,
                    "default_model_id": str(second_model),
                    "nodes": [
                        {
                            "id": "start",
                            "type": "start",
                            "label": "Start",
                            "position": {"x": 40, "y": 160},
                        },
                        {
                            "id": "pool",
                            "type": "candidate_models",
                            "label": "Pool",
                            "position": {"x": 240, "y": 160},
                            "candidates": [{"model_id": str(second_model)}],
                        },
                        {
                            "id": "strat",
                            "type": "strategy",
                            "label": "Priority",
                            "position": {"x": 460, "y": 160},
                            "strategy_type": "priority",
                            "model_configs": [
                                {
                                    "model_id": str(second_model),
                                    "priority": 1,
                                    "weight": 100,
                                }
                            ],
                        },
                        {
                            "id": "out",
                            "type": "output",
                            "label": "Output",
                            "position": {"x": 680, "y": 160},
                        },
                    ],
                },
            },
        )
        assert second_created.status_code == 201
        second_router_id = UUID(second_created.json()["id"])
        second_pub = client.post(f"/api/v1/model-routers/{second_router_id}/publish")
        assert second_pub.status_code == 200
        second_router = ModelRouter.model_validate(second_pub.json())
    finally:
        app.dependency_overrides[require_authenticated_session] = lambda: identity(
            OWNER_ID, "owner@contoso.com"
        )

    table_store: dict[str, dict[str, object]] = {}
    table_etags: dict[str, str] = {}
    etag_seq = 0
    simulate_503_on_name_get = False
    stale_etag_remaining = 0
    recorded_name_writes: list[tuple[str, str]] = []
    shared_rk = router_row_key("Shared Team Router")

    class FakeTokenProvider:
        def token(self, scope: str = "https://storage.azure.com/") -> str:
            del scope
            return "fake-token"

    def table_handler(request: httpx.Request) -> httpx.Response:
        nonlocal etag_seq, stale_etag_remaining
        from urllib.parse import unquote

        if request.url.path == "/$batch":
            from email import policy
            from email.parser import BytesParser

            from tests.backend.model_platform.test_storage_route_projection import batch_result

            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + request.headers["Content-Type"].encode()
                + b"\r\n\r\n" + request.content,
            )
            before_store, before_etags = table_store.copy(), table_etags.copy()
            operations = 0
            for part in message.walk():
                if part.get_content_type() != "application/http":
                    continue
                raw = part.get_payload(decode=True)
                assert isinstance(raw, bytes)
                head, body = raw.split(b"\r\n\r\n", 1)
                first, headers = head.split(b"\r\n", 1)
                method, address, _ = first.decode().split()
                parsed_headers = BytesParser(policy=policy.default).parsebytes(headers)
                result = table_handler(httpx.Request(
                    method, address, headers=dict(parsed_headers.items()), content=body,
                ))
                if result.status_code >= 400:
                    table_store.clear()
                    table_store.update(before_store)
                    table_etags.clear()
                    table_etags.update(before_etags)
                    return batch_result(result.status_code)
                operations += 1
            return batch_result(operations=operations)
        url_str = unquote(str(request.url))
        rk = ""
        if "RowKey='" in url_str:
            rk = url_str.split("RowKey='", 1)[1].split("'", 1)[0]
        if request.method == "GET":
            if rk == shared_rk and simulate_503_on_name_get:
                return httpx.Response(503, json={"error": "ServiceUnavailable"})
            if rk:
                ent = table_store.get(rk)
                if ent is None:
                    return httpx.Response(404)
                returned_etag = table_etags.get(rk, 'W/"1"')
                if rk == shared_rk and stale_etag_remaining > 0:
                    returned_etag = 'W/"stale-etag"'
                    stale_etag_remaining -= 1
                return httpx.Response(
                    200,
                    headers={"ETag": returned_etag},
                    json={**ent, "odata.etag": returned_etag},
                )
            return httpx.Response(200, json={"value": list(table_store.values())})
        if request.method == "POST":
            payload = json_mod.loads(request.content.decode("utf-8"))
            post_rk = str(payload.get("RowKey") or "")
            if post_rk == shared_rk:
                recorded_name_writes.append(("POST", post_rk))
            if post_rk in table_store:
                return httpx.Response(409, json={"error": "EntityAlreadyExists"})
            etag_seq += 1
            table_store[post_rk] = payload
            table_etags[post_rk] = f'W/"{etag_seq}"'
            return httpx.Response(
                201, headers={"ETag": table_etags[post_rk]}, json=payload
            )
        if request.method == "PUT" and rk:
            if rk == shared_rk:
                recorded_name_writes.append(("PUT", rk))
            if_match = request.headers.get("If-Match")
            if (
                if_match
                and if_match != "*"
                and table_etags.get(rk) != if_match
            ):
                return httpx.Response(412, json={"error": "UpdateConditionNotSatisfied"})
            etag_seq += 1
            table_store[rk] = json_mod.loads(request.content.decode("utf-8"))
            table_etags[rk] = f'W/"{etag_seq}"'
            return httpx.Response(204, headers={"ETag": table_etags[rk]})
        if request.method == "DELETE" and rk:
            if rk == shared_rk:
                recorded_name_writes.append(("DELETE", rk))
            if_match = request.headers.get("If-Match")
            if (
                if_match
                and if_match != "*"
                and rk in table_store
                and table_etags.get(rk) != if_match
            ):
                return httpx.Response(412, json={"error": "UpdateConditionNotSatisfied"})
            table_store.pop(rk, None)
            table_etags.pop(rk, None)
            return httpx.Response(204)
        return httpx.Response(200)

    projector = RouterTableProjector(
        "https://example-storage.table.core.windows.net",
        "ExampleLedgerTable",
        token_provider=FakeTokenProvider(),  # type: ignore[arg-type]
    )
    orig_client = httpx.Client

    def mock_table_client(*args: object, **kwargs: object) -> httpx.Client:
        del args, kwargs
        return orig_client(transport=httpx.MockTransport(table_handler))

    httpx.Client = mock_table_client  # type: ignore[misc,assignment]
    try:
        assert projector.upsert_router(owner_router, repository.registry())
        assert projector.upsert_router(second_router, repository.registry())
        assert shared_rk in table_store
        shared_entity = table_store[shared_rk]
        assert "AlternateDocuments" in shared_entity
        alt_docs = json_mod.loads(str(shared_entity["AlternateDocuments"]))
        assert len(alt_docs) == 1
        assert alt_docs[0]["router_id"] == str(owner_router.id)

        # Concurrency / stale ETag check:
        # 1) A single stale ETag (412) triggers a re-GET and merges cleanly on the 2nd attempt
        stale_etag_remaining = 1
        assert projector.upsert_router(second_router, repository.registry())
        assert table_store[shared_rk]["RouterId"] == str(second_router.id)

        # 2) If all 3 attempts hit stale ETag (412), PUT fails with 412 and does not overwrite
        stale_etag_remaining = 3
        from turnstile_core.integrations.table_batch import TableBatchConflict

        with pytest.raises(TableBatchConflict):
            projector.upsert_router(owner_router, repository.registry())
        assert table_store[shared_rk]["RouterId"] == str(second_router.id)

        # Transient 503 on name_rk GET: neither upsert_router nor delete_router may blind-PUT/DELETE
        recorded_name_writes.clear()
        simulate_503_on_name_get = True
        try:
            with pytest.raises(httpx.HTTPStatusError) as up_503_exc:
                projector.upsert_router(owner_router, repository.registry())
            assert up_503_exc.value.response.status_code == 503
            with pytest.raises(httpx.HTTPStatusError) as del_503_exc:
                projector.delete_router(second_router.id, "Shared Team Router")
            assert del_503_exc.value.response.status_code == 503
            assert recorded_name_writes == []
            assert shared_rk in table_store
        finally:
            simulate_503_on_name_get = False

        # Deleting a third owner's unpublished draft with the same name must not touch the entity
        projector.delete_router(
            UUID("00000000-0000-4000-8000-000000000999"), "Shared Team Router"
        )
        assert shared_rk in table_store
        assert "AlternateDocuments" in table_store[shared_rk]

        # Deleting second_router promotes owner_router back to primary Document
        projector.delete_router(second_router.id, "Shared Team Router")
        assert shared_rk in table_store
        restored_entity = table_store[shared_rk]
        assert restored_entity["RouterId"] == str(owner_router.id)
        restored_doc = json_mod.loads(str(restored_entity["Document"]))
        assert restored_doc["router_id"] == str(owner_router.id)
        assert "AlternateDocuments" not in restored_entity
        assert router_row_key(str(second_router.id)) not in table_store
        assert router_row_key(str(owner_router.id)) in table_store
    finally:
        httpx.Client = orig_client  # type: ignore[misc]

    # 8. Restricted user calling POST /api/v1/model-gateway/decision with forged
    #    metadata.workflow = "model-router-decision" is rejected with 403
    decision_model = next(
        row for row in repository.models if row["id"] == second_model
    )
    orig_caps = list(decision_model.get("capabilities") or [])
    decision_model["capabilities"] = ["decision"]
    restricted_decision_user = "restricted.decision@contoso.com"
    repository.user_model_policies[restricted_decision_user] = {
        "user_id": restricted_decision_user,
        "model_ids": [first_model],
        "updated_at": datetime.now(UTC),
        "updated_by": "owner@contoso.com",
    }
    app.dependency_overrides[require_authenticated_session] = lambda: identity(
        MEMBER_ID, restricted_decision_user, role="member"
    )
    try:
        forged_decision = client.post(
            "/api/v1/model-gateway/decision",
            json={
                "metadata": {
                    "organization_id": "org-contoso-global",
                    "organization": "Contoso Global",
                    "department_id": "department-platform",
                    "department": "AI Platform",
                    "project_id": "project-finops",
                    "project": "Model FinOps",
                    "agent_id": "agent-console",
                    "agent": "Invocation Console",
                    "user_id": restricted_decision_user,
                    "user": "restricted.decision",
                    "workflow": "model-router-decision",
                    "model_id": str(decision_model["id"]),
                    "model": str(decision_model["model_key"]),
                    "runtime": "Microsoft Foundry",
                    "request_source": "forged-test",
                    "run_id": "forged-decision-1",
                    "turn_index": 1,
                },
                "model_id": str(decision_model["id"]),
                "state": "test prompt",
                "questions": {
                    "route": {
                        "type": "choice",
                        "instructions": "Select one",
                        "criteria": {"a": "Option A", "b": "Option B"},
                    }
                },
            },
        )
        assert forged_decision.status_code == 403
        assert forged_decision.json()["detail"] == "model_not_assigned"

        # Meanwhile, in-process editor_trial_authorized=True passes _assert_model_allowed
        # for the same user with user_model_policies when running decision-trial in the editor
        from backend.services.runtime_service import ModelRuntimeService
        from turnstile_core.config import get_settings
        from turnstile_core.domain.runtime_models import (
            ChoiceQuestion,
            DecisionInvocationRequest,
            InvocationMetadata,
        )

        rt_svc = ModelRuntimeService(repository, get_settings())
        trial_req = DecisionInvocationRequest(
            metadata=InvocationMetadata(
                organization_id="unattributed",
                organization="unattributed",
                department_id="unattributed",
                department="unattributed",
                project_id="unattributed",
                project="unattributed",
                agent_id="model-router-editor",
                agent="Model Router Editor",
                user_id=restricted_decision_user,
                user="restricted.decision",
                workflow="model-router-decision-trial",
                model_id=str(decision_model["id"]),
                model=str(decision_model["model_key"]),
                runtime="Microsoft Foundry",
                request_source="model-router-trial",
                run_id="trial-1",
                turn_index=1,
            ),
            model_id=second_model,
            state="test prompt",
            questions={
                "route": ChoiceQuestion(
                    type="choice",
                    instructions="Select one",
                    criteria={str(first_model): "Option A", "__none__": "Option B"},
                )
            },
        )
        rt_svc._assert_model_allowed(
            trial_req,
            {"model_id": second_model, "model_key": decision_model["model_key"]},
            "trial-req-1",
            editor_trial_authorized=True,
        )
    finally:
        decision_model["capabilities"] = orig_caps
        app.dependency_overrides[require_authenticated_session] = lambda: identity(
            OWNER_ID, "owner@contoso.com"
        )

    # 9. APIM policy preserves caller max_tokens, translates OpenAI <-> Anthropic
    #    tools & images, and binds timeout_ms / max_latency_ms
    assert "requestedMaxTok &gt; 0 ? requestedMaxTok : 1024" in policy_xml
    assert "tool_result" in policy_xml
    assert "tool_use" in policy_xml
    assert "tool_calls" in policy_xml
    assert "input_schema" in policy_xml
    assert "image_url" in policy_xml
    assert "hasImageBlocks" in policy_xml
    assert "max_latency_ms" in policy_xml
    assert (
        'response-variable-name="routerAnthropicHttp" '
        'timeout="@(Math.Max(1, Math.Min(120, (Math.Min(' in policy_xml
    )
    assert (
        'response-variable-name="routerFallbackHttp" '
        'timeout="@(Math.Max(1, Math.Min(120, (Math.Min(' in policy_xml
    )

    # 10. Trace sync does NOT delete Table trace entity if write_token_usage fails
    trace_deletes: list[str] = []
    trace_doc = json_mod.dumps(
        {
            "request_id": "req-fail-usage-1",
            "router_id": str(owner_router.id),
            "router_name": owner_router.name,
            "router_version": 1,
            "selected_model_id": str(first_model),
            "selected_model_name": "gpt-5.6-luna",
            "attempts": [],
            "usage_records": [
                {
                    "request_id": "dec-sub-usage-1",
                    "model": "gpt-5.6-luna",
                    "model_id": str(first_model),
                    "input_tokens": 12,
                    "output_tokens": 4,
                }
            ],
        }
    )

    def trace_fail_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if "RowKey='req-fail-usage-1'" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "PartitionKey": "model-router-trace|v1",
                        "RowKey": "req-fail-usage-1",
                        "Document": trace_doc,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "PartitionKey": "model-router-trace|v1",
                            "RowKey": "req-fail-usage-1",
                            "Document": trace_doc,
                        }
                    ]
                },
            )
        if request.method == "DELETE":
            trace_deletes.append(str(request.url))
            return httpx.Response(204)
        return httpx.Response(200)

    orig_write_usage = repository.write_token_usage

    failing_write_usage = create_autospec(
        orig_write_usage, side_effect=RuntimeError("Simulated DB usage write failure"),
    )
    monkeypatch.setattr(repository, "write_token_usage", failing_write_usage)
    httpx.Client = lambda *a, **kw: orig_client(  # type: ignore[misc,assignment]
        transport=httpx.MockTransport(trace_fail_handler)
    )
    try:
        assert not projector.sync_single_trace_to_repository(
            "req-fail-usage-1", repository
        )
        assert projector.sync_traces_to_repository(repository) == 0
        assert trace_deletes == []
        failing_write_usage.assert_called()
    finally:
        monkeypatch.setattr(repository, "write_token_usage", orig_write_usage)
        httpx.Client = orig_client  # type: ignore[misc]



def _branch_router_turns(
    code_model: UUID, general_model: UUID, turns: list[tuple[str, list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[UUID]]:
    """Send the turns to a code/general condition Router in one session."""
    def branch(name: str, model: UUID, y: int) -> list[dict[str, Any]]:
        return [
            {"id": f"pool-{name}", "type": "candidate_models", "label": name,
             "position": {"x": 420, "y": y}, "candidates": [{"model_id": str(model)}]},
            {"id": f"strat-{name}", "type": "strategy", "label": name,
             "position": {"x": 620, "y": y}, "strategy_type": "priority",
             "model_configs": [{"model_id": str(model), "priority": 1, "weight": 100}]},
        ]

    definition = {
        "schema_version": 2,
        "nodes": [
            {"id": "start", "type": "start", "label": "Start", "position": {"x": 40, "y": 160}},
            {"id": "cond-task", "type": "condition", "label": "Task",
             "position": {"x": 220, "y": 160},
             "condition": {"field": "task_type", "operator": "equals", "value": "code"}},
            *branch("code", code_model, 80), *branch("general", general_model, 240),
            {"id": "output", "type": "output", "label": "Output", "position": {"x": 840, "y": 160}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "cond-task"},
            {"id": "e2", "source": "cond-task", "target": "pool-code", "label": "Match"},
            {"id": "e3", "source": "cond-task", "target": "pool-general", "label": "Else"},
            {"id": "e4", "source": "pool-code", "target": "strat-code"},
            {"id": "e5", "source": "pool-general", "target": "strat-general"},
            {"id": "e6", "source": "strat-code", "target": "output"},
            {"id": "e7", "source": "strat-general", "target": "output"},
        ],
    }
    served: list[UUID] = []

    class EchoRuntimeService:
        def invoke(
            self, request: ModelInvocationRequest, *, request_id: str | None = None,
            timeout_ms: int | None = None,
        ) -> ModelInvocationResponse:
            del timeout_ms
            assert request.model_id is not None
            served.append(request.model_id)
            return ModelInvocationResponse(
                request_id=request_id or "req", correlation_id=request_id or "req",
                content="OK", provider="Microsoft Foundry", runtime=request.metadata.runtime,
                model=request.metadata.model, gateway="Azure API Management", latency_ms=5,
                usage=None,
            )

    app.dependency_overrides[runtime_service] = EchoRuntimeService
    try:
        router_id = client.post(
            "/api/v1/model-routers", json={"name": "Branch Router", "definition": definition},
        ).json()["id"]
        assert client.post(f"/api/v1/model-routers/{router_id}/publish").status_code == 200
        assert client.put(
            f"/api/v1/model-routers/{router_id}/assignment",
            json={"user_id": "owner@contoso.com", "assigned": True},
        ).status_code == 200
        routings = []
        for task_type, messages in turns:
            response = client.post(
                "/api/v1/model-gateway/chat/completions",
                headers={"x-session-id": "sess-branch"},
                json={"model": "Branch Router", "messages": messages,
                      "router_context": {"task_type": task_type}},
            )
            assert response.status_code == 200
            routings.append(dict(response.json()["turnstile_routing"]))
    finally:
        app.dependency_overrides.pop(runtime_service, None)
    return routings, served


def _branch_user_turn(multi_turn: bool) -> list[dict[str, Any]]:
    earlier = [{"role": "user", "content": "Summarize the news"},
               {"role": "assistant", "content": "Here is the summary."}] if multi_turn else []
    return [*earlier, {"role": "user", "content": "Refactor queue.ts"}]


@pytest.mark.parametrize("multi_turn", [False, True])
def test_condition_branch_takes_precedence_over_a_sticky_model_outside_it(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    multi_turn: bool,
) -> None:
    repository, _ = router_repository
    # Index 0 is the cheaper model, so the code branch is an upgrade from the sticky model.
    general_model, code_model = enabled_model_ids(repository)[:2]
    (general, continuation, code), served = _branch_router_turns(code_model, general_model, [
        ("general", [{"role": "user", "content": "Summarize the news"}]),
        ("code", [
            {"role": "user", "content": "Summarize the news"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "c1", "content": "headlines"},
        ]),
        ("code", _branch_user_turn(multi_turn)),
    ])
    assert general["selected_model_id"] == str(general_model)
    # A tool continuation keeps the sticky model even though a pricier branch matched.
    assert continuation["selected_model_id"] == str(general_model)
    # A user turn that matches a branch with a pricier model is served by that branch.
    assert code["selected_model_id"] == str(code_model)
    assert code["decision_basis"] not in {"session_sticky", "interaction_sticky"}
    assert served == [general_model, general_model, code_model]


@pytest.mark.parametrize("multi_turn", [False, True])
def test_user_turns_keep_the_sticky_model_when_the_branch_is_cheaper(
    router_repository: tuple[InMemoryRepository, dict[str, SessionIdentity]],
    multi_turn: bool,
) -> None:
    repository, _ = router_repository
    code_model, general_model = enabled_model_ids(repository)[:2]
    (general, code), served = _branch_router_turns(code_model, general_model, [
        ("general", [{"role": "user", "content": "Summarize the news"}]),
        ("code", _branch_user_turn(multi_turn)),
    ])
    assert general["selected_model_id"] == str(general_model)
    assert code["selected_model_id"] == str(general_model)
    assert code["decision_basis"] == "session_sticky"
    assert served == [general_model, general_model]


@pytest.mark.parametrize("prompt", [
    "Pick the cheaper model.\n{catalog}", "Answer with model_id only.",
])
def test_custom_decision_prompt_always_asks_for_json(prompt: str) -> None:
    system, is_default, _ = ModelRouterExecutionService._render_decision_prompt(
        prompt, [{"model_id": "unit", "display_name": "Unit", "provider_name": "Unit",
                  "description": "", "input_cost_per_million": 1.0,
                  "output_cost_per_million": 2.0}],
    )
    assert is_default is False
    assert system.startswith(prompt.split("\n")[0])
    # json_object responses are rejected unless the messages mention JSON.
    assert "json" in system.casefold()
