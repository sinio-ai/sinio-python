"""Client for the sinio.ai ingestion API."""

from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Callable, Sequence
from typing import Any

from ._http import Sender, Transport
from ._serialize import (
    MAX_ITEMS_PER_CALL,
    assignment_to_wire,
    check_mode,
    chunked,
    entity_to_wire,
    event_to_wire,
    location_to_wire,
)
from .errors import SinioValidationError
from .models import (
    AssignmentRejection,
    AssignmentsResult,
    Event,
    EventRejection,
    IngestionMode,
    ReferenceRejection,
    SendEventsResult,
    UpsertResult,
)

SDK_VERSION = "0.1.0"
DEFAULT_BASE_URL = "https://api.sinio.ai"


class SinioClient:
    """Client for the sinio.ai ingestion API.

    Every method accepts an arbitrarily long sequence and splits it into calls
    of 500 items, the API limit, merging the counters and rejections back into
    a single result. Sending the same events twice is safe: idempotency is
    keyed on ``(project, source, source_event_id, revision)``, so a replay is
    counted as a duplicate rather than stored again.

    :param api_key: machine credential, sent as ``Authorization: Bearer``.
    :param webhook_key: key carried in the URL instead of a header, for systems
        that can only be pointed at a webhook URL. Only :meth:`send_events` is
        available in this mode. Mutually exclusive with ``api_key``.
    :param source_code: sent as ``X-Source-Code``; only for organization-wide keys
        (issued on request), which are not bound to one source. A project key
        already names its source.
    :param project: default project ``external_id``; only for organization-wide
        keys (issued on request) covering several projects.
    :param sender: transport seam, to plug ``requests``/``httpx`` or a stub.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        webhook_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        source_code: str | None = None,
        project: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        sender: Sender | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if bool(api_key) == bool(webhook_key):
            raise SinioValidationError("Provide exactly one of `api_key` or `webhook_key`.")

        self._webhook_mode = bool(webhook_key)
        self._auth_headers: dict[str, str] = {} if webhook_key else {"authorization": f"Bearer {api_key}"}
        self._events_path = (
            f"/v1/webhooks/{urllib.parse.quote(webhook_key, safe='')}" if webhook_key else "/v1/events"
        )
        self._source_code = source_code
        self._project = project
        self._transport = Transport(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            user_agent=f"sinio-python/{SDK_VERSION}",
            sender=sender,
            sleep=sleep,
        )

    def send_events(
        self,
        events: Sequence[Any],
        *,
        mode: IngestionMode = "webhook",
        source_code: str | None = None,
    ) -> SendEventsResult:
        """``POST /v1/events`` -- live ingestion.

        Derivation is asynchronous: a 200 confirms receipt, not derivation. Within
        a batch, valid events are accepted even when others are rejected, so read
        ``result.rejections``. Each call gets a ``batch_id`` from sinio.ai,
        collected in ``result.batch_ids``: quote it to support.

        ``mode`` is one of :data:`INGESTION_MODES`. ``source_code`` overrides the
        client's, for organization-wide keys (issued on request). Anything the API
        would refuse raises :class:`SinioValidationError` before any call.
        """
        checked_mode = check_mode(mode)
        wire = [event_to_wire(self._with_default_project(event), index) for index, event in enumerate(events)]

        headers = dict(self._auth_headers)
        effective_source = source_code or self._source_code
        if effective_source is not None:
            headers["x-source-code"] = effective_source

        batch_ids: list[str] = []
        rejections: list[EventRejection] = []
        accepted = pending_classification = duplicates = revisions = rejected = 0
        test_mode = False
        for part in chunked(wire, MAX_ITEMS_PER_CALL):
            body: dict[str, Any] = {"events": part, "mode": checked_mode}
            response = _as_dict(self._transport.post(self._events_path, body, headers))
            returned_batch_id = response.get("batch_id")
            if isinstance(returned_batch_id, str):
                batch_ids.append(returned_batch_id)
            accepted += _as_int(response.get("accepted"))
            pending_classification += _as_int(response.get("pending_classification"))
            duplicates += _as_int(response.get("duplicates"))
            revisions += _as_int(response.get("revisions"))
            rejected += _as_int(response.get("rejected"))
            test_mode = test_mode or response.get("test_mode") is True
            rejections.extend(
                EventRejection(
                    source_event_id=_as_str(item.get("source_event_id")),
                    code=_as_str(item.get("code"), "unknown"),
                    message=_as_str(item.get("message")),
                )
                for item in _as_dicts(response.get("rejections"))
            )
        return SendEventsResult(
            batch_ids=batch_ids,
            accepted=accepted,
            pending_classification=pending_classification,
            duplicates=duplicates,
            revisions=revisions,
            rejected=rejected,
            rejections=rejections,
            test_mode=test_mode,
        )

    def backfill_events(
        self,
        events: Sequence[Any],
        *,
        source_code: str | None = None,
    ) -> SendEventsResult:
        """Same endpoint with ``mode="backfill"``.

        History is persisted but nothing is alerted; derivation is replayed later
        in a canonical order. Live catch-up after an outage should stay on
        :meth:`send_events` -- the declared mode decides alerting, not the age of
        the events.
        """
        return self.send_events(events, mode="backfill", source_code=source_code)

    def upsert_entities(self, entities: Sequence[Any], *, project: str | None = None) -> UpsertResult:
        """``POST /v1/entities`` -- bulk upsert keyed on ``(kind, external_id)``.

        An unknown ``subkind`` never fails the call: it is created undeclared, for
        someone to qualify in the console.

        ``project`` overrides the client's default, for organization-wide keys
        (issued on request). Anything the API would refuse raises
        :class:`SinioValidationError` before any call.
        """
        wire = [entity_to_wire(entity, index) for index, entity in enumerate(entities)]
        return self._upsert_reference("/v1/entities", "entities", wire, project)

    def upsert_locations(self, locations: Sequence[Any], *, project: str | None = None) -> UpsertResult:
        """``POST /v1/locations`` -- bulk upsert of the location tree, keyed on ``external_id``.

        ``project`` overrides the client's default, for organization-wide keys
        (issued on request). Anything the API would refuse raises
        :class:`SinioValidationError` before any call.
        """
        wire = [location_to_wire(location, index) for index, location in enumerate(locations)]
        return self._upsert_reference("/v1/locations", "locations", wire, project)

    def create_assignments(
        self, assignments: Sequence[Any], *, project: str | None = None
    ) -> AssignmentsResult:
        """``POST /v1/assignments`` -- ``credential -> subject`` with a validity range.

        An overlap is rejected with ``overlapping_assignment``, never merged silently.

        ``project`` overrides the client's default, for organization-wide keys
        (issued on request). Anything the API would refuse raises
        :class:`SinioValidationError` before any call.
        """
        self._assert_bearer_mode("create_assignments")
        wire = [assignment_to_wire(item, index) for index, item in enumerate(assignments)]

        rejections: list[AssignmentRejection] = []
        accepted = rejected = 0
        for part in chunked(wire, MAX_ITEMS_PER_CALL):
            response = _as_dict(
                self._transport.post(
                    "/v1/assignments",
                    self._with_project({"assignments": part}, project),
                    dict(self._auth_headers),
                )
            )
            accepted += _as_int(response.get("accepted"))
            rejected += _as_int(response.get("rejected"))
            rejections.extend(
                AssignmentRejection(
                    credential=_as_str(item.get("credential")),
                    subject=_as_str(item.get("subject")),
                    code=_as_str(item.get("code"), "unknown"),
                    message=_as_str(item.get("message")),
                )
                for item in _as_dicts(response.get("rejections"))
            )
        return AssignmentsResult(accepted=accepted, rejected=rejected, rejections=rejections)

    def _upsert_reference(
        self,
        path: str,
        key: str,
        wire: list[dict[str, Any]],
        project: str | None,
    ) -> UpsertResult:
        self._assert_bearer_mode(path)
        rejections: list[ReferenceRejection] = []
        accepted = rejected = 0
        for part in chunked(wire, MAX_ITEMS_PER_CALL):
            response = _as_dict(
                self._transport.post(path, self._with_project({key: part}, project), dict(self._auth_headers))
            )
            accepted += _as_int(response.get("accepted"))
            rejected += _as_int(response.get("rejected"))
            rejections.extend(
                ReferenceRejection(
                    external_id=_as_str(item.get("external_id")),
                    code=_as_str(item.get("code"), "unknown"),
                    message=_as_str(item.get("message")),
                )
                for item in _as_dicts(response.get("rejections"))
            )
        return UpsertResult(accepted=accepted, rejected=rejected, rejections=rejections)

    def _with_default_project(self, event: Any) -> Any:
        if self._project is None:
            return event
        if isinstance(event, Event):
            return event if event.project is not None else dataclasses.replace(event, project=self._project)
        if isinstance(event, dict) and event.get("project") is None:
            return {**event, "project": self._project}
        return event

    def _with_project(self, body: dict[str, Any], project: str | None) -> dict[str, Any]:
        effective = project or self._project
        return body if effective is None else {"project": effective, **body}

    def _assert_bearer_mode(self, operation: str) -> None:
        if self._webhook_mode:
            raise SinioValidationError(
                f"{operation} requires an API key: a webhook key only authenticates event ingestion."
            )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default
