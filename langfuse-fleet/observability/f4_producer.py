#!/usr/bin/env python3
"""Bounded, zero-LLM synthetic producer for the F4 observation window.

It emits metadata-only synthetic turns through the REAL langfuse-fleet hooks of
an isolated canary home (never an operational profile) so the 24 h window
observes live delivery instead of reading historical traces.

Hard bounds (all from f4_manifest.json, none hardcoded):
  * one cycle every ``producer.interval_s`` (default 900 s),
  * ``producer.max_cycles`` cycles and ``producer.max_total_events`` hook calls
    (the counter measures hook calls, 3 per synthetic turn, so the manifest must
    budget ``max_cycles * events_per_cycle`` or the window is cut short),
  * stops on SIGTERM/SIGINT and on ``--stop`` from another process,
  * refuses to run if the window is closed (elapsed >= window.hours) or if
    another producer owns the lock file,
  * heartbeat file updated every cycle (f4_producer_heartbeat.json).

No model call, no prompt content, no client data: the emitted turn is a
synthetic ``pre_api_request``/``post_api_request`` pair with throwaway ids.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOME = "/opt/hermes-agent-data/fleet-canary"
HEARTBEAT = "f4_producer_heartbeat.json"
LOCK = "f4_producer.pid"


def assert_isolated(home: str) -> None:
    """Refuse to emit through an operational profile: F4 only touches the canary home."""
    inherited = os.environ.get("HERMES_HOME")
    if inherited and os.path.realpath(inherited) == os.path.realpath(home):
        raise SystemExit(f"refusing: {home} is the operational HERMES_HOME of this shell")
    if not os.path.exists(os.path.join(home, "plugins", "langfuse-fleet", "__init__.py")):
        raise SystemExit(f"refusing: {home} has no isolated langfuse-fleet plugin copy")


def utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    # Late import: this module runs both as a script and as a package submodule.
    try:
        from .f4_observe import validate_manifest
    except ImportError:  # executed as ``python3 f4_producer.py``
        from f4_observe import validate_manifest
    try:
        validate_manifest(manifest)
    except ValueError as exc:
        # Fail before a 24 h run is launched, not 16 h in when the budget cuts the window.
        raise SystemExit(str(exc))
    return manifest


def synthetic_session(manifest: dict) -> str:
    for session in manifest["sessions"]:
        if session.get("role") == "synthetic" and session.get("id"):
            return session["id"]
    raise SystemExit("manifest has no synthetic session id")


class Ctx:
    """Minimal PluginContext: hooks only (no scheduler, no cron, no tools)."""

    profile_name = "canary"

    def __init__(self) -> None:
        self.hooks: dict = {}

    def register_hook(self, name, fn) -> None:
        self.hooks[name] = fn

    def current_cron_execution(self):
        return None


def _module_from_path(name: str, path: str, package_dir: str | None = None):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[package_dir] if package_dir else None)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bootstrap_env(home: str) -> dict:
    """Isolated home owns its config: load <home>/.env, pin HERMES_HOME, never log values."""
    env = {}
    path = os.path.join(home, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if value:
                    env[key] = value
    os.environ.update(env)
    os.environ["HERMES_HOME"] = home
    return {key: "<set>" for key in env}  # key names only, never values


def load_plugin(home: str):
    pkg_dir = os.path.join(home, "plugins", "langfuse-fleet")
    return _module_from_path("langfuse_fleet", os.path.join(pkg_dir, "__init__.py"), pkg_dir)


def emit_turn(hooks: dict, session_id: str, seq: int, window_id: str) -> int:
    """One synthetic, metadata-only turn. Returns the number of hook calls made."""
    turn_id = f"f4-{window_id}-{seq:04d}"
    common = dict(task_id=f"f4-task-{window_id}-{seq:04d}", session_id=session_id,
                  turn_id=turn_id, api_request_id=f"f4-req-{window_id}-{seq:04d}")
    hooks["pre_api_request"](
        platform="cli", model="synthetic-f4-model", provider="synthetic",
        base_url="http://synthetic.invalid/v1", api_mode="chat_completions",
        api_call_count=1, request_messages=[{"role": "user", "content": "synthetic canary prompt"}],
        message_count=1, approx_input_tokens=7, **common)
    hooks["post_api_request"](
        provider="synthetic", base_url="http://synthetic.invalid/v1",
        api_mode="chat_completions", model="synthetic-f4-model", api_call_count=1,
        assistant_content_chars=24, assistant_tool_call_count=0, api_duration=0.05,
        finish_reason="stop",
        usage={"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
        response_model="synthetic-f4-model", **common)
    hooks["on_session_finalize"](session_id=session_id, reason="f4-canary")
    return 3


def plugin_emitter(home: str):
    """Real emitter bound to the canary plugin copy and its registered hooks."""
    module = load_plugin(home)
    if module._transport_mode() != "legacy" or module._capture_mode() != "metadata":
        raise SystemExit(
            f"refusing: transport={module._transport_mode()} capture={module._capture_mode()}"
            " (F4 requires legacy transport + metadata-only capture)")
    ctx = Ctx()
    module.register(ctx)
    hooks = ctx.hooks

    def emit(session_id: str, window_id: str, seq: int) -> int:
        return emit_turn(hooks, session_id, seq, window_id)

    def drain() -> None:
        module.flush_legacy_delivery(45)

    return emit, drain


def mock_emitter(home: str):
    """Local, offline emitter: writes allowlisted envelopes into a real spool."""
    module = _module_from_path("fleet_spool", os.path.join(os.path.dirname(HERE), "fleet_spool.py"))
    spool = module.FleetSpool(os.path.join(home, "plugins", "langfuse-fleet", "spool.db"))

    def emit(session_id: str, window_id: str, seq: int) -> int:
        event = {
            "id": f"mock-{window_id}-{seq:04d}",
            "type": "trace-create",
            "body": {"sessionId": session_id, "metadata": {"source": "f4-mock"}},
        }
        return 1 if spool.enqueue(event, len(json.dumps(event))) else 0

    return emit, spool


def producer_alive(home: str) -> int | None:
    path = os.path.join(home, LOCK)
    if not os.path.exists(path):
        return None
    try:
        pid = int(json.load(open(path, encoding="utf-8"))["pid"])
    except (ValueError, KeyError, OSError, TypeError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def write_heartbeat(home: str, payload: dict) -> None:
    tmp = os.path.join(home, HEARTBEAT + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, os.path.join(home, HEARTBEAT))


def window_closed(home: str, manifest: dict) -> bool:
    path = os.path.join(home, "f4_observation_state.json")
    if not os.path.exists(path):
        return True  # never emit outside an explicitly opened window
    state = json.load(open(path, encoding="utf-8"))
    return (time.time() - state["start_at_epoch"]) / 3600 >= manifest["window"]["hours"]


def run(home: str, manifest: dict, emit, *, drain=None, cycles: int | None = None,
        interval_s: float | None = None, once: bool = False,
        clock=time.time, sleep=time.sleep) -> dict:
    """Bounded emit loop. ``emit(session_id, window_id, seq) -> events``."""
    producer = manifest["producer"]
    max_cycles = 1 if once else int(cycles if cycles is not None else producer["max_cycles"])
    wait = 0 if once else float(interval_s if interval_s is not None else producer["interval_s"])
    state = json.load(open(os.path.join(home, "f4_observation_state.json"), encoding="utf-8"))
    window_id, session_id = state["window_id"], synthetic_session(manifest)

    stop = {"requested": False}

    def _stop(_signum, _frame):
        stop["requested"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    heartbeat = {"pid": os.getpid(), "window_id": window_id, "session_id": session_id,
                 "started_utc": utc(), "last_cycle_utc": None, "cycles": 0,
                 "events_emitted": 0, "stopped": False}
    write_heartbeat(home, heartbeat)
    lock_path = os.path.join(home, LOCK)
    with open(lock_path, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_utc": heartbeat["started_utc"]}, handle)

    try:
        while heartbeat["cycles"] < max_cycles and not stop["requested"]:
            if heartbeat["events_emitted"] >= producer["max_total_events"]:
                heartbeat["limit_reason"] = "max_total_events"
                break
            if window_closed(home, manifest):
                heartbeat["limit_reason"] = "window_closed"
                break
            seq = heartbeat["cycles"] + 1
            heartbeat["events_emitted"] += int(emit(session_id, window_id, seq) or 0)
            heartbeat["cycles"] = seq
            heartbeat["last_cycle_utc"] = utc()
            write_heartbeat(home, heartbeat)
            if heartbeat["cycles"] >= max_cycles or stop["requested"]:
                break
            deadline = clock() + wait
            while clock() < deadline and not stop["requested"]:
                sleep(min(5.0, max(0.05, deadline - clock())))
    finally:
        if drain is not None:
            try:
                drain()
            except Exception as exc:  # noqa: BLE001 - a failed drain must be visible, not fatal
                heartbeat["drain_error"] = f"{type(exc).__name__}: {exc}"
        heartbeat["stopped"] = True
        heartbeat["stopped_utc"] = utc()
        write_heartbeat(home, heartbeat)
        try:
            os.remove(lock_path)
        except OSError:
            pass
    return heartbeat


def main(argv: list) -> int:
    home, manifest_path = DEFAULT_HOME, os.path.join(HERE, "f4_manifest.json")
    cycles, interval = None, None
    once = mock = status = stop = False
    args = list(argv[1:])
    for flag in ("--once", "--mock", "--status", "--stop"):
        if flag in args:
            args.remove(flag)
            once = once or flag == "--once"
            mock = mock or flag == "--mock"
            status = status or flag == "--status"
            stop = stop or flag == "--stop"
    if "--home" in args:
        home = args[args.index("--home") + 1]
    if "--manifest" in args:
        manifest_path = args[args.index("--manifest") + 1]
    if "--cycles" in args:
        cycles = int(args[args.index("--cycles") + 1])
    if "--interval" in args:
        interval = float(args[args.index("--interval") + 1])

    if status:
        path = os.path.join(home, HEARTBEAT)
        print(json.dumps(json.load(open(path, encoding="utf-8"))
                         if os.path.exists(path) else {"present": False}, sort_keys=True))
        return 0
    if stop:
        pid = producer_alive(home)
        if not pid:
            print(json.dumps({"error": "no live producer"}))
            return 3
        os.kill(pid, signal.SIGTERM)
        print(json.dumps({"stopped_pid": pid}))
        return 0

    manifest = load_manifest(manifest_path)
    assert_isolated(home)
    owner = producer_alive(home)
    if owner:
        print(json.dumps({"error": f"producer already running (pid {owner})"}))
        return 3
    if window_closed(home, manifest):
        print(json.dumps({"error": "window not initialized or already closed"}))
        return 3
    spool = None
    if mock:
        emit, spool = mock_emitter(home)
        drain = None
    else:
        bootstrap_env(home)
        emit, drain = plugin_emitter(home)
    try:
        heartbeat = run(home, manifest, emit, drain=drain, cycles=cycles,
                        interval_s=interval, once=once)
    finally:
        if spool is not None:
            spool.close()
    print(json.dumps(heartbeat, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
