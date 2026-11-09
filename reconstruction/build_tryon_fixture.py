"""Create two synthetic shirt meshes for testing the local 3D asset pipeline."""

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import trimesh


def make_shirt(person: trimesh.Trimesh, hem: float, sleeve_end: float, color: list[int]) -> trimesh.Trimesh:
    # Start just outside the body surface, then cut openings for neck, waist and sleeves.
    vertices = person.vertices + 0.014 * person.vertex_normals
    faces = person.faces
    height = person.extents[1]
    planes = [
        ([0, 1, 0], [0, hem * height, 0]),
        ([0, -1, 0], [0, 0.837 * height, 0]),
        ([1, 0, 0], [-sleeve_end * height, 0, 0]),
        ([-1, 0, 0], [sleeve_end * height, 0, 0]),
    ]
    for normal, origin in planes:
        vertices, faces, _ = trimesh.intersections.slice_faces_plane(vertices, faces, normal, origin)
    if not len(faces) or not np.isfinite(vertices).all():
        raise ValueError("Could not create a shirt on this T-pose")
    shirt = trimesh.Trimesh(vertices=vertices, faces=faces)
    shirt.visual.vertex_colors = color
    return shirt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person", type=Path, required=True, help="An aligned T-pose person GLB")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog-version", default="fixture-01")
    args = parser.parse_args()
    person = trimesh.load_mesh(args.person, process=False)
    if not isinstance(person, trimesh.Trimesh) or not len(person.faces):
        parser.error("--person must contain a single non-empty mesh")
    if abs(person.bounds[0, 1]) > 1e-3 or not 1.0 <= person.extents[1] <= 2.5:
        parser.error("--person must have Y-up, floor at Y=0 and a human height in meters")

    styles = [
        ("top-001", "浅色短袖 · 测试", ["white", "casual"], 0.538, 0.21, [241, 231, 207, 255]),
        ("top-002", "深蓝短袖 · 测试", ["dark", "casual"], 0.554, 0.24, [62, 88, 154, 255]),
    ]
    shirts = [make_shirt(person, hem, sleeve, color) for _, _, _, hem, sleeve, color in styles]
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.person, output / "person-001.glb")
    assets = [{
        "asset_type": "person",
        "asset_id": "person-001",
        "relative_file": "person-001.glb",
        "source": "participant-candidate",
    }]
    for shirt, (asset_id, name, tags, _, _, _) in zip(shirts, styles):
        filename = f"{asset_id}.glb"
        shirt.export(output / filename)
        assets.append({
            "asset_type": "garment",
            "asset_id": asset_id,
            "relative_file": filename,
            "source": "synthetic-test",
            "display_name": name,
            "tags": tags,
            "available": True,
        })
    manifest = {
        "schema_version": 1,
        "person_id": "person-001",
        "catalog_version": args.catalog_version,
        "pose_id": "pose-001",
        "assets": assets,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote test asset package: {output}")


if __name__ == "__main__":
    main()
