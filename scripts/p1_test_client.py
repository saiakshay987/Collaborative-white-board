# OWNER: P1
"""p1_test_client.py: Terminal test client: create or join a room and print every message.

Run from the project root (server must be running):

    python scripts/p1_test_client.py create alice [--password pw]
    python scripts/p1_test_client.py join   bob   --room K7QX2M [--password pw]
    python scripts/p1_test_client.py join   alice --room K7QX2M --token <hex> [--last-seq N]

Options: --host (default 127.0.0.1), --port (default config.PORT).

A background thread prints every received frame (type, envelope fields that
are set, and the payload). It also answers PING with PONG so the client is
not timed out once the heartbeat exists. Commands typed on stdin:

    leave                      send LEAVE (connection stays open)
    send TYPE {json payload}   send any contract message, e.g.
                               send CHAT {"text": "hi"}
    quit                       close the connection and exit (no LEAVE: the
                               server sees a dropped connection)
"""

import argparse
import json
import os
import socket
import sys
import threading

# Make "import common" work when run as a plain script from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, framing, protocol  # noqa: E402


def show(msg):
    """Print one received envelope on a single readable line."""
    extras = ["%s=%s" % (k, msg.get(k)) for k in ("user", "op_id", "seq") if msg.get(k) is not None]
    print("<<< %-11s %s %s" % (msg.get("type"), " ".join(extras),
                               json.dumps(msg.get("payload"), ensure_ascii=False)), flush=True)


def reader(sock, stop):
    """Receive frames until the connection ends; print each one.

    PING is answered with PONG {t} (contract C1) so the server's RTT and
    timeout logic sees a live client.
    """
    try:
        while True:
            msg = framing.recv_msg(sock)
            if msg is None:
                if not stop.is_set():  # not our own quit
                    print("*** server closed the connection", flush=True)
                break
            show(msg)
            if msg.get("type") == protocol.PING:
                framing.send_msg(sock, protocol.make_msg(protocol.PONG, {"t": msg["payload"].get("t")}))
    except (OSError, framing.ProtocolError) as exc:
        if not stop.is_set():
            print("*** connection error: %s" % exc, flush=True)
    finally:
        stop.set()


def first_message(args):
    """Build the CREATE_ROOM or JOIN_ROOM envelope from the command line."""
    payload = {"username": args.username}
    if args.password:
        payload["password"] = args.password
    if args.action == "create":
        return protocol.make_msg(protocol.CREATE_ROOM, payload)
    payload["room"] = args.room
    if args.token:
        payload["token"] = args.token
    if args.last_seq is not None:
        payload["last_seq"] = args.last_seq
    return protocol.make_msg(protocol.JOIN_ROOM, payload)


def command_loop(sock, stop):
    """Read commands from stdin and send them until quit / EOF / disconnect."""
    for line in sys.stdin:
        if stop.is_set():
            break
        line = line.strip()
        if not line:
            continue
        try:
            if line == "quit":
                break
            if line == "leave":
                framing.send_msg(sock, protocol.make_msg(protocol.LEAVE, {}))
            elif line.startswith("send "):
                parts = line.split(None, 2)
                payload = json.loads(parts[2]) if len(parts) > 2 else {}
                framing.send_msg(sock, protocol.make_msg(parts[1], payload))
            else:
                print("commands: leave | send TYPE {json} | quit", flush=True)
        except (ValueError, TypeError) as exc:
            print("*** not sent: %s" % exc, flush=True)
        except OSError as exc:
            print("*** send failed: %s" % exc, flush=True)
            break


def main():
    """Parse arguments, connect, send CREATE/JOIN, then run reader + command loop."""
    ap = argparse.ArgumentParser(description="Whiteboard terminal test client")
    ap.add_argument("action", choices=["create", "join"])
    ap.add_argument("username")
    ap.add_argument("--room", help="room code (join)")
    ap.add_argument("--password")
    ap.add_argument("--token", help="token from an earlier JOIN_OK (reconnect)")
    ap.add_argument("--last-seq", type=int)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args()
    if args.action == "join" and not args.room:
        ap.error("join needs --room CODE")

    sock = socket.create_connection((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("*** connected to %s:%d" % (args.host, args.port), flush=True)

    stop = threading.Event()
    threading.Thread(target=reader, args=(sock, stop), daemon=True).start()
    framing.send_msg(sock, first_message(args))
    try:
        command_loop(sock, stop)
    except KeyboardInterrupt:
        pass
    stop.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


if __name__ == "__main__":
    main()
