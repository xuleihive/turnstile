"""Projects the PostgreSQL budget state into an Azure Table Storage ledger.

The APIM admission check reads this ledger on every employee request, so nothing in
this application may sit on the inference path. The Function pushes state outwards on
a timer instead; APIM never calls back.

Row layout, one partition per person or application per month:

    PartitionKey  "<user id>|<YYYY-MM>"

    RowKey  "Q"                       Limit, Enforce            written here
    RowKey  "C"                       ConfirmedUsed             written here
    RowKey  "M"                       Configured, Models        written here
    RowKey  "R|<iso ts>|<correlation>"  Reserved                  written by APIM

    used = C.ConfirmedUsed + sum(R.Reserved)

The Function settles reservations by correlation ID, never by a shared time frontier. It
writes the full stored usage total to C first, then deletes only reservations whose exact
request is final in PostgreSQL. A partial failure can temporarily double-count usage but
can never make usage disappear.

Complete log queries can recover missing usage. After the configured grace, unresolved
reservations are marked as upper bounds in place; their Reserved amount remains charged.
Missing or incomplete log evidence never permits releasing that charge.

The M row rides the same partition deliberately: the policy already reads the whole
partition in one filtered GET, so model access costs no additional round trip.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

from ..domain.application_access import GatewayApplicationLedgerState
from ..domain.ledger import (
    BudgetReservationAdmission,
    BudgetReservationFinalization,
    LedgerScopeType,
    strict_budget_evidence,
)
from ..domain.models import ReconciledUsage, ReservationTerminalEvidence
from ..persistence.repository import QueryRepository
from .reconciliation import ManagedIdentityTokenProvider, ReservationTerminalLog

logger = logging.getLogger(__name__)

STORAGE_RESOURCE = "https://storage.azure.com/"
# Table Storage rejects Atom without this, and bearer auth needs at least this version.
TABLE_API_VERSION = "2020-12-06"
RESERVATION_PREFIX = "R|"
MODEL_ACCESS_ROW_KEY = "M"
# Both ends are delimited so a membership test cannot match a prefix of a longer id.
MODEL_DELIMITER = "|"
# Table Storage caps a string property at 32k characters. A registry that large is not a
# real configuration, and silently truncating the list would turn into a silent denial, so
# an oversized set is projected as unconfigured instead and logged.
MODEL_PROPERTY_LIMIT = 30000
# Carries no `@`, so `merge_observed_users` can never list the roll-forward as a person
# who owns a budget — the same trick the other machine identities use.
ROLL_FORWARD_ACTOR = "system-budget-roll-forward"
APPLICATION_PARTITION_PREFIX = "app|"
APPLICATION_MAP_PARTITION_PREFIX = "app-map|"
RESERVATION_RECOVERY_LAG = timedelta(minutes=10)
RESERVATION_FINALIZATION_LAG = timedelta(hours=24)


def period_start_for(now: datetime) -> date:
    """First day of the month `now` falls in, in UTC.

    Shared so the scheduler and the ledger projection cannot disagree about which period
    they are working on.
    """
    return date(now.year, now.month, 1)


def model_access_value(identifiers: Sequence[str]) -> str:
    """Delimited membership set the policy can test with a single Contains().

    Case is folded here rather than in the policy: a model key is caller-supplied text and
    the comparison has to be stable without asking a policy expression to normalise.
    """
    unique = sorted({value.strip().lower() for value in identifiers if value.strip()})
    return MODEL_DELIMITER + MODEL_DELIMITER.join(unique) + MODEL_DELIMITER


def ledger_stamp(ts: datetime) -> str:
    """Fixed-width UTC stamp retained in row keys for ordering and diagnostics."""
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def reservation_row_key(ts: datetime, request_id: str) -> str:
    return f"{RESERVATION_PREFIX}{ledger_stamp(ts)}|{request_id}"


def partition_key(user_id: str, period: str) -> str:
    return f"{user_id}|{period}"


def application_partition_key(application_id: UUID | str, period: str) -> str:
    return f"{APPLICATION_PARTITION_PREFIX}{application_id}|{period}"


def application_map_partition_key(gateway_profile_id: UUID | str) -> str:
    return f"{APPLICATION_MAP_PARTITION_PREFIX}{gateway_profile_id}"


def parse_budget_partition(partition: str) -> tuple[LedgerScopeType, str, date] | None:
    scope_type: LedgerScopeType = "application" if partition.startswith("app|") else "person"
    value = partition[4:] if scope_type == "application" else partition
    scope_id, separator, period = value.rpartition("|")
    if not separator or not scope_id or not re.fullmatch(r"\d{4}-\d{2}", period):
        return None
    try:
        start = date.fromisoformat(f"{period}-01")
        if scope_type == "application":
            UUID(scope_id)
    except ValueError:
        return None
    return scope_type, scope_id, start


@dataclass(frozen=True)
class LedgerPerson:
    user_id: str
    department_id: str
    token_limit: int
    confirmed_tokens: int
    enforce: bool


@dataclass(frozen=True)
class LedgerReservation:
    row_key: str
    correlation_id: str
    reserved_tokens: int
    finalization_kind: str | None = None

    @property
    def created_at(self) -> datetime:
        created = datetime.fromisoformat(self.row_key.split("|", 2)[1].replace("Z", "+00:00"))
        if created.utcoffset() is None:
            raise ValueError("reservation timestamp must include a timezone")
        return created.astimezone(UTC)


@dataclass(frozen=True)
class LedgerModelAccess:
    user_id: str
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class LedgerApplication:
    application_id: str
    token_limit: int
    tokens_per_minute: int
    confirmed_tokens: int
    enforce: bool


@dataclass(frozen=True)
class LedgerApplicationModelAccess:
    application_id: str
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class LedgerSyncOutcome:
    people: int
    reservations_settled: int
    model_policies: int = 0
    applications: int = 0
    application_reservations_settled: int = 0
    application_model_policies: int = 0
    application_mappings: int = 0
    person_finalizations_written: int = 0
    application_finalizations_written: int = 0
    reservations_finalized_upper_bound: int = 0
    upper_bounds_settled: int = 0
    stale_application_reservations: int = 0


class LedgerStore(Protocol):
    def upsert(self, partition: str, row_key: str, entity: dict[str, Any]) -> None: ...

    def list_reservations(self, partition: str) -> Sequence[LedgerReservation]: ...

    def list_pending_partitions(self) -> Sequence[str]: ...

    def mark_upper_bounds(
        self, partition: str, reservations: Sequence[LedgerReservation]
    ) -> int: ...

    def delete_reservations(self, partition: str, row_keys: Sequence[str]) -> int: ...


class StorageTokenProvider(Protocol):
    def token(self, resource: str) -> str: ...


class TableStorageLedger:
    """Minimal Table Storage client over the managed identity, with no SDK dependency."""

    def __init__(
        self,
        endpoint: str,
        table: str,
        token_provider: StorageTokenProvider | None = None,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._table = table
        self._token_provider = token_provider or ManagedIdentityTokenProvider()
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._access_token: str | None = None

    def __enter__(self) -> TableStorageLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        if self._access_token is None:
            self._access_token = self._token_provider.token(STORAGE_RESOURCE)
        return {
            "Authorization": f"Bearer {self._access_token}",
            "x-ms-version": TABLE_API_VERSION,
            "x-ms-client-request-id": str(uuid4()),
            "Accept": "application/json;odata=nometadata",
            "Accept-Charset": "UTF-8",
            "DataServiceVersion": "3.0;",
            "MaxDataServiceVersion": "3.0;NetFx",
            "Content-Type": "application/json",
        }

    def _entity_url(self, partition: str, row_key: str) -> str:
        return (
            f"{self._endpoint}/{self._table}"
            f"(PartitionKey='{quote(partition, safe='')}',"
            f"RowKey='{quote(row_key, safe='')}')"
        )

    def _request(
        self, method: str, url: str, extra_headers: dict[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = {**self._headers(), **(extra_headers or {})}
        response = self._client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            self._access_token = None
            headers = {**self._headers(), **(extra_headers or {})}
            response = self._client.request(method, url, headers=headers, **kwargs)
        return response

    def upsert(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        response = self._request("PUT", self._entity_url(partition, row_key), json=entity)
        response.raise_for_status()

    def insert_if_missing(self, partition: str, row_key: str, entity: dict[str, Any]) -> None:
        response = self._request(
            "POST", f"{self._endpoint}/{self._table}",
            json={**entity, "PartitionKey": partition, "RowKey": row_key},
        )
        if response.status_code != 409:
            response.raise_for_status()

    def read_entity(self, partition: str, row_key: str) -> dict[str, Any] | None:
        response = self._request("GET", self._entity_url(partition, row_key))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    def list_reservations(self, partition: str) -> list[LedgerReservation]:
        """List every reservation in one partition, following Table continuation tokens."""
        escaped_partition = partition.replace("'", "''")
        params: dict[str, str] = {
            "$filter": (
                f"PartitionKey eq '{escaped_partition}' "
                f"and RowKey ge '{RESERVATION_PREFIX}' and RowKey lt 'S'"
            ),
            "$select": "RowKey,Reserved,FinalizationKind",
        }
        reservations: list[LedgerReservation] = []
        while True:
            response = self._request("GET", f"{self._endpoint}/{self._table}()", params=params)
            response.raise_for_status()
            for row in response.json().get("value", []):
                row_key = str(row.get("RowKey", ""))
                parts = row_key.split("|", 2)
                if len(parts) != 3 or parts[0] != "R" or not parts[2]:
                    logger.warning("Malformed reservation row key skipped: %s", row_key)
                    continue
                try:
                    reservation = LedgerReservation(
                        row_key=row_key,
                        correlation_id=parts[2],
                        reserved_tokens=int(row["Reserved"]),
                        finalization_kind=row.get("FinalizationKind"),
                    )
                    _ = reservation.created_at
                    if reservation.reserved_tokens < 0:
                        raise ValueError("negative reservation")
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError(
                        "Ledger partition contains a malformed reservation"
                    ) from None
                reservations.append(reservation)
            next_partition = response.headers.get("x-ms-continuation-nextpartitionkey")
            if not next_partition:
                break
            params["NextPartitionKey"] = next_partition
            next_row = response.headers.get("x-ms-continuation-nextrowkey")
            if next_row:
                params["NextRowKey"] = next_row
            else:
                params.pop("NextRowKey", None)
        return reservations

    def list_pending_partitions(self) -> list[str]:
        params = {"$filter": "RowKey ge 'R|' and RowKey lt 'S'", "$select": "PartitionKey"}
        partitions: set[str] = set()
        while True:
            response = self._request("GET", f"{self._endpoint}/{self._table}()", params=params)
            response.raise_for_status()
            partitions.update(str(row["PartitionKey"]) for row in response.json()["value"])
            next_partition = response.headers.get("x-ms-continuation-nextpartitionkey")
            if not next_partition:
                break
            params["NextPartitionKey"] = next_partition
            next_row = response.headers.get("x-ms-continuation-nextrowkey")
            if next_row:
                params["NextRowKey"] = next_row
            else:
                params.pop("NextRowKey", None)
        return sorted(partitions)

    def mark_upper_bounds(self, partition: str, reservations: Sequence[LedgerReservation]) -> int:
        for reservation in reservations:
            response = self._request(
                "MERGE",
                self._entity_url(partition, reservation.row_key),
                extra_headers={"If-Match": "*"},
                json={"FinalizationKind": "unverified_upper_bound"},
            )
            response.raise_for_status()
        return len(reservations)

    @staticmethod
    def _batch_delete_body(
        endpoint: str, table: str, partition: str, row_keys: Sequence[str]
    ) -> tuple[str, bytes]:
        batch = f"batch_{uuid4().hex}"
        changeset = f"changeset_{uuid4().hex}"
        lines = [
            f"--{batch}",
            f"Content-Type: multipart/mixed; boundary={changeset}",
            "",
        ]
        for content_id, row_key in enumerate(row_keys, start=1):
            entity = (
                f"{endpoint}/{table}"
                f"(PartitionKey='{quote(partition, safe='')}',"
                f"RowKey='{quote(row_key, safe='')}')"
            )
            lines.extend(
                [
                    f"--{changeset}",
                    "Content-Type: application/http",
                    "Content-Transfer-Encoding: binary",
                    f"Content-ID: {content_id}",
                    "",
                    f"DELETE {entity} HTTP/1.1",
                    "If-Match: *",
                    "Accept: application/json;odata=nometadata",
                    "DataServiceVersion: 3.0;",
                    "",
                ]
            )
        lines.extend([f"--{changeset}--", f"--{batch}--", ""])
        return batch, "\r\n".join(lines).encode("utf-8")

    def delete_reservations(self, partition: str, row_keys: Sequence[str]) -> int:
        """Delete exact settled rows in atomic same-partition batches of at most 100."""
        unique = sorted(set(row_keys))
        deleted = 0
        for offset in range(0, len(unique), 100):
            chunk = unique[offset : offset + 100]
            batch, body = self._batch_delete_body(self._endpoint, self._table, partition, chunk)
            response = self._request(
                "POST",
                f"{self._endpoint}/$batch",
                extra_headers={"Content-Type": f"multipart/mixed; boundary={batch}"},
                content=body,
            )
            response.raise_for_status()
            statuses = [int(value) for value in re.findall(r"HTTP/1\.1 (\d{3})", response.text)]
            if len(statuses) != len(chunk) or any(status != 204 for status in statuses):
                raise RuntimeError(
                    f"Table reservation delete transaction failed: statuses={statuses}"
                )
            deleted += len(chunk)
        return deleted


class LedgerSyncService:
    def __init__(
        self,
        repository: QueryRepository,
        store: LedgerStore,
        terminal_log: ReservationTerminalLog | None = None,
        recovery_lag: timedelta = RESERVATION_RECOVERY_LAG,
        finalization_lag: timedelta = RESERVATION_FINALIZATION_LAG,
    ) -> None:
        if recovery_lag <= timedelta(0) or finalization_lag <= recovery_lag:
            raise ValueError("finalization grace must exceed the positive recovery lag")
        self._repository = repository
        self._store = store
        self._terminal_log = terminal_log
        self._recovery_lag = recovery_lag
        self._finalization_lag = finalization_lag
        self._evidence_effective_at: datetime | None = None

    @staticmethod
    def _finalization_from_evidence(
        scope_type: LedgerScopeType,
        scope_id: str,
        period_start: date,
        reservation: LedgerReservation,
        evidence: ReservationTerminalEvidence,
        *,
        strict: bool = False,
        evidence_correlation_id: str | None = None,
    ) -> BudgetReservationFinalization | None:
        if evidence.correlation_id != (evidence_correlation_id or reservation.correlation_id):
            return None
        common = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "period_start": period_start,
            "correlation_id": reservation.correlation_id,
            "reservation_created_at": reservation.created_at,
            "reservation_tokens": reservation.reserved_tokens,
            "evidence_at": max(evidence.observed_at, reservation.created_at),
            "status_code": evidence.status_code,
        }
        if evidence.has_exact_usage:
            return BudgetReservationFinalization.model_validate(
                {
                    **common,
                    "input_tokens": evidence.prompt_tokens,
                    "output_tokens": evidence.completion_tokens,
                    "total_tokens": evidence.total_tokens,
                    "finalization_kind": "exact_usage",
                    "source": "apim_gateway_llm_log",
                }
            )
        if not strict and evidence.is_terminal_zero:
            return BudgetReservationFinalization.model_validate(
                {
                    **common,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "finalization_kind": "terminal_zero",
                    "source": "apim_gateway_log",
                }
            )
        return None

    def _recover_reservations(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
        period_start: date,
        reservations: Sequence[LedgerReservation],
        moment: datetime,
    ) -> int:
        if self._terminal_log is None:
            return 0
        stale = [row for row in reservations if row.created_at <= moment - self._recovery_lag]
        settled = self._repository.settled_reservation_correlations(
            [row.correlation_id for row in stale], scope_type=scope_type, scope_id=scope_id
        )
        unresolved = [row for row in stale if row.correlation_id not in settled]
        if not unresolved:
            return 0
        correlations = self._repository.reservation_evidence_correlations(
            scope_type, scope_id, [row.correlation_id for row in unresolved]
        )

        def gateway_correlation(row: LedgerReservation) -> str:
            return correlations.get(row.correlation_id, row.correlation_id)

        due = [row for row in unresolved if row.created_at <= moment - self._finalization_lag]
        try:
            evidence = list(
                self._terminal_log.fetch_correlations(
                    [gateway_correlation(row) for row in unresolved],
                    min(row.created_at for row in unresolved) - self._recovery_lag,
                    moment - self._recovery_lag,
                )
            )
            if due:
                evidence.extend(
                    self._terminal_log.fetch_correlations(
                        [gateway_correlation(row) for row in due],
                        min(row.created_at for row in due) - self._recovery_lag,
                        moment,
                    )
                )
        except (httpx.HTTPError, RuntimeError, ValueError):
            logger.exception(
                "Reservation evidence unavailable; unresolved reservations remain charged"
            )
            return 0
        by_correlation: dict[str, ReservationTerminalEvidence] = {}
        strict_correlations = {
            gateway_correlation(row)
            for row in unresolved
            if strict_budget_evidence(row.created_at, self._evidence_effective_at)
        }
        for item in evidence:
            previous = by_correlation.get(item.correlation_id)
            if item.correlation_id in strict_correlations:
                if previous is None or (item.has_exact_usage and not previous.has_exact_usage):
                    by_correlation[item.correlation_id] = item
                elif (
                    previous.has_exact_usage
                    and item.has_exact_usage
                    and (previous.total_tokens != item.total_tokens)
                ):
                    logger.warning("Conflicting terminal totals; retaining first complete evidence")
            elif previous is None or (item.has_exact_usage, item.observed_at) > (
                previous.has_exact_usage,
                previous.observed_at,
            ):
                by_correlation[item.correlation_id] = item
        self._repository.apply_reconciled_usage(
            [
                ReconciledUsage(
                    correlation_id=item.correlation_id,
                    input_tokens=int(item.prompt_tokens or 0),
                    output_tokens=int(item.completion_tokens or 0),
                    cached_tokens=None,
                )
                for item in by_correlation.values()
                if item.has_exact_usage
            ]
        )
        settled = self._repository.settled_reservation_correlations(
            [row.correlation_id for row in unresolved], scope_type=scope_type, scope_id=scope_id
        )
        kinds = self._repository.reservation_finalization_kinds(
            scope_type, scope_id, [row.correlation_id for row in unresolved]
        )
        finalizations = []
        for row in unresolved:
            if row.correlation_id in settled:
                continue
            row_evidence = by_correlation.get(gateway_correlation(row))
            finalization = (
                self._finalization_from_evidence(
                    scope_type,
                    scope_id,
                    period_start,
                    row,
                    row_evidence,
                    strict=strict_budget_evidence(row.created_at, self._evidence_effective_at),
                    evidence_correlation_id=gateway_correlation(row),
                )
                if row_evidence is not None
                else None
            )
            if (
                finalization is None
                and row in due
                and row.finalization_kind != "unverified_upper_bound"
                and kinds.get(row.correlation_id) != "unverified_upper_bound"
            ):
                finalization = BudgetReservationFinalization(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    period_start=period_start,
                    correlation_id=row.correlation_id,
                    reservation_created_at=row.created_at,
                    reservation_tokens=row.reserved_tokens,
                    total_tokens=row.reserved_tokens,
                    evidence_at=row.created_at + self._finalization_lag,
                    finalization_kind="unverified_upper_bound",
                    source="reservation_timeout",
                )
            if finalization is not None:
                finalizations.append(finalization)
        return self._repository.save_budget_reservation_finalizations(finalizations)

    def _period_bounds(self, now: datetime) -> tuple[date, date, str]:
        start = period_start_for(now)
        end = date(now.year + 1, 1, 1) if now.month == 12 else date(now.year, now.month + 1, 1)
        return start, end, f"{now.year:04d}-{now.month:02d}"

    def snapshot(self, now: datetime | None = None) -> list[LedgerPerson]:
        moment = now or datetime.now(UTC)
        period_start, period_end, _ = self._period_bounds(moment)
        rows = self._repository.budget_ledger_snapshot(period_start, period_end)
        return [
            LedgerPerson(
                user_id=str(row["user_id"]),
                department_id=str(row["department_id"]),
                token_limit=int(row["token_limit"]),
                confirmed_tokens=int(row["confirmed_tokens"]),
                enforce=str(row["mode"]) == "block",
            )
            for row in rows
        ]

    def model_access(self, user_ids: Sequence[str] | None = None) -> list[LedgerModelAccess]:
        rows = self._repository.model_access_ledger_snapshot(user_ids)
        return [
            LedgerModelAccess(
                user_id=str(row["user_id"]),
                identifiers=tuple(
                    [*(row.get("model_uuids") or []), *(row.get("model_keys") or [])]
                ),
            )
            for row in rows
        ]

    def application_snapshot(self, now: datetime | None = None) -> list[LedgerApplication]:
        moment = now or datetime.now(UTC)
        period_start, period_end, _ = self._period_bounds(moment)
        rows = self._repository.gateway_application_ledger_snapshot(period_start, period_end)
        return [
            LedgerApplication(
                application_id=str(row["application_id"]),
                token_limit=int(row["token_limit"]),
                tokens_per_minute=int(row["tokens_per_minute"]),
                confirmed_tokens=int(row["confirmed_tokens"]),
                enforce=bool(row["enforce"]),
            )
            for row in rows
            if str(row["status"]) != "retired"
        ]

    def application_model_access(self) -> list[LedgerApplicationModelAccess]:
        return [
            LedgerApplicationModelAccess(
                application_id=str(row["application_id"]),
                identifiers=tuple(
                    [*(row.get("model_uuids") or []), *(row.get("model_keys") or [])]
                ),
            )
            for row in self._repository.gateway_application_model_ledger_snapshot()
        ]

    def project_model_access(
        self, policies: Sequence[LedgerModelAccess], now: datetime | None = None
    ) -> int:
        """Write the M rows. Used by the timer and by the admin save path alike.

        The admin path calls this straight after the transaction commits so a revocation
        takes effect on the next request instead of at the next timer tick; the timer then
        only has to repair a write that failed.
        """
        moment = now or datetime.now(UTC)
        _, _, period = self._period_bounds(moment)
        written = 0
        for policy in policies:
            value = model_access_value(policy.identifiers)
            if len(value) > MODEL_PROPERTY_LIMIT:
                # Truncating would silently deny models the person is entitled to, which is
                # worse than the documented fail-open. Leave the row absent instead.
                logger.error(
                    "Model access for %s exceeds the ledger property limit; not projected",
                    policy.user_id,
                )
                continue
            self._store.upsert(
                partition_key(policy.user_id, period),
                MODEL_ACCESS_ROW_KEY,
                {"Configured": True, "Models": value},
            )
            written += 1
        return written

    def project_application_model_access(
        self,
        policies: Sequence[LedgerApplicationModelAccess],
        now: datetime | None = None,
    ) -> int:
        moment = now or datetime.now(UTC)
        _, _, period = self._period_bounds(moment)
        written = 0
        for policy in policies:
            value = model_access_value(policy.identifiers)
            if len(value) > MODEL_PROPERTY_LIMIT:
                logger.error(
                    "Application model access for %s exceeds the ledger property limit",
                    policy.application_id,
                )
                continue
            self._store.upsert(
                application_partition_key(policy.application_id, period),
                MODEL_ACCESS_ROW_KEY,
                {"Configured": True, "Models": value},
            )
            written += 1
        return written

    def project_application_mappings(self, owner_user_id: UUID | None = None) -> int:
        written = 0
        for mapping in self._repository.gateway_application_ledger_mappings(owner_user_id):
            self._store.upsert(
                application_map_partition_key(mapping["gateway_profile_id"]),
                str(mapping["apim_subscription_id"]),
                {
                    "ApplicationId": str(mapping["application_id"]),
                    "ApplicationSlug": str(mapping["application_slug"]),
                    "ApplicationName": str(mapping["application_name"]),
                    "ApplicationType": str(mapping["application_type"]),
                    "ApplicationStatus": str(mapping["application_status"]),
                    "SubscriptionState": str(mapping["subscription_state"]),
                    "ScopeExists": bool(mapping["scope_exists"]),
                    "OwnerUserId": str(mapping.get("owner_user_id") or ""),
                    "OwnerEmail": str(mapping.get("owner_email") or ""),
                    "OwnerDisplayName": str(mapping.get("owner_display_name") or ""),
                    "OwnerEnabled": bool(mapping.get("owner_enabled", False)),
                    "OwnerTenantId": str(mapping.get("owner_tenant_id") or ""),
                    "TenantId": str(mapping.get("tenant_id") or ""),
                    "TenantName": str(mapping.get("tenant_name") or ""),
                    "TenantEnabled": bool(mapping.get("tenant_enabled", False)),
                    "OrganizationUnitId": str(mapping.get("organization_unit_id") or ""),
                    "OrganizationUnitName": str(
                        mapping.get("organization_unit_name") or ""
                    ),
                    "OrganizationUnitEnabled": bool(
                        mapping.get("organization_unit_enabled", False)
                    ),
                },
            )
            written += 1
        return written

    def run(self, now: datetime | None = None) -> LedgerSyncOutcome:
        moment = now or datetime.now(UTC)
        self._evidence_effective_at = self._repository.budget_evidence_effective_at()
        current_start = period_start_for(moment)
        previous_moment = datetime.combine(current_start, time.min, tzinfo=UTC) - timedelta(
            microseconds=1
        )

        partitions: dict[str, tuple[LedgerScopeType, str, date, list[LedgerReservation]]] = {}
        written = {"person": 0, "application": 0}
        for request in self._repository.pending_billable_requests():
            period = request["period_start"].strftime("%Y-%m")
            partition = (
                partition_key(request["scope_id"], period)
                if request["scope_type"] == "person"
                else application_partition_key(request["scope_id"], period)
            )
            entity: dict[str, Any] = {"Reserved": request["reserved_tokens"]}
            if request.get("finalization_kind") == "unverified_upper_bound":
                entity["FinalizationKind"] = "unverified_upper_bound"
            self._store.upsert(
                partition, reservation_row_key(request["created_at"], str(request["id"])), entity
            )
        for partition in self._store.list_pending_partitions():
            parsed = parse_budget_partition(partition)
            if parsed is None:
                logger.warning("Skipping an invalid ledger budget partition")
                continue
            scope_type, scope_id, start = parsed
            rows = list(self._store.list_reservations(partition))
            partitions[partition] = (scope_type, scope_id, start, rows)
        self._repository.register_budget_reservations(
            [
                BudgetReservationAdmission(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    correlation_id=row.correlation_id,
                    created_at=row.created_at,
                    reserved_tokens=row.reserved_tokens,
                )
                for scope_type, scope_id, _start, rows in partitions.values()
                for row in rows
            ]
        )
        for scope_type, scope_id, start, rows in partitions.values():
            written[scope_type] += self._recover_reservations(
                scope_type, scope_id, start, rows, moment
            )

        settled = {
            partition: self._repository.settled_reservation_correlations(
                [row.correlation_id for row in rows], scope_type=scope_type, scope_id=scope_id
            )
            for partition, (scope_type, scope_id, _, rows) in partitions.items()
        }
        balances: dict[str, int] = {}
        for partition, (scope_type, scope_id, start, _) in partitions.items():
            start_moment = datetime.combine(start, time.min, tzinfo=UTC)
            _, end, _ = self._period_bounds(start_moment)
            _, following_end, following_period = self._period_bounds(
                datetime.combine(end, time.min, tzinfo=UTC)
            )
            balances[partition] = self._repository.budget_scope_confirmed_tokens(
                scope_type, scope_id, start, end
            )
            following = (
                application_partition_key(scope_id, following_period)
                if scope_type == "application"
                else partition_key(scope_id, following_period)
            )
            balances[following] = self._repository.budget_scope_confirmed_tokens(
                scope_type, scope_id, end, following_end
            )

        people_count = 0
        application_count = 0
        application_states: dict[str, tuple[date, LedgerApplication]] = {}
        for period_moment in (moment, previous_moment):
            start, end, period = self._period_bounds(period_moment)
            people = self.snapshot(period_moment)
            people_count += len(people)
            for person in people:
                partition = partition_key(person.user_id, period)
                self._store.upsert(
                    partition, "Q", {"Limit": person.token_limit, "Enforce": person.enforce}
                )
                balances[partition] = person.confirmed_tokens
            applications = self.application_snapshot(period_moment)
            application_count += len(applications)
            for application in applications:
                partition = application_partition_key(application.application_id, period)
                self._store.upsert(
                    partition,
                    "Q",
                    {
                        "Limit": application.token_limit,
                        "TokensPerMinute": application.tokens_per_minute,
                        "Enforce": application.enforce,
                    },
                )
                balances[partition] = self._repository.budget_scope_confirmed_tokens(
                    "application", application.application_id, start, end
                )
                application_states[partition] = (start, application)
        for partition, confirmed in balances.items():
            self._store.upsert(partition, "C", {"ConfirmedUsed": confirmed})

        deleted_counts = {"person": 0, "application": 0}
        marked_count = 0
        upper_bounds_settled = 0
        remaining: dict[str, list[LedgerReservation]] = {}
        for partition, (scope_type, scope_id, _, rows) in partitions.items():
            kinds = self._repository.reservation_finalization_kinds(
                scope_type, scope_id, [row.correlation_id for row in rows]
            )
            to_delete = [row for row in rows if row.correlation_id in settled[partition]]
            to_mark = [
                row
                for row in rows
                if row.correlation_id not in settled[partition]
                and row.finalization_kind is None
                and kinds.get(row.correlation_id) == "unverified_upper_bound"
            ]
            marked_count += self._store.mark_upper_bounds(partition, to_mark)
            deleted_counts[scope_type] += self._store.delete_reservations(
                partition, [row.row_key for row in to_delete]
            )
            upper_bounds_settled += sum(
                row.finalization_kind == "unverified_upper_bound" for row in to_delete
            )
            remaining[partition] = [
                replace(row, finalization_kind="unverified_upper_bound") if row in to_mark else row
                for row in rows
                if row not in to_delete
            ]

        states = []
        for partition, (start, application) in application_states.items():
            rows = remaining.get(partition, [])
            pending = [row for row in rows if row.finalization_kind != "unverified_upper_bound"]
            upper = [row for row in rows if row.finalization_kind == "unverified_upper_bound"]
            pending_tokens = sum(row.reserved_tokens for row in pending)
            upper_tokens = sum(row.reserved_tokens for row in upper)
            confirmed = balances[partition]
            states.append(
                GatewayApplicationLedgerState(
                    period_start=start,
                    application_id=UUID(application.application_id),
                    token_limit=application.token_limit,
                    confirmed_tokens=confirmed,
                    pending_reserved_tokens=pending_tokens,
                    pending_reservation_count=len(pending),
                    finalized_upper_bound_tokens=upper_tokens,
                    finalized_upper_bound_count=len(upper),
                    stale_reservation_count=sum(
                        row.created_at <= moment - self._recovery_lag for row in pending
                    ),
                    oldest_reservation_at=min((row.created_at for row in pending), default=None),
                    available_tokens=max(
                        application.token_limit - confirmed - pending_tokens - upper_tokens, 0
                    ),
                    snapshot_at=moment,
                )
            )
        self._repository.save_gateway_application_ledger_states(states)
        # Model policies are projected independently of budgets: a person may be restricted
        # to a model set without ever being given an allowance, so iterating budgets alone
        # would leave their policy unenforced.
        policies = self.project_model_access(self.model_access(), moment)
        application_policies = self.project_application_model_access(
            self.application_model_access(), moment
        )
        application_mappings = self.project_application_mappings()
        return LedgerSyncOutcome(
            people=people_count,
            reservations_settled=deleted_counts["person"],
            model_policies=policies,
            applications=application_count,
            application_reservations_settled=deleted_counts["application"],
            application_model_policies=application_policies,
            application_mappings=application_mappings,
            person_finalizations_written=written["person"],
            application_finalizations_written=written["application"],
            reservations_finalized_upper_bound=marked_count,
            upper_bounds_settled=upper_bounds_settled,
            stale_application_reservations=sum(
                row.created_at <= moment - self._recovery_lag
                and row.finalization_kind != "unverified_upper_bound"
                for partition, rows in remaining.items()
                if partition.startswith("app|")
                for row in rows
            ),
        )
