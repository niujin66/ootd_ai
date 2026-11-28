# -*- coding: utf-8 -*-
"""Idempotently embed high-DPI figures into competition/*.docx and sync 11/27 status.

Constraints:
- Uses Python standard library (zipfile + xml.etree.ElementTree) + Pillow (for PNG dimensions).
- Preserves original styles, headers/footers, PR #49 tables (表 C/D/E), and keeps <w:sectPr>
  strictly as the last child of <w:body>.
- Fully idempotent: removes any prior section tagged with SECTION_MARKER before re-inserting.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
COMP_DIR = REPO_ROOT / "competition"
FIGURES_DIR = REPO_ROOT / "docs" / "figures"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# Common OOXML namespaces preserved on write
NAMESPACES = {
    "w": W_NS,
    "r": R_NS,
    "wp": WP_NS,
    "a": A_NS,
    "pic": PIC_NS,
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "wpi": "http://schemas.microsoft.com/office/word/2010/wordprocessingInk",
    "wne": "http://schemas.microsoft.com/office/word/2006/wordml",
}
for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)

SECTION_MARKER = "【2026-11-28 参赛收口图表与实证同步】"
SECTION_PREFIX_SUBSTR = "参赛收口图表与实证同步】"


def _qn(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _p_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(_qn(W_NS, "t")))


def make_text_paragraph(
    text: str,
    *,
    bold: bool = False,
    font_size_half_pt: int = 21,
    color_hex: str | None = None,
    center: bool = False,
    space_before: int = 80,
    space_after: int = 100,
) -> ET.Element:
    p = ET.Element(_qn(W_NS, "p"))
    pPr = ET.SubElement(p, _qn(W_NS, "pPr"))
    spacing = ET.SubElement(pPr, _qn(W_NS, "spacing"))
    spacing.set(_qn(W_NS, "before"), str(space_before))
    spacing.set(_qn(W_NS, "after"), str(space_after))
    if center:
        jc = ET.SubElement(pPr, _qn(W_NS, "jc"))
        jc.set(_qn(W_NS, "val"), "center")

    r = ET.SubElement(p, _qn(W_NS, "r"))
    rPr = ET.SubElement(r, _qn(W_NS, "rPr"))
    rFonts = ET.SubElement(rPr, _qn(W_NS, "rFonts"))
    rFonts.set(_qn(W_NS, "ascii"), "Microsoft YaHei")
    rFonts.set(_qn(W_NS, "eastAsia"), "Microsoft YaHei")
    rFonts.set(_qn(W_NS, "hAnsi"), "Microsoft YaHei")
    if bold:
        ET.SubElement(rPr, _qn(W_NS, "b"))
    sz = ET.SubElement(rPr, _qn(W_NS, "sz"))
    sz.set(_qn(W_NS, "val"), str(font_size_half_pt))
    szCs = ET.SubElement(rPr, _qn(W_NS, "szCs"))
    szCs.set(_qn(W_NS, "val"), str(font_size_half_pt))
    if color_hex:
        col = ET.SubElement(rPr, _qn(W_NS, "color"))
        col.set(_qn(W_NS, "val"), color_hex)

    t = ET.SubElement(r, _qn(W_NS, "t"))
    t.text = text
    return p


def make_image_paragraph(rid: str, doc_id: int, img_name: str, cx_emu: int, cy_emu: int) -> ET.Element:
    p = ET.Element(_qn(W_NS, "p"))
    pPr = ET.SubElement(p, _qn(W_NS, "pPr"))
    jc = ET.SubElement(pPr, _qn(W_NS, "jc"))
    jc.set(_qn(W_NS, "val"), "center")
    spacing = ET.SubElement(pPr, _qn(W_NS, "spacing"))
    spacing.set(_qn(W_NS, "before"), "120")
    spacing.set(_qn(W_NS, "after"), "60")

    r = ET.SubElement(p, _qn(W_NS, "r"))
    drawing = ET.SubElement(r, _qn(W_NS, "drawing"))
    inline = ET.SubElement(
        drawing,
        _qn(WP_NS, "inline"),
        {"distT": "0", "distB": "0", "distL": "0", "distR": "0"},
    )
    ET.SubElement(inline, _qn(WP_NS, "extent"), {"cx": str(cx_emu), "cy": str(cy_emu)})
    ET.SubElement(inline, _qn(WP_NS, "effectExtent"), {"l": "0", "t": "0", "r": "0", "b": "0"})
    ET.SubElement(inline, _qn(WP_NS, "docPr"), {"id": str(doc_id), "name": img_name})
    cNv = ET.SubElement(inline, _qn(WP_NS, "cNvGraphicFramePr"))
    ET.SubElement(cNv, _qn(A_NS, "graphicFrameLocks"), {"noChangeAspect": "1"})

    graphic = ET.SubElement(inline, _qn(A_NS, "graphic"))
    gdata = ET.SubElement(graphic, _qn(A_NS, "graphicData"), {"uri": PIC_NS})
    pic = ET.SubElement(gdata, _qn(PIC_NS, "pic"))

    nvPicPr = ET.SubElement(pic, _qn(PIC_NS, "nvPicPr"))
    ET.SubElement(nvPicPr, _qn(PIC_NS, "cNvPr"), {"id": str(doc_id), "name": img_name})
    cNvPicPr = ET.SubElement(nvPicPr, _qn(PIC_NS, "cNvPicPr"))
    ET.SubElement(cNvPicPr, _qn(A_NS, "picLocks"), {"noChangeAspect": "1"})

    blipFill = ET.SubElement(pic, _qn(PIC_NS, "blipFill"))
    ET.SubElement(blipFill, _qn(A_NS, "blip"), {_qn(R_NS, "embed"): rid, "cstate": "print"})
    stretch = ET.SubElement(blipFill, _qn(A_NS, "stretch"))
    ET.SubElement(stretch, _qn(A_NS, "fillRect"))

    spPr = ET.SubElement(pic, _qn(PIC_NS, "spPr"))
    xfrm = ET.SubElement(spPr, _qn(A_NS, "xfrm"))
    ET.SubElement(xfrm, _qn(A_NS, "off"), {"x": "0", "y": "0"})
    ET.SubElement(xfrm, _qn(A_NS, "ext"), {"cx": str(cx_emu), "cy": str(cy_emu)})
    prstGeom = ET.SubElement(spPr, _qn(A_NS, "prstGeom"), {"prst": "rect"})
    ET.SubElement(prstGeom, _qn(A_NS, "avLst"))

    return p


def _strip_previous_marker_section(body: ET.Element) -> None:
    children = list(body)
    start_idx = None
    for idx, child in enumerate(children):
        if child.tag == _qn(W_NS, "p") and SECTION_PREFIX_SUBSTR in _p_text(child):
            start_idx = idx
            break
    if start_idx is None:
        return
    for child in children[start_idx:]:
        if child.tag != _qn(W_NS, "sectPr"):
            body.remove(child)


def _replace_paragraph_text(body: ET.Element, target_substr: str, old_sub: str, new_sub: str) -> bool:
    changed = False
    for p in body.iter(_qn(W_NS, "p")):
        full = _p_text(p)
        if target_substr in full and old_sub in full:
            # Rebuild single clean run preserving paragraph formatting if present
            updated = full.replace(old_sub, new_sub)
            pPr = p.find(_qn(W_NS, "pPr"))
            first_r = p.find(_qn(W_NS, "r"))
            rPr = first_r.find(_qn(W_NS, "rPr")) if first_r is not None else None
            for ch in list(p):
                if ch is not pPr:
                    p.remove(ch)
            r = ET.SubElement(p, _qn(W_NS, "r"))
            if rPr is not None:
                r.append(rPr)
            t = ET.SubElement(r, _qn(W_NS, "t"))
            t.text = updated
            changed = True
    return changed


def update_docx(
    docx_path: Path,
    *,
    section_title: str,
    lead_paragraphs: list[str],
    figures: list[tuple[Path, str]],
    inline_replacements: list[tuple[str, str, str]] | None = None,
) -> dict[str, int]:
    """Update a single .docx archive idempotently."""
    with zipfile.ZipFile(docx_path, "r") as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}

    doc_root = ET.fromstring(entries["word/document.xml"])
    body = doc_root.find(_qn(W_NS, "body"))
    assert body is not None, f"Missing <w:body> in {docx_path}"

    # Apply inline text replacements first
    replaced_count = 0
    for needle, old_txt, new_txt in (inline_replacements or []):
        if _replace_paragraph_text(body, needle, old_txt, new_txt):
            replaced_count += 1

    # Remove prior generated block if re-run
    _strip_previous_marker_section(body)

    # Parse rels and clean prior generated media entries (ootd_fig_*)
    ET.register_namespace("", REL_NS)
    rels_root = ET.fromstring(entries["word/_rels/document.xml.rels"])
    for rel in list(rels_root):
        tgt = rel.attrib.get("Target", "")
        if tgt.startswith("media/ootd_fig_"):
            rels_root.remove(rel)
    for k in list(entries.keys()):
        if k.startswith("word/media/ootd_fig_"):
            del entries[k]

    existing_rids = set()
    for rel in rels_root:
        m = re.match(r"rId(\d+)", rel.attrib.get("Id", ""))
        if m:
            existing_rids.add(int(m.group(1)))
    next_rid_num = max(existing_rids | {50}) + 1

    # Ensure png Default exists in [Content_Types].xml
    ET.register_namespace("", CT_NS)
    ct_root = ET.fromstring(entries["[Content_Types].xml"])
    has_png_ct = any(
        el.tag == f"{{{CT_NS}}}Default" and el.attrib.get("Extension", "").lower() == "png"
        for el in ct_root
    )
    if not has_png_ct:
        ET.SubElement(ct_root, f"{{{CT_NS}}}Default", {"Extension": "png", "ContentType": "image/png"})
        entries["[Content_Types].xml"] = ET.tostring(ct_root, encoding="utf-8", xml_declaration=True)

    # Extract sectPr so we always append before it and put it last
    sect_pr = body.find(_qn(W_NS, "sectPr"))
    if sect_pr is not None:
        body.remove(sect_pr)

    # Insert marker + section heading + lead paragraphs + figure blocks
    body.append(
        make_text_paragraph(
            f"{SECTION_MARKER} {section_title}",
            bold=True,
            font_size_half_pt=26,
            color_hex="1E3A8A",
            space_before=240,
            space_after=120,
        )
    )
    for lp in lead_paragraphs:
        body.append(make_text_paragraph(lp, bold=False, font_size_half_pt=21, color_hex="1E293B"))

    # Max docPr id avoid collision
    doc_id = 100
    target_cx = int(round(5.85 * 914400))  # 5.85 inches wide (~14.86 cm) fits A4 margins cleanly
    for idx, (fig_path, caption) in enumerate(figures, start=1):
        with Image.open(fig_path) as im:
            w_px, h_px = im.size
        cy_emu = int(round(target_cx * (h_px / w_px)))
        rid = f"rId{next_rid_num}"
        next_rid_num += 1
        doc_id += 1
        media_rel_target = f"media/ootd_fig_{idx:02d}_{fig_path.stem[:18]}.png"
        entries[f"word/{media_rel_target}"] = fig_path.read_bytes()
        ET.SubElement(
            rels_root,
            f"{{{REL_NS}}}Relationship",
            {"Id": rid, "Type": IMAGE_REL_TYPE, "Target": media_rel_target},
        )
        body.append(make_image_paragraph(rid, doc_id, fig_path.name, target_cx, cy_emu))
        body.append(
            make_text_paragraph(
                caption,
                bold=True,
                font_size_half_pt=18,
                color_hex="475569",
                center=True,
                space_before=40,
                space_after=160,
            )
        )

    if sect_pr is not None:
        body.append(sect_pr)

    entries["word/document.xml"] = ET.tostring(doc_root, encoding="utf-8", xml_declaration=True)
    entries["word/_rels/document.xml.rels"] = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)

    # Atomic replace via temp file
    fd, tmp_name = tempfile.mkstemp(suffix=".docx", dir=str(docx_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for name, data in entries.items():
                zout.writestr(name, data)
        shutil.move(str(tmp_path), str(docx_path))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return {"replaced": replaced_count, "figures_added": len(figures)}


def main() -> None:
    fig1 = FIGURES_DIR / "fig1_system_architecture.png"
    fig2 = FIGURES_DIR / "fig2_v9_reconstruction_pipeline.png"
    fig3 = FIGURES_DIR / "fig3_capture_ablation_trajectory.png"
    fig4 = FIGURES_DIR / "fig4_ai_benchmark_30_20.png"
    fig5 = FIGURES_DIR / "fig5_modeling_comparison_tradeoff.png"
    fig6 = FIGURES_DIR / "fig6_reliability_engineering.png"
    fig7 = FIGURES_DIR / "fig7_incremental_reshoot_and_quality_gate.png"

    jobs = [
        (
            COMP_DIR / "待填-技术研究报告.docx",
            "核心管线流程、实测消融轨迹、纹理迁移 v2 与增量补拍实证",
            [
                "为支撑技术评审直观核验，本节集中补入系统 V9 重建管线架构、无引导 vs App 引导 COLMAP 轨迹消融四联图、top-003 建模路线与纹理迁移 v2 实测对比，以及 11/28 新增的“采集质控门控 + 无位姿增量补拍”闭环对比。",
                "截至 2026-11-28，A 侧已完成 V9 写实头部（head-v9-neckcut-aligned-1024.glb，3.36 MB）与躯干+长裤鞋拼装，生成 person-001_v2.glb（4.03 MB）、模板改形兜底长袖版 top-003.glb（1.08 MB，C_half=54.36 cm）及 Scan-to-Template v2 纹理迁移（2048px 轮廓 IoU=0.8105、PSNR +5.499 dB）；并在 top-003 留出 41 帧背面大缺口上验证了 13 帧无位姿 LightGlue+PnP 增量补拍：13/13 (100%) 配准，背面命中率 34.6% -> 91.1%，原清晰区漂移 0.0。",
            ],
            [
                (fig2, "附图 1：V9 真人头部与服装三维重建全链路及止损决策流程"),
                (fig3, "附图 2：无引导 vs App 实时引导消融实验真实 COLMAP 轨道与 2DGS 指标对比"),
                (fig5, "附图 3：三维服装建模路线实测对比、止损诊断与 fixture-face-04 体积构成"),
                (fig7, "附图 4：端侧采集质控升级与无位姿增量补拍闭环实测对比"),
            ],
            [],
        ),
        (
            COMP_DIR / "待填-作品创新性分析报告.docx",
            "三端协同架构图谱、引导轨迹实证、增量补拍闭环与真实大模型评测",
            [
                "以下四幅实证图谱分别对应本作品四大核心创新点：①「端-云-重建」闭环门禁与写实全身拼装架构；② App 12 扇区引导对 COLMAP 配准与 2DGS 新视角合成（+4.041 dB PSNR）的真实增益；③ 采集端曝光/防扭手腕门控 + AI 一键定向补拍与无位姿局部增量融合（背面命中 34.6% -> 91.1%）；④ 共享后端真实大模型（Hy4-flash）30+20 全量零违反语义交互评测。",
            ],
            [
                (fig1, "附图 1：OOTD AI 端-云-重建三端协同系统架构"),
                (fig3, "附图 2：无引导 vs App 实时引导真实 COLMAP 相机轨迹与 2DGS 渲染质量对比"),
                (fig7, "附图 3：端侧质控升级与无位姿“增量补拍”闭环实测对比"),
                (fig4, "附图 4：共享测试后端 30+20 AI 接口全量评测统计（Hy4-flash vs Mock）"),
            ],
            [],
        ),
        (
            COMP_DIR / "待填-项目开发文档.docx",
            "三端协同架构、V9 增量补拍流水线与端云可靠性闭环图谱",
            [
                "截至 2026-11-28，C 侧已部署共享测试后端（OOTD_AI_MODE=ai，Hy4-flash）并完成 30+20 真实模型全量压测（50/50 有效、0 违反）；A 侧已按合龙方案交出 fixture-face-04 三衣写实全身包（6.69 MB，4/4 clean），并在采集引导与重建端新增挂拍防遮挡、曝光/防扭手腕门控、AI 一键定向补拍（OOTD_patch_）及无位姿增量纹理优化脚本 refine_texture_with_reshoot.py。",
            ],
            [
                (fig1, "附图 1：OOTD AI 端-云-重建三端协同系统架构"),
                (fig2, "附图 2：V9 真人头部与服装三维重建开发流水线及全身拼装产出"),
                (fig7, "附图 3：端侧防扭/曝光门控与重建无位姿增量补拍融合流水线"),
                (fig6, "附图 4：端-云可靠性工程防线与发布治理闭环（E01–E04 / R01–R09）"),
            ],
            [
                (
                    "实际联网结果待测",
                    "实际联网结果待测。",
                    "已于 2026-11-27 在 C 侧共享测试后端接入真实大模型（Hy4-flash）完成 30+20 全量评测，有效率 100% 且条件违反 0 条（见测试文档表 C/D/E）。",
                ),
            ],
        ),
        (
            COMP_DIR / "待填-项目测试文档.docx",
            "可视化评测统计图、增量补拍定量实测与端云可靠性基线（修订 0.5）",
            [
                "说明：本节完整保留上方表 C / 表 D / 表 E 真实大模型（Hy4-flash）实测数据，并补入量化可视化图表与截至 11/28 的全量自动化测试与实测结论：",
                "• 全量自动化基线（2026-11-28）：后端 server/tests 105 项（104 通过、1 Windows 符号链接跳过、0 失败）+ 重建 reconstruction/tests 3 项全通过 + Android JUnitCore 14 类 83 项全通过，:app:assembleDebug 构建通过。",
                "• 增量补拍大缺口实测（2026-11-28）：top-003 留出 41 帧背面后，13 帧无位姿自动抠图补拍 100% 配准，背面命中 34.6% -> 91.1%（status=ok），新填补 97,862 纹素，原清晰区漂移 0.0。",
                "• 03b 双签闭合与 fixture-face-04 门禁就绪（2026-11-27）：person-001_v2 (4.03 MB) + 三衣合计 6.69 MB，export_glb_guard.py check 4/4 clean；导入时须显式加 --expected-garments 3。",
            ],
            [
                (fig4, "附图 1：共享测试后端 Hy4-flash 真实模型 vs Mock 基线 30+20 评测统计图"),
                (fig7, "附图 2：端侧质控升级与 top-003 无位姿增量补拍实测效果对比"),
                (fig6, "附图 3：端-云可靠性防护链与全量自动化测试基线（后端 105 / 重建 3 / Android 83）"),
            ],
            [
                (
                    "03b 两衣外观与切换中断后的真机独立验收待补",
                    "03b 两衣外观与切换中断后的真机独立验收待补。",
                    "03b 两衣四视角已于 2026-11-27 由 A 侧（11/24）与 C 侧（11/27）双签正式闭合通过；切换中断及 fixture-face-04 三衣真机验收随冻结节点收口。",
                ),
            ],
        ),
        (
            COMP_DIR / "待填-AI辅助编程交互记录文档.docx",
            "11 月 27–28 日收口协同记录：图谱生成、增量补拍闭环与文档同步",
            [
                "4. 从零实现零重依赖高清图谱生成与 OOXML 无损回填：团队协同 AI 编写纯 Pillow/NumPy 双倍超采样绘图工具读取真实 COLMAP 位姿、PR #49 实测统计、Issue #13 几何度量及 11/28 增量补拍对比实测生成 7 张脱敏高清配图，并以标准 OOXML 节点方式幂等写入五份参赛文档（严格保护原有页眉页脚及 PR #49 表 C/D/E 表格）。",
                "5. 端侧-重建闭环联动：AI 辅助设计“衣架挂拍 + 曝光/逆光/视差门控 + 三色扇区 + AI 一键定向补拍 -> SuperPoint+LightGlue+PnP 无位姿融合”完整闭环，全链路新增单测全部实跑通过。",
            ],
            [
                (fig4, "附图 1：AI 辅助生成的 30+20 接口评测统计可视化图"),
                (fig5, "附图 2：AI 辅助诊断后的建模路线对比与止损交付体量图"),
                (fig7, "附图 3：AI 辅助实现的采集质控升级与增量补拍闭环对比图"),
            ],
            [],
        ),
    ]

    for docx_path, stitle, leads, figs, reps in jobs:
        stats = update_docx(
            docx_path,
            section_title=stitle,
            lead_paragraphs=leads,
            figures=figs,
            inline_replacements=reps,
        )
        print(f"UPDATED {docx_path.name}: {stats}")


if __name__ == "__main__":
    main()
