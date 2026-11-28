package com.ootd.ai.capture

import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * 采集目标类型，对齐服务端 POST /v1/capture/guide 的 target_type 枚举。
 */
enum class CaptureTargetType(val apiValue: String, val displayLabel: String) {
    HEAD("head", "头部 / 人像采集"),
    GARMENT("garment", "衣物 3D 采集");

    companion object {
        fun fromApiValue(value: String): CaptureTargetType {
            return entries.firstOrNull { it.apiValue == value }
                ?: throw IllegalArgumentException("未知的采集目标类型：$value")
        }
    }
}

/**
 * 两段式“由粗到细（Coarse-to-Fine）”实时拍摄状态机。
 */
enum class CaptureStage(val title: String) {
    PREPARE("开拍前准备"),
    STAGE_1_OVERVIEW("阶段 1 · 远景定骨架"),
    STAGE_2_CLOSEUP("阶段 2 · 慢推近补细节"),
    ANALYZING("汇总质检与请求指引"),
    RESULT("质检结果与补拍指引")
}

/**
 * 实时红绿灯状态：引导用户立即修正姿态、距离或速度。
 */
enum class TrafficLight {
    GREEN,
    YELLOW,
    RED
}

/**
 * 四边最小留白像素（px）。
 */
data class MarginPx(
    val left: Int,
    val right: Int,
    val top: Int,
    val bottom: Int
)

/**
 * 衣物采集拍摄方式：衣架挂拍无手臂遮挡（推荐），穿身拍摄需提醒露出侧缝。
 */
enum class GarmentCapturePosture(val label: String, val tip: String) {
    HANGER("衣架挂拍（推荐）", "衣架悬空挂拍，360° 无手臂遮挡侧缝与袖筒"),
    WORN("穿身拍摄", "请微抬或张开双臂露出侧缝，勿叠穿深色长袖内搭")
}

/**
 * 已生成模型常见缺陷部位与对应的 12 扇区定向补拍区。
 */
enum class ModelDefectRegion(
    val label: String,
    val targetSectors: Set<Int>,
    val guideHint: String
) {
    FRONT("正面 / 胸口", setOf(0, 1, 11), "正对前胸 0° 附近慢速移动，保持清晰对焦"),
    RIGHT_SEAM("右侧缝 / 腋下", setOf(2, 3, 4), "抬起右臂或挂拍露出右侧缝，慢拍右侧 60°–150°"),
    BACK("背面 / 印花", setOf(5, 6, 7), "绕到正后方 150°–240° 均匀采光慢拍"),
    LEFT_SEAM("左侧缝 / 腋下", setOf(8, 9, 10), "抬起左臂或挂拍露出左侧缝，慢拍左侧 240°–330°"),
    SLEEVES("双袖筒 / 袖口", setOf(2, 3, 9, 10), "撑开或挂平袖筒，重点补拍左右 60°–120° 与 240°–300°"),
    COLLAR("领口 / 肩线", setOf(0, 1, 11), "略俯拍 15° 并推近领口与肩线，拍清走线细节")
}

/**
 * 已有模型需要补拍优化的问题类型。
 */
enum class ModelDefectKind(val label: String) {
    MISSING_PATCH("缺块 / 遮挡"),
    BLURRY_OR_DARK("模糊 / 发暗")
}

/**
 * 开拍前尺度与基础属性记录。
 * 身高/性别由用户输入或标定物辅助，不靠单目摄像头瞎猜绝对身高。
 */
data class CapturePrepConfig(
    val targetType: CaptureTargetType = CaptureTargetType.HEAD,
    val subjectLabel: String = "person-001",
    val heightCmInput: String = "",
    val genderNote: String = "未填写",
    val hasScaleReference: Boolean = false,
    val garmentPosture: GarmentCapturePosture = GarmentCapturePosture.HANGER,
    val isTargetedReshoot: Boolean = false,
    val defectRegions: Set<ModelDefectRegion> = emptySet(),
    val defectKind: ModelDefectKind = ModelDefectKind.MISSING_PATCH
) {
    fun resolvedTargetSectors(): Set<Int> {
        return defectRegions.flatMap { it.targetSectors }.toSet()
    }
}

/**
 * 发送给服务端 POST /v1/capture/guide 的结构化指标。
 */
data class CaptureGuideRequest(
    val requestId: String,
    val targetType: CaptureTargetType,
    val subjectLabel: String = "",
    val coveredAzimuthDeg: List<Double>? = null,
    val azimuthSpanDeg: Double? = null,
    val upRollMedianDeg: Double? = null,
    val minMarginPx: MarginPx? = null,
    val sharpnessP50: Double? = null,
    val orbitContinuityMin: Double? = null,
    val roiDetailPx: Double? = null,
    val subjectOccupancyMax: Double? = null,
    val lostSubjectRatio: Double? = null,
    val hasScaleReference: Boolean? = null,
    val frameCount: Int? = null,
    val registeredFrames: Int? = null,
    val missingCloseups: List<String> = emptyList()
)

data class CaptureSectorSummary(
    val sectorCount: Int,
    val coveredSectors: Int?,
    val coverageRatio: Double?,
    val missingArcs: List<String>,
    val largestGapDeg: Double?
)

data class CaptureIssue(
    val code: String,
    val severity: String,
    val area: String,
    val detail: String
)

data class CaptureDiagnosis(
    val targetType: String,
    val subjectLabel: String,
    val readiness: String,
    val issueCount: Int,
    val sectorSummary: CaptureSectorSummary,
    val issues: List<CaptureIssue>
)

data class CaptureGuideStep(
    val priority: Int,
    val targetArea: String,
    val action: String,
    val reason: String
)

data class CaptureGuidePlan(
    val summary: String,
    val canReuseExisting: Boolean,
    val steps: List<CaptureGuideStep>
)

data class TargetedReshootPlan(
    val canUseIncrementalPatch: Boolean,
    val targetSectors: Set<Int>,
    val inferredRegions: Set<ModelDefectRegion>,
    val inferredDefectKind: ModelDefectKind
)

data class CaptureGuideResponse(
    val requestId: String,
    val targetType: String,
    val subjectLabel: String,
    val readiness: String,
    val diagnosis: CaptureDiagnosis,
    val guide: CaptureGuidePlan,
    val mode: String
) {
    val isPass: Boolean get() = readiness == "pass"
    val isMock: Boolean get() = mode == "mock"
    val isDirectAi: Boolean get() = mode == DirectDeepSeekCaptureGuide.MODE_DIRECT_AI
    val isOfflineFallback: Boolean get() = mode == "offline_fallback"

    /**
     * 将 AI / 离线质检建议与端侧三色黄格弱区映射到“局部定向补拍”配置，严格遵守 canReuseExisting 硬约束。
     */
    fun buildTargetedReshootPlan(weakSectors: Set<Int> = emptySet()): TargetedReshootPlan {
        val missingIndices = OrientationSectorTracker.parseGapSectorIndices(
            diagnosis.sectorSummary.missingArcs
        )
        val combinedSectors = (missingIndices + weakSectors)
            .filter { it in 0 until OrientationSectorTracker.SECTOR_COUNT }
            .toSet()

        val inferredRegions = buildSet {
            val baseRegions = listOf(
                ModelDefectRegion.FRONT,
                ModelDefectRegion.RIGHT_SEAM,
                ModelDefectRegion.BACK,
                ModelDefectRegion.LEFT_SEAM
            )
            baseRegions.forEach { region ->
                if (region.targetSectors.any { it in combinedSectors }) {
                    add(region)
                }
            }
            val stepsText = guide.steps.joinToString(" ") { "${it.targetArea} ${it.action}" }
            if (stepsText.contains("领口") || stepsText.contains("肩")) {
                add(ModelDefectRegion.COLLAR)
            }
            if (stepsText.contains("袖")) {
                add(ModelDefectRegion.SLEEVES)
            }
        }

        val hasQualityIssue = diagnosis.issues.any {
            it.code.contains("blur") || it.code.contains("dark") || it.code.contains("exposure")
        }
        val inferredDefectKind = if (missingIndices.isEmpty() && hasQualityIssue) {
            ModelDefectKind.BLURRY_OR_DARK
        } else {
            ModelDefectKind.MISSING_PATCH
        }

        val canUseIncremental = !isPass && guide.canReuseExisting && combinedSectors.isNotEmpty()
        return TargetedReshootPlan(
            canUseIncrementalPatch = canUseIncremental,
            targetSectors = combinedSectors,
            inferredRegions = inferredRegions,
            inferredDefectKind = inferredDefectKind
        )
    }
}

/**
 * 采集引导请求序列化与响应严格解析器。
 */
object CaptureGuideParser {
    private val VALID_TARGET_TYPES = setOf("head", "garment")
    private val VALID_READINESS = setOf("pass", "needs_reshoot", "invalid_retake")
    private val VALID_MODES = setOf("mock", "ai")

    fun encodeRequest(req: CaptureGuideRequest): String {
        if (req.requestId.isBlank()) {
            throw IllegalArgumentException("requestId 不能为空")
        }
        val root = JSONObject().apply {
            put("request_id", req.requestId)
            put("target_type", req.targetType.apiValue)
            put("subject_label", req.subjectLabel)
            req.coveredAzimuthDeg?.let { angles ->
                put("covered_azimuth_deg", JSONArray().apply {
                    angles.forEach { put(it) }
                })
            }
            req.azimuthSpanDeg?.let { put("azimuth_span_deg", it.coerceIn(0.0, 360.0)) }
            req.upRollMedianDeg?.let { put("up_roll_median_deg", it.coerceIn(0.0, 180.0)) }
            req.minMarginPx?.let { margin ->
                put("min_margin_px", JSONObject().apply {
                    put("left", margin.left)
                    put("right", margin.right)
                    put("top", margin.top)
                    put("bottom", margin.bottom)
                })
            }
            req.sharpnessP50?.let { put("sharpness_p50", it.coerceAtLeast(0.0)) }
            req.orbitContinuityMin?.let { put("orbit_continuity_min", it.coerceIn(-1.0, 1.0)) }
            req.roiDetailPx?.let { put("roi_detail_px", it.coerceAtLeast(0.0)) }
            req.subjectOccupancyMax?.let { put("subject_occupancy_max", it.coerceIn(0.0, 1.0)) }
            req.lostSubjectRatio?.let { put("lost_subject_ratio", it.coerceIn(0.0, 1.0)) }
            req.hasScaleReference?.let { put("has_scale_reference", it) }
            req.frameCount?.let { if (it >= 1) put("frame_count", it) }
            req.registeredFrames?.let { if (it >= 0) put("registered_frames", it) }
            put("missing_closeups", JSONArray().apply {
                req.missingCloseups.filter { it.isNotBlank() }.forEach { put(it.trim()) }
            })
        }
        return root.toString()
    }

    fun parseResponse(json: String): CaptureGuideResponse {
        val root = JSONObject(json)
        val requestId = root.requireString("request_id", "采集引导响应")
        val targetType = root.requireString("target_type", "采集引导响应")
        if (targetType !in VALID_TARGET_TYPES) {
            throw IllegalArgumentException("采集引导响应的 target_type 未知：$targetType")
        }
        val readiness = root.requireString("readiness", "采集引导响应")
        if (readiness !in VALID_READINESS) {
            throw IllegalArgumentException("采集引导响应的 readiness 未知：$readiness")
        }
        val mode = root.requireString("mode", "采集引导响应").lowercase()
        if (mode !in VALID_MODES) {
            throw IllegalArgumentException("采集引导响应的 mode 未知：$mode")
        }

        val diagnosisObj = root.requireObject("diagnosis", "采集引导响应")
        val diagReadiness = diagnosisObj.requireString("readiness", "diagnosis")
        if (diagReadiness !in VALID_READINESS) {
            throw IllegalArgumentException("diagnosis.readiness 未知：$diagReadiness")
        }
        val sectorObj = diagnosisObj.requireObject("sector_summary", "diagnosis")
        val sectorSummary = CaptureSectorSummary(
            sectorCount = sectorObj.optInt("sector_count", 12),
            coveredSectors = sectorObj.optNullableInt("covered_sectors"),
            coverageRatio = sectorObj.optNullableDouble("coverage_ratio"),
            missingArcs = sectorObj.readStringArray("missing_arcs", "sector_summary"),
            largestGapDeg = sectorObj.optNullableDouble("largest_gap_deg")
        )
        val issuesArray = diagnosisObj.optJSONArray("issues") ?: JSONArray()
        val issues = buildList {
            for (i in 0 until issuesArray.length()) {
                val item = issuesArray.optJSONObject(i)
                    ?: throw IllegalArgumentException("diagnosis.issues[$i] 必须是对象")
                add(
                    CaptureIssue(
                        code = item.requireString("code", "issues[$i]"),
                        severity = item.requireString("severity", "issues[$i]"),
                        area = item.requireString("area", "issues[$i]"),
                        detail = item.requireString("detail", "issues[$i]")
                    )
                )
            }
        }
        val diagnosis = CaptureDiagnosis(
            targetType = diagnosisObj.requireString("target_type", "diagnosis"),
            subjectLabel = diagnosisObj.optString("subject_label", ""),
            readiness = diagReadiness,
            issueCount = diagnosisObj.optInt("issue_count", issues.size),
            sectorSummary = sectorSummary,
            issues = issues
        )

        val guideObj = root.requireObject("guide", "采集引导响应")
        val stepsArray = guideObj.optJSONArray("steps") ?: JSONArray()
        val steps = buildList {
            for (i in 0 until stepsArray.length()) {
                val item = stepsArray.optJSONObject(i)
                    ?: throw IllegalArgumentException("guide.steps[$i] 必须是对象")
                val priority = item.optInt("priority", -1)
                if (priority <= 0) {
                    throw IllegalArgumentException("guide.steps[$i].priority 必须是正整数")
                }
                add(
                    CaptureGuideStep(
                        priority = priority,
                        targetArea = item.requireString("target_area", "steps[$i]"),
                        action = item.requireString("action", "steps[$i]"),
                        reason = item.requireString("reason", "steps[$i]")
                    )
                )
            }
        }.sortedBy { it.priority }

        val guide = CaptureGuidePlan(
            summary = guideObj.requireString("summary", "guide"),
            canReuseExisting = guideObj.requireBoolean("can_reuse_existing", "guide"),
            steps = steps
        )

        return CaptureGuideResponse(
            requestId = requestId,
            targetType = targetType,
            subjectLabel = root.optString("subject_label", ""),
            readiness = readiness,
            diagnosis = diagnosis,
            guide = guide,
            mode = mode
        )
    }

    private fun JSONObject.requireString(key: String, where: String): String {
        if (!has(key) || isNull(key)) {
            throw IllegalArgumentException("$where 缺少字段 $key")
        }
        val value = get(key)
        if (value !is String || value.isBlank()) {
            throw IllegalArgumentException("$where 的 $key 必须是非空字符串")
        }
        return value
    }

    private fun JSONObject.requireBoolean(key: String, where: String): Boolean {
        if (!has(key) || isNull(key)) {
            throw IllegalArgumentException("$where 缺少字段 $key")
        }
        val value = get(key)
        if (value !is Boolean) {
            throw IllegalArgumentException("$where 的 $key 应为布尔值")
        }
        return value
    }

    private fun JSONObject.requireObject(key: String, where: String): JSONObject {
        if (!has(key) || isNull(key)) {
            throw IllegalArgumentException("$where 缺少对象 $key")
        }
        val value = get(key)
        if (value !is JSONObject) {
            throw IllegalArgumentException("$where 的 $key 应为 JSON 对象")
        }
        return value
    }

    private fun JSONObject.optNullableInt(key: String): Int? {
        if (!has(key) || isNull(key)) return null
        val value = get(key)
        return (value as? Number)?.toInt()
    }

    private fun JSONObject.optNullableDouble(key: String): Double? {
        if (!has(key) || isNull(key)) return null
        val value = get(key)
        return (value as? Number)?.toDouble()
    }

    private fun JSONObject.readStringArray(key: String, where: String): List<String> {
        if (!has(key) || isNull(key)) return emptyList()
        val array = get(key)
        if (array !is JSONArray) {
            throw IllegalArgumentException("$where 的 $key 应为数组")
        }
        return buildList {
            for (i in 0 until array.length()) {
                val element = array.get(i)
                if (element !is String) {
                    throw IllegalArgumentException("$where 的 $key[$i] 应为字符串")
                }
                add(element)
            }
        }
    }
}

/**
 * 离线/超时降级诊断器：当服务端不可达（如 503 或无网络）时，
 * 直接基于端侧实测的 12 扇区角度、翻滚、清晰度与特写清单给出可读的本地缺失提示，不丢失已录视频。
 */
object OfflineCaptureDiagnoser {

    fun buildFallback(request: CaptureGuideRequest, networkReason: String): CaptureGuideResponse {
        val trackerSummary = OrientationSectorTracker.summarizeAngles(request.coveredAzimuthDeg.orEmpty())
        val issues = mutableListOf<CaptureIssue>()
        val steps = mutableListOf<CaptureGuideStep>()
        var priority = 1

        if (trackerSummary.largestGapDeg != null && trackerSummary.largestGapDeg >= 90.0) {
            val arcs = trackerSummary.missingArcs.joinToString("、")
            val detail = "离线预估仅覆盖 ${trackerSummary.coveredSectors}/12 个扇区，缺失：$arcs"
            issues += CaptureIssue("azimuth_severe_gap", "critical", "环绕方位覆盖", detail)
            steps += CaptureGuideStep(
                priority++,
                "环绕缺口（$arcs）",
                "补拍未覆盖区间 $arcs，保持缓慢匀速绕行并覆盖正反面。",
                detail
            )
        } else if (trackerSummary.missingArcs.isNotEmpty()) {
            val arcs = trackerSummary.missingArcs.joinToString("、")
            val detail = "离线预估存在部分视角缺失：$arcs"
            issues += CaptureIssue("azimuth_partial_gap", "major", "环绕方位覆盖", detail)
            steps += CaptureGuideStep(
                priority++,
                "缺口补拍（$arcs）",
                "沿缺失角度区间 $arcs 慢速补拍一遍，每 30° 停留半秒。",
                detail
            )
        }

        val sharp = request.sharpnessP50
        if (sharp != null && sharp < 35.0) {
            val sev = if (sharp < 20.0) "critical" else "major"
            val detail = "清晰度中位数 ${sharp.roundToInt()} 偏低（阈值 35），可能存在手抖模糊。"
            issues += CaptureIssue("blur_detected", sev, "画面清晰度", detail)
            steps += CaptureGuideStep(
                priority++,
                "画面稳定性",
                "双手持稳手机，将环绕/推近速度放慢一半并在光线充足处重拍。",
                detail
            )
        }

        val roll = request.upRollMedianDeg?.let { abs(it) }
        if (roll != null && roll > 20.0) {
            val detail = "手机横滚角中位偏差 ${roll.roundToInt()}°（>20°），可能影响视角对齐。"
            issues += CaptureIssue("camera_roll_warning", "major", "相机水平仪", detail)
            steps += CaptureGuideStep(
                priority++,
                "手机水平度",
                "保持手机竖直端平，使绿色水平刻度线居中。",
                detail
            )
        }

        if (request.targetType == CaptureTargetType.HEAD && request.roiDetailPx != null && request.roiDetailPx < 16.0) {
            val detail = "近景眼部距离仅约 ${request.roiDetailPx.roundToInt()} px（建议 ≥20 px）。"
            issues += CaptureIssue("roi_resolution_low", "major", "面部关键细节", detail)
            steps += CaptureGuideStep(
                priority++,
                "面部慢推近特写",
                "在阶段 2 保持被拍人静止，镜头缓慢推近至正脸及左右 30° 清晰可见。",
                detail
            )
        }

        if (request.missingCloseups.isNotEmpty()) {
            val listText = request.missingCloseups.joinToString("、")
            val detail = "本地记录尚缺特写：$listText"
            issues += CaptureIssue("missing_closeups", "major", "关键部位特写", detail)
            steps += CaptureGuideStep(
                priority++,
                "缺失特写（$listText）",
                "推近镜头依次补拍：$listText。",
                detail
            )
        }

        val hasCritical = issues.any { it.severity == "critical" }
        val readiness = when {
            issues.isEmpty() -> "pass"
            hasCritical -> "invalid_retake"
            else -> "needs_reshoot"
        }

        if (steps.isEmpty()) {
            steps += CaptureGuideStep(
                1,
                "离线预检完成",
                "端侧基础指标已达标，已保留本地录像；网络恢复后可再次上传服务端做 AI 终审。",
                "服务端暂不可用（$networkReason）"
            )
        }

        val summary = if (issues.isEmpty()) {
            "服务端暂不可用（$networkReason），端侧离线复核显示基础环绕与清晰度达标，录像已保留。"
        } else {
            "服务端暂不可用（$networkReason），端侧离线检测到 ${issues.size} 项问题，请按步骤针对性补拍。"
        }

        return CaptureGuideResponse(
            requestId = request.requestId,
            targetType = request.targetType.apiValue,
            subjectLabel = request.subjectLabel,
            readiness = readiness,
            diagnosis = CaptureDiagnosis(
                targetType = request.targetType.apiValue,
                subjectLabel = request.subjectLabel,
                readiness = readiness,
                issueCount = issues.size,
                sectorSummary = trackerSummary,
                issues = issues
            ),
            guide = CaptureGuidePlan(
                summary = summary,
                canReuseExisting = readiness != "invalid_retake",
                steps = steps.take(4)
            ),
            mode = "offline_fallback"
        )
    }
}
