# sandbox-mcp

为 AI agent 提供真实工作环境的 MCP server：持久化 shell、文件系统访问、多机管理 — 底层基于 Docker 容器或 SSH 远程主机。

## 特性

- **持久化 shell** — 有状态的 bash / PowerShell 会话，跨工具调用保持环境（变量、venv、工作目录）。
- **多机管理** — 同时管理多个 Docker 容器和 SSH 主机，各自拥有独立的工作空间和 shell 池。
- **完整文件系统** — 对任意目标机进行读、写、patch、搜索。所有写入操作均为原子操作（临时文件 + 重命名）。
- **零配置启动** — 首次运行时自动创建默认 Docker 容器，一条命令即可就绪。
- **渐进式发现** — `env` 工具逐步暴露能力，agent 调用 `env(action="help")` 即可查看可用操作。
- **Docker 全生命周期** — 创建、停止、启动、重启、删除容器。构建镜像、查看配置、提交状态、查看日志。
- **SSH 远程访问** — 通过 SSH 连接 Linux 和 Windows 主机。Windows 目标自动探测编码。
- **安全网** — 敏感路径告警（`.ssh`、`.aws`、`.env*` 等），不阻止访问。JSON/YAML/TOML 写入前语法校验（失败即拒绝）。
- **审计追踪** — 记录每次工具调用（时间戳、参数哈希、结果），agent 可在会话内查询。

## 快速开始

```bash
pip install sandbox-env-mcp

# stdio 模式 — 配合 Claude Desktop / Cline / Continue 使用
sandbox-mcp

# HTTP 模式 — 供远程 agent 使用
sandbox-mcp-http
```

首次运行时自动创建默认容器（`python:3.14-slim`，名称 `admin`），
自带持久化 bash shell，无需额外配置。

**环境要求**：Python 3.12+，Docker SDK，Docker 守护进程运行中。
SSH 模式需要 `openssh-client`。

## 工具

所有工具默认操作默认机器，可通过 `machine` 参数指定其他目标。

| 工具 | 功能 |
|------|------|
| `shell_exec` | 在持久化 shell 中执行命令。默认阻塞等待完成（`wait=true`，10 秒超时），或用 `wait=false` 异步执行。 |
| `shell_read` | 非阻塞读取 shell 缓冲输出。 |
| `shell_new` | 在目标机上创建新的 shell 会话，返回 `shell_id`。 |
| `shell_remove` | 按 `shell_id` 终止并移除 shell。 |
| `shell_list` | 列出所有 shell（状态、机器、运行时长、上一条命令）。 |
| `write_stdin` | 向运行中的 shell 写入原始字节 — 发送 Ctrl-C（`\x03`）中断命令、给 `read` / `Read-Host` 等交互式程序输入。注意：Windows/PowerShell 使用管道模式（无终端驱动），Ctrl-C 无法中断命令；用 `shell_remove + shell_new` 终止长时间运行的命令。 |
| `machine_list` | 列出所有已注册机器（后端、状态、用途、shell 数量）。 |
| `default_set` | 设置默认机器或某台机器的默认 shell。 |
| `file_read` | 按行号读取文件，支持 offset + limit 分页。 |
| `file_write` | 原子写入（临时文件 + 重命名）。自动创建父目录。 |
| `file_patch` | 查找替换（`mode=replace`）或 unified diff 应用（`mode=patch`）。支持模糊匹配。 |
| `file_search` | 按内容搜索（ripgrep）或按文件名查找（glob）。按修改时间排序。 |
| `env` | 渐进式发现入口。从 `env(action="help")` 开始。 |

当审计日志为 SQLite 数据库时，`audit_query` 工具会暴露给 agent
用于查询历史调用记录。

### Shell 状态机

每个 shell 处于四种状态之一：

| 状态 | 含义 | agent 可执行操作 |
|------|------|----------------|
| `init` | shell 刚创建，正在初始化。超时 10 秒后进入 `terminated`。 | 等待 — `shell_exec` 返回错误直到就绪。 |
| `ready` | 等待输入，可接受命令。 | 发送命令、读取输出、写入 stdin。 |
| `waiting` | 命令正在运行中。 | 通过 `shell_read` 轮询输出，通过 `write_stdin` 发送 Ctrl-C。 |
| `terminated` | shell 进程已退出（信号、exit、超时、管道断开）。最后输出保留在缓冲中。 | 读取剩余输出，然后 `shell_remove` + `shell_new` 继续工作。默认 shell **不会自动替换**。 |

`shell_exec` 关键参数：

- `wait`（默认 `true`）：阻塞等待命令完成。
- `timeout`（默认 `10` 秒）：超时后返回 `status="waiting"`，
  并提示改用 `wait=false` + `shell_read` 处理长时间运行的命令。
- `max_output`（默认 `50000` 字节）：限制返回的输出量，
  超出部分显示末尾 N 字节。

## env 操作

`env(action="help")` 列出所有可用操作。
`env(action="help", topic="<操作名>")` 返回具体操作的完整文档。

### 始终可用

| 操作 | 参数 | 描述 |
|------|------|------|
| `help` | `topic?` | 列出所有操作或查看单个操作的文档。 |
| `status` | — | 显示默认机器、机器列表、shell 列表。 |
| `list_targets` | — | 列出配置中预定义的 SSH 目标。 |
| `machine_list` | — | 列出已注册的机器。 |
| `shell_list` | `machine?` | 列出 shell，可按机器过滤。 |
| `shell_new` | `machine?`、`purpose?` | 创建新的 shell 会话。 |
| `shell_remove` | `shell_id` | 终止并移除 shell。 |
| `default_set` | `machine` 或 `shell_id` | 设置默认机器或默认 shell。 |

### Docker

| 操作 | 必需参数 | 描述 |
|------|----------|------|
| `docker_run` | `name`、`image`、`purpose` | 创建/启动容器。名称冲突时自动重新关联。 |
| `docker_ps` | — | 列出受管理的容器。 |
| `docker_images` | — | 列出 Docker 守护进程上的所有镜像。 |
| `docker_image_history` | `image` | 逐层构建历史。 |
| `docker_build` | `image_tag`、`machine` | 基于容器 `/workspace` 中的 Dockerfile 构建镜像。 |
| `docker_commit` | `machine`、`image_tag` | 将容器状态提交为新镜像。 |
| `docker_stop` | `machine` | 停止容器（保留状态）。 |
| `docker_start` | `machine` | 启动已停止的容器。 |
| `docker_remove` | `machine` | 停止并删除容器及其 shell。 |
| `docker_inspect` | `machine` | 查看配置。`kind=image` 查看镜像。 |
| `docker_logs` | `machine` | 单次日志读取，支持 `tail`、`since`、`until`。 |
| `docker_diff` | `machine` | 相对于镜像的文件系统变更（A/C/D）。 |
| `docker_stats` | `machine` | 单次 CPU/内存/网络/IO 快照。 |
| `docker_restart` | `machine` | 停止 + 启动 + 验证。 |

### SSH

| 操作 | 必需参数 | 描述 |
|------|----------|------|
| `connect` | `name` | 连接到配置的目标主机。 |
| `close` | `name` | 断开连接并注销。 |

当配置中存在 `[ssh.targets]` 时可用。

## 文件操作

| 工具 | 关键参数 | 亮点 |
|------|----------|------|
| `file_read` | `path`、`offset`、`limit` | 带行号输出。超过 50 KB 的文件返回提示。 |
| `file_write` | `path`、`content` | 原子写入（临时文件 + 重命名），自动创建父目录，写入后验证。 |
| `file_patch` | `path`、`old_string`、`new_string`（替换模式）或 `patch`（unified diff） | 模糊匹配。保留 BOM 和行尾格式。 |
| `file_search` | `pattern`、`search_type`、`path`、`file_glob`、`limit` | 基于 ripgrep。结果按修改时间排序。 |

对敏感路径（`.ssh`、`.aws`、`.env*`、`/etc/shadow` 等）会触发安全告警，
但仅为建议性质，agent 仍可正常访问。`.json`、`.yaml`、`.yml`、`.toml`
文件写入前会进行语法校验（失败即拒绝）。

## 配置

配置文件位于 `~/.sandbox-mcp/config.toml`（参考
`config/config.example.toml`）。所有字段均可通过
`SANDBOX_MCP_<段>_<键>` 环境变量覆盖。

```toml
[server]
port = 8010
auth_tokens_file = "~/.sandbox-mcp/auth_tokens"

[storage]
work_home = "/var/lib/sandbox-mcp"

[docker]
default_image = "python:3.14-slim"
auto_network = "sandbox-mcp"      # "" = 不使用网络
admin_machine = "admin"           # "" = 不挂载 /host
host = ""                         # "" = 从 Docker 环境变量读取

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

## 后端

### Docker

容器通过 bind mount 实现工作空间隔离：

- `work_home/<名称>/` → `/workspace`（读写）
- `work_home/<共享目录>/` → `/share/`（只读，跨容器共享）
- `work_home/<共享目录>/<名称>/` → `/share/<名称>/`（读写叠加）

当容器名称匹配 `admin_machine` 时，额外挂载
`work_home/` → `/host`（读写）— 全局可见所有工作空间。

服务器启动时自动与 Docker 守护进程同步：仍存活的容器会被重新纳入管理。

### SSH

通过 SSH ControlMaster 连接复用。Windows 目标自动探测远端编码并使用
编码命令执行。

## 部署

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

HTTP 模式从 `auth_tokens_file` 读取 bearer token（每次请求热加载）。
若文件为空或不存在且 `auto_generate_if_empty=true`，
启动时会在 stderr 打印随机 token。

## 审计

每次工具调用均被记录：时间戳、机器、操作、状态、耗时、参数哈希。
默认存入 `~/.sandbox-mcp/audit.db`（SQLite）。设置 `log_path=""`
可改为 stderr JSON-line 输出。
