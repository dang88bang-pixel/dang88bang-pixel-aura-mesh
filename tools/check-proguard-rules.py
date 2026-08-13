#!/usr/bin/env python3
"""Static check: R8 keep rules cover the classes that need them, and name real ones.

R8 renames anything it cannot see being used. Three bindings in this project
are resolved by *name* at runtime, so R8 cannot see them:

  - JNI: the symbol is Java_<pkg>_<Class>_<method>. Rename either side and the
    call throws UnsatisfiedLinkError -- in release builds only, so it passes
    every debug test and fails in the field.
  - Reflection: `Class.forName("androidx.core.uwb.UwbManager")`.
  - XML inflation: custom views are constructed by name from the layout.

Two failure directions are checked:

  1. A class that needs a keep rule and has none (R8 will break it).
  2. A keep rule naming a class that no longer exists (silently protects
     nothing, and hides the fact that the real class is now unprotected).

Run:  tools/check-proguard-rules.py
Exit: 0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RULES = Path("android-app/app/proguard-rules.pro")
SRC = Path("android-app/app/src/main/java")
LAYOUTS = Path("android-app/app/src/main/res/layout")


def declared_classes() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for kt in SRC.rglob("*.kt"):
        text = kt.read_text(encoding="utf-8", errors="replace")
        pkg = re.search(r"^package\s+([\w.]+)", text, re.M)
        if not pkg:
            continue
        for m in re.finditer(
            r"^\s*(?:data\s+|inner\s+|sealed\s+|abstract\s+|open\s+)*"
            r"(?:class|object|interface)\s+(\w+)", text, re.M):
            out[f"{pkg.group(1)}.{m.group(1)}"] = kt
    return out


def main() -> int:
    if not RULES.is_file():
        print(f"error: {RULES} is missing — the release build will fail",
              file=sys.stderr)
        return 1

    rules = RULES.read_text(encoding="utf-8")
    declared = declared_classes()
    problems: list[str] = []

    kept = set(re.findall(r"^-keep(?:class)?\s+class\s+([\w.$]+)", rules, re.M))

    # 1. Keep rules must name classes that exist.
    for name in sorted(kept):
        if name.startswith("com.aura.agent") and name not in declared:
            problems.append(
                f"-keep names {name}, which no longer exists — the rule "
                f"protects nothing"
            )

    def covered(fqcn: str) -> bool:
        if fqcn in kept:
            return True
        # A wildcard rule on an enclosing package also covers it.
        return any(
            k.endswith("**") and fqcn.startswith(k[:-2]) for k in kept
        )

    # 2. Every class declaring a native method must be kept.
    for fqcn, path in sorted(declared.items()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "external fun" not in text:
            continue
        cls = fqcn.rsplit(".", 1)[1]
        # Only flag the class that actually holds the external declarations.
        block = re.search(
            rf"(?:class|object)\s+{re.escape(cls)}\b(.*?)(?=\n(?:class|object)\s|\Z)",
            text, re.S)
        if block and "external fun" in block.group(1) and not covered(fqcn):
            problems.append(
                f"{fqcn} declares native methods but has no -keep rule; R8 "
                f"will rename it and the JNI symbols will not resolve"
            )

    # 3. Custom views inflated from XML must be kept.
    if LAYOUTS.is_dir():
        for layout in LAYOUTS.glob("*.xml"):
            for tag in re.findall(r"<(com\.aura\.agent[\w.]+)", layout.read_text()):
                if not covered(tag):
                    problems.append(
                        f"{tag} is inflated from {layout.name} but has no "
                        f"-keep rule; inflation fails after minification"
                    )

    if problems:
        print("ProGuard rule check FAILED:")
        for p in sorted(set(problems)):
            print(f"  - {p}")
        return 1

    print(f"OK: {len(kept)} keep rules, all JNI and inflated classes covered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
