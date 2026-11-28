package com.ootd.ai.capture

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * 手机端开发者调试配置（仅保存在本机 SharedPreferences，绝不写入仓库或硬编码密钥）。
 */
data class CaptureDebugConfig(
    val directDeepSeekEnabled: Boolean = false,
    val deepSeekBaseUrl: String = DEFAULT_DEEPSEEK_BASE_URL,
    val deepSeekApiKey: String = "",
    val deepSeekModel: String = DEFAULT_DEEPSEEK_MODEL,
    val customServerBaseUrl: String = ""
) {
    companion object {
        const val DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
        const val DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
    }
}

class DirectDeepSeekException(
    val statusCode: Int?,
    override val message: String
) : IOException(message)

/**
 * 端侧直连 OpenAI 兼容接口（如 DeepSeek）的采集补拍指导客户端（纯 Kotlin，便于 JVM 单测）。
 *
 * 与服务端 server/capture_guide.py 保持完全一致的原则：
 * 1. 先由端侧基于客观采集指标确定性生成结构化诊断 CaptureDiagnosis；
 * 2. 仅把数值诊断 JSON 发给大模型做分步文案翻译，绝不上传个人原图/视频，也不让模型编造几何数据；
 * 3. 对模型返回的 JSON 做严格校验（如 invalid_retake 不得声称 can_reuse_existing=true）。
 */
class DirectDeepSeekCaptureGuide(
    private val baseUrl: String = CaptureDebugConfig.DEFAULT_DEEPSEEK_BASE_URL,
    private val apiKey: String,
    private val model: String = CaptureDebugConfig.DEFAULT_DEEPSEEK_MODEL,
    private val transport: CaptureGuideTransport = defaultTransport()
) {

    fun generateGuide(request: CaptureGuideRequest): CaptureGuideResponse {
        val cleanKey = apiKey.trim()
        if (cleanKey.isBlank()) {
            throw DirectDeepSeekException(null, "未填写 DeepSeek API Key")
        }
        val diagnosis = analyzeMetricsDeterministically(request)
        val endpoint = "${baseUrl.trimEnd('/')}/chat/completions"
        val headers = mapOf(
            "Authorization" to "Bearer $cleanKey",
            "Content-Type" to "application/json; charset=utf-8",
            "Accept" to "application/json"
        )
        val payload = buildChatPayload(diagnosis, model.ifBlank { CaptureDebugConfig.DEFAULT_DEEPSEEK_MODEL })
        val httpResp = transport.postJson(endpoint, headers, payload)
        if (httpResp.statusCode !in 200..299) {
            val errMsg = extractErrorMessage(httpResp.statusCode, httpResp.body)
            throw DirectDeepSeekException(httpResp.statusCode, "DeepSeek 接口返回错误：$errMsg")
        }

        val content = extractChatContent(httpResp.body)
        val plan = parseAndValidateGuideOutput(content, diagnosis)
        return CaptureGuideResponse(
            requestId = request.requestId,
            targetType = diagnosis.targetType,
            subjectLabel = diagnosis.subjectLabel,
            readiness = diagnosis.readiness,
            diagnosis = diagnosis,
            guide = plan,
            mode = MODE_DIRECT_AI
        )
    }

    companion object {
        const val MODE_DIRECT_AI = "direct_ai"
        private const val CONNECT_TIMEOUT_MS = 10_000
        private const val READ_TIMEOUT_MS = 25_000

        private val SEVERITY_RANK = mapOf("critical" to 0, "major" to 1, "minor" to 2)
        private val INVALIDATING_CODES = setOf(
            "edge_cropped",
            "azimuth_severe_gap",
            "camera_roll_severe",
            "blur_severe"
        )

        fun analyzeMetricsDeterministically(req: CaptureGuideRequest): CaptureDiagnosis {
            val targetType = req.targetType.apiValue
            val issues = mutableListOf<CaptureIssue>()

            // 1. 12 扇区环绕覆盖分析
            val sectorSummary: CaptureSectorSummary = when {
                req.coveredAzimuthDeg != null -> OrientationSectorTracker.summarizeAngles(req.coveredAzimuthDeg)
                req.azimuthSpanDeg != null -> {
                    val span = req.azimuthSpanDeg.coerceIn(0.0, 360.0)
                    val gap = ((360.0 - span) * 10.0).roundToInt() / 10.0
                    CaptureSectorSummary(
                        sectorCount = OrientationSectorTracker.SECTOR_COUNT,
                        coveredSectors = null,
                        coverageRatio = ((span / 360.0) * 1000.0).roundToInt() / 1000.0,
                        missingArcs = emptyList(),
                        largestGapDeg = gap
                    )
                }
                else -> CaptureSectorSummary(OrientationSectorTracker.SECTOR_COUNT, null, null, emptyList(), null)
            }

            if (req.coveredAzimuthDeg != null) {
                val covered = sectorSummary.coveredSectors ?: 0
                val gap = sectorSummary.largestGapDeg ?: 360.0
                val arcsText = sectorSummary.missingArcs.joinToString(", ")
                if (gap >= 90.0 || covered <= 6) {
                    issues += CaptureIssue(
                        code = "azimuth_severe_gap",
                        severity = "critical",
                        area = "环绕方位覆盖",
                        detail = "仅覆盖 $covered/12 个 30° 扇区，最大空缺 ${gap.roundToInt()}°（缺失区间：$arcsText）"
                    )
                } else if (sectorSummary.missingArcs.isNotEmpty()) {
                    issues += CaptureIssue(
                        code = "azimuth_partial_gap",
                        severity = "major",
                        area = "环绕方位覆盖",
                        detail = "覆盖 $covered/12 个扇区，缺失区间：$arcsText"
                    )
                }
            }

            // 2. 相机翻滚偏差
            req.upRollMedianDeg?.let { rawRoll ->
                val roll = abs(rawRoll)
                if (roll > 45.0) {
                    issues += CaptureIssue(
                        code = "camera_roll_severe",
                        severity = "critical",
                        area = "相机姿态稳定性",
                        detail = "相机翻滚中位偏差达 ${"%.1f".format(roll)}°（>45°），严重影响多视角匹配"
                    )
                } else if (roll > 20.0) {
                    issues += CaptureIssue(
                        code = "camera_roll_warning",
                        severity = "major",
                        area = "相机姿态稳定性",
                        detail = "相机翻滚中位偏差 ${"%.1f".format(roll)}°（>20°），建议保持水平稳定拍摄"
                    )
                }
            }

            // 3. 四边留白与主体占比
            req.minMarginPx?.let { m ->
                val checked = if (targetType == "head") {
                    listOf("left" to m.left, "right" to m.right, "top" to m.top)
                } else {
                    listOf("left" to m.left, "right" to m.right, "top" to m.top, "bottom" to m.bottom)
                }
                val cropped = checked.filter { it.second <= 5 }
                val tight = checked.filter { it.second in 6..39 }
                if (cropped.isNotEmpty()) {
                    issues += CaptureIssue(
                        code = "edge_cropped",
                        severity = "critical",
                        area = "画面留白与边界",
                        detail = "边缘已贴边/裁切（≤5 px）：${cropped.joinToString { "${it.first}=${it.second}px" }}"
                    )
                } else if (tight.isNotEmpty()) {
                    issues += CaptureIssue(
                        code = "edge_tight_margin",
                        severity = "major",
                        area = "画面留白与边界",
                        detail = "边缘留白不足（<40 px）：${tight.joinToString { "${it.first}=${it.second}px" }}"
                    )
                }
            }

            req.subjectOccupancyMax?.let { occ ->
                if (occ > 0.65) {
                    issues += CaptureIssue(
                        code = "subject_too_close",
                        severity = "major",
                        area = "拍摄距离与构图",
                        detail = "主体占画面高达 ${(occ * 100).roundToInt()}%（>65%），距离过近易致头顶或耳侧出画"
                    )
                }
            }

            req.lostSubjectRatio?.let { lr ->
                if (lr > 0.25) {
                    issues += CaptureIssue(
                        code = "subject_lost_frames",
                        severity = if (lr >= 0.5) "critical" else "major",
                        area = "主体追踪稳定性",
                        detail = "约 ${(lr * 100).roundToInt()}% 采样帧未检测到完整主体"
                    )
                }
            }

            // 4. 清晰度与连续度
            req.sharpnessP50?.let { sharp ->
                if (sharp < 20.0) {
                    issues += CaptureIssue(
                        code = "blur_severe",
                        severity = "critical",
                        area = "画面清晰度",
                        detail = "清晰度中位数仅 ${"%.1f".format(sharp)}（<20），整体运动模糊或失焦"
                    )
                } else if (sharp < 35.0) {
                    issues += CaptureIssue(
                        code = "blur_moderate",
                        severity = "minor",
                        area = "画面清晰度",
                        detail = "清晰度中位数偏低（${"%.1f".format(sharp)} < 35），建议放慢移动速度"
                    )
                }
            }

            req.orbitContinuityMin?.let { cont ->
                if (cont < 0.35) {
                    issues += CaptureIssue(
                        code = "orbit_jump",
                        severity = "major",
                        area = "环绕轨迹连续性",
                        detail = "相邻帧最低连续度仅 ${"%.3f".format(cont)}（<0.35），存在跳帧或移动过快"
                    )
                }
            }

            // 5. 关键 ROI 细节分辨率
            req.roiDetailPx?.let { px ->
                if (targetType == "head") {
                    if (px < 8.0) {
                        issues += CaptureIssue(
                            code = "roi_resolution_critical",
                            severity = "critical",
                            area = "面部关键细节（眼部）",
                            detail = "单眼有效像素仅约 ${"%.1f".format(px)} px（<8 px），必须近距离慢推近补拍"
                        )
                    } else if (px < 16.0) {
                        issues += CaptureIssue(
                            code = "roi_resolution_low",
                            severity = "major",
                            area = "面部关键细节（眼部）",
                            detail = "单眼有效像素约 ${"%.1f".format(px)} px（<16 px），建议补充近景正斜脸特写"
                        )
                    }
                } else if (px < 15.0) {
                    issues += CaptureIssue(
                        code = "roi_resolution_low",
                        severity = "major",
                        area = "衣物结构细节",
                        detail = "关键结构像素仅约 ${"%.1f".format(px)} px（<15 px），建议 30–50 cm 近景补拍"
                    )
                }
            }

            if (req.hasScaleReference == false) {
                issues += CaptureIssue(
                    code = "missing_scale_reference",
                    severity = "major",
                    area = "尺度参考",
                    detail = "缺少已知尺度参考物（A4 纸 / ArUco 标定卡 / 地贴）"
                )
            }

            val cleanCloseups = req.missingCloseups.map { it.trim() }.filter { it.isNotEmpty() }
            if (cleanCloseups.isNotEmpty()) {
                issues += CaptureIssue(
                    code = "missing_closeups",
                    severity = "major",
                    area = "关键部位近景",
                    detail = "缺少以下部位近景特写：${cleanCloseups.joinToString("、")}"
                )
            }

            val sortedIssues = issues.sortedBy { SEVERITY_RANK[it.severity] ?: 9 }
            val criticalCodes = sortedIssues.filter { it.severity == "critical" }.map { it.code }.toSet()
            val readiness = when {
                sortedIssues.isEmpty() -> "pass"
                (criticalCodes.intersect(INVALIDATING_CODES).isNotEmpty()) || criticalCodes.size >= 2 -> "invalid_retake"
                else -> "needs_reshoot"
            }

            return CaptureDiagnosis(
                targetType = targetType,
                subjectLabel = req.subjectLabel,
                readiness = readiness,
                issueCount = sortedIssues.size,
                sectorSummary = sectorSummary,
                issues = sortedIssues
            )
        }

        fun buildChatPayload(diagnosis: CaptureDiagnosis, model: String): String {
            val systemPrompt =
                "你是「衣序 OOTD AI」的三维采集补拍指导助手。" +
                    "只根据输入 JSON 的客观缺口输出精简中文补拍方案，不编造数据。" +
                    "结合三维重建约束给具体动作：target_type=head 时若需重拍/补拍五官或环绕，应提醒摘掉反光眼镜、闭口静止、长发贴肩、" +
                    "全圈控在 25–40 秒减少活体晃动、避开正对窗户强逆光防后脑发丝发糊；" +
                    "target_type=garment 时应提醒均匀柔光、上下左右留白、转角稳步防跳帧。" +
                    "严格输出 JSON：{\"summary\":\"一句总结(<=40字)\",\"can_reuse_existing\":true/false," +
                    "\"steps\":[{\"priority\":1,\"target_area\":\"部位\",\"action\":\"具体动作(<=45字)\",\"reason\":\"对应指标(<=25字)\"}]}。" +
                    "readiness 为 invalid_retake 时 can_reuse_existing 必须为 false；" +
                    "最多合并输出 4 条最高优先级步骤；只输出 JSON。"

            val diagJson = JSONObject().apply {
                put("target_type", diagnosis.targetType)
                put("subject_label", diagnosis.subjectLabel)
                put("readiness", diagnosis.readiness)
                put("issue_count", diagnosis.issueCount)
                put("sector_summary", JSONObject().apply {
                    put("sector_count", diagnosis.sectorSummary.sectorCount)
                    diagnosis.sectorSummary.coveredSectors?.let { put("covered_sectors", it) }
                    diagnosis.sectorSummary.coverageRatio?.let { put("coverage_ratio", it) }
                    put("missing_arcs", JSONArray(diagnosis.sectorSummary.missingArcs))
                    diagnosis.sectorSummary.largestGapDeg?.let { put("largest_gap_deg", it) }
                })
                put("issues", JSONArray().apply {
                    diagnosis.issues.forEach { issue ->
                        put(JSONObject().apply {
                            put("code", issue.code)
                            put("severity", issue.severity)
                            put("area", issue.area)
                            put("detail", issue.detail)
                        })
                    }
                })
            }

            return JSONObject().apply {
                put("model", model)
                put("temperature", 0.0)
                put("response_format", JSONObject().put("type", "json_object"))
                put("messages", JSONArray().apply {
                    put(JSONObject().apply {
                        put("role", "system")
                        put("content", systemPrompt)
                    })
                    put(JSONObject().apply {
                        put("role", "user")
                        put("content", diagJson.toString())
                    })
                })
            }.toString()
        }

        fun extractChatContent(rawBody: String): String {
            val root = JSONObject(rawBody)
            val choices = root.optJSONArray("choices")
                ?: throw DirectDeepSeekException(null, "模型响应缺少 choices 字段")
            if (choices.length() == 0) {
                throw DirectDeepSeekException(null, "模型响应 choices 为空")
            }
            val msg = choices.getJSONObject(0).optJSONObject("message")
                ?: throw DirectDeepSeekException(null, "模型响应缺少 message 字段")
            val content = msg.optString("content", "").trim()
            if (content.isEmpty()) {
                throw DirectDeepSeekException(null, "模型响应 content 为空")
            }
            // 兼容偶发 ```json ... ``` 包裹
            return if (content.startsWith("```")) {
                content.removePrefix("```json").removePrefix("```").removeSuffix("```").trim()
            } else {
                content
            }
        }

        fun parseAndValidateGuideOutput(jsonText: String, diagnosis: CaptureDiagnosis): CaptureGuidePlan {
            val obj = runCatching { JSONObject(jsonText) }
                .getOrElse { throw IllegalArgumentException("模型输出不是合法 JSON") }

            val summary = obj.optString("summary", "").trim()
            if (summary.isEmpty()) {
                throw IllegalArgumentException("模型输出缺少非空 summary")
            }
            if (!obj.has("can_reuse_existing") || obj.get("can_reuse_existing") !is Boolean) {
                throw IllegalArgumentException("模型输出缺少布尔类型 can_reuse_existing")
            }
            val canReuse = obj.getBoolean("can_reuse_existing")
            if (diagnosis.readiness == "invalid_retake" && canReuse) {
                throw IllegalArgumentException("质检判定为 invalid_retake，模型不得返回 can_reuse_existing=true")
            }

            val rawSteps = obj.optJSONArray("steps")
                ?: throw IllegalArgumentException("模型输出缺少数组类型 steps")
            if (diagnosis.readiness != "pass" && rawSteps.length() == 0) {
                throw IllegalArgumentException("存在质检缺口时 steps 不能为空")
            }

            val steps = buildList {
                for (i in 0 until rawSteps.length()) {
                    val item = rawSteps.optJSONObject(i)
                        ?: throw IllegalArgumentException("steps[$i] 必须是对象")
                    val priorityVal = item.opt("priority")
                    if (priorityVal !is Number || priorityVal is Double || priorityVal.toInt() < 1) {
                        throw IllegalArgumentException("steps[$i].priority 必须是正整数")
                    }
                    val targetArea = item.optString("target_area", "").trim()
                    val action = item.optString("action", "").trim()
                    val reason = item.optString("reason", "").trim()
                    if (targetArea.isEmpty() || action.isEmpty() || reason.isEmpty()) {
                        throw IllegalArgumentException("steps[$i] 缺少非空 target_area / action / reason")
                    }
                    add(
                        CaptureGuideStep(
                            priority = priorityVal.toInt(),
                            targetArea = targetArea,
                            action = action,
                            reason = reason
                        )
                    )
                }
            }.sortedBy { it.priority }.take(4)

            return CaptureGuidePlan(
                summary = summary,
                canReuseExisting = canReuse,
                steps = steps
            )
        }

        private fun defaultTransport(): CaptureGuideTransport = CaptureGuideTransport { url, headers, body ->
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = READ_TIMEOUT_MS
                doInput = true
                doOutput = true
                headers.forEach { (k, v) -> setRequestProperty(k, v) }
            }
            try {
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else (conn.errorStream ?: conn.inputStream)
                val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
                HttpTransportResponse(code, text)
            } finally {
                conn.disconnect()
            }
        }

        private fun extractErrorMessage(code: Int, rawBody: String): String {
            if (rawBody.isBlank()) return "HTTP $code"
            return runCatching {
                val root = JSONObject(rawBody)
                val err = root.optJSONObject("error")
                val msg = err?.optString("message").orEmpty().ifBlank { root.optString("message") }
                msg.ifBlank { "HTTP $code" }
            }.getOrElse { "HTTP $code" }
        }
    }
}
