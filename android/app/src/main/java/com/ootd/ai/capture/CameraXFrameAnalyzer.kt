package com.ootd.ai.capture

import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.face.FaceDetection
import com.google.mlkit.vision.face.FaceDetector
import com.google.mlkit.vision.face.FaceDetectorOptions
import com.google.mlkit.vision.face.FaceLandmark
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.hypot

/**
 * CameraX ImageAnalysis 桥接器：
 * - 限流 3–4 FPS（默认最小间隔 260ms），避免主线程或 GPU 预览掉帧
 * - 从 YUV_420_888 提取紧凑 Y 平面传给 LiveFrameQualityAnalyzer
 * - target_type = HEAD 时调用纯端侧离线 ML Kit FaceDetection 抽取眼部像素与偏航角
 */
class CameraXFrameAnalyzer(
    private val analyzer: LiveFrameQualityAnalyzer,
    private val targetTypeProvider: () -> CaptureTargetType,
    private val orientationProvider: () -> OrientationSnapshot?,
    private val onSnapshot: (LiveQualitySnapshot) -> Unit
) : ImageAnalysis.Analyzer {

    private val faceDetector: FaceDetector = FaceDetection.getClient(
        FaceDetectorOptions.Builder()
            .setPerformanceMode(FaceDetectorOptions.PERFORMANCE_MODE_FAST)
            .setLandmarkMode(FaceDetectorOptions.LANDMARK_MODE_ALL)
            .setClassificationMode(FaceDetectorOptions.CLASSIFICATION_MODE_NONE)
            .setMinFaceSize(0.12f)
            .build()
    )

    private val mlKitInFlight = AtomicBoolean(false)
    private var lastAnalyzedElapsedMs = 0L
    @Volatile
    private var released = false

    @OptIn(ExperimentalGetImage::class)
    override fun analyze(imageProxy: ImageProxy) {
        if (released) {
            imageProxy.close()
            return
        }

        val now = SystemClock.elapsedRealtime()
        if (now - lastAnalyzedElapsedMs < MIN_INTERVAL_MS) {
            imageProxy.close()
            return
        }
        lastAnalyzedElapsedMs = now

        val rotation = imageProxy.imageInfo.rotationDegrees
        val gray = extractRotatedYPlane(imageProxy, rotation)
        val targetType = targetTypeProvider()
        val mediaImage = imageProxy.image

        if (targetType == CaptureTargetType.HEAD && mediaImage != null && mlKitInFlight.compareAndSet(false, true)) {
            val inputImage = InputImage.fromMediaImage(mediaImage, rotation)
            faceDetector.process(inputImage)
                .addOnSuccessListener { faces ->
                    val obs = faces.map { face ->
                        val box = face.boundingBox
                        val leftEye = face.getLandmark(FaceLandmark.LEFT_EYE)?.position
                        val rightEye = face.getLandmark(FaceLandmark.RIGHT_EYE)?.position
                        val eyeDist = if (leftEye != null && rightEye != null) {
                            hypot((leftEye.x - rightEye.x).toDouble(), (leftEye.y - rightEye.y).toDouble())
                        } else {
                            // 回退：人眼间距约为人脸框宽度的 42%
                            box.width().toDouble() * 0.42
                        }
                        FaceObservation(
                            leftPx = box.left.toFloat(),
                            topPx = box.top.toFloat(),
                            rightPx = box.right.toFloat(),
                            bottomPx = box.bottom.toFloat(),
                            eyeDistancePx = eyeDist,
                            headEulerAngleYDeg = face.headEulerAngleY
                        )
                    }
                    val snapshot = analyzer.analyzeFrame(gray, orientationProvider(), obs)
                    onSnapshot(snapshot)
                }
                .addOnFailureListener {
                    val snapshot = analyzer.analyzeFrame(gray, orientationProvider(), emptyList())
                    onSnapshot(snapshot)
                }
                .addOnCompleteListener {
                    mlKitInFlight.set(false)
                    imageProxy.close()
                }
        } else {
            val snapshot = analyzer.analyzeFrame(gray, orientationProvider(), emptyList())
            onSnapshot(snapshot)
            imageProxy.close()
        }
    }

    fun release() {
        released = true
        faceDetector.close()
    }

    companion object {
        private const val MIN_INTERVAL_MS = 260L

        private fun extractRotatedYPlane(imageProxy: ImageProxy, rotationDegrees: Int): GrayFrame {
            val plane = imageProxy.planes[0]
            val width = imageProxy.width
            val height = imageProxy.height
            val buffer = plane.buffer
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val raw = ByteArray(width * height)

            if (pixelStride == 1 && rowStride == width) {
                buffer.rewind()
                buffer.get(raw, 0, raw.size)
            } else {
                val rowData = ByteArray(rowStride)
                for (row in 0 until height) {
                    val rowStart = row * rowStride
                    buffer.position(rowStart)
                    val toRead = minOf(rowStride, buffer.remaining())
                    buffer.get(rowData, 0, toRead)
                    if (pixelStride == 1) {
                        System.arraycopy(rowData, 0, raw, row * width, width)
                    } else {
                        for (col in 0 until width) {
                            raw[row * width + col] = rowData[col * pixelStride]
                        }
                    }
                }
            }

            return when (((rotationDegrees % 360) + 360) % 360) {
                90 -> {
                    val rotated = ByteArray(width * height)
                    val outW = height
                    for (y in 0 until height) {
                        for (x in 0 until width) {
                            val nx = height - 1 - y
                            val ny = x
                            rotated[ny * outW + nx] = raw[y * width + x]
                        }
                    }
                    GrayFrame(width = height, height = width, yPlane = rotated)
                }
                180 -> {
                    val rotated = ByteArray(width * height)
                    val total = width * height
                    for (i in 0 until total) {
                        rotated[total - 1 - i] = raw[i]
                    }
                    GrayFrame(width = width, height = height, yPlane = rotated)
                }
                270 -> {
                    val rotated = ByteArray(width * height)
                    val outW = height
                    for (y in 0 until height) {
                        for (x in 0 until width) {
                            val nx = y
                            val ny = width - 1 - x
                            rotated[ny * outW + nx] = raw[y * width + x]
                        }
                    }
                    GrayFrame(width = height, height = width, yPlane = rotated)
                }
                else -> GrayFrame(width = width, height = height, yPlane = raw)
            }
        }
    }
}
