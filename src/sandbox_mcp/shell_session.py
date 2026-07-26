from __future__ import annotations

import contextlib
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
import uuid
from collections import deque

from sandbox_mcp.config import load as _load_config
from sandbox_mcp.shell_providers.bash_provider import BashShellProvider


class ShellUnhealthy(Exception):
    pass


def _health_check(session) -> None:
    result = session.send("true", wait=True, timeout=10)
    if session.state == "terminated":
        raise ShellUnhealthy("shell died during health check")
    if result.get("status") != "ready":
        raise ShellUnhealthy(f"health check returned status={result.get('status')!r}")


class ShellSession:
    def __init__(self, args=None, process=None, provider=None):
        cfg = _load_config().shell
        self.HEAD_SIZE = cfg.head_size
        self.TAIL_SIZE = cfg.tail_size
        self.DEFAULT_MAX_OUTPUT = cfg.default_max_output
        self._args = args
        self._process = process
        self._external = process is not None
        self._provider = provider or BashShellProvider()
        self._lock = threading.Lock()
        self._state = "ready"
        self._last_command = None
        self._started_at = time.time()
        self._head = bytearray()
        self._tail = deque(maxlen=self.TAIL_SIZE)
        self._head_done = False
        self._prompt_token = f"__SANDBOX_PROMPT_{uuid.uuid4().hex}__"
        self._prompt_re = re.compile(
            re.escape(self._prompt_token.encode(self._provider.output_encoding)) + rb":(-?\d+)\|"
        )
        self._prompt_event = threading.Event()
        self._pending_exit_code = None
        self._pending_marker = None
        self._use_prompt = self._provider.uses_prompt
        self._drain_thread = None
        self.exit_reason = "unknown"
        self.last_exit_code = None
        self._start()

    def _start(self):
        if not self._external:
            master, slave = pty.openpty()
            self._process = subprocess.Popen(
                self._args,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave)
            self._process.stdin = os.fdopen(os.dup(master), "wb", buffering=0)
            self._process.stdout = os.fdopen(master, "rb", buffering=0)
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()
        setup = self._provider.prompt_setup_command(self._prompt_token)
        if setup:
            try:
                self._process.stdin.write((setup + "\n").encode(self._provider.input_encoding))
                self._process.stdin.flush()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    raw = bytes(self._head) + bytes(self._tail)
                    if b"SETUP_OK" in raw:
                        self._clear_buffer()
                        self._state = "ready"
                        break
                    time.sleep(0.05)
            except (BrokenPipeError, OSError):
                self._state = "terminated"
        else:
            self._state = "ready"

    _ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][0-9;]*[^\a]*\a")

    @classmethod
    def _strip_ansi(cls, data: bytes) -> bytes:
        cleaned = cls._ANSI_RE.sub(b"", data)
        return cleaned.replace(b"\r", b"\n")

    def _drain(self):
        stdout = self._process.stdout
        carry = b""
        while True:
            try:
                select.select([stdout], [], [])
                data = os.read(stdout.fileno(), 4096)
            except (ValueError, OSError):
                break
            if not data:
                break
            combined = carry + data
            stripped = self._strip_ansi(combined)
            last = 0
            for match in self._prompt_re.finditer(stripped):
                self._store_output(stripped[last : match.start()])
                self._pending_exit_code = int(match.group(1))
                self._prompt_event.set()
                last = match.end()
            if last:
                carry = stripped[last:]
            elif self._pending_marker is not None:
                marker_text = f"__END_{self._pending_marker}__:"
                marker_bytes = marker_text.encode()
                needle = b"\n" + marker_bytes
                idx = stripped.find(needle)
                if idx < 0 and stripped.startswith(marker_bytes):
                    idx = 0
                if idx >= 0:
                    tl = idx + len(needle) if idx > 0 else len(marker_bytes)
                    self._store_output(stripped[:idx])
                    rest = stripped[tl:]
                    m = re.match(rb"(-?\d+)", rest)
                    self._pending_exit_code = int(m.group(1)) if m else 0
                    self._prompt_event.set()
                    carry = b""
                else:
                    keep = min(len(stripped), len(self._pending_marker or "") + 16)
                    if len(stripped) > keep:
                        self._store_output(stripped[:-keep])
                        carry = stripped[-keep:]
                    else:
                        carry = stripped
            else:
                keep = min(len(stripped), len(self._prompt_token) + 16)
                if len(stripped) > keep:
                    self._store_output(stripped[:-keep])
                    carry = stripped[-keep:]
                else:
                    carry = stripped
        if carry:
            self._store_output(carry)
        proc = self._process
        rc = proc.poll() if proc is not None else None
        if rc is not None:
            if rc < 0:
                self.exit_reason = "signal"
                self.last_exit_code = -rc
            else:
                self.exit_reason = "exit"
                self.last_exit_code = rc
        self._state = "terminated"
        self._prompt_event.set()

    def _clear_buffer(self):
        self._head = bytearray()
        self._tail = deque(maxlen=self.TAIL_SIZE)
        self._head_done = False

    def _store_output(self, data):
        if not data:
            return
        if not self._head_done:
            remaining = self.HEAD_SIZE - len(self._head)
            self._head.extend(data[:remaining])
            data = data[remaining:]
            if data or remaining == 0:
                self._head_done = True
        self._tail.extend(data)

    def _with_pid(self, result):
        if self.bash_pid is not None:
            result["bash_pid"] = self.bash_pid
        return result

    def send(self, command, wait=True, timeout=10, max_output=None):
        if max_output is None:
            max_output = self.DEFAULT_MAX_OUTPUT
        with self._lock:
            if self._state == "terminated":
                return self._with_pid(
                    {
                        "output": "",
                        "exit_code": None,
                        "status": "error",
                        "error": "Shell terminated. shell_remove + shell_new.",
                    }
                )
            if self._state == "init":
                return self._with_pid(
                    {
                        "output": "",
                        "exit_code": None,
                        "status": "error",
                        "error": "Shell initializing. Retry in a moment.",
                    }
                )
            if self._state == "waiting":
                return self._with_pid(
                    {
                        "output": "",
                        "exit_code": None,
                        "status": "error",
                        "error": "Shell waiting. Use shell_read/write_stdin/Ctrl-C.",
                    }
                )
            self._clear_buffer()
            self._prompt_event.clear()
            self._pending_exit_code = None
            self._last_command = command
            self._state = "waiting"
            try:
                if self._use_prompt:
                    cmd_bytes = (command + "\n").encode(self._provider.input_encoding)
                else:
                    marker = uuid.uuid4().hex
                    self._pending_marker = marker
                    cmd_bytes = (
                        f"echo __START_{marker}__\n{command}\necho __END_{marker}__:$LASTEXITCODE\n"
                    ).encode(self._provider.input_encoding)
                self._process.stdin.write(cmd_bytes)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._state = "terminated"
                self.exit_reason = "broken_pipe"
                return self._with_pid({"output": "", "exit_code": None, "status": "terminated"})
        if not wait:
            return self._with_pid({"status": "waiting"})
        if self._prompt_event.wait(timeout=timeout):
            if self._state == "terminated":
                return self._with_pid(
                    {
                        "output": self._get_buffered_output(max_output),
                        "status": "terminated",
                    }
                )
            self._state = "ready"
            return self._with_pid(
                {
                    "output": self._get_buffered_output(max_output, command),
                    "exit_code": self._pending_exit_code,
                    "status": "ready",
                }
            )
        return self._with_pid(
            {
                "output": self._get_buffered_output(max_output, command),
                "exit_code": None,
                "status": "waiting",
                "hint": "Command still running; use wait=false + shell_read.",
            }
        )

    def read(self):
        with self._lock:
            output = self._get_buffered_output(self.DEFAULT_MAX_OUTPUT, self._last_command)
            if self._state == "terminated":
                return self._with_pid({"output": output, "status": "terminated"})
            if self._state == "init":
                return self._with_pid({"output": output, "status": "init"})
            if self._prompt_event.is_set():
                self._state = "ready"
                return self._with_pid(
                    {
                        "output": output,
                        "exit_code": self._pending_exit_code,
                        "status": "ready",
                    }
                )
            return self._with_pid({"output": output, "status": self._state})

    def _decode_bytes(self, data: bytes) -> str:
        return data.decode(self._provider.output_encoding, errors="replace")

    _MARKER_RE = re.compile(rb"__START_\w+__\s*|__END_\w+__:\d+\s*")

    def _get_buffered_output(self, max_output, command=None):
        raw = bytes(self._head) + bytes(self._tail)
        raw = self._MARKER_RE.sub(b"", raw)
        full = self._decode_bytes(raw).replace("\r", "")
        if command and full.startswith(command + "\n"):
            full = full[len(command) + 1 :]
        full = full.strip("\n")
        if len(full) <= max_output:
            return full
        truncated = full[-max_output:]
        return f"[Output truncated: showing last {max_output} of {len(full)} chars]\n{truncated}"

    def write_stdin(self, data):
        if self._state == "terminated":
            return {"bytes_written": 0, "error": "Shell is terminated"}
        try:
            encoded = data.encode(self._provider.input_encoding)
            self._process.stdin.write(encoded)
            self._process.stdin.flush()
            return {"bytes_written": len(encoded)}
        except (BrokenPipeError, OSError) as exc:
            self._state = "terminated"
            return {"bytes_written": 0, "error": str(exc)}

    def close(self):
        self._state = "terminated"
        proc = self._process
        if proc is not None:
            try:
                if not self._external and proc.pid is not None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
            for stream in (
                getattr(proc, "stdin", None),
                getattr(proc, "stdout", None),
            ):
                with contextlib.suppress(Exception):
                    stream.close()
        self._prompt_event.set()
        if self._drain_thread:
            self._drain_thread.join(timeout=2)

    @property
    def state(self):
        return self._state

    @property
    def bash_pid(self):
        proc = self._process
        return getattr(proc, "exec_id", None) or getattr(proc, "pid", None) if proc else None

    @property
    def last_command(self):
        return self._last_command

    @property
    def uptime(self):
        return time.time() - self._started_at
