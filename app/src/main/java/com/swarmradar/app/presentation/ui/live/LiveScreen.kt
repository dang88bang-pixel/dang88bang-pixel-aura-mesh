package com.swarmradar.app.presentation.ui.live

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.swarmradar.app.presentation.threeD.PointCloudCanvas

@Composable
fun LiveScreen(viewModel: LiveViewModel = viewModel()) {
    val state = viewModel.uiState.collectAsState().value
    Box(modifier = Modifier.fillMaxSize()) {
        PointCloudCanvas(
            points = state.points,
            objects = state.objects,
            modifier = Modifier.fillMaxSize()
        )

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp)
        ) {
            Text("MQTT: ${state.mqttState}  |  Punkte: ${state.pointCount}")
        }

        Row(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(16.dp),
            horizontalArrangement = Arrangement.Center
        ) {
            Button(onClick = viewModel::toggleRecording) {
                Text(if (state.isRecording) "Stop" else "Start")
            }
            Spacer(modifier = Modifier.width(8.dp))
            Button(onClick = viewModel::addBookmark) { Text("Bookmark") }
            Spacer(modifier = Modifier.width(8.dp))
            Button(onClick = viewModel::takeSnapshot) { Text("Snapshot") }
        }
    }
}
