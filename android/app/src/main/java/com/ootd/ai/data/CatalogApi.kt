package com.ootd.ai.data

import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** 服务端返回了非 2xx。code 对齐接口契约：401/404/409/422/5xx。 */
class ApiException(val code: Int, message: String) : IOException(message)

/**
 * 极简 HTTP 客户端，只用 JDK 自带的 HttpURLConnection，不引额外依赖。
 *
 * 地址与令牌来自 BuildConfig（本地 local.properties 覆盖），不进仓库。
 */
class CatalogApi(
    private val baseUrl: String,
    private val token: String
) {

    private fun open(path: String, method: String, connectTimeout: Int = 8000): HttpURLConnection {
        val url = URL(if (path.startsWith("http")) path else baseUrl.trimEnd('/') + path)
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = connectTimeout
        conn.readTimeout = 20000
        conn.setRequestProperty("Accept", "application/json")
        if (token.isNotBlank()) {
            conn.setRequestProperty("Authorization", "Bearer $token")
        }
        return conn
    }

    private fun readBody(conn: HttpURLConnection): String {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
        if (code !in 200..299) {
            throw ApiException(code, extractMessage(text) ?: "HTTP $code")
        }
        return text
    }

    private fun extractMessage(body: String): String? = try {
        val root = org.json.JSONObject(body)
        root.optJSONObject("error")?.optString("message")?.ifBlank { null }
            ?: root.optString("message").ifBlank { null }
    } catch (_: Exception) {
        null
    }

    /** GET /v1/health */
    fun health() {
        val conn = open("/v1/health", "GET")
        try {
            readBody(conn)
        } finally {
            conn.disconnect()
        }
    }

    /** GET /v1/catalog?person_id= */
    fun catalog(personId: String): RemoteCatalog {
        val conn = open("/v1/catalog?person_id=${java.net.URLEncoder.encode(personId, "UTF-8")}", "GET")
        try {
            return CatalogParser.parseCatalog(readBody(conn))
        } finally {
            conn.disconnect()
        }
    }

    /** POST /v1/recommendations */
    fun recommend(request: RecommendationRequest): RecommendationResult {
        val payload = org.json.JSONObject()
            .put("request_id", request.requestId)
            .put("person_id", request.personId)
            .put("catalog_version", request.catalogVersion)
            .put("scene", request.scene)
            .put("preference", request.preference)
            .toString()

        val conn = open("/v1/recommendations", "POST")
        try {
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("X-Request-Id", request.requestId)
            conn.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
            return CatalogParser.parseRecommendation(readBody(conn))
        } finally {
            conn.disconnect()
        }
    }

    /**
     * 下载一个资产到 [dest]。返回实际字节数与 sha256 是否与目录一致由调用方判断，
     * 这里只负责把字节完整落盘。
     */
    fun download(assetUrl: String, dest: File) {
        dest.parentFile?.mkdirs()
        val conn = open(assetUrl, "GET")
        try {
            val code = conn.responseCode
            if (code !in 200..299) {
                throw ApiException(code, "下载失败：HTTP $code（${assetUrl}）")
            }
            conn.inputStream.use { input ->
                dest.outputStream().use { output -> input.copyTo(output) }
            }
        } finally {
            conn.disconnect()
        }
    }
}