package com.ootd.ai.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableIntState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.ootd.ai.BuildConfig
import com.ootd.ai.data.ActiveAssets
import com.ootd.ai.data.ApiException
import com.ootd.ai.data.CatalogApi
import com.ootd.ai.data.GarmentOption
import com.ootd.ai.data.LocalAssetRepository
import com.ootd.ai.data.RecommendationRequest
import com.ootd.ai.data.RemotePackage
import com.ootd.ai.data.RemotePackageRepository
import com.ootd.ai.data.toActiveAssets
import io.github.sceneview.ModelLoadState
import io.github.sceneview.SceneView
import io.github.sceneview.node.ModelNode
import io.github.sceneview.rememberEngine
import io.github.sceneview.rememberModelLoadState
import io.github.sceneview.rememberModelLoader
import io.github.sceneview.loaders.ModelLoader
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 加载模型：APK 内的资产（相对路径）走 SceneView 的资产加载；
 * 服务端下载到磁盘的包是绝对路径，必须用 File 直接读，
 * 否则 Filament 会把它当成 asset 名去查，必然失败。
 */
@Composable
private fun rememberModelState(modelLoader: ModelLoader, path: String?): ModelLoadState? {
    if (path.isNullOrEmpty()) return null
    if (!path.startsWith("/")) return rememberModelLoadState(modelLoader, path)

    var state by remember(path) { mutableStateOf<ModelLoadState?>(null) }
    LaunchedEffect(path) {
        state = null
        // Filament 引擎只能在它创建的那个线程（主线程）上用，放后台线程会直接 native 崩溃。
        state = runCatching { modelLoader.createModelInstance(java.io.File(path)) }
            .fold(
                onSuccess = { ModelLoadState.Ready(it) },
                onFailure = { ModelLoadState.Failed(it) }
            )
    }
    return state
}

/**
 * 三维试衣页。
 *
 * B02：从 assets/local-demo 加载本地包，手指拖动旋转、两件衣物切换。
 * B03：在旁边加远端目录入口 —— 拉 /v1/catalog、下载整包、校验 size 与 sha256，
 *      整版通过才切换；再接 /v1/recommendations 驱动换衣，展示理由与模式。
 */
/**
 * 把底层异常翻译成用户能看懂的一句话。
 *
 * 直接展示 t.message 会把 okhttp 内部对象名（如 Address@eeea04995）铺满屏幕，
 * 用户看不懂也帮不上忙。这里按类型分：网络类给中文结论，业务类保留服务端原话。
 */
private fun readableSyncError(t: Throwable): String = when (t) {
    // 业务错误：服务端明确回了状态码，原话有信息量，保留
    is ApiException -> t.message?.takeIf { it.isNotBlank() } ?: "服务端返回 HTTP ${t.code}"
    is java.net.UnknownHostException -> "找不到服务端地址，请检查网络"
    is java.net.ConnectException -> "连不上服务端"
    is java.net.SocketTimeoutException -> "连接服务端超时"
    is javax.net.ssl.SSLException -> "和服务端的安全连接建立失败"
    is java.io.IOException -> "网络连接中断"
    // 下载后的校验失败（缺文件、大小或哈希不符）自带可读说明
    is IllegalStateException -> t.message?.takeIf { it.isNotBlank() } ?: "资产校验没通过"
    else -> t.message?.trim()?.takeIf { it.isNotBlank() }
        ?.let { if (it.length > 60) it.take(60) + "…" else it }
        ?: "未知错误"
}

@Composable
fun TryOnScreen(onBack: () -> Unit) {

    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    val localRepository = remember { LocalAssetRepository(context) }
    val api = remember { CatalogApi(BuildConfig.OOTD_API_BASE_URL, BuildConfig.OOTD_API_TOKEN) }
    val remoteRepository = remember { RemotePackageRepository(context, api) }

    // 本地包：读不到就保持 null，不阻塞远端流程
    var localAssets by remember { mutableStateOf<ActiveAssets?>(null) }
    var localError by remember { mutableStateOf<String?>(null) }

    // 远端包
    var remoteAssets by remember { mutableStateOf<ActiveAssets?>(null) }
    var syncing by remember { mutableStateOf(false) }
    var syncStatus by remember { mutableStateOf<String?>(null) }
    var syncError by remember { mutableStateOf<String?>(null) }

    // 当前生效的一套资源
    var active by remember { mutableStateOf<ActiveAssets?>(null) }

    // 启动时落到内置素材的提示；远端同步成功后清空
    var offlineNotice by remember { mutableStateOf<String?>(null) }

    // E04：刚同步下来、还没确认上屏的包。上屏成功后才把它记成生效版本，
    // 否则一个「哈希对但解不开」的包会被记成生效版本，下次冷启动继续恢复它。
    var publishPending by remember { mutableStateOf<RemotePackage?>(null) }

    // E04：当前画面是不是「从本机恢复出来的」那一套。
    // 恢复出来的包如果解码失败，要允许退一次内置素材，不能把人晾在坏画面上。
    var usingRestored by remember { mutableStateOf(false) }

    // E04（P0-1 补完）：切新包之前手里那一套。新包要是画不出来，就回退到它。
    // 「失败保留旧人物、衣物、视角」不是靠祈祷，是靠真留着这一套。
    var previousActive by remember { mutableStateOf<ActiveAssets?>(null) }

    // R01：操作序号。手动换衣、切换目录都会让它 +1；
    // 在途的推荐响应回来后序号对不上就整条丢弃，不许改写当前画面。
    val operationSeq = remember { mutableIntStateOf(0) }

    LaunchedEffect(Unit) {
        // R04：先试离线冷启动恢复。走本地文件，不发任何网络请求，
        // 飞行模式下也能把上次生效的素材还原出来。
        val restored = runCatching { remoteRepository.restoreLast() }.getOrNull()
        if (restored != null) {
            val assets = restored.toActiveAssets()
            remoteAssets = assets
            active = assets
            usingRestored = true
            offlineNotice =
                "已从本机恢复上次生效的素材（${restored.catalog.catalogVersion}）。点「拉取目录」可检查更新"
            return@LaunchedEffect
        }

        // 没有可恢复的远端包，退回内置素材
        runCatching { localRepository.loadActiveAssets() }
            .onSuccess {
                localAssets = it
                if (active == null) {
                    active = it
                    // 启动这一轮不自动连服务端，先如实说明用的是内置素材，
                    // 免得用户看到一张陌生的人脸还不知道为什么
                    offlineNotice = "当前使用手机内置素材。连上服务端后，点「拉取目录」可切换为服务端素材"
                }
            }
            .onFailure { localError = it.message ?: "本地资产包读取失败" }
    }

    fun syncRemote() {
        if (syncing) return
        syncing = true
        syncError = null
        syncStatus = "准备中…"
        scope.launch {
            runCatching {
                remoteRepository.sync(personId = "person-001") { syncStatus = it }
            }.onSuccess { pkg ->
                val assets = pkg.toActiveAssets()
                // E04：覆盖之前先把当前这套收好 —— 新包画不出来时要退回来。
                // 已经有待确认包时不覆盖：那说明手里这套本身就是没确认过的，
                // 拿它当「上一套可用素材」等于把坏的收藏起来。
                if (publishPending == null) previousActive = active
                remoteAssets = assets
                active = assets
                usingRestored = false
                offlineNotice = null
                // E04：先挂起「待确认」，等这一版真的上屏（人物 Ready）再落生效指针
                publishPending = pkg
                // R01：目录换了，之前发出去的推荐一律作废，避免旧目录的结果落到新画面上
                operationSeq.intValue++
                syncStatus = "已加载远端目录 ${pkg.catalog.catalogVersion}"
            }.onFailure { t ->
                // 失败：保留上一套资源与当前画面
                syncError = readableSyncError(t)
                syncStatus = null
            }
            syncing = false
        }
    }

    /** E04：人物和默认衣物都真的画出来了，才承认它是生效版本。重复调用无副作用。 */
    fun confirmVisible() {
        val pkg = publishPending ?: return
        publishPending = null
        // 这版已经确认，没有待回的旧版了
        previousActive = null
        scope.launch { runCatching { remoteRepository.publishActive(pkg) } }
    }

    /**
     * E04：这一套画不出来时，回到上一套能用的。
     *
     * 两种来源分开处理：
     * - 刚从本机恢复出来的包坏了 → 退手机内置素材（复核意见第 2 条）
     * - 刚同步下来、还没确认生效的包坏了 → 退同步之前那一套（P0-1）
     *
     * 两种情况都不写生效指针，所以下次冷启动仍然恢复上一版。
     * 已经确认生效的包不因一次渲染失败就丢弃 —— 那属于运行期问题，不是包的问题。
     */
    fun rollbackToSafe(reason: String) {
        if (publishPending == null && !usingRestored) return
        val previous = previousActive
        publishPending = null
        usingRestored = false
        previousActive = null
        scope.launch {
            val fallback = previous ?: runCatching { localRepository.loadActiveAssets() }.getOrNull()
            if (fallback == null) {
                localError = "$reason，而且没有可用的备用素材"
                return@launch
            }
            if (previous != null) {
                remoteAssets = previous
            } else {
                remoteAssets = null
                localAssets = fallback
            }
            active = fallback
            offlineNotice = "这套素材打不开（$reason），已回到上一套可用素材。点「拉取目录」可重新下载"
        }
    }

    val current = active

    Box(modifier = Modifier.fillMaxSize()) {
        when {
            current == null && localError != null && !syncing ->
                ManifestError(
                    message = localError ?: "",
                    onRetry = { syncRemote() },
                    onBack = onBack
                )

            current == null ->
                CenteredStatus(text = syncStatus ?: "正在读取资产…")

            else -> TryOnContent(
                assets = current,
                operationSeq = operationSeq,
                onBack = onBack,
                onSyncRemote = { syncRemote() },
                syncing = syncing,
                syncStatus = syncStatus,
                syncError = syncError,
                offlineNotice = offlineNotice,
                onSceneReady = { confirmVisible() },
                onSceneBroken = { rollbackToSafe(it) },
                onRecommend = { requestId, scene, preference, onResult ->
                    scope.launch {
                        // 网络请求必须离开主线程，否则 NetworkOnMainThreadException
                        val outcome = withContext(Dispatchers.IO) {
                            runCatching {
                                api.recommend(
                                    RecommendationRequest(
                                        requestId = requestId,
                                        personId = current.personId,
                                        catalogVersion = current.catalogVersion,
                                        scene = scene,
                                        preference = preference
                                    )
                                )
                            }
                        }
                        onResult(outcome)
                    }
                }
            )
        }
    }
}

@Composable
private fun TryOnContent(
    assets: ActiveAssets,
    operationSeq: MutableIntState,
    onBack: () -> Unit,
    onSyncRemote: () -> Unit,
    syncing: Boolean,
    syncStatus: String?,
    syncError: String?,
    offlineNotice: String?,
    /** E04：人物和默认衣物都真的画上屏了 —— 只有这时候才承认这一版资源生效。 */
    onSceneReady: () -> Unit,
    /** E04：这一套画不出来（人物或首件衣物）—— 由上层决定回到哪一套。 */
    onSceneBroken: (reason: String) -> Unit,
    onRecommend: (
        requestId: String,
        scene: String,
        preference: String,
        onResult: (Result<com.ootd.ai.data.RecommendationResult>) -> Unit
    ) -> Unit
) {
    val engine = rememberEngine()
    val modelLoader = rememberModelLoader(engine)

    val garments = assets.garments

    var appliedGarmentId by remember(assets) { mutableStateOf(garments.firstOrNull()?.assetId) }
    var requestedGarmentId by remember(assets) { mutableStateOf(appliedGarmentId) }
    var garmentError by remember { mutableStateOf<String?>(null) }
    var rollbackGuard by remember { mutableStateOf(false) }
    var sceneRetry by remember { mutableIntStateOf(0) }

    // 推荐状态
    var sceneText by remember { mutableStateOf("上班") }
    var preferenceText by remember { mutableStateOf("优先浅色") }
    var recommending by remember { mutableStateOf(false) }
    var recommendation by remember { mutableStateOf<com.ootd.ai.data.RecommendationResult?>(null) }
    var recommendationError by remember { mutableStateOf<String?>(null) }

    val requested = garments.firstOrNull { it.assetId == requestedGarmentId }

    key(assets, sceneRetry) {
        val personState = rememberModelState(modelLoader, assets.personPath)
        val garmentState = rememberModelState(modelLoader, requested?.path)
        // R02：记住已生效衣物的模型实例。切衣加载期间继续显示它，避免画面空档；
        // 候选解码失败时也靠它保住当前衣物，人物与镜头不受影响。
        var appliedGarmentState by remember { mutableStateOf<ModelLoadState?>(null) }

        // E04（P0-1 补完）：判据是「真的画出来了」，而且人物和默认衣物都要算。
        // 只看人物的话，「人好、衣服坏」的包照样会被记成生效版本。
        var garmentVerified by remember { mutableStateOf(false) }

        LaunchedEffect(personState) {
            when (personState) {
                is ModelLoadState.Failed -> onSceneBroken("人物模型解不开")
                else -> Unit
            }
        }

        // 两条都成立才放行：人物 Ready + 首件衣物 Ready
        LaunchedEffect(personState, garmentVerified) {
            val personReady = personState is ModelLoadState.Ready
            if (PublishGate.shouldPublish(personReady = personReady, garmentReady = garmentVerified)) {
                onSceneReady()
            }
        }

        LaunchedEffect(garmentState, requestedGarmentId) {
            if (rollbackGuard) return@LaunchedEffect
            when (val state = garmentState) {
                is ModelLoadState.Ready -> {
                    garmentError = null
                    appliedGarmentId = requestedGarmentId
                    appliedGarmentState = state
                    // 这套资源的衣物真的被画出来了
                    garmentVerified = true
                }

                is ModelLoadState.Failed -> {
                    if (requestedGarmentId != appliedGarmentId) {
                        garmentError = "这件没加载出来（已保留当前衣物）：" +
                            (state.error.message ?: "无法识别的 glTF 错误")
                        rollbackGuard = true
                        requestedGarmentId = appliedGarmentId
                    } else if (appliedGarmentState == null) {
                        // 这套资源一件衣服都画不出来 —— 整包不能用，别让人对着空身子
                        onSceneBroken("衣物模型解不开")
                    } else {
                        garmentError = "衣物加载失败：" +
                            (state.error.message ?: "无法识别的 glTF 错误")
                    }
                }

                else -> Unit
            }
        }

        LaunchedEffect(requestedGarmentId) {
            if (rollbackGuard) rollbackGuard = false
        }

        Box(modifier = Modifier.fillMaxSize()) {
            SceneView(
                modifier = Modifier.fillMaxSize(),
                engine = engine,
                modelLoader = modelLoader
            ) {
                (personState as? ModelLoadState.Ready)?.let { ModelNode(modelInstance = it.instance) }
                // R02：新衣 Ready 才换上去；还在加载或已失败，就继续显示上一件生效的衣物
                val shownGarment = (garmentState as? ModelLoadState.Ready)
                    ?: (appliedGarmentState as? ModelLoadState.Ready)
                shownGarment?.let { ModelNode(modelInstance = it.instance) }
            }

            if (personState is ModelLoadState.Loading || personState == null) {
                CenteredCard(text = "正在加载人物…")
            }

            (personState as? ModelLoadState.Failed)?.let { failed ->
                FailedCard(
                    title = "人物加载失败",
                    detail = failed.error.message ?: "无法读取 person 的 glb",
                    onRetry = { sceneRetry++ },
                    onBack = onBack
                )
            }

            OutlinedButton(
                onClick = onBack,
                shape = RoundedCornerShape(50),
                border = BorderStroke(1.dp, Color.White.copy(alpha = 0.55f)),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = Color.White),
                contentPadding = PaddingValues(horizontal = 18.dp, vertical = 8.dp),
                modifier = Modifier.align(Alignment.TopStart).padding(16.dp)
            ) { Text("返回", fontSize = 13.sp) }

            // 顶部：当前资源来源（避开状态栏）
            Text(
                text = "${assets.sourceLabel} · ${assets.catalogVersion} · ${assets.poseId}",
                color = Color.White,
                style = MaterialTheme.typography.labelMedium,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(top = 44.dp, end = 12.dp)
                    .background(Color.Black.copy(alpha = 0.45f))
                    .padding(horizontal = 8.dp, vertical = 4.dp)
            )

            Column(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(topStart = 22.dp, topEnd = 22.dp))
                    .background(Color.Black.copy(alpha = 0.55f))
                    .padding(horizontal = 14.dp, vertical = 16.dp)
            ) {
                syncError?.let {
                    Text("远端同步失败：$it（已保留当前资源）",
                        color = Color(0xFFFF8A80), style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
                }
                // 没有报错但用的是内置素材时，也要说清楚，别让用户以为是服务端的人
                if (syncError == null) {
                    offlineNotice?.let {
                        Text(it, color = Color(0xFFFFD54F), style = MaterialTheme.typography.bodySmall)
                        Spacer(Modifier.height(6.dp))
                    }
                }
                syncStatus?.let {
                    Text(it, color = Color.White.copy(alpha = 0.85f),
                        style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
                }

                garmentError?.let {
                    Text(it, color = Color(0xFFFFD54F), style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
                }

                // 推荐：场合 + 偏好
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    OutlinedTextField(
                        value = sceneText,
                        onValueChange = { sceneText = it },
                        label = { Text("场合") },
                        singleLine = true,
                        modifier = Modifier.weight(1f)
                    )
                    OutlinedTextField(
                        value = preferenceText,
                        onValueChange = { preferenceText = it },
                        label = { Text("偏好") },
                        singleLine = true,
                        modifier = Modifier.weight(1f)
                    )
                    Button(
                        onClick = {
                            // R01：发请求前把序号与目录版本钉下来，回来时对不上就整条丢弃
                            val seqAtStart = operationSeq.intValue
                            val versionAtStart = assets.catalogVersion
                            val requestId = "req-" + java.util.UUID.randomUUID().toString().take(8)
                            recommending = true
                            recommendationError = null
                            onRecommend(requestId, sceneText, preferenceText) { result ->
                                recommending = false
                                // 期间手动换过衣、或切过目录 —— 这条结果已经失效
                                if (seqAtStart != operationSeq.intValue) {
                                    recommendationError =
                                        "这条推荐回来晚了（期间换过衣或目录），已忽略"
                                    return@onRecommend
                                }
                                result.onSuccess { r ->
                                    // 服务端回显了 request_id 就核对一遍，没回显则靠上面的序号兜底
                                    if (r.requestId.isNotBlank() && r.requestId != requestId) {
                                        recommendationError = "收到一条不匹配的推荐结果，已忽略"
                                        return@onSuccess
                                    }
                                    if (r.catalogVersion.isNotBlank() &&
                                        r.catalogVersion != versionAtStart
                                    ) {
                                        recommendationError =
                                            "推荐基于目录 ${r.catalogVersion}，当前已是 $versionAtStart，已忽略"
                                        return@onSuccess
                                    }
                                    recommendation = r
                                    if (r.isNoMatch) {
                                        recommendationError = r.reason.ifBlank { "没有合适的衣物" }
                                    } else {
                                        val target = garments.firstOrNull { it.assetId == r.garmentId }
                                        if (target == null) {
                                            recommendationError =
                                                "返回的编号 ${r.garmentId} 不在当前目录，保持当前衣物"
                                        } else {
                                            garmentError = null
                                            rollbackGuard = false
                                            requestedGarmentId = target.assetId
                                        }
                                    }
                                }.onFailure { t ->
                                    recommendationError = t.message ?: "推荐请求失败"
                                }
                            }
                        },
                        enabled = !recommending,
                        shape = RoundedCornerShape(12.dp),
                        contentPadding = PaddingValues(horizontal = 20.dp),
                        modifier = Modifier.height(56.dp)
                    ) {
                        if (recommending) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(18.dp),
                                strokeWidth = 2.dp,
                                color = Color.White
                            )
                        } else {
                            Text("推荐", fontSize = 14.sp, fontWeight = FontWeight.Medium)
                        }
                    }
                }

                recommendation?.let { r ->
                    Spacer(Modifier.height(6.dp))
                    Text(
                        text = (if (r.isMock) "【模拟推荐】" else "【AI 推荐】") + r.reason,
                        color = if (r.isMock) Color(0xFFFFD54F) else Color(0xFF80D8FF),
                        style = MaterialTheme.typography.bodySmall
                    )
                }
                recommendationError?.let {
                    Spacer(Modifier.height(6.dp))
                    Text(it, color = Color(0xFFFF8A80), style = MaterialTheme.typography.bodySmall)
                }

                Spacer(Modifier.height(8.dp))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    garments.forEach { g ->
                        val isCurrent = g.assetId == appliedGarmentId
                        val buttonModifier = Modifier
                            .weight(1f)
                            .height(46.dp)
                        if (isCurrent) {
                            Button(
                                onClick = { },
                                shape = RoundedCornerShape(12.dp),
                                contentPadding = PaddingValues(horizontal = 6.dp),
                                modifier = buttonModifier
                            ) {
                                Text(g.label, maxLines = 1, fontSize = 12.sp,
                                    fontWeight = FontWeight.SemiBold)
                            }
                        } else {
                            OutlinedButton(
                                onClick = {
                                    // R01：手动换衣立刻作废在途的推荐，避免响应回来又把你选的换回去
                                    operationSeq.intValue++
                                    garmentError = null
                                    rollbackGuard = false
                                    requestedGarmentId = g.assetId
                                },
                                shape = RoundedCornerShape(12.dp),
                                border = BorderStroke(1.dp, Color.White.copy(alpha = 0.45f)),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    contentColor = Color.White.copy(alpha = 0.92f)),
                                contentPadding = PaddingValues(horizontal = 6.dp),
                                modifier = buttonModifier
                            ) { Text(g.label, maxLines = 1, fontSize = 12.sp) }
                        }
                    }
                    OutlinedButton(
                        onClick = if (syncing) ({}) else onSyncRemote,
                        shape = RoundedCornerShape(12.dp),
                        border = BorderStroke(1.dp, Color.White.copy(alpha = 0.45f)),
                        colors = ButtonDefaults.outlinedButtonColors(
                            contentColor = Color.White.copy(alpha = 0.92f)),
                        contentPadding = PaddingValues(horizontal = 6.dp),
                        modifier = Modifier
                            .weight(1.1f)
                            .height(46.dp)
                    ) { Text(if (syncing) "同步中…" else "拉取目录", maxLines = 1, fontSize = 12.sp) }
                }
            }
        }
    }
}

@Composable
private fun CenteredStatus(text: String) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(modifier = Modifier.width(20.dp).height(20.dp))
            Spacer(Modifier.width(12.dp))
            Text(text)
        }
    }
}

@Composable
private fun CenteredCard(text: String) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .background(Color.Black.copy(alpha = 0.5f))
                .padding(horizontal = 16.dp, vertical = 12.dp)
        ) {
            CircularProgressIndicator(modifier = Modifier.width(18.dp).height(18.dp))
            Spacer(Modifier.width(10.dp))
            Text(text, color = Color.White)
        }
    }
}

@Composable
private fun FailedCard(
    title: String,
    detail: String,
    onRetry: () -> Unit,
    onBack: () -> Unit
) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier
                .fillMaxWidth(0.85f)
                .background(Color.Black.copy(alpha = 0.75f))
                .padding(20.dp)
        ) {
            Text(title, color = Color.White, style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            Text(
                text = detail,
                color = Color.White.copy(alpha = 0.85f),
                style = MaterialTheme.typography.bodySmall,
                textAlign = TextAlign.Center
            )
            Spacer(Modifier.height(16.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(
                    onClick = onRetry,
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.height(46.dp)
                ) { Text("重试", fontSize = 14.sp) }
                OutlinedButton(
                    onClick = onBack,
                    shape = RoundedCornerShape(12.dp),
                    border = BorderStroke(1.dp, Color.White.copy(alpha = 0.45f)),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Color.White),
                    modifier = Modifier.height(46.dp)
                ) { Text("返回", fontSize = 14.sp) }
            }
        }
    }
}

@Composable
private fun ManifestError(
    message: String,
    onRetry: () -> Unit,
    onBack: () -> Unit
) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier.fillMaxWidth(0.9f).padding(24.dp)
        ) {
            Text("没有找到三维资产", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            Text(
                text = message,
                style = MaterialTheme.typography.bodySmall,
                textAlign = TextAlign.Center
            )
            Spacer(Modifier.height(16.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(
                    onClick = onRetry,
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.height(46.dp)
                ) { Text("拉取服务端目录", fontSize = 14.sp) }
                OutlinedButton(
                    onClick = onBack,
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.height(46.dp)
                ) { Text("返回", fontSize = 14.sp) }
            }
        }
    }
}