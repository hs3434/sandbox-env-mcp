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

from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.backends.ssh_backend import SSHBackend
from sandbox_mcp.config import SSHMachineState, SSHTarget


def _fake_state(name="remote", **target_overrides) -> SSHMachineState:
    target_kwargs = {"host": "192.168.1.100", "user": "ubuntu", "port": 22}
    target_kwargs.update(target_overrides)
    target = SSHTarget(**target_kwargs)
    return SSHMachineState(
        target=target,
        socket=f"/tmp/sandbox-mcp-ssh-{name}/control",
        socket_dir=f"/tmp/sandbox-mcp-ssh-{name}",
        input_codepage=None,
        output_codepage=None,
        encoding_source="default",
        input_encoding="utf-8",
        output_encoding="utf-8",
        started_at=0.0,
    )


@pytest.fixture
def ssh_backend():
    return SSHBackend()


def test_ssh_create(ssh_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = ssh_backend.create(
            name="remote",
            purpose="remote",
            host="192.168.1.100",
            user="ubuntu",
        )
        assert info.name == "remote"
        assert info.backend == "ssh"
        assert info.status == "running"


def test_ssh_stop_disconnects(ssh_backend):
    ssh_backend._targets["remote"] = _fake_state()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        info = ssh_backend.stop("remote")
        assert info.status == "stopped"


def test_ssh_remove_unregisters(ssh_backend):
    ssh_backend._targets["remote"] = _fake_state()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = ssh_backend.remove("remote")
        assert result["status"] == "removed"
        assert "remote" not in ssh_backend._targets


def test_ssh_create_allocates_socket_dir_and_remove_reaps_it(ssh_backend, tmp_path):
    """``_socket_path`` uses ``tempfile.mkdtemp`` to host the SSH control
    socket; ``remove()`` must ``shutil.rmtree`` that dir, otherwise a
    long-running server leaks one directory per SSH target.
    """
    with patch("sandbox_mcp.backends.ssh_backend.tempfile.mkdtemp") as mock_mkdtemp:
        mock_mkdtemp.return_value = str(tmp_path / "sandbox-mcp-ssh-remote-abc")
        (tmp_path / "sandbox-mcp-ssh-remote-abc").mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            ssh_backend.create(
                name="remote",
                purpose="remote",
                host="192.168.1.100",
                user="ubuntu",
            )
            assert (tmp_path / "sandbox-mcp-ssh-remote-abc").is_dir()
            ssh_backend.remove("remote")
        assert not (tmp_path / "sandbox-mcp-ssh-remote-abc").exists(), (
            "remove() must rmtree the socket dir created by mkdtemp"
        )


def test_ssh_open_shell(ssh_backend):
    ssh_backend._targets["remote"] = _fake_state()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        shell = ssh_backend.open_shell("remote")
        assert "ssh" in shell._args[0]
        assert "-tt" in shell._args
        shell.close()


def test_ssh_write_file_streams_content_via_stdin(ssh_backend):
    """write_file pipes content over SSH stdin (no shell ARG_MAX)."""
    ssh_backend._targets["remote"] = _fake_state()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = ssh_backend.write_file("remote", "/tmp/x.txt", b"hello world\n")
    assert result["status"] == "ok"
    assert result["bytes_written"] == 12
    # Verify subprocess.run was called with content as stdin
    call = mock_run.call_args
    assert call.kwargs["input"] == b"hello world\n"
    # The command should set -e + mktemp + cat > + mv
    cmd = call.args[0][-1]  # last positional arg is the bash -c command
    assert "set -e" in cmd
    assert "cat >" in cmd
    assert "mv -f" in cmd


def test_ssh_write_file_stale_connection_returns_guidance(ssh_backend):
    """When the ControlMaster socket is stale, guidance is returned instead
    of proceeding to the write attempt."""
    ssh_backend._targets["remote"] = _fake_state()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="permission denied",
        )
        result = ssh_backend.write_file("remote", "/tmp/x.txt", b"hi")
    assert result.get("error_kind") == "ssh_disconnected"
    assert "connect" in result.get("hint", "").lower()


def test_ssh_base_args_includes_port(ssh_backend):
    """Port from SSHTarget must surface as ``-p`` on every SSH invocation."""
    ssh_backend._targets["remote"] = _fake_state(port=2222)
    args = ssh_backend._ssh_base_args("remote")
    assert "-p" in args
    assert args[args.index("-p") + 1] == "2222"


def test_ssh_base_args_strict_host_key_default_off(ssh_backend):
    """By default ``host_key_check=false`` keeps StrictHostKeyChecking=no."""
    ssh_backend._targets["remote"] = _fake_state()
    args = ssh_backend._ssh_base_args("remote")
    idx = args.index("StrictHostKeyChecking=no")
    assert idx > -1


def test_ssh_base_args_strict_host_key_on(ssh_backend):
    """``host_key_check=true`` switches to StrictHostKeyChecking=yes."""
    ssh_backend._targets["remote"] = _fake_state(host_key_check=True)
    args = ssh_backend._ssh_base_args("remote")
    idx = args.index("StrictHostKeyChecking=yes")
    assert idx > -1


def test_ssh_base_args_includes_key_when_set(ssh_backend, tmp_path):
    key = tmp_path / "id_test"
    key.write_text("dummy")
    ssh_backend._targets["remote"] = _fake_state(key=str(key))
    args = ssh_backend._ssh_base_args("remote")
    assert "-i" in args
    assert args[args.index("-i") + 1] == str(key)


def test_ssh_create_includes_strict_host_key_no_by_default(ssh_backend):
    """The ControlMaster bootstrap (``ssh -M``) inherits StrictHostKeyChecking=no
    from host_key_check=False on the SSHTarget."""
    from sandbox_mcp.config import SSHTarget

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr=b"")
        ssh_backend.create(
            name="remote",
            purpose="build",
            target=SSHTarget(host="10.0.0.1", user="ubuntu"),
        )
    cmd_args = mock_run.call_args.args[0]
    assert "StrictHostKeyChecking=no" in cmd_args


def test_ssh_create_includes_strict_host_key_yes_when_requested(ssh_backend):
    """host_key_check=True on the SSHTarget flips the ControlMaster
    bootstrap to StrictHostKeyChecking=yes."""
    from sandbox_mcp.config import SSHTarget

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr=b"")
        ssh_backend.create(
            name="remote",
            purpose="build",
            target=SSHTarget(host="10.0.0.1", user="ubuntu", host_key_check=True),
        )
    cmd_args = mock_run.call_args.args[0]
    assert "StrictHostKeyChecking=yes" in cmd_args
