package com.ootd.ai.capture

import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * 紧凑灰度帧容器（来自 CameraX YUV_420_888 Y 平面，也可在 JVM 单测直接构造）。
 */
data class GrayFrame(
    val width: Int,
    val height: Int,
    val yPlane: ByteArray
) {
    init {
        require(width > 0 && height > 0) { "帧宽高必须为正数" }
        require(yPlane.size >= width * height) { "Y 平面长度不足：${yPlane.size} < ${width * height}" }
    }
}

/**
 * ML Kit FaceDetection 抽取后的纯数据摘要（脱离 Android / ML Kit SDK 依赖，便于 JVM 单测）。
 */
data class FaceObservation(
    val leftPx: Float,
    val topPx: Float,
    val rightPx: Float,
    val bottomPx: Float,
    val eyeDistancePx: Double,
    val headEulerAngleYDeg: Float
)

data class SubjectFraming(
    val detected: Boolean,
    val occupancy: Double,
    val marginLeftPx: Int,
    val marginTopPx: Int,
    val marginRightPx: Int,
    val marginBottomPx: Int,
    val reliable: Boolean = true
)

enum class FramingStatus {
    NOT_FOUND,
    MANUAL_GUIDE,
    TOO_FAR,
    GOOD,
    TOO_CLOSE,
    CROPPED
}

enum class CloseupCheckId(val label: String) {
    FACE_FRONT("正脸特写"),
    FACE_LEFT("左斜侧脸（-30°）"),
    FACE_RIGHT("右斜侧脸（+30°）"),
    GARMENT_COLLAR("领口/肩部细节"),
    GARMENT_CUFF("袖口/面料纹理")
}

enum class ExposureStatus {
    GOOD,
    UNDEREXPOSED,
    OVEREXPOSED,
    BACKLIT
}

data class ExposureSnapshot(
    val status: ExposureStatus,
    val centerMeanLuma: Double,
    val borderMeanLuma: Double,
    val darkRatio: Double,
    val brightRatio: Double
)

data class LiveQualitySnapshot(
    val stage: CaptureStage,
    val trafficLight: TrafficLight,
    val framingStatus: FramingStatus,
    val actionBadge: String,
    val promptText: String,
    val voiceHint: String?,
    val sharpness: Double,
    val continuityNcc: Double?,
    val subjectOccupancy: Double,
    val minMargins: MarginPx?,
    val eyeDistancePx: Double?,
    val headYawDeg: Float?,
    val stage1ReadyForCloseup: Boolean,
    val completedCloseups: Set<CloseupCheckId>,
    val missingCloseups: List<String>,
    val analyzedFrames: Int,
    val orbitMotionState: OrbitMotionState = OrbitMotionState.STATIONARY,
    val parallaxGapPx: Double = 0.0,
    val wristTwistRejectedFrames: Int = 0,
    val exposureStatus: ExposureStatus = ExposureStatus.GOOD,
    val frameQualityAcceptableForSector: Boolean = true
)

/**
 * 端侧两段式实时帧质量分析器与状态累积器（纯 Kotlin / JVM 可测）。
 *
 * 指标与阈值对齐服务端 server/capture_guide.py 及 docs/capture-guidance-design.md 第 4 节：
 * - Y 通道降采样至 <= 320x240，3x3 拉普拉斯方差清晰度（<20 连续 3 帧严重模糊预警；<35 提示放慢）
 * - 64x64 均值方差归一化亮度向量点积（NCC），全程最小值 <0.35 实时提示移动过快
 * - 阶段 1（远景定骨架）：占比 35%–55% 为绿灯、>65% 报过近，head 查上左右留白 >=40px，garment 查四边 >=40px
 * - 阶段 2（慢推近补细节）：head 检查眼距 >=20px 且覆盖正脸 + 左右各 ~30°；garment 检查清晰细节近景
 */
class LiveFrameQualityAnalyzer(
    private val targetType: CaptureTargetType
) {
    private var stage: CaptureStage = CaptureStage.STAGE_1_OVERVIEW
    private var prev64Vector: FloatArray? = null
    private val parallaxDetector = OrbitParallaxDetector()

    private var consecutiveBlurFrames = 0
    private var consecutiveExposureBadFrames = 0
    private var analyzedFrameCount = 0
    private var lostSubjectFrames = 0

    private val sharpnessHistory = mutableListOf<Double>()
    private var minContinuityNcc: Double? = null

    private var stage1OccupancyMax: Double = 0.0
    private var stage1MinLeft: Int? = null
    private var stage1MinTop: Int? = null
    private var stage1MinRight: Int? = null
    private var stage1MinBottom: Int? = null

    private var bestRoiDetailPx: Double = 0.0
    private var stage2HighDetailFrames = 0
    private val completedCloseups = linkedSetOf<CloseupCheckId>()
    private var lastVoiceKey: String? = null

    fun reset(targetStage: CaptureStage = CaptureStage.STAGE_1_OVERVIEW) {
        stage = targetStage
        prev64Vector = null
        parallaxDetector.reset()
        consecutiveBlurFrames = 0
        consecutiveExposureBadFrames = 0
        analyzedFrameCount = 0
        lostSubjectFrames = 0
        sharpnessHistory.clear()
        minContinuityNcc = null
        stage1OccupancyMax = 0.0
        stage1MinLeft = null
        stage1MinTop = null
        stage1MinRight = null
        stage1MinBottom = null
        bestRoiDetailPx = 0.0
        stage2HighDetailFrames = 0
        completedCloseups.clear()
        lastVoiceKey = null
    }

    fun currentStage(): CaptureStage = stage

    fun enterStage2Closeup() {
        stage = CaptureStage.STAGE_2_CLOSEUP
        lastVoiceKey = "enter_stage_2"
    }

    /**
     * 手动勾选或确认衣物细节项（当无专用衣物分割模型时供拍摄者/助手点选辅助）。
     */
    fun markCloseupCompleted(item: CloseupCheckId) {
        completedCloseups += item
    }

    fun analyzeFrame(
        frame: GrayFrame,
        orientation: OrientationSnapshot?,
        faces: List<FaceObservation> = emptyList()
    ): LiveQualitySnapshot {
        analyzedFrameCount += 1

        val sharpness = computeLaplacianSharpness(frame)
        sharpnessHistory += sharpness
        if (sharpness < BLUR_CRITICAL_THRESHOLD) {
            consecutiveBlurFrames += 1
        } else {
            consecutiveBlurFrames = 0
        }

        val curr64 = downsample64x64Normalized(frame)
        val ncc = prev64Vector?.let { prev -> computeNcc(prev, curr64) }
        prev64Vector = curr64
        if (ncc != null) {
            minContinuityNcc = minContinuityNcc?.let { min(it, ncc) } ?: ncc
        }
        val parallax = parallaxDetector.analyze(frame, orientation?.relativeAzimuthDeg)
        val exposure = evaluateExposure(frame)
        if (exposure.status != ExposureStatus.GOOD) {
            consecutiveExposureBadFrames += 1
        } else {
            consecutiveExposureBadFrames = 0
        }

        val primaryFace = faces.maxByOrNull { (it.rightPx - it.leftPx) * (it.bottomPx - it.topPx) }
        val framing = evaluateFraming(frame, primaryFace)
        if (!framing.detected) {
            lostSubjectFrames += 1
        }

        if (stage == CaptureStage.STAGE_1_OVERVIEW && framing.detected && framing.reliable) {
            stage1OccupancyMax = max(stage1OccupancyMax, framing.occupancy)
            stage1MinLeft = stage1MinLeft?.let { min(it, framing.marginLeftPx) } ?: framing.marginLeftPx
            stage1MinTop = stage1MinTop?.let { min(it, framing.marginTopPx) } ?: framing.marginTopPx
            stage1MinRight = stage1MinRight?.let { min(it, framing.marginRightPx) } ?: framing.marginRightPx
            stage1MinBottom = stage1MinBottom?.let { min(it, framing.marginBottomPx) } ?: framing.marginBottomPx
        }

        var eyeDistPx: Double? = null
        var headYawDeg: Float? = null
        if (targetType == CaptureTargetType.HEAD && primaryFace != null) {
            eyeDistPx = primaryFace.eyeDistancePx
            headYawDeg = primaryFace.headEulerAngleYDeg
            if (sharpness >= SHARP_OK_THRESHOLD && eyeDistPx > bestRoiDetailPx) {
                bestRoiDetailPx = eyeDistPx
            }
            if (stage == CaptureStage.STAGE_2_CLOSEUP && sharpness >= SHARP_OK_THRESHOLD && eyeDistPx >= EYE_DETAIL_TARGET_PX) {
                stage2HighDetailFrames += 1
                updateHeadCloseupChecklist(headYawDeg)
            }
        } else if (targetType == CaptureTargetType.GARMENT && stage == CaptureStage.STAGE_2_CLOSEUP) {
            if (framing.detected && sharpness >= SHARP_OK_THRESHOLD && (!framing.reliable || framing.occupancy >= 0.45)) {
                stage2HighDetailFrames += 1
                // 连续推近高清晰度帧，自动辅助点亮衣料纹理检查；复杂室内背景下使用保守占比估算细节像素
                val effOcc = if (framing.reliable) framing.occupancy else 0.60
                val detailEstPx = min(frame.width, frame.height) * effOcc * 0.08
                if (detailEstPx > bestRoiDetailPx) {
                    bestRoiDetailPx = detailEstPx
                }
                if (stage2HighDetailFrames >= 2) {
                    completedCloseups += CloseupCheckId.GARMENT_COLLAR
                }
                if (stage2HighDetailFrames >= 4) {
                    completedCloseups += CloseupCheckId.GARMENT_CUFF
                }
            }
        }

        val rollOk = orientation?.let { !it.isRollWarning } ?: true
        val exposureOkForSector = exposure.status == ExposureStatus.GOOD
        val frameQualityAcceptable = sharpness >= SHARP_OK_THRESHOLD &&
            consecutiveBlurFrames == 0 &&
            exposureOkForSector &&
            rollOk &&
            parallax.state != OrbitMotionState.WRIST_TWIST_SUSPECTED

        val stage1CoveredEnough = (orientation?.coveredCount ?: 0) >= STAGE1_READY_SECTOR_MIN
        val stage1Ready = stage == CaptureStage.STAGE_1_OVERVIEW &&
            stage1CoveredEnough &&
            rollOk &&
            consecutiveBlurFrames == 0 &&
            (lostSubjectRatio() <= 0.25)

        val feedback = decideFeedback(
            orientation = orientation,
            sharpness = sharpness,
            ncc = ncc,
            parallax = parallax,
            exposure = exposure,
            framing = framing,
            eyeDistPx = eyeDistPx,
            stage1Ready = stage1Ready
        )

        val voiceHint = if (feedback.voiceKey != null && feedback.voiceKey != lastVoiceKey) {
            lastVoiceKey = feedback.voiceKey
            feedback.promptText
        } else {
            null
        }

        return LiveQualitySnapshot(
            stage = stage,
            trafficLight = feedback.trafficLight,
            framingStatus = feedback.framingStatus,
            actionBadge = feedback.actionBadge,
            promptText = feedback.promptText,
            voiceHint = voiceHint,
            sharpness = sharpness,
            continuityNcc = ncc,
            subjectOccupancy = framing.occupancy,
            minMargins = MarginPx(
                left = framing.marginLeftPx,
                right = framing.marginRightPx,
                top = framing.marginTopPx,
                bottom = framing.marginBottomPx
            ),
            eyeDistancePx = eyeDistPx,
            headYawDeg = headYawDeg,
            stage1ReadyForCloseup = stage1Ready,
            completedCloseups = completedCloseups.toSet(),
            missingCloseups = computeMissingCloseups(),
            analyzedFrames = analyzedFrameCount,
            orbitMotionState = parallax.state,
            parallaxGapPx = parallax.parallaxGapPx,
            wristTwistRejectedFrames = parallax.totalTwistRejectedFrames,
            exposureStatus = exposure.status,
            frameQualityAcceptableForSector = frameQualityAcceptable
        )
    }

    fun lostSubjectRatio(): Double {
        if (analyzedFrameCount == 0) return 0.0
        return lostSubjectFrames.toDouble() / analyzedFrameCount
    }

    fun computeMissingCloseups(): List<String> {
        val expected = when (targetType) {
            CaptureTargetType.HEAD -> listOf(
                CloseupCheckId.FACE_FRONT,
                CloseupCheckId.FACE_LEFT,
                CloseupCheckId.FACE_RIGHT
            )
            CaptureTargetType.GARMENT -> listOf(
                CloseupCheckId.GARMENT_COLLAR,
                CloseupCheckId.GARMENT_CUFF
            )
        }
        return expected.filter { it !in completedCloseups }.map { it.label }
    }

    /**
     * 汇总全过程指标为 POST /v1/capture/guide 请求体。
     */
    fun buildGuideRequest(
        requestId: String,
        prep: CapturePrepConfig,
        tracker: OrientationSectorTracker
    ): CaptureGuideRequest {
        val orientation = tracker.currentSnapshot()
        val hasStage1Margins = stage1MinLeft != null
        val marginObj = if (hasStage1Margins) {
            MarginPx(
                left = stage1MinLeft ?: 0,
                right = stage1MinRight ?: 0,
                top = stage1MinTop ?: 0,
                bottom = stage1MinBottom ?: 0
            )
        } else null

        val sharpP50 = if (sharpnessHistory.isNotEmpty()) {
            OrientationSectorTracker.computeMedian(sharpnessHistory)
        } else null

        val occMax = if (stage1OccupancyMax > 0.0) {
            stage1OccupancyMax.coerceIn(0.0, 1.0)
        } else null

        val roiDetail = if (bestRoiDetailPx > 0.0) bestRoiDetailPx else null

        return CaptureGuideRequest(
            requestId = requestId,
            targetType = targetType,
            subjectLabel = prep.subjectLabel.ifBlank { "${targetType.apiValue}-session" },
            coveredAzimuthDeg = tracker.exportCoveredAzimuthDeg(),
            upRollMedianDeg = orientation.upRollMedianDeg.coerceIn(0.0, 180.0),
            minMarginPx = marginObj,
            sharpnessP50 = sharpP50,
            orbitContinuityMin = minContinuityNcc?.coerceIn(-1.0, 1.0),
            roiDetailPx = roiDetail,
            subjectOccupancyMax = occMax,
            lostSubjectRatio = lostSubjectRatio().coerceIn(0.0, 1.0),
            hasScaleReference = prep.hasScaleReference,
            frameCount = analyzedFrameCount.coerceAtLeast(1),
            missingCloseups = computeMissingCloseups()
        )
    }

    private fun updateHeadCloseupChecklist(yawDeg: Float) {
        when {
            abs(yawDeg) <= 12f -> completedCloseups += CloseupCheckId.FACE_FRONT
            yawDeg <= -18f -> completedCloseups += CloseupCheckId.FACE_LEFT
            yawDeg >= 18f -> completedCloseups += CloseupCheckId.FACE_RIGHT
        }
    }

    private fun evaluateFraming(frame: GrayFrame, face: FaceObservation?): SubjectFraming {
        if (targetType == CaptureTargetType.HEAD) {
            if (face != null) {
                val faceW = max(1f, face.rightPx - face.leftPx)
                val faceH = max(1f, face.bottomPx - face.topPx)
                // 由人脸框合理外推头部+肩部可见边界，用于检测左右与头顶留白及远景占比
                val estLeft = max(0f, face.leftPx - faceW * 0.35f)
                val estRight = min(frame.width.toFloat(), face.rightPx + faceW * 0.35f)
                val estTop = max(0f, face.topPx - faceH * 0.30f)
                val estBottom = min(frame.height.toFloat(), face.bottomPx + faceH * 0.90f)
                val occ = (((estRight - estLeft) * (estBottom - estTop)) / (frame.width.toFloat() * frame.height.toFloat())).toDouble()
                return SubjectFraming(
                    detected = true,
                    occupancy = occ.coerceIn(0.0, 1.0),
                    marginLeftPx = estLeft.roundToInt(),
                    marginTopPx = estTop.roundToInt(),
                    marginRightPx = (frame.width - estRight).roundToInt().coerceAtLeast(0),
                    marginBottomPx = (frame.height - estBottom).roundToInt().coerceAtLeast(0),
                    reliable = true
                )
            }
            // 绕到后脑勺等未检测到人脸时，仅保留遮挡/纯黑检测，不用整图边缘去误判“太近/往后退”
            val bgFallback = estimateSubjectBounds(frame)
            return if (!bgFallback.detected) bgFallback else bgFallback.copy(reliable = false)
        }
        return estimateSubjectBounds(frame)
    }

    private data class FeedbackDecision(
        val trafficLight: TrafficLight,
        val framingStatus: FramingStatus,
        val actionBadge: String,
        val promptText: String,
        val voiceKey: String?
    )

    private fun computeFramingStatus(framing: SubjectFraming): FramingStatus {
        if (!framing.detected) return FramingStatus.NOT_FOUND
        if (!framing.reliable) return FramingStatus.MANUAL_GUIDE
        if (framing.occupancy > OCCUPANCY_MAX_LIMIT) return FramingStatus.TOO_CLOSE
        val minCheckedMargin = if (targetType == CaptureTargetType.HEAD) {
            minOf(framing.marginLeftPx, framing.marginRightPx, framing.marginTopPx)
        } else {
            minOf(framing.marginLeftPx, framing.marginRightPx, framing.marginTopPx, framing.marginBottomPx)
        }
        if (minCheckedMargin < MARGIN_MIN_PX) return FramingStatus.CROPPED
        if (framing.occupancy < 0.30) return FramingStatus.TOO_FAR
        return FramingStatus.GOOD
    }

    private fun suggestWalkDirection(orientation: OrientationSnapshot?): String {
        if (orientation == null) return "向右缓慢绕行"
        val cur = orientation.currentSectorIndex
        val covered = orientation.coveredSectors
        for (step in 1..OrientationSectorTracker.SECTOR_COUNT / 2) {
            val cw = (cur + step) % OrientationSectorTracker.SECTOR_COUNT
            if (!covered[cw]) return "继续向右缓慢绕行"
            val ccw = (cur - step + OrientationSectorTracker.SECTOR_COUNT) % OrientationSectorTracker.SECTOR_COUNT
            if (!covered[ccw]) return "向左补几步缺失方向"
        }
        return "保持节奏绕完剩余角度"
    }

    private fun decideFeedback(
        orientation: OrientationSnapshot?,
        sharpness: Double,
        ncc: Double?,
        parallax: OrbitParallaxSnapshot,
        exposure: ExposureSnapshot,
        framing: SubjectFraming,
        eyeDistPx: Double?,
        stage1Ready: Boolean
    ): FeedbackDecision {
        val framingStatus = computeFramingStatus(framing)

        if (consecutiveBlurFrames >= 3) {
            return FeedbackDecision(
                trafficLight = TrafficLight.RED,
                framingStatus = framingStatus,
                actionBadge = "稳住放慢",
                promptText = "画面有点模糊！请放慢走动速度，双手拿稳手机先停半秒对焦",
                voiceKey = "blur"
            )
        }
        if (orientation != null && orientation.isRollWarning) {
            val tiltHint = if (orientation.rollDeg > 0) "往左扶正手机" else "往右扶正手机"
            return FeedbackDecision(
                trafficLight = TrafficLight.YELLOW,
                framingStatus = framingStatus,
                actionBadge = tiltHint,
                promptText = "手机拿歪了（斜了 ${abs(orientation.rollDeg).roundToInt()}°），请$tiltHint，让水平仪气泡回中间",
                voiceKey = "roll"
            )
        }
        if (stage == CaptureStage.STAGE_1_OVERVIEW &&
            parallax.state == OrbitMotionState.WRIST_TWIST_SUSPECTED
        ) {
            return FeedbackDecision(
                trafficLight = TrafficLight.YELLOW,
                framingStatus = framingStatus,
                actionBadge = "别只扭手腕 · 迈步绕行",
                promptText = "检测到原地扭手腕（前景与背景同速横移，缺乏空间视差），该角度未计入覆盖！请迈开脚步绕着目标走",
                voiceKey = "wrist_twist"
            )
        }
        if (consecutiveExposureBadFrames >= 2) {
            when (exposure.status) {
                ExposureStatus.BACKLIT -> return FeedbackDecision(
                    trafficLight = TrafficLight.YELLOW,
                    framingStatus = framingStatus,
                    actionBadge = "逆光过强 · 换个朝向",
                    promptText = "检测到强逆光（主体偏暗而背景刺眼），请背对窗户或打开正面补光灯后再拍该角度",
                    voiceKey = "exposure_backlit"
                )
                ExposureStatus.UNDEREXPOSED -> return FeedbackDecision(
                    trafficLight = TrafficLight.YELLOW,
                    framingStatus = framingStatus,
                    actionBadge = "光线太暗 · 请补光",
                    promptText = "当前角度画面偏暗（容易丢失布料纹理），请开灯或移至明亮漫反射光源下拍摄",
                    voiceKey = "exposure_dark"
                )
                ExposureStatus.OVEREXPOSED -> return FeedbackDecision(
                    trafficLight = TrafficLight.YELLOW,
                    framingStatus = framingStatus,
                    actionBadge = "高光过曝 · 避开直射灯",
                    promptText = "当前角度高光泛白过曝，请稍微避开强直射灯光或点击画面测光",
                    voiceKey = "exposure_bright"
                )
                ExposureStatus.GOOD -> Unit
            }
        }
        if (ncc != null && ncc < CONTINUITY_MIN_THRESHOLD) {
            return FeedbackDecision(
                trafficLight = TrafficLight.YELLOW,
                framingStatus = framingStatus,
                actionBadge = "走太快了",
                promptText = "移动太快了！请像慢动作一样匀速挪步，别一下子晃太快",
                voiceKey = "fast"
            )
        }

        if (stage == CaptureStage.STAGE_1_OVERVIEW) {
            if (!framing.detected) {
                val targetWord = if (targetType == CaptureTargetType.HEAD) "人头和肩膀" else "整件衣服"
                return FeedbackDecision(
                    trafficLight = TrafficLight.RED,
                    framingStatus = FramingStatus.NOT_FOUND,
                    actionBadge = "对准虚线框",
                    promptText = "画面里没找到目标，请把${targetWord}移到屏幕中央虚线框里",
                    voiceKey = "lost"
                )
            }
            if (framingStatus == FramingStatus.TOO_CLOSE) {
                return FeedbackDecision(
                    trafficLight = TrafficLight.RED,
                    framingStatus = FramingStatus.TOO_CLOSE,
                    actionBadge = "往后退一大步",
                    promptText = "太近了！请往后退一大步（约 1.2–1.5 米），别让目标顶满屏幕",
                    voiceKey = "too_close"
                )
            }
            if (framingStatus == FramingStatus.CROPPED) {
                val tip = if (targetType == CaptureTargetType.HEAD) "头顶或耳朵快出画了" else "衣服边缘快被切掉了"
                return FeedbackDecision(
                    trafficLight = TrafficLight.YELLOW,
                    framingStatus = FramingStatus.CROPPED,
                    actionBadge = "退半步留边",
                    promptText = "${tip}，请稍微后退半步，让虚线框四周留出空隙",
                    voiceKey = "margin"
                )
            }
            if (stage1Ready) {
                return FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = framingStatus,
                    actionBadge = "外圈已达标 ✓",
                    promptText = "第 1 步外圈绕拍已达标！不要停录，让被拍人别动，点右下角绿色按钮进入「第 2 步推近」",
                    voiceKey = "stage1_ready"
                )
            }
            val count = orientation?.coveredCount ?: 0
            val dirHint = suggestWalkDirection(orientation)
            return when (framingStatus) {
                FramingStatus.MANUAL_GUIDE -> FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.MANUAL_GUIDE,
                    actionBadge = "$dirHint ($count/12)",
                    promptText = "请目视将目标完整装进中央虚线框（四周留边），迈步$dirHint（已点亮 $count/12 格）",
                    voiceKey = null
                )
                FramingStatus.TOO_FAR -> FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.TOO_FAR,
                    actionBadge = "往前走一两步",
                    promptText = "目标略远，可稍微走近一两步装满虚线框，同时$dirHint（$count/12 格）",
                    voiceKey = null
                )
                else -> FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.GOOD,
                    actionBadge = "$dirHint ($count/12)",
                    promptText = "距离刚刚好！请被拍人别动，你迈开脚步$dirHint（已点亮 $count/12 格，别只原地扭手腕）",
                    voiceKey = null
                )
            }
        }

        // Stage 2：一镜慢推近补细节
        if (targetType == CaptureTargetType.HEAD) {
            if (eyeDistPx == null) {
                return FeedbackDecision(
                    trafficLight = TrafficLight.YELLOW,
                    framingStatus = framingStatus,
                    actionBadge = "对准面部",
                    promptText = "第 2 步推近：请把镜头对准人脸中央，让被拍人保持不动，慢慢往前凑近",
                    voiceKey = "stage2_no_face"
                )
            }
            if (eyeDistPx < EYE_DETAIL_TARGET_PX) {
                return FeedbackDecision(
                    trafficLight = TrafficLight.YELLOW,
                    framingStatus = framingStatus,
                    actionBadge = "继续慢慢凑近",
                    promptText = "还不够近！请继续慢慢往前推近到距离面部约 40 厘米（眼部像素 ${eyeDistPx.roundToInt()}/20）",
                    voiceKey = "stage2_closer"
                )
            }
            val missing = computeMissingCloseups()
            return if (missing.isEmpty()) {
                FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.GOOD,
                    actionBadge = "特写全达标 ✓",
                    promptText = "正脸与左右侧脸高清特写全部采齐！现在可以点「完成录制并出指导」",
                    voiceKey = "stage2_done"
                )
            } else {
                val nextAngleHint = when (missing.first()) {
                    CloseupCheckId.FACE_FRONT.label -> "先对准正脸停稳 1 秒"
                    CloseupCheckId.FACE_LEFT.label -> "被拍人别动！你拿着手机往左移动一小步拍左斜侧脸"
                    else -> "被拍人别动！你拿着手机往右移动一小步拍右斜侧脸"
                }
                FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.GOOD,
                    actionBadge = "去拍：${missing.first()}",
                    promptText = "距离清晰度都很好！下一步：$nextAngleHint",
                    voiceKey = "stage2_next_${missing.first()}"
                )
            }
        } else {
            val missing = computeMissingCloseups()
            return if (missing.isEmpty()) {
                FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.GOOD,
                    actionBadge = "特写全达标 ✓",
                    promptText = "领口与面料纹理细节已采清！现在可以点「完成录制并出指导」",
                    voiceKey = "stage2_garment_done"
                )
            } else {
                FeedbackDecision(
                    trafficLight = TrafficLight.GREEN,
                    framingStatus = FramingStatus.GOOD,
                    actionBadge = "慢推领口/袖口",
                    promptText = "第 2 步推近：保持衣物固定不动，镜头慢慢靠近领口与袖口约 30–50 厘米拍清纹理（也可手动点选已拍项）",
                    voiceKey = null
                )
            }
        }
    }

    companion object {
        const val LAPLACIAN_MAX_W = 320
        const val LAPLACIAN_MAX_H = 240
        const val BLUR_CRITICAL_THRESHOLD = 20.0
        const val SHARP_OK_THRESHOLD = 35.0
        const val CONTINUITY_MIN_THRESHOLD = 0.35
        const val OCCUPANCY_MAX_LIMIT = 0.65
        const val MARGIN_MIN_PX = 40
        const val EYE_DETAIL_TARGET_PX = 20.0
        const val STAGE1_READY_SECTOR_MIN = 10
        const val EXPOSURE_DARK_LUMA = 52.0
        const val EXPOSURE_BACKLIT_DIFF = 55.0

        /**
         * 评估帧中心主体与背景环带的曝光与逆光情况（64x64 降采样，纯 Kotlin）。
         */
        fun evaluateExposure(frame: GrayFrame): ExposureSnapshot {
            val size = 64
            val yData = frame.yPlane
            var cSum = 0.0
            var cCount = 0
            var cDark = 0
            var cBright = 0
            var bSum = 0.0
            var bCount = 0

            for (y in 0 until size) {
                val srcY = (y * frame.height) / size
                val srcRow = srcY * frame.width
                val inCenterY = y in 12 until 52
                val inBorderY = y < 8 || y >= 56
                for (x in 0 until size) {
                    val srcX = (x * frame.width) / size
                    val v = (yData[srcRow + srcX].toInt() and 0xFF).toDouble()
                    val inCenterX = x in 16 until 48
                    val inBorderX = x < 10 || x >= 54
                    if (inCenterX && inCenterY) {
                        cSum += v
                        cCount += 1
                        if (v < 25.0) cDark += 1
                        if (v > 245.0) cBright += 1
                    } else if (inBorderX || inBorderY) {
                        bSum += v
                        bCount += 1
                    }
                }
            }

            val cMean = cSum / max(1, cCount)
            val bMean = bSum / max(1, bCount)
            val darkRatio = cDark.toDouble() / max(1, cCount)
            val brightRatio = cBright.toDouble() / max(1, cCount)

            val status = when {
                (bMean - cMean) >= EXPOSURE_BACKLIT_DIFF && cMean < 115.0 -> ExposureStatus.BACKLIT
                cMean < EXPOSURE_DARK_LUMA || darkRatio >= 0.35 -> ExposureStatus.UNDEREXPOSED
                brightRatio >= 0.24 -> ExposureStatus.OVEREXPOSED
                else -> ExposureStatus.GOOD
            }
            return ExposureSnapshot(
                status = status,
                centerMeanLuma = cMean,
                borderMeanLuma = bMean,
                darkRatio = darkRatio,
                brightRatio = brightRatio
            )
        }

        /**
         * 直接基于 Y 平面计算 3x3 整数算子拉普拉斯方差（降采样到 <= 320x240）。
         */
        fun computeLaplacianSharpness(frame: GrayFrame): Double {
            val dstW = min(frame.width, LAPLACIAN_MAX_W)
            val dstH = min(frame.height, LAPLACIAN_MAX_H)
            if (dstW < 3 || dstH < 3) return 0.0

            val sampled = IntArray(dstW * dstH)
            val yData = frame.yPlane
            for (y in 0 until dstH) {
                val srcY = (y * frame.height) / dstH
                val srcRowOffset = srcY * frame.width
                val dstRowOffset = y * dstW
                for (x in 0 until dstW) {
                    val srcX = (x * frame.width) / dstW
                    sampled[dstRowOffset + x] = yData[srcRowOffset + srcX].toInt() and 0xFF
                }
            }

            val interiorCount = (dstW - 2) * (dstH - 2)
            var sum = 0.0
            var sumSq = 0.0
            for (y in 1 until dstH - 1) {
                val row = y * dstW
                val up = (y - 1) * dstW
                val down = (y + 1) * dstW
                for (x in 1 until dstW - 1) {
                    val lap = (sampled[up + x] + sampled[down + x] + sampled[row + x - 1] + sampled[row + x + 1]) -
                        4 * sampled[row + x]
                    val d = lap.toDouble()
                    sum += d
                    sumSq += d * d
                }
            }
            val mean = sum / interiorCount
            return max(0.0, (sumSq / interiorCount) - mean * mean)
        }

        /**
         * 下采样到 64x64 并做均值消去与 L2 归一化，使点积直接等于零均值 NCC ∈ [-1, 1]。
         */
        fun downsample64x64Normalized(frame: GrayFrame): FloatArray {
            val size = 64
            val out = FloatArray(size * size)
            val yData = frame.yPlane
            var sum = 0.0
            for (y in 0 until size) {
                val srcY = (y * frame.height) / size
                val srcRow = srcY * frame.width
                val dstRow = y * size
                for (x in 0 until size) {
                    val srcX = (x * frame.width) / size
                    val v = (yData[srcRow + srcX].toInt() and 0xFF).toFloat()
                    out[dstRow + x] = v
                    sum += v
                }
            }
            val mean = (sum / out.size).toFloat()
            var normSq = 0.0
            for (i in out.indices) {
                val centered = out[i] - mean
                out[i] = centered
                normSq += (centered * centered).toDouble()
            }
            val norm = sqrt(normSq).toFloat()
            if (norm > 1e-5f) {
                for (i in out.indices) {
                    out[i] /= norm
                }
            }
            return out
        }

        fun computeNcc(a: FloatArray, b: FloatArray): Double {
            if (a.size != b.size || a.isEmpty()) return 0.0
            var dot = 0.0
            var normA = 0.0
            var normB = 0.0
            for (i in a.indices) {
                val va = a[i].toDouble()
                val vb = b[i].toDouble()
                dot += va * vb
                normA += va * va
                normB += vb * vb
            }
            if (normA < 1e-8 || normB < 1e-8) return 0.0
            return dot.coerceIn(-1.0, 1.0)
        }

        /**
         * 轻量级 Y 通道梯度边缘投影前景估框（无需额外 ML 分割模型即可在端侧估算衣物/主体占比与四边留白）。
         */
        fun estimateSubjectBounds(frame: GrayFrame): SubjectFraming {
            val gw = 64
            val gh = 64
            val grid = IntArray(gw * gh)
            val yData = frame.yPlane
            for (y in 0 until gh) {
                val srcY = (y * frame.height) / gh
                val srcRow = srcY * frame.width
                val dstRow = y * gw
                for (x in 0 until gw) {
                    val srcX = (x * frame.width) / gw
                    grid[dstRow + x] = yData[srcRow + srcX].toInt() and 0xFF
                }
            }

            val grad = IntArray(gw * gh)
            var maxGrad = 0
            var sumGrad = 0L
            var count = 0
            for (y in 1 until gh - 1) {
                for (x in 1 until gw - 1) {
                    val gx = abs(grid[y * gw + x + 1] - grid[y * gw + x - 1])
                    val gy = abs(grid[(y + 1) * gw + x] - grid[(y - 1) * gw + x])
                    val g = gx + gy
                    grad[y * gw + x] = g
                    if (g > maxGrad) maxGrad = g
                    sumGrad += g
                    count += 1
                }
            }

            if (maxGrad < 20) {
                return SubjectFraming(false, 0.0, 0, 0, 0, 0, reliable = true)
            }

            val meanGrad = sumGrad.toDouble() / max(1, count)
            val threshold = max(22.0, meanGrad * 1.4).toInt()

            var minX = gw
            var maxX = -1
            var minY = gh
            var maxY = -1
            var activeCount = 0

            var leftBorderHit = false
            var rightBorderHit = false
            var topBorderHit = false
            var bottomBorderHit = false
            var borderCount = 0
            var borderActiveCount = 0
            var borderSum = 0.0
            var borderSumSq = 0.0

            for (y in 1 until gh - 1) {
                val isTopBorder = y <= 3
                val isBottomBorder = y >= gh - 4
                for (x in 1 until gw - 1) {
                    val isLeftBorder = x <= 3
                    val isRightBorder = x >= gw - 4
                    val isBorder = isTopBorder || isBottomBorder || isLeftBorder || isRightBorder
                    val active = grad[y * gw + x] >= threshold
                    if (isBorder) {
                        borderCount += 1
                        val lum = grid[y * gw + x].toDouble()
                        borderSum += lum
                        borderSumSq += lum * lum
                        if (active) {
                            borderActiveCount += 1
                            if (isLeftBorder) leftBorderHit = true
                            if (isRightBorder) rightBorderHit = true
                            if (isTopBorder) topBorderHit = true
                            if (isBottomBorder) bottomBorderHit = true
                        }
                    }
                    if (active) {
                        activeCount += 1
                        if (x < minX) minX = x
                        if (x > maxX) maxX = x
                        if (y < minY) minY = y
                        if (y > maxY) maxY = y
                    }
                }
            }

            if (activeCount < 6 || maxX <= minX || maxY <= minY) {
                return SubjectFraming(false, 0.0, 0, 0, 0, 0, reliable = true)
            }

            val borderActiveSides = listOf(leftBorderHit, rightBorderHit, topBorderHit, bottomBorderHit).count { it }
            val borderActiveRatio = borderActiveCount.toDouble() / max(1, borderCount)
            val borderMean = borderSum / max(1, borderCount)
            val borderStd = sqrt(max(0.0, (borderSumSq / max(1, borderCount)) - borderMean * borderMean))

            // 仅在纯色/干净背景下，梯度外接框才可靠；宿舍等真实室内四边常有门框/家具/地砖强边缘
            val cleanOtherSidesStd = if (borderActiveSides == 1) {
                var cleanCount = 0
                var cleanSum = 0.0
                var cleanSumSq = 0.0
                for (y in 1 until gh - 1) {
                    for (x in 1 until gw - 1) {
                        val inCleanStrip =
                            (y <= 3 && !topBorderHit) ||
                                (y >= gh - 4 && !bottomBorderHit) ||
                                (x <= 3 && !leftBorderHit) ||
                                (x >= gw - 4 && !rightBorderHit)
                        if (inCleanStrip) {
                            cleanCount += 1
                            val lum = grid[y * gw + x].toDouble()
                            cleanSum += lum
                            cleanSumSq += lum * lum
                        }
                    }
                }
                val m = cleanSum / max(1, cleanCount)
                sqrt(max(0.0, (cleanSumSq / max(1, cleanCount)) - m * m))
            } else {
                borderStd
            }

            val reliable = when {
                borderActiveSides == 0 -> !(borderStd >= 22.0 && borderActiveRatio >= 0.05)
                borderActiveSides == 1 -> cleanOtherSidesStd < 22.0
                else -> false
            }

            // 注意 x/y 仅到 1..gw-2，若活跃边缘落在最外环 (1 或 gw-2)，映射回贴边 0 像素
            val leftNorm = if (minX <= 1) 0.0 else minX.toDouble() / gw
            val rightNorm = if (maxX >= gw - 2) 1.0 else (maxX + 1).toDouble() / gw
            val topNorm = if (minY <= 1) 0.0 else minY.toDouble() / gh
            val bottomNorm = if (maxY >= gh - 2) 1.0 else (maxY + 1).toDouble() / gh

            val occupancy = ((rightNorm - leftNorm) * (bottomNorm - topNorm)).coerceIn(0.0, 1.0)
            return SubjectFraming(
                detected = true,
                occupancy = occupancy,
                marginLeftPx = (leftNorm * frame.width).roundToInt(),
                marginTopPx = (topNorm * frame.height).roundToInt(),
                marginRightPx = ((1.0 - rightNorm) * frame.width).roundToInt().coerceAtLeast(0),
                marginBottomPx = ((1.0 - bottomNorm) * frame.height).roundToInt().coerceAtLeast(0),
                reliable = reliable
            )
        }
    }
}
