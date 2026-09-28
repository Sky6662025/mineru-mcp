# 部署与对接指南

本文档回答两个问题：

1. **如何部署 MinerU MCP Server**（三种形态，按场景选择）
2. **如何让各 Agent 智能体工具对接它**（Claude Desktop / Cursor / Cline / WorkBuddy / 其他）

> MinerU v4 服务端本身的部署（Docker、GPU、参数调优）不在本文主体范围，见[附录 A](#附录-amineru-v4-服务端部署要点)。本文假设你已经有一个可达的 MinerU API（例如 `http://127.0.0.1:8000`）。

---

## 目录

- [一、部署形态总览](#一部署形态总览)
- [二、前置准备](#二前置准备)
- [三、形态 1：stdio 直连（默认，推荐）](#三形态-1stdio-直连默认推荐)
- [四、形态 2：HTTP 常驻服务（多 Agent 共享）](#四形态-2http-常驻服务多-agent-共享)
- [五、对接各 Agent 智能体工具](#五对接各-agent-智能体工具)
  - [5.1 Claude Desktop](#51-claude-desktop)
  - [5.2 Cursor](#52-cursor)
  - [5.3 Cline / Roo Code（VS Code）](#53-cline--roo-codevs-code)
  - [5.4 WorkBuddy](#54-workbuddy)
  - [5.5 其他 MCP 客户端（通用规则）](#55-其他-mcp-客户端通用规则)
- [六、对接后验证清单](#六对接后验证清单)
- [七、对接故障排查](#七对接故障排查)
- [附录 A：MinerU v4 服务端部署要点](#附录-amineru-v4-服务端部署要点)
- [附录 B：实测环境快照](#附录-b实测环境快照)

---

## 一、部署形态总览

MCP Server 有三种部署形态，**先想清楚一个问题：MCP Server 跑在哪台机器上？**

```
形态 1  stdio 直连（默认，推荐）
┌──────────────┐  拉起子进程(stdio)  ┌────────────┐  HTTP  ┌─────────────┐
│ Agent 工具    │ ──────────────────► │ server.py  │ ─────► │ MinerU API  │
│ (本机)       │ ◄────────────────── │ (本机)     │ ◄───── │ (本地/远端) │
└──────────────┘                     └────────────┘        └─────────────┘
无需常驻进程，Agent 按需拉起、会话结束自动回收。绝大多数场景用这个。

形态 2  HTTP 常驻服务（多 Agent 共享 / MCP 与 Agent 异机）
┌──────────────┐                      ┌────────────────────┐  HTTP  ┌─────────────┐
│ Agent A (机器1)│ ──── URL ────────► │ server.py 常驻进程   │ ─────► │ MinerU API  │
│ Agent B (机器2)│ ──── URL ────────► │ --transport          │ ◄───── │             │
└──────────────┘   streamable-http   │ streamable-http:8765 │        └─────────────┘
                                      └────────────────────┘
团队共享一个实例、或 Agent 所在机器装不了 Python 时用这个。

形态 3  CLI 子进程驱动（非 Python Agent / 脚本集成）
┌──────────────┐  子进程调用    ┌────────────────────────────┐
│ 任意程序      │ ────────────► │ python client.py call 工具名 │ ──► (内部走形态 1)
└──────────────┘  JSON 进/出    └────────────────────────────┘
其他语言的 Agent 框架不便实现 MCP 协议时，用命令行一发一收。
```

| | 形态 1 stdio | 形态 2 HTTP 常驻 | 形态 3 CLI |
|---|---|---|---|
| 需要常驻进程 | 否 | 是 | 否 |
| 多 Agent 共享 | 否（各自拉起） | **是** | 否 |
| 跨机器 | 否 | **是** | 否 |
| 配置复杂度 | 最低 | 中（需守护/防火墙） | 低 |
| 适用 | 个人本机使用 | 团队/服务器部署 | 异构语言集成 |

三种形态共用同一份 `server.py`，只是启动参数不同；对 Agent 而言工具清单与行为完全一致。

---

## 二、前置准备

### 2.1 Python 环境

要求 **Python 3.10+**（实测 3.13）。强烈建议独立虚拟环境，避免与系统包冲突：

```bash
cd mineru-mcp

# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

安装成功的判据：

```bash
python -c "from mcp.server.fastmcp import FastMCP; print('OK')"
# 输出 OK
```

> ⚠️ **两个高频安装坑**：
> 1. **`mcp` 必须是 1.x**。`requirements.txt` 已钉死 `mcp>=1.2.0,<2.0.0`——2.x 重构了 API（`FastMCP`→`MCPServer`），与主流 Agent 客户端不兼容。若你手动装过 `mcp 2.x`，先 `pip uninstall mcp` 再装。
> 2. **国内镜像可能没有 `mcp` 包**（如北师镜像）。报 `No matching distribution found` 时改用官方源：
>    ```bash
>    pip install -i https://pypi.org/simple -r requirements.txt
>    ```

### 2.2 确认 MinerU API 可达

```bash
curl http://127.0.0.1:8000/v1/health
# 期望：{"status":"ok","version":"4.0.x","features":{...}}
```

不通则先解决 MinerU 服务端（附录 A）。记下 `features.output_formats` 与 `features.sources`——它们决定了哪些解析能力可用。

### 2.3 记下两个绝对路径

对接配置里**必须用绝对路径**（Agent 拉起子进程时不继承你的 shell 环境）：

```bash
# 解释器路径（venv 激活状态下）
python -c "import sys; print(sys.executable)"
# 例如 Windows: D:\Agent-Work\mineru-mcp\.venv\Scripts\python.exe
# 例如 Linux:   /opt/mineru-mcp/.venv/bin/python

# server.py 路径
# 例如 Windows: D:\Agent-Work\mineru-mcp\server.py
# 例如 Linux:   /opt/mineru-mcp/server.py
```

下文用 `<PYTHON>` 与 `<ABS_PATH>` 指代这两个值。

### 2.4 部署前自检（不依赖任何 Agent）

```bash
python client.py --api-base http://127.0.0.1:8000 test --pdf dev/tiny.pdf
# 期望结尾：========== 自检结果: 17 passed, 0 failed ==========
```

这一步通过，说明「MCP Server ↔ MinerU API」链路完好；之后对接 Agent 出的问题就只剩配置层面，排查范围大大缩小。

---

## 三、形态 1：stdio 直连（默认，推荐）

**没有独立的"部署"动作** —— 你只需把 `command + args + env` 三件套写进 Agent 配置（见[第五节](#五对接各-agent-智能体工具)），Agent 会在需要时自动拉起 `server.py` 子进程。

手动验证 Server 能正常启动（可选）：

```bash
python <ABS_PATH>/server.py --api-base http://127.0.0.1:8000
# 正常运行时【没有任何输出】且进程不退出 —— 它在等 stdin 的 JSON-RPC 消息。
# 这是正常现象，按 Ctrl+C 退出即可。
```

> 如果启动瞬间报错退出（如 ModuleNotFoundError、地址错误），错误信息会打印在 stderr，据此修复。

`server.py` 全部命令行参数（实测 `--help` 输出）：

| 参数 | 说明 |
|---|---|
| `--api-base URL` | MinerU API 地址（覆盖环境变量 `MINERU_API_BASE`） |
| `--api-key KEY` | Bearer Token（覆盖 `MINERU_API_KEY`） |
| `--output-dir DIR` | 产物默认保存目录（覆盖 `MINERU_OUTPUT_DIR`） |
| `--timeout SEC` | HTTP 超时秒数（覆盖 `MINERU_TIMEOUT`） |
| `--insecure` | 跳过 TLS 校验（自签名证书的远端） |
| `--use-proxy {0,1,true,false}` | 强制代理开关；**缺省时本地/私网地址自动绕过系统代理** |
| `--no-proxy` | 等效 `--use-proxy 0` |
| `--transport {stdio,sse,streamable-http}` | 传输形态，默认 stdio |
| `--host` / `--port` | 仅 HTTP/SSE 形态使用，默认 `127.0.0.1:8765` |

配置既可以全走 CLI 参数，也可以全走环境变量，**CLI 优先**。推荐环境变量（配置更干净，密钥不进命令行历史）。

---

## 四、形态 2：HTTP 常驻服务（多 Agent 共享）

### 4.1 启动

在一台**能访问 MinerU API** 的机器上：

```bash
python <ABS_PATH>/server.py \
  --transport streamable-http \
  --host 0.0.0.0 --port 8765 \
  --api-base http://127.0.0.1:8000 \
  --output-dir /data/mineru_outputs
```

启动成功后端点为 `http://<该机IP>:8765/mcp`。

> `--host 0.0.0.0` 才对外可达；仅本机使用保持默认 `127.0.0.1` 更安全。
> 只兼容 SSE 的旧客户端改用 `--transport sse`（端点 `/sse`）。

### 4.2 systemd 守护（Linux）

```ini
# /etc/systemd/system/mineru-mcp.service
[Unit]
Description=MinerU MCP Server (streamable-http)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=mineru
WorkingDirectory=/opt/mineru-mcp
Environment=MINERU_API_BASE=http://127.0.0.1:8000
Environment=MINERU_OUTPUT_DIR=/data/mineru_outputs
Environment=MINERU_TIMEOUT=120
ExecStart=/opt/mineru-mcp/.venv/bin/python server.py --transport streamable-http --host 0.0.0.0 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mineru-mcp
systemctl status mineru-mcp          # Active: active (running)
journalctl -u mineru-mcp -f          # 看日志
```

### 4.3 本地验证 HTTP 端点

```bash
python - <<'EOF'
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def m():
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("HTTP 端点正常，工具数:", len(tools.tools))   # 期望 19

asyncio.run(m())
EOF
```

### 4.4 Agent 侧接入

各客户端用 `url` 方式接入（配置片段见第五节）：

- **Cursor**：只写 `{"url": "http://SERVER:8765/mcp"}`（自动检测传输类型）
- **Cline**：必须写 `"type": "streamableHttp"`（**驼峰**！写错会退回 legacy SSE）

---

## 五、对接各 Agent 智能体工具

所有可复制的 JSON 片段都在 [`mcp_config_examples.json`](../mcp_config_examples.json)，下面按工具讲**配置文件在哪、怎么改、怎么确认生效**。

通用规则先记住三条：

1. **绝对路径**：`command` 与 `args` 里的路径必须是绝对路径（GUI 启动的 Agent 不继承 shell 的 PATH 与虚拟环境）。
2. **合并而非覆盖**：配置文件里已有其他 `mcpServers` 条目时，把 `mineru` 加进去，不要整文件替换。
3. **改完彻底重启**：关闭窗口 ≠ 退出进程。大多数客户端只在完全重启后重新加载 MCP 配置。

### 5.1 Claude Desktop

**配置文件位置**

| 系统 | 路径 |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

也可以从界面进入：Settings → Developer → Edit Config。

**写入配置**（stdio 形态）：

```json
{
  "mcpServers": {
    "mineru": {
      "command": "<PYTHON>",
      "args": ["<ABS_PATH>\\server.py"],
      "env": {
        "MINERU_API_BASE": "http://127.0.0.1:8000",
        "MINERU_OUTPUT_DIR": "<ABS_PATH>\\mineru_outputs"
      }
    }
  }
}
```

> Windows 路径在 JSON 里写 `\\` 双反斜杠，或直接用 `/` 正斜杠。

**生效与确认**

1. **完全退出** Claude Desktop（托盘图标也要退出），重新启动
2. 对话框出现 🔨 锤子图标（或 `+` → Connectors 里出现 mineru）
3. 点开应能看到 19 个 `mineru_*` 工具

**试一句对话**：

```
用 mineru_server_info 看一下 MinerU 服务的状态和支持的输出格式
```

> ⚠️ JSON 语法错误（少逗号、多逗号）会导致 Claude Desktop **静默启动失败或工具不出现**，且没有报错提示。保存前先校验：`python -m json.tool claude_desktop_config.json`。

### 5.2 Cursor

**配置文件位置**

| 作用域 | 路径 | 说明 |
|---|---|---|
| 全局 | `~/.cursor/mcp.json`（Windows: `%USERPROFILE%\.cursor\mcp.json`） | 所有项目可用 |
| 项目 | `<项目根>/.cursor/mcp.json` | 仅该项目；可提交 git 团队共享 |

**stdio 形态**（MCP 跑本机）：

```json
{
  "mcpServers": {
    "mineru": {
      "command": "<PYTHON>",
      "args": ["<ABS_PATH>/server.py"],
      "env": {
        "MINERU_API_BASE": "http://127.0.0.1:8000",
        "MINERU_OUTPUT_DIR": "<ABS_PATH>/mineru_outputs"
      }
    }
  }
}
```

**HTTP 形态**（MCP 常驻在服务器）——Cursor 只需 `url`，**自动检测** streamable HTTP / SSE，不要写 `type`：

```json
{
  "mcpServers": {
    "mineru": { "url": "http://SERVER:8765/mcp" }
  }
}
```

**生效与确认**

1. Settings（`Ctrl+Shift+J`）→ Tools & MCP（旧版叫 MCP & Integrations）
2. mineru 条目应显示**绿色**状态与工具计数 19
3. 不生效就在该面板把 mineru 开关 off→on 一次（Cursor 缓存工具列表）
4. 失败时看日志：Output 面板（`Ctrl+Shift+U`）→ 下拉选 **MCP Logs**

Cursor 额外支持插值，密钥可不落盘：`"MINERU_API_KEY": "${env:MINERU_KEY}"`。

### 5.3 Cline / Roo Code（VS Code）

**配置文件位置**（Cline VS Code 扩展）

| 系统 | 路径 |
|---|---|
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Linux | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Cline CLI | `~/.cline/mcp.json` |

**推荐从界面进入**（目录不存在时让扩展自建，别手动创建）：Cline 面板顶部 **MCP Servers 图标** → Configure 标签 → **Configure MCP Servers** 按钮。文件保存后 Cline 实时热加载，无需重启 VS Code。

**stdio 形态**：

```json
{
  "mcpServers": {
    "mineru": {
      "command": "<PYTHON>",
      "args": ["<ABS_PATH>/server.py"],
      "env": {
        "MINERU_API_BASE": "http://127.0.0.1:8000",
        "MINERU_OUTPUT_DIR": "<ABS_PATH>/mineru_outputs"
      },
      "disabled": false,
      "autoApprove": ["mineru_server_info", "mineru_health", "mineru_parse_document"]
    }
  }
}
```

- `autoApprove`：列出的工具免每次弹窗确认。建议先只放只读类工具，用顺手了再加 `mineru_upload_file` 等
- **HTTP 形态**：`type` 必须是驼峰 **`streamableHttp`**（省略会退回 legacy `sse`）：

```json
{
  "mcpServers": {
    "mineru": {
      "type": "streamableHttp",
      "url": "http://SERVER:8765/mcp",
      "disabled": false
    }
  }
}
```

**生效与确认**：MCP Servers 面板里 mineru 显示绿色、展开可见 19 个工具。Roo Code（Cline 分支）结构类似，但项目级配置在 `.roo/mcp.json`，且多 `disabledTools`/`watchPaths` 字段。

### 5.4 WorkBuddy

**配置文件**：`~/.workbuddy/mcp.json`（⚠️ 是 `mcp.json`，**不是** `.mcp.json`）。

与已有条目**合并**写入（本机实测格式，与你已装的 `kali-pentest` 等 server 并列）：

```json
{
  "mcpServers": {
    "mineru": {
      "command": "<PYTHON>",
      "args": ["<ABS_PATH>\\server.py"],
      "env": {
        "MINERU_API_BASE": "http://127.0.0.1:8000",
        "MINERU_OUTPUT_DIR": "<ABS_PATH>\\mineru_outputs"
      },
      "cwd": "<ABS_PATH>"
    }
  }
}
```

**关键一步（容易漏）**：写入后 MCP **不会自动启用** —— 打开 WorkBuddy 的「连接器管理页」，右上角「自定义连接器」找到 mineru，点击**信任**。之后新会话即可看到 19 个 `mineru_*` 工具。

### 5.5 其他 MCP 客户端（通用规则）

Cherry Studio、Continue、Zed、OpenAI Agents SDK……凡支持 **stdio 型 MCP** 的客户端，配置结构都是同一个三件套：

```json
{ "command": "<PYTHON>", "args": ["<ABS_PATH>/server.py"], "env": { "MINERU_API_BASE": "..." } }
```

差异只在：配置文件叫什么、放在哪、远程传输的 `type` 字段怎么拼。遇到没列出的客户端，查它的文档确认这三点即可。

**非 MCP 客户端 / 其他语言**：用 CLI 形态（`client.py call`），JSON 进 JSON 出：

```bash
python client.py --api-base http://127.0.0.1:8000 call mineru_health
python client.py --api-base http://127.0.0.1:8000 call mineru_parse_document \
  '{"source":{"type":"url","url":"https://example.com/x.pdf"},"tier":"flash"}'
```

---

## 六、对接后验证清单

按顺序做，每步都有明确的通过判据：

| # | 验证项 | 操作 | 通过判据 |
|---|---|---|---|
| 1 | MinerU 存活 | `curl http://127.0.0.1:8000/v1/health` | 返回 `"status":"ok"` |
| 2 | Server 可启动 | `python server.py`（stdio） | 无报错、不退出、无 stdout 输出 |
| 3 | 链路自检 | `python client.py test --pdf dev/tiny.pdf` | `17 passed, 0 failed` |
| 4 | 覆盖自检 | `python coverage_test.py --api-base <地址>` | `9 passed, 0 failed` |
| 5 | Agent 识别工具 | 看客户端 MCP 面板 | mineru 绿色/已连接，**19 个工具** |
| 6 | Agent 实际调用 | 对话：「用 mineru_health 检查服务状态」 | 返回 version 与 features |
| 7 | 端到端解析 | 对话：「用 mineru_parse_document 解析 <某PDF的URL>，tier 用 flash，只要 markdown」 | 返回解析内容或落盘路径 |

第 5 步失败 → 配置问题，看[第七节](#七对接故障排查)。第 6/7 步失败 → 看工具返回的 `error.message`（已全部翻译成结构化错误，直接可读）。

---

## 七、对接故障排查

### 7.1 Agent 里看不到 mineru / 工具数为 0

按命中率排序：

1. **JSON 语法错误**。用 `python -m json.tool <配置文件>` 校验。Claude Desktop 对语法错误**静默失败**。
2. **没有彻底重启客户端**。关窗口不够，要完全退出进程（Windows 检查托盘；macOS `Cmd+Q`）。
3. **`command` 不是绝对路径**，或指向的解释器没装 `mcp` 包。验证：
   ```bash
   <你配置里的PYTHON> -c "from mcp.server.fastmcp import FastMCP; print('OK')"
   ```
4. **WorkBuddy 忘了点「信任」**（见 5.4）。
5. **Cursor 缓存**：Settings → Tools & MCP 里把 mineru off→on。
6. **`mcp` 装成了 2.x**：`pip show mcp` 看版本，2.x 会导致 server.py 导入失败。重装 `pip install "mcp<2"`。

### 7.2 工具能看到，但一调用就失败

看返回的 `error` 字段（本项目所有错误已结构化）：

| error.type / status_code | 原因 | 处理 |
|---|---|---|
| `connection_error` | MCP 连不上 MinerU API | `curl <MINERU_API_BASE>/v1/health`；检查地址端口、Docker 是否在跑 |
| `502 upstream connect failed` | **系统代理拦截了本地请求** | 设 `MINERU_USE_PROXY=0` 或启动参数加 `--no-proxy`（详见 7.3） |
| `timeout_error` | 请求超时 | 大文档调大 `MINERU_TIMEOUT`；解析超时调大工具入参 `timeout` |
| `401/403` | 服务端开了 `--api-key` 而 MCP 没带 | 配置 `MINERU_API_KEY` |
| `400 unsupported_source` | 用了 local 源但服务端没开 | 改走 `mineru_upload_file` + `file_id` 源 |
| `poll_timeout` | 等待解析超时（任务**没丢**） | 用 `mineru_get_parse_job` 续查；并发为 1 时勿并行提交 |
| `validation_error` (422) | 工具入参不合法 | `error.message` 里有 FastAPI 的详细字段报错，按提示改 |

### 7.3 502 之谜：系统代理拦截 localhost

**症状**：`curl http://127.0.0.1:8000/v1/health` 正常，但 MCP 工具报 `502 upstream connect failed`。

**原因**：环境里有 `http_proxy`/`HTTP_PROXY` 变量时，httpx 默认把它应用到**所有**请求，包括发往 `127.0.0.1` 的；代理无法回连你的本机，返回 502。报错来自代理，极具迷惑性。

**本项目已内置自动修复**：回环/私网地址（127.x、10.x、172.16-31.x、192.168.x、localhost、::1）自动绕过代理。仍遇到说明地址被判定为公网（如自定义域名指向内网），显式关闭：

```json
"env": { "MINERU_API_BASE": "http://mineru.internal:8000", "MINERU_USE_PROXY": "0" }
```

### 7.4 会话莫名断开 / 协议错误

十有八九是 **stdout 被污染**：stdio 传输用 stdout 跑 JSON-RPC，任何写进 stdout 的内容都会破坏协议。本项目日志已全部走 stderr；若你二次开发了 `server.py`，**任何 print 必须 `print(..., file=sys.stderr)`**。

### 7.5 产物找不到

工具返回的 `saved_path` 是 MCP Server 所在机器的路径。stdio 形态 = Agent 本机；HTTP 形态 = **服务器上**，需要配置共享目录或改用 `return_content: true` 直接取回文本。默认目录可用 `MINERU_OUTPUT_DIR` 固定。

---

## 附录 A：MinerU v4 服务端部署要点

本项目对接的服务端最简部署（完整版含 GPU 驱动、参数详解见镜像官方文档）：

```bash
# 本地 WSL2 Docker（Windows）
docker run -d \
  --name mineru-api \
  --gpus '"device=0"' \
  --restart always \
  -p 8000:8000 \
  --shm-size=2g \
  -e MINERU_MODEL_SOURCE=local \
  -v mineru-data:/root/.mineru \
  mineru:v4.0.5 \
  api-server --host 0.0.0.0 --port 8000
```

**与 MCP 工具能力直接相关的服务端参数**（`mineru-kit api-server --help` 实测）：

| 服务端参数 | 默认 | 对 MCP 的影响 |
|---|---|---|
| `--api-key` | 无 | 设置后 MCP 必须配 `MINERU_API_KEY` |
| `--concurrency` | **1** | 为 1 时 MCP 批量任务只能串行，否则 `poll_timeout` |
| `--allow-local-source` | **关** | 不开则 MCP 的 `local` 源报 400；推荐改走 upload+file_id |
| `--max-inline-bytes` | 1MB | MCP 的 `inline` base64 源大小上限 |
| `--preload-models` | 关 | 不开则首个解析请求含模型冷启动（可能数十秒），MCP 侧 `timeout` 要给足 |
| `--vlm-server-url` | 空(本地) | VLM 推理分离部署时指向推理节点 |

**运维子命令**（模型排障）：

```bash
docker exec mineru-api mineru-kit models show     # 配置来源与生效后端
docker exec mineru-api mineru-kit models verify   # 权重完整性
docker exec mineru-api mineru-kit parse <file> ...  # 绕过 API 直接命令行解析，隔离定位
```

---

## 附录 B：实测环境快照

本文档所有配置与判据均在以下真实环境验证（2026-09）：

```
MCP 侧      Windows 11 + Python 3.13（venv）+ mcp 1.30.0 + httpx 0.28.1
服务端      WSL2 Ubuntu-22.04 + Docker 26.1.4
容器        mineru-api（镜像 mineru:v4.0.5，restart=always，GPU device 0）
GPU         NVIDIA RTX 4060 Laptop 8GB，驱动 591.44，CUDA 13.0.2（镜像内置）
推理栈      vLLM v0.21.0（MINERU_MODEL_VLM_ENGINE=vllm，MINERU_MODEL_SOURCE=local）
API 实测    v4.0.5，无鉴权(anonymous)，并发 1，
            输出格式 markdown/middle_json/structured_content/zip，
            输入源 file_id/url/inline（local 未启用），webhook false
测试结论    19 工具 100% 覆盖，26/26 断言通过；
            stdio / streamable-http(:8765/mcp) / CLI 三形态实测连通；
            真实文档：632KB 中文 PDF standard 档解析正确
```
