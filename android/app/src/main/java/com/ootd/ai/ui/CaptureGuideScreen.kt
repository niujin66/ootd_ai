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
import androidx.camera.video.FallbackStrategy
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
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
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
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.ootd.ai.BuildConfig
import com.ootd.ai.capture.CameraXFrameAnalyzer
import com.ootd.ai.capture.CaptureDebugConfig
import com.ootd.ai.capture.CaptureDebugPrefs
import com.ootd.ai.capture.CaptureGuideApi
import com.ootd.ai.capture.CaptureGuideResponse
import com.ootd.ai.capture.CapturePrepConfig
import com.ootd.ai.capture.CaptureStage
import com.ootd.ai.capture.CaptureTargetType
import com.ootd.ai.capture.CloseupCheckId
import com.ootd.ai.capture.DirectDeepSeekCaptureGuide
import com.ootd.ai.capture.FramingStatus
import com.ootd.ai.capture.LiveFrameQualityAnalyzer
import com.ootd.ai.capture.LiveQualitySnapshot
import com.ootd.ai.capture.OfflineCaptureDiagnoser
import com.ootd.ai.capture.OrientationSectorTracker
import com.ootd.ai.capture.OrientationSnapshot
import com.ootd.ai.capture.RotationSensorBridge
import com.ootd.ai.capture.TrafficLight
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
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
 * 1. 开拍前准备：3 步图解说明 + 目标/尺度声明 + 可折叠开发者直连 DeepSeek 调试开关；
 * 2. 阶段 1 远景环绕：中央虚线安全取景框 + 右上角紧凑 12 扇区罗盘 + 水平仪 + 大白话行动指示；
 * 3. 阶段 2 一镜慢推近：不中断录像，引导凑近面部或领口/袖口补齐高清细节；
 * 4. 结束质检：保存录像，按调试配置直连 DeepSeek 或请求后端 /v1/capture/guide，异常时离线降级。
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
    var debugConfig by remember { mutableStateOf(CaptureDebugPrefs.load(context)) }
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

    var videoCapture by remember { mutableStateOf<VideoCapture<Recorder>?>(null) }
    var activeRecording by remember { mutableStateOf<Recording?>(null) }
    var pendingRecordStart by remember { mutableStateOf(false) }
    var recordingWarmupActive by remember { mutableStateOf(false) }
    var cameraBindError by remember { mutableStateOf<String?>(null) }

    val analysisExecutor = remember { Executors.newSingleThreadExecutor() }

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

    fun beginMediaStoreRecording(vc: VideoCapture<Recorder>) {
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
    }

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

    LaunchedEffect(qualitySnapshot?.voiceHint) {
        val hint = qualitySnapshot?.voiceHint ?: return@LaunchedEffect
        if (isCapturing) {
            speakHint(hint, flush = true)
        }
    }

    LaunchedEffect(isCapturing, pendingRecordStart, videoCapture) {
        val vc = videoCapture ?: return@LaunchedEffect
        if (!isCapturing || !pendingRecordStart) return@LaunchedEffect
        recordingWarmupActive = true
        delay(1200L)
        if (!isCapturing || !pendingRecordStart) {
            recordingWarmupActive = false
            return@LaunchedEffect
        }
        tracker.reset()
        analyzer.reset(CaptureStage.STAGE_1_OVERVIEW)
        orientationSnapshot = tracker.currentSnapshot()
        qualitySnapshot = null
        pendingRecordStart = false
        recordingWarmupActive = false
        beginMediaStoreRecording(vc)
        speakHint("第 1 步，退远一点把目标装进虚线框，连人带手机真正绕着目标走一圈", flush = true)
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
        pendingRecordStart = true
        recordingWarmupActive = true
        stage = CaptureStage.STAGE_1_OVERVIEW
    }

    fun finishAndInspect() {
        pendingRecordStart = false
        recordingWarmupActive = false
        runCatching { activeRecording?.stop() }
        activeRecording = null
        stage = CaptureStage.ANALYZING

        val useDirectDeepSeek = debugConfig.directDeepSeekEnabled && debugConfig.deepSeekApiKey.isNotBlank()
        analyzeStatusText = if (useDirectDeepSeek) {
            "正在本地整理质检数据，并直连 DeepSeek 生成补拍指南…"
        } else {
            "正在汇总两阶段指标并请求 AI 质检指引…"
        }

        val req = analyzer.buildGuideRequest(
            requestId = "android-${UUID.randomUUID().toString().take(8)}",
            prep = prepConfig,
            tracker = tracker
        )

        coroutineScope.launch {
            val response = withContext(Dispatchers.IO) {
                runCatching {
                    if (useDirectDeepSeek) {
                        DirectDeepSeekCaptureGuide(
                            baseUrl = debugConfig.deepSeekBaseUrl.ifBlank { CaptureDebugConfig.DEFAULT_DEEPSEEK_BASE_URL },
                            apiKey = debugConfig.deepSeekApiKey,
                            model = debugConfig.deepSeekModel.ifBlank { CaptureDebugConfig.DEFAULT_DEEPSEEK_MODEL }
                        ).generateGuide(req)
                    } else {
                        val serverUrl = debugConfig.customServerBaseUrl.ifBlank { BuildConfig.OOTD_API_BASE_URL }
                        CaptureGuideApi(
                            baseUrl = serverUrl,
                            authToken = BuildConfig.OOTD_API_TOKEN
                        ).requestGuide(req)
                    }
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
                    debugConfig = debugConfig,
                    onConfigChange = { prepConfig = it },
                    onDebugConfigChange = { updated ->
                        debugConfig = updated
                        CaptureDebugPrefs.save(context, updated)
                    },
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
                        onVideoCaptureReady = { vc ->
                            videoCapture = vc
                        },
                        onBindError = { msg -> cameraBindError = msg },
                        onQualitySnapshot = { snap -> qualitySnapshot = snap }
                    )

                    // 顶部：返回、3步流程进度条、12扇区紧凑罗盘与静音开关
                    TopCaptureHud(
                        stage = stage,
                        orientation = orientationSnapshot,
                        targetGapSectors = targetGapSectors,
                        snapshot = qualitySnapshot,
                        voiceEnabled = voiceEnabled,
                        onToggleVoice = { voiceEnabled = !voiceEnabled },
                        onBack = {
                            pendingRecordStart = false
                            recordingWarmupActive = false
                            runCatching { activeRecording?.stop() }
                            activeRecording = null
                            stage = CaptureStage.PREPARE
                        },
                        modifier = Modifier
                            .align(Alignment.TopCenter)
                            .fillMaxWidth()
                            .statusBarsPadding()
                            .padding(horizontal = 12.dp, vertical = 10.dp)
                    )

                    // 中央：不遮挡脸部的虚线安全取景框 + 醒目行动指令徽章 + 细长水平仪
                    SafeFramingGuideOverlay(
                        stage = stage,
                        targetType = prepConfig.targetType,
                        orientation = orientationSnapshot,
                        snapshot = qualitySnapshot,
                        modifier = Modifier
                            .align(Alignment.Center)
                            .padding(bottom = 110.dp)
                    )

                    // 底部：大白话实时提示、三项状态速览与两阶段按钮
                    BottomCapturePanel(
                        stage = stage,
                        targetType = prepConfig.targetType,
                        orientation = orientationSnapshot,
                        snapshot = qualitySnapshot,
                        recordingWarmupActive = recordingWarmupActive,
                        cameraBindError = cameraBindError,
                        onAdvanceToStage2 = {
                            analyzer.enterStage2Closeup()
                            stage = CaptureStage.STAGE_2_CLOSEUP
                            speakHint("第 2 步！被拍人像雕塑一样别动，镜头慢慢凑近到 40 厘米拍正脸和侧脸", flush = true)
                        },
                        onMarkCloseupCompleted = { item ->
                            analyzer.markCloseupCompleted(item)
                            qualitySnapshot = qualitySnapshot?.let { snap ->
                                snap.copy(
                                    completedCloseups = snap.completedCloseups + item,
                                    missingCloseups = analyzer.computeMissingCloseups()
                                )
                            }
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
                    .setQualitySelector(
                        QualitySelector.fromOrderedList(
                            listOf(Quality.FHD, Quality.HD),
                            FallbackStrategy.lowerQualityOrHigherThan(Quality.HD)
                        )
                    )
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
private fun TopCaptureHud(
    stage: CaptureStage,
    orientation: OrientationSnapshot,
    targetGapSectors: Set<Int>,
    snapshot: LiveQualitySnapshot?,
    voiceEnabled: Boolean,
    onToggleVoice: () -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    Column(
        modifier = modifier,
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
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

            // 3 步流程进度指示条，一眼看清当前在哪一步
            val stepNum = when {
                stage == CaptureStage.STAGE_2_CLOSEUP -> 3
                (snapshot?.framingStatus == FramingStatus.GOOD ||
                    snapshot?.framingStatus == FramingStatus.MANUAL_GUIDE) && orientation.coveredCount >= 1 -> 2
                else -> 1
            }
            Surface(
                color = Color.Black.copy(alpha = 0.68f),
                shape = RoundedCornerShape(18.dp)
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    StepPill("①对齐", active = stepNum == 1, done = stepNum > 1)
                    Text("→", color = Color.White.copy(alpha = 0.55f), style = MaterialTheme.typography.labelSmall)
                    StepPill("②绕圈", active = stepNum == 2, done = stepNum > 2)
                    Text("→", color = Color.White.copy(alpha = 0.55f), style = MaterialTheme.typography.labelSmall)
                    StepPill("③推近", active = stepNum == 3, done = false)
                }
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

        // 右上角紧凑型 12 扇区罗盘（不再挡在屏幕正中间）
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.End
        ) {
            CompactSectorCompass(
                orientation = orientation,
                targetGapSectors = targetGapSectors
            )
        }
    }
}

@Composable
private fun StepPill(label: String, active: Boolean, done: Boolean) {
    val color = when {
        done -> Color(0xFF81C784)
        active -> Color(0xFFFFD54F)
        else -> Color.White.copy(alpha = 0.55f)
    }
    Text(
        text = if (done) "✓$label" else label,
        color = color,
        style = MaterialTheme.typography.labelMedium,
        fontWeight = if (active) FontWeight.Bold else FontWeight.Normal,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis
    )
}

@Composable
private fun CompactSectorCompass(
    orientation: OrientationSnapshot,
    targetGapSectors: Set<Int>
) {
    Surface(
        color = Color.Black.copy(alpha = 0.58f),
        shape = RoundedCornerShape(16.dp)
    ) {
        Box(
            modifier = Modifier
                .padding(8.dp)
                .size(76.dp),
            contentAlignment = Alignment.Center
        ) {
            Canvas(modifier = Modifier.fillMaxSize()) {
                val stroke = 7.dp.toPx()
                val inset = stroke / 2f
                val arcSize = Size(size.width - stroke, size.height - stroke)
                val topLeft = Offset(inset, inset)

                for (sector in 0 until OrientationSectorTracker.SECTOR_COUNT) {
                    val startAngle = -90f + sector * 30f + 2.5f
                    val sweep = 25f
                    val isCovered = orientation.coveredSectors[sector]
                    val isTargetGap = sector in targetGapSectors && !isCovered
                    val color = when {
                        isCovered -> Color(0xFF4CAF50)
                        isTargetGap -> Color(0xFFFFB300)
                        else -> Color.White.copy(alpha = 0.25f)
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

                val rad = ((orientation.relativeAzimuthDeg - 90.0) * PI / 180.0).toFloat()
                val radius = (size.minDimension - stroke) / 2f
                val cx = size.width / 2f
                val cy = size.height / 2f
                drawCircle(
                    color = Color.Cyan,
                    radius = 4.dp.toPx(),
                    center = Offset(cx + cos(rad) * radius, cy + sin(rad) * radius)
                )
            }
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text(
                    text = "${orientation.coveredCount}/12",
                    color = Color.White,
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = "已转格数",
                    color = Color.White.copy(alpha = 0.72f),
                    style = MaterialTheme.typography.labelSmall
                )
            }
        }
    }
}

@Composable
private fun SafeFramingGuideOverlay(
    stage: CaptureStage,
    targetType: CaptureTargetType,
    orientation: OrientationSnapshot,
    snapshot: LiveQualitySnapshot?,
    modifier: Modifier = Modifier
) {
    val light = snapshot?.trafficLight ?: TrafficLight.GREEN
    val guideColor = when (light) {
        TrafficLight.GREEN -> Color(0xFF69F0AE)
        TrafficLight.YELLOW -> Color(0xFFFFD54F)
        TrafficLight.RED -> Color(0xFFFF5252)
    }
    val actionBadge = snapshot?.actionBadge ?: if (stage == CaptureStage.STAGE_1_OVERVIEW) {
        "把目标放进虚线框里"
    } else {
        "慢慢凑近拍特写"
    }

    Column(
        modifier = modifier,
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        // 醒目行动指令标签
        Surface(
            color = guideColor.copy(alpha = 0.92f),
            shape = RoundedCornerShape(20.dp)
        ) {
            Text(
                text = "👉 $actionBadge",
                color = Color.Black,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp)
            )
        }

        // 中央透明虚线取景框：让人直观知道站多远、目标装多大合适
        val boxW = if (stage == CaptureStage.STAGE_1_OVERVIEW) 210.dp else 250.dp
        val boxH = if (stage == CaptureStage.STAGE_1_OVERVIEW) 265.dp else 285.dp
        Box(
            modifier = Modifier.size(width = boxW, height = boxH),
            contentAlignment = Alignment.BottomCenter
        ) {
            Canvas(modifier = Modifier.fillMaxSize()) {
                val strokeWidth = 2.5.dp.toPx()
                val dash = PathEffect.dashPathEffect(floatArrayOf(18f, 12f), 0f)
                drawRoundRect(
                    color = guideColor.copy(alpha = 0.85f),
                    topLeft = Offset(strokeWidth / 2f, strokeWidth / 2f),
                    size = Size(size.width - strokeWidth, size.height - strokeWidth),
                    cornerRadius = CornerRadius(24.dp.toPx(), 24.dp.toPx()),
                    style = Stroke(width = strokeWidth, pathEffect = dash)
                )
                if (targetType == CaptureTargetType.HEAD && stage == CaptureStage.STAGE_1_OVERVIEW) {
                    // 头部阶段 1 画一个轻轮廓提示大概大小
                    drawOval(
                        color = Color.White.copy(alpha = 0.25f),
                        topLeft = Offset(size.width * 0.22f, size.height * 0.10f),
                        size = Size(size.width * 0.56f, size.height * 0.58f),
                        style = Stroke(width = 1.5.dp.toPx(), pathEffect = dash)
                    )
                }
            }
            Surface(
                color = Color.Black.copy(alpha = 0.52f),
                shape = RoundedCornerShape(10.dp),
                modifier = Modifier.padding(bottom = 8.dp)
            ) {
                val boxCaption = when {
                    stage == CaptureStage.STAGE_2_CLOSEUP && targetType == CaptureTargetType.HEAD ->
                        "凑近到 ~40cm · 依次拍正/左/右脸"
                    stage == CaptureStage.STAGE_2_CLOSEUP ->
                        "凑近到 30–50cm · 拍清领口和纹理"
                    targetType == CaptureTargetType.HEAD ->
                        "退到 1.2–1.5m · 连人带机真正绕行"
                    else ->
                        "装进整件衣服 · 迈步绕一圈"
                }
                Text(
                    text = boxCaption,
                    color = Color.White.copy(alpha = 0.9f),
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp)
                )
            }
        }

        // 水平仪气泡条
        Surface(
            color = Color.Black.copy(alpha = 0.58f),
            shape = RoundedCornerShape(16.dp),
            modifier = Modifier
                .border(1.dp, if (orientation.isRollWarning) Color(0xFFFF5252) else Color(0xFF69F0AE).copy(alpha = 0.7f), RoundedCornerShape(16.dp))
                .padding(horizontal = 12.dp, vertical = 5.dp)
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Text(
                    text = if (orientation.isRollWarning) "机歪了 ${abs(orientation.rollDeg).roundToInt()}°" else "水平仪 ✓",
                    color = if (orientation.isRollWarning) Color(0xFFFF8A80) else Color(0xFF69F0AE),
                    style = MaterialTheme.typography.labelMedium,
                    fontWeight = FontWeight.SemiBold
                )
                Canvas(modifier = Modifier.size(width = 100.dp, height = 12.dp)) {
                    val midX = size.width / 2f
                    val midY = size.height / 2f
                    drawLine(
                        color = Color.White.copy(alpha = 0.35f),
                        start = Offset(0f, midY),
                        end = Offset(size.width, midY),
                        strokeWidth = 3.dp.toPx(),
                        cap = StrokeCap.Round
                    )
                    drawLine(
                        color = Color(0xFF4CAF50).copy(alpha = 0.65f),
                        start = Offset(midX - size.width * 0.22f, midY),
                        end = Offset(midX + size.width * 0.22f, midY),
                        strokeWidth = 5.dp.toPx(),
                        cap = StrokeCap.Round
                    )
                    val normRoll = (orientation.rollDeg / 45.0).coerceIn(-1.0, 1.0).toFloat()
                    drawCircle(
                        color = if (orientation.isRollWarning) Color(0xFFFF5252) else Color(0xFF69F0AE),
                        radius = 4.5.dp.toPx(),
                        center = Offset(midX + normRoll * (size.width * 0.44f), midY)
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun BottomCapturePanel(
    stage: CaptureStage,
    targetType: CaptureTargetType,
    orientation: OrientationSnapshot,
    snapshot: LiveQualitySnapshot?,
    recordingWarmupActive: Boolean,
    cameraBindError: String?,
    onAdvanceToStage2: () -> Unit,
    onMarkCloseupCompleted: (CloseupCheckId) -> Unit,
    onFinish: () -> Unit,
    modifier: Modifier = Modifier
) {
    val light = if (recordingWarmupActive) TrafficLight.YELLOW else (snapshot?.trafficLight ?: TrafficLight.GREEN)
    val lightColor = when (light) {
        TrafficLight.GREEN -> Color(0xFF2E7D32)
        TrafficLight.YELLOW -> Color(0xFFF9A825)
        TrafficLight.RED -> Color(0xFFC62828)
    }

    Surface(
        modifier = modifier,
        color = Color(0xFF101318).copy(alpha = 0.90f),
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
                    text = if (recordingWarmupActive) {
                        "正在自动对焦稳像（1 秒），请拿稳手机对准虚线框…"
                    } else {
                        snapshot?.promptText ?: "请站远约 1.2–1.5 米，把目标完整装进屏幕中央虚线框"
                    },
                    color = Color.White,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.SemiBold
                )
            }

            if (snapshot != null) {
                val distLabel = when (snapshot.framingStatus) {
                    FramingStatus.GOOD -> "距离合适 ✓"
                    FramingStatus.MANUAL_GUIDE -> "按虚线框目视对齐"
                    FramingStatus.TOO_FAR -> "偏远 · 可走近点"
                    FramingStatus.TOO_CLOSE -> "太近 · 退一大步"
                    FramingStatus.CROPPED -> "快切边 · 退半步"
                    FramingStatus.NOT_FOUND -> "未入框"
                }
                val sharpLabel = if (snapshot.sharpness >= LiveFrameQualityAnalyzer.SHARP_OK_THRESHOLD) {
                    "画面清晰 ✓"
                } else {
                    "略糊 · 慢一点"
                }
                val extraLabel = if (stage == CaptureStage.STAGE_2_CLOSEUP && snapshot.eyeDistancePx != null) {
                    "眼距 ${snapshot.eyeDistancePx.roundToInt()}px"
                } else {
                    "环绕 ${orientation.coveredCount}/12 格"
                }
                Text(
                    text = "$distLabel  ·  $sharpLabel  ·  $extraLabel",
                    color = Color.White.copy(alpha = 0.80f),
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
                            onClick = { onMarkCloseupCompleted(item) },
                            label = {
                                Text(
                                    text = if (checked) "✓ ${item.label}" else "待拍：${item.label}",
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
                            .weight(1.15f)
                            .heightIn(min = 48.dp)
                    ) {
                        Text(if (stage1Ready) "✓ 外圈完成 · 进入推近" else "进入第 2 步：慢推近")
                    }
                    OutlinedButton(
                        onClick = onFinish,
                        modifier = Modifier
                            .weight(0.85f)
                            .heightIn(min = 48.dp)
                    ) {
                        Text("直接结束", color = Color.White)
                    }
                } else {
                    Button(
                        onClick = onFinish,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Color(0xFF2E7D32)
                        ),
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 50.dp)
                    ) {
                        Text("完成录制 · 获取 AI 质检与补拍指南")
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
    debugConfig: CaptureDebugConfig,
    onConfigChange: (CapturePrepConfig) -> Unit,
    onDebugConfigChange: (CaptureDebugConfig) -> Unit,
    onStartCapture: () -> Unit,
    onBack: () -> Unit
) {
    var debugExpanded by remember { mutableStateOf(debugConfig.directDeepSeekEnabled) }
    var showApiKey by remember { mutableStateOf(false) }

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
            Text(
                text = "3D 实时采集引导",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            OutlinedButton(onClick = onBack) {
                Text("返回")
            }
        }

        // 上手 3 步卡，开拍前先看明白
        Card(
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer),
            modifier = Modifier.fillMaxWidth()
        ) {
            Column(
                modifier = Modifier.padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Text("3 步完成高质量采集（全程一镜到底，不要按停止）", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                Text("① 退远 1.2–1.5 米：让人头/衣服完整放进屏幕虚线框", style = MaterialTheme.typography.bodySmall)
                Text("② 缓慢绕一圈：被拍目标别动，连人带手机真正围绕目标走满一圈（别只站在原地扭手腕）", style = MaterialTheme.typography.bodySmall)
                Text("③ 推近拍特写：点「进入第 2 步」凑到 40 厘米拍正脸与左右耳侧", style = MaterialTheme.typography.bodySmall)
            }
        }

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text("1. 要拍什么？", style = MaterialTheme.typography.titleMedium)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    CaptureTargetType.entries.forEach { type ->
                        FilterChip(
                            selected = config.targetType == type,
                            onClick = {
                                val defaultLabel = if (type == CaptureTargetType.HEAD) "person-001" else "garment-001"
                                onConfigChange(config.copy(targetType = type, subjectLabel = defaultLabel))
                            },
                            label = { Text(type.displayLabel) }
                        )
                    }
                }

                OutlinedTextField(
                    value = config.subjectLabel,
                    onValueChange = { onConfigChange(config.copy(subjectLabel = it)) },
                    label = { Text("样本编号（如 person-001 / shirt-002）") },
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
                Text("2. 身高与标尺信息（防止 3D 模型大小不准）", style = MaterialTheme.typography.titleMedium)
                OutlinedTextField(
                    value = config.heightCmInput,
                    onValueChange = { raw ->
                        onConfigChange(config.copy(heightCmInput = raw.filter { it.isDigit() }.take(3)))
                    },
                    label = { Text("身高 cm（选填）") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )

                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("未填写", "男", "女").forEach { g ->
                        FilterChip(
                            selected = config.genderNote == g,
                            onClick = { onConfigChange(config.copy(genderNote = g)) },
                            label = { Text("体型：$g") }
                        )
                    }
                }

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f).padding(end = 8.dp)) {
                        Text("地上/旁边已放标尺或 A4 纸", style = MaterialTheme.typography.bodyLarge)
                        Text(
                            "用于后期把模型换算成真实厘米尺寸",
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

        // 开发者调试入口：无需本地电脑或云服务器即可手机直连 DeepSeek API
        Card(
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            modifier = Modifier.fillMaxWidth()
        ) {
            Column(
                modifier = Modifier.padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clickable { debugExpanded = !debugExpanded },
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text("开发者调试：AI 指导连接方式", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                        val modeDesc = if (debugConfig.directDeepSeekEnabled && debugConfig.deepSeekApiKey.isNotBlank()) {
                            "当前：手机直连 DeepSeek（无需电脑后端）"
                        } else {
                            val url = debugConfig.customServerBaseUrl.ifBlank { BuildConfig.OOTD_API_BASE_URL }
                            "当前：连接后端服务 ($url)，连不上时自动离线预检"
                        }
                        Text(modeDesc, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    TextButton(onClick = { debugExpanded = !debugExpanded }) {
                        Text(if (debugExpanded) "收起" else "展开配置")
                    }
                }

                if (debugExpanded) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f).padding(end = 8.dp)) {
                            Text("开启手机直连 DeepSeek API", style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
                            Text(
                                "仅用于真机调试，Key 只存在本机，不要带入正式发布包",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Switch(
                            checked = debugConfig.directDeepSeekEnabled,
                            onCheckedChange = { onDebugConfigChange(debugConfig.copy(directDeepSeekEnabled = it)) }
                        )
                    }

                    if (debugConfig.directDeepSeekEnabled) {
                        OutlinedTextField(
                            value = debugConfig.deepSeekApiKey,
                            onValueChange = { onDebugConfigChange(debugConfig.copy(deepSeekApiKey = it)) },
                            label = { Text("DeepSeek API Key（sk-...）") },
                            singleLine = true,
                            visualTransformation = if (showApiKey) VisualTransformation.None else PasswordVisualTransformation(),
                            trailingIcon = {
                                TextButton(onClick = { showApiKey = !showApiKey }) {
                                    Text(if (showApiKey) "隐藏" else "显示")
                                }
                            },
                            modifier = Modifier.fillMaxWidth()
                        )
                        OutlinedTextField(
                            value = debugConfig.deepSeekBaseUrl,
                            onValueChange = { onDebugConfigChange(debugConfig.copy(deepSeekBaseUrl = it)) },
                            label = { Text("API Base URL") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                        )
                        OutlinedTextField(
                            value = debugConfig.deepSeekModel,
                            onValueChange = { onDebugConfigChange(debugConfig.copy(deepSeekModel = it)) },
                            label = { Text("模型名称（如 deepseek-chat）") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                        )
                    } else {
                        OutlinedTextField(
                            value = debugConfig.customServerBaseUrl,
                            onValueChange = { onDebugConfigChange(debugConfig.copy(customServerBaseUrl = it)) },
                            label = { Text("自定义后端地址（选填，例如 http://192.168.1.10:8000）") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                        )
                    }
                }
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
        "pass" -> "采集达标 · 可用于 3D 重建"
        "needs_reshoot" -> "局部有漏拍 · 按下面补拍即可"
        else -> "关键画面有问题 · 建议重拍"
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
                        "ai" -> "服务端真 AI"
                        DirectDeepSeekCaptureGuide.MODE_DIRECT_AI -> "直连 DeepSeek 调试"
                        "mock" -> "服务端 Mock"
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
                text = "录像已保存到相册：$savedVideoUri",
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
            text = "采集引导会在手机端本地分析画面清晰度、距离与眼部细节像素，不会在未经授权时开启摄像头。",
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
