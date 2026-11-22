package com.ootd.ai.capture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureGuideParserTest {

    @Test
    fun `encodeRequest serializes full two-stage metrics accurately`() {
        val req = CaptureGuideRequest(
            requestId = "android-test-001",
            targetType = CaptureTargetType.HEAD,
            subjectLabel = "person-002",
            coveredAzimuthDeg = listOf(0.0, 30.0, 60.0, 90.0),
            upRollMedianDeg = 12.4,
            minMarginPx = MarginPx(left = 96, right = 102, top = 140, bottom = 0),
            sharpnessP50 = 48.2,
            orbitContinuityMin = 0.51,
            roiDetailPx = 24.5,
            subjectOccupancyMax = 0.48,
            lostSubjectRatio = 0.0,
            hasScaleReference = true,
            frameCount = 42,
            missingCloseups = listOf("左斜侧脸（-30°）")
        )

        val json = JSONObject(CaptureGuideParser.encodeRequest(req))
        assertEquals("android-test-001", json.getString("request_id"))
        assertEquals("head", json.getString("target_type"))
        assertEquals("person-002", json.getString("subject_label"))
        assertEquals(4, json.getJSONArray("covered_azimuth_deg").length())
        assertEquals(12.4, json.getDouble("up_roll_median_deg"), 1e-6)
        assertEquals(140, json.getJSONObject("min_margin_px").getInt("top"))
        assertEquals(24.5, json.getDouble("roi_detail_px"), 1e-6)
        assertTrue(json.getBoolean("has_scale_reference"))
        assertEquals(1, json.getJSONArray("missing_closeups").length())
    }

    @Test
    fun `parseResponse parses valid ai guide and sorts steps by priority`() {
        val raw = """
            {
              "request_id": "req-99",
              "target_type": "head",
              "subject_label": "person-002",
              "readiness": "needs_reshoot",
              "mode": "ai",
              "diagnosis": {
                "target_type": "head",
                "subject_label": "person-002",
                "readiness": "needs_reshoot",
                "issue_count": 1,
                "sector_summary": {
                  "sector_count": 12,
                  "covered_sectors": 9,
                  "coverage_ratio": 0.75,
                  "missing_arcs": ["270°-360°"],
                  "largest_gap_deg": 90.0
                },
                "issues": [
                  {
                    "code": "azimuth_partial_gap",
                    "severity": "major",
                    "area": "环绕方位覆盖",
                    "detail": "缺失后侧视角"
                  }
                ]
              },
              "guide": {
                "summary": "请补拍背面视角。",
                "can_reuse_existing": true,
                "steps": [
                  {
                    "priority": 2,
                    "target_area": "右耳特写",
                    "action": "缓慢靠近右耳补拍 3 秒。",
                    "reason": "近景右偏角缺口。"
                  },
                  {
                    "priority": 1,
                    "target_area": "后脑弧段 270°-360°",
                    "action": "从右侧转到正后方慢速补录一遍。",
                    "reason": "该区间未点亮扇区。"
                  }
                ]
              }
            }
        """.trimIndent()

        val parsed = CaptureGuideParser.parseResponse(raw)
        assertFalse(parsed.isPass)
        assertFalse(parsed.isMock)
        assertEquals(9, parsed.diagnosis.sectorSummary.coveredSectors)
        assertEquals(listOf("270°-360°"), parsed.diagnosis.sectorSummary.missingArcs)
        assertEquals(2, parsed.guide.steps.size)
        assertEquals(1, parsed.guide.steps[0].priority)
        assertEquals("后脑弧段 270°-360°", parsed.guide.steps[0].targetArea)
    }

    @Test
    fun `parseResponse rejects unknown readiness or mode`() {
        val badReadiness = """
            {
              "request_id": "r1",
              "target_type": "head",
              "readiness": "maybe",
              "mode": "ai",
              "diagnosis": {
                "target_type": "head",
                "readiness": "maybe",
                "issue_count": 0,
                "sector_summary": {"sector_count": 12, "missing_arcs": []},
                "issues": []
              },
              "guide": {"summary": "ok", "can_reuse_existing": true, "steps": []}
            }
        """.trimIndent()
        assertThrows(IllegalArgumentException::class.java) {
            CaptureGuideParser.parseResponse(badReadiness)
        }
    }

    @Test
    fun `api sends bearer header and maps http errors into CaptureGuideApiException`() {
        var capturedUrl = ""
        var capturedAuth = ""
        val api = CaptureGuideApi(
            baseUrl = "http://10.0.2.2:8000/",
            authToken = "secret-token",
            transport = CaptureGuideTransport { url, headers, _ ->
                capturedUrl = url
                capturedAuth = headers["Authorization"].orEmpty()
                HttpTransportResponse(
                    statusCode = 503,
                    body = """{"error":{"message":"模型服务超时"}}"""
                )
            }
        )

        val err = assertThrows(CaptureGuideApiException::class.java) {
            api.requestGuide(
                CaptureGuideRequest(
                    requestId = "req-503",
                    targetType = CaptureTargetType.GARMENT
                )
            )
        }
        assertEquals("http://10.0.2.2:8000/v1/capture/guide", capturedUrl)
        assertEquals("Bearer secret-token", capturedAuth)
        assertEquals(503, err.statusCode)
        assertTrue(err.message.contains("超时"))
    }

    @Test
    fun `offline fallback diagnoses sector gap and blur clearly`() {
        val req = CaptureGuideRequest(
            requestId = "off-1",
            targetType = CaptureTargetType.HEAD,
            coveredAzimuthDeg = listOf(0.0, 30.0, 60.0),
            upRollMedianDeg = 25.0,
            sharpnessP50 = 15.0,
            roiDetailPx = 10.0,
            missingCloseups = listOf("右斜侧脸（+30°）")
        )
        val fallback = OfflineCaptureDiagnoser.buildFallback(req, "连接超时")
        assertTrue(fallback.isOfflineFallback)
        assertEquals("invalid_retake", fallback.readiness)
        assertTrue(fallback.guide.steps.isNotEmpty())
        assertTrue(fallback.guide.summary.contains("离线"))
    }
}
