package com.ootd.ai.capture

import android.content.Context

/**
 * 手机端本地调试参数持久化（仅存于应用私有 SharedPreferences，不进入仓库）。
 */
object CaptureDebugPrefs {
    private const val PREFS_NAME = "ootd_capture_debug"
    private const val KEY_DIRECT_ENABLED = "direct_deepseek_enabled"
    private const val KEY_DIRECT_BASE_URL = "direct_deepseek_base_url"
    private const val KEY_DIRECT_API_KEY = "direct_deepseek_api_key"
    private const val KEY_DIRECT_MODEL = "direct_deepseek_model"
    private const val KEY_CUSTOM_SERVER_URL = "custom_server_base_url"

    fun load(context: Context): CaptureDebugConfig {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return CaptureDebugConfig(
            directDeepSeekEnabled = prefs.getBoolean(KEY_DIRECT_ENABLED, false),
            deepSeekBaseUrl = prefs.getString(KEY_DIRECT_BASE_URL, CaptureDebugConfig.DEFAULT_DEEPSEEK_BASE_URL)
                ?: CaptureDebugConfig.DEFAULT_DEEPSEEK_BASE_URL,
            deepSeekApiKey = prefs.getString(KEY_DIRECT_API_KEY, "") ?: "",
            deepSeekModel = prefs.getString(KEY_DIRECT_MODEL, CaptureDebugConfig.DEFAULT_DEEPSEEK_MODEL)
                ?: CaptureDebugConfig.DEFAULT_DEEPSEEK_MODEL,
            customServerBaseUrl = prefs.getString(KEY_CUSTOM_SERVER_URL, "") ?: ""
        )
    }

    fun save(context: Context, config: CaptureDebugConfig) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_DIRECT_ENABLED, config.directDeepSeekEnabled)
            .putString(KEY_DIRECT_BASE_URL, config.deepSeekBaseUrl.trim())
            .putString(KEY_DIRECT_API_KEY, config.deepSeekApiKey.trim())
            .putString(KEY_DIRECT_MODEL, config.deepSeekModel.trim())
            .putString(KEY_CUSTOM_SERVER_URL, config.customServerBaseUrl.trim())
            .apply()
    }
}
