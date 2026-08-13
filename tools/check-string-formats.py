#!/usr/bin/env python3
"""Static check: getString() arguments match the format specifiers in strings.xml.

`getString(R.string.x, a, b)` is not type-checked by the compiler. If the
resource expects `%1$d` and it is handed a Float, the app throws
`IllegalFormatConversionException` **at render time** — so a mistake here
survives the whole build and shows up as a crash in the field, on the one
screen the operator is looking at.

The checks:

1. Every `R.string.*` referenced from Kotlin exists in `strings.xml`.
2. The number of arguments passed matches the number of distinct positional
   specifiers in the resource.
3. Where the argument is an obvious literal or a typed expression, its type is
   compatible with the conversion (`%d` integral, `%f` floating point).
4. Resources with more than one specifier use positional (`%1$s`) rather than
   bare (`%s`) form — required for translation, and Android's own lint fails
   the build over it.

Run:  tools/check-string-formats.py
Exit: 0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

RES = Path("android-app/app/src/main/res/values/strings.xml")
SRC = Path("android-app/app/src/main/java")

SPEC = re.compile(r"%(?:(\d+)\$)?([-#+ 0,(]*)(\d+)?(?:\.(\d+))?([a-zA-Z])")

# Argument expressions whose type we can infer without a compiler.
FLOAT_HINT = re.compile(
    r"(\.\d+f\b|\bstate\.(respirationBpm|heartRateBpm|cirAmplitude)|"
    r"position\[\d\]|attitude\[\d\]|velocity\[\d\]|\bsigma|\bspeed\b)"
)
INT_HINT = re.compile(
    r"(^\d+$|\bstate\.(iterations|lidarPoints|uwbAnchorsInView|mmwaveTargets|"
    r"movingTargets|beacons)\b|\.size\b|\.count\(|\btoInt\(\))"
)


def split_args(argstr: str) -> list[str]:
    """Split a call's argument list on top-level commas."""
    args, depth, quote, cur = [], 0, None, ""
    for ch in argstr:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch, cur = ch, cur + ch
            quote = ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def local_float_names(text: str) -> set[str]:
    """Locals assigned from a known-Float expression.

    A bare identifier like `resp` carries no type on its own, so the argument
    heuristics cannot judge it. Resolving one level of `val x = <expr>` covers
    the common case of pulling a field into a local before formatting it,
    which is exactly where a %d/%f mismatch hides.
    """
    names: set[str] = set()
    for m in re.finditer(r"\bva[lr]\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\n]+)", text):
        if FLOAT_HINT.search(m.group(2)):
            names.add(m.group(1))
    return names


def main() -> int:
    if not RES.is_file():
        print(f"error: {RES} not found", file=sys.stderr)
        return 2

    root = ET.parse(RES).getroot()
    resources: dict[str, str] = {}
    for node in root:
        if node.tag == "string" and node.get("name"):
            resources[node.get("name")] = "".join(node.itertext())

    problems: list[str] = []

    # ------------------------------------------- resource-side sanity
    specs_by_name: dict[str, list[tuple[str, str]]] = {}
    for name, text in resources.items():
        found = SPEC.findall(text)
        # %% is an escaped percent, not a specifier.
        found = [f for f in found if f[4] != "%"]
        positions: dict[int, str] = {}
        bare = 0
        for idx, (pos, _flags, _w, _prec, conv) in enumerate(found, start=1):
            if pos:
                positions[int(pos)] = conv
            else:
                bare += 1
                positions[idx] = conv
        if bare and len(found) > 1:
            problems.append(
                f"{name}: uses bare %-specifiers with {len(found)} arguments; "
                f"use positional %1$s form"
            )
        specs_by_name[name] = [(str(k), v) for k, v in sorted(positions.items())]

    # ------------------------------------------------- call sites
    call = re.compile(r"getString\(\s*R\.string\.([A-Za-z0-9_]+)\s*(,)?", re.S)
    for kt in SRC.rglob("*.kt"):
        text = kt.read_text(encoding="utf-8", errors="replace")
        floats_here = local_float_names(text)
        for m in call.finditer(text):
            name = m.group(1)
            if name not in resources:
                problems.append(f"{kt.name}: R.string.{name} does not exist")
                continue
            expected = specs_by_name.get(name, [])
            if not m.group(2):
                if expected:
                    problems.append(
                        f"{kt.name}: R.string.{name} needs {len(expected)} "
                        f"argument(s) but is called with none"
                    )
                continue
            # Walk to the matching close paren to capture the argument list.
            i, depth = m.end(), 1
            while i < len(text) and depth:
                if text[i] in "([":
                    depth += 1
                elif text[i] in ")]":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            args = split_args(text[m.end():i])
            if len(args) != len(expected):
                problems.append(
                    f"{kt.name}: R.string.{name} expects {len(expected)} "
                    f"argument(s), {len(args)} passed"
                )
                continue
            for (pos, conv), arg in zip(expected, args):
                is_float = bool(FLOAT_HINT.search(arg)) or arg.strip() in floats_here
                if conv in "dxo" and is_float:
                    problems.append(
                        f"{kt.name}: R.string.{name} %{pos}${conv} is integral "
                        f"but '{arg}' looks floating point — "
                        f"IllegalFormatConversionException at runtime"
                    )
                if conv in "fe" and INT_HINT.search(arg) and not is_float:
                    problems.append(
                        f"{kt.name}: R.string.{name} %{pos}${conv} is floating "
                        f"point but '{arg}' looks integral — "
                        f"IllegalFormatConversionException at runtime"
                    )

    if problems:
        print("String format check FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    formatted = sum(1 for v in specs_by_name.values() if v)
    print(f"OK: {len(resources)} strings ({formatted} with format arguments), all call sites consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
