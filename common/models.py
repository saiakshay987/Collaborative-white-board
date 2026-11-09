# OWNER: P1
"""models.py: Merged board-object kinds registry and validate_obj (contract section D).

Each common/kinds_*.py file (P2 draw, P4 text, P3 image) exports
    KINDS = {kind: {"required": {field: type}, "defaults": {...}}}
This module merges them into one registry and checks objects against it.
The server calls validate_obj before accepting an OP_ADD; a client may call
it before sending.
"""

import math
import numbers

# Plain static imports (PyInstaller needs them).
from common import kinds_draw, kinds_image, kinds_text

# Fields every object has (contract section D). owner and z are set by the
# server, so they are NOT required in an object coming from a client.
COMMON_REQUIRED = {"id": str, "kind": str}


def _merge(*modules):
    """Merge the KINDS dicts of the given modules into one dict.

    A module whose KINDS is not written yet counts as empty. Two files
    defining the same kind is a programming error and fails at import.
    """
    merged = {}
    for mod in modules:
        for kind, spec in getattr(mod, "KINDS", {}).items():
            if kind in merged:
                raise ValueError("kind %r defined twice (again in %s)" % (kind, mod.__name__))
            merged[kind] = spec
    return merged


# The merged registry: kind name -> {"required": {...}, "defaults": {...}}.
KINDS = _merge(kinds_draw, kinds_text, kinds_image)


def _type_ok(value, expected):
    """Return True if value matches the expected type (or tuple of types).

    JSON does not distinguish 1 from 1.0, so an int is accepted where a float
    is expected. bool is rejected for numbers even though Python treats it as
    an int, and numbers must be finite (no NaN or infinity).
    """
    types = expected if isinstance(expected, tuple) else (expected,)
    if isinstance(value, bool):
        return bool in types
    if isinstance(value, numbers.Real) and (float in types or int in types):
        if not math.isfinite(value):
            return False
        return isinstance(value, int) or float in types
    return isinstance(value, types)


def validate_obj(obj):
    """Check a board object against the merged KINDS registry.

    Returns (True, "") if obj is a dict with a string id, a known kind and
    every required field of that kind with the right type; otherwise
    (False, reason) with a short human-readable reason. Fields listed in the
    kind's "defaults" may be missing. The object is not modified.
    """
    if not isinstance(obj, dict):
        return False, "object is not a dict"
    for field, typ in COMMON_REQUIRED.items():
        if not _type_ok(obj.get(field), typ):
            return False, "missing or bad field %r" % field
    spec = KINDS.get(obj["kind"])
    if spec is None:
        return False, "unknown kind %r" % obj["kind"]
    defaults = spec.get("defaults", {})
    for field, typ in spec.get("required", {}).items():
        if field not in obj:
            if field in defaults:
                continue
            return False, "missing field %r" % field
        if not _type_ok(obj[field], typ):
            return False, "bad type for field %r" % field
    return True, ""
