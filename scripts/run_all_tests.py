"""run_all_tests.py: Run every scripts/test_*.py and print one PASS/FAIL line each.

Run from the project root:  python scripts/run_all_tests.py [--show-output]

Each test script runs as its own subprocess (so a crash or a stuck test cannot
take the others down) with Qt's "offscreen" platform, which means no window
appears and no display is needed. A script passes when its exit status is 0.
A script that runs longer than TIMEOUT_S seconds is killed and counts as FAIL.

Output: one line per script (PASS or FAIL, then the seconds it took), then a
summary. With --show-output the captured output of every FAILED script is
printed after the summary, so the reason can be read without re-running it.

role_attack.py is not run here. It is a hostile client for a live demo: it
waits for a human host to turn it into a viewer (Session > Manage roles)
before it attacks, so it is listed as "manual" at the end of the output.

Exit status: 0 if every test script passed, 1 if any failed.

Uses only the standard library.
"""

import glob
import os
import subprocess
import sys
import time

# Folder of this file (scripts/) and the project root above it. The tests are
# started with the root as working directory, like their docstrings say.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)

# Seconds one test script may run before it is killed and counted as FAIL.
TIMEOUT_S = 120

# Scripts that need a human and are therefore only listed, never run.
MANUAL = [("role_attack.py", "needs a host to demote it to viewer; see its docstring")]


def find_tests():
    """Return the file names of every scripts/test_*.py, sorted."""
    paths = glob.glob(os.path.join(SCRIPTS_DIR, "test_*.py"))
    return sorted(os.path.basename(p) for p in paths)


def run_one(name):
    """Run scripts/<name> in a subprocess; return (passed, seconds, note, output)."""
    # Offscreen Qt: the GUI tests build real widgets but never open a window.
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    started = time.time()
    try:
        done = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, name)],
            cwd=ROOT, env=env, timeout=TIMEOUT_S,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,   # one stream, in order
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return False, time.time() - started, "timed out after %d s" % TIMEOUT_S, output
    seconds = time.time() - started
    note = "" if done.returncode == 0 else "exit status %d" % done.returncode
    return done.returncode == 0, seconds, note, done.stdout


def main():
    """Run all tests, print the table and the summary; return the exit status."""
    show_output = "--show-output" in sys.argv[1:]
    names = find_tests()
    if not names:
        print("no scripts/test_*.py found")
        return 1

    print("Running %d test scripts (offscreen Qt, %d s timeout each)\n" % (len(names), TIMEOUT_S))
    results = []                                  # (name, passed, seconds, note, output)
    started = time.time()
    for name in names:
        passed, seconds, note, output = run_one(name)
        results.append((name, passed, seconds, note, output))
        print("%-4s  %-34s %6.1f s  %s" % ("PASS" if passed else "FAIL", name, seconds, note), flush=True)

    failed = [r for r in results if not r[1]]
    print("\n%d passed, %d failed, %d total (%.1f s)"
          % (len(results) - len(failed), len(failed), len(results), time.time() - started))
    if failed:
        print("Failed: " + ", ".join(r[0] for r in failed))
    for name, why in MANUAL:
        print("manual: %s (%s)" % (name, why))

    if show_output:
        for name, _passed, _seconds, _note, output in failed:
            print("\n" + "=" * 20 + " output of " + name + " " + "=" * 20)
            print(output.rstrip())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
