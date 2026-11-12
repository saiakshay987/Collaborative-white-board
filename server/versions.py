# OWNER: P3
"""versions.py: Version history (contract C3) and autosave to disk (contract E6 and E7).

Three jobs live in this file.

1. Versions. Every room gets `room.versions`, a VersionStore: named copies of
   the board (id, name, by, ts, seq, count). They are created by

       VERSION_SAVE {name}     host or editor (permission version_save)
       auto_version()          called by board_ops right before OP_CLEAR ('before clear')
       VERSION_RESTORE {id}    host only: see below ('before restore <name>')
       auto_versions_tick()    every AUTO_VERSION_INTERVAL_S (5 min), only if the board changed

   VERSION_LIST (anybody) answers the sender; every change is pushed to the whole
   room as VERSIONS {versions:[{id, name, by, ts, seq, count}]}. Each version
   is a deep copy of the objects, kept in memory (at most MAX_VERSIONS per room)
   AND written to server_data/<ROOM>/versions/<id>.json, so versions survive a
   server restart.

   VERSION_RESTORE first saves the current board as 'before restore <name>', so a
   restore can itself be reversed, then replaces the board with the saved
   objects and bumps seq by one. This is not a board operation, so nothing goes
   into the operation log; the log is emptied instead, which makes every client
   with an older last_seq take a SNAPSHOT (BoardState.delta refuses it) instead
   of a delta with a hole in it. The SNAPSHOT is broadcast to everybody, the
   sender included. The undo stacks are reset, as after a clear (undo.py rule 5).

2. Autosave (contract E7). One background thread wakes up every
   AUTOSAVE_INTERVAL_S (10 s) and writes every room that changed to

       server_data/<ROOM>/autosave.json   {format, room, seq, saved_ts, objects, versions, assets,
                                           host, members, auth, chat}

   `versions` holds the METADATA of the versions; their object lists are in the
   versions/ folder next to it. `assets` (server/assets.py) holds the metadata of the
   pictures of the room; their bytes are in assets/<asset_id>.bin. Every file is written ATOMICALLY: the JSON goes to
   a temporary file in the same folder, is flushed to disk, and os.replace() then
   swaps it in. A crash or power cut therefore leaves either the complete old
   file or the complete new one, never a half-written file.

   A room is also saved when it closes: when its last online member leaves
   (the thread is woken up at once), when the room is deleted from memory, and
   when the server process exits (atexit). A new version also wakes the thread
   up, so it reaches the disk within moments.

3. Restore. When this module is imported (server/bootstrap.py does that when the
   server starts) the autosave thread is started. Its first job is to load the
   autosaves found in server_data/, so rooms survive a server restart. It waits
   until server/bootstrap.py has imported EVERY module (see _bootstrap_done):
   chat.py is imported last, and a room created before its on_room_created hook
   is registered would have no room.chat.

Locking. The board is only read under room.lock. File writes happen outside
room.lock, under _save_lock, and never from a handler thread: handlers and the
on_member_left hook only wake the autosave thread up. That way a slow disk can
never freeze a room, and no thread waits for room.lock while holding
_save_lock in the opposite order (no deadlock).

   The room's people and chat are saved too (see room_extras): `host` (the host's
   username), `members` (username -> color and role, so a returning user gets
   the same role back, contract E8), `auth` (the SALTED sha256 of the password
   and its salt, never the password itself) and `chat` (the chat log). Old files
   without these fields still load: the room is then simply open and has no host.

What is NOT restored: undo stacks and who is online (after a restart nobody is). A
restored room starts with an empty member list; the first user to join takes the
role their `members` entry says, and the saved host gets host back.
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
from server import permissions, rooms
from server.board_state import BoardError, MAX_OBJECTS
from server.dispatcher import handler

AUTOSAVE_INTERVAL_S = 10        # contract E7
MAX_VERSIONS = 50               # per room; the oldest are dropped (each one is a full copy)
AUTOSAVE_FORMAT = 1             # "format" field of autosave.json
VERSION_FORMAT = 1              # "format" field of versions/<id>.json
VERSIONS_DIR = 'versions'       # folder inside server_data/<room>/
AUTO_VERSION_INTERVAL_S = 300   # 5 minutes: periodic version, taken only if the board changed
MAX_NAME_LEN = 60               # longer version names are cut
ASSETS_DIR = 'assets'           # folder inside server_data/<room>/ (same name as server.assets.ASSETS_DIR)
AUTO_NAME_CLEAR = 'before clear'
AUTO_NAME_PERIODIC = 'auto'
AUTO_NAME_RESTORE = 'before restore '
SERVER_USER = 'server'          # "by" of a periodic version
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

    Each entry is {'meta': {id, name, by, ts, seq, count}, 'objects': [...],
    'auto': bool, 'on_disk': bool}. objects is None for an entry whose
    metadata came from an autosave.json of the earlier version of this server
    and that has no file in versions/: it is listed but cannot be restored.
    auto marks the periodic versions, which are the first to be dropped when
    there are more than MAX_VERSIONS. on_disk is False until the autosave
    thread has written the entry's file.
    """

    def __init__(self, room):
        """Start with no versions; remember the room (needed for the VERSIONS broadcast)."""
        self.room = room
        self.entries = []       # oldest first
        self.next_id = 1
        self.rev = 0            # +1 on every change; autosave skips rooms whose (seq, rev) is unchanged
        self.saved_key = None   # (seq, rev) of the last autosave of this room
        self.updated_ts = time.time()   # when the room last changed on disk (the room list shows it)
        self.dropped = []       # ids whose files in versions/ still have to be deleted
        self.last_seq = None    # board seq of the newest version: the periodic one is skipped if the board is still at it
        self.next_auto_ts = time.time() + AUTO_VERSION_INTERVAL_S

    def save(self, name, by, auto=False):
        """Copy the board's objects into a new version and return its metadata dict.

        The copy is deep, so later edits never change a saved version. When
        there are more than MAX_VERSIONS, the oldest periodic version goes
        first, and only if there is none the oldest version of all.
        """
        board = self.room.board
        objects = [copy.deepcopy(o) for o in board.objects.values()]    # paint order
        meta = {'id': self.next_id, 'name': name, 'by': by, 'ts': time.time(),
                'seq': board.seq, 'count': len(objects)}
        self.next_id += 1
        self.entries.append({'meta': meta, 'objects': objects, 'auto': auto, 'on_disk': False})
        self.last_seq = board.seq
        self._trim()
        self.rev += 1
        return dict(meta)

    def _trim(self):
        """Drop entries until at most MAX_VERSIONS are left (periodic ones first); remember their ids for the disk."""
        while len(self.entries) > MAX_VERSIONS:
            victim = next((e for e in self.entries if e['auto']), self.entries[0])
            self.entries.remove(victim)
            self.dropped.append(victim['meta']['id'])

    def meta_list(self):
        """Metadata of every version, oldest first (the payload of VERSIONS)."""
        return [dict(e['meta']) for e in self.entries]

    def get_meta(self, version_id):
        """A copy of the metadata of one version, or None."""
        for e in self.entries:
            if e['meta']['id'] == version_id:
                return dict(e['meta'])
        return None

    def get_objects(self, version_id):
        """A deep copy of the objects of one version, or None (unknown id, or contents not available)."""
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
                self._adopt({'meta': meta, 'objects': None, 'auto': False, 'on_disk': True})

    def load_files(self, folder):
        """Load versions/<id>.json files; they replace metadata-only entries with the same id. Returns the count loaded."""
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return 0
        loaded = 0
        for name in names:
            entry = load_version_file(os.path.join(folder, name), name)
            if entry is not None:
                self._adopt(entry)
                loaded += 1
        return loaded

    def _adopt(self, entry):
        """Insert a loaded entry in id order (replacing one with the same id) and keep the counters right."""
        vid = entry['meta']['id']
        self.entries = [e for e in self.entries if e['meta']['id'] != vid]
        self.entries.append(entry)
        self.entries.sort(key=lambda e: e['meta']['id'])
        self.next_id = max(self.next_id, vid + 1)
        seqs = [e['meta']['seq'] for e in self.entries if isinstance(e['meta'].get('seq'), int)]
        self.last_seq = max(seqs) if seqs else None
        self._trim()

    def pending_files(self):
        """(entries still to be written, ids still to be deleted) for the autosave thread; takes room.lock."""
        with self.room.lock:
            return ([e for e in self.entries if e['objects'] is not None and not e['on_disk']],
                    list(self.dropped))


def take_version(room, name, by, auto=False):
    """Save a version and tell every member (VERSIONS, contract C3). The caller holds room.lock.

    The broadcast is inside the same lock, so VERSIONS reaches every client in
    the order things happened (e.g. before the OP_CLEAR that follows it). The
    autosave thread is woken up to write the new version's file.
    """
    meta = room.versions.save(name, by, auto)
    push_versions(room)
    _wake.set()
    return meta


def request_save():
    """Wake the autosave thread: something worth saving happened (a new version, a new picture). Never blocks."""
    _wake.set()


def push_versions(room):
    """Send VERSIONS {versions} to every online member of the room."""
    room.broadcast(protocol.VERSIONS, {'versions': room.versions.meta_list()})


def send_versions_on_join(room, member, rejoin, last_seq):
    """Join step 5: send the joining member the version list, VERSIONS {versions}.

    NOT decorated with @rooms.on_member_joined: server/bootstrap.py registers
    it AFTER chat.py, so the order a (re)joining client sees is JOIN_OK,
    USER_LIST, SNAPSHOT, CHAT_HISTORY, VERSIONS. Only the joiner gets it (the
    others already have the list). rooms.manager.member_joined holds room.lock.
    """
    with room.lock:
        room.send_to(member.name, protocol.VERSIONS, {'versions': room.versions.meta_list()})


def auto_version(room, user, name):
    """Save the automatic version board_ops needs before OP_CLEAR (caller holds room.lock)."""
    return take_version(room, name, user)


def auto_version_due(room, now):
    """Take the periodic version of ONE room if its time has come and its board changed. Returns True if one was taken.

    A room is due every AUTO_VERSION_INTERVAL_S; nothing is saved when the
    board still has the seq of the newest version (nobody changed anything),
    and never for a board that was never touched (seq 0).
    """
    if getattr(room, 'versions', None) is None or room.board is None:
        return False
    with room.lock:
        store = room.versions
        if now < store.next_auto_ts:
            return False
        store.next_auto_ts = now + AUTO_VERSION_INTERVAL_S
        if room.board.seq == 0 or store.last_seq == room.board.seq:
            return False
        take_version(room, AUTO_NAME_PERIODIC, SERVER_USER, auto=True)
        return True


def auto_versions_tick(now=None):
    """Run auto_version_due for every live room (called by the autosave thread). Returns the count taken."""
    now = time.time() if now is None else now
    with rooms.manager.lock:
        live = list(rooms.manager.rooms.values())
    return sum(1 for room in live if auto_version_due(room, now))


# ---------------------------------------------------------------- version files

def version_document(entry):
    """JSON text of versions/<id>.json for one store entry."""
    return json.dumps({'format': VERSION_FORMAT, 'meta': entry['meta'], 'auto': entry['auto'],
                       'objects': entry['objects']}, separators=(',', ':'))


def load_version_file(path, filename):
    """Read one versions/<id>.json. Returns a store entry, or None if the file is unusable.

    Checked like the autosave: the id inside must match the file name, and
    every object needs a string id and kind.
    """
    if not filename.endswith('.json') or not filename[:-5].isdigit():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        print('[versions] cannot read %s: %s' % (path, exc))
        return None
    meta = doc.get('meta') if isinstance(doc, dict) else None
    objects = doc.get('objects') if isinstance(doc, dict) else None
    if (not isinstance(meta, dict) or meta.get('id') != int(filename[:-5]) or not isinstance(meta.get('name'), str)
            or doc.get('format') != VERSION_FORMAT or not isinstance(objects, list) or len(objects) > MAX_OBJECTS
            or not all(isinstance(o, dict) and isinstance(o.get('id'), str) and isinstance(o.get('kind'), str)
                       for o in objects)):
        print('[versions] ignoring %s: not a valid version file' % path)
        return None
    meta = {k: meta.get(k) for k in ('id', 'name', 'by', 'ts', 'seq', 'count')}
    return {'meta': meta, 'objects': objects, 'auto': doc.get('auto') is True, 'on_disk': True}


# ---------------------------------------------------------------- saving to disk

_save_lock = threading.Lock()       # one file write at a time (autosave thread, atexit, tests)
_known_lock = threading.Lock()
_known = []                         # every room that has a store, so a room deleted from memory still gets its last save
_wake = threading.Event()           # set -> the autosave thread runs now instead of at the next tick
_stop = threading.Event()
_thread = None


def room_extras(room):
    """The people and chat of a room as plain JSON data: {host, members, auth, chat}. Caller holds room.lock.

    members is room.profiles overlaid with the seats that exist right now,
    because a profile is only refreshed when a seat goes away and the host may
    have changed an online member's role with SET_ROLE since. auth is the salted
    hash kept by rooms.Room (P1) and the chat is the ChatLog of chat.py (P4);
    both are read through the few attributes named here so that only this
    function (and apply_extras) knows their layout.
    """
    members = {name: dict(p) for name, p in room.profiles.items()}
    for name, m in room.members.items():
        members[name] = {'color': m.color, 'role': m.role}
    chat = getattr(room, 'chat', None)
    if chat is not None:
        with chat._lock:                    # the log has its own lock (it is not room.lock)
            chat_doc = {'counter': chat._counter, 'messages': [dict(m) for m in chat._messages]}
    else:
        chat_doc = {'counter': 0, 'messages': []}
    return {'host': room.host_name, 'members': members,
            'auth': {'salt': room._salt.hex(), 'hash': room._pw_hash},
            'chat': chat_doc}


def last_updated(room):
    """Epoch seconds of the room's last change (board, versions, pictures, people or chat); the room list shows it."""
    store = getattr(room, 'versions', None)
    return store.updated_ts if store is not None else room.created_ts


def room_document(room):
    """Build the autosave.json content of a room, as (JSON text, key). Takes room.lock.

    key is (seq, versions.rev, assets.rev, extras): when it equals the key of the
    last save, nothing changed and the file does not need to be written again.
    The extras (people and chat) are part of the key as their JSON text, so a
    new chat message or a role change also makes the room "changed".
    """
    with room.lock:
        board, store = room.board, room.versions
        assets = getattr(room, 'assets', None)      # None in a program that does not load server.assets
        extras = room_extras(room)
        doc = {'format': AUTOSAVE_FORMAT, 'room': room.code, 'seq': board.seq,
               'saved_ts': time.time(),
               'objects': sorted(board.objects.values(), key=lambda o: o.get('z', 0)),
               'versions': store.meta_list(),
               'assets': assets.meta_list() if assets is not None else []}
        doc.update(extras)
        # Serialise INSIDE the lock: the objects are shared with the live board
        # and a handler thread may change them as soon as we let go. The JSON
        # text is written to disk later, outside the lock.
        text = json.dumps(doc, separators=(',', ':'))
        extras_key = json.dumps(extras, sort_keys=True, separators=(',', ':'))
        return text, (board.seq, store.rev, assets.rev if assets is not None else 0, extras_key)


def write_atomic(path, text):
    """Write text (str, or bytes for a picture) to path so that readers see the old file or the new one, never a mix.

    The text goes to a temporary file in the SAME folder (os.replace must stay
    on one file system), is flushed and fsync'ed so the bytes are really on
    disk, and then replaces the target in one step.
    """
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.autosave-', suffix='.tmp')
    try:
        binary = isinstance(text, (bytes, bytearray))
        with os.fdopen(fd, 'wb' if binary else 'w', **({} if binary else {'encoding': 'utf-8'})) as f:
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


def _write_version_files(room, folder):
    """Write the version files that are not on disk yet and delete the files of dropped versions.

    The objects of an entry are a private copy that nobody changes, so the
    JSON can be made and written outside room.lock. An entry is marked
    on_disk only after its file was replaced; an OSError stops here and the
    rest is retried at the next save.
    """
    pending, dropped = room.versions.pending_files()
    for entry in pending:
        write_atomic(os.path.join(folder, '%d.json' % entry['meta']['id']), version_document(entry))
        entry['on_disk'] = True
    for vid in dropped:
        try:
            os.remove(os.path.join(folder, '%d.json' % vid))
        except FileNotFoundError:
            pass                            # it never reached the disk
    with room.lock:
        room.versions.dropped = [v for v in room.versions.dropped if v not in dropped]


def _write_asset_files(room, folder):
    """Write the pictures that are not on disk yet, as <asset_id>.bin (atomic, like every file here).

    Pictures are immutable and never deleted, so unlike the version files
    there is nothing to remove. The bytes are immutable too, so they are
    written outside room.lock.
    """
    assets = getattr(room, 'assets', None)
    if assets is None:
        return
    for entry in assets.pending_files():
        write_atomic(os.path.join(folder, entry['meta']['asset_id'] + '.bin'), entry['data'])
        entry['on_disk'] = True


def save_room(room, data_dir=None, force=False):
    """Write one room's autosave.json and its new version files. Returns True if files were written.

    Skipped when nothing changed since the last save, unless force is True.
    The version files come first (versions/<id>.json for every version not on
    disk yet, and the files of dropped versions are deleted), then the
    autosave.json that lists them. A failed write is reported and retried at
    the next tick (the room stays "unsaved"), it never raises into the caller.
    """
    if getattr(room, 'versions', None) is None or room.board is None:
        return False                        # room still being created
    data_dir = data_dir or _default_data_dir()
    with _save_lock:
        text, key = room_document(room)     # snapshot and write are one step: a newer save is never overwritten by an older one
        if not force and room.versions.saved_key == key:
            return False
        try:
            _write_version_files(room, os.path.join(data_dir, room.code, VERSIONS_DIR))
            _write_asset_files(room, os.path.join(data_dir, room.code, ASSETS_DIR))
            write_atomic(os.path.join(data_dir, room.code, 'autosave.json'), text)
        except OSError as exc:
            print('[versions] autosave of room %s failed: %s' % (room.code, exc))
            return False
        room.versions.saved_key = key
        room.versions.updated_ts = time.time()      # the file on disk now holds this change
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
            auto_versions_tick()        # first, so a new periodic version is written in this very round
            save_all()
        except Exception:  # noqa: BLE001 - the thread must survive any bug
            traceback.print_exc()


# ---------------------------------------------------------------- loading from disk

def load_document(path, code):
    """Read and check one autosave.json. Returns (objects, seq, version metas, asset metas, extras, saved_ts) or None.

    None means unusable. extras is the cleaned people/password/chat data (see clean_extras).

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
    metas, asset_metas = doc.get('versions'), doc.get('assets')
    saved_ts = doc.get('saved_ts')
    saved_ts = float(saved_ts) if isinstance(saved_ts, (int, float)) and not isinstance(saved_ts, bool) else None
    return (objects, seq, metas if isinstance(metas, list) else [],
            asset_metas if isinstance(asset_metas, list) else [], clean_extras(doc), saved_ts)


def clean_extras(doc):
    """Check the people, password and chat fields of a loaded autosave; anything malformed is dropped, never trusted.

    Returns {host, members, salt, hash, chat_counter, chat_messages}. A file with
    no password gives an open room. A file whose `auth` is damaged but names a
    hash gives a room that stays LOCKED with a hash nobody can match (fail
    closed: better a locked room than an accidentally open one).
    """
    roles = ('host', 'editor', 'viewer')
    members = {}
    raw = doc.get('members')
    for name, p in (raw.items() if isinstance(raw, dict) else ()):
        if (isinstance(name, str) and isinstance(p, dict) and p.get('role') in roles
                and isinstance(p.get('color'), str)):
            members[name] = {'color': p['color'], 'role': p['role']}
    host = doc.get('host') if isinstance(doc.get('host'), str) else None
    auth = doc.get('auth') if isinstance(doc.get('auth'), dict) else {}
    salt, pw_hash = None, auth.get('hash')
    try:
        salt = bytes.fromhex(auth.get('salt'))
    except (TypeError, ValueError):
        pass
    if pw_hash is not None and (not isinstance(pw_hash, str) or salt is None):
        pw_hash, salt = 'unreadable', None  # fail closed: locked, no password can match
    chat = doc.get('chat') if isinstance(doc.get('chat'), dict) else {}
    raw_msgs = chat.get('messages') if isinstance(chat.get('messages'), list) else []
    msgs = [m for m in raw_msgs
            if isinstance(m, dict) and m.get('type') in ('CHAT', 'DM') and isinstance(m.get('msg_id'), str)
            and isinstance(m.get('from'), str) and isinstance(m.get('text'), str)
            and isinstance(m.get('ts'), (int, float))]
    counter = chat.get('counter')
    counter = counter if isinstance(counter, int) and not isinstance(counter, bool) and counter >= 0 else len(msgs)
    return {'host': host, 'members': members, 'salt': salt, 'hash': pw_hash,
            'chat_counter': counter, 'chat_messages': msgs}


def apply_extras(room, extras):
    """Put the cleaned people, password and chat of an autosave into a fresh room. Caller holds room.lock."""
    room.host_name = extras['host']
    room.profiles = dict(extras['members'])
    # Colors are handed out in join order; continue after the saved users so a
    # newcomer does not get the color of someone the room remembers.
    room._color_index = len(room.profiles)
    if extras['salt'] is not None:
        room._salt = extras['salt']
    room._pw_hash = extras['hash']
    chat = getattr(room, 'chat', None)
    if chat is not None:
        with chat._lock:
            chat._messages = list(extras['chat_messages'])
            chat._counter = extras['chat_counter']


def restore_rooms(data_dir=None):
    """Re-create every room that has an autosave.json; returns the list of room codes restored.

    The Room objects come from rooms.manager.create_room(), so every
    on_room_created hook runs (board, undo, versions, chat). create_room picks
    a random code; it is replaced by the saved one under both locks (lock
    order manager, then room, as in RoomManager.sweep). The rooms start with
    nobody online but keep their password hash, host and member profiles
    (apply_extras), so they can be listed and joined again straight away.
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
        objects, seq, metas, asset_metas, extras, saved_ts = loaded
        room = rooms.manager.create_room()
        with rooms.manager.lock, room.lock:
            del rooms.manager.rooms[room.code]
            room.code = code
            rooms.manager.rooms[code] = room
            room.board.restore(objects, seq)
            apply_extras(room, extras)
            if saved_ts is not None:
                room.versions.updated_ts = saved_ts
            room.versions.load_meta(metas)      # first the metadata, then the files, which replace entries with a file
            room.versions.load_files(os.path.join(data_dir, code, VERSIONS_DIR))
            if getattr(room, 'assets', None) is not None:
                room.assets.load(asset_metas, os.path.join(data_dir, code, ASSETS_DIR))
            room.versions.saved_key = room_document(room)[1]        # identical to the file: no rewrite until something changes
        restored.append(code)
        print('[versions] room %s restored from autosave (%d objects, seq %d)' % (code, len(objects), seq))
    return restored


# ---------------------------------------------------------------- handlers

def _require(session, action):
    """Raise BoardError(FORBIDDEN) unless the member's role (as stored by the SERVER) may do `action`."""
    if not permissions.can(session.member.role, action):
        raise BoardError(protocol.FORBIDDEN, '%s is not allowed to %s' % (session.member.role, action))


def _clean_name(value):
    """The version name from a client payload: a printable string, whitespace collapsed, cut to MAX_NAME_LEN.

    Raises BoardError(BAD_FORMAT) for a missing, non-string or empty name.
    """
    if not isinstance(value, str):
        raise BoardError(protocol.BAD_FORMAT, 'name must be a string')
    name = ' '.join(value.split())          # also turns newlines and tabs into single spaces
    if not name or not name.isprintable():
        raise BoardError(protocol.BAD_FORMAT, 'name must not be empty')
    return name[:MAX_NAME_LEN]


@handler(protocol.VERSION_SAVE)
def on_version_save(session, msg):
    """VERSION_SAVE {name}: store a copy of the board (host or editor) and push VERSIONS to the room."""
    room = session.room
    try:
        _require(session, 'version_save')
        name = _clean_name(msg['payload'].get('name'))
        with room.lock:
            take_version(room, name, msg['user'])
    except BoardError as exc:
        session.send_error(exc.code, exc.text, protocol.VERSION_SAVE)


@handler(protocol.VERSION_LIST)
def on_version_list(session, msg):
    """VERSION_LIST {}: send the list to the sender only. Every member may ask, viewers too (it changes nothing)."""
    with session.room.lock:
        session.send(protocol.VERSIONS, {'versions': session.room.versions.meta_list()})


@handler(protocol.VERSION_RESTORE)
def on_version_restore(session, msg):
    """VERSION_RESTORE {id}: host only. Save the current board, replace it by the version, SNAPSHOT to everyone.

    Steps, all under room.lock so no board operation falls in between:
        1. permission 'version_restore'; the id must be an integer of a version whose objects we have
        2. save the current board as 'before restore <name>' (so this restore can be reversed)
        3. BoardState.restore(objects, seq + 1): the board is replaced, seq bumped, the log emptied
        4. reset the undo stacks (their steps point at objects of the old board)
        5. VERSIONS (the list grew), then SNAPSHOT, both to EVERY member, the host included
    """
    room, user = session.room, msg['user']
    try:
        _require(session, 'version_restore')
        vid = msg['payload'].get('id')
        if not isinstance(vid, int) or isinstance(vid, bool):
            raise BoardError(protocol.BAD_FORMAT, 'id must be an integer')
        with room.lock:
            meta = room.versions.get_meta(vid)
            objects = room.versions.get_objects(vid)
            if meta is None:
                raise BoardError(protocol.NOT_FOUND, 'no version %d' % vid)
            if objects is None:
                raise BoardError(protocol.NOT_FOUND, "the contents of version '%s' are not available" % meta['name'])
            take_version(room, AUTO_NAME_RESTORE + meta['name'], user)
            room.board.restore(objects, room.board.seq + 1)
            room.undo.reset()
            room.broadcast(protocol.SNAPSHOT, room.board.snapshot_payload())
            print('[versions] room %s: %s restored version %d (%s)' % (room.code, user, vid, meta['name']))
    except BoardError as exc:
        session.send_error(exc.code, exc.text, protocol.VERSION_RESTORE)


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
