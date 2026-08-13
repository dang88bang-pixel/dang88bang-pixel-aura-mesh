pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // usb-serial-for-android is published via JitPack, not Maven Central.
        // Scoped to that one group so a JitPack outage or a typo elsewhere
        // cannot silently pull an unexpected artifact from it.
        maven {
            url = uri("https://jitpack.io")
            content { includeGroup("com.github.mik3y") }
        }
    }
}
rootProject.name = "AuraAgent"
include(":app")
