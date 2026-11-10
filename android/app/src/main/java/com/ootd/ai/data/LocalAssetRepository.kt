package com.ootd.ai.data

import android.content.Context

/**
 * 从 APK 的 assets 目录读取本地演示资产包。
 *
 * 当前阶段（B02）资产放在 `assets/local-demo/`，该目录在 .gitignore 中，
 * 不进公开仓库。B03 接后端目录后，这里会多一个走 HTTP 的实现，
 * 本类保留为断网和本地调试的兜底来源。
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
                    "放置步骤见 android/README.md「本地资产」一节",
                e
            )
        }
        return AssetManifestParser.parse(json)
    }

    /** 把清单里的 relative_file 变成 ModelLoader 能用的 asset 路径。 */
    fun assetPath(entry: AssetEntry): String = "$ASSET_DIR/${entry.relativeFile}"
}