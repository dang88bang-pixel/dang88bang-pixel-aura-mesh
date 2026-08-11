package com.swarmradar.app.presentation.ui.settings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

class SettingsViewModel {
    val imuEnabled = mutableStateOf(true)
    val bleEnabled = mutableStateOf(true)
    val gnssEnabled = mutableStateOf(false)
    val radarEnabled = mutableStateOf(true)
    val mqttUrl = mutableStateOf("tcp://broker.local:1883")
}

@Composable
fun SettingsScreen(viewModel: SettingsViewModel = SettingsViewModel()) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Einstellungen")
        SettingRow("IMU", viewModel.imuEnabled.value) { viewModel.imuEnabled.value = it }
        SettingRow("BLE-Triangulation", viewModel.bleEnabled.value) { viewModel.bleEnabled.value = it }
        SettingRow("GNSS (Außen)", viewModel.gnssEnabled.value) { viewModel.gnssEnabled.value = it }
        SettingRow("Externes Radar", viewModel.radarEnabled.value) { viewModel.radarEnabled.value = it }
        Text("MQTT-Broker: ${viewModel.mqttUrl.value}")
    }
}

@Composable
private fun SettingRow(label: String, checked: Boolean, onToggle: (Boolean) -> Unit) {
    androidx.compose.foundation.layout.Row(
        modifier = androidx.compose.ui.Modifier.padding(vertical = 4.dp),
        verticalAlignment = androidx.compose.ui.Alignment.CenterVertically
    ) {
        Text(label, modifier = androidx.compose.ui.Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onToggle)
    }
}
