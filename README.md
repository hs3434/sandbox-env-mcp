# sandbox-mcp

<!-- mcp-name: io.github.hs3434/sandbox-env-mcp -->

MCP server that gives AI agents a real working environment: persistent
shells, a filesystem, and multi-machine management — backed by Docker
containers or remote SSH hosts.

## Features

- **Persistent shells** — stateful bash or PowerShell sessions that
  survive across tool calls.  Set env vars, activate venvs, change
  directories, and they stay.
- **Multi-machine** — manage several Docker containers and SSH hosts
  simultaneously.  Each has its own isolated workspace and shell pool.
- **Full filesystem access** — read, write, patch, and search files on
  any target machine.  All writes are atomic (temp-file + rename).
- **Zero-config startup** — creates a default Docker container
  automatically on first run.  One command, ready to go.
- **Progressive discovery** — `env` tool exposes capabilities step by
  step.  Agents call `env(action="help")` to see what's available.
- **Docker lifecycle** — create, stop, start, restart, remove containers.
  Build images, inspect configs, commit state, view logs.
- **SSH remote access** — connect to Linux and Windows machines over
  SSH.  Windows targets get automatic code-page probing.
- **Safety net** — sensitive-path warnings (`.ssh`, `.aws`, `.env*`)
  without blocking access.  Pre-write syntax lint for JSON/YAML/TOML.
- **Audit trail** — every tool call is logged with timestamps,
  parameters, and outcomes.  Queryable from within the agent session.

## Quick start

```bash
pip install sandbox-env-mcp

# stdio — for Claude Desktop, Cline, Continue
sandbox-mcp

# HTTP — for remote agents
sandbox-mcp-http
```

On first run a default container (`python:3.14-slim`, named `admin`)
starts automatically with a persistent bash shell.  No other setup.

**Requirements**: Python 3.12+, Docker SDK, running Docker daemon.
SSH mode needs `openssh-client`.

## Tools

All tools target the default machine unless an explicit `machine`
parameter is passed.

| Tool | What it does |
|------|--------------|
| `shell_exec` | Run a command in a persistent shell.  Blocks until the command finishes (`wait=true`, 10 s timeout) or fire-and-forget with `wait=false`. |
| `shell_read` | Read buffered output from a running or finished command. |
| `shell_new` | Create a fresh shell on a machine.  Returns a `shell_id`. |
| `shell_remove` | Terminate and remove a shell by `shell_id`. |
| `shell_list` | List all shells with state, machine, uptime, last command. |
| `write_stdin` | Write raw bytes to a running shell — interrupt with Ctrl-C (`\x03`) or feed input to interactive programs like `read` / `Read-Host`.  On Windows/PowerShell, Ctrl-C is unsupported (pipe mode has no terminal driver); kill the shell instead. |
| `machine_list` | List all registered machines with backend, status, purpose, shell count. |
| `default_set` | Set the default machine or default shell for a machine. |
| `file_read` | Read a file with line numbers.  Supports offset + limit pagination. |
| `file_write` | Write content atomically.  Creates parent directories automatically. |
| `file_patch` | Targeted edits with fuzzy matching. `mode=replace` (find-and-replace) or `mode=patch` (unified diff). |
| `file_search` | Search file contents (ripgrep) or find files (glob).  Sorted by modification time. |
| `env` | Progressive-discovery portal.  Start with `env(action="help")`. |

`audit_query` is exposed when the audit log is a SQLite database —
it lets the agent search historical tool calls.

### Shell states

Every shell is in one of four states:

| State | What it means | What the agent can do |
|-------|---------------|-----------------------|
| `init` | Shell just created; booting up.  Times out → `terminated` at 10 s. | Wait — `shell_exec` returns an error until ready. |
| `ready` | At a prompt, accepting commands. | Send commands, read output, write stdin. |
| `waiting` | A command is running. | Poll output with `shell_read`.  Send Ctrl-C with `write_stdin`. |
| `terminated` | Shell process exited (signal, exit, timeout, broken pipe).  Last output is preserved. | Read remaining output, then `shell_remove` + `shell_new` to continue.  Default shells are **never** auto-replaced. |

Key `shell_exec` parameters:

- `wait` (default `true`): block until the command completes.
- `timeout` (default `10` s): on expiry returns `status="waiting"`
  with a hint to switch to `wait=false` + `shell_read` for
  long-running commands.
- `max_output` (default `50000` bytes): caps returned output;
  excess is shown as the tail (last N bytes).

## env actions

`env(action="help")` lists what's available.  `env(action="help",
topic="<action>")` returns full docs for a specific action.

### Always available

| Action | Params | Description |
|--------|--------|-------------|
| `help` | `topic?` | List actions or get docs for one. |
| `status` | — | Default machine, machines, shells. |
| `list_targets` | — | Pre-defined SSH targets from config. |
| `machine_list` | — | Registered machines. |
| `shell_list` | `machine?` | Shells, optionally filtered. |
| `shell_new` | `machine?`, `purpose?` | New shell session. |
| `shell_remove` | `shell_id` | Terminate and remove. |
| `default_set` | `machine` or `shell_id` | Set default machine or shell. |

### Docker

| Action | Required params | Description |
|--------|----------------|-------------|
| `docker_run` | `name`, `image`, `purpose` | Create/start container.  Reattaches on name collision. |
| `docker_ps` | — | List managed containers. |
| `docker_images` | — | List all images on daemon. |
| `docker_image_history` | `image` | Layer-by-layer build history. |
| `docker_build` | `image_tag`, `machine` | Build from a Dockerfile in `/workspace`. |
| `docker_commit` | `machine`, `image_tag` | Commit container as new image. |
| `docker_stop` | `machine` | Stop (state preserved). |
| `docker_start` | `machine` | Start a stopped container. |
| `docker_remove` | `machine` | Stop + remove container and its shells. |
| `docker_inspect` | `machine` | Curated config.  `kind=image` for images. |
| `docker_logs` | `machine` | Logs with `tail`, `since`, `until`. |
| `docker_diff` | `machine` | Filesystem changes vs image. |
| `docker_stats` | `machine` | CPU/memory/network/IO snapshot. |
| `docker_restart` | `machine` | Stop + start + verify. |

### SSH

| Action | Required params | Description |
|--------|----------------|-------------|
| `connect` | `name` | Connect to a configured target. |
| `close` | `name` | Disconnect and unregister. |

Available when `[ssh.targets]` is configured.

## File operations

| Tool | Key params | Highlights |
|------|-----------|------------|
| `file_read` | `path`, `offset`, `limit` | Line-numbered.  Rejects files > 50 KB with a hint. |
| `file_write` | `path`, `content` | Atomic (temp + rename), auto-creates parent dirs, post-write verification. |
| `file_patch` | `path`, `old_string`, `new_string` (replace mode) or `patch` (unified diff) | Fuzzy matching.  Preserves BOM and line endings. |
| `file_search` | `pattern`, `search_type`, `path`, `file_glob`, `limit` | Powered by ripgrep.  Results sorted by modification time. |

Safety warnings are surfaced for sensitive paths (`.ssh`, `.aws`,
`.env*`, `/etc/shadow`, etc.) — advisory only, agents still have full
access.  Writes to `.json`, `.yaml`, `.yml`, `.toml` are
syntax-checked before writing (fail-closed).

## Configuration

Config lives at `~/.sandbox-mcp/config.toml` (copy
`config/config.example.toml`).  Every field can be overridden with
`SANDBOX_MCP_<SECTION>_<KEY>` env vars.

```toml
[server]
port = 8010
auth_tokens_file = "~/.sandbox-mcp/auth_tokens"

[storage]
work_home = "/var/lib/sandbox-mcp"

[docker]
default_image = "python:3.14-slim"
auto_network = "sandbox-mcp"      # "" = none
admin_machine = "admin"           # "" = no /host mount
host = ""                         # "" = from Docker environment

[ssh]
connect_timeout = 10
[ssh.targets.win-build]
host = "192.168.1.100"
user = "builder"
os_type = "windows"

[default_machine]
enabled = true
backend = "docker"
name = "admin"

[shell]
default_max_output = 50000

[files]
max_file_size = 51200
```

## Backends

### Docker

Containers get bind mounts for workspace isolation:

- `work_home/<name>/` → `/workspace` (rw)
- `work_home/<share_subdir>/` → `/share/` (ro, shared across peers)
- `work_home/<share_subdir>/<name>/` → `/share/<name>/` (rw overlay)

When a container's name matches `admin_machine`, it also gets
`work_home/` → `/host` (rw) — a global view of all workspaces.

Server startup auto-reconciles with the Docker daemon: surviving
containers are re-adopted into the registry.

### SSH

Connects over SSH with ControlMaster for connection reuse.  Windows
targets get automatic code-page probing and encoded-command execution.

## Deployment

```yaml
# docker-compose.yml
services:
  sandbox-mcp:
    image: ghcr.io/hs3434/sandbox-env-mcp:latest
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /var/lib/sandbox-mcp:/var/lib/sandbox-mcp
      - ./config:/root/.sandbox-mcp
```

HTTP mode reads bearer tokens from `auth_tokens_file` (hot-reload on
every request).  If the file is empty or missing and
`auto_generate_if_empty=true`, a random token is printed to stderr at
startup.

## Audit

Every tool call is recorded: timestamp, machine, action, status,
duration, and hashed parameters.  Defaults to SQLite at
`~/.sandbox-mcp/audit.db`.  Set `log_path=""` for JSON-line stderr
output instead.
