"""Compare PIXIE T-pose shapes inferred from the same photograph."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import torch
import trimesh


def measure_tpose(mesh: trimesh.Trimesh, height_m: float) -> dict:
    if not np.isfinite(mesh.vertices).all():
        raise ValueError("Non-finite geometry")
    floor, ceiling = mesh.bounds[:, 1]
    height = float(ceiling - floor)
    if height <= 0:
        raise ValueError("Non-positive mesh height")

    sections = {}
    for name, fraction in (("hip", 0.50), ("waist", 0.65), ("chest", 0.72)):
        segments = trimesh.intersections.mesh_plane(mesh, [0, 1, 0], [0, floor + fraction * height, 0])
        if not len(segments):
            raise ValueError(f"Empty {name} cross-section at {fraction:.0%} height")
        width, _, depth = np.ptp(segments.reshape(-1, 3), axis=0) * (height_m / height)
        sections[name] = {"height_fraction": fraction, "width_m": float(width), "depth_m": float(depth)}
    return {"raw_height_m": height, "sections": sections}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pixie-dir", type=Path, default=Path(__file__).resolve().parents[2] / "3d模型测试")
    parser.add_argument("--bbox", type=int, nargs=4, metavar=("L", "T", "R", "B"))
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--axis", type=int, action="append", choices=[0, 1])
    parser.add_argument("--deltas", default="-2,-1,-0.5,0.5,1,2")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--target-waist", type=float, help="Target cross-section width in meters")
    target.add_argument("--narrow-by", type=float, default=0.08, help="Experimental reduction from the baseline")
    parser.add_argument("--height-m", type=float, required=True)
    args = parser.parse_args()

    if not args.image.is_file():
        parser.error("--image must exist")
    if not np.isfinite(args.height_m) or args.height_m <= 0:
        parser.error("--height-m must be positive and finite")
    if args.target_waist is not None and (not np.isfinite(args.target_waist) or args.target_waist <= 0):
        parser.error("--target-waist must be positive and finite")
    if not 0 < args.narrow_by < 1:
        parser.error("--narrow-by must be between 0 and 1")
    try:
        deltas = list(dict.fromkeys(float(delta) for delta in args.deltas.split(",")))
    except ValueError:
        parser.error("--deltas must be comma-separated numbers")
    if not np.isfinite(deltas).all():
        parser.error("--deltas must be finite")
    if not (args.pixie_dir / "pixie-5c8879b" / "pixielib").is_dir():
        parser.error("--pixie-dir must contain the configured PIXIE source directory")

    os.environ["OOTD_PIXIE_ROOT"] = str(args.pixie_dir.resolve())
    from run_pixie_mesh import PIXIE, input_image, prepare_config

    args.output_dir.mkdir(parents=True, exist_ok=False)
    print("Loading PIXIE...", flush=True)
    model = PIXIE(config=prepare_config(), device=args.device)
    print("Encoding input image once...", flush=True)
    with torch.inference_mode():
        data = input_image(args.image, args.bbox, args.device)
        params = model.encode(data, threthold=True, keep_local=True, copy_and_paste=False)["body"]
        # Decode converts jaw pose to rotation matrices required by decode_Tpose.
        model.decode(params, param_type="body")
        base_shape = params["shape"]
        faces = model.smplx.faces_tensor.cpu().numpy()
        trials = [(None, 0.0)]
        trials += [(axis, delta) for axis in dict.fromkeys(args.axis or [0, 1]) for delta in deltas if delta]
        rows = []
        meshes = []
        rejected = []
        for axis, delta in trials:
            shape = base_shape.clone()
            if axis is not None:
                shape[0, axis] += delta
            vertices = model.decode_Tpose({**params, "shape": shape})[0].cpu().numpy().copy()
            mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
            try:
                metrics = measure_tpose(mesh, args.height_m)
            except ValueError as exc:
                if axis is None:
                    raise
                rejected.append({"axis": axis, "delta": delta, "error": str(exc)})
                continue
            rows.append({"axis": axis, "delta": delta, **metrics})
            meshes.append(mesh)

    baseline_width = rows[0]["sections"]["waist"]["width_m"]
    target_width = args.target_waist if args.target_waist is not None else baseline_width * (1 - args.narrow_by)
    selected = min(range(len(rows)), key=lambda i: (
        abs(rows[i]["sections"]["waist"]["width_m"] - target_width), abs(rows[i]["delta"])
    ))

    for filename, index in (("person-baseline.glb", 0), ("person-tuned.glb", selected)):
        mesh = meshes[index]
        mesh.visual.face_colors = [155, 181, 208, 255]
        mesh.export(args.output_dir / filename)
    report = {
        "input": args.image.name,
        "bbox": args.bbox,
        "axis_betas": {axis: float(base_shape[0, axis]) for axis in (0, 1)},
        "height_reference_m": args.height_m,
        "rows": rows,
        "rejected": rejected,
        "selection": rows[selected],
        "target_waist_m": target_width,
        "output": "person-tuned.glb",
        "note": "截面宽度用于几何对比。目标比例为试验值，需本人确认相似度。",
    }
    (args.output_dir / "tune-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Baseline waist {baseline_width:.3f} m, selected {rows[selected]['sections']['waist']['width_m']:.3f} m")
    print(f"Compared {len(rows)} shapes ({len(rejected)} rejected); wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
