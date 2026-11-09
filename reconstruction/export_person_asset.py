"""Repeatable export of a validated person mesh into the shared asset frame.

Task 2: freeze a fixed T-pose into a candidate person asset following the
project conventions (docs/architecture.md §4):

- metric, Y-up, right-handed
- person faces +Z      (NOT validated by this script: needs a human check)
- foot-center at the common origin (X=0, Z=0), Y floor at 0

This script only re-aligns one input mesh, computes the transform from the
actual measured vertices and records it in transform.json.  It never modifies
the input file.  Output is labelled a *联调资产* until person similarity is
confirmed by the participant; no independent normalization of each asset.

Usage:
    python export_person_asset.py --input person-tpose.glb --output-dir DIR \
        --person person-001 --pose pose-001 --name person-001
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import trimesh

# GBK consoles cannot print some CJK/emoji; never crash on console encoding.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_mesh(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(path, process=False)
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError(f"{path} 不是单网格 GLB")
    if len(mesh.vertices) == 0:
        raise RuntimeError("网格无顶点")
    if not np.isfinite(mesh.vertices).all():
        raise RuntimeError("网格包含非有限坐标")
    return mesh


def measure(mesh: trimesh.Trimesh) -> dict:
    vertices = mesh.vertices
    y_min = float(vertices[:, 1].min())
    foot = vertices[vertices[:, 1] < y_min + 0.03]
    return {
        "vertices": int(len(vertices)),
        "triangles": int(len(mesh.faces)),
        "height_m": round(float(vertices[:, 1].max() - vertices[:, 1].min()), 6),
        "y_floor": round(y_min, 6),
        "foot_center_xz": [round(float(foot[:, 0].mean()), 6), round(float(foot[:, 2].mean()), 6)],
        "bounds": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
    }


def align(mesh: trimesh.Trimesh, foot_center_xz: list[float]) -> np.ndarray:
    """Translate so the foot center lands on (0, 0) and the floor on Y=0.

    Returns the applied translation as [dx, dy, dz].
    """
    vertices = mesh.vertices
    y_min = float(vertices[:, 1].min())
    dx = -foot_center_xz[0]
    dy = -y_min
    dz = -foot_center_xz[1]
    mesh.vertices = vertices + np.array([dx, dy, dz])
    return [round(dx, 6), round(dy, 6), round(dz, 6)]


def scale_to_height(mesh: trimesh.Trimesh, target_height_m: float) -> float:
    """Uniformly scale the whole mesh so its Y span equals target_height_m.

    The same uniform factor applies to the person and, later, to garments,
    so relative fit is preserved (no per-asset normalization).
    """
    current = float(mesh.vertices[:, 1].max() - mesh.vertices[:, 1].min())
    factor = target_height_m / current
    mesh.vertices = mesh.vertices * factor
    return round(factor, 6)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="输入 T 姿势 GLB（如 person-tpose.glb）")
    parser.add_argument("--output-dir", required=True, type=Path, help="候选资产输出目录")
    parser.add_argument("--person", default="person-001")
    parser.add_argument("--pose", default="pose-001")
    parser.add_argument("--name", default="person-001", help="输出 GLB 文件名（不含扩展名）")
    parser.add_argument(
        "--height-m",
        type=float,
        default=None,
        help="真实身高（米）；提供则先做统一等比缩放再对齐（记录缩放系数）",
    )
    args = parser.parse_args()

    if args.height_m is not None and (not math.isfinite(args.height_m) or args.height_m <= 0):
        parser.error("--height-m must be positive and finite")
    if not args.input.is_file():
        print(f"错误：输入文件不存在：{args.input}", file=sys.stderr)
        return 2

    mesh = load_mesh(args.input)
    before = measure(mesh)
    scale_factor = None
    if args.height_m is not None:
        scale_factor = scale_to_height(mesh, args.height_m)
    translation = align(mesh, measure(mesh)["foot_center_xz"])
    after = measure(mesh)

    target = args.output_dir / f"{args.name}.glb"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    mesh.export(target)

    # verify post-conditions on the written file
    written = load_mesh(Path(target))
    written_measure = measure(written)
    if abs(written_measure["foot_center_xz"][0]) > 1e-4 or abs(written_measure["foot_center_xz"][1]) > 1e-4:
        print("错误：导出后脚底中心未对齐原点", file=sys.stderr)
        return 1
    if abs(written_measure["y_floor"]) > 1e-4:
        print("错误：导出后 Y 地面未归零", file=sys.stderr)
        return 1
    if args.height_m is not None and abs(written_measure["height_m"] - args.height_m) > 1e-3:
        print("错误：导出后身高与目标不符", file=sys.stderr)
        return 1

    transform_parts = [{"type": "translation", "translation_xyz": translation, "note": "脚底中心归零"}]
    if scale_factor is not None:
        transform_parts.insert(0, {"type": "scale_uniform", "factor": scale_factor, "note": "按输入身高等比缩放"})
    record = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "person_id": args.person,
        "pose_id": args.pose,
        "conventions": {
            "unit": "meters",
            "y_up": True,
            "front_facing": "+Z（沿用输入方向，需通过预览核验）",
            "origin": "脚底中心 (0, 0)，Y 地面 0",
        },
        "input": {
            "file": str(args.input),
            "sha256": sha256(args.input),
        },
        "height_reference": {
            "real_height_m": args.height_m,
            "source": "CLI --height-m" if args.height_m is not None else None,
        },
        "transform": transform_parts,
        "measurements_before": before,
        "measurements_after": after,
        "labels": {
            "appearance": "保留输入网格的外观",
            "similarity_status": "候选资产，需由本人确认相似度",
            "height_status": "已按输入身高校准" if scale_factor is not None else "保留输入身高",
        },
        "output": {
            "file": f"{args.name}.glb",
            "path": str(target),
            "sha256": sha256(target),
        },
    }
    (args.output_dir / "transform.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
