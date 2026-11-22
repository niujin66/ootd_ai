"""Deterministic capture quality extractor for frame folders, videos, and COLMAP sparse models.

Outputs structured metrics directly compatible with `capture_guide.analyze_capture_metrics`
and `POST /v1/capture/guide`.

Design constraints:
- Runs out-of-the-box inside `server/.venv` using only the Python standard library + NumPy
  (no mandatory OpenCV/Pillow/SciPy dependency).
- Includes a pure-NumPy 8-bit PNG decoder (color types 0/2/4/6, filters 0-4), native
  Windows Media Foundation MP4/MOV video decoder via ctypes (zero external binaries),
  vectorized 3x3 Laplacian variance sharpness, 64x64 histogram-equalized NCC orbit
  continuity, GrabCut-lite illumination-invariant foreground margin estimation, and
  automatic head/garment ROI detail pixel estimation.
- Automatically uses Pillow / OpenCV / ffmpeg if present.
- Parses both binary (`images.bin`) and text (`images.txt`) COLMAP sparse reconstructions
  and computes look-at target center via regularized ray intersection + SVD orbit plane.
"""

from __future__ import annotations

import ctypes
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from collections import deque
from ctypes import wintypes
from pathlib import Path
from typing import Any

import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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
            recon[y] = (
                (np.cumsum(row.reshape(width, channels), axis=0) & 0xFF)
                .reshape(stride)
                .astype(np.uint8)
            )
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


def _decode_images_via_dotnet(paths: list[Path]) -> list[np.ndarray]:
    """Batch-decode non-PNG images on Windows via .NET System.Drawing without PIL/cv2."""
    if os.name != "nt" or not paths:
        raise RuntimeError("Windows .NET 解码仅支持 Windows 环境")

    ps_script = r"""
Add-Type -AssemblyName System.Drawing
$ms = New-Object System.IO.MemoryStream
while ($line = [Console]::In.ReadLine()) {
    if (-not $line) { continue }
    $bmp = [System.Drawing.Bitmap]::FromFile($line)
    $ms.SetLength(0)
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $bytes = $ms.ToArray()
    $lenBytes = [BitConverter]::GetBytes([int]$bytes.Length)
    $stdout = [Console]::OpenStandardOutput()
    $stdout.Write($lenBytes, 0, 4)
    $stdout.Write($bytes, 0, $bytes.Length)
    $stdout.Flush()
}
"""
    stdin_data = "\n".join(str(p) for p in paths) + "\n"
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        input=stdin_data.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"Windows .NET 图像解码失败：{err}")

    out = proc.stdout
    pos = 0
    frames: list[np.ndarray] = []
    while pos + 4 <= len(out):
        size = struct.unpack("<i", out[pos : pos + 4])[0]
        pos += 4
        if size <= 0 or pos + size > len(out):
            break
        frames.append(decode_png_bytes(out[pos : pos + size]))
        pos += size
    if len(frames) != len(paths):
        raise RuntimeError(f"Windows .NET 仅解码出 {len(frames)}/{len(paths)} 张图像")
    return frames


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

    if os.name == "nt":
        return _decode_images_via_dotnet([path])[0]

    raise RuntimeError(
        f"当前环境缺少 Pillow/OpenCV/ffmpeg，无法解码图像文件：{path.name}"
    )


def _load_mask_binary(path: Path, ffmpeg_bin: str | None = None) -> np.ndarray:
    rgb = load_image_rgb(path, ffmpeg_bin=ffmpeg_bin)
    return rgb.max(axis=2) > 127


def _load_frames_media_foundation(video_path: Path, max_frames: int) -> tuple[list[np.ndarray], int]:
    """Decode evenly spaced RGB frames on Windows via native Media Foundation (zero deps)."""

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", wintypes.USHORT),
            ("w1", wintypes.WORD),
            ("w2", wintypes.WORD),
            ("w3", wintypes.WORD),
            ("val", ctypes.c_int64),
            ("pad", ctypes.c_int64),
        ]

    def _guid(s: str) -> _GUID:
        g = _GUID()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
        return g

    def _vcall(ptr: ctypes.c_void_p, idx: int, restype: Any, *argtypes: Any):
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[idx])

    ole32 = ctypes.windll.ole32
    mfplat = ctypes.windll.mfplat
    mfreadwrite = ctypes.windll.mfreadwrite

    MF_VERSION = 0x00020070
    FIRST_VIDEO_STREAM = 0xFFFFFFFC
    MEDIASOURCE = 0xFFFFFFFF
    ENABLE_VIDEO_PROCESSING = _guid("fb394f3d-ccf1-42ee-bbb3-f9b845d5681d")
    PD_DURATION = _guid("6c990d33-bb8e-477a-8598-0d5d96fcd88a")
    MT_MAJOR_TYPE = _guid("48eba18e-f8c9-4687-bf11-0a74c9f96a8f")
    MT_SUBTYPE = _guid("f7e34c9a-42e8-4714-b74b-cb29d72c35e5")
    MT_FRAME_SIZE = _guid("1652c33d-d6b2-4012-b834-72030849a37d")
    MT_DEFAULT_STRIDE = _guid("644b4e48-1e02-4516-b0eb-c01ca9d49ac6")
    MT_VIDEO_ROTATION = _guid("c380465d-2271-428c-9b83-ecea3b4a85c1")
    MEDIA_TYPE_VIDEO = _guid("73646976-0000-0010-8000-00AA00389B71")
    VIDEO_FORMAT_RGB32 = _guid("00000016-0000-0010-8000-00AA00389B71")
    GUID_NULL = _GUID()

    ole32.CoInitializeEx(None, 0)
    mfplat.MFStartup(MF_VERSION, 0)

    attrs = ctypes.c_void_p()
    reader = ctypes.c_void_p()
    mtype = ctypes.c_void_p()
    cur_type = ctypes.c_void_p()
    try:
        mfplat.MFCreateAttributes(ctypes.byref(attrs), 1)
        _vcall(attrs, 21, ctypes.HRESULT, ctypes.POINTER(_GUID), wintypes.UINT)(
            attrs, ctypes.byref(ENABLE_VIDEO_PROCESSING), 1
        )
        hr = mfreadwrite.MFCreateSourceReaderFromURL(
            ctypes.c_wchar_p(str(video_path.resolve())), attrs, ctypes.byref(reader)
        )
        if hr != 0 or not reader.value:
            raise RuntimeError(f"MediaFoundation 无法打开视频（hr=0x{hr & 0xFFFFFFFF:x}）")

        mfplat.MFCreateMediaType(ctypes.byref(mtype))
        _vcall(mtype, 24, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(_GUID))(
            mtype, ctypes.byref(MT_MAJOR_TYPE), ctypes.byref(MEDIA_TYPE_VIDEO)
        )
        _vcall(mtype, 24, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(_GUID))(
            mtype, ctypes.byref(MT_SUBTYPE), ctypes.byref(VIDEO_FORMAT_RGB32)
        )
        _vcall(reader, 7, ctypes.HRESULT, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)(
            reader, FIRST_VIDEO_STREAM, None, mtype
        )
        _vcall(reader, 6, ctypes.HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
            reader, FIRST_VIDEO_STREAM, ctypes.byref(cur_type)
        )

        wh = ctypes.c_uint64()
        _vcall(cur_type, 8, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_uint64))(
            cur_type, ctypes.byref(MT_FRAME_SIZE), ctypes.byref(wh)
        )
        width, height = wh.value >> 32, wh.value & 0xFFFFFFFF

        stride_val = ctypes.c_int32()
        hr_stride = _vcall(
            cur_type, 7, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_int32)
        )(cur_type, ctypes.byref(MT_DEFAULT_STRIDE), ctypes.byref(stride_val))
        raw_stride = int(stride_val.value) if hr_stride == 0 and stride_val.value != 0 else width * 4
        bottom_up = raw_stride < 0
        abs_stride = abs(raw_stride)

        rot_val = wintypes.UINT()
        hr_rot = _vcall(
            cur_type, 7, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(wintypes.UINT)
        )(cur_type, ctypes.byref(MT_VIDEO_ROTATION), ctypes.byref(rot_val))
        rot_cw = int(rot_val.value) if hr_rot == 0 else 0

        pv_dur = _PROPVARIANT()
        hr_dur = _vcall(
            reader,
            12,
            ctypes.HRESULT,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_PROPVARIANT),
        )(reader, MEDIASOURCE, ctypes.byref(PD_DURATION), ctypes.byref(pv_dur))
        dur_100ns = int(pv_dur.val) if hr_dur == 0 and pv_dur.val > 0 else 0

        frames: list[np.ndarray] = []
        n_target = max(1, max_frames)
        for k in range(n_target):
            if dur_100ns > 0:
                pos_100ns = int(dur_100ns * k / n_target)
                pv_pos = _PROPVARIANT(vt=20, val=pos_100ns)
                _vcall(reader, 10, ctypes.HRESULT, wintypes.DWORD)(reader, FIRST_VIDEO_STREAM)
                _vcall(reader, 8, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(_PROPVARIANT))(
                    reader, ctypes.byref(GUID_NULL), ctypes.byref(pv_pos)
                )

            for _ in range(15):
                sidx = wintypes.DWORD()
                flags = wintypes.DWORD()
                ts = ctypes.c_int64()
                sample = ctypes.c_void_p()
                _vcall(
                    reader,
                    9,
                    ctypes.HRESULT,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(ctypes.c_int64),
                    ctypes.POINTER(ctypes.c_void_p),
                )(
                    reader,
                    FIRST_VIDEO_STREAM,
                    0,
                    ctypes.byref(sidx),
                    ctypes.byref(flags),
                    ctypes.byref(ts),
                    ctypes.byref(sample),
                )
                if sample:
                    buf = ctypes.c_void_p()
                    _vcall(sample, 41, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))(
                        sample, ctypes.byref(buf)
                    )
                    ptr = ctypes.c_void_p()
                    ml = wintypes.DWORD()
                    cl = wintypes.DWORD()
                    _vcall(
                        buf,
                        3,
                        ctypes.HRESULT,
                        ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD),
                        ctypes.POINTER(wintypes.DWORD),
                    )(buf, ctypes.byref(ptr), ctypes.byref(ml), ctypes.byref(cl))
                    needed = height * abs_stride
                    if cl.value >= needed:
                        raw_buf = (ctypes.c_uint8 * cl.value).from_address(ptr.value)
                        rows = np.ctypeslib.as_array(raw_buf)[:needed].reshape(height, abs_stride)
                        rgb = rows[:, : width * 4].reshape(height, width, 4)[:, :, [2, 1, 0]]
                        if bottom_up:
                            rgb = rgb[::-1]
                        if rot_cw == 90:
                            rgb = np.rot90(rgb, k=3)
                        elif rot_cw == 180:
                            rgb = np.rot90(rgb, k=2)
                        elif rot_cw == 270:
                            rgb = np.rot90(rgb, k=1)
                        frames.append(rgb.copy())
                    _vcall(buf, 4, ctypes.HRESULT)(buf)
                    _vcall(buf, 2, ctypes.c_ulong)(buf)
                    _vcall(sample, 2, ctypes.c_ulong)(sample)
                    break
                if flags.value & 0x2:  # MF_SOURCE_READERF_ENDOFSTREAM
                    break

        if not frames:
            raise RuntimeError(f"MediaFoundation 未能从视频读取任何帧：{video_path}")
        return frames, len(frames)
    finally:
        for obj in (cur_type, mtype, reader, attrs):
            if obj and obj.value:
                _vcall(obj, 2, ctypes.c_ulong)(obj)
        mfplat.MFShutdown()


def load_frames_from_video(
    video_path: Path,
    max_frames: int = 36,
    ffmpeg_bin: str | None = None,
) -> tuple[list[np.ndarray], int]:
    """Decode evenly spaced frames from a video file using OpenCV, MediaFoundation, or ffmpeg."""
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

    if os.name == "nt" and not ffmpeg_bin:
        try:
            return _load_frames_media_foundation(video_path, max_frames=max_frames)
        except Exception:
            pass

    exe = ffmpeg_bin or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "解析视频需要 OpenCV、Windows Media Foundation 或 PATH 中的 ffmpeg"
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

        # Fast path: batch-decode non-PNG images on Windows if PIL/cv2/ffmpeg absent
        non_png = [f for f in picked if f.suffix.lower() != ".png"]
        if non_png and os.name == "nt" and not (ffmpeg_bin or shutil.which("ffmpeg")):
            try:
                __import__("PIL")
                has_py_img = True
            except ImportError:
                try:
                    __import__("cv2")
                    has_py_img = True
                except ImportError:
                    has_py_img = False
            if not has_py_img:
                decoded_map = dict(zip(non_png, _decode_images_via_dotnet(non_png)))
                frames = [
                    decoded_map[f] if f in decoded_map else load_image_rgb(f, ffmpeg_bin=ffmpeg_bin)
                    for f in picked
                ]
                return frames, total, [f.name for f in picked]

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


def _erode_3x3(mask: np.ndarray) -> np.ndarray:
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return mask
    p = np.pad(mask.astype(bool), 1, mode="constant", constant_values=False)
    return (
        p[:-2, :-2]
        & p[:-2, 1:-1]
        & p[:-2, 2:]
        & p[1:-1, :-2]
        & p[1:-1, 1:-1]
        & p[1:-1, 2:]
        & p[2:, :-2]
        & p[2:, 1:-1]
        & p[2:, 2:]
    )


def _dilate_3x3(mask: np.ndarray) -> np.ndarray:
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return mask
    p = np.pad(mask.astype(bool), 1, mode="constant", constant_values=False)
    return (
        p[:-2, :-2]
        | p[:-2, 1:-1]
        | p[:-2, 2:]
        | p[1:-1, :-2]
        | p[1:-1, 1:-1]
        | p[1:-1, 2:]
        | p[2:, :-2]
        | p[2:, 1:-1]
        | p[2:, 2:]
    )


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


def extract_head_margins_from_mask(
    mask: np.ndarray,
    orig_h: int,
    orig_w: int,
) -> dict[str, Any]:
    """Isolate crown-to-neck head region from a person mask so wide shoulders don't skew margins."""
    fg = mask.astype(bool)
    mh, mw = fg.shape
    ys_nz, xs_nz = np.nonzero(fg)
    if len(xs_nz) == 0:
        return {
            "fg_ratio": 0.0,
            "margins": None,
            "bbox_width_px": 0,
            "bbox_height_px": 0,
            "head_mask": fg,
        }

    fg_ratio = round(float(fg.mean()), 4)
    y_top = int(ys_nz.min())
    y_bot = int(ys_nz.max())
    span = y_bot - y_top + 1

    # Find neck constriction below the upper head peak (ignoring lower shoulder flare)
    row_widths = fg.sum(axis=1)
    peak_start = min(mh - 1, y_top + max(2, int(0.08 * mh)))
    peak_end = min(mh - 1, y_top + max(peak_start - y_top + 1, int(0.42 * span)))
    if peak_end > peak_start:
        peak_y = peak_start + int(np.argmax(row_widths[peak_start:peak_end]))
        peak_w = int(row_widths[peak_y])
    else:
        peak_y = y_top
        peak_w = int(row_widths[y_top])

    neck_end = min(y_bot, y_top + max(4, int(0.68 * span)))
    if neck_end > peak_y + 2 and peak_w > 0:
        cand_idx = int(np.argmin(row_widths[peak_y:neck_end]))
        neck_w = int(row_widths[peak_y + cand_idx])
        neck_y = peak_y + cand_idx if neck_w <= int(0.88 * peak_w) else neck_end
    else:
        neck_y = y_bot

    head_mask = np.zeros_like(fg, dtype=bool)
    head_mask[y_top : neck_y + 1, :] = fg[y_top : neck_y + 1, :]
    hys, hxs = np.nonzero(head_mask)
    if len(hxs) == 0:
        hys, hxs = ys_nz, xs_nz
        head_mask = fg

    sx = orig_w / float(mw)
    sy = orig_h / float(mh)
    left_px = int(round(hxs.min() * sx))
    right_px = int(round((mw - 1 - hxs.max()) * sx))
    top_px = int(round(hys.min() * sy))
    bottom_px = int(round((mh - 1 - hys.max()) * sy))

    return {
        "fg_ratio": fg_ratio,
        "margins": {
            "left": max(0, left_px),
            "right": max(0, right_px),
            "top": max(0, top_px),
            "bottom": max(0, bottom_px),
        },
        "bbox_width_px": max(1, int(round((hxs.max() - hxs.min() + 1) * sx))),
        "bbox_height_px": max(1, int(round((hys.max() - hys.min() + 1) * sy))),
        "head_mask": head_mask,
    }


def estimate_foreground_margins(
    rgb: np.ndarray,
    mask: np.ndarray | None = None,
    border_px: int = 8,
    color_thresh: float = 30.0,
    target_type: str | None = None,
) -> dict[str, Any]:
    """Estimate foreground bounding-box margins using mask or GrabCut-lite color likelihood."""
    h, w = rgb.shape[:2]
    if mask is not None:
        if target_type == "head":
            return extract_head_margins_from_mask(mask, h, w)
        fg = mask.astype(bool)
        mh, mw = fg.shape
        ys_nz, xs_nz = np.nonzero(fg)
        if len(xs_nz) == 0:
            return {"fg_ratio": 0.0, "margins": None, "bbox_width_px": 0, "bbox_height_px": 0}
        sx = w / float(mw)
        sy = h / float(mh)
        left_px = int(round(xs_nz.min() * sx))
        right_px = int(round((mw - 1 - xs_nz.max()) * sx))
        top_px = int(round(ys_nz.min() * sy))
        bottom_px = int(round((mh - 1 - ys_nz.max()) * sy))
        return {
            "fg_ratio": round(float(fg.mean()), 4),
            "margins": {
                "left": max(0, left_px),
                "right": max(0, right_px),
                "top": max(0, top_px),
                "bottom": max(0, bottom_px),
            },
            "bbox_width_px": max(1, int(round((xs_nz.max() - xs_nz.min() + 1) * sx))),
            "bbox_height_px": max(1, int(round((ys_nz.max() - ys_nz.min() + 1) * sy))),
        }

    # In head mode without segmentation mask, restrict background sampling to upper half
    # so shoulders at bottom corners don't pollute the background palette.
    work_rgb = rgb[: int(h * 0.68)] if (target_type == "head" and h > w) else rgb
    wh, ww = work_rgb.shape[:2]
    b = max(2, min(border_px, wh // 10, ww // 10))
    cw = max(2, min(ww // 6, 32))
    ch = max(2, min(wh // 6, 32))
    if target_type == "head":
        bg_patches = [
            work_rgb[:ch, :cw],
            work_rgb[:ch, -cw:],
            work_rgb[:ch, ww // 3 : 2 * ww // 3],
            work_rgb[ch : min(wh, ch * 3), :b],
            work_rgb[ch : min(wh, ch * 3), -b:],
        ]
    else:
        bg_patches = [
            work_rgb[:ch, :cw],
            work_rgb[:ch, -cw:],
            work_rgb[-ch:, :cw],
            work_rgb[-ch:, -cw:],
            work_rgb[wh // 3 : 2 * wh // 3, :b],
            work_rgb[wh // 3 : 2 * wh // 3, -b:],
        ]
    bg_pal = np.asarray([np.median(p.reshape(-1, 3), axis=0) for p in bg_patches], dtype=np.float32)

    scale = min(1.0, 160.0 / max(wh, ww))
    sh, sw = max(16, int(round(wh * scale))), max(16, int(round(ww * scale)))
    ys = np.linspace(0, wh - 1, sh).astype(int)
    xs = np.linspace(0, ww - 1, sw).astype(int)
    small = work_rgb[np.ix_(ys, xs)].astype(np.float32)

    # Illumination-robust RGB + chromaticity distance against perimeter palette
    small_c = small / (small.sum(axis=2, keepdims=True) + 15.0) * 255.0
    bg_c = bg_pal / (bg_pal.sum(axis=1, keepdims=True) + 15.0) * 255.0
    d_rgb = np.linalg.norm(small[:, :, None, :] - bg_pal[None, None, :, :], axis=3)
    d_chr = np.linalg.norm(small_c[:, :, None, :] - bg_c[None, None, :, :], axis=3)
    d_bg = np.min(np.minimum(d_rgb, np.maximum(d_chr * 1.4, d_rgb * 0.65)), axis=2)

    # Central subject palette (GrabCut-lite prior) prevents edge wall shadows from leaking in
    cy0, cy1 = sh // 4, 3 * sh // 4
    cx0, cx1 = sw // 4, 3 * sw // 4
    c_mask = d_bg[cy0:cy1, cx0:cx1] > color_thresh
    if np.any(c_mask):
        fg_pixels = small[cy0:cy1, cx0:cx1][c_mask]
        lum = fg_pixels.mean(axis=1)
        qs = np.quantile(lum, [0.15, 0.4, 0.65, 0.9])
        fg_pal = []
        for q in qs:
            near = fg_pixels[np.abs(lum - q) <= 25.0]
            fg_pal.append(np.median(near, axis=0) if len(near) else np.median(fg_pixels, axis=0))
        fg_pal_arr = np.asarray(fg_pal, dtype=np.float32)
        d_fg = np.min(
            np.linalg.norm(small[:, :, None, :] - fg_pal_arr[None, None, :, :], axis=3), axis=2
        )
    else:
        d_fg = np.zeros_like(d_bg)

    yy, xx = np.ogrid[:sh, :sw]
    outer_band = (
        (yy < int(0.10 * sh))
        | (yy >= int(0.90 * sh))
        | (xx < int(0.12 * sw))
        | (xx >= int(0.88 * sw))
    )
    fg = (d_bg > color_thresh) & (~outer_band | (d_fg <= d_bg * 1.05))
    fg = _majority_smooth_3x3(fg)
    fg = _dilate_3x3(_erode_3x3(fg))
    fg = _largest_central_component(fg)

    ry, rx = np.nonzero(fg)
    if len(rx) == 0:
        return {"fg_ratio": 0.0, "margins": None, "bbox_width_px": 0, "bbox_height_px": 0}
    left_px = int(xs[rx.min()])
    right_px = int(ww - 1 - xs[rx.max()])
    top_px = int(ys[ry.min()])
    bottom_px = int(h - 1 - ys[ry.max()])
    return {
        "fg_ratio": round(float(fg.mean()), 4),
        "margins": {
            "left": left_px,
            "right": right_px,
            "top": top_px,
            "bottom": bottom_px,
        },
        "bbox_width_px": max(1, ww - left_px - right_px),
        "bbox_height_px": max(1, wh - top_px - int(ys[ry.min()])),
    }


def estimate_head_eye_detail_px(
    rgb: np.ndarray,
    bbox_width_px: int = 0,
    fg_mask: np.ndarray | None = None,
) -> float | None:
    """Estimate single-eye effective pixel width from upper central YCbCr skin-tone region."""
    h, w = rgb.shape[:2]
    work_rgb = rgb
    work_mask = fg_mask
    scale_x = 1.0
    if fg_mask is not None and fg_mask.shape != (h, w):
        mh, mw = fg_mask.shape[:2]
        ys = np.linspace(0, h - 1, mh).astype(int)
        xs = np.linspace(0, w - 1, mw).astype(int)
        work_rgb = rgb[np.ix_(ys, xs)]
        work_mask = fg_mask.astype(bool)
        scale_x = w / float(mw)

    wh, ww = work_rgb.shape[:2]
    arr = work_rgb.astype(np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    y_ch = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
    skin = (y_ch > 45.0) & (cb >= 77.0) & (cb <= 127.0) & (cr >= 133.0) & (cr <= 173.0)
    if work_mask is not None and work_mask.shape == (wh, ww) and np.any(work_mask):
        skin &= work_mask
        # If very little skin inside the head mask (back-of-head view), skip eye estimate for this frame
        if skin.sum() < 0.08 * work_mask.sum():
            return None
    else:
        zone = np.zeros((wh, ww), dtype=bool)
        zone[int(0.05 * wh) : int(0.75 * wh), int(0.15 * ww) : int(0.85 * ww)] = True
        skin &= zone

    skin = _majority_smooth_3x3(skin)
    ys_s, xs_s = np.nonzero(skin)
    if len(xs_s) >= 16:
        y_top = float(np.percentile(ys_s, 5))
        y_bot = float(np.percentile(ys_s, 95))
        head_cut = y_top + 0.45 * max(1.0, y_bot - y_top)
        head_xs = xs_s[ys_s <= head_cut]
        if len(head_xs) >= 8:
            face_w = float(np.percentile(head_xs, 92) - np.percentile(head_xs, 8)) * scale_x
            if face_w >= 4.0:
                return round(face_w * 0.21, 1)
    if bbox_width_px > 0 and fg_mask is None:
        return round(float(bbox_width_px) * 0.21, 1)
    return None


def _find_torch_runtime() -> tuple[str | None, dict[str, str]]:
    """Locate a Python runtime with torch + torchvision and optional local torch cache."""
    env = os.environ.copy()
    cache_dir = Path(__file__).resolve().parent.parent.parent / "ootd_gsscan" / ".torch-cache"
    if cache_dir.is_dir() and "TORCH_HOME" not in env:
        env["TORCH_HOME"] = str(cache_dir)

    candidates = [sys.executable]
    if os.name == "nt":
        dl_py = Path.home() / "anaconda3" / "envs" / "dl_env" / "python.exe"
        if dl_py.is_file() and str(dl_py) not in candidates:
            candidates.append(str(dl_py))

    for py in candidates:
        try:
            proc = subprocess.run(
                [py, "-c", "import torch, torchvision"],
                env=env,
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return py, env
        except Exception:
            continue
    return None, env


def segment_person_masks_torch(
    frames: list[np.ndarray],
    max_dim: int = 480,
) -> tuple[list[np.ndarray], list[float]] | None:
    """Run DeepLabV3 person segmentation in an available PyTorch runtime (auto-cleaned temp dir)."""
    py, env = _find_torch_runtime()
    if not py or not frames:
        return None
    helper = Path(__file__).resolve().parent / "tools" / "seg_person_torch.py"
    if not helper.is_file():
        return None

    h, w = frames[0].shape[:2]
    scale = min(1.0, float(max_dim) / max(h, w))
    sh, sw = max(32, int(round(h * scale))), max(32, int(round(w * scale)))
    ys = np.linspace(0, h - 1, sh).astype(int)
    xs = np.linspace(0, w - 1, sw).astype(int)
    batch = np.stack([f[np.ix_(ys, xs)] for f in frames], axis=0).astype(np.uint8)

    with tempfile.TemporaryDirectory() as tmp:
        in_npz = Path(tmp) / "frames.npz"
        out_npz = Path(tmp) / "masks.npz"
        np.savez(in_npz, frames=batch)
        proc = subprocess.run(
            [py, str(helper), "--input-npz", str(in_npz), "--output-npz", str(out_npz)],
            env=env,
            capture_output=True,
            timeout=90,
        )
        if proc.returncode != 0 or not out_npz.is_file():
            return None
        with np.load(out_npz) as res:
            masks = [res["masks"][i].astype(bool) for i in range(len(frames))]
            probs = [float(res["probs"][i]) for i in range(len(frames))]
        return masks, probs


def analyze_frames(
    frames: list[np.ndarray],
    masks: list[np.ndarray | None] | None = None,
    margin_sample_count: int = 8,
    target_type: str | None = None,
) -> dict[str, Any]:
    """Compute sharpness, orbit continuity, border margins, and optional ROI detail px."""
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
    lost_count = 0
    side_mins: dict[str, list[int]] = {"left": [], "right": [], "top": [], "bottom": []}
    roi_estimates: list[float] = []

    for idx in sample_idxs:
        mask = masks[idx] if masks is not None and idx < len(masks) else None
        res = estimate_foreground_margins(frames[idx], mask=mask, target_type=target_type)
        fg_ratios.append(res["fg_ratio"])
        if res["margins"] is None or res["fg_ratio"] <= 0.0:
            lost_count += 1
        else:
            for side, val in res["margins"].items():
                side_mins[side].append(val)
        if target_type == "head":
            eye_px = estimate_head_eye_detail_px(
                frames[idx],
                bbox_width_px=res["bbox_width_px"],
                fg_mask=res.get("head_mask") if mask is not None else None,
            )
            if eye_px is not None:
                roi_estimates.append(eye_px)
        elif target_type == "garment" and res["bbox_width_px"] > 0:
            roi_estimates.append(round(res["bbox_width_px"] * 0.08, 1))

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
        "fg_ratio_max": round(float(max(fg_ratios)), 4) if fg_ratios else None,
        "lost_subject_ratio": round(lost_count / len(sample_idxs), 3) if len(sample_idxs) else 0.0,
        "min_margin_px": min_margin_px,
        "roi_detail_px_est": round(float(np.median(roi_estimates)), 1) if roi_estimates else None,
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
            idx += 1
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
    person_seg: str = "auto",
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
        used_person_seg = False
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
            used_person_seg = any(m is not None for m in masks)
        elif target_type == "head" and person_seg in ("auto", "torch"):
            seg_res = segment_person_masks_torch(frames)
            if seg_res is not None:
                masks = seg_res[0]
                used_person_seg = True
            elif person_seg == "torch":
                raise RuntimeError("未找到可用的 PyTorch + torchvision 人像分割运行环境")

        frame_stats = analyze_frames(frames, masks=masks, target_type=target_type)
        payload["sharpness_p50"] = frame_stats["sharpness_p50"]
        payload["orbit_continuity_min"] = frame_stats["orbit_continuity_min"]
        if frame_stats["min_margin_px"] is not None:
            payload["min_margin_px"] = frame_stats["min_margin_px"]
        if used_person_seg:
            if frame_stats.get("fg_ratio_max") is not None:
                payload["subject_occupancy_max"] = frame_stats["fg_ratio_max"]
            if frame_stats.get("lost_subject_ratio") is not None:
                payload["lost_subject_ratio"] = frame_stats["lost_subject_ratio"]
        if roi_detail_px is None and frame_stats.get("roi_detail_px_est") is not None:
            payload["roi_detail_px"] = frame_stats["roi_detail_px_est"]
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
