<p align="center"><img src="../assets/langfuse.jpg" alt="Langfuse: official Hermes Agent plugin" width="100%"></p>

# Langfuse for Hermes Agent — fleet variant

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly bundled with Hermes core as `observability/langfuse`.
>
> **Fleet variant.** This directory is an independent, spool-backed build of the official `langfuse`
> plugin: same hooks, same wire format, but delivery goes through a durable per-profile SQLite spool
> (see [Durable fleet spool](#durable-fleet-spool)). The original `langfuse/` plugin is untouched;
> install one or the other for a given profile, not both.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that traces Hermes turns,
LLM calls and tool invocations to [Langfuse](https://langfuse.com), the open-source LLM observability
platform. One span per turn, one generation per API call, one observation per tool call and subagent.
Usage totals, per-type token counts and cost estimates come from Hermes' own pricing, so Langfuse sees
the same breakdown (input / output / cache read / cache write / reasoning tokens) as `hermes logs`.

The plugin fails open: no SDK, no credentials or a transient Langfuse error all turn into a no-op in
the hook (a missing SDK or missing/placeholder keys log one warning). The agent loop is never affected.

**If you already used Langfuse with Hermes, you don't need to do anything.** `hermes update` (or the
first start after updating) installs and enables this plugin for every profile that had
`observability/langfuse` enabled. Your keys and every `HERMES_LANGFUSE_*` setting stay exactly as they
were.

## Install

From the plugin catalog (this variant is published under its own name):

```bash
hermes plugins install langfuse-fleet
```

Install asks for your project keys and saves them to the profile's `.env`. For a self-hosted Langfuse,
also set the server URL:

```bash
HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-...
HERMES_LANGFUSE_SECRET_KEY=sk-lf-...
HERMES_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Keys pasted straight from a template (`pk-lf-...`, `your-key`, ...) are refused with a warning. The
`pk-lf-` / `sk-lf-` prefix is enforced only for Langfuse Cloud (`*.langfuse.com`); on any other
host a key without it logs a one-time warning and is still used, since a custom
Langfuse-compatible endpoint may issue its own keys and authenticates them itself.

Restart Hermes and the next turn ships a trace. Plugins are installed per profile: use
`hermes -p <profile> plugins install langfuse-fleet` for another profile.

### Python dependency (required step)

This plugin needs the `langfuse` **4.x** SDK, and neither plugin install nor turn execution
provisions it: discovery only reports missing manifest dependencies, and `hermes pm repair` only
builds plugins that are *members* of the shared venv. `plugin.yaml` therefore declares
`python_runtime: external` (this is a sidecar) — the bundled `observability/langfuse` plugin runs
the 2.x API from the shared lazy store, so installing 4.x into the venv would change its SDK under
every profile.

Provision the plugin's own pinned, per-interpreter store once per host (again after a Python
upgrade):

```bash
# from the installed plugin directory (<plugin-dir>/scripts/provision_sdk_store.sh)
bash scripts/provision_sdk_store.sh
# non-default interpreter or location:
HERMES_PYTHON=/path/to/python HERMES_LANGFUSE_STORE_BASE=/opt/data/lazy-packages-langfuse4 \
  bash scripts/provision_sdk_store.sh
```

The store lands in `<base>/py<major><minor>` and is appended to `sys.path` at import;
`HERMES_LANGFUSE_SDK_PATH` (path-separated) overrides the resolution for an alternative location.

It is a full `uv pip install --target` tree (~31 MB), so it carries its own `pydantic`/`httpx` next
to `langfuse`. Only the *order* is contractual: appended after `site-packages`, so wherever the
interpreter already provides `pydantic`/`httpx` (the Hermes venv does) that copy keeps winning and
the store only supplies the missing SDK. On an interpreter that provides neither, the store's pinned
copies are what make the SDK importable.

If the SDK cannot be imported — store not provisioned, or a 2.x SDK resolved from elsewhere — the
plugin still loads (fail-open) but says so exactly once per process, for **either** transport
(`otlp` is the default and `legacy` is the opt-in), with the real import error, the stores it
searched, the interpreter and the provision command; `transport_health()` returns
`{state: "degraded", reason, python, stores}` so an operator can check state without digging
through logs.

## Verify

```bash
hermes plugins list                 # langfuse should show "enabled"
hermes chat -q "hello"              # then check Langfuse for a "Hermes turn" trace
```

Generation observations include the Hermes system prompt when the provider uses a separate `system`
param (Anthropic Messages API). Open an **LLM call** child span to inspect `role: system` (truncated
via `HERMES_LANGFUSE_MAX_CHARS`).

Root traces carry `platform:<name>` and `trigger:cron|interactive` tags (cron runs also
`cron-job:<job-id>`), and their metadata names the Hermes `profile` the turn ran for, so scheduled
runs and profiles sharing one project can be filtered apart. Kanban worker turns also carry
`kanban_task_id` / `kanban_run_id`, so a trace joins back to its board card.

## How it works

| Hook | Behaviour |
|---|---|
| `pre_api_request` / `pre_llm_call` | Open (or reuse) a per-turn root span "Hermes turn". Start a `generation` child observation for this API call with serialized recent messages as input. |
| `post_api_request` / `post_llm_call` | Close the generation, attach `usage_details`, `cost_details`, `finish_reason`, assistant output + tool calls. If no tool calls and non-empty content, close the turn. |
| `api_request_error` | Close the failed generation with `level=ERROR`, status code, retry counters and a capture-mode-scrubbed error message. Non-retryable failures also finish the turn. |
| `pre_tool_call` | Start a `tool` child observation with sanitized `args`. |
| `post_tool_call` | Close the tool observation with sanitized `result`. `read_file` payloads get summarized (head + tail + omitted-line count) so a huge file read stays under `HERMES_LANGFUSE_MAX_CHARS`. |
| `subagent_start` / `subagent_stop` | One observation per delegated subagent under the parent turn. |
| `on_session_end` / `on_session_finalize` | Close any still-open traces for the session, so interrupted or tool-only turns don't dangle. Only process exit flushes. |

Ended observations are serialized into a bounded per-profile SQLite spool; the hook only pays one
local insert and never touches the network. A process-wide daemon claims due rows, delivers them over
the SDK4 low-level REST client and only marks an event `done` after the transport accepts it, so a
slow or unavailable backend cannot hold up a finished turn (or a cron job past its watchdog) and
unverified events survive a restart. Delivery is at-least-once with stable event ids; see
[Durable fleet spool](#durable-fleet-spool) for limits and counters.

Session grouping keys off the Hermes session ID (or task ID for subagents) via
`langfuse.propagate_attributes`, so everything in one `hermes chat` session lives under one Langfuse
session. Under a multiplexed gateway each profile traces with its own keys.
Each turn is its own trace, so `HERMES_LANGFUSE_SAMPLE_RATE` samples individual turns, not whole
conversations.

## Durable fleet spool

The spool is the only durability boundary, so the hook path is a single local `INSERT`.

- **Location:** `<hermes home>/plugins/langfuse-fleet/spool.db`, one spool per profile — a
  multiplexed gateway keeps each profile's events in its own file and its own counters.
- **Delivery:** a process-wide daemon thread claims rows whose `next_attempt_at` is due, delivers
  them over the SDK4 low-level REST client, and settles each event individually. An event becomes
  `done` only on confirmed acceptance (or a non-retryable rejection, counted as `rejected`);
  retryable statuses (`429`, `500`, `502`, `503`, `504`) and transport errors are requeued with
  exponential backoff and stay pending.
- **Recovery:** rows leased by a process that died are recovered by the next boot, so an abrupt kill
  loses nothing that was ever spooled. Delivery is at-least-once with stable event ids — Langfuse
  de-duplicates them.
- **Policy limits** (module globals, overridable by tests):

| Limit | Value |
|---|---|
| `MAX_ROWS` | 4096 pending rows |
| `MAX_PENDING_BYTES` | 8 MiB pending payload |
| `MAX_BATCH_BYTES` | 512 KiB per delivery batch |
| `MAX_ATTEMPTS` | 5 per event before it is lost and counted |
| `RETRY_BASE_S` / `RETRY_CAP_S` | 5 s base, 900 s cap |
| `EXPIRY_AGE_S` | 24 h before an undeliverable event expires |

When the spool is full or an event is oversized, the event is dropped at the producer (a counter
records it, never a log line with content) — telemetry never blocks a turn. Counters live in the
`counters` table: `delivered`, `lost_overflow`, `lost_oversize`, `lost_attempts`, `expired`,
`recovered`, `batches_acked`, `rejected`.

## Optional tuning

```bash
HERMES_LANGFUSE_ENV=production       # environment tag
HERMES_LANGFUSE_RELEASE=v1.0.0       # release tag
HERMES_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
HERMES_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
HERMES_LANGFUSE_MAX_DEPTH=4          # max payload depth (default: 4)
HERMES_LANGFUSE_CAPTURE=sanitized    # content capture mode (see below)
HERMES_LANGFUSE_DEBUG=true           # verbose plugin logging to agent.log
OTEL_SERVICE_NAME=my-agent           # OTel service.name on traces (default: hermes-agent)
```

The standard SDK names (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`) are
accepted too; the `HERMES_`-prefixed ones win when both are set.

`HERMES_LANGFUSE_MAX_DEPTH` controls nested payload capture in both `sanitized` and `full` modes,
including tool arguments and JSON tool results. The root is depth 0; each dictionary value or array
element adds one level. Values beyond the limit become `<max-depth>`, including scalars. For deeper MCP
responses, set it to a higher non-negative integer (for example, `10`). Unset or blank values default
to `4`; invalid or negative values log a warning and fall back to `4`. `0` keeps only the root level.
Increasing the depth exports more content and may produce larger traces; secret redaction,
string-length limits and the 50-item collection limit remain unchanged. `metadata` mode still omits
content.

## Capture modes

`HERMES_LANGFUSE_CAPTURE` controls how much *content* (prompts, responses, tool arguments/results) is
exported. Structural metadata (IDs, roles, tool names, token usage, cost, timing) is captured in every
mode.

| mode | behavior |
|------|----------|
| `metadata` | No content. Each content field is replaced by a shape/size stub (`{"omitted": true, "type": "text", "chars": N}`). |
| `sanitized` | **(default)** Content is exported after secret-pattern redaction (API keys, tokens, JWTs, private keys, `password=`-style assignments) and truncation. Redaction runs *before* truncation. |
| `full` | Raw content, truncated only. Explicit opt-in: traces will contain whatever passed through the conversation, including injected memory and file contents. |

The active mode is recorded on every trace as `metadata.capture_mode`. `sanitized` is pattern-based
defense in depth, not a DLP guarantee. For personal sessions or shared Langfuse projects, prefer
`metadata`.

## Langfuse v2 REST compatibility (opt-in)

The default transport remains the Langfuse SDK's current OTLP path. For an existing Langfuse v2
server, explicitly set `HERMES_LANGFUSE_TRANSPORT=legacy`; unset it or set it to `otlp` to use the
default path again. An unrecognized value disables tracing rather than guessing from the server URL.
Restart Hermes after changing this setting.

The legacy transport uses the installed SDK's low-level REST client and `/api/public/ingestion`;
it does not initialize the SDK's high-level OTLP resource manager. This transport is always
metadata-only, regardless of `HERMES_LANGFUSE_CAPTURE`: it omits prompts, completions, tool
arguments/results, arbitrary metadata, free-form names, status messages, and cost details. It keeps
bounded validated profile/platform/provider/model identifiers, trace/session/parent correlation,
event timing and error levels, and only non-negative integer input/output/total token counts when
present. Langfuse v2 does not receive newer `usageDetails` or `costDetails` fields. The event union
and body-field allowlist are based on the [v2.95.11 ingestion schema](https://github.com/langfuse/langfuse/blob/v2.95.11/packages/shared/src/server/ingestion/types.ts).

Delivery uses one process-wide daemon worker (one in-flight batch at a time), with a queue capped at
128 events / 512 KiB; a full queue drops new events with a count-only warning rather than blocking a
Hermes turn. A batch is at most 128 events / 512 KiB. The low-level HTTP client has a 1.5-second
inactivity timeout and redirects disabled; each event batch has at most two adapter attempts, while
SDK-internal retries/backoff are disabled. Partial-success responses retry only identified retryable
events with their original IDs. Each flush, including an exit-time flush, waits at most 50 ms;
multiple cached-client flushes can accumulate, so this is not a process-wide shutdown deadline.
Queued events can be lost on a slow backend or abrupt process exit. The HTTP inactivity timeout
is not a total request deadline: a slow-trickling body may occupy the single daemon worker, but cannot
hold producer locks or spawn additional workers; the bounded queue then drops new events. This is a
local transport option, not evidence that a live Langfuse v2 server accepted or displayed traces.
Verify against the intended deployment with a separately authorized no-sensitive-content canary
before relying on it.

## Disable or remove

```bash
hermes plugins disable langfuse-fleet     # keep it installed, stop tracing
hermes plugins remove langfuse-fleet      # uninstall
```

The Langfuse client is cached after the first hook call. If credentials or the SDK are missing, that
decision is cached too, so later hooks return immediately.

## Development

```bash
git clone https://github.com/NousResearch/hermes-agent
git clone https://github.com/NousResearch/hermes-official-plugins
cd hermes-official-plugins/langfuse
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

On a Hermes core that still bundles `observability/langfuse`, an installed copy of this plugin stays
inactive (the bundled copy keeps tracing) so traces are never sent twice.
