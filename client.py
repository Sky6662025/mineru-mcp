#!/usr/bin/env python3
"""
MinerU MCP Client（通用客户端）
================================

面向「所有 Agent 工具」的 MCP 客户端，三种用途：

1) 库调用：任何 Python Agent 框架可 `from client import MinerUMCPClient`，
   用 await client.call_tool("mineru_parse_document", {...}) 直接驱动。
2) 命令行：`python client.py --api-base http://127.0.0.1:8000 test`
   一键对 MCP Server 做端到端自检（连接→列举工具→逐个冒烟调用）。
3) 交互式：`python client.py shell` 进入 REPL，手动 list/call 任意工具。

传输：默认 stdio（拉起 server.py 子进程，Agent 工具的标准接法），
也支持 --transport streamable-http 连接远程已部署的 MCP Server。

MCP 协议版本：基于 mcp 1.x Python SDK（Claude/Cursor/Cline/WorkBuddy 通用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER_PY = str(HERE / "server.py")

# 让子进程能 import 到同目录的 mineru_client.py
_CHILD_ENV_EXTRA = {"PYTHONPATH": str(HERE), "PYTHONIOENCODING": "utf-8"}


def _build_stdio_params(api_base: Optional[str], api_key: Optional[str],
                        output_dir: Optional[str], insecure: bool,
                        python_exe: Optional[str]) -> StdioServerParameters:
    env = dict(os.environ)
    env.update(_CHILD_ENV_EXTRA)
    args = [SERVER_PY]
    if api_base:
        args += ["--api-base", api_base]
    if api_key:
        args += ["--api-key", api_key]
    if output_dir:
        args += ["--output-dir", output_dir]
    if insecure:
        args += ["--insecure"]
    return StdioServerParameters(
        command=python_exe or sys.executable,
        args=args,
        env=env,
    )


class MinerUMCPClient:
    """通过 stdio 连接 MinerU MCP Server 的异步客户端（上下文管理器）。"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        output_dir: Optional[str] = None,
        insecure: bool = False,
        python_exe: Optional[str] = None,
        server_py: Optional[str] = None,
    ) -> None:
        self.api_base = api_base or os.environ.get("MINERU_API_BASE")
        self.api_key = api_key or os.environ.get("MINERU_API_KEY")
        self.output_dir = output_dir or os.environ.get("MINERU_OUTPUT_DIR")
        self.insecure = insecure
        self.python_exe = python_exe
        self.server_py = server_py or SERVER_PY
        self._session: Optional[ClientSession] = None
        self._stack: Any = None
        self._tools_cache: Optional[list] = None

    async def __aenter__(self) -> "MinerUMCPClient":
        from contextlib import AsyncExitStack
        self._stack = AsyncExitStack()
        params = _build_stdio_params(
            self.api_base, self.api_key, self.output_dir, self.insecure, self.python_exe)
        # server_py 可被覆盖（测试时指向自定义脚本）
        params.args[0] = self.server_py
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    # ---- 基础能力 ---------------------------------------------------------
    async def list_tools(self, refresh: bool = False) -> list[dict]:
        assert self._session, "client not connected"
        if self._tools_cache is not None and not refresh:
            return self._tools_cache
        resp = await self._session.list_tools()
        self._tools_cache = [
            {"name": t.name, "description": t.description or "",
             "inputSchema": t.inputSchema}
            for t in resp.tools
        ]
        return self._tools_cache

    async def tool_names(self) -> list[str]:
        return [t["name"] for t in await self.list_tools()]

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """调用工具，返回解析后的结构化结果（dict/list）。

        MCP 工具结果封装在 content[].text；本客户端自动把 JSON 文本反解为对象，
        失败时原样返回文本。isError=True 时抛 RuntimeError。
        """
        assert self._session, "client not connected"
        result = await self._session.call_tool(name, arguments or {})
        texts = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                texts.append(block.text)
        joined = "\n".join(texts) if texts else ""
        parsed: Any = joined
        if joined:
            try:
                parsed = json.loads(joined)
            except json.JSONDecodeError:
                parsed = joined
        if result.isError:
            raise RuntimeError(f"tool '{name}' returned error: {joined[:800]}")
        return parsed

    # ---- 便捷封装 ---------------------------------------------------------
    async def server_info(self) -> Any:
        return await self.call_tool("mineru_server_info")

    async def parse_document(self, source: dict, **kw: Any) -> Any:
        return await self.call_tool("mineru_parse_document", {"source": source, **kw})


# ---------------------------------------------------------------------------
# CLI: test —— 端到端自检
# ---------------------------------------------------------------------------
async def _cmd_test(args: argparse.Namespace) -> int:
    pdf = args.pdf or str(HERE / "dev" / "tiny.pdf")
    if not Path(pdf).is_file():
        print(f"[WARN] 测试 PDF 不存在: {pdf}，将跳过 inline 解析用例")
        pdf = None

    import base64
    passed, failed = 0, 0

    def check(name: str, cond: bool, detail: str = "") -> bool:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}  {detail}")
        return cond

    print(f"连接 MCP Server (api_base={args.api_base or 'env/default'}) ...")
    async with MinerUMCPClient(
        api_base=args.api_base, api_key=args.api_key,
        output_dir=args.output_dir, insecure=args.insecure,
    ) as cli:
        names = await cli.tool_names()
        print(f"已连接，发现 {len(names)} 个工具。")

        print("\n== 1. 工具清单 ==")
        check("tool count >= 19", len(names) >= 19, f"got {len(names)}")
        for n in names:
            print("   -", n)

        print("\n== 2. mineru_health ==")
        h = await cli.call_tool("mineru_health")
        check("health.ok", h.get("ok") is True, json.dumps(h, ensure_ascii=False)[:200])
        if h.get("ok"):
            d = h["data"]
            print(f"   version={d.get('version')} formats={d.get('features',{}).get('output_formats')} sources={d.get('features',{}).get('sources')}")

        print("\n== 3. mineru_server_info ==")
        si = await cli.call_tool("mineru_server_info")
        check("server_info.ok", si.get("ok") is True)
        if si.get("ok"):
            print(f"   tiers={[t['id'] for t in si['data']['tiers']]} models={len(si['data']['models'])}")

        print("\n== 4. mineru_list_tiers / list_models / get_usage ==")
        for tool in ("mineru_list_tiers", "mineru_list_models", "mineru_get_usage"):
            r = await cli.call_tool(tool)
            check(f"{tool}.ok", r.get("ok") is True, str(r)[:150])

        print("\n== 5. mineru_list_files (分页) ==")
        lf = await cli.call_tool("mineru_list_files", {"limit": 5})
        check("list_files.ok", lf.get("ok") is True)

        print("\n== 6. mineru_list_parse_jobs ==")
        lj = await cli.call_tool("mineru_list_parse_jobs", {"limit": 5})
        check("list_jobs.ok", lj.get("ok") is True)

        print("\n== 7. 错误翻译：不存在的 job ==")
        e1 = await cli.call_tool("mineru_get_parse_job", {"job_id": "job_does_not_exist"})
        check("get_job 404 -> ok=false", e1.get("ok") is False and e1.get("error", {}).get("status_code") == 404,
              json.dumps(e1, ensure_ascii=False)[:200])

        print("\n== 8. 一步式解析 mineru_parse_document (inline, flash) ==")
        if pdf:
            b64 = base64.b64encode(Path(pdf).read_bytes()).decode()
            outdir = args.output_dir or str(HERE / "mineru_outputs")
            pr = await cli.parse_document(
                {"type": "inline", "name": Path(pdf).name, "data": b64},
                tier="flash", output_formats=["markdown", "middle_json"],
                wait=True, timeout=180, download=True, output_dir=outdir,
                return_content=True,
            )
            okp = pr.get("ok") is True and pr.get("data", {}).get("status") == "completed"
            check("parse_document completed", okp, json.dumps(pr, ensure_ascii=False)[:300])
            if okp:
                outputs = pr["data"]["outputs"]
                print(f"   job_id={pr['data']['job_id']} outputs={len(outputs)}")
                for o in outputs:
                    print(f"     fmt={o['format']} saved={o.get('saved_path')} bytes={o.get('bytes')}")
                md = [o for o in outputs if o["format"] == "markdown"]
                check("markdown 产物存在", bool(md))
                if md and md[0].get("saved_path"):
                    check("markdown 落盘可读", Path(md[0]["saved_path"]).is_file())

                print("\n== 9. 文件管理：list→get→download→delete ==")
                if md:
                    fid = md[0]["file_id"]
                    gf = await cli.call_tool("mineru_get_file", {"file_id": fid})
                    check("get_file.ok", gf.get("ok") is True)
                    dl = await cli.call_tool("mineru_download_file",
                                             {"file_id": fid, "output_dir": outdir, "filename": "cli_dl.md"})
                    check("download_file.ok + path", dl.get("ok") is True and Path(dl["data"]["path"]).is_file(),
                          str(dl)[:150])
                    de = await cli.call_tool("mineru_delete_file", {"file_id": fid})
                    check("delete_file.ok", de.get("ok") is True and de["data"].get("deleted") is True)

                print("\n== 10. 异步任务：create_parse_job→get→cancel ==")
                cj = await cli.call_tool("mineru_create_parse_job", {
                    "sources": [{"source": {"type": "inline", "name": "t.pdf", "data": b64}}],
                    "tier": "flash", "output_formats": ["markdown"],
                })
                if cj.get("ok"):
                    jid = cj["data"]["job_id"]
                    gj = await cli.call_tool("mineru_get_parse_job", {"job_id": jid})
                    check("async get_job.ok", gj.get("ok") is True)
                    # 等它终态再下产物（并发=1，需等前面的完成）
                    for _ in range(60):
                        st = (await cli.call_tool("mineru_get_parse_job", {"job_id": jid}))["data"]["status"]
                        if st in ("completed", "partial", "failed", "canceled"):
                            break
                        await asyncio.sleep(2)
                    dr = await cli.call_tool("mineru_download_job_results",
                                             {"job_id": jid, "output_dir": outdir, "return_content": True})
                    check("download_job_results.ok", dr.get("ok") is True, str(dr)[:200])
                else:
                    check("create_parse_job.ok", False, str(cj)[:200])
        else:
            print("   [SKIP] 无测试 PDF")

    print(f"\n========== 自检结果: {passed} passed, {failed} failed ==========")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI: shell —— 交互式 REPL
# ---------------------------------------------------------------------------
async def _cmd_shell(args: argparse.Namespace) -> int:
    print("MinerU MCP 交互式客户端。命令: ")
    print("  tools                      列出所有工具")
    print("  schema <tool>              查看工具入参 schema")
    print("  call <tool> '<json-args>'  调用工具")
    print("  info                       = call mineru_server_info")
    print("  quit / exit                退出")
    async with MinerUMCPClient(api_base=args.api_base, api_key=args.api_key,
                               output_dir=args.output_dir, insecure=args.insecure) as cli:
        await cli.list_tools()
        while True:
            try:
                line = (await asyncio.to_thread(input, "mineru> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            try:
                if line == "tools":
                    for t in await cli.list_tools():
                        print(f"  {t['name']}: {t['description'][:80]}")
                elif line == "info":
                    print(json.dumps(await cli.server_info(), ensure_ascii=False, indent=2))
                elif line.startswith("schema "):
                    tn = line.split(None, 1)[1].strip()
                    t = next((x for x in await cli.list_tools() if x["name"] == tn), None)
                    print(json.dumps(t["inputSchema"] if t else {"error": "no such tool"}, ensure_ascii=False, indent=2))
                elif line.startswith("call "):
                    rest = line.split(None, 1)[1]
                    tn, _, js = rest.partition(" ")
                    argv = json.loads(js) if js.strip() else {}
                    print(json.dumps(await cli.call_tool(tn.strip(), argv), ensure_ascii=False, indent=2)[:4000])
                else:
                    print("未知命令，输入 tools 查看帮助")
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {exc}")
    return 0


# ---------------------------------------------------------------------------
# CLI: call —— 单次调用（便于脚本/其他语言 Agent 通过子进程驱动）
# ---------------------------------------------------------------------------
async def _cmd_call(args: argparse.Namespace) -> int:
    argv = json.loads(args.arguments) if args.arguments else {}
    async with MinerUMCPClient(api_base=args.api_base, api_key=args.api_key,
                               output_dir=args.output_dir, insecure=args.insecure) as cli:
        res = await cli.call_tool(args.tool, argv)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="MinerU MCP 通用客户端")
    ap.add_argument("--api-base", help="MinerU API 地址（透传给 server 子进程）")
    ap.add_argument("--api-key", help="可选 Bearer Token")
    ap.add_argument("--output-dir", help="产物保存目录")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 校验")
    ap.add_argument("--python", dest="python_exe", help="拉起 server 用的 python 解释器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_test = sub.add_parser("test", help="端到端自检")
    p_test.add_argument("--pdf", help="测试用本地 PDF 路径（默认 dev/tiny.pdf）")
    p_test.set_defaults(func=_cmd_test)

    p_shell = sub.add_parser("shell", help="交互式 REPL")
    p_shell.set_defaults(func=_cmd_shell)

    p_call = sub.add_parser("call", help="单次调用一个工具")
    p_call.add_argument("tool", help="工具名")
    p_call.add_argument("arguments", nargs="?", default="{}", help="JSON 参数")
    p_call.set_defaults(func=_cmd_call)

    args = ap.parse_args()
    # Windows 下 asyncio 默认 ProactorEventLoop 对子进程 stdio 支持良好，无需特殊处理
    code = asyncio.run(args.func(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
