"""Optional PyTorch/DeepLabV3 COCO person-mask helper for head capture QC.

Invoked by `capture_analyzer.py` / `analyze_capture.py` when `target_type == "head"`
and a PyTorch + torchvision runtime is available (e.g. local GPU conda env).
Reads downsampled RGB frames from a temporary `.npz` and writes boolean person masks.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np


def _load_model(device):
    import torch
    from torchvision.models.segmentation import (
        DeepLabV3_MobileNet_V3_Large_Weights,
        DeepLabV3_ResNet50_Weights,
        deeplabv3_mobilenet_v3_large,
        deeplabv3_resnet50,
    )

    try:
        model = deeplabv3_resnet50(weights=DeepLabV3_ResNet50_Weights.DEFAULT)
    except Exception:
        model = deeplabv3_mobilenet_v3_large(
            weights=DeepLabV3_MobileNet_V3_Large_Weights.DEFAULT
        )
    return model.to(device).eval()


def segment_frames(frames_u8: np.ndarray, threshold: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _load_model(device)
    mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)

    n, h, w, _ = frames_u8.shape
    masks = np.zeros((n, h, w), dtype=bool)
    probs = np.zeros((n,), dtype=np.float32)

    with torch.no_grad():
        for i in range(n):
            ten = torch.from_numpy(frames_u8[i]).to(device).permute(2, 0, 1).unsqueeze(0).float() / 255.0
            out = model((ten - mean) / std)["out"][0]
            prob = torch.softmax(out, dim=0)[15]  # COCO class 15 = person
            probs[i] = float(prob.mean().item())
            masks[i] = (prob > threshold).cpu().numpy()

    return masks, probs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepLabV3 person mask helper")
    parser.add_argument("--input-npz", required=True)
    parser.add_argument("--output-npz", required=True)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args(argv)

    data = np.load(args.input_npz)
    frames = data["frames"]
    masks, probs = segment_frames(frames, threshold=args.threshold)
    np.savez_compressed(args.output_npz, masks=masks, probs=probs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
