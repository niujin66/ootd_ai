"""Asset package validation for the OOTD AI server (task 3).

Follows the package and manifest conventions in docs/architecture.md (§5):
a package directory contains ``manifest.json`` plus the GLB files it
references.  This module performs the *automatic, machine-checkable*
validation only.  Human checks — person similarity, size truthfulness,
garment fitting and clipping — are listed in ``manual_checks`` and are
explicitly NOT verified here.

The validator never modifies the input package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import numpy as np
import trimesh

SUPPORTED_SCHEMA_VERSION = 1
TOOL_VERSION = "0.1.0"

REQUIRED_MANIFEST_FIELDS = (
    "schema_version",
    "person_id",
    "catalog_version",
    "pose_id",
    "assets",
)
REQUIRED_ASSET_FIELDS = ("asset_type", "asset_id", "relative_file", "source")
GARMENT_REQUIRED_FIELDS = ("display_name", "available")
ALLOWED_ASSET_TYPES = ("person", "garment")

# Generous sanity bounds.  Violations are reported as warnings only: the tool
# must not turn an unproven threshold into a verdict about the real person.
MAX_PLAUSIBLE_AXIS_METERS = 10.0
MIN_PLAUSIBLE_AXIS_METERS = 1e-3

MANUAL_CHECKS = [
    "本人相似度：需本人确认面部、头身比与体形能对应本人输入",
    "尺寸真实性：需真实身高数据对照；工具只报告异常范围",
    "穿模与贴合：需人物与两件上衣在同一场景做正/侧/背面检查",
]

_UNSAFE_PART = re.compile(r"[^0-9A-Za-z._\-]+")


@dataclass
class ValidationReport:
    """Result of validating one asset package."""

    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    assets: list[dict] = field(default_factory=list)
    manual_checks: list[str] = field(default_factory=lambda: list(MANUAL_CHECKS))
    tool_version: str = TOOL_VERSION
    input_dir: str = ""

    def fail(self, *messages: str) -> None:
        self.ok = False
        self.errors.extend(messages)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tool_version": self.tool_version,
            "input_dir": self.input_dir,
            "errors": self.errors,
            "warnings": self.warnings,
            "manifest": self.manifest,
            "assets": self.assets,
            "manual_checks": self.manual_checks,
        }


def _is_safe_relative_path(relative_file: object) -> tuple[bool, str | None]:
    """Check a manifest ``relative_file`` value without touching the disk.

    Rejects empty values, absolute paths (POSIX root, Windows drive/UNC,
    including backslash variants), directory traversal and any path part
    outside the conservative ``[0-9A-Za-z._-]`` character set.
    """
    if not isinstance(relative_file, str) or not relative_file.strip():
        return False, "relative_file 为空或不是字符串"
    text = relative_file.replace("\\", "/").strip()
    if text.startswith("/") or text.startswith("//"):
        return False, "绝对路径"
    if re.match(r"^[A-Za-z]:", text):
        return False, "绝对路径（盘符）"
    candidate = PurePosixPath(text)
    parts = candidate.parts
    if not parts:
        return False, "空路径"
    for part in parts:
        if part == "..":
            return False, "目录穿越（..）"
        if part in ("", "."):
            continue
        if ":" in part:
            return False, f"路径含非法字符（:）于 {part!r}"
        if _UNSAFE_PART.search(part):
            return False, f"路径含非法字符于 {part!r}"
    return True, None


def _resolved_within(package_root: Path, candidate: Path) -> bool:
    """True when ``candidate`` is inside (or equal to) ``package_root``.

    A plain ``commonpath`` equality check is not sufficient: when ``candidate``
    is a file under a *sibling* directory of the root, their common path is
    exactly the root itself.  Use the strict prefix rule instead.
    """
    try:
        root_text = os.path.normcase(os.path.abspath(str(package_root)))
        candidate_text = os.path.normcase(os.path.abspath(str(candidate)))
    except (ValueError, OSError):
        return False
    return candidate_text == root_text or candidate_text.startswith(root_text + os.sep)


def _load_glb(path: Path) -> tuple[trimesh.Trimesh | None, str | None]:
    """Parse a GLB into a single Trimesh, or return a failure message."""
    try:
        loaded = trimesh.load(path, process=False)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a validation error
        return None, f"无法解析 GLB：{exc}"
    if isinstance(loaded, trimesh.Scene):
        meshes = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not meshes:
            return None, "GLB 场景中没有任何三角网格"
        if len(meshes) == 1:
            loaded = meshes[0]
        else:
            loaded = trimesh.util.concatenate(meshes)
    if not isinstance(loaded, trimesh.Trimesh):
        return None, "GLB 顶层不是网格或场景"
    return loaded, None


def _check_geometry(mesh: trimesh.Trimesh) -> tuple[dict | None, list[str], list[str]]:
    """Validate finite, non-empty, well-referenced geometry.

    Returns (geometry_record, errors, warnings).  Extreme axis extents are
    warnings only.
    """
    errors: list[str] = []
    warnings: list[str] = []
    vertices = getattr(mesh, "vertices", None)
    if vertices is None or len(vertices) == 0:
        return None, ["几何为空（无顶点）"], warnings
    arr = np.asarray(vertices)
    if not np.isfinite(arr).all():
        return None, ["几何包含非有限坐标（NaN/Inf）"], warnings
    minimum = arr.min(axis=0)
    maximum = arr.max(axis=0)
    bounds = [minimum.tolist(), maximum.tolist()]
    spans = maximum - minimum
    for label, span in zip("XYZ", spans):
        if span < MIN_PLAUSIBLE_AXIS_METERS or span > MAX_PLAUSIBLE_AXIS_METERS:
            warnings.append(
                f"几何 {label} 轴尺寸 {span:.4f} m 超出常规范围，需人工核对尺寸真实性"
            )
    faces = getattr(mesh, "faces", None)
    triangles = 0
    if faces is not None and len(faces):
        triangles = int(len(faces))
        if int(faces.min()) < 0 or int(faces.max()) >= len(arr):
            errors.append("面索引越界")
    else:
        errors.append("网格无三角形面")
    return (
        {"vertices": int(len(arr)), "triangles": triangles, "bounds": bounds},
        errors,
        warnings,
    )


def validate_package(package_dir: str | os.PathLike, expected_garments: int = 2) -> ValidationReport:
    """Validate one asset package directory.  Never writes into the package."""
    given = Path(package_dir)
    report = ValidationReport(input_dir=str(given))
    try:
        root = given.resolve()
    except OSError:
        report.fail(f"资产包目录无法解析：{given}")
        return report
    if not root.is_dir():
        report.fail(f"资产包目录不存在：{given}")
        return report

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        report.fail("缺少 manifest.json")
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.fail(f"manifest.json 不是有效 JSON：{exc}")
        return report
    if not isinstance(manifest, dict):
        report.fail("manifest.json 顶层必须是 JSON 对象")
        return report
    report.manifest = manifest

    for key in manifest:
        if key not in REQUIRED_MANIFEST_FIELDS:
            report.warnings.append(f"manifest 未知字段（忽略）：{key}")
    for field_name in REQUIRED_MANIFEST_FIELDS:
        if field_name not in manifest:
            report.fail(f"缺少必需字段：{field_name}")

    schema = manifest.get("schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        report.fail(f"不支持的 schema_version：{schema!r}（当前支持 {SUPPORTED_SCHEMA_VERSION}）")

    for field_name in ("person_id", "catalog_version", "pose_id"):
        value = manifest.get(field_name)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            report.fail(f"{field_name} 必须为 1–64 位字母、数字、下划线或短横线，且以字母或数字开头")

    assets = manifest.get("assets")
    if not isinstance(assets, list):
        report.fail("assets 必须是数组")
        assets = []

    seen_ids: dict[str, int] = {}
    seen_files: dict[Path, str] = {}
    garment_count = 0
    person_count = 0

    for index, asset in enumerate(assets):
        record: dict = {
            "index": index,
            "asset_id": None,
            "asset_type": None,
            "relative_file": None,
            "errors": [],
            "warnings": [],
            "size_bytes": None,
            "sha256": None,
            "vertices": None,
            "triangles": None,
            "bounds": None,
        }
        if not isinstance(asset, dict):
            report.fail(f"assets[{index}] 必须是对象")
            record["errors"].append("assets 元素不是对象")
            report.assets.append(record)
            continue

        for key in asset:
            if key not in REQUIRED_ASSET_FIELDS + GARMENT_REQUIRED_FIELDS + ("tags",):
                record["warnings"].append(f"资产 {index} 未知字段（忽略）：{key}")
        for field_name in REQUIRED_ASSET_FIELDS:
            if field_name not in asset:
                report.fail(f"assets[{index}] 缺少必需字段：{field_name}")

        asset_type = asset.get("asset_type")
        asset_id = asset.get("asset_id")
        relative_file = asset.get("relative_file")
        record.update(
            asset_id=asset_id,
            asset_type=asset_type,
            relative_file=relative_file,
        )

        if asset_type not in ALLOWED_ASSET_TYPES:
            report.fail(f"assets[{index}] asset_type 不支持：{asset_type!r}（允许 {'/'.join(ALLOWED_ASSET_TYPES)}）")
        if not isinstance(asset_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", asset_id):
            report.fail(f"assets[{index}] asset_id 格式无效")
        else:
            if asset_id in seen_ids:
                report.fail(f"重复的 asset_id：{asset_id}")
            seen_ids[asset_id] = index

        if asset_type == "garment":
            garment_count += 1
            for field_name in GARMENT_REQUIRED_FIELDS:
                if field_name not in asset:
                    report.fail(f"衣物资产 {asset_id or index} 缺少字段：{field_name}")
            display_name = asset.get("display_name")
            if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()):
                report.fail(f"衣物资产 {asset_id or index} display_name 必须是非空字符串")
            available = asset.get("available")
            if available is not None and not isinstance(available, bool):
                report.fail(f"衣物资产 {asset_id or index} available 必须是布尔值")
            if "tags" in asset:
                tags = asset["tags"]
                if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                    record["warnings"].append("tags 应为字符串数组（按原样接受，建议修正）")
            else:
                record["warnings"].append("衣物缺少 tags（推荐功能需要颜色/风格标签）")
        elif asset_type == "person":
            person_count += 1
            person_id = manifest.get("person_id")
            if person_id is not None and asset.get("asset_id") != person_id:
                report.fail(
                    f"人物资产 asset_id {asset.get('asset_id')!r} 与 manifest person_id {person_id!r} 不一致"
                )

        # ---- file-level checks ----
        if relative_file is None:
            record["errors"].append("relative_file 缺失")
            report.assets.append(record)
            continue
        safe, reason = _is_safe_relative_path(relative_file)
        if not safe:
            report.fail(f"资产 {asset_id or index} 路径不安全：{reason}（{relative_file!r}）")
            record["errors"].append(f"路径不安全：{reason}")
            report.assets.append(record)
            continue
        if PurePosixPath(relative_file.replace("\\", "/")).suffix.lower() != ".glb":
            report.fail(f"资产 {asset_id or index} 不是 GLB 文件：{relative_file!r}")
            record["errors"].append("不是 .glb 文件")
            report.assets.append(record)
            continue

        target = root / relative_file.replace("\\", "/")
        try:
            resolved = target.resolve()
        except OSError as exc:
            report.fail(f"资产 {asset_id or index} 路径无法解析：{exc}")
            record["errors"].append("路径无法解析")
            report.assets.append(record)
            continue
        if not _resolved_within(root, resolved):
            report.fail(f"资产 {asset_id or index} 路径解析后逃出资产包目录：{relative_file!r}")
            record["errors"].append("路径逃出资产包目录")
            report.assets.append(record)
            continue
        real_root = Path(os.path.realpath(root))
        real_target = Path(os.path.realpath(resolved))
        if not _resolved_within(real_root, real_target):
            report.fail(f"资产 {asset_id or index} 符号链接/junction 指向资产包外：{relative_file!r}")
            record["errors"].append("符号链接/junction 指向包外")
            report.assets.append(record)
            continue
        if not resolved.is_file():
            report.fail(f"资产 {asset_id or index} 文件缺失：{relative_file}")
            record["errors"].append("文件缺失")
            report.assets.append(record)
            continue
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            report.fail(f"资产 {asset_id or index} 无法读取：{exc}")
            record["errors"].append("无法读取")
            report.assets.append(record)
            continue
        record["size_bytes"] = len(content)
        record["sha256"] = hashlib.sha256(content).hexdigest()
        if resolved in seen_files:
            record["warnings"].append(f"多个资产引用同一文件：{relative_file}")
        seen_files[resolved] = asset_id or str(index)

        mesh, load_error = _load_glb(resolved)
        if load_error is not None:
            report.fail(f"资产 {asset_id or index}（{relative_file}）：{load_error}")
            record["errors"].append(load_error)
            report.assets.append(record)
            continue
        geometry, geometry_errors, geometry_warnings = _check_geometry(mesh)
        record["warnings"].extend(geometry_warnings)
        report.warnings.extend(geometry_warnings)
        if geometry_errors:
            for message in geometry_errors:
                report.fail(f"资产 {asset_id or index}（{relative_file}）：{message}")
            record["errors"].extend(geometry_errors)
        if geometry is not None:
            record.update(
                vertices=geometry["vertices"],
                triangles=geometry["triangles"],
                bounds=geometry["bounds"],
            )
        report.warnings.extend(record["warnings"])
        report.assets.append(record)

    if person_count != 1:
        report.fail(f"必须恰好包含 1 个人物资产，实际 {person_count} 个")
    if garment_count != expected_garments:
        report.fail(f"当前版本要求恰好 {expected_garments} 件衣物，实际 {garment_count} 件")

    return report
