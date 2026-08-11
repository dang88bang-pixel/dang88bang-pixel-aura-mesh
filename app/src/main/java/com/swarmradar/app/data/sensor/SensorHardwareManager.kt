package com.swarmradar.app.data.sensor

import android.bluetooth.BluetoothManager
import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import com.swarmradar.app.domain.model.BleMeasurement
import com.swarmradar.app.domain.model.ImuFrame
import com.swarmradar.app.domain.model.RadarFrame
import com.swarmradar.app.domain.model.RadarTarget
import com.swarmradar.app.domain.model.Vector3
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import java.net.DatagramPacket
import java.net.DatagramSocket
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Kapselt die Hardware-Kommunikation: UDP (externe Radar/LiDAR), BLE (andere Clients)
 * und interne IMU. Sendet alles als SharedFlow weiter (NorthWorks-/wallhacks.-Inspired).
 */
@Singleton
class SensorHardwareManager @Inject constructor(
    private val context: Context,
    private val scope: CoroutineScope
) : SensorEventListener {

    private var udpServer: DatagramSocket? = null

    private val _radarData = MutableSharedFlow<RadarFrame>(extraBufferCapacity = 128)
    val radarData: SharedFlow<RadarFrame> = _radarData.asSharedFlow()

    private val bleScanner =
        context.getSystemService(BluetoothManager::class.java)?.adapter?.bluetoothLeScanner

    private val _bleMeasurements = MutableSharedFlow<BleMeasurement>(extraBufferCapacity = 128)
    val bleMeasurements: SharedFlow<BleMeasurement> = _bleMeasurements.asSharedFlow()

    private val sensorManager =
        context.getSystemService(Context.SENSOR_SERVICE) as SensorManager

    private val _imuData = MutableSharedFlow<ImuFrame>(extraBufferCapacity = 128)
    val imuData: SharedFlow<ImuFrame> = _imuData.asSharedFlow()

    private var registered = false

    fun startUdpServer(port: Int = 8888) {
        udpServer = DatagramSocket(port)
        scope.launch(Dispatchers.IO) {
            val buffer = ByteArray(4096)
            while (true) {
                val packet = DatagramPacket(buffer, buffer.size)
                udpServer?.receive(packet)
                if (packet.length > 4) {
                    _radarData.tryEmit(parseRadarFrame(packet.data.copyOf(packet.length)))
                }
            }
        }
    }

    fun registerImu() {
        if (registered) return
        sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)?.let {
            sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME)
        }
        sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)?.let {
            sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME)
        }
        registered = true
    }

    fun stop() {
        sensorManager.unregisterListener(this)
        udpServer?.close()
        udpServer = null
        registered = false
    }

    override fun onSensorChanged(event: SensorEvent) {
        val v = event.values
        val accel = Vector3(v[0].toDouble(), v[1].toDouble(), v[2].toDouble())
        // Vereinfacht: Beschleunigung als IMU-Frame; Gyro wäre separat zu aggregieren.
        _imuData.tryEmit(ImuFrame(accel, Vector3(0.0, 0.0, 0.0), System.currentTimeMillis()))
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    /** HLK-LD2450-ähnliches Protokoll: 4 Byte Header + 12 Byte pro Ziel. */
    private fun parseRadarFrame(raw: ByteArray): RadarFrame {
        val targets = mutableListOf<RadarTarget>()
        var offset = 4
        while (offset + 12 <= raw.size) {
            val x = readShort(raw, offset).toFloat() / 100f
            val y = readShort(raw, offset + 2).toFloat() / 100f
            val z = readShort(raw, offset + 4).toFloat() / 100f
            val vel = readShort(raw, offset + 8).toFloat() / 100f
            targets.add(RadarTarget(x, y, z, vel))
            offset += 12
        }
        return RadarFrame(System.currentTimeMillis(), targets)
    }

    private fun readShort(b: ByteArray, i: Int): Short =
        ((b[i].toInt() and 0xFF) or ((b[i + 1].toInt() and 0xFF) shl 8)).toShort()
}
