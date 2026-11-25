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

    private fun makeClutteredRoomFrame(w: Int, h: Int): GrayFrame {
        val buf = ByteArray(w * h) { 120 }
        // 模拟真实宿舍/室内四边环境边缘（门框、踢脚线、柜边）与中央衣物
        for (y in 0 until h) {
            for (x in 0 until w) {
                val isBorderClutter =
                    (x in 4 until 24 && (y % 16) < 8) ||
                        (x in (w - 24) until (w - 4) && ((y + 8) % 16) < 8) ||
                        (y in 4 until 24 && (x % 16) < 8) ||
                        (y in (h - 24) until (h - 4) && ((x + 8) % 16) < 8)
                val isGarment = x in (w / 3) until (2 * w / 3) && y in (h / 4) until (3 * h / 4)
                val v = when {
                    isBorderClutter -> 235
                    isGarment && (((x / 8) + (y / 8)) % 2 == 0) -> 210
                    isGarment -> 80
                    else -> 120
                }
                buf[y * w + x] = v.toByte()
            }
        }
        return GrayFrame(w, h, buf)
    }

    @Test
    fun `garment edge estimation detects centered vs single-side cropped box`() {
        val centered = makeCenteredBoxFrame(400, 400, 100, 80, 300, 320)
        val boundsOk = LiveFrameQualityAnalyzer.estimateSubjectBounds(centered)
        assertTrue(boundsOk.detected)
        assertTrue(boundsOk.reliable)
        assertTrue(boundsOk.occupancy in 0.20..0.45)
        assertTrue(boundsOk.marginLeftPx >= 40)
        assertTrue(boundsOk.marginTopPx >= 40)

        // 纯色背景单侧贴边裁切应保留可靠告警
        val singleSideCropped = makeCenteredBoxFrame(400, 400, 0, 80, 220, 320)
        val boundsCropped = LiveFrameQualityAnalyzer.estimateSubjectBounds(singleSideCropped)
        assertTrue(boundsCropped.detected)
        assertTrue(boundsCropped.reliable)
        assertEquals(0, boundsCropped.marginLeftPx)
        val cropSnap = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT).analyzeFrame(singleSideCropped, null)
        assertEquals(FramingStatus.CROPPED, cropSnap.framingStatus)

        // 纯色背景下居中但占比 > 65% 应正常报 TOO_CLOSE
        val tooCloseClean = makeCenteredBoxFrame(600, 600, 50, 50, 550, 550)
        val tooCloseSnap = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT).analyzeFrame(tooCloseClean, null)
        assertEquals(FramingStatus.TOO_CLOSE, tooCloseSnap.framingStatus)
    }

    @Test
    fun `cluttered indoor background does not falsely trigger step back for garment`() {
        val roomFrame = makeClutteredRoomFrame(400, 400)
        val bounds = LiveFrameQualityAnalyzer.estimateSubjectBounds(roomFrame)
        assertTrue(bounds.detected)
        assertFalse(bounds.reliable)

        val tracker = OrientationSectorTracker()
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT)
        var lastSnap: LiveQualitySnapshot? = null
        for (i in 0 until 10) {
            val orient = tracker.updateEulerDegrees(i * 32.0, 2.0)
            lastSnap = analyzer.analyzeFrame(roomFrame, orient)
            assertEquals(TrafficLight.GREEN, lastSnap.trafficLight)
            assertEquals(FramingStatus.MANUAL_GUIDE, lastSnap.framingStatus)
            assertFalse("不应误报往后退: ${lastSnap.promptText}", lastSnap.promptText.contains("后退"))
            assertFalse("不应误报太近: ${lastSnap.promptText}", lastSnap.promptText.contains("太近"))
            assertFalse("徽章不应报后退: ${lastSnap.actionBadge}", lastSnap.actionBadge.contains("后退"))
        }
        assertTrue(lastSnap!!.stage1ReadyForCloseup)

        // 阶段 2 在复杂背景下推近仍可点亮领口/袖口检查
        analyzer.enterStage2Closeup()
        for (i in 0 until 4) {
            lastSnap = analyzer.analyzeFrame(roomFrame, tracker.currentSnapshot())
        }
        assertTrue(lastSnap!!.missingCloseups.isEmpty())

        val req = analyzer.buildGuideRequest(
            requestId = "req-garment-room",
            prep = CapturePrepConfig(targetType = CaptureTargetType.GARMENT, hasScaleReference = true),
            tracker = tracker
        )
        assertEquals(null, req.minMarginPx)
        assertEquals(null, req.subjectOccupancyMax)

        val diag = DirectDeepSeekCaptureGuide.analyzeMetricsDeterministically(req)
        val issueCodes = diag.issues.map { it.code }.toSet()
        assertFalse(issueCodes.contains("edge_cropped"))
        assertFalse(issueCodes.contains("subject_too_close"))
    }

    @Test
    fun `head back-of-head frames without face detection do not trigger false step back`() {
        val tracker = OrientationSectorTracker()
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.HEAD)
        val roomFrame = makeClutteredRoomFrame(480, 640)
        val goodFace = FaceObservation(
            leftPx = 150f,
            topPx = 180f,
            rightPx = 330f,
            bottomPx = 360f,
            eyeDistancePx = 12.0,
            headEulerAngleYDeg = 0f
        )

        for (i in 0 until 5) {
            val orient = tracker.updateEulerDegrees(i * 30.0, 3.0)
            val snap = analyzer.analyzeFrame(roomFrame, orient, listOf(goodFace))
            assertEquals(FramingStatus.GOOD, snap.framingStatus)
        }

        // 绕到后脑勺时人脸检测为空，不应触发“往后退”或污染 stage1 占比
        for (i in 5 until 10) {
            val orient = tracker.updateEulerDegrees(i * 30.0, 3.0)
            val snap = analyzer.analyzeFrame(roomFrame, orient, emptyList())
            assertEquals(FramingStatus.MANUAL_GUIDE, snap.framingStatus)
            assertEquals(TrafficLight.GREEN, snap.trafficLight)
            assertFalse(snap.promptText.contains("后退"))
        }

        val req = analyzer.buildGuideRequest(
            requestId = "req-head-back",
            prep = CapturePrepConfig(targetType = CaptureTargetType.HEAD, hasScaleReference = true),
            tracker = tracker
        )
        assertTrue("Head occMax should stay from reliable face frames <= 0.65, was ${req.subjectOccupancyMax}", req.subjectOccupancyMax!! <= 0.65)
    }
}
