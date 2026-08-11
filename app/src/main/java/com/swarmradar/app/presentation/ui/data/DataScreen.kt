package com.swarmradar.app.presentation.ui.data

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

data class SessionEntry(val id: Long, val date: String, val durationSec: Int, val pointCount: Int)

class DataViewModel {
    val sessions = mutableStateOf(
        listOf(
            SessionEntry(1, "2026-08-11 14:00", 320, 12450),
            SessionEntry(2, "2026-08-11 15:10", 95, 4120)
        )
    )
}

@Composable
fun DataScreen(viewModel: DataViewModel = DataViewModel()) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Daten & Export")
        viewModel.sessions.value.forEach { s ->
            Text("Session ${s.id}: ${s.date} – ${s.durationSec}s – ${s.pointCount} Punkte")
        }
        Button(onClick = { /* Export als CSV/JSON/MP4 */ }) {
            Text("Export teilen")
        }
    }
}
