package com.ootd.ai.data

import java.io.File

/**
 * P0-2：把已经下载好的 staging 目录整体顶替成正式版本目录。
 *
 * 这一段的旧写法是「先 delete 旧目录，再 rename 新目录」。删完和改名之间只要出事
 * （异常、进程被杀、断电），旧素材就没了、新的也没顶上，用户手里两手空。
 *
 * 现在的顺序是「挪开旧的 → 新的顶上 → 成功后才删旧的」：
 *
 * ```
 * target  --rename-->  target.retired-<ts>
 * staging --rename-->  target            (失败则把 retired 改回 target)
 * target.retired-<ts>  --delete-->       (只有真的换成了才删)
 * ```
 *
 * 对外保证：**只要这个函数抛了异常，[target] 就还是调用前那个版本**。
 */
object PackageDirSwap {

    fun swapIn(target: File, staging: File) {
        val parent = target.parentFile
            ?: throw IllegalStateException("目标目录没有父目录：${target.path}")
        if (!staging.isDirectory) {
            throw IllegalStateException("staging 目录不存在：${staging.path}")
        }
        // 空目录改名也会「成功」，但切过去等于把素材换没了 —— 先在这拦掉
        if (staging.listFiles().isNullOrEmpty()) {
            throw IllegalStateException("staging 目录是空的，没有可切换的内容")
        }

        val retired = File(parent, "${target.name}.retired-${System.currentTimeMillis()}")
        val hadOld = target.exists()

        if (hadOld && !target.renameTo(retired)) {
            // 挪不开就别硬来。宁可这一轮同步失败，也不能把还能用的旧版本删掉。
            throw IllegalStateException("旧版本目录改名失败，放弃本次切换（旧版本保持不动）")
        }

        try {
            moveStagingInto(target, staging)
        } catch (t: Throwable) {
            // 新版没顶上，把旧版挪回去，目录结构恢复原样再往外抛
            runCatching {
                if (target.exists()) target.deleteRecursively()
                if (hadOld && retired.exists()) retired.renameTo(target)
            }
            throw t
        }

        // 新版已经就位，旧版才真正没用了
        if (hadOld) runCatching { retired.deleteRecursively() }
    }

    /**
     * 把 [staging] 变成 [target]。
     *
     * 优先整目录改名（同一分区下是原子操作）；跨分区或目标已被占用时，
     * 退化成逐文件复制，并在复制完后确认目标里确实有东西。
     */
    private fun moveStagingInto(target: File, staging: File) {
        if (staging.renameTo(target)) return

        target.mkdirs()
        val children = staging.listFiles() ?: emptyArray()
        if (children.isEmpty()) {
            throw IllegalStateException("staging 目录是空的，没有可切换的内容")
        }
        children.forEach { child ->
            child.copyTo(File(target, child.name), overwrite = true)
        }

        val landed = target.listFiles() ?: emptyArray()
        if (landed.isEmpty()) {
            throw IllegalStateException("文件搬移后目标目录仍为空")
        }

        staging.deleteRecursively()
    }
}
