# -*- coding: utf-8 -*-
"""Generate the 12-slide OOTD AI defense deck (.pptx) using pure Python stdlib + Pillow.

Zero external python-pptx dependency required: emits valid ECMA-376 PresentationML
(.pptx) XML with 16:9 widescreen layout (12192000 x 6858000 EMU), Microsoft YaHei
typography, and aspect-preserving embedded high-DPI figures from docs/figures/.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
FIGURES_DIR = REPO_ROOT / "docs" / "figures"
OUT_PPTX = REPO_ROOT / "competition" / "OOTD-AI-答辩PPT初稿-20261128.pptx"

SLIDE_W = 12192000  # 13.333 in (16:9)
SLIDE_H = 6858000   # 7.5 in
EMU_PER_PX = 9525   # 96 dpi logical grid: 1280 x 720

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _e(val_px: float) -> int:
    return int(round(val_px * EMU_PER_PX))


@dataclass
class SlideSpec:
    title: str
    subtitle: str
    page_tag: str
    is_cover: bool = False
    cards: list[tuple[float, float, float, float, str, str, list[str], str, str]] = field(default_factory=list)
    # card: (x_px, y_px, w_px, h_px, header_title, badge_text, bullets, accent_hex, bg_hex)
    figure: tuple[Path, float, float, float, float, str] | None = None
    # figure: (img_path, x_px, y_px, w_px, h_px, caption)
    footer_note: str = "衣序 OOTD AI · 端云协同高写实三维试衣与智能采集系统"


def _sp_text_box(
    sp_id: int,
    name: str,
    x: int,
    y: int,
    w: int,
    h: int,
    paragraphs: list[tuple[str, int, bool, str, str]],
    *,
    fill_hex: str | None = None,
    line_hex: str | None = None,
    line_w_pt: float = 1.0,
    round_rect: bool = False,
    anchor: str = "t",
    margins_px: tuple[int, int, int, int] = (10, 6, 10, 6),
    dash: bool = False,
) -> str:
    """Build a DrawingML shape (<p:sp>) with styled text paragraphs."""
    prst = "roundRect" if round_rect else "rect"
    av_lst = '<a:avLst><a:gd name="adj" fmla="val 4500"/></a:avLst>' if round_rect else "<a:avLst/>"
    fill_xml = f'<a:solidFill><a:srgbClr val="{fill_hex}"/></a:solidFill>' if fill_hex else "<a:noFill/>"
    if line_hex:
        dash_xml = '<a:prstDash val="dash"/>' if dash else ""
        ln_xml = (
            f'<a:ln w="{int(line_w_pt * 12700)}">'
            f'<a:solidFill><a:srgbClr val="{line_hex}"/></a:solidFill>'
            f"{dash_xml}"
            f"</a:ln>"
        )
    else:
        ln_xml = '<a:ln><a:noFill/></a:ln>'

    p_xml_parts: list[str] = []
    for text, pt_sz, bold, color_hex, align in paragraphs:
        b_attr = ' b="1"' if bold else ""
        sz_val = int(pt_sz * 100)
        safe = escape(text)
        p_xml_parts.append(
            f'<a:p><a:pPr algn="{align}" spcBef="400" spcAft="200"><a:lnSpc><a:spcPct val="118000"/></a:lnSpc></a:pPr>'
            f'<a:r><a:rPr lang="zh-CN" altLang="en-US" sz="{sz_val}"{b_attr} dirty="0">'
            f'<a:solidFill><a:srgbClr val="{color_hex}"/></a:solidFill>'
            f'<a:latin typeface="Microsoft YaHei"/>'
            f'<a:ea typeface="Microsoft YaHei"/>'
            f'<a:cs typeface="Microsoft YaHei"/>'
            f"</a:rPr><a:t>{safe}</a:t></a:r>"
            f'<a:endParaRPr lang="zh-CN" sz="{sz_val}"/></a:p>'
        )

    l_ins, t_ins, r_ins, b_ins = [_e(m) for m in margins_px]
    return (
        f'<p:sp>'
        f'<p:nvSpPr><p:cNvPr id="{sp_id}" name="{escape(name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr>'
        f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}">{av_lst}</a:prstGeom>'
        f"{fill_xml}{ln_xml}"
        f"</p:spPr>"
        f'<p:txBody><a:bodyPr wrap="square" anchor="{anchor}" lIns="{l_ins}" tIns="{t_ins}" rIns="{r_ins}" bIns="{b_ins}"/>'
        f"<a:lstStyle/>{''.join(p_xml_parts)}</p:txBody>"
        f"</p:sp>"
    )


def _pic_xml(sp_id: int, name: str, rid: str, x: int, y: int, w: int, h: int) -> str:
    return (
        f'<p:pic>'
        f'<p:nvPicPr><p:cNvPr id="{sp_id}" name="{escape(name)}"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:ln w="9525"><a:solidFill><a:srgbClr val="CBD5E1"/></a:solidFill></a:ln>'
        f'</p:spPr>'
        f'</p:pic>'
    )


def _fit_box(img_path: Path, box_x: int, box_y: int, box_w: int, box_h: int) -> tuple[int, int, int, int]:
    with Image.open(img_path) as im:
        iw, ih = im.size
    ratio = min(box_w / iw, box_h / ih)
    fw = int(round(iw * ratio))
    fh = int(round(ih * ratio))
    fx = box_x + (box_w - fw) // 2
    fy = box_y + (box_h - fh) // 2
    return fx, fy, fw, fh


def build_slides_spec() -> list[SlideSpec]:
    fig1 = FIGURES_DIR / "fig1_system_architecture.png"
    fig2 = FIGURES_DIR / "fig2_v9_reconstruction_pipeline.png"
    fig3 = FIGURES_DIR / "fig3_capture_ablation_trajectory.png"
    fig4 = FIGURES_DIR / "fig4_ai_benchmark_30_20.png"
    fig5 = FIGURES_DIR / "fig5_modeling_comparison_tradeoff.png"
    fig6 = FIGURES_DIR / "fig6_reliability_engineering.png"
    fig7 = FIGURES_DIR / "fig7_incremental_reshoot_and_quality_gate.png"

    return [
        # P1 Cover
        SlideSpec(
            title="衣序 OOTD AI —— 端云协同高写实三维试衣与智能采集系统",
            subtitle="手机普通视频采集  ×  V9 多先验 2DGS 三维重建  ×  局部定向补拍  ×  Filament 零闪烁渲染",
            page_tag="P1 / 12",
            is_cover=True,
            cards=[
                (70, 415, 270, 150, "端侧采集智能引导", "Android / CameraX", [
                    "• 两段式 12 扇区三色+橙罗盘",
                    "• 曝光/逆光与防扭手腕视差拦截",
                    "• AI 建议一键跳转局部定向补拍",
                ], "2563EB", "EFF6FF"),
                (365, 415, 270, 150, "V9 多先验高写实重建", "2DGS / 纹理迁移", [
                    "• SuperPoint+LightGlue 双阶段 SfM",
                    "• head-v9 3.36 MB · 全身 4.03 MB",
                    "• 无位姿增量补拍背面 34.6%→91.1%",
                ], "0D9488", "F0FDFA"),
                (660, 415, 270, 150, "双模 AI 导购与门禁", "FastAPI / Hy4-flash", [
                    "• Hy4-flash 30+20 评测 0 条件违反",
                    "• invalid_retake 严禁复用护栏",
                    "• SHA-256 + GlbSanitizer + Probe",
                ], "6D28D9", "F5F3FF"),
                (955, 415, 255, 150, "参赛信息（待定稿确认）", "汇报 / 交付", [
                    "• 赛题：移动端三维虚拟试衣",
                    "• 汇报人 / 团队：[待 12/7 确认]",
                    "• 日期：2026 年 12 月",
                ], "475569", "F8FAFC"),
            ],
        ),
        # P2 Pain points
        SlideSpec(
            title="P2  行业痛点与场景定义",
            subtitle="围绕普通用户用手机就能拍、就能穿的三大落地瓶颈，建立对应技术破局路径",
            page_tag="P2 / 12",
            cards=[
                (60, 140, 370, 470, "痛点 1：纸片感 2D 换装失真", "体验瓶颈", [
                    "• 传统 2D 贴图换衣缺乏真实人体立体轮廓，转体即破功，领口、袖口、下摆贴合无法直观检视。",
                    "• 【本方案破局】：采用真人头部 2DGS V9 重建 + 标准素体长裤鞋拼装全身（4.03 MB），支持 360° 实时绕行试穿。",
                    "• 实测三件上衣在正/侧/背/斜侧 4 视角均无明显穿模或悬空，支持丝滑切换。",
                ], "2563EB", "EFF6FF"),
                (455, 140, 370, 470, "痛点 2：随手乱拍致 3D 重建失败", "采集瓶颈", [
                    "• 用户常原地扭手腕、逆光或距离过近导致 COLMAP 断层、2DGS 糊片，且局部缺块后往往只能全部重拍。",
                    "• 【本方案破局】：CameraX 实时检测曝光/逆光与光流视差，用灰/黄/绿/橙扇区引导真绕行。",
                    "• 若模型仍现缺块，支持 AI 一键定向补拍 + 无位姿 LightGlue+PnP 局部修补（不用重拍整套）。",
                ], "0D9488", "F0FDFA"),
                (850, 140, 370, 470, "痛点 3：端侧大模型崩溃与错乱覆盖", "工程瓶颈", [
                    "• 移动端渲染对 GLB 体积和材质扩展极其敏感（KHR_materials_specular/sheen 易致 Filament 崩溃）；弱网拉包与切换异步冲突易导致黑屏或丢人。",
                    "• 【本方案破局】：设计「暂存 → SHA 校验 → 扩展净化 → 真解码探测 → 双 Ready 上屏 → 原子切换」闭环。",
                    "• 实测故障注入 E01–E04 / X01–X02 零黑屏、零崩溃。",
                ], "D97706", "FFFBEB"),
            ],
        ),
        # P3 Architecture (fig1)
        SlideSpec(
            title="P3  端-云-重建三端协同总体架构",
            subtitle="Android 前端采集渲染  ↔  FastAPI 门禁与双模 AI 网关  ↔  V9 离线三维重建与资产交付",
            page_tag="P3 / 12",
            cards=[
                (40, 118, 285, 530, "架构要点与契约边界", "系统分工", [
                    "1. Android 采集试衣端：",
                    "   Filament 引擎本地渲染，双状态切换护栏 + CameraX 1080p 实时质控引导。",
                    "2. FastAPI 网关（唯一在线服务）：",
                    "   Bearer 鉴权、双模推荐/补拍指导（Hy4-flash / Mock）、每日配额防刷。",
                    "3. 离线三维重建（GPU 工作站）：",
                    "   V9 SfM + 2DGS + Open3D 烘焙 + 无位姿补拍优化 + 止损兜底打包。",
                    "4. 资产门禁：",
                    "   所有产物经 export_glb_guard 净化并生成内容摘要与校验清单。",
                ], "1E3A8A", "F8FAFC"),
            ],
            figure=(fig1, 345, 114, 895, 540, "图 1：OOTD AI 端-云-重建三端协同架构"),
        ),
        # P4 V9 Pipeline (fig2)
        SlideSpec(
            title="P4  核心技术 1：V9 多先验融合三维重建管线",
            subtitle="SuperPoint + LightGlue 双阶段 SfM  ×  多模型语义几何先验  ×  2DGS 法向一致性烘焙",
            page_tag="P4 / 12",
            cards=[
                (40, 118, 285, 530, "关键工程突破", "head-v9 · 3.36 MB", [
                    "1. 双阶段 SuperPoint + LightGlue：",
                    "   远景定环绕骨架，近景精准锚定五官与领口细节，攻克传统 SIFT 在纯色皮肤/衣物上的断层。",
                    "2. 三先验融合剔除噪点：",
                    "   SegFormer + BiRefNet 扣净背景并剥离腕带，DA2 深度约束消除悬浮飞点。",
                    "3. 2048px 烘焙 + 颈裁拼装：",
                    "   仅重纹理面部（head-v9 3.36 MB），与标准素体长裤鞋无缝融合得 person-001_v2（4.03 MB）。",
                ], "0D9488", "F0FDFA"),
            ],
            figure=(fig2, 345, 114, 895, 540, "图 2：V9 真人头部与服装三维重建全链路"),
        ),
        # P5 Capture Ablation (fig3)
        SlideSpec(
            title="P5  创新实证 1：App 引导 vs 无引导消融实验",
            subtitle="同室内环境同目标实拍 60 帧等参对比（COLMAP + 2DGS 7k 步 · 720p 吃亏条件下仍全面反超）",
            page_tag="P5 / 12",
            cards=[
                (40, 118, 285, 530, "真实定量收益", "+4.041 dB PSNR", [
                    "1. 相机配准率跃升：",
                    "   80.0%（48/60）→ 91.7%（55/60），消除轨道断裂，稀疏 3D 点云 +49.7%（4,071 → 6,093）。",
                    "2. 构图更安全稳健：",
                    "   阶段 1 切边率由 90.2% 砍半至 50.0%，丢帧率由 2.4% 降至 0.0%。",
                    "3. 2DGS 新视角合成质量反超：",
                    "   PSNR：19.710 → 23.751 dB（+4.041 dB）；SSIM：+0.0252；LPIPS 感知失真降低 19.9%。",
                    "4. 针对消融定位问题已二次升级至 1080p + 防扭手腕门控。",
                ], "2563EB", "EFF6FF"),
            ],
            figure=(fig3, 345, 114, 895, 540, "图 3：真实 COLMAP 相机轨道与 2DGS 渲染质量消融对照"),
        ),
        # P6 Stop-loss & Texture Transfer v2 (fig5)
        SlideSpec(
            title="P6  核心技术 2：止损闸门、全身拼装与纹理迁移 v2",
            subtitle="面对穿身遮挡扫描残缺果断止损兜底，并用软蒙皮纹理迁移 v2 找回真实褶皱与印花",
            page_tag="P6 / 12",
            cards=[
                (40, 118, 285, 530, "务实止损与视觉保真", "4/4 GLB Clean", [
                    "1. 自动止损闸门：",
                    "   top-003 穿身扫描诊断出 1,021 碎块（厚度比 0.543），果断切换模板改形兜底（1.08 MB，C_half 54.36 cm）。",
                    "2. Scan-to-Template 纹理迁移 v2：",
                    "   纯几何袖筒软蒙皮临时下垂 72.0° + 躯干 ICP，轮廓 IoU 由 0.7482 升至 0.8105（8 视角 0.8234），90 分位误差降至 0.2045 m。",
                    "3. 对称布料借用 + 零几何漂移：",
                    "   补齐 89,388 素色纹素（防单侧印花误镜像），PSNR +5.499 dB。",
                ], "D97706", "FFFBEB"),
            ],
            figure=(fig5, 345, 114, 895, 540, "图 5：建模路线定量对照、止损诊断与 fixture-face-04 体积分布"),
        ),
        # P7 Incremental Reshoot & Quality Gate (fig7)
        SlideSpec(
            title="P7  创新升级：端侧质控与无位姿“增量补拍”闭环",
            subtitle="解决实拍遗漏或局部糊块痛点：App 一键定向补拍（OOTD_patch_）→ 无位姿自动抠图局部精修",
            page_tag="P7 / 12",
            cards=[
                (40, 118, 285, 530, "全自动补缺实测成果", "13/13 (100%)", [
                    "1. 源头防遮挡与质量门控：",
                    "   衣架挂拍推荐 + 曝光/逆光防抖 + 视差防扭手腕 + 灰/黄/绿/橙扇区。",
                    "2. AI 建议一键定向补拍：",
                    "   自动映射缺失与弱区橙格；invalid_retake 强制完整重拍。",
                    "3. top-003 留出 41 帧背面实测：",
                    "   • 13/13 无位姿配准（2.83–4.28 px）",
                    "   • 背面命中 34.6% → 91.1%（ok）",
                    "   • 新填补 97,862 px，原正面漂移 0.0。",
                ], "0D9488", "F0FDFA"),
            ],
            figure=(fig7, 345, 114, 895, 540, "图 7：端侧质控升级与无位姿“增量补拍”闭环实测对比"),
        ),
        # P8 AI Benchmark 30+20 (fig4)
        SlideSpec(
            title="P8  创新实证 2：真实大模型 30+20 全量评测",
            subtitle="共享后端 OOTD_AI_MODE=ai（Hy4-flash）实跑 50 条边界用例：协议 100% 有效、0 条件违反",
            page_tag="P8 / 12",
            cards=[
                (40, 118, 285, 530, "严守边界的实用 AI", "50 / 50 · 0 违反", [
                    "1. 穿搭推荐 30 条（Q04-ai）：",
                    "   覆盖 10 浅色 / 10 深色 / 7 无匹配 / 3 抗提示注入。目录越权 0 次，无匹配严格返回 no_match。",
                    "2. 采集补拍指导 20 条：",
                    "   pass 3 / invalid_retake 5 / needs_reshoot 12。readiness 命中 20/20，非法复用 0 次。",
                    "3. 与 Mock 毫秒基线诚实对照：",
                    "   Mock P50 3.87 / 3.61 ms，Hy4-flash P50 6.26 s / 8.66 s；设 12 s 熔断与离线降级兜底。",
                ], "6D28D9", "F5F3FF"),
            ],
            figure=(fig4, 345, 114, 895, 540, "图 4：共享测试后端 30+20 AI 接口全量评测统计"),
        ),
        # P9 Engineering Reliability (fig6)
        SlideSpec(
            title="P9  工程质量：端云可靠性与全量自动化测试基线",
            subtitle="暂存下载 → SHA-256 核验 → 扩展净化 → 解码探测 → 双 Ready 上屏 → 原子切换的零损坏链路",
            page_tag="P9 / 12",
            cards=[
                (40, 118, 285, 530, "191 项自动化回归护城河", "全链 0 失败", [
                    "1. 后端测试（server/tests）：",
                    "   105 项（104 通过，1 Windows 符号链接用例按环境跳过，0 失败）。",
                    "2. 重建与补拍（reconstruction/tests）：",
                    "   3 项端到端合成测试全通过（对齐 / 软蒙皮 / 增量补拍）。",
                    "3. Android 编译与单测（14 类）：",
                    "   83 项 JUnitCore 全通过（含推荐护栏、GLB 净化探测、视差/曝光/定向补拍），:app:assembleDebug 通过。",
                ], "059669", "ECFDF5"),
            ],
            figure=(fig6, 345, 114, 895, 540, "图 6：端-云可靠性工程防线与发布治理闭环"),
        ),
        # P10 Live Demo Script & Video Slot
        SlideSpec(
            title="P10  2 分 30 秒真机写实全身 Demo 演示",
            subtitle="一镜串联「写实全身试穿 → 3 衣平滑切换 → Hy4-flash 推荐/拒答 → 定向补拍与故障保护」",
            page_tag="P10 / 12",
            cards=[
                (55, 130, 560, 510, "演示分镜与现场亮点提示", "11/30 真机实录", [
                    "• 0:00–0:20 首页进入「三维试衣」：展示 4.03 MB 写实全身数字人毫秒级流畅旋转。",
                    "• 0:20–0:45 360° 转体 + 底部 3 衣横滑切换：验证零穿模与双 Ready 防闪烁。",
                    "• 0:45–1:25 AI 导购实战：『上班/优先浅色』准确推荐；『正式西装』触发 no_match 硬拒答不乱配。",
                    "• 1:25–2:05 3D 采集引导亮点：衣架挂拍 + 曝光/防扭手腕拦截 + 三色罗盘 + AI 一键跳转橙色定向补拍。",
                    "• 2:05–2:30 故障/离线自愈：断网冷启动秒恢复本地已校验资产指针。",
                ], "2563EB", "EFF6FF"),
                (645, 130, 580, 510, "【11/30 真机实录 MP4 视频嵌入位】", "待嵌入 Demo 视频/截图", [
                    "",
                    "        ▶  现场播放：2 分 30 秒真机录屏（1080p MP4）",
                    "",
                    "  • 左/上主窗：小米 / 华为真机试穿 3 衣 + AI 推荐录屏",
                    "  • 设备口径：Xiaomi 17 Pro + 共享测试后端（OOTD_AI_MODE=ai）",
                    "  • 备注：11/30 录制完成后将 MP4 与关键 4 视角截图插入此处",
                ], "475569", "F1F5F9"),
            ],
        ),
        # P11 Test Summary & Honest Boundaries
        SlideSpec(
            title="P11  测试总结与诚实工程边界声明",
            subtitle="坚持实测即所得：严格区分真实 AI / Mock 基线、模板改形兜底与扫描直出，如实披露遗留边界",
            page_tag="P11 / 12",
            cards=[
                (60, 132, 570, 505, "已闭合验收结论", "可交付依据", [
                    "• 03b 两衣真机四视角已由 A（11/24）与 C（11/27）正式双签闭合，content_digest 四来源逐字节一致。",
                    "• fixture-face-04（person-001_v2 + 3 件上衣，合计 6.69 MB）过 export_glb_guard 4/4 clean。",
                    "• Hy4-flash 真实大模型 30+20 全量实跑 50/50 有效、0 条件违反，并如实对比 Mock 毫秒基线。",
                    "• 全链路自动化测试合计 191 项（后端 105 + 重建 3 + Android 83）全部通过，0 失败。",
                ], "059669", "ECFDF5"),
                (655, 132, 565, 505, "诚实边界与收口待办（不遮掩不夸大）", "12/7 冻结前清零", [
                    "• 建模路线声明：top-003 为「扫描参考 + 模板改形兜底与多视角纹理迁移」，非原始穿身扫描网格直出。",
                    "• 遗留微贴合：03b 圆领/POLO 存在 2–4 mm 领圈微贴合，按团队拍板口径如实登记。",
                    "• 真机待补实测：fixture-face-04 三衣横向列表切换、APK arm64-v8a 瘦身（目标 < 40 MB）、5 次冷启动与峰值内存采样随 B/C 11/29–30 联调补齐。",
                ], "D97706", "FFFBEB"),
            ],
        ),
        # P12 Conclusion & Future Work
        SlideSpec(
            title="P12  核心成果总结与未来演进路线",
            subtitle="让普通手机用户低门槛拍得准、局部缺块一键补、端侧换装毫秒级零崩溃",
            page_tag="P12 / 12",
            cards=[
                (60, 134, 570, 500, "项目四大核心成果", "已落地实证", [
                    "1. 高写实低体量全身数字人：V9 写实头部（3.36 MB）+ 全身拼装（4.03 MB）+ 三衣稳态切换。",
                    "2. 数据驱动采集闭环：App 引导使 2DGS PSNR +4.041 dB，新增曝光/防扭门控与三色扇区。",
                    "3. 局部缺损「补拍即修」闭环：AI 一键跳转定向补拍 + 无位姿 LightGlue+PnP 融合使背面命中 34.6% → 91.1%。",
                    "4. 军工级端云门禁与双模 AI：SHA + 扩展净化 + 真解码探测 + 双 Ready 护栏 + Hy4-flash 零违反推荐。",
                ], "1E3A8A", "EFF6FF"),
                (655, 134, 565, 500, "后续演进规划与 Q&A", "欢迎专家批评指正", [
                    "• 短期（12/7 冻结前）：完成真机实录嵌入、arm64 瘦身与全部冷启动/换衣性能实测回填。",
                    "• 中期：支持裤装/配饰多槽位协同试穿与云端 GPU 异步队列自动化调度。",
                    "• 长期：结合衣架分离扫衣与轻量化生成先验，实现更精细织物物理悬垂与跨体型迁移。",
                    "",
                    "      感谢各位评委老师审阅！敬请提问交流（Q&A）",
                ], "0D9488", "F0FDFA"),
            ],
        ),
    ]


def _render_slide_xml(spec: SlideSpec) -> tuple[str, list[tuple[str, Path]]]:
    """Return slide XML string and list of (rId, image_path) media relationships."""
    shapes: list[str] = []
    media_rels: list[tuple[str, Path]] = []
    sp_id = 2

    # Subtle background rect
    bg_hex = "0F172A" if spec.is_cover else "F8FAFC"
    shapes.append(_sp_text_box(sp_id, "bg", 0, 0, SLIDE_W, SLIDE_H, [], fill_hex=bg_hex))
    sp_id += 1

    if spec.is_cover:
        # Cover top accent bar + hero banner
        shapes.append(_sp_text_box(sp_id, "accent", 0, 0, SLIDE_W, _e(12), [], fill_hex="2563EB"))
        sp_id += 1
        shapes.append(
            _sp_text_box(
                sp_id,
                "cover_hero",
                _e(70),
                _e(95),
                _e(1140),
                _e(270),
                [
                    ("衣序 OOTD AI", 40, True, "60A5FA", "l"),
                    ("端云协同高写实三维试衣与智能采集系统", 28, True, "FFFFFF", "l"),
                    (spec.subtitle, 15, False, "CBD5E1", "l"),
                ],
                fill_hex="1E293B",
                line_hex="334155",
                line_w_pt=1.25,
                round_rect=True,
                anchor="ctr",
                margins_px=(28, 18, 28, 18),
            )
        )
        sp_id += 1
    else:
        # Header bar
        shapes.append(_sp_text_box(sp_id, "hdr_bg", 0, 0, SLIDE_W, _e(96), [], fill_hex="0F172A"))
        sp_id += 1
        shapes.append(_sp_text_box(sp_id, "hdr_line", 0, _e(92), SLIDE_W, _e(4), [], fill_hex="2563EB"))
        sp_id += 1
        shapes.append(
            _sp_text_box(
                sp_id,
                "hdr_title",
                _e(40),
                _e(12),
                _e(1060),
                _e(76),
                [
                    (spec.title, 22, True, "FFFFFF", "l"),
                    (spec.subtitle, 12, False, "CBD5E1", "l"),
                ],
                margins_px=(4, 4, 4, 4),
            )
        )
        sp_id += 1
        shapes.append(
            _sp_text_box(
                sp_id,
                "page_pill",
                _e(1135),
                _e(24),
                _e(105),
                _e(38),
                [(spec.page_tag, 12, True, "93C5FD", "ctr")],
                fill_hex="1E293B",
                line_hex="3B82F6",
                round_rect=True,
                anchor="ctr",
            )
        )
        sp_id += 1

    # Cards
    for idx, (cx, cy, cw, ch, ctitle, cbadge, cbullets, accent_hex, cbg_hex) in enumerate(spec.cards):
        dash_box = "占位" in ctitle
        shapes.append(
            _sp_text_box(
                sp_id,
                f"card_{idx}",
                _e(cx),
                _e(cy),
                _e(cw),
                _e(ch),
                [],
                fill_hex=cbg_hex,
                line_hex=accent_hex,
                line_w_pt=1.5,
                round_rect=True,
                dash=dash_box,
            )
        )
        sp_id += 1
        shapes.append(
            _sp_text_box(
                sp_id,
                f"card_hdr_{idx}",
                _e(cx),
                _e(cy),
                _e(cw),
                _e(42),
                [(f"{ctitle}   [{cbadge}]", 13, True, "FFFFFF", "l")],
                fill_hex=accent_hex,
                anchor="ctr",
                margins_px=(14, 4, 14, 4),
            )
        )
        sp_id += 1
        paras = [
            (line if line else " ", 12 if len(spec.cards) > 1 or len(cbullets) > 5 else 13, False, "1E293B", "l")
            for line in cbullets
        ]
        shapes.append(
            _sp_text_box(
                sp_id,
                f"card_body_{idx}",
                _e(cx),
                _e(cy + 46),
                _e(cw),
                _e(ch - 52),
                paras,
                anchor="t",
                margins_px=(14, 8, 14, 8),
            )
        )
        sp_id += 1

    # Optional embedded figure
    if spec.figure is not None:
        img_path, fx, fy, fw, fh, cap = spec.figure
        if img_path.is_file():
            rid = "rId2"
            media_rels.append((rid, img_path))
            px, py, pw, ph = _fit_box(img_path, _e(fx), _e(fy), _e(fw), _e(fh - 26))
            shapes.append(_pic_xml(sp_id, f"fig_{img_path.stem}", rid, px, py, pw, ph))
            sp_id += 1
            shapes.append(
                _sp_text_box(
                    sp_id,
                    "fig_cap",
                    _e(fx),
                    _e(fy + fh - 26),
                    _e(fw),
                    _e(24),
                    [(cap, 10, True, "475569", "ctr")],
                    anchor="ctr",
                )
            )
            sp_id += 1

    # Footer
    footer_col = "94A3B8" if spec.is_cover else "64748B"
    shapes.append(
        _sp_text_box(
            sp_id,
            "footer",
            _e(40),
            _e(682),
            _e(1200),
            _e(28),
            [(spec.footer_note, 10, False, footer_col, "l")],
            anchor="ctr",
            margins_px=(0, 0, 0, 0),
        )
    )

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        f"{''.join(shapes)}"
        "</p:spTree></p:cSld>"
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        "</p:sld>"
    )
    return xml, media_rels


def build_pptx(out_path: Path = OUT_PPTX) -> Path:
    slides = build_slides_spec()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
        "</Relationships>"
    )

    theme_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="OOTDTheme">'
        '<a:themeElements>'
        '<a:clrScheme name="OOTD">'
        '<a:dk1><a:srgbClr val="0F172A"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="1E3A8A"/></a:dk2><a:lt2><a:srgbClr val="F8FAFC"/></a:lt2>'
        '<a:accent1><a:srgbClr val="2563EB"/></a:accent1><a:accent2><a:srgbClr val="0D9488"/></a:accent2>'
        '<a:accent3><a:srgbClr val="D97706"/></a:accent3><a:accent4><a:srgbClr val="6D28D9"/></a:accent4>'
        '<a:accent5><a:srgbClr val="059669"/></a:accent5><a:accent6><a:srgbClr val="DC2626"/></a:accent6>'
        '<a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="6D28D9"/></a:folHlink>'
        "</a:clrScheme>"
        '<a:fontScheme name="OOTDFonts">'
        '<a:majorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface="Microsoft YaHei"/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface="Microsoft YaHei"/></a:minorFont>'
        "</a:fontScheme>"
        '<a:fmtScheme name="OOTDFmt"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
        '<a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
        '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
        '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>'
        "</a:themeElements></a:theme>"
    )

    master_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        "</p:spTree></p:cSld>"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
        'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        "</p:sldMaster>"
    )
    master_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )

    layout_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
        '<p:cSld name="Blank"><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        "</p:spTree></p:cSld>"
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        "</p:sldLayout>"
    )
    layout_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )

    pres_sld_ids = []
    pres_rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
    ]
    ct_overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
    ]

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_rels/.rels", root_rels.encode("utf-8"))
        zf.writestr("ppt/theme/theme1.xml", theme_xml.encode("utf-8"))
        zf.writestr("ppt/slideMasters/slideMaster1.xml", master_xml.encode("utf-8"))
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels.encode("utf-8"))
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", layout_xml.encode("utf-8"))
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels.encode("utf-8"))

        seen_media: dict[Path, str] = {}
        for idx, spec in enumerate(slides, start=1):
            pres_rid = f"rId{idx + 1}"
            pres_sld_ids.append(f'<p:sldId id="{255 + idx}" r:id="{pres_rid}"/>')
            pres_rels.append(
                f'<Relationship Id="{pres_rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{idx}.xml"/>'
            )
            ct_overrides.append(
                f'<Override PartName="/ppt/slides/slide{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            )

            sld_xml, media_list = _render_slide_xml(spec)
            s_rels = [
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            ]
            for rid, img_path in media_list:
                if img_path not in seen_media:
                    media_name = f"media/{img_path.name}"
                    seen_media[img_path] = media_name
                    zf.writestr(f"ppt/{media_name}", img_path.read_bytes())
                else:
                    media_name = seen_media[img_path]
                s_rels.append(
                    f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../{media_name}"/>'
                )

            zf.writestr(f"ppt/slides/slide{idx}.xml", sld_xml.encode("utf-8"))
            zf.writestr(
                f"ppt/slides/_rels/slide{idx}.xml.rels",
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f"{''.join(s_rels)}</Relationships>"
                ).encode("utf-8"),
            )

        pres_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            f'<p:sldIdLst>{"".join(pres_sld_ids)}</p:sldIdLst>'
            f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}" type="screen16x9"/>'
            '<p:notesSz cx="6858000" cy="9144000"/>'
            "</p:presentation>"
        )
        zf.writestr("ppt/presentation.xml", pres_xml.encode("utf-8"))
        zf.writestr(
            "ppt/_rels/presentation.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f"{''.join(pres_rels)}</Relationships>"
            ).encode("utf-8"),
        )
        ct_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-officedocument.presentationml.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            f"{''.join(ct_overrides)}</Types>"
        )
        zf.writestr("[Content_Types].xml", ct_xml.encode("utf-8"))

    return out_path


def verify_pptx(pptx_path: Path) -> dict[str, Any]:
    """Verify ZIP readability, well-formed XML in every entry, and zero dangling slide relationships."""
    with zipfile.ZipFile(pptx_path, "r") as zf:
        names = set(zf.namelist())
        xml_checked = 0
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                ET.fromstring(zf.read(name))
                xml_checked += 1

        slides = sorted(n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        media_links = 0
        for sp in slides:
            rel_name = sp.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
            assert rel_name in names, f"Missing rels {rel_name}"
            rel_root = ET.fromstring(zf.read(rel_name))
            for rel in rel_root:
                tgt = rel.attrib.get("Target", "")
                if tgt.startswith("../"):
                    resolved = "ppt/" + tgt[3:]
                    assert resolved in names, f"Dangling target {resolved} in {rel_name}"
                    if "/media/" in resolved:
                        media_links += 1

    return {
        "slides": len(slides),
        "xml_checked": xml_checked,
        "media_links": media_links,
        "size_mb": round(pptx_path.stat().st_size / 1048576.0, 3),
    }


def main() -> None:
    out = build_pptx(OUT_PPTX)
    stats = verify_pptx(out)
    print("GENERATED PPTX:", out)
    print("VERIFY STATS:", stats)


if __name__ == "__main__":
    main()
