# -*- coding: utf-8 -*-
"""生成用于故障复现的「匿名候选包」。

每次生成三个目录，各自有不同的 catalog_version，方便客户端把它们当成
「一次新发布」来走完整的下载/校验/解码流程，而不是命中旧缓存：

  good/        正常包：manifest 里的 sha256 和文件字节对得上
  sha-mismatch/  E03：文件字节是对的，但 top-001 的 sha256 在 manifest 里被故意写错
  corrupt-glb/   E04：manifest 里的 sha256 和文件字节对得上，但 top-001 的字节不是合法 GLB，
                 客户端解码必失败

默认会自动找仓库里的真实候选包 local-data/handoff/T02-candidate-20261113 当素材，
生成的是能真正解码的 GLB；找不到时退回占位字节（只能验证哈希校验/解码失败两条路径）。
也可以用 --source 指定别的候选包目录。

只用 Python 标准库，任何环境都能直接运行。
"""
import argparse
import hashlib
import json
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parents[1]
DEFAULT_SOURCE = REPO_DIR / "local-data" / "handoff" / "T02-candidate-20261113"

# 占位字节：明显不是 GLB，只在没有真实候选包时兜底。
PERSON_FALLBACK = b"NOT-A-GLB-person-placeholder"
TOP001_FALLBACK = b"NOT-A-GLB-top-001-placeholder"
TOP002_FALLBACK = b"NOT-A-GLB-top-002-placeholder"
CORRUPT_BYTES = b"CORRUPT-GLB-BYTES-FOR-DECODE-FAIL"

PERSON_ID = "person-001"
POSE_ID = "pose-001"

# 真实候选包里三个资产可能的文件名（新名字优先）。
REAL_NAMES = {
    "person-001": ["person-001.glb"],
    "top-001": ["top-001_v2.glb", "top-001.glb"],
    "top-002": ["top-002_v1.glb", "top-002.glb"],
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_source(source_dir):
    """读真实候选包里的三个 GLB；返回 {asset_id: bytes}，缺的就不放。"""
    found = {}
    for asset_id, names in REAL_NAMES.items():
        for name in names:
            path = Path(source_dir) / name
            if path.is_file():
                found[asset_id] = path.read_bytes()
                break
    return found


def load_source_meta(source_dir):
    """读真实候选包 manifest 里的展示名和标签，让故障包和真包更像。"""
    path = Path(source_dir) / "manifest.json"
    if not path.is_file():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {item.get("asset_id"): item for item in manifest.get("assets", [])}


def asset_entry(asset_id, asset_type, relative_file, display_name, tags, available, data):
    return {
        "asset_id": asset_id,
        "asset_type": asset_type,
        "relative_file": relative_file,
        "display_name": display_name,
        "tags": tags,
        "available": available,
        "size_bytes": len(data),
        "sha256": sha256_hex(data),
    }


def build(root: Path, name: str, catalog_version: str, person: bytes, top001: bytes,
          top002: bytes, meta: dict, top001_sha_override: str | None = None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "person-001.glb").write_bytes(person)
    (directory / "top-001.glb").write_bytes(top001)
    (directory / "top-002.glb").write_bytes(top002)

    top1_meta = meta.get("top-001", {})
    top2_meta = meta.get("top-002", {})
    assets = [
        asset_entry("person-001", "person", "person-001.glb", "测试人物", [], False, person),
        asset_entry(
            "top-001", "garment", "top-001.glb",
            top1_meta.get("display_name") or "浅色印花短袖",
            top1_meta.get("tags") or ["white", "casual"],
            True, top001,
        ),
        asset_entry(
            "top-002", "garment", "top-002.glb",
            top2_meta.get("display_name") or "深蓝翻领 POLO",
            top2_meta.get("tags") or ["dark", "casual"],
            True, top002,
        ),
    ]
    if top001_sha_override:
        assets[1]["sha256"] = top001_sha_override

    manifest = {
        "person_id": PERSON_ID,
        "catalog_version": catalog_version,
        "pose_id": POSE_ID,
        "assets": assets,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description="生成故障复现用的匿名候选包")
    parser.add_argument(
        "--out",
        default=str(REPO_DIR / "outputs" / "fault-fixtures"),
        help="输出根目录（默认仓库根目录下的 outputs/fault-fixtures）",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="真实候选包目录；默认自动找 local-data/handoff/T02-candidate-20261113",
    )
    args = parser.parse_args()

    source = args.source
    if source is None and DEFAULT_SOURCE.is_dir():
        source = str(DEFAULT_SOURCE)

    source_bytes = load_source(source) if source else {}
    meta = load_source_meta(source) if source else {}
    person = source_bytes.get("person-001", PERSON_FALLBACK)
    top001 = source_bytes.get("top-001", TOP001_FALLBACK)
    top002 = source_bytes.get("top-002", TOP002_FALLBACK)

    base = "fixture-face-03" if source_bytes else "v1"
    root = Path(args.out)
    build(root, "good", base, person, top001, top002, meta)
    build(root, "sha-mismatch", base + "-sha-bad", person, top001, top002, meta,
          top001_sha_override="0" * 64)
    build(root, "corrupt-glb", base + "-corrupt", person, CORRUPT_BYTES, top002, meta)

    if source_bytes:
        print(f"素材来自真实候选包：{Path(source).resolve()}")
    else:
        print("没找到真实候选包，使用占位字节（只能验证哈希/解码失败路径）")
    print(f"已生成故障候选包：{root.resolve()}")
    print(f"  - good/          catalog_version={base}")
    print(f"  - sha-mismatch/  catalog_version={base}-sha-bad（top-001 哈希写错）")
    print(f"  - corrupt-glb/   catalog_version={base}-corrupt（top-001 哈希能过、解码会挂）")


if __name__ == "__main__":
    main()
