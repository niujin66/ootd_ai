"""Deterministic capture quality extractor for frame folders, videos, and COLMAP sparse models.

Outputs structured metrics directly compatible with `capture_guide.analyze_capture_metrics`
and `POST /v1/capture/guide`.

Design constraints:
- Runs out-of-the-box inside `server/.venv` using only the Python standard library + NumPy
  (no mandatory OpenCV/Pillow/SciPy dependency).
- Includes a pure-NumPy 8-bit PNG decoder (color types 0/2/4/6, filters 0-4), vectorized
  3x3 Laplacian variance sharpness, 64x64 histogram-equalized NCC orbit continuity, and
  border-color foreground margin estimation.
- Automatically uses Pillow / OpenCV / ffmpeg if present for JPEG or MP4 inputs.
- Parses both binary (`images.bin`) and text (`images.txt`) COLMAP sparse reconstructions
  and computes look-at target center via regularized ray intersection + SVD orbit plane.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import zlib
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    p = a.astype(np.int32) + b.astype(np.int32) - c.astype(np.int32)
    pa = np.abs(p - a)
    pb = np.abs(p - b)
    pc = np.abs(p - c)
    out = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
    return out.astype(np.uint8)


def decode_png_bytes(data: bytes) -> np.ndarray:
    """Decode an 8-bit non-interlaced PNG into an (H, W, 3) uint8 RGB array."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("不是有效的 PNG 文件")

    pos = 8
    width = height = bit_depth = color_type = interlace = None
    idat_chunks: list[bytes] = []

    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif ctype == b"IDAT":
            idat_chunks.append(payload)
        elif ctype == b"IEND":
            break

    if width is None or height is None or not idat_chunks:
        raise ValueError("PNG 缺少 IHDR 或 IDAT")
    if bit_depth != 8 or interlace != 0:
        raise ValueError(f"仅支持 8-bit 非隔行 PNG（收到 bit_depth={bit_depth}, interlace={interlace}）")

    channels_map = {0: 1, 2: 3, 4: 2, 6: 4}
    if color_type not in channels_map:
        raise ValueError(f"不支持的 PNG color_type={color_type}")
    channels = channels_map[color_type]

    raw = zlib.decompress(b"".join(idat_chunks))
    stride = width * channels
    expected = height * (1 + stride)
    if len(raw) < expected:
        raise ValueError("PNG 解压字节数不足")

    rows = np.frombuffer(raw[:expected], dtype=np.uint8).reshape(height, 1 + stride)
    filters = rows[:, 0]
    scanlines = rows[:, 1:].copy()
    recon = np.zeros_like(scanlines, dtype=np.uint8)

    for y in range(height):
        ftype = int(filters[y])
        row = scanlines[y]
        up = recon[y - 1] if y > 0 else np.zeros(stride, dtype=np.uint8)
        if ftype == 0:
            recon[y] = row
        elif ftype == 1:
            recon[y] = (np.cumsum(row.reshape(width, channels), axis=0) & 0xFF).reshape(stride).astype(np.uint8)
        elif ftype == 2:
            recon[y] = ((row.astype(np.int32) + up.astype(np.int32)) & 0xFF).astype(np.uint8)
        elif ftype == 3:
            out = np.zeros(stride, dtype=np.int32)
            for x in range(stride):
                left = int(out[x - channels]) if x >= channels else 0
                out[x] = (int(row[x]) + ((left + int(up[x])) >> 1)) & 0xFF
            recon[y] = out.astype(np.uint8)
        elif ftype == 4:
            out = np.zeros(stride, dtype=np.int32)
            for x in range(stride):
                a = out[x - channels] if x >= channels else 0
                b = int(up[x])
                c = int(up[x - channels]) if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out[x] = (int(row[x]) + pred) & 0xFF
            recon[y] = out.astype(np.uint8)
        else:
            raise ValueError(f"未知 PNG 滤波器类型：{ftype}")

    img = recon.reshape(height, width, channels)
    if color_type == 0:
        return np.repeat(img, 3, axis=2)
    if color_type == 4:
        return np.repeat(img[:, :, :1], 3, axis=2)
    return img[:, :, :3]


def _split_png_stream(stream_bytes: bytes) -> list[bytes]:
    """Split concatenated PNG files from ffmpeg image2pipe output."""
    chunks: list[bytes] = []
    idx = stream_bytes.find(PNG_SIGNATURE)
    while idx != -1:
        nxt = stream_bytes.find(PNG_SIGNATURE, idx + len(PNG_SIGNATURE))
        if nxt == -1:
            chunks.append(stream_bytes[idx:])
            break
        chunks.append(stream_bytes[idx:nxt])
        idx = nxt
    return chunks


def load_image_rgb(path: Path, ffmpeg_bin: str | None = None) -> np.ndarray:
    """Load an image file as (H, W, 3) uint8 RGB array."""
    suffix = path.suffix.lower()
    if suffix == ".png":
        raw = path.read_bytes()
        try:
            return decode_png_bytes(raw)
        except ValueError:
            pass

    try:
        from PIL import Image

        with Image.open(path) as im:
            return np.asarray(im.convert("RGB"), dtype=np.uint8)
    except ImportError:
        pass

    try:
        import cv2

        buf = np.fromfile(str(path), dtype=np.uint8)
        bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError(f"OpenCV 无法解码图像：{path}")
        return bgr[:, :, ::-1].copy()
    except ImportError:
        pass

    exe = ffmpeg_bin or shutil.which("ffmpeg")
    if exe:
        proc = subprocess.run(
            [exe, "-v", "error", "-i", str(path), "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True,
        )
        if proc.returncode == 0 and proc.stdout.startswith(PNG_SIGNATURE):
            return decode_png_bytes(proc.stdout)

    raise RuntimeError(
        f"当前环境缺少 Pillow/OpenCV/ffmpeg，无法解码非标准 8-bit PNG 文件：{path.name}"
    )


def _load_mask_binary(path: Path, ffmpeg_bin: str | None = None) -> np.ndarray:
    rgb = load_image_rgb(path, ffmpeg_bin=ffmpeg_bin)
    return rgb.max(axis=2) > 127


def load_frames_from_video(
    video_path: Path,
    max_frames: int = 36,
    ffmpeg_bin: str | None = None,
) -> tuple[list[np.ndarray], int]:
    """Decode evenly spaced frames from a video file using OpenCV or ffmpeg."""
    try:
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        if cap.isOpened():
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            if total > 0:
                indices = np.linspace(0, max(total - 1, 0), min(max_frames, total)).astype(int)
                frames: list[np.ndarray] = []
                for idx in indices:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
                    ok, bgr = cap.read()
                    if ok and bgr is not None:
                        frames.append(bgr[:, :, ::-1].copy())
                cap.release()
                if frames:
                    return frames, total
            cap.release()
    except ImportError:
        pass

    exe = ffmpeg_bin or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "解析视频需要安装 OpenCV 或在 PATH 中提供 ffmpeg（也可先抽帧为 PNG 目录再传入）"
        )

    proc = subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-i",
            str(video_path),
            "-vf",
            "fps=2,scale='min(720,iw)':-2",
            "-frames:v",
            str(max_frames),
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"ffmpeg 解码视频失败：{err}")
    pngs = _split_png_stream(proc.stdout)
    frames = [decode_png_bytes(chunk) for chunk in pngs]
    if not frames:
        raise RuntimeError(f"未能从视频中抽取到任何帧：{video_path}")
    return frames, len(frames)


def load_frames(
    input_path: Path,
    max_frames: int = 48,
    ffmpeg_bin: str | None = None,
) -> tuple[list[np.ndarray], int, list[str]]:
    """Load evenly sampled RGB frames from an image directory or video file."""
    path = Path(input_path)
    if path.is_dir():
        files = sorted(
            f for f in path.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS
        )
        if not files:
            raise ValueError(f"目录中未找到支持的图像文件：{path}")
        total = len(files)
        if total > max_frames:
            idxs = np.linspace(0, total - 1, max_frames).astype(int)
            picked = [files[i] for i in idxs]
        else:
            picked = files
        frames = [load_image_rgb(f, ffmpeg_bin=ffmpeg_bin) for f in picked]
        return frames, total, [f.name for f in picked]

    if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
        frames, total = load_frames_from_video(path, max_frames=max_frames, ffmpeg_bin=ffmpeg_bin)
        names = [f"video-frame-{i:04d}" for i in range(len(frames))]
        return frames, total, names

    raise ValueError(f"不支持的采集输入路径（需图像目录或视频文件）：{path}")


def _to_gray(rgb: np.ndarray) -> np.ndarray:
    arr = rgb.astype(np.float64)
    return 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]


def compute_laplacian_sharpness(rgb: np.ndarray) -> float:
    """Compute discrete 3x3 Laplacian variance (equivalent to cv2.Laplacian.var)."""
    gray = _to_gray(rgb)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    lap = (
        gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
        - 4.0 * gray[1:-1, 1:-1]
    )
    return float(np.var(lap))


def _equalize_hist_uint8(gray_u8: np.ndarray) -> np.ndarray:
    hist = np.bincount(gray_u8.ravel(), minlength=256)
    nz = hist[hist > 0]
    if nz.size <= 1:
        return gray_u8
    cdf = hist.cumsum()
    cdf_min = int(nz[0])
    total = int(cdf[-1])
    if total <= cdf_min:
        return gray_u8
    lut = np.clip(((cdf - cdf_min) * 255.0) / (total - cdf_min), 0, 255).astype(np.uint8)
    return lut[gray_u8]


def _downsample_64_feature(rgb: np.ndarray) -> np.ndarray:
    gray = np.clip(_to_gray(rgb), 0, 255)
    h, w = gray.shape
    ys = np.linspace(0, h - 1, 64).astype(int)
    xs = np.linspace(0, w - 1, 64).astype(int)
    small = gray[np.ix_(ys, xs)].astype(np.uint8)
    eq = _equalize_hist_uint8(small).astype(np.float32)
    eq -= eq.mean()
    norm = float(np.linalg.norm(eq))
    if norm < 1e-9:
        return np.zeros(64 * 64, dtype=np.float32)
    return (eq / norm).reshape(-1)


def compute_orbit_continuity(frames: list[np.ndarray]) -> dict[str, float | None]:
    """Compute consecutive frame cosine similarity on 64x64 equalized luminance grids."""
    if len(frames) < 2:
        return {"mean": None, "min": None, "p10": None}
    feats = np.stack([_downsample_64_feature(f) for f in frames])
    consec = [float(np.dot(feats[i], feats[i + 1])) for i in range(len(feats) - 1)]
    return {
        "mean": round(float(np.mean(consec)), 4),
        "min": round(float(np.min(consec)), 4),
        "p10": round(float(np.percentile(consec, 10)), 4),
    }


def _majority_smooth_3x3(mask: np.ndarray) -> np.ndarray:
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return mask
    padded = np.pad(mask.astype(np.uint8), 1, mode="edge")
    acc = (
        padded[:-2, :-2]
        + padded[:-2, 1:-1]
        + padded[:-2, 2:]
        + padded[1:-1, :-2]
        + padded[1:-1, 1:-1]
        + padded[1:-1, 2:]
        + padded[2:, :-2]
        + padded[2:, 1:-1]
        + padded[2:, 2:]
    )
    return acc >= 5


def _largest_central_component(mask: np.ndarray) -> np.ndarray:
    """Keep the 4-connected foreground component best anchored in the central 50% ROI."""
    sh, sw = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    best_comp: list[tuple[int, int]] | None = None
    best_score = -1.0
    cy0, cy1 = sh // 4, 3 * sh // 4
    cx0, cx1 = sw // 4, 3 * sw // 4
    for y in range(sh):
        for x in range(sw):
            if not mask[y, x] or visited[y, x]:
                continue
            q = deque([(y, x)])
            visited[y, x] = True
            pts: list[tuple[int, int]] = []
            center_hits = 0
            while q:
                py, px = q.popleft()
                pts.append((py, px))
                if cy0 <= py < cy1 and cx0 <= px < cx1:
                    center_hits += 1
                for ny, nx in ((py - 1, px), (py + 1, px), (py, px - 1), (py, px + 1)):
                    if 0 <= ny < sh and 0 <= nx < sw and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        q.append((ny, nx))
            score = center_hits * 2.0 + len(pts) * 0.2
            if center_hits > 0 and score > best_score:
                best_score = score
                best_comp = pts
    out = np.zeros_like(mask, dtype=bool)
    if best_comp:
        ys, xs = zip(*best_comp)
        out[np.array(ys), np.array(xs)] = True
    return out


def estimate_foreground_margins(
    rgb: np.ndarray,
    mask: np.ndarray | None = None,
    border_px: int = 8,
    color_thresh: float = 30.0,
) -> dict[str, Any]:
    """Estimate foreground bounding-box margins in pixels (left/right/top/bottom)."""
    h, w = rgb.shape[:2]
    if mask is not None:
        fg = mask.astype(bool)
        ys_nz, xs_nz = np.nonzero(fg)
        if len(xs_nz) == 0:
            return {"fg_ratio": 0.0, "margins": None}
        return {
            "fg_ratio": round(float(fg.mean()), 4),
            "margins": {
                "left": int(xs_nz.min()),
                "right": int(w - 1 - xs_nz.max()),
                "top": int(ys_nz.min()),
                "bottom": int(h - 1 - ys_nz.max()),
            },
        }

    b = max(2, min(border_px, h // 10, w // 10))
    cw = max(2, min(w // 6, 32))
    ch = max(2, min(h // 6, 32))
    patches = [
        rgb[:ch, :cw],
        rgb[:ch, -cw:],
        rgb[-ch:, :cw],
        rgb[-ch:, -cw:],
        rgb[h // 3 : 2 * h // 3, :b],
        rgb[h // 3 : 2 * h // 3, -b:],
    ]
    pal = np.asarray([np.median(p.reshape(-1, 3), axis=0) for p in patches], dtype=np.float32)

    scale = min(1.0, 160.0 / max(h, w))
    sh, sw = max(16, int(round(h * scale))), max(16, int(round(w * scale)))
    ys = np.linspace(0, h - 1, sh).astype(int)
    xs = np.linspace(0, w - 1, sw).astype(int)
    small = rgb[np.ix_(ys, xs)].astype(np.float32)
    diff = small[:, :, None, :] - pal[None, None, :, :]
    min_dist = np.min(np.linalg.norm(diff, axis=3), axis=2)
    fg = _majority_smooth_3x3(_majority_smooth_3x3(min_dist > color_thresh))
    fg = _largest_central_component(fg)

    ry, rx = np.nonzero(fg)
    if len(rx) == 0:
        return {"fg_ratio": 0.0, "margins": None}
    return {
        "fg_ratio": round(float(fg.mean()), 4),
        "margins": {
            "left": int(xs[rx.min()]),
            "right": int(w - 1 - xs[rx.max()]),
            "top": int(ys[ry.min()]),
            "bottom": int(h - 1 - ys[ry.max()]),
        },
    }


def analyze_frames(
    frames: list[np.ndarray],
    masks: list[np.ndarray | None] | None = None,
    margin_sample_count: int = 8,
) -> dict[str, Any]:
    """Compute sharpness, orbit continuity, and worst-case border margins across frames."""
    if not frames:
        raise ValueError("frames 不能为空")

    sharpness_vals = [compute_laplacian_sharpness(f) for f in frames]
    continuity = compute_orbit_continuity(frames)

    n = len(frames)
    if n > margin_sample_count:
        sample_idxs = np.linspace(0, n - 1, margin_sample_count).astype(int)
    else:
        sample_idxs = np.arange(n)

    fg_ratios: list[float] = []
    side_mins: dict[str, list[int]] = {"left": [], "right": [], "top": [], "bottom": []}
    for idx in sample_idxs:
        mask = masks[idx] if masks is not None and idx < len(masks) else None
        res = estimate_foreground_margins(frames[idx], mask=mask)
        fg_ratios.append(res["fg_ratio"])
        if res["margins"] is not None:
            for side, val in res["margins"].items():
                side_mins[side].append(val)

    min_margin_px = (
        {side: int(min(vals)) for side, vals in side_mins.items() if vals}
        if any(side_mins.values())
        else None
    )

    return {
        "sampled_frame_count": len(frames),
        "sharpness_p50": round(float(np.median(sharpness_vals)), 2),
        "sharpness_min": round(float(np.min(sharpness_vals)), 2),
        "orbit_continuity_min": continuity["min"],
        "orbit_continuity_mean": continuity["mean"],
        "orbit_continuity_p10": continuity["p10"],
        "fg_ratio_mean": round(float(np.mean(fg_ratios)), 4) if fg_ratios else None,
        "min_margin_px": min_margin_px,
    }


def _qvec2rotmat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
            [2 * x * y + 2 * z * w, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * x * w],
            [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x * x - 2 * y * y],
        ],
        dtype=np.float64,
    )


def parse_colmap_images(sparse_dir: Path) -> list[dict[str, Any]]:
    """Parse COLMAP images.bin or images.txt into pose dictionaries."""
    bin_path = sparse_dir / "images.bin"
    txt_path = sparse_dir / "images.txt"
    if bin_path.is_file():
        imgs: list[dict[str, Any]] = []
        with bin_path.open("rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            for _ in range(n):
                props = struct.unpack("<idddddddi", fh.read(64))
                iid, qvec, tvec = props[0], np.array(props[1:5]), np.array(props[5:8])
                name_bytes = bytearray()
                while True:
                    ch = fh.read(1)
                    if not ch or ch == b"\x00":
                        break
                    name_bytes.extend(ch)
                npts = struct.unpack("<Q", fh.read(8))[0]
                fh.read(24 * npts)
                imgs.append(
                    {
                        "id": iid,
                        "name": name_bytes.decode("utf-8", "replace"),
                        "qvec": qvec,
                        "tvec": tvec,
                    }
                )
        return imgs

    if txt_path.is_file():
        imgs = []
        lines = txt_path.read_text(encoding="utf-8", errors="replace").splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            idx += 1
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 10:
                continue
            iid = int(parts[0])
            qvec = np.array([float(v) for v in parts[1:5]], dtype=np.float64)
            tvec = np.array([float(v) for v in parts[5:8]], dtype=np.float64)
            name = " ".join(parts[9:])
            imgs.append({"id": iid, "name": name, "qvec": qvec, "tvec": tvec})
            idx += 1  # skip 2D points line
        return imgs

    raise FileNotFoundError(f"未在 {sparse_dir} 找到 COLMAP images.bin 或 images.txt")


def _estimate_ray_target(centers: np.ndarray, view_dirs: np.ndarray) -> np.ndarray:
    """Least-squares point closest to all camera optical axes (with mild regularization)."""
    A = np.eye(3, dtype=np.float64) * 1e-6
    b = centers.mean(axis=0) * 1e-6
    eye3 = np.eye(3, dtype=np.float64)
    for c, v in zip(centers, view_dirs):
        nrm = float(np.linalg.norm(v))
        if nrm < 1e-9:
            continue
        vn = v / nrm
        P = eye3 - np.outer(vn, vn)
        A += P
        b += P @ c
    return np.linalg.solve(A, b)


def analyze_colmap_sparse(sparse_dir: Path, total_frames: int | None = None) -> dict[str, Any]:
    """Compute azimuth sector coverage and camera up-axis roll consistency from COLMAP."""
    imgs = parse_colmap_images(Path(sparse_dir))
    if len(imgs) < 3:
        raise ValueError(f"已配准相机数过少（{len(imgs)} < 3），无法稳定估计环绕轨道")

    centers, x_axes, y_axes, z_axes = [], [], [], []
    for im in imgs:
        R_c2w = _qvec2rotmat(im["qvec"]).T
        centers.append(-R_c2w @ im["tvec"])
        x_axes.append(R_c2w[:, 0])
        y_axes.append(R_c2w[:, 1])
        z_axes.append(R_c2w[:, 2])

    C = np.asarray(centers, dtype=np.float64)
    X = np.asarray(x_axes, dtype=np.float64)
    Y = np.asarray(y_axes, dtype=np.float64)
    Z = np.asarray(z_axes, dtype=np.float64)

    cam_mean = C.mean(axis=0)
    _, _, vt = np.linalg.svd(C - cam_mean, full_matrices=False)
    up = vt[2] / np.linalg.norm(vt[2])

    # Automatically handle portrait vs landscape orientation by picking dominant up-aligned axis
    mean_abs_x = float(np.mean(np.abs(X @ up)))
    mean_abs_y = float(np.mean(np.abs(Y @ up)))
    vert_axes = X if mean_abs_x >= mean_abs_y else -Y

    rolls: list[float] = []
    for i in range(len(imgs)):
        u_proj = up - np.dot(up, Z[i]) * Z[i]
        nrm = float(np.linalg.norm(u_proj))
        if nrm < 1e-6:
            continue
        u_proj /= nrm
        cos_a = float(np.clip(abs(np.dot(vert_axes[i], u_proj)), -1.0, 1.0))
        rolls.append(math.degrees(math.acos(cos_a)))

    target = _estimate_ray_target(C, Z)
    d = C - target
    horiz = d - np.outer(d @ up, up)

    ref = np.array([1.0, 0.0, 0.0]) if abs(up[0]) < 0.9 else np.array([0.0, 0.0, 1.0])
    e1 = ref - np.dot(ref, up) * up
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)

    az = np.degrees(np.arctan2(horiz @ e2, horiz @ e1)) % 360.0
    covered_azimuth_deg = [round(float(a), 2) for a in az]

    registered = len(imgs)
    frame_count = total_frames if (total_frames and total_frames >= registered) else registered

    return {
        "registered_frames": registered,
        "frame_count": frame_count,
        "covered_azimuth_deg": covered_azimuth_deg,
        "up_roll_median_deg": round(float(np.median(rolls)), 2) if rolls else 0.0,
    }


def analyze_capture_source(
    *,
    request_id: str,
    target_type: str,
    subject_label: str = "",
    input_path: Path | None = None,
    sparse_dir: Path | None = None,
    masks_dir: Path | None = None,
    max_frames: int = 48,
    ffmpeg_bin: str | None = None,
    roi_detail_px: float | None = None,
    has_scale_reference: bool | None = None,
    missing_closeups: list[str] | None = None,
) -> dict[str, Any]:
    """Extract capture quality metrics ready to send to POST /v1/capture/guide."""
    if input_path is None and sparse_dir is None:
        raise ValueError("必须至少提供 input_path（图像目录/视频）或 sparse_dir（COLMAP sparse/0）之一")

    payload: dict[str, Any] = {
        "request_id": request_id,
        "target_type": target_type,
        "subject_label": subject_label,
    }

    total_frames: int | None = None
    if input_path is not None:
        frames, total_frames, names = load_frames(
            Path(input_path), max_frames=max_frames, ffmpeg_bin=ffmpeg_bin
        )
        masks: list[np.ndarray | None] | None = None
        if masks_dir is not None and Path(masks_dir).is_dir():
            mdir = Path(masks_dir)
            masks = []
            for name in names:
                stem = Path(name).stem
                cand = next(
                    (
                        mdir / f"{stem}{ext}"
                        for ext in (".png", ".jpg", ".jpeg")
                        if (mdir / f"{stem}{ext}").is_file()
                    ),
                    None,
                )
                masks.append(_load_mask_binary(cand, ffmpeg_bin=ffmpeg_bin) if cand else None)

        frame_stats = analyze_frames(frames, masks=masks)
        payload["sharpness_p50"] = frame_stats["sharpness_p50"]
        payload["orbit_continuity_min"] = frame_stats["orbit_continuity_min"]
        if frame_stats["min_margin_px"] is not None:
            payload["min_margin_px"] = frame_stats["min_margin_px"]
        payload["frame_count"] = total_frames

    if sparse_dir is not None:
        sparse_stats = analyze_colmap_sparse(Path(sparse_dir), total_frames=total_frames)
        payload["registered_frames"] = sparse_stats["registered_frames"]
        payload["frame_count"] = sparse_stats["frame_count"]
        payload["covered_azimuth_deg"] = sparse_stats["covered_azimuth_deg"]
        payload["up_roll_median_deg"] = sparse_stats["up_roll_median_deg"]

    if roi_detail_px is not None:
        payload["roi_detail_px"] = roi_detail_px
    if has_scale_reference is not None:
        payload["has_scale_reference"] = has_scale_reference
    if missing_closeups:
        payload["missing_closeups"] = missing_closeups

    return payload
