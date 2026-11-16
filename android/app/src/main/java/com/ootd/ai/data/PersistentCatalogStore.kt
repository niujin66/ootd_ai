package com.ootd.ai.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * R03：远端目录与「最后完整生效版本」的持久化。
 *
 * 落在 filesDir 而不是 cacheDir —— cacheDir 会被系统在空间紧张时整体清掉，
 * 那样 R04 的离线冷启动就恢复不了；filesDir 只有卸载应用才会消失。
 *
 * 目录布局：
 * ```
 * filesDir/ootd-packages/
 *   active.json                  ← 当前生效的版本指针
 *   person-001/
 *     fixture-face-03/
 *       manifest.json            ← 目录快照（冷启动时还原 catalog）
 *       person-001.glb
 *       top-001_v2.glb
 * ```
 */
class PersistentCatalogStore(context: Context) {

    companion object {
        private const val ROOT_DIR = "ootd-packages"
        private const val MANIFEST_FILE = "manifest.json"
        private const val ACTIVE_FILE = "active.json"
        private const val MANIFEST_SCHEMA = 1
    }

    private val root = File(context.filesDir, ROOT_DIR)

    /** 所有已下载包的根目录，供 RemotePackageRepository 放版本目录。 */
    fun packagesRoot(): File = root

    /** 已缓存过资源的人物 id 列表。 */
    fun listPersons(): List<String> {
        if (!root.isDirectory) return emptyList()
        return root.listFiles()
            ?.filter { it.isDirectory }
            ?.map { it.name }
            ?: emptyList()
    }

    /**
     * 某个人物下已下载的版本目录，按「保存时间」从新到旧。
     *
     * E04 复核意见第 3 条：原来按目录 mtime 排。跨目录 rename（sync 里先写 staging
     * 再改名过去）之后，mtime 反映的是改名那一刻而不是真正的下载完成时间，排序会错。
     * manifest 里本来就存了 `saved_at`，按它排才可靠；读不到 saved_at 的目录排到最后
     * —— 它多半也恢复不了，排前面只是多一次白试。
     */
    fun versionsOf(personId: String): List<File> {
        val dir = File(root, personId)
        if (!dir.isDirectory) return emptyList()
        return dir.listFiles()
            ?.filter { it.isDirectory && !it.name.contains(".tmp-") && !it.name.endsWith(".lck") }
            ?.sortedByDescending { readSavedAt(personId, it.name) }
            ?: emptyList()
    }

    /** 这一版是什么时候保存的；快照缺失或读不出来就返回 0。 */
    fun readSavedAt(personId: String, catalogVersion: String): Long {
        val file = File(versionDir(personId, catalogVersion), MANIFEST_FILE)
        if (!file.isFile) return 0L
        return runCatching { JSONObject(file.readText()).optLong("saved_at", 0L) }.getOrDefault(0L)
    }

    /** 版本目录。 */
    fun versionDir(personId: String, catalogVersion: String): File =
        File(File(root, personId), catalogVersion)

    /**
     * R03：写出目录快照。
     * 冷启动时没有网络，只能靠这份快照还原 catalog —— 所以 assets 要全量落盘，
     * 包括 size/sha256，恢复时才能逐个复核文件有没有被改动或截断。
     */
    fun saveManifest(catalog: RemoteCatalog) {
        val dir = versionDir(catalog.personId, catalog.catalogVersion)
        dir.mkdirs()

        val json = JSONObject().apply {
            put("schema", MANIFEST_SCHEMA)
            put("person_id", catalog.personId)
            put("catalog_version", catalog.catalogVersion)
            put("pose_id", catalog.poseId)
            put("saved_at", System.currentTimeMillis())
            put(
                "assets",
                JSONArray().apply {
                    catalog.assets.forEach { a ->
                        put(
                            JSONObject().apply {
                                put("asset_id", a.assetId)
                                put("asset_type", a.assetType)
                                put("display_name", a.displayName)
                                put("tags", JSONArray(a.tags))
                                put("available", a.available)
                                put("asset_url", a.assetUrl)
                                put("size_bytes", a.sizeBytes)
                                put("sha256", a.sha256)
                            }
                        )
                    }
                }
            )
        }
        writeAtomically(File(dir, MANIFEST_FILE), json.toString())
    }

    /** 读回目录快照；文件缺失或结构不认就返回 null（视为不可恢复）。 */
    fun readManifest(personId: String, catalogVersion: String): RemoteCatalog? {
        val file = File(versionDir(personId, catalogVersion), MANIFEST_FILE)
        if (!file.isFile) return null

        return runCatching {
            val root = JSONObject(file.readText())
            if (root.optInt("schema", 0) != MANIFEST_SCHEMA) return null

            val array = root.optJSONArray("assets") ?: return null
            val assets = buildList {
                for (i in 0 until array.length()) {
                    val item = array.optJSONObject(i) ?: continue
                    val id = item.optString("asset_id")
                    if (id.isBlank()) continue

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
                            assetUrl = item.optString("asset_url"),
                            sizeBytes = item.optLong("size_bytes", -1L),
                            sha256 = item.optString("sha256").lowercase()
                        )
                    )
                }
            }

            // 没有人物就没法开场景，这份快照不算可恢复
            if (assets.none { it.assetType == "person" }) return null

            RemoteCatalog(
                personId = root.optString("person_id").ifBlank { personId },
                catalogVersion = root.optString("catalog_version").ifBlank { catalogVersion },
                poseId = root.optString("pose_id"),
                assets = assets
            )
        }.getOrNull()
    }

    /** R03：记下「现在画面用的是哪一版」，冷启动先恢复它。 */
    fun saveActive(personId: String, catalogVersion: String) {
        root.mkdirs()
        val json = JSONObject().apply {
            put("person_id", personId)
            put("catalog_version", catalogVersion)
            put("activated_at", System.currentTimeMillis())
        }
        writeAtomically(File(root, ACTIVE_FILE), json.toString())
    }

    fun readActive(): Pair<String, String>? {
        val file = File(root, ACTIVE_FILE)
        if (!file.isFile) return null
        return runCatching {
            val json = JSONObject(file.readText())
            val personId = json.optString("person_id")
            val version = json.optString("catalog_version")
            if (personId.isBlank() || version.isBlank()) null else personId to version
        }.getOrNull()
    }

    fun clearActive() {
        File(root, ACTIVE_FILE).delete()
    }

    /** 坏版本：文件校验不过就整目录清掉，别留着下次又去恢复它。 */
    fun dropVersion(personId: String, catalogVersion: String) {
        versionDir(personId, catalogVersion).deleteRecursively()
    }

    /** 清理下载中断留下的临时目录。 */
    fun clearStaging(personId: String) {
        File(root, personId).listFiles()
            ?.filter { it.name.contains(".tmp-") }
            ?.forEach { it.deleteRecursively() }
    }

    /**
     * 原子写：先写 .tmp，再改名顶上。
     * 写到一半掉电，留下的只是 .tmp，正式文件仍是上一份完好的。
     */
    private fun writeAtomically(target: File, content: String) {
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, "${target.name}.tmp")
        tmp.writeText(content)
        if (target.exists() && !target.delete()) {
            // 删不掉就直接覆盖，代价是可能留下半个文件，但至少不会两边都不对
            target.writeText(content)
            tmp.delete()
            return
        }
        if (!tmp.renameTo(target)) {
            target.writeText(content)
            tmp.delete()
        }
    }
}
