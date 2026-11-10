package com.ootd.ai.data

import org.json.JSONObject

/** manifest.json 里的一条资产记录。字段见 docs/architecture.md 第 5 节。 */
data class AssetEntry(
    val assetType: String,
    val assetId: String,
    val relativeFile: String,
    val source: String,
    val displayName: String?,
    val available: Boolean
)

/** 一个完整资产包（一个人物 + 若干衣物）的清单。 */
data class AssetManifest(
    val schemaVersion: Int,
    val personId: String,
    val catalogVersion: String,
    val poseId: String,
    val assets: List<AssetEntry>
) {
    val person: AssetEntry?
        get() = assets.firstOrNull { it.assetType == "person" }

    /** 可用衣物，按清单顺序。 */
    val garments: List<AssetEntry>
        get() = assets.filter { it.assetType == "garment" && it.available }

    fun garmentById(id: String): AssetEntry? = garments.firstOrNull { it.assetId == id }
}

object AssetManifestParser {

    /**
     * 解析清单。字段缺失或类型不对时抛 [IllegalArgumentException]，
     * 由调用方转成界面上可读的错误提示。
     */
    fun parse(json: String): AssetManifest {
        val root = JSONObject(json)

        val schemaVersion = root.optInt("schema_version", 1)
        val personId = root.optString("person_id").ifBlank {
            throw IllegalArgumentException("manifest 缺少 person_id")
        }
        val catalogVersion = root.optString("catalog_version").ifBlank {
            throw IllegalArgumentException("manifest 缺少 catalog_version")
        }
        val poseId = root.optString("pose_id").ifBlank {
            throw IllegalArgumentException("manifest 缺少 pose_id")
        }

        val array = root.optJSONArray("assets")
            ?: throw IllegalArgumentException("manifest 缺少 assets 数组")

        val assets = buildList {
            for (i in 0 until array.length()) {
                val item = array.optJSONObject(i) ?: continue
                val id = item.optString("asset_id")
                val file = item.optString("relative_file")
                if (id.isBlank() || file.isBlank()) continue
                add(
                    AssetEntry(
                        assetType = item.optString("asset_type"),
                        assetId = id,
                        relativeFile = file,
                        source = item.optString("source"),
                        displayName = item.optString("display_name").ifBlank { null },
                        available = item.optBoolean("available", true)
                    )
                )
            }
        }

        if (assets.none { it.assetType == "person" }) {
            throw IllegalArgumentException("manifest 里没有 asset_type=person 的条目")
        }

        return AssetManifest(schemaVersion, personId, catalogVersion, poseId, assets)
    }
}