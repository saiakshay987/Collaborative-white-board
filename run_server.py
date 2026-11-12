# OWNER: P1
"""run_server.py: Entry point: starts the whiteboard server.

All the work is in server/server.py (main): it binds 0.0.0.0, prints every
IPv4 address of this PC with the likely Wi-Fi / hotspot one marked, and
serves until Ctrl+C.

    python run_server.py [--port N]
"""

import server.bootstrap  # noqa: F401  (registers handlers and hooks; static import for PyInstaller)
from server import server as server_mod

if __name__ == "__main__":
    server_mod.main()
