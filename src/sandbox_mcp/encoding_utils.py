"""Encoding helpers for the PowerShell-over-SSH path.

Remote Windows hosts report their console codepage / web-name rather than
the Python codec sandbox-mcp should use.  This module turns those
declarations into codec names, probes a remote SSH host for its real
console encoding, and surfaces the result (with the source) for
diagnostic tools.

The probe respects the remote host's native encoding — no ``chcp`` or
``[Console]::OutputEncoding`` overrides are issued.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from dataclasses import dataclass

_CP_TO_CODEC: dict[int, str] = {
    65001: "utf-8",
    1200: "utf-16-le",
    1201: "utf-16-be",
    1252: "cp1252",
    932: "cp932",
    936: "gbk",
}


@dataclass(frozen=True)
class EncodingInfo:
    """Resolved codec pair for a Windows PowerShell-over-SSH target.

    ``input_encoding`` is used to encode bytes written to the remote
    stdin (via ``-Command`` argv or PTY); ``output_encoding`` decodes
    bytes received on stdout / PTY output.  ``source`` records how the
    codecs were picked (``"probe"``, ``"config"``, ``"default"``) so
    operators can see why a fallback was used.
    """

    input_encoding: str
    output_encoding: str
    input_codepage: int | None = None
    output_codepage: int | None = None
    source: str = "default"


def map_codepage_to_codec(
    codepage: int | str | None,
    web_name: str | None = None,
    fallback: str = "gbk",
) -> str:
    """Map a Windows CodePage number or WebName to a Python codec.

    CodePage numbers (936, 65001, 1200, ...) are preferred because they
    are a single integer.  When the probe yields a WebName (``gb2312``,
    ``utf-8``, ...), we still normalize the common ones.  ``fallback``
    is returned when both signals are missing or unknown.
    """

    if codepage is not None and codepage != "":
        try:
            cp_int = int(codepage)
        except (TypeError, ValueError):
            cp_int = None
        if cp_int is not None and cp_int in _CP_TO_CODEC:
            return _CP_TO_CODEC[cp_int]

    if web_name:
        normalized = web_name.strip().lower()
        if normalized in {"gb2312", "gbk", "cp936"}:
            return "gbk"
        if normalized in {"utf-8", "utf8"}:
            return "utf-8"
        if normalized == "utf-16":
            return "utf-16-le"
        if normalized == "shift_jis":
            return "cp932"

    return fallback


_PROBE_SCRIPT = (
    "$global:__sandbox_probe_out = ''; "
    "[Console]::OutputEncoding.CodePage | Write-Host; "
    "[Console]::InputEncoding.CodePage | Write-Host; "
    "[Console]::OutputEncoding.WebName | Write-Host; "
    "[Console]::InputEncoding.WebName | Write-Host; "
    "[Console]::OutputEncoding.EncodingName | Write-Host; "
    "[Console]::InputEncoding.EncodingName | Write-Host"
)


def _parse_probe_lines(raw: bytes) -> dict[str, str]:
    """Walk ``raw`` and yield printable lines.

    PowerShell's ``Write-Host`` writes OutputEncoding-coded bytes
    followed by ``\\r\\n``.  We decode conservatively: try the supplied
    candidate codec, fall back to GBK, then Latin-1 — and split on
    any combination of CR / LF.
    """

    text: str = ""
    for codec in ("utf-8", "gbk", "cp1252"):
        with contextlib.suppress(Exception):
            text = raw.decode(codec)
            break
    else:
        text = raw.decode("latin-1")

    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", text) if line.strip()]
    return {
        "output_codepage": lines[0] if len(lines) > 0 else "",
        "input_codepage": lines[1] if len(lines) > 1 else "",
        "output_webname": lines[2] if len(lines) > 2 else "",
        "input_webname": lines[3] if len(lines) > 3 else "",
        "output_encodingname": lines[4] if len(lines) > 4 else "",
        "input_encodingname": lines[5] if len(lines) > 5 else "",
    }


def probe_remote_encoding(
    ssh_args: list[str],
    *,
    connect_timeout: int = 10,
    fallback_encoding: str = "gbk",
) -> EncodingInfo:
    """Run a small PowerShell probe over SSH and decode its output.

    ``ssh_args`` is everything *after* the ``ssh`` binary itself; the
    caller picks the host/user/port/key.  Anything missing or
    unparseable falls back to ``fallback_encoding`` with ``source =
    "config"`` (or ``"default"`` if the fallback is also missing).
    """

    cmd = [
        *ssh_args,
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _PROBE_SCRIPT,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=connect_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return _fallback(fallback_encoding)

    if proc.returncode != 0:
        return _fallback(fallback_encoding)

    lines = _parse_probe_lines(proc.stdout or b"")

    output_codec = map_codepage_to_codec(
        lines.get("output_codepage"), lines.get("output_webname"), fallback_encoding
    )
    input_codec = map_codepage_to_codec(
        lines.get("input_codepage"), lines.get("input_webname"), output_codec
    )

    def _as_int(s: str) -> int | None:
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    return EncodingInfo(
        input_encoding=input_codec,
        output_encoding=output_codec,
        input_codepage=_as_int(lines.get("input_codepage", "")),
        output_codepage=_as_int(lines.get("output_codepage", "")),
        source="probe",
    )


def _fallback(fallback_encoding: str) -> EncodingInfo:
    codec = fallback_encoding or "gbk"
    return EncodingInfo(
        input_encoding=codec,
        output_encoding=codec,
        input_codepage=None,
        output_codepage=None,
        source="default" if not fallback_encoding else "config",
    )
