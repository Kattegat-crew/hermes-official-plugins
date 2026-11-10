"""Metadata-only Langfuse v2 ingestion over the installed SDK's low-level REST client."""
from __future__ import annotations

import json
import logging
import re
import time
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MAX_BATCH_BYTES = 512 * 1024
_MAX_BATCH_EVENTS = 128
_MAX_QUEUED_EVENTS = 128
_MAX_ATTEMPTS = 2
_FINISH_WAIT_SECONDS = 0.05
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")
_SECRET_PREFIX = re.compile(r"(?i)^(?:pk-lf-|sk-lf-|sk-|pk_|sk_|gh[pousr]_|github_pat_|xox[baprs]-|bearer\s)")
_METADATA_KEYS = frozenset({
    "source", "platform", "provider", "model", "api_mode", "profile", "turn_id",
    "api_request_id", "session_id", "trace_id", "parentObservationId",
})
_LEVELS = frozenset({"DEBUG", "DEFAULT", "WARNING", "ERROR"})


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identifier(value: Any) -> str | None:
    if (isinstance(value, str) and _IDENTIFIER.fullmatch(value)
            and not _SECRET_PREFIX.search(value)
            and not re.search(r"(?i)(password|secret|api[_-]?key|token=)", value)):
        return value
    return None


def _safe_metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: safe for key, raw in value.items()
        if key in _METADATA_KEYS and (safe := _identifier(raw)) is not None
    }


def _safe_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage: dict[str, int] = {}
    for source, target in (("input", "input"), ("input_tokens", "input"),
                           ("output", "output"), ("output_tokens", "output")):
        count = value.get(source)
        if type(count) is int and count >= 0:
            usage[target] = count
    result = {key: usage[key] for key in ("input", "output") if key in usage}
    if "input" in result and "output" in result:
        result["total"] = result["input"] + result["output"]
    return result or None


def create_langfuse_api_client(*, base_url: str, public_key: str, secret_key: str,
                               httpx_client: Any = None) -> Any:
    """Build the low-level REST client without creating the high-level OTLP manager."""
    from langfuse.api.client import LangfuseAPI

    return LangfuseAPI(
        base_url=base_url,
        username=public_key,
        password=secret_key,
        x_langfuse_public_key=public_key,
        timeout=1.5,
        follow_redirects=False,
        httpx_client=httpx_client,
    )


class _DeliveryWorker:
    """One process-wide daemon serializes REST work without holding producer locks."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queue: deque[tuple["LegacyIngestionClient", dict[str, Any], int]] = deque()
        self._queued_bytes = 0
        self._in_flight = False
        self._thread: threading.Thread | None = None

    def enqueue(self, client: "LegacyIngestionClient", event: dict[str, Any], size: int) -> bool:
        with self._condition:
            if len(self._queue) >= _MAX_QUEUED_EVENTS or self._queued_bytes + size > _MAX_BATCH_BYTES:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="langfuse-legacy-delivery", daemon=True,
                )
                self._thread.start()
            self._queue.append((client, event, size))
            self._queued_bytes += size
            client._pending_events += 1
            self._condition.notify_all()
            return True

    def flush(self, client: "LegacyIngestionClient", timeout: float = _FINISH_WAIT_SECONDS) -> None:
        deadline = time.monotonic() + max(0.0, min(timeout, _FINISH_WAIT_SECONDS))
        with self._condition:
            while client._pending_events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(remaining)

    def flush_all(self, timeout: float = _FINISH_WAIT_SECONDS) -> None:
        deadline = time.monotonic() + max(0.0, min(timeout, _FINISH_WAIT_SECONDS))
        with self._condition:
            while self._queue or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(remaining)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                client = self._queue[0][0]
                batch: list[dict[str, Any]] = []
                batch_bytes = 0
                while self._queue and self._queue[0][0] is client and len(batch) < _MAX_BATCH_EVENTS:
                    _, event, size = self._queue[0]
                    if batch and batch_bytes + size > _MAX_BATCH_BYTES:
                        break
                    self._queue.popleft()
                    self._queued_bytes -= size
                    batch.append(event)
                    batch_bytes += size
                self._in_flight = True
            try:
                client._deliver(batch)
            except Exception:
                logger.warning("Langfuse legacy ingestion worker failed")
            finally:
                with self._condition:
                    client._pending_events -= len(batch)
                    self._in_flight = False
                    self._condition.notify_all()


_DELIVERY_WORKER = _DeliveryWorker()


def flush_legacy_delivery(timeout: float = _FINISH_WAIT_SECONDS) -> None:
    """Best-effort atexit drain; queued work may be lost when the process exits."""
    _DELIVERY_WORKER.flush_all(timeout)


class LegacyIngestionClient:
    """Small Langfuse SDK-compatible surface used by the plugin's existing hooks."""

    is_legacy_ingestion = True

    def __init__(self, api: Any) -> None:
        self._api = api
        self._pending_events = 0

    def create_trace_id(self, *, seed: str = "") -> str:
        return uuid.uuid4().hex

    def start_observation(self, *, trace_context: dict[str, Any], name: str,
                          as_type: str, input: Any = None, metadata: Any = None,
                          model: str | None = None, model_parameters: Any = None) -> "LegacyObservation":
        trace_id = _identifier(trace_context.get("trace_id"))
        if trace_id is None:
            trace_id = uuid.uuid4().hex
        session_id = _identifier(trace_context.get("session_id"))
        safe_metadata = _safe_metadata(metadata)
        created = _timestamp()
        trace_body: dict[str, Any] = {"id": trace_id, "timestamp": created}
        if session_id:
            trace_body["sessionId"] = session_id
        if safe_metadata:
            trace_body["metadata"] = safe_metadata
        self._enqueue("trace-create", trace_body)
        return self._observation(trace_id, None, as_type, metadata=safe_metadata,
                                 model=model, created=created, root=True)

    def _observation(self, trace_id: str, parent_id: str | None, as_type: str, *,
                     metadata: Any = None, model: Any = None, model_parameters: Any = None,
                     created: str | None = None,
                     root: bool = False) -> "LegacyObservation":
        observation_id = uuid.uuid4().hex
        kind = "generation" if as_type == "generation" else "span"
        # Names supplied by Hermes can contain arbitrary tool or user-controlled text.
        name = "Hermes turn" if root else {
            "generation": "Hermes generation", "tool": "Hermes tool", "subagent": "Hermes subagent",
        }.get(as_type, "Hermes observation")
        body: dict[str, Any] = {
            "id": observation_id, "traceId": trace_id, "name": name,
            "startTime": created or _timestamp(),
        }
        if parent_id:
            body["parentObservationId"] = parent_id
        safe_metadata = _safe_metadata(metadata)
        if safe_metadata:
            body["metadata"] = safe_metadata
        safe_model = _identifier(model)
        if kind == "generation" and safe_model:
            body["model"] = safe_model
        if kind == "generation" and isinstance(model_parameters, dict):
            safe_parameters = {key: value for key, value in model_parameters.items()
                               if key in {"temperature", "top_p", "max_tokens"}
                               and type(value) in (int, float) and abs(value) <= 1_000_000}
            if safe_parameters:
                body["modelParameters"] = safe_parameters
        self._enqueue(f"{kind}-create", body)
        return LegacyObservation(self, trace_id, observation_id, parent_id, kind)

    def _enqueue(self, event_type: str, body: dict[str, Any]) -> None:
        event = {"id": uuid.uuid4().hex, "timestamp": _timestamp(),
                 "type": event_type, "body": body}
        size = len(json.dumps(event, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        if size > _MAX_BATCH_BYTES:
            logger.warning("Langfuse legacy ingestion dropped an oversized event")
            return
        if not _DELIVERY_WORKER.enqueue(self, event, size):
            logger.warning("Langfuse legacy ingestion queue full; event dropped")

    def flush(self) -> None:
        _DELIVERY_WORKER.flush(self)

    def _deliver(self, events: list[dict[str, Any]]) -> None:
        pending = events
        for attempt in range(_MAX_ATTEMPTS):
            if not pending:
                return
            try:
                response = self._api.ingestion.batch(
                    batch=pending, request_options={"max_retries": 0},
                )
                errors = getattr(response, "errors", None)
                if not errors:
                    return
                by_id = {event["id"]: event for event in pending}
                retry: list[dict[str, Any]] = []
                for error in errors:
                    event_id = getattr(error, "id", None)
                    status = getattr(error, "status", None)
                    if isinstance(error, dict):
                        event_id = error.get("id", event_id)
                        status = error.get("status", status)
                    if event_id in by_id and status in (429, 500, 502, 503, 504) and attempt + 1 < _MAX_ATTEMPTS:
                        retry.append(by_id[event_id])
                if retry:
                    pending = retry
                    continue
                logger.warning("Langfuse legacy ingestion rejected %d event(s)", len(errors))
                return
            except Exception:
                # The event envelope ID is stable, so a bounded retry is idempotent if the
                # server accepted the request but its response was lost.
                if attempt + 1 == _MAX_ATTEMPTS:
                    logger.warning("Langfuse legacy ingestion request failed")
                    return


class LegacyObservation:
    def __init__(self, client: LegacyIngestionClient, trace_id: str, observation_id: str,
                 parent_id: str | None, kind: str) -> None:
        self._client = client
        self._trace_id = trace_id
        self._id = observation_id
        self._parent_id = parent_id
        self._kind = kind
        self._updates: dict[str, Any] = {}
        self._ended = False
        self._lock = threading.Lock()

    def start_observation(self, *, name: str, as_type: str, input: Any = None,
                          metadata: Any = None, model: str | None = None,
                          model_parameters: Any = None) -> "LegacyObservation":
        return self._client._observation(self._trace_id, self._id, as_type,
                                         metadata=metadata, model=model,
                                         model_parameters=model_parameters)

    def set_trace_io(self, **kwargs: Any) -> None:
        # Deliberately omit all content in this transport, including trace-level input/output.
        return None

    def update(self, **kwargs: Any) -> "LegacyObservation":
        with self._lock:
            if self._ended:
                return self
            if "metadata" in kwargs:
                safe_metadata = _safe_metadata(kwargs["metadata"])
                if safe_metadata:
                    self._updates["metadata"] = safe_metadata
            if "usage_details" in kwargs:
                usage = _safe_usage(kwargs["usage_details"])
                if usage:
                    self._updates["usage"] = usage
            level = kwargs.get("level")
            if isinstance(level, str) and level.upper() in _LEVELS:
                self._updates["level"] = level.upper()
        return self

    def end(self) -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
            updates = dict(self._updates)
        body: dict[str, Any] = {"id": self._id, "traceId": self._trace_id, "endTime": _timestamp()}
        body.update(updates)
        self._client._enqueue(f"{self._kind}-update", body)
