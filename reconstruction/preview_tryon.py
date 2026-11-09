"""Inspect a local three-file asset package and switch garments on the same person."""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import trimesh

from preview_person_asset import PANEL_HEIGHT, PANEL_WIDTH, render_view


def load_outfits(folder: Path) -> tuple[list[tuple[str, trimesh.Trimesh]], float, np.ndarray]:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8-sig"))
    assets = manifest["assets"]
    person = next(asset for asset in assets if asset["asset_type"] == "person")
    person_mesh = trimesh.load(folder / person["relative_file"], force="mesh", process=False)
    if person_mesh.visual.kind == "texture":
        person_mesh.visual = person_mesh.visual.to_color()
    outfits = []
    for asset in assets:
        if asset["asset_type"] != "garment" or not asset["available"]:
            continue
        garment = trimesh.load(folder / asset["relative_file"], force="mesh", process=False)
        if garment.visual.kind == "texture":
            garment.visual = garment.visual.to_color()
        outfits.append((asset["asset_id"], trimesh.util.concatenate([person_mesh, garment])))
    if not outfits:
        raise ValueError("No available garment in the manifest")
    scale = min(350 / max(person_mesh.extents[0], person_mesh.extents[2]), 400 / person_mesh.extents[1])
    return outfits, scale, person_mesh.bounds.mean(axis=0)


def save_comparison(
    outfits: list[tuple[str, trimesh.Trimesh]], scale: float, center: np.ndarray, output: Path
) -> None:
    sheet = Image.new("RGB", (3 * PANEL_WIDTH, (PANEL_HEIGHT + 24) * len(outfits)), "#eef2f7")
    draw = ImageDraw.Draw(sheet)
    for row, (asset_id, mesh) in enumerate(outfits):
        y = row * (PANEL_HEIGHT + 24)
        for column, (label, yaw) in enumerate((("Front", 0), ("Side", np.pi / 2), ("Back", np.pi))):
            x = column * PANEL_WIDTH
            sheet.paste(render_view(mesh, yaw, scale, center), (x, y))
            draw.text((x + PANEL_WIDTH // 2, y + PANEL_HEIGHT + 7), f"{asset_id} / {label}", fill="#21344b", anchor="mt")
    sheet.save(output)


def show_window(outfits: list[tuple[str, trimesh.Trimesh]], scale: float, center: np.ndarray) -> None:
    import tkinter as tk
    from tkinter import ttk

    from PIL import ImageTk

    window = tk.Tk()
    window.title("衣序 · 本地三维资产联调")
    ttk.Label(window, text="拖动角度查看正侧背面 · 切换衣服时保持角度").pack(pady=6)
    view = ttk.Label(window)
    view.pack()
    selected = tk.IntVar(value=0)
    yaw = tk.DoubleVar(value=0)

    def redraw(*_):
        mesh = outfits[selected.get()][1]
        view.image = ImageTk.PhotoImage(render_view(mesh, np.deg2rad(yaw.get()), scale, center))
        view.configure(image=view.image)

    ttk.Scale(window, from_=0, to=360, variable=yaw, command=redraw).pack(fill="x", padx=14, pady=8)
    options = ttk.Frame(window)
    options.pack(pady=8)
    for i, (asset_id, _) in enumerate(outfits):
        ttk.Radiobutton(options, text=asset_id, variable=selected, value=i, command=redraw).pack(side="left", padx=10)
    redraw()
    window.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Path to a validated local asset package")
    parser.add_argument("--output", type=Path, help="Save a front/side/back sheet instead of opening the window")
    args = parser.parse_args()
    outfits, scale, center = load_outfits(args.folder)
    if args.output is not None:
        save_comparison(outfits, scale, center, args.output)
    else:
        show_window(outfits, scale, center)


if __name__ == "__main__":
    main()
