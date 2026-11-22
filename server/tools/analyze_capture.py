"""CLI tool: extract capture metrics from frames/video/COLMAP and invoke /v1/capture/guide.

Examples (run from repo root):
    # 1) Inspect a frame folder + call local /v1/capture/guide (mock or ai via OOTD_AI_MODE):
    ./server/.venv/Scripts/python.exe server/tools/analyze_capture.py \
        --input <frames_dir> --target-type garment --subject-label sample-01 --call-guide

    # 2) Inspect frames + COLMAP sparse/0 + call a running backend server:
    ./server/.venv/Scripts/python.exe server/tools/analyze_capture.py \
        --input <frames_dir> --sparse <sparse/0> --target-type head \
        --server-url http://127.0.0.1:8000 --call-guide
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from capture_analyzer import analyze_capture_source


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从视频/帧目录/COLMAP 提取三维采集质检指标并生成补拍指导")
    parser.add_argument("--input", default=None, help="图像目录或视频文件路径")
    parser.add_argument("--sparse", default=None, help="COLMAP sparse/0 目录（含 images.bin 或 images.txt）")
    parser.add_argument("--masks-dir", default=None, help="可选前景掩膜目录")
    parser.add_argument("--target-type", required=True, choices=("head", "garment"), help="采集对象类型")
    parser.add_argument("--subject-label", default="", help="样本标签")
    parser.add_argument("--request-id", default=f"cap-cli-{int(time.time())}", help="请求编号")
    parser.add_argument("--max-frames", type=int, default=48, help="最大等间隔采样帧数")
    parser.add_argument("--ffmpeg-bin", default=None, help="可选 ffmpeg 可执行文件路径")
    parser.add_argument(
        "--person-seg",
        choices=("auto", "torch", "none"),
        default="auto",
        help="head 模式下是否自动调用 DeepLabV3 人像分割（auto=有环境则用，none=纯 NumPy）",
    )
    parser.add_argument("--roi-detail-px", type=float, default=None, help="可选关键局部像素（如单眼像素）")
    parser.add_argument(
        "--has-scale-reference",
        choices=("true", "false"),
        default=None,
        help="是否包含尺度参考（如 ArUco/卷尺）",
    )
    parser.add_argument("--missing-closeups", nargs="*", default=[], help="已知缺少的近景部位名称")
    parser.add_argument("--call-guide", action="store_true", help="提取后继续调用 /v1/capture/guide 输出补拍方案")
    parser.add_argument("--server-url", default=None, help="远端服务端地址；省略时在本地进程内调用 FastAPI")
    parser.add_argument("--token", default=None, help="可选 Bearer 鉴权令牌")
    parser.add_argument("--json-out", default=None, help="可选结果 JSON 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.input is None and args.sparse is None:
        print("错误：至少需要提供 --input 或 --sparse 之一", file=sys.stderr)
        return 2

    scale_ref = None
    if args.has_scale_reference == "true":
        scale_ref = True
    elif args.has_scale_reference == "false":
        scale_ref = False

    payload = analyze_capture_source(
        request_id=args.request_id,
        target_type=args.target_type,
        subject_label=args.subject_label,
        input_path=Path(args.input) if args.input else None,
        sparse_dir=Path(args.sparse) if args.sparse else None,
        masks_dir=Path(args.masks_dir) if args.masks_dir else None,
        max_frames=args.max_frames,
        ffmpeg_bin=args.ffmpeg_bin,
        person_seg=args.person_seg,
        roi_detail_px=args.roi_detail_px,
        has_scale_reference=scale_ref,
        missing_closeups=args.missing_closeups,
    )

    result: dict = {"extracted_payload": payload}

    if args.call_guide:
        if args.server_url:
            headers = {"Content-Type": "application/json"}
            token = args.token or os.environ.get("OOTD_AUTH_TOKEN", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = httpx.post(
                f"{args.server_url.rstrip('/')}/v1/capture/guide",
                headers=headers,
                json=payload,
                timeout=35.0,
            )
            result["guide_status"] = resp.status_code
            result["guide_response"] = resp.json()
        else:
            from fastapi.testclient import TestClient
            from app import create_app

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                app = create_app(db_path=tmp_path / "cli.db", assets_root=tmp_path / "assets")
                client = TestClient(app)
                resp = client.post("/v1/capture/guide", json=payload)
                result["guide_status"] = resp.status_code
                result["guide_response"] = resp.json()

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")

    if args.call_guide and result.get("guide_status") != 200:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
