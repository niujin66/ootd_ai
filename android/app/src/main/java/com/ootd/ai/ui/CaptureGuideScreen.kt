package com.ootd.ai.ui

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.MediaStore
import android.speech.tts.TextToSpeech
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.MediaStoreOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.ootd.ai.BuildConfig
import com.ootd.ai.capture.CameraXFrameAnalyzer
import com.ootd.ai.capture.CaptureGuideApi
import com.ootd.ai.capture.CaptureGuideRequest
import com.ootd.ai.capture.CaptureGuideResponse
import com.ootd.ai.capture.CapturePrepConfig
import com.ootd.ai.capture.CaptureStage
import com.ootd.ai.capture.CaptureTargetType
import com.ootd.ai.capture.CloseupCheckId
import com.ootd.ai.capture.LiveFrameQualityAnalyzer
import com.ootd.ai.capture.LiveQualitySnapshot
import com.ootd.ai.capture.OfflineCaptureDiagnoser
import com.ootd.ai.capture.OrientationSectorTracker
import com.ootd.ai.capture.OrientationSnapshot
import com.ootd.ai.capture.RotationSensorBridge
import com.ootd.ai.capture.TrafficLight
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import java.util.concurrent.Executors
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * 两段式实时采集引导页面：
 * 1. 开拍前准备：声明目标类型（head / garment）、可选身高/性别与尺度参考标记；
 * 2. 阶段 1 远景环绕：12 扇区打卡环 + 水平仪条 + 占屏比/边距/模糊/速度实时红绿灯；
 * 3. 阶段 2 一镜慢推近：不中断录像，引导靠近补齐面部眼距 ≥20px 或衣物细节特写；
 * 4. 结束质检：保存录像并请求 POST /v1/capture/guide，异常时本地离线降级 + 语音播报 + 一键缺口重拍。
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CaptureGuideScreen(
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val coroutineScope = rememberCoroutineScope()

    var hasCameraPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { granted ->
        hasCameraPermission = granted
    }

    LaunchedEffect(Unit) {
        if (!hasCameraPermission) {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    var prepConfig by remember { mutableStateOf(CapturePrepConfig()) }
    var stage by remember { mutableStateOf(CaptureStage.PREPARE) }
    var voiceEnabled by remember { mutableStateOf(true) }
    var targetGapSectors by remember { mutableStateOf<Set<Int>>(emptySet()) }

    val tracker = remember { OrientationSectorTracker() }
    var analyzer by remember(prepConfig.targetType) {
        mutableStateOf(LiveFrameQualityAnalyzer(prepConfig.targetType))
    }

    var orientationSnapshot by remember { mutableStateOf(tracker.currentSnapshot()) }
    var qualitySnapshot by remember { mutableStateOf<LiveQualitySnapshot?>(null) }

    var savedVideoUri by remember { mutableStateOf<String?>(null) }
    var recordError by remember { mutableStateOf<String?>(null) }
    var guideResult by remember { mutableStateOf<CaptureGuideResponse?>(null) }
    var analyzeStatusText by remember { mutableStateOf("正在汇总轨迹与清晰度指标…") }

    // CameraX 控制器与录像状态
    var videoCapture by remember { mutableStateOf<VideoCapture<Recorder>?>(null) }
    var activeRecording by remember { mutableStateOf<Recording?>(null) }
    var cameraBindError by remember { mutableStateOf<String?>(null) }

    val analysisExecutor = remember { Executors.newSingleThreadExecutor() }

    // TextToSpeech 用于实时语音提醒及结果卡片播报
    val ttsHolder = remember { mutableStateOf<TextToSpeech?>(null) }
    var ttsReady by remember { mutableStateOf(false) }

    DisposableEffect(context) {
        lateinit var tts: TextToSpeech
        tts = TextToSpeech(context.applicationContext) { status ->
            if (status == TextToSpeech.SUCCESS) {
                val res = tts.setLanguage(Locale.SIMPLIFIED_CHINESE)
                ttsReady = res != TextToSpeech.LANG_MISSING_DATA &&
                    res != TextToSpeech.LANG_NOT_SUPPORTED
            }
        }
        ttsHolder.value = tts
        onDispose {
            ttsReady = false
            ttsHolder.value = null
            tts.stop()
            tts.shutdown()
            analysisExecutor.shutdown()
        }
    }

    fun speakHint(text: String, flush: Boolean = false) {
        if (!voiceEnabled || !ttsReady || text.isBlank()) return
        val queueMode = if (flush) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
        ttsHolder.value?.speak(text, queueMode, null, "ootd_hint_${System.currentTimeMillis()}")
    }

    // 传感器仅在拍摄阶段启用，离开界面时安全注销
    val isCapturing = stage == CaptureStage.STAGE_1_OVERVIEW || stage == CaptureStage.STAGE_2_CLOSEUP
    DisposableEffect(isCapturing) {
        if (!isCapturing) {
            onDispose { }
        } else {
            val bridge = RotationSensorBridge(context, tracker) { snap ->
                orientationSnapshot = snap
            }
            bridge.start()
            onDispose {
                bridge.stop()
            }
        }
    }

    // 播报关键状态切换与纠偏提示（节流由 LiveFrameQualityAnalyzer.voiceHint 控制）
    LaunchedEffect(qualitySnapshot?.voiceHint) {
        val hint = qualitySnapshot?.voiceHint ?: return@LaunchedEffect
        if (isCapturing) {
            speakHint(hint, flush = true)
        }
    }

    fun startRecordingSession(gapIndices: Set<Int> = emptySet()) {
        tracker.reset()
        analyzer = LiveFrameQualityAnalyzer(prepConfig.targetType)
        orientationSnapshot = tracker.currentSnapshot()
        qualitySnapshot = null
        savedVideoUri = null
        recordError = null
        guideResult = null
        targetGapSectors = gapIndices
        stage = CaptureStage.STAGE_1_OVERVIEW

        val vc = videoCapture ?: return
        val name = "OOTD_${prepConfig.targetType.apiValue}_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}"
        val contentValues = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, name)
            put(MediaStore.MediaColumns.MIME_TYPE, "video/mp4")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                put(MediaStore.Video.Media.RELATIVE_PATH, "Movies/OOTD-Capture")
            }
        }
        val outputOptions = MediaStoreOutputOptions.Builder(
            context.contentResolver,
            MediaStore.Video.Media.EXTERNAL_CONTENT_URI
        ).setContentValues(contentValues).build()

        runCatching {
            activeRecording?.stop()
            activeRecording = vc.output
                .prepareRecording(context, outputOptions)
                .start(ContextCompat.getMainExecutor(context)) { event ->
                    when (event) {
                        is VideoRecordEvent.Finalize -> {
                            if (event.hasError()) {
                                recordError = "录像保存警告（error=${event.error}），分析指标仍已保留"
                            } else {
                                savedVideoUri = event.outputResults.outputUri.toString()
                            }
                        }
                    }
                }
        }.onFailure { err ->
            recordError = "录像启动异常（${err.message}），已继续使用实时质量指导"
        }
        speakHint("开始第一阶段，请围绕目标缓慢环绕一圈", flush = true)
    }

    fun finishAndInspect() {
        runCatching { activeRecording?.stop() }
        activeRecording = null
        stage = CaptureStage.ANALYZING
        analyzeStatusText = "正在汇总两阶段指标并请求 AI 质检指引…"

        val req = analyzer.buildGuideRequest(
            requestId = "android-${UUID.randomUUID().toString().take(8)}",
            prep = prepConfig,
            tracker = tracker
        )

        coroutineScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching {
                    val api = CaptureGuideApi(
                        baseUrl = BuildConfig.OOTD_API_BASE_URL,
                        authToken = BuildConfig.OOTD_API_TOKEN
                    )
                    api.requestGuide(req)
                }.getOrElse { err ->
                    OfflineCaptureDiagnoser.buildFallback(
                        request = req,
                        networkReason = err.message ?: err.javaClass.simpleName
                    )
                }
            }
            guideResult = response
            stage = CaptureStage.RESULT
            speakHint(response.guide.summary, flush = true)
        }
    }

    Surface(
        modifier = modifier.fillMaxSize(),
        color = MaterialTheme.colorScheme.background
    ) {
        if (!hasCameraPermission) {
            CameraPermissionDeniedContent(
                onRequestPermission = { permissionLauncher.launch(Manifest.permission.CAMERA) },
                onBack = onBack
            )
            return@Surface
        }

        when (stage) {
            CaptureStage.PREPARE -> {
                PrepareCaptureContent(
                    config = prepConfig,
                    onConfigChange = { prepConfig = it },
                    onStartCapture = { startRecordingSession() },
                    onBack = onBack
                )
            }

            CaptureStage.STAGE_1_OVERVIEW, CaptureStage.STAGE_2_CLOSEUP -> {
                Box(modifier = Modifier.fillMaxSize()) {
                    CameraPreviewAndAnalyzer(
                        context = context,
                        lifecycleOwner = lifecycleOwner,
                        analysisExecutor = analysisExecutor,
                        analyzer = analyzer,
                        targetTypeProvider = { prepConfig.targetType },
                        orientationProvider = { orientationSnapshot },
                        onVideoCaptureReady = { vc -> videoCapture = vc },
                        onBindError = { msg -> cameraBindError = msg },
                        onQualitySnapshot = { snap -> qualitySnapshot = snap }
                    )

                    // 中心 12 扇区打卡环 + 水平仪条叠加层
                    SectorAndLevelOverlay(
                        orientation = orientationSnapshot,
                        targetGapSectors = targetGapSectors,
                        trafficLight = qualitySnapshot?.trafficLight ?: TrafficLight.GREEN,
                        modifier = Modifier
                            .align(Alignment.Center)
                            .padding(bottom = 120.dp)
                    )

                    // 顶部阶段条与静音开关
                    TopCaptureHud(
                        stage = stage,
                        orientation = orientationSnapshot,
                        voiceEnabled = voiceEnabled,
                        targetGapCount = targetGapSectors.size,
                        onToggleVoice = { voiceEnabled = !voiceEnabled },
                        onBack = {
                            runCatching { activeRecording?.stop() }
                            activeRecording = null
                            stage = CaptureStage.PREPARE
                        },
                        modifier = Modifier
                            .align(Alignment.TopCenter)
                            .fillMaxWidth()
                            .statusBarsPadding()
                            .padding(12.dp)
                    )

                    // 底部红绿灯指引、核心参数与两阶段切换按钮
                    BottomCapturePanel(
                        stage = stage,
                        targetType = prepConfig.targetType,
                        snapshot = qualitySnapshot,
                        cameraBindError = cameraBindError,
                        onAdvanceToStage2 = {
                            analyzer.enterStage2Closeup()
                            stage = CaptureStage.STAGE_2_CLOSEUP
                            speakHint("进入阶段二，请被拍人保持不动，镜头慢慢向前推近", flush = true)
                        },
                        onBackToStage1 = {
                            analyzer.reset(CaptureStage.STAGE_1_OVERVIEW)
                            stage = CaptureStage.STAGE_1_OVERVIEW
                        },
                        onToggleManualCloseup = { checkId ->
                            analyzer.markCloseupCompleted(checkId)
                        },
                        onFinish = { finishAndInspect() },
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .fillMaxWidth()
                            .navigationBarsPadding()
                            .padding(12.dp)
                    )
                }
            }

            CaptureStage.ANALYZING -> {
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(24.dp),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    CircularProgressIndicator()
                    Spacer(modifier = Modifier.height(16.dp))
                    Text(
                        text = analyzeStatusText,
                        style = MaterialTheme.typography.titleMedium
                    )
                }
            }

            CaptureStage.RESULT -> {
                val resp = guideResult
                if (resp != null) {
                    CaptureResultContent(
                        response = resp,
                        savedVideoUri = savedVideoUri,
                        recordError = recordError,
                        onSpeakResult = {
                            val text = buildString {
                                append(resp.guide.summary)
                                resp.guide.steps.forEach { step ->
                                    append("。第${step.priority}步，${step.targetArea}：${step.action}")
                                }
                            }
                            speakHint(text, flush = true)
                        },
                        onRetakeMissingArcs = {
                            val missingIndices = OrientationSectorTracker.parseGapSectorIndices(
                                resp.diagnosis.sectorSummary.missingArcs
                            )
                            startRecordingSession(gapIndices = missingIndices)
                        },
                        onRestartFull = { stage = CaptureStage.PREPARE },
                        onBackHome = onBack
                    )
                }
            }
        }
    }
}

@Composable
private fun CameraPreviewAndAnalyzer(
    context: Context,
    lifecycleOwner: androidx.lifecycle.LifecycleOwner,
    analysisExecutor: java.util.concurrent.ExecutorService,
    analyzer: LiveFrameQualityAnalyzer,
    targetTypeProvider: () -> CaptureTargetType,
    orientationProvider: () -> OrientationSnapshot?,
    onVideoCaptureReady: (VideoCapture<Recorder>) -> Unit,
    onBindError: (String) -> Unit,
    onQualitySnapshot: (LiveQualitySnapshot) -> Unit
) {
    val previewView = remember {
        PreviewView(context).apply {
            scaleType = PreviewView.ScaleType.FILL_CENTER
        }
    }

    val frameAnalyzer = remember(analyzer) {
        CameraXFrameAnalyzer(
            analyzer = analyzer,
            targetTypeProvider = targetTypeProvider,
            orientationProvider = orientationProvider,
            onSnapshot = { snap ->
                ContextCompat.getMainExecutor(context).execute {
                    onQualitySnapshot(snap)
                }
            }
        )
    }

    DisposableEffect(frameAnalyzer) {
        onDispose {
            frameAnalyzer.release()
        }
    }

    DisposableEffect(lifecycleOwner, frameAnalyzer) {
        val future = ProcessCameraProvider.getInstance(context)
        val mainExecutor = ContextCompat.getMainExecutor(context)
        var providerRef: ProcessCameraProvider? = null
        future.addListener({
            runCatching {
                val provider = future.get()
                providerRef = provider
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                val recorder = Recorder.Builder()
                    .setQualitySelector(QualitySelector.from(Quality.HD))
                    .build()
                val videoCapture = VideoCapture.withOutput(recorder)
                val imageAnalysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                    .also { it.setAnalyzer(analysisExecutor, frameAnalyzer) }

                provider.unbindAll()
                provider.bindToLifecycle(
                    lifecycleOwner,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    imageAnalysis,
                    videoCapture
                )
                onVideoCaptureReady(videoCapture)
            }.onFailure { err ->
                onBindError("相机初始化失败：${err.message ?: err.javaClass.simpleName}")
            }
        }, mainExecutor)

        onDispose {
            providerRef?.unbindAll()
        }
    }

    AndroidView(
        factory = { previewView },
        modifier = Modifier.fillMaxSize()
    )
}

@Composable
private fun SectorAndLevelOverlay(
    orientation: OrientationSnapshot,
    targetGapSectors: Set<Int>,
    trafficLight: TrafficLight,
    modifier: Modifier = Modifier
) {
    val ringTint = when (trafficLight) {
        TrafficLight.GREEN -> Color(0xFF2E7D32)
        TrafficLight.YELLOW -> Color(0xFFF9A825)
        TrafficLight.RED -> Color(0xFFC62828)
    }
    Column(
        modifier = modifier,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Box(
            modifier = Modifier.size(220.dp),
            contentAlignment = Alignment.Center
        ) {
            Canvas(modifier = Modifier.fillMaxSize()) {
                val stroke = 14.dp.toPx()
                val inset = stroke / 2f
                val arcSize = Size(size.width - stroke, size.height - stroke)
                val topLeft = Offset(inset, inset)

                for (sector in 0 until OrientationSectorTracker.SECTOR_COUNT) {
                    val startAngle = -90f + sector * 30f + 2f
                    val sweep = 26f
                    val isCovered = orientation.coveredSectors[sector]
                    val isTargetGap = sector in targetGapSectors && !isCovered
                    val color = when {
                        isCovered -> Color(0xFF4CAF50)
                        isTargetGap -> Color(0xFFFFB300)
                        else -> Color.White.copy(alpha = 0.28f)
                    }
                    drawArc(
                        color = color,
                        startAngle = startAngle,
                        sweepAngle = sweep,
                        useCenter = false,
                        topLeft = topLeft,
                        size = arcSize,
                        style = Stroke(width = stroke, cap = StrokeCap.Round)
                    )
                }

                // 当前方位指针
                val rad = ((orientation.relativeAzimuthDeg - 90.0) * PI / 180.0).toFloat()
                val radius = (size.minDimension - stroke) / 2f
                val cx = size.width / 2f
                val cy = size.height / 2f
                val dotCenter = Offset(
                    cx + cos(rad) * radius,
                    cy + sin(rad) * radius
                )
                drawCircle(
                    color = Color.Cyan,
                    radius = 7.dp.toPx(),
                    center = dotCenter
                )
            }

            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    text = "${orientation.coveredCount}/12",
                    color = Color.White,
                    style = MaterialTheme.typography.headlineMedium,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = "${orientation.relativeAzimuthDeg.roundToInt()}°",
                    color = Color.White.copy(alpha = 0.85f),
                    style = MaterialTheme.typography.bodySmall
                )
            }
        }

        Spacer(modifier = Modifier.height(10.dp))

        // 水平仪条（±20° 为绿色安全区间）
        Surface(
            color = Color.Black.copy(alpha = 0.55f),
            shape = RoundedCornerShape(16.dp),
            modifier = Modifier
                .border(1.dp, ringTint.copy(alpha = 0.8f), RoundedCornerShape(16.dp))
                .padding(horizontal = 14.dp, vertical = 6.dp)
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text(
                    text = "水平仪 ${orientation.rollDeg.roundToInt()}°",
                    color = Color.White,
                    style = MaterialTheme.typography.labelMedium
                )
                Canvas(modifier = Modifier.size(width = 110.dp, height = 14.dp)) {
                    val midX = size.width / 2f
                    val midY = size.height / 2f
                    drawLine(
                        color = Color.White.copy(alpha = 0.35f),
                        start = Offset(0f, midY),
                        end = Offset(size.width, midY),
                        strokeWidth = 3.dp.toPx(),
                        cap = StrokeCap.Round
                    )
                    // 安全区标线
                    drawLine(
                        color = Color(0xFF4CAF50).copy(alpha = 0.65f),
                        start = Offset(midX - size.width * 0.22f, midY),
                        end = Offset(midX + size.width * 0.22f, midY),
                        strokeWidth = 5.dp.toPx(),
                        cap = StrokeCap.Round
                    )
                    val normRoll = (orientation.rollDeg / 45.0).coerceIn(-1.0, 1.0).toFloat()
                    val bubbleX = midX + normRoll * (size.width * 0.44f)
                    drawCircle(
                        color = if (orientation.isRollWarning) Color(0xFFFF5252) else Color(0xFF69F0AE),
                        radius = 5.dp.toPx(),
                        center = Offset(bubbleX, midY)
                    )
                }
            }
        }
    }
}

@Composable
private fun TopCaptureHud(
    stage: CaptureStage,
    orientation: OrientationSnapshot,
    voiceEnabled: Boolean,
    targetGapCount: Int,
    onToggleVoice: () -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Surface(
            color = Color.Black.copy(alpha = 0.65f),
            shape = RoundedCornerShape(18.dp),
            modifier = Modifier.clickable(onClick = onBack)
        ) {
            Text(
                text = "返回",
                color = Color.White,
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp)
            )
        }

        Surface(
            color = Color.Black.copy(alpha = 0.65f),
            shape = RoundedCornerShape(18.dp)
        ) {
            val badge = if (targetGapCount > 0) " · 缺口补拍" else ""
            Text(
                text = "${stage.title}$badge · 覆盖 ${(orientation.coverageProgress * 100).roundToInt()}%",
                color = Color.White,
                style = MaterialTheme.typography.labelLarge,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp)
            )
        }

        Surface(
            color = if (voiceEnabled) Color(0xFF1B5E20).copy(alpha = 0.85f) else Color.Black.copy(alpha = 0.65f),
            shape = RoundedCornerShape(18.dp),
            modifier = Modifier.clickable(onClick = onToggleVoice)
        ) {
            Text(
                text = if (voiceEnabled) "语音开" else "已静音",
                color = Color.White,
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)
            )
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun BottomCapturePanel(
    stage: CaptureStage,
    targetType: CaptureTargetType,
    snapshot: LiveQualitySnapshot?,
    cameraBindError: String?,
    onAdvanceToStage2: () -> Unit,
    onBackToStage1: () -> Unit,
    onToggleManualCloseup: (CloseupCheckId) -> Unit,
    onFinish: () -> Unit,
    modifier: Modifier = Modifier
) {
    val light = snapshot?.trafficLight ?: TrafficLight.GREEN
    val lightColor = when (light) {
        TrafficLight.GREEN -> Color(0xFF2E7D32)
        TrafficLight.YELLOW -> Color(0xFFF9A825)
        TrafficLight.RED -> Color(0xFFC62828)
    }

    Surface(
        modifier = modifier,
        color = Color(0xFF101318).copy(alpha = 0.88f),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(max = 320.dp)
                .verticalScroll(rememberScrollState())
                .padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            if (cameraBindError != null) {
                Text(
                    text = cameraBindError,
                    color = Color(0xFFFF8A80),
                    style = MaterialTheme.typography.bodySmall
                )
            }

            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Box(
                    modifier = Modifier
                        .size(14.dp)
                        .background(lightColor, CircleShape)
                )
                Text(
                    text = snapshot?.promptText ?: "请将目标居中，保持稳定距离准备环绕",
                    color = Color.White,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.SemiBold
                )
            }

            if (snapshot != null) {
                val occPct = (snapshot.subjectOccupancy * 100).roundToInt()
                val sharpTxt = snapshot.sharpness.roundToInt()
                val eyeTxt = snapshot.eyeDistancePx?.let { " | 眼距 ${it.roundToInt()}px" }.orEmpty()
                val nccTxt = snapshot.continuityNcc?.let { " | 连续度 ${"%.2f".format(it)}" }.orEmpty()
                Text(
                    text = "占比 $occPct% | 清晰度 $sharpTxt$nccTxt$eyeTxt",
                    color = Color.White.copy(alpha = 0.78f),
                    style = MaterialTheme.typography.bodySmall
                )
            }

            if (stage == CaptureStage.STAGE_2_CLOSEUP) {
                val items = if (targetType == CaptureTargetType.HEAD) {
                    listOf(CloseupCheckId.FACE_FRONT, CloseupCheckId.FACE_LEFT, CloseupCheckId.FACE_RIGHT)
                } else {
                    listOf(CloseupCheckId.GARMENT_COLLAR, CloseupCheckId.GARMENT_CUFF)
                }
                val done = snapshot?.completedCloseups.orEmpty()
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    items.forEach { item ->
                        val checked = item in done
                        FilterChip(
                            selected = checked,
                            onClick = { onToggleManualCloseup(item) },
                            label = {
                                Text(
                                    text = if (checked) "✓ ${item.label}" else item.label,
                                    color = if (checked) Color.White else Color.LightGray
                                )
                            }
                        )
                    }
                }
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                if (stage == CaptureStage.STAGE_1_OVERVIEW) {
                    val stage1Ready = snapshot?.stage1ReadyForCloseup == true
                    Button(
                        onClick = onAdvanceToStage2,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = if (stage1Ready) Color(0xFF2E7D32) else MaterialTheme.colorScheme.primary
                        ),
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = 48.dp)
                    ) {
                        Text(if (stage1Ready) "已达标 · 进入推近" else "进入推近阶段")
                    }
                    OutlinedButton(
                        onClick = onFinish,
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = 48.dp)
                    ) {
                        Text("结束并质检", color = Color.White)
                    }
                } else {
                    OutlinedButton(
                        onClick = onBackToStage1,
                        modifier = Modifier
                            .weight(0.85f)
                            .heightIn(min = 48.dp)
                    ) {
                        Text("补环绕角", color = Color.White)
                    }
                    Button(
                        onClick = onFinish,
                        modifier = Modifier
                            .weight(1.15f)
                            .heightIn(min = 48.dp)
                    ) {
                        Text("完成录制并 AI 质检")
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PrepareCaptureContent(
    config: CapturePrepConfig,
    onConfigChange: (CapturePrepConfig) -> Unit,
    onStartCapture: () -> Unit,
    onBack: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = "3D 实时采集引导",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            OutlinedButton(onClick = onBack) {
                Text("返回")
            }
        }

        Text(
            text = "采用「先远景定骨架 → 一镜慢推近补细节」两段式流程，避免只远拍眼部模糊或只近拍丢失空间方位。",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text("1. 选择采集目标", style = MaterialTheme.typography.titleMedium)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    CaptureTargetType.entries.forEach { type ->
                        FilterChip(
                            selected = config.targetType == type,
                            onClick = {
                                val defaultLabel = if (type == CaptureTargetType.HEAD) "person-001" else "garment-001"
                                onConfigChange(
                                    config.copy(
                                        targetType = type,
                                        subjectLabel = defaultLabel
                                    )
                                )
                            },
                            label = { Text(type.displayLabel) }
                        )
                    }
                }

                OutlinedTextField(
                    value = config.subjectLabel,
                    onValueChange = { onConfigChange(config.copy(subjectLabel = it)) },
                    label = { Text("样本标识（如 person-001 / shirt-002）") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
            }
        }

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text("2. 真实尺度与属性声明（不靠单目镜头猜身高）", style = MaterialTheme.typography.titleMedium)
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    OutlinedTextField(
                        value = config.heightCmInput,
                        onValueChange = { raw ->
                            val filtered = raw.filter { it.isDigit() }.take(3)
                            onConfigChange(config.copy(heightCmInput = filtered))
                        },
                        label = { Text("身高 cm（选填）") },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        singleLine = true,
                        modifier = Modifier.weight(1f)
                    )
                }

                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("未填写", "男", "女").forEach { g ->
                        FilterChip(
                            selected = config.genderNote == g,
                            onClick = { onConfigChange(config.copy(genderNote = g)) },
                            label = { Text("体型参考：$g") }
                        )
                    }
                }

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f).padding(end = 8.dp)) {
                        Text("已放置已知尺度参考物", style = MaterialTheme.typography.bodyLarge)
                        Text(
                            "例如地面 A4 纸、ArUco 标定卡或已知长度标尺",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Switch(
                        checked = config.hasScaleReference,
                        onCheckedChange = { onConfigChange(config.copy(hasScaleReference = it)) }
                    )
                }
            }
        }

        Card(
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant
            ),
            modifier = Modifier.fillMaxWidth()
        ) {
            Column(
                modifier = Modifier.padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Text("拍摄口诀", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                Text("• 阶段 1（10–15 秒）：全身/头部占屏幕 35%–55%，保持水平绕行点亮 12 个扇区。", style = MaterialTheme.typography.bodySmall)
                Text("• 阶段 2（5–10 秒）：不要停录！请被拍人静止，镜头缓慢向前推近拍正/左右斜脸或领口。", style = MaterialTheme.typography.bodySmall)
            }
        }

        Button(
            onClick = onStartCapture,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 52.dp)
        ) {
            Text("开始一镜到底采集")
        }
    }
}

@Composable
private fun CaptureResultContent(
    response: CaptureGuideResponse,
    savedVideoUri: String?,
    recordError: String?,
    onSpeakResult: () -> Unit,
    onRetakeMissingArcs: () -> Unit,
    onRestartFull: () -> Unit,
    onBackHome: () -> Unit
) {
    val badgeColor = when (response.readiness) {
        "pass" -> Color(0xFF2E7D32)
        "needs_reshoot" -> Color(0xFFEF6C00)
        else -> Color(0xFFC62828)
    }
    val badgeLabel = when (response.readiness) {
        "pass" -> "采集达标 · 可导出上传重建"
        "needs_reshoot" -> "建议局部补拍（可复用已有素材）"
        else -> "关键几何受损 · 建议重拍"
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("采集质检与补拍指南", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            AssistChip(
                onClick = {},
                label = {
                    val modeText = when (response.mode) {
                        "ai" -> "真 AI 指南"
                        "mock" -> "Mock 模式"
                        else -> "离线降级预检"
                    }
                    Text(modeText)
                }
            )
        }

        Card(
            colors = CardDefaults.cardColors(containerColor = badgeColor.copy(alpha = 0.14f)),
            modifier = Modifier.fillMaxWidth()
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Text(badgeLabel, color = badgeColor, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Text(response.guide.summary, style = MaterialTheme.typography.bodyMedium)
                val sec = response.diagnosis.sectorSummary
                if (sec.coveredSectors != null) {
                    Text(
                        text = "扇区覆盖：${sec.coveredSectors}/${sec.sectorCount}（最大缺口 ${sec.largestGapDeg?.roundToInt() ?: 0}°）",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }

        if (savedVideoUri != null) {
            Text(
                text = "已本地保留录像：$savedVideoUri",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        if (recordError != null) {
            Text(
                text = recordError,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error
            )
        }

        response.guide.steps.forEach { step ->
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(
                    modifier = Modifier.padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    Text(
                        text = "步骤 ${step.priority} · ${step.targetArea}",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold
                    )
                    Text(step.action, style = MaterialTheme.typography.bodyMedium)
                    Text(
                        text = "原因：${step.reason}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }

        OutlinedButton(
            onClick = onSpeakResult,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
        ) {
            Text("语音播报补拍清单")
        }

        if (!response.isPass) {
            Button(
                onClick = onRetakeMissingArcs,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp)
            ) {
                Text("一键针对缺口角度重拍")
            }
        }

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            OutlinedButton(
                onClick = onRestartFull,
                modifier = Modifier
                    .weight(1f)
                    .heightIn(min = 48.dp)
            ) {
                Text("重新拍摄")
            }
            OutlinedButton(
                onClick = onBackHome,
                modifier = Modifier
                    .weight(1f)
                    .heightIn(min = 48.dp)
            ) {
                Text("返回首页")
            }
        }
    }
}

@Composable
private fun CameraPermissionDeniedContent(
    onRequestPermission: () -> Unit,
    onBack: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            text = "需要相机权限以开启实时采集引导",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold
        )
        Spacer(modifier = Modifier.height(10.dp))
        Text(
            text = "采集引导会在手机端本地分析画面清晰度、占比与眼部细节像素，不会在未经授权时开启摄像头。",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.height(20.dp))
        Button(
            onClick = onRequestPermission,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
        ) {
            Text("授予相机权限")
        }
        Spacer(modifier = Modifier.height(10.dp))
        OutlinedButton(
            onClick = onBack,
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
        ) {
            Text("返回首页")
        }
    }
}
