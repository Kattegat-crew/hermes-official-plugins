"""F3 readback: verify canary traces in Langfuse PROD via read-only REST API.

Checks per trace: HTTP 200 (no 404), observation hierarchy (1 root span +
1 generation + 1 tool span), usage on the generation, and the absence of
FAKE/synthetic CONTENT (capture=metadata must never carry prompt text).
Prints one JSON verdict. Credentials: canary .env (values never printed).
"""
import base64
import json
import os
import sys
import urllib.request

CANARY = "/opt/hermes-agent-data/fleet-canary"
BASE = "http://100.73.30.29:3010"
EXPECTED_TRACES = [
    "25ede40106cd4cb2844fedd6245ec2f8",  # canary phase (fresh spool)
    "572f09337ad94409a52926f2721aed32",  # crash-outage -> recover phase
]
EXPECTED_META_KEYS = {"api_mode", "api_request_id", "model", "platform", "profile", "provider", "source", "turn_id"}

env = {}
with open(os.path.join(CANARY, ".env")) as f:
    for line in f:
        k, _, v = line.strip().partition("=")
        if v:
            env[k] = v
BASE = env.get("HERMES_LANGFUSE_BASE_URL", BASE)
auth = base64.b64encode(
    f"{env['HERMES_LANGFUSE_PUBLIC_KEY']}:{env['HERMES_LANGFUSE_SECRET_KEY']}".encode()).decode()


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


verdict = {"base_url": BASE, "traces": [], "ok": True}
for tid in EXPECTED_TRACES:
    entry = {"trace_id": tid}
    try:
        status, trace = get(f"/api/public/traces/{tid}")
    except urllib.error.HTTPError as exc:
        entry["http_status"] = exc.code
        entry["found"] = False
        verdict["ok"] = False
        verdict["traces"].append(entry)
        continue
    entry["http_status"] = status
    entry["found"] = True
    entry["session_id"] = trace.get("sessionId")
    obs = trace.get("observations") or []
    by_type = {}
    for o in obs:
        by_type.setdefault(o.get("type"), []).append(o.get("name"))
    entry["observations_by_type"] = by_type
    entry["hierarchy_ok"] = (
        len(by_type.get("SPAN", [])) == 2  # root "Hermes turn" + tool "Hermes tool"
        and len(by_type.get("GENERATION", [])) == 1
        and "Hermes turn" in by_type.get("SPAN", [])
        and "Hermes tool" in by_type.get("SPAN", [])
    )
    gen = next((o for o in obs if o.get("type") == "GENERATION"), None)
    if gen:
        usage = gen.get("usageDetails") or gen.get("usage") or {}
        entry["usage"] = usage
        entry["usage_ok"] = usage.get("input") == 12 and usage.get("output") == 34
        entry["model"] = gen.get("model")
        meta = gen.get("metadata") or {}
        entry["generation_metadata_keys"] = sorted(meta.keys())
    trace_meta = trace.get("metadata") or {}
    entry["trace_metadata_keys"] = sorted(trace_meta.keys()) if isinstance(trace_meta, dict) else "dict-expected"
    entry["trace_metadata_allowlist_ok"] = set(trace_meta.keys()) == EXPECTED_META_KEYS if isinstance(trace_meta, dict) else False
    # privacy: no synthetic prompt/tool content anywhere in the trace payload
    raw = json.dumps(trace)
    entry["content_leak_found"] = [tok for tok in ("FAKE", "canary prompt", "synthetic tool result") if tok in raw]
    entry["privacy_ok"] = not entry["content_leak_found"]
    entry["ok"] = entry["found"] and entry["hierarchy_ok"] and entry.get("usage_ok", False) and entry["privacy_ok"] and entry["trace_metadata_allowlist_ok"]
    verdict["ok"] = verdict["ok"] and entry["ok"]
    verdict["traces"].append(entry)

# crash-recovered trace re-sent across 3 recover cycles must appear exactly once
# (idempotent envelope ids). Calibration traces from driver-fix runs are listed
# but must not duplicate the target ids.
status, listing = get("/api/public/traces?sessionId=fleet-f3-canary&limit=50")
ids = [t.get("id") for t in (listing.get("data") or listing) if isinstance(t, dict)]
verdict["server_trace_ids_for_session"] = sorted(ids)
verdict["no_duplicate_traces"] = (
    len(ids) == len(set(ids))
    and all(ids.count(t) == 1 for t in EXPECTED_TRACES)
    and set(EXPECTED_TRACES) <= set(ids)
)
verdict["ok"] = verdict["ok"] and verdict["no_duplicate_traces"]
print("F3_READBACK: " + json.dumps(verdict))
sys.exit(0 if verdict["ok"] else 1)
