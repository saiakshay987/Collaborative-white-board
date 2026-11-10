# OWNER: P3
"""versions.py: Named board versions (in memory) and autosave to disk (CONTRACT E6 and E7).

Two jobs live in this file.

1. Versions. Every room gets `room.versions`, a VersionStore. board_ops calls
   auto_version() just before it applies OP_CLEAR, so the host can always get
   the board back (OP_CLEAR itself is not undoable, contract E6). Versions are
   kept IN MEMORY only for now: each one is a deep copy of the objects. The
   VERSION_SAVE / VERSION_LIST / VERSION_RESTORE handlers are a later step.

2. Autosave (contract E7). One background thread wakes up every
   AUTOSAVE_INTERVAL_S (10 s) and writes every room that changed to

       server_data/<ROOM>/autosave.json   {format, room, seq, saved_ts, objects, versions}

   `versions` holds the METADATA of the versions only ({id, name, by, ts, seq,
   count}); their object lists stay in memory. The file is written ATOMICALLY:
   the JSON goes to a temporary file in the same folder, is flushed to disk,
   and os.replace() then swaps it in. A crash or power cut therefore leaves
   either the complete old file or the complete new one, never a half-written
   file.

   A room is also saved when it closes: when its last online member leaves
   (the thread is woken up at once), when the room is deleted from memory, and
   when the server process exits (atexit).

   When this module is imported (server/bootstrap.py does that when the server
   starts) the autosave thread is started. Its first job is to load the
   autosaves found in server_data/, so rooms survive a server restart. It waits
   until server/bootstrap.py has imported EVERY module (see _bootstrap_done):
   chat.py is imported last, and a room created before its on_room_created hook
   is registered would have no room.chat.

Locking. The board is only read under room.lock. File writes happen outside
room.lock, under _save_lock, and never from a handler thread: the
on_member_left hook only wakes the autosave thread up. That way a slow disk can
never freeze a room, and no thread waits for room.lock while holding
_save_lock in the opposite order (no deadlock).

What is NOT restored: passwords (contract B keeps them in memory only), members
and roles, chat, undo stacks, and the object lists of versions. The first user
to join a restored room becomes its host (rooms.RoomManager.join: host if the
room has nobody in it).
"""
import atexit
import copy
import json
import os
import sys
import tempfile
import threading
import time
import traceback

from common import protocol
from server import rooms
from server.board_state import MAX_OBJECTS

AUTOSAVE_INTERVAL_S = 10        # contract E7
MAX_VERSIONS = 50               # per room; the oldest are dropped (each one is a full copy)
AUTOSAVE_FORMAT = 1             # "format" field of autosave.json
AUTO_NAME_CLEAR = 'before clear'
BOOTSTRAP_WAIT_S = 30           # how long the autosave thread waits for server.bootstrap before giving up on restoring


def _default_data_dir():
    """Folder that holds one sub-folder per room: $WB_DATA_DIR, else <project>/server_data.

    Next to the program, not in the current directory, so the server finds
    its rooms again however it is started. A PyInstaller build keeps the data
    next to the .exe because its bundled files are unpacked to a temporary
    folder that is deleted on exit.
    """
    env = os.environ.get('WB_DATA_DIR')
    if env:
        return env
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'server_data')


# ---------------------------------------------------------------- the version store

class VersionStore:
    """The saved versions of ONE room (room.versions). Callers hold room.lock.

    Each entry is {'meta': {id, name, by, ts, seq, count}, 'objects': [...]}.
    objects is None for an entry whose metadata was loaded from an autosave
    file: the object lists are not written to disk, so it cannot be restored.
    """

    def __init__(self, room):
        """Start with no versions; remember the room (needed for the VERSIONS broadcast)."""
        self.room = room
        self.entries = []       # oldest first
        self.next_id = 1
        self.rev = 0            # +1 on every change; autosave skips rooms whose (seq, rev) is unchanged
        self.saved_key = None   # (seq, rev) of the last autosave of this room

    def save(self, name, by):
        """Copy the board's objects into a new version and return its metadata dict.

        The copy is deep, so later edits never change a saved version. The
        oldest version is dropped when there are more than MAX_VERSIONS.
        """
        board = self.room.board
        objects = [copy.deepcopy(o) for o in board.objects.values()]    # paint order
        meta = {'id': self.next_id, 'name': name, 'by': by, 'ts': time.time(),
                'seq': board.seq, 'count': len(objects)}
        self.next_id += 1
        self.entries.append({'meta': meta, 'objects': objects})
        del self.entries[:max(0, len(self.entries) - MAX_VERSIONS)]
        self.rev += 1
        return dict(meta)

    def meta_list(self):
        """Metadata of every version, oldest first (the payload of VERSIONS)."""
        return [dict(e['meta']) for e in self.entries]

    def get_objects(self, version_id):
        """A deep copy of the objects of one version, or None (unknown id, or contents not in memory)."""
        for e in self.entries:
            if e['meta']['id'] == version_id and e['objects'] is not None:
                return copy.deepcopy(e['objects'])
        return None

    def load_meta(self, metas):
        """Take version metadata from an autosave file (see the class docstring).

        Entries that are not well-formed are skipped. next_id continues after
        the highest id so a new version never reuses an old id.
        """
        for m in metas:
            if (isinstance(m, dict) and isinstance(m.get('id'), int) and not isinstance(m['id'], bool)
                    and isinstance(m.get('name'), str)):
                meta = {'id': m['id'], 'name': m['name'], 'by': m.get('by'), 'ts': m.get('ts'),
                        'seq': m.get('seq'), 'count': m.get('count')}
                self.entries.append({'meta': meta, 'objects': None})
                self.next_id = max(self.next_id, m['id'] + 1)
        del self.entries[:max(0, len(self.entries) - MAX_VERSIONS)]


def auto_version(room, user, name):
    """Save an automatic version and tell every member (VERSIONS, contract C3).

    The caller (board_ops, before it applies OP_CLEAR) holds room.lock. The
    broadcast is inside the same lock, so VERSIONS reaches every client
    before the operation that follows it.
    """
    meta = room.versions.save(name, user)
    room.broadcast(protocol.VERSIONS, {'versions': room.versions.meta_list()})
    return meta


# ---------------------------------------------------------------- saving to disk

_save_lock = threading.Lock()       # one file write at a time (autosave thread, atexit, tests)
_known_lock = threading.Lock()
_known = []                         # every room that has a store, so a room deleted from memory still gets its last save
_wake = threading.Event()           # set -> the autosave thread runs now instead of at the next tick
_stop = threading.Event()
_thread = None


def room_document(room):
    """Build the autosave.json content of a room, as (JSON text, key). Takes room.lock.

    key is (seq, versions.rev): when it equals the key of the last save,
    nothing changed and the file does not need to be written again.
    """
    with room.lock:
        board, store = room.board, room.versions
        doc = {'format': AUTOSAVE_FORMAT, 'room': room.code, 'seq': board.seq,
               'saved_ts': time.time(),
               'objects': sorted(board.objects.values(), key=lambda o: o.get('z', 0)),
               'versions': store.meta_list()}
        # Serialise INSIDE the lock: the objects are shared with the live board
        # and a handler thread may change them as soon as we let go. The JSON
        # text is written to disk later, outside the lock.
        text = json.dumps(doc, separators=(',', ':'))
        return text, (board.seq, store.rev)


def write_atomic(path, text):
    """Write text to path so that readers see the old file or the new one, never a mix.

    The text goes to a temporary file in the SAME folder (os.replace must stay
    on one file system), is flushed and fsync'ed so the bytes are really on
    disk, and then replaces the target in one step.
    """
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.autosave-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_room(room, data_dir=None, force=False):
    """Write one room's autosave.json. Returns True if a file was written.

    Skipped when nothing changed since the last save, unless force is True.
    A failed write is reported and retried at the next tick (the room stays
    "unsaved"), it never raises into the caller.
    """
    if getattr(room, 'versions', None) is None or room.board is None:
        return False                        # room still being created
    data_dir = data_dir or _default_data_dir()
    with _save_lock:
        text, key = room_document(room)     # snapshot and write are one step: a newer save is never overwritten by an older one
        if not force and room.versions.saved_key == key:
            return False
        try:
            write_atomic(os.path.join(data_dir, room.code, 'autosave.json'), text)
        except OSError as exc:
            print('[versions] autosave of room %s failed: %s' % (room.code, exc))
            return False
        room.versions.saved_key = key
        return True


def save_all(data_dir=None):
    """Save every live room that changed, plus the rooms that were deleted from memory. Returns the count written.

    The known rooms are read BEFORE the live ones: a room created in between
    is then in the live list too, and is not mistaken for a deleted one.
    """
    with _known_lock:
        known = list(_known)
    with rooms.manager.lock:
        live = list(rooms.manager.rooms.values())
    written = sum(1 for room in live if save_room(room, data_dir))
    for room in known:
        if room.closed or not any(room is r for r in live):     # deleted: its last save, then forget it
            written += 1 if save_room(room, data_dir) else 0
            with _known_lock:
                if room in _known:
                    _known.remove(room)
    return written


def _bootstrap_done():
    """True once server/bootstrap.py has imported all of its modules.

    The last line of bootstrap.py is `from server import chat` (chat stays
    last, contract G), which binds the name `chat` in the bootstrap module
    only after chat.py has run completely and registered its room hooks.
    """
    return hasattr(sys.modules.get('server.bootstrap'), 'chat')


def _autosave_loop():
    """Thread body: restore the saved rooms once, then save every AUTOSAVE_INTERVAL_S seconds, or at once when woken up.

    Without server.bootstrap (a unit test that imports a single module) there
    is no real server: after BOOTSTRAP_WAIT_S nothing is restored and only the
    saving continues.
    """
    waited = 0.0
    while not _stop.is_set() and not _bootstrap_done() and waited < BOOTSTRAP_WAIT_S:
        _stop.wait(0.1)
        waited += 0.1
    if _bootstrap_done() and not _stop.is_set():
        try:
            restore_rooms()
        except Exception:  # noqa: BLE001 - a bad autosave must not keep the server from saving
            traceback.print_exc()
    while not _stop.is_set():
        _wake.wait(AUTOSAVE_INTERVAL_S)
        _wake.clear()
        if _stop.is_set():
            break
        try:
            save_all()
        except Exception:  # noqa: BLE001 - the thread must survive any bug
            traceback.print_exc()


# ---------------------------------------------------------------- loading from disk

def load_document(path, code):
    """Read and check one autosave.json. Returns (objects, seq, version metas) or None if unusable.

    The file is data written by an earlier run, but it may be damaged or
    edited by hand, so every field is checked before it can reach a room.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        print('[versions] cannot read %s: %s' % (path, exc))
        return None
    seq, objects = (doc.get('seq'), doc.get('objects')) if isinstance(doc, dict) else (None, None)
    if (not isinstance(doc, dict) or doc.get('format') != AUTOSAVE_FORMAT or doc.get('room') != code
            or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0
            or not isinstance(objects, list) or len(objects) > MAX_OBJECTS
            or not all(isinstance(o, dict) and isinstance(o.get('id'), str) and isinstance(o.get('kind'), str)
                       for o in objects)):
        print('[versions] ignoring %s: not a valid autosave' % path)
        return None
    metas = doc.get('versions')
    return objects, seq, metas if isinstance(metas, list) else []


def restore_rooms(data_dir=None):
    """Re-create every room that has an autosave.json; returns the list of room codes restored.

    The Room objects come from rooms.manager.create_room(), so every
    on_room_created hook runs (board, undo, versions, chat). create_room picks
    a random code; it is replaced by the saved one under both locks (lock
    order manager, then room, as in RoomManager.sweep). The rooms start with
    no members and no password, see the module docstring.
    """
    data_dir = data_dir or _default_data_dir()
    restored = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return restored                     # first run: no server_data yet
    for code in names:
        path = os.path.join(data_dir, code, 'autosave.json')
        try:
            valid = rooms.clean_code(code) == code      # folder names come from disk: only real room codes
        except rooms.JoinError:
            valid = False
        if not valid or not os.path.isfile(path) or rooms.manager.get(code) is not None:
            continue
        loaded = load_document(path, code)
        if loaded is None:
            continue
        objects, seq, metas = loaded
        room = rooms.manager.create_room()
        with rooms.manager.lock, room.lock:
            del rooms.manager.rooms[room.code]
            room.code = code
            rooms.manager.rooms[code] = room
            room.board.restore(objects, seq)
            room.versions.load_meta(metas)
            room.versions.saved_key = (seq, room.versions.rev)  # identical to the file: no rewrite until something changes
        restored.append(code)
        print('[versions] room %s restored from autosave (%d objects, seq %d)' % (code, len(objects), seq))
    return restored


# ---------------------------------------------------------------- room hooks and start-up

@rooms.on_room_created
def attach_versions(room):
    """Hook: give every new room its VersionStore and remember it for the autosave thread."""
    room.versions = VersionStore(room)
    with _known_lock:
        _known.append(room)


@rooms.on_member_left
def save_when_empty(room, member):
    """Hook: the last online member left, so the room is effectively closed: save it soon.

    Only wakes the autosave thread up. Writing here would run disk I/O while
    the caller holds room.lock (see the Locking paragraph in the module docstring).
    """
    if not room.online_members():
        _wake.set()


def start():
    """Start the autosave thread (it restores the saved rooms first). Safe to call more than once."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_autosave_loop, name='autosave', daemon=True)
    _thread.start()


def stop():
    """Stop the autosave thread and write every room one last time."""
    global _thread
    _stop.set()
    _wake.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
    save_all()


# The server process ends (Ctrl+C included): one last save of every room, so
# changes made in the last seconds before shutdown are not lost.
atexit.register(save_all)

# Start when the module is imported: server/bootstrap.py does that before the
# server accepts connections, and P3 owns no file that runs later. If P1 ever
# adds a "server started" call, versions.start() can move there.
start()
