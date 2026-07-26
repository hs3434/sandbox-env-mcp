"""Verify SSH backend honors the remote Windows host's native encoding.

* ``create`` for an os_type="windows" target probes the remote
  console's input/output codepage and stores both codecs on the
  target record so subsequent calls (exec_oneoff, write_file,
  ShellSession) can read / write the right bytes.
* ``exec_oneoff`` does NOT use ``text=True`` — it captures raw bytes
  and decodes via the target's ``output_encoding`` (GBK on Chinese
  Windows, UTF-8 / UTF-16-LE elsewhere).
* ``write_file`` forwards the caller's bytes verbatim — the SSH
  channel hands them to ``[Console]::In`` on the remote side which
  decodes with the InputEncoding reported by the probe.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.backends.ssh_backend import SSHBackend


@pytest.fixture
def ssh_backend():
    return SSHBackend()


@pytest.fixture
def gbk_probe():
    """Pretend PowerShell probe answered with CodePage 936 / WebName gb2312."""
    body = "936\r\n936\r\ngb2312\r\ngb2312\r\nGB2312\r\nGB2312\r\n".encode("gbk")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=body, stderr=b"")
        yield mock_run


@pytest.fixture
def utf8_probe():
    body = b"65001\r\n65001\r\nutf-8\r\nutf-8\r\nUnicode\r\nUnicode\r\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=body, stderr=b"")
        yield mock_run


def test_create_windows_target_probes_encoding(ssh_backend, gbk_probe):
    info = ssh_backend.create(
        name="win",
        purpose="builds",
        host="10.0.0.1",
        user="builder",
        os_type="windows",
    )
    assert info.status == "running"
    target = ssh_backend._targets["win"]
    assert target["input_encoding"] == "gbk"
    assert target["output_encoding"] == "gbk"
    assert target["input_codepage"] == 936
    assert target["output_codepage"] == 936
    assert target["encoding_source"] in {"probe", "config", "default"}


def test_create_windows_target_probe_called_with_powershell(ssh_backend, gbk_probe):
    ssh_backend.create(
        name="win",
        purpose="builds",
        host="10.0.0.1",
        user="builder",
        os_type="windows",
    )
    # First subprocess.run invocation is the control-master connect; the
    # second one is the encoding probe.
    assert len(gbk_probe.call_args_list) >= 2
    probe_call = gbk_probe.call_args_list[1]
    args = probe_call.args[0]
    assert any("powershell.exe" in str(a) for a in args)
    # Reading [Console]::OutputEncoding is fine; rewriting it is not.
    # The probe MUST NOT issue a chcp or an assignment to Console encodings.
    script = args[-1]
    assert "chcp" not in script
    assert "[Console]::OutputEncoding =" not in script
    assert "[Console]::InputEncoding =" not in script
    assert "$OutputEncoding" not in script
    assert "PYTHONIOENCODING" not in script
    assert "PSDefaultParameterValues" not in script


def test_create_windows_target_falls_back_when_probe_fails(ssh_backend):
    connect_ok = MagicMock(returncode=0, stdout=b"", stderr=b"")
    probe_fail = MagicMock(returncode=1, stdout=b"", stderr=b"")
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [connect_ok, probe_fail]
        ssh_backend.create(
            name="win",
            purpose="builds",
            host="10.0.0.1",
            user="builder",
            os_type="windows",
        )
    target = ssh_backend._targets["win"]
    # Falls back to the config encoding, which defaults to gbk on Windows.
    assert target["input_encoding"] == "gbk"
    assert target["output_encoding"] == "gbk"
    assert target["encoding_source"] in {"config", "default"}


def test_create_windows_target_uses_config_override_when_probe_fails(ssh_backend):
    connect_ok = MagicMock(returncode=0, stdout=b"", stderr=b"")
    probe_fail = MagicMock(returncode=1, stdout=b"", stderr=b"")
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [connect_ok, probe_fail]
        ssh_backend.create(
            name="win",
            purpose="builds",
            host="10.0.0.1",
            user="builder",
            os_type="windows",
            encoding="utf-8",
        )
    target = ssh_backend._targets["win"]
    assert target["input_encoding"] == "utf-8"
    assert target["output_encoding"] == "utf-8"
    assert target["encoding_source"] == "config"


def test_create_linux_target_does_not_probe(ssh_backend):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        ssh_backend.create(
            name="lin",
            purpose="dev",
            host="10.0.0.2",
            user="ubuntu",
        )
    target = ssh_backend._targets["lin"]
    # Non-Windows targets still get utf-8 stored as default — no probe needed.
    assert target["input_encoding"] == "utf-8"
    assert target["output_encoding"] == "utf-8"


def test_exec_oneoff_uses_raw_bytes_and_decodes_via_output_encoding(ssh_backend, gbk_probe):
    """Real Chinese Windows: stdout bytes are GBK; must be decoded as gbk."""
    ssh_backend.create(
        name="win",
        purpose="builds",
        host="10.100.1.1",
        user="hs3434",
        os_type="windows",
    )
    # Chinese for "hello world"
    gbk_hello = "你好世界".encode("gbk")  # c4 e3 ba c3 ca c0 bd e7
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=gbk_hello + b"\r\n", stderr=b"")
        result = ssh_backend.exec_oneoff("win", "Write-Host '你好世界'")
    assert result["exit_code"] == 0
    assert "你好世界" in result["output"]
    # Confirm subprocess was called with text=False (raw bytes)
    call = mock_run.call_args
    assert call.kwargs.get("text", "MISSING") is False
    assert call.kwargs.get("capture_output") is True


def test_exec_oneoff_decodes_utf8_when_probe_says_so(ssh_backend, utf8_probe):
    ssh_backend.create(
        name="win",
        purpose="builds",
        host="10.100.1.1",
        user="hs3434",
        os_type="windows",
    )
    payload = "héllo".encode()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr=b"")
        result = ssh_backend.exec_oneoff("win", "Write-Host héllo")
    assert "héllo" in result["output"]


def test_exec_oneoff_handles_undecodable_bytes_without_crashing(ssh_backend, gbk_probe):
    """Probe says GBK; one-off returns bytes that aren't valid GBK.

    Defensive: replace undecodable bytes rather than crash, mirroring
    Python's ``errors="replace"`` policy used elsewhere in the
    codebase.
    """
    ssh_backend.create(
        name="win",
        purpose="builds",
        host="10.100.1.1",
        user="hs3434",
        os_type="windows",
    )
    # Random bytes that are not valid GBK
    bogus = b"\xff\xfe\xfd\xfc\xfb"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=bogus, stderr=b"")
        result = ssh_backend.exec_oneoff("win", "anything")
    assert result["exit_code"] == 0
    assert isinstance(result["output"], str)


def test_write_file_does_not_touch_encoding_and_streams_bytes(ssh_backend, gbk_probe):
    ssh_backend.create(
        name="win",
        purpose="builds",
        host="10.100.1.1",
        user="hs3434",
        os_type="windows",
    )
    # Chinese content encoded as GBK bytes (caller's responsibility)
    content = "你好世界".encode("gbk")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        result = ssh_backend.write_file("win", "C:\\tmp\\hi.txt", content)
    assert result["status"] == "ok"
    assert result["bytes_written"] == len(content)
    call = mock_run.call_args
    # Must NOT re-encode — bytes the caller gave us are bytes the remote reads.
    assert call.kwargs["input"] == content
