package com.ootd.ai.data

import android.content.Context
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
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
    private val api: CatalogApi,
    private val store: PersistentCatalogStore = PersistentCatalogStore(context)
) {
    // R03：落到 filesDir（持久），不再用 cacheDir —— 那里会被系统清掉，
    // 一清就无法离线冷启动恢复了。
    private val cacheRoot: File = store.packagesRoot()

    /**
     * E04 复核意见第 4 条：sync 与 restoreLast 之间加互斥。
     *
     * 两个都在 IO 线程上，同时跑会一起改同一个 person 目录（一个在 rename 版本目录，
     * 另一个在遍历/删版本）。界面通常不会并发触发，但「通常不会」不等于不会 ——
     * 冷启动恢复还没走完，用户就点了「拉取目录」，正好撞上。
     */
    private val ioLock = Mutex()

    /**
     * 同步一套资源。已存在且校验通过的版本直接复用，不重复下载。
     *
     * @throws ApiException 服务端返回错误
     * @throws IllegalStateException 校验不通过（大小或哈希不符、解不开、缺文件）
     */
    suspend fun sync(personId: String, onProgress: (String) -> Unit = {}): RemotePackage =
        ioLock.withLock {
            withContext(Dispatchers.IO) {
                onProgress("连接服务端…")
                api.health()

                onProgress("拉取目录…")
                val catalog = api.catalog(personId)

                val personDir = File(cacheRoot, personId)
                val target = File(personDir, catalog.catalogVersion)

                // 服务端目录里的 sha256 描述的是「原始文件」；本地这份可能被净化过
                // （剥掉 Filament 会崩的材质扩展），哈希自然对不上。所以服务端校验
                // 不过时，再用本地快照复核一次 —— 那份快照记的就是落盘文件的哈希。
                val localManifest = store.readManifest(personId, catalog.catalogVersion)
                val reusable = isValid(target, catalog) ||
                    (localManifest != null && isValid(target, localManifest))
                if (reusable) {
                    onProgress("复用已缓存版本 ${catalog.catalogVersion}")
                    // R03：快照也补齐，冷启动时才认得出这一版。
                    // E04：生效指针不在这一步写 —— 见 publishActive()。
                    val snapshot = localManifest ?: catalog
                    store.saveManifest(snapshot)
                    return@withContext RemotePackage(snapshot, target, filesOf(target, snapshot))
                }

                // R03：清掉上次下载中断留下的临时目录，避免越积越多
                store.clearStaging(catalog.personId)

                val staging = File(personDir, "${catalog.catalogVersion}.tmp-${System.currentTimeMillis()}")
                if (staging.exists()) staging.deleteRecursively()
                staging.mkdirs()

                try {
                    val ordered = buildList {
                        catalog.person?.let { add(it) }
                        addAll(catalog.garments)
                    }
                    val sanitizedAssets = mutableMapOf<String, RemoteAsset>()
                    ordered.forEachIndexed { index, asset ->
                        onProgress("下载 ${asset.assetId}（${index + 1}/${ordered.size}）…")
                        val dest = File(staging, fileNameOf(asset))
                        api.download(asset.assetUrl, dest)
                        // 顺序不能换：verify 比的是服务端原始文件的哈希，
                        // 净化会改字节，必须放在 verify 之后。
                        verify(dest, asset)
                        val effective = sanitizeIfNeeded(dest, asset)
                        if (effective !== asset) sanitizedAssets[asset.assetId] = effective
                        // E04：哈希对不代表能打开。下载完立刻做一次真解码探测，
                        // 坏包必须在「切换」之前就被拒 —— 否则它会先切目录、再上屏失败，
                        // 不但旧素材被顶掉，生效指针也会被写成这一版。
                        probeDecodable(dest, effective)
                    }

                    // 净化过的资产，快照里要记它落盘后的真实哈希，
                    // 否则下一轮 sync 复用缓存、以及冷启动恢复都会判它「哈希不符」。
                    val effectiveCatalog =
                        if (sanitizedAssets.isEmpty()) catalog
                        else catalog.copy(assets = catalog.assets.map { sanitizedAssets[it.assetId] ?: it })

                    // P0-2：切版本目录交给 PackageDirSwap。
                    // 它保证「要么整份换成，要么一点没变」——不再先删旧版，
                    // 而是把旧版挪开、新版顶上、成功了才删旧的。
                    PackageDirSwap.swapIn(target, staging)

                    // R03：校验全部通过、目录已经切过去，才落快照。
                    // E04：这里不写生效指针 —— 包还没真正上过屏，
                    // 写指针等于替用户宣布「这一版能用」。由界面在上屏成功后调 publishActive()。
                    store.saveManifest(effectiveCatalog)

                    onProgress("已切换到 ${catalog.catalogVersion}（${ordered.size} 个资产，校验通过）")
                    RemotePackage(effectiveCatalog, target, filesOf(target, effectiveCatalog))
                } catch (t: Throwable) {
                    // P0-2：取消要原样上抛。不能当成普通失败吞掉，
                    // 否则界面会把「用户自己取消」显示成「同步失败」，
                    // 状态还停在半路。取消时也不做清理，把收尾交给下一次 sync。
                    if (t is CancellationException) throw t
                    staging.deleteRecursively()
                    throw t
                }
            }
        }

    /**
     * E04：确认这一版真的上屏了，才把它记成「生效版本」。
     *
     * 复核意见要求把 saveActive 挪到「新衣真的 Ready 并上屏」之后：
     * 只在文件层面校验通过就写指针，坏包会被记成生效版本，下次冷启动继续恢复它。
     * 现在的顺序是 —— 下载→校验→解码探测→上屏成功→写指针，任何一环断了，
     * 指针都还指着上一套能用的资源。
     */
    suspend fun publishActive(pkg: RemotePackage) = withContext(Dispatchers.IO) {
        ioLock.withLock {
            store.saveActive(pkg.catalog.personId, pkg.catalog.catalogVersion)
        }
    }

    /**
     * R04：离线冷启动恢复。
     *
     * 顺序：先认 active 指针指向的那一版；它坏了就清掉指针，退回最近一个
     * 还能通过校验的版本；全都不行才返回 null（由上层退回内置素材）。
     *
     * 校验不过的版本会被直接删掉 —— 留着它只会让下次启动再白试一遍。
     * 整个过程不碰网络，飞行模式下也能用。
     */
    suspend fun restoreLast(): RemotePackage? = ioLock.withLock {
        withContext(Dispatchers.IO) {
            // 1. 上次生效的那一版优先
            val active = store.readActive()
            if (active != null) {
                val (personId, version) = active
                loadPackage(personId, version)?.let { return@withContext it }
                // 指针指向的东西已经不可用了，先清掉，免得每次都先撞一次空
                store.clearActive()
            }

            // 2. 按保存时间倒序找其他还能用的版本
            store.listPersons().forEach { personId ->
                store.versionsOf(personId).forEach { dir ->
                    loadPackage(personId, dir.name)?.let { pkg ->
                        store.saveActive(personId, dir.name)
                        return@withContext pkg
                    }
                }
            }

            null
        }
    }


    /** 读快照 + 逐个复核文件；任何一环不过就返回 null，并把坏版本清掉。 */
    private fun loadPackage(personId: String, catalogVersion: String): RemotePackage? {
        val catalog = store.readManifest(personId, catalogVersion) ?: run {
            // 连快照都没有，这个目录留着没意义
            store.dropVersion(personId, catalogVersion)
            return null
        }

        val dir = File(cacheRoot, "$personId/$catalogVersion")
        if (!isValid(dir, catalog)) {
            store.dropVersion(personId, catalogVersion)
            return null
        }

        return RemotePackage(catalog, dir, filesOf(dir, catalog))
    }

    private fun fileNameOf(asset: RemoteAsset): String = "${asset.assetId}.glb"

    private fun filesOf(dir: File, catalog: RemoteCatalog): Map<String, File> {
        val map = mutableMapOf<String, File>()
        catalog.person?.let { map[it.assetId] = File(dir, fileNameOf(it)) }
        catalog.garments.forEach { map[it.assetId] = File(dir, fileNameOf(it)) }
        return map
    }

    /**
     * E04：能不能把这一版当成可用的资源。
     *
     * 三个条件缺一不可：文件在、大小与 sha256 对得上、**模型真能解开**。
     * 最后一条是复核意见里的阻塞项 —— 只看哈希的话，一个「哈希完全正确、
     * GLB 内部是坏的」包会被当成正常版本，上屏失败还会被下次冷启动恢复出来。
     */
    private fun isValid(dir: File, catalog: RemoteCatalog): Boolean {
        if (!dir.isDirectory) return false
        val all = buildList {
            catalog.person?.let { add(it) }
            addAll(catalog.garments)
        }
        return all.all { asset ->
            val f = File(dir, fileNameOf(asset))
            f.isFile &&
                runCatching { verify(f, asset) }.isSuccess &&
                // 真解码探测放在最后：前面两条便宜，先挡掉大部分坏包
                GlbProbe.canDecode(f)
        }
    }

    /**
     * E04：解不开就当场判失败，让 sync 走「保留上一套」的路径。
     *
     * 和 [isValid] 用的是同一个探测，区别只在用途：那边回答「这一版还能不能用」，
     * 这边回答「刚下下来的这个文件能不能用」。
     */
    private fun probeDecodable(file: File, asset: RemoteAsset) {
        try {
            GlbProbe.probe(file)
        } catch (e: IllegalStateException) {
            // 后缀「已保留当前资源」由界面统一补，这里只说清哪个文件、为什么坏
            throw IllegalStateException("${asset.assetId} 解不开（${e.message}）", e)
        }
    }

    /**
     * 剥掉「文件合法、但 Filament 会崩」的材质扩展。
     *
     * 2026-11-18 定位：服务端下发的衣物包带了 `KHR_materials_specular` / `KHR_materials_sheen`，
     * 文件经 Khronos 校验器 0 错 0 警，Filament 1.77.3 处理时却直接 abort 掉整个进程。
     * 这两个扩展都在 `extensionsUsed` 而不是 `extensionsRequired`，按规范客户端可以忽略，
     * 所以剥掉不算降级 —— 剥完的文件和 App 内置素材逐字节一致，那份在真机上一直能用。
     *
     * 返回更新后的资产条目（记的是净化后落盘文件的真实哈希）；没改动就原样返回。
     */
    private fun sanitizeIfNeeded(file: File, asset: RemoteAsset): RemoteAsset {
        val changed = runCatching { GlbSanitizer.sanitizeInPlace(file) }.getOrDefault(false)
        if (!changed) return asset
        return asset.copy(sizeBytes = file.length(), sha256 = sha256(file))
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