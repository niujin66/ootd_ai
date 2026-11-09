"""Command line entry point for the OOTD AI asset package validator (task 3).

Usage:
    python validate_asset_package.py PACKAGE_DIR [--report out.json] [--expected-garments N]

Exit codes:
    0  package is valid and importable (automatic checks only)
    1  package failed validation
    2  usage error (package directory missing or unreadable)

The tool never modifies the input package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from asset_validation import TOOL_VERSION, validate_package

EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 OOTD AI 三维资产包（自动检查，不修改原资产）。",
    )
    parser.add_argument("package_dir", help="资产包目录（含 manifest.json 与 GLB 文件）")
    parser.add_argument(
        "--report",
        metavar="JSON",
        help="校验报告 JSON 输出路径；不指定则报告写入 stdout",
    )
    parser.add_argument(
        "--expected-garments",
        type=int,
        default=2,
        help="当前版本要求的衣物件数（默认 2）",
    )
    args = parser.parse_args(argv)

    package = Path(args.package_dir)
    if not package.is_dir():
        print(f"错误：资产包目录不存在：{package}", file=sys.stderr)
        return EXIT_USAGE

    report = validate_package(package, expected_garments=args.expected_garments)
    exit_code = EXIT_VALID if report.ok else EXIT_INVALID
    payload = report.as_dict()
    payload["exit_code"] = exit_code

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        try:
            Path(args.report).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"错误：无法写入报告文件 {args.report}：{exc}", file=sys.stderr)
            return EXIT_USAGE
        print(f"校验报告已写入：{args.report}")

    status = "通过（可导入）" if report.ok else "未通过"
    print(f"[asset-validator {TOOL_VERSION}] {package} => {status} (exit {exit_code})")
    for error in report.errors:
        print(f"  ERROR: {error}")
    for warning in report.warnings:
        print(f"  WARN:  {warning}")
    for asset in report.assets:
        asset_id = asset.get("asset_id") or f"#{asset.get('index')}"
        if asset.get("errors"):
            print(f"  --  {asset_id:12s} {asset['errors'][0]}")
        elif asset.get("sha256"):
            print(
                f"  OK  {asset_id:12s} {asset['size_bytes']:>10d} B  "
                f"sha256={asset['sha256'][:12]}…  v={asset['vertices']} t={asset['triangles']}"
            )
    if report.ok:
        print("提示：自动校验通过不代表本人相似度、尺寸真实性或穿模合格，见报告的 manual_checks。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())