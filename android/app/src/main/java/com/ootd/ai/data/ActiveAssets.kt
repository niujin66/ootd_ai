package com.ootd.ai.data

/** 一件可选衣物。path 是传给 SceneView 的加载位置（assets 相对路径或本地文件绝对路径）。 */
data class GarmentOption(
    val assetId: String,
    val label: String,
    val path: String
)

/**
 * 界面当前生效的一套资源。本地包与远端包都归一到这个形状，
 * 让三维页面不必区分资源从哪来。
 */
data class ActiveAssets(
    val sourceLabel: String,
    val personId: String,
    val catalogVersion: String,
    val poseId: String,
    val personPath: String?,
    val garments: List<GarmentOption>
)

/** 远端已下载的包 → 界面用的形状。 */
fun RemotePackage.toActiveAssets(): ActiveAssets = ActiveAssets(
    sourceLabel = "服务端",
    personId = catalog.personId,
    catalogVersion = catalog.catalogVersion,
    poseId = catalog.poseId,
    personPath = catalog.person?.let { files[it.assetId]?.absolutePath },
    garments = catalog.garments.mapNotNull { asset ->
        files[asset.assetId]?.let { file ->
            GarmentOption(asset.assetId, asset.displayName, file.absolutePath)
        }
    }
)