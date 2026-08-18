# sandbox-mcp - Sandbox Environment Manager MCP server
# Copyright (C) 2024  Sandbox MCP Contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.server import SandboxServer


@pytest.fixture(autouse=True)
def _disable_default_machine(monkeypatch):
    """Unit tests run in lazy mode: opt-out of default-machine provisioning.

    The shipped config enables [default_machine] so out-of-box installs
    bring up an admin container.  Provisioning requires a real Docker
    daemon, which is not available in this test environment — every
    test that instantiates SandboxServer() would otherwise hit
    ``failed to provision default machine 'admin'`` on the MagicMock.

    Tests that DO exercise provisioning override this by setting
    ``SANDBOX_MCP_DEFAULT_MACHINE_ENABLED=true`` after this fixture runs
    (pytest's monkeypatch preserves later ``setenv`` calls).
    """
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "false")


@pytest.fixture
def server():
    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        return SandboxServer()


def test_list_tools_includes_audit_query_by_default(server):
    """With the default config, audit is file-backed, so the tool is exposed."""
    tools = server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "shell_exec",
        "shell_read",
        "file_read",
        "file_write",
        "file_patch",
        "file_search",
        "env",
        "audit_query",
    }
    assert expected.issubset(names)


def test_list_tools_omits_audit_query_when_log_path_empty(monkeypatch):
    """When [audit] log_path is empty, the audit tool is hidden from agents."""
    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", "")
    from unittest.mock import patch

    with patch("sandbox_mcp.server.DockerBackend"), patch("sandbox_mcp.server.SSHBackend"):
        srv = SandboxServer()
    names = {t.name for t in srv.list_tools()}
    assert "audit_query" not in names
    # Sanity: other tools still present
    assert "shell_exec" in names


def test_call_unknown_tool(server):
    result = server.call_tool("nonexistent", {})
    data = json.loads(result[0].text)
    assert "error" in data


def test_sandbox_env_help(server):
    result = server.call_tool("env", {"action": "help"})
    data = json.loads(result[0].text)
    assert "operations" in data
    assert "default_actions" in data
    actions = [op["action"] for op in data["operations"]]
    assert "docker_run" in actions
    assert "docker_build" in actions


def test_sandbox_env_status_empty(server):
    result = server.call_tool("env", {"action": "status"})
    data = json.loads(result[0].text)
    assert data["default_machine"] is None
    assert data["machines"] == []


def test_server_bootstraps_registry_via_docker_ps(monkeypatch):
    """``SandboxServer.__init__`` calls ``docker_ps`` once before serving
    requests so the registry reflects pre-existing labeled containers
    on the daemon.  No separate ``_reconcile_managed_containers``
    function — the existing ``docker_ps`` path IS the refresh.
    """
    from unittest.mock import patch

    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker = mock_docker_cls.return_value
        attrs = {"State": {"Status": "running"}, "Config": {"Image": "alpine:3"}}
        mock_docker.list_managed_containers.return_value = [("dev", attrs)]
        srv = SandboxServer()
    # The dispatcher ran docker_ps during init, populating the registry.
    assert srv.machines.list_machines() == ["dev"]
    mock_docker.list_managed_containers.assert_called_once()
    # No create() was ever invoked — adoption only.
    mock_docker.create.assert_not_called()


def test_audit_records_inner_action_for_sandbox_env(monkeypatch, tmp_path):
    """``env`` is a meta-tool: the inner ``action`` arg is the
    real action and should land in the indexed ``action`` column, not
    the wrapper tool name ``"env"``.
    """
    import sqlite3

    from sandbox_mcp.audit import AuditLogger

    db = tmp_path / "audit.db"
    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", str(db))
    with (
        patch("sandbox_mcp.server.DockerBackend"),
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        srv = SandboxServer(audit=AuditLogger(sink=str(db)))
    # Trigger an audit entry by calling env(action="status").
    srv.call_tool("env", {"action": "status"})

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT action, details FROM audit").fetchall()
    assert len(rows) == 1
    action, details_json = rows[0]
    # Inner action is recorded at the top column level.
    assert action == "status"
    # ``action`` is filtered out of details (already promoted).
    assert "action" not in (details_json or "{}")


def test_audit_query_does_not_record_itself(monkeypatch, tmp_path):
    """Querying the audit log must not pollute it with self-references."""
    import sqlite3

    from sandbox_mcp.audit import AuditLogger

    db = tmp_path / "audit.db"
    # Pre-existing row from before the query.
    AuditLogger(sink=str(db)).record(machine=None, action="pre_existing")

    monkeypatch.setenv("SANDBOX_MCP_AUDIT_LOG_PATH", str(db))
    with (
        patch("sandbox_mcp.server.DockerBackend"),
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        srv = SandboxServer(audit=AuditLogger(sink=str(db)))

    # Make a query through the tool.
    srv.call_tool("audit_query", {})

    with sqlite3.connect(db) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit").fetchall()]
    # Only the pre-existing row is present; the query did not record itself.
    assert actions == ["pre_existing"]


# ---- [default_machine] startup provisioning ----


def test_provision_default_machine_disabled_is_noop(monkeypatch):
    """enabled=false → no provisioning, no default machine."""
    # Autouse fixture already set enabled=false; explicit for clarity.
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "false")
    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        srv = SandboxServer()
    mock_docker_cls.return_value.create.assert_not_called()
    assert srv.machines.get_default() is None


def test_provision_default_machine_docker(monkeypatch):
    """enabled=true + docker backend -> default machine created and set."""
    from sandbox_mcp.backends.base import TargetInfo

    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_BACKEND", "docker")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "dev")
    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker = mock_docker_cls.return_value
        mock_docker.create.return_value = TargetInfo(
            name="dev", backend="docker", status="running", image="python:3.12"
        )
        srv = SandboxServer()

    mock_docker.create.assert_called_once()
    # No image kwarg is forwarded by provisioning -- the docker backend
    # falls back to [docker] default_image inside create().
    assert "image" not in mock_docker.create.call_args.kwargs
    assert srv.machines.get_default() == "dev"
    assert srv.machines.list_machines() == ["dev"]


def test_provision_default_machine_docker_surfaces_reattach_note(monkeypatch):
    """When create() reattaches (409) it returns a note; provisioning
    still succeeds (status=running) and logs the note."""
    from sandbox_mcp.backends.base import TargetInfo

    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "default")
    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker_cls.return_value.create.return_value = TargetInfo(
            name="default",
            backend="docker",
            status="running",
            note="reattached to existing container (already running)",
        )
        srv = SandboxServer()
    assert srv.machines.get_default() == "default"


def test_provision_default_machine_ssh(monkeypatch, tmp_path):
    """enabled=true + ssh backend -> looks up [ssh.targets.{name}] and
    sets the default machine."""
    from sandbox_mcp.backends.base import TargetInfo

    key_path = tmp_path / "id_ed25519"
    key_path.write_text("dummy")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f"""
[ssh.targets.remote]
host = "10.0.0.5"
user = "ubuntu"
port = 2222
key = "{key_path}"
"""
    )
    monkeypatch.setenv("SANDBOX_MCP_CONFIG", str(cfg_file))
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_BACKEND", "ssh")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "remote")
    with (
        patch("sandbox_mcp.server.DockerBackend"),
        patch("sandbox_mcp.server.SSHBackend") as mock_ssh_cls,
    ):
        mock_ssh = mock_ssh_cls.return_value
        mock_ssh.create.return_value = TargetInfo(name="remote", backend="ssh", status="running")
        srv = SandboxServer()

    mock_ssh.create.assert_called_once()
    kwargs = mock_ssh.create.call_args.kwargs
    assert kwargs["host"] == "10.0.0.5"
    assert kwargs["user"] == "ubuntu"
    assert kwargs["port"] == 2222
    assert kwargs["key"] == str(key_path)
    assert srv.machines.get_default() == "remote"


def test_provision_default_machine_failure_raises(monkeypatch):
    """enabled=true + backend raises -> RuntimeError, server refuses to start."""
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_BACKEND", "docker")
    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker_cls.return_value.create.side_effect = Exception("daemon unreachable")
        with pytest.raises(RuntimeError, match="failed to provision default machine"):
            SandboxServer()


def test_provision_default_machine_error_info_raises(monkeypatch):
    """create() returns a non-running TargetInfo -> RuntimeError (fail-closed)."""
    from sandbox_mcp.backends.base import TargetInfo

    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "default")
    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker_cls.return_value.create.return_value = TargetInfo(
            name="default", backend="docker", status="error", error="image pull failed"
        )
        with pytest.raises(RuntimeError, match="image pull failed"):
            SandboxServer()


def test_provision_default_machine_reattaches_when_already_adopted(monkeypatch):
    """If docker_ps already adopted the default name, provisioning just
    sets it as default - no second create()."""
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_NAME", "dev")
    with (
        patch("sandbox_mcp.server.DockerBackend") as mock_docker_cls,
        patch("sandbox_mcp.server.SSHBackend"),
    ):
        mock_docker = mock_docker_cls.return_value
        attrs = {"State": {"Status": "running"}, "Config": {"Image": "alpine:3"}}
        mock_docker.list_managed_containers.return_value = [("dev", attrs)]
        srv = SandboxServer()

    # docker_ps adopted "dev"; provisioning must not create again.
    mock_docker.create.assert_not_called()
    assert srv.machines.get_default() == "dev"


def test_provision_default_machine_ssh_requires_target(monkeypatch):
    """backend='ssh' without matching [ssh.targets.{name}] -> RuntimeError."""
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MCP_DEFAULT_MACHINE_BACKEND", "ssh")
    with (
        patch("sandbox_mcp.server.DockerBackend"),
        patch("sandbox_mcp.server.SSHBackend"),
        pytest.raises(RuntimeError, match=r"requires \[ssh.targets.admin\]"),
    ):
        SandboxServer()


# ---------- shell_exec error_kind responses ----------


def test_handle_shell_exec_returns_shell_unhealthy_error_kind(monkeypatch, server):
    """When open()'s health check fails, response has error_kind='shell_unhealthy'."""
    from sandbox_mcp.backends.base import TargetInfo
    from sandbox_mcp.shell_session import ShellUnhealthy

    # Register a stub machine so _resolve_machine doesn't raise first.
    server.machines.adopt(
        "dev", MagicMock(), TargetInfo(name="dev", backend="docker", status="running")
    )

    def raise_unhealthy(*a, **kw):
        raise ShellUnhealthy("broken bash")

    monkeypatch.setattr(server.shells, "get_or_create_default", raise_unhealthy)

    result = server._handle_shell_exec({"command": "echo hi", "machine": "dev"})
    assert result["status"] == "error"
    assert result["error_kind"] == "shell_unhealthy"
    assert "broken bash" in result["error"]
    assert result["machine"] == "dev"


def test_handle_shell_exec_returns_shell_create_failed_error_kind(monkeypatch, server):
    """When factory() raises a non-shell error, error_kind='shell_create_failed'."""
    from sandbox_mcp.backends.base import TargetInfo

    server.machines.adopt(
        "dev", MagicMock(), TargetInfo(name="dev", backend="docker", status="running")
    )

    def raise_runtime(*a, **kw):
        raise RuntimeError("docker daemon down")

    monkeypatch.setattr(server.shells, "get_or_create_default", raise_runtime)

    result = server._handle_shell_exec({"command": "echo hi", "machine": "dev"})
    assert result["status"] == "error"
    assert result["error_kind"] == "shell_create_failed"
    assert "docker daemon down" in result["error"]
    assert result["machine"] == "dev"


# ---------- shell protocol schema drift guards ----------
#
# These tests assert the agent-visible tool definitions use the canonical
# shell protocol (ready / waiting / terminated) and do NOT expose the
# historical idle / busy / completed vocabulary that an LLM could
# otherwise latch onto when interpreting responses.


def _tool(server, name):
    tools = server.list_tools()
    matches = [t for t in tools if t.name == name]
    assert matches, f"tool {name!r} not exposed"
    return matches[0]


def test_shell_new_description_documents_canonical_states(server):
    """shell_new's description must refer to canonical shell states, not
    the legacy busy/idle vocabulary."""
    tool = _tool(server, "shell_new")
    desc = tool.description.lower()
    # The canonical scenario for shell_new is "the default shell is busy
    # running a long command"; rewrite in terms of waiting/ready/terminated.
    assert "busy" not in desc, (
        f"shell_new description still references legacy 'busy': {tool.description!r}"
    )
    assert any(word in desc for word in ("waiting", "ready", "terminated")), (
        f"shell_new description does not reference any canonical state: {tool.description!r}"
    )


def test_shell_remove_description_lists_canonical_states(server):
    """shell_remove advertises that it works on any shell state.  The
    enumerated states must be the canonical three, not the legacy four."""
    tool = _tool(server, "shell_remove")
    desc = tool.description.lower()
    for state in ("ready", "waiting", "terminated"):
        assert state in desc, (
            f"shell_remove description missing canonical state {state!r}: {tool.description!r}"
        )
    for legacy in ("idle", "busy"):
        assert legacy not in desc, (
            f"shell_remove description still references legacy {legacy!r}: {tool.description!r}"
        )


def test_shell_exec_input_schema_documents_wait_default(server):
    """shell_exec schema must tell the agent that wait defaults to true."""
    tool = _tool(server, "shell_exec")
    props = tool.input_schema["properties"]
    assert props["wait"]["description"].lower().startswith("wait"), (
        f"shell_exec.wait description missing 'wait' prefix: {props['wait']!r}"
    )
    assert "true" in props["wait"]["description"].lower(), (
        f"shell_exec.wait description missing 'true' default hint: {props['wait']!r}"
    )
    timeout = props["timeout"]["description"].lower()
    assert "10" in timeout, f"shell_exec.timeout description missing '10': {props['timeout']!r}"


def test_shell_exec_error_guidance_offers_remove_and_new(server, monkeypatch):
    """When shell_exec returns a waiting-or-terminated error the response
    must expose escape_routes naming both shell_new and shell_remove."""
    from sandbox_mcp.backends.base import TargetInfo

    session = MagicMock()
    session.send.return_value = {
        "output": "",
        "exit_code": None,
        "status": "error",
        "error": "Shell is waiting for the previous command.",
    }
    server.machines.adopt(
        "dev", MagicMock(), TargetInfo(name="dev", backend="docker", status="running")
    )
    monkeypatch.setattr(server.shells, "get", lambda shell_id: session)

    result = server._handle_shell_exec({"command": "echo hi", "shell_id": "sh_x", "machine": "dev"})
    assert "escape_routes" in result, f"missing escape_routes: {result!r}"
    routes = result["escape_routes"]
    assert "shell_new" in routes, f"escape_routes missing shell_new: {routes!r}"
    assert "shell_remove" in routes, f"escape_routes missing shell_remove: {routes!r}"
