"""WinRM backend: remote Windows management via PowerShell Remoting.

This backend uses ``pywinrm`` (optional dependency) to connect to remote
Windows machines via WinRM / WS-Management.  Each command is executed as a
one-off PowerShell invocation — WinRM does not support persistent shell
sessions like SSH ControlMaster.

Installation: ``pip install sandbox-mcp[winrm]`` or ``pip install pywinrm``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_provider import ShellProvider
from sandbox_mcp.shell_providers.powershell_provider import PowerShellProvider

if TYPE_CHECKING:
    # Used only for type annotations; lazy-imported at runtime.
    import winrm


def _get_winrm():
    """Lazy import of pywinrm — not available in SSH-only deployments."""
    import winrm

    return winrm


class WinRMSession:
    """Wraps a WinRM session to provide ShellSession-compatible interface.

    WinRM does not support persistent shells like SSH ControlMaster.
    Each command is a new ``run_ps`` call.  This class preserves the
    ``send()`` / ``read()`` / ``close()`` interface so it can be treated
    as a :class:`~sandbox_mcp.shell_session.ShellSession` by callers that
    expect one.
    """

    def __init__(self, session: winrm.Session, machine: str):
        self._session = session
        self._machine = machine
        self._state = "idle"
        self._last_command: str | None = None
        self._last_result: dict | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_command(self) -> str | None:
        return self._last_command

    @property
    def bash_pid(self) -> str | None:
        return f"winrm:{self._machine}"

    def send(self, command: str, wait: bool = True, timeout: float = 30,
             max_output: int = 50000) -> dict:
        self._last_command = command
        self._state = "busy" if wait else "running"
        try:
            result = self._session.run_ps(command)
        except Exception as e:
            self._state = "idle"
            return {"output": "", "exit_code": -1, "status": "error", "error": str(e)}

        stdout = result.std_out.decode("utf-8", errors="replace") if result.std_out else ""
        stderr = result.std_err.decode("utf-8", errors="replace") if result.std_err else ""

        output = stdout
        if stderr:
            output = stderr + "\n" + stdout

        exit_code = result.status_code
        self._state = "idle"
        self._last_result = {
            "output": output[:max_output],
            "exit_code": exit_code,
            "status": "completed",
        }
        return self._last_result

    def read(self) -> dict:
        if self._last_result:
            return self._last_result
        return {"output": "", "status": self._state}

    def write_stdin(self, data: str) -> dict:
        return {"bytes_written": 0, "error": "stdin write not supported over WinRM"}

    def close(self):
        self._state = "terminated"
        self._last_result = None


class WinRMBackend(Backend):
    """Windows remote management backend via WinRM/PowerShell Remoting.

    Creates one-shot PowerShell sessions over WS-Management.  No persistent
    shell is maintained — each ``open_shell`` call returns a
    :class:`WinRMSession` adapter that issues individual ``run_ps`` calls.
    """

    def __init__(self):
        self._targets: dict[str, dict] = {}
        self._sessions: dict[str, object] = {}
        self._provider: dict[str, ShellProvider] = {}

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        """Establish a WinRM connection.

        Required kwargs:
            host: str       — IP or hostname
            user: str       — Username (``DOMAIN\\user`` or local user)
            password: str   — Password

        Optional kwargs:
            port: int       — Default 5985 (HTTP) or 5986 (HTTPS)
            use_ssl: bool   — Use HTTPS
            transport: str  — ``"ntlm"`` (default), ``"kerberos"``,
                              ``"credssp"``, or ``"basic"``
        """
        winrm = _get_winrm()

        host = kwargs.get("host", "")
        user = kwargs.get("user", "")
        password = kwargs.get("password", "")
        use_ssl = kwargs.get("use_ssl", False)
        port = kwargs.get("port", 5986 if use_ssl else 5985)
        transport = kwargs.get("transport", "ntlm")

        if not host or not user:
            return TargetInfo(
                name=name, backend="winrm", status="error",
                purpose=purpose,
                error="host and user are required",
            )

        protocol = "https" if use_ssl else "http"
        url = f"{protocol}://{host}:{port}/wsman"
        auth = (user, password) if transport not in ("kerberos",) else None

        try:
            session = winrm.Session(url, auth=auth, transport=transport)
            test_result = session.run_cmd("echo", ["OK"])
            if test_result.status_code != 0:
                stderr = test_result.std_err.decode("utf-8", errors="replace") if test_result.std_err else ""
                return TargetInfo(
                    name=name, backend="winrm", status="error",
                    purpose=purpose,
                    error=f"WinRM test failed: {stderr}",
                )
        except Exception as e:
            return TargetInfo(
                name=name, backend="winrm", status="error",
                purpose=purpose, error=str(e),
            )

        os_type = kwargs.get("os_type", "windows")
        self._targets[name] = {
            "host": host,
            "user": user,
            "port": port,
            "use_ssl": use_ssl,
            "transport": transport,
            "purpose": purpose,
        }
        self._sessions[name] = session
        self._provider[name] = PowerShellProvider()

        return TargetInfo(
            name=name, backend="winrm", status="running",
            purpose=purpose,
        )

    def start(self, name: str) -> TargetInfo:
        """Reconnect a WinRM target."""
        target = self._targets.get(name, {})
        if not target:
            return TargetInfo(name=name, backend="winrm", status="error")
        return self.create(name, **target)

    def stop(self, name: str) -> TargetInfo:
        """Disconnect the WinRM session."""
        self._sessions.pop(name, None)
        return TargetInfo(name=name, backend="winrm", status="stopped")

    def remove(self, name: str) -> dict:
        self._sessions.pop(name, None)
        self._targets.pop(name, None)
        return {"machine": name, "status": "removed"}

    def get_info(self, name: str) -> TargetInfo:
        session = self._sessions.get(name)
        if not session:
            return TargetInfo(name=name, backend="winrm", status="error")
        try:
            r = session.run_cmd("echo", ["ping"])
            status = "running" if r.status_code == 0 else "error"
        except Exception:
            status = "error"
        target = self._targets.get(name, {})
        return TargetInfo(
            name=name, backend="winrm", status=status,
            purpose=target.get("purpose", ""),
        )

    def open_shell(self, name: str):
        """Return a WinRMSession adapter (no persistent shell).

        Each ``send()`` call issues a new ``run_ps`` invocation.
        """
        session = self._sessions.get(name)
        if not session:
            raise RuntimeError(f"No WinRM session for {name}")
        return WinRMSession(session, name)

    def exec_oneoff(self, name: str, command: str, timeout: float = 30) -> dict:
        session = self._sessions.get(name)
        if not session:
            return {"output": "", "exit_code": -1, "status": "error", "stderr": "no session"}
        try:
            result = session.run_ps(command)
            stdout = result.std_out.decode("utf-8", errors="replace") if result.std_out else ""
            stderr = result.std_err.decode("utf-8", errors="replace") if result.std_err else ""
            return {
                "output": stdout,
                "stderr": stderr,
                "exit_code": result.status_code,
            }
        except Exception as e:
            return {"output": "", "exit_code": -1, "status": "error", "stderr": str(e)}

    def write_file(self, name: str, path: str, content: bytes) -> dict:
        """Write a file via base64-encoded PowerShell script.

        Content is base64-encoded and embedded inline to avoid stdin
        piping issues over WinRM.
        """
        import base64 as b64

        if isinstance(content, str):
            content = content.encode("utf-8")
        encoded = b64.b64encode(content).decode("ascii")

        safe_path = path.replace("'", "''")
        script = (
            f"$t='{safe_path}'; "
            f"$dir=Split-Path $t -Parent; "
            f"if($dir){{New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null}}; "
            f"$bytes=[Convert]::FromBase64String('{encoded}'); "
            f"[IO.File]::WriteAllBytes($t, $bytes); "
            f"Write-Host 'ok'"
        )
        result = self.exec_oneoff(name, script)
        if result.get("exit_code") == 0:
            return {"status": "ok", "path": path, "bytes_written": len(content)}
        return {
            "status": "error",
            "stage": "write",
            "error": result.get("stderr") or result.get("output") or "write failed",
        }
