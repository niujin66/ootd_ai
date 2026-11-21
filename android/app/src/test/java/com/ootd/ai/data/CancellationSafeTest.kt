package com.ootd.ai.data

import kotlinx.coroutines.CancellationException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * P0-2 回归：协程取消必须原样上抛。
 *
 * 普通 `runCatching` 会把 [CancellationException] 也装进 Result，结果就是
 * 「用户点了取消，代码当成失败继续跑」。这组测试盯的就是这条边界。
 */
class CancellationSafeTest {

    @Test
    fun `取消异常必须原样上抛而不是被装进 Result`() {
        var caught: CancellationException? = null
        try {
            runCatchingNotCancelling<Unit> {
                throw CancellationException("用户取消")
            }
        } catch (e: CancellationException) {
            caught = e
        }

        assertTrue("取消异常必须抛出来，不能被 Result 吃掉", caught != null)
        assertEquals("用户取消", caught?.message)
    }

    @Test
    fun `普通业务异常照旧装进 Result`() {
        val result = runCatchingNotCancelling<Unit> {
            throw IllegalStateException("网络断了")
        }

        assertTrue("业务异常应当交给调用方判断", result.isFailure)
        assertEquals("网络断了", result.exceptionOrNull()?.message)
    }

    @Test
    fun `正常返回时结果是成功`() {
        val result = runCatchingNotCancelling { 42 }

        assertFalse(result.isFailure)
        assertEquals(42, result.getOrNull())
    }
}
