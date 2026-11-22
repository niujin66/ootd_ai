package com.ootd.ai.capture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class DirectDeepSeekCaptureGuideTest {

    @Test
    fun `deterministic diagnosis detects severe azimuth gap and low eye px`() {
        val req = CaptureGuideRequest(
            requestId = "dbg-1",
            targetType = CaptureTargetType.HEAD,
            subjectLabel = "person-01",
            coveredAzimuthDeg = listOf(0.0, 30.0, 60.0),
            upRollMedianDeg = 25.0,
            sharpnessP50 = 42.0,
            roiDetailPx = 10.0,
            hasScaleReference = false
        )

        val diag = DirectDeepSeekCaptureGuide.analyzeMetricsDeterministically(req)
        assertEquals("invalid_retake", diag.readiness)
        assertTrue(diag.issues.any { it.code == "azimuth_severe_gap" })
        assertTrue(diag.issues.any { it.code == "camera_roll_warning" })
        assertTrue(diag.issues.any { it.code == "roi_resolution_low" })
        assertTrue(diag.issues.any { it.code == "missing_scale_reference" })
    }

    @Test
    fun `direct client sends bearer key parses markdown-fenced json and returns direct_ai`() {
        var capturedUrl = ""
        var capturedAuth = ""
        var capturedModel = ""

        val fakeResponseJson = """
            {
              "choices": [
                {
                  "message": {
                    "content": "```json\n{\"summary\":\"请按步骤补拍背面与眼部。\",\"can_reuse_existing\":true,\"steps\":[{\"priority\":1,\"target_area\":\"背面角度\",\"action\":\"缓慢绕到背面补拍缺口\",\"reason\":\"漏拍90度区间\"}]}\n```"
                  }
                }
              ]
            }
        """.trimIndent()

        val client = DirectDeepSeekCaptureGuide(
            baseUrl = "https://api.deepseek.com/v1/",
            apiKey = "sk-test-debug-key",
            model = "deepseek-chat",
            transport = CaptureGuideTransport { url, headers, body ->
                capturedUrl = url
                capturedAuth = headers["Authorization"].orEmpty()
                capturedModel = JSONObject(body).getString("model")
                HttpTransportResponse(200, fakeResponseJson)
            }
        )

        val resp = client.generateGuide(
            CaptureGuideRequest(
                requestId = "dbg-2",
                targetType = CaptureTargetType.HEAD,
                coveredAzimuthDeg = (0..9).map { it * 30.0 },
                roiDetailPx = 12.0,
                sharpnessP50 = 45.0,
                hasScaleReference = true
            )
        )

        assertEquals("https://api.deepseek.com/v1/chat/completions", capturedUrl)
        assertEquals("Bearer sk-test-debug-key", capturedAuth)
        assertEquals("deepseek-chat", capturedModel)
        assertTrue(resp.isDirectAi)
        assertEquals("needs_reshoot", resp.readiness)
        assertEquals(1, resp.guide.steps.size)
    }

    @Test
    fun `rejects contradictory model output claiming invalid_retake can reuse existing`() {
        val diag = DirectDeepSeekCaptureGuide.analyzeMetricsDeterministically(
            CaptureGuideRequest(
                requestId = "dbg-3",
                targetType = CaptureTargetType.HEAD,
                coveredAzimuthDeg = listOf(0.0, 30.0)
            )
        )
        assertEquals("invalid_retake", diag.readiness)

        val contradictory = """
            {
              "summary": "勉强能用",
              "can_reuse_existing": true,
              "steps": [
                {"priority": 1, "target_area": "背面", "action": "补拍一圈", "reason": "覆盖不足"}
              ]
            }
        """.trimIndent()

        assertThrows(IllegalArgumentException::class.java) {
            DirectDeepSeekCaptureGuide.parseAndValidateGuideOutput(contradictory, diag)
        }
    }

    @Test
    fun `blank api key fails fast without network call`() {
        val client = DirectDeepSeekCaptureGuide(apiKey = "   ")
        val err = assertThrows(DirectDeepSeekException::class.java) {
            client.generateGuide(
                CaptureGuideRequest(requestId = "dbg-4", targetType = CaptureTargetType.GARMENT)
            )
        }
        assertFalse(err.message.isBlank())
    }
}
