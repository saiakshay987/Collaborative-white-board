# OWNER: P1
"""protocol.py: Message type names, envelope builder and op_id generator.

Contract sections B and C. Every message type and envelope field used by the
app is defined here; use these constants instead of string literals so a typo
fails at import time instead of silently on the wire.
"""

import itertools
import threading

# Envelope version (contract section B: "v: always 1").
VERSION = 1

# --- C1. Session and presence (P1) -------------------------------------------
CREATE_ROOM = "CREATE_ROOM"
JOIN_ROOM = "JOIN_ROOM"
JOIN_OK = "JOIN_OK"
LEAVE = "LEAVE"
USER_LIST = "USER_LIST"
USER_JOINED = "USER_JOINED"
USER_LEFT = "USER_LEFT"
PING = "PING"
PONG = "PONG"
STATS = "STATS"
CURSOR = "CURSOR"
KICK = "KICK"
ERROR = "ERROR"

# --- C2. Board operations (P3 server side) -----------------------------------
OP_ADD = "OP_ADD"
OP_STROKE_PART = "OP_STROKE_PART"
OP_UPDATE = "OP_UPDATE"
OP_DELETE = "OP_DELETE"
OP_CLEAR = "OP_CLEAR"
UNDO = "UNDO"
REDO = "REDO"

# --- C3. State, history and images (P3) --------------------------------------
SNAPSHOT = "SNAPSHOT"
SYNC_REQUEST = "SYNC_REQUEST"
SYNC_DELTA = "SYNC_DELTA"
VERSION_SAVE = "VERSION_SAVE"
VERSION_LIST = "VERSION_LIST"
VERSIONS = "VERSIONS"
VERSION_RESTORE = "VERSION_RESTORE"
IMG_BEGIN = "IMG_BEGIN"
IMG_CHUNK = "IMG_CHUNK"
IMG_END = "IMG_END"
IMG_OK = "IMG_OK"
ASSET_GET = "ASSET_GET"

# --- C4. Chat and access (P4) ------------------------------------------------
CHAT = "CHAT"
DM = "DM"
CHAT_HISTORY = "CHAT_HISTORY"
SET_ROLE = "SET_ROLE"

# --- C1 (Revision 5). Room list, allowed before joining -----------------------
ROOM_LIST_REQUEST = "ROOM_LIST_REQUEST"
ROOM_LIST = "ROOM_LIST"

# Every valid message type. The server answers anything else with BAD_FORMAT.
MESSAGE_TYPES = frozenset([
    CREATE_ROOM, JOIN_ROOM, JOIN_OK, LEAVE, USER_LIST, USER_JOINED, USER_LEFT,
    PING, PONG, STATS, CURSOR, KICK, ERROR,
    OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE, OP_CLEAR, UNDO, REDO,
    SNAPSHOT, SYNC_REQUEST, SYNC_DELTA, VERSION_SAVE, VERSION_LIST, VERSIONS,
    VERSION_RESTORE, IMG_BEGIN, IMG_CHUNK, IMG_END, IMG_OK, ASSET_GET,
    CHAT, DM, CHAT_HISTORY, SET_ROLE,
    ROOM_LIST_REQUEST, ROOM_LIST,
])

# Messages that change the board: the client gives each an op_id, and the
# server gives each accepted one a seq (contract section B).
BOARD_OPS = frozenset([OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE, OP_CLEAR, UNDO, REDO])

# --- ERROR codes (contract section B) ----------------------------------------
BAD_FORMAT = "BAD_FORMAT"
NO_ROOM = "NO_ROOM"
BAD_PASSWORD = "BAD_PASSWORD"
NAME_TAKEN = "NAME_TAKEN"
FORBIDDEN = "FORBIDDEN"
TOO_LARGE = "TOO_LARGE"
NOT_FOUND = "NOT_FOUND"

ERROR_CODES = frozenset([BAD_FORMAT, NO_ROOM, BAD_PASSWORD, NAME_TAKEN, FORBIDDEN, TOO_LARGE, NOT_FOUND])

# --- Envelope ----------------------------------------------------------------

# Envelope fields other than type and payload, with their defaults.
# user/seq/ts are filled in by the SERVER; op_id by the client for BOARD_OPS.
ENVELOPE_DEFAULTS = {"v": VERSION, "room": None, "user": None, "op_id": None, "seq": None, "ts": 0.0}


def make_msg(type, payload=None, **env):
    """Build a complete envelope dict for message `type`.

    payload defaults to {}. env may set any envelope field (room, user,
    op_id, seq, ts, v); missing fields get ENVELOPE_DEFAULTS. Raises
    ValueError for an unknown type and TypeError for an unknown envelope
    field, so no one can put an off-contract message on the wire.
    """
    if type not in MESSAGE_TYPES:
        raise ValueError("unknown message type: %r" % (type,))
    unknown = set(env) - set(ENVELOPE_DEFAULTS)
    if unknown:
        raise TypeError("unknown envelope field(s): %s" % ", ".join(sorted(unknown)))
    msg = dict(ENVELOPE_DEFAULTS)
    msg.update(env)
    msg["type"] = type
    msg["payload"] = {} if payload is None else payload
    return msg


# --- op_id -------------------------------------------------------------------

# One process-wide counter. itertools.count is advanced under a lock so two
# threads can never receive the same number.
_op_counter = itertools.count(1)
_op_lock = threading.Lock()


def new_op_id(user):
    """Return a new unique operation id "<user>-<counter>", e.g. "alice-41".

    Used as op_id on board operations and as obj_id for new objects. The
    counter is shared by all callers in this process and is thread-safe.
    """
    with _op_lock:
        n = next(_op_counter)
    return "%s-%d" % (user, n)
