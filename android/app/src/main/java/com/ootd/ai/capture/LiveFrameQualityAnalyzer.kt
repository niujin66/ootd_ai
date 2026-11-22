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
    val marginBottomPx: Int
)

enum class CloseupCheckId(val label: String) {
    FACE_FRONT("正脸特写"),
    FACE_LEFT("左斜侧脸（-30°）"),
    FACE_RIGHT("右斜侧脸（+30°）"),
    GARMENT_COLLAR("领口/肩部细节"),
    GARMENT_CUFF("袖口/面料纹理")
}

data class LiveQualitySnapshot(
    val stage: CaptureStage,
    val trafficLight: TrafficLight,
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
    val analyzedFrames: Int
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

    private var consecutiveBlurFrames = 0
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
        consecutiveBlurFrames = 0
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

        val primaryFace = faces.maxByOrNull { (it.rightPx - it.leftPx) * (it.bottomPx - it.topPx) }
        val framing = evaluateFraming(frame, primaryFace)
        if (!framing.detected) {
            lostSubjectFrames += 1
        }

        if (stage == CaptureStage.STAGE_1_OVERVIEW && framing.detected) {
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
            if (framing.detected && framing.occupancy >= 0.45 && sharpness >= SHARP_OK_THRESHOLD) {
                stage2HighDetailFrames += 1
                // 连续推近高清晰度高占比帧，自动辅助点亮衣料纹理检查；关键结构可协同确认
                val detailEstPx = min(frame.width, frame.height) * framing.occupancy * 0.08
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
        val stage1CoveredEnough = (orientation?.coveredCount ?: 0) >= STAGE1_READY_SECTOR_MIN
        val stage1Ready = stage == CaptureStage.STAGE_1_OVERVIEW &&
            stage1CoveredEnough &&
            rollOk &&
            consecutiveBlurFrames == 0 &&
            (lostSubjectRatio() <= 0.25)

        val (light, prompt, voiceKey) = decideFeedback(
            orientation = orientation,
            sharpness = sharpness,
            ncc = ncc,
            framing = framing,
            eyeDistPx = eyeDistPx,
            stage1Ready = stage1Ready
        )

        val voiceHint = if (voiceKey != null && voiceKey != lastVoiceKey) {
            lastVoiceKey = voiceKey
            prompt
        } else {
            null
        }

        return LiveQualitySnapshot(
            stage = stage,
            trafficLight = light,
            promptText = prompt,
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
            analyzedFrames = analyzedFrameCount
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
        if (targetType == CaptureTargetType.HEAD && face != null) {
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
                marginBottomPx = (frame.height - estBottom).roundToInt().coerceAtLeast(0)
            )
        }
        return estimateSubjectBounds(frame)
    }

    private fun decideFeedback(
        orientation: OrientationSnapshot?,
        sharpness: Double,
        ncc: Double?,
        framing: SubjectFraming,
        eyeDistPx: Double?,
        stage1Ready: Boolean
    ): Triple<TrafficLight, String, String?> {
        if (consecutiveBlurFrames >= 3) {
            return Triple(TrafficLight.RED, "画面模糊，请放慢移动速度并稳住手机", "blur")
        }
        if (orientation != null && orientation.isRollWarning) {
            return Triple(TrafficLight.YELLOW, "请保持手机端平（当前倾斜 ${abs(orientation.rollDeg).roundToInt()}°）", "roll")
        }
        if (ncc != null && ncc < CONTINUITY_MIN_THRESHOLD) {
            return Triple(TrafficLight.YELLOW, "移动过快，请匀速缓慢环绕", "fast")
        }

        if (stage == CaptureStage.STAGE_1_OVERVIEW) {
            if (!framing.detected) {
                return Triple(TrafficLight.RED, "未检测到完整主体，请将目标移回画面中央", "lost")
            }
            if (framing.occupancy > OCCUPANCY_MAX_LIMIT) {
                return Triple(TrafficLight.RED, "距离过近（占比 ${(framing.occupancy * 100).roundToInt()}%），请后退半步", "too_close")
            }
            val minCheckedMargin = if (targetType == CaptureTargetType.HEAD) {
                minOf(framing.marginLeftPx, framing.marginRightPx, framing.marginTopPx)
            } else {
                minOf(framing.marginLeftPx, framing.marginRightPx, framing.marginTopPx, framing.marginBottomPx)
            }
            if (minCheckedMargin < MARGIN_MIN_PX) {
                val tip = if (targetType == CaptureTargetType.HEAD) "头顶或耳侧贴边" else "衣物边缘贴边"
                return Triple(TrafficLight.YELLOW, "${tip}（留白 ${minCheckedMargin}px < 40px），请略微后退", "margin")
            }
            if (stage1Ready) {
                return Triple(
                    TrafficLight.GREEN,
                    "远景扇区已达标！请被拍人保持不动，点击“进入推近阶段”慢推补细节",
                    "stage1_ready"
                )
            }
            val count = orientation?.coveredCount ?: 0
            val hint = if (framing.occupancy in 0.35..0.55) {
                "距离合适！匀速围绕点亮扇区（已完成 $count/12）"
            } else if (framing.occupancy < 0.35) {
                "主体略小，可稍微走近半步并绕行（已完成 $count/12）"
            } else {
                "稳住距离，缓慢围绕主体点亮扇区（已完成 $count/12）"
            }
            return Triple(TrafficLight.GREEN, hint, null)
        }

        // Stage 2：慢推近补细节
        if (targetType == CaptureTargetType.HEAD) {
            if (eyeDistPx == null) {
                return Triple(TrafficLight.YELLOW, "正处于阶段 2，请将镜头缓慢对准面部不要停顿", "stage2_no_face")
            }
            if (eyeDistPx < EYE_DETAIL_TARGET_PX) {
                return Triple(
                    TrafficLight.YELLOW,
                    "请再慢慢靠近些（当前眼部 ${eyeDistPx.roundToInt()}px，目标 ≥20px）",
                    "stage2_closer"
                )
            }
            val missing = computeMissingCloseups()
            return if (missing.isEmpty()) {
                Triple(TrafficLight.GREEN, "阶段 2 正/左/右面部特写全部达标！可点击完成并质检", "stage2_done")
            } else {
                Triple(TrafficLight.GREEN, "眼部清晰达标（${eyeDistPx.roundToInt()}px），请缓慢微转补拍：${missing.first()}", "stage2_next_${missing.first()}")
            }
        } else {
            val missing = computeMissingCloseups()
            return if (missing.isEmpty()) {
                Triple(TrafficLight.GREEN, "衣物领口与纹理特写已完成！可点击完成并质检", "stage2_garment_done")
            } else {
                Triple(TrafficLight.GREEN, "保持手机缓慢推近领口/袖口（建议 30–50 cm）", null)
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
                return SubjectFraming(false, 0.0, 0, 0, 0, 0)
            }

            val meanGrad = sumGrad.toDouble() / max(1, count)
            val threshold = max(22.0, meanGrad * 1.4).toInt()

            var minX = gw
            var maxX = -1
            var minY = gh
            var maxY = -1
            var activeCount = 0

            for (y in 1 until gh - 1) {
                for (x in 1 until gw - 1) {
                    if (grad[y * gw + x] >= threshold) {
                        activeCount += 1
                        if (x < minX) minX = x
                        if (x > maxX) maxX = x
                        if (y < minY) minY = y
                        if (y > maxY) maxY = y
                    }
                }
            }

            if (activeCount < 6 || maxX <= minX || maxY <= minY) {
                return SubjectFraming(false, 0.0, 0, 0, 0, 0)
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
                marginBottomPx = ((1.0 - bottomNorm) * frame.height).roundToInt().coerceAtLeast(0)
            )
        }
    }
}
