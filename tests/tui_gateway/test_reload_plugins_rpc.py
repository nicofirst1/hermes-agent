"""``reload.plugins`` RPC: confirm gate + full-session tool refresh.

The plugin registry is process-global while ``agent.tools`` is per-agent: a reload that
refreshes only the requester's session would leave siblings on stale tools (same contract as
``reload.mcp``, see ``test_mcp_reload_all_sessions.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tui_gateway.server as srv
from tools import mcp_tool_agent as _mcp_agent


@pytest.fixture()
def reload_env(monkeypatch):
    refreshed: list[str] = []
    monkeypatch.setattr(_mcp_agent, "refresh_agent_mcp_tools",
                        lambda agent, **_kw: refreshed.append(agent.name) or set())
    monkeypatch.setattr(srv, "_emit", lambda *_a, **_k: True)
    monkeypatch.setattr(srv, "_session_info", lambda agent, session=None: {})

    def _reload_plugins():
        return {"added": [], "removed": [], "unchanged": [], "total": 0, "errors": []}

    import hermes_cli.plugins_reload as pr
    monkeypatch.setattr(pr, "reload_plugins", _reload_plugins)

    def _session(name):
        return {"agent": SimpleNamespace(name=name), "history": [],
                "history_lock": __import__("threading").RLock(), "running": False}

    monkeypatch.setattr(srv, "_sessions", {
        "A": _session("agent-A"), "B": _session("agent-B"),
    })
    return SimpleNamespace(refreshed=refreshed)


def _call(rid=1, **params):
    return srv._methods["reload.plugins"](rid, params)


def test_reload_without_confirm_asks_for_confirmation(reload_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    resp = _call(session_id="A")

    assert resp["result"]["status"] == "confirm_required"
    assert "prompt cache" in resp["result"]["message"]
    assert reload_env.refreshed == []  # nothing ran


def test_reload_with_confirm_refreshes_every_live_session(reload_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    resp = _call(session_id="A", confirm=True)

    assert resp["result"]["status"] == "reloaded"
    assert sorted(reload_env.refreshed) == ["agent-A", "agent-B"]


def test_confirm_gate_opt_out_runs_without_confirm(reload_env, tmp_path, monkeypatch):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.yaml").write_text(
        "approvals:\n  plugins_reload_confirm: false\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    resp = _call(session_id="A")

    assert resp["result"]["status"] == "reloaded"
    assert sorted(reload_env.refreshed) == ["agent-A", "agent-B"]
