#!/usr/bin/env bash
# Provision the pinned Langfuse SDK 4 store for the langfuse-fleet plugin (F5).
#
# Why a dedicated store: Hermes does not install plugin dependencies during
# discovery, and PM only builds plugin *members* into the shared venv — where
# langfuse 2.x serves the bundled observability/langfuse plugin. This plugin
# pins langfuse 4.x, so plugin.yaml declares `python_runtime: external` and the
# SDK lives in a store this script owns, tagged by interpreter (pyXY) because the
# dependency tree carries compiled wheels.
#
# Idempotent: re-running refreshes the same target. Never edits the Hermes venv,
# never touches the shared lazy store (/opt/data/lazy-packages), never downgrades
# pydantic or httpx (the venv always wins: the plugin *appends* the store to
# sys.path).
set -euo pipefail

PYTHON="${HERMES_PYTHON:-/opt/hermes-agent/.venv/bin/python}"
STORE_BASE="${HERMES_LANGFUSE_STORE_BASE:-/opt/data/lazy-packages-langfuse4}"
SDK_VERSION="${LANGFUSE_SDK_VERSION:-4.15.2}"

if [[ ! -x "$PYTHON" ]]; then
  echo "provision_sdk_store: interpreter not executable: $PYTHON" >&2
  exit 1
fi

TAG="$("$PYTHON" -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")')"
TARGET="$STORE_BASE/$TAG"

echo "provision_sdk_store: interpreter=$PYTHON tag=$TAG target=$TARGET sdk=langfuse==$SDK_VERSION"
mkdir -p "$TARGET"

uv pip install --python "$PYTHON" --target "$TARGET" --upgrade "langfuse==$SDK_VERSION"

# Verify in a brand-new process: the tag exists, the 4.x low-level client imports, and the
# venv still wins for pydantic/httpx (the plugin appends the store, it never inserts it).
"$PYTHON" - "$TARGET" <<'PY'
import sys, json
target = sys.argv[1]
sys.path.append(target)
from langfuse.api.client import LangfuseAPI  # noqa: F401
import langfuse, pydantic, httpx
print(json.dumps({
    "langfuse": langfuse.__version__,
    "langfuse_file": langfuse.__file__,
    "pydantic_file": pydantic.__file__,
    "httpx_file": httpx.__file__,
}, indent=2))
if not langfuse.__version__.startswith("4."):
    raise SystemExit(f"unexpected langfuse version: {langfuse.__version__}")
PY

echo "provision_sdk_store: OK — restart Hermes (the plugin resolves the store at import time)"
