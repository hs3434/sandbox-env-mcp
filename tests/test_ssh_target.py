"""Unit tests for the SSHTarget dataclass schema and validation."""

from __future__ import annotations

import dataclasses

import pytest

from sandbox_mcp.config import SSHTarget


def test_ssh_target_minimal_required_fields():
    """Only host+user are required; everything else has a default."""
    t = SSHTarget(host="10.0.0.1", user="ubuntu")
    assert t.port == 22
    assert t.key is None
    assert t.os_type == "linux"
    assert t.purpose == ""
    assert t.shell == ""
    assert t.encoding is None
    assert t.host_key_check is False


def test_ssh_target_all_fields(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("dummy")
    t = SSHTarget(
        host="192.168.1.10",
        user="builder",
        port=2222,
        key=str(key),
        os_type="windows",
        purpose="builds",
        shell="powershell.exe",
        encoding="gbk",
        host_key_check=True,
    )
    assert t.port == 2222
    assert t.os_type == "windows"
    assert t.encoding == "gbk"
    assert t.host_key_check is True


def test_ssh_target_empty_host_rejected():
    with pytest.raises(ValueError, match="host must be non-empty"):
        SSHTarget(host="", user="u")


def test_ssh_target_whitespace_host_rejected():
    with pytest.raises(ValueError, match="host must be non-empty"):
        SSHTarget(host="   ", user="u")


def test_ssh_target_empty_user_rejected():
    with pytest.raises(ValueError, match="user must be non-empty"):
        SSHTarget(host="h", user="")


def test_ssh_target_port_zero_rejected():
    with pytest.raises(ValueError, match="port must be in 1\\.\\.65535"):
        SSHTarget(host="h", user="u", port=0)


def test_ssh_target_port_too_high_rejected():
    with pytest.raises(ValueError, match="port must be in 1\\.\\.65535"):
        SSHTarget(host="h", user="u", port=70000)


def test_ssh_target_port_negative_rejected():
    with pytest.raises(ValueError, match="port must be in 1\\.\\.65535"):
        SSHTarget(host="h", user="u", port=-1)


def test_ssh_target_os_type_must_be_known():
    with pytest.raises(ValueError, match="os_type must be 'linux' or 'windows'"):
        SSHTarget(host="h", user="u", os_type="macos")


def test_ssh_target_os_type_accepts_windows():
    t = SSHTarget(host="h", user="u", os_type="windows")
    assert t.os_type == "windows"


def test_ssh_target_missing_key_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="key path not found"):
        SSHTarget(host="h", user="u", key=str(tmp_path / "missing"))


def test_ssh_target_existing_key_path_accepted(tmp_path):
    k = tmp_path / "id_test"
    k.write_text("dummy")
    t = SSHTarget(host="h", user="u", key=str(k))
    assert t.key == str(k)


def test_ssh_target_key_none_skips_existence_check():
    # key=None should not raise even on a fresh machine with no keys.
    t = SSHTarget(host="h", user="u", key=None)
    assert t.key is None


def test_ssh_target_encoding_empty_string_rejected():
    with pytest.raises(ValueError, match="encoding must be non-empty when set"):
        SSHTarget(host="h", user="u", encoding="")


def test_ssh_target_validation_aggregates_multiple_errors():
    """All violations should be reported together, not just the first one."""
    with pytest.raises(ValueError) as exc:
        SSHTarget(host="", user="", port=0)
    msg = str(exc.value)
    assert "host must be non-empty" in msg
    assert "user must be non-empty" in msg
    assert "port" in msg


def test_ssh_target_is_frozen():
    """frozen=True: attribute reassignment should fail."""
    t = SSHTarget(host="h", user="u")
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        t.host = "other"  # type: ignore[misc]


def test_ssh_target_eq_by_field_values():
    """Same field values → equal (frozen dataclass default)."""
    a = SSHTarget(host="h", user="u", port=22)
    b = SSHTarget(host="h", user="u", port=22)
    assert a == b
    c = SSHTarget(host="h", user="u", port=2222)
    assert a != c
