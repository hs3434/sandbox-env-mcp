# Sandbox 环境管理 MCP 服务器

<!-- mcp-name: io.github.hs3434/sandbox-env-mcp -->

一个提供持久化沙箱环境管理的 MCP（Model Context Protocol）服务器。
为 AI agent 管理 Docker 容器与 SSH 远程机器（含 Windows 上的
PowerShell-over-SSH）作为执行目标，支持基于 shell 的命令执行和完整的
文件操作能力。

设计用来替代 Hermes Agent 内置的 terminal / file / code_execution 工具，
在内置工具基础上增加持久化的环境管理能力。

## 特性

- **简洁的 MCP 接口**：12 个顶层工具 + 渐进式发现 `sandbox_env`（文件型
  audit 时还会出现 `audit_query`）
- **双传输**：stdio 或 HTTP（streamable-http）
- **多 backend**：Docker 容器（SDK）+ SSH 远程机器（Linux / Windows）
  —— **不再支持 WinRM**，Windows 一律走 SSH
- **持久 PTY shell**：三种 backend（本地、Docker、SSH）都在真 PTY 下
  跑 shell，pager / 提示符等交互行为自然工作
- **一次性随机 Prompt 协议**：shell 启动时只安装一次随机 prompt token
  （bash 用 `PS1`，PowerShell 用 `prompt` 函数），**不再每条命令
  插 marker**；agent 命令按原样写入 stdin，drain 线程靠下一个 prompt
  判定完成
- **持久化机器**：Docker 容器在 MCP 重启后依然存在，可用 `docker_ps` 发现
- **状态机**：shell 只有 `ready` / `waiting` / `terminated` 三态。
  `wait=true`（默认 10 秒超时）超时后返回 `waiting`，并提示长任务改用
  `wait=false` + `shell_read`。`terminated` 默认 shell **不自动替换**，
  agent 必须 `shell_remove` 后再 `shell_new`
- **完整文件操作**：读、写（原子）、patch（模糊匹配）、搜索（ripgrep / glob）
- **进程内 linter**：Python `ast`、JSON、可选 YAML/TOML 写前校验
- **安全提示**：对敏感路径（`.ssh`、`.aws`、`.env*`）的非阻塞警告
- **审计日志**：所有工具调用的 JSON-line 流（内容做哈希）

## 快速开始

### 安装

```bash
pip install .
pip install -e ".[dev]"   # 加上测试 / lint 工具

# 跑单元测试（默认跳过集成测试）
pytest tests/ -v

# 跑集成测试（需要本机 Docker daemon 在跑）
pytest tests/ -m integration -v
```

### 运行

sandbox-mcp 有两种传输模式：

- **`sandbox-mcp-http`** —— 独立 HTTP 服务，从 shell 启动：
  ```bash
  sandbox-mcp-http
  # 然后用任意 MCP 客户端连 http://127.0.0.1:8010/mcp
  ```
- **`sandbox-mcp`**（stdio）—— 由 MCP host 作为子进程拉起。
  不要在 shell 里直接跑这个命令，要在 host 里配置（见下面
  [注册到 Hermes](#注册到-hermesstdio)）。

### 命令行参数

| 参数 | 适用 | 用途 |
|---|---|---|
| `--config PATH` / `-c PATH` | 两者 | TOML 配置文件路径 |
| `--host ADDR` / `-H ADDR` | `sandbox-mcp-http` | HTTP 绑定地址 |
| `--port N` / `-p N` | `sandbox-mcp-http` | HTTP 端口 |

```bash
# 独立 HTTP 服务（默认：streamable-http，监听 /mcp）
sandbox-mcp-http -c /etc/sandbox-mcp/prod.toml --port 9000

# stdio（在 MCP host 的配置里传，不从 shell 跑）
#   下面"注册到 Hermes"小节有完整示例
```

优先级（从高到低）：**CLI 参数** → 环境变量 → 配置文件 → 内置默认值。

### 配置

sandbox-mcp 按以下优先级读配置（从高到低）：

1. **CLI 参数**（见上表）
2. **环境变量** —— `SANDBOX_MCP_*`（例如 `SANDBOX_MCP_SERVER_PORT`）
3. **配置文件** —— 默认 `~/.sandbox-mcp/config.toml`，可用 `--config PATH` 或 `SANDBOX_MCP_CONFIG` 覆盖
4. **内置默认值**（在 `src/sandbox_mcp/config.py` 里声明）

要自定义，把 [`config/config.example.toml`](https://github.com/hs3434/sandbox-env-mcp/blob/main/config/config.example.toml) 拷贝到
`~/.sandbox-mcp/config.toml` 后改需要的字段。保持默认就什么都不用做。

主要配置项：

```toml
[server]                # HTTP 服务
host = "0.0.0.0"
port = 8010
transport = "streamable-http"

[storage]               # 持久化 workspace 目录（必须是主机的绝对路径）
work_home = "/var/lib/sandbox-mcp"   # 不要用 ~ — 原样传给 Docker daemon

[audit]                 # SQLite 审计日志（每次工具调用一行）
log_path = "~/.sandbox-mcp/audit.db"
                        # "" = stderr（隐藏 sandbox_audit_query）；文件 = 启用查询工具

[docker]                # 容器默认设置
default_image = "python:3.14-slim"
restart_policy_name = "on-failure"
restart_max_retry_count = 3

[ssh]
connect_timeout = 10
socket_dir_prefix = "sandbox-mcp-ssh-"
tmpfile_pattern = ".sandbox-mcp-tmp.XXXXXX"

# [ssh.targets.{name}] 是 [default_machine] backend="ssh" 时查找的目标。
# 目标以内联表形式定义：
#   [ssh.targets]
#   my-box = { host = "10.0.0.5", user = "ubuntu" }
#   win-build = { host = "192.168.1.100", user = "builder", os_type = "windows", encoding = "gbk" }

[shell]
# 缓冲区大小和默认每次调用的输出上限（字节）。
default_max_output = 50000
head_size = 5120
tail_size = 46080

[files]
max_file_size = 51200
default_read_limit = 500
max_read_limit = 2000
default_search_limit = 50

[default_machine]       # 可选：启动时自动准备一个默认 machine
enabled = true          # false = 懒加载
backend = "docker"      # "docker" 或 "ssh"
name = "admin"          # 连接参数按 name 查找（targets / image）
purpose = ""            # 详见 config.example.toml
```

每个值都能用环境变量覆盖（大写、点 → 下划线）：

```bash
SANDBOX_MCP_SERVER_PORT=9000 sandbox-mcp-http
SANDBOX_MCP_DOCKER_CONTAINER_NAME_PREFIX="box-" sandbox-mcp
SANDBOX_MCP_AUDIT_LOG_PATH=/var/log/sandbox-mcp/audit.db sandbox-mcp
```

`work_home` 目录会自动创建。`docker_run` 被调用时，会在 `work_home/<机器名>/`
下创建子目录并 bind-mount 到容器内的 `/workspace` —— agent 在 `/workspace`
工作，**永远看不到宿主路径**。

### 启动时准备默认 machine

默认 sandbox-mcp 是**懒加载**的：agent 用 `docker_run` / `connect`
按需创建首个 machine，在此之前没有默认 machine。设置
`[default_machine] enabled = true` 可在启动时直接准备一个默认 machine，
这样 agent 一上来就能用 `shell_exec` / `file_*`：

```toml
[default_machine]
enabled = true
backend = "docker"     # 或 "ssh"
name = "dev"
# docker 在这里不需要别的，镜像用 [docker] default_image。

# SSH 的话，目标在 [ssh.targets.{name}] 里定义：
# [ssh.targets]
# dev = { host = "10.0.0.5", user = "ubuntu" }
```

行为：

- `docker_ps` 认领流程会先跑，所以重启后存活的默认容器会被**重新认领**
  （而不是重建）。
- 准备失败是**致命的**（fail-closed）：既然你开启了此选项，启动时拿不到
  默认 machine 就会让 agent 在首次使用时踩空，不如直接拒绝启动并报清晰错误。
- `name` 默认是 `"admin"`。`admin_machine` 启用且 `name = "admin"` 时，
  `DockerBackend.create` 检测到名字匹配，自动应用
  [admin mount 布局](#admin-机器跨机器运维)。想脱离 admin 改用普通
  peer 作默认，显式设 `name = "dev"` 之类即可。
- `docker_run` 现在**会话内幂等**：若同名容器已存在，会重新挂载（已停止则
  启动），而不是因名字冲突报错。响应里带 `note`（"reattached to existing
  container ..."），让 agent 知道这是复用而非新建。
- `docker_run` 不再覆盖镜像自带的 CMD。容器按镜像作者设定的 CMD / ENTRYPOINT
  运行——`postgres:16` 真的会启动 postgres，`redis:7` 真的会启动 redis，
  等等。想用普通镜像做纯 exec 沙箱，自己 build 一个 `CMD sleep infinity`
  （或任何常驻 shell）的镜像，再对那个镜像 `docker_run`。
- `docker_run` 接受可选的 `shell` 参数（默认 `"bash"`），控制 `docker exec`
  进去时用的二进制。alpine / distroless / busybox 等没带 bash 的镜像，
  设成 `/bin/sh` 之类的即可。
- `start()` / 重新挂载能抓到**快速崩溃**的容器（CMD 在启动后毫秒级退出），
  方法是 start 请求返回后立即 reload state。如果容器在一段时间后才崩溃，
  可能会短暂报 `"running"`，随后才转到 `"exited"`——需要稳健存活检测的话，
  自己用 `docker_inspect` 轮询并加合适延迟。


### 注册到 Hermes

**Stdio 传输**（`sandbox-mcp` 命令）：

加到 `~/.hermes/config.yaml`：

```yaml
mcp_servers:
  sandbox:
    command: sandbox-mcp
    # 可选：给 server 传 CLI 参数。
    args:
      - --config
      - /etc/sandbox-mcp/prod.toml

# 禁用 Hermes 内置工具（可选，避免 schema 重复）
agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

Hermes 把 `sandbox-mcp` 当成子进程拉起，通过它的 stdin/stdout 走 JSON-RPC。
server 没有 UI，只等请求。

**HTTP 传输**（`sandbox-mcp-http` 命令）：

```yaml
mcp_servers:
  sandbox:
    url: "http://localhost:8010/mcp"
    headers:
      Authorization: "Bearer <你的token>"

agent:
  disabled_toolsets:
    - terminal
    - file
    - code_execution
```

Hermes 连到 HTTP MCP 端点（`/mcp`，即 MCP 规范当前的 "Streamable HTTP" 传输）。
适合 MCP server 跑在不同机器上，或作为 systemd 服务管理的情况。

## 工具列表

| 工具 | 用途 |
|---|---|
| `shell_exec` | 在默认（或指定）shell 上跑命令。`wait=true` 阻塞，默认 10 秒超时。 |
| `shell_read` | 读 shell 缓冲区的输出（非阻塞）。 |
| `shell_new` | 在某台 machine 上额外开一个 shell。 |
| `shell_remove` | 终止并移除一个 shell（任意状态）。 |
| `shell_list` | 列出所有 shell（shell_id / machine / state / is_default / ...）。 |
| `sandbox_machine_list` | 列出所有已注册的 machine（backend / status / shell 数 / uptime）。 |
| `sandbox_default_set` | 设置默认 machine 或默认 shell。 |
| `file_read` | 读文本文件，带行号。 |
| `file_write` | 写文件（自动 mkdir、语法检查、原子写）。 |
| `file_patch` | 模糊匹配的定向编辑。 |
| `file_search` | ripgrep 内容搜索 + glob 文件搜索。 |
| `sandbox_env` | 渐进式发现管理动作（`default_set`、`shell_*`、`docker_*`、`connect`、`close`...）。 |
| `sandbox_audit_query` | 读取审计日志（带过滤 / 分页）—— 仅在 `[audit] log_path` 指向文件时启用。 |

### Shell 状态机

每个持久 shell 永远处于三种状态之一：

| 状态 | 含义 | 你能做什么 |
|------|------|-----------|
| `ready` | 坐在 prompt 前，没有命令在跑。 | 发新命令（`shell_exec`）、读缓冲输出（`shell_read`）。 |
| `waiting` | 有命令在跑，drain 线程还没看到下一个 prompt。 | `shell_read` 拿增量输出；若确信 shell 已经空闲才能发下一条命令（否则 API 会返回 `error="waiting"`）。 |
| `terminated` | 底层 shell 进程已退出（`exit`、被 kill、管道断开...）。shell 留在 registry 里，最后的输出还能取到。 | `shell_read` 返回剩余输出加 `status="terminated"`。继续工作：先 `shell_remove` 清理，再用 `shell_new` 开新 shell。**默认 shell 永不自动替换** —— 必须由 agent 显式决定。 |

`shell_exec` 参数：

- `wait`（默认 `true`）：阻塞直到 shell 回到 `ready`。
- `timeout`（默认 `10` 秒）：最长等多久。超时后响应是 `status="waiting"`，
  并带 `hint` 建议长任务改用 `wait=false` + `shell_read`。shell 保持
  `waiting` —— 命令还在目标上跑。
- `max_output`（默认 `50000` 字节）：返回缓冲输出的字节上限。超出时
  返回**尾部**（最后 N 字节）并带截断提示，不是 head+tail。
- `shell_id`：指定某个 shell。默认是该 machine 的默认 shell（首次调用
  时懒创建）。
- `machine`：目标 machine 名。默认是默认 machine。

`shell_read` 返回缓冲输出和当前 state，非阻塞，可放心轮询。

### Prompt 协议 —— 一次性，不逐命令

每个 shell 启动时只安装一次随机 prompt token（**不**再每条命令插 marker）：

- **Bash**：`unset PROMPT_COMMAND; export PS1='<token>:$?>'`
- **PowerShell**：`prompt` 函数，输出 `'<token>:' + $LASTEXITCODE + '>'`

drain 线程扫描这个 token 来判定命令完成（**不**判定 PS2 / 续行 prompt——
保留原样，因为交互程序确实会输出续行 prompt）。发命令就是一次 stdin 写入：
命令 + `\n`。agent 看到的是纯净的命令输出；只有尾部那行 prompt-token 是
服务端用来切状态、捕获 exit code 的。

任何交互 shell 都能正确工作，包括那些会在命令中途打印 prompt 的程序、
pager、需要输入的程序，等等。

## sandbox_env 操作

`sandbox_env` 默认只暴露 `help` 和 `status`。
调用 `action=help` 发现全部操作，包括 Docker 和 SSH 后端动作。
用 `action=list_targets` 查看预定义的 SSH 目标。

| 命名空间 | 操作 |
|---|---|
| 发现 | `help`, `status`, `list_targets` |
| 通用 | `machine_list`, `default_set` |
| Shell | `shell_new`, `shell_list`, `shell_remove` |
| Docker | `docker_run`, `docker_build`, `docker_commit`, `docker_stop`, `docker_start`, `docker_remove`, `docker_restart`, `docker_ps`, `docker_images`, `docker_image_history`, `docker_inspect`, `docker_logs`, `docker_diff`, `docker_stats` |
| SSH | `connect`, `close` |

`docker_run` 是幂等的：如果同名容器已经存在（比如 MCP 重启后），
会重新挂载而不是失败。

### 容器网络

所有 `docker_run` 创建的容器加入同一个 user-defined bridge 网络（默认
`sandbox-mcp`）。这意味着容器之间可以通过你传给 `docker_run` 的 `name`
（DNS 主机名）互相访问：

```python
sandbox_env(action="docker_run", name="db", image="postgres:16")
sandbox_env(action="docker_run", name="dev", image="debian:stable-slim")
# 在名为 "dev" 的容器里：psql -h db
#                              ^ DNS 解析到 "db" 容器的 IP

sandbox_env(action="docker_run", name="web", image="nginx:latest")
# 在名为 "dev" 的容器里：curl http://web
#                              ^ DNS 解析到 "web" 容器的 IP
```

网络名通过 `[docker] auto_network` 配置（默认 `"sandbox-mcp"`）。
设为空字符串可取消自动网络：

```toml
[docker]
auto_network = ""
```

网络在首次 `docker_run` 时惰性创建，没有启动时依赖。

### `docker_build` 用法

agent 永远不接触宿主文件系统。`docker_build` 只接受文件模式：

```python
file_write(path="/workspace/Dockerfile",
                   content="FROM debian:stable-slim\nRUN apt install -y python3\n")
sandbox_env(action="docker_build",
            machine="dev",
            image_tag="myapp:v1")
# 默认 dockerfile=/workspace/Dockerfile, context_dir=/workspace
# sandbox-mcp 自动把容器路径翻译成宿主 work_home/<machine>/ 下的路径
```

**沙箱边界保护**：`dockerfile` 和 `context_dir` 必须在 `/workspace/` 下，
宿主路径会被拒绝 —— 防止 agent 读到 `work_home` 之外的文件。只有
`/workspace/` 子树被 bind-mount 到宿主，所以容器里的 `/etc/foo`
这样的文件在宿主侧根本没有对应物，docker daemon 读不到；build
会直接报 "context not a directory"，即便 agent 自己 `shell_exec`
能看到那个文件。

> **为什么没有内联 `dockerfile_content`？** 内联模式会跳过 sandbox
> 的 file-write 审计链，而且 Dockerfile 直接喂给 docker daemon，build
> 步骤以宿主内核全能力执行（BuildKit `--mount=type=bind,source=/,...`）。
> 强制要求 agent 先用 `file_write` 把 Dockerfile 落到磁盘，
> 保证每行可审计、build context 留在 `work_home` 内。

### 检查镜像和容器

```python
# 容器视图：状态、cmd、entrypoint、挂载、labels、重启策略。
# Env 值故意省略（用 shell_exec env/pwd/whoami 拿运行时 env）。
sandbox_env(action="docker_inspect", machine="dev")

# 镜像视图：身份、tags、大小、cmd/entrypoint、env KEYS（值脱敏）、
# 暴露端口、声明的卷、labels、working_dir、user。
# 接受任意镜像引用：name:tag、short id、full id。
sandbox_env(action="docker_inspect", machine="python:3.12", kind="image")
sandbox_env(action="docker_inspect", machine="sha256:abc123def456", kind="image")

# 单个镜像的 layer-by-layer 构建历史（对应 `docker history`）。
# 查单个镜像的来历用这个；枚举多个镜像用 docker_images。
sandbox_env(action="docker_image_history", image="python:3.12")
# 返回：{image, layers: [{id (12-hex), created, created_by, size_bytes, tags}], total_size_bytes, layer_count}
```

`docker_inspect`（容器视图）、`docker_logs`、`docker_diff`、`docker_stats`、
`docker_restart` 都操作**托管机器**（`docker_run` 创建的容器）。
`docker_inspect` 配 `kind="image"` 是唯一的例外：它直接接受镜像引用，完全
不碰 registry。`docker_logs` 和 `docker_diff` 严格 container-only——镜像没有
日志流，也没有 overlay 文件系统可 diff；想查镜像来历，用 `docker_image_history`。

### `docker_run` 沙箱边界

agent 无法把宿主路径走私进容器：

- **`volumes=[]` 不接受**（Docker SDK 的原始 `volumes` 形参）。
  `volumes=["/:/host", "/etc:/host-etc"]` 会被静默丢弃。
- 自动挂载集合**固定**：每台机器的工作目录（`work_home/<name>` → `/workspace`，rw）
  + 容器间共享目录（见下）。没有 per-run 挂载参数 —— agent 通过 sandbox-mcp
  无法触达任意宿主路径。
- agent 可以在容器里跑任何镜像、`docker exec` 任何命令，但**不能**挂载
  任意宿主路径、不能从容器内读宿主的 `/etc`、`/root` 等。

#### 容器间共享目录

每次 `docker_run` 都会自动 bind-mount `work_home/<share_subdir>/`
（默认 `_share/`）到容器内的 `/share/`。挂载规格固定为两条
bind，跟 peer 数量无关：

1. 整个 share 根目录以 **ro** 挂到 `/share/`
2. 容器自己的子目录 `work_home/_share/<machine>/` 以 **rw** 覆盖到
   `/share/<machine>/` —— agent 可以写自己的产物，但 ro
   父挂载会阻止对任何 peer 子目录的写（内核 mount flag 强制）

约定：

```text
# 在 "dev" 容器内：
echo "build output" > /share/dev/result.txt       # self rw
cat /share/alice/notes.md                         # peer ro（经父挂载）
ls /share/                                        # 发现 peer
```

**新 peer 自动可见**。因为父挂载覆盖整个 `_share/` 树，内核在访问时
才解析其内容 —— 容器启动后**新加**的 peer 子目录，下次 `ls` 就能看到，
不需要重建容器。

关闭：`[storage] share_subdir = ""`（env：`SANDBOX_MCP_STORAGE_SHARE_SUBDIR`）。

这是 sandbox 文件写入边界向 `docker_run` 的延伸 —— **第一道防线**。
容器与宿主共享内核，内核能力逃逸（`unshare`、内核 CVE）仍需 rootless
docker 或 gVisor（`runsc`）等更强的隔离手段来堵。

#### Admin 机器（跨机器运维）

**Admin 机器**是一个特殊容器，完全靠**名字**识别。两个配置协同：

- **`[docker] admin_machine`**（`admin` 默认；空字符串关闭）——
  **功能开关 + 名字**。非空时，`DockerBackend.create` 检测到匹配的
  名字就走下面的 god-mode mount；为空时，任何名字都不触发。
- **`[default_machine] enabled = true`** + `name = "admin"`（默认值）
  —— 通过既有的 default-machine 机制真正创建容器。把 `name` 改成
  其他（如 `"dev"`）就脱离 admin，改为普通 peer 作默认。

普通 peer 的 mount 布局：

| 容器内挂载点              | 宿主源路径                          | 模式 |
|---------------------------|-------------------------------------|------|
| `/workspace`              | `work_home/<name>/`                 | rw   |
| `/share`        | `work_home/_share/`                 | ro   |
| `/share/<self>` | `work_home/_share/<self>/`          | rw   |

Admin mount 布局（当 `name == admin_machine`）：

  | 容器内挂载点 | 宿主源路径              | 模式 | 用途 |
  |--------------|-------------------------|------|------|
  | `/workspace` | `work_home/admin/`      | rw   | admin 自己的 scratch |
  | `/host`      | `work_home/`（整棵）    | rw   | 全局视图：所有 peer + share |

  跳过 share bindings（`/share/*`）—— 全局 `/host` 挂载已覆盖 `work_home/_share/`。

**约定：**

```text
# 在 "admin" 容器内：
ls /workspace/             # admin 自己的 scratch
ls /host/                  # 所有 workspace + _share + admin/
ls /host/dev/              # peer 的 workspace（约定上只读）
rm -rf /host/dev/build     # 清理 peer 的陈旧构建
cp /workspace/notes.txt /host/alice/        # 投递给 peer
cat /host/_share/bob/log.txt                # 读 peer 的 share 输出
```

**为什么两条 mount：** 让 agent 默认在 `/workspace` 写自己的东西；要跨机器
操作必须显式走 `/host/<peer>/...`，这样 agent 的命令历史能清楚看到「这是
跨机器操作」。两个路径在 `work_home/admin/` 上重叠（同一组 inode），任一
路径写入都落到同一份数据。

**WARNING — god-mode 容器。** `/host` 是 rw 且覆盖所有 peer 的 workspace。
这里的操作**不可逆**，可能影响正在运行的 peer。慎用 —— 优先让 peer 各自清理
自己的 `/workspace`。

**配置示例** —— 让 admin 成为初始默认：

```toml
[docker]
admin_machine = "admin"   # 功能开关（默认 ON，名字 = "admin"）

[default_machine]
enabled = true            # 启用自动创建
name = "admin"            # 默认 —— 触发 admin 容器创建
```

`default_machine` 调用 `DockerBackend.create("admin", ...)`，
`create` 检测到名字匹配就用 god-mode mount。server 启动路径里没有任何
admin 特判。

**升级已有部署：** 如果名为 `admin` 的容器已经作为 peer 存在（挂的是
`work_home/admin/`），先 `docker_remove admin`（或 host 上 `docker rm admin`），
server 下次启动会按 admin 规则重建。不自动迁移 —— 否则会沉默失败。

关闭：`[docker] admin_machine = ""`（env：`SANDBOX_MCP_DOCKER_ADMIN_MACHINE`）。
设为空后，`admin` 这个名字就是普通 peer（自己 mount + share，没有 `/host`）。

### 连接远程 Docker Daemon

默认 `sandbox-mcp` 跟本地 docker daemon 通信
（`unix:///var/run/docker.sock`，或 `$DOCKER_HOST` 指向的位置）。
要指向远程 daemon，在 `config.toml` 设 `[docker] host`（环境变量
`SANDBOX_MCP_DOCKER_HOST` 覆盖）：

```toml
# 远程 daemon，走 TLS（推荐用于非本地 daemon）
[docker]
host = "tcp://docker.internal:2376"
tls_verify = true
cert_path = "/etc/sandbox-mcp/docker-certs"

# 或走 SSH 信任（用 paramiko，无需证书）
# host = "ssh://deploy@docker-prod.internal"

# 容器内挂载的 socket 路径不同时
# host = "unix:///var/run/docker.sock"
```

URL 协议头（`unix://` / `tcp://` / `ssh://`）决定传输方式。
完整选项见 [`config/config.example.toml`](https://github.com/hs3434/sandbox-env-mcp/blob/main/config/config.example.toml)。

## HTTP 鉴权

HTTP 模式（`sandbox-mcp-http`）需要 bearer token 鉴权。token 存在文件里，一行一个：

```
~/.sandbox-mcp/auth_tokens           # 默认路径
```

**文件必须 0600 权限**，否则 sandbox-mcp 拒绝启动（fail-closed）：

```bash
chmod 600 ~/.sandbox-mcp/auth_tokens
```

路径可在 `config.toml` 里改：

```toml
[server]
auth_tokens_file = "/etc/sandbox-mcp/auth_tokens"
```

或通过环境变量（优先级最高）：

```bash
SANDBOX_MCP_SERVER_AUTH_TOKENS_FILE=/run/secrets/auth_tokens sandbox-mcp-http
```

MCP 客户端连接时传 `Authorization: Bearer <token>` header：

```bash
# 默认 streamable-http 传输
curl -X POST -H "Authorization: Bearer <你的token>" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' \
     http://127.0.0.1:8010/mcp
```

### 自动生成开发用 token

在 `config.toml` 里设 `auto_generate_if_empty = true`，
或导出 `SANDBOX_MCP_SERVER_AUTO_GENERATE_IF_EMPTY=true`。
如果 token 文件不存在或为空，启动时生成一个临时 token 并打印到 stderr：

```
[sandbox-mcp-http] WARNING: no tokens found at ~/.sandbox-mcp/auth_tokens.
Generated ephemeral token (capture now, will not be shown again):
  XKTUv1Gjv2...33-chars-long
Pass it as: Authorization: Bearer <token>
```

拷贝这个 token 给当前 session 用。server 重启后不会重复生成同一个（文件还在会读文件）。

## Windows 支持

sandbox-mcp **仅**通过 SSH 支持 Windows（**项目里没有 WinRM backend**）。
两种方式得到 Windows 目标：

### 1. SSH 连接远程 Windows

连接任何装了 OpenSSH Server 的 Windows 机器。
详细配置见 [Windows SSH 配置指南](https://github.com/hs3434/sandbox-env-mcp/blob/main/docs/windows-ssh-guide.md)。

```toml
[ssh.targets]
win-build = { host = "10.100.1.1", user = "builder", os_type = "windows", encoding = "gbk", key = "/home/sandbox/.sandbox-mcp/windows_rsa" }
```

```python
sandbox_env(action="connect", name="win-build")
```

`os_type = "windows"` 选择 PowerShell provider（设置 UTF-8 控制台编码、
装 prompt 函数、禁用 PSReadLine 的续行 prompt）。`encoding` 默认 `gbk`
以兼容 Windows 代码页。

### 2. Docker Windows 容器

当 sandbox-mcp 跑在 Windows 宿主机上且 Docker Desktop 处于 Windows 容器模式时：

```python
sandbox_env(action="docker_run",
            name="winbox",
            image="mcr.microsoft.com/windows/servercore:ltsc2022")
# os_type 会从镜像的 Os 字段自动判定
```

后端通过镜像元数据的 `Os` 字段自动检测 Windows 镜像，并选用 PowerShell 作为
默认 shell。

### 发现可用目标

```python
sandbox_env(action="list_targets")
# 返回 config.toml 里预定义的 SSH 目标
```

## 限制

- **没有 WinRM**。远程 Windows 一律走 SSH（Windows 宿主机装 OpenSSH Server）。
- **Backend 只有 Docker 和 SSH**。没有独立的 local-backend / 进程内
  pseudo-target —— 但本地 PTY 代码路径由测试覆盖（也是 SSH tunnel
  回环到本机时走的路径）。
- **持久 shell 跑在真 PTY 下**。bash 和 PowerShell（over SSH）以及目标
  上的任何交互 shell 都能正常工作。
- **状态在内存里**。Shell session 服务端重启后丢失，重新 `shell_new`。
  容器能跨重启存活，重新 `docker_run` 挂载，或 `docker_ps` 查看。
- **`terminated` shell 不自动替换**。Agent 跑 `exit`（或 shell 因其他
  原因挂掉）后，shell 进入 `terminated` 状态。Agent 需要先调用 `shell_remove`
  清理，再用 `shell_new` 创建新的 shell；或者用 `default_set(shell_id=...)`
  切换到其他已就绪的 shell。
- **没有 session 隔离**。多个 agent 连同一个 server 共享 machine / shell
  registry。这跟 Hermes 自带的 MCP 行为一致。

## 架构概览

```text
Agent (LLM)
  │
  ▼
MCP Client (Hermes Gateway | 任意 MCP host)
  │  JSON-RPC over stdio │  或  │ HTTP (/mcp)
  ▼                              ▼
sandbox-mcp                     sandbox-mcp-http
  │  (stdio transport)           │  (streamable-http, port 8010)
  │                              │
  └──────────┬───────────────────┘
             │
             ▼
      Application Layer
  ┌──────────────────────┐
  │ 12 个 MCP 工具 + env │
  │ sandbox_env 调度      │
  │ ShellSession / ShellReg│
  │ MachineRegistry       │
  │ FileOperations        │
  │ AuditLogger / Safety  │
  └──────────┬───────────┘
             │
      ┌──────┴───────┐
      ▼              ▼
   Docker SDK     SSH (key-only)
   (SDK exec,     (ControlMaster,
    tty=True)      ssh -tt, PTY)
```

三种 shell 类型 —— 本地 PTY（测试用）、Docker exec（`tty=True`）、SSH
（`ssh -tt`） —— 都在真 PTY 下跑 shell，共用同一套 `ShellSession` drain
线程逻辑。Prompt 协议（token + prompt 函数）只装一次；不再用逐命令 marker。

## 设计

设计规格见 [docs/design-spec-v2.md](https://github.com/hs3434/sandbox-env-mcp/blob/main/docs/design-spec-v2.md)。
实现笔记见 [docs/implementation-plan.md](https://github.com/hs3434/sandbox-env-mcp/blob/main/docs/implementation-plan.md)。

## 贡献

```bash
# 跑本地 CI（跟 GitHub Actions 一致）
./scripts/ci.sh
```

## 许可证

本项目采用**双重许可（dual-licensed）**：

- **开源使用** — [GNU Affero General Public License v3.0](LICENSE)
  （AGPL-3.0-only）。你可以依据 AGPLv3 的条款自由使用、修改和分发本软件，
  包括将修改版本作为网络服务提供给用户时，必须同时公开源代码的要求。
- **商业使用** — 如果你希望在闭源或专有场景中使用本软件而不受 AGPLv3
  条款约束（特别是 AGPLv3 § 13 的源码披露义务），可获取独立的商业许可。
  请发邮件至 **1606272735@qq.com**，描述你的预期用途；具体条款（费用、
  期限、席位等）按单次洽谈。

### 贡献

提交贡献即表示你同意项目的[贡献条款](CONTRIBUTING.md#1-贡献者授权与签名sign-off)：
你的贡献按 AGPLv3 授权，且维护者有权按上述商业条款再许可。开发流程、
代码风格与签名说明见 [**CONTRIBUTING.md**](CONTRIBUTING.md)。