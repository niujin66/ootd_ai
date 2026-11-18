package com.ootd.ai.ui

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * E04（P0-1 补完）：生效指针对「人物」和「默认衣物」都要看着落。
 *
 * 修复前是只看人物，结果「人好、衣服坏」的包会被记成生效版本：
 * 人事后一点换衣就砸，下次冷启动还在恢复它。
 */
class PublishGateTest {

    @Test
    fun `人物与衣物都渲染成功才允许发布`() {
        assertTrue(PublishGate.shouldPublish(personReady = true, garmentReady = true))
    }

    @Test
    fun `只有人物成功不算通过`() {
        assertFalse(PublishGate.shouldPublish(personReady = true, garmentReady = false))
    }

    @Test
    fun `只有衣物成功不算通过`() {
        assertFalse(PublishGate.shouldPublish(personReady = false, garmentReady = true))
    }

    @Test
    fun `都没成功不通过`() {
        assertFalse(PublishGate.shouldPublish(personReady = false, garmentReady = false))
    }
}
