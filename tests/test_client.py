from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sinio import (
    Assignment,
    Entity,
    Event,
    EventRoles,
    HttpResponse,
    Location,
    SinioAuthError,
    SinioClient,
    SinioRateLimitError,
    SinioRequestError,
    SinioValidationError,
)


class StubSender:
    """Records the calls and replays a scripted list of responses."""

    def __init__(self, responses: list[tuple[int, dict[str, Any], dict[str, str] | None]]) -> None:
        self._responses = responses
        self._index = 0
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, url: str, payload: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.calls.append((url, headers, json.loads(payload.decode())))
        status, body, extra = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return HttpResponse(status=status, headers=extra or {}, body=json.dumps(body))


OK_EVENTS: tuple[int, dict[str, Any], dict[str, str] | None] = (
    200,
    {
        "batch_id": "b_1",
        "accepted": 1,
        "pending_classification": 0,
        "duplicates": 0,
        "revisions": 0,
        "rejected": 0,
        "rejections": [],
        "test_mode": False,
    },
    None,
)


def an_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {
        "source_event_id": "evt_1",
        "type": "badge_in",
        "occurred_at": "2026-08-28T07:58:12+02:00",
        "payload": {"badge": "04A2B9C1"},
    }
    defaults.update(overrides)
    return Event(**defaults)


def client_with(sender: StubSender, **kwargs: Any) -> SinioClient:
    options: dict[str, Any] = {"base_url": "https://api.test", "sender": sender, "sleep": lambda _: None}
    options.update(kwargs)
    return SinioClient(**options)


def test_sends_a_batch_with_bearer_auth_and_wire_field_names() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk_test", source_code="gmao_prod")

    result = client.send_events([an_event(roles=EventRoles(device="BORNE-A12"))])

    url, headers, body = sender.calls[0]
    assert url == "https://api.test/v1/events"
    assert headers["authorization"] == "Bearer sk_test"
    assert headers["x-source-code"] == "gmao_prod"
    assert body == {
        "events": [
            {
                "source_event_id": "evt_1",
                "type": "badge_in",
                "occurred_at": "2026-08-28T07:58:12+02:00",
                "roles": {"device": "BORNE-A12"},
                "payload": {"badge": "04A2B9C1"},
            }
        ],
        "mode": "webhook",
    }
    assert result.batch_ids == ["b_1"]
    assert result.accepted == 1
    assert result.test_mode is False


def test_says_when_the_key_is_in_test_mode() -> None:
    status, body, headers = OK_EVENTS
    sender = StubSender([(status, {**body, "test_mode": True}, headers)])
    client = client_with(sender, api_key="sk_test")

    result = client.send_events([an_event()])

    assert result.test_mode is True


def test_splits_above_500_events_and_merges_counters() -> None:
    sender = StubSender(
        [
            (200, {**OK_EVENTS[1], "batch_id": "b_1", "accepted": 500}, None),
            (
                200,
                {
                    "batch_id": "b_2",
                    "accepted": 100,
                    "pending_classification": 2,
                    "duplicates": 1,
                    "revisions": 0,
                    "rejected": 1,
                    "rejections": [
                        {"source_event_id": "evt_x", "code": "invalid_occurred_at", "message": "nope"}
                    ],
                },
                None,
            ),
        ]
    )
    client = client_with(sender, api_key="sk")

    result = client.send_events([an_event(source_event_id=f"evt_{index}") for index in range(601)])

    assert len(sender.calls) == 2
    assert len(sender.calls[0][2]["events"]) == 500
    assert len(sender.calls[1][2]["events"]) == 101
    assert result.accepted == 600
    assert result.pending_classification == 2
    assert result.batch_ids == ["b_1", "b_2"]
    assert result.rejections[0].source_event_id == "evt_x"


def test_applies_the_client_project_only_when_the_event_has_none() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk", project="client-acme")

    client.send_events([an_event(), an_event(source_event_id="evt_2", project="client-other")])

    sent = sender.calls[0][2]["events"]
    assert sent[0]["project"] == "client-acme"
    assert sent[1]["project"] == "client-other"


def test_accepts_plain_dicts_and_aware_datetimes() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk")

    moment = datetime(2026, 8, 28, 7, 58, 12, tzinfo=timezone(timedelta(hours=2)))
    client.send_events(
        [
            {
                "source_event_id": "evt_dict",
                "type": "badge_in",
                "occurred_at": moment,
                "roles": {"subject": "P-4582"},
            }
        ]
    )

    sent = sender.calls[0][2]["events"][0]
    assert sent["occurred_at"] == "2026-08-28T07:58:12+02:00"
    assert sent["roles"] == {"subject": "P-4582"}
    assert "payload" not in sent


def test_refuses_an_unknown_role_before_any_call() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk")

    with pytest.raises(SinioValidationError):
        client.send_events(
            [
                {
                    "source_event_id": "evt_dict",
                    "type": "badge_in",
                    "occurred_at": "2026-08-28T07:58:12+02:00",
                    "roles": {"badge": "04A2B9C1"},
                }
            ]
        )
    assert sender.calls == []


def test_backfill_declares_the_backfill_mode() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk")

    client.backfill_events([an_event()])

    assert sender.calls[0][2]["mode"] == "backfill"


def test_webhook_key_authenticates_through_the_url_and_blocks_reference_calls() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, webhook_key="whk_abc")

    client.send_events([an_event()])

    url, headers, _ = sender.calls[0]
    assert url == "https://api.test/v1/webhooks/whk_abc"
    assert "authorization" not in headers
    with pytest.raises(SinioValidationError):
        client.upsert_entities([])


def test_sends_the_count_an_event_stands_for_and_refuses_one_out_of_range() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk")

    client.send_events([an_event(count=10)])
    assert sender.calls[0][2]["events"][0]["count"] == 10

    for count in (0, 2.5, True, 1_000_001):
        with pytest.raises(SinioValidationError, match="count"):
            client.send_events([an_event(count=count)])
    assert len(sender.calls) == 1


def test_rejects_a_timestamp_without_offset_before_any_call() -> None:
    sender = StubSender([OK_EVENTS])
    client = client_with(sender, api_key="sk")

    with pytest.raises(SinioValidationError, match="offset"):
        client.send_events([an_event(occurred_at="2026-08-28T07:58:12")])
    with pytest.raises(SinioValidationError, match="timezone-aware"):
        client.send_events([an_event(occurred_at=datetime(2026, 8, 28, 7, 58, 12))])
    # What the API refuses too: a space for the `T`, no seconds, a lowercase `z`.
    for occurred_at in ("2026-08-28 07:58:12Z", "2026-08-28T07:58Z", "2026-08-28T07:58:12z"):
        with pytest.raises(SinioValidationError, match="offset"):
            client.send_events([an_event(occurred_at=occurred_at)])
    assert sender.calls == []


def test_upserts_reference_data_with_the_project_at_the_top_level() -> None:
    sender = StubSender([(200, {"accepted": 1, "rejected": 0, "rejections": []}, None)])
    client = client_with(sender, api_key="sk", project="client-acme")

    client.upsert_entities(
        [
            Entity(
                external_id="BORNE-A12", kind="device", subkind="borne_nfc", attributes={"firmware": "2.4.1"}
            )
        ]
    )
    client.upsert_locations([Location(external_id="SITE-A.HALL", kind="zone", name="Hall", parent="SITE-A")])

    assert sender.calls[0][2] == {
        "project": "client-acme",
        "entities": [
            {
                "external_id": "BORNE-A12",
                "kind": "device",
                "subkind": "borne_nfc",
                "attributes": {"firmware": "2.4.1"},
            }
        ],
    }
    assert sender.calls[1][2] == {
        "project": "client-acme",
        "locations": [{"external_id": "SITE-A.HALL", "kind": "zone", "name": "Hall", "parent": "SITE-A"}],
    }


def test_creates_assignments_and_surfaces_overlaps() -> None:
    sender = StubSender(
        [
            (
                200,
                {
                    "accepted": 0,
                    "rejected": 1,
                    "rejections": [
                        {
                            "credential": "04A2B9C1",
                            "subject": "P-4582",
                            "code": "overlapping_assignment",
                            "message": "overlap",
                        }
                    ],
                },
                None,
            )
        ]
    )
    client = client_with(sender, api_key="sk")

    result = client.create_assignments(
        [
            Assignment(
                credential="04A2B9C1",
                subject="P-4582",
                relation="assigned_to",
                valid_from="2026-01-01T00:00:00+01:00",
            )
        ]
    )

    assert sender.calls[0][2]["assignments"] == [
        {
            "credential": "04A2B9C1",
            "subject": "P-4582",
            "relation": "assigned_to",
            "valid_from": "2026-01-01T00:00:00+01:00",
        }
    ]
    assert result.rejections[0].code == "overlapping_assignment"


def test_retries_a_503_then_succeeds() -> None:
    sender = StubSender([(503, {"message": "down"}, None), OK_EVENTS])
    client = client_with(sender, api_key="sk")

    result = client.send_events([an_event()])

    assert len(sender.calls) == 2
    assert result.accepted == 1


def test_surfaces_a_429_with_its_retry_after() -> None:
    sender = StubSender([(429, {"message": "slow down"}, {"retry-after": "12"})])
    client = client_with(sender, api_key="sk", max_retries=1)

    with pytest.raises(SinioRateLimitError) as raised:
        client.send_events([an_event()])

    assert raised.value.retry_after_seconds == 12
    assert len(sender.calls) == 2


def test_does_not_retry_a_401_and_joins_validation_messages() -> None:
    sender = StubSender([(401, {"message": "Invalid or revoked API key"}, None)])
    client = client_with(sender, api_key="sk")

    with pytest.raises(SinioAuthError):
        client.send_events([an_event()])
    assert len(sender.calls) == 1

    other = client_with(
        StubSender([(400, {"message": ["type must be a string", "payload must be an object"]}, None)]),
        api_key="sk",
    )
    with pytest.raises(SinioRequestError, match="payload must be an object"):
        other.send_events([an_event()])


def test_requires_exactly_one_credential() -> None:
    with pytest.raises(SinioValidationError):
        SinioClient()
    with pytest.raises(SinioValidationError):
        SinioClient(api_key="sk", webhook_key="whk")
