"""``/reload-plugins`` — re-read ``plugins.enabled`` from config.yaml and load/unload plugins
mid-session without ``/new``.

The heavy lifting is ``PluginManager.discover_and_load(force=True)``: the ownership ledger
unloads every registration in reverse order, stale persistent registrations are evicted
(#91701), config-owned shell hooks/outbound webhooks are re-registered (#60036), and plugin
secret sources are re-applied. This module adds the thin diff + summary layer the three
surfaces (CLI REPL, gateway, TUI/Desktop RPC) share.

Tool-schema visibility is handled by the existing machinery: plugin tool (de)registration
bumps ``registry._generation``, which keys the ``get_tool_definitions`` memo, and
``refresh_agent_mcp_tools`` re-derives a built agent's snapshot from the live registry (the
``/reload-mcp`` path); callers that hold a cached agent refresh it after the reload.

Memory-provider kind: a force reload re-scans and (de)registers providers, but the ACTIVE
provider is bound at agent init, so switching ``memory.provider`` mid-conversation still
takes effect on next session — surfaces say so instead of implying full hot-swap.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def reload_plugins() -> Dict[str, Any]:
    """Force plugin re-discovery and return a diff of the loaded-plugin set.

    Shape mirrors :func:`agent.skill_commands.reload_skills` so surfaces render reloads
    uniformly: ``added`` / ``removed`` are ``[{name, key, kind}]``, ``unchanged`` a name
    list, plus ``total`` (loaded count after reload) and ``errors`` (plugins that failed
    to load, from ``list_plugins()``).
    """
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    def _snapshot() -> Dict[str, Dict[str, Any]]:
        # Enabled (loaded) plugins only: list_plugins() also reports installed-but-disabled
        # entries, which must not mask a real enable/disable transition in the diff.
        manager = get_plugin_manager()
        return {
            info["key"]: {"name": info["name"], "kind": info["kind"] or "general"}
            for info in manager.list_plugins() if info["enabled"]
        }

    before = _snapshot()
    discover_plugins(force=True)
    after = _snapshot()

    added = [{"key": k, **after[k]} for k in sorted(set(after) - set(before))]
    removed = [{"key": k, **before[k]} for k in sorted(set(before) - set(after))]
    manager = get_plugin_manager()
    errors = [
        {"name": info["name"], "error": info["error"]}
        for info in manager.list_plugins() if info.get("error")
    ]
    return {
        "added": added,
        "removed": removed,
        "unchanged": sorted(info["name"] for k, info in after.items() if k in before),
        "total": len(after),
        "errors": errors,
    }


def summarize_reload_plugins(result: Dict[str, Any]) -> List[str]:
    """Render the :func:`reload_plugins` diff as user-facing lines (shared by the CLI print
    and the gateway text so both surfaces stay in lockstep)."""
    lines: List[str] = ["🔄 Plugins reloaded"]
    added, removed = result.get("added") or [], result.get("removed") or []
    if not added and not removed:
        lines.append(f"  No changes — {result.get('total', 0)} plugin(s) loaded")
    for item in added:
        lines.append(f"  ➕ Loaded: {item['name']} ({item['key']})")
    for item in removed:
        lines.append(f"  ➖ Unloaded: {item['name']} ({item['key']})")
    for item in result.get("errors") or []:
        lines.append(f"  ❌ {item['name']}: {item['error']}")
    lines.append(
        "  ℹ️ Tool changes reach the model on the next message and invalidate the prompt cache"
        " for this conversation (next message re-sends full input tokens)."
        " Switching the active memory provider still takes effect on next session."
    )
    return lines


__all__ = ["reload_plugins", "summarize_reload_plugins"]
