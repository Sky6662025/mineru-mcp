#!/usr/bin/env python3
"""
MinerU v4 MCP Server
====================

将 MinerU v4 REST API 的全部能力封装为 MCP 工具，任何支持 MCP 的 Agent
（Claude Desktop / Cursor / Cline / WorkBuddy / Cherry Studio 等）均可直接调用。

对接方式（填一个 API 地址即可）：
  * 本地 WSL Docker 部署:  MINERU_API_BASE=http://127.0.0.1:8000
  * 远端算力服务器部署:    MINERU_API_BASE=http://<server-ip>:<port>
  * 可选鉴权（远端网关）:  MINERU_API_KEY=xxx

运行:
  python server.py                                  # stdio（Agent 工具标准接法）
  python server.py --api-base http://1.2.3.4:8000   # CLI 覆盖
  python server.py --transport streamable-http --port 8765   # 远程 HTTP 部署
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from mineru_client import (
    MinerUAPIError,
    MinerUClient,
    OUTPUT_EXT_MAP,
    SUPPORTED_OCR_MODES,
    SUPPORTED_OUTPUT_FORMATS,
    SUPPORTED_TIERS,
    TERMINAL_JOB_STATUSES,
    guess_mime,
)

# ---------------------------------------------------------------------------
# 全局配置（由 CLI/环境变量填充）
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = str(Path.cwd() / "mineru_outputs")

CONFIG = {
    "api_base": os.environ.get("MINERU_API_BASE", "http://127.0.0.1:8000"),
    "api_key": os.environ.get("MINERU_API_KEY") or None,
    "output_dir": os.environ.get("MINERU_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR,
    "timeout": float(os.environ.get("MINERU_TIMEOUT", "60")),
    "verify_ssl": os.environ.get("MINERU_VERIFY_SSL", "1").lower() not in ("0", "false", "no"),
    "use_proxy": None,  # None=本地/私网自动绕过代理，公网沿用环境代理
}

_raw_proxy = os.environ.get("MINERU_USE_PROXY")
if _raw_proxy is not None:
    CONFIG["use_proxy"] = _raw_proxy.strip().lower() not in ("0", "false", "no", "off")

_client: Optional[MinerUClient] = None


def get_client() -> MinerUClient:
    """惰性单例客户端（FastMCP 工具在事件循环内调用，此处安全）。"""
    global _client
    if _client is None:
        _client = MinerUClient(
            base_url=CONFIG["api_base"],
            api_key=CONFIG["api_key"],
            timeout=CONFIG["timeout"],
            verify_ssl=CONFIG["verify_ssl"],
            use_proxy=CONFIG["use_proxy"],
        )
    return _client


def _err(exc: MinerUAPIError) -> dict:
    """把 API 错误翻译成结构化返回值（Agent 可读、可自行纠正）。"""
    return {"ok": False, "error": exc.to_dict()}


def _ok(data: Any, **extra: Any) -> dict:
    if isinstance(data, dict):
        return {"ok": True, **extra, "data": data}
    return {"ok": True, **extra, "data": data}


def _resolve_output_dir(output_dir: Optional[str]) -> str:
    d = output_dir or CONFIG["output_dir"]
    Path(d).expanduser().mkdir(parents=True, exist_ok=True)
    return str(Path(d).expanduser())


TEXT_FORMATS = {"markdown", "html", "latex", "middle_json", "structured_content"}


async def _collect_job_outputs(
    client: MinerUClient,
    job: dict,
    download: bool,
    output_dir: Optional[str],
    return_content: bool,
    max_content_chars: int,
) -> list[dict]:
    """把 job 的 output_files 引用转换为可落盘/可内联的结果列表。"""
    results: list[dict] = []
    dest = _resolve_output_dir(output_dir) if download else None
    for f in job.get("files") or []:
        of = f.get("output_files") or {}
        for fmt, ref in of.items():
            if not ref or not isinstance(ref, dict):
                continue
            fid = ref.get("file_id")
            if not fid:
                continue
            item: dict = {
                "source_file": f.get("name"),
                "format": fmt,
                "file_id": fid,
                "bytes": ref.get("bytes"),
            }
            if download and dest:
                try:
                    meta = await client.get_file(fid)
                    fname = meta.get("filename") or f"{f.get('name', 'output')}{OUTPUT_EXT_MAP.get(fmt, '.bin')}"
                    saved = await client.download_output_file(fid, dest, filename=fname)
                    item["saved_path"] = saved["path"]
                except MinerUAPIError as e:
                    item["download_error"] = e.to_dict()
            if return_content and fmt in TEXT_FORMATS:
                try:
                    data, _ct = await client.get_file_content_bytes(fid)
                    text = data.decode("utf-8", errors="replace")
                    if len(text) > max_content_chars:
                        item["content"] = text[:max_content_chars]
                        item["content_truncated"] = True
                        item["content_total_chars"] = len(text)
                    else:
                        item["content"] = text
                        item["content_truncated"] = False
                except MinerUAPIError as e:
                    item["content_error"] = e.to_dict()
            results.append(item)
    return results


# ---------------------------------------------------------------------------
# MCP Server 定义
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "mineru-mcp",
    instructions=(
        "MinerU v4 文档解析服务（PDF/图片 → Markdown/JSON/HTML/LaTeX/DOCX/ZIP）。"
        "典型流程：mineru_parse_document 一步完成 上传→解析→等待→取回内容；"
        "长任务可用 mineru_create_parse_job 异步提交，再用 mineru_get_parse_job 查询、"
        "mineru_download_job_results 下载产物。开始前可调用 mineru_server_info 了解服务能力。"
    ),
)

# =========================== 系统信息类工具 ================================

@mcp.tool()
async def mineru_server_info() -> dict:
    """获取 MinerU 服务聚合信息：健康状态、版本、支持的输出格式与输入源、
    解析档位(tier)、可用模型、用量限额。首次使用/排障时先调用本工具。"""
    c = get_client()
    try:
        health, tiers, models, usage = await asyncio.gather(
            c.health(), c.list_tiers(), c.list_models(), c.get_usage(),
        )
    except MinerUAPIError as e:
        return _err(e)
    return _ok({
        "api_base": c.base_url,
        "health": health,
        "tiers": tiers.get("data", []),
        "models": models.get("data", []),
        "usage": usage,
    })


@mcp.tool()
async def mineru_health() -> dict:
    """健康检查（GET /v1/health）：返回服务状态、MinerU 版本、
    启用的输出格式(output_formats)与输入源类型(sources)、webhook 支持。"""
    try:
        return _ok(await get_client().health())
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_list_models() -> dict:
    """列出全部可用解析模型（GET /v1/models），含模型 ID 与归属。"""
    try:
        return _ok(await get_client().list_models())
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_get_model(
    model: Annotated[str, Field(description="模型 ID，如 MinerU2.5-Pro-2605-1.2B")],
) -> dict:
    """查询单个模型详情（GET /v1/models/{model}）。"""
    try:
        return _ok(await get_client().get_model(model))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_list_tiers() -> dict:
    """列出解析档位（GET /v1/tiers）：flash(极速本地文本)/basic(轻量)/
    standard(标准,多数文档)/advanced(复杂文档)。返回各档当前绑定模型。"""
    try:
        return _ok(await get_client().list_tiers())
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_get_usage() -> dict:
    """查询当前用量与限额（GET /v1/usage）：已处理页数/文件数/任务数、
    单文件最大页数、最大文件大小、并发任务上限等。"""
    try:
        return _ok(await get_client().get_usage())
    except MinerUAPIError as e:
        return _err(e)

# ============================ 文件上传类工具 ================================

@mcp.tool()
async def mineru_upload_file(
    file_path: Annotated[str, Field(description="要上传的本地文件绝对路径（PDF/图片等）")],
    purpose: Annotated[Literal["parse", "input_image"], Field(description="用途：parse=待解析文档，input_image=输入图片")] = "parse",
    expires_seconds: Annotated[Optional[int], Field(description="文件保留秒数（3600~2592000），缺省用服务端默认值", ge=3600, le=2592000)] = None,
) -> dict:
    """上传本地文件到 MinerU（自动完成 创建上传→PUT字节→complete 三步，含 SHA256 校验）。
    返回 file_id，可用于后续创建解析任务。"""
    c = get_client()
    try:
        usage = await c.get_usage()
        max_bytes = (usage.get("limits") or {}).get("max_file_size_bytes")
        result = await c.upload_local_file(
            file_path, purpose=purpose,
            expires_seconds=expires_seconds, max_bytes=max_bytes,
        )
    except MinerUAPIError as e:
        return _err(e)
    result.pop("upload", None)
    return _ok(result)


@mcp.tool()
async def mineru_get_upload(
    upload_id: Annotated[str, Field(description="上传 ID（upload_...）")],
) -> dict:
    """查询上传状态（GET /v1/uploads/{upload_id}）：pending/completed/cancelled/expired。"""
    try:
        return _ok(await get_client().get_upload(upload_id))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_cancel_upload(
    upload_id: Annotated[str, Field(description="上传 ID（upload_...）")],
) -> dict:
    """取消进行中的上传（POST /v1/uploads/{upload_id}/cancel）。已完成的上传无法取消(409)。"""
    try:
        return _ok(await get_client().cancel_upload(upload_id))
    except MinerUAPIError as e:
        return _err(e)

# ============================ 文件管理类工具 ================================

@mcp.tool()
async def mineru_list_files(
    limit: Annotated[int, Field(description="每页数量 1~1000", ge=1, le=1000)] = 100,
    after: Annotated[Optional[str], Field(description="分页游标：上一页返回的 last_id")] = None,
    order: Annotated[Literal["asc", "desc"], Field(description="按创建时间排序方向")] = "desc",
    purpose: Annotated[Optional[Literal["parse", "parse_output", "input_image"]], Field(description="按用途过滤")] = None,
) -> dict:
    """列出文件（GET /v1/files），游标分页。purpose: parse=源文档 / parse_output=解析产物 / input_image=输入图片。
    返回 data[]、first_id、last_id、has_more。"""
    try:
        return _ok(await get_client().list_files(after=after, limit=limit, order=order, purpose=purpose))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_get_file(
    file_id: Annotated[str, Field(description="文件 ID（file-...）")],
) -> dict:
    """查询文件元数据（GET /v1/files/{file_id}）：文件名、大小、用途、SHA256、过期时间。"""
    try:
        return _ok(await get_client().get_file(file_id))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_delete_file(
    file_id: Annotated[str, Field(description="文件 ID（file-...）")],
) -> dict:
    """删除文件（DELETE /v1/files/{file_id}）。⚠️ 不可恢复，删除前请确认 ID。"""
    try:
        return _ok(await get_client().delete_file(file_id))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_download_file(
    file_id: Annotated[str, Field(description="文件 ID（file-...）")],
    output_dir: Annotated[Optional[str], Field(description="保存目录，缺省用 MINERU_OUTPUT_DIR")] = None,
    filename: Annotated[Optional[str], Field(description="自定义保存文件名，缺省用服务端记录的原名")] = None,
) -> dict:
    """下载文件内容到本地（GET /v1/files/{file_id}/content）。
    常用于取回解析产物（markdown/json/zip 等）。返回保存路径与字节数。"""
    c = get_client()
    try:
        dest = _resolve_output_dir(output_dir)
        saved = await c.download_output_file(file_id, dest, filename=filename)
    except MinerUAPIError as e:
        return _err(e)
    return _ok(saved)
# ============================ 解析任务类工具 ================================

@mcp.tool()
async def mineru_create_parse_job(
    sources: Annotated[list[dict], Field(description=(
        "文件源列表(1~100个)，每项 {source, page_range?}。source 四种类型："
        "{\"type\":\"local\",\"path\":\"/abs/path.pdf\"} 本地路径（需服务端启用 local 源，且为服务端容器内路径）；"
        "{\"type\":\"url\",\"url\":\"https://...\"} 远程 URL（推荐，本地部署常用）；"
        "{\"type\":\"file_id\",\"file_id\":\"file-...\"} 已上传文件（先用 mineru_upload_file）；"
        "{\"type\":\"inline\",\"name\":\"x.pdf\",\"data\":\"<base64>\"} 内联 base64。"
        "page_range 例 \"1-5,8\" 或 \"all\"，留空为全部。"
    ))],
    tier: Annotated[Optional[Literal["flash", "basic", "standard", "advanced"]], Field(description="解析档位，缺省 standard")] = None,
    ocr_mode: Annotated[Literal["auto", "txt", "ocr"], Field(description="OCR模式：auto=自动检测/txt=强制原生文本/ocr=强制OCR")] = "auto",
    output_formats: Annotated[list[str], Field(description="输出格式列表，取值见 mineru_health 的 features.output_formats，常用 [\"markdown\"]")] = ["markdown"],
    callback_url: Annotated[Optional[str], Field(description="完成回调 webhook URL（需服务端启用 webhook 特性）")] = None,
    callback_secret: Annotated[Optional[str], Field(description="回调签名密钥")] = None,
) -> dict:
    """创建异步解析任务（POST /v1/parse/jobs，返回 202）。立即返回 job_id 不阻塞。
    之后用 mineru_get_parse_job 轮询状态，完成后用 mineru_download_job_results 取回产物。
    注意：服务端并发任务数有限(max_concurrent_jobs，本地常为1)，请串行提交。"""
    c = get_client()
    files = []
    for i, s in enumerate(sources or []):
        if not isinstance(s, dict) or "source" not in s:
            raise ToolError(f"sources[{i}] 必须是含 'source' 键的对象")
        src = s["source"]
        if not isinstance(src, dict) or src.get("type") not in ("file_id", "url", "inline", "local"):
            raise ToolError(f"sources[{i}].source.type 必须是 file_id/url/inline/local 之一")
        entry: dict = {"source": src}
        pr = s.get("page_range")
        if pr:
            entry["page_range"] = str(pr)
        files.append(entry)
    body: dict = {"files": files, "ocr_mode": ocr_mode,
                  "output_formats": list(output_formats or ["markdown"])}
    if tier:
        body["tier"] = tier
    if callback_url:
        cb: dict = {"url": callback_url}
        if callback_secret:
            cb["secret"] = callback_secret
        body["callback"] = cb
    try:
        return _ok(await c.create_job(body))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_get_parse_job(
    job_id: Annotated[str, Field(description="任务 ID（job_...）")],
) -> dict:
    """查询解析任务状态与结果（GET /v1/parse/jobs/{job_id}）。
    status: queued/running/completed/partial/failed/canceled。
    终态时 files[].output_files 给出各格式产物的 file_id 与字节数。"""
    try:
        return _ok(await get_client().get_job(job_id))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_list_parse_jobs(
    status: Annotated[Optional[str], Field(description="按状态过滤，逗号分隔，如 'running,queued'")] = None,
    limit: Annotated[int, Field(description="每页数量 1~100", ge=1, le=100)] = 20,
    after: Annotated[Optional[str], Field(description="分页游标：上一页 last_id")] = None,
    order: Annotated[Literal["asc", "desc"], Field(description="按创建时间排序方向")] = "desc",
    created_after: Annotated[Optional[str], Field(description="ISO-8601 创建时间下界，如 2026-09-01T00:00:00Z")] = None,
) -> dict:
    """列出解析任务（GET /v1/parse/jobs），游标分页。返回 job_id/status/created_at/file_count。"""
    try:
        return _ok(await get_client().list_jobs(
            status=status, limit=limit, after=after, order=order, created_after=created_after))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_cancel_parse_job(
    job_id: Annotated[str, Field(description="任务 ID（job_...）")],
) -> dict:
    """取消排队中或运行中的任务（DELETE /v1/parse/jobs/{job_id}）。
    已终态(completed/failed/canceled)的任务无法取消，返回 409。"""
    try:
        return _ok(await get_client().cancel_job(job_id))
    except MinerUAPIError as e:
        return _err(e)


@mcp.tool()
async def mineru_download_job_results(
    job_id: Annotated[str, Field(description="任务 ID（job_...）")],
    output_dir: Annotated[Optional[str], Field(description="保存目录，缺省用 MINERU_OUTPUT_DIR")] = None,
    formats: Annotated[Optional[list[str]], Field(description="只下载指定格式，缺省下载任务产出的全部格式")] = None,
    return_content: Annotated[bool, Field(description="是否在返回值中内联文本类产物内容（markdown/html/latex/json）")] = False,
    max_content_chars: Annotated[int, Field(description="内联内容最大字符数，超出截断", ge=100)] = 20000,
) -> dict:
    """下载已完成任务的全部产物到本地（遍历 output_files 逐个 GET content 落盘）。
    任务须为终态；return_content=true 时文本类结果直接内联返回，便于阅读。"""
    c = get_client()
    try:
        job = await c.get_job(job_id)
    except MinerUAPIError as e:
        return _err(e)
    if job.get("status") not in TERMINAL_JOB_STATUSES:
        raise ToolError(f"任务尚未结束(status={job.get('status')})，请先轮询 mineru_get_parse_job")
    if formats:
        want = set(formats)
        for f in job.get("files") or []:
            of = f.get("output_files") or {}
            f["output_files"] = {k: v for k, v in of.items() if k in want and v}
    outputs = await _collect_job_outputs(
        c, job, download=True, output_dir=output_dir,
        return_content=return_content, max_content_chars=max_content_chars,
    )
    return _ok({"job_id": job_id, "status": job.get("status"), "outputs": outputs},
               output_count=len(outputs))


@mcp.tool()
async def mineru_parse_document(
    ctx: Context,
    source: Annotated[dict, Field(description=(
        "单个文件源，四种类型之一："
        "{\"type\":\"local\",\"path\":\"/abs/path.pdf\"}（需服务端启用 local 源）；"
        "{\"type\":\"url\",\"url\":\"https://...\"}；"
        "{\"type\":\"file_id\",\"file_id\":\"file-...\"}；"
        "{\"type\":\"inline\",\"name\":\"x.pdf\",\"data\":\"<base64>\"}"
    ))],
    tier: Annotated[Optional[Literal["flash", "basic", "standard", "advanced"]], Field(description="解析档位，缺省 standard；快速预览用 flash")] = None,
    ocr_mode: Annotated[Literal["auto", "txt", "ocr"], Field(description="OCR模式")] = "auto",
    output_formats: Annotated[list[str], Field(description="输出格式，缺省 [\"markdown\"]")] = ["markdown"],
    page_range: Annotated[Optional[str], Field(description="页码选择，如 '1-5,8' 或 'all'，缺省全部")] = None,
    wait: Annotated[bool, Field(description="是否等待任务完成（true=同步阻塞轮询；false=立即返回job_id自行轮询）")] = True,
    timeout: Annotated[float, Field(description="wait=true 时的最长等待秒数", ge=5)] = 600.0,
    download: Annotated[bool, Field(description="完成后是否下载产物到本地")] = True,
    output_dir: Annotated[Optional[str], Field(description="产物保存目录，缺省用 MINERU_OUTPUT_DIR")] = None,
    return_content: Annotated[bool, Field(description="是否内联返回文本类产物内容")] = True,
    max_content_chars: Annotated[int, Field(description="内联内容最大字符数", ge=100)] = 20000,
) -> dict:
    """【一步式解析·最常用】提交单个文档并完成解析全流程：创建任务→(可选)等待→下载产物→(可选)内联内容。
    这是使用 MinerU 的主入口。wait=false 时退化为异步提交。
    返回 job 状态、各产物 file_id、本地保存路径与文本内容。"""
    c = get_client()
    if not isinstance(source, dict) or source.get("type") not in ("file_id", "url", "inline", "local"):
        raise ToolError("source.type 必须是 file_id/url/inline/local 之一")
    entry: dict = {"source": source}
    if page_range:
        entry["page_range"] = str(page_range)
    body = {"files": [entry], "ocr_mode": ocr_mode,
            "output_formats": list(output_formats or ["markdown"])}
    if tier:
        body["tier"] = tier
    try:
        job = await c.create_job(body)
    except MinerUAPIError as e:
        return _err(e)
    job_id = job["job_id"]
    if not wait:
        return _ok(job, job_id=job_id, waited=False,
                   hint="任务已提交，请用 mineru_get_parse_job 轮询，完成后用 mineru_download_job_results 取回产物")
    # 同步等待 + 进度上报
    async def _progress(j: dict) -> None:
        pg = j.get("progress") or {}
        try:
            await ctx.report_progress(
                progress=float(pg.get("completed", 0)),
                total=float(pg.get("total", 1)) or 1.0,
                message=f"{j.get('status')} {pg.get('completed',0)}/{pg.get('total',0)}",
            )
        except Exception:
            pass
    try:
        final = await c.wait_job(job_id, poll_interval=2.0, timeout=timeout, on_progress=_progress)
    except MinerUAPIError as e:
        return _err(e)
    outputs = await _collect_job_outputs(
        c, final, download=download, output_dir=output_dir,
        return_content=return_content, max_content_chars=max_content_chars,
    ) if download or return_content else []
    status = final.get("status")
    files = final.get("files") or []
    first_parse = (files[0].get("parse") or {}) if files else {}
    result = {
        "job_id": job_id,
        "status": status,
        "tier": final.get("tier"),
        "duration_ms": first_parse.get("duration_ms"),
        "outputs": outputs,
    }
    # 失败时把文件级错误带出来
    if status in ("failed", "partial"):
        result["file_errors"] = [
            {"name": f.get("name"), "error": f.get("error")}
            for f in final.get("files") or [] if f.get("error")
        ]
    return _ok(result, waited=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _apply_args(args: argparse.Namespace) -> None:
    if args.api_base:
        CONFIG["api_base"] = args.api_base
    if args.api_key:
        CONFIG["api_key"] = args.api_key
    if args.output_dir:
        CONFIG["output_dir"] = args.output_dir
    if args.timeout:
        CONFIG["timeout"] = args.timeout
    if args.insecure:
        CONFIG["verify_ssl"] = False
    if args.use_proxy is not None:
        CONFIG["use_proxy"] = args.use_proxy.lower() not in ("0", "false", "no", "off")
    elif args.no_proxy:
        CONFIG["use_proxy"] = False


def main() -> None:
    ap = argparse.ArgumentParser(description="MinerU v4 MCP Server")
    ap.add_argument("--api-base", help="MinerU API 地址，如 http://127.0.0.1:8000（覆盖环境变量 MINERU_API_BASE）")
    ap.add_argument("--api-key", help="可选 Bearer Token（覆盖 MINERU_API_KEY）")
    ap.add_argument("--output-dir", help="产物默认保存目录（覆盖 MINERU_OUTPUT_DIR）")
    ap.add_argument("--timeout", type=float, help="HTTP 超时秒数")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（自签名远端）")
    ap.add_argument("--use-proxy", choices=["0", "1", "true", "false"], default=None,
                    help="是否使用系统代理；缺省时本地/私网地址自动绕过代理")
    ap.add_argument("--no-proxy", action="store_true", help="等效 --use-proxy 0，强制直连")
    ap.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    _apply_args(args)

    if args.transport == "stdio":
        # stdio 模式下 stdout 被协议占用，日志一律走 stderr
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
