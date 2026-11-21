# goose-codex-pool — GPT (Codex) account pool for Goose

A multi-account pool for a self-hosted [Goose](https://github.com/aaif-goose/goose)
agent that runs on a ChatGPT/Codex subscription (`chatgpt_codex` provider).

One account is *active*; the rest are *standby* or *parked*. A patrol runs
every 5 minutes: if the active account hits its quota cap or its token is
invalidated, the pool parks it until its reset time and fails over to a
healthy account automatically. Switching is verified with a real probe
request and rolls back on failure.

中文版说明在下方 ↓

## Layout

| File | Role |
|---|---|
| `pool.py` | The engine: CLI + patrol. Subcommands: `status`, `usage`, `status-json`, `switch`, `park`, `unpark`, `refresh`, `add-begin`, `add-finish`, `remove`, `patrol`, `log`. |
| `pool_mcp.py` | The face: a stdio MCP server (11 tools) wrapping the CLI — status/usage/switch/park/unpark/log, device-code add (`pool_add_begin` / `pool_add_finish`), `pool_remove`, plus `pool_data` (structured JSON) and `pool_panel`. |
| `pool_panel.html` | An MCP App (`ui://pool/panel`) rendered inside the chat on desktop and web clients: account list, quota bars, and working switch / park / add / remove buttons (the app calls the pool tools through the host bridge). |
| `probe-*.py` | ACP-level verification probes used during development. |
| `check-codex-acct.py`, `check-codex-proc.ps1` | Small diagnostics for the Windows Codex login state. |

## Token-safety design

Learned the hard way (a whole token family was revoked by the provider):

- **Single refresher.** The active account's tokens are refreshed only by
  Goose itself. The patrol *harvests* the live tokens back into the pool
  store after use — it never refreshes the active account itself.
- **Shared-grant isolation.** An account whose authorization family is
  shared with another client (e.g. a desktop Codex login) is marked
  `grant_shared` and is never refreshed from the pool side.
- **Lazy refresh.** Parked accounts are refreshed only when close to
  expiry or right before a switch — no forced daily refresh.
- Adding an account uses the OAuth device-code flow, so every pooled
  account gets its own isolated authorization family.

## State (runtime, not in this repo)

On the host, state lives next to the deployment, never in git:

```
~/goose/pool/
  pool.json            # account registry (ids, states, park metadata)
  accounts/<id>/       # per-account auth.json + tokens.json (mode 600)
  pool.log, patrol.log
```

Credentials are never printed by any command (account ids are shown
truncated) and never committed.

## Wiring into Goose

Register the MCP server in `~/.config/goose/config.yaml`:

```yaml
extensions:
  pool:
    enabled: true
    type: stdio
    name: pool
    description: GPT account pool
    cmd: python3
    args: [/home/gmy/goose/pool/pool_mcp.py]
    timeout: 600
```

Patrol via cron:

```
*/5 * * * * python3 /home/gmy/goose/pool/pool.py patrol >> /home/gmy/goose/pool/patrol.log 2>&1
```

For the panel to render, the ACP client must declare MCP Apps support in
`initialize` (`clientCapabilities._meta.goose.mcpHostCapabilities` with the
`io.modelcontextprotocol/ui` extension) — the Goose desktop app does this
natively.

---

# goose-codex-pool — Goose 的 GPT（Codex）账号池

给自托管 [Goose](https://github.com/aaif-goose/goose) 智能体用的多账号池，
智能体走 ChatGPT/Codex 订阅（`chatgpt_codex` 通道）。

池中一个账号为「在用」，其余为「备用」或「停泊」。巡查每 5 分钟运行
一次：在用账号额度用满或令牌失效时，池子把它停泊到其额度重置时间，
并自动切换到健康账号。切换要用一次真实请求探针验证，失败自动回滚。

## 文件构成

| 文件 | 作用 |
|---|---|
| `pool.py` | 发动机：CLI + 巡查。子命令：`status`、`usage`、`status-json`、`switch`、`park`、`unpark`、`refresh`、`add-begin`、`add-finish`、`remove`、`patrol`、`log`。 |
| `pool_mcp.py` | 脸面：stdio MCP 服务器（11 个工具），封装 CLI —— 状态/额度/切换/停泊/放回/日志、设备码加号（`pool_add_begin` / `pool_add_finish`）、`pool_remove`，以及 `pool_data`（结构化 JSON）和 `pool_panel`。 |
| `pool_panel.html` | MCP 应用（`ui://pool/panel`），在桌面端与网页端的对话里渲染：账号列表、额度条，以及可点的切换 / 停泊 / 加号 / 删号按钮（应用经宿主桥接回调号池工具）。 |
| `probe-*.py` | 开发期间用的 ACP 层验证探针。 |
| `check-codex-acct.py`、`check-codex-proc.ps1` | Windows 上 Codex 登录状态的小诊断工具。 |

## 令牌安全设计

教训来自一次真实事故（一个账号的整组令牌被服务端作废）：

- **单一刷新者。** 在用账号的令牌只由 Goose 自己刷新。巡查只把活令牌
  「收割」回池子存储，永不自己刷新在用账号。
- **共享授权隔离。** 与其他客户端（如桌面 Codex 登录）共用同一授权
  家族的账号会被标记 `grant_shared`，池子一侧永不刷新它。
- **懒刷新。** 停泊账号只在临近过期或切换前才刷新，没有每日强刷。
- 加号走 OAuth 设备码流程，每个入池账号都有自己独立的授权家族。

## 运行状态（不在本仓库中）

状态文件放在部署目录旁，永不进 git：

```
~/goose/pool/
  pool.json            # 账号注册表（编号、状态、停泊信息）
  accounts/<id>/       # 每个账号的 auth.json + tokens.json（权限 600）
  pool.log, patrol.log
```

任何命令都不会打印凭据（账号编号只显示截断形式），凭据也从不提交。

## 接入 Goose

在 `~/.config/goose/config.yaml` 注册 MCP 服务器：

```yaml
extensions:
  pool:
    enabled: true
    type: stdio
    name: pool
    description: GPT account pool
    cmd: python3
    args: [/home/gmy/goose/pool/pool_mcp.py]
    timeout: 600
```

巡查用 cron：

```
*/5 * * * * python3 /home/gmy/goose/pool/pool.py patrol >> /home/gmy/goose/pool/patrol.log 2>&1
```

面板要能渲染，ACP 客户端须在 `initialize` 时声明支持 MCP 应用
（`clientCapabilities._meta.goose.mcpHostCapabilities` 里带
`io.modelcontextprotocol/ui` 扩展）—— Goose 桌面版原生带这个声明。
