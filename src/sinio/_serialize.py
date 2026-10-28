"""Mapping between the SDK dataclasses and the JSON wire format.

Every rule here mirrors one of the API's own: what the API would refuse is refused before any
call, with a :class:`SinioValidationError` naming the field and its index. No rule the API does
not have is added.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from .errors import SinioValidationError
from .models import (
    ASSIGNMENT_RELATIONS,
    ENTITY_KINDS,
    EVENT_ROLES,
    INGESTION_MODES,
    LOCATION_LEVELS,
    Assignment,
    Entity,
    Event,
    EventRoles,
    IngestionMode,
    Location,
    Timestamp,
)

#: The API caps every collection at 500 items per call.
MAX_ITEMS_PER_CALL = 500

#: The most units one event may stand for.
MAX_EVENT_COUNT = 1_000_000

# The API's own rule: a `T`, seconds, an optional fraction, then `Z` or an offset. It rejects the
# event otherwise (`invalid_occurred_at`); refused here, it is a fixable local error before any call.
_ISO_WITH_OFFSET = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|([+-])(\d{2}):?(\d{2}))$"
)

T = TypeVar("T")


def _show(value: Any) -> str:
    """A received value in an error message: strings in double quotes, like the other SDKs."""
    return json.dumps(value) if isinstance(value, str) else repr(value)


def to_iso(value: Timestamp, field: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise SinioValidationError(f"{field} must be timezone-aware (got a naive datetime)")
        return value.isoformat()
    if not isinstance(value, str) or not _ISO_WITH_OFFSET.match(value):
        raise SinioValidationError(
            f"{field} must be an ISO 8601 timestamp with an offset (got {_show(value)})"
        )
    return value


def _to_moment(value: Timestamp, field: str) -> tuple[str, datetime]:
    """A validity bound: the same form as ``occurred_at``, and a real calendar moment."""
    iso = to_iso(value, field)
    if isinstance(value, datetime):
        return iso, value
    match = _ISO_WITH_OFFSET.match(iso)
    assert match is not None  # to_iso checked it
    year, month, day, hour, minute, second, fraction, zone, sign, off_h, off_m = match.groups()
    try:
        offset = (
            timedelta(0)
            if zone == "Z"
            else (1 if sign == "+" else -1) * timedelta(hours=int(off_h), minutes=int(off_m))
        )
        moment = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            int((fraction or "0")[:6].ljust(6, "0")),
            tzinfo=timezone(offset),
        )
    except ValueError as error:
        raise SinioValidationError(f"{field} is not a valid date and time (got {_show(value)})") from error
    return iso, moment


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    """Drops ``None`` keys so they never reach the wire as JSON ``null``."""
    return {key: value for key, value in record.items() if value is not None}


def chunked(items: Sequence[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def coerce(value: Any, target: type[T], field: str) -> T:
    """Accepts either a dataclass instance or a mapping of its field names."""
    if isinstance(value, target):
        return value
    if isinstance(value, Mapping):
        names = [item.name for item in dataclasses.fields(target)]  # type: ignore[arg-type]
        for key in value:
            if key not in names:
                raise SinioValidationError(
                    f"{field} has an unknown field {_show(key)}; expected one of {', '.join(names)}"
                )
        try:
            return target(**value)
        except TypeError as error:
            # Only a missing required field is left: the unknown ones were refused above.
            required = [
                item.name
                for item in dataclasses.fields(target)  # type: ignore[arg-type]
                if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING
            ]
            missing = next((name for name in required if name not in value), None)
            if missing is not None:
                raise SinioValidationError(f"{field}.{missing} is required") from error
            raise SinioValidationError(f"{field}: {error}") from error
    if dataclasses.is_dataclass(value):
        raise SinioValidationError(f"{field} must be a {target.__name__}, got {type(value).__name__}")
    raise SinioValidationError(f"{field} must be a {target.__name__} or a mapping")


def check_mode(mode: Any) -> IngestionMode:
    return _one_of(mode, INGESTION_MODES, "mode")


def _one_of(value: Any, allowed: tuple[T, ...], field: str) -> T:
    for candidate in allowed:
        if isinstance(value, str) and value == candidate:
            return candidate
    raise SinioValidationError(f"{field} must be one of {', '.join(map(str, allowed))} (got {_show(value)})")


def _required_str(value: Any, field: str) -> str:
    if value is None or value == "":
        raise SinioValidationError(f"{field} is required")
    if not isinstance(value, str):
        raise SinioValidationError(f"{field} must be a string (got {_show(value)})")
    return value


def _optional_str(value: Any, field: str, *, non_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SinioValidationError(f"{field} must be a string (got {_show(value)})")
    if non_empty and value == "":
        raise SinioValidationError(f"{field} must not be empty when given")
    return value


def _optional_object(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SinioValidationError(f"{field} must be a JSON object (got {type(value).__name__})")
    return dict(value)


def _roles_to_wire(value: Any, field: str) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in value:
            if key not in EVENT_ROLES:
                raise SinioValidationError(
                    f"{field} has an unknown role {_show(key)}; expected one of {', '.join(EVENT_ROLES)}"
                    " (anything else belongs in payload)"
                )
    roles = coerce(value, EventRoles, field)
    wire: dict[str, str] = {}
    for role in EVENT_ROLES:
        identifier = _optional_str(getattr(roles, role), f"{field}.{role}", non_empty=True)
        if identifier is not None:
            wire[role] = identifier
    return wire


def _count(value: Any, field: str) -> int | None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_EVENT_COUNT
    ):
        raise SinioValidationError(f"{field} must be an integer between 1 and {MAX_EVENT_COUNT}")
    return value


def event_to_wire(value: Any, index: int) -> dict[str, Any]:
    at = f"events[{index}]"
    event = coerce(value, Event, at)
    source_event_id = _required_str(event.source_event_id, f"{at}.source_event_id")
    event_type = _required_str(event.type, f"{at}.type")
    if event.occurred_at is None or event.occurred_at == "":
        raise SinioValidationError(f"{at}.occurred_at is required")
    return _compact(
        {
            "source_event_id": source_event_id,
            "source_version": _optional_str(event.source_version, f"{at}.source_version"),
            "project": _optional_str(event.project, f"{at}.project"),
            "type": event_type,
            "label": _optional_str(event.label, f"{at}.label"),
            "occurred_at": to_iso(event.occurred_at, f"{at}.occurred_at"),
            "roles": _roles_to_wire(event.roles, f"{at}.roles"),
            "payload": _optional_object(event.payload, f"{at}.payload"),
            "count": _count(event.count, f"{at}.count"),
        }
    )


def entity_to_wire(value: Any, index: int) -> dict[str, Any]:
    at = f"entities[{index}]"
    entity = coerce(value, Entity, at)
    external_id = _required_str(entity.external_id, f"{at}.external_id")
    _required_str(entity.kind, f"{at}.kind")
    return _compact(
        {
            "external_id": external_id,
            "kind": _one_of(entity.kind, ENTITY_KINDS, f"{at}.kind"),
            "subkind": _optional_str(entity.subkind, f"{at}.subkind", non_empty=True),
            "label": _optional_str(entity.label, f"{at}.label"),
            "location": _optional_str(entity.location, f"{at}.location"),
            "attributes": _optional_object(entity.attributes, f"{at}.attributes"),
        }
    )


def location_to_wire(value: Any, index: int) -> dict[str, Any]:
    at = f"locations[{index}]"
    location = coerce(value, Location, at)
    external_id = _required_str(location.external_id, f"{at}.external_id")
    _required_str(location.kind, f"{at}.kind")
    kind = _one_of(location.kind, LOCATION_LEVELS, f"{at}.kind")
    return _compact(
        {
            "external_id": external_id,
            "kind": kind,
            "name": _required_str(location.name, f"{at}.name"),
            "parent": _optional_str(location.parent, f"{at}.parent"),
        }
    )


def assignment_to_wire(value: Any, index: int) -> dict[str, Any]:
    at = f"assignments[{index}]"
    assignment = coerce(value, Assignment, at)
    credential = _required_str(assignment.credential, f"{at}.credential")
    subject = _required_str(assignment.subject, f"{at}.subject")
    relation = (
        None
        if assignment.relation is None
        else _one_of(assignment.relation, ASSIGNMENT_RELATIONS, f"{at}.relation")
    )
    if assignment.valid_from is None or assignment.valid_from == "":
        raise SinioValidationError(f"{at}.valid_from is required")
    valid_from, starts = _to_moment(assignment.valid_from, f"{at}.valid_from")
    valid_to: str | None = None
    if assignment.valid_to is not None:
        valid_to, ends = _to_moment(assignment.valid_to, f"{at}.valid_to")
        # The API's `invalid_validity_period`, refused here before any call.
        if ends <= starts:
            raise SinioValidationError(f"{at}.valid_to must be after valid_from")
    return _compact(
        {
            "credential": credential,
            "subject": subject,
            "relation": relation,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
    )
