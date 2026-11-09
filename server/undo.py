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

# =============================================================================
# UNDO RULES (contract E6). Read this before changing anything below.
#
# 1. Own actions only. Every user has a private undo stack and a private redo
#    stack, keyed by the user name the SERVER stamped on the message. UNDO
#    reverts the sender's own most recent undoable action and never anybody
#    else's, so "A presses Ctrl+Z" cannot remove what B drew. The stacks keep
#    at most MAX_HISTORY steps per user (older ones fall off the left end).
#
# 2. What is undoable: add (OP_ADD, or a whole stroke = its first
#    OP_STROKE_PART), update (OP_UPDATE) and delete (OP_DELETE). A stroke is
#    ONE step however many parts it was sent in. Undo of an add deletes the
#    object, undo of a delete adds the saved copy back (same owner, same z, so
#    it returns to its old place in the paint order), undo of an update puts
#    the old values of exactly the fields that update changed back.
#
# 3. Redo. Undoing a step moves it to the redo stack; REDO repeats it. Any NEW
#    action of the same user empties that user's redo stack (record() below),
#    as in every editor, because the redo chain no longer fits the board.
#
# 4. Somebody else changed or deleted the object meanwhile.
#    * The object is gone (deleted by another user, or by the user's own
#      later delete): the step cannot be applied, so it is DROPPED and the
#      previous step is tried instead (_step). With no step left, the server
#      answers ERROR NOT_FOUND ("nothing to undo").
#    * The object still exists but another user edited it after our action:
#      the undo is applied anyway. It is a normal operation with a new seq,
#      and "the later seq wins, field by field" (contract E1) decides the
#      result. Example: we moved a shape, then B moved it again and recoloured
#      it. Our undo moves it back to where it was before OUR move (B's move is
#      overwritten, it has the lower seq), but B's colour stays, because only
#      the fields our update changed are restored. An undo is never refused
#      just because somebody else touched the object.
#
# 5. Why OP_CLEAR is not undoable. A clear removes every object of every user,
#    so every step on every stack would point at an object that is gone, and
#    an undo of a "delete" step would resurrect single objects out of a
#    cleared board, which looks like a bug. record() therefore empties ALL
#    stacks on a clear. Getting the board back is a job for versions: the
#    server takes an automatic version just before it applies OP_CLEAR
#    (contract E6, server/versions.py) and the host restores it.
#
# 6. The answer to UNDO/REDO is an ordinary OP_ADD / OP_UPDATE / OP_DELETE
#    with a fresh seq and payload field cause:'undo' or 'redo' (built in
#    server/board_ops.py). Clients apply it like any remote operation and
#    need no undo code. Inverse operations are never passed to record().
# =============================================================================


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
