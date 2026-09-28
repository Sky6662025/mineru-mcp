# 一键部署提示词（给任意 Agent 使用）

## 一句话版（最省事，能力强的 Agent 用这个）

> 把 `D:\Agent-Work\mineru-mcp` 部署为 MCP Server 并接入当前 Agent：在该目录建 `.venv` 并执行
> `pip install -i https://pypi.org/simple -r requirements.txt`（必须 `mcp<2`），先用该 venv 的 python 跑
> `client.py --api-base http://127.0.0.1:8000 test --pdf dev/tiny.pdf` 确认 **17 passed, 0 failed**，
> 再把 `mineru` 条目**合并**进本 Agent 的 MCP 配置文件（`command`=venv 里 python 的绝对路径、
> `args`=[`server.py` 绝对路径]、`cwd`=项目目录、`env` 里 `MINERU_API_BASE=http://127.0.0.1:8000`），
> 校验 JSON 语法后彻底重启 Agent，任一步失败就停下报告原因、不要修改项目代码。

替换其中的项目路径与 API 地址即可。需要更稳妥的分步约束（含故障处理规则与各 Agent 配置文件路径对照表），用下面的完整版。

---

## 完整版

把下面【提示词正文】整段复制给你的 Agent（Claude Desktop / Cursor / Cline / WorkBuddy / Codex CLI / 任意有 shell 与文件读写能力的 Agent），替换掉开头的 4 个占位符即可。

提示词内置了本项目开发过程中实测踩到的全部坑作为硬约束，Agent 照做即可，无需自己摸索。

---

## 占位符说明（只需改这 4 处）

| 占位符 | 含义 | 示例 |
|---|---|---|
| `{{CODE_SOURCE}}` | 代码来源：本地目录 / zip 路径 / git 仓库 URL | `D:\Agent-Work\mineru-mcp` |
| `{{PROJECT_PATH}}` | 部署后项目所在绝对路径 | `D:\Agent-Work\mineru-mcp` |
| `{{MINERU_API_BASE}}` | MinerU API 地址 | `http://127.0.0.1:8000` |
| `{{AGENT_TYPE}}` | 你正在用的 Agent | `WorkBuddy` / `Cursor` / `Cline` / `Claude Desktop` |

> 若 `{{CODE_SOURCE}}` 是本地目录且与 `{{PROJECT_PATH}}` 相同，两者填一样的值。

---

## 提示词正文（从这里开始复制）

````text
# 任务：部署 MinerU v4 MCP Server 并对接到 {{AGENT_TYPE}}

## 目标
把 MinerU v4 文档解析能力（PDF/图片 → Markdown/JSON/ZIP）以 MCP 工具形式接入 {{AGENT_TYPE}}，
对接的 MinerU API 地址为 {{MINERU_API_BASE}}。最终我需要能在对话里直接让 Agent 解析文档。

## 输入
- 代码来源：{{CODE_SOURCE}}
- 目标目录：{{PROJECT_PATH}}
- 该项目已实现完毕，含 19 个 MCP 工具，你【不需要写任何业务代码】，只做部署与对接。
- 项目内已有文档可参考：README.md、docs/DEPLOYMENT.md、mcp_config_examples.json

## 执行步骤（严格按序，每步验证通过才进入下一步）

### 步骤 0：获取代码
若 {{CODE_SOURCE}} 是本地目录且已存在于 {{PROJECT_PATH}}，跳过本步。
若是 zip，解压到 {{PROJECT_PATH}}（确认解压后 server.py 直接位于该目录，而非多套一层子目录）。
若是 git URL，clone 到 {{PROJECT_PATH}}。
完成后列出目录，确认这 4 个文件存在：server.py、mineru_client.py、client.py、requirements.txt
缺任何一个就停下来报告，不要继续。

### 步骤 1：创建独立虚拟环境并装依赖
在 {{PROJECT_PATH}} 下创建 .venv（不要装进系统 Python）：
  Windows:  python -m venv .venv  然后用 .venv\Scripts\python.exe
  Linux/Mac: python3 -m venv .venv 然后用 .venv/bin/python
要求 Python 3.10+。装依赖：
  <venv-python> -m pip install -r requirements.txt
【关键】若报 "No matching distribution found for mcp"，说明镜像源缺该包，改用官方源重试：
  <venv-python> -m pip install -i https://pypi.org/simple -r requirements.txt
若报 ResolutionImpossible，先原样重试一次（PyPI 偶发解析抖动，重试通常即成功），仍失败再报告。

### 步骤 2：验证 mcp SDK 版本正确（这一步不能省）
执行：
  <venv-python> -c "from mcp.server.fastmcp import FastMCP; print('OK')"
- 输出 OK → 通过
- 报 "No module named 'mcp.server.fastmcp'" → 说明装成了 mcp 2.x（2.x 把 FastMCP 改名为
  MCPServer，与主流 Agent 客户端不兼容）。必须先卸载再装 1.x：
      <venv-python> -m pip uninstall -y mcp
      <venv-python> -m pip install -i https://pypi.org/simple "mcp<2"
  然后重新验证，直到输出 OK。

### 步骤 3：探测 MinerU API 是否可达
  curl {{MINERU_API_BASE}}/v1/health
期望返回含 "status":"ok" 和 version 字段。
若不通：这是 MinerU 服务端问题，不是 MCP 问题。停止部署并告诉我，附上报错。
若通了，记下返回里的 features.output_formats 和 features.sources —— 它们决定哪些解析能力可用，
最后在总结里告诉我。

### 步骤 4：跑项目自带端到端自检（最重要的验证）
  cd {{PROJECT_PATH}}
  <venv-python> client.py --api-base {{MINERU_API_BASE}} test --pdf dev/tiny.pdf
必须看到结尾：========== 自检结果: 17 passed, 0 failed ==========
若有 failed，把失败项原文贴给我，不要跳过、不要继续对接。
这一步通过就意味着「MCP Server ↔ MinerU API」链路完好，后续问题只可能在 Agent 配置层面。

（可选）补充覆盖测试，验证上传/取消/边界共 9 项：
  <venv-python> coverage_test.py --api-base {{MINERU_API_BASE}}
期望结尾：========== 覆盖测试结果: 9 passed, 0 failed ==========
两者合计覆盖全部 19 个工具、26 项断言。

### 步骤 5：定位 {{AGENT_TYPE}} 的 MCP 配置文件
【硬约束】不要凭记忆猜路径。按下面表格取，若表格没有 {{AGENT_TYPE}}，先去查它的官方文档确认，
或直接在文件系统里搜索已存在的配置文件（如搜 mcp.json / *mcp_settings.json）。
找不到就问我，不要自己新建一个可能不被读取的文件。

| Agent | 配置文件路径 |
|---|---|
| Claude Desktop | Windows: %APPDATA%\Claude\claude_desktop_config.json<br>macOS: ~/Library/Application Support/Claude/claude_desktop_config.json |
| Cursor | 全局 ~/.cursor/mcp.json（Windows: %USERPROFILE%\.cursor\mcp.json）<br>或项目级 <项目根>/.cursor/mcp.json |
| Cline (VS Code) | Windows: %APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json<br>macOS: ~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev\settings\cline_mcp_settings.json<br>Linux: ~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json<br>Cline CLI: ~/.cline/mcp.json |
| Roo Code | 同 Cline 结构，另有项目级 .roo/mcp.json |
| WorkBuddy | ~/.workbuddy/mcp.json （注意不是 .mcp.json） |
| VS Code (原生) | 用户级 settings.json 里的 mcp.servers，或项目级 .vscode/mcp.json |
| Codex CLI | ~/.codex/config.toml （TOML 格式，不是 JSON） |

### 步骤 6：备份配置文件
若文件已存在，先复制一份带时间戳的备份（如 mcp.json.bak-YYYYmmdd-HHMMSS），并告诉我备份路径。
文件不存在则跳过。

### 步骤 7：合并写入 mineru 配置
【硬约束】必须【合并】进已有的 mcpServers 对象，绝对不能整文件覆盖 —— 里面可能有我其他的连接器。
写入前先把原文件读出来给我看一眼现有条目，确认你是在追加。

要写入的条目（stdio 形态，最通用）：
{
  "mcpServers": {
    "mineru": {
      "command": "<venv-python 的绝对路径>",
      "args": ["<PROJECT_PATH 绝对路径>/server.py"],
      "env": {
        "MINERU_API_BASE": "{{MINERU_API_BASE}}",
        "MINERU_OUTPUT_DIR": "<PROJECT_PATH 绝对路径>/mineru_outputs",
        "MINERU_TIMEOUT": "120",
        "PYTHONIOENCODING": "utf-8"
      },
      "cwd": "<PROJECT_PATH 绝对路径>"
    }
  }
}

【硬约束】
- command / args / cwd 全部用【绝对路径】。GUI 启动的 Agent 不继承 shell 的 PATH 和虚拟环境，
  写 "python" 或相对路径必然失败。
- command 必须是【步骤 1 那个 venv 里的 python】，不是系统 python（系统 python 没装 mcp 包）。
- cwd 必须设为项目目录，否则 server.py 无法 import 同目录的 mineru_client.py。
- Windows 路径在 JSON 里要么写双反斜杠 \\，要么直接用正斜杠 /，单个反斜杠会导致 JSON 解析失败。
- 若 {{AGENT_TYPE}} 的字段名与上面不同（例如 Codex CLI 是 TOML），按其官方格式等价转换，不要硬塞 JSON。

创建产物目录（若不存在）：<PROJECT_PATH>/mineru_outputs

### 步骤 8：校验 JSON 语法
  python -m json.tool <配置文件路径>
必须无报错。【重要】JSON 语法错误会让部分 Agent（尤其 Claude Desktop）静默启动失败且不给任何提示，
所以这一步必须做。有错就修到通过为止。

### 步骤 9：模拟 Agent 真实启动方式验证（关键，别跳过）
不要只改完文件就说好了。用与配置完全相同的 command/args/cwd/env 实际拉起子进程验证一遍。
可以复用项目自带的客户端（它会按 stdio 协议拉起 server.py）：
  cd {{PROJECT_PATH}}
  <venv-python> client.py --api-base {{MINERU_API_BASE}} call mineru_server_info
期望返回 "ok": true 且 data 里含 health / tiers / models / usage 四部分。
再验一个解析能力：
  <venv-python> client.py --api-base {{MINERU_API_BASE}} call mineru_health
期望返回 version 与 features。
两条都成功，说明配置里的解释器路径、模块导入、API 连通全部真实可用。

### 步骤 10：告诉我还需要手动做什么
不同 Agent 生效方式不同，明确告诉我：
- Claude Desktop：必须【完全退出】（含托盘图标）再重启，关窗口不够
- Cursor：Settings → Tools & MCP 里确认 mineru 为绿色、工具数 19；不生效就把开关 off→on
- Cline / Roo Code：从面板保存后一般热加载；若无效则完全重启 VS Code
- WorkBuddy：写入 mcp.json 后【不会自动启用】，必须去「连接器管理页 → 右上角自定义连接器 →
  对 mineru 点击『信任』」，然后在【新会话】里才看得到工具
- 其他：按其文档说明
并提醒我：新会话里应能看到 19 个 mineru_* 工具。

## 故障处理规则（遇到就按这个来，不要自己乱试）

1. 工具在 Agent 里看不到 / 数量为 0
   → 依次查：JSON 语法（步骤 8）、是否彻底重启、command 是否绝对路径、
     该解释器能否 import mcp（重跑步骤 2 的命令）、WorkBuddy 是否忘了点信任。

2. 工具能看到但调用报错，返回 502 "upstream connect failed"
   → 这是【系统代理拦截了 localhost 请求】，不是 MinerU 挂了（curl 直连通常是通的）。
     httpx 默认会把 http_proxy 环境变量应用到所有请求，包括发往 127.0.0.1 的，而代理无法回连本机。
     本项目已内置自动绕过（回环与私网网段直连），若仍出现，说明地址被判定为公网，
     在 env 里显式加：  "MINERU_USE_PROXY": "0"
     然后重启 Agent 重试。

3. 报 ModuleNotFoundError: No module named 'mineru_client'
   → cwd 没设对。确认配置里 cwd 指向 server.py 所在目录。

4. 报 ModuleNotFoundError: No module named 'mcp'
   → command 指向的解释器不是步骤 1 那个 venv。用步骤 2 的命令验证配置里那个解释器。

5. 解析调用返回 poll_timeout
   → 任务没丢，只是还没跑完（MinerU 本地部署并发常为 1，多任务会排队）。
     用 mineru_get_parse_job 传 job_id 继续查；后续调用可把 timeout 参数调大。

6. 报 400 unsupported_source（用了 local 源）
   → MinerU 服务端未开启 --allow-local-source。改用 mineru_upload_file 先上传拿 file_id，
     再用 {"type":"file_id","file_id":"..."} 作为源。这是最稳妥的方式，不依赖服务端配置。

7. 请求了 html/latex/docx 格式但没有产物
   → 服务端未启用该格式。查 mineru_health 返回的 features.output_formats 确认实际支持哪些
     （常见默认为 markdown / middle_json / structured_content / zip）。

## 硬性禁止
- 不要修改 server.py / mineru_client.py / client.py 的任何逻辑（它们已通过 26/26 测试）
- 不要把 mcp 升级到 2.x
- 不要整文件覆盖我的 Agent 配置
- 不要在任何步骤失败后继续往下走并宣称成功
- 不要用 print 往 stdout 输出内容到 server.py（stdio 传输的 stdout 是 JSON-RPC 协议流，
  污染它会导致会话莫名断开）

## 最终请给我的总结
1. 每步执行结果（通过/失败），特别是步骤 4 的 17 passed 与步骤 9 的两条调用输出
2. 写入的完整 mineru 配置片段、配置文件路径、备份路径
3. MinerU 服务端实际能力：version、支持的 output_formats、支持的 sources、max_concurrent_jobs
4. 我还需要手动做什么才能让工具生效
5. 一句可直接复制的试用指令，例如：
   「用 mineru_parse_document 解析 <某个PDF路径或URL>，tier 用 flash，只要 markdown」
````

## 提示词正文（到这里结束）

---

## 精简版（Agent 能力较强、只想快速部署时用）

```text
部署 MinerU MCP Server 到 {{AGENT_TYPE}}，MinerU API 在 {{MINERU_API_BASE}}。
代码在 {{PROJECT_PATH}}（已实现完毕，含 19 个工具，不要改任何业务代码）。

步骤：
1. 在 {{PROJECT_PATH}} 建 .venv，pip install -r requirements.txt
   （若报找不到 mcp 包，加 -i https://pypi.org/simple 重试）
2. 验证：.venv 的 python -c "from mcp.server.fastmcp import FastMCP; print('OK')"
   必须输出 OK。若报没有 fastmcp 模块，说明装成了 mcp 2.x，卸载后重装 "mcp<2"。
3. curl {{MINERU_API_BASE}}/v1/health 确认服务可达。
4. 跑自检：.venv 的 python client.py --api-base {{MINERU_API_BASE}} test --pdf dev/tiny.pdf
   必须 17 passed, 0 failed，否则停下报告。
5. 找到 {{AGENT_TYPE}} 的 MCP 配置文件（不确定路径就查官方文档或搜文件系统，别猜、别新建）。
   先备份，再【合并】追加 mineru 条目（不要覆盖已有条目）：
     command = .venv 里 python 的绝对路径
     args    = ["{{PROJECT_PATH}}/server.py 的绝对路径"]
     cwd     = {{PROJECT_PATH}}
     env     = MINERU_API_BASE={{MINERU_API_BASE}},
               MINERU_OUTPUT_DIR={{PROJECT_PATH}}/mineru_outputs,
               MINERU_TIMEOUT=120, PYTHONIOENCODING=utf-8
   注意：全部绝对路径；Windows 路径在 JSON 里用 \\ 或 /；cwd 必填否则 import 会失败。
6. python -m json.tool 校验配置文件语法（语法错会让 Agent 静默失败）。
7. 实跑验证：.venv 的 python client.py --api-base {{MINERU_API_BASE}} call mineru_server_info
   返回 "ok": true 才算通。
8. 告诉我如何让它生效（多数需彻底重启；WorkBuddy 还要在连接器管理页对 mineru 点「信任」）。

若调用报 502 upstream connect failed：是系统代理拦了 localhost，在 env 加 "MINERU_USE_PROXY":"0"。
任一步失败就停下报告，不要跳过继续。
```

---

## 附：给 Agent 的能力速查（可选附在提示词后）

```text
部署完成后，我可能会用到的核心工具（共 19 个，这里是最常用的 5 个）：

mineru_server_info     先看服务能做什么（版本/格式/档位/限额），排障首选
mineru_parse_document  【主入口】一步式解析：提交→等待→下载→返回内容
mineru_upload_file     上传本地文件拿 file_id（最稳妥的输入方式）
mineru_get_parse_job   查异步任务状态
mineru_download_job_results  批量下载已完成任务的产物

mineru_parse_document 的 source 四种写法：
  {"type":"url","url":"https://..."}                  远程 URL
  {"type":"file_id","file_id":"file-xxx"}             先 upload_file 拿到
  {"type":"inline","name":"x.pdf","data":"<base64>"}  小文件（服务端上限默认 1MB）
  {"type":"local","path":"/data/x.pdf"}               需服务端开 --allow-local-source

常用参数：
  tier           flash（最快）/ basic / standard（默认）/ advanced（复杂文档）
  output_formats ["markdown"] 最常用；服务端还支持 middle_json/structured_content/zip
  page_range     "1-5,8" 或 "all"
  wait           true=同步等结果；false=立即返回 job_id 自行轮询
  return_content true=把文本内容直接返回给我（省去再读文件）
```
