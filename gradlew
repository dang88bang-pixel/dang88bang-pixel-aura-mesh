#!/bin/sh

# Minimaler Gradle-Wrapper-Launcher.
# Eine vollständige, plattformtolerante Version kann lokal via
#   gradle wrapper
# (benötigt eine installierte Gradle-Distribution) erzeugt werden.

APP_HOME=$( cd "$(dirname "$0")" >/dev/null 2>&1 && pwd )
exec java -classpath "$APP_HOME/gradle/wrapper/gradle-wrapper.jar" org.gradle.wrapper.GradleWrapperMain "$@"
