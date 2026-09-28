#!/usr/bin/env python3
"""coverage_test.py — 覆盖 self-test 未触及的 5 个工具，确保 19/19 全测过。
   mineru_upload_file / mineru_get_upload / mineru_cancel_upload /
   mineru_get_model / mineru_cancel_parse_job

用法：
   python coverage_test.py                                  # 默认 http://127.0.0.1:8000
   python coverage_test.py --api-base http://10.0.0.5:8000  # 远端算力服务器
   MINERU_API_BASE=http://x:8000 python coverage_test.py    # 环境变量方式
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import MinerUMCPClient

HERE = Path(__file__).resolve().parent

_ap = argparse.ArgumentParser(description="MinerU MCP 补充覆盖测试")
_ap.add_argument("--api-base", default=os.environ.get("MINERU_API_BASE", "http://127.0.0.1:8000"),
                 help="MinerU API 地址（默认取 MINERU_API_BASE，再默认 127.0.0.1:8000）")
_ap.add_argument("--pdf", default=str(HERE / "dev" / "tiny.pdf"),
                 help="测试用本地 PDF 路径")
_ap.add_argument("--output-dir", default=str(HERE / "mineru_outputs"),
                 help="产物保存目录")
_ARGS, _ = _ap.parse_known_args()

PDF = _ARGS.pdf
API = _ARGS.api_base
OUTPUT_DIR = _ARGS.output_dir

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  [PASS] {name}")
    else:
        failed += 1; print(f"  [FAIL] {name}  {detail}")

async def main():
    if not Path(PDF).is_file():
        print(f"[ERROR] 测试 PDF 不存在: {PDF}（用 --pdf 指定）")
        return 2
    print(f"目标 API: {API}\n")
    async with MinerUMCPClient(api_base=API, output_dir=OUTPUT_DIR) as cli:
        print("== 11. mineru_get_model (取列表首个模型) ==")
        models = (await cli.call_tool("mineru_list_models"))["data"]["data"]
        mid = models[0]["id"]
        gm = await cli.call_tool("mineru_get_model", {"model": mid})
        check(f"get_model({mid}).ok", gm.get("ok") is True and gm["data"]["id"] == mid, str(gm)[:150])
        gm404 = await cli.call_tool("mineru_get_model", {"model": "no-such-model"})
        check("get_model 404 -> ok=false", gm404.get("ok") is False, str(gm404)[:150])

        print("\n== 12. mineru_upload_file (三步上传) ==")
        up = await cli.call_tool("mineru_upload_file", {"file_path": PDF, "purpose": "parse"})
        ok_up = up.get("ok") is True and up["data"].get("file_id", "").startswith("file-")
        check("upload_file.ok + file_id", ok_up, str(up)[:200])
        fid = up["data"]["file_id"] if ok_up else None
        if fid:
            print(f"   file_id={fid} sha256={up['data']['sha256'][:16]}... bytes={up['data']['bytes']}")

        print("\n== 13. 上传后用 file_id 源解析 ==")
        if fid:
            pr = await cli.parse_document({"type": "file_id", "file_id": fid},
                                          tier="flash", wait=True, timeout=120,
                                          download=False, return_content=True)
            check("parse via file_id completed",
                  pr.get("ok") is True and pr["data"]["status"] == "completed", str(pr)[:200])

        print("\n== 14. mineru_upload_file 边界：不存在的本地文件 ==")
        bad = await cli.call_tool("mineru_upload_file", {"file_path": "/no/such/file.pdf"})
        check("upload 不存在文件 -> ok=false", bad.get("ok") is False, str(bad)[:150])

        print("\n== 15. mineru_cancel_parse_job (提交后立即取消 running/queued) ==")
        # 先占满并发：提交一个 standard 任务（较慢），再提交第二个并尝试取消
        import base64
        b64 = base64.b64encode(Path(PDF).read_bytes()).decode()
        cj = await cli.call_tool("mineru_create_parse_job", {
            "sources": [{"source": {"type": "inline", "name": "t.pdf", "data": b64}}],
            "tier": "flash", "output_formats": ["markdown"],
        })
        jid = cj["data"]["job_id"]
        # 立即取消（flash 很快，可能已 completed -> 409；也可能 queued -> canceled）
        cc = await cli.call_tool("mineru_cancel_parse_job", {"job_id": jid})
        st = cc.get("data", {}).get("status") if cc.get("ok") else None
        # 两种都算正确行为：取消成功 或 已终态409
        acceptable = (cc.get("ok") is True and st == "canceled") or \
                     (cc.get("ok") is False and cc.get("error", {}).get("status_code") == 409)
        check("cancel_parse_job 行为正确(canceled或409)", acceptable, str(cc)[:200])

        print("\n== 16. mineru_get_upload / cancel_upload 边界 ==")
        # 直接走 client 层创建 upload 以拿到 upload_id
        from mineru_client import MinerUClient, guess_mime
        c = MinerUClient(base_url=API)
        raw = Path(PDF).read_bytes()
        u = await c.create_upload("tiny.pdf", len(raw), guess_mime("tiny.pdf"))
        uid = u["id"]
        gu = await cli.call_tool("mineru_get_upload", {"upload_id": uid})
        check("get_upload.ok status=pending",
              gu.get("ok") is True and gu["data"]["status"] == "pending", str(gu)[:150])
        cu = await cli.call_tool("mineru_cancel_upload", {"upload_id": uid})
        check("cancel_upload.ok status=cancelled",
              cu.get("ok") is True and cu["data"]["status"] == "cancelled", str(cu)[:150])
        gu404 = await cli.call_tool("mineru_get_upload", {"upload_id": "upload_nope"})
        check("get_upload 404 -> ok=false", gu404.get("ok") is False, str(gu404)[:150])
        await c.aclose()

        # 清理上传产生的文件
        if fid:
            await cli.call_tool("mineru_delete_file", {"file_id": fid})

    print(f"\n========== 覆盖测试结果: {passed} passed, {failed} failed ==========")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
