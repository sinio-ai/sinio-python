# sinio — Python

Official client for the sinio.ai ingestion API. No runtime dependencies (stdlib
`urllib`), Python 3.10+, fully typed (`mypy --strict` clean).

```bash
pip install sinio
```

## Quick start

```python
from sinio import SinioClient, Event, EventRoles

sinio = SinioClient(
    api_key=os.environ["SINIO_API_KEY"],
    base_url="https://api.sinio.ai",
)

result = sinio.send_events([
    Event(
        source_event_id="evt_88213",
        type="badge_in",
        occurred_at="2026-08-28T07:58:12+02:00",
        roles=EventRoles(device="BORNE-A12", subject="P-4582", location="SITE-A.HALL"),
        payload={"badge": "04A2B9C1", "reader_firmware": "2.4.1"},
    ),
])

# The four roles (device, subject, location, actor) are read from `roles` only;
# any other key is refused. `payload` is free and optional ({} by default).

print(result.accepted, result.duplicates, result.rejections)
```

Plain dicts work too, and `occurred_at` accepts a timezone-aware `datetime`:

```python
sinio.send_events([
    {"source_event_id": "evt_1", "type": "badge_in",
     "occurred_at": datetime.now(timezone.utc), "roles": {"subject": "04A2B9C1"}},
])
```

## What the client does for you

- **Chunking.** Pass 50 000 events; the SDK splits them into calls of 500 (the API
  limit) and merges the counters and rejections into one result.
- **Retries.** 429, 5xx and dropped connections are retried with exponential
  backoff and full jitter, honouring `Retry-After`. This is safe because
  a resend with the same `source_event_id` and the same content is counted in
  `duplicates`, never stored twice.
- **Local validation.** What the API would refuse raises `SinioValidationError`
  before any call, naming the field and its index: a `kind`, `relation` or `mode`
  outside its vocabulary, a missing or empty required field, an unknown role, a
  `payload` that is not an object, a naive `datetime` or a timestamp without an
  offset, a `valid_to` not after `valid_from`.
- **Typed errors.** `SinioAuthError` (401/403, do not replay),
  `SinioRequestError` (400/404/413, fix the request), `SinioRateLimitError`
  (429, carries `retry_after_seconds`), `SinioServerError` (5xx, replayable),
  `SinioConnectionError`, `SinioValidationError`.

## Partial acceptance

A 200 means the batch was **received**, not derived — derivation is asynchronous.
Within a batch, valid events are accepted even when others are rejected, so always
read `rejections`:

```python
for rejection in result.rejections:
    log.warning("%s %s %s", rejection.source_event_id, rejection.code, rejection.message)
```

`duplicates` and `revisions` are successes, not failures.

Every closed vocabulary is exported, as a `Literal` type and the tuple of its values:
`EntityKind` / `ENTITY_KINDS`, `LocationLevel` / `LOCATION_LEVELS`, `IngestionMode` /
`INGESTION_MODES`, `AssignmentRelation` / `ASSIGNMENT_RELATIONS`, `EventRole` / `EVENT_ROLES`,
`RejectionCode` / `REJECTION_CODES`. A rejection's `code` stays a `str`, since the API may add
codes; `known_code` narrows it, or is `None` for a code newer than the SDK:

```python
from sinio import ENTITY_KINDS

assert "device" in ENTITY_KINDS
if rejection.known_code == "unknown_location":
    sinio.upsert_locations(missing_locations)
```

## Reference data

```python
from sinio import Assignment, Entity, Location

sinio.upsert_locations([
    Location(external_id="SITE-A", kind="site", name="Site A"),
    Location(external_id="SITE-A.HALL", kind="zone", name="Hall", parent="SITE-A"),
])

sinio.upsert_entities([
    Entity(external_id="BORNE-A12", kind="device", subkind="nfc_kiosk",
           location="SITE-A.HALL", attributes={"firmware": "2.4.1"}),
])

sinio.create_assignments([
    Assignment(credential="04A2B9C1", subject="P-4582", relation="assigned_to",
               valid_from="2026-01-01T00:00:00+01:00"),
])
```

The reference is never a condition of acceptance: an event pointing at an unknown
device is still accepted. Upserting is how you enrich it, not how you gate it.

## Backfill

```python
sinio.backfill_events(history)
```

`mode="backfill"` persists history without raising alerts; once it has arrived, the
project's processes replay it in a canonical order. Live catch-up after an outage should stay on
`send_events` — the declared mode decides alerting, not the age of the events.

## Webhook key

For systems that can only be pointed at a URL and cannot set a header:

```python
sinio = SinioClient(webhook_key=os.environ["SINIO_WEBHOOK_KEY"])
sinio.send_events(events)  # POST /v1/webhooks/<key>
```

In this mode only event ingestion is available; the reference endpoints require an
API key.


## Bring your own HTTP client

`sender` replaces the stdlib transport — useful in a project that already depends
on `requests` or `httpx`, or to stub the network in tests:

```python
from sinio import HttpResponse

def httpx_sender(url, payload, headers, timeout):
    response = httpx.post(url, content=payload, headers=headers, timeout=timeout)
    return HttpResponse(response.status_code, dict(response.headers), response.text)

sinio = SinioClient(api_key, sender=httpx_sender)
```

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m mypy
.venv/bin/python -m ruff check .
```
