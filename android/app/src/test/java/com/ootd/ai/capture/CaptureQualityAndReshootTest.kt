package com.ootd.ai.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.cos
import kotlin.math.sin

class CaptureQualityAndReshootTest {

    private fun makeUniformLumaFrame(w: Int, h: Int, centerLuma: Int, borderLuma: Int): GrayFrame {
        val buf = ByteArray(w * h)
        for (y in 0 until h) {
            val inBorderY = y < (h / 8) || y >= (h * 7 / 8)
            for (x in 0 until w) {
                val inBorderX = x < (w / 6) || x >= (w * 5 / 6)
                val base = if (inBorderX || inBorderY) borderLuma else centerLuma
                // 加入微弱纹理使清晰度不至于全零，避免混淆曝光与纯模糊测试
                val tex = if (((x / 4) + (y / 4)) % 2 == 0) 6 else -6
                buf[y * w + x] = (base + tex).coerceIn(0, 255).toByte()
            }
        }
        return GrayFrame(w, h, buf)
    }

    private fun makeSharpTexturedFrame(w: Int = 256, h: Int = 256, centerLuma: Int = 150, borderLuma: Int = 140): GrayFrame {
        val buf = ByteArray(w * h)
        for (y in 0 until h) {
            for (x in 0 until w) {
                val isCenter = x in (w / 4) until (w * 3 / 4) && y in (h / 5) until (h * 4 / 5)
                val base = if (isCenter) centerLuma else borderLuma
                // 8x8 棋盘格高频细节纹理，保证清晰度分数远高于 35.0
                val delta = if (((x / 8) + (y / 8)) % 2 == 0) 45 else -45
                buf[y * w + x] = (base + delta).coerceIn(0, 255).toByte()
            }
        }
        return GrayFrame(w, h, buf)
    }

    @Test
    fun `evaluateExposure distinguishes good, underexposed, overexposed and backlit frames`() {
        val good = makeSharpTexturedFrame(centerLuma = 145, borderLuma = 135)
        assertEquals(ExposureStatus.GOOD, LiveFrameQualityAnalyzer.evaluateExposure(good).status)

        val dark = makeUniformLumaFrame(128, 128, centerLuma = 30, borderLuma = 35)
        assertEquals(ExposureStatus.UNDEREXPOSED, LiveFrameQualityAnalyzer.evaluateExposure(dark).status)

        val bright = makeUniformLumaFrame(128, 128, centerLuma = 252, borderLuma = 230)
        assertEquals(ExposureStatus.OVEREXPOSED, LiveFrameQualityAnalyzer.evaluateExposure(bright).status)

        val backlit = makeUniformLumaFrame(128, 128, centerLuma = 65, borderLuma = 225)
        assertEquals(ExposureStatus.BACKLIT, LiveFrameQualityAnalyzer.evaluateExposure(backlit).status)
    }

    @Test
    fun `two consecutive backlit frames trigger yellow backlight correction warning`() {
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT)
        val backlit = makeUniformLumaFrame(128, 128, centerLuma = 70, borderLuma = 220)

        val s1 = analyzer.analyzeFrame(backlit, null)
        assertEquals(ExposureStatus.BACKLIT, s1.exposureStatus)
        assertFalse(s1.frameQualityAcceptableForSector)

        val s2 = analyzer.analyzeFrame(backlit, null)
        assertEquals(TrafficLight.YELLOW, s2.trafficLight)
        assertTrue("Expected backlight badge, got ${s2.actionBadge}", s2.actionBadge.contains("逆光"))
        assertTrue(s2.promptText.contains("逆光"))
    }

    @Test
    fun `orientation sector tracker tracks three-state quality and upgrades weak sector on good frame`() {
        val tracker = OrientationSectorTracker(requireOrbitVerification = true)
        val s0 = tracker.updateEulerDegrees(0.0, 1.0)
        assertEquals(SectorQuality.GOOD, s0.sectorQualities[0])
        assertEquals(1, s0.coveredCount)

        // 转到扇区 2（65 度），首先遇到低质量帧（模糊或逆光）-> 标记黄格 NEEDS_IMPROVEMENT，不增加覆盖数
        tracker.updateEulerDegrees(65.0, 2.0)
        val sWeak = tracker.recordSectorObservation(qualityGood = false, rejectedByTwist = false)
        assertEquals(1, sWeak.coveredCount)
        assertEquals(1, sWeak.weakSectorCount)
        assertEquals(SectorQuality.NEEDS_IMPROVEMENT, sWeak.sectorQualities[2])

        // 随后在该扇区拍到合格帧 -> 升级为 GOOD 绿格并计入覆盖数
        val sUpgraded = tracker.recordSectorObservation(qualityGood = true, rejectedByTwist = false)
        assertEquals(2, sUpgraded.coveredCount)
        assertEquals(0, sUpgraded.weakSectorCount)
        assertEquals(SectorQuality.GOOD, sUpgraded.sectorQualities[2])
    }

    @Test
    fun `targeted reshoot defect regions merge into expected sector set`() {
        val cfg = CapturePrepConfig(
            targetType = CaptureTargetType.GARMENT,
            isTargetedReshoot = true,
            defectRegions = setOf(ModelDefectRegion.RIGHT_SEAM, ModelDefectRegion.BACK),
            defectKind = ModelDefectKind.MISSING_PATCH
        )
        val sectors = cfg.resolvedTargetSectors()
        assertEquals(setOf(2, 3, 4, 5, 6, 7), sectors)
    }

    @Test
    fun `sharp well-exposed frame is marked acceptable for sector coverage`() {
        val analyzer = LiveFrameQualityAnalyzer(CaptureTargetType.GARMENT)
        val tracker = OrientationSectorTracker(requireOrbitVerification = true)
        val crisp = makeSharpTexturedFrame(256, 256, centerLuma = 150, borderLuma = 140)

        val o0 = tracker.updateEulerDegrees(0.0, 2.0)
        val snap = analyzer.analyzeFrame(crisp, o0)
        assertEquals(ExposureStatus.GOOD, snap.exposureStatus)
        assertTrue("Expected crisp well-exposed frame acceptable, sharpness=${snap.sharpness}", snap.frameQualityAcceptableForSector)
    }

    private fun makeGuideResponse(
        readiness: String,
        canReuseExisting: Boolean,
        missingArcs: List<String>,
        issueCodes: List<String> = emptyList(),
        steps: List<CaptureGuideStep> = emptyList()
    ): CaptureGuideResponse {
        return CaptureGuideResponse(
            requestId = "req-test",
            targetType = "garment",
            subjectLabel = "top-003",
            readiness = readiness,
            diagnosis = CaptureDiagnosis(
                targetType = "garment",
                subjectLabel = "top-003",
                readiness = readiness,
                issueCount = issueCodes.size,
                sectorSummary = CaptureSectorSummary(
                    sectorCount = 12,
                    coveredSectors = 12 - missingArcs.size * 3,
                    coverageRatio = 0.75,
                    missingArcs = missingArcs,
                    largestGapDeg = if (missingArcs.isEmpty()) 30.0 else 90.0
                ),
                issues = issueCodes.map { CaptureIssue(it, "major", "质量", "test") }
            ),
            guide = CaptureGuidePlan(
                summary = "test summary",
                canReuseExisting = canReuseExisting,
                steps = steps
            ),
            mode = "ai"
        )
    }

    @Test
    fun `ai needs_reshoot response maps missing arcs to targeted back patch plan`() {
        val resp = makeGuideResponse(
            readiness = "needs_reshoot",
            canReuseExisting = true,
            missingArcs = listOf("150°-240°"),
            issueCodes = listOf("azimuth_partial_gap"),
            steps = listOf(CaptureGuideStep(1, "背面印花", "绕背面补拍", "missing"))
        )
        val plan = resp.buildTargetedReshootPlan()
        assertTrue(plan.canUseIncrementalPatch)
        assertEquals(setOf(5, 6, 7), plan.targetSectors)
        assertTrue(ModelDefectRegion.BACK in plan.inferredRegions)
        assertEquals(ModelDefectKind.MISSING_PATCH, plan.inferredDefectKind)
    }

    @Test
    fun `ai needs_reshoot merges weak yellow sectors and infers blurry defect kind`() {
        val resp = makeGuideResponse(
            readiness = "needs_reshoot",
            canReuseExisting = true,
            missingArcs = emptyList(),
            issueCodes = listOf("blur_detected"),
            steps = listOf(CaptureGuideStep(1, "右袖与侧缝", "放慢移动并拍清袖口", "blur"))
        )
        val plan = resp.buildTargetedReshootPlan(weakSectors = setOf(2, 3))
        assertTrue(plan.canUseIncrementalPatch)
        assertEquals(setOf(2, 3), plan.targetSectors)
        assertEquals(ModelDefectKind.BLURRY_OR_DARK, plan.inferredDefectKind)
        assertTrue(ModelDefectRegion.RIGHT_SEAM in plan.inferredRegions)
        assertTrue(ModelDefectRegion.SLEEVES in plan.inferredRegions)
    }

    @Test
    fun `invalid_retake with canReuseExisting false forbids incremental reshoot`() {
        val resp = makeGuideResponse(
            readiness = "invalid_retake",
            canReuseExisting = false,
            missingArcs = listOf("150°-240°"),
            issueCodes = listOf("azimuth_severe_gap")
        )
        val plan = resp.buildTargetedReshootPlan(weakSectors = setOf(1, 2))
        assertFalse(plan.canUseIncrementalPatch)
    }
}
