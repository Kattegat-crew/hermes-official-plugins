#!/usr/bin/env python
"""F3 canary driver for the langfuse-fleet plugin (isolated HERMES_HOME).

Phases (argv[1]):
  allowlist-demo  poisoned metadata + blocked transport; proves the metadata-only
                  allowlist and durable spool BEFORE any network delivery.
  canary          fresh spool, real transport, one deterministic synthetic turn;
                  flushes, prints spool counters + trace ids.
  crash-outage    fresh spool, blocked transport (simulated outage), claim a batch
                  to land rows in-flight, then SIGKILL the process (crash recovery
                  precondition; no data ever leaves the host).
  recover         restart: no new events; the constructor re-delivers recovered
                  rows; flushes and prints counters.

Prints one JSON object per phase on the last line (prefix F3_RESULT:).
No client data: all payloads are FAKE/synthetic and capture=metadata filters them.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

CANARY = "/opt/hermes-agent-data/fleet-canary"
PHASE = sys.argv[1] if len(sys.argv) > 1 else "allowlist-demo"
FRESH = "--fresh" in sys.argv


def fail(msg: str):
    print(f"F3_RESULT: {json.dumps({'phase': PHASE, 'ok': False, 'error': msg})}")
    sys.exit(1)


# --- env: force canary .env over any inherited profile env -------------------
env = {}
with open(os.path.join(CANARY, ".env")) as f:
    for line in f:
        k, _, v = line.strip().partition("=")
        if v:
            env[k] = v
for k, v in env.items():
    os.environ[k] = v
os.environ["HERMES_HOME"] = CANARY  # spool + config scope

# --- import plugin from the canary copy -------------------------------------
PKG_DIR = os.path.join(CANARY, "plugins", "langfuse-fleet")
spec = importlib.util.spec_from_file_location(
    "langfuse_fleet", os.path.join(PKG_DIR, "__init__.py"),
    submodule_search_locations=[PKG_DIR])
m = importlib.util.module_from_spec(spec)
sys.modules["langfuse_fleet"] = m
try:
    spec.loader.exec_module(m)
except Exception as exc:  # noqa: BLE001
    fail(f"plugin import failed: {type(exc).__name__}: {exc}")

assert m._transport_mode() == "legacy", f"transport={m._transport_mode()}"
assert m._capture_mode() == "metadata", f"capture={m._capture_mode()}"


class _Ctx:
    profile_name = "canary"
    def __init__(self):
        self.hooks = {}
    def register_hook(self, name, fn):
        self.hooks[name] = fn
    def current_cron_execution(self):
        return None  # no scheduler in the canary home


ctx = _Ctx()
m.register(ctx)
assert len(ctx.hooks) >= 8, f"only {len(ctx.hooks)} hooks registered"


def settled_client():
    client = m._get_langfuse()  # lazy-builds once and settles the slot
    if client is None or client is m._INIT_FAILED:
        fail(f"client init failed: {getattr(m, '_INIT_ERROR', 'unknown')}")
    return client


def block_api(client):
    state = {"attempts": 0}

    class _Batch:
        def batch(self, *a, **k):
            state["attempts"] += 1
            raise ConnectionError("F3 fault injection: simulated outage")

    class _Ing:
        batch = _Batch().batch  # bound method, not the instance

    class _API:
        ingestion = _Ing()

    client._api = _API()  # replace wholesale: LangfuseAPI re-creates .ingestion per access
    return state


def spool_of(client):
    return client._spool


def spool_rows(spool):
    rows = []
    con = sqlite3.connect(str(Path(spool.path) if hasattr(spool, "path") else spool._path))
    try:
        for row in con.execute("SELECT id, type, state, attempts, body_json FROM events ORDER BY created_at"):
            rows.append({"id": row[0][:8], "type": row[1], "state": row[2],
                         "attempts": row[3], "body_keys": sorted(json.loads(row[4]).keys()),
                         "body_json": row[4]})
    finally:
        con.close()
    return rows


def dump_and_check(rows):
    poisoned = [p for p in ("FAKE", "password", "sk-lf-") if any(p in r["body_json"] for r in rows)]
    return poisoned


# --- deterministic synthetic turn (no client data) ---------------------------
def emit_turn(hooks, *, turn_suffix):
    task_id = f"canary-task-{turn_suffix}"
    session_id = "fleet-f3-canary"
    turn_id = f"turn-f3-{turn_suffix}"
    req_id = f"req-f3-{turn_suffix}"
    common = dict(task_id=task_id, session_id=session_id, turn_id=turn_id, api_request_id=req_id)
    hooks["pre_api_request"](
        task_id=task_id, session_id=session_id, platform="cli", model="synthetic-model",
        provider="synthetic", base_url="http://synthetic.invalid/v1", api_mode="chat_completions",
        api_call_count=1, request_messages=[{"role": "user", "content": "FAKE-synthetic canary prompt"}],
        message_count=1, approx_input_tokens=7, turn_id=turn_id, api_request_id=req_id)
    hooks["pre_tool_call"](tool_name="canary_tool",
                           args={"deterministic_param": "ok", "password": "FAKE-pw-never-capture",
                                 "api_token": "sk-lf-FAKE-never-capture"},
                           task_id=task_id, session_id=session_id, tool_call_id="tc_f3_001",
                           turn_id=turn_id, api_request_id=req_id)
    hooks["post_tool_call"](tool_name="canary_tool", args={"deterministic_param": "ok"},
                            result="FAKE-synthetic tool result", status="ok",
                            task_id=task_id, session_id=session_id, tool_call_id="tc_f3_001",
                            turn_id=turn_id, api_request_id=req_id)
    hooks["post_api_request"](task_id=task_id, session_id=session_id, provider="synthetic",
                              base_url="http://synthetic.invalid/v1", api_mode="chat_completions",
                              model="synthetic-model", api_call_count=1, assistant_content_chars=31,
                              assistant_tool_call_count=0, api_duration=0.123, finish_reason="stop",
                              usage={"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
                              turn_id=turn_id, api_request_id=req_id, response_model="synthetic-model")
    hooks["on_session_finalize"](session_id=session_id, reason="canary")
    return common


def trace_ids():
    out = {}
    for key, st in getattr(m, "_TRACE_STATE", {}).items():
        tid = getattr(st, "trace_id", None) or getattr(st, "trace", None)
        out[str(key)] = tid if isinstance(tid, str) else str(tid)
    return out


def result(**kw):
    payload = {"phase": PHASE, "ok": True, "hermes_home": CANARY,
               "transport": m._transport_mode(), "capture": m._capture_mode(), **kw}
    print(f"F3_RESULT: {json.dumps(payload)}")


if FRESH:
    spool_path = os.path.join(CANARY, "plugins", "langfuse-fleet", "spool.db")
    for suffix in ("", "-wal", "-shm"):
        q = Path(spool_path + suffix)
        if q.exists():
            q.unlink()
    print(f"fresh spool: {spool_path}")

client = settled_client()

if PHASE == "allowlist-demo":
    attempts = block_api(client)
    spool = spool_of(client)
    emit_turn(ctx.hooks, turn_suffix="allowlist")
    time.sleep(3.0)  # worker attempts + backoff against the blocked transport
    rows = spool_rows(spool)
    if not rows:
        fail("spool empty after blocked emission — persistence not demonstrated")
    poisoned = dump_and_check(rows)
    counters = spool.counters()
    result(spool_rows=[{k: v for k, v in r.items() if k != "body_json"} for r in rows],
           body_keys=sorted({k for r in rows for k in r["body_keys"]}),
           poisoned_tokens_found=poisoned, blocked_attempts=attempts["attempts"],
           counters=counters, assertion_allowlist_clean=(not poisoned))

elif PHASE == "canary":
    spool = spool_of(client)
    emit_turn(ctx.hooks, turn_suffix="canary")
    time.sleep(0.5)
    rows_before = spool_rows(spool)
    poisoned = dump_and_check(rows_before)
    m.flush_legacy_delivery(30)
    counters = spool.counters()
    result(trace_ids=trace_ids(), rows_before=[{k: v for k, v in r.items() if k != "body_json"} for r in rows_before],
           poisoned_tokens_found=poisoned, counters=counters,
           assertion_allowlist_clean=(not poisoned), assertion_all_delivered=(counters.get("delivered", 0) >= 4))

elif PHASE == "crash-outage":
    attempts = block_api(client)
    spool = spool_of(client)
    emit_turn(ctx.hooks, turn_suffix="crash")
    time.sleep(2.0)
    counters_mid = spool.counters()
    print(f"pre-kill counters: {json.dumps(counters_mid)} blocked_attempts={attempts['attempts']}")
    claimed = spool.claim_batch()  # leave rows in-flight across the crash
    print(f"manually claimed {len(claimed)} events to force in-flight state at kill")
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)  # crash: no atexit, no flush
    result(counters=counters_mid)  # unreachable (SIGKILL)

elif PHASE == "recover":
    spool = spool_of(client)
    counters_before = spool.counters()
    recovered = spool.recover()  # constructor path also does this; explicit for evidence
    m.flush_legacy_delivery(60)
    # flush() only waits for this process's own enqueued events; recovered orphans
    # are claimed by the worker asynchronously — keep the process alive until the
    # spool reaches terminal state (rows deleted on ack).
    deadline = time.time() + 60
    while time.time() < deadline:
        if not spool_rows(spool):
            break
        time.sleep(1.0)
    counters_after = spool.counters()
    rows_left = spool_rows(spool)
    result(counters_before=counters_before, recovered_events=recovered,
           counters_after=counters_after, rows_left=len(rows_left),
           assertion_no_pending=(not rows_left))
else:
    fail(f"unknown phase {PHASE}")
