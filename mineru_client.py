"""
mineru_client.py — MinerU v4 REST API 异步客户端封装层
========================================================

严格对照 MinerU v4.0.x OpenAPI 规范实现（FastAPI 生成，OpenAI 兼容风格）：

  GET    /v1/health                              健康检查
  GET    /v1/models                              模型列表
  GET    /v1/models/{model}                      单个模型
  GET    /v1/tiers                               解析档位列表
  POST   /v1/uploads                             创建上传（三步上传 step1）
  GET    /v1/uploads/{upload_id}                 查询上传
  PUT    /v1/uploads/{upload_id}/content         上传原始字节（step2, octet-stream）
  POST   /v1/uploads/{upload_id}/complete        完成上传并生成 File 对象（step3）
  POST   /v1/uploads/{upload_id}/cancel          取消上传
  GET    /v1/files                               文件列表（游标分页）
  GET    /v1/files/{file_id}                     文件元数据
  DELETE /v1/files/{file_id}                     删除文件
  GET    /v1/files/{file_id}/content             下载文件内容（云模式 302 → 自动跟随）
  POST   /v1/parse/jobs                          创建解析任务（202 异步）
  GET    /v1/parse/jobs                          任务列表
  GET    /v1/parse/jobs/{job_id}                 任务详情/状态
  DELETE /v1/parse/jobs/{job_id}                 取消任务
  GET    /v1/usage                               用量与限额

错误响应统一为 {"error": {"type","code","message","param"}}；
FastAPI 422 校验错误为 {"detail": [...]}。两者均被翻译成 MinerUAPIError。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

__all__ = [
    "MinerUAPIError",
    "MinerUClient",
    "TERMINAL_JOB_STATUSES",
    "SUPPORTED_TIERS",
    "SUPPORTED_OCR_MODES",
    "SUPPORTED_OUTPUT_FORMATS",
    "OUTPUT_EXT_MAP",
    "PAGE_RANGE_RE",
]

# ---- 常量（来自 OpenAPI 枚举定义） -------------------------------------------
TERMINAL_JOB_STATUSES = {"completed", "partial", "failed", "canceled"}
SUPPORTED_TIERS = ("flash", "basic", "standard", "advanced")
SUPPORTED_OCR_MODES = ("auto", "txt", "ocr")
SUPPORTED_OUTPUT_FORMATS = (
    "markdown", "middle_json", "structured_content", "html", "latex", "docx", "zip",
)
UPLOAD_PURPOSES = ("parse", "input_image")
FILE_PURPOSES = ("parse", "parse_output", "input_image")
SOURCE_TYPES = ("file_id", "url", "inline", "local")

# 输出格式 → 本地保存扩展名（与 MinerU 服务端命名习惯一致，实测验证）
OUTPUT_EXT_MAP = {
    "markdown": ".md",
    "middle_json": ".middle.json",
    "structured_content": ".structured_content.json",
    "html": ".html",
    "latex": ".tex",
    "docx": ".docx",
    "zip": ".zip",
}

# 页码选择语法（1 起始，含端点；r 前缀表示倒数；'all' 全选）
# 例: "1-5,8,r3-r1" / "all"。倒序区间(如 5-1)由服务端拒绝，此处仅做形状校验。
PAGE_RANGE_RE = re.compile(r"^(all|r?\d+(?:-r?\d+)?)(?:,r?\d+(?:-r?\d+)?)*$")


class MinerUAPIError(Exception):
    """MinerU API 错误的统一表示（网络层错误也翻译到这里）。"""

    def __init__(
        self,
        status_code: int,
        error_type: str,
        message: str,
        code: Optional[str] = None,
        param: Optional[str] = None,
        raw: Any = None,
    ) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.code = code
        self.param = param
        self.raw = raw
        super().__init__(f"[{status_code}] {error_type}: {message}")

    def to_dict(self) -> dict:
        d = {
            "status_code": self.status_code,
            "type": self.error_type,
            "message": self.message,
        }
        if self.code:
            d["code"] = self.code
        if self.param:
            d["param"] = self.param
        return d


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _host_of(url: str) -> str:
    """从 URL 中提取主机名（不含端口）。"""
    m = re.match(r"^https?://([^/:?#]+)", url)
    return (m.group(1) if m else url).lower()


def _is_local_host(host: str) -> bool:
    """判断主机名是否为本机/回环/私网地址（这些地址不应走系统代理）。"""
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    if h == "::1":
        return True
    m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", h)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a == 127:                      # 回环
            return True
        if a == 10:                         # 私网 A 类
            return True
        if a == 172 and 16 <= b <= 31:      # 私网 B 类
            return True
        if a == 192 and b == 168:           # 私网 C 类
            return True
        if a == 169 and b == 254:           # 链路本地
            return True
    return False


def guess_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    if mime:
        return mime
    low = filename.lower()
    if low.endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"


def safe_filename(name: str, fallback: str = "download.bin") -> str:
    """清洗文件名，防止路径穿越（zip 解压与下载落盘共用）。"""
    name = (name or "").strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f]", "", name)
    if not name or name in (".", ".."):
        return fallback
    return name


class MinerUClient:
    """MinerU v4 API 异步客户端。

    - base_url 支持本地 WSL Docker（http://127.0.0.1:8000）或远端算力服务器。
    - api_key 可选：本地部署默认无鉴权；远端若开启网关鉴权，自动加
      ``Authorization: Bearer <key>``。
    - extra_headers 可注入任意自定义头（如租户 ID）。
    - follow_redirects=True：兼容云模式文件下载 302 → CDN。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        api_key: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        timeout: float = 60.0,
        verify_ssl: bool = True,
        use_proxy: Optional[bool] = None,
    ) -> None:
        base_url = (base_url or "").strip()
        if not base_url:
            raise ValueError("base_url 不能为空")
        if not re.match(r"^https?://", base_url):
            base_url = "http://" + base_url
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        headers = {"Accept": "application/json", "User-Agent": "mineru-mcp/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        # 代理策略：
        #   use_proxy=None（默认）→ 本地/私网地址自动绕过系统代理，公网地址沿用环境代理。
        #     原因：httpx 默认 trust_env=True，会把 http_proxy 应用到 127.0.0.1，
        #     而企业代理/透明代理通常无法回连本机，导致「本地 WSL Docker 部署」连不上。
        #   use_proxy=True  → 强制使用环境代理。
        #   use_proxy=False → 强制不使用任何代理。
        host = _host_of(self.base_url)
        if use_proxy is None:
            trust_env = not _is_local_host(host)
        else:
            trust_env = bool(use_proxy)
        self.trust_env = trust_env
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
            follow_redirects=True,
            verify=verify_ssl,
            trust_env=trust_env,
        )

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "MinerUClient":
        import os
        e = env if env is not None else os.environ
        extra = None
        raw_headers = e.get("MINERU_HEADERS")
        if raw_headers:
            try:
                extra = json.loads(raw_headers)
                if not isinstance(extra, dict):
                    raise ValueError("MINERU_HEADERS 必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"MINERU_HEADERS 解析失败: {exc}") from exc
        verify = e.get("MINERU_VERIFY_SSL", "1").strip().lower() not in ("0", "false", "no", "off")
        raw_proxy = e.get("MINERU_USE_PROXY")
        use_proxy = None if raw_proxy is None else raw_proxy.strip().lower() not in ("0", "false", "no", "off")
        return cls(
            base_url=e.get("MINERU_API_BASE", "http://127.0.0.1:8000"),
            api_key=e.get("MINERU_API_KEY") or None,
            extra_headers=extra,
            timeout=float(e.get("MINERU_TIMEOUT", "60")),
            verify_ssl=verify,
            use_proxy=use_proxy,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- 底层 HTTP ------------------------------------------------------------
    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        payload: Any = None
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                raise MinerUAPIError(
                    resp.status_code,
                    str(err.get("type") or "api_error"),
                    str(err.get("message") or resp.text[:300]),
                    err.get("code"),
                    err.get("param"),
                    payload,
                )
            if "detail" in payload:  # FastAPI 422
                raise MinerUAPIError(
                    resp.status_code,
                    "validation_error",
                    json.dumps(payload["detail"], ensure_ascii=False)[:1200],
                    None, None, payload,
                )
        raise MinerUAPIError(
            resp.status_code, "api_error",
            (resp.text[:500] if resp.text else f"HTTP {resp.status_code}"),
            None, None, payload,
        )

    def _parse_json(self, resp: httpx.Response) -> Any:
        if resp.status_code == 204 or not resp.content:
            return {}
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            return resp.json()
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}

    async def _request(self, method: str, url: str, **kw: Any) -> Any:
        try:
            resp = await self._client.request(method, url, **kw)
        except httpx.TimeoutException as exc:
            raise MinerUAPIError(0, "timeout_error", f"请求超时: {exc}", None, None) from exc
        except httpx.ConnectError as exc:
            raise MinerUAPIError(
                0, "connection_error",
                f"无法连接 MinerU API ({self.base_url}): {exc}。请确认服务已启动、地址端口正确。",
                None, None,
            ) from exc
        except httpx.HTTPError as exc:
            raise MinerUAPIError(0, "network_error", f"网络错误: {exc}", None, None) from exc
        self._raise_for_status(resp)
        return self._parse_json(resp)

    # ---- 系统信息 ---------------------------------------------------------
    async def health(self) -> dict:
        return await self._request("GET", "/v1/health")

    async def list_models(self) -> dict:
        return await self._request("GET", "/v1/models")

    async def get_model(self, model: str) -> dict:
        return await self._request("GET", f"/v1/models/{model}")

    async def list_tiers(self) -> dict:
        return await self._request("GET", "/v1/tiers")

    async def get_usage(self) -> dict:
        return await self._request("GET", "/v1/usage")

    # ---- 上传（OpenAI 兼容三步式） ----------------------------------------
    async def create_upload(
        self,
        filename: str,
        size_bytes: int,
        mime_type: str,
        purpose: str = "parse",
        sha256sum: Optional[str] = None,
        expires_seconds: Optional[int] = None,
    ) -> dict:
        body: dict = {
            "filename": filename,
            "bytes": int(size_bytes),
            "mime_type": mime_type,
            "purpose": purpose,
        }
        if sha256sum:
            body["sha256sum"] = sha256sum
        if expires_seconds:
            body["expires_after"] = {"anchor": "created_at", "seconds": int(expires_seconds)}
        return await self._request("POST", "/v1/uploads", json=body)

    async def get_upload(self, upload_id: str) -> dict:
        return await self._request("GET", f"/v1/uploads/{upload_id}")

    async def upload_content(
        self,
        upload_id: str,
        data: bytes,
        upload_url: Optional[str] = None,
        upload_headers: Optional[dict] = None,
    ) -> Any:
        """PUT 原始字节。优先使用 create_upload 返回的 upload_url/upload_headers
        （云模式下可能是对象存储直传地址）。"""
        url = upload_url or f"/v1/uploads/{upload_id}/content"
        headers = {"Content-Type": "application/octet-stream"}
        if upload_headers:
            headers.update({str(k): str(v) for k, v in upload_headers.items()})
        return await self._request("PUT", url, content=data, headers=headers)

    async def complete_upload(self, upload_id: str, sha256sum: Optional[str] = None) -> dict:
        body = {"sha256sum": sha256sum} if sha256sum else {}
        return await self._request("POST", f"/v1/uploads/{upload_id}/complete", json=body)

    async def cancel_upload(self, upload_id: str) -> dict:
        return await self._request("POST", f"/v1/uploads/{upload_id}/cancel")

    async def upload_local_file(
        self,
        file_path: str,
        purpose: str = "parse",
        expires_seconds: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> dict:
        """完整三步上传本地文件 → 返回 {"file_id","filename","bytes","sha256","upload"}。"""
        p = Path(file_path).expanduser()
        if not p.is_file():
            raise MinerUAPIError(0, "local_file_error", f"本地文件不存在: {file_path}", "files.path")
        size = p.stat().st_size
        if size == 0:
            raise MinerUAPIError(0, "local_file_error", f"文件为空(0字节): {file_path}", "files.path")
        if max_bytes and size > max_bytes:
            raise MinerUAPIError(
                413, "file_too_large",
                f"文件 {size} 字节超过服务端上限 {max_bytes} 字节", "bytes",
            )
        data = p.read_bytes()  # 服务端上限 200MB，整读可接受
        sha = _sha256_bytes(data)
        up = await self.create_upload(
            filename=p.name, size_bytes=size, mime_type=guess_mime(p.name),
            purpose=purpose, expires_seconds=expires_seconds,
        )
        await self.upload_content(
            up["id"], data,
            upload_url=up.get("upload_url"),
            upload_headers=up.get("upload_headers"),
        )
        done = await self.complete_upload(up["id"], sha256sum=sha)
        file_obj = done.get("file") or {}
        fid = file_obj.get("id")
        if not fid:
            raise MinerUAPIError(500, "upload_incomplete",
                                 "complete 未返回 file 对象", None, None, done)
        return {
            "file_id": fid,
            "filename": file_obj.get("filename") or p.name,
            "bytes": file_obj.get("bytes") or size,
            "sha256": sha,
            "file": file_obj,
            "upload": done,
        }
    # ---- 文件 -------------------------------------------------------------
    async def list_files(
        self,
        after: Optional[str] = None,
        limit: int = 100,
        order: str = "desc",
        purpose: Optional[str] = None,
    ) -> dict:
        params: dict = {"limit": int(limit), "order": order}
        if after:
            params["after"] = after
        if purpose:
            params["purpose"] = purpose
        return await self._request("GET", "/v1/files", params=params)

    async def get_file(self, file_id: str) -> dict:
        return await self._request("GET", f"/v1/files/{file_id}")

    async def delete_file(self, file_id: str) -> dict:
        return await self._request("DELETE", f"/v1/files/{file_id}")

    async def get_file_content_bytes(self, file_id: str) -> tuple[bytes, str]:
        """下载文件原始字节，返回 (data, content_type)。"""
        try:
            resp = await self._client.get(f"/v1/files/{file_id}/content")
        except httpx.HTTPError as exc:
            raise MinerUAPIError(0, "network_error", f"下载失败: {exc}", None, None) from exc
        self._raise_for_status(resp)
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    async def download_output_file(self, file_id: str, dest_dir: str,
                                   filename: Optional[str] = None) -> dict:
        """下载文件并保存到本地目录，返回 {path, bytes, filename}。"""
        data, _ctype = await self.get_file_content_bytes(file_id)
        if not filename:
            try:
                meta = await self.get_file(file_id)
                filename = meta.get("filename") or f"{file_id}.bin"
            except MinerUAPIError:
                filename = f"{file_id}.bin"
        outdir = Path(dest_dir).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        outpath = outdir / safe_filename(filename, f"{file_id}.bin")
        outpath.write_bytes(data)
        return {"path": str(outpath), "bytes": len(data), "filename": outpath.name}

    # ---- 解析任务 -----------------------------------------------------------
    async def create_job(self, body: dict) -> dict:
        return await self._request("POST", "/v1/parse/jobs", json=body)

    async def list_jobs(
        self,
        status: Optional[str] = None,
        limit: int = 20,
        after: Optional[str] = None,
        order: str = "desc",
        created_after: Optional[str] = None,
    ) -> dict:
        params: dict = {"limit": int(limit), "order": order}
        if status:
            params["status"] = status
        if after:
            params["after"] = after
        if created_after:
            params["created_after"] = created_after
        return await self._request("GET", "/v1/parse/jobs", params=params)

    async def get_job(self, job_id: str) -> dict:
        return await self._request("GET", f"/v1/parse/jobs/{job_id}")

    async def cancel_job(self, job_id: str) -> dict:
        return await self._request("DELETE", f"/v1/parse/jobs/{job_id}")

    async def wait_job(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
        on_progress: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> dict:
        """轮询任务直到终态或超时。on_progress 每轮回调最新 job 快照。"""
        deadline = time.monotonic() + float(timeout)
        while True:
            job = await self.get_job(job_id)
            if on_progress is not None:
                try:
                    await on_progress(job)
                except Exception:
                    pass  # 进度回调失败不得影响主流程
            if job.get("status") in TERMINAL_JOB_STATUSES:
                return job
            if time.monotonic() >= deadline:
                raise MinerUAPIError(
                    0, "poll_timeout",
                    f"等待任务 {job_id} 超时（{timeout:.0f}s），当前状态={job.get('status')}。"
                    f"可稍后用 get_job 继续查询。", None, None, job,
                )
            await asyncio.sleep(max(0.5, float(poll_interval)))
