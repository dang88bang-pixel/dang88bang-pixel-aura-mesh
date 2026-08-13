#!/usr/bin/env bash
#
# Run the host-side Kotlin tests without Gradle or the Android SDK.
#
# Why this exists: the Android tier of this project cannot be built in every
# environment (no SDK, no network route to dl.google.com). But the classes that
# carry the most risk — the audit chain that has to hash identically to the
# Python agent — are pure JVM logic with no Android dependency beyond
# `org.json`. This script compiles them against a small shim and runs them, so
# that logic is verified even where `gradlew assembleDebug` is impossible.
#
# It deliberately does NOT replace a real Android build. Anything touching
# Context, SensorManager, SQLiteOpenHelper or JNI still needs the SDK.
#
# Requirements: a JRE (JAVA_HOME or java on PATH) and a Kotlin compiler.
#   - KOTLIN_JARS  directory containing kotlin-stdlib + the compiler jar
#   - or kotlinc on PATH
#
# Usage:  tools/run-kotlin-tests.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${TMPDIR:-/tmp}/aura-kotlin-tests"
APP="$ROOT/android-app/app/src"

# ---------------------------------------------------------------- toolchain
if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVA="$JAVA_HOME/bin/java"
elif command -v java >/dev/null 2>&1; then
  JAVA=java
else
  echo "error: no JRE found (set JAVA_HOME or install java)" >&2
  exit 127
fi

compile() {
  local out="$1"; shift
  if [[ -n "${KOTLIN_JARS:-}" ]]; then
    "$JAVA" -cp "$KOTLIN_JARS/*" org.jetbrains.kotlin.cli.jvm.K2JVMCompiler \
      "$@" -no-stdlib -cp "$KOTLIN_STDLIB" -d "$out" -nowarn 2>&1 \
      | grep -viE "^warning:|WARNING:|jansi|native-access" || true
  elif command -v kotlinc >/dev/null 2>&1; then
    kotlinc "$@" -d "$out" -nowarn
  else
    echo "error: no Kotlin compiler (set KOTLIN_JARS or install kotlinc)" >&2
    exit 127
  fi
}

if [[ -n "${KOTLIN_JARS:-}" ]]; then
  KOTLIN_STDLIB="$(ls "$KOTLIN_JARS"/kotlin-stdlib-*.jar | head -1)"
  ANNOTATIONS="$(ls "$KOTLIN_JARS"/annotations-*.jar 2>/dev/null | head -1 || true)"
  [[ -n "$ANNOTATIONS" ]] && KOTLIN_STDLIB="$KOTLIN_STDLIB:$ANNOTATIONS"
fi

# ------------------------------------------------- refresh the Python fixtures
# The cross-platform test pins Kotlin's digests against Python's. Regenerating
# them here means a drift in aura/audit.py fails this suite immediately rather
# than silently in the field.
echo "==> regenerating cross-platform fixtures from the Python reference"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON_BIN="$ROOT/.venv/bin/python"
  else PYTHON_BIN=python3; fi
fi

FIXTURES="$("$PYTHON_BIN" "$ROOT/tools/generate_audit_fixtures.py")"
DIGEST0="$(echo "$FIXTURES" | sed -n 's/^DIGEST0=//p')"
DIGEST1="$(echo "$FIXTURES" | sed -n 's/^DIGEST1=//p')"
if [[ -z "$DIGEST0" || -z "$DIGEST1" ]]; then
  echo "error: could not generate fixtures from the Python reference" >&2
  exit 1
fi
echo "    DIGEST0=$DIGEST0"

# ---------------------------------------------------------------- build
rm -rf "$BUILD"
mkdir -p "$BUILD/src" "$BUILD/out"

# Substitute the freshly computed digests into a copy of the test source.
sed -e "s/@DIGEST0@/$DIGEST0/g" -e "s/@DIGEST1@/$DIGEST1/g" \
    "$APP/test/kotlin/AuditChainTest.kt" > "$BUILD/src/AuditChainTest.kt"

echo "==> compiling the audit-chain suite"
compile "$BUILD/out" \
  "$APP/test/shim/org/json/JsonShim.kt" \
  "$APP/main/java/com/aura/agent/security/CausalValidator.kt" \
  "$BUILD/src/AuditChainTest.kt"

if [[ ! -f "$BUILD/out/AuditChainTestKt.class" ]]; then
  echo "error: compilation produced no test class" >&2
  exit 1
fi

# The UWB parser and trilateration solve are Android-free by construction
# (see UwbGeometry's header), so they are compiled and run the same way.
echo "==> compiling the UWB geometry suite"
mkdir -p "$BUILD/out-uwb"
compile "$BUILD/out-uwb" \
  "$APP/main/java/com/aura/agent/sensors/UwbGeometry.kt" \
  "$APP/test/kotlin/UwbGeometryTest.kt"

if [[ ! -f "$BUILD/out-uwb/UwbGeometryTestKt.class" ]]; then
  echo "error: UWB suite produced no test class" >&2
  exit 1
fi

echo "==> generating position-quality fixtures from the Python reference"
CASES="$("$PYTHON_BIN" "$ROOT/tools/generate_quality_fixtures.py" | sed -n 's/^CASES=//p')"
if [[ -z "$CASES" ]]; then
  echo "error: could not generate quality fixtures" >&2
  exit 1
fi
sed -e "s|@CASES@|$CASES|" \
    "$APP/test/kotlin/PositionQualityTest.kt" > "$BUILD/src/PositionQualityTest.kt"

echo "==> compiling the position-quality suite"
mkdir -p "$BUILD/out-quality"
compile "$BUILD/out-quality" \
  "$APP/main/java/com/aura/agent/fusion/NativeEkf.kt" \
  "$BUILD/src/PositionQualityTest.kt"

if [[ ! -f "$BUILD/out-quality/PositionQualityTestKt.class" ]]; then
  echo "error: position-quality suite produced no test class" >&2
  exit 1
fi

echo "==> compiling the geo-anchor suite"
mkdir -p "$BUILD/out-geo"
compile "$BUILD/out-geo" \
  "$APP/main/java/com/aura/agent/sensors/GeoAnchorMath.kt" \
  "$APP/test/kotlin/GeoAnchorMathTest.kt"

if [[ ! -f "$BUILD/out-geo/GeoAnchorMathTestKt.class" ]]; then
  echo "error: geo-anchor suite produced no test class" >&2
  exit 1
fi

echo "==> compiling the vitals suite"
mkdir -p "$BUILD/out-vitals"
compile "$BUILD/out-vitals" \
  "$APP/main/java/com/aura/agent/sensors/VitalsEstimator.kt" \
  "$APP/test/kotlin/VitalsEstimatorTest.kt"

if [[ ! -f "$BUILD/out-vitals/VitalsEstimatorTestKt.class" ]]; then
  echo "error: vitals suite produced no test class" >&2
  exit 1
fi

echo "==> compiling the vector-store suite"
mkdir -p "$BUILD/out-llm"
compile "$BUILD/out-llm" \
  "$APP/main/java/com/aura/agent/llm/VectorStore.kt" \
  "$APP/test/kotlin/VectorStoreTest.kt"

if [[ ! -f "$BUILD/out-llm/VectorStoreTestKt.class" ]]; then
  echo "error: vector-store suite produced no test class" >&2
  exit 1
fi

echo "==> compiling the LiDAR baud suite"
mkdir -p "$BUILD/out-lidar"
compile "$BUILD/out-lidar" \
  "$APP/main/java/com/aura/agent/sensors/LidarBaud.kt" \
  "$APP/test/kotlin/LidarBaudTest.kt"

if [[ ! -f "$BUILD/out-lidar/LidarBaudTestKt.class" ]]; then
  echo "error: LiDAR baud suite produced no test class" >&2
  exit 1
fi

echo "==> running"
STATUS=0
"$JAVA" -cp "$BUILD/out:${KOTLIN_STDLIB:-}" AuditChainTestKt || STATUS=1
"$JAVA" -cp "$BUILD/out-uwb:${KOTLIN_STDLIB:-}" UwbGeometryTestKt || STATUS=1
"$JAVA" -cp "$BUILD/out-vitals:${KOTLIN_STDLIB:-}" VitalsEstimatorTestKt || STATUS=1
"$JAVA" -cp "$BUILD/out-quality:${KOTLIN_STDLIB:-}" PositionQualityTestKt || STATUS=1
"$JAVA" -cp "$BUILD/out-geo:${KOTLIN_STDLIB:-}" GeoAnchorMathTestKt || STATUS=1
"$JAVA" -cp "$BUILD/out-llm:${KOTLIN_STDLIB:-}" VectorStoreTestKt || STATUS=1
"$JAVA" -cp "$BUILD/out-lidar:${KOTLIN_STDLIB:-}" LidarBaudTestKt || STATUS=1

if [[ $STATUS -ne 0 ]]; then
  echo
  echo "FAILED: at least one Kotlin suite reported failures" >&2
fi
exit $STATUS
