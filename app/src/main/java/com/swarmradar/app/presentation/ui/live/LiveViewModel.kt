package com.swarmradar.app.presentation.ui.live

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewmodel.compose.viewModel
import com.swarmradar.app.domain.model.DetectedObject
import com.swarmradar.app.domain.model.Point3D
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

data class LiveUiState(
    val points: List<Point3D> = emptyList(),
    val objects: List<DetectedObject> = emptyList(),
    val mqttState: String = "verbunden",
    val pointCount: Int = 0,
    val isRecording: Boolean = false,
    val selectedObject: DetectedObject? = null
)

class LiveViewModel : ViewModel() {
    private val _uiState = MutableStateFlow(LiveUiState())
    val uiState: StateFlow<LiveUiState> = _uiState

    fun toggleRecording() = _uiState.value.let { _uiState.value = it.copy(isRecording = !it.isRecording) }
    fun addBookmark() {}
    fun takeSnapshot() {}
    fun toggleFocusMode() {}
    fun onObjectSelected(o: DetectedObject) { _uiState.value = _uiState.value.copy(selectedObject = o) }
}
