# OWNER: P3
"""board_model.py: BoardModel: objects, last_seq, apply_local/apply_remote, gap buffering, SYNC_REQUEST and snapshot loading; emits changed.

Skeleton stub written by P1 so the client runs. It only stores objects and
emits changed; P3 adds seq ordering, duplicate dropping, pending ops, gap
buffering, SYNC_REQUEST and load_snapshot (contract section E2/E3).
"""

from collections import OrderedDict

from PyQt5.QtCore import QObject, pyqtSignal

from common import protocol


class BoardModel(QObject):
    """The client's copy of the board (contract section F, client)."""

    # obj_id of the changed object, or None when many/all changed.
    changed = pyqtSignal(object)

    def __init__(self, parent=None):
        """Start with an empty board."""
        super().__init__(parent)
        self.objects = OrderedDict()  # obj_id -> obj
        self.last_seq = 0

    def _apply(self, type, payload):
        """Apply one board operation to self.objects; return the touched obj_id or None."""
        if type == protocol.OP_ADD:
            obj = payload.get("obj") or {}
            if "id" in obj:
                self.objects[obj["id"]] = dict(obj)
                return obj["id"]
        elif type == protocol.OP_UPDATE:
            obj = self.objects.get(payload.get("obj_id"))
            if obj is not None:
                obj.update(payload.get("set") or {})
                return obj["id"]
        elif type == protocol.OP_DELETE:
            self.objects.pop(payload.get("obj_id"), None)
            return payload.get("obj_id")
        elif type == protocol.OP_CLEAR:
            self.objects.clear()
        return None

    def apply_local(self, type, payload, op_id):
        """STUB: apply a local operation at once and emit changed."""
        self.changed.emit(self._apply(type, payload))

    def apply_remote(self, msg):
        """STUB: apply a server message (no ordering or dedupe yet) and emit changed."""
        if msg.get("seq") is not None:
            self.last_seq = msg["seq"]
        self.changed.emit(self._apply(msg.get("type"), msg.get("payload") or {}))
