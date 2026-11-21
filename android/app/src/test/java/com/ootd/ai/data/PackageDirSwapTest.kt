package com.ootd.ai.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * P0-2 回归：切版本目录不能把旧素材弄丢。
 *
 * 老实现是「删掉旧目录 → 改名新目录」。删完和改名之间出事，用户就两手空。
 * 这组测试盯的就是 [PackageDirSwap] 的那条保证：
 * **只要抛异常，目标目录还是调用前那份。**
 */
class PackageDirSwapTest {

    private fun tempDir(prefix: String): File =
        Files.createTempDirectory(prefix).toFile()

    private fun File.putFiles(vararg names: String): File {
        mkdirs()
        names.forEach { File(this, it).writeText("old") }
        return this
    }

    private fun File.names(): Set<String> =
        listFiles()?.map { it.name }?.toSet() ?: emptySet()

    @Test
    fun `首次安装时 staging 直接变成正式目录`() {
        val root = tempDir("swap-first-")
        try {
            val target = File(root, "v1")
            val staging = File(root, "v1.tmp-1").putFiles("person.glb")

            PackageDirSwap.swapIn(target, staging)

            assertTrue("目标目录应该被创建出来", target.isDirectory)
            assertEquals(setOf("person.glb"), target.names())
            assertFalse("staging 不该还留着", staging.exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `换新版本后内容是新那份，旧文件名不残留`() {
        val root = tempDir("swap-replace-")
        try {
            val target = File(root, "v1").putFiles("old-a.glb", "old-b.glb")
            val staging = File(root, "v1.tmp-2").putFiles("new-a.glb")

            PackageDirSwap.swapIn(target, staging)

            assertEquals("旧文件不能残留", setOf("new-a.glb"), target.names())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `staging 不存在时抛错且旧版本原封不动`() {
        val root = tempDir("swap-missing-")
        try {
            val target = File(root, "v1").putFiles("old-a.glb", "old-b.glb")
            val staging = File(root, "v1.tmp-3") // 不创建

            var threw = false
            try {
                PackageDirSwap.swapIn(target, staging)
            } catch (e: IllegalStateException) {
                threw = true
            }

            assertTrue("staging 缺失必须明确报错", threw)
            assertEquals("旧版本必须一个文件都不少", setOf("old-a.glb", "old-b.glb"), target.names())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `空的 staging 目录不算一次成功的切换`() {
        val root = tempDir("swap-empty-")
        try {
            val target = File(root, "v1").putFiles("old-a.glb")
            val staging = File(root, "v1.tmp-4").apply { mkdirs() }

            var threw = false
            try {
                PackageDirSwap.swapIn(target, staging)
            } catch (e: IllegalStateException) {
                threw = true
            }

            assertTrue("空目录切换应当失败", threw)
            assertEquals("失败后旧版本仍要完好", setOf("old-a.glb"), target.names())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `切换完成后不留下 retired 临时目录`() {
        val root = tempDir("swap-retired-")
        try {
            val target = File(root, "v1").putFiles("old.glb")
            val staging = File(root, "v1.tmp-5").putFiles("new.glb")

            PackageDirSwap.swapIn(target, staging)

            val leftovers = root.listFiles()?.filter { it.name.contains(".retired-") } ?: emptyList()
            assertTrue("成功切换后不该留着 retired 目录：$leftovers", leftovers.isEmpty())
        } finally {
            root.deleteRecursively()
        }
    }
}
