# OWNER: P1
"""run_server.py: Entry point: starts the whiteboard server.

Imports server.bootstrap (registers every handler and hook), binds
0.0.0.0:config.PORT and prints the LAN address clients should type in.

    python run_server.py [port]
"""

import socket
import sys

from common import config
import server.bootstrap  # noqa: F401  (registers handlers and hooks)
from server import server as server_mod


def lan_ip():
    """Return this machine's LAN IP address.

    Connecting a UDP socket sends no packet; it only makes the OS choose
    the outgoing interface, whose address getsockname() then reports.
    Falls back to 127.0.0.1 when there is no network.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    """Start the server and run until Ctrl+C."""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else config.PORT
    srv = server_mod.Server("0.0.0.0", port)
    srv.start()
    print("Whiteboard server listening on %s:%d (LAN)" % (lan_ip(), port))
    print("Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
