from __future__ import annotations

import statistics
import time
from typing import Any
from uuid import uuid4

from tests.backend.persistence.test_directory_migration import (
    directory_database as directory_database,
)
from tests.backend.persistence.test_directory_migration import local_postgres as local_postgres
from turnstile_core.domain.directory import DirectoryPrincipal
from turnstile_core.persistence.directory_store import DirectoryStore
from turnstile_core.services.directory_service import DirectoryService


def test_directory_search_and_scoped_page_at_required_scale(
    directory_database: str,
    record_property: Any,
) -> None:
    store = DirectoryStore(directory_database)
    store.initialize_empty()
    try:
        with store.connection() as connection:
            connection.execute(
                """INSERT INTO directory_organization(id,code,name,updated_by)
                   SELECT 'org-scale-'||n,'scale-'||n,'Scale Organization '||n,'scale-fixture'
                   FROM generate_series(0,9) n;
                   INSERT INTO directory_unit(id,organization_id,kind,code,name,updated_by)
                   SELECT 'department-scale-'||n,'org-scale-'||(n/100),'department',
                     'department-'||n,'Scale Department '||n,'scale-fixture'
                   FROM generate_series(0,999) n;
                   INSERT INTO directory_person(
                     id,governance_user_id,display_name,employee_number,updated_by)
                   SELECT md5('scale-person-'||n)::uuid,'scale-'||n||'@example.com',
                     'Scale Person '||n,n::text,'scale-fixture' FROM generate_series(0,99999) n;
                   INSERT INTO directory_membership(
                     person_id,unit_id,organization_id,membership_kind)
                   SELECT md5('scale-person-'||n)::uuid,'department-scale-'||(n%1000),
                     'org-scale-'||((n%1000)/100),'primary_department'
                   FROM generate_series(0,99999) n;
                   ANALYZE directory_person; ANALYZE directory_membership;
                   ANALYZE directory_unit;"""
            )
        service = DirectoryService(store)
        owner = DirectoryPrincipal(
            account_id=uuid4(), email="scale-owner@example.com", role="owner"
        )
        member = DirectoryPrincipal(
            account_id=uuid4(),
            email="scale-member@example.com",
            role="member",
            department_ids=("department-scale-0",),
            capabilities=("directory.read",),
        )
        checks: list[tuple[DirectoryPrincipal, dict[str, Any], int]] = [
            (owner, {}, 100_000),
            (owner, {"query": "scale-98765@example.com"}, 1),
            (member, {}, 100),
        ]
        timings = []
        for principal, filters, expected in checks:
            for _ in range(5):
                started = time.perf_counter()
                page = service.people(principal, **filters)
                timings.append(time.perf_counter() - started)
                assert page.total == expected
                assert len(page.items) == min(expected, 50)
                if principal is member:
                    assert all(
                        person["department_id"] == "department-scale-0" for person in page.items
                    )
        p95 = statistics.quantiles(timings, n=20, method="inclusive")[18]
        record_property("directory_local_p95_seconds", round(p95, 4))
        assert p95 < 1.0, f"Local 100k directory list/search p95 exceeded 1 second: {p95:.3f}s"
    finally:
        store.close()
