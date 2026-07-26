import os
import time

import pytest

from sandbox_mcp.shell_registry import ShellRegistry
from sandbox_mcp.shell_session import ShellSession, ShellUnhealthy, _health_check


def _ready_bash():
    session = ShellSession(["bash"])
    session.wait_until_ready()
    return session


def test_fresh_local_shell_uses_pty_and_is_ready():
    session = _ready_bash()
    try:
        assert session.state == "ready"
        assert os.isatty(session._process.stdin.fileno())
    finally:
        session.close()


def test_wait_true_returns_ready_output_and_exit_code():
    session = _ready_bash()
    try:
        result = session.send("printf hello; false", timeout=5)
        assert result["status"] == "ready"
        assert result["exit_code"] == 1
        assert "hello" in result["output"]
    finally:
        session.close()


def test_state_is_preserved_between_commands():
    session = _ready_bash()
    try:
        assert session.send("export FOO=bar", timeout=5)["status"] == "ready"
        assert "bar" in session.send('printf %s "$FOO"', timeout=5)["output"]
    finally:
        session.close()


def test_wait_false_returns_waiting_immediately():
    session = _ready_bash()
    try:
        started = time.monotonic()
        result = session.send("sleep 1; printf done", wait=False)
        assert time.monotonic() - started < 0.5
        assert result["status"] == "waiting"
        assert session.state == "waiting"
    finally:
        session.close()


def test_wait_timeout_keeps_waiting_and_gives_async_hint():
    session = _ready_bash()
    try:
        result = session.send("sleep 2", timeout=0.1)
        assert result["status"] == "waiting"
        assert session.state == "waiting"
        assert "wait=false" in result["hint"]
        assert "shell_read" in result["hint"]
    finally:
        session.close()


def test_waiting_shell_rejects_independent_exec_but_accepts_ctrl_c():
    session = _ready_bash()
    try:
        session.send("sleep 20", wait=False)
        rejected = session.send("printf nope")
        assert rejected["status"] == "error"
        assert "waiting" in rejected["error"].lower()
        assert session.write_stdin("\x03")["bytes_written"] == 1
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and session.read()["status"] != "ready":
            time.sleep(0.05)
        assert session.state == "ready"
    finally:
        session.close()


def test_shell_read_detects_prompt_and_returns_ready():
    session = _ready_bash()
    try:
        session.send("sleep 0.1; printf done", wait=False)
        deadline = time.monotonic() + 3
        result = {}
        while time.monotonic() < deadline:
            result = session.read()
            if result["status"] == "ready":
                break
            time.sleep(0.05)
        assert result["status"] == "ready"
        assert result["exit_code"] == 0
        assert "done" in result["output"]
    finally:
        session.close()


def test_only_public_session_states_are_used():
    session = _ready_bash()
    try:
        assert session.state in {"ready", "waiting", "terminated"}
        session.send("sleep 1", wait=False)
        assert session.state in {"ready", "waiting", "terminated"}
        session.close()
        assert session.state == "terminated"
    finally:
        session.close()


def test_terminated_send_has_remove_new_guidance():
    session = ShellSession(["bash"])
    session.close()
    result = session.send("true")
    assert result["status"] == "error"
    assert "shell_remove" in result["error"]
    assert "shell_new" in result["error"]


def test_output_truncation():
    session = _ready_bash()
    try:
        result = session.send("printf 'hello world'", timeout=5, max_output=1)
        assert result["status"] == "ready"
        assert "truncated" in result["output"].lower()
    finally:
        session.close()


def test_exit_terminates_shell_and_captures_code():
    session = _ready_bash()
    session.send("exit 42", timeout=2)

    session._drain_thread.join(timeout=2)
    assert session.state == "terminated"
    assert session.exit_reason == "exit"
    assert session.last_exit_code == 42


def test_health_check_passes_for_fresh_session():
    session = _ready_bash()
    try:
        _health_check(session)
    finally:
        session.close()


def test_health_check_rejects_non_ready(monkeypatch):
    session = ShellSession(["bash"])
    monkeypatch.setattr(session, "send", lambda *a, **kw: {"status": "waiting"})
    with pytest.raises(ShellUnhealthy, match="waiting"):
        _health_check(session)
    session.close()


def test_default_registry_preserves_terminated_shell(monkeypatch):
    monkeypatch.setattr("sandbox_mcp.shell_registry._health_check", lambda session: None)
    registry = ShellRegistry()
    dead = type("Dead", (), {"state": "terminated", "close": lambda self: None})()
    shell_id = registry.get_or_create_default("dev", lambda: dead)
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return dead

    assert registry.get_or_create_default("dev", factory) == shell_id
    assert factory_calls == 0
