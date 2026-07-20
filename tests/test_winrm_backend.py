"""Tests for WinRMBackend (mocked pywinrm, no actual WinRM connection)."""

from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.backends.winrm_backend import WinRMBackend, WinRMSession


@pytest.fixture
def mock_winrm():
    """Mock the entire winrm module so tests don't need pywinrm installed."""
    with patch("sandbox_mcp.backends.winrm_backend._get_winrm") as mock_get:
        mock_winrm = MagicMock()
        mock_get.return_value = mock_winrm

        # Mock Session
        mock_session_cls = MagicMock()
        mock_winrm.Session = mock_session_cls

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        # Mock run_cmd result (for connection test)
        mock_test_result = MagicMock()
        mock_test_result.status_code = 0
        mock_test_result.std_out = b"OK"
        mock_test_result.std_err = b""
        mock_session.run_cmd.return_value = mock_test_result

        yield mock_session, mock_session_cls


class TestWinRMBackend:
    def test_create_success(self, mock_winrm):
        mock_session, _ = mock_winrm
        backend = WinRMBackend()
        info = backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        assert info.name == "win-vm"
        assert info.backend == "winrm"
        assert info.status == "running"
        mock_session.run_cmd.assert_called_once_with("echo", ["OK"])

    def test_create_missing_credentials(self, mock_winrm):
        backend = WinRMBackend()
        info = backend.create(
            name="win-vm", purpose="build",
            host="", user="",
        )
        assert info.status == "error"
        assert "host and user are required" in info.error

    def test_create_connection_failure(self, mock_winrm):
        mock_session, _ = mock_winrm
        mock_session.run_cmd.side_effect = Exception("Connection refused")

        backend = WinRMBackend()
        info = backend.create(
            name="win-vm", purpose="build",
            host="10.0.0.1", user="admin", password="secret",
        )
        assert info.status == "error"
        assert "Connection refused" in info.error

    def test_start_reconnects(self, mock_winrm):
        mock_session, mock_session_cls = mock_winrm
        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        # Start should reconnect
        info = backend.start("win-vm")
        assert info.status == "running"

    def test_stop_disconnects(self, mock_winrm):
        mock_session, _ = mock_winrm
        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        info = backend.stop("win-vm")
        assert info.status == "stopped"

    def test_remove_cleans_up(self, mock_winrm):
        mock_session, _ = mock_winrm
        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        result = backend.remove("win-vm")
        assert result["status"] == "removed"
        assert "win-vm" not in backend._targets

    def test_get_info_running(self, mock_winrm):
        mock_session, _ = mock_winrm
        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        info = backend.get_info("win-vm")
        assert info.status == "running"

    def test_get_info_unknown(self, mock_winrm):
        backend = WinRMBackend()
        info = backend.get_info("nonexistent")
        assert info.status == "error"

    def test_open_shell_returns_wrapped_session(self, mock_winrm):
        mock_session, _ = mock_winrm
        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        shell = backend.open_shell("win-vm")
        assert isinstance(shell, WinRMSession)
        assert shell.bash_pid == "winrm:win-vm"

    def test_open_shell_unknown_raises(self, mock_winrm):
        backend = WinRMBackend()
        with pytest.raises(RuntimeError, match="No WinRM session"):
            backend.open_shell("nonexistent")

    def test_exec_oneoff_success(self, mock_winrm):
        mock_session, _ = mock_winrm
        mock_session.run_ps.return_value.status_code = 0
        mock_session.run_ps.return_value.std_out = b"Hello World"
        mock_session.run_ps.return_value.std_err = b""

        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        result = backend.exec_oneoff("win-vm", "Write-Host 'Hello World'")
        assert result["exit_code"] == 0
        assert "Hello World" in result["output"]

    def test_exec_oneoff_no_session(self, mock_winrm):
        backend = WinRMBackend()
        result = backend.exec_oneoff("nonexistent", "echo hi")
        assert result["exit_code"] == -1
        assert "no session" in result["stderr"]

    def test_write_file_base64(self, mock_winrm):
        mock_session, _ = mock_winrm
        mock_session.run_ps.return_value.status_code = 0
        mock_session.run_ps.return_value.std_out = b"ok"
        mock_session.run_ps.return_value.std_err = b""

        backend = WinRMBackend()
        backend.create(
            name="win-vm", purpose="build",
            host="192.168.1.100", user="admin", password="secret",
        )
        result = backend.write_file("win-vm", "C:\\workspace\\app.py", b"print('hello')")
        assert result["status"] == "ok"
        assert result["bytes_written"] == 14


class TestWinRMSession:
    """Test the WinRMSession adapter (ShellSession-compatible wrapper)."""

    @pytest.fixture
    def session(self):
        mock_winrm_session = MagicMock()
        mock_winrm_session.run_ps.return_value.status_code = 0
        mock_winrm_session.run_ps.return_value.std_out = b"completed"
        mock_winrm_session.run_ps.return_value.std_err = b""
        return WinRMSession(mock_winrm_session, "win-vm")

    def test_send_returns_parsed_result(self, session):
        result = session.send("Get-Process powershell")
        assert result["status"] == "completed"
        assert "completed" in result["output"]

    def test_send_error_returns_error_result(self, session):
        session._session.run_ps.side_effect = Exception("timeout")
        result = session.send("bad command")
        assert result["status"] == "error"

    def test_read_after_send(self, session):
        session.send("echo hi")
        result = session.read()
        assert result["status"] == "completed"

    def test_read_initial_state(self, session):
        result = session.read()
        assert result["status"] == "idle"

    def test_write_stdin_not_supported(self, session):
        result = session.write_stdin("data")
        assert result["bytes_written"] == 0
        assert "not supported" in result["error"]

    def test_close_sets_terminated(self, session):
        session.close()
        assert session.state == "terminated"
