"""Tests for the Windows PowerShell encoding helpers.

Cover the table-driven codec mapping plus the SSH probe end-to-end
(mocks ``subprocess.run`` for the probe so tests are hermetic).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sandbox_mcp.encoding_utils import (
    EncodingInfo,
    _parse_probe_lines,
    map_codepage_to_codec,
    probe_remote_encoding,
)


class TestMapCodepage:
    """The mapping is the contract every consumer depends on."""

    @pytest.mark.parametrize(
        "codepage,webname,expected",
        [
            (936, None, "gbk"),
            (936, "gb2312", "gbk"),
            (936, "gbk", "gbk"),
            (936, "cp936", "gbk"),
            (65001, "utf-8", "utf-8"),
            (65001, None, "utf-8"),
            (1200, None, "utf-16-le"),
            (1200, "utf-16", "utf-16-le"),
            (932, "shift_jis", "cp932"),
            (1252, None, "cp1252"),
            (None, None, "gbk"),
            (None, "gb2312", "gbk"),
            ("not-a-number", "utf-8", "utf-8"),
        ],
    )
    def test_known_codepages_and_webnames(self, codepage, webname, expected):
        assert map_codepage_to_codec(codepage, webname) == expected

    def test_unknown_codepage_returns_fallback(self):
        assert map_codepage_to_codec(99999, "weird") == "gbk"
        assert map_codepage_to_codec(99999, "weird", fallback="utf-8") == "utf-8"

    def test_empty_inputs_use_fallback(self):
        assert map_codepage_to_codec("", None) == "gbk"
        assert map_codepage_to_codec(None, "") == "gbk"
        assert map_codepage_to_codec(None, None, fallback="latin-1") == "latin-1"


class TestParseProbeLines:
    """The probe decoder turns PowerShell bytes into a small dict."""

    def test_parses_gbk_codepage_936(self):
        # Real shape: OutputEncoding writes "936\r\n936\r\ngb2312\r\ngb2312\r\n..." in GBK
        raw = "936\r\n936\r\ngb2312\r\ngb2312\r\nGB2312\r\nGB2312\r\n".encode("gbk")
        parsed = _parse_probe_lines(raw)
        assert parsed["output_codepage"] == "936"
        assert parsed["input_codepage"] == "936"
        assert parsed["output_webname"] == "gb2312"
        assert parsed["input_webname"] == "gb2312"

    def test_parses_utf8_codepage_65001(self):
        raw = b"65001\r\n65001\r\nutf-8\r\nutf-8\r\nUnicode\r\nUnicode\r\n"
        parsed = _parse_probe_lines(raw)
        assert parsed["output_codepage"] == "65001"
        assert parsed["input_codepage"] == "65001"
        assert parsed["output_webname"] == "utf-8"


class TestProbeRemoteEncoding:
    """End-to-end probe with mocked subprocess."""

    def test_probe_returns_encoding_info_from_powershell_output(self):
        # Construct a realistic PowerShell probe payload in GBK
        body = "936\r\n936\r\ngb2312\r\ngb2312\r\nGB2312\r\nGB2312\r\n".encode("gbk")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=body, stderr=b"")
            info = probe_remote_encoding(
                ["ssh", "user@host"], connect_timeout=5, fallback_encoding="gbk"
            )

        assert isinstance(info, EncodingInfo)
        assert info.source == "probe"
        assert info.output_codepage == 936
        assert info.input_codepage == 936
        assert info.output_encoding == "gbk"
        assert info.input_encoding == "gbk"

    def test_probe_invokes_ssh_and_powershell(self):
        body = b"65001\r\n65001\r\nutf-8\r\nutf-8\r\nUnicode\r\nUnicode\r\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=body, stderr=b"")
            probe_remote_encoding(["ssh", "u@h"], fallback_encoding="gbk")

        args = mock_run.call_args.args[0]
        assert "ssh" in args[0] or args[0].endswith("ssh")
        assert "u@h" in args
        assert "powershell.exe" in args

    def test_probe_falls_back_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"")
            info = probe_remote_encoding(["u@h"], fallback_encoding="gbk")
        assert info.source in {"config", "default"}
        assert info.output_encoding == "gbk"
        assert info.input_encoding == "gbk"

    def test_probe_falls_back_on_timeout(self):
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            info = probe_remote_encoding(["u@h"], fallback_encoding="gbk")
        assert info.source in {"config", "default"}
        assert info.output_encoding == "gbk"

    def test_probe_falls_back_when_subprocess_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            info = probe_remote_encoding(["u@h"], fallback_encoding="gbk")
        assert info.source in {"config", "default"}
        assert info.output_encoding == "gbk"

    def test_probe_distinct_input_and_output(self):
        # Hypothetical case where input/output differ
        body = "65001\r\n936\r\nutf-8\r\ngb2312\r\nUnicode\r\nGB2312\r\n".encode("gbk")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=body, stderr=b"")
            info = probe_remote_encoding(["u@h"], fallback_encoding="gbk")
        assert info.output_encoding == "utf-8"
        assert info.input_encoding == "gbk"


class TestEncodingInfoDataclass:
    """Light surface checks for the dataclass used in diagnostics."""

    def test_carries_codepages_and_source(self):
        info = EncodingInfo(
            input_encoding="gbk",
            output_encoding="gbk",
            input_codepage=936,
            output_codepage=936,
            source="probe",
        )
        assert info.source == "probe"
        assert info.input_codepage == 936
