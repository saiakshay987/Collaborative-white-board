# OWNER: P1
"""rooms.py: Room, Member and RoomManager, plus the room lifecycle hooks.

Contract section F (server).

Concurrency model: every client has its own reader thread on the server, so
several threads can touch the same room at the same time. Each Room has one
re-entrant lock, `room.lock` (threading.RLock). Every change of room state
(members, board, chat, ...) happens while holding it, which serialises the
room: operations are processed one at a time and the seq order is the truth
(contract E1). It is an RLock so a handler holding the lock can call helpers
(broadcast, hooks) that take it again without deadlocking.

Hooks let other owners react to the room lifecycle without editing this
file. They run in registration order, i.e. in server/bootstrap.py import
order (presence, then board/sync of P3, then chat of P4):

    rooms.on_room_created(fn(room))
    rooms.on_member_joined(fn(room, member, rejoin, last_seq))
    rooms.on_member_left(fn(room, member))
"""

import hashlib
import secrets
import threading
import time
import traceback
from collections import OrderedDict

from common import config

# Room codes: 6 characters from A-Z and 2-9 without the look-alikes 0, O, 1, I
# (contract section B), so a code read aloud in the lab is unambiguous.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

ROLE_HOST = "host"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

# --- Hook registries ---------------------------------------------------------

_room_created_hooks = []
_member_joined_hooks = []
_member_left_hooks = []


def on_room_created(fn):
    """Register fn(room), called once right after a room is created.

    P3 uses it to attach room.board / room.assets, P4 to attach room.chat.
    Returns fn so it can be used as a decorator.
    """
    _room_created_hooks.append(fn)
    return fn


def on_member_joined(fn):
    """Register fn(room, member, rejoin, last_seq), called after JOIN_OK was sent.

    rejoin is True when an offline member came back inside the rejoin
    window; last_seq is the client's last seen seq (None on a first join).
    Returns fn so it can be used as a decorator.
    """
    _member_joined_hooks.append(fn)
    return fn


def on_member_left(fn):
    """Register fn(room, member), called when a member leaves or goes offline.

    Returns fn so it can be used as a decorator.
    """
    _member_left_hooks.append(fn)
    return fn


def _run_hooks(hooks, *args):
    """Call every hook with args, in order. One failing hook is logged and
    does not stop the others (a P4 bug must not break P3's snapshot)."""
    for fn in list(hooks):
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


# --- Member ------------------------------------------------------------------

class Member:
    """One user seat in a room.

    The seat outlives the TCP connection: when the connection drops, the
    member is marked offline (session None) and kept for
    config.REJOIN_WINDOW_S so a reconnect with the same token resumes it.
    """

    def __init__(self, name, role, color, session=None):
        """Create a member; a random token identifies it on reconnect."""
        self.name = name
        self.role = role
        self.color = color
        self.token = secrets.token_hex(16)
        self.session = session
        self.online = session is not None
        self.offline_since = None if self.online else time.time()

    def public(self):
        """Return the member as it appears in USER_LIST (no token!)."""
        return {"name": self.name, "color": self.color, "role": self.role, "online": self.online}


# --- Room --------------------------------------------------------------------

class Room:
    """One whiteboard room: members plus feature state attached by hooks."""

    def __init__(self, code, password=None):
        """Create an empty room. A password is kept only as salted sha256."""
        self.code = code
        self.members = OrderedDict()  # name -> Member, in join order
        self.lock = threading.RLock()
        self.created_ts = time.time()
        self.board = None   # attached by P3 (server/board_state.py)
        self.chat = None    # attached by P4 (server/chat.py)
        self.assets = None  # attached by P3 (server/assets.py)
        self._color_index = 0
        self._salt = secrets.token_bytes(16)
        self._pw_hash = self._hash(password) if password else None

    # -- password ------------------------------------------------------------

    def _hash(self, password):
        """Return sha256(salt + password) as hex."""
        return hashlib.sha256(self._salt + password.encode("utf-8")).hexdigest()

    def check_password(self, password):
        """Return True if the room has no password or password matches.

        secrets.compare_digest compares in constant time.
        """
        if self._pw_hash is None:
            return True
        if not isinstance(password, str):
            return False
        return secrets.compare_digest(self._pw_hash, self._hash(password))

    # -- members -------------------------------------------------------------

    def next_color(self):
        """Return the next color from config.USER_COLORS, in join order."""
        with self.lock:
            color = config.USER_COLORS[self._color_index % len(config.USER_COLORS)]
            self._color_index += 1
            return color

    def online_members(self):
        """Return a list of members that currently have a connection."""
        with self.lock:
            return [m for m in self.members.values() if m.online and m.session is not None]

    # -- sending -------------------------------------------------------------

    def broadcast(self, type, payload, exclude=None, **env):
        """Send one message to every online member of the room.

        exclude: a member name to skip (e.g. the sender of a CURSOR).
        env may carry op_id, seq, user; room and ts are filled in by
        session.send. Board ops are broadcast INCLUDING the sender (echo
        rule, contract C2), so callers simply do not pass exclude there.
        The member list is copied under the lock, so sending is safe while
        other threads join or leave.
        """
        for m in self.online_members():
            if m.name != exclude:
                m.session.send(type, payload, **env)

    def send_to(self, name, type, payload, **env):
        """Send one message to the member called name, if online.

        Returns True if it was handed to the socket, False otherwise.
        """
        with self.lock:
            m = self.members.get(name)
            session = m.session if m is not None and m.online else None
        if session is None:
            return False
        session.send(type, payload, **env)
        return True


# --- RoomManager -------------------------------------------------------------

class RoomManager:
    """All rooms of this server, keyed by code.

    Its own lock protects only the rooms dict; each room's state is
    protected by that room's lock.
    """

    def __init__(self):
        """Start with no rooms."""
        self.rooms = {}
        self.lock = threading.RLock()

    def _new_code(self):
        """Return a random room code that is not in use yet."""
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self.rooms:
                return code

    def create_room(self, password=None):
        """Create, register and return a new Room, then run on_room_created hooks."""
        with self.lock:
            room = Room(self._new_code(), password)
            self.rooms[room.code] = room
        with room.lock:
            _run_hooks(_room_created_hooks, room)
        return room

    def get(self, code):
        """Return the room with this code, or None."""
        with self.lock:
            return self.rooms.get(code)

    def add_member(self, room, name, role, session):
        """Create a new online Member in room and return it.

        The caller (presence) has already checked name and password and
        holds room.lock.
        """
        with room.lock:
            member = Member(name, role, room.next_color(), session)
            room.members[name] = member
            return member

    def resume_member(self, room, member, session):
        """Attach a new connection to an existing (offline) member seat."""
        with room.lock:
            member.session = session
            member.online = True
            member.offline_since = None
            return member

    def member_joined(self, room, member, rejoin=False, last_seq=None):
        """Run on_member_joined hooks. Call this AFTER JOIN_OK was sent."""
        with room.lock:
            _run_hooks(_member_joined_hooks, room, member, rejoin, last_seq)

    def member_left(self, room, member, remove=False):
        """Mark member offline (or remove it) and run on_member_left hooks.

        remove=True deletes the seat (clean LEAVE, kick, rejoin window over);
        otherwise the seat is kept for config.REJOIN_WINDOW_S.
        """
        with room.lock:
            member.session = None
            if member.online:
                member.online = False
                member.offline_since = time.time()
            if remove:
                room.members.pop(member.name, None)
            _run_hooks(_member_left_hooks, room, member)


# The one RoomManager of the server process.
manager = RoomManager()
