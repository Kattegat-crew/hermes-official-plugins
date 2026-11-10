"""Metadata-only Langfuse v2 ingestion with a durable sqlite spool (langfuse-fleet).

Extends the recovered legacy transport: the producer's only synchronous work is
the allowlist check plus one sqlite insert; a process-wide daemon claims due
rows from spools, delivers them over the SDK4 low-level REST client and settles
per-event ACKs back into the spool. Unverified events survive restarts
(at-least-once with stable ids). Design:
`.specs/langfuse-f1-recuperacion-y-diseno/02-design.md`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import (Hermes plugin loader, pytest suite)
    from .fleet_spool import FleetSpool
except ImportError:  # pragma: no cover - loaded standalone (SDK contract script)
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fleet_spool import FleetSpool
    from sdk_env import ensure_sdk_paths

    # Standalone callers (scripts that load this module by path) get the same pinned SDK store
    # the plugin resolves at import time (F5).
    ensure_sdk_paths()

logger = logging.getLogger(__name__)

_MAX_BATCH_BYTES = 512 * 1024
_MAX_BATCH_EVENTS = 128
_FINISH_WAIT_SECONDS = 0.05
# The worker's idle wake doubles as the expiry-sweep cadence (02-design.md §3: ~10 min).
_IDLE_WAKE_SECONDS = 600.0
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/@+ \[\]-]{1,80}$")
_SECRET_PREFIX = re.compile(r"(?i)^(?:pk-lf-|sk-lf-|sk-|pk_|sk_|gh[pousr]_|github_pat_|xox[baprs]-|bearer\s)")
_METADATA_KEYS = frozenset({
    "source", "platform", "provider", "model", "api_mode", "profile", "env", "turn_id",
    "api_request_id", "session_id", "trace_id", "parentObservationId",
    "tool_name", "tool_call_id", "skill",
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


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _SAFE_NAME.fullmatch(value) and not _SECRET_PREFIX.search(value):
        return value
    return None


def _capture_enabled() -> bool:
    """Legacy transport carries content only when the profile opts into sanitized/full."""
    return os.environ.get("HERMES_LANGFUSE_CAPTURE", "metadata").strip().lower() in ("sanitized", "full")


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


def default_spool_path() -> Path:
    """Spool location for the delivering home — read per call, never cached globally (AC8)."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "plugins" / "langfuse-fleet" / "spool.db"


class _DeliveryWorker:
    """One process-wide daemon serializing REST work without holding producer locks.

    Durability lives in each client's spool, not in memory: the worker only
    drains clients whose spool reports due rows, wakes itself for retry
    deadlines, and sweeps expiry on idle wakes. It dies with the process by
    design (AC11/§6): pending rows stay in the spool for the next start.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._registered: set[LegacyIngestionClient] = set()
        self._due: set[LegacyIngestionClient] = set()
        self._in_flight = False
        self._thread: threading.Thread | None = None

    def register(self, client: LegacyIngestionClient) -> None:
        with self._condition:
            self._registered.add(client)
            self._start_locked()
            self._condition.notify_all()

    def notify(self, client: LegacyIngestionClient) -> None:
        with self._condition:
            self._due.add(client)
            self._start_locked()
            self._condition.notify_all()

    def _start_locked(self) -> None:
        # A worker that died (e.g. a client whose spool was closed mid-drain) must not
        # disable delivery for the rest of the process: any later register/notify revives it.
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="langfuse-fleet-delivery", daemon=True,
            )
            self._thread.start()

    def flush(self, client: LegacyIngestionClient, timeout: float = _FINISH_WAIT_SECONDS) -> None:
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
            while self._due or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(remaining)

    def _wait_delay_locked(self) -> float:
        """Idle wake delay: the earliest retry deadline, capped at the expiry cadence."""
        delay = _IDLE_WAKE_SECONDS
        now = time.time()
        for client in self._registered:
            try:
                earliest = client._spool.next_due_at()
            except Exception:
                continue  # a dead spool must not stall the idle wait
            if earliest is not None:
                delay = min(delay, max(0.0, earliest - now))
        return delay

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._due:
                    self._condition.wait(self._wait_delay_locked())
                for client in tuple(self._registered):
                    try:
                        due = client._spool.has_due()
                    except Exception:
                        logger.warning("Langfuse fleet due check failed")
                        continue
                    if due:
                        self._due.add(client)
                if not self._due:
                    self._sweep_expiry()
                    continue
                client = next(iter(self._due))
                self._in_flight = True
            had_work = self._drain_client(client)
            with self._condition:
                self._in_flight = False
                if not had_work:
                    self._due.discard(client)
                self._condition.notify_all()

    def _sweep_expiry(self) -> None:
        for client in self._registered:
            try:
                client._spool.expire()
            except Exception:
                # A failed sweep must not kill the worker; the next wake retries it.
                logger.warning("Langfuse fleet expiry sweep failed")

    def _claim(self, client: LegacyIngestionClient) -> list[dict[str, Any]]:
        """Claim under the worker lock: a producer holding it can finish batching first."""
        with self._condition:
            return client._spool.claim_batch(limit=_MAX_BATCH_EVENTS, max_bytes=_MAX_BATCH_BYTES)

    def _drain_client(self, client: LegacyIngestionClient) -> bool:
        progressed = False
        try:
            while True:
                batch = self._claim(client)
                if not batch:
                    return progressed
                try:
                    outcomes = client._deliver(batch)
                except Exception:
                    logger.warning("Langfuse fleet delivery worker failed")
                    outcomes = {event["id"]: None for event in batch}
                terminal_ids = client._settle(batch, outcomes)
                with self._condition:
                    for event_id in terminal_ids:
                        # Only events this client enqueued leave its pending count: recovered
                        # orphans from a previous boot belong to a dead producer.
                        if event_id in client._enqueued_ids:
                            client._enqueued_ids.discard(event_id)
                            client._pending_events -= 1
                progressed = True
        except Exception:
            # A client whose spool was closed (or became unreadable) must not kill the
            # worker; its durable rows stay in the file for the next live client.
            logger.warning("Langfuse fleet drain aborted for a client; rows stay durable")
            return progressed


_DELIVERY_WORKER = _DeliveryWorker()


def flush_legacy_delivery(timeout: float = _FINISH_WAIT_SECONDS) -> None:
    """Best-effort atexit drain; anything unverified stays in its spool for the next start (AC6)."""
    _DELIVERY_WORKER.flush_all(timeout)


class LegacyIngestionClient:
    """Small Langfuse SDK-compatible surface used by the plugin's existing hooks."""

    is_legacy_ingestion = True

    def __init__(self, api: Any, spool: FleetSpool | None = None) -> None:
        self._api = api
        self._spool = spool if spool is not None else FleetSpool(default_spool_path())
        self._pending_events = 0
        self._enqueued_ids: set[str] = set()
        self._spool.expire()  # startup sweep (AC4)
        _DELIVERY_WORKER.register(self)  # also re-delivers rows recovered from a previous run

    def create_trace_id(self, *, seed: str = "") -> str:
        return uuid.uuid4().hex

    def start_observation(self, *, trace_context: dict[str, Any], name: str,
                          as_type: str, input: Any = None, metadata: Any = None,
                          model: str | None = None, model_parameters: Any = None,
                          tags: list[str] | None = None) -> "LegacyObservation":
        trace_id = _identifier(trace_context.get("trace_id"))
        if trace_id is None:
            trace_id = uuid.uuid4().hex
        session_id = _identifier(trace_context.get("session_id"))
        safe_metadata = _safe_metadata(metadata)
        safe_tags = list(dict.fromkeys(
            tag for tag in (tags or []) if isinstance(tag, str) and _identifier(tag)
        ))
        created = _timestamp()
        trace_body: dict[str, Any] = {"id": trace_id, "timestamp": created}
        if session_id:
            trace_body["sessionId"] = session_id
        if safe_metadata:
            trace_body["metadata"] = safe_metadata
        if safe_tags:
            trace_body["tags"] = safe_tags
        safe_name = _safe_label(name)
        trace_body["name"] = safe_name or "Hermes turn"
        profile = safe_metadata.get("profile")
        if profile:
            trace_body["userId"] = "ragnar" if profile == "default" else profile
        if _capture_enabled() and input is not None:
            trace_body["input"] = input
        self._enqueue("trace-create", trace_body)
        return self._observation(trace_id, None, as_type, metadata=safe_metadata,
                                 model=model, created=created, root=True, name=name, input=input)

    def _observation(self, trace_id: str, parent_id: str | None, as_type: str, *,
                     metadata: Any = None, model: Any = None, model_parameters: Any = None,
                     created: str | None = None,
                     root: bool = False, name: str | None = None,
                     input: Any = None, output: Any = None) -> "LegacyObservation":
        observation_id = uuid.uuid4().hex
        kind = "generation" if as_type == "generation" else "span"
        # Names supplied by Hermes can contain arbitrary tool or user-controlled text.
        default_name = "Hermes turn" if root else {
            "generation": "Hermes generation", "tool": "Hermes tool", "subagent": "Hermes subagent",
        }.get(as_type, "Hermes observation")
        safe_name = _safe_label(name)
        body: dict[str, Any] = {
            "id": observation_id, "traceId": trace_id, "name": safe_name or default_name,
            "startTime": created or _timestamp(),
        }
        if _capture_enabled():
            if input is not None:
                body["input"] = input
            if output is not None:
                body["output"] = output
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
        # The insert and the pending bookkeeping share the worker lock: the worker's
        # decrement takes the same lock, so it can never settle an event whose id has
        # not been registered as ours yet (that would strand a phantom pending count).
        with _DELIVERY_WORKER._condition:
            if not self._spool.enqueue(event, size):
                # Count-only warning: the rejection reason (overflow/oversize) lives in the
                # spool counters, so no event content is ever needed in a log line.
                logger.warning("Langfuse fleet spool rejected an event; see spool counters")
                return
            self._enqueued_ids.add(event["id"])
            self._pending_events += 1
        _DELIVERY_WORKER.notify(self)

    def flush(self) -> None:
        _DELIVERY_WORKER.flush(self)

    def _deliver(self, events: list[dict[str, Any]]) -> dict[str, int | None]:
        """One bounded REST attempt (timeout 1.5 s, max_retries=0). Per-event outcome map:
        absent or 200 = accepted, ``None`` = transport failure (retryable), otherwise the
        server's per-event status."""
        try:
            response = self._api.ingestion.batch(
                batch=events, request_options={"max_retries": 0},
            )
        except Exception:
            # The event envelope ID is stable, so a retry is idempotent if the server
            # accepted the request but its response was lost.
            return {event["id"]: None for event in events}
        outcomes: dict[str, int | None] = {event["id"]: 200 for event in events}
        for error in (getattr(response, "errors", None) or []):
            event_id = getattr(error, "id", None)
            status = getattr(error, "status", None)
            if isinstance(error, dict):
                event_id = error.get("id", event_id)
                status = error.get("status", status)
            if event_id in outcomes and status is not None:
                outcomes[event_id] = status if isinstance(status, int) else 0
        return outcomes

    def _settle(self, batch: list[dict[str, Any]], outcomes: dict[str, int | None]) -> list[str]:
        """Apply outcomes to the spool; returns the event ids that reached a terminal state
        (only those leave the producer's pending count — retried events stay pending)."""
        acked = [event["id"] for event in batch if outcomes.get(event["id"], 200) == 200]
        terminal: list[str] = []
        if acked and self._spool.ack(acked):
            terminal.extend(acked)
        errored = {event["id"]: outcomes[event["id"]] for event in batch
                   if event["id"] in outcomes and outcomes[event["id"]] != 200}
        if errored:
            terminal.extend(self._spool.settle_batch(errored)[1])
        return terminal


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
                                         model_parameters=model_parameters, name=name, input=input)

    def set_trace_io(self, **kwargs: Any) -> None:
        if not _capture_enabled():
            return
        body: dict[str, Any] = {"id": self._trace_id}
        for key in ("input", "output"):
            if key in kwargs and kwargs[key] is not None:
                body[key] = kwargs[key]
        if len(body) > 1:
            self._client._enqueue("trace-update", body)

    def update(self, **kwargs: Any) -> "LegacyObservation":
        with self._lock:
            if self._ended:
                return self
            if _capture_enabled():
                for key in ("input", "output"):
                    if key in kwargs and kwargs[key] is not None:
                        self._updates[key] = kwargs[key]
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
