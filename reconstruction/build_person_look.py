"""Build a demo-stronger person-look candidate on the confirmed body (round 2).

Minimal-edits-only appearance pass on top of the face-coloured body mesh:

1. Recolour every vertex BELOW the head from the raw blue-grey base to a
   photo-derived skin tone; an upper-leg band is painted with an approximate
   dark "shorts" colour (colour-only fixed bottoms, NO new geometry).
2. Add an approximate hair cap as a SEPARATE sub-mesh (extruded crown/nape
   shell).  It is marked approximate: no photo reference for hair shape,
   open rim, simplified style.
3. Head vertex colours are preserved exactly; body vertex positions and
   faces are not modified (verified after export).

Outputs:
    <output-dir>/person-001.glb          GLB scene {body, hair-approx}
    <output-dir>/look-report.json        parameters + verification numbers
    <output-dir>/head-zoom-look.png      head closeups: base vs look

Usage:
    python reconstruction/build_person_look.py --person <face-color person glb> \
        --output-dir outputs/person-look-01 --pixie-dir <3d模型测试 dir>
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

from preview_person_asset import PANEL_HEIGHT, PANEL_WIDTH, render_view

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HAIR = "hair-approx"
BODY = "body"


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_single_mesh(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load(path, process=False)
    if isinstance(mesh, trimesh.Scene):
        meshes = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if len(meshes) != 1:
            raise RuntimeError(f"输入人物 GLB 应包含单个网格，实际 {len(meshes)} 个")
        mesh = meshes[0]
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise RuntimeError("输入人物 GLB 不是有效的三角网格")
    if not np.isfinite(mesh.vertices).all():
        raise RuntimeError("输入人物 GLB 包含非有限坐标")
    if mesh.visual.kind == "texture":
        mesh.visual = mesh.visual.to_color()
    if mesh.visual.kind != "vertex":
        raise RuntimeError(f"输入网格顶点色类型为 {mesh.visual.kind}，仅支持 vertex")
    return mesh


def head_vertex_ids(body: trimesh.Trimesh, pixie_dir: Path, fallback_below: float) -> np.ndarray:
    """FLAME head ids from utilities.zip when available; else a y-band fallback."""
    archive_path = pixie_dir / "utilities.zip"
    if archive_path.is_file():
        try:
            with zipfile.ZipFile(archive_path) as archive:
                ids = np.load(BytesIO(archive.read("SMPL-X__FLAME_vertex_ids.npy")), allow_pickle=False)
            ids = ids.reshape(-1)
            if ids.dtype == np.bool_ or ids.dtype == np.object_:
                raise ValueError("unexpected dtype")
            ids = ids.astype(np.int64)
            if len(ids) and ids.max() < len(body.vertices) and ids.min() >= 0:
                used = np.zeros(len(body.vertices), dtype=bool)
                used[ids] = True
                if np.count_nonzero(used) > 50:
                    print(f"head ids from FLAME: {int(np.count_nonzero(used))} vertices")
                    return used
        except Exception as exc:  # noqa: BLE001
            print(f"FLAME head ids unavailable ({exc}); using y-band fallback")
    crown = float(body.vertices[:, 1].max())
    mask = body.vertices[:, 1] >= crown - fallback_below
    print(f"head ids from y-band fallback: {int(np.count_nonzero(mask))} vertices")
    return mask


def recolor_body(body: trimesh.Trimesh, skin: np.ndarray, pants: np.ndarray,
                 pants_top: float, pants_hem: float, threshold: float) -> tuple[np.ndarray, dict]:
    """Recolour below-head vertices; returns (new RGBA colors, stats)."""
    colors = body.visual.vertex_colors.copy()
    y = body.vertices[:, 1]
    below = y < threshold
    in_pants = below & (y >= pants_hem) & (y <= pants_top)
    counts = {"recolored_to_skin": int(np.count_nonzero(below & ~in_pants)),
              "recolored_to_pants": int(np.count_nonzero(in_pants)),
              "kept_head_unchanged": int(len(y) - np.count_nonzero(below))}
    colors[below & ~in_pants, :3] = skin
    colors[in_pants, :3] = pants
    colors[:, 3] = 255
    return colors, counts


def build_hair(body: trimesh.Trimesh, head_mask: np.ndarray, offset: float,
               front_z: float, color: np.ndarray) -> tuple[trimesh.Trimesh, dict]:
    verts = body.vertices
    normals = body.vertex_normals
    crown = float(verts[:, 1].max())
    y = verts[:, 1]
    up = normals[:, 1]
    cap = head_mask & (y >= crown - 0.13) & (up >= 0.12) & (verts[:, 2] <= front_z)
    nape = head_mask & (y >= crown - 0.20) & (y <= crown - 0.05) & (verts[:, 2] <= -0.03) & (np.abs(verts[:, 0]) <= 0.10)
    kept = cap | nape
    count = int(np.count_nonzero(kept))
    if count < 200:
        raise RuntimeError(f"头发选区过小（{count} 顶点），请检查输入几何与参数")
    hair_verts = verts[kept] + offset * normals[kept]
    kept_index = np.flatnonzero(kept)
    row_map = np.full(len(verts), -1, dtype=np.int64)
    row_map[kept_index] = np.arange(count)
    faces = body.faces
    face_ok = kept[faces[:, 0]] & kept[faces[:, 1]] & kept[faces[:, 2]]
    hair_faces = row_map[faces[face_ok]]
    hair = trimesh.Trimesh(vertices=hair_verts, faces=hair_faces, process=False)
    hair.visual.vertex_colors = np.tile(color, (count, 1))
    stats = {
        "hair_vertices": int(len(hair.vertices)),
        "hair_triangles": int(len(hair.faces)),
        "hair_bounds_min": hair.bounds[0].tolist(),
        "hair_bounds_max": hair.bounds[1].tolist(),
        "hair_front_z_limit": front_z,
    }
    return hair, stats


def render_head_sheet(base: trimesh.Trimesh, look: trimesh.Trimesh, output: Path,
                      center: np.ndarray, scale: float) -> None:
    canvas = Image.new("RGB", (3 * PANEL_WIDTH, 2 * (PANEL_HEIGHT + 24)), "#eef2f7")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("segoeui.ttf", 18)
    for row, (mesh, tag) in enumerate(((base, "base(face-color)"), (look, "look-01"))):
        y = row * (PANEL_HEIGHT + 24)
        for column, (label, yaw) in enumerate((("Front", 0), ("35 deg", 0.61), ("Side", np.pi / 2))):
            x = column * PANEL_WIDTH
            canvas.paste(render_view(mesh, yaw, scale, center), (x, y))
            draw.text((x + PANEL_WIDTH // 2, y + PANEL_HEIGHT + 6), f"{tag} / {label}",
                      fill="#21344b", font=font, anchor="mt")
    canvas.save(output)
    print(f"Wrote {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person", type=Path, required=True, help="已确认体型 + 面部颜色的单网格 GLB")
    parser.add_argument("--output-dir", type=Path, required=True, help="新实验目录（必须不存在）")
    parser.add_argument("--pixie-dir", type=Path, default=Path(__file__).resolve().parents[2] / "3d模型测试")
    parser.add_argument("--catalog-version", default="fixture-look-01")
    parser.add_argument("--person-id", default="person-001")
    parser.add_argument("--pose-id", default="pose-001")
    parser.add_argument("--head-below", type=float, default=0.24,
                        help="冠顶以下多少米内视为头部区域并保持原色")
    parser.add_argument("--skin-fallback", default="",
                        help="肤色 RGB，如 171,133,114；缺省从面部前方顶点取中位")
    parser.add_argument("--pants-top", type=float, default=0.98)
    parser.add_argument("--pants-hem", type=float, default=0.58)
    parser.add_argument("--pants-color", default="46,58,96")
    parser.add_argument("--hair-offset", type=float, default=0.014)
    parser.add_argument("--hair-front", type=float, default=0.065)
    parser.add_argument("--hair-color", default="40,34,30")
    args = parser.parse_args()

    if not args.person.is_file():
        parser.error(f"--person 文件不存在：{args.person}")

    body = load_single_mesh(args.person)
    print(f"loaded: {len(body.vertices)} verts, {len(body.faces)} faces, "
          f"bounds_y=[{body.bounds[0, 1]:.3f}, {body.bounds[1, 1]:.3f}]")
    crown = float(body.vertices[:, 1].max())
    threshold = crown - args.head_below

    head_mask = head_vertex_ids(body, args.pixie_dir, args.head_below)
    face_front = head_mask & (body.vertices[:, 2] > 0.02)
    if np.count_nonzero(face_front) < 50:
        raise RuntimeError("面部前方顶点不足，无法推导肤色")
    skin = np.median(body.visual.vertex_colors[face_front, :3], axis=0).astype(np.uint8)
    if args.skin_fallback:
        skin = np.array([int(v) for v in args.skin_fallback.split(",")], dtype=np.uint8)
    pants = np.array([int(v) for v in args.pants_color.split(",")], dtype=np.uint8)
    hair_clr = np.array([int(v) for v in args.hair_color.split(",")] + [255], dtype=np.uint8)
    print(f"derived skin rgb={skin.tolist()} pants rgb={pants.tolist()} hair rgb={hair_clr[:3].tolist()}")

    colors, recolor_stats = recolor_body(
        body, skin, pants, args.pants_top, args.pants_hem, threshold)
    # The preserved zone is the head region ABOVE the recolor threshold (face,
    # scalp).   Vertices at/under the threshold (neck, shoulders) are recolored
    # to skin on purpose — they carry the blue-grey mix from the face pass.
    preserved = body.vertices[:, 1] >= threshold
    preserved_idx = np.flatnonzero(preserved)
    preserved_orig = body.visual.vertex_colors[preserved_idx]
    changed_head = int(np.count_nonzero(np.any(preserved_orig[:, :3] != colors[preserved_idx, :3], axis=1)))
    if changed_head:
        raise RuntimeError(f"保留区域 {changed_head} 顶点颜色被意外修改")

    body_mesh = body.copy()
    body_mesh.visual.vertex_colors = colors
    hair, hair_stats = build_hair(body, head_mask, args.hair_offset, args.hair_front, hair_clr)

    args.output_dir.mkdir(parents=True, exist_ok=False)  # never overwrite
    scene = trimesh.Scene({BODY: body_mesh, HAIR: hair})
    target = args.output_dir / "person-001.glb"
    scene.export(target)

    # ---- post-condition verification (fresh read) ----
    reloaded = trimesh.load(target, process=False)
    if not isinstance(reloaded, trimesh.Scene):
        raise RuntimeError("导出文件不是 Scene（缺少子网格结构）")
    geoms = {name: g for name, g in reloaded.geometry.items()}
    if BODY not in geoms or HAIR not in geoms:
        raise RuntimeError(f"导出缺少子网格：{list(geoms.keys())}")
    rb = geoms[BODY]
    if not np.array_equal(rb.vertices, body.vertices) or not np.array_equal(rb.faces, body.faces):
        raise RuntimeError("身体几何在导出后发生变化")
    rh = geoms[HAIR]
    if not np.isfinite(rh.vertices).all() or len(rh.faces) == 0:
        raise RuntimeError("头发几何无效")
    if np.count_nonzero(np.any(preserved_orig[:, :3] != rb.visual.vertex_colors[preserved_idx, :3], axis=1)):
        raise RuntimeError("回读后保留区域顶点色与输入不一致")
    if float(rh.bounds[1, 2]) > args.hair_front + 0.02:
        raise RuntimeError(f"头发前缘越过正面限制：z_max={rh.bounds[1, 2]:.3f}")
    if float(rh.bounds[0, 1]) < crown - 0.24:
        raise RuntimeError(f"头发下缘过低：y_min={rh.bounds[0, 1]:.3f}")

    report = {
        "catalog_version": args.catalog_version,
        "input_person": {"path": str(args.person), "sha256": sha256(args.person)},
        "output": {"path": str(target), "sha256": sha256(target), "bytes": target.stat().st_size},
        "method": [
            "recolor below-head blue-grey base to photo-derived skin",
            "colour-only approximate shorts band (no geometry)",
            "approximate hair cap sub-mesh (crown+nape extrude; open rim; no photo reference)",
        ],
        "parameters": {
            "head_below_m": args.head_below, "threshold_y": round(threshold, 4),
            "skin_rgb": skin.tolist(), "pants_rgb": pants.tolist(),
            "pants_top_m": args.pants_top, "pants_hem_m": args.pants_hem,
            "hair_offset_m": args.hair_offset, "hair_front_z_m": args.hair_front,
            "hair_rgb": hair_clr[:3].tolist(),
        },
        "recolor": recolor_stats,
        "head_preserved": {"vertices_checked": int(len(preserved_idx)), "changed": changed_head},
        "hair": hair_stats,
        "body_geometry_unchanged": True,
        "garments_untouched": True,
        "bounds_full_scene": [scene.bounds[0].tolist(), scene.bounds[1].tolist()],
        "approximations": [
            "头发无照片参照，为人工近似外壳（冠顶+后脑），边缘开放",
            "短裤为颜色近似（无几何），领口/裤腰衔接待人工检查",
            "眼镜仍为表面贴色，未做精细几何",
        ],
        "not_claimed": [
            "未声称发型/侧后外观与本人一致",
            "未进行真实衣物适配",
        ],
    }
    (args.output_dir / "look-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # head closeups for manual review
    head_center = body.vertices[preserved_idx].mean(axis=0)
    render_head_sheet(body, body_mesh, args.output_dir / "head-zoom-look.png",
                       head_center, scale=2600)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())