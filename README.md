# Agent Bridge

Agent Bridge 是 ChatGPT Web、DeepSeek Executor 与 Codex Reviewer 之间的跨平台通信中间层。Bridge 只验证、保存和转发消息；所有阶段、轮次、返工与推进决策均由 ChatGPT 明确发起。

当前完成前八个阶段：Foundation、Session + Git Workspace、Async Request Manager、Web Dashboard、MCP Server、DeepSeek Executor Adapter、Codex Reviewer Adapter 和完整端到端链路验证。系统现在支持请求快速路径、后台执行、状态查询、等待、失败处理、取消、跨平台进程树管理，以及通过 MCP 进行完整的一跳通信。

## 开发环境

需要 Python 3.11+。在 Windows PowerShell 或 Ubuntu shell 中进入项目目录后：

```text
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest
```

所有运行路径均由 `config/config.example.yaml` 配置，并使用跨平台相对路径。核心代码不依赖 PowerShell、cmd 或 bash。

## Session Workspace

每个 Session 从指定 base branch 的 commit 创建独立 Git Worktree：

```text
runtime/workspaces/<session-id>/repo
```

Executor 后续只在该 Worktree 中读写。关闭 Session 时会移除 Worktree，但保留其 Git 分支，避免丢失尚未合并的工作成果。原始仓库的工作目录不会被 Agent 直接修改。

## 核心约束

一次 `Router.route()` 只选择一个 receiver、调用一个 Adapter 一次，并返回该 Agent 的一条响应。Router 不检查审核 verdict 来触发后续工作，也不会自动调用其他 Agent。

## Async Requests

`RequestManager.send()` 会先持久化 Message 和 Request，再执行一次 Router turn。目标 Agent 在同步等待窗口内完成时直接返回结果，否则返回同一 `request_id` 的 `running` 状态。后续 `wait()` 和 `status()` 只观察该请求，不会启动新的 Agent turn；`cancel()` 会终止运行中的 Adapter turn或安全取消尚在排队的请求。

## Web Dashboard

启动本地监控台：

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。Dashboard 提供 Session 列表、Agent 状态、显式 Stage/Round、当前活动、Message/Event 时间线、请求耗时、错误详情和 Cancel 操作。`/api/events` 提供 SSE 实时事件流；页面也保留低频刷新作为连接中断时的回退。

## MCP Server

本地 MCP Host 默认通过 stdio 启动 Bridge：

```text
python -m app.mcp.server --config config/config.example.yaml
```

需要通过网络连接时，可启动 Streamable HTTP：

```text
python -m app.mcp.server --config config/config.example.yaml --transport streamable-http
```

默认端点为 `http://127.0.0.1:8001/mcp`，可在配置文件的 `mcp` 部分修改。V1 只暴露以下六个工具：

```text
bridge_create_session
bridge_send
bridge_wait
bridge_status
bridge_cancel
bridge_close_session
```

`bridge_send` 的输入中没有 `sender`、`id` 或 `created_at`；Bridge 会强制发送者为 ChatGPT 并生成其余字段。每次调用只执行目标 Agent 的一次 turn。`bridge_wait` 和 `bridge_status` 只观察已有请求，不会触发新的 Agent 调用。工具失败会返回稳定的 `error.code` 和 `error.message`，不会向客户端暴露内部 traceback。

## DeepSeek Executor

第六阶段通过独立的 CLI Transport 接入支持 DeepSeek 的终端执行器。默认命令为 `deepseek`，也可将 `executable` 改为兼容 Codewhale `exec --output-format stream-json` 协议的命令。执行器必须已完成 API Key 配置。

配置示例：

```yaml
deepseek:
  enabled: true
  transport: "cli"
  executable: "deepseek"
  timeout_seconds: 1800
  health_timeout_seconds: 15
```

每个 Bridge Session 会保持一个 Executor Session。第一次调用创建执行上下文，后续调用使用外部 Session ID 恢复；CLI 的 cwd 和 `--workspace` 都固定为该 Session 的 Git Worktree。执行使用参数数组而非 shell 字符串，并支持进程树取消、硬超时以及 `doctor --json` 健康检查。

## Codex Reviewer

第七阶段通过 Codex CLI 的非交互模式接入独立代码审核员。Codex CLI 必须已安装并完成登录。

```yaml
codex:
  enabled: true
  transport: "cli"
  executable: "codex"
  timeout_seconds: 1800
  health_timeout_seconds: 15
```

每个 `review_request` 都使用全新的临时 Reviewer Session，不恢复之前的审核会话。进程 cwd 与 `--cd` 都绑定当前 Session Worktree，并强制使用 `read-only` sandbox。Reviewer 会直接读取实际 Git 状态和 Diff，但不得修改、格式化或提交文件。最终结果由 JSON Schema 约束为 `PASS` 或 `CHANGES_REQUIRED`，并包含结构化问题列表；进程支持取消、硬超时和健康检查。

## 完整链路与一跳约束

第八阶段通过内存 MCP Client、真实 Session Worktree、Router、Request Manager 和 SQLite 验证完整返工链路：ChatGPT 显式调用 DeepSeek，显式调用 Codex，收到 `CHANGES_REQUIRED` 后再次显式调用 DeepSeek，最后显式调用 Codex 得到 `PASS`。每次 `bridge_send` 只增加一个 Request 和一个 Agent turn；`bridge_status` 只读取状态，审核结果不会触发隐藏的返工、复审或阶段推进。
