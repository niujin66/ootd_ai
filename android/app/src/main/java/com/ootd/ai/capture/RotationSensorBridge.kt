package com.ootd.ai.capture

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager

/**
 * Android 旋转矢量传感器桥接器，将 TYPE_ROTATION_VECTOR 实时喂给 OrientationSectorTracker。
 * 当设备缺少旋转矢量传感器时安全降级为无数据状态，不崩溃。
 */
class RotationSensorBridge(
    context: Context,
    private val tracker: OrientationSectorTracker,
    private val onUpdate: (OrientationSnapshot) -> Unit
) : SensorEventListener {

    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
    private val rotationSensor: Sensor? = sensorManager?.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
    private val rotationMatrix = FloatArray(9)
    private var started = false

    val isSensorAvailable: Boolean
        get() = rotationSensor != null

    fun start() {
        val manager = sensorManager ?: return
        val sensor = rotationSensor ?: return
        if (started) return
        started = manager.registerListener(this, sensor, SensorManager.SENSOR_DELAY_GAME)
    }

    fun stop() {
        if (!started) return
        sensorManager?.unregisterListener(this)
        started = false
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type != Sensor.TYPE_ROTATION_VECTOR) return
        SensorManager.getRotationMatrixFromVector(rotationMatrix, event.values)
        val snapshot = tracker.updateFromRotationMatrix(rotationMatrix)
        onUpdate(snapshot)
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
}
