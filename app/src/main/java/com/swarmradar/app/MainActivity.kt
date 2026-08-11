package com.swarmradar.app

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.swarmradar.app.presentation.navigation.AppNavHost
import com.swarmradar.app.presentation.theme.RadarAppTheme
import com.swarmradar.app.service.SensorService
import dagger.hilt.android.AndroidEntryPoint

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            RadarAppTheme {
                AppNavHost()
            }
        }
        // Sensor-Erfassung als Vordergrund-Service starten
        startService(Intent(this, SensorService::class.java))
    }
}
