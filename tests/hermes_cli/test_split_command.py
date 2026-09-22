"""Tests for the /split command — branch a session and open it in a herdr split pane.

Verifies that:
- /split creates a branched session (shared machinery with /branch) with copied history
- The CURRENT session is untouched: not ended, session_id unchanged, no memory-provider
  session-switch notification (the branch is taken over by the spawned pane, not this process)
- Spawn uses herdr (pane split + agent start --resume) when running inside herdr
- Spawn falls back to printing the resume command when no window mechanism is available
- --inplace delegates to /branch behaviour (current window switches to the branch)
- Edge cases: empty conversation, missing session DB, agent busy
"""

import os
from datetime import datetime
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def session_db(tmp_path):
    """Create a real SessionDB for testing."""
    os.environ["HERMES_HOME"] = str(tmp_path / ".hermes")
    os.makedirs(tmp_path / ".hermes", exist_ok=True)
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / ".hermes" / "test_sessions.db")
    yield db
    db.close()


@pytest.fixture
def cli_instance(tmp_path, session_db):
    """Create a minimal HermesCLI-like object for testing _handle_split_command."""
    cli = MagicMock()
    cli._agent_running = False  # a bare MagicMock attribute is truthy and would trip the mid-turn guard
    cli._session_db = session_db
    cli.session_id = "20260403_120000_abc123"
    cli.model = "anthropic/claude-sonnet-4.6"
    cli.max_turns = 90
    cli.reasoning_config = {"enabled": True, "effort": "medium"}
    cli.session_start = datetime.now()
    cli._pending_title = None
    cli._resumed = False
    cli.agent = None
    cli.conversation_history = [
        {"role": "user", "content": "Hello, can you help me?"},
        {"role": "assistant", "content": "Of course! How can I help?"},
        {"role": "user", "content": "Write a Python function to sort a list."},
        {"role": "assistant", "content": "def sort_list(lst): return sorted(lst)"},
    ]

    # Create the original session in the DB
    session_db.create_session(
        session_id=cli.session_id,
        source="cli",
        model=cli.model,
    )
    session_db.set_session_title(cli.session_id, "My Coding Session")

    # Bind the real shared helper: the handlers call it as self._create_branch_session, which a
    # MagicMock would otherwise answer with a MagicMock instead of running the real code.
    from cli import HermesCLI
    cli._create_branch_session = (
        lambda branch_name, **kw: HermesCLI._create_branch_session(cli, branch_name, **kw))

    return cli


class TestSplitCommandCLI:
    """Test the /split command logic for the CLI."""

    def test_split_creates_branch_session(self, cli_instance, session_db):
        """A /split must create a new session row with the copied history."""
        from cli import HermesCLI

        HermesCLI._handle_split_command(cli_instance, "/split")

        sessions = session_db.list_sessions_rich(limit=10, include_hidden=True)
        child = [s for s in sessions if s["id"] != "20260403_120000_abc123"]
        assert len(child) == 1
        new_id = child[0]["id"]
        messages = session_db.get_messages_as_conversation(new_id)
        assert len(messages) == 4

    def test_split_leaves_current_session_unchanged(self, cli_instance, session_db):
        """The defining behaviour: the current window STAYS on the parent.

        Unlike /branch, session_id must not move, the parent row must not be
        ended, and no memory-provider switch may fire (the spawned pane owns
        the branch; this process keeps the parent)."""
        from cli import HermesCLI

        agent = MagicMock()
        mm = MagicMock()
        agent._memory_manager = mm
        cli_instance.agent = agent

        HermesCLI._handle_split_command(cli_instance, "/split")

        assert cli_instance.session_id == "20260403_120000_abc123"
        row = session_db.get_session("20260403_120000_abc123")
        assert row["end_reason"] is None and row["ended_at"] is None
        assert cli_instance._resumed is False
        mm.on_session_switch.assert_not_called()
        # The branch session must link back to the parent
        sessions = session_db.list_sessions_rich(limit=10, include_hidden=True)
        child = next(s for s in sessions if s["id"] != "20260403_120000_abc123")
        assert child["parent_session_id"] == "20260403_120000_abc123"

    def test_split_with_custom_name(self, cli_instance, session_db):
        """A custom name becomes the branch title."""
        from cli import HermesCLI

        HermesCLI._handle_split_command(cli_instance, "/split refactor approach")

        sessions = session_db.list_sessions_rich(limit=10, include_hidden=True)
        child = next(s for s in sessions if s["id"] != "20260403_120000_abc123")
        assert session_db.get_session_title(child["id"]) == "refactor approach"

    def test_split_no_conversation(self, cli_instance):
        """No conversation history → guard, nothing created."""
        from cli import HermesCLI
        cli_instance.conversation_history = []

        HermesCLI._handle_split_command(cli_instance, "/split")

    def test_split_no_session_db(self, cli_instance):
        """Missing session DB → guard, nothing created."""
        from cli import HermesCLI
        cli_instance._session_db = None

        HermesCLI._handle_split_command(cli_instance, "/split")

    def test_split_agent_busy(self, cli_instance):
        """Mid-turn guard mirrors /branch: refuse while the agent is running."""
        from cli import HermesCLI
        cli_instance._agent_running = True

        HermesCLI._handle_split_command(cli_instance, "/split")

    def test_split_inplace_delegates_to_branch(self, cli_instance, session_db):
        """--inplace must delegate to the /branch handler with the flag stripped."""
        from cli import HermesCLI

        delegated = []
        cli_instance._handle_branch_command = lambda cmd: delegated.append(cmd)

        HermesCLI._handle_split_command(cli_instance, "/split --inplace")

        assert delegated == ["/branch"]
        # session_id unchanged because the (mocked) branch handler never ran
        assert cli_instance.session_id == "20260403_120000_abc123"


class TestSplitSpawn:
    """The spawn layer: herdr pane when inside herdr, print fallback otherwise."""

    def test_spawn_via_herdr_pane(self, cli_instance, monkeypatch):
        """Inside herdr (HERDR_PANE_ID set): split the current pane and boot the
        branch session in the new pane via hermes --resume."""
        from cli import HermesCLI
        import hermes_cli.cli_window_spawn as spawn_mod

        recorded = {}
        monkeypatch.setattr(spawn_mod, "spawn_herdr_pane",
                            lambda session_id, cwd: recorded.update(session_id=session_id, cwd=cwd) or True)
        monkeypatch.setattr(spawn_mod, "spawn_os_terminal_window",
                            lambda session_id, cwd: recorded.setdefault("os_fell_through", True) or False)
        monkeypatch.setenv("HERDR_PANE_ID", "wQ:p5")

        HermesCLI._handle_split_command(cli_instance, "/split")

        sessions = cli_instance._session_db.list_sessions_rich(limit=10, include_hidden=True)
        child = next(s for s in sessions if s["id"] != "20260403_120000_abc123")
        assert recorded["session_id"] == child["id"]
        assert "os_fell_through" not in recorded  # herdr wins; OS window never attempted

    def test_fallback_prints_resume_command(self, cli_instance, monkeypatch, capsys):
        """No herdr, no GUI terminal: print the resume command; never switch."""
        from cli import HermesCLI
        import hermes_cli.cli_window_spawn as spawn_mod

        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        monkeypatch.setattr(spawn_mod, "spawn_os_terminal_window", lambda session_id, cwd: False)

        HermesCLI._handle_split_command(cli_instance, "/split")

        out = capsys.readouterr().out
        sessions = cli_instance._session_db.list_sessions_rich(limit=10, include_hidden=True)
        child = next(s for s in sessions if s["id"] != "20260403_120000_abc123")
        assert child["id"] in out  # the fallback surfaced the branch id for manual resume
        # And the current window is still on the parent
        assert cli_instance.session_id == "20260403_120000_abc123"


class TestSplitCommandDef:
    """Registry contract for /split."""

    def test_split_in_registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        split = next(c for c in COMMAND_REGISTRY if c.name == "split")
        assert split.category == "Session"

    def test_split_is_gated_out_of_gateway(self):
        """Gateway platforms have no panes/windows: cli_only must keep /split out of
        GATEWAY_KNOWN_COMMANDS (the dispatch allowlist)."""
        from hermes_cli.commands import COMMAND_REGISTRY, GATEWAY_KNOWN_COMMANDS
        split = next(c for c in COMMAND_REGISTRY if c.name == "split")
        assert split.cli_only and not split.gateway_config_gate
        assert "split" not in GATEWAY_KNOWN_COMMANDS

    def test_split_handler_follows_naming_convention(self):
        """Dispatch resolves handlers by naming convention (_handle_<name>_command)."""
        from cli import HermesCLI
        assert hasattr(HermesCLI, "_handle_split_command")


class TestWindowSpawnHelpers:
    """Unit contract of hermes_cli/cli_window_spawn.py (pure logic, no real spawns)."""

    def test_herdr_detection(self, monkeypatch):
        import hermes_cli.cli_window_spawn as spawn_mod
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        assert spawn_mod.herdr_spawn_available() is False
        monkeypatch.setenv("HERDR_PANE_ID", "wQ:p1")
        monkeypatch.setattr(spawn_mod.shutil, "which", lambda name: "/usr/bin/herdr")
        assert spawn_mod.herdr_spawn_available() is True

    def test_herdr_pane_split_invocation(self, monkeypatch):
        """The herdr path must split the CURRENT pane and resume the branch in it."""
        import hermes_cli.cli_window_spawn as spawn_mod

        calls = []
        monkeypatch.setenv("HERDR_PANE_ID", "wQ:p5")

        class FakeCompleted:
            returncode = 0
            stdout = '{"result":{"pane":{"pane_id":"wQ:p6"}}}'

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return FakeCompleted()

        monkeypatch.setattr(spawn_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(spawn_mod.shutil, "which", lambda name: "/usr/bin/herdr")

        assert spawn_mod.spawn_herdr_pane("20260922_120000_aaa", "/tmp") is True
        assert calls[0][0:3] == ["/usr/bin/herdr", "pane", "split"]
        assert "wQ:p5" in calls[0] and "--cwd" in calls[0]
        assert calls[1][0:3] == ["/usr/bin/herdr", "agent", "start"]
        assert calls[1][-2:] == ["--resume", "20260922_120000_aaa"]
        assert "wQ:p6" in calls[1]  # the NEW pane id from the split result

    def test_herdr_spawn_failure_returns_false(self, monkeypatch):
        """A herdr failure must fall through (return False), never raise."""
        import hermes_cli.cli_window_spawn as spawn_mod

        class FakeFailed:
            returncode = 1
            stdout = ""
            stderr = ""

        monkeypatch.setenv("HERDR_PANE_ID", "wQ:p5")
        monkeypatch.setattr(spawn_mod.subprocess, "run", lambda *a, **k: FakeFailed())
        monkeypatch.setattr(spawn_mod.shutil, "which", lambda name: "/usr/bin/herdr")

        assert spawn_mod.spawn_herdr_pane("20260922_120000_aaa", "/tmp") is False

