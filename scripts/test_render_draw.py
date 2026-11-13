# OWNER: P2
"""test_render_draw.py: Offline test for common/kinds_draw.py and client/render_draw.py.

Run from the project root:  python scripts/test_render_draw.py

Checks:
  1. One object of every draw kind (and every stroke tool) passes
     models.validate_obj (types) and kinds_draw.check (value ranges).
  2. Broken objects (bad color, width 0 or 41, alpha 2, unknown tool,
     20001 points, negative size, wrong types) are rejected.
  3. render.render_board paints all of them onto a QImage without errors,
     which is saved as test_render_draw.png for a visual check.
No server or network is needed.
"""

import math
import os
import sys

# Make "import common" / "import client" work when run as a plain script.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

from client import render, render_draw  # noqa: E402,F401  (render_draw registers renderers)
from common import kinds_draw, models  # noqa: E402

OUT_PNG = os.path.join(ROOT, "test_render_draw.png")


def wave(x0, y0, length, amp, n=60):
    """Return n points of a sine wave starting at (x0, y0), rounded to 0.1 unit."""
    return [[round(x0 + length * i / (n - 1), 1),
             round(y0 + amp * math.sin(i / (n - 1) * 2 * math.pi), 1)] for i in range(n)]


def good_objects():
    """Build one valid object of every kind; z gives the paint order."""
    return [
        {"id": "p2-1", "kind": "rect", "x": 40, "y": 40, "w": 200, "h": 120,
         "color": "#4363d8", "width": 4, "fill": None, "z": 1},
        {"id": "p2-2", "kind": "rect", "x": 280, "y": 40, "w": 200, "h": 120,
         "color": "#000000", "width": 2, "fill": "#ffe08a", "z": 2},
        {"id": "p2-3", "kind": "ellipse", "x": 520, "y": 40, "w": 200, "h": 120,
         "color": "#e6194b", "width": 6, "z": 3},  # fill missing: default None
        {"id": "p2-4", "kind": "ellipse", "x": 760, "y": 40, "w": 120, "h": 120,
         "color": "#3cb44b", "width": 3, "fill": "#c8f0c8", "z": 4},
        {"id": "p2-5", "kind": "line", "x1": 40, "y1": 220, "x2": 440, "y2": 260,
         "color": "#911eb4", "width": 5, "z": 5},
        {"id": "p2-6", "kind": "arrow", "x1": 480, "y1": 300, "x2": 860, "y2": 200,
         "color": "#f58231", "width": 4, "z": 6},
        {"id": "p2-7", "kind": "arrow", "x1": 860, "y1": 320, "x2": 500, "y2": 340,
         "color": "#008080", "width": 12, "z": 7},
        {"id": "p2-8", "kind": "stroke", "tool": "pen", "color": "#000000", "width": 3,
         "alpha": 1.0, "points": wave(40, 400, 360, 30), "z": 8},
        {"id": "p2-9", "kind": "stroke", "tool": "marker", "color": "#e6194b", "width": 12,
         "alpha": 0.8, "points": wave(40, 480, 360, 30), "z": 9},
        {"id": "p2-10", "kind": "stroke", "tool": "highlighter", "color": "#ffd800",
         "width": 30, "alpha": 0.4, "points": [[30, 400], [420, 470]], "z": 10},
        {"id": "p2-11", "kind": "stroke", "points": [[600, 450]], "width": 20,
         "color": "#4363d8", "z": 11},  # one point = dot; tool/alpha defaults
    ]


def bad_objects():
    """Build objects that must be rejected, with a label for each."""
    base_stroke = {"id": "b", "kind": "stroke", "tool": "pen", "color": "#000000",
                   "width": 3, "alpha": 1.0, "points": [[0, 0], [1, 1]]}
    base_rect = {"id": "b", "kind": "rect", "x": 0, "y": 0, "w": 10, "h": 10,
                 "color": "#000000", "width": 3, "fill": None}
    return [
        ("color not #rrggbb", {**base_stroke, "color": "red"}),
        ("color bad hex", {**base_stroke, "color": "#12345g"}),
        ("width 0", {**base_stroke, "width": 0}),
        ("width 41", {**base_stroke, "width": 41}),
        ("width is a string", {**base_stroke, "width": "3"}),
        ("alpha 2", {**base_stroke, "alpha": 2}),
        ("alpha -0.1", {**base_stroke, "alpha": -0.1}),
        ("unknown tool", {**base_stroke, "tool": "spray"}),
        ("20001 points", {**base_stroke, "points": [[0, 0]] * 20001}),
        ("point not a pair", {**base_stroke, "points": [[0, 0, 0]]}),
        ("point not numbers", {**base_stroke, "points": [["a", 0]]}),
        ("points missing", {k: v for k, v in base_stroke.items() if k != "points"}),
        ("line missing x2", {"id": "b", "kind": "line", "x1": 0, "y1": 0, "y2": 5}),
        ("arrow NaN", {"id": "b", "kind": "arrow", "x1": 0, "y1": 0,
                       "x2": float("nan"), "y2": 5}),
        ("rect negative w", {**base_rect, "w": -5}),
        ("rect fill bad", {**base_rect, "fill": "blue"}),
        ("rect fill number", {**base_rect, "fill": 3}),
        ("ellipse bool width", {**base_rect, "kind": "ellipse", "width": True}),
    ]


def main():
    """Run the validation checks, render the board and save the PNG. Exit 1 on failure."""
    failures = 0

    print("Valid objects:")
    objects = good_objects()
    for obj in objects:
        ok, reason = kinds_draw.validate(obj)
        type_ok, _ = models.validate_obj(obj)
        status = "OK  " if ok and type_ok else "FAIL"
        failures += status == "FAIL"
        print("  %s %-8s %-11s %s" % (status, obj["kind"], obj.get("tool", ""), reason))

    print("Invalid objects (must be rejected):")
    for label, obj in bad_objects():
        ok, reason = kinds_draw.validate(obj)
        status = "OK  " if not ok else "FAIL"
        failures += status == "FAIL"
        print("  %s %-20s -> %s" % (status, label, reason or "accepted!"))

    # Paint the board offscreen. A QGuiApplication is needed for QPainter.
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    image = QImage(920, 560, QImage.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QColor("#eeeeee"))
    for x in range(0, 920, 20):  # board grid (config.GRID) as a background
        painter.drawLine(x, 0, x, 560)
    for y in range(0, 560, 20):
        painter.drawLine(0, y, 920, y)
    # Dict keyed by id like BoardModel.objects; shuffle order to prove z sorting.
    board = {o["id"]: o for o in reversed(objects)}
    render.render_board(painter, board, None)
    painter.end()

    # Spot-check pixels: filled rect/ellipse centers carry their fill, the
    # one-point stroke is a visible dot, and an empty cell stays white.
    checks = [((380, 100), "#ffe08a"), ((820, 100), "#c8f0c8"), ((600, 450), "#4363d8"),
              ((10, 10), "#ffffff")]
    for (x, y), want in checks:
        got = QColor(image.pixel(x, y)).name()
        status = "OK  " if got == want else "FAIL"
        failures += status == "FAIL"
        print("  %s pixel (%d, %d) = %s (want %s)" % (status, x, y, got, want))

    if not image.save(OUT_PNG):
        print("FAIL could not save %s" % OUT_PNG)
        failures += 1
    else:
        print("Saved %s" % OUT_PNG)

    print("ALL PASSED" if failures == 0 else "%d FAILURE(S)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
