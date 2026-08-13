#!/usr/bin/env bash
#
# Provision a JVM + Kotlin compiler for the host-side tests.
#
# The Android SDK is not required to verify the pure-logic Kotlin in this
# project (the audit chain, the sensor parsers). A JRE and kotlinc are enough,
# and both can be obtained from PyPI when dl.google.com / Maven Central are
# unreachable — which was the situation this project was authored in.
#
# Prints the environment to export. Usage:
#
#     eval "$(tools/setup-kotlin-toolchain.sh)"
#     tools/run-kotlin-tests.sh
#
# On a normal developer machine you almost certainly already have both; this
# script is for CI and for sandboxes with restricted egress.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${AURA_TOOLCHAIN_CACHE:-$ROOT/.toolchain}"
mkdir -p "$CACHE"

log() { echo "[toolchain] $*" >&2; }

# Modern distros mark the system Python "externally managed" (PEP 668), so
# everything is installed into a venv we own rather than system-wide.
VENV="${AURA_VENV:-$ROOT/.venv}"
if [[ ! -x "$VENV/bin/python" ]]; then
  log "creating a virtualenv at $VENV"
  "${PYTHON:-python3}" -m venv "$VENV"
fi
VPY="$VENV/bin/python"

# ----------------------------------------------------------------- JRE
JAVA_HOME_OUT=""
if command -v java >/dev/null 2>&1 && [[ -z "${AURA_FORCE_JDK4PY:-}" ]]; then
  log "using the java already on PATH"
  JAVA_HOME_OUT="${JAVA_HOME:-}"
else
  if ! "$VPY" -c 'import jdk4py' >/dev/null 2>&1; then
    log "installing jdk4py (a packaged Temurin runtime) from PyPI"
    "$VPY" -m pip install --quiet jdk4py
  fi
  JAVA_HOME_OUT="$("$VPY" -c 'import jdk4py; print(jdk4py.JAVA_HOME)')"
  log "JAVA_HOME=$JAVA_HOME_OUT"
fi

# ------------------------------------------------------------- kotlinc
KOTLIN_JARS_OUT=""
if command -v kotlinc >/dev/null 2>&1; then
  log "using the kotlinc already on PATH"
else
  JARS="$CACHE/kotlin/extracted/run_kotlin_kernel/jars"
  if [[ ! -d "$JARS" ]]; then
    log "fetching a Kotlin compiler (bundled in kotlin-jupyter-kernel on PyPI)"
    mkdir -p "$CACHE/kotlin"
    "$VPY" -m pip download kotlin-jupyter-kernel -d "$CACHE/kotlin" --no-deps --quiet
    # shellcheck disable=SC2086
    unzip -o -q "$CACHE"/kotlin/kotlin_jupyter_kernel-*.whl -d "$CACHE/kotlin/extracted"
  fi
  KOTLIN_JARS_OUT="$JARS"
  log "KOTLIN_JARS=$KOTLIN_JARS_OUT"
fi

# The caller evals this.
[[ -n "$JAVA_HOME_OUT" ]]   && echo "export JAVA_HOME='$JAVA_HOME_OUT'"
[[ -n "$KOTLIN_JARS_OUT" ]] && echo "export KOTLIN_JARS='$KOTLIN_JARS_OUT'"
echo "export AURA_VENV='$VENV'"
