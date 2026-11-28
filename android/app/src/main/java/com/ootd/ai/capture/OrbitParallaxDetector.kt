package com.ootd.ai.capture

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt

enum class OrbitMotionState {
    STATIONARY,
    TRUE_ORBIT,
    WRIST_TWIST_SUSPECTED,
    INSUFFICIENT_TEXTURE
}

data class OrbitParallaxSnapshot(
    val state: OrbitMotionState,
    val centerFlowDx: Double,
    val borderFlowDx: Double,
    val parallaxGapPx: Double,
    val parallaxRatio: Double,
    val deltaAzimuthDeg: Double,
    val validCenterPatches: Int,
    val validBorderPatches: Int,
    val consecutiveTwistFrames: Int,
    val totalTwistRejectedFrames: Int,
    val totalTwistEvents: Int
)

/**
 * 端侧「真绕行 vs 原地扭手腕」轻量级前景/背景稀疏光流视差检测器（纯 Kotlin，JVM 可测）。
 *
 * 物理依据：
 * - 原地扭手腕（纯旋转相机）：场景近似受单应矩阵支配，前景中心块与左右背景块在画面上以近乎相同方向与速度横移；
 * - 真正迈步绕行（平移 + 旋转以目标为中心）：目标始终保持在屏幕中央（centerFlowDx ≈ 0），而背景沿反方向显著横移，
 *   前景/背景之间产生明显的运动视差（parallaxGapPx = |borderFlowDx - centerFlowDx|）。
 */
class OrbitParallaxDetector {

    private var prev64Gray: FloatArray? = null
    private var prevRelativeAzimuthDeg: Double? = null
    private var consecutiveTwistFrames = 0
    private var totalTwistRejectedFrames = 0
    private var totalTwistEvents = 0
    private var lastState: OrbitMotionState = OrbitMotionState.STATIONARY

    fun reset() {
        prev64Gray = null
        prevRelativeAzimuthDeg = null
        consecutiveTwistFrames = 0
        totalTwistRejectedFrames = 0
        totalTwistEvents = 0
        lastState = OrbitMotionState.STATIONARY
    }

    fun analyze(frame: GrayFrame, relativeAzimuthDeg: Double?): OrbitParallaxSnapshot {
        val curr64 = downsample64x64Gray(frame)
        val prev = prev64Gray
        val prevAzimuth = prevRelativeAzimuthDeg
        prev64Gray = curr64
        if (relativeAzimuthDeg != null) {
            prevRelativeAzimuthDeg = relativeAzimuthDeg
        }

        val deltaAzimuthDeg = if (relativeAzimuthDeg != null && prevAzimuth != null) {
            shortestSignedAngleDiff(relativeAzimuthDeg, prevAzimuth)
        } else {
            0.0
        }

        if (prev == null) {
            lastState = OrbitMotionState.STATIONARY
            return OrbitParallaxSnapshot(
                state = OrbitMotionState.STATIONARY,
                centerFlowDx = 0.0,
                borderFlowDx = 0.0,
                parallaxGapPx = 0.0,
                parallaxRatio = 0.0,
                deltaAzimuthDeg = deltaAzimuthDeg,
                validCenterPatches = 0,
                validBorderPatches = 0,
                consecutiveTwistFrames = 0,
                totalTwistRejectedFrames = totalTwistRejectedFrames,
                totalTwistEvents = totalTwistEvents
            )
        }

        val centerFlows = mutableListOf<Double>()
        for ((px, py) in CENTER_PATCHES) {
            if (patchStd(curr64, px, py) < MIN_PATCH_STD) continue
            val dx = matchPatchHorizontalDx(prev, curr64, px, py) ?: continue
            centerFlows += dx
        }

        val borderFlows = mutableListOf<Double>()
        for ((px, py) in BORDER_PATCHES) {
            if (patchStd(curr64, px, py) < MIN_PATCH_STD) continue
            val dx = matchPatchHorizontalDx(prev, curr64, px, py) ?: continue
            borderFlows += dx
        }

        if (centerFlows.isEmpty() || borderFlows.size < 2) {
            consecutiveTwistFrames = 0
            lastState = OrbitMotionState.INSUFFICIENT_TEXTURE
            return OrbitParallaxSnapshot(
                state = OrbitMotionState.INSUFFICIENT_TEXTURE,
                centerFlowDx = 0.0,
                borderFlowDx = 0.0,
                parallaxGapPx = 0.0,
                parallaxRatio = 0.0,
                deltaAzimuthDeg = deltaAzimuthDeg,
                validCenterPatches = centerFlows.size,
                validBorderPatches = borderFlows.size,
                consecutiveTwistFrames = 0,
                totalTwistRejectedFrames = totalTwistRejectedFrames,
                totalTwistEvents = totalTwistEvents
            )
        }

        val centerDx = medianOf(centerFlows)
        val borderDx = medianOf(borderFlows)
        val parallaxGapPx = abs(borderDx - centerDx)
        val maxFlow = max(abs(borderDx), abs(centerDx))
        val parallaxRatio = if (maxFlow > 1e-3) parallaxGapPx / maxFlow else 0.0
        val sameDirection = (centerDx * borderDx) > 0.0
        val hasYawOrPan = (abs(deltaAzimuthDeg) >= MIN_AZIMUTH_DELTA_DEG) || (maxFlow >= MIN_PAN_FLOW_PX)

        val pureRotationEvidence = hasYawOrPan &&
            sameDirection &&
            abs(centerDx) >= MIN_TWIST_FLOW_PX &&
            abs(borderDx) >= MIN_TWIST_FLOW_PX &&
            parallaxRatio <= MAX_TWIST_PARALLAX_RATIO

        val trueOrbitEvidence = parallaxGapPx >= MIN_ORBIT_PARALLAX_GAP_PX &&
            abs(centerDx) <= max(MIN_ORBIT_PARALLAX_GAP_PX, abs(borderDx) * MAX_ORBIT_CENTER_RATIO)

        val state = when {
            pureRotationEvidence -> {
                consecutiveTwistFrames += 1
                if (consecutiveTwistFrames >= TWIST_CONSECUTIVE_REQUIRED) {
                    if (lastState != OrbitMotionState.WRIST_TWIST_SUSPECTED) {
                        totalTwistEvents += 1
                    }
                    totalTwistRejectedFrames += 1
                    OrbitMotionState.WRIST_TWIST_SUSPECTED
                } else {
                    if (abs(deltaAzimuthDeg) >= MIN_AZIMUTH_DELTA_DEG || maxFlow >= 1.0) {
                        OrbitMotionState.TRUE_ORBIT
                    } else {
                        OrbitMotionState.STATIONARY
                    }
                }
            }
            trueOrbitEvidence -> {
                consecutiveTwistFrames = 0
                OrbitMotionState.TRUE_ORBIT
            }
            maxFlow < 1.0 && abs(deltaAzimuthDeg) < MIN_AZIMUTH_DELTA_DEG -> {
                consecutiveTwistFrames = 0
                OrbitMotionState.STATIONARY
            }
            else -> {
                consecutiveTwistFrames = 0
                OrbitMotionState.TRUE_ORBIT
            }
        }

        lastState = state
        return OrbitParallaxSnapshot(
            state = state,
            centerFlowDx = centerDx,
            borderFlowDx = borderDx,
            parallaxGapPx = parallaxGapPx,
            parallaxRatio = parallaxRatio,
            deltaAzimuthDeg = deltaAzimuthDeg,
            validCenterPatches = centerFlows.size,
            validBorderPatches = borderFlows.size,
            consecutiveTwistFrames = consecutiveTwistFrames,
            totalTwistRejectedFrames = totalTwistRejectedFrames,
            totalTwistEvents = totalTwistEvents
        )
    }

    companion object {
        const val GRID_SIZE = 64
        const val PATCH_SIZE = 8
        const val SEARCH_DX = 10
        const val SEARCH_DY = 2
        const val MIN_PATCH_STD = 8.0
        const val MAX_MATCH_ZMAD = 26.0
        const val MIN_AZIMUTH_DELTA_DEG = 2.5
        const val MIN_PAN_FLOW_PX = 2.0
        const val MIN_TWIST_FLOW_PX = 1.5
        const val MAX_TWIST_PARALLAX_RATIO = 0.35
        const val MIN_ORBIT_PARALLAX_GAP_PX = 1.6
        const val MAX_ORBIT_CENTER_RATIO = 0.48
        const val TWIST_CONSECUTIVE_REQUIRED = 2

        private val CENTER_PATCHES = listOf(
            22 to 20,
            34 to 20,
            22 to 34,
            34 to 34
        )

        private val BORDER_PATCHES = listOf(
            6 to 16,
            6 to 28,
            6 to 40,
            50 to 16,
            50 to 28,
            50 to 40
        )

        fun shortestSignedAngleDiff(currDeg: Double, prevDeg: Double): Double {
            var diff = (currDeg - prevDeg) % 360.0
            if (diff > 180.0) diff -= 360.0
            if (diff < -180.0) diff += 360.0
            return diff
        }

        fun downsample64x64Gray(frame: GrayFrame): FloatArray {
            val out = FloatArray(GRID_SIZE * GRID_SIZE)
            val yData = frame.yPlane
            for (y in 0 until GRID_SIZE) {
                val srcY = (y * frame.height) / GRID_SIZE
                val srcRow = srcY * frame.width
                val dstRow = y * GRID_SIZE
                for (x in 0 until GRID_SIZE) {
                    val srcX = (x * frame.width) / GRID_SIZE
                    out[dstRow + x] = (yData[srcRow + srcX].toInt() and 0xFF).toFloat()
                }
            }
            return out
        }

        private fun patchMean(grid: FloatArray, x0: Int, y0: Int): Double {
            var sum = 0.0
            for (dy in 0 until PATCH_SIZE) {
                val row = (y0 + dy) * GRID_SIZE + x0
                for (dx in 0 until PATCH_SIZE) {
                    sum += grid[row + dx]
                }
            }
            return sum / (PATCH_SIZE * PATCH_SIZE)
        }

        private fun patchStd(grid: FloatArray, x0: Int, y0: Int): Double {
            val mean = patchMean(grid, x0, y0)
            var sumSq = 0.0
            for (dy in 0 until PATCH_SIZE) {
                val row = (y0 + dy) * GRID_SIZE + x0
                for (dx in 0 until PATCH_SIZE) {
                    val d = grid[row + dx] - mean
                    sumSq += d * d
                }
            }
            return sqrt(sumSq / (PATCH_SIZE * PATCH_SIZE))
        }

        /**
         * 在 prev 帧搜索 curr(x0, y0) 的对应位置，返回帧间位移 (currX - prevX) = -bestShiftX。
         * 使用零均值绝对差（ZMAD），对全局自动曝光亮度偏移保持鲁棒。
         */
        private fun matchPatchHorizontalDx(prev: FloatArray, curr: FloatArray, x0: Int, y0: Int): Double? {
            val currMean = patchMean(curr, x0, y0)
            var bestCost = Double.MAX_VALUE
            var bestShiftX = 0

            for (sy in -SEARCH_DY..SEARCH_DY) {
                val py0 = y0 + sy
                if (py0 < 0 || py0 + PATCH_SIZE > GRID_SIZE) continue
                for (sx in -SEARCH_DX..SEARCH_DX) {
                    val px0 = x0 + sx
                    if (px0 < 0 || px0 + PATCH_SIZE > GRID_SIZE) continue
                    val prevMean = patchMean(prev, px0, py0)
                    var sad = 0.0
                    for (dy in 0 until PATCH_SIZE) {
                        val cRow = (y0 + dy) * GRID_SIZE + x0
                        val pRow = (py0 + dy) * GRID_SIZE + px0
                        for (dx in 0 until PATCH_SIZE) {
                            val cNorm = curr[cRow + dx] - currMean
                            val pNorm = prev[pRow + dx] - prevMean
                            sad += abs(cNorm - pNorm)
                        }
                    }
                    val zmad = sad / (PATCH_SIZE * PATCH_SIZE)
                    if (zmad < bestCost || (abs(zmad - bestCost) < 1e-6 && abs(sx) < abs(bestShiftX))) {
                        bestCost = zmad
                        bestShiftX = sx
                    }
                }
            }

            if (bestCost > MAX_MATCH_ZMAD) return null
            // patch 在上一帧位于 x0 + bestShiftX，在当前帧位于 x0，故特征水平位移为 -bestShiftX
            return (-bestShiftX).toDouble()
        }

        private fun medianOf(values: List<Double>): Double {
            val sorted = values.sorted()
            val mid = sorted.size / 2
            return if (sorted.size % 2 == 1) {
                sorted[mid]
            } else {
                (sorted[mid - 1] + sorted[mid]) / 2.0
            }
        }
    }
}
