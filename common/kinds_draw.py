"""kinds_draw.py: KINDS registry and validators for stroke, line, arrow, rect and ellipse objects.

Contract section D. Two layers of checking:

1. KINDS (merged by common/models.py): field names, their types and the
   defaults of optional fields. models.validate_obj uses this for the type
   check that the server runs before accepting OP_ADD.
2. check(obj): the VALUE checks that a type cannot express, such as
   width 1-40, alpha 0-1, color '#rrggbb', tool in pen/marker/highlighter
   and at most 20000 points per stroke. It assumes the type check passed.

The PAGE rule (every coordinate inside 0..BOARD_W x 0..BOARD_H) is written
twice, because the server only runs models.validate_obj: the "limits" of
KINDS bound single fields (x1, y, w ...) and so are enforced by the server;
check() also tests what a per-field limit cannot say (x + w, every point of
a stroke) and is run by the client before it sends.

validate(obj) runs both and is what a draw tool can call before sending.
Server rule: never trust the client, so a hostile client sending
width 9999 or a million points must be rejected, not just a wrong type.
"""

import math
import numbers

from common import config

# --- Limits from contract section D ------------------------------------------

# Page rule: every coordinate of a draw object must lie on the page,
# 0..BOARD_W x 0..BOARD_H. EPS only absorbs float rounding in x + w.
EPS = 1e-6

WIDTH_MIN = 1
WIDTH_MAX = 40
ALPHA_MIN = 0.0
ALPHA_MAX = 1.0
MAX_POINTS = 20000
TOOLS = ("pen", "marker", "highlighter")

NUMBER = (int, float)     # JSON does not distinguish 1 from 1.0
NONE_OR_STR = (type(None), str)

# Style fields shared by every draw kind: their types, and their defaults
# (all of them are optional in an object).
_STYLE_TYPES = {"color": str, "width": NUMBER}
_STYLE_DEFAULTS = {"color": "#000000", "width": 3}

# --- The KINDS registry (contract section D) ---------------------------------
# required: every field of the kind with its type. A field that also appears
# in defaults may be missing from the object; the default is used instead.
#
# limits (contract rev 3): {field: [min, max]}. models.validate_obj, which the
# SERVER runs on every OP_ADD and on the object that results from an
# OP_UPDATE, rejects a number outside its range. Here the ranges are the
# page, so a hostile client cannot store a line that starts at x = -500.
# A limit can only bound ONE field, so "x + w <= BOARD_W" and the points of a
# stroke are checked by check() below instead.

_X_RANGE = [0, config.BOARD_W]
_Y_RANGE = [0, config.BOARD_H]
_LINE_LIMITS = {"x1": _X_RANGE, "x2": _X_RANGE, "y1": _Y_RANGE, "y2": _Y_RANGE}
_BOX_LIMITS = {"x": _X_RANGE, "y": _Y_RANGE, "w": _X_RANGE, "h": _Y_RANGE}


def _limits(spec):
    """Return a fresh copy of a limits dict, so no KINDS entry shares a list with another."""
    return {field: list(bounds) for field, bounds in spec.items()}


KINDS = {
    "stroke": {
        "required": {"points": list, "tool": str, "alpha": NUMBER, **_STYLE_TYPES},
        "defaults": {"tool": "pen", "alpha": 1.0, **_STYLE_DEFAULTS},
    },
    "line": {
        "required": {"x1": NUMBER, "y1": NUMBER, "x2": NUMBER, "y2": NUMBER, **_STYLE_TYPES},
        "defaults": dict(_STYLE_DEFAULTS),
        "limits": _limits(_LINE_LIMITS),
    },
    "arrow": {
        "required": {"x1": NUMBER, "y1": NUMBER, "x2": NUMBER, "y2": NUMBER, **_STYLE_TYPES},
        "defaults": dict(_STYLE_DEFAULTS),
        "limits": _limits(_LINE_LIMITS),
    },
    "rect": {
        "required": {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER,
                     "fill": NONE_OR_STR, **_STYLE_TYPES},
        "defaults": {"fill": None, **_STYLE_DEFAULTS},
        "limits": _limits(_BOX_LIMITS),
    },
    "ellipse": {
        "required": {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER,
                     "fill": NONE_OR_STR, **_STYLE_TYPES},
        "defaults": {"fill": None, **_STYLE_DEFAULTS},
        "limits": _limits(_BOX_LIMITS),
    },
}


# --- Helpers -----------------------------------------------------------------

def get(obj, field):
    """Return obj[field], or the kind's default when the field is missing.

    Renderers and tools use this so an object that relies on a default
    (for example a rect without fill) behaves exactly like a full one.
    """
    if field in obj:
        return obj[field]
    return KINDS.get(obj.get("kind"), {}).get("defaults", {}).get(field)


def is_color(value):
    """Return True if value is a '#rrggbb' hex color string."""
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[1:])


def _is_number(value):
    """Return True for a finite int or float (bool and NaN/inf rejected)."""
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and math.isfinite(value))


def _check_style(obj):
    """Check color and width, shared by every draw kind. Returns a reason or ''."""
    if not is_color(get(obj, "color")):
        return "color must be '#rrggbb'"
    width = get(obj, "width")
    if not WIDTH_MIN <= width <= WIDTH_MAX:
        return "width must be %d-%d" % (WIDTH_MIN, WIDTH_MAX)
    return ""


def point_on_page(x, y):
    """True when the board point (x, y) lies on the page (edges included)."""
    return -EPS <= x <= config.BOARD_W + EPS and -EPS <= y <= config.BOARD_H + EPS


def _check_stroke(obj):
    """Value checks for a stroke: tool, alpha, the points list and the page rule."""
    if get(obj, "tool") not in TOOLS:
        return "tool must be one of %s" % ", ".join(TOOLS)
    if not ALPHA_MIN <= get(obj, "alpha") <= ALPHA_MAX:
        return "alpha must be 0-1"
    points = obj["points"]
    if len(points) > MAX_POINTS:
        return "at most %d points per stroke" % MAX_POINTS
    for p in points:
        # Every point is a pair [x, y] of finite numbers in board units.
        if not (isinstance(p, (list, tuple)) and len(p) == 2
                and _is_number(p[0]) and _is_number(p[1])):
            return "every point must be [x, y]"
        if not point_on_page(p[0], p[1]):
            return "every point must be on the page (0-%d, 0-%d)" % (config.BOARD_W, config.BOARD_H)
    return ""


def _check_line(obj):
    """Value check for line and arrow: both end points on the page."""
    if not (point_on_page(obj["x1"], obj["y1"]) and point_on_page(obj["x2"], obj["y2"])):
        return "both end points must be on the page (0-%d, 0-%d)" % (config.BOARD_W, config.BOARD_H)
    return ""


def _check_box(obj):
    """Value checks for rect and ellipse: non-negative size, fill color and fully on the page."""
    if obj["w"] < 0 or obj["h"] < 0:
        return "w and h must be >= 0"
    fill = get(obj, "fill")
    if fill is not None and not is_color(fill):
        return "fill must be null or '#rrggbb'"
    # Both opposite corners on the page means the whole box is.
    if not (point_on_page(obj["x"], obj["y"])
            and point_on_page(obj["x"] + obj["w"], obj["y"] + obj["h"])):
        return "the shape must lie fully on the page (0-%d, 0-%d)" % (config.BOARD_W, config.BOARD_H)
    return ""


# kind -> extra value check (beyond the shared style check).
_CHECKS = {
    "stroke": _check_stroke,
    "line": _check_line,
    "arrow": _check_line,
    "rect": _check_box,
    "ellipse": _check_box,
}


def check(obj):
    """Range and format checks for a draw object that already passed the type check.

    Returns (True, "") or (False, reason). Objects of other kinds are
    accepted untouched (they belong to kinds_text / kinds_image).
    """
    extra = _CHECKS.get(obj.get("kind"))
    if extra is None:
        return True, ""
    reason = _check_style(obj) or extra(obj)
    return (False, reason) if reason else (True, "")


def validate(obj):
    """Full check of a draw object: models.validate_obj types, then check() values.

    Imported lazily because common.models imports this module.
    """
    from common import models  # noqa: PLC0415 (cycle: models imports kinds_draw)

    ok, reason = models.validate_obj(obj)
    if not ok:
        return ok, reason
    return check(obj)
