package com.ootd.ai.data

import org.json.JSONObject

/** 服务端目录里的一个资产条目，字段见 docs/architecture.md 的 /v1/catalog。 */
data class RemoteAsset(
    val assetId: String,
    val assetType: String,
    val displayName: String,
    val tags: List<String>,
    val available: Boolean,
    val assetUrl: String,
    val sizeBytes: Long,
    val sha256: String
)

/** GET /v1/catalog 的结果。 */
data class RemoteCatalog(
    val personId: String,
    val catalogVersion: String,
    val poseId: String,
    val assets: List<RemoteAsset>
) {
    val person: RemoteAsset?
        get() = assets.firstOrNull { it.assetType == "person" }

    val garments: List<RemoteAsset>
        get() = assets.filter { it.assetType == "garment" && it.available }

    fun garmentById(id: String): RemoteAsset? = garments.firstOrNull { it.assetId == id }
}

/** POST /v1/recommendations 的请求体。 */
data class RecommendationRequest(
    val requestId: String,
    val personId: String,
    val catalogVersion: String,
    val scene: String,
    val preference: String
)

/** POST /v1/recommendations 的结果。 */
data class RecommendationResult(
    val requestId: String,
    val catalogVersion: String,
    val status: String,
    val garmentId: String?,
    val reason: String,
    val mode: String
) {
    /** 没有合适衣物时，界面要说明理由并保留当前衣物。 */
    val isNoMatch: Boolean
        get() = status == "no_match" || garmentId.isNullOrBlank()

    /** mock 结果必须在界面上明确标注，不能让人误以为是真的 AI 推荐。 */
    val isMock: Boolean
        get() = mode.equals("mock", ignoreCase = true)
}

object CatalogParser {

    /** 资产 ID 的形状：服务端用 `top-001_v2` 这类名字，不允许空格、斜杠、中文。 */
    private val ID_PATTERN = Regex("^[A-Za-z0-9._-]{1,64}$")

    /** sha256 必须是 64 位小写十六进制；别的形状一律当无效。 */
    private val SHA_PATTERN = Regex("^[0-9a-f]{64}$")

    private val ASSET_TYPES = setOf("person", "garment")
    private val STATUSES = setOf("ok", "no_match")
    private val MODES = setOf("mock", "ai")

    /**
     * P1：严格解析目录。
     *
     * 旧实现用 `optString` 一路往下读，缺字段返回空串、类型错也不报错，
     * 于是「少了 sha256」「size_bytes 是字符串」这种包会被当成正常目录，
     * 一路走到下载和校验才炸，甚至直接落成生效版本。
     *
     * 现在的原则是**整份拒绝**：任何一条资产不合法就抛异常，不做「跳过坏的留下好的」。
     * 理由是这个目录是一个整体，缺一个人物或衣物时，切换过去只会让画面更糟。
     */
    fun parseCatalog(json: String): RemoteCatalog {
        val root = JSONObject(json)

        val personId = root.requireString("person_id", "目录")
        val version = root.requireString("catalog_version", "目录")
        val poseId = root.optString("pose_id").orEmpty()

        val array = root.optJSONArray("assets")
            ?: throw IllegalArgumentException("目录响应缺少 assets 数组")
        if (array.length() == 0) {
            throw IllegalArgumentException("目录响应 assets 为空")
        }

        val seenIds = mutableSetOf<String>()
        val assets = buildList {
            for (i in 0 until array.length()) {
                val item = array.optJSONObject(i)
                    ?: throw IllegalArgumentException("assets[$i] 不是对象")

                val id = item.requireString("asset_id", "assets[$i]")
                if (!ID_PATTERN.matches(id)) {
                    throw IllegalArgumentException("assets[$i] 的 asset_id 不合法：$id")
                }
                if (!seenIds.add(id)) {
                    throw IllegalArgumentException("asset_id 重复：$id")
                }

                val type = item.requireString("asset_type", "assets[$i]")
                if (type !in ASSET_TYPES) {
                    throw IllegalArgumentException("assets[$i] 的 asset_type 未知：$type")
                }

                val url = item.requireString("asset_url", "assets[$i]")
                val size = item.requireLong("size_bytes", "assets[$i]")
                if (size <= 0) {
                    throw IllegalArgumentException("assets[$i] 的 size_bytes 必须大于 0，实际 $size")
                }

                val sha = item.requireString("sha256", "assets[$i]").lowercase()
                if (!SHA_PATTERN.matches(sha)) {
                    throw IllegalArgumentException("assets[$i] 的 sha256 不是 64 位十六进制")
                }

                add(
                    RemoteAsset(
                        assetId = id,
                        assetType = type,
                        displayName = item.optString("display_name").ifBlank { id },
                        tags = item.readStringArray("tags", "assets[$i]"),
                        available = item.requireBoolean("available", "assets[$i]"),
                        assetUrl = url,
                        sizeBytes = size,
                        sha256 = sha
                    )
                )
            }
        }

        if (assets.none { it.assetType == "person" }) {
            throw IllegalArgumentException("目录里没有 asset_type=person 的条目")
        }

        return RemoteCatalog(personId, version, poseId, assets)
    }

    /**
     * P1：严格解析推荐结果。
     *
     * 收紧的重点是 `status` 和 `mode`：这两个值界面会直接拿来做判断
     * （no_match 要说明理由、mock 要明确标注不是真 AI）。
     * 遇到不认识的值必须拒绝，而不是当成「大概是 ok」继续换衣。
     */
    fun parseRecommendation(json: String): RecommendationResult {
        val root = JSONObject(json)

        val status = root.requireString("status", "推荐")
        if (status !in STATUSES) {
            throw IllegalArgumentException("推荐响应的 status 未知：$status")
        }

        val mode = root.requireString("mode", "推荐")
        if (!MODES.contains(mode.lowercase())) {
            throw IllegalArgumentException("推荐响应的 mode 未知：$mode")
        }

        val garmentId = if (root.isNull("garment_id")) {
            null
        } else {
            root.optString("garment_id").ifBlank { null }
        }

        // status=ok 却没给衣物，等于说了「推荐成功」但没说推荐什么
        if (status == "ok" && garmentId == null) {
            throw IllegalArgumentException("推荐响应 status=ok 但缺少 garment_id")
        }

        return RecommendationResult(
            requestId = root.requireString("request_id", "推荐"),
            catalogVersion = root.requireString("catalog_version", "推荐"),
            status = status,
            garmentId = garmentId,
            reason = root.optString("reason").orEmpty(),
            mode = mode.lowercase()
        )
    }

    // ---- 下面几个小工具把「缺字段」和「类型错」分开报，方便定位是哪一条包坏了 ----

    private fun JSONObject.requireString(key: String, where: String): String {
        if (!has(key) || isNull(key)) {
            throw IllegalArgumentException("$where 缺少字段 $key")
        }
        val value = get(key)
        if (value !is String) {
            throw IllegalArgumentException("$where 的 $key 应为字符串，实际是 ${value.javaClass.simpleName}")
        }
        if (value.isBlank()) {
            throw IllegalArgumentException("$where 的 $key 不能为空")
        }
        return value
    }

    private fun JSONObject.requireLong(key: String, where: String): Long {
        if (!has(key) || isNull(key)) {
            throw IllegalArgumentException("$where 缺少字段 $key")
        }
        val value = get(key)
        if (value !is Number) {
            throw IllegalArgumentException("$where 的 $key 应为数字，实际是 ${value.javaClass.simpleName}")
        }
        return value.toLong()
    }

    private fun JSONObject.requireBoolean(key: String, where: String): Boolean {
        if (!has(key) || isNull(key)) {
            throw IllegalArgumentException("$where 缺少字段 $key")
        }
        val value = get(key)
        if (value !is Boolean) {
            throw IllegalArgumentException("$where 的 $key 应为布尔值，实际是 ${value.javaClass.simpleName}")
        }
        return value
    }

    private fun JSONObject.readStringArray(key: String, where: String): List<String> {
        if (!has(key) || isNull(key)) return emptyList()
        val array = get(key)
        if (array !is org.json.JSONArray) {
            throw IllegalArgumentException("$where 的 $key 应为数组，实际是 ${array.javaClass.simpleName}")
        }
        return buildList {
            for (i in 0 until array.length()) {
                val element = array.get(i)
                if (element !is String) {
                    throw IllegalArgumentException("$where 的 $key[$i] 应为字符串，实际是 ${element.javaClass.simpleName}")
                }
                add(element)
            }
        }
    }
}

/**
 * P1：推荐结果能不能直接拿去换衣。
 *
 * 解析层只能看出「形状对不对」，看不出「这件衣服是不是这次目录里的」。
 * 后面这条必须对着当前目录校验：服务端返回一个不存在的 garment_id，
 * 或者旧一轮推荐带着已经下线的编号回来，都不能让画面去加载它。
 */
object RecommendationGuard {

    /** 返回 null 表示可以用；否则是要在界面上说清的理由。 */
    fun rejectReason(result: RecommendationResult, availableGarments: Set<String>): String? {
        if (result.isNoMatch) return null // 没有匹配是正常结果，不是错误
        val id = result.garmentId ?: return "推荐结果没有给出衣物编号"
        if (id !in availableGarments) {
            return "推荐了本次目录里没有的衣物：$id"
        }
        return null
    }
}