# OWNER: P1
"""p1_test_rejoin.py: End-to-end test of leaving a room and joining it again.

Run from the project root:  python scripts/p1_test_rejoin.py

It starts a real server on a free port (data folder: a temp dir, so
server_data/ is untouched) and talks to it with plain sockets, like the real
client does. Scenario:

  1. alice creates a room, draws and chats; bob joins, draws and chats.
  2. alice sends LEAVE. bob keeps drawing and chatting. The room stays.
  3. alice joins again with the same name (no token): she gets the same color
     and role (host), and the messages arrive in the contract order
     JOIN_OK, USER_LIST, SNAPSHOT, CHAT_HISTORY, VERSIONS. The snapshot holds
     bob's new objects and the chat history holds every message, once.
  4. The host leaves while bob stays; bob is not promoted; the host returns
     and is host again.
  5. Everybody leaves: the room is still there, with its board and chat.
  6. A connection that just drops (no LEAVE) and comes back without its token
     takes its old seat back instead of getting NAME_TAKEN.
  7. Two online users may not share a name (NAME_TAKEN).
"""

import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import zlib

# Make "import common" work when run as a plain script from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["WB_DATA_DIR"] = tempfile.mkdtemp(prefix="wb_rejoin_")  # before server.versions loads

import server.bootstrap  # noqa: E402,F401  (registers handlers and hooks)
from common import framing, protocol  # noqa: E402
from server import rooms  # noqa: E402
from server import server as server_mod  # noqa: E402

FAILS = []
NOISE = (protocol.PING, protocol.STATS)

# The order a joining client must see (contract C1).
JOIN_ORDER = [protocol.JOIN_OK, protocol.USER_LIST, protocol.SNAPSHOT,
              protocol.CHAT_HISTORY, protocol.VERSIONS]


def check(name, ok, detail=""):
    """Print one result line and remember a failure."""
    print("%-4s %s %s" % ("ok" if ok else "FAIL", name, "" if ok else detail))
    if not ok:
        FAILS.append(name)


class Client:
    """A bare-bones client: one socket, frames in, frames out."""

    def __init__(self, port):
        """Connect to the test server."""
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.n = 0

    def send(self, type, payload=None, **env):
        """Send one envelope."""
        framing.send_msg(self.sock, protocol.make_msg(type, payload or {}, **env))

    def recv(self):
        """Next frame that is not PING/STATS, answering PING; None on close or timeout."""
        while True:
            try:
                msg = framing.recv_msg(self.sock)
            except (socket.timeout, OSError):
                return None
            if msg is None:
                return None
            if msg["type"] == protocol.PING:
                framing.send_msg(self.sock, protocol.make_msg(protocol.PONG, {"t": msg["payload"]["t"]}))
            if msg["type"] not in NOISE:
                return msg

    def until(self, type):
        """Collect frames up to and including the first of `type`; returns the list."""
        out = []
        while True:
            msg = self.recv()
            if msg is None:
                return out
            out.append(msg)
            if msg["type"] == type:
                return out

    def drain(self, quiet=0.4):
        """Read everything that arrives until the line is quiet for `quiet` seconds."""
        self.sock.settimeout(quiet)
        out = []
        while True:
            msg = self.recv()
            if msg is None:
                break
            out.append(msg)
        self.sock.settimeout(5)
        return out

    def op(self, type, payload):
        """Send a board operation with an op_id."""
        self.n += 1
        self.send(type, payload, op_id="%s-%d" % (id(self) % 1000, self.n))

    def close(self):
        """Close the socket (no LEAVE)."""
        try:
            self.sock.close()
        except OSError:
            pass


def rect_obj(oid, x):
    """A valid rectangle object for OP_ADD."""
    return {"id": oid, "kind": "rect", "x": x, "y": 40, "w": 80, "h": 60,
            "color": "#ff0000", "width": 2, "fill": None}


def snapshot_ids(frames):
    """Object ids inside the SNAPSHOT of a frame list."""
    for f in frames:
        if f["type"] == protocol.SNAPSHOT:
            raw = zlib.decompress(base64.b64decode(f["payload"]["data"]))
            return {o["id"] for o in json.loads(raw)["objects"]}
    return None


def join(port, room, name, token=None):
    """Open a connection and JOIN_ROOM; returns (client, frames up to VERSIONS or the ERROR)."""
    c = Client(port)
    payload = {"room": room, "username": name}
    if token:
        payload["token"] = token
    c.send(protocol.JOIN_ROOM, payload)
    first = c.recv()
    if first is None or first["type"] != protocol.JOIN_OK:
        return c, [first]
    return c, [first] + c.until(protocol.VERSIONS)


def main():
    """Run the scenario against a real in-process server."""
    srv = server_mod.Server("127.0.0.1", 0)
    srv.start()
    port = srv.sock.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        scenario(port)
    finally:
        srv.stop()
    print("\nFAILED: %s" % ", ".join(FAILS) if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


def scenario(port):
    """The seven steps of the module docstring."""
    # 1. alice hosts, bob joins; both draw and chat.
    alice = Client(port)
    alice.send(protocol.CREATE_ROOM, {"username": "alice"})
    frames = alice.until(protocol.VERSIONS)
    room = frames[0]["payload"]["room"]
    a_color, a_role = frames[0]["payload"]["color"], frames[0]["payload"]["role"]
    check("1 first join order", [f["type"] for f in frames] == JOIN_ORDER,
          [f["type"] for f in frames])
    check("1 creator is host", a_role == "host")
    alice.op(protocol.OP_ADD, {"obj": rect_obj("alice-1", 100)})
    alice.send(protocol.CHAT, {"text": "hello from alice"})
    bob, frames = join(port, room, "bob")
    b_color = frames[0]["payload"]["color"]
    check("1 bob is editor", frames[0]["payload"]["role"] == "editor")
    bob.op(protocol.OP_ADD, {"obj": rect_obj("bob-1", 300)})
    bob.send(protocol.CHAT, {"text": "hello from bob"})
    alice.drain()
    bob.drain()

    # 2. alice leaves; bob keeps going.
    alice.send(protocol.LEAVE)
    time.sleep(0.3)
    alice.close()
    bob.op(protocol.OP_ADD, {"obj": rect_obj("bob-2", 500)})
    bob.send(protocol.CHAT, {"text": "alice is gone"})
    bob.drain()

    # 3. alice joins again, without a token.
    alice, frames = join(port, room, "alice")
    check("3 rejoin order", [f["type"] for f in frames] == JOIN_ORDER, [f["type"] for f in frames])
    ok = frames[0]["payload"]
    check("3 same color", ok["color"] == a_color, "%s vs %s" % (ok["color"], a_color))
    check("3 host again", ok["role"] == "host", ok["role"])
    check("3 snapshot has everything", snapshot_ids(frames) == {"alice-1", "bob-1", "bob-2"},
          snapshot_ids(frames))
    texts = [m["text"] for f in frames if f["type"] == protocol.CHAT_HISTORY
             for m in f["payload"]["messages"]]
    check("3 chat history complete, once",
          texts == ["hello from alice", "hello from bob", "alice is gone"], texts)
    users = {u["name"]: u for u in frames[1]["payload"]["users"]}
    check("3 bob still editor with his color",
          users["bob"]["role"] == "editor" and users["bob"]["color"] == b_color)
    alice.drain()
    bob.drain()

    # 4. the host leaves while bob stays.
    alice.send(protocol.LEAVE)
    time.sleep(0.3)
    alice.close()
    left = [f for f in bob.drain() if f["type"] == protocol.USER_LIST]
    check("4 bob is not promoted", all(
        next(u for u in f["payload"]["users"] if u["name"] == "bob")["role"] == "editor" for f in left))
    bob.op(protocol.OP_ADD, {"obj": rect_obj("bob-3", 700)})
    bob.drain()
    alice, frames = join(port, room, "alice")
    check("4 host gets host back", frames[0]["payload"]["role"] == "host")
    check("4 snapshot has bob's work done meanwhile", "bob-3" in (snapshot_ids(frames) or ()),
          snapshot_ids(frames))
    alice.drain()

    # 5. everybody leaves: the room stays.
    for c in (alice, bob):
        c.send(protocol.LEAVE)
    time.sleep(0.3)
    alice.close()
    bob.close()
    rooms.manager.sweep(time.time() + 24 * 3600)   # a day later
    check("5 empty room is kept", rooms.manager.get(room) is not None)
    carol, frames = join(port, room, "carol")
    check("5 newcomer sees the old board",
          snapshot_ids(frames) == {"alice-1", "bob-1", "bob-2", "bob-3"}, snapshot_ids(frames))
    check("5 newcomer is editor, not host", frames[0]["payload"]["role"] == "editor")
    carol.send(protocol.LEAVE)
    time.sleep(0.2)
    carol.close()

    # 6. a dropped connection, rejoin without the token.
    alice, frames = join(port, room, "alice")
    alice.close()                       # no LEAVE: the server sees a dropped connection
    time.sleep(0.5)
    alice, frames = join(port, room, "alice")
    check("6 offline seat is taken back", frames[0]["type"] == protocol.JOIN_OK
          and frames[0]["payload"]["role"] == "host",
          frames[0]["payload"] if frames[0] else None)

    # 7. a name that is online is taken.
    dup, frames = join(port, room, "alice")
    check("7 online name is refused", frames[0] is not None and frames[0]["type"] == protocol.ERROR
          and frames[0]["payload"]["code"] == protocol.NAME_TAKEN, frames[0])
    for c in (alice, dup):
        c.close()


if __name__ == "__main__":
    sys.exit(main())
