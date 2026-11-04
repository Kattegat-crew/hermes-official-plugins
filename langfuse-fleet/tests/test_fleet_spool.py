"""Fleet spool + durable delivery contracts (F3 T-07..T-16, matrix B1-B11).

Every test runs offline against a mocked transport. The spool is the durability
boundary: producers only ever pay one sqlite insert, delivery is a daemon worker
driven by due rows, and nothing is marked done without verified acceptance.
"""
from __future__ import annotations

import importlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


def _fleet():
    return importlib.import_module("langfuse_fleet_plugin.legacy_ingestion")


def _fleet_spool_module():
    return importlib.import_module("langfuse_fleet_plugin.fleet_spool")


def _spool(tmp_path, name="spool.db", **kwargs):
    return _fleet_spool_module().FleetSpool(Path(tmp_path) / name, **kwargs)


def _event(event_id, type_="trace-create"):
    return {"id": event_id, "timestamp": "2026-11-02T00:00:00.000Z",
            "type": type_, "body": {"id": event_id}}


def _wait_until(predicate, timeout=2.0, message="condition not met"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.005)


class _GoodIngestion:
    def __init__(self):
        self.batches = []

    def batch(self, *, batch, request_options=None):
        self.batches.append(list(batch))
        return type("Response", (), {"errors": []})()


class _BlockedIngestion:
    def __init__(self, release):
        self.release = release

    def batch(self, *, batch, request_options=None):
        assert self.release.wait(5.0), "test did not release the mocked transport"
        return type("Response", (), {"errors": []})()


class _API:
    def __init__(self, ingestion):
        self.ingestion = ingestion


# ---------------------------------------------------------------- B1 (AC8) home isolation

def test_spool_paths_are_keyed_by_hermes_home(monkeypatch, tmp_path):
    fleet = _fleet()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
    path_a = fleet.default_spool_path()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
    path_b = fleet.default_spool_path()
    assert path_a != path_b
    assert path_a == Path(tmp_path / "home-a") / "plugins" / "langfuse-fleet" / "spool.db"


def test_two_homes_get_isolated_spools(tmp_path):
    a, b = _spool(tmp_path, "a.db"), _spool(tmp_path, "b.db")
    assert a.enqueue(_event("evt-a"), 64) is True
    assert b.claim_batch() == []
    assert b.counters()["delivered"] == 0
    assert len(a.claim_batch()) == 1


# ---------------------------------------------------------------- B2 (AC2) ack per event

def test_ack_marks_only_verified_events(tmp_path):
    spool = _spool(tmp_path)
    assert spool.enqueue(_event("evt-ok"), 64)
    assert spool.enqueue(_event("evt-retry"), 64)
    batch = spool.claim_batch()
    assert {event["id"] for event in batch} == {"evt-ok", "evt-retry"}

    spool.ack(["evt-ok"])
    counters = spool.counters()
    assert counters["delivered"] == 1 and counters["batches_acked"] == 1

    spool.settle_batch({"evt-retry": 503})
    rows = spool.pending_stats()
    assert rows[0] == 1  # still pending, awaiting its retry deadline
    # Once its retry deadline passes the same stable id is claimed again.
    batch = spool.claim_batch(now=time.time() + 10_000)
    assert [event["id"] for event in batch] == ["evt-retry"]


def test_non_retryable_error_is_settled_without_delivered_credit(tmp_path):
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-bad"), 64)
    spool.claim_batch()
    spool.settle_batch({"evt-bad": 400})
    counters = spool.counters()
    assert counters["rejected"] == 1
    assert counters["delivered"] == 0
    assert spool.pending_stats() == (0, 0)


# ---------------------------------------------------------------- B5 (AC3) bounded overflow

def test_overflow_rejects_new_events_without_blocking(tmp_path, monkeypatch):
    module = _fleet_spool_module()
    monkeypatch.setattr(module, "MAX_ROWS", 4)
    spool = _spool(tmp_path)
    for index in range(4):
        assert spool.enqueue(_event(f"evt-{index}"), 64)
    started = time.monotonic()
    for index in range(200):
        assert spool.enqueue(_event(f"overflow-{index}"), 64) is False
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"producer blocked on saturated spool: {elapsed:.3f}s"
    assert spool.counters()["lost_overflow"] == 200
    assert spool.pending_stats()[0] == 4  # accepted events are never evicted


def test_oversize_event_is_counted_not_persisted(tmp_path, monkeypatch):
    module = _fleet_spool_module()
    monkeypatch.setattr(module, "MAX_BATCH_BYTES", 128)
    spool = _spool(tmp_path)
    assert spool.enqueue(_event("evt-big"), 4096) is False
    assert spool.counters()["lost_oversize"] == 1
    assert spool.pending_stats() == (0, 0)


# ---------------------------------------------------------------- B6 (AC4) expiry

def test_expiry_purges_aged_events_and_counts_them(tmp_path):
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-fresh"), 64)
    spool.enqueue(_event("evt-old"), 64, created_at=time.time() - 25 * 3600)
    assert spool.expire() == 1
    assert spool.counters()["expired"] == 1
    assert spool.pending_stats() == (1, spool.pending_stats()[1])
    remaining = {event["id"] for event in spool.claim_batch(now=time.time() + 10_000)}
    assert remaining == {"evt-fresh"}


def test_expiry_is_idempotent(tmp_path):
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-old"), 64, created_at=time.time() - 25 * 3600)
    assert spool.expire() == 1
    assert spool.expire() == 0
    assert spool.counters()["expired"] == 1


# ---------------------------------------------------------------- B4 (AC1) crash recovery

def test_orphan_in_flight_recovered_on_reopen(tmp_path):
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-orphan"), 64)
    spool.claim_batch(owner="9999:dead-boot")
    spool.close()

    reopened = _spool(tmp_path)
    assert reopened.counters()["recovered"] == 1
    batch = reopened.claim_batch()
    assert [event["id"] for event in batch] == ["evt-orphan"]


def test_persistence_survives_sigkill(tmp_path):
    script = textwrap.dedent("""
        import importlib.util, os, signal, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "fleet_spool", Path(sys.argv[1]) / "fleet_spool.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        spool = module.FleetSpool(Path(sys.argv[2]) / "spool.db")
        for index in range(3):
            spool.enqueue({"id": f"evt-{index}", "timestamp": "t", "type": "trace-create",
                           "body": {"id": f"evt-{index}"}}, 64)
        spool.claim_batch(limit=1, owner="dead-process")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(_fleet_spool_module().__file__).parent), str(tmp_path)],
        capture_output=True, timeout=30)
    assert result.returncode == -signal.SIGKILL, result.stderr

    reopened = _spool(tmp_path)
    assert reopened.counters()["recovered"] == 1
    ids = {event["id"] for event in reopened.claim_batch(now=time.time() + 10_000)}
    assert ids == {"evt-0", "evt-1", "evt-2"}


def test_recovered_events_are_redelivered_by_a_new_client(tmp_path):
    fleet = _fleet()
    old_spool = _spool(tmp_path)
    old_spool.enqueue(_event("evt-orphan"), 64)
    old_spool.claim_batch(owner="dead:boot")
    old_spool.close()

    api = _API(_GoodIngestion())
    client = fleet.LegacyIngestionClient(api, spool=_spool(tmp_path))
    client._enqueue("trace-create", {"id": "evt-new", "timestamp": "t", "body": {}})
    client.flush()
    _wait_until(lambda: client._pending_events == 0, message="client did not drain")
    _wait_until(lambda: spool_pending_ids(tmp_path) == set(), message="spool did not drain")
    delivered = [event for batch in api.ingestion.batches for event in batch]
    # _enqueue mints fresh envelope ids (client contract); identity is the body id.
    assert {event["body"]["id"] for event in delivered} == {"evt-orphan", "evt-new"}


def spool_pending_ids(tmp_path):
    conn = sqlite3.connect(f"file:{Path(tmp_path) / 'spool.db'}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute(
            "SELECT id FROM events WHERE state IN ('pending', 'in_flight')")}
    finally:
        conn.close()


# ---------------------------------------------------------------- B7 (AC5) bounded retries

def test_retry_backoff_grows_and_is_capped(tmp_path):
    module = _fleet_spool_module()
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-flaky"), 64)
    delays = []
    for _ in range(module.MAX_ATTEMPTS - 1):
        # A fresh lease requires a due row: claim past the pending retry deadline.
        spool.claim_batch(now=time.time() + 10_000)
        before = time.time()
        spool.settle_batch({"evt-flaky": 429})
        row = spool.claim_batch(now=time.time() + 10_000, dry_run=True)
        delays.append(row[0]["next_attempt_at"] - before)
    assert delays[0] == pytest.approx(min(module.RETRY_BASE_S * 2, module.RETRY_CAP_S), abs=5)
    assert delays == sorted(delays)
    assert max(delays) <= module.RETRY_CAP_S + 5


def test_attempts_exhaustion_marks_event_lost(tmp_path):
    module = _fleet_spool_module()
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-doomed"), 64)
    for _ in range(module.MAX_ATTEMPTS):
        spool.claim_batch(now=time.time() + 10_000)
        spool.settle_batch({"evt-doomed": 503})
    assert spool.counters()["lost_attempts"] == 1
    assert spool.pending_stats() == (0, 0)
    assert spool.claim_batch(now=time.time() + 10_000) == []


def test_transport_failure_is_retryable(tmp_path):
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-net"), 64)
    spool.claim_batch()
    spool.settle_batch({"evt-net": None})
    assert spool.counters()["lost_attempts"] == 0
    assert spool.pending_stats()[0] == 1


# ---------------------------------------------------------------- B8 (AC6) bounded shutdown

def test_shutdown_bounded_and_pending_survives(tmp_path):
    fleet = _fleet()
    release = threading.Event()
    spool = _spool(tmp_path)
    client = fleet.LegacyIngestionClient(_API(_BlockedIngestion(release)), spool=spool)
    for index in range(3):
        client._enqueue("trace-create", {"id": f"evt-{index}", "timestamp": "t", "body": {}})
    started = time.monotonic()
    fleet._DELIVERY_WORKER.flush_all(timeout=0.2)
    assert time.monotonic() - started < 2.0, "shutdown drain exceeded its deadline"

    # Nothing was verified while the transport was blocked; a live worker keeps the
    # rows it leased and drains the rest once the transport accepts again.
    release.set()
    _wait_until(lambda: client._pending_events == 0, message="client did not drain after release")
    assert _spool(tmp_path).counters()["delivered"] == 3


def test_unverified_pending_rows_survive_a_restart(tmp_path):
    abandoned = _spool(tmp_path)
    for index in range(3):
        abandoned.enqueue(_event(f"evt-{index}"), 64)
    abandoned.claim_batch(owner="dead:boot")  # leased by a process that then dies
    abandoned.close()

    reopened = _spool(tmp_path)  # a fresh boot recovers the orphaned lease
    ids = {event["body"]["id"] for event in reopened.claim_batch(now=time.time() + 10_000)}
    assert ids == {"evt-0", "evt-1", "evt-2"}


# ---------------------------------------------------------------- B9 (AC7) privacy

def test_allowlist_runs_before_any_spool_write(tmp_path, caplog):
    fleet = _fleet()
    spool = _spool(tmp_path)
    client = fleet.LegacyIngestionClient(_API(_GoodIngestion()), spool=spool)
    with caplog.at_level("WARNING"):
        client.start_observation(
            trace_context={"trace_id": "trace-1", "session_id": "session-1"},
            name="Hermes turn", as_type="chain",
            metadata={"platform": "telegram", "model": "gpt-4.1",
                      "unsafe": "private prompt content", "api_key": "sk-lf-abcdef"},
        )
        client.flush()
    _wait_until(lambda: client._pending_events == 0, message="client did not drain")

    conn = sqlite3.connect(f"file:{Path(tmp_path) / 'spool.db'}?mode=ro", uri=True)
    try:
        bodies = [row[0] for row in conn.execute("SELECT body_json FROM events")]
    finally:
        conn.close()
    serialized = " ".join(bodies)
    for private in ("private prompt content", "sk-lf-abcdef"):
        assert private not in serialized
    for body in bodies:
        metadata = json.loads(body).get("metadata", {})
        assert set(metadata) <= {"platform", "model"}


# ---------------------------------------------------------------- B10 (AC10) counters

def test_counters_are_visible_from_a_second_readonly_connection(tmp_path):
    spool = _spool(tmp_path)
    spool.enqueue(_event("evt-1"), 64)
    spool.enqueue(_event("evt-2"), 64)
    spool.claim_batch()
    spool.ack(["evt-1", "evt-2"])

    ro_conn = sqlite3.connect(f"file:{Path(tmp_path) / 'spool.db'}?mode=ro", uri=True)
    try:
        stored = dict(ro_conn.execute("SELECT key, value FROM counters"))
    finally:
        ro_conn.close()
    assert stored["delivered"] == 2
    assert spool.counters()["delivered"] == 2


def test_writer_contention_keeps_every_event(tmp_path):
    spool_a = _spool(tmp_path, "shared.db")
    spool_b = _spool(tmp_path, "shared.db")
    errors = []

    def produce(spool, prefix):
        try:
            for index in range(25):
                assert spool.enqueue(_event(f"{prefix}-{index}"), 64)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=produce, args=(spool, prefix))
               for spool, prefix in ((spool_a, "a"), (spool_b, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors, errors
    assert spool_a.pending_stats() == (50, spool_a.pending_stats()[1])
    assert len(spool_a.claim_batch(now=time.time() + 10_000, limit=128)) == 50


# ---------------------------------------------------------------- B11 (AC11) producer never blocks

def test_producer_latency_stays_bounded_with_blocked_transport(tmp_path):
    fleet = _fleet()
    release = threading.Event()
    client = fleet.LegacyIngestionClient(
        _API(_BlockedIngestion(release)), spool=_spool(tmp_path))
    started = time.monotonic()
    for index in range(100):
        client._enqueue("trace-create", {"id": f"evt-{index}", "timestamp": "t", "body": {}})
        assert time.monotonic() - started < 2.0, "producer waited on the transport"
    total = time.monotonic() - started
    assert total < 2.0, f"100 enqueues took {total:.3f}s"
    release.set()
    _wait_until(lambda: client._pending_events == 0, timeout=5.0,
                message="client did not drain after release")


# ---------------------------------------------------------------- end-to-end delivery

def test_delivered_events_leave_the_spool_with_counters(tmp_path):
    fleet = _fleet()
    api = _API(_GoodIngestion())
    client = fleet.LegacyIngestionClient(api, spool=_spool(tmp_path))
    for index in range(5):
        client._enqueue("trace-create", {"id": f"evt-{index}", "timestamp": "t", "body": {}})
    client.flush()
    _wait_until(lambda: client._pending_events == 0, message="client did not drain")
    _wait_until(lambda: spool_pending_ids(tmp_path) == set(), message="spool did not drain")
    counters = client._spool.counters()
    assert counters["delivered"] == 5
    assert counters["batches_acked"] >= 1
