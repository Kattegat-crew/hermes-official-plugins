from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest


def _legacy_module():
    return importlib.import_module("langfuse_plugin.legacy_ingestion")


class _Ingestion:
    def __init__(self):
        self.batches = []
        self.called = threading.Event()

    def batch(self, *, batch, request_options=None):
        self.batches.append(list(batch))
        self.called.set()
        return type("Response", (), {"errors": []})()


class _API:
    def __init__(self):
        self.ingestion = _Ingestion()


def _events(api):
    return [event for batch in api.ingestion.batches for event in batch]


def _wait_for_batches(api, count, timeout=2.0):
    deadline = time.monotonic() + timeout
    while len(api.ingestion.batches) < count:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"expected {count} batches, observed {len(api.ingestion.batches)}"
        if not api.ingestion.called.wait(remaining):
            raise AssertionError(f"expected {count} batches, observed {len(api.ingestion.batches)}")
        api.ingestion.called.clear()


def test_legacy_facade_emits_metadata_only_v2_events_with_parentage(monkeypatch):
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)

    trace_id = client.create_trace_id(seed="session-token::turn-token")
    root = client.start_observation(
        trace_context={"trace_id": trace_id, "session_id": "session-token"},
        name="Hermes turn", as_type="chain",
        input={"role": "user", "content": "private prompt"},
        metadata={"platform": "telegram", "provider": "openai", "model": "gpt-4.1",
                  "api_request_id": "request-token", "profile": "ghp_secret-value",
                  "unsafe": "private metadata"},
    )
    root.set_trace_io(input="private trace input")
    generation = root.start_observation(
        name="LLM call private name", as_type="generation", input="private messages",
        metadata={"finish_reason": "private finish reason"}, model="gpt-4.1",
        model_parameters={"temperature": 0.7, "api_key": "sk-lf-private"},
    )
    generation.update(output="private completion", usage_details={"input": 13, "output": 5},
                      cost_details={"input": 0.01}, level="ERROR",
                      status_message="private error details")
    generation.end()
    root.update(output="private final output")
    root.end()
    client.flush()
    _wait_for_batches(api, 1)

    events = _events(api)
    assert [event["type"] for event in events] == [
        "trace-create", "span-create", "generation-create", "generation-update", "span-update",
    ]
    trace = events[0]["body"]
    root_span = events[1]["body"]
    gen_create, gen_update = events[2]["body"], events[3]["body"]
    assert trace["id"] == trace_id and trace["sessionId"] == "session-token"
    assert "createdAt" not in trace and "timestamp" in trace
    assert "profile" not in trace["metadata"]
    assert gen_create["traceId"] == trace_id and gen_create["parentObservationId"] == root_span["id"]
    assert gen_update["id"] == gen_create["id"]
    assert gen_update["usage"] == {"input": 13, "output": 5, "total": 18}
    assert gen_update["level"] == "ERROR"
    assert gen_create["modelParameters"] == {"temperature": 0.7}
    serialized = repr(events)
    for private in ("private prompt", "private trace input", "private messages", "private completion",
                    "private final output", "private metadata", "private finish reason", "private error details",
                    "sk-lf-private"):
        assert private not in serialized
    assert all("id" in event and "timestamp" in event for event in events)
    assert len({event["id"] for event in events}) == len(events)


def test_missing_usage_is_not_fabricated_and_invalid_levels_are_dropped():
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="one-turn")
    root = client.start_observation(trace_context={"trace_id": trace_id}, name="Hermes turn",
                                    as_type="chain", metadata={})
    generation = root.start_observation(name="generation", as_type="generation", input=None)
    generation.update(usage_details={"input": True, "output": -1}, level="SECRET", status_message="secret")
    generation.end()
    root.end()
    client.flush()
    _wait_for_batches(api, 1)

    update = next(e["body"] for e in _events(api) if e["type"] == "generation-update")
    assert "usage" not in update
    assert update.get("level") is None
    assert "statusMessage" not in update


def test_partial_success_retries_only_failed_event_with_same_event_id():
    legacy = _legacy_module()

    class PartialIngestion(_Ingestion):
        def batch(self, *, batch, request_options=None):
            self.batches.append(list(batch))
            self.called.set()
            if len(self.batches) == 1:
                return type("Response", (), {"errors": [{"id": batch[1]["id"], "status": 503}]})()
            return type("Response", (), {"errors": []})()

    api = _API()
    api.ingestion = PartialIngestion()
    client = legacy.LegacyIngestionClient(api)
    with legacy._DELIVERY_WORKER._condition:
        trace_id = client.create_trace_id(seed="batch")
        root = client.start_observation(trace_context={"trace_id": trace_id}, name="Hermes turn",
                                        as_type="chain", metadata={})
        root.end()
    client.flush()
    _wait_for_batches(api, 2)

    first, retry = api.ingestion.batches
    assert len(first) == 3
    assert retry == [first[1]]


def test_concurrent_end_calls_emit_one_observation_update():
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="concurrent-end")
    root = client.start_observation(trace_context={"trace_id": trace_id}, name="Hermes turn",
                                    as_type="chain", metadata={})
    generation = root.start_observation(name="generation", as_type="generation", input=None)
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: generation.end(), range(32)))
    client.flush()
    _wait_for_batches(api, 1)
    assert sum(event["type"] == "generation-update" for event in _events(api)) == 1
