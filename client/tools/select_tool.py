# OWNER: P2
"""select_tool.py: Select tool: select, move, resize, delete, and double-click editing through register_editor.

SelectTool ('select', V), registered with registry.register_tool.

What it does
------------
click          selects the TOPMOST object under the mouse (same hit test and
               paint order as the eraser, pen_tool.topmost_hit). The selection
               is shown with a dashed bounding box (paint_overlay, local only).
               A click on empty board clears the selection.
drag           moves the selected object.
corner drag    resizes rect, ellipse, text, sticky and image (the kinds with
               x, y, w, h); the opposite corner stays where it is.
Delete key     OP_DELETE for the selected object (register_shortcut).
Escape         during a drag: put the object back where it was (one
               OP_UPDATE with the press-time values, only if something
               was already sent); otherwise clear the selection.
double-click   calls the editor registered with register_editor for the
               object's kind (P4 registers text and sticky), as
               fn(app, canvas, obj).

What goes over the network
--------------------------
Every change is an OP_UPDATE {obj_id, set:{...}} through client/ops.send_op,
so it is applied locally at once and the server echo only confirms it:

    move rect/ellipse/text/sticky/image   set {x, y}
    move line/arrow                       set {x1, y1, x2, y2}
    move stroke                           set {points}  (every point offset)
    resize                                set {x, y, w, h}

A drag produces many mouse events (often 100+ per second). Sending one
OP_UPDATE per event would flood the server and every other client, so moves
are THROTTLED: at most one message every THROTTLE_MS (30 ms, about 33 per
second). The newest position always wins: if the mouse moves inside the 30 ms
window, the position is kept and a single-shot QTimer sends it when the window
ends, so the board never stays behind a mouse that stopped. On release one
final OP_UPDATE carries the exact end position, so every client ends up with
the same value.

The geometry is always computed from a copy of the object taken at press
time plus the total mouse offset (not from the previous update), so rounding
never accumulates, and a remote change to the same object between our
updates cannot make the drag jump.

Snap-to-grid: the canvas hands tools snapped points. For a move, the object's
anchor (x, y / x1, y1 / first point) is snapped with canvas.snap() and the
whole object follows by the same offset; a resize puts the dragged corner on
the snapped mouse point. So the server only ever stores snapped values.

Viewers: the tool dock already clears the active tool for a viewer, and
this tool refuses to select (toast) as a second guard. The server checks
permissions.can(role, 'draw') on every OP_UPDATE / OP_DELETE anyway.
"""

import copy
import time

from PyQt5.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QPen

from client import ops, registry
from client.tools.pen_tool import topmost_hit
from common import protocol

# Throttle for move/resize updates while dragging (milliseconds).
THROTTLE_MS = 30

# Sizes on SCREEN (pixels), converted to board units with the zoom, so the
# handles and the click tolerance feel the same at every zoom level.
HIT_TOL_PX = 4          # extra reach around thin lines and strokes when clicking
HANDLE_PX = 8           # side of a resize handle square
HANDLE_HIT_PX = 7       # how far from a corner a press still grabs the handle
BOX_PAD_PX = 4          # gap between the object and its dashed selection box

# Kinds with x, y, w, h that get corner resize handles.
RESIZABLE = ("rect", "ellipse", "text", "sticky", "image")

# Smallest width/height a resize can produce (board units).
MIN_SIZE = 1.0

SELECT_COLOR = QColor("#1565c0")


def _num(value, default=0.0):
    """Return value as a float, or default if it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _r1(value):
    """Round to one decimal (contract D), as an int when it is whole (smaller JSON)."""
    value = round(float(value), 1)
    return int(value) if value.is_integer() else value


def _is_viewer(app):
    """True when the server made me a viewer."""
    return getattr(app.me, "role", None) == "viewer"


def _points(obj):
    """The valid [x, y] points of a stroke (empty list for other kinds)."""
    pts = obj.get("points")
    if not isinstance(pts, list):
        return []
    return [p for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]


def box_of(obj):
    """The x, y, w, h box of a resizable object as a normalized QRectF."""
    return QRectF(_num(obj.get("x")), _num(obj.get("y")),
                  _num(obj.get("w")), _num(obj.get("h"))).normalized()


def tight_bounds(obj):
    """Bounding QRectF of an object in board units (half the line width included), or None."""
    half = _num(obj.get("width"), 0.0) / 2.0
    pts = _points(obj)
    if pts:
        xs, ys = [_num(p[0]) for p in pts], [_num(p[1]) for p in pts]
        rect = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    elif "x1" in obj and "x2" in obj:
        rect = QRectF(QPointF(_num(obj.get("x1")), _num(obj.get("y1"))),
                      QPointF(_num(obj.get("x2")), _num(obj.get("y2")))).normalized()
    elif "x" in obj and "y" in obj:
        return box_of(obj)        # text, sticky and image have no line width
    else:
        return None
    return rect.adjusted(-half, -half, half, half)


def corners(rect):
    """The four corners of rect: top-left, top-right, bottom-right, bottom-left.

    Corner i and corner (i + 2) % 4 are opposite each other.
    """
    return [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()]


def anchor_of(obj):
    """The point a move snaps to the grid: (x, y), (x1, y1) or the first stroke point."""
    pts = _points(obj)
    if pts:
        return QPointF(_num(pts[0][0]), _num(pts[0][1]))
    if "x1" in obj:
        return QPointF(_num(obj.get("x1")), _num(obj.get("y1")))
    return QPointF(_num(obj.get("x")), _num(obj.get("y")))


def moved_fields(obj, dx, dy):
    """The OP_UPDATE 'set' that moves obj (the press-time copy) by (dx, dy)."""
    if obj.get("kind") == "stroke":
        return {"points": [[_r1(_num(p[0]) + dx), _r1(_num(p[1]) + dy)] for p in _points(obj)]}
    if "x1" in obj:
        return {"x1": _r1(_num(obj.get("x1")) + dx), "y1": _r1(_num(obj.get("y1")) + dy),
                "x2": _r1(_num(obj.get("x2")) + dx), "y2": _r1(_num(obj.get("y2")) + dy)}
    return {"x": _r1(_num(obj.get("x")) + dx), "y": _r1(_num(obj.get("y")) + dy)}


def resized_fields(fixed, moving):
    """The OP_UPDATE 'set' for a box between the fixed corner and the dragged corner.

    The box is normalized (the validators require w, h >= 0), so dragging a
    corner past the opposite one flips the box instead of breaking it.
    """
    rect = QRectF(fixed, moving).normalized()
    return {"x": _r1(rect.x()), "y": _r1(rect.y()),
            "w": _r1(max(MIN_SIZE, rect.width())), "h": _r1(max(MIN_SIZE, rect.height()))}


class SelectTool:
    """Select, move, resize, delete and edit (double-click) one board object."""

    id = "select"
    label = "Select"
    shortcut = "V"
    group = "select"

    def __init__(self):
        """Start with nothing selected and no drag."""
        self.selected_id = None      # obj_id of the selected object, or None
        self._canvas = None          # canvas of the current drag (for the timer)
        self._mode = None            # None, "move" or "resize"
        self._orig = None            # deep copy of the object at press time
        self._press = None           # raw board point of the press (move)
        self._fixed = None           # opposite corner (resize)
        self._pending = None         # newest 'set' not sent yet
        self._last_sent = None       # last 'set' sent during this drag
        self._last_time = 0.0        # time.monotonic() of the last send
        self._timer = None           # single-shot QTimer that flushes _pending

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _raw_point(canvas, ev):
        """Unsnapped board point of the mouse: used for hit testing (snapping would miss)."""
        return canvas.screen_to_board(ev.localPos())

    def selected(self, app):
        """The selected object (live, from BoardModel), or None if nothing or it is gone."""
        board = getattr(app, "board", None)
        if self.selected_id is None or board is None:
            return None
        obj = board.objects.get(self.selected_id)
        if obj is None:
            self.selected_id = None   # deleted by someone else (or undone)
        return obj

    def _handle_at(self, canvas, obj, pt):
        """Index (0-3) of the resize handle of obj under board point pt, or None."""
        if obj is None or obj.get("kind") not in RESIZABLE:
            return None
        reach = HANDLE_HIT_PX / canvas.zoom
        for i, c in enumerate(corners(box_of(obj))):
            if abs(pt.x() - c.x()) <= reach and abs(pt.y() - c.y()) <= reach:
                return i
        return None

    # --------------------------------------------------------- tool events
    def on_press(self, canvas, p, ev):
        """Left press: open the editor (double-click), grab a handle, or select and start a move."""
        if ev.button() != Qt.LeftButton:
            return
        app = canvas.app
        if _is_viewer(app):
            self.selected_id = None
            app.toast("Viewers cannot select or edit objects")
            return
        raw = self._raw_point(canvas, ev)

        # Qt delivers the second press of a double-click as MouseButtonDblClick
        # (QWidget's default mouseDoubleClickEvent calls mousePressEvent).
        if ev.type() == QEvent.MouseButtonDblClick:
            self._open_editor(canvas, raw)
            return

        # 1. A corner handle of the current selection: start a resize.
        obj = self.selected(app)
        handle = self._handle_at(canvas, obj, raw)
        if handle is not None:
            self._start_drag(canvas, obj, "resize")
            self._fixed = corners(box_of(obj))[(handle + 2) % 4]
            return

        # 2. The topmost object under the mouse: select it and start a move.
        board = app.board
        hit = topmost_hit(board.objects, raw.x(), raw.y(), HIT_TOL_PX / canvas.zoom) \
            if board is not None else None
        self.selected_id = hit
        if hit is not None:
            self._start_drag(canvas, board.objects[hit], "move")
            self._press = raw

    def on_move(self, canvas, p, ev):
        """Drag: move or resize (throttled). Hover: show a move/resize cursor."""
        if self._mode is None:
            self._hover_cursor(canvas, self._raw_point(canvas, ev))
            return
        if not ev.buttons() & Qt.LeftButton:
            return
        self._queue(self._drag_fields(canvas, p, ev))

    def on_release(self, canvas, p, ev):
        """End the drag and send the final position (if anything moved)."""
        if self._mode is None:
            return
        fields = self._drag_fields(canvas, p, ev)
        self._stop_timer()
        self._pending = None
        # The final update: the exact end position, even if the throttle
        # already sent the same value (a cheap guarantee that every client
        # ends with it). A click without a real move sends nothing.
        if fields is not None and fields != self._orig_fields(fields):
            self._send(fields)
        self._end_drag()

    def cursor(self):
        """Arrow: this tool points at objects."""
        return Qt.ArrowCursor

    def cancel(self, canvas):
        """Escape (canvas.cancel_tool): undo the drag in progress, or clear the selection.

        The throttle may already have sent OP_UPDATEs during the drag, so
        the other clients have seen the object move. One more OP_UPDATE
        with the press-time values puts it back for everyone.
        """
        if self._mode is None:
            self.selected_id = None
            return
        self._stop_timer()
        self._pending = None
        if self._last_sent is not None:
            self._send(self._orig_fields(self._last_sent))
        self._end_drag()

    # ------------------------------------------------------------- drag
    def _start_drag(self, canvas, obj, mode):
        """Remember a copy of obj and reset the throttle for a new move/resize."""
        self._canvas = canvas
        self._mode = mode
        self._orig = copy.deepcopy(obj)
        self._pending = None
        self._last_sent = None
        self._last_time = 0.0

    def _end_drag(self):
        """Forget everything about the drag (the selection stays)."""
        self._mode = None
        self._orig = self._press = self._fixed = None
        self._last_sent = None
        self._canvas = None

    def _drag_fields(self, canvas, p, ev):
        """The 'set' for the current mouse position, or None if not dragging.

        move: offset = raw mouse movement; the object's anchor + offset is
        snapped (canvas.snap), and the whole object gets the snapped offset.
        resize: the dragged corner goes to p, which the canvas already snapped.
        """
        if self._mode == "move":
            raw = self._raw_point(canvas, ev)
            anchor = anchor_of(self._orig)
            target = canvas.snap(QPointF(anchor.x() + raw.x() - self._press.x(),
                                         anchor.y() + raw.y() - self._press.y()))
            return moved_fields(self._orig, target.x() - anchor.x(), target.y() - anchor.y())
        if self._mode == "resize":
            return resized_fields(self._fixed, QPointF(p))
        return None

    def _orig_fields(self, fields):
        """The press-time values of the fields in 'fields' (to skip a no-op update)."""
        return {k: self._orig.get(k) for k in fields}

    def _queue(self, fields):
        """Throttle: send now if THROTTLE_MS passed since the last send, else keep the newest.

        A kept value is sent by a single-shot QTimer when the 30 ms window
        ends, so the last position is never lost while the mouse rests.
        Everything runs on the GUI thread, so no locking is needed.
        """
        if fields is None or fields == self._last_sent:
            return
        if self._last_sent is None and fields == self._orig_fields(fields):
            return                              # not moved yet (e.g. inside one grid cell)
        wait_ms = THROTTLE_MS - (time.monotonic() - self._last_time) * 1000.0
        if wait_ms <= 0:
            self._stop_timer()
            self._pending = None
            self._send(fields)
            return
        self._pending = fields
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._flush)
        if not self._timer.isActive():
            self._timer.start(int(wait_ms) + 1)

    def _flush(self):
        """Timer: send the newest kept position, unless the drag or the tool is gone."""
        fields, self._pending = self._pending, None
        canvas = self._canvas
        if fields is None or canvas is None or self._mode is None:
            return
        if canvas.app.active_tool is not self or _is_viewer(canvas.app):
            self._end_drag()                     # tool switched or role lost mid-drag
            return
        self._send(fields)
        canvas.update()

    def _stop_timer(self):
        """Cancel a scheduled flush."""
        if self._timer is not None:
            self._timer.stop()

    def _send(self, fields):
        """One OP_UPDATE {obj_id, set} through send_op (applied locally at once)."""
        canvas = self._canvas
        if canvas is None or self.selected_id is None or self.selected(canvas.app) is None:
            return
        ops.send_op(canvas.app, protocol.OP_UPDATE, {"obj_id": self.selected_id, "set": fields})
        self._last_sent = fields
        self._last_time = time.monotonic()

    # --------------------------------------------------- delete and edit
    def delete_selected(self, app):
        """Send OP_DELETE for the selected object and clear the selection."""
        if _is_viewer(app):
            app.toast("Viewers cannot delete objects")
            return
        if self._mode is not None:
            return                               # not in the middle of a drag
        if self.selected(app) is None:
            return
        ops.send_op(app, protocol.OP_DELETE, {"obj_id": self.selected_id})
        self.selected_id = None
        if getattr(app, "canvas", None) is not None:
            app.canvas.update()

    def _open_editor(self, canvas, raw):
        """Double-click: select the object under the mouse and open its registered editor.

        The editor gets a copy of the object; it sends its own OP_UPDATE
        through send_op. Kinds without an editor (shapes, strokes) do nothing.
        """
        app = canvas.app
        board = app.board
        hit = topmost_hit(board.objects, raw.x(), raw.y(), HIT_TOL_PX / canvas.zoom) \
            if board is not None else None
        self.selected_id = hit
        if hit is None:
            return
        obj = board.objects[hit]
        editor = registry.get_editor(obj.get("kind"))
        if editor is not None:
            editor(app, canvas, copy.deepcopy(obj))

    # ---------------------------------------------------------- painting
    def _hover_cursor(self, canvas, pt):
        """Diagonal arrows over a resize handle, the move cross over the selection, else arrow."""
        obj = self.selected(canvas.app)
        handle = self._handle_at(canvas, obj, pt)
        if handle is not None:
            canvas.setCursor(Qt.SizeFDiagCursor if handle in (0, 2) else Qt.SizeBDiagCursor)
        elif obj is not None and topmost_hit({obj.get("id"): obj}, pt.x(), pt.y(),
                                             HIT_TOL_PX / canvas.zoom):
            canvas.setCursor(Qt.SizeAllCursor)
        else:
            canvas.setCursor(Qt.ArrowCursor)

    def paint_overlay(self, painter, canvas):
        """Dashed box around the selection, plus corner handles for resizable kinds.

        Local only (never sent). The painter is in board units; pens are
        cosmetic and sizes are divided by the zoom so they look the same
        at every zoom level.
        """
        obj = self.selected(canvas.app)
        if obj is None:
            return
        rect = tight_bounds(obj)
        if rect is None:
            return
        zoom = canvas.zoom
        pad = BOX_PAD_PX / zoom
        pen = QPen(SELECT_COLOR, 1, Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(-pad, -pad, pad, pad))
        if obj.get("kind") in RESIZABLE:
            side = HANDLE_PX / zoom
            solid = QPen(SELECT_COLOR, 1)
            solid.setCosmetic(True)
            painter.setPen(solid)
            painter.setBrush(QBrush(QColor("#ffffff")))
            for c in corners(box_of(obj)):
                painter.drawRect(QRectF(c.x() - side / 2, c.y() - side / 2, side, side))


def delete_selection(app):
    """Delete key (register_shortcut): delete the selection when the select tool is active.

    QLineEdit widgets (chat) take the Delete key themselves before a
    window shortcut fires, so typing in the chat never deletes objects.
    """
    tool = getattr(app, "active_tool", None)
    if isinstance(tool, SelectTool):
        tool.delete_selected(app)


registry.register_tool(SelectTool)
registry.register_shortcut("Delete", delete_selection)
