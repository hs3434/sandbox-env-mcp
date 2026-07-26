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

"""SSH backend: manages remote machines via SSH with ControlMaster (key auth only)."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.config import load as _load_config
from sandbox_mcp.encoding_utils import (
    EncodingInfo,
    probe_remote_encoding,
)
from sandbox_mcp.shell_provider import ShellProvider, ShellProviderFactory
from sandbox_mcp.shell_session import ShellSession


def _find_ssh():
    p = shutil.which("ssh")
    if not p:
        raise RuntimeError("ssh not found on PATH")
    return p


def _probe_ssh_encoding(args_for_ssh: list[str], *, fallback: str = "gbk") -> EncodingInfo:
    """Probe a remote Windows host's native console encoding.

    The probe asks the host for its real ``[Console]::OutputEncoding``
    / ``[Console]::InputEncoding`` — it does NOT issue ``chcp 65001``
    or rewrite the OutputEncoding.  Anything missing falls back to
    *fallback* with ``source="config"`` / ``"default"`` so operators
    can see why a fallback was used.
    """
    ssh_args = [str(a) for a in args_for_ssh if str(a) not in {"true"}]
    return probe_remote_encoding(ssh_args, fallback_encoding=fallback)


def _decode_bytes(data: bytes, codec: str) -> str:
    """Decode *data* with *codec*, replacing undecodable bytes."""
    return data.decode(codec, errors="replace")


class SSHBackend(Backend):
    """SSH remote machine backend with ControlMaster multiplexing."""

    def __init__(self):
        self._ssh = _find_ssh()
        self._targets: dict[str, dict] = {}
        self._provider: dict[str, ShellProvider] = {}

    def _socket_path(self, name):
        target = self._targets.get(name)
        if target is not None and "socket" in target:
            return target["socket"]
        # Per-target socket directory; predictable name but isolated.
        prefix = _load_config().ssh.socket_dir_prefix
        d = tempfile.mkdtemp(prefix=f"{prefix}{name}-")
        return f"{d}/control"

    def _socket_dir(self, name) -> str:
        """Return the parent dir of the control socket, for cleanup on remove()."""
        return os.path.dirname(self._socket_path(name))

    def _ensure_alive(self, name):
        """Check if the ControlMaster socket is alive; reconnect if stale.

        SSH connections can drop due to network issues, server restarts,
        or idle timeouts exceeding ``ControlPersist``.  This method is
        called before every operation so callers never see a stale socket.
        """
        target = self._targets.get(name)
        if target is None:
            return
        socket = self._socket_path(name)
        user = target.get("user", "")
        host = target.get("host", "")
        try:
            check = subprocess.run(
                [self._ssh, "-S", socket, "-O", "check", f"{user}@{host}"],
                capture_output=True,
                timeout=10,
            )
            if check.returncode == 0:
                return  # Still alive
        except Exception:
            pass

        # Stale — try reconnect.  Machine stays registered regardless
        # of outcome so the agent can see the "disconnected" status.
        keys = {"host", "user", "port", "key", "os_type", "encoding"}
        self.create(name, **{k: v for k, v in target.items() if k in keys})

    def _ssh_base_args(self, name):
        target = self._targets.get(name)
        if target is None:
            raise RuntimeError(f"Unknown SSH target: {name}")
        socket = self._socket_path(name)
        connect_timeout = _load_config().ssh.connect_timeout
        args = [
            self._ssh,
            "-o",
            f"ControlPath={socket}",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={connect_timeout}",
        ]
        port = target.get("port", 22)
        args += ["-p", str(port)]
        key = target.get("key")
        if key:
            args += ["-i", key]
        user = target.get("user", "")
        host = target.get("host", "")
        args.append(f"{user}@{host}" if user else host)
        return args

    def create(self, name, purpose="", **kwargs):
        host = kwargs.get("host", "")
        user = kwargs.get("user", "")
        port = kwargs.get("port", 22)
        key = kwargs.get("key")
        os_type = kwargs.get("os_type", "linux")
        connect_timeout = _load_config().ssh.connect_timeout

        cmd = [
            self._ssh,
            "-M",
            "-S",
            self._socket_path(name),
            "-o",
            "ControlPersist=300",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-p",
            str(port),
        ]
        if key:
            cmd += ["-i", key]
        cmd.append(f"{user}@{host}")
        cmd.append("true")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return TargetInfo(
                name=name,
                backend="ssh",
                status="error",
                purpose=purpose,
                error=f"SSH connection to {user}@{host}:{port} timed out",
            )
        except FileNotFoundError:
            return TargetInfo(
                name=name,
                backend="ssh",
                status="error",
                purpose=purpose,
                error="ssh binary not found on PATH",
            )
        if result.returncode != 0:
            err = (
                result.stderr or ""
            ).strip() or f"ssh connection to {user}@{host}:{port} failed (exit {result.returncode})"
            return TargetInfo(name=name, backend="ssh", status="error", purpose=purpose, error=err)

        encoding_info = self._resolve_encoding(name, **kwargs)
        encoding = encoding_info.output_encoding

        self._targets[name] = {
            "host": host,
            "user": user,
            "port": port,
            "key": key,
            "os_type": os_type,
            "encoding": encoding,
            "input_encoding": encoding_info.input_encoding,
            "output_encoding": encoding_info.output_encoding,
            "input_codepage": encoding_info.input_codepage,
            "output_codepage": encoding_info.output_codepage,
            "encoding_source": encoding_info.source,
            "socket": self._socket_path(name),
            "socket_dir": self._socket_dir(name),
            "purpose": purpose,
            "started_at": time.time(),
        }
        provider_kwargs = (
            {
                "input_encoding": encoding_info.input_encoding,
                "output_encoding": encoding_info.output_encoding,
            }
            if os_type == "windows"
            else {}
        )
        self._provider[name] = ShellProviderFactory.create(os_type, **provider_kwargs)
        return TargetInfo(name=name, backend="ssh", status="running", purpose=purpose)

    def _resolve_encoding(self, name: str, **kwargs) -> EncodingInfo:
        """Pick codecs for *name*: probe when remote is Windows,
        config-overridable, fall back to defaults on failure.

        * ``os_type != "windows"`` → utf-8 on both sides, source
          ``"default"`` (no probe is even attempted).
        * ``os_type == "windows"`` + probe succeeds → use probe, source
          ``"probe"``.
        * ``os_type == "windows"`` + probe fails + explicit
          ``encoding=`` in kwargs → use it, source ``"config"``.
        * ``os_type == "windows"`` + probe fails + no explicit
          encoding → ``gbk`` on both sides, source ``"default"``.
        """
        os_type = kwargs.get("os_type", "linux")

        if os_type != "windows":
            return EncodingInfo(
                input_encoding="utf-8",
                output_encoding="utf-8",
                source="default",
            )

        target = self._targets.get(name) or {}
        probe_args = [
            self._ssh,
            "-S",
            self._socket_path(name),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={_load_config().ssh.connect_timeout}",
            "-p",
            str(target.get("port") or kwargs.get("port") or 22),
        ]
        key = target.get("key") or kwargs.get("key")
        if key:
            probe_args += ["-i", str(key)]
        probe_args.append(
            f"{target.get('user') or kwargs.get('user', '')}"
            f"@{target.get('host') or kwargs.get('host', '')}"
        )
        fallback = str(kwargs.get("encoding") or "gbk")

        info = _probe_ssh_encoding(probe_args, fallback=fallback)
        if info.source == "probe":
            return info
        # Probe failed — explicitly respect an operator-provided encoding
        explicit = kwargs.get("encoding")
        if explicit:
            return EncodingInfo(
                input_encoding=explicit,
                output_encoding=explicit,
                source="config",
            )
        return info

    def start(self, name):
        """Reconnect SSH ControlMaster."""
        target = self._targets.get(name, {})
        keys = {"host", "user", "port", "key", "os_type", "encoding"}
        return self.create(name, **{k: v for k, v in target.items() if k in keys})

    def stop(self, name):
        """Close the SSH master connection."""
        if name not in self._targets:
            return TargetInfo(name=name, backend="ssh", status="error")
        socket = self._socket_path(name)
        target = self._targets.get(name, {})
        user = target.get("user", "")
        host = target.get("host", "")
        try:
            result = subprocess.run(
                [self._ssh, "-S", socket, "-O", "exit", f"{user}@{host}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return TargetInfo(name=name, backend="ssh", status="error")
        if result.returncode != 0:
            return TargetInfo(
                name=name,
                backend="ssh",
                status="error",
                error=result.stderr.strip() or "ssh exit failed",
            )
        return TargetInfo(name=name, backend="ssh", status="stopped")

    def remove(self, name):
        if name in self._targets:
            # Clean up the per-target control-socket directory created by
            # ``tempfile.mkdtemp`` in ``_socket_path``.  Without this, a
            # long-running server leaks one dir + control socket per
            # SSH target it creates.
            socket_dir = self._targets[name].get("socket_dir")
            with contextlib.suppress(Exception):
                self.stop(name)
            self._targets.pop(name, None)
            if socket_dir:
                with contextlib.suppress(Exception):
                    shutil.rmtree(socket_dir, ignore_errors=True)
        return {"machine": name, "status": "removed"}

    def get_info(self, name):
        target = self._targets.get(name)
        if not target:
            return TargetInfo(name=name, backend="ssh", status="error")
        socket = self._socket_path(name)
        user = target.get("user", "")
        host = target.get("host", "")

        # Check if the ControlMaster socket is still alive.
        try:
            check = subprocess.run(
                [self._ssh, "-S", socket, "-O", "check", f"{user}@{host}"],
                capture_output=True,
                timeout=10,
            )
            if check.returncode == 0:
                return TargetInfo(
                    name=name,
                    backend="ssh",
                    status="running",
                    purpose=target.get("purpose", ""),
                )
            # Socket exists but is stale -> connection dropped.
            return TargetInfo(
                name=name,
                backend="ssh",
                status="disconnected",
                purpose=target.get("purpose", ""),
                error="SSH connection dropped. Next operation will auto-reconnect.",
            )
        except FileNotFoundError:
            return TargetInfo(
                name=name,
                backend="ssh",
                status="error",
                purpose=target.get("purpose", ""),
                error="ssh binary not found",
            )
        except subprocess.TimeoutExpired:
            return TargetInfo(
                name=name,
                backend="ssh",
                status="disconnected",
                purpose=target.get("purpose", ""),
                error="SSH socket check timed out",
            )

    def open_shell(self, name):
        self._ensure_alive(name)
        dead = self._check_alive(name)
        if dead:
            raise RuntimeError(dead["hint"])
        provider = self._provider.get(name, ShellProviderFactory.create("linux"))
        if provider.default_shell == "powershell.exe":
            shell_args = [
                *self._ssh_base_args(name),
                *provider.default_shell_args,
                "-NoExit",
                "-File",
                "-",
            ]
        else:
            interactive_args = [a for a in provider.default_shell_args if a != "-NonInteractive"]
            shell_args = [*self._ssh_base_args(name), "-tt", *interactive_args]
        return ShellSession(shell_args, provider=provider)

    def _check_alive(self, name):
        """Return None if alive, or an error dict with guidance if dead."""
        target = self._targets.get(name)
        if target is None:
            return {
                "exit_code": -1,
                "output": "",
                "stderr": "unknown machine",
                "error_kind": "ssh_disconnected",
                "hint": "Use connect(name) to reconnect.",
            }
        socket = self._socket_path(name)
        user = target.get("user", "")
        host = target.get("host", "")
        try:
            check = subprocess.run(
                [self._ssh, "-S", socket, "-O", "check", f"{user}@{host}"],
                capture_output=True,
                timeout=10,
            )
            if check.returncode == 0:
                return None
        except Exception:
            pass
        return {
            "exit_code": -1,
            "output": "",
            "stderr": "SSH connection lost",
            "error_kind": "ssh_disconnected",
            "hint": "Connection dropped. Run connect(name) to reconnect, "
            "then shell_remove to clean up stale shells.",
        }

    def exec_oneoff(self, name, command, timeout=30):
        self._ensure_alive(name)
        dead = self._check_alive(name)
        if dead:
            return dead
        target = self._targets.get(name, {})
        provider = self._provider.get(name, ShellProviderFactory.create("linux"))
        output_encoding = target.get("output_encoding") or provider.output_encoding
        try:
            if provider.default_shell == "powershell.exe":
                import base64

                encoded = base64.b64encode(command.encode("utf-16-le")).decode()
                args = [
                    *self._ssh_base_args(name),
                    *provider.default_shell_args,
                    "-EncodedCommand",
                    encoded,
                ]
            else:
                args = [
                    *self._ssh_base_args(name),
                    *provider.default_shell_args,
                    provider.exec_flag,
                    command,
                ]
            result = subprocess.run(args, capture_output=True, text=False, timeout=timeout)
            stdout_bytes = result.stdout or b""
            stderr_bytes = result.stderr or b""
            return {
                "exit_code": result.returncode,
                "output": _decode_bytes(stdout_bytes, output_encoding),
                "stderr": _decode_bytes(stderr_bytes, output_encoding),
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "output": "", "stderr": "timeout"}

    def write_file(self, name, path, content):
        """Atomic write by streaming content through SSH stdin.

        Content goes directly over the SSH channel to a remote
        ``cat > tmp; mv -f tmp path`` script, bypassing the command-line
        ARG_MAX limit entirely. The remote ``set -e`` ensures the script
        aborts on any error.

        ``content`` is forwarded as-is — the caller's bytes must match
        the remote console's InputEncoding (GBK on Chinese Windows,
        UTF-8 on Linux/macOS).  Sandbox-mcp does NOT re-encode here,
        because re-encoding a string assumed by the caller would lose
        information on code pages not supported by Python's codecs.
        """
        import os as _os

        provider = self._provider.get(name, ShellProviderFactory.create("linux"))

        parent = _os.path.dirname(path) or "/"
        if parent != "/":
            mkdir = self.exec_oneoff(name, provider.mkdir_command(parent))
            if mkdir.get("exit_code") not in (0, None):
                result = {
                    "status": "error",
                    "stage": "mkdir",
                    "error": mkdir.get("stderr") or "mkdir failed",
                }
                if mkdir.get("error_kind"):
                    result["error_kind"] = mkdir["error_kind"]
                if mkdir.get("hint"):
                    result["hint"] = mkdir["hint"]
                return result

        script = provider.atomic_write_script(path)
        try:
            if provider.default_shell == "powershell.exe":
                import base64

                encoded = base64.b64encode(script.encode("utf-16-le")).decode()
                args = [
                    *self._ssh_base_args(name),
                    *provider.default_shell_args,
                    "-EncodedCommand",
                    encoded,
                ]
            else:
                args = [
                    *self._ssh_base_args(name),
                    *provider.default_shell_args,
                    provider.exec_flag,
                    script,
                ]
            result = subprocess.run(
                args, input=content, capture_output=True, text=False, timeout=60
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "stage": "write", "error": "timeout"}
        if result.returncode != 0:
            err_msg = result.stderr or result.stdout or b"atomic write failed"
            try:
                err_msg = err_msg.decode(provider.output_encoding, errors="replace")
            except Exception:
                err_msg = repr(err_msg)
            return {
                "status": "error",
                "stage": "write",
                "error": err_msg,
            }
        return {"status": "ok", "path": path, "bytes_written": len(content)}
