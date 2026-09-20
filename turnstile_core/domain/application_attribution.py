"""Work out who holds a gateway subscription, and how confident we are about it.

Attribution used to arrive in request headers. At an install where nothing sets them every
request lands on `unattributed`, which the budget rollup discards -- so department and person
budgets counted zero no matter what anyone configured. The subscription itself is the one thing
the gateway identifies on every call, so that is where attribution has to come from.

Two questions, deliberately kept apart because the evidence for them is not equally good:

**Which department** -- answerable for every subscription, because a department is assigned by an
administrator rather than read out of a name. Nothing here guesses it; `department_for_owner`
only carries a department across to a second key held by the same person.

**Which person** -- answerable only when a real email is in evidence. `governance_directory`
requires an `@` in a person id, and that rule is load-bearing: it is what keeps machine identities
like `system-runtime-health-check` out of the person list and off the budget screens. Inventing
`dongyuli-IT@example.com` to satisfy it would put a fabricated address in front of an
administrator as though it were a fact.

So `derive_owner` returns an owner only when it has one, and `person_group` separately returns a
grouping key for every subscription whether or not an email was found. The grouping key is what
surfaces "these two keys look like one person" on screen -- which at the install this was built
for is 52 people holding 104 keys, none of whom carry an email anywhere in their APIM record.
Those 52 are exactly the people for whom a per-subscription budget is not a per-person budget,
and the honest thing is to show an administrator the pairing rather than to guess an identity for
it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import Field

from .models import StrictModel

AttributionSource = Literal["apim", "derived", "manual"]

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Suffixes that say what a key is *for* rather than who holds it. Stripping them is what makes
# "Cecilia Ying" and "Cecilia Ying - Databricks" read as one person with two keys. The list is
# deliberately literal: a general "strip anything after a dash" rule would merge `a-zagainov-IT`
# with `a-zagainov-Finance`, who may well be two different people.
USAGE_SUFFIXES = (
    re.compile(r"\s*-\s*Databricks\s*$", re.IGNORECASE),
    re.compile(r"\s*-\s*Foundry\s*$", re.IGNORECASE),
    re.compile(r"\s*-?\s*IT\s+Databricks\s+Claude\s+API.*$", re.IGNORECASE),
    re.compile(r"\s*-?\s*API\s+IT\s+Databricks.*$", re.IGNORECASE),
    re.compile(r"\s*subscription\s*$", re.IGNORECASE),
)

USAGE_PREFIXES = (re.compile(r"^\s*Subscription\s+for\b\s*", re.IGNORECASE),)


class OwnerDerivation(StrictModel):
    """What a single subscription's name and APIM record say about who holds it."""

    owner_id: str | None = Field(default=None, max_length=255)
    owner_source: AttributionSource | None = None
    person_group: str | None = Field(default=None, max_length=255)


def _strip_usage_markers(display_name: str) -> str:
    value = display_name.strip()
    for _ in range(3):
        for pattern in USAGE_PREFIXES:
            value = pattern.sub("", value).strip(" -")
        for pattern in USAGE_SUFFIXES:
            value = pattern.sub("", value).strip(" -")
    return value


def person_group(display_name: str) -> str | None:
    """A key that two subscriptions share when one person appears to hold both.

    Case-folded because APIM display names are typed by hand and `David-IT` and `david-IT` are
    the same person. Returns None when nothing survives the strip, which happens for a name that
    was only ever a usage marker.
    """
    stripped = _strip_usage_markers(display_name)
    email = EMAIL_PATTERN.search(stripped)
    if email is not None:
        return email.group(0).casefold()
    return stripped.casefold() or None


def derive_owner(
    display_name: str,
    *,
    apim_owner_email: str | None = None,
) -> OwnerDerivation:
    """Read an owner off a subscription, preferring what APIM knows over what the name suggests.

    `apim_owner_email` is the email of the APIM user the subscription's `ownerId` points at --
    the customer's own record of who holds the key, so it wins whenever it is present. The name
    is a fallback, and only when it actually contains an email; a name like `dongyuli-IT` yields
    a group key and no owner, because the alternative is to make an address up.
    """
    group = person_group(display_name)
    if apim_owner_email and "@" in apim_owner_email:
        return OwnerDerivation(
            owner_id=apim_owner_email.strip(),
            owner_source="apim",
            person_group=apim_owner_email.strip().casefold(),
        )
    match = EMAIL_PATTERN.search(display_name)
    if match is not None:
        return OwnerDerivation(
            owner_id=match.group(0),
            owner_source="derived",
            person_group=group,
        )
    return OwnerDerivation(owner_id=None, owner_source=None, person_group=group)


def department_for_owner(
    owner_id: str | None,
    person_group_key: str | None,
    existing: Iterable[Mapping[str, Any]],
) -> str | None:
    """Carry a department across to another key held by the same person.

    A second key issued to someone who already has one almost always belongs to the same part of
    the organization, and asking an administrator the same question twice for the same person is
    the kind of configuration that does not get done. Only an existing assignment is copied --
    this never invents a department, and it returns None the moment the person's existing keys
    disagree, because picking one of two answers silently is worse than leaving it blank.
    """
    departments: set[str] = set()
    for row in existing:
        row_department = (row.get("department_id") or "").strip()
        if not row_department:
            continue
        row_owner = (row.get("owner_id") or "").strip()
        row_group = (row.get("person_group") or "").strip()
        same_person = bool(
            owner_id and row_owner and row_owner.casefold() == owner_id.casefold()
        )
        same_group = bool(
            person_group_key and row_group and row_group == person_group_key
        )
        if same_person or same_group:
            departments.add(row_department)
    if len(departments) == 1:
        return departments.pop()
    return None
