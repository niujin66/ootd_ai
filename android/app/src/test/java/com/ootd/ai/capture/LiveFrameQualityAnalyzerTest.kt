package com.ootd.ai.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveFrameQualityAnalyzerTest {

    private fun makeUniformFrame(w: Int, h: Int, value: Int): GrayFrame {
        return GrayFrame(w, h, ByteArray(w * h) { value.toByte() })
    }

    private fun makeCenteredBoxFrame(w: Int, h: Int, left: Int, top: Int, right: Int, bottom: Int): GrayFrame {
        val buf = ByteArray(w * h) { 30 }
        for (y in top until bottom.coerceAtMost(h)) {
            for (x in left until right.coerceAtMost(w)) {
                // 棋盘格子高对比纹理，既有强外缘也能产生高拉普拉斯清晰度
                val v = if (((x / 8) + (y / 8)) % 2 == 0) 220 else 90
                buf[y * w + x] = v.toByte()
            }
        }
        return GrayFrame(w, h, buf)
    }

    @Test
    fun `laplacian sharpness distinguishes crisp checkerboard from blank blurry frame`() {
        val sharp = makeCenteredBoxFrame(320, 240, 60, 40, 260, 200)
        val flat = makeUniformFrame(320, 240, 128)

        val sharpScore = LiveFrameQualityAnalyzer.computeLaplacianSharpness(sharp)
        val flatScore = LiveFrameQualityAnalyzer.computeLaplacianSharpness(flat)

        assertTrue("Expected sharp frame >= 35, got $sharpScore", sharpScore >= 35.0)
        assertEquals(0.0, flatScore, 1e-6)
    }

    @Test
    fun `three consecutive blurry frames raise red blur warning`() {
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT)
        val flat = makeUniformFrame(160, 120, 100)
        val s1 = analyzer.analyzeFrame(flat, null)
        val s2 = analyzer.analyzeFrame(flat, null)
        val s3 = analyzer.analyzeFrame(flat, null)

        assertFalse(s1.trafficLight == TrafficLight.RED && s1.promptText.contains("模糊"))
        assertFalse(s2.trafficLight == TrafficLight.RED && s2.promptText.contains("模糊"))
        assertEquals(TrafficLight.RED, s3.trafficLight)
        assertTrue(s3.promptText.contains("模糊"))
    }

    @Test
    fun `ncc drops when scene shifts drastically between frames`() {
        val f1 = makeCenteredBoxFrame(128, 128, 10, 10, 50, 110)
        val f2 = makeCenteredBoxFrame(128, 128, 78, 10, 118, 110)
        val v1 = LiveFrameQualityAnalyzer.downsample64x64Normalized(f1)
        val v2 = LiveFrameQualityAnalyzer.downsample64x64Normalized(f2)

        assertEquals(1.0, LiveFrameQualityAnalyzer.computeNcc(v1, v1), 1e-4)
        val cross = LiveFrameQualityAnalyzer.computeNcc(v1, v2)
        assertTrue("Expected shifted frame NCC < 0.35, got $cross", cross < 0.35)
    }

    @Test
    fun `head two-stage flow preserves stage1 occupancy and checks off stage2 facial angles`() {
        val tracker = OrientationSectorTracker()
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.HEAD)
        val sharpFrame = makeCenteredBoxFrame(480, 640, 100, 120, 380, 520)

        // 模拟阶段 1 覆盖 10 个扇区，占比在 35%-55% 的绿色区间
        val goodFace = FaceObservation(
            leftPx = 150f,
            topPx = 180f,
            rightPx = 330f,
            bottomPx = 360f,
            eyeDistancePx = 12.0,
            headEulerAngleYDeg = 0f
        )
        var lastSnap: LiveQualitySnapshot? = null
        for (i in 0 until 10) {
            val orient = tracker.updateEulerDegrees(i * 32.0, 4.0)
            lastSnap = analyzer.analyzeFrame(sharpFrame, orient, listOf(goodFace))
        }
        assertTrue(lastSnap!!.stage1ReadyForCloseup)
        assertTrue(lastSnap.subjectOccupancy in 0.25..0.60)

        // 进入阶段 2：推近特写（占比更大不应污染 stage1 subject_occupancy_max）
        analyzer.enterStage2Closeup()
        val closeAngles = listOf(0f, -25f, 25f)
        for (yaw in closeAngles) {
            val closeFace = FaceObservation(
                leftPx = 40f,
                topPx = 60f,
                rightPx = 440f,
                bottomPx = 560f,
                eyeDistancePx = 28.0,
                headEulerAngleYDeg = yaw
            )
            lastSnap = analyzer.analyzeFrame(sharpFrame, tracker.currentSnapshot(), listOf(closeFace))
        }

        assertTrue(lastSnap.missingCloseups.isEmpty())
        val req = analyzer.buildGuideRequest(
            requestId = "req-head-ok",
            prep = CapturePrepConfig(targetType = CaptureTargetType.HEAD, hasScaleReference = true),
            tracker = tracker
        )
        assertTrue("Stage1 occMax should stay <= 0.65, was ${req.subjectOccupancyMax}", req.subjectOccupancyMax!! <= 0.65)
        assertEquals(28.0, req.roiDetailPx!!, 1e-6)
        assertTrue(req.missingCloseups.isEmpty())
    }

    @Test
    fun `garment edge estimation detects centered vs cropped box`() {
        val centered = makeCenteredBoxFrame(400, 400, 100, 80, 300, 320)
        val boundsOk = LiveFrameQualityAnalyzer.estimateSubjectBounds(centered)
        assertTrue(boundsOk.detected)
        assertTrue(boundsOk.occupancy in 0.20..0.45)
        assertTrue(boundsOk.marginLeftPx >= 40)
        assertTrue(boundsOk.marginTopPx >= 40)

        val cropped = makeCenteredBoxFrame(400, 400, 0, 0, 395, 395)
        val boundsCropped = LiveFrameQualityAnalyzer.estimateSubjectBounds(cropped)
        assertTrue(boundsCropped.detected)
        assertTrue(boundsCropped.occupancy > 0.65)
        assertEquals(0, boundsCropped.marginLeftPx)
    }
}
