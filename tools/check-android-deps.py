#!/usr/bin/env python3
"""Static check: every third-party import resolves to a declared, fetchable dependency.

Two distinct build failures are caught here, both of which have actually
occurred in this repository:

1. An import with no declared coordinate at all.
2. A declared coordinate whose *repository* is missing. `usb-serial-for-android`
   was correctly listed in `build.gradle.kts`, but it is published on JitPack
   and only `google()` and `mavenCentral()` were configured, so resolution
   would have failed on a clean checkout.

Why a script rather than a grep in the CI yaml: a Java package name is not a
Maven group id. `okhttp3.*` ships in `com.squareup.okhttp3`, and
`com.hoho.android.usbserial.*` ships in `com.github.mik3y`. A grep that assumes
the first two package segments are the group id reports false positives on
both, and a gate that cries wolf gets ignored or deleted.

Run:  tools/check-android-deps.py [--app-dir android-app]
Exit: 0 clean, 1 problems found.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Package prefixes that need no dependency: the app itself, the JDK, the Kotlin
# stdlib and the Android platform (provided by the compile SDK).
STDLIB_PREFIXES = (
    "com.aura.agent",
    "java.",
    "javax.",
    "kotlin.",
    "android.",
    "androidx.",  # handled separately below - still needs a dependency
    "dalvik.",
    "org.json",
)

# Package prefix -> Maven group id, where they differ.
PACKAGE_TO_GROUP = {
    "okhttp3": "com.squareup.okhttp3",
    "okio": "com.squareup.okio",
    "retrofit2": "com.squareup.retrofit2",
    "com.hoho.android.usbserial": "com.github.mik3y",
    "com.google.gson": "com.google.code.gson",
    "kotlinx.coroutines": "org.jetbrains.kotlinx",
    "kotlinx.serialization": "org.jetbrains.kotlinx",
}

# Groups served by the repositories every Android build already declares.
DEFAULT_REPO_GROUPS = (
    "androidx.",
    "com.android.",
    "com.google.",
    "org.jetbrains.",
    "com.squareup.",
    "org.jspecify",
    "junit",
    "org.robolectric",
)


def imports_of(src_dir: Path) -> set[str]:
    found: set[str] = set()
    for kt in src_dir.rglob("*.kt"):
        for line in kt.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^import\s+([a-z][A-Za-z0-9._]*)", line)
            if m:
                found.add(m.group(1))
    return found


def group_for(pkg: str) -> str | None:
    """Map an imported package to the Maven group that should provide it."""
    for prefix, group in sorted(PACKAGE_TO_GROUP.items(), key=lambda kv: -len(kv[0])):
        if pkg == prefix or pkg.startswith(prefix + "."):
            return group
    if pkg.startswith("androidx.") or pkg.startswith("com.google.android.material"):
        # androidx groups are the first N segments; the artifact split varies,
        # so match on the longest declared coordinate prefix instead.
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default="android-app")
    args = ap.parse_args()

    root = Path(args.app_dir)
    gradle = (root / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    settings = (root / "settings.gradle.kts").read_text(encoding="utf-8")
    src = root / "app" / "src" / "main" / "java"
    if not src.is_dir():
        print(f"error: no sources at {src}", file=sys.stderr)
        return 2

    coords = re.findall(r'"([A-Za-z0-9._-]+):([A-Za-z0-9._-]+):([^"]+)"', gradle)
    declared_groups = {g for g, _, _ in coords}

    problems: list[str] = []

    # ---------------------------------------------------------- 1. imports
    for pkg in sorted(imports_of(src)):
        if pkg.startswith("com.aura.agent") or pkg.startswith(
            ("java.", "javax.", "kotlin.", "android.", "dalvik.", "org.json")
        ):
            # kotlin.* is the stdlib, but kotlinx.* is not - it is caught below.
            continue
        group = group_for(pkg)
        if group is not None:
            if group not in declared_groups:
                problems.append(f"import {pkg} needs group {group}, which is not declared")
            continue
        # Fall back to longest-prefix match against the declared coordinates,
        # which covers the androidx/material families.
        if not any(pkg.startswith(g) or g.startswith(pkg.split(".")[0]) for g in declared_groups):
            problems.append(f"import {pkg} has no matching declared dependency")

    # ----------------------------------------------------- 2. repositories
    for group in sorted(declared_groups):
        if any(group.startswith(p) for p in DEFAULT_REPO_GROUPS):
            continue
        # Anything else must be covered by an explicitly configured repository.
        if f'includeGroup("{group}")' not in settings and "jitpack.io" not in settings:
            problems.append(
                f"group {group} is not served by google()/mavenCentral() and has "
                f"no repository configured in settings.gradle.kts"
            )
        elif f'includeGroup("{group}")' not in settings:
            problems.append(
                f"group {group} relies on an unscoped custom repository; add "
                f'content {{ includeGroup("{group}") }}'
            )

    if problems:
        print("Android dependency check FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK: {len(declared_groups)} declared groups, all imports resolvable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
