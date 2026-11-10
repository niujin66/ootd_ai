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
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import io.github.sceneview.ModelLoadState
import io.github.sceneview.SceneView
import io.github.sceneview.node.ModelNode
import io.github.sceneview.rememberEngine
import io.github.sceneview.rememberModelLoadState
import io.github.sceneview.rememberModelLoader
import com.ootd.ai.data.AssetManifest
import com.ootd.ai.data.LocalAssetRepository

/**
 * 三维试衣页（B02）。
 *
 * 从 assets/local-demo 读 manifest，加载同一个人物和衣物，手指拖动可绕人物旋转。
 * 三态：加载中 / 失败（可重试）/ 就绪。换衣失败时回滚到上一件，不让人物消失。
 */
@Composable
fun TryOnScreen(onBack: () -> Unit) {

    val context = LocalContext.current
    val repository = remember { LocalAssetRepository(context) }

    // 清单读取结果。null = 还在读。
    var manifestResult by remember { mutableStateOf<Result<AssetManifest>?>(null) }
    var manifestRetry by remember { mutableIntStateOf(0) }

    LaunchedEffect(manifestRetry) {
        manifestResult = null
        manifestResult = runCatching { repository.loadManifest() }
    }

    Box(modifier = Modifier.fillMaxSize()) {
        when (val result = manifestResult) {
            null -> CenteredStatus(text = "正在读取资产清单…")

            else -> when (val manifest = result.getOrNull()) {
                null -> ManifestError(
                    message = result.exceptionOrNull()?.message ?: "资产清单读取失败",
                    onRetry = { manifestRetry++ },
                    onBack = onBack
                )

                else -> TryOnContent(
                    manifest = manifest,
                    repository = repository,
                    onBack = onBack
                )
            }
        }
    }
}

@Composable
private fun TryOnContent(
    manifest: AssetManifest,
    repository: LocalAssetRepository,
    onBack: () -> Unit
) {
    val engine = rememberEngine()
    val modelLoader = rememberModelLoader(engine)

    val person = manifest.person
    val garments = manifest.garments

    // 正在显示的衣服。换衣失败时回滚到这个值。
    var appliedGarmentId by remember { mutableStateOf(garments.firstOrNull()?.assetId) }
    // 用户点了哪件（可能加载失败）。
    var requestedGarmentId by remember { mutableStateOf(appliedGarmentId) }
    // 换衣失败提示 + 回滚保护，避免失败后反复重试同一条路径。
    var garmentError by remember { mutableStateOf<String?>(null) }
    var rollbackGuard by remember { mutableStateOf(false) }
    var sceneRetry by remember { mutableIntStateOf(0) }

    val personPath = person?.let { repository.assetPath(it) }
    val requestedGarment = manifest.garmentById(requestedGarmentId.orEmpty())
    val requestedGarmentPath = requestedGarment?.let { repository.assetPath(it) }

    if (personPath == null) {
        ManifestError(
            message = "清单里没有人物资产（asset_type=person）",
            onRetry = { },
            onBack = onBack
        )
        return
    }

    // 换衣服：加载成功才提交，失败回滚到上一件。
    key(sceneRetry) {
        val personState = rememberModelLoadState(modelLoader, personPath)
        val garmentState = requestedGarmentPath?.let {
            rememberModelLoadState(modelLoader, it)
        }

        LaunchedEffect(garmentState, requestedGarmentId) {
            if (rollbackGuard) return@LaunchedEffect
            when (val state = garmentState) {
                is ModelLoadState.Ready -> {
                    garmentError = null
                    appliedGarmentId = requestedGarmentId
                }

                is ModelLoadState.Failed -> {
                    if (requestedGarmentId != appliedGarmentId) {
                        garmentError = "这件没加载出来：" +
                            (state.error.message ?: "无法识别的 glTF 错误") +
                            "（已保留当前衣物）"
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

        LaunchedEffect(requestedGarmentPath) {
            if (rollbackGuard) rollbackGuard = false
        }

        Box(modifier = Modifier.fillMaxSize()) {

            SceneView(
                modifier = Modifier.fillMaxSize(),
                engine = engine,
                modelLoader = modelLoader
            ) {
                // 人物和衣物是同一个父节点下的两个独立模型，共用 pose-001 和同一原点，
                // 因此不需要各自缩放：统一按米制原尺寸显示。
                (personState as? ModelLoadState.Ready)?.let { ready ->
                    ModelNode(modelInstance = ready.instance)
                }
                (garmentState as? ModelLoadState.Ready)?.let { ready ->
                    ModelNode(modelInstance = ready.instance)
                }
            }

            // 加载中
            if (personState is ModelLoadState.Loading) {
                CenteredCard(text = "正在加载人物…")
            }

            // 人物失败：整个场景不可用，只能重试
            (personState as? ModelLoadState.Failed)?.let { failed ->
                FailedCard(
                    title = "人物加载失败",
                    detail = failed.error.message ?: "无法读取 person 的 glb",
                    onRetry = { sceneRetry++ },
                    onBack = onBack
                )
            }

            // 顶部返回
            OutlinedButton(
                onClick = onBack,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .padding(16.dp)
            ) {
                Text("返回")
            }

            // 底部：衣物切换 + 状态
            Column(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .background(Color.Black.copy(alpha = 0.35f))
                    .padding(16.dp)
            ) {
                garmentError?.let { message ->
                    Text(
                        text = message,
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(0xFFFFD54F)
                    )
                    Spacer(Modifier.height(8.dp))
                }

                when {
                    garmentState is ModelLoadState.Loading ->
                        Text("正在切换衣物…", color = Color.White)

                    garmentState is ModelLoadState.Failed && garmentError == null ->
                        Text("衣物加载失败", color = Color(0xFFFFD54F))

                    else -> Unit
                }

                Spacer(Modifier.height(8.dp))

                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    garments.forEach { garment ->
                        val isCurrent = garment.assetId == appliedGarmentId
                        if (isCurrent) {
                            Button(onClick = { }) {
                                Text(garment.displayName ?: garment.assetId)
                            }
                        } else {
                            OutlinedButton(
                                onClick = {
                                    garmentError = null
                                    rollbackGuard = false
                                    requestedGarmentId = garment.assetId
                                }
                            ) {
                                Text(garment.displayName ?: garment.assetId)
                            }
                        }
                    }
                }

                Spacer(Modifier.height(8.dp))

                Text(
                    text = "拖动手势可绕人物旋转 · 目录 ${manifest.catalogVersion} · 姿势 ${manifest.poseId}",
                    style = MaterialTheme.typography.labelSmall,
                    color = Color.White.copy(alpha = 0.8f)
                )
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
            modifier = Modifier
                .fillMaxWidth(0.9f)
                .padding(24.dp)
        ) {
            Text(
                "没有找到三维资产",
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = message,
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