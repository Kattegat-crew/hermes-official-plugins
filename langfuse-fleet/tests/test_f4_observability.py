"""F4 observability contract tests: manifest gates, baseline-delta verdict, window
lifecycle, producer bounds and the mid-window recovery drill.

Every test is deterministic and offline (injected readback, fake clock, local
spool in ``tmp_path``). The autouse ``_hermes_home`` fixture keeps every home
under ``tmp_path``.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from langfuse_fleet_plugin.observability import f4_observe, f4_producer, f4_recovery_drill

MANIFEST_PATH = os.path.join(os.path.dirname(f4_observe.__file__), "f4_manifest.json")
START = 1_700_000_000.0
CADENCE = 900
HOURS = 24


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def manifest() -> dict:
    return f4_observe.load_manifest(MANIFEST_PATH)


def mk_sample(index: int, *, rows: int, delivered: int, stuck: int = 0,
              in_flight: int = 0, lost: int = 0, expired: int = 0, traces: int = 100,
              producer: bool = True) -> dict:
    ts = START + index * CADENCE
    counters = {"delivered": delivered, "recovered": 0, "batches_acked": delivered,
                "expired": expired, "rejected": 0, "lost_overflow": lost,
                "lost_oversize": 0, "lost_attempts": 0}
    return {
        "ts_utc": iso(ts),
        "window_id": "w-1",
        "elapsed_h": round(index * CADENCE / 3600, 3),
        "baseline": index == 0,
        "spool": {"counters": counters,
                  "events_by_state": {},
                  "stuck_in_flight": stuck,
                  "in_flight_rows": in_flight,
                  "rows_total": rows, "attempts_sum": rows},
        "readback": {"synthetic": {"http_status": 200, "n_traces": traces,
                                   "duplicate_ids": 0, "metadata_within_allowlist": True,
                                   "content_leak_traces": 0}},
        "producer": ({"present": True, "cycles": index,
                      "last_cycle_utc": iso(ts)} if producer else {"present": False}),
    }


def healthy_window() -> list:
    """Baseline already carries the F3 leftovers (7 rows, 7 delivered); only deltas count."""
    samples = []
    for index in range(97):
        samples.append(mk_sample(index, rows=7 + index, delivered=7 + index, traces=100 + index))
    return samples


# --------------------------------------------------------------- manifest gates

def test_shipped_manifest_is_valid(manifest):
    assert manifest["real_turn"]["provider"] == "openai-codex"
    assert manifest["real_turn"]["model"]
    assert manifest["real_turn"]["fallback_providers"] == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda m: m["real_turn"].pop("model"), "explicit provider AND model"),
    (lambda m: m["real_turn"].update(model=None), "explicit provider AND model"),
    (lambda m: m["real_turn"].update(fallback_providers=["openai"]), "must not declare fallbacks"),
    (lambda m: m["real_turn"].update(billing_mode_auto_accepted=True), "fallbacks"),
    (lambda m: m["real_turn"].update(billing_mode_expected="paid_byo"), "subscription_included"),
    (lambda m: m["sessions"].append({"role": "synthetic", "id": ""}), "empty session id"),
    (lambda m: m["producer"].update(max_total_events=0), "max_total_events"),
    (lambda m: m["producer"].update(
        max_total_events=m["producer"]["max_cycles"] * m["producer"]["events_per_cycle"] - 1),
     "max_total_events"),
    (lambda m: m["window"].update(min_samples=96), "min_samples"),
])
def test_manifest_rejects_unsafe_verdict_shapes(manifest, mutate, needle):
    broken = copy.deepcopy(manifest)
    mutate(broken)
    with pytest.raises(ValueError) as excinfo:
        f4_observe.validate_manifest(broken)
    assert needle in str(excinfo.value)


def test_shipped_manifest_budget_covers_the_full_window(manifest):
    """Contract: the event cap must cover every planned cycle at the measured per-cycle cost.

    ``run()`` counts hook calls, not cycles, so a cap below
    ``max_cycles * events_per_cycle`` truncates the window mid-way and the verdict
    then fails ``no_zero_emission_intervals``/``producer_heartbeat`` on a healthy system.
    """
    calls = []
    hooks = {name: (lambda **kwargs: calls.append(name))
             for name in ("pre_api_request", "post_api_request", "on_session_finalize")}
    per_cycle = f4_producer.emit_turn(hooks, "fleet-f4-live", 1, "w-contract")
    assert per_cycle == len(calls) == 3
    producer = manifest["producer"]
    assert producer["events_per_cycle"] == per_cycle
    assert producer["max_total_events"] >= producer["max_cycles"] * per_cycle


def test_manifest_rejects_embedded_langfuse_key(manifest):
    broken = copy.deepcopy(manifest)
    broken["endpoint"]["public_key"] = "pk-lf-deadbeef"
    with pytest.raises(ValueError, match="key material"):
        f4_observe.validate_manifest(broken)


# -------------------------------------------------------------------- verdict

def test_healthy_window_passes_and_counts_only_deltas(manifest):
    verdict = f4_observe.evaluate(healthy_window(), {"window_id": "w-1", "start_at_epoch": START},
                                  manifest, now_epoch=START + HOURS * 3600)
    assert verdict["verdict"] == "F4 PASS"
    assert verdict["failed_criteria"] == []
    assert verdict["criteria"]["emitted_total_delta"] == 96
    assert verdict["criteria"]["delivered_delta"] == 96
    assert verdict["criteria"]["delivery_ratio"] == 1.0
    assert verdict["criteria"]["trace_delta"] == 96


def test_window_not_elapsed_fails(manifest):
    verdict = f4_observe.evaluate(healthy_window(), {"start_at_epoch": START},
                                  manifest, now_epoch=START + 23 * 3600)
    assert verdict["verdict"] == "F4 FAIL"
    assert "window_elapsed" in verdict["failed_criteria"]


def test_counter_reset_invalidates_the_window(manifest):
    samples = healthy_window()
    samples[40]["spool"]["counters"]["delivered"] = 9  # was 47
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "no_counter_resets" in verdict["failed_criteria"]
    assert verdict["criteria"]["resets"][0]["decreased"] == ["delivered"]


def test_missing_samples_create_gaps(manifest):
    samples = healthy_window()
    del samples[50:53]
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "no_sampling_gaps" in verdict["failed_criteria"]
    assert "samples_complete" in verdict["failed_criteria"]


def test_an_interval_without_emissions_fails(manifest):
    samples = healthy_window()
    samples[11]["spool"]["rows_total"] = samples[10]["spool"]["rows_total"]
    samples[11]["spool"]["counters"]["delivered"] = samples[10]["spool"]["counters"]["delivered"]
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "no_zero_emission_intervals" in verdict["failed_criteria"]


def test_delivery_ratio_below_threshold_fails(manifest):
    samples = healthy_window()
    for index in range(50, len(samples)):
        samples[index]["spool"]["counters"]["delivered"] = 7 + 40  # freeze deliveries
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "delivery_ratio" in verdict["failed_criteria"]


@pytest.mark.parametrize("mutate, criterion", [
    (lambda s: [sample["spool"].update(stuck_in_flight=2) for sample in s[30:]], "no_stuck_rows"),
    (lambda s: [sample["spool"]["counters"].update(lost_overflow=1) for sample in s[30:]],
     "no_lost_counters"),
])
def test_stalled_or_lost_rows_fail(manifest, mutate, criterion):
    samples = healthy_window()
    mutate(samples)
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert criterion in verdict["failed_criteria"]


def test_stale_readback_is_not_evidence_of_active_export(manifest):
    samples = healthy_window()
    for sample in samples:
        sample["readback"]["synthetic"]["n_traces"] = 100  # F3 traces only, nothing new
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "active_export_observed" in verdict["failed_criteria"]


def test_content_leak_and_metadata_leak_fail_readback(manifest):
    samples = healthy_window()
    samples[20]["readback"]["synthetic"]["content_leak_traces"] = 1
    samples[21]["readback"]["synthetic"]["metadata_within_allowlist"] = False
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "readback_clean" in verdict["failed_criteria"]


def test_missing_producer_heartbeat_fails(manifest):
    samples = healthy_window()
    for sample in samples:
        sample["producer"] = {"present": False}
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "producer_heartbeat" in verdict["failed_criteria"]


def test_readback_http_error_fails(manifest):
    samples = healthy_window()
    samples[5]["readback"]["synthetic"] = {"error": "URLError"}
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert "readback_clean" in verdict["failed_criteria"]
    assert verdict["criteria"]["readback_errors"] == 1


# ------------------------------------------------------------------ lifecycle

def _home_with_spool(tmp_path, home):
    spool_path = os.path.join(home, "plugins", "langfuse-fleet", "spool.db")
    os.makedirs(os.path.dirname(spool_path), exist_ok=True)
    spool = f4_recovery_drill.load_spool_module().FleetSpool(spool_path)
    return spool, spool_path


def test_init_creates_window_and_refuses_overwrite(tmp_path, manifest):
    home = str(tmp_path / "home")
    os.makedirs(home)
    first = f4_observe.init_window(home, manifest)
    assert first["ok"] and len(first["state"]["window_id"]) > 10
    state_before = json.load(open(os.path.join(home, "f4_observation_state.json")))
    second = f4_observe.init_window(home, manifest)
    assert "error" in second
    assert json.load(open(os.path.join(home, "f4_observation_state.json"))) == state_before


def test_rotate_archives_history_before_opening_a_new_window(tmp_path, manifest):
    home = str(tmp_path / "home")
    os.makedirs(home)
    first = f4_observe.init_window(home, manifest)
    with open(os.path.join(home, "f4_samples.jsonl"), "w", encoding="utf-8") as handle:
        handle.write('{"sample": 1}\n')
    rotated = f4_observe.init_window(home, manifest, rotate=True)
    assert rotated["ok"]
    assert rotated["state"]["window_id"] != first["state"]["window_id"]
    archive = rotated["state"]["archived_from"]
    assert os.path.exists(os.path.join(archive, "archive_manifest.json"))
    assert os.path.exists(os.path.join(archive, "f4_observation_state.json"))
    assert open(os.path.join(archive, "f4_samples.jsonl"), encoding="utf-8").read().strip() == '{"sample": 1}'
    assert not os.path.exists(os.path.join(home, "f4_samples.jsonl"))
    archived = json.load(open(os.path.join(archive, "archive_manifest.json"), encoding="utf-8"))
    assert archived["source_window"]["window_id"] == first["state"]["window_id"]


def test_sample_marks_baseline_and_skips_null_sessions(tmp_path, manifest):
    home = str(tmp_path / "home")
    os.makedirs(home)
    spool, _ = _home_with_spool(tmp_path, home)
    spool.enqueue({"id": "pre-1", "type": "trace-create", "body": {}}, 10)
    spool.close()
    f4_observe.init_window(home, manifest)
    calls = []

    def fake_readback(env, base, session_id, allowlist, leaks):
        calls.append(session_id)
        return {"http_status": 200, "n_traces": 3, "duplicate_ids": 0,
                "metadata_within_allowlist": True, "content_leak_traces": 0}

    first = f4_observe.take_sample(home, manifest, readback_fn=fake_readback)
    second = f4_observe.take_sample(home, manifest, readback_fn=fake_readback)
    assert first["baseline"] is True and second["baseline"] is False
    assert calls == ["fleet-f4-live", "fleet-f4-live"]  # the null real_turn session is skipped
    assert second["readback"]["real_turn"] == {"skipped": "no session id configured"}
    assert second["spool"]["rows_total"] == 1
    assert second["spool"]["counters"]["delivered"] == 0


def test_spool_snapshot_counts_expired_rows_as_emitted(tmp_path, manifest):
    home = str(tmp_path / "home")
    os.makedirs(home)
    spool, spool_path = _home_with_spool(tmp_path, home)
    old = time.time() - 48 * 3600
    spool.enqueue({"id": "old-1", "type": "trace-create", "body": {}}, 10, created_at=old)
    spool.enqueue({"id": "new-1", "type": "trace-create", "body": {}}, 10)
    spool.expire()
    spool.close()
    snapshot = f4_observe.spool_snapshot(spool_path)
    sample = {"spool": snapshot}
    assert snapshot["counters"]["expired"] == 1
    assert snapshot["rows_total"] == 1
    assert f4_observe.emitted_total(sample) == 2


def test_spool_snapshot_ages_a_stalled_retried_claim(tmp_path, manifest):
    """``stuck_in_flight`` is a real measurement: an ``in_flight`` row whose last transition
    (``next_attempt_at``: the retry schedule it is waiting on) is older than the lease."""
    home = str(tmp_path / "home")
    os.makedirs(home)
    spool, spool_path = _home_with_spool(tmp_path, home)
    spool.enqueue({"id": "stalled-1", "type": "trace-create", "body": {}}, 10)
    spool.claim_batch()
    # attempts=1: the row already went through a delivery attempt and is back in flight
    con = sqlite3.connect(spool_path)
    con.execute("UPDATE events SET attempts=1, next_attempt_at=? WHERE id=?",
                (time.time() - 7200, "stalled-1"))
    con.commit()
    con.close()
    snapshot = f4_observe.spool_snapshot(spool_path, stuck_after_s=3600)
    assert snapshot["in_flight_rows"] == 1
    assert snapshot["stuck_in_flight"] == 1
    fresh = f4_observe.spool_snapshot(spool_path, stuck_after_s=36000)
    assert fresh["stuck_in_flight"] == 0


def test_a_fresh_claim_is_not_stuck(tmp_path, manifest):
    """RED: ``claim_batch`` never stamps the claim and ``enqueue`` leaves ``next_attempt_at``
    at its 0 default, so the old snapshot counted every first-attempt in-flight row as
    stalled with age ``now - 0``. The age must come from the row's own lifecycle."""
    home = str(tmp_path / "home")
    os.makedirs(home)
    spool, spool_path = _home_with_spool(tmp_path, home)
    for index in range(5):
        assert spool.enqueue({"id": f"fresh-{index}", "type": "trace-create", "body": {}}, 10)
    assert len(spool.claim_batch()) == 5          # a real claim, milliseconds old
    snapshot = f4_observe.spool_snapshot(spool_path, stuck_after_s=3600)
    assert snapshot["in_flight_rows"] == 5        # the rows are observed as in flight
    assert snapshot["stuck_in_flight"] == 0       # ...but none of them is stalled
    assert snapshot["in_flight_max_age_s"] < 60
    spool.close()


def test_a_row_in_flight_past_the_lease_is_stuck(tmp_path, manifest):
    """Module-real control: a row queued long ago and now in flight is the lag the criterion
    must catch, and the same row with a comfortable lease passes."""
    home = str(tmp_path / "home")
    os.makedirs(home)
    spool, spool_path = _home_with_spool(tmp_path, home)
    assert spool.enqueue({"id": "late-1", "type": "trace-create", "body": {}}, 10,
                         created_at=time.time() - 7200)
    spool.claim_batch()
    snapshot = f4_observe.spool_snapshot(spool_path, stuck_after_s=3600)
    assert snapshot["stuck_in_flight"] == 1
    assert snapshot["in_flight_max_age_s"] >= 3600
    assert f4_observe.spool_snapshot(spool_path, stuck_after_s=36000)["stuck_in_flight"] == 0
    spool.close()


def test_transient_in_flight_rows_do_not_fail_a_healthy_window(manifest):
    """A healthy window has in-flight rows on every sample while deliveries are in the air;
    only rows past the lease are a failure."""
    samples = healthy_window()
    for index in (7, 20, 41, 58, 73, 90):
        samples[index]["spool"]["in_flight_rows"] = 12   # deliverer mid-POST
    verdict = f4_observe.evaluate(samples, {"start_at_epoch": START}, manifest,
                                  now_epoch=START + HOURS * 3600)
    assert verdict["verdict"] == "F4 PASS"
    assert verdict["failed_criteria"] == []
    assert verdict["criteria"]["in_flight_rows_max"] == 12
    assert verdict["criteria"]["stuck_rows_max"] == 0


def test_sample_without_window_reports_error(tmp_path, manifest):
    home = str(tmp_path / "empty")
    os.makedirs(home)
    assert "not initialized" in f4_observe.take_sample(home, manifest)["error"]


# ------------------------------------------------------------------- producer

def _producer_home(tmp_path, manifest) -> str:
    home = str(tmp_path / "producer-home")
    os.makedirs(home)
    f4_observe.init_window(home, manifest)
    return home


class Clock:
    def __init__(self, step: float = 1.0) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_producer_once_emits_exactly_one_cycle(tmp_path, manifest):
    home = _producer_home(tmp_path, manifest)
    emitted = []
    heartbeat = f4_producer.run(home, manifest,
                                lambda sid, wid, seq: emitted.append((sid, wid, seq)) or 3,
                                once=True)
    assert heartbeat["cycles"] == 1 and heartbeat["events_emitted"] == 3
    assert heartbeat["stopped"] is True
    assert emitted[0][0] == "fleet-f4-live" and emitted[0][2] == 1
    assert not os.path.exists(os.path.join(home, f4_producer.LOCK))
    on_disk = json.load(open(os.path.join(home, f4_producer.HEARTBEAT)))
    assert on_disk["cycles"] == 1 and on_disk["last_cycle_utc"]


def test_producer_respects_cycle_and_event_caps(tmp_path, manifest):
    home = _producer_home(tmp_path, manifest)
    seqs = []
    heartbeat = f4_producer.run(home, manifest,
                                lambda sid, wid, seq: seqs.append(seq) or 1,
                                cycles=4, interval_s=5, clock=Clock(5), sleep=lambda s: None)
    assert heartbeat["cycles"] == 4 and seqs == [1, 2, 3, 4]

    small = copy.deepcopy(manifest)
    small["producer"]["max_total_events"] = 2
    seqs.clear()
    heartbeat = f4_producer.run(home, small, lambda sid, wid, seq: seqs.append(seq) or 1,
                                cycles=10, interval_s=0, clock=Clock(1), sleep=lambda s: None)
    assert heartbeat["cycles"] == 2
    assert heartbeat["limit_reason"] == "max_total_events"


def test_producer_completes_a_full_window_without_hitting_the_event_cap(tmp_path, manifest):
    """96 planned cycles must finish on a valid manifest; the cap is a safety net, not a cutoff."""
    home = _producer_home(tmp_path, manifest)
    per_cycle = 3  # what emit_turn() really does; the manifest must budget for it
    heartbeat = f4_producer.run(home, manifest, lambda *_: per_cycle,
                                interval_s=0, clock=Clock(1), sleep=lambda _s: None)
    assert heartbeat["cycles"] == manifest["producer"]["max_cycles"]
    assert "limit_reason" not in heartbeat
    assert heartbeat["events_emitted"] == heartbeat["cycles"] * per_cycle


def test_producer_stops_when_window_closed(tmp_path, manifest):
    home = _producer_home(tmp_path, manifest)
    state_path = os.path.join(home, "f4_observation_state.json")
    state = json.load(open(state_path))
    state["start_at_epoch"] = time.time() - (HOURS + 1) * 3600
    json.dump(state, open(state_path, "w"))
    assert f4_producer.window_closed(home, manifest) is True
    heartbeat = f4_producer.run(home, manifest, lambda *a: 1, cycles=5)
    assert heartbeat["cycles"] == 0 and heartbeat["limit_reason"] == "window_closed"


def test_producer_refuses_without_window(tmp_path, manifest):
    home = str(tmp_path / "nowindow")
    os.makedirs(home)
    assert f4_producer.window_closed(home, manifest) is True


def test_default_home_never_inherits_the_operator_profile(tmp_path, monkeypatch):
    # Regression: running the sampler from an operational profile shell must not
    # sample that profile's spool nor write window files into its home.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "brokkr-profile"))
    assert f4_observe._default_home() == "/opt/hermes-agent-data/fleet-canary"


def test_producer_refuses_the_operational_home(tmp_path, manifest, monkeypatch):
    home = str(tmp_path / "operator")
    os.makedirs(os.path.join(home, "plugins", "langfuse-fleet"))
    open(os.path.join(home, "plugins", "langfuse-fleet", "__init__.py"), "w").close()
    monkeypatch.setenv("HERMES_HOME", home)
    with pytest.raises(SystemExit, match="operational HERMES_HOME"):
        f4_producer.assert_isolated(home)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    f4_producer.assert_isolated(home)  # isolated copy: allowed


def test_producer_detects_live_owner(tmp_path, manifest):
    home = _producer_home(tmp_path, manifest)
    with open(os.path.join(home, f4_producer.LOCK), "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid()}, handle)
    assert f4_producer.producer_alive(home) == os.getpid()
    os.remove(os.path.join(home, f4_producer.LOCK))
    assert f4_producer.producer_alive(home) is None


# -------------------------------------------------------------- recovery drill

def test_mid_window_recovery_drill_passes(tmp_path):
    report = f4_recovery_drill.drill(str(tmp_path / "drill"))
    assert report["ok"] is True, report
    assert report["recovered"] == f4_recovery_drill.EVENTS
    assert report["checks"]["repeated_ack_credits_nothing"] is True
    assert report["checks"]["no_lost_counters"] is True
    assert report["checks"]["no_duplicate_ids"] is True
