package com.ootd.ai.data

import kotlinx.coroutines.CancellationException

/**
 * P0-2：[runCatching] 的取消安全版。
 *
 * `runCatching` 捕获的是 `Throwable`，会把 [CancellationException] 也一起吞掉。
 * 后果不是「少了个报错」，而是**协程取消被吞之后不会真正停下**：
 *
 * - 界面上用户点了取消，代码却把它当成「同步失败」继续往下走
 * - 更糟的是后续逻辑还在改状态，看起来像是取消没生效
 *
 * 规范做法是让取消异常一路往上抛，交给协程框架处理；只有真正的业务异常
 * 才装进 `Result` 里由调用方判断。这个函数就是干这件事的。
 */
inline fun <T> runCatchingNotCancelling(block: () -> T): Result<T> =
    try {
        Result.success(block())
    } catch (t: Throwable) {
        if (t is CancellationException) throw t
        Result.failure(t)
    }
