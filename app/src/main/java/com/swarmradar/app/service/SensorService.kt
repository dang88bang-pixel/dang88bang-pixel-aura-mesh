package com.swarmradar.app.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.swarmradar.app.MainActivity
import com.swarmradar.app.data.remote.MqttSwarmClient
import com.swarmradar.app.data.sensor.SensorHardwareManager
import com.swarmradar.app.domain.ekf.CollaborativeEKF
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.launch
import javax.inject.Inject

@AndroidEntryPoint
class SensorService : Service() {

    @Inject lateinit var sensorManager: SensorHardwareManager
    @Inject lateinit var ekf: CollaborativeEKF
    @Inject lateinit var mqtt: MqttSwarmClient

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification("Erfassung aktiv"))
        sensorManager.startUdpServer()
        sensorManager.registerImu()
        wireStreams()
        scope.launch { publishLoop() }
    }

    private fun wireStreams() {
        sensorManager.imuData
            .onEach { ekf.predict(it, 0.05) }
            .launchIn(scope)

        sensorManager.radarData
            .onEach { ekf.updateWithRadar(it, "self") }
            .launchIn(scope)
    }

    private suspend fun publishLoop() {
        while (true) {
            delay(200)
            val state = ekf.getFusedState()
            mqtt.publishState(
                com.swarmradar.app.domain.model.EKFState(
                    state.position,
                    com.swarmradar.app.domain.model.Vector3(0.0, 0.0, 0.0),
                    state.orientation,
                    DoubleArray(100)
                ),
                emptyList(),
                emptyList()
            )
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        sensorManager.stop()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID, "SwarmRadar Erfassung",
            NotificationManager.IMPORTANCE_LOW
        )
        mgr.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pi = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SwarmRadar")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setContentIntent(pi)
            .build()
    }

    companion object {
        const val CHANNEL_ID = "swarmradar_channel"
        const val NOTIFICATION_ID = 1001
    }
}
