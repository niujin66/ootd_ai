"""Render a single candidate GLB asset from front / side / back (task 2).

Usage:
    python preview_person_asset.py CANDIDATE_GLB [--output preview.png]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import trimesh

PANEL_WIDTH = 396
PANEL_HEIGHT = 460
LIGHT = np.array([-0.38, 0.67, 0.64])
CLAY = np.array([125, 168, 214])


def render_view(
    mesh: trimesh.Trimesh, yaw: float, scale: float | None = None, center: np.ndarray | None = None
) -> Image.Image:
    cosine, sine = np.cos(yaw), np.sin(yaw)
    rotation = np.array([[cosine, 0, -sine], [0, 1, 0], [sine, 0, cosine]])
    points = mesh.vertices @ rotation
    minimum, maximum = points.min(axis=0), points.max(axis=0)
    extent = maximum - minimum
    if scale is None:
        scale = min(360 / extent[0], 410 / extent[1])
    screen_center = (minimum + maximum)[:2] / 2 if center is None else (center @ rotation)[:2]
    pixels = ((points[:, :2] - screen_center) * [scale, -scale] + [198, 222]).round().astype(np.int32)
    brightness = np.clip((mesh.face_normals @ rotation) @ LIGHT, 0, 1) * 0.62 + 0.36
    visual = mesh.visual.to_color() if mesh.visual.kind == "texture" else mesh.visual
    base_colors = visual.face_colors[:, :3] if visual.defined else CLAY
    colors = (base_colors * brightness[:, None]).astype(np.uint8).tolist()
    panel = np.full((PANEL_HEIGHT, PANEL_WIDTH, 3), [247, 249, 253], dtype=np.uint8)
    depth_order = np.argsort(points[mesh.faces, 2].mean(axis=1))
    for index in depth_order:
        cv2.fillConvexPoly(panel, pixels[mesh.faces[index]], colors[index], lineType=cv2.LINE_AA)
    return Image.fromarray(panel)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("glb", type=Path)
    parser.add_argument("--compare", type=Path, help="Second mesh, rendered below at the same scale")
    parser.add_argument("--output", type=Path, default=Path("preview.png"))
    args = parser.parse_args()

    if not args.glb.is_file():
        print(f"错误：文件不存在：{args.glb}", file=sys.stderr)
        return 2
    paths = [args.glb] if args.compare is None else [args.glb, args.compare]
    meshes = [trimesh.load(path, force="mesh", process=False) for path in paths]
    if any(len(mesh.faces) == 0 or not np.isfinite(mesh.vertices).all() for mesh in meshes):
        print("错误：输入网格为空或包含无效坐标", file=sys.stderr)
        return 1

    views = [
        ("Front (+Z)", 0),
        ("Side", np.pi / 2),
        ("Back (-Z)", np.pi),
    ]
    row_height = 515
    canvas = Image.new("RGB", (1200, row_height * len(meshes)), "#eef2f7")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("segoeui.ttf", 20)
    scale = min(
        min(360 / max(mesh.extents[0], mesh.extents[2]), 410 / mesh.extents[1])
        for mesh in meshes
    )
    for row, mesh in enumerate(meshes):
        y = row * row_height
        prefix = ("A / " if row == 0 else "B / ") if len(meshes) > 1 else ""
        for index, (label, yaw) in enumerate(views):
            canvas.paste(render_view(mesh, yaw, scale), (2 + 400 * index, y + 8))
            draw.text((200 + 400 * index, y + 480), prefix + label, fill="#21344b", font=font, anchor="mt")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
