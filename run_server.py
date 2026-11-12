# OWNER: P1
"""run_server.py: Entry point: starts the whiteboard server.

All the work is in server/server.py (main): it binds 0.0.0.0, prints every
IPv4 address of this PC with the likely Wi-Fi / hotspot one marked, and
serves until Ctrl+C.

    python run_server.py [--port N]

Two things only happen in the built executable (sys.frozen is set by
PyInstaller); `python run_server.py` behaves exactly as before:

  * server_data/ is kept NEXT TO WhiteboardServer(.exe). A one-file build
    unpacks itself into a temporary folder that is deleted on exit, so the
    rooms must not be saved there. The folder is handed to server/versions.py
    through its documented $WB_DATA_DIR setting (unless the user set it).
  * On Windows, if the server stops with an error (port already in use, a
    crash), the console window waits for Enter before closing, so whoever
    double-clicked it can read the message. A normal Ctrl+C never waits, and
    it never waits when stdin is not an interactive console (scripts, the
    smoke test), so nothing can hang.
"""

import os
import sys
import traceback

from common import app_paths

FROZEN = getattr(sys, "frozen", False)

# Must happen BEFORE the server modules are imported, so they all see it.
if FROZEN and not os.environ.get("WB_DATA_DIR"):
    os.environ["WB_DATA_DIR"] = app_paths.data_dir("server_data")

import server.bootstrap  # noqa: E402,F401  (registers handlers and hooks; static import for PyInstaller)
from server import server as server_mod  # noqa: E402


def _stdin_is_interactive():
    """True only when a person can answer a prompt: stdin exists and is a terminal/console."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _pause_before_close():
    """Wait for Enter so a double-clicked console does not vanish (skipped when not interactive)."""
    if not _stdin_is_interactive():
        return
    try:
        input("\nThe server stopped because of the error above. Press Enter to close.")
    except (EOFError, KeyboardInterrupt):
        pass


def run_frozen_windows():
    """Run main(); on an error exit or uncaught exception, show it and pause before closing.

    Exit paths:
      * normal Ctrl+C shutdown (main catches KeyboardInterrupt and returns),
        a KeyboardInterrupt during start-up, or sys.exit(0): no pause;
      * sys.exit(non-zero), e.g. "Cannot listen on port" or a bad --port:
        main already printed why; pause, then exit with the same code;
      * any other exception: print the traceback, pause, exit with code 1.
    """
    try:
        server_mod.main()
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _pause_before_close()
        raise
    except BaseException:
        traceback.print_exc()
        _pause_before_close()
        sys.exit(1)


if __name__ == "__main__":
    if FROZEN and sys.platform == "win32":
        run_frozen_windows()
    else:
        server_mod.main()
