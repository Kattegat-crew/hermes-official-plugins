from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest


def _legacy_module():
    return importlib.import_module("langfuse_fleet_plugin.legacy_ingestion")


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


def test_partial_success_retries_only_failed_event_with_same_event_id(monkeypatch):
    legacy = _legacy_module()
    # In the fleet transport the spool owns retries with backoff; make the
    # backoff immediate so the bounded retry lands inside the wait window.
    monkeypatch.setattr(importlib.import_module("langfuse_fleet_plugin.fleet_spool"),
                        "RETRY_BASE_S", 0.0)

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


def test_finished_legacy_trace_is_delivered_without_flushing_default_client(monkeypatch):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="finish")
    root = client.start_observation(trace_context={"trace_id": trace_id}, name="Hermes turn",
                                    as_type="chain", metadata={})
    plugin._TRACE_STATE["legacy-finish-test"] = plugin.TraceState(trace_id, root)
    monkeypatch.setattr(plugin, "_get_langfuse", lambda: client)

    plugin._finish_trace("legacy-finish-test")
    _wait_for_batches(api, 1)
    assert api.ingestion.batches
    assert "legacy-finish-test" not in plugin._TRACE_STATE

    class ModernClient:
        flushes = 0

        def flush(self):
            self.flushes += 1

    modern = ModernClient()
    modern_root = type("Root", (), {"end": lambda self: None, "set_trace_io": lambda self, **kw: None,
                                    "update": lambda self, **kw: None})()
    plugin._TRACE_STATE["modern-finish-test"] = plugin.TraceState("modern", modern_root)
    monkeypatch.setattr(plugin, "_get_langfuse", lambda: modern)
    plugin._finish_trace("modern-finish-test")
    assert modern.flushes == 0


def test_blocked_transport_does_not_hold_finish_or_producer_lock(monkeypatch):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    legacy = _legacy_module()
    entered = threading.Event()
    release = threading.Event()
    in_flight = 0
    max_in_flight = 0
    state_lock = threading.Lock()

    class BlockingIngestion:
        def batch(self, *, batch, request_options=None):
            nonlocal in_flight, max_in_flight
            with state_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            entered.set()
            try:
                assert release.wait(2.0), "test did not release mocked transport"
                return type("Response", (), {"errors": []})()
            finally:
                with state_lock:
                    in_flight -= 1

    client = legacy.LegacyIngestionClient(type("API", (), {"ingestion": BlockingIngestion()})())
    monkeypatch.setattr(plugin, "_get_langfuse", lambda: client)
    finished = []
    try:
        for index in range(2):
            task_key = f"blocked-finish-{index}"
            trace_id = client.create_trace_id(seed=task_key)
            root = client.start_observation(trace_context={"trace_id": trace_id},
                                            name="Hermes turn", as_type="chain", metadata={})
            plugin._TRACE_STATE[task_key] = plugin.TraceState(trace_id, root)
            started = time.monotonic()
            plugin._finish_trace(task_key)
            finished.append(time.monotonic() - started)
            if index == 0:
                assert entered.wait(1.0), "worker did not reach blocked transport"
        assert max(finished) < 0.5, f"finish blocked behind transport: {finished}"
        assert max_in_flight == 1
    finally:
        release.set()
    client.flush()
    deadline = time.monotonic() + 2.0
    while client._pending_events:
        assert time.monotonic() < deadline, "worker did not drain after release"
        time.sleep(0.001)




@pytest.mark.parametrize(("configured", "expected"), [(None, "otlp"), ("otlp", "otlp"), ("legacy", "legacy")])
def test_transport_mode_is_explicit_opt_in(monkeypatch, configured, expected):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    if configured is None:
        monkeypatch.delenv("HERMES_LANGFUSE_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("HERMES_LANGFUSE_TRANSPORT", configured)
    assert plugin._transport_mode() == expected


def test_invalid_transport_mode_fails_closed(monkeypatch, caplog):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    monkeypatch.setenv("HERMES_LANGFUSE_TRANSPORT", "https://secret.example/token")
    with caplog.at_level("WARNING", logger=plugin.__name__):
        assert plugin._transport_mode() == "invalid"
    assert "secret.example" not in caplog.text


def test_legacy_mode_uses_only_low_level_api_constructor(monkeypatch):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    monkeypatch.setenv("HERMES_LANGFUSE_TRANSPORT", "legacy")
    monkeypatch.setattr(plugin, "_secret", lambda name: {
        "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-1234567890abcdef",
        "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-1234567890abcdef",
    }.get(name, ""))
    api_sentinel = object()
    calls = []

    class Wrapper:
        def __init__(self, api):
            calls.append(("wrapper", api))

    monkeypatch.setattr(plugin, "create_langfuse_api_client", lambda **kwargs: calls.append(("api", kwargs)) or api_sentinel)
    monkeypatch.setattr(plugin, "LegacyIngestionClient", Wrapper)
    monkeypatch.setattr(plugin, "Langfuse", lambda **kwargs: calls.append(("otlp", kwargs)))

    plugin._LANGFUSE_CLIENT = None
    plugin._LANGFUSE_CLIENT_BY_HOME.clear()
    assert isinstance(plugin._build_client(), Wrapper)
    assert calls[0][0] == "api"
    assert calls[0][1]["base_url"] == "https://cloud.langfuse.com"
    assert calls[1] == ("wrapper", api_sentinel)
    assert all(kind != "otlp" for kind, _ in calls)


def test_metadata_only_mode_does_not_log_unused_capture_configuration(monkeypatch, caplog):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    legacy = _legacy_module()
    monkeypatch.setenv("HERMES_LANGFUSE_TRANSPORT", "legacy")
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "sk-lf-secret-value")
    client = legacy.LegacyIngestionClient(_API())
    with caplog.at_level("WARNING", logger=plugin.__name__):
        assert plugin._capture_content("private prompt") is None
        plugin._start_root_trace(
            "task:opaque", task_id="task", session_id="session", platform="telegram",
            provider="openai", model="gpt-4.1", api_mode="chat",
            messages=[{"role": "user", "content": "private prompt"}], client=client,
        )
    assert "sk-lf-secret-value" not in caplog.text


def test_legacy_tool_span_keeps_name_and_skill_metadata():
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="session-token::turn-token")
    root = client.start_observation(
        trace_context={"trace_id": trace_id, "session_id": "session-token"},
        name="Hermes turn", as_type="chain", metadata={"profile": "default"})
    tool = root.start_observation(
        name="Tool: skill_view", as_type="tool",
        metadata={"tool_name": "skill_view", "tool_call_id": "call-1", "skill": "hermes-cron-ops"})
    tool.end()
    root.end()
    client.flush()
    _wait_for_batches(api, 1)
    span = next(e["body"] for e in _events(api)
                if e["type"] == "span-create" and e["body"].get("name") == "Tool: skill_view")
    assert span["name"] == "Tool: skill_view"
    assert span["metadata"] == {"tool_name": "skill_view", "tool_call_id": "call-1", "skill": "hermes-cron-ops"}


def test_legacy_trace_keeps_profile_name_and_user_id():
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="session-token::turn-token")
    root = client.start_observation(
        trace_context={"trace_id": trace_id, "session_id": "session-token"},
        name="Hermes turn [ragnar]", as_type="chain", metadata={"profile": "default"})
    root.end()
    client.flush()
    _wait_for_batches(api, 1)
    trace = _events(api)[0]["body"]
    assert trace["name"] == "Hermes turn [ragnar]"
    assert trace["userId"] == "ragnar"


def test_legacy_full_capture_includes_trace_io(monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "full")
    legacy = _legacy_module()
    api = _API()
    client = legacy.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="session-token::turn-token")
    root = client.start_observation(
        trace_context={"trace_id": trace_id, "session_id": "session-token"},
        name="Hermes turn [ragnar]", as_type="chain",
        input={"role": "user", "content": "hola"}, metadata={"profile": "default"})
    root.set_trace_io(input={"role": "user", "content": "hola"})
    gen = root.start_observation(name="Hermes generation", as_type="generation",
                                 input={"messages": [{"role": "user", "content": "hola"}]}, metadata={})
    gen.update(output={"content": "respuesta"})
    gen.end()
    root.set_trace_io(output={"content": "respuesta"})
    root.end()
    client.flush()
    _wait_for_batches(api, 1)
    events = _events(api)
    trace = events[0]["body"]
    assert trace["input"] == {"role": "user", "content": "hola"}
    assert trace["userId"] == "ragnar"
    trace_updates = [e["body"] for e in events if e["type"] == "trace-update"]
    assert any(u.get("output") == {"content": "respuesta"} for u in trace_updates)
    gen_create = next(e["body"] for e in events if e["type"] == "generation-create")
    assert gen_create["input"] == {"messages": [{"role": "user", "content": "hola"}]}
    gen_update = next(e["body"] for e in events if e["type"] == "generation-update" and "output" in e["body"])
    assert gen_update["output"] == {"content": "respuesta"}
