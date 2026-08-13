# AURA 6.0 native core
cmake_minimum_required(VERSION 3.22.1)
project(aura_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(aura_core SHARED
    aura_core.cpp
    aura_jni.cpp
)

target_include_directories(aura_core PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

# -ffast-math is deliberately NOT enabled: the EKF and FISTA rely on IEEE
# semantics (NaN checks, denormal handling) and drift measurably without them.
target_compile_options(aura_core PRIVATE
    -O3
    -fvisibility=hidden
    -Wall
    -Wextra
    -fno-exceptions
    -fno-rtti
)

if(ANDROID_ABI STREQUAL "arm64-v8a")
    target_compile_options(aura_core PRIVATE -march=armv8-a+simd)
elseif(ANDROID_ABI STREQUAL "armeabi-v7a")
    target_compile_options(aura_core PRIVATE -mfpu=neon -mfloat-abi=softfp)
endif()

find_library(log-lib log)
target_link_libraries(aura_core ${log-lib})

# ---------------------------------------------------------------------------
# Optional offline assistant (separate library on purpose).
#
# The sensor pipeline must never depend on llama.cpp: a device with no model
# installed has to boot and map normally. LLMService wraps System.loadLibrary
# in runCatching, so an absent libllama_bridge.so degrades to "assistant
# unavailable" instead of crashing.
#
# Built by default in STUB mode, which provides the JNI symbols and returns an
# honest "not compiled in" message. Without it every LLMService call throws
# UnsatisfiedLinkError, which at the call site is indistinguishable from a real
# crash. tools/check-jni-symbols.py enforces that these symbols exist.
#
# For the real thing, vendor llama.cpp and configure with:
#     -DAURA_WITH_LLAMA=ON
# ---------------------------------------------------------------------------
option(AURA_WITH_LLAMA "Build the real llama.cpp backend for the assistant" OFF)

add_library(llama_bridge SHARED llama_bridge.cpp)
target_include_directories(llama_bridge PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(llama_bridge PRIVATE -O3 -fvisibility=hidden -Wall -Wextra)
target_link_libraries(llama_bridge ${log-lib})

if(AURA_WITH_LLAMA)
    # Expected layout: cpp/vendor/llama.cpp (git submodule).
    set(LLAMA_DIR ${CMAKE_CURRENT_SOURCE_DIR}/vendor/llama.cpp)
    if(NOT EXISTS ${LLAMA_DIR}/CMakeLists.txt)
        message(FATAL_ERROR
            "AURA_WITH_LLAMA=ON but ${LLAMA_DIR} is missing.\n"
            "  git submodule add https://github.com/ggerganov/llama.cpp "
            "android-app/app/src/main/cpp/vendor/llama.cpp")
    endif()
    set(LLAMA_BUILD_TESTS   OFF CACHE BOOL "" FORCE)
    set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
    set(LLAMA_BUILD_SERVER  OFF CACHE BOOL "" FORCE)
    set(GGML_OPENMP         OFF CACHE BOOL "" FORCE)  # no OpenMP runtime on Android
    add_subdirectory(${LLAMA_DIR} ${CMAKE_BINARY_DIR}/llama EXCLUDE_FROM_ALL)
    target_compile_definitions(llama_bridge PRIVATE AURA_WITH_LLAMA)
    target_link_libraries(llama_bridge llama ggml)
    message(STATUS "AURA: building llama_bridge WITH llama.cpp")
else()
    message(STATUS "AURA: building llama_bridge in STUB mode (assistant disabled)")
endif()

# Host-side test binary: cmake -DAURA_BUILD_TESTS=ON
option(AURA_BUILD_TESTS "Build the native test suite" OFF)
if(AURA_BUILD_TESTS)
    add_executable(test_aura_core tests/test_aura_core.cpp aura_core.cpp)
    target_include_directories(test_aura_core PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
    enable_testing()
    add_test(NAME aura_core_tests COMMAND test_aura_core)
endif()
