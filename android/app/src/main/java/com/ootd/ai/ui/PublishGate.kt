package com.ootd.ai.ui

/**
 * E04（P0-1 补完）：什么时候才允许把一个刚同步下来的候选包登记成「生效版本」。
 *
 * 之前的写法是「人物一 Ready 就写生效指针」。实测能过，但漏了一种包：
 * 人物那件是好的、可切换衣物是坏的 —— 这种包人照样画得出来，
 * 指针就被记上去了，用户一点换衣就炸，下次冷启动还在恢复它。
 *
 * 所以判据要两条都成立：
 * - 人物真的被 Filament 画出来过（不是「哈希对」也不是「结构能解析」）
 * - 默认展示的那件衣物也真的被画出来过
 *
 * 单独抽出来是为了能写单测把语义钉住 —— 以后再有人想改回「只看人物」，
 * 会直接踩到 [PublishGateTest] 上。
 */
internal object PublishGate {

    /**
     * @param personReady 人物是否已经实际渲染成功
     * @param garmentReady 默认展示的衣物是否已经实际渲染成功
     * @return true 才允许写生效指针
     */
    fun shouldPublish(personReady: Boolean, garmentReady: Boolean): Boolean =
        personReady && garmentReady
}
