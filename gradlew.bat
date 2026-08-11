@echo off
rem Minimaler Gradle-Wrapper-Launcher (siehe gradlew / "gradle wrapper" für vollständige Version).
set APP_HOME=%~dp0
java -classpath "%APP_HOME%gradle\wrapper\gradle-wrapper.jar" org.gradle.wrapper.GradleWrapperMain %*
