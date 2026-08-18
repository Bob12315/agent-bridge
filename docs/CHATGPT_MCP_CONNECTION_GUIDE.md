# Agent Bridge 连接 ChatGPT 插件 MCP 完整操作说明

> 适用环境：Windows 10/11、Python 3.11+、Agent Bridge V1
> 文档日期：2026-08-18

## 1. 最终目标

完成配置后，ChatGPT 可以通过插件中的 MCP 连接调用本机 Agent Bridge，并发现以下七个工具：

```text
bridge_create_session
bridge_inspect
bridge_send
bridge_wait
bridge_status
bridge_cancel
bridge_close_session
```

推荐连接结构：

```text
ChatGPT
   │
   │ OpenAI Secure MCP Tunnel
   ▼
tunnel-client（本机运行）
   │
   │ http://127.0.0.1:8001/mcp
   ▼
Agent Bridge MCP Server
   ├── DeepSeek Executor
   └── Codex Reviewer
```

本机地址 `http://127.0.0.1:8001/mcp` 不能直接填写到 ChatGPT 中，因为 ChatGPT 无法访问用户电脑的回环地址。开发测试应使用 Secure MCP Tunnel；公开发布则需要稳定的公网 HTTPS MCP 地址。

## 2. 开始前准备

### 2.1 必备项目

- Windows PowerShell。
- Python 3.11 或更高版本。
- Git。
- 已下载的 Agent Bridge 项目。
- 能使用 ChatGPT 插件和开发者模式的 ChatGPT 账号。
- 能访问 OpenAI Platform Tunnel 设置的账号。
- `tunnel_id` 和仅供 `tunnel-client` 使用的运行时 API Key。
- 如果要实际调用 DeepSeek 和 Codex：相应 CLI 已安装并完成登录或 API Key 配置。

ChatGPT 开发者模式和 Platform Tunnel 权限是两套独立权限。团队、Enterprise 或 Edu 工作区可能需要管理员授权。Tunnel 至少需要 Read + Use；创建或修改 Tunnel 还需要 Read + Manage。

### 2.2 网络要求

运行 `tunnel-client` 的电脑需要：

- 能访问本机 `http://127.0.0.1:8001/mcp`。
- 能通过 HTTPS 出站访问 `api.openai.com:443`。
- 不需要开放路由器端口，也不需要允许公网主动访问电脑。

## 3. 安装 Agent Bridge

打开 PowerShell，执行：

```powershell
Set-Location "C:\Users\level6\Desktop\mcp bridge\agent-bridge"
python --version
git --version
```

创建虚拟环境并安装项目：

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

运行测试：

```powershell
python -m pytest
```

测试通过后再继续。

## 4. 配置 Agent Bridge

首次使用时创建个人配置，避免直接修改示例文件：

```powershell
Copy-Item .\config\config.example.yaml .\config\config.local.yaml
notepad .\config\config.local.yaml
```

MCP 部分保持如下：

```yaml
mcp:
  transport: "streamable-http"
  host: "127.0.0.1"
  port: 8001
  path: "/mcp"
```

### 4.1 只测试 ChatGPT 与 MCP 连通性

可以暂时保留模拟执行器：

```yaml
deepseek:
  enabled: false
  transport: "mock"

codex:
  enabled: false
  transport: "mock"
```

这适合先确认 ChatGPT 能发现并调用七个 Bridge 工具。

### 4.2 启用真实 DeepSeek 和 Codex

确认两套 CLI 在 PowerShell 中能正常运行后，将配置改为：

```yaml
deepseek:
  enabled: true
  transport: "cli"
  executable: "deepseek"
  timeout_seconds: 1800
  health_timeout_seconds: 15

codex:
  enabled: true
  transport: "cli"
  executable: "codex"
  timeout_seconds: 1800
  health_timeout_seconds: 15
```

如果可执行文件不在系统 PATH 中，把 `executable` 改为它的完整路径。

## 5. 启动本地 MCP Server

在第一个 PowerShell 窗口中执行：

```powershell
Set-Location "C:\Users\level6\Desktop\mcp bridge\agent-bridge"
.\.venv\Scripts\Activate.ps1
python -m app.mcp.server --config config/config.local.yaml --transport streamable-http
```

保持该窗口运行。默认 MCP 地址是：

```text
http://127.0.0.1:8001/mcp
```

如果提示端口被占用，可以在 `config.local.yaml` 中修改 `mcp.port`，后续 Tunnel 地址也要同步修改。

## 6. 可选：启动监控面板

另开一个 PowerShell 窗口：

```powershell
Set-Location "C:\Users\level6\Desktop\mcp bridge\agent-bridge"
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开：

```text
http://127.0.0.1:8000
```

监控面板不是 ChatGPT 的 MCP 地址。ChatGPT 使用的是端口 8001 的 `/mcp`。

## 7. 在 OpenAI Platform 创建 Secure MCP Tunnel

1. 登录 OpenAI Platform。
2. 打开 Tunnel 设置页面。
3. 选择正确的 Platform Organization。
4. 创建一个 Tunnel，例如命名为 `agent-bridge-local`。
5. 将目标 ChatGPT Workspace 与该 Tunnel 关联。
6. 保存系统生成的 `tunnel_id`，格式类似：

```text
tunnel_0123456789abcdef0123456789abcdef
```

7. 创建一个供 `tunnel-client` 使用的运行时 API Key。

不要把 API Key 写入项目文件、聊天内容、截图或 Git 提交中。

## 8. 安装并配置 tunnel-client

从 OpenAI Platform Tunnel 设置页面提供的下载入口获取最新版 `tunnel-client`。下载后，在第二个 PowerShell 窗口进入该程序所在目录。

先查看当前版本支持的参数：

```powershell
.\tunnel-client.exe help quickstart
```

仅在当前 PowerShell 会话中设置运行时密钥：

```powershell
$env:CONTROL_PLANE_API_KEY = "在这里填写运行时API Key"
```

初始化 Agent Bridge 的 HTTP MCP 配置。请把示例 Tunnel ID 换成自己的值：

```powershell
.\tunnel-client.exe init `
  --sample sample_mcp_stdio_local `
  --profile agent-bridge `
  --tunnel-id tunnel_0123456789abcdef0123456789abcdef `
  --mcp-server-url "http://127.0.0.1:8001/mcp"
```

不同版本的 `tunnel-client` 参数可能更新。如果上述命令提示未知参数，以 `help quickstart` 和 OpenAI Platform 下载页面生成的命令为准；关键配置是同一个 `tunnel_id` 和本地地址 `http://127.0.0.1:8001/mcp`。

执行诊断：

```powershell
.\tunnel-client.exe doctor --profile agent-bridge --explain
```

诊断通过后启动 Tunnel：

```powershell
.\tunnel-client.exe run --profile agent-bridge
```

保持这个窗口运行。关闭它以后，ChatGPT 将无法继续调用本机 Bridge。

`tunnel-client` 运行时通常还会提供仅限本机访问的管理界面 `/ui`，以及 `/healthz`、`/readyz` 和 `/metrics`。实际端口以程序启动输出为准。

## 9. 在 ChatGPT 中添加 Agent Bridge

### 9.1 打开开发者模式

在 ChatGPT 中：

1. 打开“设置”。
2. 进入“安全与登录”（Security and login）。
3. 开启“开发者模式”（Developer mode）。

如果没有该开关，通常是账号暂未开放，或工作区管理员没有授权。

### 9.2 创建 MCP 插件连接

1. 打开 ChatGPT Plugins 页面。
2. 点击加号创建开发者模式插件。
3. 名称填写：`Agent Bridge`。
4. 描述可填写：`连接本机 DeepSeek Executor 与 Codex Reviewer 的任务通信桥。`
5. 在 Connection 中选择 `Tunnel`。
6. 从列表选择 `agent-bridge-local`；如果没有显示，手动粘贴 `tunnel_id`。
7. 创建连接。
8. 检查 ChatGPT 发现的工具是否正好为七个 Bridge 工具。

如果工具名称、描述或参数后来发生变化，请重启 MCP Server，然后在 ChatGPT 的插件连接页面点击 Refresh，并新建一个对话重新测试。

## 10. 首次连通测试

开始新对话，在工具菜单中启用 Agent Bridge。建议依次发送以下指令。

### 10.1 查看工具

```text
请列出 Agent Bridge 当前提供的 MCP 工具，不要调用它们。
```

应看到七个工具，没有多余工具。

### 10.2 创建 Bridge Session

把仓库路径改为实际需要操作的 Git 项目，并确认该项目存在 `main` 分支：

```text
请使用 Agent Bridge 创建一个会话：
project_name 为 agent-bridge-test，
repo_path 为 C:\Users\level6\Desktop\mcp bridge\agent-bridge，
base_branch 为 main，access_mode 为 develop。
请返回 session_id 和 workspace。
```

保存返回的 `session_id`。Bridge 会为该会话建立独立 Git Worktree，不会直接让 Executor 修改原始工作目录。

### 10.3 只读检查项目

只读需求优先使用 `bridge_inspect`，而不是让 DeepSeek 执行自然语言任务：

```text
请调用 bridge_inspect：使用刚才的 session_id，operation 为 list_files。
只返回 Session Worktree 内的文件列表。
```

可用 operation 为：`list_files`、`read_file`、`search_text`、`git_status`、`git_log`、`git_diff`。该工具没有任意 shell、网络或写入能力。

### 10.4 向 DeepSeek 发送开发任务

```text
请调用 bridge_send，把任务发给 deepseek。
使用刚才的 session_id，type 使用 task，execution_mode 使用 develop，stage 为 1，round 为 1。
任务内容：只修改 Session Worktree 内实现所需的文件，并在完成后总结。
```

如果返回 `running`，保存 `request_id`，然后发送：

```text
请调用 bridge_wait 等待刚才的 request_id，timeout 为 30 秒。
如果仍在运行，只报告状态，不要重复发送任务。
```

### 10.5 请求 Codex 审核

```text
请调用 bridge_send，把审核请求发给 codex。
使用当前 session_id，type 使用 review_request，execution_mode 使用 review，stage 为 1，round 为 1。
要求 Codex 只读检查当前 Worktree，并返回 PASS 或 CHANGES_REQUIRED。
```

### 10.6 关闭 Session

确认没有 `queued` 或 `running` 请求后：

```text
请调用 bridge_close_session 关闭当前 session_id，并告诉我 workspace 是否已移除。
```

关闭 Session 会移除对应 Worktree，但保留它的 Git 分支，避免丢失工作成果。

## 11. 每次使用时的启动顺序

1. 启动 Agent Bridge MCP Server。
2. 确认 `http://127.0.0.1:8001/mcp` 对应服务正在运行。
3. 设置 `CONTROL_PLANE_API_KEY` 并启动 `tunnel-client`。
4. 运行 `tunnel-client doctor`，确认 Tunnel ready。
5. 打开 ChatGPT，新建对话并启用 Agent Bridge 插件。
6. 创建 Session，再发送任务。
7. 使用完毕后关闭 Session。
8. 停止 Tunnel 和 MCP Server。

## 12. 安全停止服务

先在 ChatGPT 中等待或取消所有运行中的请求，然后关闭 Session。之后分别在两个 PowerShell 窗口按 `Ctrl+C`：

1. 先停止 `tunnel-client`。
2. 再停止 Agent Bridge MCP Server。
3. 如果启动了监控面板，最后停止 Uvicorn。

清除当前 PowerShell 会话中的密钥：

```powershell
Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue
```

## 13. 常见故障排查

### 13.1 ChatGPT 中没有“开发者模式”

- 确认账号支持该功能。
- 如果使用团队、Enterprise 或 Edu 工作区，请让管理员开放开发者模式。
- 退出并重新登录 ChatGPT 后再检查。

### 13.2 ChatGPT 中看不到 Tunnel

- 确认 Tunnel 已关联目标 ChatGPT Workspace，而不只是 Platform Organization。
- 确认当前账号拥有 Tunnels Read + Use。
- 新授权可能需要一段时间才能生效。
- 可以在创建插件时直接粘贴 `tunnel_id`。

### 13.3 ChatGPT 显示无法连接 MCP

按顺序检查：

1. Agent Bridge MCP Server 窗口是否仍在运行。
2. 端口是否与 Tunnel 配置一致。
3. `tunnel-client run --profile agent-bridge` 是否仍在运行。
4. 重新执行：

```powershell
.\tunnel-client.exe doctor --profile agent-bridge --explain
```

5. 检查防火墙或代理是否允许访问 `api.openai.com:443`。
6. 确认 Tunnel 与 ChatGPT 使用的是同一个组织和工作区。

### 13.4 只能看到部分工具或工具定义没有更新

- 重启 Agent Bridge MCP Server。
- 在 ChatGPT 插件页面打开该连接并点击 Refresh。
- 新建对话，不要继续使用缓存旧工具定义的对话。

### 13.5 DeepSeek 或 Codex 调用失败

- 先分别在 PowerShell 中确认 `deepseek` 和 `codex` 命令能运行。
- 确认 CLI 已登录或已配置 API Key。
- 检查 `config.local.yaml` 中的 `enabled`、`transport` 和 `executable`。
- 查看 `runtime/logs` 和本地监控面板中的错误信息。
- 这类错误通常表示 ChatGPT → Tunnel → MCP 已连通，只是后端执行器尚未就绪。

### 13.6 创建 Session 失败

- `repo_path` 必须指向现有 Git 仓库。
- `base_branch` 必须真实存在。
- 确认 Git 可用且项目目录有写入权限。
- 不要对同一个 Session 重复创建相同 Worktree。

### 13.7 请求一直处于 running

- 使用 `bridge_status` 查询，不要重复调用 `bridge_send`。
- 使用 `bridge_wait` 继续等待。
- 确认对应 CLI 进程仍在运行。
- 确实需要停止时再调用 `bridge_cancel`。

## 14. 公网 HTTPS 连接方案

如果不使用 Secure MCP Tunnel，可以把 Agent Bridge 部署到服务器，并向 ChatGPT 提供：

```text
https://你的域名/mcp
```

公网方案至少需要：

- 稳定域名和有效 HTTPS 证书。
- Streamable HTTP MCP 端点。
- 严格的身份验证和授权。
- 限流、日志脱敏、超时和异常处理。
- 不直接暴露本地工作区、数据库或管理端口。

当前 Agent Bridge 的默认地址只监听 `127.0.0.1`，并且尚未把公网身份验证作为默认部署能力。因此不要简单地把端口 8001 映射到公网。Secure MCP Tunnel 适合当前的个人开发和私有测试；Tunnel 不能代替公开插件提交所要求的公网 HTTPS MCP 服务。

## 15. 验收清单

- [ ] Python、Git 和项目依赖安装完成。
- [ ] `python -m pytest` 通过。
- [ ] Agent Bridge MCP Server 在端口 8001 运行。
- [ ] OpenAI Platform 已创建 Tunnel。
- [ ] Tunnel 已关联正确的 ChatGPT Workspace。
- [ ] `tunnel-client doctor` 检查通过。
- [ ] `tunnel-client run` 持续运行。
- [ ] ChatGPT 开发者模式已开启。
- [ ] ChatGPT 插件连接已创建。
- [ ] ChatGPT 能发现七个 Bridge 工具。
- [ ] 能创建 Session 并获得 `session_id`。
- [ ] 能向 DeepSeek 或模拟执行器发送任务。
- [ ] 能向 Codex 或模拟审核器发送审核请求。
- [ ] 能查询、等待、取消请求并关闭 Session。

## 16. 官方参考资料

- OpenAI：Connect and test your plugin
  https://developers.openai.com/plugins/deploy/connect-chatgpt
- OpenAI：Secure MCP Tunnel
  https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
