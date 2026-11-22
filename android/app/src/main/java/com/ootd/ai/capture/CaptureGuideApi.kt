package com.ootd.ai.capture

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * 采集引导接口调用异常，保留 HTTP 状态码与服务端可读报错。
 */
class CaptureGuideApiException(
    val statusCode: Int,
    override val message: String,
    val requestId: String? = null
) : IOException(message)

/**
 * 可插拔 HTTP 传输层返回结果（便于 JVM 单测注入桩实现）。
 */
data class HttpTransportResponse(
    val statusCode: Int,
    val body: String
)

fun interface CaptureGuideTransport {
    fun postJson(url: String, headers: Map<String, String>, body: String): HttpTransportResponse
}

/**
 * 对接服务端 POST /v1/capture/guide 的 HTTP 客户端。
 * 默认读取超时 30s，以覆盖服务端 OOTD_CAPTURE_GUIDE_TIMEOUT_SECONDS=25s 的真 AI 推理耗时。
 */
class CaptureGuideApi(
    private val baseUrl: String,
    private val authToken: String = "",
    private val transport: CaptureGuideTransport = defaultTransport()
) {

    fun requestGuide(request: CaptureGuideRequest): CaptureGuideResponse {
        val url = "${baseUrl.trimEnd('/')}/v1/capture/guide"
        val headers = buildMap {
            put("Accept", "application/json")
            put("Content-Type", "application/json; charset=utf-8")
            put("X-Request-Id", request.requestId)
            if (authToken.isNotBlank()) {
                put("Authorization", "Bearer $authToken")
            }
        }
        val payload = CaptureGuideParser.encodeRequest(request)
        val response = transport.postJson(url, headers, payload)
        if (response.statusCode !in 200..299) {
            throw CaptureGuideApiException(
                statusCode = response.statusCode,
                message = extractErrorMessage(response.statusCode, response.body),
                requestId = request.requestId
            )
        }
        return CaptureGuideParser.parseResponse(response.body)
    }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 10_000
        private const val READ_TIMEOUT_MS = 30_000

        fun defaultTransport(): CaptureGuideTransport = CaptureGuideTransport { url, headers, body ->
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = READ_TIMEOUT_MS
                doInput = true
                doOutput = true
                headers.forEach { (k, v) -> setRequestProperty(k, v) }
            }
            try {
                conn.outputStream.use { out ->
                    out.write(body.toByteArray(Charsets.UTF_8))
                }
                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else (conn.errorStream ?: conn.inputStream)
                val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
                HttpTransportResponse(statusCode = code, body = text)
            } finally {
                conn.disconnect()
            }
        }

        private fun extractErrorMessage(code: Int, rawBody: String): String {
            if (rawBody.isBlank()) return "HTTP $code"
            return runCatching {
                val root = JSONObject(rawBody)
                val err = root.optJSONObject("error")
                val msg = err?.optString("message").orEmpty().ifBlank { root.optString("detail") }
                msg.ifBlank { "HTTP $code" }
            }.getOrElse { "HTTP $code" }
        }
    }
}
