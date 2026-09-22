"""Window/pane spawn helpers for the ``/split`` slash command.

``/split`` forks the session like ``/branch`` but opens the branch somewhere ELSE instead of
switching the current window to it. The spawn strategy degrades in order:

1. **Inside herdr** (``HERDR_PANE_ID`` set): split the current pane and boot the branch with
   ``hermes --resume <id>`` via ``herdr agent start`` — the branch appears beside the current
   conversation.
2. **Bare OS terminal window** (GUI present): macOS Terminal/iTerm2, Linux terminal emulators,
   Windows Terminal / cmd start.
3. **Nowhere to open** (SSH, headless, cron): report the resume command; the branch already
   exists in the session DB. Never silently switch the current window — that is ``/branch``.

Pure helpers only; the handler lives in ``hermes_cli/cli_commands_mixin.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def herdr_spawn_available() -> bool:
    """True when this process runs inside a herdr pane and the herdr CLI is on PATH."""
    return bool(os.environ.get("HERDR_PANE_ID")) and shutil.which("herdr") is not None


def spawn_herdr_pane(session_id: str, cwd: str | None = None) -> bool:
    """Open the branch in a new herdr split pane next to the current pane.

    ``herdr pane split`` creates the pane (returning its id), ``herdr agent start`` boots
    Hermes there resuming the branch. Best-effort: any failure returns False so the caller
    falls through to the next strategy. Must run herdr against the CURRENT session's socket —
    HERDR_SOCKET_PATH already carries it; pass the environment through untouched.
    """
    if not herdr_spawn_available():
        return False
    pane_id = os.environ["HERDR_PANE_ID"]
    cwd = cwd or os.getcwd()
    herdr = shutil.which("herdr") or "herdr"
    try:
        result = subprocess.run(
            [herdr, "pane", "split", "--pane", pane_id, "--direction", "right",
             "--ratio", "0.5", "--cwd", cwd],
            capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return False
        new_pane = _pane_id_from_split_output(result.stdout)
        if not new_pane:
            return False
        result = subprocess.run(
            [herdr, "agent", "start", f"split-{session_id}", "--kind", "hermes",
             "--pane", new_pane, "--timeout", "90000", "--", "--resume", session_id],
            capture_output=True, text=True, timeout=120)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _pane_id_from_split_output(stdout: str) -> str | None:
    """The ``pane_id`` of the created pane from ``herdr pane split`` JSON output."""
    import json
    with __import__("contextlib").suppress(Exception):
        payload = json.loads(stdout)
        return payload.get("result", {}).get("pane", {}).get("pane_id")
    return None


# Bare OS window fallback (outside herdr).

_LINUX_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
                    "alacritty", "kitty")


def spawn_os_terminal_window(session_id: str, cwd: str | None = None) -> bool:
    """Open the branch in a new OS terminal window running ``hermes --resume <id>``.

    Returns False when no GUI terminal can be spawned (headless/SSH) — the caller then prints
    the resume command instead of switching the current window.
    """
    cwd = cwd or os.getcwd()
    resume_argv = [sys.executable, sys.argv[0], "--resume", session_id] \
        if os.path.basename(sys.argv[0] or "") not in {"hermes", "hermes.py"} \
        else ["hermes", "--resume", session_id]
    try:
        if sys.platform == "darwin":
            return _spawn_macos(resume_argv, cwd)
        if sys.platform in ("win32", "cygwin"):
            return _spawn_windows(resume_argv, cwd)
        return _spawn_linux(resume_argv, cwd)
    except (OSError, subprocess.SubprocessError):
        return False


def _spawn_macos(resume_argv: list[str], cwd: str) -> bool:
    cmd = _shell_command(resume_argv, cwd)
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        script = f'tell application "iTerm2"\ncreate window with default profile\ntell current session of current window to write text {cmd}\nend tell'
    else:
        script = f'tell application "Terminal" to do script {cmd}'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    return result.returncode == 0


def _spawn_windows(resume_argv: list[str], cwd: str) -> bool:
    cmd = _shell_command(resume_argv, cwd)
    if shutil.which("wt.exe"):
        result = subprocess.run(["wt.exe", "new-tab", cmd], capture_output=True, timeout=10)
        return result.returncode == 0
    result = subprocess.run(["cmd.exe", "/c", "start", cmd], capture_output=True, timeout=10)
    return result.returncode == 0


def _spawn_linux(resume_argv: list[str], cwd: str) -> bool:
    forced = os.environ.get("TERMINAL")
    candidates = ([forced] if forced and shutil.which(forced) else []) + [
        term for term in _LINUX_TERMINALS if shutil.which(term)]
    if not candidates:
        return False
    term_path = shutil.which(candidates[0])
    if not term_path:
        return False
    cmd = _shell_command(resume_argv, cwd)
    result = subprocess.run([term_path, "-e", cmd], capture_output=True, timeout=10)
    return result.returncode == 0


def _shell_command(resume_argv: list[str], cwd: str) -> str:
    """A quoted shell line that cds and resumes."""
    import shlex
    return "cd " + shlex.quote(cwd) + " && " + shlex.join(resume_argv)


def spawn_branch_surface(session_id: str, cwd: str | None = None) -> bool:
    """Ordered strategy chain: herdr split pane → OS terminal window → False (print fallback)."""
    return spawn_herdr_pane(session_id, cwd) or spawn_os_terminal_window(session_id, cwd)
