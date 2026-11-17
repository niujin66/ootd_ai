package com.ootd.ai.data

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile

/**
 * E04：GLB 解码探测的回归测试。
 *
 * 这里要盯住的只有一件事：**哈希对不代表能打开**。
 * 所以除了「真包要过」，更重要的是「坏包要拦」——
 * 尤其是「改了一个字节、体积不变」和「内容被截断」这两类，
 * 它们正是 sha256 检查看不出来、而用户会看到坏画面的情况。
 */
class GlbProbeTest {

    /** gradle 跑单元测试时的工作目录是 app/，仓库根在它的上两级。 */
    private val repoRoot: File = File(System.getProperty("user.dir") ?: ".")
        .let { it.parentFile?.parentFile ?: it }

    /** 仓库里现成的人物模型，用来证明真包不会被误杀。 */
    private fun realGlb(): File =
        File(repoRoot, "outputs/handoff/person-001/fixture-01/person-001.glb")

    private fun tempCopy(source: File): File {
        val tmp = File.createTempFile("probe-", ".glb")
        source.copyTo(tmp, overwrite = true)
        return tmp
    }

    @Test
    fun `正常 glb 能通过探测`() {
        val file = realGlb()
        assumeTrue("仓库里没有样例模型，跳过", file.isFile)

        assertTrue("真包不该被判坏", GlbProbe.canDecode(file))
    }

    @Test
    fun `hash 对但 JSON chunk 坏掉的包会被拦下`() {
        val source = realGlb()
        assumeTrue("仓库里没有样例模型，跳过", source.isFile)

        val broken = tempCopy(source)
        try {
            // 只改 JSON chunk 里的一个字节：文件大小不变，只靠 sha256 完全看不出来
            RandomAccessFile(broken, "rw").use { raf ->
                raf.seek(20) // 头 12 字节 + chunk 头 8 字节之后就是 JSON 内容
                val original = raf.readByte()
                raf.seek(20)
                raf.writeByte(if (original == 0x00.toByte()) 0x41 else 0x00)
            }

            assertFalse("JSON 被改坏还放过去，等于没修 E04", GlbProbe.canDecode(broken))
        } finally {
            broken.delete()
        }
    }

    @Test
    fun `被截断的包会被拦下`() {
        val source = realGlb()
        assumeTrue("仓库里没有样例模型，跳过", source.isFile)

        val truncated = tempCopy(source)
        try {
            RandomAccessFile(truncated, "rw").use { raf ->
                raf.setLength(raf.length() / 2)
            }

            assertFalse("声明长度对不上实际长度，必须拦", GlbProbe.canDecode(truncated))
        } finally {
            truncated.delete()
        }
    }

    @Test
    fun `不是 glTF 的文件会被拦下`() {
        val junk = File.createTempFile("probe-junk-", ".glb")
        try {
            junk.writeBytes(ByteArray(1024) { 0x7F })
            assertFalse("随便一堆字节不能当模型用", GlbProbe.canDecode(junk))
        } finally {
            junk.delete()
        }
    }

    @Test
    fun `空文件会被拦下`() {
        val empty = File.createTempFile("probe-empty-", ".glb")
        try {
            assertFalse(GlbProbe.canDecode(empty))
        } finally {
            empty.delete()
        }
    }
}
