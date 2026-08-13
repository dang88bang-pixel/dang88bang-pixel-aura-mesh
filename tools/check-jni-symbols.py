#!/usr/bin/env python3
"""Verify that Kotlin `external fun` declarations and exported JNI symbols agree.

Why this exists
---------------
A mismatch between a Kotlin ``external fun`` and the C++ symbol that is supposed
to implement it is **not a build error**. Both sides compile happily; the app
then dies with ``UnsatisfiedLinkError`` the first time the method is called —
on the device, in the field, possibly mid-survey.

Renaming a class or moving it to another package silently breaks the mangled
name (``Java_<package>_<Class>_<method>``), which makes this the single easiest
way to ship a broken APK from a green build.

This script cross-checks both directions:

* every ``external fun`` must have a matching exported symbol
  -> otherwise: guaranteed ``UnsatisfiedLinkError`` at runtime
* every exported ``Java_*`` symbol should have a Kotlin declaration
  -> otherwise: dead native code, or a Kotlin class that was never written

Nested types matter: ``object NativeEngine`` inside ``AuraApplication.kt``
mangles to ``..._NativeEngine_...``, *not* ``..._AuraApplication_...``, because
JNI uses the enclosing **class**, not the file. A naive file-name-based check
gets this wrong and reports false positives.

Usage
-----
    tools/check-jni-symbols.py                  # uses nm on a built .so
    tools/check-jni-symbols.py --source-only    # parse the C++ instead of nm

Exit code 0 when consistent, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KOTLIN_ROOT = REPO / "android-app/app/src/main/java"
CPP_ROOT = REPO / "android-app/app/src/main/cpp"

# JNI escapes an underscore in an identifier as "_1".
def mangle(package: str, class_name: str, method: str) -> str:
    def esc(text: str) -> str:
        return text.replace("_", "_1")

    return f"Java_{esc(package).replace('.', '_')}_{esc(class_name)}_{esc(method)}"


def parse_kotlin(root: Path) -> list[tuple[str, str, str, Path, int]]:
    """Return (package, enclosing class, method, file, line) for each external fun.

    Tracks brace depth so that a type declared inside another type is attributed
    to the innermost enclosing class, which is what JNI mangling uses.
    """
    results: list[tuple[str, str, str, Path, int]] = []
    type_re = re.compile(
        r"^\s*(?:(?:public|internal|private|abstract|open|sealed|data|value)\s+)*"
        r"(?:class|object|interface)\s+([A-Za-z_][A-Za-z0-9_]*)"
    )
    external_re = re.compile(
        r"^\s*(?:(?:private|internal|public|protected)\s+)?external\s+fun\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)"
    )

    for path in sorted(root.rglob("*.kt")):
        package = ""
        # stack of (type name, brace depth at which it was opened)
        stack: list[tuple[str, int]] = []
        depth = 0
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("//")[0]

            if not package:
                match = re.match(r"^\s*package\s+([\w.]+)", line)
                if match:
                    package = match.group(1)

            type_match = type_re.match(line)
            pending_type = type_match.group(1) if type_match else None

            external_match = external_re.match(line)
            if external_match:
                enclosing = stack[-1][0] if stack else path.stem
                results.append((package, enclosing, external_match.group(1), path, lineno))

            opens = line.count("{")
            closes = line.count("}")
            if pending_type and opens:
                # companion objects do NOT create a new JNI class prefix for
                # members declared in the outer type, but a named object does.
                if not re.search(r"\bcompanion\s+object\b", line):
                    stack.append((pending_type, depth))
            depth += opens - closes
            while stack and depth <= stack[-1][1]:
                stack.pop()
    return results


def symbols_from_nm(library: Path) -> set[str]:
    output = subprocess.run(
        ["nm", "-D", "--defined-only", str(library)],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(re.findall(r"\bJava_[A-Za-z0-9_]+", output))


def symbols_from_source(root: Path) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*.cpp"):
        found |= set(re.findall(r"\b(Java_[A-Za-z0-9_]+)\s*\(", path.read_text(encoding="utf-8")))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, action="append", default=None,
                        help="path to a built .so (repeatable; the app ships "
                             "libaura_core.so and libllama_bridge.so)")
    parser.add_argument("--source-only", action="store_true",
                        help="parse the C++ sources instead of running nm")
    args = parser.parse_args()

    declarations = parse_kotlin(KOTLIN_ROOT)
    if not declarations:
        print("error: no 'external fun' declarations found; is the path right?", file=sys.stderr)
        return 1

    if args.source_only or not args.library:
        exported = symbols_from_source(CPP_ROOT)
        source_desc = f"C++ sources in {CPP_ROOT.relative_to(REPO)}"
    else:
        exported = set()
        for library in args.library:
            exported |= symbols_from_nm(library)
        source_desc = ", ".join(str(p) for p in args.library)

    print(f"Kotlin external declarations : {len(declarations)}")
    print(f"Exported JNI symbols         : {len(exported)}  ({source_desc})")
    print()

    # --- Kotlin -> native ------------------------------------------------
    missing: list[str] = []
    for package, class_name, method, path, lineno in declarations:
        expected = mangle(package, class_name, method)
        if expected not in exported:
            missing.append(
                f"  {path.relative_to(REPO)}:{lineno}\n"
                f"    {class_name}.{method}() expects {expected}"
            )

    # --- native -> Kotlin ------------------------------------------------
    declared_symbols = {mangle(p, c, m) for p, c, m, _, _ in declarations}
    orphans = sorted(exported - declared_symbols)

    failed = False
    if missing:
        failed = True
        print(f"MISSING NATIVE IMPLEMENTATION ({len(missing)}) "
              f"-- these throw UnsatisfiedLinkError at runtime:")
        print("\n".join(missing))
        print()

    if orphans:
        print(f"EXPORTED BUT NEVER DECLARED IN KOTLIN ({len(orphans)}) "
              f"-- dead native code, or a missing Kotlin class:")
        for symbol in orphans:
            print(f"  {symbol}")
        print()

    if not failed and not orphans:
        print("OK: every external fun has a matching symbol, and vice versa.")
    elif not failed:
        print("OK: every external fun has a matching symbol "
              "(orphaned exports listed above are non-fatal).")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
