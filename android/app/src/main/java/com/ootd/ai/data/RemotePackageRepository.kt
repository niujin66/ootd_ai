package com.ootd.ai.data

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.security.MessageDigest

/** 一整套已下载并通过校验的远端资源。 */
data class RemotePackage(
    val catalog: RemoteCatalog,
    val directory: File,
    val files: Map<String, File>
) {
    fun fileOf(assetId: String): File? = files[assetId]
}

/**
 * 拉目录 → 下载整包 → 逐个校验 size_bytes 与 sha256 → 全部通过才切换。
 *
 * 约定（B03 卡第 3 条）：下载取消、损坏或缺件时保留上一套已验证资源，
 * 所以这里始终先写临时目录，校验通过后再整体改名过去。
 */
class RemotePackageRepository(
    context: Context,
    private val api: CatalogApi
) {
    private val cacheRoot: File = File(context.cacheDir, "ootd")

    /**
     * 同步一套资源。已存在且校验通过的版本直接复用，不重复下载。
     *
     * @throws ApiException 服务端返回错误
     * @throws IllegalStateException 校验不通过（大小或哈希不符、缺文件）
     */
    suspend fun sync(personId: String, onProgress: (String) -> Unit = {}) = withContext(Dispatchers.IO) {
        onProgress("连接服务端…")
        api.health()

        onProgress("拉取目录…")
        val catalog = api.catalog(personId)

        val personDir = File(cacheRoot, personId)
        val target = File(personDir, catalog.catalogVersion)

        if (isValid(target, catalog)) {
            onProgress("复用已缓存版本 ${catalog.catalogVersion}")
            return@withContext RemotePackage(catalog, target, filesOf(target, catalog))
        }

        val staging = File(personDir, "${catalog.catalogVersion}.tmp-${System.currentTimeMillis()}")
        if (staging.exists()) staging.deleteRecursively()
        staging.mkdirs()

        try {
            val ordered = buildList {
                catalog.person?.let { add(it) }
                addAll(catalog.garments)
            }
            ordered.forEachIndexed { index, asset ->
                onProgress("下载 ${asset.assetId}（${index + 1}/${ordered.size}）…")
                val dest = File(staging, fileNameOf(asset))
                api.download(asset.assetUrl, dest)
                verify(dest, asset)
            }

            // 全部通过，才替换旧版本
            if (target.exists()) target.deleteRecursively()
            if (!staging.renameTo(target)) {
                // 某些设备跨目录改名会失败，退化为逐文件搬移
                target.mkdirs()
                staging.listFiles()?.forEach { it.copyTo(File(target, it.name), overwrite = true) }
                staging.deleteRecursively()
            }

            onProgress("已切换到 ${catalog.catalogVersion}（${ordered.size} 个资产，校验通过）")
            RemotePackage(catalog, target, filesOf(target, catalog))
        } catch (t: Throwable) {
            staging.deleteRecursively()
            throw t
        }
    }

    private fun fileNameOf(asset: RemoteAsset): String = "${asset.assetId}.glb"

    private fun filesOf(dir: File, catalog: RemoteCatalog): Map<String, File> {
        val map = mutableMapOf<String, File>()
        catalog.person?.let { map[it.assetId] = File(dir, fileNameOf(it)) }
        catalog.garments.forEach { map[it.assetId] = File(dir, fileNameOf(it)) }
        return map
    }

    private fun isValid(dir: File, catalog: RemoteCatalog): Boolean {
        if (!dir.isDirectory) return false
        val all = buildList {
            catalog.person?.let { add(it) }
            addAll(catalog.garments)
        }
        return all.all { asset ->
            val f = File(dir, fileNameOf(asset))
            f.isFile && runCatching { verify(f, asset) }.isSuccess
        }
    }

    private fun verify(file: File, asset: RemoteAsset) {
        if (!file.isFile) {
            throw IllegalStateException("缺少文件：${asset.assetId}")
        }
        if (asset.sizeBytes >= 0 && file.length() != asset.sizeBytes) {
            throw IllegalStateException(
                "大小不符：${asset.assetId} 期望 ${asset.sizeBytes} 实际 ${file.length()}"
            )
        }
        if (asset.sha256.isNotBlank()) {
            val actual = sha256(file)
            if (!actual.equals(asset.sha256, ignoreCase = true)) {
                throw IllegalStateException(
                    "哈希不符：${asset.assetId} 期望 ${asset.sha256.take(12)}… 实际 ${actual.take(12)}…"
                )
            }
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}