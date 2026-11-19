package com.ootd.ai.data

import java.io.File
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * P0-2：文件级原子写。
 *
 * 从 [PersistentCatalogStore] 里抽出来，一是让它不依赖 Android Context 就能单测，
 * 二是把「写到一半算什么」这件事说清楚：
 *
 * - 正常路径：写 .tmp，再一次 rename 顶上。rename 在 Linux/Android 上本身就能覆盖，
 *   所以**不需要先删目标**——先删就会出现「旧的没了、新的没到」的窗口。
 * - 不支持原子改名时（个别外置存储）：降级成普通替换，但仍然是 rename 语义，
 *   目标不会出现「先空着」的状态。
 * - 失败：抛 [IllegalStateException]，目标文件保持调用前的样子。
 *   绝不像旧版那样退化成直接覆写（那会把目标写成半截，还让调用方以为成功了）。
 */
object AtomicFileWrite {

    fun write(target: File, content: String) {
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, "${target.name}.tmp")
        tmp.writeText(content)

        try {
            runCatching {
                Files.move(
                    tmp.toPath(),
                    target.toPath(),
                    StandardCopyOption.REPLACE_EXISTING,
                    StandardCopyOption.ATOMIC_MOVE,
                )
            }.recoverCatching { e ->
                if (e is AtomicMoveNotSupportedException) {
                    Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
                } else {
                    throw e
                }
            }.getOrThrow()
        } catch (t: Throwable) {
            // 写不成就明确失败：调用方据此不宣布「已同步」，旧文件也不会被动过
            runCatching { tmp.delete() }
            throw IllegalStateException("写 ${target.name} 失败", t)
        }
    }
}
