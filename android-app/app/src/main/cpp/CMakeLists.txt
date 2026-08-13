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

# Host-side test binary: cmake -DAURA_BUILD_TESTS=ON
option(AURA_BUILD_TESTS "Build the native test suite" OFF)
if(AURA_BUILD_TESTS)
    add_executable(test_aura_core tests/test_aura_core.cpp aura_core.cpp)
    target_include_directories(test_aura_core PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
    enable_testing()
    add_test(NAME aura_core_tests COMMAND test_aura_core)
endif()
