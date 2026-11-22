package com.ootd.ai.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OrientationSectorTrackerTest {

    @Test
    fun `tracks 12 sectors relative to start azimuth across 360 wrap`() {
        val tracker = OrientationSectorTracker()
        // 第一帧朝向 210° 作为 0° 起点
        val s0 = tracker.updateEulerDegrees(210.0, 2.0)
        assertEquals(0.0, s0.relativeAzimuthDeg, 1e-6)
        assertEquals(0, s0.currentSectorIndex)
        assertEquals(1, s0.coveredCount)

        // 转过 360° 边界到 15° -> 相对角 165°（扇区 5）
        val s1 = tracker.updateEulerDegrees(15.0, -5.0)
        assertEquals(165.0, s1.relativeAzimuthDeg, 1e-6)
        assertEquals(5, s1.currentSectorIndex)
        assertEquals(2, s1.coveredCount)
        assertFalse(s1.isRollWarning)

        // 倾斜 26° 应触发水平预警，且中位数反映历史绝对倾角
        val s2 = tracker.updateEulerDegrees(190.0, 26.0)
        assertTrue(s2.isRollWarning)
        // abs samples: [2.0, 5.0, 26.0] -> median = 5.0
        assertEquals(5.0, s2.upRollMedianDeg, 1e-6)
    }

    @Test
    fun `summarizeAngles merges consecutive missing sectors cleanly`() {
        // 缺少扇区 3,4,5 (90°-180°) 和扇区 10 (300°-330°)
        val present = listOf(0, 1, 2, 6, 7, 8, 9, 11).map { it * 30.0 + 5.0 }
        val summary = OrientationSectorTracker.summarizeAngles(present)
        assertEquals(8, summary.coveredSectors)
        assertEquals(90.0, summary.largestGapDeg!!, 1e-6)
        assertEquals(listOf("90°-180°", "300°-330°"), summary.missingArcs)
        assertEquals(setOf(3, 4, 5, 10), OrientationSectorTracker.parseGapSectorIndices(summary.missingArcs))
    }

    @Test
    fun `updateFromRotationMatrix handles upright portrait and 90-degree clockwise roll`() {
        val tracker = OrientationSectorTracker()
        // 竖屏正北：right=East(1,0,0), up=Up(0,0,1), out=South(0,-1,0)
        val uprightNorth = floatArrayOf(
            1f, 0f, 0f,
            0f, 0f, -1f,
            0f, 1f, 0f
        )
        val snap0 = tracker.updateFromRotationMatrix(uprightNorth)
        assertEquals(0.0, snap0.rollDeg, 1e-3)
        assertFalse(snap0.isRollWarning)

        // 顺时针翻滚 90°（屏幕顶指向东）：right=Down(0,0,-1), up=East(1,0,0), out=South(0,-1,0)
        val rolled90 = floatArrayOf(
            0f, 1f, 0f,
            0f, 0f, -1f,
            -1f, 0f, 0f
        )
        val snap1 = tracker.updateFromRotationMatrix(rolled90)
        assertEquals(90.0, snap1.rollDeg, 1e-3)
        assertTrue(snap1.isRollWarning)
    }
}
