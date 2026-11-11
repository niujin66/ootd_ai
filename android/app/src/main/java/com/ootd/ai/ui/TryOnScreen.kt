package com.ootd.ai.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.ootd.ai.BuildConfig
import com.ootd.ai.data.ActiveAssets
import com.ootd.ai.data.CatalogApi
import com.ootd.ai.data.GarmentOption
import com.ootd.ai.data.LocalAssetRepository
import com.ootd.ai.data.RecommendationRequest
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

    LaunchedEffect(Unit) {
        runCatching { localRepository.loadActiveAssets() }
            .onSuccess {
                localAssets = it
                if (active == null) active = it
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
                remoteAssets = assets
                active = assets
                syncStatus = "已加载远端目录 ${pkg.catalog.catalogVersion}"
            }.onFailure { t ->
                // 失败：保留上一套资源与当前画面
                syncError = t.message ?: t::class.simpleName ?: "未知错误"
                syncStatus = null
            }
            syncing = false
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
                onBack = onBack,
                onSyncRemote = { syncRemote() },
                syncing = syncing,
                syncStatus = syncStatus,
                syncError = syncError,
                onRecommend = { scene, preference, onResult ->
                    scope.launch {
                        // 网络请求必须离开主线程，否则 NetworkOnMainThreadException
                        val outcome = withContext(Dispatchers.IO) {
                            runCatching {
                                api.recommend(
                                    RecommendationRequest(
                                        requestId = "req-" + System.currentTimeMillis(),
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
    onBack: () -> Unit,
    onSyncRemote: () -> Unit,
    syncing: Boolean,
    syncStatus: String?,
    syncError: String?,
    onRecommend: (
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

        LaunchedEffect(garmentState, requestedGarmentId) {
            if (rollbackGuard) return@LaunchedEffect
            when (val state = garmentState) {
                is ModelLoadState.Ready -> {
                    garmentError = null
                    appliedGarmentId = requestedGarmentId
                }

                is ModelLoadState.Failed -> {
                    if (requestedGarmentId != appliedGarmentId) {
                        garmentError = "这件没加载出来（已保留当前衣物）：" +
                            (state.error.message ?: "无法识别的 glTF 错误")
                        rollbackGuard = true
                        requestedGarmentId = appliedGarmentId
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
                (garmentState as? ModelLoadState.Ready)?.let { ModelNode(modelInstance = it.instance) }
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
                modifier = Modifier.align(Alignment.TopStart).padding(16.dp)
            ) { Text("返回") }

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
                    .background(Color.Black.copy(alpha = 0.45f))
                    .padding(12.dp)
            ) {
                syncError?.let {
                    Text("远端同步失败：$it（已保留当前资源）",
                        color = Color(0xFFFF8A80), style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(6.dp))
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
                            recommending = true
                            recommendationError = null
                            onRecommend(sceneText, preferenceText) { result ->
                                recommending = false
                                result.onSuccess { r ->
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
                        enabled = !recommending
                    ) { Text(if (recommending) "…" else "推荐") }
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
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    garments.forEach { g ->
                        val isCurrent = g.assetId == appliedGarmentId
                        val buttonModifier = Modifier.weight(1f)
                        if (isCurrent) {
                            Button(onClick = { }, modifier = buttonModifier) {
                                Text(g.label, maxLines = 1)
                            }
                        } else {
                            OutlinedButton(
                                onClick = {
                                    garmentError = null
                                    rollbackGuard = false
                                    requestedGarmentId = g.assetId
                                },
                                modifier = buttonModifier
                            ) { Text(g.label, maxLines = 1) }
                        }
                    }
                    OutlinedButton(
                        onClick = if (syncing) ({}) else onSyncRemote,
                        modifier = Modifier.weight(1.1f)
                    ) { Text(if (syncing) "同步中…" else "拉取目录", maxLines = 1) }
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
                Button(onClick = onRetry) { Text("重试") }
                OutlinedButton(onClick = onBack) { Text("返回") }
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
                Button(onClick = onRetry) { Text("拉取服务端目录") }
                OutlinedButton(onClick = onBack) { Text("返回") }
            }
        }
    }
}