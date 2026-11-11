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

    fun parseCatalog(json: String): RemoteCatalog {
        val root = JSONObject(json)
        val personId = root.optString("person_id")
        val version = root.optString("catalog_version")
        val poseId = root.optString("pose_id")
        if (personId.isBlank() || version.isBlank()) {
            throw IllegalArgumentException("目录响应缺少 person_id 或 catalog_version")
        }

        val array = root.optJSONArray("assets")
            ?: throw IllegalArgumentException("目录响应缺少 assets 数组")

        val assets = buildList {
            for (i in 0 until array.length()) {
                val item = array.optJSONObject(i) ?: continue
                val id = item.optString("asset_id")
                val url = item.optString("asset_url")
                if (id.isBlank() || url.isBlank()) continue

                val tags = mutableListOf<String>()
                item.optJSONArray("tags")?.let { t ->
                    for (j in 0 until t.length()) tags += t.optString(j)
                }

                add(
                    RemoteAsset(
                        assetId = id,
                        assetType = item.optString("asset_type"),
                        displayName = item.optString("display_name").ifBlank { id },
                        tags = tags,
                        available = item.optBoolean("available", true),
                        assetUrl = url,
                        sizeBytes = item.optLong("size_bytes", -1L),
                        sha256 = item.optString("sha256").lowercase()
                    )
                )
            }
        }

        if (assets.none { it.assetType == "person" }) {
            throw IllegalArgumentException("目录里没有 asset_type=person 的条目")
        }

        return RemoteCatalog(personId, version, poseId, assets)
    }

    fun parseRecommendation(json: String): RecommendationResult {
        val root = JSONObject(json)
        return RecommendationResult(
            requestId = root.optString("request_id"),
            catalogVersion = root.optString("catalog_version"),
            status = root.optString("status"),
            garmentId = if (root.isNull("garment_id")) null else root.optString("garment_id").ifBlank { null },
            reason = root.optString("reason"),
            mode = root.optString("mode")
        )
    }
}