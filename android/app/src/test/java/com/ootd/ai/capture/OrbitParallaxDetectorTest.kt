package com.ootd.ai.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.cos
import kotlin.math.sin

class OrbitParallaxDetectorTest {

    /**
     * 构造高纹理前景目标与可独立平移的纹理背景帧（用于合成纯旋转 vs 真绕行序列）。
     * 前景占中心区域 [w*0.30, w*0.70] x [h*0.25, h*0.75]；其余区域为高频纹理背景。
     */
    private fun makeParallaxScene(
        w: Int = 256,
        h: Int = 256,
        fgShiftX: Int,
        bgShiftX: Int,
        brightnessBias: Int = 0
    ): GrayFrame {
        val buf = ByteArray(w * h)
        val fgLeft = (w * 0.30).toInt() + fgShiftX
        val fgRight = (w * 0.70).toInt() + fgShiftX
        val fgTop = (h * 0.25).toInt()
        val fgBottom = (h * 0.75).toInt()

        for (y in 0 until h) {
            for (x in 0 until w) {
                val inFg = x in fgLeft until fgRight && y in fgTop until fgBottom
                val base = if (inFg) {
                    val sx = x - fgShiftX
                    val sy = y
                    val v = 135.0 + 65.0 * sin(sx * 0.16) * cos(sy * 0.14)
                    v.toInt()
                } else {
                    val bx = x - bgShiftX
                    val v = 115.0 + 70.0 * sin(bx * 0.18) + 35.0 * cos(y * 0.15)
                    v.toInt()
                }
                buf[y * w + x] = (base + brightnessBias).coerceIn(0, 255).toByte()
            }
        }
        return GrayFrame(w, h, buf)
    }

    private fun makeBlankWallScene(w: Int = 256, h: Int = 256): GrayFrame {
        return GrayFrame(w, h, ByteArray(w * h) { 210.toByte() })
    }

    @Test
    fun `pure wrist twist moves foreground and background together and is flagged`() {
        val detector = OrbitParallaxDetector()
        // 连续同向平移（前景和背景在 64x64 下采样中同步右移 4 px，相当于 256px 帧中移动 16 px）
        val f0 = makeParallaxScene(fgShiftX = 0, bgShiftX = 0)
        val f1 = makeParallaxScene(fgShiftX = 16, bgShiftX = 16)
        val f2 = makeParallaxScene(fgShiftX = 32, bgShiftX = 32)

        val s0 = detector.analyze(f0, 0.0)
        assertEquals(OrbitMotionState.STATIONARY, s0.state)

        val s1 = detector.analyze(f1, 10.0)
        // 第一帧刚起手防抖，不误触发
        assertFalse(s1.state == OrbitMotionState.WRIST_TWIST_SUSPECTED)

        val s2 = detector.analyze(f2, 20.0)
        assertEquals(OrbitMotionState.WRIST_TWIST_SUSPECTED, s2.state)
        assertTrue("Expected positive center flow, got ${s2.centerFlowDx}", s2.centerFlowDx >= 2.0)
        assertTrue("Expected positive border flow, got ${s2.borderFlowDx}", s2.borderFlowDx >= 2.0)
        assertTrue("Expected low parallax ratio <= 0.35, got ${s2.parallaxRatio}", s2.parallaxRatio <= 0.35)
        assertEquals(1, s2.totalTwistEvents)
        assertEquals(1, s2.totalTwistRejectedFrames)
    }

    @Test
    fun `true walking orbit keeps foreground centered while background shifts oppositely`() {
        val detector = OrbitParallaxDetector()
        // 真绕行：目标留在居中位置 (fgShiftX=0)，背景朝反方向滑动 (bgShiftX = -20, -40)
        val f0 = makeParallaxScene(fgShiftX = 0, bgShiftX = 0)
        val f1 = makeParallaxScene(fgShiftX = 0, bgShiftX = -20)
        val f2 = makeParallaxScene(fgShiftX = 0, bgShiftX = -40)

        detector.analyze(f0, 0.0)
        val s1 = detector.analyze(f1, 12.0)
        val s2 = detector.analyze(f2, 24.0)

        assertEquals(OrbitMotionState.TRUE_ORBIT, s1.state)
        assertEquals(OrbitMotionState.TRUE_ORBIT, s2.state)
        assertTrue("Expected parallaxGapPx >= 2.0, got ${s2.parallaxGapPx}", s2.parallaxGapPx >= 2.0)
        assertTrue("Expected |centerFlowDx| <= 1.0, got ${s2.centerFlowDx}", kotlin.math.abs(s2.centerFlowDx) <= 1.0)
        assertEquals(0, s2.totalTwistRejectedFrames)
    }

    @Test
    fun `low texture white wall degrades safely to insufficient texture without false positive`() {
        val detector = OrbitParallaxDetector()
        val wall1 = makeBlankWallScene()
        val wall2 = makeBlankWallScene()

        detector.analyze(wall1, 0.0)
        val snap = detector.analyze(wall2, 18.0)

        assertEquals(OrbitMotionState.INSUFFICIENT_TEXTURE, snap.state)
        assertEquals(0, snap.totalTwistRejectedFrames)
    }

    @Test
    fun `exposure brightness shift does not break zero-mean block matching`() {
        val detector = OrbitParallaxDetector()
        val f0 = makeParallaxScene(fgShiftX = 0, bgShiftX = 0, brightnessBias = 0)
        val f1 = makeParallaxScene(fgShiftX = 16, bgShiftX = 16, brightnessBias = 25)
        val f2 = makeParallaxScene(fgShiftX = 32, bgShiftX = 32, brightnessBias = 30)

        detector.analyze(f0, 0.0)
        detector.analyze(f1, 10.0)
        val s2 = detector.analyze(f2, 20.0)

        assertEquals(OrbitMotionState.WRIST_TWIST_SUSPECTED, s2.state)
    }

    @Test
    fun `detector recovers immediately from wrist twist to true orbit`() {
        val detector = OrbitParallaxDetector()
        detector.analyze(makeParallaxScene(fgShiftX = 0, bgShiftX = 0), 0.0)
        detector.analyze(makeParallaxScene(fgShiftX = 16, bgShiftX = 16), 8.0)
        val twistSnap = detector.analyze(makeParallaxScene(fgShiftX = 32, bgShiftX = 32), 16.0)
        assertEquals(OrbitMotionState.WRIST_TWIST_SUSPECTED, twistSnap.state)

        // 改回迈步绕行：中心稳住、背景大幅相对滑动
        val orbitSnap = detector.analyze(makeParallaxScene(fgShiftX = 32, bgShiftX = 8), 26.0)
        assertEquals(OrbitMotionState.TRUE_ORBIT, orbitSnap.state)
        assertEquals(0, orbitSnap.consecutiveTwistFrames)
    }

    @Test
    fun `orientation sector tracker gate blocks unverified rotation until committed`() {
        val tracker = OrientationSectorTracker(requireOrbitVerification = true)
        val s0 = tracker.updateEulerDegrees(100.0, 1.0)
        assertEquals(1, s0.coveredCount)

        // 转过 45 度到扇区 1，但未经视觉视差提交，不能提前点亮
        val s1 = tracker.updateEulerDegrees(145.0, 2.0)
        assertEquals(1, s1.coveredCount)
        assertEquals(1, s1.currentSectorIndex)

        // 判定为扭手腕并拦截
        val sRejected = tracker.recordWristTwistRejected()
        assertEquals(1, sRejected.coveredCount)
        assertEquals(1, sRejected.wristTwistRejectedCount)

        // 再转到扇区 3 并通过视觉绕行提交 -> 正式点亮第 2 个扇区
        tracker.updateEulerDegrees(195.0, 1.5)
        val sCommitted = tracker.commitVerifiedCurrentSector()
        assertEquals(2, sCommitted.coveredCount)
        assertTrue(sCommitted.coveredSectors[0])
        assertTrue(sCommitted.coveredSectors[3])
        assertFalse(sCommitted.coveredSectors[1])
    }

    @Test
    fun `live frame quality analyzer surfaces yellow warning badge on wrist twist`() {
        val tracker = OrientationSectorTracker(requireOrbitVerification = true)
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT)

        // 每次平移 8px（64x64 网格约 2px），既保持画面连续性 NCC >= 0.35，又明确命中同向无视差扭手腕
        val f0 = makeParallaxScene(fgShiftX = 0, bgShiftX = 0)
        val f1 = makeParallaxScene(fgShiftX = 8, bgShiftX = 8)
        val f2 = makeParallaxScene(fgShiftX = 16, bgShiftX = 16)

        analyzer.analyzeFrame(f0, tracker.updateEulerDegrees(0.0, 1.0))
        analyzer.analyzeFrame(f1, tracker.updateEulerDegrees(12.0, 1.0))
        val snap2 = analyzer.analyzeFrame(f2, tracker.updateEulerDegrees(24.0, 1.0))

        assertEquals(OrbitMotionState.WRIST_TWIST_SUSPECTED, snap2.orbitMotionState)
        assertEquals(TrafficLight.YELLOW, snap2.trafficLight)
        assertTrue("Expected wrist twist badge, got: ${snap2.actionBadge}", snap2.actionBadge.contains("扭手腕"))
        assertTrue("Expected wrist twist prompt, got: ${snap2.promptText}", snap2.promptText.contains("扭手腕"))
        assertEquals(1, snap2.wristTwistRejectedFrames)
    }
}
