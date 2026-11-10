# OWNER: P2
"""kinds_draw.py: KINDS registry and validators for stroke, line, arrow, rect and ellipse objects.

Contract section D. Two layers of checking:

1. KINDS (merged by common/models.py): field names, their types and the
   defaults of optional fields. models.validate_obj uses this for the type
   check that the server runs before accepting OP_ADD.
2. check(obj): the VALUE checks that a type cannot express, such as
   width 1-40, alpha 0-1, color '#rrggbb', tool in pen/marker/highlighter
   and at most 20000 points per stroke. It assumes the type check passed.

validate(obj) runs both and is what a draw tool can call before sending.
Server rule: never trust the client, so a hostile client sending
width 9999 or a million points must be rejected, not just a wrong type.
"""

import math
import numbers

# --- Limits from contract section D ------------------------------------------

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

KINDS = {
    "stroke": {
        "required": {"points": list, "tool": str, "alpha": NUMBER, **_STYLE_TYPES},
        "defaults": {"tool": "pen", "alpha": 1.0, **_STYLE_DEFAULTS},
    },
    "line": {
        "required": {"x1": NUMBER, "y1": NUMBER, "x2": NUMBER, "y2": NUMBER, **_STYLE_TYPES},
        "defaults": dict(_STYLE_DEFAULTS),
    },
    "arrow": {
        "required": {"x1": NUMBER, "y1": NUMBER, "x2": NUMBER, "y2": NUMBER, **_STYLE_TYPES},
        "defaults": dict(_STYLE_DEFAULTS),
    },
    "rect": {
        "required": {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER,
                     "fill": NONE_OR_STR, **_STYLE_TYPES},
        "defaults": {"fill": None, **_STYLE_DEFAULTS},
    },
    "ellipse": {
        "required": {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER,
                     "fill": NONE_OR_STR, **_STYLE_TYPES},
        "defaults": {"fill": None, **_STYLE_DEFAULTS},
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


def _check_stroke(obj):
    """Value checks for a stroke: tool, alpha and the points list."""
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
    return ""


def _check_box(obj):
    """Value checks for rect and ellipse: non-negative size and fill color."""
    if obj["w"] < 0 or obj["h"] < 0:
        return "w and h must be >= 0"
    fill = get(obj, "fill")
    if fill is not None and not is_color(fill):
        return "fill must be null or '#rrggbb'"
    return ""


# kind -> extra value check (beyond the shared style check).
_CHECKS = {
    "stroke": _check_stroke,
    "line": lambda obj: "",
    "arrow": lambda obj: "",
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
