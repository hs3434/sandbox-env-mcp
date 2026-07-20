# Sandbox MCP Windows Support Plan

> **Goal:** Extend sandbox-mcp to support three Windows access modes:
> 1. **Docker Windows containers** — via `DockerBackend` OS detection + `PowerShellProvider`
> 2. **SSH to remote Windows** — by integrating `SSHBackend` with `ShellProvider`
> 3. **WinRM to remote Windows** — new `WinRMBackend`

**Architecture:** Introduce a `ShellProvider` abstraction layer that generates OS-specific shell commands. `BashShellProvider` preserves current Linux behavior. `PowerShellProvider` generates equivalent PowerShell commands. `ShellSession` gains platform-aware subprocess handling.

**Tech Stack:** Python 3.12+, `pywinrm>=0.5.0` (optional), existing `docker`, `mcp`, `pytest`

---

## File Structure

```
src/sandbox_mcp/
├── shell_provider.py                    [NEW] ShellProvider ABC + factory
├── shell_providers/
│   ├── __init__.py                      [NEW]
│   ├── bash_provider.py                 [NEW] Linux bash commands (extracted from file_operations.py)
│   └── powershell_provider.py           [NEW] Windows PowerShell commands
├── shell_session.py                     [MODIFY] Windows subprocess handling
├── file_operations.py                   [MODIFY] Use ShellProvider instead of inline commands
├── backends/
│   ├── base.py                          [MODIFY] Add ShellProvider field, add TargetInfo.os_type
│   ├── docker_backend.py                [MODIFY] Container OS detection, select provider
│   ├── ssh_backend.py                   [MODIFY] ShellProvider instead of hardcoded POSIX
│   └── winrm_backend.py                 [NEW] WinRM/PowerShell Remoting backend
├── safety.py                            [MODIFY] Add Windows-sensitive paths
└── config.py                            [MODIFY] Add [winrm], os_type, shell config entries
tests/
├── test_shell_provider.py               [NEW]
├── test_powershell_provider.py          [NEW]
├── test_shell_session_windows.py        [NEW]
├── test_winrm_backend.py                [NEW]
└── test_file_operations.py              [MODIFY] Update for ShellProvider
```

---

## Phase 0: ShellProvider ABC + Factory

**Files:**
- Create: `src/sandbox_mcp/shell_provider.py`
- Create: `src/sandbox_mcp/shell_providers/__init__.py`

### Step 1: Define ABC

```python
# src/sandbox_mcp/shell_provider.py
from __future__ import annotations
from abc import ABC, abstractmethod


class ShellProvider(ABC):
    """Generates OS-specific shell commands for file operations and shell management."""

    @property
    @abstractmethod
    def default_shell(self) -> str:
        """Shell binary name: 'bash', 'powershell.exe', etc."""
        ...

    @property
    @abstractmethod
    def default_shell_args(self) -> list[str]:
        """Shell startup arguments, e.g. ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive']"""
        ...

    # ---- File read ----

    @abstractmethod
    def file_read_command(self, path: str, offset: int, limit: int,
                          max_size: int) -> str:
        """Generate a one-shot file read command.
        
        Must return structured output with 4 newline-separated sections:
          line 1: file size in bytes
          line 2: base64 of first 4096 bytes (binary detection)
          line 3: base64 of requested line range
          line 4: total line count
        
        Exit code 2 if file not readable.
        If file_size > max_size, only emit line 1 (size).
        """
        ...

    # ---- File write ----

    @abstractmethod
    def atomic_write_script(self, path: str) -> str:
        """Generate atomic write script.
        
        Reads content from stdin, writes to temp file, mv to target.
        Returns a script string that the backend pipes content into.
        """
        ...

    @abstractmethod
    def mkdir_command(self, parent_dir: str) -> str:
        """Generate mkdir -p command."""
        ...

    # ---- File utilities ----

    @abstractmethod
    def cat_command(self, path: str) -> str:
        """Read entire file content to stdout."""
        ...

    @abstractmethod
    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        """List files in a directory (names only, one per line)."""
        ...

    # ---- Search ----

    @abstractmethod
    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        """ripgrep --files with glob filter and mtime sort."""
        ...

    @abstractmethod
    def search_content_command(self, pattern: str, path: str, file_glob: str,
                                limit: int, output_mode: str,
                                context: int) -> str:
        """ripgrep content search."""
        ...

    # ---- Markers (dual-marker protocol) ----

    @abstractmethod
    def marker_start_command(self, marker_id: str) -> str:
        """Emit __START_{uuid}__ marker. Bash: echo; PS: Write-Host."""
        ...

    @abstractmethod
    def marker_end_command(self, marker_id: str) -> str:
        """Emit __END_{uuid}__:$? marker with exit code."""
        ...

    # ---- Binary utilities ----

    @abstractmethod
    def base64_decode_command(self, encoded: str) -> str:
        """Decode base64 from stdin or inline string."""
        ...

    @abstractmethod
    def patch_apply_command(self) -> str:
        """Apply a unified diff patch from stdin."""
        ...


class ShellProviderFactory:
    """Select ShellProvider by OS type string."""

    _providers: dict[str, type[ShellProvider]] = {}

    @classmethod
    def register(cls, os_type: str, provider_cls: type[ShellProvider]) -> None:
        cls._providers[os_type] = provider_cls

    @classmethod
    def create(cls, os_type: str) -> ShellProvider:
        from sandbox_mcp.shell_providers.bash_provider import BashShellProvider
        from sandbox_mcp.shell_providers.powershell_provider import PowerShellProvider

        if not cls._providers:
            cls.register("linux", BashShellProvider)
            cls.register("windows", PowerShellProvider)

        provider_cls = cls._providers.get(os_type)
        if provider_cls is None:
            raise ValueError(f"Unknown OS type: {os_type!r}. Available: {list(cls._providers)}")
        return provider_cls()
```

### Step 2: Create shell_providers package

```python
# src/sandbox_mcp/shell_providers/__init__.py
"""Shell provider implementations for different operating systems."""
```

- [ ] **Step 3: Run type check on ABC**

```bash
pytest tests/ -k "shell_provider" -v 2>/dev/null || echo "No tests yet — ABC defined, ready for implementation"
```

- [ ] **Step 4: Commit**

```bash
git add src/sandbox_mcp/shell_provider.py src/sandbox_mcp/shell_providers/__init__.py
git commit -m "feat: ShellProvider ABC with factory for cross-platform command generation"
```

---

## Phase 1: BashShellProvider (Refactor from file_operations.py)

**Files:**
- Create: `src/sandbox_mcp/shell_providers/bash_provider.py`
- Create: `tests/test_shell_provider.py` (tests for BashShellProvider)

**Strategy:** Pure refactor — extract all inline bash commands from `file_operations.py` into `BashShellProvider` methods. Zero behavior change. All existing tests must pass after the extraction.

### Step 1: Write failing tests for BashShellProvider

```python
# tests/test_shell_provider.py
import pytest
from sandbox_mcp.shell_provider import ShellProviderFactory
from sandbox_mcp.shell_providers.bash_provider import BashShellProvider


@pytest.fixture
def bash():
    return ShellProviderFactory.create("linux")


class TestBashShellProvider:
    def test_default_shell(self, bash):
        assert bash.default_shell == "bash"

    def test_file_read_command_structure(self, bash):
        cmd = bash.file_read_command("/tmp/test.py", 1, 10, 1048576)
        assert "stat -c %s" in cmd
        assert "base64 -w0" in cmd
        assert "sed -n" in cmd
        assert "wc -l" in cmd
        assert 'exit 2' in cmd  # not-found guard

    def test_file_read_too_large(self, bash):
        cmd = bash.file_read_command("/tmp/big.log", 1, 10, 1000)
        # When file > max_size, only emit size line
        assert "stat" in cmd
        assert "sed" not in cmd

    def test_cat_command(self, bash):
        cmd = bash.cat_command("/tmp/test.py")
        assert "cat" in cmd

    def test_list_dir_command(self, bash):
        cmd = bash.list_dir_command("/workspace")
        assert "ls -1" in cmd
        assert "head -50" in cmd

    def test_mkdir_command(self, bash):
        cmd = bash.mkdir_command("/workspace/sub")
        assert "mkdir -p" in cmd

    def test_atomic_write_script(self, bash):
        script = bash.atomic_write_script("/workspace/app.py")
        assert "mktemp" in script
        assert "cat >" in script
        assert "mv -f" in script
        assert "set -e" in script

    def test_marker_start(self, bash):
        cmd = bash.marker_start_command("abc123")
        assert "echo" in cmd
        assert "__START_abc123__" in cmd

    def test_marker_end(self, bash):
        cmd = bash.marker_end_command("abc123")
        assert "echo" in cmd
        assert "__END_abc123__" in cmd
        assert "$?" in cmd

    def test_search_files_command(self, bash):
        cmd = bash.search_files_command("*.py", "/workspace", 100)
        assert "rg --files" in cmd
        assert "head -n" in cmd

    def test_search_content_command(self, bash):
        cmd = bash.search_content_command("TODO", "/workspace", "*.py", 50, "content", 0)
        assert "rg --line-number" in cmd
        assert "TODO" in cmd
```

### Step 2: Implement BashShellProvider

Extract command strings from `file_operations.py`:

```python
# src/sandbox_mcp/shell_providers/bash_provider.py
from __future__ import annotations
import shlex
from sandbox_mcp.shell_provider import ShellProvider


class BashShellProvider(ShellProvider):
    """Generates bash/shell commands for Linux containers."""
    
    def __init__(self):
        from sandbox_mcp.config import load as _load_config
        self._cfg = _load_config()

    @property
    def default_shell(self) -> str:
        return "bash"

    @property
    def default_shell_args(self) -> list[str]:
        return ["bash"]

    def mkdir_command(self, parent_dir: str) -> str:
        return f"mkdir -p {shlex.quote(parent_dir)}"

    def cat_command(self, path: str) -> str:
        return f"cat {shlex.quote(path)}"

    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        return f"ls -1 {shlex.quote(dir_)} 2>/dev/null | head -{limit}"

    def file_read_command(self, path: str, offset: int, limit: int,
                          max_size: int) -> str:
        q_path = shlex.quote(path)
        q_max_size = shlex.quote(str(max_size))
        end_line = offset + limit - 1
        cfs = self._cfg.files
        return (
            f"f={q_path}; ms={q_max_size}; "
            f'[[ ! -r "$f" ]] && exit 2; '
            f'sz=$(stat -c %s "$f"); echo "$sz"; '
            f"if (( sz <= ms )); then "
            f'head -c 4096 "$f" | base64 -w0; echo; '
            f'sed -n {offset},{end_line}p "$f" | base64 -w0; echo; '
            f"wc -l < \"$f\" | tr -d ' '; "
            f"fi"
        )

    def atomic_write_script(self, path: str) -> str:
        tmp_pattern = self._cfg.ssh.tmpfile_pattern
        return (
            "set -e; "
            f"t={shlex.quote(path)}; "
            f'tmp=$(mktemp -p "${{t%/*}}" {tmp_pattern} 2>/dev/null || '
            f"mktemp {tmp_pattern} 2>/dev/null); "
            '[ -n "$tmp" ] || { echo "atomic write: mktemp failed" >&2; exit 1; }; '
            'cat > "$tmp"; '
            'mv -f "$tmp" "$t"; '
            'rm -f "$tmp"'
        )

    def marker_start_command(self, marker_id: str) -> str:
        return f"echo __START_{marker_id}__"

    def marker_end_command(self, marker_id: str) -> str:
        return f"echo __END_{marker_id}__:$?"

    def base64_decode_command(self, encoded: str) -> str:
        return f"echo {shlex.quote(encoded)} | base64 -d"

    def patch_apply_command(self) -> str:
        return "patch -p0"

    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        glob_pattern = (
            f"*{pattern}" if "/" not in pattern and not pattern.startswith("*") else pattern
        )
        return (
            f"set -o pipefail; "
            f"rg --files --sortr=modified -g {shlex.quote(glob_pattern)} "
            f"{shlex.quote(path)} 2>/dev/null | head -n {limit}"
        )

    def search_content_command(self, pattern: str, path: str, file_glob: str,
                                limit: int, output_mode: str,
                                context: int) -> str:
        q_pattern = shlex.quote(pattern)
        q_path = shlex.quote(path)
        cmd_parts = ["set -o pipefail; rg", "--line-number", "--no-heading", "--with-filename"]
        if context > 0:
            cmd_parts += ["-C", str(context)]
        if file_glob:
            cmd_parts += ["--glob", shlex.quote(file_glob)]
        if output_mode == "files_only":
            cmd_parts.append("-l")
        elif output_mode == "count":
            cmd_parts.append("-c")
        cmd_parts += [q_pattern, q_path, "|", "head", "-n", str(limit)]
        return " ".join(cmd_parts)
```

### Step 3: Wire file_operations.py to use ShellProvider

Replace inline commands with `provider.xxx_command()` calls. Pass `provider` as constructor arg to `FileOperations`.

```python
# In FileOperations.__init__
def __init__(self, backend, provider: ShellProvider | None = None):
    self._backend = backend
    self._provider = provider or ShellProviderFactory.create("linux")

# In FileOperations.read()
# Replace:
#   cmd = (f"f={q_path}; ms={q_max_size}; ...")
# With:
#   cmd = self._provider.file_read_command(path, offset, limit, cfg.max_file_size)
```

- [ ] **Step 4: Run all existing tests**

```bash
pytest tests/ -v
```
Expected: All existing tests pass (zero behavior change).

- [ ] **Step 5: Commit**

```bash
git add src/sandbox_mcp/shell_providers/bash_provider.py tests/test_shell_provider.py
git add src/sandbox_mcp/file_operations.py
git commit -m "feat: BashShellProvider — extract inline bash commands from file_operations.py"
```

---

## Phase 2: PowerShellProvider

**Files:**
- Create: `src/sandbox_mcp/shell_providers/powershell_provider.py`
- Create: `tests/test_powershell_provider.py`

### Step 1: Command mapping design

| Operation | Bash | PowerShell |
|-----------|------|------------|
| File size | `stat -c %s "$f"` | `(Get-Item '$f').Length` |
| Head bytes + base64 | `head -c 4096 "$f" \| base64 -w0` | `[Convert]::ToBase64String([IO.File]::ReadAllBytes('$f')[0..4095])` |
| Line range | `sed -n 1,10p "$f"` | `(Get-Content '$f' -First 10 \| Out-String).TrimEnd() \| base64` |
| Line count | `wc -l < "$f"` | `(Get-Content '$f' \| Measure-Object -Line).Lines` |
| Cat | `cat "$f"` | `Get-Content '$f' -Raw` |
| Ls | `ls -1 "$d" \| head -50` | `Get-ChildItem '$d' -Name \| Select-Object -First 50` |
| Mkdir | `mkdir -p "$d"` | `New-Item -ItemType Directory -Path '$d' -Force \| Out-Null` |
| Atomic write | `mktemp; cat > tmp; mv -f tmp t` | `[Console]::In.ReadToEnd() \| Set-Content; Move-Item -Force` |
| Marker start | `echo __START_xxx__` | `Write-Host '__START_xxx__'` |
| Marker end | `echo __END_xxx__:$?` | `Write-Host \"__END_xxx__:$LASTEXITCODE\"` |
| Base64 decode | `echo encoded \| base64 -d` | `[Convert]::FromBase64String('encoded')` |
| Patch | `patch -p0` | Not available; fall back to Python difflib |

### Step 2: Implementation

```python
# src/sandbox_mcp/shell_providers/powershell_provider.py
from __future__ import annotations
from sandbox_mcp.shell_provider import ShellProvider


class PowerShellProvider(ShellProvider):
    """Generates PowerShell commands for Windows containers and remote Windows machines."""

    _PS = "powershell.exe"
    _PS_ARGS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]

    @property
    def default_shell(self) -> str:
        return self._PS

    @property
    def default_shell_args(self) -> list[str]:
        return self._PS_ARGS

    def _escape_ps_path(self, path: str) -> str:
        """Escape path for single-quoted PowerShell string."""
        return path.replace("'", "''")

    def file_read_command(self, path: str, offset: int, limit: int,
                          max_size: int) -> str:
        p = self._escape_ps_path(path)
        end_line = offset + limit - 1
        return (
            f"$f='{p}'; "
            f"if(-not (Test-Path $f -PathType Leaf)){{exit 2}}; "
            f"$sz=(Get-Item $f).Length; Write-Host $sz; "
            f"if($sz -le {max_size}){{"
            f"  $bytes=[IO.File]::ReadAllBytes($f); "
            f"  $head=$bytes[0..4095]; Write-Host ([Convert]::ToBase64String($head)); "
            f"  $lines=Get-Content $f -TotalCount {end_line} | Select-Object -Skip {offset - 1}; "
            f"  $txt=($lines -join \"`n\"); Write-Host ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($txt))); "
            f"  $lc=(Get-Content $f | Measure-Object -Line).Lines; Write-Host $lc"
            f"}}"
        )

    def atomic_write_script(self, path: str) -> str:
        p = self._escape_ps_path(path)
        return (
            f"$t='{p}'; "
            f"$dir=Split-Path $t -Parent; "
            f"if($dir -and -not (Test-Path $dir)){{New-Item -ItemType Directory -Path $dir -Force | Out-Null}}; "
            f"$tmp=Join-Path $dir \".sandbox_mcp_$(Get-Random -Hex 6)\"; "
            f"$content=[Console]::In.ReadToEnd(); "
            f"[IO.File]::WriteAllText($tmp, $content, [Text.Encoding]::UTF8); "
            f"Move-Item -Force $tmp $t; "
            f"Remove-Item -Force $tmp -ErrorAction SilentlyContinue"
        )

    def cat_command(self, path: str) -> str:
        p = self._escape_ps_path(path)
        return f"Get-Content '{p}' -Raw 2>$null"

    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        d = self._escape_ps_path(dir_)
        return (
            f"Get-ChildItem '{d}' -Name -ErrorAction SilentlyContinue "
            f"| Select-Object -First {limit}"
        )

    def mkdir_command(self, parent_dir: str) -> str:
        d = self._escape_ps_path(parent_dir)
        return f"New-Item -ItemType Directory -Path '{d}' -Force -ErrorAction SilentlyContinue | Out-Null"

    def marker_start_command(self, marker_id: str) -> str:
        return f"Write-Host '__START_{marker_id}__'"

    def marker_end_command(self, marker_id: str) -> str:
        return f"Write-Host \"__END_{marker_id}__:$LASTEXITCODE\""

    def base64_decode_command(self, encoded: str) -> str:
        return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))"

    def patch_apply_command(self) -> str:
        # Windows does not have `patch` natively.
        # FileOperations._patch_apply will detect this absence and fall back
        # to Python difflib on the host side.
        return "patch -p0 2>$null || exit 2"

    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        p = self._escape_ps_path(path)
        return (
            f"$err = $null; "
            f"rg --files --sortr=modified -g '{pattern}' '{p}' 2>&1 | "
            f"Select-Object -First {limit}"
        )

    def search_content_command(self, pattern: str, path: str, file_glob: str,
                                limit: int, output_mode: str,
                                context: int) -> str:
        p = self._escape_ps_path(path)
        q_pattern = pattern.replace("'", "''")
        cmd = "rg --line-number --no-heading --with-filename"
        if context > 0:
            cmd += f" -C {context}"
        if file_glob:
            cmd += f" --glob '{file_glob}'"
        if output_mode == "files_only":
            cmd += " -l"
        elif output_mode == "count":
            cmd += " -c"
        cmd += f" '{q_pattern}' '{p}' | Select-Object -First {limit}"
        return cmd
```

### Step 3: Tests

```python
# tests/test_powershell_provider.py
import pytest
from sandbox_mcp.shell_provider import ShellProviderFactory


@pytest.fixture
def ps():
    return ShellProviderFactory.create("windows")


class TestPowerShellProvider:
    def test_default_shell(self, ps):
        assert "powershell" in ps.default_shell

    def test_file_read_command_structure(self, ps):
        cmd = ps.file_read_command("C:\\workspace\\test.py", 1, 10, 1048576)
        assert "Get-Item" in cmd
        assert "Convert]::ToBase64String" in cmd
        assert "Get-Content" in cmd
        assert "Measure-Object -Line" in cmd

    def test_markers_use_write_host(self, ps):
        start = ps.marker_start_command("abc")
        end = ps.marker_end_command("abc")
        assert "Write-Host" in start
        assert "Write-Host" in end
        assert "LASTEXITCODE" in end

    def test_atomic_write_uses_powershell(self, ps):
        script = ps.atomic_write_script("C:\\workspace\\app.py")
        assert "Move-Item -Force" in script
        assert "Con
ole]::In.ReadToEnd()" in script

    def test_mkdir_command(self, ps):
        cmd = ps.mkdir_command("C:\\workspace\\sub")
        assert "New-Item -ItemType Directory" in cmd

    def test_cat_command(self, ps):
        cmd = ps.cat_command("C:\\workspace\\app.py")
        assert "Get-Content" in cmd
        assert "-Raw" in cmd
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_powershell_provider.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/sandbox_mcp/shell_providers/powershell_provider.py tests/test_powershell_provider.py
git commit -m "feat: PowerShellProvider for Windows command generation"
```

---

## Phase 3: ShellSession Windows Subprocess Adaptation

**Files:**
- Modify: `src/sandbox_mcp/shell_session.py`

### Changes needed

```python
# shell_session.py — _start() method

def _start(self):
    import sys

    kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    self._process = subprocess.Popen(self._args, **kwargs)
    # ... rest unchanged
```

```python
# shell_session.py — close() method

def close(self):
    import sys

    with self._lock:
        self._state = "terminated"
    if self._process:
        try:
            if sys.platform == "win32":
                self._process.kill()  # TerminateProcess
            else:
                if hasattr(self._process, "pid") and self._process.pid is not None:
                    pgid = os.getpgid(self._process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    self._process.kill()
            self._process.wait(timeout=5)
        except Exception:
            pass
        self._process = None
    # ... rest unchanged
```

```python
# shell_session.py — send() method marker generation

# Use the provider's marker commands instead of hardcoded echo:
# Before:
#   full_input = f"echo {start_marker}\n{command}\necho {end_marker}:$?\n"
# After:
#   start_cmd = provider.marker_start_command(marker_id)
#   end_cmd = provider.marker_end_command(marker_id)
#   full_input = f"{start_cmd}\n{command}\n{end_cmd}\n"
```

- [ ] **Commit**

```bash
git add src/sandbox_mcp/shell_session.py
git commit -m "feat: Windows subprocess handling in ShellSession (CREATE_NEW_PROCESS_GROUP)"
```

---

## Phase 4: DockerBackend OS Detection + Windows Container Support

**Files:**
- Modify: `src/sandbox_mcp/backends/docker_backend.py`

### Changes needed

```python
class DockerBackend(Backend):
    def __init__(self):
        # ... existing init ...
        self._provider: dict[str, ShellProvider] = {}

    def _detect_os(self, container) -> str:
        """Detect container OS by inspecting the image's Platform/Os field."""
        docker_cfg = _load_config().docker
        explicit = docker_cfg.os_type
        if explicit:
            return explicit

        # Inspect image metadata
        attrs = container.attrs or {}
        image_os = (attrs.get("Platform", "") or
                    attrs.get("Config", {}).get("Image", ""))
        # Docker SDK: container.image.attrs["Os"]
        try:
            img_attrs = container.image.attrs or {}
            os_field = img_attrs.get("Os", "").lower()
            if os_field == "windows":
                return "windows"
        except Exception:
            pass
        return "linux"

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        # ... existing create logic ...
        # After container is created/reattached:
        container = self._ensure_client().containers.get(name)
        os_type = kwargs.get("os_type") or self._detect_os(container)
        self._provider[name] = ShellProviderFactory.create(os_type)

        shell_override = kwargs.get("shell")
        if shell_override:
            self._shell[name] = shell_override
        elif os_type == "windows":
            self._shell[name] = "powershell.exe"
        else:
            self._shell[name] = "bash"
        # ... rest unchanged

    def open_shell(self, name: str):
        provider = self._provider.get(name)
        shell = self._shell.get(name, "bash")
        # Build args: docker exec -i <container> <shell>
        container_name = self._ensure_client().containers.get(name).name
        if shell == "powershell.exe":
            cmd = [shell] + PowerShellProvider._PS_ARGS
        else:
            cmd = [shell]
        return DockerExecProcess(container_name, cmd)
```

- [ ] **Commit**

```bash
git add src/sandbox_mcp/backends/docker_backend.py
git commit -m "feat: DockerBackend container OS detection and ShellProvider selection"
```

---

## Phase 5: SSHBackend ShellProvider Integration → Remote Windows SSH

**Files:**
- Modify: `src/sandbox_mcp/backends/ssh_backend.py`

### Changes needed

```python
class SSHBackend(Backend):
    def __init__(self):
        # ... existing init ...
        self._provider: dict[str, ShellProvider] = {}

    def create(self, name, purpose="", **kwargs):
        os_type = kwargs.get("os_type", "linux")
        self._provider[name] = ShellProviderFactory.create(os_type)
        if os_type == "windows":
            kwargs.setdefault("shell", "powershell.exe")
        # ... rest of existing create logic ...

    def open_shell(self, name):
        shell = self._shell.get(name, "bash")
        base_args = self._ssh_base_args(name)
        if shell == "powershell.exe":
            return ShellSession([*base_args, shell] + PowerShellProvider._PS_ARGS)
        return ShellSession([*base_args, shell])

    def write_file(self, name, path, content):
        """Use ShellProvider's atomic write script instead of hardcoded POSIX."""
        provider = self._provider.get(name)
        script = provider.atomic_write_script(path)
        result = subprocess.run(
            [*self._ssh_base_args(name), self._shell.get(name, "bash"), "-c", script],
            input=content,
            capture_output=True,
            timeout=60,
        )
        # ... existing result handling ...
```

### Config example for SSH to Windows

```toml
# config.toml

[[ssh_targets]]
name = "win-build"
host = "192.168.1.100"
user = "builder"
port = 22
key = "~/.ssh/id_rsa"
os_type = "windows"
shell = "powershell.exe"
```

- [ ] **Commit**

```bash
git add src/sandbox_mcp/backends/ssh_backend.py
git commit -m "feat: SSHBackend ShellProvider integration for remote Windows SSH"
```

---

## Phase 6: WinRMBackend

**Files:**
- Create: `src/sandbox_mcp/backends/winrm_backend.py`
- Create: `tests/test_winrm_backend.py`

### Dependencies

```toml
# pyproject.toml — add to [project.optional-dependencies]
winrm = ["pywinrm>=0.5.0"]
```

### Implementation

```python
# src/sandbox_mcp/backends/winrm_backend.py
"""WinRM backend: remote Windows management via PowerShell Remoting."""

from __future__ import annotations
import base64
from typing import TYPE_CHECKING

from sandbox_mcp.backends.base import Backend, TargetInfo
from sandbox_mcp.shell_provider import ShellProviderFactory

if TYPE_CHECKING:
    import winrm


def _get_winrm():
    """Lazy import pywinrm."""
    import winrm
    return winrm


class WinRMSession:
    """Wraps a WinRM session to provide ShellSession-compatible interface.
    
    WinRM does not have persistent shells like SSH ControlMaster.
    Each command is a new Invoke-Command / run_ps call.
    This class mimics ShellSession's send/read/close interface
    while executing commands as one-off WinRM invocations.
    """

    def __init__(self, winrm_session, machine: str):
        self._session = winrm_session
        self._machine = machine
        self._state = "idle"
        self._last_command = None
        self._last_result = None

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
            self._state = "error"
            return {"output": "", "exit_code": -1, "status": "error", "error": str(e)}

        stdout = result.std_out.decode("utf-8", errors="replace") if result.std_out else ""
        stderr = result.std_err.decode("utf-8", errors="replace") if result.std_err else ""

        if stderr:
            stdout = stderr + "\n" + stdout

        exit_code = result.status_code
        self._state = "idle"
        self._last_result = {
            "output": stdout[:max_output],
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


class WinRMBackend(Backend):
    """Windows remote management backend via WinRM/PowerShell Remoting."""

    def __init__(self):
        self._targets: dict[str, dict] = {}
        self._sessions: dict[str, object] = {}
        self._provider: dict[str, ShellProvider] = {}

    def create(self, name: str, purpose: str = "", **kwargs) -> TargetInfo:
        """Establish WinRM connection.
        
        Required kwargs:
            host: str       — IP or hostname
            user: str       — Username (DOMAIN\\User or local User)
            password: str   — Password
        
        Optional kwargs:
            port: int       — Default 5985 (HTTP) or 5986 (HTTPS)
            use_ssl: bool   — Use HTTPS
            transport: str  — 'ntlm' (default), 'kerberos', 'credssp', 'basic'
            os_type: str    — Default 'windows'
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
                purpose=purpose, error="host and user are required"
            )

        protocol = "https" if use_ssl else "http"
        url = f"{protocol}://{host}:{port}/wsman"
        auth = (user, password) if transport not in ("kerberos",) else None

        try:
            session = winrm.Session(url, auth=auth, transport=transport)
            result = session.run_cmd("echo", ["OK"])
            if result.status_code != 0:
                return TargetInfo(
                    name=name, backend="winrm", status="error",
                    purpose=purpose,
                    error=f"WinRM test failed: {result.std_err.decode('utf-8', errors='replace')}"
                )
        except Exception as e:
            return TargetInfo(
                name=name, backend="winrm", status="error",
                purpose=purpose, error=str(e)
            )

        os_type = kwargs.get("os_type", "windows")
        self._targets[name] = {
            "host": host, "user": user, "port": port,
            "use_ssl": use_ssl, "transport": transport,
            "purpose": purpose,
        }
        self._sessions[name] = session
        self._provider[name] = ShellProviderFactory.create(os_type)

        return TargetInfo(name=name, backend="winrm", status="running", purpose=purpose,
                          os_type=os_type)

    def start(self, name: str) -> TargetInfo:
        """Reconnect to a WinRM target."""
        target = self._targets.get(name, {})
        if not target:
            return TargetInfo(name=name, backend="winrm", status="error")
        return self.create(name, **target)

    def stop(self, name: str) -> TargetInfo:
        """Disconnect WinRM session (no persistent connection to close)."""
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
            os_type="windows",
        )

    def open_shell(self, name: str):
        """Return a WinRMSession wrapper (no persistent shell)."""
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
            return {
                "output": result.std_out.decode("utf-8", errors="replace") if result.std_out else "",
                "stderr": result.std_err.decode("utf-8", errors="replace") if result.std_err else "",
                "exit_code": result.status_code,
            }
        except Exception as e:
            return {"output": "", "exit_code": -1, "status": "error", "stderr": str(e)}

    def write_file(self, name: str, path: str, content: bytes) -> dict:
        """Write file via base64-encoded PowerShell script.
        
        Content is base64-encoded and embedded inline — avoids stdin piping
        issues over WinRM.
        """
        import base64 as b64

        if isinstance(content, str):
            content = content.encode("utf-8")
        encoded = b64.b64encode(content).decode("ascii")

        # Escape single quotes in path
        safe_path = path.replace("'", "''")
        script = (
            f"$t='{safe_path}'; "
            f"$dir=Split-Path $t -Parent; "
            f"if($dir){{New-Item -ItemType Directory -Path $dir -Force | Out-Null}}; "
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
```

### Config

```toml
# config.toml

[winrm]
default_port = 5986
default_use_ssl = true
default_transport = "ntlm"
connect_timeout = 30

# Per-target overrides handled via sandbox_env params
```

- [ ] **Commit**

```bash
git add src/sandbox_mcp/backends/winrm_backend.py tests/test_winrm_backend.py
git commit -m "feat: WinRMBackend for remote Windows management via PowerShell Remoting"
```

---

## Phase 7: Safety Paths + Configuration + Integration

**Files:**
- Modify: `src/sandbox_mcp/safety.py`
- Modify: `src/sandbox_mcp/config.py`
- Modify: `src/sandbox_mcp/server.py` (register new backends)
- Modify: `src/sandbox_mcp/sandbox_env.py` (add winrm actions)
- Modify: `pyproject.toml` (add pywinrm optional dep)

### Safety paths — Add Windows-sensitive paths

```python
# safety.py — add to CHECK_PATTERNS
if sys.platform == "win32":
    _WINDOWS_SENSITIVE = {
        r"C:\Windows\System32\config\SAM": "system_password_database",
        r"C:\Windows\System32\config\SECURITY": "system_security_policy",
        r"C:\Windows\System32\config\SYSTEM": "system_registry_hive",
        r"~\AppData\Roaming\Microsoft\Crypto": "crypto_keys",
        r"~\AppData\Roaming\Microsoft\Protect": "dpapi_keys",
    }
```

### Config — Add os_type and winrm sections

```python
# config.py — add to SandboxConfig dataclass
@dataclass
class WinRMConfig:
    default_port: int = 5986
    default_use_ssl: bool = True
    default_transport: str = "ntlm"
    connect_timeout: int = 30

# Add to DockerConfig:
os_type: str = ""  # Empty = auto-detect, "linux" or "windows"
```

### Server — Register WinRMBackend

```python
# server.py
from sandbox_mcp.backends.winrm_backend import WinRMBackend

class SandboxServer:
    def __init__(self):
        self._backends = {
            "docker": DockerBackend(),
            "ssh": SSHBackend(),
            "winrm": WinRMBackend(),
        }
```

### sandbox_env — Add WinRM actions

```python
# sandbox_env.py — add dispatch cases
"winrm_connect": self._op_winrm_connect,
"winrm_disconnect": self._op_winrm_disconnect,
"winrm_remove": self._op_winrm_remove,
```

- [ ] **Commit**

```bash
git add src/sandbox_mcp/safety.py src/sandbox_mcp/config.py
git add src/sandbox_mcp/server.py src/sandbox_mcp/sandbox_env.py
git add pyproject.toml
git commit -m "feat: Windows safety paths, config, and server integration"
```

---

## Phase 8: End-to-End Tests

**Files:**
- Create: `tests/test_windows_integration.py` (requires Windows container or WinRM target)

### Test scenarios

1. **Docker Windows container**: `docker run -d mcr.microsoft.com/windows/servercore:ltsc2022 powershell.exe` → exec hello world
2. **SSH to Windows**: Connect to a Windows VM with OpenSSH → execute `Get-Process`
3. **WinRM**: Connect to a Windows VM → file write + read round-trip
4. **Cross-platform**: Same MCP server managing one Linux container + one Windows container simultaneously

- [ ] **Commit**

```bash
git add tests/test_windows_integration.py
git commit -m "test: Windows integration tests (Docker + SSH + WinRM)"
```

---

## Summary: Implementation Order

| Phase | Description | Files Changed | Effort |
|-------|-------------|---------------|--------|
| 0 | ShellProvider ABC + factory | 2 new | 0.5 day |
| 1 | BashShellProvider — extract from file_operations.py | 2 new + 1 modify | 1 day |
| 2 | PowerShellProvider | 2 new | 1 day |
| 3 | ShellSession Windows adaptation | 1 modify | 0.5 day |
| 4 | DockerBackend OS detection | 1 modify | 0.5 day |
| 5 | SSHBackend ShellProvider integration | 1 modify | 0.5 day |
| 6 | WinRMBackend | 2 new | 1 day |
| 7 | Safety + config + server integration | 5 modify | 0.5 day |
| 8 | Integration tests | 1 new | 1 day |

**Total: ~6 days**

### Critical Path

P0 → P1 → P3 (ShellSession depends on provider)  
P1 → P2 (PowerShellProvider mirrors BashShellProvider)  
P4, P5 depend on P0-P3  
P6 depends on P0-P3  
P7, P8 depend on all above

### Risk Items

1. **PowerShell escaping**: Single quotes in paths, embedded `$` variables — test against real Windows containers early
2. **WinRM large output**: stdout can be truncated; need buffer handling
3. **Dual-marker protocol on PowerShell**: marker detection logic unchanged (regex), but `Write-Host` output format differs from `echo`
4. **Docker Desktop for Windows**: daemon must be in "Windows containers" mode (mutually exclusive with Linux mode)
