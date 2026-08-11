package com.swarmradar.app.presentation.theme

import androidx.compose.ui.graphics.Color
import com.swarmradar.app.domain.model.ObjectType

object ObjectColors {
    val PERSON = Color(0xFF00FF66u)         // Grün
    val ELECTRICAL = Color(0xFF1E90FFu)    // Blau
    val EXIT = Color(0xFFFF3333u)           // Rot
    val REMOTE_DEVICE = Color(0xFFFFD700u)  // Gold

    fun colorFor(type: ObjectType): Color = when (type) {
        ObjectType.PERSON -> PERSON
        ObjectType.ELECTRICAL -> ELECTRICAL
        ObjectType.EXIT -> EXIT
        else -> REMOTE_DEVICE
    }
}
