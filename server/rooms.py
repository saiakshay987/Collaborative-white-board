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
import logging
import secrets
import threading
import time
import traceback
from collections import OrderedDict

from common import config, protocol

# Debug-level log for failed sends (silent unless logging is configured).
_log = logging.getLogger(__name__)

# Room codes: 6 characters from A-Z and 2-9 without the look-alikes 0, O, 1, I
# (contract section B), so a code read aloud in the lab is unambiguous.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

ROLE_HOST = "host"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

# Server-side sanity limit for usernames (they appear in every envelope and
# in obj_ids "<user>-<n>", so keep them short).
MAX_USERNAME = 24

# USER_LEFT reasons (contract C1).
REASON_LEAVE = "leave"
REASON_TIMEOUT = "timeout"
REASON_KICKED = "kicked"

# Rooms are never deleted while the server runs: when the last member leaves,
# the room keeps its board, chat, versions and assets, so everybody can come
# back later with the room code and find everything as they left it (the old
# "delete an empty room after 10 minutes" rule is gone). Nothing on disk is
# touched either: the autosave file (contract E7, P3) is written every 10 s.


class JoinError(Exception):
    """A CREATE_ROOM / JOIN_ROOM request was refused.

    code is one of the contract ERROR codes (NO_ROOM, BAD_PASSWORD,
    NAME_TAKEN, BAD_FORMAT); text is a human-readable explanation. The
    presence handler turns it into ERROR {code, text, ref_type}.
    """

    def __init__(self, code, text):
        """Store the error code and text."""
        super().__init__(text)
        self.code = code
        self.text = text


def clean_username(value):
    """Validate a client-supplied username and return it stripped.

    Never trust the client: it must be a non-empty string of at most
    MAX_USERNAME printable characters. Raises JoinError(BAD_FORMAT).
    """
    if not isinstance(value, str):
        raise JoinError(protocol.BAD_FORMAT, "username must be a string")
    name = value.strip()
    if not name or len(name) > MAX_USERNAME or not name.isprintable():
        raise JoinError(protocol.BAD_FORMAT,
                        "username must be 1-%d printable characters" % MAX_USERNAME)
    return name


def clean_password(value):
    """Return the optional password as a str, or None when absent/empty.

    Raises JoinError(BAD_FORMAT) for a non-string password.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise JoinError(protocol.BAD_FORMAT, "password must be a string")
    return value


def clean_code(value):
    """Normalise a client-supplied room code (strip, upper-case).

    Raises JoinError(NO_ROOM) if it cannot be a valid code at all, so the
    caller does not even need to look it up.
    """
    code = value.strip().upper() if isinstance(value, str) else ""
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        raise JoinError(protocol.NO_ROOM, "no room with code %r" % (value,))
    return code

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
        # Why the member last left (REASON_*); read by the USER_LEFT hook.
        self.left_reason = None

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
        # Kept for code that checks it (versions.py autosave): rooms are no
        # longer deleted while the server runs, so this stays False.
        self.closed = False
        # Who is who, by username, for as long as the room exists. A seat is
        # deleted on LEAVE / kick / expiry, but the profile stays, so a user
        # who comes back with the same name gets the same color and role.
        # name -> {"color": '#rrggbb', "role": 'host'|'editor'|'viewer'}
        self.profiles = {}
        # Username of the room's host (the creator, or the first user to join
        # a room restored from disk). None until somebody has joined.
        self.host_name = None
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

    def remember(self, member):
        """Store member's color and role under its username (call under room.lock).

        Called whenever a seat goes away or offline, so the profile holds the
        LATEST role (the host may have changed it with SET_ROLE meanwhile).
        """
        with self.lock:
            self.profiles[member.name] = {"color": member.color, "role": member.role}

    def is_locked(self):
        """True if the room has a password (the room list shows a lock)."""
        return self._pw_hash is not None

    def summary(self):
        """One row of the room list (ROOM_LIST, contract C1): what a stranger may know about the room.

        Only counts and the host's name: no member names, no chat, no board
        content. `members` counts every username the room remembers (online
        or not), `online` those with a live connection right now. `updated`
        is the time of the last change that reached the autosave (the
        VersionStore, P3, keeps it); a room without one reports its creation.
        """
        with self.lock:
            names = set(self.profiles) | set(self.members)
            store = getattr(self, "versions", None)
            return {
                "code": self.code,
                "host": self.host_name,
                "online": len(self.online_members()),
                "members": len(names),
                "locked": self.is_locked(),
                "objects": len(self.board.objects) if self.board is not None else 0,
                "updated": store.updated_ts if store is not None else self.created_ts,
            }

    def has_host(self):
        """True if some seat in the room currently has the host role."""
        with self.lock:
            return any(m.role == ROLE_HOST for m in self.members.values())

    # -- sending -------------------------------------------------------------

    # The race these two methods avoid (check-then-use between threads):
    #
    # The old broadcast copied the member list under the lock, then, AFTER
    # releasing it, read m.session again to call send(). Between the check
    # ("m.session is not None") and the use ("m.session.send"), another
    # thread could run member_left() (a reader thread whose socket died, the
    # heartbeat timing a client out, a KICK) and set m.session = None, so
    # the broadcasting thread crashed with AttributeError: 'NoneType' has no
    # attribute 'send', and the members after it never got the message.
    #
    # Fix: read m.session exactly ONCE per member, under room.lock, into a
    # local snapshot of (name, session) pairs. The local variable cannot be
    # changed by another thread. The sending itself happens AFTER the lock
    # is released, because sendall() can block on a slow client whose TCP
    # window is full; holding room.lock that long would stall every other
    # thread of the room (all ops, joins and chats wait on that lock).
    # A snapshotted session may close meanwhile; that is fine: send() on a
    # closed session is a no-op, and any error is caught per recipient.

    def _snapshot_sessions(self, exclude=None):
        """Return [(name, session)] of the online members, read under room.lock.

        m.session is read once per member and None sessions are skipped, so
        the caller gets references that stay valid after the lock is gone.
        exclude: a member name to leave out.
        """
        with self.lock:
            targets = []
            for m in self.members.values():
                session = m.session  # read ONCE; do not re-read m.session later
                if m.online and session is not None and m.name != exclude:
                    targets.append((m.name, session))
            return targets

    def _safe_send(self, name, session, type, payload, env):
        """Send to one session; never raise. Return True if no error occurred.

        One dead or closing client must not abort the broadcast to the rest.
        The error is only logged at debug level: marking the member offline
        is the job of its reader thread / the heartbeat, which notice the
        broken socket themselves (and do it under room.lock).
        """
        try:
            session.send(type, payload, **env)
            return True
        except (OSError, AttributeError, ValueError) as e:
            _log.debug("send %s to %s in room %s failed: %r", type, name, self.code, e)
            return False

    def broadcast(self, type, payload, exclude=None, **env):
        """Send one message to every online member of the room.

        exclude: a member name to skip (e.g. the sender of a CURSOR).
        env may carry op_id, seq, user; room and ts are filled in by
        session.send. Board ops are broadcast INCLUDING the sender (echo
        rule, contract C2), so callers simply do not pass exclude there.
        The targets are snapshotted under room.lock, then sent to outside
        it (see the race comment above).
        """
        for name, session in self._snapshot_sessions(exclude):
            # A fresh dict per recipient: session.send stamps room/ts into env.
            self._safe_send(name, session, type, payload, dict(env))

    def send_to(self, name, type, payload, **env):
        """Send one message to the member called name, if online.

        Returns True if it was handed to the socket, False otherwise.
        Same pattern as broadcast: session read once under the lock, sent
        outside it.
        """
        with self.lock:
            m = self.members.get(name)
            session = m.session if m is not None else None  # read ONCE
            if m is None or not m.online or session is None:
                return False
        return self._safe_send(name, session, type, payload, env)


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

    def summaries(self):
        """Return the summary() of every room, most recently updated first (for ROOM_LIST).

        The room list is copied under manager.lock and each summary is taken
        under that room's own lock afterwards, so this never holds two room
        locks at once and cannot deadlock with join() or sweep().
        """
        with self.lock:
            room_list = list(self.rooms.values())
        rows = [room.summary() for room in room_list]
        rows.sort(key=lambda row: row["updated"], reverse=True)
        return rows

    def get(self, code):
        """Return the room with this code, or None."""
        with self.lock:
            return self.rooms.get(code)

    def add_member(self, room, name, role, session, color=None):
        """Create a new online Member in room and return it.

        The caller (join) has already checked name and password. color is a
        remembered color for a returning user; None picks the next free one.
        """
        with room.lock:
            member = Member(name, role, color or room.next_color(), session)
            room.members[name] = member
            room.remember(member)
            return member

    def purge_expired(self, room):
        """Drop offline seats whose rejoin window (config.REJOIN_WINDOW_S) is over.

        Their on_member_left hooks already ran when they went offline, so
        the seat is simply removed; the name becomes free again. The profile
        (color and role by username) is refreshed first, because the host may
        have changed the role of an offline member in the meantime.
        """
        now = time.time()
        with room.lock:
            for name, m in list(room.members.items()):
                if not m.online and m.offline_since is not None \
                        and now - m.offline_since > config.REJOIN_WINDOW_S:
                    room.remember(m)
                    del room.members[name]

    def sweep(self, now=None):
        """Housekeeping, called every tick by the heartbeat thread (presence.py).

        Drops, in every room, the offline seats whose rejoin window is over.
        Rooms themselves are NEVER deleted: an empty room keeps its board,
        chat, versions and assets for as long as the server runs, so a user
        can leave and come back later. Returns the list of deleted room
        codes, which is always empty (kept so callers need no change).

        Lock order is always manager.lock, then room.lock (join() never
        takes manager.lock while holding room.lock), so this cannot deadlock.
        """
        with self.lock:
            room_list = list(self.rooms.values())
        for room in room_list:
            with room.lock:
                self.purge_expired(room)
        return []

    def join(self, room, name, session, password=None, token=None):
        """Seat `session` in room as `name`; return (member, rejoin).

        Decision order, all under room.lock so two clients racing for the
        same name cannot both win:
          1. A seat with this name exists and the token matches it: this is
             a reconnect (contract E4). Resume the seat (same role, color,
             token); any stale connection still attached to it is closed.
          2. Wrong or missing password: JoinError BAD_PASSWORD.
          3. A seat with this name exists and its owner is ONLINE: JoinError
             NAME_TAKEN.
          4. A seat with this name exists but is OFFLINE (the connection
             dropped and the client lost its token, e.g. after the 60 s
             auto-reconnect gave up): the user is coming back, resume it.
             The password was checked in step 2.
          5. Otherwise a new seat. A name the room has seen before gets its
             remembered color and role back (room.profiles); the room's host
             always gets host back when nobody else holds it. A brand-new
             name is editor, and the very first user of a room is host.
        """
        with room.lock:
            # 0. Deleted room (never happens now, kept as a safety net).
            if room.closed:
                raise JoinError(protocol.NO_ROOM, "room %s no longer exists" % room.code)
            self.purge_expired(room)
            existing = room.members.get(name)

            # 1. Reconnect with token. compare_digest avoids timing leaks.
            if existing is not None and isinstance(token, str)                     and secrets.compare_digest(existing.token, token):
                old = existing.session
                self.resume_member(room, existing, session)
                if old is not None and old is not session:
                    old.close()  # its reader thread sees member.session changed: no USER_LEFT
                return existing, True

            # 2. Password (a valid token above already proved membership).
            if not room.check_password(password):
                raise JoinError(protocol.BAD_PASSWORD, "wrong room password")

            # 3. Name must be unique among the members that are online.
            if existing is not None and existing.online:
                raise JoinError(protocol.NAME_TAKEN, "the name %r is already in this room" % name)

            # 4. Same name, seat offline: take the old seat back.
            if existing is not None:
                self.resume_member(room, existing, session)
                return existing, True

            # 5. New seat, with the remembered color and role if we know the name.
            profile = room.profiles.get(name)
            if profile is not None:
                role, color = profile["role"], profile["color"]
            else:
                role = ROLE_HOST if room.host_name is None else ROLE_EDITOR
                color = None
            if room.host_name is None:
                room.host_name = name
            elif name == room.host_name and not room.has_host():
                role = ROLE_HOST  # the original host gets host back
            return self.add_member(room, name, role, session, color), False

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

    def member_left(self, room, member, remove=False, reason=REASON_TIMEOUT):
        """Mark member offline (or remove it) and run on_member_left hooks.

        remove=True deletes the seat (clean LEAVE, kick); otherwise the seat
        is kept for config.REJOIN_WINDOW_S. reason (REASON_*) is stored on
        member.left_reason so hooks (presence's USER_LEFT) can report it
        without changing the contract's hook signature fn(room, member).
        """
        with room.lock:
            member.left_reason = reason
            member.session = None
            if member.online:
                member.online = False
                member.offline_since = time.time()
            room.remember(member)  # color and role survive the seat
            if remove:
                room.members.pop(member.name, None)
            _run_hooks(_member_left_hooks, room, member)


# The one RoomManager of the server process.
manager = RoomManager()
