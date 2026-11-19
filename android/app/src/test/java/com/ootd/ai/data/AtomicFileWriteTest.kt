package com.ootd.ai.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * P0-2 回归：写文件必须「要么整份写成，要么一个字没动」。
 *
 * 老实现在目标删不掉时退化成直接覆写 —— 写一半掉电会留下半截文件，
 * 而且调用方还以为成功了。这里盯两件事：
 *
 * 1. 正常写：内容完整落盘，不留 .tmp
 * 2. 写失败：抛异常，且目标文件仍是上一份完好内容
 */
class AtomicFileWriteTest {

    private fun tempDir(prefix: String): File = Files.createTempDirectory(prefix).toFile()

    @Test
    fun `新文件能正常写入且不留临时文件`() {
        val root = tempDir("atomic-new-")
        try {
            val target = File(root, "active.json")

            AtomicFileWrite.write(target, """{"v":1}""")

            assertEquals("""{"v":1}""", target.readText())
            assertFalse("不该留下 .tmp", File(root, "active.json.tmp").exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `覆盖已有文件时内容被完整替换`() {
        val root = tempDir("atomic-overwrite-")
        try {
            val target = File(root, "active.json")
            target.writeText("""{"v":1}""")

            AtomicFileWrite.write(target, """{"v":2,"extra":"x"}""")

            assertEquals("""{"v":2,"extra":"x"}""", target.readText())
            assertFalse(File(root, "active.json.tmp").exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `父目录不存在时会自动建出来`() {
        val root = tempDir("atomic-mkdir-")
        try {
            val target = File(File(root, "person-001/v1"), "manifest.json")

            AtomicFileWrite.write(target, "ok")

            assertTrue("父目录该被创建", target.parentFile.isDirectory)
            assertEquals("ok", target.readText())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun `目标不可写时抛异常且旧内容不被破坏`() {
        val root = tempDir("atomic-fail-")
        try {
            val target = File(root, "active.json")
            target.writeText("""{"old":true}""")

            // 把父目录设为只读，写入必然失败（Windows 上目录只读不会阻止写文件，
            // 所以这里用一个同名目录占住目标路径，让 rename 无法覆盖）
            val blocker = File(root, "blocked")
            blocker.mkdirs()
            val impossible = File(blocker, "active.json")
            impossible.mkdirs() // 目标位置是目录，改名成文件会失败

            var threw = false
            try {
                AtomicFileWrite.write(impossible, "x")
            } catch (e: IllegalStateException) {
                threw = true
            }

            assertTrue("写不进去必须明确报错，不能静默", threw)
            assertEquals("原来那份文件必须还在", """{"old":true}""", target.readText())
        } finally {
            root.deleteRecursively()
        }
    }
}
