"""test_message_limits.py: Tests for the optional "limits" in KINDS (contract rev 3).

Run from the project root:  python scripts/test_message_limits.py

Test-only kinds are added to models.KINDS for the run (and removed after),
so this does not depend on the limits the other modules put in their kinds files.

Checks:
  1. In-range numbers and string lengths pass.
  2. Edge values (exactly min and exactly max) pass.
  3. Out-of-range values fail with a reason naming the field.
  4. Wrong types fail: bool for a number, NaN, infinity, list.
  5. A kind with no "limits" behaves exactly as before (huge values pass).
  6. A limited field that is missing but has a default still passes.
  7. A limited field that is not in "required" is still type-guarded
     (bool, NaN and lists fail; a missing one is fine).
"""

import os
import sys

# Make "import common" work when run as a plain script from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import models  # noqa: E402

NUMBER = (int, float)

TEST_KINDS = {
    "_t_limited": {
        "required": {"w": NUMBER, "alpha": float, "label": str, "count": int},
        "defaults": {"label": "x"},
        "limits": {"w": [1, 40], "alpha": [0, 1], "label": [1, 5], "count": [0, 3],
                   "extra": [0, 10]},
    },
    "_t_plain": {
        "required": {"w": NUMBER, "label": str},
        "defaults": {},
    },
}

FAILS = []


def obj(kind="_t_limited", **fields):
    """Build a test object of the given kind with sensible in-range values."""
    base = {"id": "o1", "kind": kind}
    if kind == "_t_limited":
        base.update({"w": 10, "alpha": 0.5, "label": "abc", "count": 1})
    else:
        base.update({"w": 10, "label": "abc"})
    base.update(fields)
    return base


def expect(name, o, ok, field=None):
    """Run validate_obj on o and record a failure if the result is unexpected.

    When ok is False and field is given, the reason must mention that field.
    """
    got_ok, reason = models.validate_obj(o)
    good = got_ok == ok and (ok or field is None or repr(field) in reason)
    print("%-4s %-40s -> %s %s" % ("ok" if good else "FAIL", name, got_ok, reason))
    if not good:
        FAILS.append(name)


def main():
    """Install test kinds, run all cases, remove them, and exit non-zero on failure."""
    models.KINDS.update(TEST_KINDS)
    try:
        # 1. in range
        expect("in range", obj(), True)
        expect("float where NUMBER", obj(w=12.5), True)
        # 2. edges
        expect("w == min", obj(w=1), True)
        expect("w == max", obj(w=40), True)
        expect("alpha == 0 (int ok for float)", obj(alpha=0), True)
        expect("alpha == 1.0", obj(alpha=1.0), True)
        expect("label length == min", obj(label="a"), True)
        expect("label length == max", obj(label="abcde"), True)
        expect("count == max", obj(count=3), True)
        # 3. out of range
        expect("w below min", obj(w=0.999), False, "w")
        expect("w above max", obj(w=9999), False, "w")
        expect("alpha negative", obj(alpha=-0.01), False, "alpha")
        expect("label empty", obj(label=""), False, "label")
        expect("label too long", obj(label="abcdef"), False, "label")
        expect("count above max", obj(count=4), False, "count")
        # 4. wrong types
        expect("bool for number", obj(w=True), False, "w")
        expect("NaN", obj(w=float("nan")), False, "w")
        expect("inf", obj(w=float("inf")), False, "w")
        expect("-inf", obj(alpha=float("-inf")), False, "alpha")
        expect("float for int field", obj(count=1.5), False, "count")
        expect("list for number", obj(w=[1]), False, "w")
        expect("string for number", obj(w="10"), False, "w")
        # 5. no limits: unchanged behaviour
        expect("no limits: huge w", obj("_t_plain", w=1e9), True)
        expect("no limits: empty label", obj("_t_plain", label=""), True)
        expect("no limits: bool still rejected", obj("_t_plain", w=False), False, "w")
        # 6. limited field missing but defaulted
        o = obj()
        del o["label"]
        expect("limited field missing, has default", o, True)
        o = obj()
        del o["w"]
        expect("limited field missing, no default", o, False, "w")
        # 7. limited field not in required: only the limits guard sees it
        expect("unrequired limited: in range", obj(extra=10), True)
        expect("unrequired limited: out of range", obj(extra=11), False, "extra")
        expect("unrequired limited: bool", obj(extra=True), False, "extra")
        expect("unrequired limited: NaN", obj(extra=float("nan")), False, "extra")
        expect("unrequired limited: list", obj(extra=[1]), False, "extra")
    finally:
        for k in TEST_KINDS:
            models.KINDS.pop(k, None)

    print()
    if FAILS:
        print("FAILED: %d case(s): %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("All limits tests passed.")


if __name__ == "__main__":
    main()
