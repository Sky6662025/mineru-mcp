# MinerU v4 MCP —— 功能与特点（叙事版）

## 一、它解决的是什么问题

MinerU 是个很好的文档解析引擎，但它说的话是 REST 的语言：十八个操作、三步式上传、异步任务加轮询、产物只是一串 `file_id` 引用。让一个 Agent 自己去啃这套流程，等于要求它先当一回 API 工程师——记住端点顺序、算 SHA256、处理 202 与终态判断、再逐个下载产物。中间任何一步走错，拿到的就是一坨 HTTP 状态码。

这个 MCP 做的事情，就是把这层认知负担整体接过来。Agent 从此只需要表达意图：「把这份 PDF 变成 Markdown」，剩下的由十九件工具在背后完成。

## 二、一次调用的完整旅程

假设你对 Agent 说：解析一下这篇论文，我要 Markdown。

Agent 调用 `mineru_parse_document`，传入一个源。这一步之后发生的事，它其实不需要知道，但值得记下来：

服务端先创建一个解析任务，拿回 `job_id` 和一个 `queued` 状态。然后进入轮询——每两秒问一次进度，同时通过 MCP 的 `report_progress` 把 `completed/total` 推回 Agent，于是用户在界面上能看到进度条在动，而不是对着一个静默的等待。任务一旦进入终态（`completed`/`partial`/`failed`/`canceled`），轮询立刻停止。

接着是产物处理。MinerU 返回的并不是文本，而是若干个 `OutputFileRef`——每种格式一个 `file_id` 加字节数。封装层遍历这些引用，逐个调 `GET /v1/files/{file_id}/content` 把内容取回，落盘到 `mineru_outputs/`。文件名取自服务端元数据（`tiny.pdf.md`、`tiny.pdf.middle.json` 这种），并经 `safe_filename()` 清洗——因为解析产物可能包含 ZIP，而 ZIP 里的条目名是不可信输入。

最后，如果调用方要求 `return_content`，文本类产物（markdown/html/latex/json）会被直接内联进返回值，超过 `max_content_chars` 则截断并标注 `content_truncated: true` 与真实长度。Agent 于是当场就能读到内容，无需再发起第二次工具调用。

实测中，一份 632KB、13 页的中文 PDF 走 standard 档位，两页范围解析耗时 1488 毫秒，Markdown 里的目录层级、标题、正文都被正确还原。整本约六十五秒。

## 三、十九件工具，三种性格

**六件是「问情况」的。** `mineru_server_info` 是其中最值得先调的一个——它把 health、tiers、models、usage 四个端点并发聚合，一次返回服务版本、启用的输出格式、可用的输入源类型、四档解析能力及其绑定模型、以及当前的用量与限额。Agent 由此知道「这台 MinerU 能做什么」，而不用靠猜。其余五件（`health`、`list_models`、`get_model`、`list_tiers`、`get_usage`）是它的单点拆解，用于精确查询。

**七件是「管文件」的。** `mineru_upload_file` 把 MinerU 那套 OpenAI 兼容的三步上传（创建 → PUT 裸字节 → complete）压成一次调用，中途自动计算 SHA256 并在 complete 时提交校验；上传前还会先查 `max_file_size_bytes`，超限直接本地拒绝，不浪费一次网络往返。它优先使用服务端返回的 `upload_url` 与 `upload_headers`——这意味着云模式下走对象存储直传也能正常工作。配套的 `get_upload`、`cancel_upload` 管上传生命周期，`list_files`、`get_file`、`delete_file`、`download_file` 管文件生命周期，列表类支持游标分页与按 purpose 过滤（源文档 / 解析产物 / 输入图片）。

**六件是「跑任务」的。** 除了上面那个一步式的 `mineru_parse_document`，另外五件把异步能力完整暴露出来：`create_parse_job` 支持一次提交多达一百个文件、四种源类型、四档 tier、三种 OCR 模式、七种输出格式、页码范围选择（`1-5,8,r3-r1` 这类语法），以及 webhook 回调；`get_parse_job` 与 `list_parse_jobs` 负责观测；`cancel_parse_job` 负责止损；`download_job_results` 负责批量收产物。

这六件的存在意义在于：一步式工具适合「我现在就要结果」，异步组合适合「我要提交二十份文档，晚点回来收」。两者共用同一套底层封装，行为一致。

## 四、那些看不见的地方

真正决定这东西能不能长期用的，是不起眼的几处。

**代理绕过。** 这是开发过程中发现的一个真实故障。`httpx` 默认 `trust_env=True`，会把系统代理应用到所有请求——包括发往 `127.0.0.1` 的那些。在带透明代理的企业网络里，代理往往无法回连本机，于是「本地 WSL Docker 部署」这个最主流的场景直接连不上，而且报错来自代理（一个令人困惑的 502），根本看不出问题在哪。现在的处理是：解析 base_url 的主机名，凡是回环或私网网段（127.x、10.x、172.16-31.x、192.168.x、169.254.x、localhost、::1）自动关闭 `trust_env` 直连，公网地址则保留环境代理。需要强制时用 `MINERU_USE_PROXY=0/1` 或 `--no-proxy` 覆盖。

**错误翻译。** MinerU 的错误有两种形状：业务错误是 `{"error":{type,code,message,param}}`，FastAPI 的参数校验错误是 `{"detail":[...]}`。再加上网络层的超时、拒连、DNS 失败——如果原样丢给 Agent，它面对的将是三种互不相干的结构。现在全部收敛为 `{"ok": false, "error": {"status_code","type","message","code","param"}}`。成功则是 `{"ok": true, "data": ...}`。Agent 判断一个字段就知道成败，读 message 就知道该怎么纠正——比如收到 `page_range_invalid` 它会自己改掉倒序区间，收到 `job_already_terminal` 它知道不必再等。

**stdio 洁净。** Server 的所有日志走 stderr。这听起来是常识，但一旦有一行 print 漏到 stdout，JSON-RPC 流就被污染，整个 MCP 会话会莫名其妙地断掉，且极难排查。

**协议流内的进度。** 长任务不靠打印日志，靠 MCP 原生的 `report_progress`。同时 `Context` 通过类型注解自动注入，不会泄漏到客户端看到的 inputSchema 里。

**SDK 版本锁定。** 依赖钉死 `mcp<2`。2.x 把 `FastMCP` 重构成了 `MCPServer`，API 全面变动，而当前主流 Agent 客户端实现仍围绕 1.x 的 stdio 协议。选 1.30.0 是为了「所有 Agent 工具都能用」这个硬性要求，不是为了追新。

## 五、换个地址就搬家

整套代码里没有任何一处硬编码的服务地址。配置只有六项，全部既能用环境变量也能用 CLI 参数（CLI 优先）：`MINERU_API_BASE`、`MINERU_API_KEY`、`MINERU_OUTPUT_DIR`、`MINERU_TIMEOUT`、`MINERU_VERIFY_SSL`、`MINERU_USE_PROXY`，外加一个注入自定义请求头的 `MINERU_HEADERS`。

本地 WSL Docker 是 `http://127.0.0.1:8000`；搬到远端算力服务器，改成 `http://<server-ip>:<port>`，若网关开了鉴权再补一个 `MINERU_API_KEY`（自动成为 `Authorization: Bearer`），自签名证书就把 `MINERU_VERIFY_SSL` 设 0。代码零改动。

传输层同样可换。默认 stdio——Agent 拉起子进程，这是各家的标准接法；也可以用 `--transport streamable-http --host 0.0.0.0 --port 8765` 让 Server 常驻在一台能访问 MinerU 的机器上，多个 Agent 通过 URL 共享同一个实例；SSE 亦支持。三种形态共用同一份工具定义。

## 六、诚实地说明边界

有几件事它做不到，或者取决于服务端配置，说清楚比含糊好。

**并发是一。** 本地部署的 `max_concurrent_jobs` 实测为 1。批量提交二十份文档，它们会排队；如果轮询超时设得太短，后面的任务会在队列里等到超时。工具会在这种情况下抛出带当前状态的超时错误，并提示「可稍后用 get_job 继续查询」——任务并未丢失，只是还没轮到。

**local 源默认关闭。** 服务端未加 `--allow-local-source` 时，`{"type":"local","path":...}` 会被拒绝（400 `unsupported_source`）。工具保留了这个源类型以兼容开启该选项的部署，但本地场景请走 `mineru_upload_file` 或 `url`、`inline`。

**输出格式看服务端。** 协议定义了七种（markdown、middle_json、structured_content、html、latex、docx、zip），当前实例只启用了四种：markdown、middle_json、structured_content、zip。请求未启用的格式不会得到产物。调用前用 `mineru_health` 看一眼 `features.output_formats` 即可确认。

**webhook 未启用。** `callback` 参数已按协议实现并保留，当前实例 `features.webhook` 为 false，故实际不会触发回调。轮询是目前唯一可靠的完成通知方式。

**匿名访问。** 当前实例无鉴权（`access_level: anonymous`），文件与任务按租户隔离的能力依赖服务端配置。若把它暴露到公网，务必在网关层加鉴权——`MINERU_API_KEY` 已经准备好了。

## 七、验证状态

十九件工具全部经过实测，无一跳过。两套测试合计 26 项断言全绿：`client.py test` 覆盖 17 项（连通、清单、健康、聚合信息、档位、模型、用量、文件列表分页、任务列表、404 错误翻译、一步式解析并落盘、文件 get/download/delete 全链、异步 create→get→download），`coverage_test.py` 补足 9 项（单模型查询与 404、三步上传、file_id 源解析、上传不存在文件的边界、任务取消的 canceled/409 双语义、上传状态查询与取消）。

传输层三种形态均已验证：stdio 拉起子进程完整跑通、streamable-http 独立进程监听 8765 端口后客户端成功 initialize 并调用工具、CLI 单次调用模式输出结构化 JSON。真实文档端到端通过。

---

*源文件：`mineru-mcp/docs/narrative.md`。技术细节与接入配置见同目录 `README.md`，分层架构图见 `docs/architecture.svg`。*
