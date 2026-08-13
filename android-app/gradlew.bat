@rem Gradle wrapper launcher for Windows.
@rem See gradlew for why gradle-wrapper.jar is not committed.
@if "%DEBUG%"=="" @echo off
setlocal
set DIRNAME=%~dp0
set WRAPPER_JAR=%DIRNAME%gradle\wrapper\gradle-wrapper.jar
if not exist "%WRAPPER_JAR%" (
    echo gradle-wrapper.jar missing. Run: gradle wrapper --gradle-version 8.7
    exit /b 1
)
if defined JAVA_HOME (set JAVACMD=%JAVA_HOME%\bin\java.exe) else (set JAVACMD=java.exe)
"%JAVACMD%" -classpath "%WRAPPER_JAR%" org.gradle.wrapper.GradleWrapperMain %*
