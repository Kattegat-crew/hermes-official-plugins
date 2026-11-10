"""Contract tests for the plugin-declared Langfuse SDK 4 store (F5).

Hermes never installs a plugin's Python dependencies while discovering it
(``hermes_cli/plugins_loader.py::_warn_python_dependencies``: "Discovery does not
install dependencies"), and PM only builds *members* into the shared venv. This
plugin must stay a sidecar (``python_runtime: external``) because it needs
``langfuse`` 4.x while the bundled ``observability/langfuse`` plugin, enabled in
most profiles, runs the 2.x API from the shared lazy store: putting 4.x in the
venv would change that plugin's SDK under every profile.

So the plugin owns a pinned, per-interpreter store, appends it to ``sys.path``
(never inserts: the venv keeps precedence for pydantic/httpx) and reports a
visible degraded state when the SDK is not importable.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_NAME = "langfuse_fleet_plugin"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _fresh_plugin():
    sys.modules.pop(PLUGIN_NAME, None)
    return importlib.import_module(PLUGIN_NAME)


def _legacy_credentials(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_TRANSPORT", "legacy")
    monkeypatch.setattr(plugin, "_secret", lambda name: {
        "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-fixture",
        "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-fixture",
    }.get(name, ""))


def _hermes_core_paths() -> list[str]:
    """Directories of the *running* Hermes core, without any store/overlay entry.

    A fresh-process probe of ``_get_langfuse()`` needs ``hermes_constants``; it must not inherit a
    hand-built ``PYTHONPATH`` (that is the overlay F5 removes).
    """
    return [entry for entry in sys.path
            if entry and (Path(entry) / "hermes_constants.py").exists()]


@pytest.fixture(autouse=True)
def _restore_sys_path():
    """Reloading the plugin appends store paths; never leak them into other tests."""
    before = list(sys.path)
    yield
    sys.path[:] = before


class TestStoreResolution:
    def test_operator_override_comes_first_and_shared_v2_store_is_never_added(self, tmp_path, monkeypatch):
        override = tmp_path / "sdk-store"
        override.mkdir()
        monkeypatch.setenv("HERMES_LANGFUSE_SDK_PATH", str(override))
        plugin = _fresh_plugin()
        candidates = plugin.sdk_store_candidates()
        assert candidates[0] == str(override)
        assert "/opt/data/lazy-packages" not in candidates, (
            "the shared lazy store carries langfuse 2.x (no LangfuseAPI); adding it would silently "
            "shadow the pinned 4.x SDK"
        )
        assert candidates[-1].startswith(plugin.DEFAULT_SDK_STORE_BASE)
        assert candidates[-1].endswith(f"py{sys.version_info.major}{sys.version_info.minor}")

    def test_pathsep_override_lists_several_stores(self, tmp_path, monkeypatch):
        first, second = tmp_path / "a-store", tmp_path / "b-store"
        first.mkdir()
        second.mkdir()
        monkeypatch.setenv("HERMES_LANGFUSE_SDK_PATH", f"{first}{':' if sys.platform != 'win32' else ';'}{second}")
        plugin = _fresh_plugin()
        assert plugin.sdk_store_candidates()[:2] == [str(first), str(second)]

    def test_bootstrap_appends_after_the_venv_and_is_idempotent(self, tmp_path, monkeypatch):
        store = tmp_path / "sdk-store"
        store.mkdir()
        monkeypatch.setenv("HERMES_LANGFUSE_SDK_PATH", str(store))
        sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != store.resolve()]
        plugin = _fresh_plugin()

        assert str(store) in sys.path
        site_packages = next(
            (index for index, entry in enumerate(sys.path) if entry.endswith("site-packages")), None)
        if site_packages is not None:
            assert sys.path.index(str(store)) > site_packages, (
                "the store must never take precedence over the Hermes venv's pydantic/httpx"
            )
        before = list(sys.path)
        plugin.ensure_sdk_paths()
        assert sys.path == before, "bootstrap is not idempotent"


class TestDegradedVisibility:
    def test_missing_sdk_degrades_with_one_actionable_warning(self, monkeypatch, caplog):
        plugin = _fresh_plugin()
        _legacy_credentials(plugin, monkeypatch)

        def missing_sdk(**kwargs):
            raise ModuleNotFoundError("No module named 'langfuse'")

        monkeypatch.setattr(plugin, "create_langfuse_api_client", missing_sdk)
        plugin._LANGFUSE_CLIENT = None
        plugin._LANGFUSE_CLIENT_BY_HOME.clear()

        with caplog.at_level(logging.WARNING, logger=PLUGIN_NAME):
            assert plugin._get_langfuse() is None
            assert plugin._get_langfuse() is None

        warnings = [record.getMessage() for record in caplog.records
                    if record.levelname == "WARNING" and record.name == PLUGIN_NAME]
        matching = [message for message in warnings if "provision_sdk_store.sh" in message]
        assert len(matching) == 1, f"expected exactly one actionable warning, got {warnings}"
        message = matching[0]
        assert "No module named 'langfuse'" in message, "the warning must carry the real import error"
        assert plugin.SDK_STORE_ENV in message, "the warning must name the operator override"
        assert sys.executable in message, "the warning must name the interpreter that is missing the SDK"

        health = plugin.transport_health()
        assert health["state"] == "degraded"
        assert "ModuleNotFoundError" in health["reason"]
        assert health["python"] == sys.executable
        assert isinstance(health["stores"], list) and health["stores"]

    def test_health_reports_ok_after_a_successful_build(self, monkeypatch):
        plugin = _fresh_plugin()
        _legacy_credentials(plugin, monkeypatch)
        sentinel = object()
        monkeypatch.setattr(plugin, "create_langfuse_api_client", lambda **kwargs: object())
        monkeypatch.setattr(plugin, "LegacyIngestionClient", lambda api: sentinel)
        plugin._LANGFUSE_CLIENT = None
        plugin._LANGFUSE_CLIENT_BY_HOME.clear()

        assert plugin._get_langfuse() is sentinel
        health = plugin.transport_health()
        assert health["state"] == "ok"
        assert health["reason"] == ""

    def test_non_import_failures_keep_the_generic_warning(self, monkeypatch, caplog):
        plugin = _fresh_plugin()
        _legacy_credentials(plugin, monkeypatch)

        def broken(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(plugin, "create_langfuse_api_client", broken)
        plugin._LANGFUSE_CLIENT = None
        plugin._LANGFUSE_CLIENT_BY_HOME.clear()

        with caplog.at_level(logging.WARNING, logger=PLUGIN_NAME):
            assert plugin._get_langfuse() is None
        assert plugin.transport_health()["state"] == "degraded"
        assert "RuntimeError" in plugin.transport_health()["reason"]
        assert any("Could not initialize Langfuse legacy REST client" in record.getMessage()
                   for record in caplog.records)


class TestOtlpDegradedVisibility:
    """The default transport (``otlp``) must degrade as loudly as the opt-in ``legacy`` one.

    AC3 is not transport-scoped. With the SDK missing, ``otlp`` used to emit a generic line whose
    remedy (``hermes plugins install langfuse --force``) installs a *different* plugin and PM never
    builds a ``python_runtime: external`` sidecar — leaving the operator exactly in "the turn
    succeeded, zero traces".
    """

    @pytest.mark.parametrize("error", [
        ModuleNotFoundError("No module named 'langfuse'"),
        ImportError("cannot import name 'propagate_attributes' from 'langfuse' (2.60.10)"),
    ])
    def test_default_otlp_transport_reports_one_actionable_warning(self, error, monkeypatch, caplog):
        plugin = _fresh_plugin()
        monkeypatch.delenv("HERMES_LANGFUSE_TRANSPORT", raising=False)
        monkeypatch.setattr(plugin, "Langfuse", None)
        plugin._SDK_IMPORT_ERROR = error
        plugin._LANGFUSE_CLIENT = None
        plugin._LANGFUSE_CLIENT_BY_HOME.clear()

        assert plugin._transport_mode() == "otlp", "otlp is the default transport"
        with caplog.at_level(logging.WARNING, logger=PLUGIN_NAME):
            assert plugin._get_langfuse() is None
            assert plugin._get_langfuse() is None

        warnings = [record.getMessage() for record in caplog.records
                    if record.levelname == "WARNING" and record.name == PLUGIN_NAME]
        matching = [message for message in warnings if "provision_sdk_store.sh" in message]
        assert len(matching) == 1, f"expected exactly one actionable warning, got {warnings}"
        message = matching[0]
        assert str(error) in message, "the warning must carry the real import error"
        assert plugin.SDK_STORE_ENV in message, "the warning must name the operator override"
        assert sys.executable in message, "the warning must name the interpreter missing the SDK"
        assert "install langfuse --force" not in message, (
            "that command installs another plugin and PM never builds an `external` sidecar"
        )

        health = plugin.transport_health()
        assert health["state"] == "degraded"
        assert type(error).__name__ in health["reason"], "reason must not be generic"
        assert health["python"] == sys.executable
        assert isinstance(health["stores"], list) and health["stores"]


_OTLP_PROBE = """
import importlib.util, json, logging, sys
from pathlib import Path

root = Path({plugin_root!r})
spec = importlib.util.spec_from_file_location(
    "langfuse_fleet_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["langfuse_fleet_plugin"] = plugin
spec.loader.exec_module(plugin)

messages = []
class Capture(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())

logger = logging.getLogger("langfuse_fleet_plugin")
logger.addHandler(Capture())
logger.setLevel(logging.WARNING)

first = plugin._get_langfuse()
second = plugin._get_langfuse()
print(json.dumps({{
    "transport": plugin._transport_mode(),
    "sdk_present": plugin.Langfuse is not None,
    "first": first is None,
    "second": second is None,
    "warnings": messages,
    "health": plugin.transport_health(),
}}))
"""


class TestOtlpFreshProcessDegradedPath:
    """Same degraded path in a brand-new interpreter: no overlay, no provisioned store."""

    def test_missing_sdk_is_reported_by_the_default_transport_in_a_new_process(self, tmp_path):
        probe = tmp_path / "probe_otlp.py"
        probe.write_text(_OTLP_PROBE.format(plugin_root=str(PLUGIN_ROOT)), encoding="utf-8")
        empty_base = tmp_path / "no-store"
        empty_base.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("HERMES_LANGFUSE_") and key != "PYTHONPATH"}
        env["PYTHONPATH"] = os.pathsep.join(_hermes_core_paths())
        env["HERMES_LANGFUSE_STORE_BASE"] = str(empty_base)
        env["HERMES_HOME"] = str(tmp_path / "home")

        completed = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                                   env=env, timeout=180)
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        if payload["sdk_present"]:
            pytest.skip("the langfuse SDK is installed in this interpreter; degraded path unreachable")

        assert payload["transport"] == "otlp"
        assert payload["first"] is True and payload["second"] is True
        assert len(payload["warnings"]) == 1, payload["warnings"]
        message = payload["warnings"][0]
        assert "No module named 'langfuse'" in message, message
        assert "provision_sdk_store.sh" in message, message
        assert "HERMES_LANGFUSE_SDK_PATH" in message, message
        assert sys.executable in message, message
        assert "install langfuse --force" not in message, message

        health = payload["health"]
        assert health["state"] == "degraded"
        assert "ModuleNotFoundError" in health["reason"]
        assert health["python"] == sys.executable
        assert health["stores"] and all(str(empty_base) in store for store in health["stores"])


class TestManifestContract:
    def test_declares_a_sidecar_runtime_so_pm_never_builds_4x_into_the_venv(self):
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        assert "python_runtime: external" in manifest, (
            "without this, PM classifies the plugin as a venv member and installs langfuse 4.x "
            "into the shared venv, changing the SDK under the bundled 2.x plugin"
        )

    def test_pm_reads_the_plugin_as_a_non_member(self):
        """Confirm the contract against Hermes PM itself, not against our reading of it."""
        from pm.plugin_declarations import read_python_declaration

        declaration = read_python_declaration(PLUGIN_ROOT)
        assert declaration.external is True
        assert declaration.is_member is False, (
            "PM would build langfuse 4.x into the shared venv, where ten profiles run the 2.x plugin"
        )

    def test_keeps_the_pinned_sdk_in_pyproject_and_sdk_env(self):
        pyproject = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "langfuse==4.15.2" in pyproject
        sdk_env = (PLUGIN_ROOT / "sdk_env.py").read_text(encoding="utf-8")
        assert 'SDK_VERSION = "4.15.2"' in sdk_env
        script = (PLUGIN_ROOT / "scripts" / "provision_sdk_store.sh").read_text(encoding="utf-8")
        assert "4.15.2" in script


class TestProvisionedStore:
    """Runs in a new interpreter: no PYTHONPATH overlay, no manual bootstrap.

    The store is a full ``uv pip install --target`` tree (31 MB on the canary host), so it carries
    its own pydantic/httpx alongside langfuse. Only the *order* is contractual: appended after
    ``site-packages``. Wherever the interpreter already provides pydantic/httpx the venv copy must
    still win; on an interpreter that does not provide them the store's copy is what makes the SDK
    importable — that is a documented property of the store, not a precedence regression.
    """

    def test_plugin_resolution_exposes_sdk4_to_a_fresh_process(self, tmp_path):
        store = Path(_fresh_plugin().sdk_store_candidates()[-1])
        if not (store / "langfuse" / "api" / "client.py").exists():
            pytest.skip(f"SDK store not provisioned at {store}; run scripts/provision_sdk_store.sh")

        probe = tmp_path / "probe_store.py"
        probe.write_text(
            "import json, sys\n"
            f"sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
            "import sdk_env\n"
            "def where(name):\n"
            "    try:\n"
            "        module = __import__(name)\n"
            "    except Exception:\n"
            "        return None\n"
            "    return getattr(module, '__file__', None)\n"
            "before = {name: where(name) for name in ('pydantic', 'httpx')}\n"
            "added = list(sdk_env.ensure_sdk_paths())\n"
            "after = {name: where(name) for name in ('pydantic', 'httpx')}\n"
            "from langfuse.api.client import LangfuseAPI\n"
            "import langfuse\n"
            "print(json.dumps({'added': added, 'before': before, 'after': after,\n"
            "                  'langfuse': langfuse.__version__, 'sys_path': sys.path,\n"
            "                  'api': LangfuseAPI.__module__}))\n",
            encoding="utf-8",
        )
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "HERMES_LANGFUSE_SDK_PATH", "HERMES_LANGFUSE_STORE_BASE"}}
        completed = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                                   env=env, timeout=120)
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert str(store) in payload["added"]
        assert payload["langfuse"].startswith("4."), payload

        site_packages = next((index for index, entry in enumerate(payload["sys_path"])
                              if entry.endswith("site-packages")), None)
        if site_packages is not None:
            assert payload["sys_path"].index(str(store)) > site_packages, (
                "the store must be appended, never inserted"
            )

        for name in ("pydantic", "httpx"):
            resolved_before = payload["before"][name]
            resolved_after = payload["after"][name]
            assert resolved_after, f"{name} must be importable once the store is appended: {payload}"
            if resolved_before is not None:
                assert resolved_after == resolved_before, (
                    f"the store shadowed the interpreter's {name}: {resolved_before} -> {resolved_after}"
                )
                if "site-packages" in resolved_before:
                    assert "site-packages" in resolved_after, payload
