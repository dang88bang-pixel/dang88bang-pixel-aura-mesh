package com.swarmradar.app.presentation.ui.map

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.swarmradar.app.domain.model.DetectedObject
import com.swarmradar.app.domain.model.ObjectType

data class MapUiState(
    val objects: List<DetectedObject> = emptyList()
)

class MapViewModel {
    val uiState = mutableStateOf(MapUiState())
}

@Composable
fun MapScreen(viewModel: MapViewModel = MapViewModel()) {
    val state = viewModel.uiState.value
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Text("Kartierung & Objekte")
        LazyColumn {
            items(state.objects) { obj ->
                val label = when (obj.type) {
                    ObjectType.PERSON -> "Person"
                    ObjectType.ELECTRICAL -> "Elektro"
                    ObjectType.EXIT -> "Ausgang"
                    else -> "Remote"
                }
                Text("- $label  (${obj.position.x.toFloat().format(2)}, ${obj.position.z.toFloat().format(2)})  [${obj.source}]")
            }
        }
    }
}

private fun Float.format(d: Int) = "%.${d}f".format(this)
