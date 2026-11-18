package com.ootd.ai.data

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.security.MessageDigest

/**
 * 2026-11-18 真机闪退的回归测试。
 *
 * 那次的根因不是坏包，而是两个**完全合法**的材质扩展：
 * `KHR_materials_specular` / `KHR_materials_sheen`。Filament 1.77.3 处理它们时直接 abort，
 * Java 层拦不到。这组测试盯的就是「剥掉扩展之后，文件是不是还原成了能加载的那份」。
 *
 * 最强的一条断言是哈希相等：剥完的字节必须和 App 内置素材一模一样 ——
 * 内置那份在真机上一直能正常加载，所以这条等价于「剥完一定能加载」。
 */
class GlbSanitizerTest {

    private val repoRoot: File = File(System.getProperty("user.dir") ?: ".")
        .let { it.parentFile?.parentFile ?: it }

    private fun remote(name: String) = File(repoRoot, "outputs/fault-fixtures/good/$name.glb")
    private fun bundled(name: String) =
        File(repoRoot, "android/app/src/main/assets/local-demo/$name.glb")

    private fun sha256(file: File): String =
        MessageDigest.getInstance("SHA-256").digest(file.readBytes())
            .joinToString("") { "%02x".format(it) }

    private fun tempCopy(source: File): File {
        val tmp = File.createTempFile("sanitize-", ".glb")
        source.copyTo(tmp, overwrite = true)
        return tmp
    }

    /**
     * 剥完必须和内置素材等价。
     *
     * 为什么不比字节：JVM 测试环境的 `org.json` 不保证 key 顺序（Android 运行时是
     * LinkedHashMap，这里不是），重新序列化后字节顺序会变。所以比「JSON 语义 + BIN 字节」：
     * 两者都对上，说明除了字段顺序之外，这个文件和内置那份完全一样。
     */
    @Test
    fun `top-001 剥掉材质扩展后与内置素材等价`() {
        val src = remote("top-001")
        val expected = bundled("top-001_v2")
        assumeTrue("缺少样例模型，跳过", src.isFile && expected.isFile)

        val work = tempCopy(src)
        try {
            assertTrue("带 specular 扩展的包应该被改动", GlbSanitizer.sanitizeInPlace(work))
            assertEquivalent(expected, work)
        } finally {
            work.delete()
        }
    }

    @Test
    fun `top-002 剥掉材质扩展后与内置素材等价`() {
        val src = remote("top-002")
        val expected = bundled("top-002_v1")
        assumeTrue("缺少样例模型，跳过", src.isFile && expected.isFile)

        val work = tempCopy(src)
        try {
            assertTrue("带 specular + sheen 扩展的包应该被改动", GlbSanitizer.sanitizeInPlace(work))
            assertEquivalent(expected, work)
        } finally {
            work.delete()
        }
    }

    /** 比对 JSON（排序后逐键比）和 BIN chunk（逐字节）。 */
    private fun assertEquivalent(expected: File, actual: File) {
        val expectedJson = jsonChunk(expected).sortedKeyJson()
        val actualJson = jsonChunk(actual).sortedKeyJson()
        assertEquals("JSON 语义必须与内置素材一致", expectedJson, actualJson)

        val expectedBin = binChunk(expected)
        val actualBin = binChunk(actual)
        assertNotNull("内置素材必须有 BIN chunk", expectedBin)
        assertNotNull("净化后不能丢 BIN chunk", actualBin)
        assertTrue("BIN chunk 必须逐字节一致", expectedBin!!.contentEquals(actualBin!!))
    }

    @Test
    fun `已经干净的包不会被改动`() {
        val clean = bundled("top-001_v2")
        assumeTrue("缺少内置素材，跳过", clean.isFile)

        val work = tempCopy(clean)
        try {
            val before = sha256(work)
            assertFalse("没有可剥的扩展就不该动文件", GlbSanitizer.sanitizeInPlace(work))
            assertEquals("不该改动字节", before, sha256(work))
        } finally {
            work.delete()
        }
    }

    @Test
    fun `剥离后几何与贴图数据保持原样`() {
        val src = remote("top-001")
        assumeTrue("缺少样例模型，跳过", src.isFile)

        val work = tempCopy(src)
        try {
            val beforeBin = binChunk(work)
            GlbSanitizer.sanitizeInPlace(work)
            val afterBin = binChunk(work)
            assertNotNull("BIN chunk 不能丢", afterBin)
            assertEquals("BIN chunk（几何/贴图）必须一个字节都不变", beforeBin?.size, afterBin?.size)
            assertTrue("BIN 内容必须完全一致", beforeBin!!.contentEquals(afterBin!!))
        } finally {
            work.delete()
        }
    }

    @Test
    fun `净化后的文件仍然能通过结构探测`() {
        val src = remote("top-002")
        assumeTrue("缺少样例模型，跳过", src.isFile)

        val work = tempCopy(src)
        try {
            GlbSanitizer.sanitizeInPlace(work)
            assertTrue("剥扩展不能把结构弄坏", GlbProbe.canDecode(work))
        } finally {
            work.delete()
        }
    }

    @Test
    fun `不是 GLB 的文件会抛错而不是静默改坏`() {
        val junk = File.createTempFile("sanitize-junk-", ".glb")
        try {
            junk.writeBytes(ByteArray(64) { 0x7F })
            var threw = false
            try {
                GlbSanitizer.sanitize(junk)
            } catch (e: IllegalStateException) {
                threw = true
            }
            assertTrue("结构坏到没法处理时应该明确报错，不能悄悄返回", threw)
            assertFalse("原地净化遇到坏文件也不能动它", GlbSanitizer.sanitizeInPlace(junk))
        } finally {
            junk.delete()
        }
    }

    /** 取 JSON chunk 并解析。 */
    private fun jsonChunk(file: File): JSONObject {
        val bytes = file.readBytes()
        var offset = 12
        while (offset + 8 <= bytes.size) {
            val length = java.nio.ByteBuffer.wrap(bytes, offset, 4)
                .order(java.nio.ByteOrder.LITTLE_ENDIAN).int.toLong() and 0xFFFFFFFFL
            val type = java.nio.ByteBuffer.wrap(bytes, offset + 4, 4)
                .order(java.nio.ByteOrder.LITTLE_ENDIAN).int
            val start = offset + 8
            if (type == 0x4E4F534A) {
                val text = String(bytes, start, length.toInt(), Charsets.UTF_8)
                return JSONObject(text.trim().trimEnd('\u0000'))
            }
            offset = start + length.toInt()
        }
        throw IllegalStateException("没有 JSON chunk")
    }

    /** 把 JSON 递归按键排序后重新序列化，用来消除字段顺序差异。 */
    private fun JSONObject.sortedKeyJson(): String = sortValue(this).toString()

    private fun sortValue(value: Any?): Any? = when (value) {
        is JSONObject -> {
            val sorted = JSONObject()
            value.keys().asSequence().sorted().forEach { k ->
                sorted.put(k, sortValue(value.opt(k)))
            }
            sorted
        }
        is org.json.JSONArray -> {
            val arr = org.json.JSONArray()
            for (i in 0 until value.length()) arr.put(sortValue(value.opt(i)))
            arr
        }
        else -> value
    }

    /** 取 BIN chunk 的原始字节。 */
    private fun binChunk(file: File): ByteArray? {
        val bytes = file.readBytes()
        var offset = 12
        while (offset + 8 <= bytes.size) {
            val length = java.nio.ByteBuffer.wrap(bytes, offset, 4)
                .order(java.nio.ByteOrder.LITTLE_ENDIAN).int.toLong() and 0xFFFFFFFFL
            val type = java.nio.ByteBuffer.wrap(bytes, offset + 4, 4)
                .order(java.nio.ByteOrder.LITTLE_ENDIAN).int
            val start = offset + 8
            if (type == 0x004E4942) {
                return bytes.copyOfRange(start, start + length.toInt())
            }
            offset = start + length.toInt()
        }
        return null
    }
}
