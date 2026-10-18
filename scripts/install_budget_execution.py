"""Journaled gates for adopting an existing installation's budget balances.

This is an operator library, not a startup/bootstrap hook. Cloud adapters must
obtain fresh readbacks while holding the reviewed shared maintenance fence.
The CLI checks a saved plan only; it never treats a JSON file as cloud proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
from xml.etree import ElementTree as ET

from pydantic import AwareDatetime, Field, model_validator

from scripts.budget_policy_bindings import bind_policy_parameters
from scripts.prepare_budget_execution import CutoverReadback, FenceConfiguration
from turnstile_core.domain.models import StrictModel
from turnstile_core.integrations.apim_budget_execution import budget_execution_fence


def materialize_shared_fence(api_ids: Sequence[str], bindings: Mapping[str, str]) -> ET.Element:
    """Bind the shared deny event too, not just the Table request's URL."""
    return bind_policy_parameters(budget_execution_fence(api_ids), bindings)


class BudgetInstallationPlan(StrictModel):
    version: Literal[1] = 1
    epoch: UUID
    gateway_profile_ids: list[UUID] = Field(min_length=1)
    period_start: AwareDatetime
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_fence: FenceConfiguration
    minimum_drain_seconds: int = Field(default=600, ge=360, le=3600, strict=True)
    package_sha256: dict[str, str] = Field(min_length=4)
    policy_sha256: dict[str, str] = Field(min_length=2)
    backup_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_plan(self) -> BudgetInstallationPlan:
        if len(set(self.gateway_profile_ids)) != len(self.gateway_profile_ids):
            raise ValueError("Installation cannot repeat a Gateway")
        if (self.expected_fence.mode != "paused" or self.expected_fence.epoch != self.epoch
            or self.expected_fence.gateway_profile_id not in self.gateway_profile_ids):
            raise ValueError("The plan must bind the exact paused fence and delegated identity")
        if self.expected_fence.delegation is None:
            raise ValueError("Personal USD installation requires the reviewed backend identity")
        period = self.period_start.astimezone(UTC)
        if period.day != 1 or any((period.hour, period.minute, period.second, period.microsecond)):
            raise ValueError("Installation requires the exact UTC budget period boundary")
        self.period_start = period
        if not timedelta(0) < self.expires_at - self.created_at <= timedelta(hours=24):
            raise ValueError("An installation plan must expire within 24 hours")
        if set(self.package_sha256) != {"api", "telemetry", "control-plane", "observer"}:
            raise ValueError("All four matching runtime artifacts are required")
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in [*self.package_sha256.values(), *self.policy_sha256.values()]
        ):
            raise ValueError("Every artifact must have an exact SHA256")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def require_current(self, now: datetime) -> None:
        if not self.created_at <= now < self.expires_at:
            raise ValueError("Installation plan is not current")
        if now.astimezone(UTC).date().replace(day=1) != self.period_start.date():
            raise ValueError("Installation cannot cross a budget period boundary")


class MaintenanceReadback(StrictModel):
    observed_at: AwareDatetime
    fence_etag: str = Field(min_length=1)
    fence: FenceConfiguration
    revision_policy_sha256: dict[str, str] = Field(min_length=1)
    reachable_revisions: list[str] = Field(min_length=1)
    fenced_revisions: list[str] = Field(min_length=1)
    package_sha256: dict[str, str] = Field(min_length=4)
    pending_publications: int = Field(ge=0, strict=True)
    active_dispatches: int = Field(ge=0, strict=True)
    pending_telemetry: int = Field(ge=0, strict=True)
    pending_settlements: int = Field(ge=0, strict=True)
    admission_fenced_at: AwareDatetime
    drain_completed_at: AwareDatetime
    balances: CutoverReadback

    def require_paused(self, plan: BudgetInstallationPlan, now: datetime) -> None:
        plan.require_current(now)
        if not 0 <= (now - self.observed_at).total_seconds() <= 30:
            raise ValueError("Maintenance observation must be no older than 30 seconds")
        if self.fence_etag == "*":
            raise ValueError("A wildcard is not a read-back fence ETag")
        if self.fence.mode != "paused" or self.fence.epoch != plan.epoch:
            raise ValueError("Initialization requires the matching shared paused fence")
        if self.fence != plan.expected_fence:
            raise ValueError("The shared fence identity or counting contracts changed")
        if self.fence.gateway_profile_id not in plan.gateway_profile_ids:
            raise ValueError("The observed fence belongs to a different Gateway")
        revisions = set(self.reachable_revisions)
        if len(revisions) != len(self.reachable_revisions):
            raise ValueError("Reachable revision inventory contains duplicates")
        if (revisions != set(self.fenced_revisions)
            or revisions != self.revision_policy_sha256.keys()):
            raise ValueError("Every reachable old and candidate revision must be fenced")
        if revisions != plan.policy_sha256.keys():
            raise ValueError("The plan must review every reachable revision policy")
        if self.package_sha256 != plan.package_sha256:
            raise ValueError("A runtime artifact changed after the reviewed plan")
        if any(
            self.revision_policy_sha256.get(resource) != digest
            for resource, digest in plan.policy_sha256.items()
        ):
            raise ValueError("A reviewed policy changed before initialization")
        if any((self.pending_publications, self.active_dispatches,
                self.pending_telemetry, self.pending_settlements)):
            raise ValueError("In-flight work must be drained before initialization")
        if not self.admission_fenced_at <= self.drain_completed_at <= self.observed_at:
            raise ValueError("Drain evidence does not cover the old admission window")
        if ((self.drain_completed_at - self.admission_fenced_at).total_seconds()
            < plan.minimum_drain_seconds):
            raise ValueError("Old admission and telemetry have not had time to drain")
        for summary in self.balances.summaries(plan.epoch, now):
            if (summary.enabled or not summary.enforce or summary.token_limit != 0
                or summary.budget_mode != "tokens" or summary.confirmed_usd
                or summary.active_reserved_usd or summary.upper_bound_usd):
                raise ValueError("Opening balances must be disabled Token scope seeds")


class InstallationJournal:
    """An interrupted write remains unresolved until an operator reads it back."""

    def __init__(self, path: Path, plan: BudgetInstallationPlan) -> None:
        self.path, self.plan = path, plan
        self._mutex = threading.RLock()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"plan_sha256": plan.digest, "events": []}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self._acquire()

    def _acquire(self) -> None:
        if os.name != "posix":
            raise ValueError("The installation journal requires a POSIX operator host")
        import fcntl

        self._descriptor = os.open(
            str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
        )
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self._descriptor)
            self._descriptor = -1
            raise

    @classmethod
    def resume(cls, path: Path, plan: BudgetInstallationPlan) -> InstallationJournal:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("Recovery requires the original private regular journal file")
        journal = cls.__new__(cls)
        journal.path, journal.plan = path, plan
        journal._mutex = threading.RLock()
        journal._acquire()
        try:
            document = json.loads(path.read_text())
            if document["plan_sha256"] != plan.digest:
                raise ValueError("Recovery journal belongs to another plan")
        except BaseException:
            journal.close()
            raise
        return journal

    def close(self) -> None:
        with self._mutex:
            if self._descriptor >= 0:
                os.close(self._descriptor)
                self._descriptor = -1

    def __enter__(self) -> InstallationJournal:
        return self

    def __exit__(self, *arguments: object) -> None:
        self.close()

    def _append(self, event: dict[str, Any]) -> None:
        document = json.loads(self.path.read_text())
        if document["plan_sha256"] != self.plan.digest:
            raise ValueError("Installation journal belongs to another plan")
        document["events"].append(event)
        temporary = self.path.with_suffix(".pending")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(document, stream, default=str)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def perform(
        self, operation: str, *, intent: dict[str, Any],
        apply: Callable[[], None], readback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._mutex:
            if self._descriptor < 0:
                raise ValueError("Installation journal is closed")
            return self._perform(operation, intent=intent, apply=apply, readback=readback)

    def _perform(
        self, operation: str, *, intent: dict[str, Any],
        apply: Callable[[], None], readback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        events = json.loads(self.path.read_text())["events"]
        if any(event["operation"] == operation for event in events):
            raise ValueError("A journaled operation must be recovered, never replayed")
        intents = {event["operation"] for event in events if event["phase"] == "intent"}
        verified = {event["operation"] for event in events if event["phase"] == "verified"}
        if intents - verified:
            raise ValueError("An unresolved external write must be recovered before another write")
        self._append({"operation": operation, "phase": "intent", "at": datetime.now(UTC),
                      "expected": intent})
        # Do not retry or roll back an unknown external outcome automatically.
        apply()
        actual = readback()
        if actual != intent:
            raise ValueError("External readback differs from the journaled intent")
        self._append({"operation": operation, "phase": "verified", "at": datetime.now(UTC),
                      "actual": actual})
        return actual

    def recover(
        self, operation: str, readback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._mutex:
            if self._descriptor < 0:
                raise ValueError("Installation journal is closed")
            return self._recover(operation, readback)

    def _recover(
        self, operation: str, readback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        events = json.loads(self.path.read_text())["events"]
        pending = [event for event in events
                   if event["operation"] == operation and event["phase"] == "intent"]
        if len(pending) != 1 or any(
            event["operation"] == operation and event["phase"] == "verified" for event in events
        ):
            raise ValueError("Recovery requires exactly one unresolved intent")
        actual = readback()
        if actual != pending[0]["expected"]:
            raise ValueError("Recovery cannot certify a different external outcome")
        self._append({"operation": operation, "phase": "verified", "at": datetime.now(UTC),
                      "actual": actual, "recovered_without_replay": True})
        return actual


def initialize_rollout(
    connection: Any, plan: BudgetInstallationPlan, readback: MaintenanceReadback,
    *, now: datetime | None = None,
) -> None:
    """Create coordination state, never balances, under a verified maintenance pause."""
    moment = now or datetime.now(UTC)
    readback.require_paused(plan, moment)
    if len(plan.gateway_profile_ids) != 1:
        raise ValueError("Multi-target installation requires independent fence proofs per target")
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute("LOCK TABLE budget_execution_rollout IN EXCLUSIVE MODE")
        if connection.execute("SELECT 1 FROM budget_execution_rollout LIMIT 1").fetchone():
            raise ValueError("An existing epoch must be recovered, not replaced")
        pending = connection.execute(
            """SELECT count(*) FROM gateway_publication
               WHERE status NOT IN ('active','failed','superseded','rolled_back')""",
        ).fetchone()[0]
        if pending:
            raise ValueError("Publication state changed before rollout initialization")
        target = connection.execute(
            """SELECT target.gateway_profile_id FROM apim_target_metadata target
               JOIN gateway_profile profile ON profile.id = target.gateway_profile_id
               WHERE target.gateway_profile_id = %s AND profile.enabled
                 AND profile.implementation = 'apim' AND target.api_id = %s
                 AND lower(target.service_name) = lower(%s)""",
            (readback.fence.gateway_profile_id, readback.fence.api_id,
             readback.fence.apim_service_id),
        ).fetchone()
        if target is None:
            raise ValueError("Installation target does not match its registered APIM identity")
        source = connection.execute(
            "SELECT source FROM directory_control_state WHERE singleton",
        ).fetchone()
        if source is None or source[0] != "database":
            raise ValueError("Existing installation adoption requires its authoritative directory")
        scopes = connection.execute(
            """SELECT 'person', governance_user_id FROM directory_person
                 WHERE status = 'active' AND NOT manual_disabled
               UNION SELECT 'person', scope_id FROM token_budget
                 WHERE scope_type = 'user' AND period_start = %s
               UNION SELECT 'person', user_id FROM user_model_policy
               UNION SELECT 'person', email FROM app_user
               UNION SELECT 'person', user_id FROM token_usage
                 WHERE usage_domain = 'apim' AND user_id LIKE '%%@%%'
                   AND ts >= %s::date - interval '1 month'
                   AND ts < %s::date + interval '1 month'
               UNION SELECT 'application', id::text FROM gateway_application
                 WHERE NOT system_managed AND status <> 'retired'""",
            (plan.period_start.date(), plan.period_start.date(), plan.period_start.date()),
        ).fetchall()
        observed = {
            (item.summary.scope.scope_type, item.summary.scope.scope_id)
            for item in readback.balances.scopes
        }
        if set(scopes) != observed:
            raise ValueError("Opening balance evidence must cover every authoritative scope")
        # Every opening balance comes from a separately observed, drained scope.
        for summary in readback.balances.summaries(plan.epoch, moment):
            scope = summary.scope
            if scope.period_start != plan.period_start.date() or summary.budget_mode != "tokens":
                raise ValueError("Existing installations must first preserve their Token balances")
            row = connection.execute(
                "SELECT budget_scope_confirmed_tokens(%s,%s,%s,%s)",
                (scope.scope_type, scope.scope_id, scope.period_start,
                 (scope.period_start.replace(day=28) + timedelta(days=4)).replace(day=1)),
            ).fetchone()
            if row is None or row[0] != summary.confirmed_tokens:
                raise ValueError("SQL and the freshly drained legacy ledger do not agree")
            outstanding = connection.execute(
                """SELECT budget_scope_pending_request_tokens(%s,%s,%s),
                   (SELECT count(*) FROM budget_reservation_finalization_effective
                    WHERE scope_type = %s AND scope_id = %s AND period_start = %s
                      AND finalization_kind = 'unverified_upper_bound')""",
                (scope.scope_type, scope.scope_id, scope.period_start,
                 scope.scope_type, scope.scope_id, scope.period_start),
            ).fetchone()
            if outstanding is None or outstanding[0] or outstanding[1]:
                raise ValueError("SQL still has pending or conservatively charged legacy requests")
        readback.require_paused(plan, now or datetime.now(UTC))
        # Coordination active is NOT ordinary admission active: the Table fence
        # remains paused until real verification and financial recovery pass.
        connection.execute(
            """INSERT INTO budget_execution_rollout
               (epoch,state,gateway_profile_ids,admitted_after,legacy_fence_verified_at,
                scope_sync_enabled) VALUES (%s,'active',%s,%s,%s,TRUE)""",
            (plan.epoch, plan.gateway_profile_ids, readback.drain_completed_at,
             readback.observed_at),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an existing-installation budget plan")
    parser.add_argument("--plan", required=True, type=Path)
    arguments = parser.parse_args()
    plan = BudgetInstallationPlan.model_validate_json(arguments.plan.read_text())
    plan.require_current(datetime.now(UTC))
    print(json.dumps({"plan_sha256": plan.digest, "cloud_writes": 0,
                      "activation": False, "requires_live_maintenance_readback": True}))


if __name__ == "__main__":
    main()
