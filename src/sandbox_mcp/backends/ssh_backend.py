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
import shlex
import shutil
import subprocess
import tempfile
import time

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.config import load as _load_config
from sandbox_mcp.shell_provider import ShellProvider, ShellProviderFactory
from sandbox_mcp.shell_session import ShellSession


def _find_ssh():
    p = shutil.which("ssh")
    if not p:
        raise RuntimeError("ssh not found on PATH")
    return p


class SSHBackend(Backend):
    """SSH remote machine backend with ControlMaster multiplexing."""

    def __init__(self):
        self._ssh = _find_ssh()
        self._targets: dict[str, dict] = {}
        self._shell: dict[str, str] = {}
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

        # Stale — reconnect transparently.
        keys = {"host", "user", "port", "key", "os_type"}
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
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return TargetInfo(name=name, backend="ssh", status="error", purpose=purpose)
        if result.returncode != 0:
            return TargetInfo(name=name, backend="ssh", status="error", purpose=purpose)
        self._targets[name] = {
            "host": host,
            "user": user,
            "port": port,
            "key": key,
            "os_type": kwargs.get("os_type", "linux"),
            "socket": self._socket_path(name),
            "socket_dir": self._socket_dir(name),
            "purpose": purpose,
            "started_at": time.time(),
        }
        os_type = kwargs.get("os_type", "linux")
        self._provider[name] = ShellProviderFactory.create(os_type)
        self._shell[name] = kwargs.get("shell", "powershell.exe" if os_type == "windows" else "bash")
        return TargetInfo(name=name, backend="ssh", status="running", purpose=purpose)

    def start(self, name):
        """Reconnect SSH ControlMaster."""
        target = self._targets.get(name, {})
        keys = {"host", "user", "port", "key", "os_type"}
        return self.create(
            name, **{k: v for k, v in target.items() if k in keys}
        )

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
        try:
            result = subprocess.run(
                [*self._ssh_base_args(name), "true"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            status = "running" if result.returncode == 0 else "error"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            status = "error"
        return TargetInfo(
            name=name, backend="ssh", status=status, purpose=target.get("purpose", "")
        )

    def open_shell(self, name):
        self._ensure_alive(name)
        provider = self._provider.get(name, ShellProviderFactory.create("linux"))
        shell_args = [*self._ssh_base_args(name), *provider.default_shell_args]
        return ShellSession(shell_args, provider=provider)

    def exec_oneoff(self, name, command, timeout=30):
        self._ensure_alive(name)
        provider = self._provider.get(name, ShellProviderFactory.create("linux"))
        try:
            result = subprocess.run(
                [*self._ssh_base_args(name), *provider.default_shell_args, provider.exec_flag, command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": result.returncode,
                "output": result.stdout or "",
                "stderr": result.stderr or "",
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "output": "", "stderr": "timeout"}

    def write_file(self, name, path, content):
        """Atomic write by streaming content through SSH stdin.

        Content goes directly over the SSH channel to a remote
        ``cat > tmp; mv -f tmp path`` script, bypassing the command-line
        ARG_MAX limit entirely. The remote ``set -e`` ensures the script
        aborts on any error.
        """
        import os as _os

        provider = self._provider.get(name, ShellProviderFactory.create("linux"))

        parent = _os.path.dirname(path) or "/"
        if parent != "/":
            mkdir = self.exec_oneoff(name, provider.mkdir_command(parent))
            if mkdir.get("exit_code") not in (0, None):
                return {
                    "status": "error",
                    "stage": "mkdir",
                    "error": mkdir.get("stderr") or "mkdir failed",
                }

        script = provider.atomic_write_script(path)
        try:
            result = subprocess.run(
                [*self._ssh_base_args(name), *provider.default_shell_args, provider.exec_flag, script],
                input=content,
                capture_output=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "stage": "write", "error": "timeout"}
        if result.returncode != 0:
            return {
                "status": "error",
                "stage": "write",
                "error": (result.stderr or result.stdout or "atomic write failed"),
            }
        return {"status": "ok", "path": path, "bytes_written": len(content)}
