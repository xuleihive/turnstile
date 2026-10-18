from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import psycopg
import pytest

from backend.services.auth_service import SessionIdentity
from backend.services.budget_service import TokenBudgetService
from backend.services.user_settings_service import UserSettingsService
from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from tests.platform.api.api_support import _usage_record
from turnstile_core.config import Settings
from turnstile_core.domain.data_access import DataAccessScope
from turnstile_core.persistence.repository import PostgreSqlOpsDbProxy


def test_execution_period_uses_persistent_people_not_the_legacy_demo_directory(
    directory_database: str,
) -> None:
    epoch = uuid4()
    period = datetime.now(UTC).date().replace(day=1)
    with psycopg.connect(directory_database) as db:
        db.execute("UPDATE directory_control_state SET source='database', phase='active'")
        db.execute(
            "INSERT INTO directory_organization(id,code,name,updated_by) "
            "VALUES('custom-org','custom-org','Custom Organization','test')",
        )
        for identity, status in (
            ("custom@example.com", "active"), ("past@example.com", "archived"),
        ):
            db.execute(
                "INSERT INTO directory_person(governance_user_id,display_name,status,"
                "organization_id,updated_by) VALUES(%s,%s,%s,'custom-org','test')",
                (identity, identity, status),
            )
        db.execute(
            "INSERT INTO budget_execution_rollout(epoch,state,gateway_profile_ids,"
            "admitted_after,legacy_fence_verified_at,scope_sync_enabled) "
            "VALUES(%s,'active',%s,clock_timestamp(),clock_timestamp(),TRUE)",
            (epoch, [uuid4()]),
        )
    repo = PostgreSqlOpsDbProxy(directory_database)
    try:
        repo.prepare_budget_execution_period(epoch, period, ["demo-only@example.com"])
        with psycopg.connect(directory_database) as db:
            people = {row[0] for row in db.execute(
                "SELECT scope_id FROM budget_execution_scope WHERE scope_type='person'",
            )}
            assert "custom@example.com" in people
            assert not people & {"demo-only@example.com", "past@example.com"}
            # Preparation creates disabled scope seeds, not accounts, budgets, or consumption.
            assert db.execute("SELECT count(*) FROM app_user").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM token_budget").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM token_usage").fetchone() == (0,)
    finally:
        repo.close()


@pytest.mark.parametrize("canonical", [False, True])
def test_exact_personal_attempts_are_included_in_department_people_without_rewriting_history(
    directory_database: str,
    canonical: bool,
) -> None:
    person = "test.user01@contoso.com"
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    department, organization = "department-platform", "org-contoso-global"
    attempt_id = uuid4()
    with psycopg.connect(directory_database) as db:
        for scope_kind, key, parent in (
            ("organization", organization, None),
            ("department", department, organization),
            ("user", person, department),
        ):
            db.execute(
                "INSERT INTO token_budget(period_start,scope_type,scope_id,parent_scope_id,"
                "token_limit,updated_by) VALUES(%s,%s,%s,%s,60000000,'test')",
                (start, scope_kind, key, parent),
            )
        for index, (key, period, state, amount) in enumerate(
            (
                (person, start, "exact", 2579),
                (person, start, "uncertain", None),
                ("other@example.com", start, "exact", 900),
                (person, end, "exact", 700),
            )
        ):
            db.execute(
                "INSERT INTO billable_request_attempt(id,operation_key,plan_sha256,scope_type,"
                "scope_id,period_start,model_id,model_key,reserved_tokens,attempt_index,"
                "state,actual_tokens,created_at,correlation_id) "
                "VALUES(%s,%s,%s,'person',%s,%s,'fixture-model','fixture',10000,1,%s,%s,%s,%s)",
                (
                    attempt_id if index == 0 else uuid4(),
                    f"budget-consistency-{index}",
                    "a" * 64,
                    key,
                    period,
                    state,
                    amount,
                    datetime(period.year, period.month, 1, tzinfo=UTC),
                    f"budget-consistency-{index}",
                ),
            )

    def snapshot() -> dict[str, list[tuple[object, ...]]]:
        with psycopg.connect(directory_database) as db:
            return {
                table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in (
                    "token_usage",
                    "token_budget",
                    "billable_request_attempt",
                    "schema_migration",
                )
            }

    repo = PostgreSqlOpsDbProxy(directory_database)
    identity = SessionIdentity(
        id=str(uuid4()),
        name="Edwin",
        email=person,
        role="owner",
        method="password",
        session_expires_at=datetime.now(UTC),
    )
    try:
        if canonical:
            record = _usage_record("canonical-budget", "Foundry via APIM", 2579).model_copy(
                update={
                    "ts": datetime(2026, 10, 1, tzinfo=UTC),
                    "request_id": str(attempt_id),
                    "correlation_id": "budget-consistency-0",
                    "model_id": "fixture-model",
                }
            )
            repo.write_token_usage(record)
        before = snapshot()
        personal_state = repo.person_budget_state(person, start, end)
        assert personal_state is not None and personal_state["used_tokens"] == 2579
        settings = Settings()
        assert UserSettingsService(repo, settings).budget(identity, "2026-10").used_tokens == 2579
        service = TokenBudgetService(repo)
        for scope in (None, DataAccessScope(department_ids=(department,))):
            people = service.people("2026-10", department, None, "all", 0, 50, data_scope=scope)
            row = next(x for x in people.items if x.scope_id == person)
            assert row.used_tokens == 2579
        totals = repo.token_usage_by_budget_scope(
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 11, 1, tzinfo=UTC),
            allowed_department_ids=(department,),
        )
        assert {(x["scope_type"], x["scope_id"]): x["used_tokens"] for x in totals} == {
            ("organization", organization): 2579,
            ("department", department): 2579,
            ("user", person): 2579,
        }
    finally:
        repo.close()
    assert snapshot() == before


def test_people_budget_counts_the_persons_whole_month_not_only_department_headers(
    directory_database: str,
) -> None:
    person, department = "test.user01@contoso.com", "department-platform"
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    with psycopg.connect(directory_database) as db:
        for kind, key, parent in (
            ("organization", "org-contoso-global", None),
            ("department", department, "org-contoso-global"),
            ("user", person, department),
        ):
            db.execute(
                "INSERT INTO token_budget(period_start,scope_type,scope_id,parent_scope_id,"
                "token_limit,updated_by) VALUES(%s,%s,%s,%s,60000000,'test')",
                (start, kind, key, parent),
            )
        db.execute(
            "INSERT INTO billable_request_attempt(id,operation_key,plan_sha256,scope_type,"
            "scope_id,period_start,model_id,model_key,reserved_tokens,attempt_index,"
            "state,actual_tokens,created_at) "
            "VALUES(%s,'legacy-exact',%s,'person',%s,%s,'fixture','fixture',1000,1,'exact',492,%s)",
            (uuid4(), "a" * 64, person, start, datetime(2026, 10, 1, tzinfo=UTC)),
        )
    repo = PostgreSqlOpsDbProxy(directory_database)
    try:
        for index, (dept, amount) in enumerate(
            ((department, 19976), ("unattributed", 1847), ("protocol-validation", 240))
        ):
            repo.write_token_usage(
                _usage_record(f"history-{index}", "APIM", amount).model_copy(
                    update={"ts": datetime(2026, 10, 1, tzinfo=UTC), "department_id": dept},
                )
            )
        with psycopg.connect(directory_database) as db:
            before = db.execute("SELECT to_jsonb(u) FROM token_usage u ORDER BY id").fetchall()
        state = repo.person_budget_state(person, start, end)
        assert state is not None and state["used_tokens"] == 22555
        for scope in (
            None,
            DataAccessScope(department_ids=(department,)),
            DataAccessScope(user_ids=(person,)),
        ):
            people = TokenBudgetService(repo).people(
                "2026-10", department, None, "all", 0, 50, data_scope=scope
            )
            assert next(x for x in people.items if x.scope_id == person).used_tokens == 22555
        with psycopg.connect(directory_database) as db:
            assert (
                db.execute("SELECT to_jsonb(u) FROM token_usage u ORDER BY id").fetchall() == before
            )
            assert db.execute(
                "SELECT department_id FROM token_usage WHERE id='history-2'"
            ).fetchone() == ("protocol-validation",)
    finally:
        repo.close()
