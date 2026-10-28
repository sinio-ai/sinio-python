"""Request and response types of the ingestion API.

Every closed vocabulary of the API is defined once here, as a ``Literal`` type and the tuple of its
values (read from the ``Literal`` itself, so the two never drift). The SDK checks requests against
these tuples before any call; responses keep any value the API may add later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args

#: Ingestion mode. ``backfill`` persists history without triggering alerts.
IngestionMode = Literal["webhook", "backfill"]
INGESTION_MODES: tuple[IngestionMode, ...] = get_args(IngestionMode)

#: Kinds accepted by ``POST /v1/entities``.
EntityKind = Literal["device", "subject", "asset", "actor", "credential"]
ENTITY_KINDS: tuple[EntityKind, ...] = get_args(EntityKind)

#: Levels accepted by ``POST /v1/locations``.
LocationLevel = Literal["site", "zone", "point"]
LOCATION_LEVELS: tuple[LocationLevel, ...] = get_args(LocationLevel)

#: Relations accepted by ``POST /v1/assignments`` (optional, ``assigned_to`` server-side when omitted).
AssignmentRelation = Literal["assigned_to"]
ASSIGNMENT_RELATIONS: tuple[AssignmentRelation, ...] = get_args(AssignmentRelation)

#: The four structural roles of an event (the keys of :class:`EventRoles`).
EventRole = Literal["device", "subject", "location", "actor"]
EVENT_ROLES: tuple[EventRole, ...] = get_args(EventRole)

#: Rejection codes the API knows today. The list may grow (additive changes), so a rejection's
#: ``code`` stays a plain ``str``; :attr:`EventRejection.known_code` narrows it to this type.
RejectionCode = Literal[
    "payload_too_large",
    "unknown_project",
    "unknown_source",
    "archived_source",
    "source_mismatch",
    "ambiguous_source",
    "invalid_occurred_at",
    "missing_required_field",
    "unknown_location",
    "unknown_parent_location",
    "overlapping_assignment",
    "invalid_validity_period",
    "trial_ended_quota",
]
REJECTION_CODES: tuple[RejectionCode, ...] = get_args(RejectionCode)

#: An ISO 8601 string *with* an offset, or a timezone-aware ``datetime``.
Timestamp = str | datetime


def _known_code(code: str) -> RejectionCode | None:
    for known in REJECTION_CODES:
        if known == code:
            return known
    return None


@dataclass
class EventRoles:
    """The four structural roles the derivation engine resolves.

    Each is a raw identifier from the source, a non-empty string. Anything else
    (ticket number, firmware, ...) belongs in ``payload``; the API refuses any other role.
    """

    device: str | None = None
    subject: str | None = None
    location: str | None = None
    actor: str | None = None


@dataclass
class Event:
    #: Identity at the source, stable over time. Part of the idempotency key.
    source_event_id: str
    #: ``code`` of an event type in the catalog.
    type: str
    #: ISO 8601 with offset. Without an offset the API rejects the event.
    occurred_at: Timestamp
    #: Free-form JSON object, optional (``{}`` server-side when omitted); readings (battery,
    #: signal, ...) are read from it as its source declares them.
    payload: dict[str, Any] | None = None
    #: The four roles, read from here only, never from ``payload``.
    roles: EventRoles | None = None
    #: Source-side version, kept for tracing: it plays no part in deduplication.
    source_version: str | None = None
    #: Project ``external_id``, only for organization-wide keys (issued on request) covering
    #: several projects; a project key already names its project.
    project: str | None = None
    label: str | None = None
    #: How many units this one event stands for, from 1 to 1,000,000. ``None`` = 1.
    count: int | None = None


@dataclass
class Entity:
    external_id: str
    #: One of :data:`ENTITY_KINDS`.
    kind: EntityKind
    #: Non-empty when given. An unknown one is created undeclared, never refused.
    subkind: str | None = None
    label: str | None = None
    #: External id of the location the entity sits in.
    location: str | None = None
    #: Free-form JSON object.
    attributes: dict[str, Any] | None = None


@dataclass
class Location:
    external_id: str
    #: One of :data:`LOCATION_LEVELS`.
    kind: LocationLevel
    name: str
    #: External id of the parent location, if any.
    parent: str | None = None


@dataclass
class Assignment:
    #: External id of the credential (badge, card, ...).
    credential: str
    #: External id of the subject the credential is assigned to.
    subject: str
    #: ISO 8601 with offset, or a timezone-aware ``datetime``.
    valid_from: Timestamp
    #: Same form, after ``valid_from``. ``None`` = open-ended.
    valid_to: Timestamp | None = None
    #: One of :data:`ASSIGNMENT_RELATIONS`.
    relation: AssignmentRelation | None = None


@dataclass(frozen=True)
class EventRejection:
    source_event_id: str
    #: A :data:`RejectionCode`, or a code added to the API after this SDK version.
    code: str
    message: str

    @property
    def known_code(self) -> RejectionCode | None:
        """``code`` as a :data:`RejectionCode`, or ``None`` when this SDK version does not know it."""
        return _known_code(self.code)


@dataclass(frozen=True)
class ReferenceRejection:
    external_id: str
    #: A :data:`RejectionCode`, or a code added to the API after this SDK version.
    code: str
    message: str

    @property
    def known_code(self) -> RejectionCode | None:
        """``code`` as a :data:`RejectionCode`, or ``None`` when this SDK version does not know it."""
        return _known_code(self.code)


@dataclass(frozen=True)
class AssignmentRejection:
    credential: str
    subject: str
    #: A :data:`RejectionCode`, or a code added to the API after this SDK version.
    code: str
    message: str

    @property
    def known_code(self) -> RejectionCode | None:
        """``code`` as a :data:`RejectionCode`, or ``None`` when this SDK version does not know it."""
        return _known_code(self.code)


@dataclass(frozen=True)
class SendEventsResult:
    """Outcome of :meth:`SinioClient.send_events`.

    ``duplicates`` and ``revisions`` are successes, not errors: replaying a
    batch is expected and never stores anything twice.
    """

    #: One server-side batch id per HTTP call the SDK made (batches are chunked at 500).
    batch_ids: list[str] = field(default_factory=list)
    accepted: int = 0
    pending_classification: int = 0
    duplicates: int = 0
    revisions: int = 0
    rejected: int = 0
    rejections: list[EventRejection] = field(default_factory=list)
    #: The key is in test mode: its events are checked and kept apart, never used for detection.
    test_mode: bool = False


@dataclass(frozen=True)
class UpsertResult:
    accepted: int = 0
    rejected: int = 0
    rejections: list[ReferenceRejection] = field(default_factory=list)


@dataclass(frozen=True)
class AssignmentsResult:
    accepted: int = 0
    rejected: int = 0
    rejections: list[AssignmentRejection] = field(default_factory=list)
