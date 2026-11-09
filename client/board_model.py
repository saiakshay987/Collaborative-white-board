# OWNER: P3
"""board_model.py: BoardModel, the client's copy of the board (contract sections E2, E3 and F).

Data flow (everything here runs on the GUI thread, because net_client delivers
incoming messages through a Qt signal, so no locking is needed):

    my tool --send_op--> apply_local()  applies AT ONCE (optimistic) and remembers the op_id as pending
                         net.send()     the server accepts it, stamps seq/owner/z and broadcasts it
    server echo ------>  apply_remote() op_id is pending -> only CONFIRM it (never draw it twice)
    other user's op -->  apply_remote() op_id unknown    -> apply it
    SNAPSHOT --------->  load_snapshot() replace the whole board
    SYNC_DELTA ------->  apply every missed op in order

Widgets never read the network: they listen to the `changed` signal and read
`objects`. This first version applies messages in arrival order (TCP keeps
them in order) and drops duplicates by seq; gap buffering and SYNC_REQUEST
(contract E2) come later.
"""
import base64
import copy
import json
import zlib
from collections import OrderedDict

from PyQt5.QtCore import QObject, pyqtSignal

from common import protocol
from client.registry import on_message

# The operations that change objects. UNDO and REDO are not in this set: the
# server answers them with ordinary OP_* messages (contract C2).
OBJECT_OPS = (protocol.OP_ADD, protocol.OP_STROKE_PART, protocol.OP_UPDATE,
              protocol.OP_DELETE, protocol.OP_CLEAR)

# Fields OP_UPDATE may never change; mirrors the server's rule.
PROTECTED_FIELDS = frozenset(("id", "kind", "owner", "z"))

# Style a stroke gets for every field its first part leaves out (same as the server).
STROKE_DEFAULTS = {"tool": "pen", "color": "#000000", "width": 2, "alpha": 1}


def decode_snapshot(payload):
    """Return the list of objects inside a SNAPSHOT payload {seq, enc, data}.

    data is base64(zlib(JSON {objects:[...]})) (contract C3). Raises
    ValueError for an unknown encoding.
    """
    if payload.get("enc") != "zlib-b64":
        raise ValueError("unknown snapshot encoding %r" % (payload.get("enc"),))
    raw = zlib.decompress(base64.b64decode(payload["data"]))
    return json.loads(raw.decode("utf-8"))["objects"]


class BoardModel(QObject):
    """The client's copy of the board (contract section F, client).

    objects   OrderedDict obj_id -> object dict (the renderer paints them in z order)
    last_seq  seq of the last server operation applied; sent on reconnect
    pending   op_id -> type of my operations the server has not echoed yet
    changed   signal(obj_id or None): None means "many or all objects changed"
    """

    changed = pyqtSignal(object)

    def __init__(self, parent=None):
        """Start with an empty board that is not yet attached to an app."""
        super().__init__(parent)
        self.objects = OrderedDict()
        self.last_seq = 0
        self.pending = OrderedDict()
        self.app = None

    def attach(self, app):
        """Remember the AppState and publish this model as app.board (idempotent)."""
        self.app = app
        app.board = self

    # ------------------------------------------------------------------ apply
    def _apply(self, type, payload, user=None, seq=None):
        """Apply one operation to self.objects. Returns (changed, obj_id).

        user is the author (new strokes get it as owner); seq, when known, is
        the z of a new stroke. obj_id is None for OP_CLEAR. Everything stored
        is copied, so a tool that keeps editing the dict it passed to
        send_op can never alter the board behind our back.
        """
        if type == protocol.OP_ADD:
            obj = payload.get("obj")
            if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                self.objects[obj["id"]] = copy.deepcopy(obj)
                return True, obj["id"]
        elif type == protocol.OP_STROKE_PART:
            return self._apply_stroke_part(payload, user, seq)
        elif type == protocol.OP_UPDATE:
            obj = self.objects.get(payload.get("obj_id"))
            fields = payload.get("set")
            if obj is not None and isinstance(fields, dict):
                obj.update(copy.deepcopy({k: v for k, v in fields.items() if k not in PROTECTED_FIELDS}))
                return True, obj["id"]
        elif type == protocol.OP_DELETE:
            if self.objects.pop(payload.get("obj_id"), None) is not None:
                return True, payload["obj_id"]
        elif type == protocol.OP_CLEAR:
            self.objects.clear()
            return True, None
        return False, None

    def _apply_stroke_part(self, payload, user, seq):
        """OP_STROKE_PART: the first part (with a style) creates the stroke, later parts append."""
        oid, points = payload.get("obj_id"), payload.get("points")
        if not isinstance(oid, str) or not isinstance(points, list):
            return False, None
        obj = self.objects.get(oid)
        if obj is None:
            style = payload.get("style")
            if not isinstance(style, dict):
                return False, None      # a later part of a stroke that is gone
            obj = {"id": oid, "kind": "stroke", "owner": user, "z": seq, "points": []}
            for key, default in STROKE_DEFAULTS.items():
                obj[key] = style.get(key, default)
            self.objects[oid] = obj
        elif obj.get("kind") != "stroke":
            return False, None
        obj["points"].extend(copy.deepcopy(points))
        return True, oid

    def apply_local(self, type, payload, op_id):
        """Apply my own operation at once (optimistic) and remember op_id as pending.

        Called by client/ops.send_op just before the message is sent. The
        object has no z and no seq yet; the server echo adds them (see
        _confirm). Painting such objects last, on top, is render.py's job.
        """
        if type not in OBJECT_OPS or not isinstance(payload, dict):
            return          # UNDO/REDO: nothing to apply, the server sends the result
        user = self.app.me.username if self.app is not None else None
        done, oid = self._apply(type, payload, user=user)
        if op_id is not None:
            self.pending[op_id] = type
        if done:
            self.changed.emit(oid)

    def apply_remote(self, msg):
        """Handle one OP_* envelope from the server.

        * op_id is pending: it is my own operation coming back. It is already
          drawn, so only confirm it (_confirm) and advance last_seq.
        * seq <= last_seq: a duplicate (for example already inside a SNAPSHOT); dropped.
        * otherwise: apply it, in arrival order, and advance last_seq.
        """
        type, payload, seq = msg.get("type"), msg.get("payload"), msg.get("seq")
        if type not in OBJECT_OPS or not isinstance(payload, dict):
            return
        op_id = msg.get("op_id")
        if op_id is not None and op_id in self.pending:
            del self.pending[op_id]
            self._confirm(type, payload, msg)
        elif seq is not None and seq <= self.last_seq:
            return
        else:
            done, oid = self._apply(type, payload, user=msg.get("user"), seq=seq)
            if done:
                self.changed.emit(oid)
        if seq is not None and seq > self.last_seq:
            self.last_seq = seq

    def _confirm(self, type, payload, msg):
        """The server echoed one of my pending operations: do not apply it again.

        Two small repairs keep my copy equal to the server's:
        * OP_ADD / first OP_STROKE_PART: copy in the fields only the server
          sets, owner and z (z is the paint order).
        * OP_UPDATE: re-assert the fields. Another user's update with a LOWER
          seq may have arrived after my optimistic apply and overwritten it,
          but the server order says mine came later and wins (contract E1).
          Setting the same values again is idempotent, nothing is drawn twice.
        """
        if type == protocol.OP_ADD:
            echo = payload.get("obj") or {}
            local = self.objects.get(echo.get("id"))
            if local is not None:
                local["owner"], local["z"] = echo.get("owner"), echo.get("z")
                self.changed.emit(local["id"])
        elif type == protocol.OP_STROKE_PART:
            local = self.objects.get(payload.get("obj_id"))
            if local is not None and local.get("z") is None and msg.get("seq") is not None:
                local["owner"], local["z"] = msg.get("user"), msg["seq"]
                self.changed.emit(local["id"])
        elif type == protocol.OP_UPDATE:
            done, oid = self._apply(type, payload)
            if done:
                self.changed.emit(oid)

    # --------------------------------------------------------------- snapshots
    def load_snapshot(self, payload):
        """Replace the whole board with a SNAPSHOT payload and set last_seq (contract C3)."""
        objects = decode_snapshot(payload)
        self.objects = OrderedDict((o["id"], o) for o in objects)
        self.last_seq = payload["seq"]
        self.changed.emit(None)

    def apply_delta(self, payload):
        """Apply the ops of a SYNC_DELTA {from_seq, to_seq, ops} in order.

        Ops already applied are dropped by apply_remote (seq <= last_seq), so
        an overlap is harmless. One `changed(None)` is emitted at the end
        instead of one per op, and last_seq moves up to to_seq.
        """
        was_blocked = self.blockSignals(True)       # collect: no signal per op
        try:
            for op in payload.get("ops") or []:
                self.apply_remote(op)
        finally:
            self.blockSignals(was_blocked)
        to_seq = payload.get("to_seq")
        if isinstance(to_seq, int) and to_seq > self.last_seq:
            self.last_seq = to_seq
        self.changed.emit(None)


# ---------------------------------------------------------------------- handlers
# net_client calls these on the GUI thread through registry.deliver_message.

def model_of(app):
    """Return app.board as an attached BoardModel, creating it if run_client did not."""
    board = app.board if isinstance(app.board, BoardModel) else BoardModel()
    if board.app is not app:
        board.attach(app)
    return board


def _on_op(app, msg):
    """Any OP_* message from the server."""
    model_of(app).apply_remote(msg)


for _op_type in OBJECT_OPS:
    on_message(_op_type)(_on_op)


@on_message(protocol.SNAPSHOT)
def _on_snapshot(app, msg):
    """SNAPSHOT: replace the whole board."""
    model_of(app).load_snapshot(msg.get("payload") or {})


@on_message(protocol.SYNC_DELTA)
def _on_sync_delta(app, msg):
    """SYNC_DELTA: apply the operations missed while offline."""
    model_of(app).apply_delta(msg.get("payload") or {})
