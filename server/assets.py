"""assets.py: The picture store of a room: IMG_BEGIN / IMG_CHUNK / IMG_END uploads and ASSET_GET downloads (contract C3).

A board object of kind 'image' only names its picture (asset_id, see common/kinds_image.py, which also explains
why pictures travel in chunks and why the id is the sha256 of the bytes). The bytes live here, in
`room.assets`, an AssetStore.

UPLOAD (client to server), host or editor (permission 'upload'):

    IMG_BEGIN {asset_id, name, mime, size, chunks, w, h}   open an upload: everything is checked NOW, from this
                                                            small message, before any picture byte arrives
    IMG_CHUNK {asset_id, idx, data}                        data = base64 of at most 24 KB; idx counts 0, 1, 2 ...
    IMG_END   {asset_id, sha256}                           the server verifies size, hash and file type
    answered with IMG_OK {asset_id} to the sender only, or ERROR TOO_LARGE / BAD_FORMAT (ref_type = the message
    that failed). After IMG_OK the client sends OP_ADD with the image object.

The server never trusts what the client says about the picture:
  * size above MAX_IMAGE_BYTES (5 MB): ERROR TOO_LARGE at IMG_BEGIN, nothing is buffered;
  * the chunk count must be exactly ceil(size / 24 KB), chunks must arrive in order and have exactly the
    announced length, so the buffer can never grow beyond the declared size;
  * the sha256 of the received bytes must equal both the sha256 field and the asset_id (content addressing),
    and the first bytes must really be a PNG or a JPEG (common.kinds_image.mime_matches);
  * quotas: at most MAX_PENDING_PER_USER uploads in progress per user and MAX_ROOM_ASSET_BYTES stored per room.
Any error in the middle of an upload throws that upload away (the client starts again with a new IMG_BEGIN; a
second IMG_BEGIN for the same asset simply replaces the unfinished one). Unfinished uploads of a member are also
dropped when the member leaves.

Reassembly is per (user, asset_id) and happens under room.lock, like every other change of room state. The
hashing of a finished picture (up to 5 MB) is done outside the lock.

DOWNLOAD (ASSET_GET {asset_id}, any member, viewers too): the server streams IMG_BEGIN, IMG_CHUNK x n and
IMG_END back to the asker only, in the same shapes as an upload. A picture that does not exist: ERROR NOT_FOUND.
The bytes are read under the lock (they are immutable) but SENT after it is released, so a slow client can never
hold up the room.

PERSISTENCE: the metadata of every asset goes into autosave.json (server/versions.py) and the bytes into
server_data/<room>/assets/<asset_id>.bin, written atomically by the autosave thread. The id being the sha256, a
file is checked against its own name when it is loaded. Assets are never deleted (an undo can bring an object
back, and an old version may still show the picture).
"""
import base64
import binascii
import hashlib
import os
import time
from collections import OrderedDict

from common import kinds_image, protocol
from server import permissions, rooms, versions
from server.board_state import BoardError
from server.dispatcher import handler

MAX_ROOM_ASSET_BYTES = 64 * 1024 * 1024     # stored pictures per room (server memory is the limit here)
MAX_PENDING_PER_USER = 2                    # uploads in progress at the same time, per user
MAX_DIM = 4000                              # largest w or h that is accepted as metadata
ASSETS_DIR = 'assets'                       # folder inside server_data/<room>/
B64_CHUNK_CHARS = 4 * -(-kinds_image.CHUNK_BYTES // 3)      # longest legal base64 text of one chunk


def _is_int(value):
    """True for a real integer (bool is an int in Python, but never a size)."""
    return isinstance(value, int) and not isinstance(value, bool)


def clean_name(value):
    """The picture's file name as a short printable label ('' if it is missing or not a string)."""
    if not isinstance(value, str):
        return ''
    return ''.join(ch for ch in value if ch.isprintable())[:kinds_image.MAX_NAME]


class AssetStore:
    """The pictures of ONE room (room.assets). Every method is called with room.lock held, except where noted.

    assets   OrderedDict asset_id -> {'meta': {...}, 'data': bytes, 'on_disk': bool}; meta has
             asset_id, name, mime, size, w, h, by, ts
    pending  (user, asset_id) -> {'meta': {...}, 'buf': bytearray, 'next': index of the next expected chunk}
    """

    def __init__(self, room):
        """Start empty; the room is remembered for nothing but symmetry with VersionStore."""
        self.room = room
        self.assets = OrderedDict()
        self.pending = {}
        self.total = 0          # bytes stored
        self.rev = 0            # +1 on every new asset; autosave writes the room again when it changes

    # ---------------------------------------------------------------- upload
    def begin(self, user, p):
        """IMG_BEGIN: check everything that can be checked from the announcement and open an upload."""
        if not isinstance(p, dict):
            raise BoardError(protocol.BAD_FORMAT, 'payload must be an object')
        asset_id, mime, size, chunks = p.get('asset_id'), p.get('mime'), p.get('size'), p.get('chunks')
        if not kinds_image.is_asset_id(asset_id):
            raise BoardError(protocol.BAD_FORMAT, 'asset_id must be the sha256 of the picture (64 hex characters)')
        if mime not in kinds_image.MIMES:
            raise BoardError(protocol.BAD_FORMAT, 'mime must be one of %s' % ', '.join(kinds_image.MIMES))
        if not _is_int(size) or size <= 0:
            raise BoardError(protocol.BAD_FORMAT, 'size must be a positive integer')
        if size > kinds_image.MAX_IMAGE_BYTES:
            raise BoardError(protocol.TOO_LARGE, 'picture is %d bytes, the limit is %d' % (size, kinds_image.MAX_IMAGE_BYTES))
        if not _is_int(chunks) or chunks != kinds_image.chunk_count(size):      # True == 1 in Python: bool is not a count
            raise BoardError(protocol.BAD_FORMAT, 'chunks must be %d for %d bytes' % (kinds_image.chunk_count(size), size))
        w, h = p.get('w'), p.get('h')
        if not (_is_int(w) and _is_int(h) and 1 <= w <= MAX_DIM and 1 <= h <= MAX_DIM):
            raise BoardError(protocol.BAD_FORMAT, 'w and h must be integers from 1 to %d' % MAX_DIM)
        key = (user, asset_id)
        self.pending.pop(key, None)             # a new BEGIN replaces an unfinished upload of the same picture
        if sum(1 for k in self.pending if k[0] == user) >= MAX_PENDING_PER_USER:
            raise BoardError(protocol.TOO_LARGE, 'too many uploads in progress')
        waiting = sum(u['meta']['size'] for u in self.pending.values())
        if self.total + waiting + size > MAX_ROOM_ASSET_BYTES:
            raise BoardError(protocol.TOO_LARGE, 'this room has no space left for pictures')
        meta = {'asset_id': asset_id, 'name': clean_name(p.get('name')), 'mime': mime, 'size': size,
                'w': w, 'h': h, 'by': user, 'ts': time.time()}
        self.pending[key] = {'meta': meta, 'buf': bytearray(), 'next': 0}

    def chunk(self, user, p):
        """IMG_CHUNK: append the next piece. Any error throws the whole upload away."""
        if not isinstance(p, dict) or not kinds_image.is_asset_id(p.get('asset_id')):
            raise BoardError(protocol.BAD_FORMAT, 'asset_id must be the sha256 of the picture')
        key = (user, p['asset_id'])
        up = self.pending.get(key)
        if up is None:
            raise BoardError(protocol.NOT_FOUND, 'no upload in progress for this asset')
        try:
            idx, data = p.get('idx'), p.get('data')
            if not _is_int(idx) or idx != up['next']:
                raise BoardError(protocol.BAD_FORMAT, 'chunk %r out of order, expected %d' % (idx, up['next']))
            if not isinstance(data, str) or len(data) > B64_CHUNK_CHARS:
                raise BoardError(protocol.BAD_FORMAT, 'data must be base64 text of at most %d bytes' % kinds_image.CHUNK_BYTES)
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                raise BoardError(protocol.BAD_FORMAT, 'data is not valid base64')
            meta = up['meta']
            last = idx == kinds_image.chunk_count(meta['size']) - 1
            want = meta['size'] - kinds_image.CHUNK_BYTES * idx if last else kinds_image.CHUNK_BYTES
            if len(raw) != want:
                raise BoardError(protocol.BAD_FORMAT, 'chunk %d has %d bytes, expected %d' % (idx, len(raw), want))
        except BoardError:
            del self.pending[key]
            raise
        up['buf'] += raw
        up['next'] += 1

    def finish(self, user, p):
        """IMG_END, first half: take the finished upload out of `pending`. Returns (meta, bytes, claimed sha256).

        Raises BoardError (and drops the upload) if chunks are missing.
        """
        if not isinstance(p, dict) or not kinds_image.is_asset_id(p.get('asset_id')):
            raise BoardError(protocol.BAD_FORMAT, 'asset_id must be the sha256 of the picture')
        up = self.pending.pop((user, p['asset_id']), None)
        if up is None:
            raise BoardError(protocol.NOT_FOUND, 'no upload in progress for this asset')
        meta = up['meta']
        if up['next'] != kinds_image.chunk_count(meta['size']) or len(up['buf']) != meta['size']:
            raise BoardError(protocol.BAD_FORMAT, 'IMG_END before all %d chunks arrived' % kinds_image.chunk_count(meta['size']))
        return meta, bytes(up['buf']), p.get('sha256')

    def add(self, meta, data):
        """IMG_END, second half: store a verified picture (nothing changes if it is already there). Returns the meta."""
        old = self.assets.get(meta['asset_id'])
        if old is not None:
            return old['meta']
        if self.total + len(data) > MAX_ROOM_ASSET_BYTES:
            raise BoardError(protocol.TOO_LARGE, 'this room has no space left for pictures')
        self.assets[meta['asset_id']] = {'meta': meta, 'data': data, 'on_disk': False}
        self.total += len(data)
        self.rev += 1
        return meta

    def drop_pending_of(self, user):
        """Throw away the unfinished uploads of one user (it left the room)."""
        for key in [k for k in self.pending if k[0] == user]:
            del self.pending[key]

    # ---------------------------------------------------------------- reading
    def has(self, asset_id):
        """True if the picture is stored."""
        return asset_id in self.assets

    def get(self, asset_id):
        """The stored entry {'meta', 'data', 'on_disk'} or None. The data is immutable bytes."""
        return self.assets.get(asset_id)

    def meta_list(self):
        """Metadata of every stored picture, oldest first (goes into autosave.json)."""
        return [dict(a['meta']) for a in self.assets.values()]

    # ---------------------------------------------------------------- disk (called by server/versions.py)
    def pending_files(self):
        """The entries whose file is not on disk yet; takes room.lock."""
        with self.room.lock:
            return [a for a in self.assets.values() if not a['on_disk']]

    def load(self, metas, folder):
        """Load the pictures listed in an autosave.json from `folder`. Returns how many were loaded.

        The file name is the asset id, so the bytes are checked against it: a
        missing, short, damaged or doctored file is skipped (the object that
        uses it then shows a placeholder instead of a wrong picture).
        """
        loaded = 0
        for m in metas if isinstance(metas, list) else []:
            if not (isinstance(m, dict) and kinds_image.is_asset_id(m.get('asset_id')) and m.get('mime') in kinds_image.MIMES
                    and _is_int(m.get('size')) and _is_int(m.get('w')) and _is_int(m.get('h'))):
                continue
            try:
                with open(os.path.join(folder, m['asset_id'] + '.bin'), 'rb') as f:
                    data = f.read(kinds_image.MAX_IMAGE_BYTES + 1)
            except OSError:
                continue
            if (len(data) != m['size'] or hashlib.sha256(data).hexdigest() != m['asset_id']
                    or not kinds_image.mime_matches(m['mime'], data) or m['asset_id'] in self.assets):
                continue
            meta = {'asset_id': m['asset_id'], 'name': clean_name(m.get('name')), 'mime': m['mime'], 'size': len(data),
                    'w': m['w'], 'h': m['h'], 'by': m.get('by'), 'ts': m.get('ts')}
            self.assets[m['asset_id']] = {'meta': meta, 'data': data, 'on_disk': True}
            self.total += len(data)
            loaded += 1
        return loaded


# ---------------------------------------------------------------- handlers

def _fail(session, exc, ref_type):
    """Answer a rejected request with ERROR to the sender only (contract B)."""
    session.send_error(exc.code, exc.text, ref_type)


def _upload_step(session, msg, ref_type, step):
    """Common frame of IMG_BEGIN and IMG_CHUNK: permission check, then step(store, user, payload) under room.lock."""
    room = session.room
    try:
        if not permissions.can(session.member.role, 'upload'):
            raise BoardError(protocol.FORBIDDEN, '%s is not allowed to upload' % session.member.role)
        with room.lock:
            step(room.assets, msg['user'], msg['payload'])
    except BoardError as exc:
        _fail(session, exc, ref_type)


@handler(protocol.IMG_BEGIN)
def on_img_begin(session, msg):
    """IMG_BEGIN: open an upload (see AssetStore.begin for what is checked)."""
    _upload_step(session, msg, protocol.IMG_BEGIN, AssetStore.begin)


@handler(protocol.IMG_CHUNK)
def on_img_chunk(session, msg):
    """IMG_CHUNK: append one piece (see AssetStore.chunk)."""
    _upload_step(session, msg, protocol.IMG_CHUNK, AssetStore.chunk)


@handler(protocol.IMG_END)
def on_img_end(session, msg):
    """IMG_END: verify size, sha256 and file type, store the picture and answer IMG_OK.

    The upload is taken out of the pending table under room.lock; the hash
    of the (up to 5 MB) picture is computed OUTSIDE the lock so that nobody
    waits for it; the result is stored under the lock again.
    """
    room, user = session.room, msg['user']
    try:
        if not permissions.can(session.member.role, 'upload'):
            raise BoardError(protocol.FORBIDDEN, '%s is not allowed to upload' % session.member.role)
        with room.lock:
            meta, data, claimed = room.assets.finish(user, msg['payload'])
        digest = hashlib.sha256(data).hexdigest()
        if claimed != digest or meta['asset_id'] != digest:
            raise BoardError(protocol.BAD_FORMAT, 'sha256 does not match the received bytes')
        if not kinds_image.mime_matches(meta['mime'], data):
            raise BoardError(protocol.BAD_FORMAT, 'the bytes are not a %s picture' % meta['mime'])
        with room.lock:
            room.assets.add(meta, data)
        versions.request_save()
        session.send(protocol.IMG_OK, {'asset_id': meta['asset_id']})
    except BoardError as exc:
        _fail(session, exc, protocol.IMG_END)


@handler(protocol.ASSET_GET)
def on_asset_get(session, msg):
    """ASSET_GET {asset_id}: stream IMG_BEGIN, IMG_CHUNK x n, IMG_END to the asker. Any member may ask."""
    room = session.room
    asset_id = msg['payload'].get('asset_id') if isinstance(msg['payload'], dict) else None
    if not kinds_image.is_asset_id(asset_id):
        session.send_error(protocol.BAD_FORMAT, 'asset_id must be the sha256 of the picture', protocol.ASSET_GET)
        return
    with room.lock:
        asset = room.assets.get(asset_id)
    if asset is None:
        session.send_error(protocol.NOT_FOUND, 'no such picture in this room', protocol.ASSET_GET)
        return
    meta, data = asset['meta'], asset['data']       # immutable: safe to use after the lock is released
    n = kinds_image.chunk_count(len(data))
    session.send(protocol.IMG_BEGIN, {'asset_id': asset_id, 'name': meta['name'], 'mime': meta['mime'],
                                      'size': len(data), 'chunks': n, 'w': meta['w'], 'h': meta['h']})
    for idx in range(n):
        if getattr(session, "closed", False):
            return                                  # the asker is gone: stop sending
        piece = data[idx * kinds_image.CHUNK_BYTES:(idx + 1) * kinds_image.CHUNK_BYTES]
        session.send(protocol.IMG_CHUNK, {'asset_id': asset_id, 'idx': idx, 'data': base64.b64encode(piece).decode('ascii')})
    session.send(protocol.IMG_END, {'asset_id': asset_id, 'sha256': hashlib.sha256(data).hexdigest()})


# ---------------------------------------------------------------- room hooks

@rooms.on_room_created
def attach_assets(room):
    """Hook: give every new room its AssetStore (room.assets, contract F)."""
    room.assets = AssetStore(room)


@rooms.on_member_left
def drop_unfinished_uploads(room, member):
    """Hook: a member left (or went offline): forget its unfinished uploads. Runs under room.lock."""
    room.assets.drop_pending_of(member.name)
