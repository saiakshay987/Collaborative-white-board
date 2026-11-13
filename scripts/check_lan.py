# OWNER: P1
"""check_lan.py: Can this PC open a TCP connection to the whiteboard server?

    python scripts/check_lan.py <ip> [port]

Prints whether the TCP connect succeeded and how long it took. Run it on a
FRIEND's laptop, with the IP the server printed ("Clients should connect
to: ..."), before blaming the whiteboard client. It uses only the standard
library, so it works on any machine with Python.

How to read the result:
    OK      the network path and the server's firewall are fine.
    refused the PC answered, but nothing listens on that port (server not
            started, or started with another --port).
    timeout the packets vanish: wrong IP, other network, the server's
            firewall blocks TCP 5050, or the Wi-Fi has client isolation
            (the access point refuses to forward traffic between two Wi-Fi
            devices; common on campus / guest Wi-Fi, never on a phone hotspot).
    unreachable   this PC has no route to that IP (not on the same network).
"""

import errno
import socket
import sys
import time

DEFAULT_PORT = 5050   # same as common/config.PORT; repeated so the script stands alone
TIMEOUT_S = 5

# Same classification as client/net_client.describe_connect_error (repeated:
# this script must run on a laptop that only has Python, without the project).
TIMEOUT_CODES = {errno.ETIMEDOUT, 10060}
REFUSED_CODES = {errno.ECONNREFUSED, 10061}
UNREACHABLE_CODES = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, 10050, 10051, 10065}


def check(ip, port, timeout=TIMEOUT_S):
    """Try one TCP connect; return (verdict, detail, seconds).

    verdict is one of "ok", "timeout", "refused", "unreachable", "error".
    """
    start = time.time()
    try:
        socket.create_connection((ip, port), timeout=timeout).close()
    except OSError as exc:
        seconds = time.time() - start
        codes = {getattr(exc, "errno", None), getattr(exc, "winerror", None)}
        if isinstance(exc, socket.gaierror):
            return "error", "not a valid IP address / host name", seconds
        if isinstance(exc, (socket.timeout, TimeoutError)) or codes & TIMEOUT_CODES:
            return "timeout", "no answer within %d s: check same hotspot, firewall on the " \
                              "server, correct IP" % timeout, seconds
        if isinstance(exc, ConnectionRefusedError) or codes & REFUSED_CODES:
            return "refused", "the PC answered but nothing listens on that port", seconds
        if codes & UNREACHABLE_CODES:
            return "unreachable", "no route to that address (are you on the same network?)", seconds
        return "error", str(exc), seconds
    return "ok", "TCP connection established", time.time() - start


def main(argv):
    """Parse <ip> [port], run the check, print one line; exit code 0 only on success."""
    if len(argv) < 2 or len(argv) > 3:
        print("usage: python scripts/check_lan.py <ip> [port]")
        return 2
    ip = argv[1]
    try:
        port = int(argv[2]) if len(argv) == 3 else DEFAULT_PORT
    except ValueError:
        print("port must be a number")
        return 2
    verdict, detail, seconds = check(ip, port)
    print("%s:%d  %s  (%.0f ms)  %s" % (ip, port, verdict.upper(), seconds * 1000, detail))
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
