package com.ootd.ai.data

import android.content.Context

/**
 * 从 APK 的 assets 目录读取本地演示资产包（B02 的入口，保留作为离线兜底）。
 *
 * 资产放在 `assets/local-demo/`，该目录在 .gitignore 中，不进公开仓库。
 * B03 起也从服务端目录下载同一个人物与衣物，两条路都归一到 [ActiveAssets]。
 */
class LocalAssetRepository(private val context: Context) {

    companion object {
        /** assets 下的目录名，见 android/README.md。 */
        const val ASSET_DIR = "local-demo"
        private const val MANIFEST_FILE = "manifest.json"
    }

    /** 读取并解析清单；资产未按说明放置时抛出带处理步骤的异常。 */
    fun loadManifest(): AssetManifest {
        val path = "$ASSET_DIR/$MANIFEST_FILE"
        val json = try {
            context.assets.open(path).bufferedReader().use { it.readText() }
        } catch (e: Exception) {
            throw IllegalStateException(
                "缺少本地资产包：assets/$path\n" +
                    "放置步骤见 android/README.md「本地资产」一节，或点「拉取服务端目录」",
                e
            )
        }
        return AssetManifestParser.parse(json)
    }

    /** 把清单里的 relative_file 变成 ModelLoader 能用的 asset 路径。 */
    fun assetPath(entry: AssetEntry): String = "$ASSET_DIR/${entry.relativeFile}"

    /** 本地包 → 统一形状，供三维页直接使用。 */
    fun loadActiveAssets(): ActiveAssets {
        val manifest = loadManifest()
        val person = manifest.person
            ?: throw IllegalStateException("本地清单里没有 asset_type=person 的条目")

        return ActiveAssets(
            sourceLabel = "本地包",
            personId = manifest.personId,
            catalogVersion = manifest.catalogVersion,
            poseId = manifest.poseId,
            personPath = assetPath(person),
            garments = manifest.garments.map { garment ->
                GarmentOption(
                    assetId = garment.assetId,
                    label = garment.displayName ?: garment.assetId,
                    path = assetPath(garment)
                )
            }
        )
    }
}