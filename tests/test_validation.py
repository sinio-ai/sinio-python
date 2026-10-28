"""Local refusals: what the API would reject is refused before any call, naming the field."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

import pytest

from sinio import (
    ASSIGNMENT_RELATIONS,
    ENTITY_KINDS,
    EVENT_ROLES,
    INGESTION_MODES,
    LOCATION_LEVELS,
    REJECTION_CODES,
    EventRejection,
    HttpResponse,
    SinioClient,
    SinioValidationError,
)

EVENT: dict[str, Any] = {
    "source_event_id": "evt_1",
    "type": "badge_in",
    "occurred_at": "2026-08-28T07:58:12+02:00",
}
ENTITY: dict[str, Any] = {"external_id": "BORNE-A12", "kind": "device"}
LOCATION: dict[str, Any] = {"external_id": "SITE-A", "kind": "site", "name": "Site A"}
ASSIGNMENT: dict[str, Any] = {
    "credential": "04A2B9C1",
    "subject": "P-4582",
    "valid_from": "2026-01-01T00:00:00+01:00",
}


class Recorder:
    def __init__(self, body: str = '{"accepted": 1, "rejected": 0, "rejections": []}') -> None:
        self.calls = 0
        self.body = body

    def __call__(self, url: str, payload: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.calls += 1
        return HttpResponse(status=200, headers={}, body=self.body)


def client(sender: Recorder) -> SinioClient:
    return SinioClient(api_key="sk", base_url="https://api.test", sender=sender, sleep=lambda _: None)


def refused(call: Callable[[SinioClient], object], message: str) -> None:
    sender = Recorder()
    with pytest.raises(SinioValidationError) as raised:
        call(client(sender))
    assert str(raised.value) == message
    assert sender.calls == 0


def _call(method: str, items: list[dict[str, Any]], sinio: SinioClient) -> object:
    result: object = getattr(sinio, method)(items)
    return result


def test_vocabularies_match_the_api_contract() -> None:
    assert INGESTION_MODES == ("webhook", "backfill")
    assert ENTITY_KINDS == ("device", "subject", "asset", "actor", "credential")
    assert LOCATION_LEVELS == ("site", "zone", "point")
    assert ASSIGNMENT_RELATIONS == ("assigned_to",)
    assert EVENT_ROLES == ("device", "subject", "location", "actor")
    assert REJECTION_CODES == (
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
    )


def test_refuses_a_value_outside_its_vocabulary() -> None:
    refused(
        lambda c: c.upsert_entities([ENTITY, ENTITY, ENTITY, {**ENTITY, "kind": "devise"}]),
        'entities[3].kind must be one of device, subject, asset, actor, credential (got "devise")',
    )
    refused(
        lambda c: c.upsert_locations([{**LOCATION, "kind": "building"}]),
        'locations[0].kind must be one of site, zone, point (got "building")',
    )
    refused(
        lambda c: c.create_assignments([{**ASSIGNMENT, "relation": "owns"}]),
        'assignments[0].relation must be one of assigned_to (got "owns")',
    )
    refused(
        lambda c: c.send_events([EVENT], mode="live"),  # type: ignore[arg-type]
        'mode must be one of webhook, backfill (got "live")',
    )
    refused(
        lambda c: c.upsert_entities([{**ENTITY, "kind": 3}]),
        "entities[0].kind must be a string (got 3)",
    )


@pytest.mark.parametrize(
    ("method", "base", "collection", "field"),
    [
        ("send_events", EVENT, "events", "source_event_id"),
        ("send_events", EVENT, "events", "type"),
        ("send_events", EVENT, "events", "occurred_at"),
        ("upsert_entities", ENTITY, "entities", "external_id"),
        ("upsert_entities", ENTITY, "entities", "kind"),
        ("upsert_locations", LOCATION, "locations", "external_id"),
        ("upsert_locations", LOCATION, "locations", "kind"),
        ("upsert_locations", LOCATION, "locations", "name"),
        ("create_assignments", ASSIGNMENT, "assignments", "credential"),
        ("create_assignments", ASSIGNMENT, "assignments", "subject"),
        ("create_assignments", ASSIGNMENT, "assignments", "valid_from"),
    ],
)
def test_refuses_a_required_string_missing_or_empty(
    method: str, base: dict[str, Any], collection: str, field: str
) -> None:
    missing = {key: value for key, value in base.items() if key != field}
    for item in (missing, {**base, field: ""}, {**base, field: None}):
        refused(partial(_call, method, [base, item]), f"{collection}[1].{field} is required")


def test_refuses_an_unknown_field() -> None:
    refused(
        lambda c: c.upsert_entities([{**ENTITY, "type": "device"}]),
        'entities[0] has an unknown field "type"; expected one of external_id, kind, subkind, label, '
        "location, attributes",
    )


def test_refuses_an_empty_subkind_but_not_an_empty_label() -> None:
    refused(
        lambda c: c.upsert_entities([{**ENTITY, "subkind": ""}]),
        "entities[0].subkind must not be empty when given",
    )
    sender = Recorder()
    client(sender).upsert_entities([{**ENTITY, "label": ""}])
    assert sender.calls == 1


def test_refuses_an_unknown_or_empty_role() -> None:
    refused(
        lambda c: c.send_events([{**EVENT, "roles": {"badge": "04A2B9C1"}}]),
        'events[0].roles has an unknown role "badge"; expected one of device, subject, location, actor'
        " (anything else belongs in payload)",
    )
    refused(
        lambda c: c.send_events([{**EVENT, "roles": {"subject": ""}}]),
        "events[0].roles.subject must not be empty when given",
    )
    refused(
        lambda c: c.send_events([{**EVENT, "roles": {"device": 12}}]),
        "events[0].roles.device must be a string (got 12)",
    )


def test_refuses_a_payload_or_attributes_that_is_not_an_object() -> None:
    refused(
        lambda c: c.send_events([{**EVENT, "payload": [1, 2]}]),
        "events[0].payload must be a JSON object (got list)",
    )
    refused(
        lambda c: c.send_events([{**EVENT, "payload": "battery=12"}]),
        "events[0].payload must be a JSON object (got str)",
    )
    refused(
        lambda c: c.upsert_entities([{**ENTITY, "attributes": 4}]),
        "entities[0].attributes must be a JSON object (got int)",
    )


def test_validity_bounds_need_an_offset_and_an_order() -> None:
    refused(
        lambda c: c.create_assignments([{**ASSIGNMENT, "valid_from": "2026-01-01T00:00:00"}]),
        'assignments[0].valid_from must be an ISO 8601 timestamp with an offset (got "2026-01-01T00:00:00")',
    )
    refused(
        lambda c: c.create_assignments([{**ASSIGNMENT, "valid_to": "2026-02-30T00:00:00Z"}]),
        'assignments[0].valid_to is not a valid date and time (got "2026-02-30T00:00:00Z")',
    )
    refused(
        lambda c: c.create_assignments([{**ASSIGNMENT, "valid_from": datetime(2026, 1, 1)}]),
        "assignments[0].valid_from must be timezone-aware (got a naive datetime)",
    )
    # 2026-01-01T00:00:00+01:00 is 2025-12-31T23:00:00Z: equal is not after.
    for valid_to in ("2025-12-31T23:00:00Z", "2025-12-31T22:59:59.999+00:00"):
        refused(
            partial(_call, "create_assignments", [{**ASSIGNMENT, "valid_to": valid_to}]),
            "assignments[0].valid_to must be after valid_from",
        )


def test_accepts_a_well_formed_validity_period() -> None:
    sender = Recorder()
    later = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone(timedelta(hours=2)))
    client(sender).create_assignments(
        [
            {**ASSIGNMENT, "valid_to": "2025-12-31T23:00:00.5Z", "relation": "assigned_to"},
            {**ASSIGNMENT, "valid_to": later},
            {**ASSIGNMENT, "valid_to": "2026-12-31T23:59:59+0100"},
        ]
    )
    assert sender.calls == 1


def test_keeps_an_unknown_rejection_code_without_failing() -> None:
    sender = Recorder(
        '{"batch_id": "b_1", "accepted": 0, "rejected": 2, "future_counter": 3, "rejections": ['
        '{"source_event_id": "evt_1", "code": "source_on_fire", "message": "new", "hint": "x"},'
        '{"source_event_id": "evt_2", "code": "invalid_occurred_at", "message": "bad"}]}'
    )

    result = client(sender).send_events([EVENT, {**EVENT, "source_event_id": "evt_2"}])

    unknown, known = result.rejections
    assert unknown == EventRejection(source_event_id="evt_1", code="source_on_fire", message="new")
    assert unknown.known_code is None
    assert known.known_code == "invalid_occurred_at"


def test_results_are_immutable() -> None:
    sender = Recorder('{"batch_id": "b_1", "accepted": 1, "rejections": []}')
    result = client(sender).send_events([EVENT])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.accepted = 2  # type: ignore[misc]
