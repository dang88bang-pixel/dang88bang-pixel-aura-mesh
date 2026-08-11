package com.swarmradar.app.service

import android.app.NotificationManager
import android.content.Context
import androidx.core.app.NotificationCompat
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/** Hilfs-Klasse für App-interne Benachrichtigungen (Alarme, Mayday, Status). */
@Singleton
class NotificationHelper @Inject constructor(
    @ApplicationContext private val context: Context
) {
    fun notify(title: String, body: String, id: Int) {
        val mgr = context.getSystemService(NotificationManager::class.java)
        val channelId = "swarmradar_alerts"
        mgr.notify(
            id,
            NotificationCompat.Builder(context, channelId)
                .setContentTitle(title)
                .setContentText(body)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setAutoCancel(true)
                .build()
        )
    }
}
