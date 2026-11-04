#!/usr/bin/env python3
"""Reproducible mid-window recovery drill (isolated home, never PROD).

Simulates the failure the F4 window must survive without touching the real
exporters or the PROD Langfuse instance:

1. a child process enqueues N events, claims them (state ``in_flight``) and is
   killed with SIGKILL — no flush, no atexit;
2. a fresh process opens the same spool: ``recover()`` returns the orphaned
   leases to ``pending`` (counter ``recovered``);
3. the batch is delivered and acked (``delivered`` + N, ``batches_acked`` + 1);
4. a repeated ack of the same ids credits nothing (idempotent: at-least-once,
   never exactly-once);
5. counters are conserved: delivered == N, no lost_*, no duplicate ids.

Exit 0 only if every assertion holds. No network, no model call.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPOOL_MODULE = os.path.join(os.path.dirname(HERE), "fleet_spool.py")
EVENTS = 3


def load_spool_module():
    spec = importlib.util.spec_from_file_location("fleet_spool", SPOOL_MODULE)
    if spec is None or spec.loader is None:
        raise ImportError(SPOOL_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(spool_path: str, count: int) -> int:
    module = load_spool_module()
    spool = module.FleetSpool(spool_path)
    ids = []
    for index in range(count):
        event = {"id": f"drill-{index:03d}", "type": "trace-create",
                 "body": {"sessionId": "fleet-f4-drill", "metadata": {"source": "drill"}}}
        if spool.enqueue(event, len(json.dumps(event))):
            ids.append(event["id"])
    claimed = spool.claim_batch(limit=count)
    print(json.dumps({"child_enqueued": len(ids), "child_claimed": [e["id"] for e in claimed],
                      "counters": spool.counters()}))
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)
    return 1


def drill(workdir: str | None = None) -> dict:
    auto = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="f4-recovery-")
    spool_path = os.path.join(workdir, "spool.db")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", spool_path, str(EVENTS)],
                          capture_output=True, text=True, timeout=120)
    child_report = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}

    module = load_spool_module()
    spool = module.FleetSpool(spool_path)  # __init__ runs recover(): orphan leases return to pending
    recovered = spool.counters().get("recovered", 0)
    before = spool.counters()
    claimed = spool.claim_batch(limit=EVENTS)
    ids = [event["id"] for event in claimed]
    delivered = spool.ack(ids)
    after = spool.counters()
    repeated = spool.ack(ids)  # must credit nothing: rows are no longer in_flight
    final = spool.counters()
    pending_left, _ = spool.pending_stats()
    spool.close()
    import sqlite3
    con = sqlite3.connect(f"file:{spool_path}?mode=ro", uri=True)
    try:
        rows = [row[0] for row in con.execute("SELECT id FROM events").fetchall()]
    finally:
        con.close()

    checks = {
        "child_killed_by_sigkill": proc.returncode == -signal.SIGKILL,
        "orphans_recovered": recovered == EVENTS,
        "batch_delivered": delivered == EVENTS,
        "delivered_counter_conserved": after.get("delivered", 0) - before.get("delivered", 0) == EVENTS,
        "batches_acked_incremented": after.get("batches_acked", 0) > before.get("batches_acked", 0),
        "repeated_ack_credits_nothing": repeated == 0 and final.get("delivered") == after.get("delivered"),
        "no_pending_left": pending_left == 0,
        "no_lost_counters": all(value == 0 for key, value in final.items() if key.startswith("lost_")),
        "no_duplicate_ids": len(rows) == len(set(rows)),
    }
    report = {
        "drill": "f4-mid-window-recovery",
        "workdir": workdir,
        "child": child_report,
        "recovered": recovered,
        "counters_before_delivery": before,
        "counters_after_delivery": after,
        "counters_after_repeat_ack": final,
        "checks": checks,
        "ok": all(checks.values()),
        "note": "at-least-once delivery; exactly-once is not claimed",
    }
    if auto:
        shutil.rmtree(workdir, ignore_errors=True)
    return report


def main(argv: list) -> int:
    args = argv[1:]
    if args and args[0] == "--child":
        return child(args[1], int(args[2]))
    workdir = args[args.index("--workdir") + 1] if "--workdir" in args else None
    report = drill(workdir)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
