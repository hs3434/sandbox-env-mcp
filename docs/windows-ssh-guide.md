# Windows SSH 远程连接配置指南

通过 SSH 连接远程 Windows 机器需要完成以下步骤：

## 1. Windows 端：安装 OpenSSH Server

```powershell
# 管理员 PowerShell
Add-WindowsCapability -Online -Name OpenSSH.Server*
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
```

## 2. Windows 端：配置防火墙

```powershell
New-NetFirewallRule -Name "OpenSSH-Server" -DisplayName "OpenSSH Server" `
  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

## 3. MCP 服务器端：生成密钥对

在运行 sandbox-mcp 的 Linux 机器上执行：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/windows_rsa -C "sandbox-mcp-windows"

# 查看公钥（下一步要用）
cat ~/.ssh/windows_rsa.pub
```

## 4. Windows 端：配置公钥认证

```powershell
# 检查用户是否是 Administrators 组成员
whoami /groups | findstr S-1-5-32-544

# === 情况 A：用户是管理员（最常见） ===
# 公钥放入 administrators_authorized_keys
$pubkey = "ssh-ed25519 AAAAC3... sandbox-mcp-windows"  # 替换为你的公钥
$pubkey | Out-File -Encoding UTF8 "C:\ProgramData\ssh\administrators_authorized_keys"

# 设置权限（仅 SYSTEM 和 Administrators 可读）
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /grant "SYSTEM:F"
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /grant "BUILTIN\Administrators:F"

# === 情况 B：用户不是管理员 ===
# 公钥放入用户目录
mkdir $env:USERPROFILE\.ssh -Force
Add-Content $env:USERPROFILE\.ssh\authorized_keys "ssh-ed25519 AAAAC3... sandbox-mcp-windows"

# 设置权限（仅当前用户和 SYSTEM）
icacls $env:USERPROFILE\.ssh\authorized_keys /inheritance:r
icacls $env:USERPROFILE\.ssh\authorized_keys /grant "$env:USERNAME:F"
icacls $env:USERPROFILE\.ssh\authorized_keys /grant "SYSTEM:F"

# 重启 sshd
Restart-Service sshd
```

## 5. 配置 sandbox-mcp

将私钥复制到 sandbox-mcp 配置目录：

```bash
cp ~/.ssh/windows_rsa /path/to/sandbox-mcp/config/windows_rsa
chmod 600 /path/to/sandbox-mcp/config/windows_rsa
```

在 `config.toml` 中添加目标：

```toml
[ssh.targets]
win-build = { host = "10.100.1.1", user = "hs3434", os_type = "windows", shell = "powershell.exe", key = "/home/sandbox/.sandbox-mcp/windows_rsa" }
```

**参数说明：**

| 参数 | 说明 |
|------|------|
| `host` | Windows 机器的 IP 或主机名 |
| `user` | 登录用户名 |
| `os_type` | 固定为 `"windows"`，自动选择 PowerShell 命令生成 |
| `shell` | 固定为 `"powershell.exe"` |
| `key` | 私钥在 sandbox-mcp 容器内的路径 |

### 路径说明

- Docker 部署时，`config.toml` 所在目录（`./config`）被挂载到容器内的 `/home/sandbox/.sandbox-mcp/`
- 所以私钥放在 `config/windows_rsa`，容器内路径为 `/home/sandbox/.sandbox-mcp/windows_rsa`
- 如果 sandbox-mcp 直接运行（非 Docker），使用绝对路径如 `/home/user/.ssh/windows_rsa`

## 6. 验证连接

```bash
# 从 sandbox-mcp 容器直接测试
docker exec sandbox-mcp ssh -i /home/sandbox/.sandbox-mcp/windows_rsa \
  -o StrictHostKeyChecking=no \
  hs3434@10.100.1.1 \
  powershell.exe -Command "Get-Process powershell"

# 通过 MCP 测试
# list_targets()           → 显示 win-build
# connect(name="win-build") → 连接
# shell_exec(command="Get-Process powershell", machine="win-build")
```

## 7. 网络要求

sandbox-mcp 容器需要能够直接访问 Windows 机器的 IP 和端口 22。

- **Docker 部署**：容器默认在隔离的 bridge 网络，可能无法访问宿主机局域网。需要在 `docker-compose.yml` 中设置 `network_mode: host` 或自定义网络。
- **直接部署**：服务直接运行在宿主机上，无需额外网络配置。

`docker-compose.yml` 中启用 host 网络模式：

```yaml
services:
  sandbox-mcp:
    network_mode: host
    # ports:  # host 网络模式下不需要端口映射
```

## 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| `Connection timed out` | IP/端口不可达 | 检查网络、防火墙、sshd 是否运行 |
| `Permission denied (publickey)` | 密钥认证失败 | 检查 authorized_keys 权限（仅用户+SYSTEM） |
| `shell died during health check` | PowerShell 启动参数不对 | 确认 `shell = "powershell.exe"` 且 `-Command` 配置正确 |
| shell_exec 返回空输出 | 命令执行但无返回 | 确保命令有输出（PowerShell 管道输出） |

更多配置选项见 [config.example.toml](config/config.example.toml)。
