"""Deterministic capture quality inspector + replaceable AI reshoot guide.

Design principles (aligned with recommender.py and docs/architecture.md):
- The geometric/visual metrics are computed deterministically from capture stats
  (12-sector azimuth coverage, camera roll deviation, edge margins, sharpness,
  orbit continuity, key ROI detail pixels, scale reference, close-ups).
- The LLM never invents missing 3D geometry or inspects raw personal photos.
  It only receives the structured diagnosis JSON and translates verified numeric
  gaps into prioritized, plain-language reshoot instructions.
- Strict JSON output validation guards against malformed or contradictory model
  outputs (e.g., claiming an invalid capture can be reused).
- mode=ai is returned only after a real, validated model call.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from recommender import (
    InvalidModelOutputError,
    RecommenderError,
    RecommenderTimeoutError,
    RecommenderUpstreamError,
)

SECTOR_COUNT = 12
SECTOR_DEG = 360 // SECTOR_COUNT

SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}
INVALIDATING_CODES = {
    "edge_cropped",
    "azimuth_severe_gap",
    "camera_roll_severe",
    "blur_severe",
}


def _merge_missing_sectors(missing_indices: list[int]) -> list[str]:
    """Merge consecutive 30-degree sector indices into readable degree arcs."""
    if not missing_indices:
        return []
    ordered = sorted(set(missing_indices))
    arcs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for idx in ordered[1:]:
        if idx == prev + 1:
            prev = idx
        else:
            arcs.append((start, prev))
            start = prev = idx
    arcs.append((start, prev))
    return [f"{s * SECTOR_DEG}°-{(e + 1) * SECTOR_DEG}°" for s, e in arcs]


def _largest_sector_gap_deg(covered_indices: set[int]) -> int:
    if not covered_indices:
        return 360
    if len(covered_indices) >= SECTOR_COUNT:
        return 0
    best = 0
    cur = 0
    for i in range(SECTOR_COUNT * 2):
        if (i % SECTOR_COUNT) not in covered_indices:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return min(best, SECTOR_COUNT) * SECTOR_DEG


def analyze_capture_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Deterministically detect capture data gaps from structured metrics."""
    target_type = metrics.get("target_type")
    if target_type not in ("head", "garment"):
        raise ValueError(f"不支持的 target_type：{target_type!r}")

    issues: list[dict[str, Any]] = []
    sector_summary: dict[str, Any] = {
        "sector_count": SECTOR_COUNT,
        "covered_sectors": None,
        "coverage_ratio": None,
        "missing_arcs": [],
        "largest_gap_deg": None,
    }

    # 1. Azimuth coverage (per-frame angles or total span)
    covered_azimuth = metrics.get("covered_azimuth_deg")
    azimuth_span = metrics.get("azimuth_span_deg")
    if covered_azimuth is not None:
        covered_set = {int((float(angle) % 360.0) // SECTOR_DEG) for angle in covered_azimuth}
        missing = [i for i in range(SECTOR_COUNT) if i not in covered_set]
        missing_arcs = _merge_missing_sectors(missing)
        largest_gap = _largest_sector_gap_deg(covered_set)
        sector_summary.update(
            {
                "covered_sectors": len(covered_set),
                "coverage_ratio": round(len(covered_set) / SECTOR_COUNT, 3),
                "missing_arcs": missing_arcs,
                "largest_gap_deg": largest_gap,
            }
        )
        if largest_gap >= 90 or len(covered_set) <= 6:
            issues.append(
                {
                    "code": "azimuth_severe_gap",
                    "severity": "critical",
                    "area": "环绕方位覆盖",
                    "detail": (
                        f"仅覆盖 {len(covered_set)}/{SECTOR_COUNT} 个 30° 扇区，"
                        f"最大空缺 {largest_gap}°（缺失区间：{', '.join(missing_arcs)}）"
                    ),
                    "value": largest_gap,
                }
            )
        elif missing:
            issues.append(
                {
                    "code": "azimuth_partial_gap",
                    "severity": "major",
                    "area": "环绕方位覆盖",
                    "detail": (
                        f"覆盖 {len(covered_set)}/{SECTOR_COUNT} 个扇区，"
                        f"缺失区间：{', '.join(missing_arcs)}"
                    ),
                    "value": largest_gap,
                }
            )
    elif azimuth_span is not None:
        span = max(0.0, min(360.0, float(azimuth_span)))
        gap = round(360.0 - span, 1)
        sector_summary["coverage_ratio"] = round(span / 360.0, 3)
        sector_summary["largest_gap_deg"] = gap
        if span < 240.0:
            issues.append(
                {
                    "code": "azimuth_severe_gap",
                    "severity": "critical",
                    "area": "环绕方位覆盖",
                    "detail": f"连续方位覆盖仅 {span:.1f}°，约 {gap:.1f}° 背面/侧后方完全缺失",
                    "value": span,
                }
            )
        elif span < 330.0:
            issues.append(
                {
                    "code": "azimuth_partial_gap",
                    "severity": "major",
                    "area": "环绕方位覆盖",
                    "detail": f"连续方位覆盖 {span:.1f}°，存在约 {gap:.1f}° 视角空缺",
                    "value": span,
                }
            )

    # 2. Camera up-axis roll consistency
    roll_deg = metrics.get("up_roll_median_deg")
    if roll_deg is not None:
        roll = abs(float(roll_deg))
        if roll > 45.0:
            issues.append(
                {
                    "code": "camera_roll_severe",
                    "severity": "critical",
                    "area": "相机姿态稳定性",
                    "detail": f"相机 up 轴翻滚中位偏差达 {roll:.1f}°（>45°），严重影响多视角表面约束",
                    "value": round(roll, 2),
                }
            )
        elif roll > 20.0:
            issues.append(
                {
                    "code": "camera_roll_warning",
                    "severity": "major",
                    "area": "相机姿态稳定性",
                    "detail": f"相机 up 轴翻滚中位偏差 {roll:.1f}°（>20°），建议保持水平稳定拍摄",
                    "value": round(roll, 2),
                }
            )

    # 3. Framing / edge cropping margin
    margins = metrics.get("min_margin_px")
    if isinstance(margins, dict) and margins:
        check_sides = ("left", "right", "top") if target_type == "head" else tuple(margins.keys())
        checked_vals = {k: margins[k] for k in check_sides if k in margins}
        cropped_sides = [k for k, v in checked_vals.items() if float(v) <= 5.0]
        tight_sides = [k for k, v in checked_vals.items() if 5.0 < float(v) < 40.0]
        if cropped_sides:
            issues.append(
                {
                    "code": "edge_cropped",
                    "severity": "critical",
                    "area": "画面留白与边界",
                    "detail": f"边缘已贴边/裁切（≤5 px）：{', '.join(f'{s}={checked_vals[s]}px' for s in cropped_sides)}",
                    "value": min(float(v) for v in checked_vals.values()),
                }
            )
        elif tight_sides:
            issues.append(
                {
                    "code": "edge_tight_margin",
                    "severity": "major",
                    "area": "画面留白与边界",
                    "detail": (
                        f"边缘留白不足（<40 px），存在裁切风险："
                        f"{', '.join(f'{s}={checked_vals[s]}px' for s in tight_sides)}"
                    ),
                    "value": min(float(v) for v in checked_vals.values()),
                }
            )

    # 3b. Subject framing occupancy & segmentation loss (head/person close-up overfill)
    occ_max = metrics.get("subject_occupancy_max")
    if occ_max is not None and float(occ_max) > 0.65:
        occ = float(occ_max)
        issues.append(
            {
                "code": "subject_too_close",
                "severity": "major",
                "area": "拍摄距离与构图",
                "detail": f"主体占画面高达 {occ * 100:.0f}%（>65%），距离过近易致头顶或耳侧出画",
                "value": round(occ, 3),
            }
        )

    lost_ratio = metrics.get("lost_subject_ratio")
    if lost_ratio is not None and float(lost_ratio) > 0.25:
        lr = float(lost_ratio)
        issues.append(
            {
                "code": "subject_lost_frames",
                "severity": "critical" if lr >= 0.5 else "major",
                "area": "主体追踪稳定性",
                "detail": f"约 {lr * 100:.0f}% 采样帧未检测到完整主体（偏离画面或贴得过近）",
                "value": round(lr, 3),
            }
        )

    # 4. Sharpness / motion blur
    sharpness = metrics.get("sharpness_p50")
    if sharpness is not None:
        sharp = float(sharpness)
        if sharp < 20.0:
            issues.append(
                {
                    "code": "blur_severe",
                    "severity": "critical",
                    "area": "画面清晰度",
                    "detail": f"清晰度中位数仅 {sharp:.1f}（<20），整体运动模糊或失焦",
                    "value": round(sharp, 2),
                }
            )
        elif sharp < 35.0:
            issues.append(
                {
                    "code": "blur_moderate",
                    "severity": "minor",
                    "area": "画面清晰度",
                    "detail": f"清晰度中位数偏低（{sharp:.1f} < 35），建议放慢移动速度并锁定对焦",
                    "value": round(sharp, 2),
                }
            )

    # 5. Orbit continuity
    continuity = metrics.get("orbit_continuity_min")
    if continuity is not None:
        cont = float(continuity)
        if cont < 0.35:
            issues.append(
                {
                    "code": "orbit_jump",
                    "severity": "major",
                    "area": "环绕轨迹连续性",
                    "detail": f"相邻帧最低相似度仅 {cont:.3f}（<0.35），局部移动过快或存在跳剪断点",
                    "value": round(cont, 4),
                }
            )

    # 6. Key ROI detail resolution (e.g., head eyes 2-3 px in round 65)
    roi_px = metrics.get("roi_detail_px")
    if roi_px is not None:
        px = float(roi_px)
        if target_type == "head":
            if px < 8.0:
                issues.append(
                    {
                        "code": "roi_resolution_critical",
                        "severity": "critical",
                        "area": "面部关键细节（眼部）",
                        "detail": (
                            f"单眼有效像素仅约 {px:.1f} px（<8 px），源数据高频信息不足，"
                            "后期贴图无法恢复真实眼形，必须近距离补拍"
                        ),
                        "value": round(px, 2),
                    }
                )
            elif px < 16.0:
                issues.append(
                    {
                        "code": "roi_resolution_low",
                        "severity": "major",
                        "area": "面部关键细节（眼部）",
                        "detail": f"单眼有效像素约 {px:.1f} px（<16 px），建议补充近景正斜脸特写",
                        "value": round(px, 2),
                    }
                )
        else:
            if px < 15.0:
                issues.append(
                    {
                        "code": "roi_resolution_low",
                        "severity": "major",
                        "area": "衣物结构细节（领口/袖口）",
                        "detail": f"关键结构像素仅约 {px:.1f} px（<15 px），建议 30–50 cm 近景补拍",
                        "value": round(px, 2),
                    }
                )

    # 7. Scale reference
    if metrics.get("has_scale_reference") is False:
        issues.append(
            {
                "code": "missing_scale_reference",
                "severity": "major",
                "area": "尺度参考",
                "detail": "缺少已知尺度参考（如 ArUco 标记、卷尺或地贴），无法做真实尺寸标定",
                "value": False,
            }
        )

    # 8. Frame registration ratio
    frame_count = metrics.get("frame_count")
    registered = metrics.get("registered_frames")
    if frame_count and registered is not None and int(frame_count) > 0:
        ratio = int(registered) / int(frame_count)
        if ratio < 0.3:
            issues.append(
                {
                    "code": "low_registration_ratio",
                    "severity": "critical",
                    "area": "相机配准率",
                    "detail": f"仅 {registered}/{frame_count} 帧成功配准（{ratio * 100:.1f}% < 30%）",
                    "value": round(ratio, 3),
                }
            )
        elif ratio < 0.7:
            issues.append(
                {
                    "code": "moderate_registration_ratio",
                    "severity": "major",
                    "area": "相机配准率",
                    "detail": f"{registered}/{frame_count} 帧成功配准（{ratio * 100:.1f}% < 70%）",
                    "value": round(ratio, 3),
                }
            )

    # 9. Missing close-ups
    missing_closeups = [str(item).strip() for item in (metrics.get("missing_closeups") or []) if str(item).strip()]
    if missing_closeups:
        issues.append(
            {
                "code": "missing_closeups",
                "severity": "major",
                "area": "关键部位近景",
                "detail": f"缺少以下部位的近景特写：{'、'.join(missing_closeups)}",
                "value": missing_closeups,
            }
        )

    issues.sort(key=lambda item: SEVERITY_RANK.get(item["severity"], 9))

    critical_codes = {item["code"] for item in issues if item["severity"] == "critical"}
    if not issues:
        readiness = "pass"
    elif (critical_codes & INVALIDATING_CODES) or len(critical_codes) >= 2:
        readiness = "invalid_retake"
    else:
        readiness = "needs_reshoot"

    return {
        "target_type": target_type,
        "subject_label": metrics.get("subject_label") or "",
        "readiness": readiness,
        "issue_count": len(issues),
        "sector_summary": sector_summary,
        "issues": issues,
    }


def _default_action_for_issue(issue: dict[str, Any], target_type: str) -> str:
    code = issue["code"]
    if code in ("azimuth_severe_gap", "azimuth_partial_gap"):
        if target_type == "head":
            return "摘掉反光眼镜、闭口保持头部静止，在 0.8–1.2 m 均匀柔光下 25–40 秒内匀速走完缺失角度（避开正对窗户强逆光）。"
        return "保持衣物垂坠褶皱固定，在柔和漫射光下绕行每 30° 补拍缺失角度，转角略停确保正/侧/背完整闭环。"
    if code in ("camera_roll_severe", "camera_roll_warning"):
        return "开启手机网格参考线并双手持稳，全程保持手机水平不翻滚，重新绕拍一圈。"
    if code in ("edge_cropped", "edge_tight_margin"):
        if target_type == "head":
            return "将镜头后撤至 0.9–1.2 米，确保头顶发冠与左右双耳四周留有 40 px 以上安全边距、不贴边裁切。"
        return "向后退半步或切换 1× 焦段，确保上下左右四周保留至少 80 px 留白，勿裁切衣摆/袖口/领口。"
    if code == "subject_too_close":
        return "镜头距离过近，向后退至 0.9–1.3 米使人物/主体完整处于取景框中央（占比控制在 35%–55%）。"
    if code == "subject_lost_frames":
        return "拍摄时保持主体始终位于取景框中央，匀速小步绕拍，切勿贴近至局部遮满镜头。"
    if code in ("blur_severe", "blur_moderate"):
        return "在均匀室内柔光下放慢环绕速度，双手持稳并在每个关键角度略停 0.5 秒待对焦稳定后再移动。"
    if code == "orbit_jump":
        return "降低相机移动速度，在拐角和侧背交界处加密停顿拍摄，避免画面跳变。"
    if code in ("roi_resolution_critical", "roi_resolution_low"):
        if target_type == "head":
            return "不佩戴反光眼镜且保持闭口静止，靠近至 40–60 cm 补充正面与左右 30° 侧脸眼部高清特写（避开硬阴影）。"
        return "靠近至 30–50 cm 对领口、肩线、袖口、腋下与下摆各补拍一正一斜两张清晰近景。"
    if code == "missing_scale_reference":
        return "在主体旁放置已知尺寸的卷尺或 A4 纸/ArUco 标定卡，补拍 1–2 张刻度清晰可见的参照图。"
    if code in ("low_registration_ratio", "moderate_registration_ratio"):
        return "避免强逆光与大面积纯黑死暗背景，锁定曝光/白平衡并增大相邻视角重叠度（每次转动不超过 20°–30°）。"
    if code == "missing_closeups":
        return f"补充缺失部位近景特写（{issue['detail']}），对焦点打在结构边缘线上。"
    return "按提示复查拍摄条件后补充采集。"


def mock_capture_guide(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Deterministic fallback guide when OOTD_AI_MODE=mock."""
    readiness = diagnosis["readiness"]
    target_type = diagnosis["target_type"]
    issues = diagnosis["issues"]

    if readiness == "pass":
        return {
            "summary": "当前采集质量达标，方位覆盖与细节指标均满足重建要求，可直接进入下一步处理（规则评估）。",
            "can_reuse_existing": True,
            "steps": [],
        }

    steps = []
    for idx, issue in enumerate(issues, start=1):
        steps.append(
            {
                "priority": idx,
                "target_area": issue["area"],
                "action": _default_action_for_issue(issue, target_type),
                "reason": issue["detail"],
            }
        )

    if readiness == "invalid_retake":
        summary = f"检测到 {len(issues)} 项质检问题且含严重缺陷，建议按优先级重新规范采集（规则评估）。"
        can_reuse = False
    else:
        summary = f"检测到 {len(issues)} 项局部缺口，保留现有素材的同时按以下步骤定向补拍即可（规则评估）。"
        can_reuse = True

    return {
        "summary": summary,
        "can_reuse_existing": can_reuse,
        "steps": steps,
    }


def build_capture_guide_prompt(diagnosis: dict[str, Any]) -> list[dict[str, str]]:
    """Build system/user messages for an OpenAI-compatible chat API."""
    system = (
        "你是「衣序 OOTD AI」的三维采集补拍指导助手。"
        "只根据输入 JSON 的客观缺口输出精简中文补拍方案，不编造数据。"
        "结合三维重建约束给具体动作：target_type=head 时若需重拍/补拍五官或环绕，应提醒摘掉反光眼镜、闭口静止、长发贴肩、"
        "全圈控在 25–40 秒减少活体晃动、避开正对窗户强逆光防后脑发丝发糊；"
        "target_type=garment 时应提醒均匀柔光、上下左右留白、转角稳步防跳帧。"
        "严格输出 JSON：{\"summary\":\"一句总结(<=40字)\",\"can_reuse_existing\":true/false,"
        "\"steps\":[{\"priority\":1,\"target_area\":\"部位\",\"action\":\"具体动作(<=45字)\",\"reason\":\"对应指标(<=25字)\"}]}。"
        "readiness 为 invalid_retake 时 can_reuse_existing 必须为 false；"
        "最多合并输出 4 条最高优先级步骤；只输出 JSON。"
    )
    user = json.dumps(diagnosis, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_guide_output(text: str, diagnosis: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Strictly validate the AI reshoot guide JSON against protocol and guardrails."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False, {"error": "模型输出不是合法 JSON"}
    if not isinstance(data, dict):
        return False, {"error": "模型输出必须是 JSON 对象"}

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, {"error": "模型输出缺少非空 summary"}

    can_reuse = data.get("can_reuse_existing")
    if not isinstance(can_reuse, bool):
        return False, {"error": "模型输出缺少布尔类型 can_reuse_existing"}

    if diagnosis["readiness"] == "invalid_retake" and can_reuse is True:
        return False, {"error": "质检判定为 invalid_retake，模型不得返回 can_reuse_existing=true"}

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        return False, {"error": "模型输出缺少数组类型 steps"}
    if diagnosis["readiness"] != "pass" and len(raw_steps) == 0:
        return False, {"error": "存在质检缺口时 steps 不能为空"}

    steps: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            return False, {"error": f"steps[{idx - 1}] 必须是对象"}
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
            return False, {"error": f"steps[{idx - 1}].priority 必须是正整数"}
        target_area = item.get("target_area")
        action = item.get("action")
        reason = item.get("reason")
        if not all(isinstance(v, str) and v.strip() for v in (target_area, action, reason)):
            return False, {"error": f"steps[{idx - 1}] 缺少非空 target_area / action / reason"}
        steps.append(
            {
                "priority": priority,
                "target_area": target_area.strip(),
                "action": action.strip(),
                "reason": reason.strip(),
            }
        )

    steps.sort(key=lambda s: s["priority"])
    return True, {
        "summary": summary.strip(),
        "can_reuse_existing": can_reuse,
        "steps": steps,
    }


@dataclass
class GuideCall:
    summary: str
    can_reuse_existing: bool
    steps: list[dict[str, Any]]
    latency_seconds: float
    usage: dict | None
    raw: str


class AICaptureGuide:
    """OpenAI-compatible adapter for translating capture diagnostics into reshoot steps."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 12.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)

    def generate(self, diagnosis: dict[str, Any]) -> GuideCall:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": build_capture_guide_prompt(diagnosis),
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
        except httpx.TimeoutException as exc:
            raise RecommenderTimeoutError(f"补拍指导调用超时（>{self.timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:
            raise RecommenderUpstreamError(f"补拍指导服务连接失败：{exc}") from exc
        latency = round(time.perf_counter() - started, 4)
        if response.status_code >= 400:
            raise RecommenderUpstreamError(f"模型服务返回 HTTP {response.status_code}")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise InvalidModelOutputError("模型返回缺少 choices/message/content 字段") from exc

        valid, result = validate_guide_output(content, diagnosis)
        if not valid:
            raise InvalidModelOutputError(result["error"])
        return GuideCall(
            summary=result["summary"],
            can_reuse_existing=result["can_reuse_existing"],
            steps=result["steps"],
            latency_seconds=latency,
            usage=usage,
            raw=content,
        )


def create_capture_guide_from_env() -> AICaptureGuide:
    base_url = os.environ.get("OOTD_AI_BASE_URL", "").strip()
    api_key = os.environ.get("OOTD_AI_API_KEY", "").strip()
    model = os.environ.get("OOTD_AI_MODEL", "").strip()
    if not (base_url and api_key and model):
        raise RecommenderError(
            "AI 模式未配置：缺少 OOTD_AI_BASE_URL / OOTD_AI_API_KEY / OOTD_AI_MODEL"
        )
    timeout = float(
        os.environ.get(
            "OOTD_CAPTURE_GUIDE_TIMEOUT_SECONDS",
            os.environ.get("OOTD_AI_TIMEOUT_SECONDS", "25"),
        )
    )
    return AICaptureGuide(base_url=base_url, api_key=api_key, model=model, timeout_seconds=timeout)
