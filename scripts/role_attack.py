# OWNER: P4
"""role_attack.py: Test script: a hostile client that tries forbidden actions to prove the server enforces roles.

This is a RAW client: it speaks the wire protocol directly with
common/framing.py and common/protocol.py, with no GUI and no client-side
checks at all. That is the point: a real attacker would not use our GUI
either, so only the SERVER's permissions.can(role, action) can stop it.

Demo (start the server and a normal host client first):

    python scripts/role_attack.py --host 192.168.1.20 --room K7QX2M --name mallory

  1. The script joins the room. A new member is an editor.
  2. It waits for Enter. On the host's screen: Session > Manage roles, set
     "mallory" to viewer. Then press Enter here.
  3. It checks that the server really lists it as a viewer and sends nine
     actions a viewer must not do. Every ERROR FORBIDDEN is a green PASS;
     anything else (accepted, another error code, no answer) is a red FAIL.
  4. Control: a viewer MAY chat, so a CHAT must still be accepted. This shows
     the PASSes are real refusals and not a server that rejects everything.

Networking notes:
  * A reader thread receives every frame. It answers the server's PING with
    PONG at once, because the server closes a connection that is silent for
    config.TIMEOUT_S (15 s) and this script waits for Enter much longer
    than that. PING/PONG is the only thing it does on its own.
  * It also follows USER_LIST, so it knows its current role (the host's
    change arrives as a USER_LIST) and can warn if it is not a viewer yet.
  * Replies are matched by ERROR.ref_type (the type of the message that was
    refused), see contract section B.

Exit status: 0 if every attack was refused with FORBIDDEN, 1 otherwise.
"""

import argparse
import os
import queue
import socket
import sys
import threading
import time

# Allow `python scripts/role_attack.py` (the script folder is not the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import config, framing, protocol  # noqa: E402

# Seconds to wait for the server's answer to one message.
REPLY_WAIT_S = 1.5

# --- Colours -------------------------------------------------------------------


def _colours_enabled():
    """True if the terminal should get ANSI colours (not piped, NO_COLOR not set)."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # makes the Windows console understand ANSI escape codes
    return True


_COLOUR = _colours_enabled()


def paint(text, code):
    """Return text wrapped in the ANSI colour `code` (plain text if colours are off)."""
    return "\033[%sm%s\033[0m" % (code, text) if _COLOUR else text


def green(text):
    """Bold green text."""
    return paint(text, "1;32")


def red(text):
    """Bold red text."""
    return paint(text, "1;31")


def yellow(text):
    """Bold yellow text."""
    return paint(text, "1;33")


def dim(text):
    """Grey text for secondary information."""
    return paint(text, "2")


def bold(text):
    """Bold text."""
    return paint(text, "1")


# --- The hostile connection --------------------------------------------------------

class Attacker:
    """One raw TCP connection to the server, with a reader thread.

    The reader thread keeps the connection alive (PING -> PONG), tracks the
    role the server reports for this user, and puts every other frame on
    `inbox` for the main thread. It puts None on `inbox` when the
    connection ends.
    """

    def __init__(self, host, port, name):
        """Open the TCP connection to host:port; `name` is the username to join with."""
        self.name = name
        self.role = None
        self.room = None
        self.inbox = queue.Queue()
        self.closed = threading.Event()  # set by the reader when the connection ends
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(None)  # the reader thread blocks in recv()
        threading.Thread(target=self._reader, name="attacker-reader", daemon=True).start()

    def _reader(self):
        """Thread: read frames until the connection ends (see the module docstring)."""
        while True:
            try:
                msg = framing.recv_msg(self.sock)
            except (OSError, framing.ProtocolError):
                msg = None
            if msg is None:
                self.closed.set()
                self.inbox.put(None)
                return
            kind = msg.get("type")
            payload = msg.get("payload") or {}
            if kind == protocol.PING:
                # Answer from this thread: without it the server drops us after 15 s.
                self.send(protocol.PONG, {"t": payload.get("t")})
            elif kind == protocol.USER_LIST:
                for user in payload.get("users") or []:
                    if user.get("name") == self.name:
                        self.role = user.get("role")
            elif kind != protocol.STATS:
                self.inbox.put(msg)

    def send(self, msg_type, payload, op_id=None):
        """Send one message. framing.send_msg holds a per-socket lock, so the
        reader thread's PONG and the main thread's attacks cannot interleave."""
        framing.send_msg(self.sock, protocol.make_msg(msg_type, payload, room=self.room, op_id=op_id))

    def wait_for(self, accept, timeout):
        """Return the first inbox message for which accept(msg) is true.

        Other messages are skipped. Returns None on timeout and "closed" if
        the connection ended.
        """
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                msg = self.inbox.get(timeout=left)
            except queue.Empty:
                return None
            if msg is None:
                return "closed"
            if accept(msg):
                return msg

    def join(self, room, password):
        """JOIN_ROOM as a normal member; returns the role the server gave, or raises RuntimeError."""
        self.room = room
        payload = {"room": room, "username": self.name}
        if password:
            payload["password"] = password
        self.send(protocol.JOIN_ROOM, payload)
        reply = self.wait_for(lambda m: m["type"] in (protocol.JOIN_OK, protocol.ERROR), 5)
        if reply is None or reply == "closed":
            raise RuntimeError("no answer to JOIN_ROOM")
        if reply["type"] == protocol.ERROR:
            raise RuntimeError("join refused: %s (%s)" % (reply["payload"].get("code"),
                                                          reply["payload"].get("text")))
        self.role = reply["payload"].get("role")
        return self.role

    def close(self):
        """Close the connection."""
        try:
            self.sock.close()
        except OSError:
            pass


# --- The attacks ----------------------------------------------------------------------

def attack_list(me):
    """Return the attacks as (message type, payload, what it tries) tuples.

    Each payload is VALID for its message type, so the only thing that can
    stop it is the permission check, not payload validation. KICK names a
    user that does not exist, so a missing check cannot hurt anyone.
    """
    rect = {"id": "role-attack-rect", "kind": "rect", "x": 10, "y": 10, "w": 80, "h": 50,
            "fill": None, "color": "#ff0000", "width": 3}
    style = {"tool": "pen", "color": "#ff0000", "width": 3, "alpha": 1.0}
    return [
        (protocol.OP_ADD, {"obj": rect}, "draw a rectangle"),
        (protocol.OP_STROKE_PART,
         {"obj_id": "role-attack-stroke", "points": [[10, 10], [60, 80], [120, 40]],
          "done": True, "style": style}, "draw a stroke"),
        (protocol.OP_DELETE, {"obj_id": "role-attack-rect"}, "delete an object"),
        (protocol.OP_CLEAR, {}, "clear the whole board"),
        (protocol.UNDO, {}, "undo"),
        (protocol.VERSION_RESTORE, {"id": "v1"}, "restore an old version"),
        (protocol.SET_ROLE, {"user": me, "role": "host"}, "promote myself to host"),
        (protocol.KICK, {"user": "nobody"}, "kick a user"),
        (protocol.IMG_BEGIN, {"asset_id": "role-attack-img", "name": "evil.png",
                              "mime": "image/png", "size": 1024, "chunks": 1, "w": 10, "h": 10},
         "upload an image"),
    ]


def try_attack(att, msg_type, payload, op_id):
    """Send one forbidden message and judge the server's answer.

    Returns (passed, code, text): passed is True only for ERROR FORBIDDEN.
    """
    # Forget answers still queued from the previous attack.
    while not att.inbox.empty():
        att.inbox.get_nowait()
    att.send(msg_type, payload, op_id=op_id)

    def relevant(msg):
        """An ERROR about this message type, or the accepted message coming back."""
        if msg["type"] == protocol.ERROR:
            return (msg.get("payload") or {}).get("ref_type") == msg_type
        return msg["type"] == msg_type

    reply = att.wait_for(relevant, REPLY_WAIT_S)
    if reply == "closed":
        return False, "CLOSED", "the server closed the connection"
    if reply is None:
        return False, "NO REPLY", "no ERROR within %.1f s: it may have been accepted" % REPLY_WAIT_S
    if reply["type"] != protocol.ERROR:
        return False, "ACCEPTED", "the server accepted a forbidden action!"
    code = reply["payload"].get("code")
    return code == protocol.FORBIDDEN, code, reply["payload"].get("text") or ""


def run_attacks(att):
    """Run every attack and the control, printing one line each; return the number of failures."""
    print(bold("Attacks (a viewer must be refused with FORBIDDEN)"))
    failures = 0
    for number, (msg_type, payload, what) in enumerate(attack_list(att.name), start=1):
        op_id = "role-attack-%d" % number if msg_type in protocol.BOARD_OPS else None
        passed, code, text = try_attack(att, msg_type, payload, op_id)
        failures += 0 if passed else 1
        tag = green("PASS") if passed else red("FAIL")
        reason = dim(text) if passed else yellow(text)
        print("  %s  %-16s %-11s %s" % (tag, msg_type, code, reason))
        print("        %s" % dim("tried to " + what))
        time.sleep(0.05)

    print()
    print(bold("Control (a viewer MAY chat, so this must be accepted)"))
    while not att.inbox.empty():
        att.inbox.get_nowait()
    att.send(protocol.CHAT, {"text": "role_attack.py: viewers can still chat"})
    reply = att.wait_for(lambda m: m["type"] in (protocol.CHAT, protocol.ERROR), REPLY_WAIT_S)
    if isinstance(reply, dict) and reply["type"] == protocol.CHAT:
        print("  %s  %-16s %s" % (green("OK  "), "CHAT", dim("accepted, as it should be")))
    else:
        failures += 1
        print("  %s  %-16s %s" % (red("FAIL"), "CHAT", yellow("a viewer should be allowed to chat")))
    return failures


# --- Main ---------------------------------------------------------------------------------

def ask(prompt, default=None):
    """Read a line from the user; an empty answer gives `default`."""
    shown = "%s%s: " % (prompt, " [%s]" % default if default else "")
    answer = input(shown).strip()
    return answer or default


def main():
    """Join, wait for the host to demote us, attack, print the verdict; return the exit status."""
    parser = argparse.ArgumentParser(description="Hostile client: proves the server enforces roles.")
    parser.add_argument("--host", help="server IP (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--room", help="room code")
    parser.add_argument("--name", default="mallory", help="username to join with")
    parser.add_argument("--password", default=None, help="room password, if it has one")
    args = parser.parse_args()
    host = args.host or ask("Server IP", "127.0.0.1")
    room = (args.room or ask("Room code")).strip().upper()

    print()
    print(bold("ROLE ATTACK: a hostile client against the server's role checks"))
    print(dim("server %s:%d   room %s   name %r" % (host, args.port, room, args.name)))
    try:
        att = Attacker(host, args.port, args.name)
        role = att.join(room, args.password)
    except (OSError, RuntimeError) as exc:
        print(red("Could not join: %s" % exc))
        return 1
    print("Joined as %s." % bold(role))

    print()
    print(yellow("On the HOST's screen: Session > Manage roles, set %r to viewer." % args.name))
    try:
        input("Press Enter here when you have done that... ")
    except (EOFError, KeyboardInterrupt):
        att.close()
        return 1
    time.sleep(0.3)  # let the USER_LIST that carries the new role arrive

    if att.closed.is_set():
        print(red("The server closed the connection while we waited."))
        return 1
    print("Server says my role is now: %s" % bold(att.role))
    if att.role != "viewer":
        print(red("I am not a viewer, so the results below do not prove anything."))
        try:
            if input("Run the attacks anyway? [y/N] ").strip().lower() != "y":
                att.close()
                return 1
        except (EOFError, KeyboardInterrupt):
            att.close()
            return 1
    print()

    failures = run_attacks(att)
    att.close()

    print()
    total = len(attack_list(att.name))
    if failures == 0:
        print(green("ALL %d ATTACKS REFUSED: the server enforces roles." % total))
        return 0
    print(red("%d CHECK(S) FAILED: see the lines marked FAIL above." % failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
