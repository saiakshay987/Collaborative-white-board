# OWNER: P3
"""board_state.py: Server-side board state of ONE room (CONTRACT sections C2, C3, D, E).

BoardState keeps the ordered objects, the seq counter and the log of accepted
operations. It is plain Python with no I/O and NO locking of its own: the
caller (server/board_ops.py, server/sync.py) holds room.lock around apply()
and around everything that reads the state, so operations are processed one
at a time per room and the seq order is the truth (contract E1).

Other modules use it like this:
    prev = state.get_copy(obj_id)    # before applying (needed by undo.record)
    env = state.apply(env)           # stamps seq, owner and z; appends to the log
    state.snapshot_payload()         # SNAPSHOT payload for a joining client
    state.delta(from_seq)            # SYNC_DELTA ops, or None -> send a SNAPSHOT
"""
import base64
import copy
import hashlib
import json
import zlib
from collections import OrderedDict

from common import config

# Limits of contract section D / C2. MAX_DELTA comes from config so the
# server and the contract can never disagree; the others are not in config.
MAX_DELTA = config.MAX_DELTA
MAX_OBJECTS = 20000
MAX_POINTS = 20000
MAX_PART_POINTS = 50
# Entries kept in the log. Must be >= MAX_DELTA; older ones can no longer
# be served as a delta anyway (the client gets a SNAPSHOT instead).
LOG_LIMIT = 1000

OP_ADD = 'OP_ADD'
OP_STROKE_PART = 'OP_STROKE_PART'
OP_UPDATE = 'OP_UPDATE'
OP_DELETE = 'OP_DELETE'
OP_CLEAR = 'OP_CLEAR'

# Fields OP_UPDATE may never touch (identity and server-owned fields).
PROTECTED_FIELDS = frozenset({'id', 'kind', 'owner', 'z'})

# Style a stroke gets for every field its first OP_STROKE_PART leaves out.
STROKE_DEFAULTS = {'tool': 'pen', 'color': '#000000', 'width': 2, 'alpha': 1}


class BoardError(Exception):
    """A rejected operation. `code` is a CONTRACT section B error code.

    board_ops turns it into ERROR {code, text, ref_type} for the sender only.
    """

    def __init__(self, code, text):
        """Remember the error code (FORBIDDEN, NOT_FOUND, ...) and a human text."""
        super().__init__(f'{code}: {text}')
        self.code = code
        self.text = text


class BoardState:
    """Objects, seq counter and operation log of one room."""

    def __init__(self):
        """Start with an empty board at seq 0."""
        self.objects = OrderedDict()   # obj_id -> obj, in paint (z) order
        self.seq = 0                   # seq of the last accepted operation
        self.log = []                  # accepted envelopes, consecutive seq

    # ------------------------------------------------------------------ apply
    def apply(self, envelope, restore=False):
        """Apply one board operation and append it to the log.

        The envelope gets its seq stamped in place if it has none (it must be
        exactly self.seq + 1 if it has one). Server-set fields are written into
        the payload (owner and z on OP_ADD) so the returned envelope, which is
        also what the log keeps, is what should be broadcast.

        restore=True is for server-generated inverse operations from undo.py:
        an OP_ADD then keeps the owner and z of the object it brings back.

        A rejected operation raises BoardError and changes nothing (no state,
        no seq, no log entry).
        """
        handler = self._HANDLERS.get(envelope.get('type'))
        if handler is None:
            raise BoardError('BAD_FORMAT', f"not a board operation: {envelope.get('type')!r}")
        expected = self.seq + 1
        seq = envelope.get('seq')
        if seq is None:
            seq = expected
        elif seq != expected:
            raise ValueError(f'seq {seq} out of order, expected {expected}')
        payload = envelope.get('payload')
        if not isinstance(payload, dict):
            raise BoardError('BAD_FORMAT', 'payload must be an object')

        handler(self, envelope, payload, seq, restore)   # validates, then mutates

        envelope['seq'] = seq
        self.seq = seq
        self.log.append(copy.deepcopy(envelope))
        if len(self.log) > LOG_LIMIT:
            del self.log[:len(self.log) - LOG_LIMIT]
        return envelope

    def _add(self, env, p, seq, restore):
        """OP_ADD: store a complete object; the server sets owner and z.

        A client-sent owner or z is overwritten (never trust the client).
        With restore=True (undo of a delete) the old owner and z are kept
        and the object goes back to its old place in the paint order.
        """
        obj = p.get('obj')
        if not isinstance(obj, dict) or not isinstance(obj.get('id'), str) or not obj.get('kind'):
            raise BoardError('BAD_FORMAT', 'OP_ADD needs obj with id and kind')
        if obj['id'] in self.objects:
            raise BoardError('BAD_FORMAT', f"object {obj['id']} already exists")
        if len(self.objects) >= MAX_OBJECTS:
            raise BoardError('TOO_LARGE', f'more than {MAX_OBJECTS} objects')
        if isinstance(obj.get('points'), list) and len(obj['points']) > MAX_POINTS:
            raise BoardError('TOO_LARGE', f'more than {MAX_POINTS} points')
        new = copy.deepcopy(obj)
        if not (restore and 'owner' in new and 'z' in new):
            new['owner'] = env.get('user')
            new['z'] = seq
        self.objects[new['id']] = new
        if restore:
            self._sort_by_z()
        p['obj'] = copy.deepcopy(new)   # the broadcast carries owner and z

    def _stroke_part(self, env, p, seq, restore):
        """OP_STROKE_PART: the first part creates the stroke, later parts append.

        Every part is its own accepted operation with its own seq; z is the
        seq of the FIRST part. Only the stroke's owner may append to it.
        """
        oid = p.get('obj_id')
        pts = p.get('points')
        if not isinstance(oid, str) or not isinstance(pts, list):
            raise BoardError('BAD_FORMAT', 'OP_STROKE_PART needs obj_id and points')
        if len(pts) > MAX_PART_POINTS:
            raise BoardError('TOO_LARGE', f'more than {MAX_PART_POINTS} points in one part')
        obj = self.objects.get(oid)
        if obj is None:
            style = p.get('style')
            if not isinstance(style, dict):
                # A later part for a stroke that is gone (erased, cleared).
                raise BoardError('NOT_FOUND', f'stroke {oid} does not exist')
            if len(self.objects) >= MAX_OBJECTS:
                raise BoardError('TOO_LARGE', f'more than {MAX_OBJECTS} objects')
            new = {'id': oid, 'kind': 'stroke', 'owner': env.get('user'), 'z': seq,
                   'points': copy.deepcopy(pts)}
            for key, default in STROKE_DEFAULTS.items():
                new[key] = style.get(key, default)
            self.objects[oid] = new
        else:
            if obj.get('kind') != 'stroke':
                raise BoardError('BAD_FORMAT', f'{oid} is not a stroke')
            if obj.get('owner') != env.get('user'):
                raise BoardError('FORBIDDEN', f'{oid} belongs to {obj.get("owner")}')
            if len(obj['points']) + len(pts) > MAX_POINTS:
                raise BoardError('TOO_LARGE', f'more than {MAX_POINTS} points in a stroke')
            obj['points'].extend(copy.deepcopy(pts))

    def _update(self, env, p, seq, restore):
        """OP_UPDATE: overwrite the given fields of one object.

        Two edits to the same object: the later seq wins, field by field
        (contract E1). id, kind, owner and z can never be changed.
        """
        oid = p.get('obj_id')
        fields = p.get('set')
        if not isinstance(oid, str) or not isinstance(fields, dict) or not fields:
            raise BoardError('BAD_FORMAT', 'OP_UPDATE needs obj_id and a non-empty set')
        obj = self.objects.get(oid)
        if obj is None:
            raise BoardError('NOT_FOUND', f'object {oid} does not exist')
        bad = PROTECTED_FIELDS.intersection(fields)
        if bad:
            raise BoardError('FORBIDDEN', f'cannot update {sorted(bad)}')
        obj.update(copy.deepcopy(fields))

    def _delete(self, env, p, seq, restore):
        """OP_DELETE: remove the whole object (object eraser)."""
        oid = p.get('obj_id')
        if oid not in self.objects:
            raise BoardError('NOT_FOUND', f'object {oid!r} does not exist')
        del self.objects[oid]

    def _clear(self, env, p, seq, restore):
        """OP_CLEAR: remove every object. The caller checked that the sender is host."""
        self.objects.clear()

    # Message type -> method. Filled after the methods exist.
    _HANDLERS = {
        OP_ADD: _add,
        OP_STROKE_PART: _stroke_part,
        OP_UPDATE: _update,
        OP_DELETE: _delete,
        OP_CLEAR: _clear,
    }

    def _sort_by_z(self):
        """Re-order self.objects by z (used when undo brings an old object back)."""
        ordered = sorted(self.objects.values(), key=lambda o: o.get('z', 0))
        self.objects.clear()
        for o in ordered:
            self.objects[o['id']] = o

    def get_copy(self, obj_id):
        """Deep copy of an object, or None. Take this BEFORE apply() to get the
        prev_obj that UndoManager.record() needs."""
        obj = self.objects.get(obj_id)
        return copy.deepcopy(obj) if obj is not None else None

    # --------------------------------------------------------------- snapshots
    def snapshot_payload(self):
        """SNAPSHOT payload: {seq, enc, data}, data = base64(zlib(JSON {objects})).

        Objects are sorted by z so the client can paint them in order. zlib
        shrinks the JSON a lot (many similar objects) and base64 keeps the
        binary result inside a JSON string.
        """
        body = {'objects': sorted(self.objects.values(), key=lambda o: o.get('z', 0))}
        raw = json.dumps(body, separators=(',', ':')).encode('utf-8')
        return {'seq': self.seq, 'enc': 'zlib-b64',
                'data': base64.b64encode(zlib.compress(raw)).decode('ascii')}

    def load_snapshot(self, payload):
        """Replace the whole state with a snapshot payload. The log restarts
        empty, so deltas from before this seq are refused."""
        self.restore(decode_snapshot(payload), payload['seq'])

    def restore(self, objects, seq):
        """Replace the whole state with a list of objects and a seq counter.

        Used for a snapshot and for loading an autosave file after a server
        restart (server/versions.py). The objects are put in z order. The log
        restarts empty, so a client that was at an older seq gets a SNAPSHOT
        instead of a SYNC_DELTA (delta() cannot serve it), and the next
        accepted operation continues with seq + 1.
        """
        self.objects.clear()
        for obj in sorted(objects, key=lambda o: o.get('z', 0)):
            self.objects[obj['id']] = obj
        self.seq = seq
        self.log = []

    def checksum(self):
        """Fingerprint of the whole board: sha1 of the objects as sorted JSON.

        Objects are sorted by id and keys by name, so two boards with the
        same content give the same string whatever the insertion order.
        client/board_model.py computes it the same way; when two clients
        and the server are at the same seq the three values must be equal.
        """
        return objects_checksum(self.objects)

    # ------------------------------------------------------------------ delta
    def delta(self, from_seq):
        """Log entries with seq > from_seq, or None if the client has to take a
        snapshot: gap above MAX_DELTA, or from_seq not covered by the log."""
        if not isinstance(from_seq, int) or isinstance(from_seq, bool):
            return None
        if from_seq > self.seq or self.seq - from_seq > MAX_DELTA:
            return None
        first = self.log[0]['seq'] if self.log else self.seq + 1
        if from_seq < first - 1:
            return None
        return copy.deepcopy(self.log[from_seq - (first - 1):])


def objects_checksum(objects):
    """sha1 hex of the sorted JSON of a dict obj_id -> object (see BoardState.checksum).

    The client keeps an identical copy of this function in
    client/board_model.py (the client must not import server code);
    p3_test_board_model.py checks that the two agree.
    """
    blob = json.dumps(sorted(objects.values(), key=lambda o: o['id']),
                      sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()


def decode_snapshot(payload):
    """Inverse of snapshot_payload(): returns the list of objects."""
    if payload.get('enc') != 'zlib-b64':
        raise ValueError(f"unknown snapshot encoding {payload.get('enc')!r}")
    raw = zlib.decompress(base64.b64decode(payload['data']))
    return json.loads(raw.decode('utf-8'))['objects']
