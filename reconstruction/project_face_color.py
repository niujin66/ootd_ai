"""Transfer color from a local face photo onto an aligned PIXIE body mesh."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import zipfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps
import torch
import trimesh

from preview_person_asset import PANEL_HEIGHT, PANEL_WIDTH, render_view


def find_face(image: Image.Image) -> tuple[float, float, float, float]:
    reduced = image.copy()
    reduced.thumbnail((960, 960))
    gray = cv2.cvtColor(np.asarray(reduced), cv2.COLOR_RGB2GRAY)
    path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_alt2.xml"
    data = cv2.FileStorage(path.read_text(encoding="utf-8"), cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
    detector = cv2.CascadeClassifier()
    if not detector.read(data.getFirstTopLevelNode()):
        raise RuntimeError("Cannot load OpenCV's face detector")
    faces = detector.detectMultiScale(gray, 1.08, 5, minSize=(60, 60))
    if len(faces) != 1:
        raise ValueError("Expected one detected face; use --bbox to select it manually")
    x, y, width, height = faces[0]
    sx, sy = image.width / reduced.width, image.height / reduced.height
    return (x * sx, y * sy, (x + width) * sx, (y + height) * sy)


def crop_for_pixie(image: Image.Image, box: tuple[float, ...]) -> Image.Image:
    left, top, right, bottom = box
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        raise ValueError("Face box must be within the EXIF-oriented photo")
    half = max(right - left, bottom - top) * 0.75
    cx, cy = (left + right) / 2, (top + bottom) / 2
    return image.transform(
        (640, 640), Image.Transform.EXTENT, (cx - half, cy - half, cx + half, cy + half),
        resample=Image.Resampling.BICUBIC, fillcolor=(144, 144, 144),
    )


def project(
    body: trimesh.Trimesh, crop: Image.Image, pixie_dir: Path, device: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    os.environ["OOTD_PIXIE_ROOT"] = str(pixie_dir.resolve())
    from run_pixie_mesh import PIXIE, prepare_config

    model = PIXIE(config=prepare_config(), device=device)
    tensor = torch.from_numpy(np.array(crop.resize((224, 224), Image.Resampling.BILINEAR)))
    tensor = tensor.permute(2, 0, 1)[None].to(device, torch.float32) / 255
    with torch.inference_mode():
        params = model.encode({"head": {"image": tensor}})["head"]
        pred = model.decode(params, "head")
    faces = model.smplx.faces_tensor.cpu().numpy()
    vertices = pred["vertices"][0].cpu().numpy()
    if len(body.vertices) != len(vertices) or not np.array_equal(body.faces, faces):
        raise ValueError("Body GLB must preserve the PIXIE SMPL-X vertex and face order")
    if not all(torch.isfinite(pred[key]).all() for key in ("vertices", "transformed_vertices", "face_kpt")):
        raise ValueError("PIXIE returned invalid face geometry")
    projected = (pred["transformed_vertices"][0, :, :2].cpu().numpy() + 1) * (crop.width / 2) - 0.5
    landmarks = (pred["face_kpt"][0].cpu().numpy() + 1) * (crop.width / 2) - 0.5
    posed = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    with zipfile.ZipFile(pixie_dir / "utilities.zip") as archive:
        head_ids = np.load(BytesIO(archive.read("SMPL-X__FLAME_vertex_ids.npy")), allow_pickle=False).reshape(-1)
    return projected, landmarks, -posed.vertex_normals[:, 2], head_ids


def face_colors(
    body: trimesh.Trimesh, crop: Image.Image, pixels: np.ndarray, landmarks: np.ndarray,
    facing: np.ndarray, head_ids: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    # Use the projected jaw and brows to limit color sampling to the visible face.
    jaw, brows = landmarks[:17], landmarks[17:27]
    forehead = brows.copy()
    forehead[:, 1] -= 0.30 * (jaw[8, 1] - np.median(brows[:, 1]))
    polygon = cv2.convexHull(np.rint(np.vstack([jaw, forehead])).astype(np.int32))
    mask = np.zeros((crop.height, crop.width), dtype=np.uint8)
    cv2.fillConvexPoly(mask, polygon, 255)
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    x, y = (axis.astype(np.float32)[:, None] for axis in pixels.T)
    rgb = cv2.remap(np.asarray(crop), x, y, cv2.INTER_LINEAR)[:, 0]
    weight = cv2.remap(distance, x, y, cv2.INTER_LINEAR)[:, 0]
    weight = np.clip(weight / max(1, 0.065 * np.ptp(jaw[:, 0])), 0, 1)
    weight *= np.clip((facing - 0.12) / 0.55, 0, 1)
    head_mask = np.zeros(len(body.vertices), dtype=bool)
    head_mask[head_ids] = True
    weight *= head_mask
    strong_samples = weight > 0.75
    if np.count_nonzero(strong_samples) < 100:
        raise ValueError("Face projection did not cover enough visible vertices")

    skin = np.median(rgb[strong_samples], axis=0)
    height_below_crown = body.bounds[1, 1] - body.vertices[:, 1]
    head_blend = np.clip((0.27 - height_below_crown) / 0.075, 0, 1)
    base = body.visual.vertex_colors[:, :3]
    base = base * (1 - head_blend[:, None]) + skin * head_blend[:, None]
    colors = base * (1 - weight[:, None]) + rgb * weight[:, None]
    return np.column_stack((np.rint(colors).astype(np.uint8), np.full(len(base), 255, np.uint8))), weight


def preview_face(body: trimesh.Trimesh, colored: trimesh.Trimesh, output: Path) -> None:
    chin_to_neck = body.bounds[1, 1] - 0.26
    head_faces = np.flatnonzero((body.vertices[body.faces, 1] >= chin_to_neck).all(axis=1))
    center = body.vertices[np.unique(body.faces[head_faces])].mean(axis=0)
    canvas = Image.new("RGB", (3 * PANEL_WIDTH, 2 * (PANEL_HEIGHT + 24)), "#eef2f7")
    draw = ImageDraw.Draw(canvas)
    for row, mesh in enumerate((body, colored)):
        head = mesh.submesh([head_faces], append=True)
        y = row * (PANEL_HEIGHT + 24)
        for column, (angle, label) in enumerate(((0, "Front"), (35, "35 degrees"), (85, "Side"))):
            x = column * PANEL_WIDTH
            canvas.paste(render_view(head, np.deg2rad(angle), 1150, center), (x, y))
            draw.text((x + PANEL_WIDTH // 2, y + PANEL_HEIGHT + 6),
                      f"{'B geometry' if row == 0 else 'B with face color'} / {label}",
                      fill="#21344b", anchor="mt")
    canvas.save(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pixie-dir", type=Path, default=Path(__file__).resolve().parents[2] / "3d模型测试")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--bbox", nargs=4, type=float)
    parser.add_argument("--debug-overlay", action="store_true")
    args = parser.parse_args()

    body = trimesh.load_mesh(args.person, process=False)
    if not isinstance(body, trimesh.Trimesh) or not np.isfinite(body.vertices).all():
        parser.error("--person must contain one valid PIXIE mesh")
    with Image.open(args.image) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
    crop = crop_for_pixie(image, args.bbox or find_face(image))
    pixels, landmarks, facing, head_ids = project(body, crop, args.pixie_dir, args.device)
    colors, weight = face_colors(body, crop, pixels, landmarks, facing, head_ids)
    colored = body.copy()
    colored.visual.vertex_colors = colors
    args.output_dir.mkdir(parents=True, exist_ok=False)
    output = args.output_dir / "person-001.glb"
    colored.export(output)
    written = trimesh.load_mesh(output, process=False)
    if not np.array_equal(written.faces, body.faces) or not np.array_equal(written.vertices, body.vertices):
        raise RuntimeError("Face color export changed body geometry")
    if not np.array_equal(written.visual.vertex_colors, colors):
        raise RuntimeError("Face color did not survive GLB export")
    preview_face(body, written, args.output_dir / "face-comparison.png")
    if args.debug_overlay:
        overlay = crop.copy()
        draw = ImageDraw.Draw(overlay)
        for x, y in landmarks:
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill="#39fdd2")
        overlay.save(args.output_dir / "projection-check.png")
    report = {
        "method": "PIXIE head projection to vertex colors",
        "body_geometry_unchanged": True,
        "photo_colored_vertices": int(np.count_nonzero(weight > 0.5)),
        "unobserved_areas": "Side and back of head approximated with a flat skin tone; no hair mesh",
    }
    (args.output_dir / "face-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
