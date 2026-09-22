"""``/reload-plugins`` core helper: enable a plugin mid-session and it becomes loaded (and its
tool registered) without a restart; disable it and the tool deregisters.

Loads through the REAL discovery path with a temp ``HERMES_HOME`` (plugins/AGENTS.md test rules):
a plugin installed on disk but disabled at startup must be picked up by the reload after
``plugins.enabled`` flips in config.yaml.
"""

from __future__ import annotations

import textwrap

import pytest

from hermes_cli.plugins import get_plugin_manager
from hermes_cli.plugins_reload import reload_plugins, summarize_reload_plugins

PLUGIN_BODY = textwrap.dedent("""\
    def register(ctx):
        ctx.register_tool(
            name="echo_kit_ping", toolset="echo_kit",
            schema={"name": "echo_kit_ping", "description": "echo test tool",
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda **kwargs: "pong")
""")

_MANIFEST_YAML = "name: echo-kit\nversion: 1.0.0\nkind: standalone\n"


def _install(home, enable: bool) -> None:
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled: [{'echo-kit' if enable else ''}]\n"
    )
    plugin = home / "plugins" / "echo-kit"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.yaml").write_text(_MANIFEST_YAML)
    (plugin / "__init__.py").write_text(PLUGIN_BODY)


@pytest.fixture()
def plugin_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty-bundled"))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    monkeypatch.chdir(home)
    _install(home, enable=False)  # installed on disk, NOT enabled yet
    return home


def _tool_registered() -> bool:
    from tools.registry import registry
    return registry.get_entry("echo_kit_ping") is not None


def test_enable_via_config_reaches_the_running_session(plugin_home):
    # get_plugin_manager() keys per resolved home, so the patched HERMES_HOME gets a fresh
    # manager here — the same resolution a live session's reload path uses.
    get_plugin_manager().discover_and_load()
    assert not _tool_registered()  # installed but disabled: tool absent

    _install(plugin_home, enable=True)  # flip plugins.enabled in config.yaml
    result = reload_plugins()

    assert result["total"] == 1
    assert [item["name"] for item in result["added"]] == ["echo-kit"]
    assert result["removed"] == []
    assert _tool_registered()  # the reload loaded it and registered its tool


def test_disable_unloads_the_tool(plugin_home):
    _install(plugin_home, enable=True)
    get_plugin_manager().discover_and_load()
    assert _tool_registered()

    _install(plugin_home, enable=False)
    result = reload_plugins()

    assert [item["name"] for item in result["removed"]] == ["echo-kit"]
    assert result["added"] == []
    assert not _tool_registered()


def test_noop_reload_reports_no_changes(plugin_home):
    _install(plugin_home, enable=True)
    reload_plugins()

    second = reload_plugins()

    assert second["added"] == [] and second["removed"] == []
    assert "echo-kit" in second["unchanged"]
    assert second["total"] == 1


def test_summary_lines_render_the_diff(plugin_home):
    _install(plugin_home, enable=True)
    result = reload_plugins()

    lines = summarize_reload_plugins(result)

    assert lines[0].startswith("🔄")
    assert any("echo-kit" in line and "➕" in line for line in lines)
