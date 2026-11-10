# OWNER: P4
"""kinds_text.py: KINDS registry and validators for text and sticky objects.

Contract section D (rev 3). Two board kinds with the same fields:

    text    {x, y, w, h, text, color, size}    free text in a box, drawn in `color`
    sticky  {x, y, w, h, text, color, size}    sticky note, `color` is its BACKGROUND

x, y, w, h are board units (the box); text is the content, word-wrapped inside
the box by the renderer; size is the font size in board units.

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
SIZE_MAX = 72             # largest font size

LIMITS = {
    "text": [0, TEXT_MAX_CHARS],   # strings: the LENGTH must be in this range
    "size": [SIZE_MIN, SIZE_MAX],  # numbers: the value must be in this range
}

# --- The KINDS registry (contract section D) ----------------------------------
# required: every field with its type. A field that also appears in defaults
# may be left out by the client; the server fills the default in.

_REQUIRED = {"x": NUMBER, "y": NUMBER, "w": NUMBER, "h": NUMBER,
             "text": str, "color": str, "size": NUMBER}

TEXT_DEFAULTS = {"text": "", "color": "#000000", "size": 18}
STICKY_DEFAULTS = {"text": "", "color": "#fff176", "size": 16}


def _limits():
    """Return a fresh copy of LIMITS, so no KINDS entry shares a list with another."""
    return {field: list(bounds) for field, bounds in LIMITS.items()}


KINDS = {
    "text": {
        "required": dict(_REQUIRED),
        "defaults": dict(TEXT_DEFAULTS),
        "limits": _limits(),
    },
    "sticky": {
        "required": dict(_REQUIRED),
        "defaults": dict(STICKY_DEFAULTS),
        "limits": _limits(),
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
