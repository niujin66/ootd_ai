package com.ootd.ai.data

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * GLB 材质扩展净化。
 *
 * 为什么需要它：2026-11-18 定位到真机闪退的真正原因 —— 服务端下发的衣物包用了
 * `KHR_materials_specular` 和 `KHR_materials_sheen` 两个材质扩展，Filament 1.77.3 的
 * gltfio 在 `AssetLoader::createAsset` 里处理它们时直接 `PreconditionPanic` → SIGABRT，
 * Java 层拦不到，进程整个死掉。
 *
 * 但这两个文件**完全合法**：Khronos 官方校验器 0 错误 0 警告，`extensionsRequired` 是空的。
 * 按 glTF 2.0 规范，客户端可以安全忽略非必需扩展 —— 所以崩是 Filament 的实现问题，不是资产问题。
 *
 * 本地做过字节级验证：把这两个扩展从 JSON 里剥掉之后，文件哈希与 App 内置素材**完全一致**，
 * 而内置素材在真机上一直能正常加载。也就是说"剥掉扩展"不是降级，而是还原成原本能用的那份。
 *
 * 这里做的事：解析 GLB 容器 → 在 JSON chunk 里删掉这些扩展的声明与材质引用 → 重新打包。
 * 几何、贴图、BIN chunk 一个字节都不动。
 */
object GlbSanitizer {

    /**
     * 需要剥掉的扩展。
     *
     * 只在 `extensionsUsed` 里出现、`extensionsRequired` 里没出现时才会剥 —— 必需扩展不能删，
     * 删了模型就真的坏了，那种情况应该让上层按「不支持」处理。
     */
    private val STRIP_EXTENSIONS = setOf(
        "KHR_materials_specular",
        "KHR_materials_sheen",
    )

    private const val MAGIC_GLTF = 0x46546C67 // "glTF"
    private const val MAGIC_JSON = 0x4E4F534A // "JSON"

    /** 原地净化。返回 true 表示文件被改动过，false 表示本来就没有要剥的扩展。 */
    fun sanitizeInPlace(file: File): Boolean {
        val result = runCatching { sanitize(file) }.getOrNull() ?: return false
        if (result == null) return false

        // 先写临时文件再原子替换，避免写一半把原文件弄坏
        val tmp = File(file.parentFile, file.name + ".sanitizing")
        try {
            tmp.writeBytes(result)
            Files.move(
                tmp.toPath(),
                file.toPath(),
                StandardCopyOption.REPLACE_EXISTING,
            )
        } finally {
            if (tmp.exists()) tmp.delete()
        }
        return true
    }

    /**
     * 返回净化后的 GLB 字节；如果不需要改动则返回 null。
     *
     * 抛异常表示这个文件结构坏到没法处理，调用方按「处理失败」对待。
     */
    fun sanitize(file: File): ByteArray? {
        val bytes = file.readBytes()
        if (bytes.size < 20) throw IllegalStateException("文件太小，不是 GLB")

        val header = ByteBuffer.wrap(bytes, 0, 12).order(ByteOrder.LITTLE_ENDIAN)
        if (header.int != MAGIC_GLTF) throw IllegalStateException("GLB 头不对")
        val version = header.int
        val declared = header.int.toLong() and 0xFFFFFFFFL
        if (version != 2) throw IllegalStateException("只支持 glTF 2.0")
        if (declared != bytes.size.toLong()) throw IllegalStateException("声明长度与实际不符")

        // 切出所有 chunk，保留原始字节
        val chunks = mutableListOf<Chunk>()
        var offset = 12
        while (offset + 8 <= bytes.size) {
            val buf = ByteBuffer.wrap(bytes, offset, 8).order(ByteOrder.LITTLE_ENDIAN)
            val length = buf.int.toLong() and 0xFFFFFFFFL
            val type = buf.int
            val dataStart = offset + 8
            if (dataStart + length > bytes.size) throw IllegalStateException("chunk 超出文件尾")
            chunks += Chunk(type, bytes.copyOfRange(dataStart, (dataStart + length).toInt()))
            offset = dataStart + length.toInt()
        }
        if (chunks.isEmpty() || chunks[0].type != MAGIC_JSON) {
            throw IllegalStateException("第一个 chunk 不是 JSON")
        }

        val json = parseJson(chunks[0].data) ?: return null
        val changed = stripExtensions(json) ?: return null
        if (!changed) return null

        chunks[0] = Chunk(MAGIC_JSON, encodeJson(json))
        return repack(chunks)
    }

    private fun parseJson(data: ByteArray): JSONObject? = runCatching {
        JSONObject(String(data, Charsets.UTF_8).trim().trimEnd('\u0000'))
    }.getOrNull()

    /**
     * 删掉扩展声明与材质里的引用。返回是否改动了内容。
     *
     * 必需扩展（`extensionsRequired`）里的不删 —— 那种包不该走到这里。
     */
    private fun stripExtensions(json: JSONObject): Boolean? {
        val required = json.optJSONArray("extensionsRequired")
        val requiredSet = buildSet {
            if (required != null) {
                for (i in 0 until required.length()) {
                    required.optString(i).takeIf { it.isNotEmpty() }?.let { add(it) }
                }
            }
        }
        val strippable = STRIP_EXTENSIONS - requiredSet
        if (strippable.isEmpty()) return null

        var changed = false

        json.optJSONArray("extensionsUsed")?.let { used ->
            val kept = JSONArray()
            for (i in 0 until used.length()) {
                val name = used.optString(i)
                if (name in strippable) changed = true else kept.put(name)
            }
            if (kept.length() == 0) json.remove("extensionsUsed") else json.put("extensionsUsed", kept)
        }

        json.optJSONArray("materials")?.let { materials ->
            for (i in 0 until materials.length()) {
                val material = materials.optJSONObject(i) ?: continue
                val ext = material.optJSONObject("extensions") ?: continue
                var touched = false
                for (name in strippable) {
                    if (ext.has(name)) {
                        ext.remove(name)
                        touched = true
                    }
                }
                if (touched) {
                    changed = true
                    if (ext.length() == 0) material.remove("extensions")
                }
            }
        }

        return if (changed) true else null
    }

    private fun encodeJson(json: JSONObject): ByteArray {
        val raw = json.toString().toByteArray(Charsets.UTF_8)
        val pad = (4 - raw.size % 4) % 4
        return if (pad == 0) raw else raw + ByteArray(pad) { ' '.code.toByte() }
    }

    /** 按 GLB 规范重新打包：每段都补到 4 字节对齐，并重算总长度。 */
    private fun repack(chunks: List<Chunk>): ByteArray {
        var total = 12
        val padded = chunks.map { chunk ->
            val pad = (4 - chunk.data.size % 4) % 4
            val data = if (pad == 0) chunk.data else chunk.data + ByteArray(pad)
            total += 8 + data.size
            Chunk(chunk.type, data)
        }

        val out = ByteBuffer.allocate(total).order(ByteOrder.LITTLE_ENDIAN)
        out.putInt(MAGIC_GLTF)
        out.putInt(2)
        out.putInt(total)
        for (chunk in padded) {
            out.putInt(chunk.data.size)
            out.putInt(chunk.type)
            out.put(chunk.data)
        }
        return out.array()
    }

    private data class Chunk(val type: Int, val data: ByteArray)
}
