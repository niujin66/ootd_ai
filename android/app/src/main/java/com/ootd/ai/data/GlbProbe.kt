package com.ootd.ai.data

import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * 真解码探测（E04 缺口修复）。
 *
 * 为什么需要它：`sha256` 只能证明「文件字节没被改动过」，不能证明「模型能打开」。
 * 一个 GLB 完全可能在哈希对上、体积也对上的情况下，内部 JSON chunk 是坏的、
 * 引用的 accessor 越界、或者干脆只剩一个 BIN 段 —— Filament 打开它必然失败。
 * 如果只按哈希放行，这种包会被当成正常版本存下来、标成生效版本、上屏失败，
 * 并且下次冷启动还会被再次恢复出来。
 *
 * 这里按 glTF 2.0 二进制容器格式把结构走一遍。它不是完整的解码器（不做几何与
 * accessor 的数值校验），但足以挡住「哈希对、结构坏」这一类包：
 *
 * 1. 12 字节头：magic 必须是 `glTF`，version 必须是 2，声明长度必须等于实际文件长度
 * 2. 第一个 chunk 必须是 JSON chunk，且能被完整解析
 * 3. `asset.version` 必须是 2.x
 * 4. 至少有一个 mesh，且 mesh 里有图元（没有图元的包画不出任何东西）
 *
 * 任何一条不过都抛带中文原因的异常，调用方按「校验失败」处理。
 */
object GlbProbe {

    private const val MAGIC_GLTF = 0x46546C67 // "glTF"
    private const val MAGIC_JSON = 0x4E4F534A // "JSON"
    private const val HEADER_BYTES = 12
    private const val CHUNK_HEADER_BYTES = 8

    /** JSON chunk 撑到这么大就不是模型了，多半是被塞了别的东西。 */
    private const val MAX_JSON_CHUNK_BYTES = 16L * 1024 * 1024

    /** 只回答「能不能用」，不解释原因。 */
    fun canDecode(file: File): Boolean = runCatching { probe(file) }.isSuccess

    /**
     * 结构探测。能打开就正常返回，打不开就抛 [IllegalStateException]。
     */
    fun probe(file: File) {
        if (!file.isFile) {
            throw IllegalStateException("文件不存在")
        }

        val size = file.length()
        if (size < HEADER_BYTES + CHUNK_HEADER_BYTES) {
            throw IllegalStateException("文件太小，不是 glTF 二进制（$size 字节）")
        }

        RandomAccessFile(file, "r").use { raf ->
            val header = readChunk(raf, 0, HEADER_BYTES)
            val magic = header.int
            val version = header.int
            val declared = header.int.toLong() and 0xFFFFFFFFL

            if (magic != MAGIC_GLTF) {
                throw IllegalStateException("GLB 头不对：magic 不是 glTF")
            }
            if (version != 2) {
                throw IllegalStateException("只支持 glTF 2.0，当前是 $version")
            }
            if (declared != size) {
                throw IllegalStateException("GLB 声明长度 $declared 与实际 $size 不符")
            }

            var offset = HEADER_BYTES.toLong()
            var sawJson = false

            while (offset + CHUNK_HEADER_BYTES <= size) {
                val chunkHeader = readChunk(raf, offset, CHUNK_HEADER_BYTES)
                val length = chunkHeader.int.toLong() and 0xFFFFFFFFL
                val type = chunkHeader.int
                val dataStart = offset + CHUNK_HEADER_BYTES

                if (dataStart + length > size) {
                    throw IllegalStateException("chunk 声明长度超出文件尾（数据被截断）")
                }

                if (!sawJson) {
                    if (type != MAGIC_JSON) {
                        throw IllegalStateException("第一个 chunk 不是 JSON chunk")
                    }
                    if (length > MAX_JSON_CHUNK_BYTES) {
                        throw IllegalStateException("JSON chunk 异常大（$length 字节）")
                    }
                    inspectJsonChunk(raf, dataStart, length)
                    sawJson = true
                }

                offset = dataStart + length
            }

            if (!sawJson) {
                throw IllegalStateException("GLB 里没有 JSON chunk")
            }
        }
    }

    /**
     * 解析 JSON chunk，确认这份 glTF 描述的是一个能画的模型。
     *
     * 这里刻意不看 accessor/buffer 的数值细节 —— 那属于完整解码，成本高且容易
     * 误伤；本函数的目标只是把「结构坏」「不是模型」挡在门外。
     */
    private fun inspectJsonChunk(raf: RandomAccessFile, start: Long, length: Long) {
        if (length <= 0) {
            throw IllegalStateException("JSON chunk 是空的")
        }

        val bytes = ByteArray(length.toInt())
        raf.seek(start)
        raf.readFully(bytes)

        val text = String(bytes, Charsets.UTF_8).trim().trimEnd('\u0000')
        val root = try {
            JSONObject(text)
        } catch (e: Exception) {
            throw IllegalStateException("JSON chunk 解析失败，模型描述是坏的", e)
        }

        val asset = root.optJSONObject("asset")
            ?: throw IllegalStateException("GLB 缺少 asset 段")
        val assetVersion = asset.optString("version")
        if (!assetVersion.startsWith("2.")) {
            throw IllegalStateException("asset.version=$assetVersion 不是 glTF 2.x")
        }

        val meshes = root.optJSONArray("meshes")
            ?: throw IllegalStateException("GLB 里没有 meshes，画不出任何东西")
        if (meshes.length() == 0) {
            throw IllegalStateException("GLB 的 meshes 是空的")
        }

        var primitives = 0
        for (i in 0 until meshes.length()) {
            val mesh = meshes.optJSONObject(i) ?: continue
            primitives += mesh.optJSONArray("primitives")?.length() ?: 0
        }
        if (primitives == 0) {
            throw IllegalStateException("GLB 里没有任何图元（primitives），画不出任何东西")
        }
    }

    /** 从 [offset] 读 [count] 字节，按小端解释（glTF 全程小端）。 */
    private fun readChunk(raf: RandomAccessFile, offset: Long, count: Int): ByteBuffer {
        val buffer = ByteArray(count)
        raf.seek(offset)
        raf.readFully(buffer)
        return ByteBuffer.wrap(buffer).order(ByteOrder.LITTLE_ENDIAN)
    }
}
