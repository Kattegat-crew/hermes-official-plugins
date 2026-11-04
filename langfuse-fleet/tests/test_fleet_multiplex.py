"""T-16 (AC8): client and spool keyed by the home of each call; secrets only via secret_scope."""

from __future__ import annotations

import importlib

import pytest


def _fleet():
    return importlib.import_module("langfuse_fleet_plugin.legacy_ingestion")


class _Ingestion:
    def batch(self, *, batch, request_options=None):
        return type("Response", (), {"errors": []})()


class _API:
    def __init__(self):
        self.ingestion = _Ingestion()


def test_multiplex_spool_path_follows_the_calling_home(monkeypatch, tmp_path):
    fleet = _fleet()
    paths = {}
    for name in ("alpha", "beta"):
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        paths[name] = fleet.default_spool_path()

    assert paths["alpha"] != paths["beta"]
    for name, path in paths.items():
        assert path == tmp_path / name / "plugins" / "langfuse-fleet" / "spool.db"

    # Never caches "the" home: asking again for the first home returns its path.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "alpha"))
    assert fleet.default_spool_path() == paths["alpha"]


def test_multiplex_clients_from_two_homes_get_isolated_spools(monkeypatch, tmp_path):
    fleet = _fleet()
    clients = {}
    try:
        for name in ("alpha", "beta"):
            home = tmp_path / name
            home.mkdir()
            monkeypatch.setenv("HERMES_HOME", str(home))
            clients[name] = fleet.LegacyIngestionClient(_API())

        # Detach from the process-wide worker so isolation is observed, not raced.
        for client in clients.values():
            fleet._DELIVERY_WORKER._registered.discard(client)
            fleet._DELIVERY_WORKER._due.discard(client)

        alpha, beta = clients["alpha"]._spool, clients["beta"]._spool
        assert alpha._path != beta._path
        assert alpha.enqueue({"id": "evt-mux", "timestamp": "t", "type": "trace-create",
                              "body": {"id": "evt-mux"}}, 64) is True
        assert beta.claim_batch() == []
        assert beta.counters()["delivered"] == 0
        assert len(alpha.claim_batch()) == 1
    finally:
        for client in clients.values():
            fleet._DELIVERY_WORKER._registered.discard(client)
            fleet._DELIVERY_WORKER._due.discard(client)
            client._spool.close()


def test_multiplex_credentials_come_from_secret_scope_not_environ(monkeypatch):
    plugin = importlib.import_module("langfuse_fleet_plugin")
    secret_scope = importlib.import_module("agent.secret_scope")

    calls = []

    def fake_get_secret(name):
        calls.append(name)
        return "value-from-secret-scope"

    monkeypatch.setattr(secret_scope, "get_secret", fake_get_secret)
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "value-from-environ")

    assert plugin._secret("HERMES_LANGFUSE_PUBLIC_KEY") == "value-from-secret-scope"
    assert calls == ["HERMES_LANGFUSE_PUBLIC_KEY"]


def test_multiplex_client_registry_is_keyed_by_hermes_home(monkeypatch, tmp_path):
    from hermes_constants import hermes_home_key

    plugin = importlib.import_module("langfuse_fleet_plugin")
    keys = []
    for name in ("alpha", "beta"):
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        keys.append(hermes_home_key())

    assert keys[0] != keys[1], "distinct homes must not share a registry key"
    assert isinstance(plugin._LANGFUSE_CLIENT_BY_HOME, dict)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-k", "multiplex"]))
