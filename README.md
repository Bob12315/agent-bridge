# Agent Bridge

Agent Bridge 是 ChatGPT Web、DeepSeek Executor 与 Codex Reviewer 之间的跨平台通信中间层。Bridge 只验证、保存和转发消息；所有阶段、轮次、返工与推进决策均由 ChatGPT 明确发起。

当前完成前三个阶段：Foundation、Session + Git Workspace，以及 Async Request Manager。系统现在支持请求快速路径、后台执行、状态查询、等待、失败处理、取消和跨平台进程树管理。Web UI、MCP 及真实 Agent Adapter 将按后续阶段实现。

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
