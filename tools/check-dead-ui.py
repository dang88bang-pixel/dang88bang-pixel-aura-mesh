#!/usr/bin/env python3
"""Static check: interactive views declared in a layout are actually used in code.

A `<Button>` with an id that no Kotlin file ever looks up compiles cleanly,
renders perfectly, and does nothing when pressed. That is a worse failure than
a crash: the user believes the action was performed. This project shipped 23
such ids at one point, which is how three of the four tabs came to be inert.

Only *interactive* and *data-bearing* views are required to be bound. Purely
decorative ones — a toolbar, a static label, a divider — legitimately have ids
they never need read, so the check would be noise if it demanded otherwise.

Run:  tools/check-dead-ui.py
Exit: 0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LAYOUTS = Path("android-app/app/src/main/res/layout")
SRC = Path("android-app/app/src/main/java")

# Views the user can act on, or that must be filled with data to be meaningful.
INTERACTIVE = {
    "Button", "ImageButton", "CheckBox", "RadioButton", "Switch", "ToggleButton",
    "EditText", "SeekBar", "Spinner", "RatingBar", "SearchView", "WebView",
    "MaterialButton", "MaterialSwitch", "MaterialCheckBox", "MaterialRadioButton",
    "Slider", "RangeSlider", "TextInputEditText", "FloatingActionButton",
    "ExtendedFloatingActionButton", "Chip", "BottomNavigationView", "NavigationView",
    "ViewPager2", "RecyclerView", "TabLayout", "LinearProgressIndicator",
    "CircularProgressIndicator", "ProgressBar", "SwitchMaterial",
}

# Custom views in this project that exist to display live data.
DATA_VIEWS = {"AttitudeView", "PointCloudView", "RssiBarView"}

# Decorative or structural: an id is allowed to be unused.
DECORATIVE_HINT = re.compile(r"(toolbar|divider|spacer|appbar|header|title)", re.I)


def local_name(tag: str) -> str:
    return tag.rsplit(".", 1)[-1]


def main() -> int:
    if not LAYOUTS.is_dir():
        print(f"error: {LAYOUTS} not found", file=sys.stderr)
        return 2

    used: set[str] = set()
    for kt in SRC.rglob("*.kt"):
        text = kt.read_text(encoding="utf-8", errors="replace")
        used.update(re.findall(r"R\.id\.([A-Za-z0-9_]+)", text))

    ns = "{http://schemas.android.com/apk/res/android}"
    problems: list[str] = []
    checked = 0

    for layout in sorted(LAYOUTS.glob("*.xml")):
        try:
            root = ET.parse(layout).getroot()
        except ET.ParseError as exc:
            problems.append(f"{layout.name}: malformed XML: {exc}")
            continue

        for node in root.iter():
            raw = node.get(f"{ns}id")
            if not raw:
                continue
            name = raw.split("/")[-1]
            kind = local_name(node.tag)
            requires_binding = kind in INTERACTIVE or kind in DATA_VIEWS
            if not requires_binding:
                continue
            checked += 1
            if name in used:
                continue
            if DECORATIVE_HINT.search(name):
                continue
            problems.append(
                f"{layout.name}: <{kind} android:id=\"@+id/{name}\"> is never "
                f"referenced from Kotlin — it will render but do nothing"
            )

    if problems:
        print("Dead UI check FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK: {checked} interactive/data views, all bound in code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
