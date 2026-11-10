#!/usr/bin/env python3
"""F4 observation sampler + report for the langfuse-fleet canary (READ-ONLY).

One invocation = one action. No daemon, no sleeping, no cron created here.

Modes
  --init [--rotate]  Start a window. Records the real UTC start from the system
                     clock plus a fresh window_id. If a window already exists it
                     refuses unless --rotate: rotation archives the previous
                     state/samples (never overwrites history) and then opens a
                     new window with new IDs.
  --sample           Take one sample: spool snapshot (opened ``mode=ro``) and a
                     read-only REST readback for every configured session.
  --report           Aggregate every sample and emit the PASS/FAIL verdict.
  --status           Print the window state without touching it.

Contract (what a sample records)
  ts_utc, elapsed_h, window_id (from state), spool{counters, events_by_state,
  rows_total, attempts_sum, in_flight_rows, in_flight_max_age_s, stuck_in_flight,
  stuck_after_s}, readback{session_id: counts}, producer heartbeat counters, and the
  row-level emitted_total derived independently as ``rows_total + counters.expired``
  (the counters table has no "emitted" key).

The report is baseline-delta based: only the difference between the first
(baseline) sample and the last sample counts, per interval. Counters are
cumulative and monotonic; any decrease is reported as a reset and fails the
window instead of silently restarting the ratio.

Exit codes: 0 ok; 3 window/config problem; 4 spool unreadable.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

MONOTONIC_COUNTERS = (
    "delivered", "recovered", "batches_acked", "expired", "rejected",
    "lost_overflow", "lost_oversize", "lost_attempts",
)
COUNTER_KEYS = MONOTONIC_COUNTERS
LOST_PREFIX = "lost_"
# ``claim_batch`` does not stamp the claim, and ``enqueue`` leaves ``next_attempt_at`` at
# its 0 default: a first-attempt row therefore has no claim timestamp. The age of an
# ``in_flight`` row is measured from its own last lifecycle mark that provably precedes
# the claim (see ``spool_snapshot``), and this is the lease it must not exceed.
DEFAULT_STUCK_LEASE_S = 3600.0


# --------------------------------------------------------------------------- io

def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict) -> None:
    """Fail closed on a manifest that could produce a decorative or paid verdict."""
    problems = []
    required = ("schema", "namespace", "window", "sessions", "spool",
                "metadata_allowlist", "thresholds", "producer", "endpoint", "real_turn")
    problems += [f"missing:{key}" for key in required if key not in manifest]
    if problems:
        raise ValueError("; ".join(problems))

    window = manifest["window"]
    for key in ("hours", "cadence_min", "expected_intervals", "min_samples", "max_gap_min"):
        if key not in window:
            problems.append(f"window.missing:{key}")
    if window.get("min_samples", 0) < window.get("expected_intervals", 0) + 1:
        problems.append("window.min_samples must include the baseline sample (>= expected_intervals+1)")

    ids = [s.get("id") for s in manifest["sessions"] if s.get("required")]
    if not ids or any(not isinstance(sid, str) or not sid.strip() for sid in ids):
        problems.append("sessions: every required session needs a non-empty id")
    for session in manifest["sessions"]:
        if isinstance(session.get("id"), str) and not session["id"].strip():
            problems.append("sessions: an empty session id must be null, not \"\"")

    producer = manifest["producer"]
    per_cycle = producer.get("events_per_cycle", 0)
    cycles = producer.get("max_cycles", 0)
    if per_cycle < 1:
        problems.append("producer.events_per_cycle must be >= 1 (hook calls per synthetic turn)")
    if cycles < 1:
        problems.append("producer.max_cycles must be >= 1")
    # run() accumulates hook calls, not cycles: a cap below cycles*per_cycle truncates the
    # window mid-way and the verdict then fails no_zero_emission_intervals on a healthy system.
    budget = producer.get("max_total_events", 0)
    if budget < cycles * per_cycle:
        problems.append(
            f"producer.max_total_events ({budget}) cannot cover the window: "
            f"max_cycles*events_per_cycle = {cycles}*{per_cycle} = {cycles * per_cycle}")
    if producer.get("stuck_lease_s", 0) < producer.get("interval_s", 0):
        problems.append("producer.stuck_lease_s must be >= one producer interval")
    if producer.get("heartbeat_max_age_s", 0) < producer.get("interval_s", 0):
        problems.append("producer.heartbeat_max_age_s must cover one interval")

    real = manifest["real_turn"]
    if manifest.get("real_turn_disposition") != real.get("disposition"):
        problems.append("real_turn_disposition must mirror real_turn.disposition")
    if not real.get("provider") or not real.get("model"):
        problems.append("real_turn requires an explicit provider AND model pin (never auto)")
    if real.get("fallback_providers") or real.get("fallback_model"):
        problems.append("real_turn must not declare fallbacks (they are paid routes)")
    if real.get("billing_mode_expected") != "subscription_included":
        problems.append("real_turn.billing_mode_expected must be subscription_included")
    if real.get("billing_mode_auto_accepted") is not False:
        problems.append("real_turn must not auto-accept a paid billing mode (fallbacks stay off)")
    if real.get("disposition") not in {"pending_auth_provisioning", "executed", "not_applicable"}:
        problems.append("real_turn.disposition unknown")

    blob = json.dumps(manifest).lower()
    if "sk-lf-" in blob or "pk-lf-" in blob:
        problems.append("manifest must not carry Langfuse key material")

    if problems:
        raise ValueError("manifest rejected: " + "; ".join(problems))


def read_env(path: str) -> dict:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.strip().partition("=")
            if value:
                env[key] = value
    return env


def _claim_age(row, now: float) -> float:
    """Seconds since the last lifecycle mark that provably precedes the claim.

    ``claim_batch`` only flips ``state``/``owner``: it never stamps a claim time, and a
    first attempt carries ``next_attempt_at = 0`` (the column default). So the evidence a
    row is judged on is its own history — ``created_at`` while ``attempts == 0``, the
    retry schedule ``settle_batch`` wrote (``next_attempt_at``) once a delivery has
    failed. Both precede the claim, so this is an *upper* bound of the time the row has
    been in flight; a row that aged in the queue longer than the lease before being
    claimed is flagged too, which is delivery lag and not a healthy window.
    """
    attempts, created_at, next_attempt_at = row
    stamp = float(next_attempt_at) if (attempts and next_attempt_at) else float(created_at)
    return now - stamp


def spool_snapshot(spool_path: str, stuck_after_s: float = DEFAULT_STUCK_LEASE_S,
                   now: float | None = None) -> dict:
    now = time.time() if now is None else now
    con = sqlite3.connect(f"file:{spool_path}?mode=ro", uri=True)
    try:
        stored = dict(con.execute("SELECT key, value FROM counters"))
        states = dict(con.execute("SELECT state, COUNT(*) FROM events GROUP BY state"))
        rows_total, attempts_sum = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(attempts), 0) FROM events").fetchone()
        in_flight = con.execute(
            "SELECT attempts, created_at, next_attempt_at FROM events WHERE state='in_flight'"
        ).fetchall()
    finally:
        con.close()
    # A fresh spool has no rows in ``counters``; record zeros so every sample
    # (and every archived sample) carries the same schema.
    counters = {key: int(stored.get(key, 0)) for key in COUNTER_KEYS}
    counters.update({key: int(value) for key, value in stored.items() if key not in counters})
    ages = [_claim_age(row, now) for row in in_flight]
    return {
        "counters": counters,
        "events_by_state": states,
        "rows_total": int(rows_total),
        "attempts_sum": int(attempts_sum),
        "in_flight_rows": len(ages),
        "in_flight_max_age_s": round(max(ages), 3) if ages else 0.0,
        "stuck_in_flight": sum(1 for age in ages if age >= float(stuck_after_s)),
        "stuck_after_s": float(stuck_after_s),
    }


def emitted_total(sample: dict) -> int:
    """Rows ever enqueued, measured independently of the delivery counters."""
    spool = sample["spool"]
    return int(spool["rows_total"]) + int(spool["counters"].get("expired", 0))


def readback_session(env: dict, base_url: str, session_id: str,
                     allowlist: set, leak_tokens: tuple) -> dict:
    """Read-only REST readback. Returns counts only; never persists payloads."""
    auth = base64.b64encode(
        f"{env['HERMES_LANGFUSE_PUBLIC_KEY']}:{env['HERMES_LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    url = f"{base_url}/api/public/traces?sessionId={urllib.parse.quote(session_id)}&limit=100"
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode())
    traces = payload.get("data", payload) if isinstance(payload, dict) else payload
    ids = [t.get("id") for t in traces if isinstance(t, dict)]
    metadata_ok = all(
        isinstance(t.get("metadata"), dict) and set(t["metadata"]) <= allowlist
        for t in traces
    )
    leaks = sum(1 for t in traces
                if any(token in json.dumps(t) for token in leak_tokens))
    return {
        "http_status": 200,
        "n_traces": len(ids),
        "duplicate_ids": len(ids) - len(set(ids)),
        "metadata_within_allowlist": metadata_ok,
        "content_leak_traces": leaks,
    }


def read_heartbeat(path: str) -> dict:
    if not os.path.exists(path):
        return {"present": False}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"present": False}
    data["present"] = True
    return data


# ------------------------------------------------------------------ lifecycle

def archive_window(home: str, state: dict) -> str:
    """Copy (never move-and-lose) the previous window into f4_archive/<id>/."""
    window_id = state.get("window_id") or state.get("start_at_utc", "unknown")
    stamp = str(window_id).replace(":", "").replace("-", "")
    target = os.path.join(home, "f4_archive", stamp)
    os.makedirs(target, exist_ok=True)
    for name in ("f4_observation_state.json", "f4_samples.jsonl"):
        source = os.path.join(home, name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(target, name))
            shutil.copy2(source, os.path.join(target, name + ".bak"))
    manifest = {
        "archived_at_utc": now_utc(),
        "source_window": state,
        "files": sorted(os.listdir(target)),
    }
    with open(os.path.join(target, "archive_manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return target


def init_window(home: str, manifest: dict, rotate: bool = False) -> dict:
    state_path = os.path.join(home, "f4_observation_state.json")
    samples_path = os.path.join(home, "f4_samples.jsonl")
    archived = None
    if os.path.exists(state_path):
        if not rotate:
            return {"error": "window already initialized; use --rotate to archive and open a new one",
                    "state": json.load(open(state_path, encoding="utf-8"))}
        archived = archive_window(home, json.load(open(state_path, encoding="utf-8")))
        # history is archived first; only then the live files are reset
        if os.path.exists(samples_path):
            os.remove(samples_path)
    start = time.time()
    state = {
        "window_id": f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "namespace": manifest["namespace"],
        "start_at_utc": now_utc(),
        "start_at_epoch": start,
        "window_hours": manifest["window"]["hours"],
        "cadence_min": manifest["window"]["cadence_min"],
        "sessions": [s["id"] for s in manifest["sessions"] if s.get("required")],
        "samples": 0,
        "archived_from": archived,
    }
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    return {"ok": True, "state": state}


def take_sample(home: str, manifest: dict, readback_fn=readback_session) -> dict:
    state_path = os.path.join(home, "f4_observation_state.json")
    if not os.path.exists(state_path):
        return {"error": "window not initialized; run --init first"}
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
    spool_path = os.path.join(home, manifest["spool"]["relpath"])
    sample = {
        "ts_utc": now_utc(),
        "window_id": state["window_id"],
        "elapsed_h": round((time.time() - state["start_at_epoch"]) / 3600, 3),
        "baseline": state.get("samples", 0) == 0,
    }
    try:
        sample["spool"] = spool_snapshot(
            spool_path, stuck_after_s=float(manifest["producer"]["stuck_lease_s"]))
    except Exception as exc:  # noqa: BLE001 - a sample without the spool is worthless
        return {"error": f"spool unreadable: {type(exc).__name__}: {exc}"}

    env = read_env(os.path.join(home, ".env"))
    base = env.get("HERMES_LANGFUSE_BASE_URL", manifest["endpoint"]["base_url"])
    allowlist = set(manifest["metadata_allowlist"])
    leaks = tuple(manifest["leak_tokens"])
    readback = {}
    for session in manifest["sessions"]:
        session_id = session.get("id")
        if not session_id:
            readback[session["role"]] = {"skipped": "no session id configured"}
            continue
        try:
            readback[session["role"]] = readback_fn(env, base, session_id, allowlist, leaks)
        except Exception as exc:  # noqa: BLE001 - gaps are recorded, never silent
            readback[session["role"]] = {"error": type(exc).__name__}
    sample["readback"] = readback
    sample["producer"] = read_heartbeat(os.path.join(home, "f4_producer_heartbeat.json"))

    with open(os.path.join(home, "f4_samples.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, sort_keys=True) + "\n")
    state["samples"] = state.get("samples", 0) + 1
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    return sample


# -------------------------------------------------------------------- verdict

def _intervals(samples: list) -> list:
    out = []
    for prev, cur in zip(samples, samples[1:]):
        minutes = round((_epoch(cur["ts_utc"]) - _epoch(prev["ts_utc"])) / 60, 2)
        entry = {"from": prev["ts_utc"], "to": cur["ts_utc"], "minutes": minutes}
        entry["emitted_delta"] = emitted_total(cur) - emitted_total(prev)
        current = cur["spool"]["counters"]
        before = prev["spool"]["counters"]
        entry["delivered_delta"] = current.get("delivered", 0) - before.get("delivered", 0)
        entry["retries_delta"] = cur["spool"]["attempts_sum"] - prev["spool"]["attempts_sum"]
        entry["lost_delta"] = sum(current.get(key, 0) - before.get(key, 0)
                                  for key in current if key.startswith(LOST_PREFIX))
        out.append(entry)
    return out


def _resets(samples: list) -> list:
    resets = []
    for prev, cur in zip(samples, samples[1:]):
        before, after = prev["spool"], cur["spool"]
        shrinking = [
            key for key in MONOTONIC_COUNTERS
            if after["counters"].get(key, 0) < before["counters"].get(key, 0)
        ]
        if emitted_total(cur) < emitted_total(prev):
            shrinking.append("emitted_total")
        if shrinking:
            resets.append({"from": prev["ts_utc"], "to": cur["ts_utc"], "decreased": shrinking})
    return resets


def evaluate(samples: list, window: dict, manifest: dict, now_epoch: float | None = None) -> dict:
    """Baseline-delta verdict. Every criterion is a hard gate; PASS needs all."""
    thresholds = manifest["thresholds"]
    prod = manifest["producer"]
    if not samples:
        return {"error": "no samples", "verdict": "F4 FAIL — no samples"}
    base, last = samples[0], samples[-1]
    intervals = _intervals(samples)
    gaps = [i for i in intervals
            if i["minutes"] > manifest["window"]["max_gap_min"]]
    empty_intervals = [i for i in intervals if i["emitted_delta"] <= 0]
    resets = _resets(samples)

    emitted_delta = emitted_total(last) - emitted_total(base)
    delivered_delta = last["spool"]["counters"].get("delivered", 0) - \
        base["spool"]["counters"].get("delivered", 0)
    lost_delta = sum(last["spool"]["counters"].get(k, 0) - base["spool"]["counters"].get(k, 0)
                     for k in last["spool"]["counters"] if k.startswith(LOST_PREFIX))
    retries_delta = last["spool"]["attempts_sum"] - base["spool"]["attempts_sum"]
    # ``fleet_spool`` never writes a ``blocked`` state; a stalled claim is the real signal,
    # aged by ``spool_snapshot`` from each row's own lifecycle mark (see ``_claim_age``).
    stuck_max = max((int(sample["spool"].get("stuck_in_flight", 0)) for sample in samples),
                    default=0)
    in_flight_max = max((int(sample["spool"].get("in_flight_rows", 0)) for sample in samples),
                        default=0)

    start_epoch = window.get("start_at_epoch")
    real_elapsed_h = round((now_epoch - start_epoch) / 3600, 3) if (now_epoch and start_epoch) else None
    elapsed_h = real_elapsed_h if real_elapsed_h is not None else float(last["elapsed_h"])

    readbacks = []
    readback_errors = 0
    for sample in samples:
        for role, value in (sample.get("readback") or {}).items():
            if not isinstance(value, dict):
                continue
            if "error" in value:
                readback_errors += 1
            elif "skipped" not in value:
                readbacks.append((role, value))
    per_session_first, per_session_last = {}, {}
    for role, value in readbacks:
        per_session_first.setdefault(role, value.get("n_traces", 0))
        per_session_last[role] = value.get("n_traces", 0)
    trace_delta = sum(per_session_last.get(r, 0) - per_session_first.get(r, 0) for r in per_session_last)
    configured_roles = {s["role"] for s in manifest["sessions"] if s.get("required")}
    sessions_seen = {role for role, _ in readbacks}

    heartbeats = [s.get("producer") or {} for s in samples]
    hb_present = [hb for hb in heartbeats if hb.get("present")]
    hb_cycles = [int(hb.get("cycles", 0)) for hb in hb_present]
    hb_last_age = None
    if hb_present and hb_present[-1].get("last_cycle_utc"):
        hb_last_age = round(_epoch(last["ts_utc"]) - _epoch(hb_present[-1]["last_cycle_utc"]), 1)

    checks = {
        "window_elapsed": elapsed_h >= manifest["window"]["hours"],
        "samples_complete": len(samples) >= manifest["window"]["min_samples"],
        "no_sampling_gaps": not gaps,
        "no_zero_emission_intervals": not empty_intervals,
        "emissions_observed": emitted_delta > 0,
        "delivery_ratio": emitted_delta > 0 and delivered_delta / emitted_delta >= thresholds["delivery_ratio_min"],
        "no_stuck_rows": stuck_max <= thresholds.get("stuck_rows_max", 0),
        "no_lost_counters": lost_delta == 0,
        "no_counter_resets": not resets,
        "producer_heartbeat": bool(hb_present) and len(set(hb_cycles)) > 1
                              and hb_last_age is not None
                              and hb_last_age <= prod["heartbeat_max_age_s"],
        "readback_clean": readback_errors <= thresholds["readback_errors_max"]
                           and sum(v.get("duplicate_ids", 0) for _, v in readbacks) <= thresholds["duplicate_ids_max"]
                           and sum(v.get("content_leak_traces", 0) for _, v in readbacks) <= thresholds["content_leak_traces_max"]
                           and all(v.get("metadata_within_allowlist") for _, v in readbacks),
        "readback_covers_sessions": sessions_seen >= configured_roles,
        "active_export_observed": trace_delta > 0,
    }

    verdict = {
        "window_id": window.get("window_id"),
        "start_at_utc": window.get("start_at_utc"),
        "n_samples": len(samples),
        "baseline_ts_utc": base["ts_utc"],
        "last_ts_utc": last["ts_utc"],
        "elapsed_h": elapsed_h,
        "criteria": {
            "emitted_total_delta": emitted_delta,
            "delivered_delta": delivered_delta,
            "retries_delta": retries_delta,
            "lost_delta": lost_delta,
            "delivery_ratio": round(delivered_delta / emitted_delta, 4) if emitted_delta else None,
            "stuck_rows_max": stuck_max,
            "in_flight_rows_max": in_flight_max,
            "in_flight_max_age_s": max((float(sample["spool"].get("in_flight_max_age_s", 0.0))
                                        for sample in samples), default=0.0),
            "gaps": gaps,
            "zero_emission_intervals": empty_intervals,
            "resets": resets,
            "readback_errors": readback_errors,
            "readback_samples": len(readbacks),
            "sessions_seen": sorted(sessions_seen),
            "trace_delta": trace_delta,
            "producer_heartbeats": len(hb_present),
            "producer_last_heartbeat_age_s": hb_last_age,
        },
        "checks": checks,
        "failed_criteria": sorted(name for name, passed in checks.items() if not passed),
    }
    verdict["verdict"] = "F4 PASS" if all(checks.values()) else "F4 FAIL"
    return verdict


def report(home: str, manifest: dict, now_epoch: float | None = None) -> dict:
    state_path = os.path.join(home, "f4_observation_state.json")
    samples_path = os.path.join(home, "f4_samples.jsonl")
    if not os.path.exists(samples_path):
        return {"error": "no samples"}
    with open(samples_path, encoding="utf-8") as handle:
        samples = [json.loads(line) for line in handle if line.strip()]
    window = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    return evaluate(samples, window, manifest, now_epoch if now_epoch is not None else time.time())


# ------------------------------------------------------------------------ cli

def _default_home() -> str:
    # Deliberately NOT os.environ["HERMES_HOME"]: the sampler usually runs from an
    # operational profile shell, and inheriting that would sample the operator's
    # own spool and write window files into their profile. The canary home is
    # explicit, and --home overrides it.
    return "/opt/hermes-agent-data/fleet-canary"


def main(argv: list) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    home, manifest_path = _default_home(), os.path.join(here, "f4_manifest.json")
    args = list(argv[1:])
    mode = "sample"
    for flag in ("--init", "--sample", "--report", "--status"):
        if flag in args:
            mode = flag[2:]
            args.remove(flag)
    rotate = "--rotate" in args
    if "--home" in args:
        home = args[args.index("--home") + 1]
    if "--manifest" in args:
        manifest_path = args[args.index("--manifest") + 1]

    manifest = load_manifest(manifest_path)
    if mode == "init":
        result = init_window(home, manifest, rotate=rotate)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 3
    if mode == "status":
        path = os.path.join(home, "f4_observation_state.json")
        print(json.dumps(json.load(open(path, encoding="utf-8"))
                         if os.path.exists(path) else {"error": "not initialized"}, sort_keys=True))
        return 0
    if mode == "report":
        result = report(home, manifest)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("verdict") == "F4 PASS" else 3
    sample = take_sample(home, manifest)
    print(json.dumps(sample, sort_keys=True))
    return 0 if "error" not in sample else (3 if "not initialized" in sample["error"] else 4)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
