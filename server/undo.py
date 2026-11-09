# OWNER: P3
"""undo.py: Per-user undo/redo (CONTRACT section E6).

UNDO reverts only the sender's own most recent undoable action (add or
stroke, update, delete). The answer is an ordinary inverse OP_* envelope that
the server applies and broadcasts with a fresh seq, so clients need no undo
logic at all. OP_CLEAR is not undoable.

Usage by board_ops, under room.lock:

    prev = state.get_copy(obj_id)          # before applying (None for adds)
    env = state.apply(env)
    undo.record(user, env, prev)           # only for ops that came from a client

    inv = undo.undo(user, state)           # None -> reply ERROR NOT_FOUND
    if inv: inv = state.apply(inv, restore=True)   # then broadcast; do NOT record()

Inverse operations must not be passed to record(): that would wipe the redo
stack they just filled.
"""
import copy
from collections import deque

from server.board_state import OP_ADD, OP_STROKE_PART, OP_UPDATE, OP_DELETE, OP_CLEAR

MAX_HISTORY = 200   # undo steps remembered per user


def _envelope(type_, room, user, payload):
    """Build a server-made envelope for an inverse operation.

    seq, ts and op_id are None here: the server fills seq in when it applies
    the envelope and ts when it sends it.
    """
    return {'v': 1, 'type': type_, 'room': room, 'user': user,
            'op_id': None, 'seq': None, 'ts': None, 'payload': payload}


class UndoManager:
    """Undo and redo stacks of every user in ONE room.

    Entries describe the ORIGINAL action: {'op': 'add'|'delete'|'update',
    'room', 'obj_id', 'obj', 'old', 'new'}. Undo and redo apply the opposite
    or the same direction of that action.
    """

    def __init__(self, limit=MAX_HISTORY):
        """Create empty stacks; each user keeps at most `limit` steps."""
        self._limit = limit
        self._undo = {}   # user -> deque of entries, newest on the right
        self._redo = {}

    def reset(self):
        """Forget everything (after OP_CLEAR or VERSION_RESTORE)."""
        self._undo.clear()
        self._redo.clear()

    # ----------------------------------------------------------------- record
    def record(self, user, envelope, prev_obj):
        """Call after an accepted op. prev_obj is the object before the op
        (None for adds and for the first stroke part).

        A new action clears this user's redo stack (contract E6).
        """
        t = envelope['type']
        p = envelope.get('payload') or {}
        if t == OP_CLEAR:
            self.reset()    # nothing before a clear can be undone sensibly
            return
        if t == OP_ADD:
            entry = {'op': 'add', 'obj_id': p['obj']['id'], 'obj': None}
        elif t == OP_STROKE_PART:
            if prev_obj is not None:
                return      # a later part of a stroke already recorded
            entry = {'op': 'add', 'obj_id': p['obj_id'], 'obj': None}
        elif t == OP_UPDATE:
            if prev_obj is None:
                return
            fields = p['set']
            entry = {'op': 'update', 'obj_id': p['obj_id'],
                     'old': {k: copy.deepcopy(prev_obj.get(k)) for k in fields},
                     'new': copy.deepcopy(fields)}
        elif t == OP_DELETE:
            if prev_obj is None:
                return
            entry = {'op': 'delete', 'obj_id': p['obj_id'], 'obj': copy.deepcopy(prev_obj)}
        else:
            return
        entry['room'] = envelope.get('room')
        self._push(self._undo, user, entry)
        self._redo.pop(user, None)      # a new action ends the redo chain

    # --------------------------------------------------------- undo and redo
    def undo(self, user, state):
        """Return the inverse envelope for user's last action, or None."""
        return self._step(user, state, self._undo, self._redo, undoing=True)

    def redo(self, user, state):
        """Return the envelope that repeats user's last undone action, or None."""
        return self._step(user, state, self._redo, self._undo, undoing=False)

    def _step(self, user, state, src, dst, undoing):
        """Pop entries from src until one can be applied; push its counterpart on dst.

        An entry whose target object no longer exists is dropped and the
        previous entry is tried (contract E6). None means nothing is left.
        """
        stack = src.get(user)
        while stack:
            result = self._invert(stack.pop(), user, state, undoing)
            if result is None:
                continue        # target is gone: drop the entry, try the previous one
            envelope, counter = result
            self._push(dst, user, counter)
            return envelope
        return None

    def _invert(self, entry, user, state, undoing):
        """Returns (envelope to apply, entry for the opposite stack) or None."""
        op, oid, room = entry['op'], entry['obj_id'], entry['room']
        exists = oid in state.objects
        if op == 'add':
            if undoing:     # remove it, remembering its current form for redo
                if not exists:
                    return None
                counter = dict(entry, obj=state.get_copy(oid))
                return _envelope(OP_DELETE, room, user, {'obj_id': oid}), counter
            if exists or entry.get('obj') is None:
                return None
            return (_envelope(OP_ADD, room, user, {'obj': copy.deepcopy(entry['obj'])}),
                    dict(entry, obj=None))
        if op == 'delete':
            if undoing:     # bring the old object back
                if exists:
                    return None
                return _envelope(OP_ADD, room, user, {'obj': copy.deepcopy(entry['obj'])}), entry
            if not exists:
                return None
            return (_envelope(OP_DELETE, room, user, {'obj_id': oid}),
                    dict(entry, obj=state.get_copy(oid)))
        if op == 'update':
            if not exists:
                return None
            fields = entry['old'] if undoing else entry['new']
            return _envelope(OP_UPDATE, room, user,
                             {'obj_id': oid, 'set': copy.deepcopy(fields)}), entry
        return None

    def _push(self, stacks, user, entry):
        """Append entry to user's stack in `stacks`, creating it on first use."""
        stack = stacks.get(user)
        if stack is None:
            stack = stacks[user] = deque(maxlen=self._limit)
        stack.append(entry)
