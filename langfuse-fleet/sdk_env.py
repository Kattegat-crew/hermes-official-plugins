"""Locate the pinned Langfuse SDK 4 store this sidecar plugin owns (F5).

Why the plugin cannot rely on the runtime it happens to be loaded by:

* Hermes never installs a plugin's Python dependencies while discovering it
  (``hermes_cli/plugins_loader.py``: "Discovery does not install dependencies";
  the manifest field is surfaced only).
* PM only builds *members* into the shared venv, and this plugin needs the
  langfuse 4.x API (``langfuse.api.client.LangfuseAPI``) while the bundled
  ``observability/langfuse`` plugin — enabled in most profiles — runs the 2.x
  API. Putting 4.x in the venv would change that plugin's SDK everywhere, so
  ``plugin.yaml`` declares ``python_runtime: external`` and the plugin owns its
  store instead.
* The shared lazy store (``/opt/data/lazy-packages``) carries langfuse 2.60.10,
  which has no ``LangfuseAPI`` at all; it is never added here.

The store is per interpreter (``pyXY``) because it ships compiled wheels, and it
is *appended* to ``sys.path``: the Hermes venv keeps precedence for pydantic,
httpx and everything else it already provides.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: Explicit operator override; may list several stores separated by ``os.pathsep``.
SDK_STORE_ENV = "HERMES_LANGFUSE_SDK_PATH"
#: Base directory of the per-interpreter stores (``<base>/pyXY``).
SDK_STORE_BASE_ENV = "HERMES_LANGFUSE_STORE_BASE"
DEFAULT_SDK_STORE_BASE = "/opt/data/lazy-packages-langfuse4"
#: Pinned SDK; keep in sync with ``pyproject.toml`` and ``scripts/provision_sdk_store.sh``.
SDK_VERSION = "4.15.2"

_APPLIED: list[str] = []


def interpreter_tag() -> str:
    """``py314`` for CPython 3.14 — compiled wheels are not portable across ABIs."""
    return f"py{sys.version_info.major}{sys.version_info.minor}"


def sdk_store_candidates() -> list[str]:
    """Stores to consider, operator override first, then this interpreter's store.

    The shared 2.x lazy store is deliberately absent: adding it would shadow the
    pinned 4.x SDK with an incompatible API surface.
    """
    override = os.environ.get(SDK_STORE_ENV, "").strip()
    candidates = [part.strip() for part in override.split(os.pathsep) if part.strip()] if override else []
    base = os.environ.get(SDK_STORE_BASE_ENV, "").strip() or DEFAULT_SDK_STORE_BASE
    candidates.append(str(Path(base) / interpreter_tag()))
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def ensure_sdk_paths() -> tuple[str, ...]:
    """Append every existing candidate store to ``sys.path``.

    Idempotent and additive: never inserts, so the venv's own pydantic/httpx keep
    precedence. Returns every store this process has applied.
    """
    for candidate in sdk_store_candidates():
        if candidate in sys.path or not Path(candidate).is_dir():
            continue
        sys.path.append(candidate)
        if candidate not in _APPLIED:
            _APPLIED.append(candidate)
    return tuple(_APPLIED)
