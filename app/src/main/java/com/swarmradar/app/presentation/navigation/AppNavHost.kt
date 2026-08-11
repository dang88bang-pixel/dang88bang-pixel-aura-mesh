package com.swarmradar.app.presentation.navigation

import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.navigation.NavHostController
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import com.swarmradar.app.presentation.ui.data.DataScreen
import com.swarmradar.app.presentation.ui.live.LiveScreen
import com.swarmradar.app.presentation.ui.map.MapScreen
import com.swarmradar.app.presentation.ui.settings.SettingsScreen

@Composable
fun AppNavHost(navController: NavHostController = rememberNavController()) {
    Scaffold(bottomBar = { BottomNavBar(navController) }) { padding ->
        NavHost(
            navController,
            startDestination = "live",
            modifier = Modifier.fillMaxSize().padding(padding)
        ) {
            composable("live") { LiveScreen() }
            composable("map") { MapScreen() }
            composable("settings") { SettingsScreen() }
            composable("data") { DataScreen() }
        }
    }
}

@Composable
private fun BottomNavBar(navController: NavHostController) {
    val items = listOf(
        "live" to "Live",
        "map" to "Karte",
        "settings" to "Einstellungen",
        "data" to "Daten"
    )
    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val current = navBackStackEntry?.destination?.route
    NavigationBar {
        items.forEach { (route, label) ->
            NavigationBarItem(
                selected = current == route,
                onClick = { navController.navigate(route) },
                icon = { },
                label = { Text(label) }
            )
        }
    }
}
