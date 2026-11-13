"""kinds_text.py: KINDS registry and validators for text and sticky objects.

Contract section D (rev 3 + the text-format fields below). Two board kinds:

    text    {x, y, w, h, text, color, size, font, bold, italic, underline, align}
            free text in a box, drawn in `color`
    sticky  {x, y, w, h, text, color, size, font, bold, italic, underline, align,
             text_color}
            sticky note, `color` is its BACKGROUND, `text_color` the text

x, y, w, h are board units (the box); text is the content, word-wrapped inside
the box by the renderer; size is the font size in board units (8-96).

Format fields (all optional; an object saved before they existed still loads
and gets the defaults): font = family name, bold / italic / underline = bool,
align = "left", "center" or "right", text_color = '#rrggbb' (sticky only).

Two layers of checking, like common/kinds_draw.py:

1. KINDS (merged by common/models.py): field types, defaults of optional
   fields and the optional "limits" entry of contract rev 3,
   {field: [min, max]}: a number must lie in [min, max] and a string must
   have a LENGTH in [min, max]. The SERVER enforces this on OP_ADD and on the
   object resulting from an OP_UPDATE (models.validate_obj), so a hostile
   client cannot store a 10 MB text or a 5000 pt font.
2. check(obj): value checks a type cannot express (color format, non-negative
   size of the box). The server does not call it (it only runs
   models.validate_obj); the client does, through validate(), so a tool never
   sends an object it already knows is bad. Renderers do not rely on it: they
   fall back to defaults for anything unusable.

LIMITS is the same {field: [min, max]} dict, exported on its own so client code
(the text editor's length cap) uses exactly the numbers the server enforces.
"""

import math
import numbers

NUMBER = (int, float)     # JSON does not distinguish 1 from 1.0

# --- Limits (contract rev 3 shape: field -> [min, max]) -----------------------

TEXT_MAX_CHARS = 2000     # longest text, in characters
SIZE_MIN = 8              # smallest font size
SIZE_MAX = 96             # largest font size
FONT_MAX_CHARS = 64       # longest font family name

ALIGNS = ("left", "center", "right")   # allowed values of `align`

LIMITS = {
    "text": [0, TEXT_MAX_CHARS],   # strings: the LENGTH must be in this range
    "size": [SIZE_MIN, SIZE_MAX],  # numbers: the value must be in this range
    "font": [1, FONT_MAX_CHARS],   # a family name: 1 to 64 characters
    "align": [4, 6],               # "left"/"right" = 4-5, "center" = 6 characters
}
STICKY_LIMITS = dict(LIMITS, text_color=[7, 7])   # '#rrggbb' is exactly 7 characters

# --- The KINDS registry (contract section D) ----------------------------------
# required: every field with its type. A field that also appears in defaults
# may be left out by the client; the server fills the default in.

_REQUIRED = {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER,
             "text": str, "color": str, "size": NUMBER,
             "font": str, "bold": bool, "italic": bool, "underline": bool, "align": str}

_FORMAT_DEFAULTS = {"font": "Segoe UI", "bold": False, "italic": False,
                    "underline": False, "align": "left"}
STICKY_TEXT_COLOR = "#2b2b2b"   # default text color of a sticky (dark on every pastel)

TEXT_DEFAULTS = {"text": "", "color": "#000000", "size": 18, **_FORMAT_DEFAULTS}
STICKY_DEFAULTS = {"text": "", "color": "#fff176", "size": 16, "text_color": STICKY_TEXT_COLOR,
                   **_FORMAT_DEFAULTS}


def _limits(limits):
    """Return a fresh copy of a limits dict, so no KINDS entry shares a list with another."""
    return {field: list(bounds) for field, bounds in limits.items()}


KINDS = {
    "text": {
        "required": dict(_REQUIRED),
        "defaults": dict(TEXT_DEFAULTS),
        "limits": _limits(LIMITS),
    },
    "sticky": {
        "required": dict(_REQUIRED, text_color=str),
        "defaults": dict(STICKY_DEFAULTS),
        "limits": _limits(STICKY_LIMITS),
    },
}


# --- Helpers ---------------------------------------------------------------------

def get(obj, field):
    """Return obj[field], or the kind's default when the field is missing.

    Renderers and tools use this so an object that relies on a default
    (a text without color, say) behaves exactly like a complete one.
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


def check(obj):
    """Format and range checks for a text or sticky object that passed the type check.

    Returns (True, "") or (False, reason). Objects of other kinds are accepted
    untouched (they belong to kinds_draw / kinds_image).
    """
    if obj.get("kind") not in KINDS:
        return True, ""
    if not is_color(get(obj, "color")):
        return False, "color must be '#rrggbb'"
    if obj["kind"] == "sticky" and not is_color(get(obj, "text_color")):
        return False, "text_color must be '#rrggbb'"
    if get(obj, "align") not in ALIGNS:
        return False, "align must be one of %s" % ", ".join(ALIGNS)
    if not str(get(obj, "font")).strip():
        return False, "font must not be empty"
    for field in ("x", "y", "w", "h"):
        if not _is_number(obj.get(field)):
            return False, "%s must be a finite number" % field
    if obj["w"] < 0 or obj["h"] < 0:
        return False, "w and h must be >= 0"
    return True, ""


def validate(obj):
    """Full check of a text or sticky object: models.validate_obj, then check().

    models.validate_obj covers the field types and the limits (text up to
    2000 characters, size 8-72). Imported lazily because common.models
    imports this module.
    """
    from common import models  # noqa: PLC0415 (cycle: models imports kinds_text)

    ok, reason = models.validate_obj(obj)
    if not ok:
        return ok, reason
    return check(obj)
