package com.ootd.ai.capture

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.sqrt

/**
 * 12 扇区环绕覆盖与相机 up 轴翻滚跟踪器（纯 Kotlin 实现，无 Android 依赖，便于 JVM 单测）。
 *
 * 与服务端 server/capture_guide.py 保持一致：
 * - 将 360° 均分为 12 个 30° 扇区；
 * - 以首帧方位角为 0° 基准计算相对偏航角 ∈ [0, 360)；
 * - |rollDeg| > 20° 触发水平倾斜预警，并记录全过程横滚角绝对值中位数写入 up_roll_median_deg。
 */
class OrientationSectorTracker {

    private var anchorAzimuthDeg: Double? = null
    private var lastValidAzimuthDeg: Double = 0.0

    private val sectorsCovered = BooleanArray(SECTOR_COUNT)
    private val capturedAngles = mutableListOf<Double>()
    private val rollSamplesAbs = mutableListOf<Double>()

    var latestRelativeAzimuthDeg: Double = 0.0
        private set

    var latestRollDeg: Double = 0.0
        private set

    fun reset() {
        anchorAzimuthDeg = null
        lastValidAzimuthDeg = 0.0
        sectorsCovered.fill(false)
        capturedAngles.clear()
        rollSamplesAbs.clear()
        latestRelativeAzimuthDeg = 0.0
        latestRollDeg = 0.0
    }

    /**
     * 从 SensorManager.getRotationMatrixFromVector 得到的 3x3 行主序旋转矩阵更新姿态。
     */
    fun updateFromRotationMatrix(r: FloatArray): OrientationSnapshot {
        require(r.size >= 9) { "旋转矩阵至少需要 9 个元素" }
        // 后置摄像头光轴在设备坐标系为 (0, 0, -1)，映射至世界坐标系 (East, North, Up)
        val fwdX = -r[2].toDouble()
        val fwdY = -r[5].toDouble()
        val fwdZ = -r[8].toDouble()

        val upX = r[1].toDouble()
        val upY = r[4].toDouble()
        val upZ = r[7].toDouble()

        val rightX = r[0].toDouble()
        val rightY = r[3].toDouble()
        val rightZ = r[6].toDouble()

        val horizNorm = sqrt(fwdX * fwdX + fwdY * fwdY)
        val rawAzimuthDeg = if (horizNorm > 1e-4) {
            normalize360(atan2(fwdX, fwdY) * RAD_TO_DEG)
        } else {
            lastValidAzimuthDeg
        }
        lastValidAzimuthDeg = rawAzimuthDeg

        // 水平参考右向 = fwd × worldUp = (fwdY, -fwdX, 0)
        val rollDeg = if (horizNorm > 1e-4) {
            val hRightX = fwdY / horizNorm
            val hRightY = -fwdX / horizNorm
            // 竖屏端平时 rightWorld 对齐 hRight；若设备绕光轴翻滚，upWorld 会倒向 hRight
            val cosRoll = rightX * hRightX + rightY * hRightY
            val sinRoll = upX * hRightX + upY * hRightY
            atan2(sinRoll, cosRoll) * RAD_TO_DEG
        } else {
            // 当俯仰接近天顶/天底时，用竖直分量退化估算倾斜角
            atan2(rightZ, upZ.coerceAtLeast(1e-4)) * RAD_TO_DEG
        }

        // 记录未使用 fwdZ/rightZ 的静默警告防止 lint，同时保留扩展空间
        @Suppress("UNUSED_VARIABLE")
        val unusedZ = fwdZ + rightZ
        return updateEulerDegrees(rawAzimuthDeg, rollDeg)
    }

    /**
     * 直接以绝对方位角与横滚角更新（供旋转矩阵换算与 JVM 单测复用）。
     */
    fun updateEulerDegrees(rawAzimuthDeg: Double, rollDeg: Double): OrientationSnapshot {
        val anchor = anchorAzimuthDeg ?: rawAzimuthDeg.also { anchorAzimuthDeg = it }
        val relativeDeg = normalize360(rawAzimuthDeg - anchor)
        val sectorIndex = angleToSectorIndex(relativeDeg)

        val isNewSector = !sectorsCovered[sectorIndex]
        sectorsCovered[sectorIndex] = true
        if (isNewSector || capturedAngles.isEmpty() || abs(capturedAngles.last() - relativeDeg) >= 5.0) {
            capturedAngles.add((relativeDeg * 10.0).toInt() / 10.0)
        }

        latestRelativeAzimuthDeg = relativeDeg
        latestRollDeg = rollDeg
        rollSamplesAbs.add(abs(rollDeg))

        return currentSnapshot()
    }

    fun currentSnapshot(): OrientationSnapshot {
        val coveredFlags = sectorsCovered.copyOf()
        val coveredCount = coveredFlags.count { it }
        return OrientationSnapshot(
            relativeAzimuthDeg = latestRelativeAzimuthDeg,
            currentSectorIndex = angleToSectorIndex(latestRelativeAzimuthDeg),
            coveredSectors = coveredFlags,
            coveredCount = coveredCount,
            rollDeg = latestRollDeg,
            isRollWarning = abs(latestRollDeg) > ROLL_WARNING_DEG,
            upRollMedianDeg = computeMedian(rollSamplesAbs)
        )
    }

    /** 返回目前已覆盖各扇区的代表角度（每扇区一个中心角 + 样本），与服务端口径一致。 */
    fun exportCoveredAzimuthDeg(): List<Double> {
        if (capturedAngles.isNotEmpty()) {
            return capturedAngles.toList()
        }
        return sectorsCovered.indices
            .filter { sectorsCovered[it] }
            .map { (it * SECTOR_DEG + SECTOR_DEG / 2).toDouble() }
    }

    fun exportMissingSectorArcs(): List<String> = summarize().missingArcs

    fun summarize(): CaptureSectorSummary {
        val coveredSet = sectorsCovered.indices.filter { sectorsCovered[it] }.toSet()
        return summarizeSectorSet(coveredSet)
    }

    companion object {
        const val SECTOR_COUNT = 12
        const val SECTOR_DEG = 360 / SECTOR_COUNT
        const val ROLL_WARNING_DEG = 20.0
        private const val RAD_TO_DEG = 180.0 / PI

        fun normalize360(deg: Double): Double {
            val mod = deg % 360.0
            return if (mod < 0.0) mod + 360.0 else mod
        }

        fun angleToSectorIndex(relativeAzimuthDeg: Double): Int {
            val norm = normalize360(relativeAzimuthDeg)
            return floor(norm / SECTOR_DEG).toInt().coerceIn(0, SECTOR_COUNT - 1)
        }

        fun summarizeAngles(anglesDeg: List<Double>): CaptureSectorSummary {
            val coveredSet = anglesDeg.map { angleToSectorIndex(it) }.toSet()
            return summarizeSectorSet(coveredSet)
        }

        private fun summarizeSectorSet(coveredSet: Set<Int>): CaptureSectorSummary {
            val missing = (0 until SECTOR_COUNT).filter { it !in coveredSet }
            val missingArcs = mergeMissingSectors(missing)
            val largestGapDeg = largestSectorGapDeg(coveredSet).toDouble()
            return CaptureSectorSummary(
                sectorCount = SECTOR_COUNT,
                coveredSectors = coveredSet.size,
                coverageRatio = ((coveredSet.size.toDouble() / SECTOR_COUNT) * 1000.0).toInt() / 1000.0,
                missingArcs = missingArcs,
                largestGapDeg = largestGapDeg
            )
        }

        fun mergeMissingSectors(missingIndices: List<Int>): List<String> {
            if (missingIndices.isEmpty()) return emptyList()
            val ordered = missingIndices.distinct().sorted()
            val arcs = mutableListOf<Pair<Int, Int>>()
            var start = ordered[0]
            var prev = ordered[0]
            for (idx in ordered.drop(1)) {
                if (idx == prev + 1) {
                    prev = idx
                } else {
                    arcs += start to prev
                    start = idx
                    prev = idx
                }
            }
            arcs += start to prev
            return arcs.map { (s, e) -> "${s * SECTOR_DEG}°-${(e + 1) * SECTOR_DEG}°" }
        }

        fun largestSectorGapDeg(coveredIndices: Set<Int>): Int {
            if (coveredIndices.isEmpty()) return 360
            if (coveredIndices.size >= SECTOR_COUNT) return 0
            var best = 0
            var cur = 0
            for (i in 0 until SECTOR_COUNT * 2) {
                if ((i % SECTOR_COUNT) !in coveredIndices) {
                    cur += 1
                    if (cur > best) best = cur
                } else {
                    cur = 0
                }
            }
            return best.coerceAtMost(SECTOR_COUNT) * SECTOR_DEG
        }

        fun computeMedian(values: List<Double>): Double {
            if (values.isEmpty()) return 0.0
            val sorted = values.sorted()
            val mid = sorted.size / 2
            return if (sorted.size % 2 == 1) {
                sorted[mid]
            } else {
                (sorted[mid - 1] + sorted[mid]) / 2.0
            }
        }

        /**
         * 将服务端返回的 "90°-180°" 等缺失弧段文本映射回 0..11 扇区索引，供补拍模式高亮提示。
         */
        fun parseGapSectorIndices(missingArcs: List<String>): Set<Int> {
            val indices = mutableSetOf<Int>()
            val regex = Regex("""(\d+)°?-(\d+)°?""")
            for (arc in missingArcs) {
                val match = regex.find(arc) ?: continue
                val startDeg = match.groupValues[1].toIntOrNull() ?: continue
                val endDeg = match.groupValues[2].toIntOrNull() ?: continue
                val startIdx = (startDeg / SECTOR_DEG).coerceIn(0, SECTOR_COUNT - 1)
                val endIdx = ((endDeg - 1) / SECTOR_DEG).coerceIn(0, SECTOR_COUNT - 1)
                for (i in startIdx..endIdx) {
                    indices += i
                }
            }
            return indices
        }
    }
}

data class OrientationSnapshot(
    val relativeAzimuthDeg: Double,
    val currentSectorIndex: Int,
    val coveredSectors: BooleanArray,
    val coveredCount: Int,
    val rollDeg: Double,
    val isRollWarning: Boolean,
    val upRollMedianDeg: Double
) {
    val coverageProgress: Float
        get() = coveredCount.toFloat() / OrientationSectorTracker.SECTOR_COUNT
}
