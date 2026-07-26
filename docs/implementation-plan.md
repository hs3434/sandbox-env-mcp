# Sandbox MCP Implementation Notes

> **What this is**: a top-down tour of the implementation as it stands
> today.  For *why*, see [`design-spec-v2.md`](design-spec-v2.md).  For
> *how to run*, see [`README.md`](../README.md).

## Layout

```
src/sandbox_mcp/
├── __init__.py
├── server.py                 # SandboxServer (transport-agnostic core),
│                             # tool definitions, stdio + HTTP entry points
├── config.py                 # AppConfig dataclasses + TOML + env loader
├── audit.py                  # SQLite / JSONL audit logger + query_audit
├── auth.py                   # bearer-token file loader (0600-enforced)
├── safety.py                 # non-blocking path-safety advisories
├── sandbox_env.py            # sandbox_env action dispatcher + help
├── shell_session.py          # ShellSession: PTY + drain thread + state
├── shell_registry.py         # ShellRegistry: in-memory shell_id -> session
├── shell_provider.py         # ShellProvider ABC + ShellProviderFactory
├── shell_providers/
│   ├── __init__.py
│   ├── bash_provider.py      # bash command generator
│   └── powershell_provider.py# PowerShell command generator
├── file_operations.py        # read/write/patch/search via backend hooks
├── target_registry.py        # in-memory machine -> backend
└── backends/
    ├── __init__.py
    ├── base.py               # Backend ABC, TargetInfo dataclass
    ├── docker_backend.py     # Docker SDK (lifecycle, exec, builds)
    └── ssh_backend.py        # SSH ControlMaster + ssh -tt
```

## Module responsibilities

### `config.py`

* Frozen-dataclass `AppConfig` composed of `ServerConfig`,
  `StorageConfig`, `AuditConfig`, `DockerConfig`, `SSHConfig`,
  `ShellConfig`, `FilesConfig`, `DefaultMachineConfig`.
* `load()` reads the TOML file (default `~/.sandbox-mcp/config.toml`
  or `$SANDBOX_MCP_CONFIG`) then layers `SANDBOX_MCP_*` env vars.
* Unknown TOML keys are silently dropped (forward-compat).
* `storage.work_home` rejects paths starting with `~` or without a
  leading `/` — it's passed verbatim to the docker daemon, which may
  be in a different process / container / machine.

### `shell_session.py`

PTY-backed persistent shell.  Public surface:

* `__init__(args=None, process=None, provider=None)` — either a
  Popen-style arg list (local PTY, SSH) or an external process-like
  object (Docker exec).  `provider` defaults to
  `BashShellProvider()`.
* `send(command, wait=True, timeout=10, max_output=None)` — write
  the command, wait for the next prompt token, return buffered
  output.  `wait=False` returns immediately.  `timeout` defaults
  to **10 s**.
* `read()` — non-blocking pull of buffered output; reports the
  current state.
* `write_stdin(data)` — raw stdin write (Ctrl-C, etc.).
* `close()` — kill the process group (local + SSH), close fds,
  signal the drain thread.
* Properties: `state`, `bash_pid`, `last_command`, `uptime`.

State (`init` / `ready` / `waiting` / `terminated`) is set under the
instance's `_lock`.  The drain thread:

* Reads stdout bytes (PTY master for local / SSH;
  `DockerExecProcess` demuxes the SDK exec socket).
* Scans for `<token>:N>` (the prompt regex).  On match, stores any
  pre-match bytes as command output, sets `_pending_exit_code`,
  fires `_prompt_event`, and continues draining.
* On EOF / process exit, sets `state="terminated"` and fires the
  event so any waiting `send` unblocks with `status="terminated"`.

Buffer strategy: `head` is a fixed-size `bytearray` (`head_size`,
default 5 KB); `tail` is a `deque(maxlen=tail_size)` (default 45 KB).
When `send` returns, `_get_buffered_output` strips the echoed
command from the head and trims `\r` / leading newlines.  If the
joined text exceeds `max_output`, it returns the **tail** (last N
bytes) with a `[Output truncated: showing last N of M chars]`
notice.

The shell provider's `prompt_setup_command(token)` runs once during
`_start()`.  The drain thread detects `SETUP_OK` followed by the first
prompt token, then sets state to `ready`.  If init does not complete
within 10 seconds, `_init_timeout_kill` sets state to `terminated`.
On `BrokenPipeError` / `OSError`, state is `terminated` immediately.

### `shell_registry.py`

* `open(machine, session, purpose)` runs `_health_check` (sends
  `true`, waits ≤10 s for `status="ready"`).  On failure closes the
  session and raises `ShellUnhealthy`.
* `close(shell_id)` — pops entry, calls `session.close()`, clears
  default-shell mapping if it pointed here.
* `get_or_create_default(machine, factory)` — returns the existing
  default shell (even if `terminated`); **does not** auto-replace.
  This is the contract that prevents silent re-spawn on a dead
  shell.
* `list_shells(machine=None)` — projects the registry into the
  wire-format used by `shell_list`, including per-state
  hints.

### `shell_provider.py`

* `ShellProvider` ABC + `ShellProviderFactory` keyed on
  `os_type ∈ {"linux", "windows"}`.
* `default_shell_args` is what `open_shell` execs; `exec_flag` is
  what `exec_oneoff` uses (`-c` / `-Command`).
* `prompt_setup_command(token)` is the single most important
  method — see § 4 of the design spec.

### `file_operations.py`

* All paths go through `backend.write_file` (atomic) or
  `backend.exec_oneoff` (small read-style probes).
* Reads produce a 4-line structured payload (size, base64 head
  sample, base64 line-range, total line count).  Exit code 2 means
  not found.  Files larger than `[files] max_file_size` return
  `status="too_large"` with a hint to use the shell tool.
* Writes: in-process lint for `.py`/`.json`/`.yaml`/`.yml`/`.toml`
  (Python: `ast`; JSON: `json`; YAML/TOML: optional modules) →
  atomic write via backend.
* Patches: replace-mode (fuzzy match `old_string` → `new_string`,
  9 strategies), or patch-mode (unified diff via stdin).
* Search: ripgrep wrapper, with diagnostics split out.

### `target_registry.py`

* `register(name, backend, purpose, **kwargs)` — calls
  `backend.create()` and records; first machine auto-becomes default.
* `adopt(name, backend, info)` — records without creating; used by
  `docker_ps` reconciliation.  Idempotent (no-op if already known).
* `unregister(name)` — drops entry; clears default if it pointed
  here.
* `resolve_machine(name=None)` — None → default machine; explicit →
  validate membership.

### `backends/docker_backend.py`

* `DockerExecProcess` — wraps a docker SDK exec instance as a
  Popen-like object (stdin pipe → socket, demux socket → stdout
  pipe).  `tty=True` so the daemon runs the shell under a PTY.
* `create()` — applies the bind mounts:
  - peer: `work_home/<name>` → `/workspace` (rw) +
    `work_home/_share/` (ro) + `work_home/_share/<name>/` → `/share/<name>/` (rw);
  - admin (name matches `[docker] admin_machine`): replaces with
    `work_home/` → `/host` (rw); skips share bindings.
* `_reattach_existing` — on HTTP 409 (name conflict), adopts the
  existing container and starts it if needed.
* `_running_info` — single-shot post-start `container.reload()` to
  catch CMDs that exit within ms of `start()`.  Not polling.
* `exec_oneoff` — `container.exec_run` with the provider's shell
  args + exec flag.
* `write_file` — `put_archive` of a tar containing the file into a
  tmp dir, then `mv -f` into place.  Used by `file_write`.
* `inspect`, `logs`, `diff`, `stats`, `history`, `list_images`,
  `list_managed_containers` — direct daemon queries; `inspect`
  has a curated container view (no env values, no NetworkSettings)
  and an image view (env keys only).

### `backends/ssh_backend.py`

* `_ensure_alive` — `ssh -O check` before every operation; if the
  socket is stale, calls `create()` again to reconnect.
* `_socket_path` — per-target ControlMaster socket in
  `tempfile.mkdtemp(prefix=<socket_dir_prefix><name>-)`.
* `create()` — `ssh -M -S <socket> ... true` to establish the
  master.
* `open_shell(name)` — for Linux: `ssh -tt <args> <provider.default_shell_args>` (strips `-NonInteractive`).  For PowerShell: `ssh <args> -NoExit -File -` (pipe mode, no PTY).
* `exec_oneoff` — same shape without `-tt` (one-off commands).
* `write_file` — pipes content over SSH stdin to the provider's
  atomic write script (tmp + `mv -f`).
* `_decode_bytes` — decodes stdout/stderr with the target's configured
  codec, falling back to `replace` on errors.

### `server.py`

* `TOOL_DEFINITIONS` — the 13-tool schema (plus the optional
  `audit_query` when `[audit] log_path` is set).
* `SandboxServer.__init__`:
  1. Build registries + backends + `SandboxEnv` dispatcher.
  2. Run `docker_ps` once to adopt surviving containers.  Failures
     are logged, not fatal.
  3. If `[default_machine] enabled = true`, provision via the
     configured backend.  Failure is fatal (fail-closed).
* `_handle_shell_exec` — resolves machine, lazily creates a
  default shell if needed (via `shell_registry.open`,
  which health-checks).  Pass-through to `session.send`.
* `_handle_shell_read` — `session.read()`.
* `_handle_*_file` — `FileOperations(backend)`.
* `_handle_env` — `sandbox_env.dispatch(action, params)`.
* `main` / `main_http` — stdio / streamable-http transports.
  `main_http` mounts `BearerAuthMiddleware` which re-reads the token
  file on every request (hot-reload, like sshd).

## Tests

`tests/` holds unit tests; `pytest -m integration` enables the ones
that require a real Docker daemon.

The `tests/test_lint.py` file runs `ruff format --check`, `ruff check`,
and `mypy src/sandbox_mcp` as subprocesses — a single `pytest` run
catches what CI catches.

Key test invariants worth highlighting:

* `tests/test_config.py::test_repo_example_matches_dataclass_defaults`
  is a **drift guard**: it parses `config/config.example.toml` and
  asserts every key/value matches `AppConfig()`'s defaults.  Any
  change to defaults in `config.py` must be reflected in the example
  TOML (and vice versa).
* `tests/test_shell_session.py::test_only_public_session_states_are_used`
  is a guard against accidentally reintroducing `idle` / `busy` /
  `running` states in the public `state` API.
* `tests/test_shell_provider.py::TestBashShellProvider::test_prompt_setup`
  asserts the bash prompt-setup command includes the token and
  **does not** mention `PS2`.

## Local CI

`./scripts/ci.sh` runs the same sequence as GitHub Actions:

```
ruff format --check .
ruff check .
mypy src/sandbox_mcp
pytest tests/ -v
```

Run before pushing to catch regressions locally.