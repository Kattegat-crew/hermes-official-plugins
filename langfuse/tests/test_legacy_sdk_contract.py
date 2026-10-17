"""Exercise the installed SDK4 HTTP serializer without a live endpoint or credentials."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from unittest.mock import patch
from pathlib import Path
from typing import Any

try:
    import pytest
except ModuleNotFoundError:  # The Hermes runtime ships SDK4 but intentionally not pytest.
    pytest = None


try:
    SDK_AVAILABLE = importlib.util.find_spec("langfuse.api.client") is not None
except ModuleNotFoundError:
    SDK_AVAILABLE = False
if pytest is not None:
    pytestmark = pytest.mark.skipif(not SDK_AVAILABLE, reason="requires the selected Langfuse SDK4 runtime")


def wait_for_delivery(adapter: Any, client: Any, timeout: float = 2.0) -> None:
    """Wait for queued and in-flight events before closing their HTTP client."""
    worker = adapter._DELIVERY_WORKER
    deadline = time.monotonic() + timeout
    with worker._condition:
        while client._pending_events:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "timed out waiting for legacy delivery to drain"
            worker._condition.wait(remaining)


def run_sdk4_v2_wire_contract() -> None:
    # Field allowlists follow Langfuse v2.95.11's ingestion schema:
    # packages/shared/src/server/ingestion/types.ts (TraceBody, CreateSpanBody,
    # CreateGenerationBody, UpdateSpanBody, UpdateGenerationBody, ingestionEvent).
    import httpx

    adapter_path = Path(__file__).resolve().parents[1] / "legacy_ingestion.py"
    spec = importlib.util.spec_from_file_location("hermes_legacy_ingestion", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = adapter
    spec.loader.exec_module(adapter)

    requests = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            batch = json.loads(request.content)["batch"]
            return httpx.Response(207, json={
                "successes": [{"id": batch[0]["id"], "status": 200}],
                "errors": [{"id": batch[1]["id"], "status": 503, "message": "fixture"}],
            }, request=request)
        return httpx.Response(200, json={"successes": [], "errors": []}, request=request)

    transport = httpx.MockTransport(receive)
    http = httpx.Client(transport=transport, follow_redirects=False, timeout=1.5)
    api = adapter.create_langfuse_api_client(
        base_url="https://langfuse.invalid", public_key="pk-lf-test-only",
        secret_key="sk-lf-test-only", httpx_client=http,
    )
    client = adapter.LegacyIngestionClient(api)
    trace_id = client.create_trace_id(seed="session-placeholder::turn-placeholder")
    worker = adapter._DELIVERY_WORKER
    with worker._condition:
        root = client.start_observation(
            trace_context={"trace_id": trace_id, "session_id": "session-placeholder"},
            name="Hermes turn", as_type="chain", input="private input",
            metadata={"platform": "telegram", "provider": "openai", "model": "gpt-4.1"},
        )
        generation = root.start_observation(name="private generation name", as_type="generation",
                                            input="private messages", model="gpt-4.1",
                                            model_parameters={"temperature": 0.4, "api_key": "sk-lf-test-only"})
        generation.update(output="private output", usage_details={"input": 4, "output": 3},
                          cost_details={"input": 0.2}, status_message="private error")
        generation.end()
        root.end()
    client.flush()
    wait_for_delivery(adapter, client)
    http.close()

    assert len(requests) == 2
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/public/ingestion"
    assert request.headers.get("authorization", "").startswith("Basic ")
    payload = json.loads(request.content)
    assert set(payload) == {"batch"}
    events = payload["batch"]
    assert [event["type"] for event in events] == [
        "trace-create", "span-create", "generation-create", "generation-update", "span-update",
    ]
    retry_payload = json.loads(requests[1].content)
    assert retry_payload["batch"] == [events[1]]
    for event in events:
        assert set(event) == {"id", "timestamp", "type", "body"}
    expected_body_fields = {
        "trace-create": {"id", "timestamp", "sessionId", "metadata"},
        "span-create": {"id", "traceId", "name", "startTime", "metadata"},
        "generation-create": {"id", "traceId", "parentObservationId", "name", "startTime", "model", "modelParameters"},
        "generation-update": {"id", "traceId", "endTime", "usage", "level"},
        "span-update": {"id", "traceId", "endTime"},
    }
    for event in events:
        assert set(event["body"]) <= expected_body_fields[event["type"]], (
            f"{event['type']} carried unexpected fields: {sorted(set(event['body']) - expected_body_fields[event['type']])}"
        )
    required_body_fields = {
        "trace-create": {"id", "timestamp"},
        "span-create": {"id", "traceId", "name", "startTime"},
        "generation-create": {"id", "traceId", "parentObservationId", "name", "startTime"},
        "generation-update": {"id", "traceId", "endTime", "usage"},
        "span-update": {"id", "traceId", "endTime"},
    }
    for event in events:
        assert required_body_fields[event["type"]] <= set(event["body"])
    assert events[3]["body"]["usage"] == {"input": 4, "output": 3, "total": 7}
    assert events[2]["body"]["modelParameters"] == {"temperature": 0.4}
    wire = "\n".join(item.content.decode("utf-8") for item in requests)
    for private in ("private input", "private generation name", "private messages", "private output", "private error"):
        assert private not in wire


def test_low_level_sdk_client_has_bounded_timeout_and_disables_redirects():
    from pathlib import Path
    import importlib.util

    adapter_path = Path(__file__).resolve().parents[1] / "legacy_ingestion.py"
    spec = importlib.util.spec_from_file_location("hermes_legacy_ingestion_config", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = adapter
    spec.loader.exec_module(adapter)
    api = adapter.create_langfuse_api_client(
        base_url="https://langfuse.invalid", public_key="pk-lf-test-only",
        secret_key="sk-lf-test-only",
    )
    configured_http = api._client_wrapper.httpx_client.httpx_client
    try:
        assert configured_http.follow_redirects is False
        assert configured_http.timeout.connect == 1.5
    finally:
        configured_http.close()


def run_sdk4_retry_budget_contract() -> None:
    """Reproduce 503 Retry-After without sleeping or contacting a real endpoint."""
    import httpx
    import langfuse.api.core.http_client as sdk_http

    adapter_path = Path(__file__).resolve().parents[1] / "legacy_ingestion.py"
    spec = importlib.util.spec_from_file_location("hermes_legacy_ingestion_retry", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = adapter
    spec.loader.exec_module(adapter)

    requests = []
    sleeps = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, headers={"Retry-After": "60"}, text="unavailable", request=request)

    http = httpx.Client(transport=httpx.MockTransport(unavailable), follow_redirects=False, timeout=1.5)
    api = adapter.create_langfuse_api_client(
        base_url="https://langfuse.invalid", public_key="pk-lf-test-only",
        secret_key="sk-lf-test-only", httpx_client=http,
    )
    client = adapter.LegacyIngestionClient(api)
    with adapter._DELIVERY_WORKER._condition:
        client._enqueue("trace-create", {
            "id": "retry-budget-trace", "timestamp": "2024-01-01T00:00:00Z",
        })
    with patch.object(sdk_http.time, "sleep", side_effect=lambda seconds: sleeps.append(seconds)):
        client.flush()
        wait_for_delivery(adapter, client)
    http.close()
    assert sleeps == [], f"SDK performed retry backoff: {sleeps}"
    assert len(requests) == 2, f"expected two bounded outer attempts, got {len(requests)}"


def run_sdk4_split_batch_order_contract() -> None:
    """A blocked first request makes later events a deterministic second batch."""
    import httpx

    adapter_path = Path(__file__).resolve().parents[1] / "legacy_ingestion.py"
    spec = importlib.util.spec_from_file_location("hermes_legacy_ingestion_split", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = adapter
    spec.loader.exec_module(adapter)

    requests = []
    first_request_entered = threading.Event()
    release_first_request = threading.Event()

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            first_request_entered.set()
            assert release_first_request.wait(2.0), "test did not release first mocked response"
        return httpx.Response(200, json={"successes": [], "errors": []}, request=request)

    http = httpx.Client(transport=httpx.MockTransport(receive), follow_redirects=False, timeout=1.5)
    api = adapter.create_langfuse_api_client(
        base_url="https://langfuse.invalid", public_key="pk-lf-test-only",
        secret_key="sk-lf-test-only", httpx_client=http,
    )
    client = adapter.LegacyIngestionClient(api)
    with adapter._DELIVERY_WORKER._condition:
        root = client.start_observation(
            trace_context={"trace_id": "split-trace", "session_id": "split-session"},
            name="Hermes turn", as_type="chain", metadata={"platform": "telegram"},
        )
    try:
        assert first_request_entered.wait(2.0), "worker did not start the first request"
        generation = root.start_observation(
            name="private generation name", as_type="generation", model="gpt-4.1",
        )
        generation.update(usage_details={"input": 2, "output": 1})
        generation.end()
        root.end()
    finally:
        release_first_request.set()
    client.flush()
    wait_for_delivery(adapter, client)
    http.close()

    batches = [json.loads(request.content)["batch"] for request in requests]
    assert [[event["type"] for event in batch] for batch in batches] == [
        ["trace-create", "span-create"],
        ["generation-create", "generation-update", "span-update"],
    ]
    assert len({event["id"] for batch in batches for event in batch}) == 5


def run_blocked_sdk_transport_does_not_block_finish() -> None:
    """A stuck response body cannot hold plugin finish or create extra workers."""
    import httpx

    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "langfuse_plugin", plugin_path,
        submodule_search_locations=[str(plugin_path.parent)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    adapter = sys.modules[plugin.LegacyIngestionClient.__module__]

    entered, release = threading.Event(), threading.Event()
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def blocked(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        entered.set()
        try:
            assert release.wait(2.0), "test did not release mocked HTTP response"
            return httpx.Response(200, json={"successes": [], "errors": []}, request=request)
        finally:
            with lock:
                in_flight -= 1

    http = httpx.Client(transport=httpx.MockTransport(blocked), timeout=1.5)
    api = plugin.create_langfuse_api_client(
        base_url="https://langfuse.invalid", public_key="pk-lf-test-only",
        secret_key="sk-lf-test-only", httpx_client=http,
    )
    client = plugin.LegacyIngestionClient(api)
    durations = []
    try:
        with patch.object(plugin, "_get_langfuse", return_value=client):
            for index in range(2):
                task_key = f"blocked-sdk-finish-{index}"
                trace_id = client.create_trace_id(seed=task_key)
                root = client.start_observation(
                    trace_context={"trace_id": trace_id}, name="Hermes turn",
                    as_type="chain", metadata={},
                )
                plugin._TRACE_STATE[task_key] = plugin.TraceState(trace_id, root)
                started = time.monotonic()
                plugin._finish_trace(task_key)
                durations.append(time.monotonic() - started)
                if index == 0:
                    assert entered.wait(1.0), "worker did not reach mocked HTTP transport"
        assert max(durations) < 0.5, f"finish blocked behind HTTP: {durations}"
        assert max_in_flight == 1, f"created concurrent delivery workers: {max_in_flight}"
    finally:
        release.set()
        try:
            client.flush()
            wait_for_delivery(adapter, client)
        finally:
            http.close()


def test_serialized_sdk4_payload_uses_only_langfuse_v2_fields():
    run_sdk4_v2_wire_contract()


if __name__ == "__main__":
    run_sdk4_v2_wire_contract()
    test_low_level_sdk_client_has_bounded_timeout_and_disables_redirects()
    run_sdk4_retry_budget_contract()
    run_sdk4_split_batch_order_contract()
    run_blocked_sdk_transport_does_not_block_finish()
    print("SDK4 serializer, retry, split-batch, and blocked-transport contracts passed")
