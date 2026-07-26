# Sandbox MCP Design Spec (current)

> **Scope**: describes what sandbox-mcp is *now*, not the historical
> v1 → v2 evolution.  The implementation plan / test plan lives in
> [`implementation-plan.md`](implementation-plan.md); the operator-facing
> workflow lives in [`README.md`](../README.md).

## 1. Problem

Hermes Agent's built-in terminal/file/code_execution tools create
ephemeral containers that reset on recreation. The agent cannot define
persistent environments, deploy long-running services, or reliably
manage process state — and every tool call burns context window space
on tool schemas.

`Sandbox MCP` exposes a small set of persistent tools (Docker
containers + SSH remote machines) that the agent can drive across many
turns without losing state, while keeping `tools/list` small.

## 2. Backends

Two backends, both supported in production:

| Backend | Auth | Shell transport | Used for |
|---|---|---|---|
| **Docker** | docker daemon socket / TCP / SSH transport (config) | `docker exec` with `tty=True` via the Python SDK | Linux + Windows containers, locally or remote daemons |
| **SSH**    | SSH key (`key = "..."` per target) | `ssh -tt <user>@<host> powershell.exe` / `bash` | Linux or Windows (PowerShell) over SSH; OpenSSH Server must be running on the Windows host |

There is **no WinRM backend**; Windows remote access is SSH-only.
history retains the commits if needed for archaeology.

All shell sessions run under a **real PTY**.  This is what makes
`pager less`, `vim`, password prompts, and PowerShell's interactive
prompt work normally — the agent sees a transparent byte stream and the
drain thread reads the same stream.

## 3. MCP surface

`tools/list` advertises 12 tools (13 when audit log is file-backed):

| Tool | Purpose |
|---|---|
| `shell_exec` | Send a command to a shell. `wait=true` blocks (default 10 s), `wait=false` returns immediately. |
| `shell_read` | Read buffered output from a shell (non-blocking). |
| `shell_new` | Open an extra shell on a machine (the default shell is auto-created on first `shell_exec` if missing). |
| `shell_remove` | Terminate and remove a shell (any state). |
| `shell_list` | List all shells with state, machine, is_default, last_command. |
| `sandbox_machine_list` | List all machines with backend, status, shell count, uptime. |
| `sandbox_default_set` | Set default machine *or* default shell (exactly one of `machine` / `shell_id`). |
| `file_read` | Read a text file with line numbers and pagination. |
| `file_write` | Write a file (atomic, in-process lint for known extensions). |
| `file_patch` | Targeted find-and-replace (replace mode) or unified-diff (patch mode), fuzzy-matched. |
| `file_search` | ripgrep content search + glob file search. |
| `sandbox_env` | Progressive discovery: `help`, `status`, `list_targets`, `machine_list`, `default_set`, `shell_*`, `docker_*`, `connect`, `close`. |
| `sandbox_audit_query` | Read the audit log (filtered, paginated). Only present when `[audit] log_path` is set. |

`tools/list` always advertises the 6 core tools (`shell_*`, `file_*`)
plus `env` so the agent can discover management actions on demand;
the agent opts in to backend-specific actions via `action=help` /
`action=list_targets`.

### `tools/list` size budget

The 12-tool schema is the production contract.  Adding a new tool
requires deleting or demoting one.  Backend-specific discovery is
deliberately *not* in `tools/list` — `sandbox_env` is the single
meta-tool whose description stays short.

## 4. Shell protocol — one-shot random Prompt

Each shell installs a single random prompt token **at startup**; we no
longer wrap every command in start/end markers.

* **Bash** — `unset PROMPT_COMMAND; export PS1='<token>:$?>'`.  Setting
  `PROMPT_COMMAND` empty stops bash from re-emitting its own prompt
  prefix; PS1 is the only prompt line.
* **PowerShell** — `prompt` function that prints
  `'<token>:' + $LASTEXITCODE + '>'`.  Also runs `setup_command`
  once (UTF-8 console encoding, `chcp 65001`, `PYTHONUTF8=1`,
  disables PSReadLine's continuation prompt).
* **The drain thread** scans for `<token>:N>` and uses the leading
  digits as the captured exit code.  When the agent writes
  `command + '\n'`, the next prompt line is the gate from
  `waiting` → `ready`.

We deliberately do **not** detect PS2 (the secondary /
continuation prompt).  Reasons:

* Shell configuration varies wildly across distros / Windows versions,
  and any regex risks false matches inside command output.
* The drain thread's only required signal is the primary prompt after
  each command; PS2 only matters for incomplete input, which the agent
  never sends (every command is one full line followed by `\n`).
* Real interactive programs emit PS2 freely; we want it preserved in
  the output stream as-is.

This keeps the protocol robust against agents whose commands include
unusual quoting, embedded newlines in quoted strings, or paginated
output.  Anything that prints to stdout after the prompt is captured
as part of the *next* command's output — same as a human's terminal.

## 5. Shell state machine

Three states.  The per-shell lock is held internally for the duration
of a single `send(wait=True)` call, but that is never exposed to the
agent — the only `state` values the agent ever sees are these three:

```
                   +-------+
                   | ready |  <-- drain thread saw prompt line
                   +-------+
                      |
        agent.send() |  (lock acquired)
                      v
                   +---------+
                   | waiting |  <-- command bytes written, prompt not yet seen
                   +---------+
                      |
            drain sees| prompt token,
            _prompt_  | captures exit code
            event.set  v
                   +-------+  (back to ready, exit code returned)

  (any state) --> +-----------+
                  |terminated |  <-- shell process exited / broken pipe
                  +-----------+
                  state kept; last output preserved;
                  agent must shell_remove + shell_new.
```

| State | `send(wait=True)` returns | `send(wait=False)` returns | `read()` returns | `remove()` |
|-------|---------------------------|----------------------------|------------------|-----------|
| `ready`      | prompt is set; `output` + `exit_code` + `status="ready"` | prompt not seen yet → `status="waiting"`. Caller follows up with `shell_read`. | last buffered output, `status="ready"` if prompt has been seen since last `send` | kills shell, removes registry entry |
| `waiting`    | `error` with guidance (`"waiting for previous command"`) | same error | buffered output, `status="waiting"` | kills shell, removes registry entry |
| `terminated` | `error` with guidance (`"use shell_remove then shell_new"`) | same error | buffered output + `status="terminated"` | removes registry entry (no-op on already-dead shell) |

### `wait=True` timeout semantics

`send(wait=True, timeout=10)` blocks until the prompt is seen.  When
the timeout elapses the response is:

```python
{
    "output": "...everything seen so far...",
    "exit_code": None,
    "status": "waiting",
    "hint": "Command is still running. For long tasks use wait=false and shell_read.",
}
```

The shell stays in `waiting`; the agent's command is still running on
the target.  The recommended workflow is:

1. First attempt: `shell_exec(wait=True, timeout=10)`.
2. On `status="waiting"`, call `shell_read` to pull incremental output.
3. If still waiting after another interval, retry
   `shell_exec(wait=True, timeout=N)` against the same `shell_id` —
   this is a *new* command; it cannot be used to read the prior one's
   output.  To read without sending, use `shell_read`.
4. Or, more efficient for known long tasks: `shell_exec(wait=False, ...)`
   to fire the command and immediately return, then poll `shell_read`.

### `terminated` shells are never auto-replaced

When the default shell's process exits (clean `exit`, signal, broken
pipe), the registry keeps the shell entry.  `get_or_create_default`
returns the same `shell_id`; subsequent `shell_exec` calls fail with
the `shell_remove`/`shell_new` guidance.  The agent must explicitly:

* `shell_remove(shell_id=...)` — drops the registry entry.
* `shell_new(machine=...)` — opens a fresh shell.

Terminated shells stay in the registry until the agent explicitly removes
and made it impossible to inspect the post-mortem output.

## 6. Persistent backend state

* **Docker containers** survive an MCP server restart.  Server boot
  calls `docker_ps` (filtered by the `sandbox-mcp.managed=true` label)
  to re-adopt every surviving container into the in-process
  `MachineRegistry`.  This is the single source of truth — labels are
  authoritative; name prefix is not consulted (an attacker can't trick
  the server into managing an arbitrary container by naming it
  `sandbox-foo`).
* **SSH machines** do *not* survive restart.  The process registry is
  in-memory; on restart the agent must `connect(name=...)` to reopen
  the ControlMaster.  (The SSH socket itself survives until the OS
  cleans `/tmp`, but the sandbox-mcp side has no record of it.)
* **Shell sessions** never survive restart.  Re-create with
  `shell_new`.

## 7. Default machine / default shell

The registry tracks two levels of "default":

* **Default machine** — the machine that receives file_* / shell_exec
  calls when `[machine]` is omitted.  Set with
  `default_set(machine=...)` or auto-provisioned via
  `[default_machine] enabled = true` in config.
* **Default shell per machine** — the shell that receives commands
  when `[shell_id]` is omitted.  Lazy-created on first `shell_exec`
  against a given machine.

Both targets are *not* auto-replaced when their underlying object dies.
If the default machine exits, `machine_list` still shows it; the agent
must `docker_remove` + `docker_run` (or `close` + `connect` for SSH)
to get a working default.

## 8. sandbox_env actions (current)

```
Default discovery:  help / status / list_targets
Common:             default_set / machine_list
Shell:              shell_new / shell_list / shell_remove
Docker:             docker_run / docker_build / docker_commit
                    docker_stop / docker_start / docker_remove
                    docker_restart / docker_ps / docker_images
                    docker_image_history / docker_inspect
                    docker_logs / docker_diff / docker_stats
SSH:                connect / close
```

`connect(name)` and `close(name)` are the only SSH-touching actions.
`connect` reads its connection params from the `[ssh.targets.{name}]`
table in `config.toml`; the agent cannot supply `host` / `user` /
`key` directly — that's a deliberate boundary.

## 9. Out of scope

The following are *not* in scope for the current design:

* WinRM backend (not present; SSH is the only Windows transport).
* Local pseudo-backend (no in-process target that is not backed by a
  real container or SSH session — the "local PTY" code path is shared
  with the SSH backend and exercised by tests).
* PowerShell uses per-command echo markers (no PTY / PSReadLine).
* In-place migration of `admin` containers between peer and god-mode
  layouts (explicit `docker_remove` + recreate is required).
* Resource limits (CPU / memory) per machine.
* Built-in session isolation between agents (matches Hermes MCP
  semantics — agents share the in-process registry).