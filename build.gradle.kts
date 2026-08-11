// Root build file – deklariert nur die Plugin-Versionen (die eigentlichen
// Abhängigkeiten liegen im Version-Katalog gradle/libs.versions.toml).
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.kapt) apply false
    alias(libs.plugins.hilt.android) apply false
    alias(libs.plugins.google.ar.sceneform) apply false
}
