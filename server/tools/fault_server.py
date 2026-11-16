# -*- coding: utf-8 -*-
"""一个极简的故障注入服务，用来单独复现「延迟旧响应 / 哈希错误 / 坏文件」这三类问题。

它对外提供的接口和真实后端保持一致：

  GET  /v1/health
  GET  /v1/catalog?person_id=person-001
  GET  /v1/assets/{person_id}/{catalog_version}/{asset_id}
  POST /v1/recommendations

所有数据都来自 make_fault_package.py 生成的目录（默认仓库根下 outputs/fault-fixtures/good）。
manifest.json 里写死的 sha256 / size_bytes 原样返回给客户端，所以：

  - good/          → 哈希、文件都对，客户端应正常上屏
  - sha-mismatch/  → catalog 里的 sha256 和文件字节对不上，客户端应拒绝
  - corrupt-glb/   → sha256 和坏字节对得上，但解码失败，客户端应不上屏、保留旧衣

常用参数：
  --fixtures DIR   候选包目录（默认仓库根下 outputs/fault-fixtures/good）
  --port 8001      监听端口
  --delay 5        推荐接口先睡 5 秒再返回，用来复现「延迟旧响应被丢弃」(E02)
  --token abc      开启鉴权，客户端必须带 Authorization: Bearer abc
  --recommend top-001   推荐接口固定返回这件衣服（默认 top-001）；传 no_match 则返回无匹配

只用 Python 标准库。
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


class FaultHandler(BaseHTTPRequestHandler):
    server_version = "FaultServer/1.0"

    # 由 main() 注入
    fixtures_root: Path = None
    token: str | None = None
    delay: float = 0.0
    recommend: str = "top-001"

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.token}"

    def _load_manifest(self):
        return json.loads((self.fixtures_root / "manifest.json").read_text(encoding="utf-8"))

    def _catalog_payload(self):
        manifest = self._load_manifest()
        assets = []
        for a in manifest["assets"]:
            assets.append(
                {
                    "asset_id": a["asset_id"],
                    "asset_type": a["asset_type"],
                    "display_name": a["display_name"],
                    "tags": a["tags"],
                    "available": a["available"],
                    "asset_url": (
                        f"/v1/assets/{manifest['person_id']}/"
                        f"{manifest['catalog_version']}/{a['asset_id']}"
                    ),
                    "size_bytes": a["size_bytes"],
                    "sha256": a["sha256"],
                }
            )
        return {
            "person_id": manifest["person_id"],
            "catalog_version": manifest["catalog_version"],
            "pose_id": manifest["pose_id"],
            "assets": assets,
        }

    def _handle_health(self):
        self._send_json({"status": "ok"})

    def _handle_catalog(self):
        if not self._authed():
            self._send_json({"detail": "unauthorized"}, status=401)
            return
        self._send_json(self._catalog_payload())

    def _handle_asset(self, path):
        if not self._authed():
            self._send_json({"detail": "unauthorized"}, status=401)
            return
        parts = [p for p in path.split("/") if p]
        # /v1/assets/{person}/{version}/{asset_id}
        if len(parts) != 5:
            self._send_json({"detail": "not found"}, status=404)
            return
        asset_id = parts[-1]
        manifest = self._load_manifest()
        entry = next((a for a in manifest["assets"] if a["asset_id"] == asset_id), None)
        if entry is None:
            self._send_json({"detail": "not found"}, status=404)
            return
        data = (self.fixtures_root / entry["relative_file"]).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "model/gltf-binary")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_recommend(self):
        if not self._authed():
            self._send_json({"detail": "unauthorized"}, status=401)
            return
        # 延迟旧响应复现：先睡够时长再回，模拟网络慢。
        if self.delay > 0:
            time.sleep(self.delay)

        manifest = self._load_manifest()
        if self.recommend == "no_match":
            payload = {
                "request_id": "fault-request",
                "person_id": manifest["person_id"],
                "catalog_version": manifest["catalog_version"],
                "status": "no_match",
                "garment_id": None,
                "reason": "没找到合适的衣服",
                "mode": "mock",
            }
        else:
            payload = {
                "request_id": "fault-request",
                "person_id": manifest["person_id"],
                "catalog_version": manifest["catalog_version"],
                "status": "ok",
                "garment_id": self.recommend,
                "reason": "测试推荐",
                "mode": "mock",
            }
        self._send_json(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/health":
            self._handle_health()
        elif path.startswith("/v1/catalog"):
            self._handle_catalog()
        elif path.startswith("/v1/assets"):
            self._handle_asset(path)
        else:
            self._send_json({"detail": "not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/recommendations":
            self._handle_recommend()
        else:
            self._send_json({"detail": "not found"}, status=404)

    def log_message(self, format, *args):
        print(f"[fault-server] {self.address_string()} - {format % args}")


def main():
    parser = argparse.ArgumentParser(description="故障注入测试服务")
    parser.add_argument(
        "--fixtures",
        default=str(Path(__file__).resolve().parents[2] / "outputs" / "fault-fixtures" / "good"),
        help="候选包目录（默认仓库根目录下的 outputs/fault-fixtures/good）",
    )
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--delay", type=float, default=0.0, help="推荐接口延迟秒数（复现 E02）")
    parser.add_argument("--token", default=None, help="可选鉴权 token")
    parser.add_argument("--recommend", default="top-001", help="推荐接口固定返回的衣服，或 no_match")
    args = parser.parse_args()

    root = Path(args.fixtures)
    if not (root / "manifest.json").exists():
        raise SystemExit(f"找不到 {root / 'manifest.json'}，先跑 make_fault_package.py 生成候选包")

    FaultHandler.fixtures_root = root
    FaultHandler.token = args.token
    FaultHandler.delay = args.delay
    FaultHandler.recommend = args.recommend

    server = ThreadingHTTPServer(("127.0.0.1", args.port), FaultHandler)
    print(f"故障注入服务已启动：http://127.0.0.1:{args.port}")
    print(f"候选包目录：{root.resolve()}")
    print(f"推荐延迟：{args.delay}s，固定推荐：{args.recommend}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
