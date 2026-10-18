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
             FinalizationKind on R       Unverified upper bound    written here

    used = C.ConfirmedUsed + sum(R.Reserved)

The Function settles reservations by correlation ID, never by a shared time frontier. It
writes the full stored usage total to C first, then deletes only reservations whose exact
request is final in PostgreSQL. If every telemetry path is silent beyond a bounded grace
period, R is marked as an unverified upper bound without changing its Reserved value. It is
terminal and auditable, but remains charged until stronger evidence arrives. A partial failure can
double-count usage but can never make usage disappear.

The M row rides the same partition deliberately: the policy already reads the whole
partition in one filtered GET, so model access costs no additional round trip.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

from ..domain.application_access import GatewayApplicationLedgerState, application_organization
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
APPLICATION_RATE_LIMIT_ROW_KEY = "T"
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
APPLICATION_RATE_LIMIT_PARTITION_PREFIX = "app-tpm|"
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


def application_rate_limit_partition_key(application_id: UUID | str) -> str:
    return f"{APPLICATION_RATE_LIMIT_PARTITION_PREFIX}{application_id}"


def parse_budget_partition(partition: str) -> tuple[LedgerScopeType, str, date] | None:
    scope_type: LedgerScopeType
    value = partition
    if value.startswith(APPLICATION_PARTITION_PREFIX):
        scope_type = "application"
        value = value[len(APPLICATION_PARTITION_PREFIX) :]
    else:
        scope_type = "person"
    scope_id, separator, period = value.rpartition("|")
    if not separator or not scope_id or not re.fullmatch(r"\d{4}-\d{2}", period):
        return None
    try:
        period_start = date.fromisoformat(f"{period}-01")
        if scope_type == "application":
            UUID(scope_id)
    except ValueError:
        return None
    return scope_type, scope_id, period_start


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
    created_at: datetime
    finalization_kind: str | None = None


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
    budget_mode: str = "tokens"
    usd_limit: Decimal | None = None
    confirmed_usd: Decimal | None = None
    usd_cost_available: bool = True


@dataclass(frozen=True)
class LedgerApplicationModelAccess:
    application_id: str
    identifiers: tuple[str, ...]
    configured: bool = True


@dataclass(frozen=True)
class LedgerSyncOutcome:
    people: int
    reservations_settled: int
    model_policies: int = 0
    applications: int = 0
    application_reservations_settled: int = 0
    application_model_policies: int = 0
    application_mappings: int = 0
    stale_application_reservations: int = 0
    person_finalizations_written: int = 0
    application_finalizations_written: int = 0
    reservations_finalized_upper_bound: int = 0
    upper_bounds_settled: int = 0


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
        response = self._request(
            "PUT", self._entity_url(partition, row_key), json=entity
        )
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

    def read_versioned_entity(
        self, partition: str, row_key: str,
    ) -> tuple[dict[str, Any], str] | None:
        response = self._request("GET", self._entity_url(partition, row_key))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        etag = response.headers.get("ETag")
        if not etag or not etag.strip() or etag == "*" or "\r" in etag or "\n" in etag:
            raise ValueError("Table entity version is unavailable")
        return dict(response.json()), etag

    def replace_if_match(
        self, partition: str, row_key: str, entity: dict[str, Any], etag: str | None,
    ) -> bool:
        if etag is not None and (not etag.strip() or etag == "*" or "\r" in etag or "\n" in etag):
            raise ValueError("A concrete Table entity version is required")
        response = self._request(
            "POST" if etag is None else "PUT",
            f"{self._endpoint}/{self._table}" if etag is None
            else self._entity_url(partition, row_key),
            {"If-Match": etag} if etag is not None else None,
            json={**entity, "PartitionKey": partition, "RowKey": row_key},
        )
        if response.status_code in {409, 412} or (etag is not None and response.status_code == 404):
            return False
        response.raise_for_status()
        return True

    def list_partition_entities(
        self, partition: str, *, max_entities: int = 2000, max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        if max_entities < 1 or max_pages < 1:
            raise ValueError("Table inventory limits must be positive")
        escaped = partition.replace("'", "''")
        params = {"$filter": f"PartitionKey eq '{escaped}'", "$top": str(min(max_entities, 1000))}
        rows: list[dict[str, Any]] = []
        identities: set[str] = set()
        continuations: set[tuple[str, str]] = set()
        for _ in range(max_pages):
            response = self._request(
                "GET", f"{self._endpoint}/{self._table}()",
                {"Cache-Control": "no-cache"}, params=params,
            )
            response.raise_for_status()
            if len(response.content) > 2_097_152:
                raise ValueError("Table inventory response exceeds its limit")
            payload = response.json()
            values = payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(values, list) or len(rows) + len(values) > max_entities:
                raise ValueError("Table inventory is incomplete or exceeds its limit")
            for value in values:
                if (
                    not isinstance(value, dict) or value.get("PartitionKey") != partition
                    or not isinstance(value.get("RowKey"), str) or not value["RowKey"]
                    or value["RowKey"] in identities
                ):
                    raise ValueError("Table inventory identity is invalid or repeated")
                identities.add(value["RowKey"])
                rows.append(value)
            next_partition = response.headers.get("x-ms-continuation-NextPartitionKey", "")
            next_row = response.headers.get("x-ms-continuation-NextRowKey", "")
            if not next_partition and not next_row:
                return rows
            continuation = (next_partition, next_row)
            if (
                not next_partition or len(next_partition) > 1024 or len(next_row) > 1024
                or continuation in continuations
            ):
                raise ValueError("Table inventory continuation is invalid or repeated")
            continuations.add(continuation)
            params["NextPartitionKey"] = next_partition
            if next_row:
                params["NextRowKey"] = next_row
            else:
                params.pop("NextRowKey", None)
        raise ValueError("Table inventory pagination did not complete")

    def list_reservations(self, partition: str) -> list[LedgerReservation]:
        """List every open or conservatively finalized reservation in a partition."""
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
            response = self._request(
                "GET", f"{self._endpoint}/{self._table}()", params=params
            )
            response.raise_for_status()
            for row in response.json().get("value", []):
                row_key = str(row.get("RowKey", ""))
                parts = row_key.split("|", 2)
                if len(parts) != 3 or parts[0] != "R" or not parts[2]:
                    logger.warning("Malformed reservation row key skipped: %s", row_key)
                    continue
                try:
                    created_at = datetime.fromisoformat(
                        parts[1].replace("Z", "+00:00")
                    )
                except ValueError:
                    logger.warning("Malformed reservation timestamp skipped: %s", row_key)
                    continue
                reservations.append(
                    LedgerReservation(
                        row_key=row_key,
                        correlation_id=parts[2],
                        reserved_tokens=int(row.get("Reserved", 0)),
                        created_at=created_at,
                        finalization_kind=(
                            str(row["FinalizationKind"])
                            if row.get("FinalizationKind")
                            else None
                        ),
                    )
                )
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
        """Discover every partition containing R rows, with continuation paging."""
        params: dict[str, str] = {
            "$filter": "RowKey ge 'R|' and RowKey lt 'S'",
            "$select": "PartitionKey",
        }
        partitions: set[str] = set()
        while True:
            response = self._request(
                "GET", f"{self._endpoint}/{self._table}()", params=params
            )
            response.raise_for_status()
            partitions.update(
                str(row["PartitionKey"])
                for row in response.json().get("value", [])
                if row.get("PartitionKey")
            )
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

    def mark_upper_bounds(
        self, partition: str, reservations: Sequence[LedgerReservation]
    ) -> int:
        """Mark R terminal without changing its admission charge or row identity."""
        written = 0
        for reservation in reservations:
            response = self._request(
                "MERGE",
                self._entity_url(partition, reservation.row_key),
                extra_headers={"If-Match": "*"},
                json={
                    "Reserved": reservation.reserved_tokens,
                    "FinalizationKind": "unverified_upper_bound",
                },
            )
            response.raise_for_status()
            written += 1
        return written

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
            batch, body = self._batch_delete_body(
                self._endpoint, self._table, partition, chunk
            )
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
        self._repository = repository
        self._store = store
        self._terminal_log = terminal_log
        self._recovery_lag = recovery_lag
        self._finalization_lag = finalization_lag
        self._evidence_effective_at: datetime | None = None

    def _recover_reservations(
        self,
        scope_type: LedgerScopeType,
        scope_id: str,
        period_start: date,
        reservations: Sequence[LedgerReservation],
        moment: datetime,
    ) -> tuple[int, int, int]:
        stale = [
            reservation
            for reservation in reservations
            if reservation.created_at <= moment - self._recovery_lag
        ]
        if not stale:
            return 0, 0, 0
        settled_before = self._repository.settled_reservation_correlations(
            [reservation.correlation_id for reservation in stale],
            scope_type=scope_type,
            scope_id=scope_id,
        )
        unresolved = [
            reservation
            for reservation in stale
            if reservation.correlation_id not in settled_before
        ]
        if not unresolved:
            return 0, 0, 0
        gateway_correlations = self._repository.reservation_evidence_correlations(
            scope_type, scope_id, [row.correlation_id for row in unresolved],
        )

        def gateway_correlation(reservation: LedgerReservation) -> str:
            return gateway_correlations.get(reservation.correlation_id, reservation.correlation_id)

        existing_finalizations = self._repository.reservation_finalization_kinds(
            scope_type,
            scope_id,
            [reservation.correlation_id for reservation in unresolved],
        )
        terminal_items: Sequence[ReservationTerminalEvidence] = []
        safely_unobserved: set[str] = set()
        if self._terminal_log is not None:
            try:
                terminal_items = self._terminal_log.fetch_correlations(
                    [gateway_correlation(reservation) for reservation in unresolved],
                    min(reservation.created_at for reservation in unresolved)
                    - self._recovery_lag,
                    moment - self._recovery_lag,
                )
                due_for_finalization = [
                    reservation
                    for reservation in unresolved
                    if reservation.finalization_kind is None
                    and reservation.created_at <= moment - self._finalization_lag
                ]
                if due_for_finalization:
                    finalization_items = self._terminal_log.fetch_correlations(
                        [
                            gateway_correlation(reservation)
                            for reservation in due_for_finalization
                        ],
                        min(
                            reservation.created_at
                            for reservation in due_for_finalization
                        )
                        - self._recovery_lag,
                        moment,
                    )
                    by_correlation = {
                        item.correlation_id: item for item in terminal_items
                    }
                    strict_correlations = {
                        gateway_correlation(reservation) for reservation in due_for_finalization
                        if strict_budget_evidence(
                            reservation.created_at, self._evidence_effective_at,
                        )
                    }
                    for window_evidence in finalization_items:
                        if window_evidence.correlation_id not in strict_correlations:
                            by_correlation[window_evidence.correlation_id] = window_evidence
                            continue
                        previous = by_correlation.get(window_evidence.correlation_id)
                        if previous is None or (
                            window_evidence.has_exact_usage and not previous.has_exact_usage
                        ):
                            by_correlation[window_evidence.correlation_id] = window_evidence
                        elif (
                            previous.has_exact_usage and window_evidence.has_exact_usage
                            and previous.total_tokens != window_evidence.total_tokens
                        ):
                            logger.warning(
                                "Conflicting terminal totals for %s %s correlation %s: "
                                "%s versus %s; "
                                "retaining the first complete result",
                                scope_type, scope_id, window_evidence.correlation_id,
                                previous.total_tokens, window_evidence.total_tokens,
                            )
                    terminal_items = list(by_correlation.values())
                    safely_unobserved = {
                        reservation.correlation_id
                        for reservation in due_for_finalization
                        if (
                            (item := by_correlation.get(gateway_correlation(reservation)))
                            is None
                            or self._finalization_from_evidence(
                                scope_type,
                                scope_id,
                                period_start,
                                reservation,
                                item,
                                strict=strict_budget_evidence(
                                    reservation.created_at, self._evidence_effective_at,
                                ),
                                evidence_correlation_id=gateway_correlation(reservation),
                            )
                            is None
                        )
                    }
            except (httpx.HTTPError, RuntimeError, ValueError):
                logger.exception(
                    "%s reservation recovery query failed; reservations remain pending",
                    scope_type.capitalize(),
                )
        evidence = {item.correlation_id: item for item in terminal_items}
        self._repository.apply_reconciled_usage(
            [
                ReconciledUsage(
                    correlation_id=item.correlation_id,
                    input_tokens=item.prompt_tokens,
                    output_tokens=item.completion_tokens,
                    cached_tokens=None,
                )
                for item in terminal_items
                if item.has_exact_usage
                and item.prompt_tokens is not None
                and item.completion_tokens is not None
            ]
        )
        settled_after_reconciliation = self._repository.settled_reservation_correlations(
            [reservation.correlation_id for reservation in unresolved],
            scope_type=scope_type,
            scope_id=scope_id,
        )
        existing = self._repository.existing_usage_correlations(
            [reservation.correlation_id for reservation in unresolved],
            scope_type=scope_type,
            scope_id=scope_id,
        )
        finalizations: list[BudgetReservationFinalization] = []
        upper_bound_correlations: set[str] = set()
        for reservation in unresolved:
            if reservation.correlation_id in settled_after_reconciliation:
                continue
            item = evidence.get(gateway_correlation(reservation))
            finalization = (
                self._finalization_from_evidence(
                    scope_type, scope_id, period_start, reservation, item,
                    strict=strict_budget_evidence(
                        reservation.created_at, self._evidence_effective_at,
                    ),
                    evidence_correlation_id=gateway_correlation(reservation),
                )
                if item is not None
                and (reservation.correlation_id not in existing
                     or strict_budget_evidence(reservation.created_at, self._evidence_effective_at))
                else None
            )
            if (
                finalization is None
                and reservation.finalization_kind == "unverified_upper_bound"
            ):
                continue
            if (
                finalization is None
                and reservation.finalization_kind is None
                and existing_finalizations.get(reservation.correlation_id)
                == "unverified_upper_bound"
            ):
                continue
            if (
                finalization is None
                and reservation.correlation_id in safely_unobserved
                and reservation.finalization_kind is None
                and reservation.created_at <= moment - self._finalization_lag
            ):
                finalization = self._upper_bound_finalization(
                    scope_type,
                    scope_id,
                    period_start,
                    reservation,
                    reservation.created_at + self._finalization_lag,
                )
                upper_bound_correlations.add(reservation.correlation_id)
            if finalization is not None:
                finalizations.append(finalization)
        written = self._repository.save_budget_reservation_finalizations(
            finalizations
        )
        finalization_kinds = self._repository.reservation_finalization_kinds(
            scope_type,
            scope_id,
            [reservation.correlation_id for reservation in unresolved],
        )
        finalized = {
            reservation.correlation_id
            for reservation in unresolved
            if reservation.correlation_id in finalization_kinds
        }
        unresolved_after = len(unresolved) - len(finalized)
        return written, unresolved_after, len(upper_bound_correlations & finalized)

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
        if evidence.has_exact_usage:
            assert evidence.prompt_tokens is not None
            assert evidence.completion_tokens is not None
            assert evidence.total_tokens is not None
            return BudgetReservationFinalization(
                scope_type=scope_type,
                scope_id=scope_id,
                correlation_id=reservation.correlation_id,
                period_start=period_start,
                reservation_created_at=reservation.created_at,
                reservation_tokens=reservation.reserved_tokens,
                evidence_at=max(evidence.observed_at, reservation.created_at),
                status_code=evidence.status_code,
                input_tokens=evidence.prompt_tokens,
                output_tokens=evidence.completion_tokens,
                total_tokens=evidence.total_tokens,
                finalization_kind="exact_usage",
                source="apim_gateway_llm_log",
            )
        if not strict and evidence.is_terminal_zero:
            return BudgetReservationFinalization(
                scope_type=scope_type,
                scope_id=scope_id,
                correlation_id=reservation.correlation_id,
                period_start=period_start,
                reservation_created_at=reservation.created_at,
                reservation_tokens=reservation.reserved_tokens,
                evidence_at=max(evidence.observed_at, reservation.created_at),
                status_code=evidence.status_code,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                finalization_kind="terminal_zero",
                source="apim_gateway_log",
            )
        return None

    @staticmethod
    def _upper_bound_finalization(
        scope_type: LedgerScopeType,
        scope_id: str,
        period_start: date,
        reservation: LedgerReservation,
        moment: datetime,
    ) -> BudgetReservationFinalization:
        return BudgetReservationFinalization(
            scope_type=scope_type,
            scope_id=scope_id,
            period_start=period_start,
            correlation_id=reservation.correlation_id,
            reservation_created_at=reservation.created_at,
            reservation_tokens=reservation.reserved_tokens,
            evidence_at=moment,
            status_code=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=reservation.reserved_tokens,
            finalization_kind="unverified_upper_bound",
            source="reservation_timeout",
        )

    def _period_bounds(self, now: datetime) -> tuple[date, date, str]:
        start = period_start_for(now)
        end = (
            date(now.year + 1, 1, 1)
            if now.month == 12
            else date(now.year, now.month + 1, 1)
        )
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

    def model_access(
        self, user_ids: Sequence[str] | None = None
    ) -> list[LedgerModelAccess]:
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

    def application_snapshot(
        self, now: datetime | None = None
    ) -> list[LedgerApplication]:
        moment = now or datetime.now(UTC)
        period_start, period_end, _ = self._period_bounds(moment)
        rows = self._repository.gateway_application_ledger_snapshot(
            period_start, period_end
        )
        return [
            LedgerApplication(
                application_id=str(row["application_id"]),
                token_limit=(int(row["token_limit"]) if row.get("token_limit") is not None else 0),
                tokens_per_minute=int(row["tokens_per_minute"]),
                confirmed_tokens=int(row["confirmed_tokens"]),
                enforce=bool(row["enforce"]),
                budget_mode=str(row.get("budget_mode", "tokens")),
                usd_limit=(
                    Decimal(str(row["usd_limit"])) if row.get("usd_limit") is not None else None
                ),
                confirmed_usd=(
                    Decimal(str(row["confirmed_usd"]))
                    if row.get("confirmed_usd") is not None else None
                ),
                usd_cost_available=bool(row.get("usd_cost_available", True)),
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
                configured=bool(row["configured"]),
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
                {"Configured": policy.configured, "Models": value},
            )
            written += 1
        return written

    def project_application_mappings(self, application_id: UUID | None = None) -> int:
        from ..persistence.directory_catalog import catalog_for

        written = 0
        catalog = catalog_for(self._repository, include_inactive=True)
        departments = {item.id: item.name for item in catalog.departments}
        for mapping in self._repository.gateway_application_ledger_mappings():
            if application_id is not None and str(mapping["application_id"]) != str(application_id):
                continue
            organization = application_organization(
                dict(mapping.get("tags") or {}), catalog=catalog,
            )
            self._store.upsert(
                application_map_partition_key(mapping["gateway_profile_id"]),
                str(mapping["apim_subscription_id"]),
                {
                    "ApplicationId": str(mapping["application_id"]),
                    "ApplicationSlug": str(mapping["application_slug"]),
                    "ApplicationName": str(mapping["application_name"]),
                    "ApplicationType": str(mapping["application_type"]),
                    "ApplicationStatus": str(mapping["application_status"]),
                    "OrganizationId": organization.organization_id or "unattributed",
                    "OrganizationName": organization.organization_name or "unattributed",
                    "DepartmentId": str(mapping.get("department_id") or "unattributed"),
                    "DepartmentName": departments.get(
                        str(mapping.get("department_id")), "unattributed",
                    ),
                    "SubscriptionState": str(mapping["subscription_state"]),
                    "ScopeExists": bool(mapping["scope_exists"]),
                },
            )
            written += 1
        return written

    def run(self, now: datetime | None = None) -> LedgerSyncOutcome:
        moment = now or datetime.now(UTC)
        self._evidence_effective_at = self._repository.budget_evidence_effective_at()
        current_start = period_start_for(moment)
        previous_moment = datetime.combine(
            current_start, time.min, tzinfo=UTC
        ) - timedelta(microseconds=1)

        people_count = 0
        settled_count = 0
        application_count = 0
        application_settled_count = 0
        stale_application_reservations = 0
        person_finalizations_written = 0
        application_finalizations_written = 0
        reservations_finalized_upper_bound = 0
        upper_bounds_settled = 0

        for request in self._repository.pending_billable_requests():
            partition = (
                partition_key(request["scope_id"], request["period_start"].strftime("%Y-%m"))
                if request["scope_type"] == "person"
                else application_partition_key(
                    request["scope_id"], request["period_start"].strftime("%Y-%m")
                )
            )
            entity = {"Reserved": request["reserved_tokens"]}
            if request.get("finalization_kind") == "unverified_upper_bound":
                entity["FinalizationKind"] = "unverified_upper_bound"
            self._store.upsert(
                partition, reservation_row_key(request["created_at"], str(request["id"])), entity
            )

        pending_partitions: dict[
            tuple[LedgerScopeType, str, date], tuple[str, list[LedgerReservation]]
        ] = {}
        for partition in self._store.list_pending_partitions():
            parsed = parse_budget_partition(partition)
            if parsed is None:
                logger.warning("Malformed Ledger budget partition skipped: %s", partition)
                continue
            pending_partitions[parsed] = (
                partition,
                list(self._store.list_reservations(partition)),
            )

        self._repository.register_budget_reservations([
            BudgetReservationAdmission(
                scope_type=scope_type, scope_id=scope_id, correlation_id=row.correlation_id,
                created_at=row.created_at, reserved_tokens=row.reserved_tokens,
            )
            for (scope_type, scope_id, _period), (_partition, rows) in pending_partitions.items()
            for row in rows
        ])

        for (scope_type, scope_id, period_start), (_, rows) in pending_partitions.items():
            written, stale, upper_bounds = self._recover_reservations(
                scope_type, scope_id, period_start, rows, moment
            )
            if scope_type == "person":
                person_finalizations_written += written
            else:
                application_finalizations_written += written
                stale_application_reservations += stale
            reservations_finalized_upper_bound += upper_bounds

        settled_before_projection = {
            identity: self._repository.settled_reservation_correlations(
                [reservation.correlation_id for reservation in rows],
                scope_type=identity[0], scope_id=identity[1],
            )
            for identity, (_partition, rows) in pending_partitions.items()
        }

        # Process every discovered pending partition, not just active rows in two months.
        # This is the recovery path for removed people, retired Applications, and older
        # periods. C is always projected before an R is marked or deleted.
        for (scope_type, scope_id, period_start), (
            partition,
            rows,
        ) in pending_partitions.items():
            period_end = (
                date(period_start.year + 1, 1, 1)
                if period_start.month == 12
                else date(period_start.year, period_start.month + 1, 1)
            )
            confirmed = self._repository.budget_scope_confirmed_tokens(
                scope_type, scope_id, period_start, period_end
            )
            next_period_end = (
                date(period_end.year + 1, 1, 1)
                if period_end.month == 12
                else date(period_end.year, period_end.month + 1, 1)
            )
            next_confirmed = self._repository.budget_scope_confirmed_tokens(
                scope_type, scope_id, period_end, next_period_end
            )
            if next_confirmed:
                next_partition = (
                    application_partition_key(scope_id, period_end.strftime("%Y-%m"))
                    if scope_type == "application"
                    else partition_key(scope_id, period_end.strftime("%Y-%m"))
                )
                self._store.upsert(
                    next_partition, "C", {"ConfirmedUsed": next_confirmed}
                )
            self._store.upsert(partition, "C", {"ConfirmedUsed": confirmed})
            settled = settled_before_projection[(scope_type, scope_id, period_start)]
            finalization_kinds = self._repository.reservation_finalization_kinds(
                scope_type,
                scope_id,
                [reservation.correlation_id for reservation in rows],
            )
            upper_bound_rows = [
                reservation
                for reservation in rows
                if reservation.finalization_kind is None
                and finalization_kinds.get(reservation.correlation_id)
                == "unverified_upper_bound"
            ]
            self._store.mark_upper_bounds(partition, upper_bound_rows)
            for reservation in upper_bound_rows:
                object.__setattr__(
                    reservation,
                    "finalization_kind",
                    "unverified_upper_bound",
                )
            to_delete = [
                reservation.row_key
                for reservation in rows
                if reservation.correlation_id in settled
            ]
            deleted = self._store.delete_reservations(partition, to_delete)
            pending_partitions[(scope_type, scope_id, period_start)] = (
                partition,
                [reservation for reservation in rows if reservation.row_key not in to_delete],
            )
            upper_bounds_settled += sum(
                reservation.finalization_kind == "unverified_upper_bound"
                and reservation.row_key in to_delete
                for reservation in rows
            )
            if scope_type == "person":
                settled_count += deleted
            else:
                application_settled_count += deleted

        # Current first: a request reserved just before midnight can land in the new
        # month's usage. Writing the current C before deleting the previous R preserves
        # conservative accounting if the Function fails between those two partitions.
        for period_moment in (moment, previous_moment):
            period_start, _, period = self._period_bounds(period_moment)

            people = self.snapshot(period_moment)
            people_count += len(people)
            for person in people:
                partition = partition_key(person.user_id, period)
                self._store.upsert(
                    partition,
                    "Q",
                    {"Limit": person.token_limit, "Enforce": person.enforce},
                )
                self._store.upsert(
                    partition,
                    "C",
                    {"ConfirmedUsed": person.confirmed_tokens},
                )
            applications = self.application_snapshot(period_moment)
            application_count += len(applications)
            application_states: list[GatewayApplicationLedgerState] = []
            application_finalization_states: list[dict[str, Any]] = []
            for application in applications:
                partition = application_partition_key(
                    application.application_id, period
                )
                application_id = UUID(application.application_id)
                if application.budget_mode == "tokens":
                    self._store.upsert(
                        partition,
                        "Q",
                        {
                            "Limit": application.token_limit,
                            "TokensPerMinute": application.tokens_per_minute,
                            "Enforce": application.enforce,
                        },
                    )
                    self._store.upsert(
                        partition,
                        "C",
                        {"ConfirmedUsed": application.confirmed_tokens},
                    )
                else:
                    # USD reservations are authoritative in the budget2 S/A/D rows;
                    # the legacy token partition must not publish a competing limit.
                    continue
                application_reservations = list(
                    pending_partitions.get(
                        ("application", application.application_id, period_start),
                        (partition, []),
                    )[1]
                )
                remaining_reservations = [
                    reservation
                    for reservation in application_reservations
                    if reservation.finalization_kind != "unverified_upper_bound"
                ]
                pending_tokens = sum(
                    reservation.reserved_tokens
                    for reservation in remaining_reservations
                )
                finalized_upper_bound_rows = [
                    reservation
                    for reservation in application_reservations
                    if (
                        reservation.finalization_kind == "unverified_upper_bound"
                    )
                ]
                application_finalization_states.append(
                    {
                        "period_start": period_start,
                        "application_id": application_id,
                        "finalized_upper_bound_tokens": sum(
                            reservation.reserved_tokens
                            for reservation in finalized_upper_bound_rows
                        ),
                        "finalized_upper_bound_count": len(
                            finalized_upper_bound_rows
                        ),
                        "snapshot_at": moment,
                    }
                )
                application_states.append(
                    GatewayApplicationLedgerState(
                        period_start=period_start,
                        application_id=application_id,
                        token_limit=application.token_limit,
                        confirmed_tokens=application.confirmed_tokens,
                        pending_reserved_tokens=pending_tokens,
                        pending_reservation_count=len(remaining_reservations),
                        stale_reservation_count=sum(
                            reservation.created_at <= moment - self._recovery_lag
                            for reservation in remaining_reservations
                        ),
                        oldest_reservation_at=min(
                            (
                                reservation.created_at
                                for reservation in remaining_reservations
                            ),
                            default=None,
                        ),
                        available_tokens=max(
                            application.token_limit
                            - application.confirmed_tokens
                            - pending_tokens,
                            0,
                        ),
                        snapshot_at=moment,
                    )
                )
            self._repository.save_gateway_application_ledger_snapshot(
                application_states, application_finalization_states,
            )
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
            reservations_settled=settled_count,
            model_policies=policies,
            applications=application_count,
            application_reservations_settled=application_settled_count,
            application_model_policies=application_policies,
            application_mappings=application_mappings,
            stale_application_reservations=stale_application_reservations,
            person_finalizations_written=person_finalizations_written,
            application_finalizations_written=application_finalizations_written,
            reservations_finalized_upper_bound=reservations_finalized_upper_bound,
            upper_bounds_settled=upper_bounds_settled,
        )
