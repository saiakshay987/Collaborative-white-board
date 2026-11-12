# OWNER: P2
"""select_tool.py: Select tool: select (one or many), move, resize, delete, nudge and double-click editing through register_editor.

SelectTool ('select', V), registered with registry.register_tool.

What it does
------------
click          selects the TOPMOST object under the mouse (same hit test and
               paint order as the eraser, pen_tool.topmost_hit). The selection
               is shown with a dashed bounding box (paint_overlay, local only).
               A click on empty board clears the selection.
Shift+click    adds the object to the selection, or removes it if it is in it.
drag           on an object: moves it (or the whole group, see below).
               on empty board: draws a dashed selection rectangle; on release
               every object whose bounding box lies completely inside it is
               selected (strokes included). Shift+drag ADDS that area to the
               current selection instead of replacing it.
corner drag    resizes rect, ellipse, text, sticky and image (the kinds with
               x, y, w, h) when exactly ONE object is selected; the opposite
               corner stays where it is.
Ctrl+A         selects every object on the board (register_shortcut).
Delete,        OP_DELETE for every selected object (register_shortcut).
Backspace
Arrow keys     nudge the selection by one grid step (Shift: 5 steps); the
               move is previewed while keys are held and sent when the key
               is released.
Escape         during a drag: put the object back where it was (single
               object, only if something was already sent) or drop the
               preview (group, nothing was sent); otherwise clear the
               selection.
double-click   calls the editor registered with register_editor for the
               object's kind (P4 registers text and sticky), as
               fn(app, canvas, obj).

The selection is a list of object ids (selected_ids). With one object it
looks and behaves as before (box and resize handles). With several, every
object gets its own dashed outline and the group gets ONE bounding box with
corner handles. The handles are only a visual cue for the group: grabbing
one moves the group (a group resize is not part of this tool).

What goes over the network
--------------------------
Every change is an OP_UPDATE {obj_id, set:{...}} through client/ops.send_op,
so it is applied locally at once and the server echo only confirms it:

    move rect/ellipse/text/sticky/image   set {x, y}
    move line/arrow                       set {x1, y1, x2, y2}
    move stroke                           set {points}  (every point offset)
    resize                                set {x, y, w, h}

A drag of ONE object produces many mouse events (often 100+ per second).
Sending one OP_UPDATE per event would flood the server and every other
client, so moves are THROTTLED: at most one message every THROTTLE_MS (30 ms,
about 33 per second). The newest position always wins: if the mouse moves
inside the 30 ms window, the position is kept and a single-shot QTimer sends
it when the window ends, so the board never stays behind a mouse that
stopped. On release one final OP_UPDATE carries the exact end position, so
every client ends up with the same value.

Moving a GROUP (or nudging with the arrow keys) is deliberately NOT live.
A group of N objects moved like a single one would cost N x 33 messages per
second, each one fanned out by the server to every client (and a stroke's
update carries all of its points). Six objects are already 200 messages a
second. Instead the group is shown as a LOCAL PREVIEW on this client only
(the moved copies are painted semi-transparent in paint_overlay) and exactly
ONE OP_UPDATE PER OBJECT is sent on release: N messages in total, however
long the drag lasted. The price is that other users see the group jump to
its final place instead of gliding; the final state is the same on every
client either way. The same reasoning applies to the arrow keys, where the
update is sent when the key is released, so holding a key (auto-repeat)
is still one message per object.

The geometry is always computed from a copy of the object taken at press
time plus the total mouse offset (not from the previous update), so rounding
never accumulates, and a remote change to the same object between our
updates cannot make the drag jump.

Snap-to-grid: the canvas hands tools snapped points. For a move, the object's
anchor (x, y / x1, y1 / first point; for a group the top-left corner of the
group box) is snapped with canvas.snap() and everything follows by the same
offset; a resize puts the dragged corner on the snapped mouse point. So the
server only ever stores snapped values.

Page rule: a move is limited so the object's box (for a group: the box of
the whole group) stays inside the page (it stops at the edge; clamp_rect from
canvas.py), and the canvas already clamps the dragged corner of a resize to
the page.

Viewers: the tool dock already clears the active tool for a viewer, and
this tool refuses to select (toast) as a second guard. The server checks
permissions.can(role, 'draw') on every OP_UPDATE / OP_DELETE anyway.

Keyboard: the canvas offers key events to the tool through the optional hooks
on_key_press(canvas, ev), on_key_release(canvas, ev) and focus_lost(canvas)
(each returns True when it used the event), the same way it offers the
optional cancel() and paint_overlay().
"""

import copy
import time

from PyQt5.QtCore import QEvent, QPointF, QRectF, QSizeF, Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QPen

from client import ops, registry, render
from client.canvas import clamp_point, clamp_rect
from client.tools.pen_tool import _num, topmost_hit
from client.tools.shape_tools import round1
from common import protocol

# Throttle for move/resize updates while dragging (milliseconds).
THROTTLE_MS = 30

# Sizes on SCREEN (pixels), converted to board units with the zoom, so the
# handles and the click tolerance feel the same at every zoom level.
HIT_TOL_PX = 4          # extra reach around thin lines and strokes when clicking
HANDLE_PX = 8           # side of a resize handle square
HANDLE_HIT_PX = 7       # how far from a corner a press still grabs the handle
BOX_PAD_PX = 4          # gap between the object and its dashed selection box
GROUP_PAD_PX = 8        # gap between the objects and the group box
DRAG_PX = 3             # the mouse must travel this far (screen pixels) for a press to count as a drag

# Kinds with x, y, w, h that get corner resize handles.
RESIZABLE = ("rect", "ellipse", "text", "sticky", "image")

# Smallest width/height a resize can produce (board units).
MIN_SIZE = 1.0

# Arrow keys: the direction each one nudges in, and the size of a nudge in
# grid steps (the canvas grid, 20 units by default).
ARROW_KEYS = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0), Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
NUDGE_STEPS = 1
NUDGE_STEPS_SHIFT = 5

# Painting of a multi-selection.
MAX_OUTLINES = 300      # above this many objects only the group box is drawn (speed)
GHOST_OPACITY = 0.55    # opacity of the moved copies shown while a group is dragged

SELECT_COLOR = QColor("#1565c0")
OUTLINE_COLOR = QColor(21, 101, 192, 150)
MARQUEE_FILL = QColor(21, 101, 192, 28)


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


def tight_bounds(obj, with_width=True):
    """Bounding QRectF of an object in board units, or None.

    Half the line width is included (it is where the ink really ends) unless
    with_width is False: that gives the box of the stored COORDINATES, which
    is what the page rule is about (the validators check coordinates).
    """
    half = _num(obj.get("width"), 0.0) / 2.0 if with_width else 0.0
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


def union_bounds(objs, with_width=True):
    """The one QRectF around every object of objs (see tight_bounds), or None if none has geometry.

    QRectF.united() would silently skip a zero-size box (a single-point
    stroke), so the four edges are tracked by hand.
    """
    left = top = right = bottom = None
    for obj in objs:
        box = tight_bounds(obj, with_width)
        if box is None:
            continue
        left = box.left() if left is None else min(left, box.left())
        top = box.top() if top is None else min(top, box.top())
        right = box.right() if right is None else max(right, box.right())
        bottom = box.bottom() if bottom is None else max(bottom, box.bottom())
    if left is None:
        return None
    return QRectF(QPointF(left, top), QPointF(right, bottom))


def clamp_offset(box, dx, dy):
    """Return (dx, dy) cut back so that box moved by it still lies on the page.

    Uses the page rule of canvas.clamp_rect. The result is rounded to one
    decimal (contract D) so every object of a group gets exactly the same
    offset and their positions relative to each other never drift.
    """
    moved = box.translated(dx, dy)
    inside = clamp_rect(moved)
    return (round(dx + inside.x() - moved.x(), 1), round(dy + inside.y() - moved.y(), 1))


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
        return {"points": [[round1(_num(p[0]) + dx), round1(_num(p[1]) + dy)] for p in _points(obj)]}
    if "x1" in obj:
        return {"x1": round1(_num(obj.get("x1")) + dx), "y1": round1(_num(obj.get("y1")) + dy),
                "x2": round1(_num(obj.get("x2")) + dx), "y2": round1(_num(obj.get("y2")) + dy)}
    return {"x": round1(_num(obj.get("x")) + dx), "y": round1(_num(obj.get("y")) + dy)}


def resized_fields(fixed, moving):
    """The OP_UPDATE 'set' for a box between the fixed corner and the dragged corner.

    The box is normalized (the validators require w, h >= 0), so dragging a
    corner past the opposite one flips the box instead of breaking it.
    """
    rect = QRectF(fixed, moving).normalized()
    rect.setSize(QSizeF(max(MIN_SIZE, rect.width()), max(MIN_SIZE, rect.height())))
    rect = clamp_rect(rect)          # MIN_SIZE may push a box at the edge over it
    return {"x": round1(rect.x()), "y": round1(rect.y()),
            "w": round1(rect.width()), "h": round1(rect.height())}


def group_rect(objs, zoom):
    """The padded box around a multi-selection (board units), or None: where the group box and its handles sit."""
    box = union_bounds(objs)
    if box is None:
        return None
    pad = GROUP_PAD_PX / zoom
    return box.adjusted(-pad, -pad, pad, pad)


class SelectTool:
    """Select, move, resize, delete, nudge and edit (double-click) one or several board objects."""

    id = "select"
    label = "Select"
    shortcut = "V"
    group = "select"

    def __init__(self):
        """Start with nothing selected and no drag."""
        self.selected_ids = []       # obj_ids of the selection, in the order they were added
        self._canvas = None          # canvas of the current drag (for the timer)
        self._mode = None            # None, "move", "resize" (one object), "group" or "marquee"
        self._drag_id = None         # obj_id being moved / resized (single-object modes)
        self._orig = None            # deep copy of the object at press time (single-object modes)
        self._press = None           # raw board point of the press (move, group, marquee)
        self._press_screen = None    # local (screen) point of the press, for the click-or-drag test
        self._moved = False          # the mouse travelled DRAG_PX or more since the press
        self._fixed = None           # opposite corner (resize)
        self._pending = None         # newest 'set' not sent yet (single-object modes)
        self._last_sent = None       # last 'set' sent during this drag
        self._last_time = 0.0        # time.monotonic() of the last send
        self._timer = None           # single-shot QTimer that flushes _pending
        # Group move and nudge: a local preview, nothing is sent until release.
        self._group_orig = {}        # obj_id -> deep copy at the start of the preview
        self._group_box = None       # QRectF of the group's coordinates (page rule, snapping)
        self._offset = (0.0, 0.0)    # (dx, dy) of the preview, board units
        self._nudging = False        # an arrow key is held: a nudge preview is active
        self._narrow_to = None       # click (no drag) on a member of a group: select only it
        self._marquee_end = None     # raw board point of the other corner of the marquee
        self._marquee_base = []      # selection kept by a Shift+drag marquee

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _raw_point(canvas, ev):
        """Unsnapped board point of the mouse: used for hit testing (snapping would miss)."""
        return canvas.screen_to_board(ev.localPos())

    def selected_objects(self, app):
        """The selected objects (live dicts from BoardModel), in selection order.

        Ids whose object is gone (deleted by someone else, or undone) are
        dropped from the selection here, so every caller sees a valid list.
        """
        board = getattr(app, "board", None)
        if board is None:
            self.selected_ids = []
            return []
        live = [obj_id for obj_id in self.selected_ids if obj_id in board.objects]
        if len(live) != len(self.selected_ids):
            self.selected_ids = live
        return [board.objects[obj_id] for obj_id in live]

    def selected(self, app):
        """The selected object when EXACTLY ONE is selected (it can be resized), else None."""
        objs = self.selected_objects(app)
        return objs[0] if len(objs) == 1 else None

    def _handle_at(self, canvas, obj, pt):
        """Index (0-3) of the resize handle of obj under board point pt, or None."""
        if obj is None or obj.get("kind") not in RESIZABLE:
            return None
        reach = HANDLE_HIT_PX / canvas.zoom
        for i, c in enumerate(corners(box_of(obj))):
            if abs(pt.x() - c.x()) <= reach and abs(pt.y() - c.y()) <= reach:
                return i
        return None

    def _group_handle_at(self, canvas, objs, pt):
        """True when board point pt is on a corner handle of the group box of objs."""
        rect = group_rect(objs, canvas.zoom)
        if rect is None:
            return False
        reach = HANDLE_HIT_PX / canvas.zoom
        return any(abs(pt.x() - c.x()) <= reach and abs(pt.y() - c.y()) <= reach
                   for c in corners(rect))

    def _passed_threshold(self, ev):
        """True once the mouse is DRAG_PX or more away from the press (and from then on).

        A click always wobbles a pixel or two; without this a click on an
        object would be sent as a tiny move, and a click on empty board
        would count as an empty selection rectangle.
        """
        if not self._moved and self._press_screen is not None:
            if (QPointF(ev.localPos()) - self._press_screen).manhattanLength() >= DRAG_PX:
                self._moved = True
        return self._moved

    # --------------------------------------------------------- tool events
    def on_press(self, canvas, p, ev):
        """Left press: open the editor (double-click), grab a handle, select, or start a move / marquee."""
        if ev.button() != Qt.LeftButton:
            return
        app = canvas.app
        if _is_viewer(app):
            self.selected_ids = []
            app.toast("Viewers cannot select or edit objects")
            return
        raw = self._raw_point(canvas, ev)

        # Qt delivers the second press of a double-click as MouseButtonDblClick
        # (QWidget's default mouseDoubleClickEvent calls mousePressEvent).
        if ev.type() == QEvent.MouseButtonDblClick:
            self._open_editor(canvas, raw)
            return

        if self._nudging:
            self._send_preview(app)          # a key was still held: finish that nudge first
        board = app.board
        if board is None:
            return
        selection = self.selected_objects(app)

        # 1. A corner handle of the selection. One object: start a resize.
        #    A group: the handles only mark the group, so grab it and move it.
        if len(selection) == 1:
            handle = self._handle_at(canvas, selection[0], raw)
            if handle is not None:
                self._start_drag(canvas, selection[0], "resize")
                self._fixed = corners(box_of(selection[0]))[(handle + 2) % 4]
                return
        elif len(selection) > 1 and self._group_handle_at(canvas, selection, raw):
            self._start_group_move(canvas, ev, raw, selection)
            return

        hit = topmost_hit(board.objects, raw.x(), raw.y(), HIT_TOL_PX / canvas.zoom)

        # 2. Shift: add the object to / remove it from the selection (no drag),
        #    or, on empty board, start a rectangle that ADDS to the selection.
        if ev.modifiers() & Qt.ShiftModifier:
            if hit is None:
                self._start_marquee(canvas, ev, raw, keep=self.selected_ids)
            elif hit in self.selected_ids:
                self.selected_ids = [i for i in self.selected_ids if i != hit]
            else:
                self.selected_ids = self.selected_ids + [hit]
            return

        # 3. Empty board: clear the selection and start a rectangle. If the
        #    mouse does not travel, it was a plain click and the clear stands.
        if hit is None:
            self.selected_ids = []
            self._start_marquee(canvas, ev, raw, keep=[])
            return

        # 4. An object that is already part of a group: drag the WHOLE group.
        #    A click without a drag then narrows the selection to this object.
        if len(selection) > 1 and hit in self.selected_ids:
            self._start_group_move(canvas, ev, raw, selection)
            self._narrow_to = hit
            return

        # 5. Any other object: select only it and start a (live, throttled) move.
        self.selected_ids = [hit]
        self._start_drag(canvas, board.objects[hit], "move")
        self._press = raw

    def on_move(self, canvas, p, ev):
        """Drag: move, resize, preview a group move or grow the rectangle. Hover: show a cursor."""
        if self._mode is None:
            self._hover_cursor(canvas, self._raw_point(canvas, ev))
            return
        if not ev.buttons() & Qt.LeftButton:
            return
        if self._mode == "marquee":
            self._update_marquee(canvas, ev)
        elif self._mode == "group":
            self._update_group(canvas, ev)
        else:
            self._queue(self._drag_fields(canvas, p, ev))

    def on_release(self, canvas, p, ev):
        """End the drag: send the final position, or the group's updates, or select the rectangle."""
        if self._mode is None:
            return
        if self._mode == "marquee":
            self._finish_marquee(canvas, ev)
            return
        if self._mode == "group":
            self._finish_group(canvas, ev)
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

        Single object: the throttle may already have sent OP_UPDATEs during
        the drag, so the other clients have seen the object move. One more
        OP_UPDATE with the press-time values puts it back for everyone.
        Group, nudge or rectangle: nothing was sent, so dropping the local
        preview is all there is to do.
        """
        if self._nudging:
            self._end_drag()
            return
        if self._mode is None:
            self.selected_ids = []
            return
        if self._mode in ("group", "marquee"):
            self._end_drag()
            return
        self._stop_timer()
        self._pending = None
        if self._last_sent is not None:
            self._send(self._orig_fields(self._last_sent))
        self._end_drag()

    # ------------------------------------------------------------ keyboard
    def on_key_press(self, canvas, ev):
        """Arrow keys: grow the nudge preview by one grid step (Shift: five). True if used.

        Held keys auto-repeat, so the preview keeps moving; nothing is sent
        yet (see on_key_release and the module doc).
        """
        step_dir = ARROW_KEYS.get(ev.key())
        app = canvas.app
        if step_dir is None or self._mode is not None or _is_viewer(app):
            return False
        objs = self.selected_objects(app)
        if not objs:
            return False
        if not self._nudging:
            self._canvas = canvas
            self._group_orig = {i: copy.deepcopy(o) for i, o in zip(self.selected_ids, objs)}
            self._group_box = union_bounds(objs, with_width=False)
            self._offset = (0.0, 0.0)
            self._nudging = True
        if self._group_box is None:
            self._end_drag()
            return False
        step = canvas.grid_size * (NUDGE_STEPS_SHIFT if ev.modifiers() & Qt.ShiftModifier
                                   else NUDGE_STEPS)
        self._offset = clamp_offset(self._group_box, self._offset[0] + step_dir[0] * step,
                                    self._offset[1] + step_dir[1] * step)
        return True

    def on_key_release(self, canvas, ev):
        """Arrow key released: send the nudge now, one OP_UPDATE per object. True if used.

        Releases that the OS generates between auto-repeats are ignored
        (isAutoRepeat), so only the real release sends.
        """
        if ev.key() not in ARROW_KEYS or not self._nudging:
            return False
        if not ev.isAutoRepeat():
            self._send_preview(canvas.app)
        return True

    def focus_lost(self, canvas):
        """The canvas lost the keyboard focus: the key release will never arrive, so send the nudge now."""
        if self._nudging:
            self._send_preview(canvas.app)

    # ------------------------------------------------------------- drag
    def _start_drag(self, canvas, obj, mode):
        """Remember a copy of obj and reset the throttle for a new single-object move/resize."""
        self._canvas = canvas
        self._mode = mode
        self._drag_id = obj.get("id")
        self._orig = copy.deepcopy(obj)
        self._pending = None
        self._last_sent = None
        self._last_time = 0.0

    def _end_drag(self):
        """Forget everything about the drag, preview or nudge (the selection stays)."""
        self._mode = None
        self._drag_id = None
        self._orig = self._press = self._press_screen = self._fixed = None
        self._moved = False
        self._last_sent = None
        self._canvas = None
        self._group_orig = {}
        self._group_box = None
        self._offset = (0.0, 0.0)
        self._nudging = False
        self._narrow_to = None
        self._marquee_end = None
        self._marquee_base = []

    def _drag_fields(self, canvas, p, ev):
        """The 'set' for the current mouse position, or None if not dragging one object.

        move: offset = raw mouse movement; the object's anchor + offset is
        snapped (canvas.snap), and the whole object gets the snapped offset.
        resize: the dragged corner goes to p, which the canvas already snapped.
        """
        if self._mode == "move":
            raw = self._raw_point(canvas, ev)
            anchor = anchor_of(self._orig)
            target = canvas.snap(QPointF(anchor.x() + raw.x() - self._press.x(),
                                         anchor.y() + raw.y() - self._press.y()))
            dx, dy = target.x() - anchor.x(), target.y() - anchor.y()
            # Page rule: push the moved box back inside the page, so the
            # object stops at the edge instead of leaving it.
            box = tight_bounds(self._orig, with_width=False)
            if box is not None:
                dx, dy = clamp_offset(box, dx, dy)
            return moved_fields(self._orig, dx, dy)
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
        if fields is None or canvas is None or self._mode not in ("move", "resize"):
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
        board = canvas.app.board if canvas is not None else None
        if board is None or self._drag_id is None or self._drag_id not in board.objects:
            return                               # the object was deleted meanwhile
        ops.send_op(canvas.app, protocol.OP_UPDATE, {"obj_id": self._drag_id, "set": fields})
        self._last_sent = fields
        self._last_time = time.monotonic()

    # ------------------------------------------------- group move (preview)
    def _start_group_move(self, canvas, ev, raw, objs):
        """Begin dragging the whole selection: copy the objects and remember the press.

        Nothing is sent while the mouse moves; see "Moving a GROUP" in the
        module doc. The preview offset lives in self._offset.
        """
        box = union_bounds(objs, with_width=False)
        if box is None:
            return
        self._canvas = canvas
        self._mode = "group"
        self._press = raw
        self._press_screen = QPointF(ev.localPos())
        self._moved = False
        self._group_orig = {i: copy.deepcopy(o) for i, o in zip(self.selected_ids, objs)}
        self._group_box = box
        self._offset = (0.0, 0.0)

    def _update_group(self, canvas, ev):
        """Mouse moved: recompute the preview offset (grid-snapped, kept on the page). Local only."""
        if not self._passed_threshold(ev):
            return
        raw = self._raw_point(canvas, ev)
        anchor = self._group_box.topLeft()
        target = canvas.snap(QPointF(anchor.x() + raw.x() - self._press.x(),
                                     anchor.y() + raw.y() - self._press.y()))
        self._offset = clamp_offset(self._group_box, target.x() - anchor.x(),
                                    target.y() - anchor.y())

    def _finish_group(self, canvas, ev):
        """Release after a group drag: send the move (one OP_UPDATE per object), or narrow the selection."""
        self._update_group(canvas, ev)
        moved, narrow = self._moved, self._narrow_to
        self._send_preview(canvas.app)           # sends if the offset is not (0, 0), then resets
        if not moved and narrow is not None:
            self.selected_ids = [narrow]         # a plain click on one member of the group

    def _send_preview(self, app):
        """Send the previewed move (drag or nudge): one OP_UPDATE per object, then forget the preview.

        This is the ONLY place where a group move touches the network. Each
        update is built from the copy taken when the preview started
        (moved_fields) plus the one shared offset, so every object moves by
        exactly the same amount. An object that someone else deleted in the
        meantime is skipped (the server would answer NOT_FOUND).
        """
        originals, (dx, dy) = self._group_orig, self._offset
        self._end_drag()
        if (dx, dy) == (0.0, 0.0) or not originals:
            return
        if _is_viewer(app):
            app.toast("Viewers cannot move objects")
            return
        board = app.board
        for obj_id, orig in originals.items():
            if board is not None and obj_id in board.objects:
                ops.send_op(app, protocol.OP_UPDATE,
                            {"obj_id": obj_id, "set": moved_fields(orig, dx, dy)})
        if getattr(app, "canvas", None) is not None:
            app.canvas.update()

    # ------------------------------------------------------ selection marquee
    def _start_marquee(self, canvas, ev, raw, keep):
        """Begin a selection rectangle at raw; keep is the selection a Shift+drag adds to."""
        self._canvas = canvas
        self._mode = "marquee"
        self._press = raw
        self._press_screen = QPointF(ev.localPos())
        self._moved = False
        self._marquee_end = raw
        self._marquee_base = list(keep)

    def _update_marquee(self, canvas, ev):
        """Mouse moved: the free corner follows it (kept on the page)."""
        self._passed_threshold(ev)
        self._marquee_end = clamp_point(self._raw_point(canvas, ev))

    def _finish_marquee(self, canvas, ev):
        """Release: select every object that lies COMPLETELY inside the rectangle.

        The test is on each object's tight bounding box (ink included), so a
        stroke or a shape the rectangle only touches is not selected. With
        Shift the objects are added to the selection that was there before.
        A press that never moved is a plain click: nothing to select.
        """
        self._update_marquee(canvas, ev)
        moved, base = self._moved, self._marquee_base
        area = QRectF(self._press, self._marquee_end).normalized()
        self._end_drag()
        if not moved:
            return
        board = canvas.app.board
        inside = []
        if board is not None:
            for obj_id, obj in board.objects.items():
                box = tight_bounds(obj)
                if box is not None and area.contains(box):
                    inside.append(obj_id)
        self.selected_ids = list(base) + [i for i in inside if i not in base]

    # --------------------------------------------------- delete and edit
    def select_all(self, app):
        """Ctrl+A: select every object on the board."""
        if _is_viewer(app):
            app.toast("Viewers cannot select or edit objects")
            return
        if self._mode is not None or app.board is None:
            return
        self._end_drag()                         # drop a nudge preview, if any
        self.selected_ids = list(app.board.objects.keys())
        if getattr(app, "canvas", None) is not None:
            app.canvas.update()

    def delete_selected(self, app):
        """Send one OP_DELETE per selected object and clear the selection."""
        if _is_viewer(app):
            app.toast("Viewers cannot delete objects")
            return
        if self._mode is not None:
            return                               # not in the middle of a drag
        self._end_drag()                         # a nudge preview is pointless now
        objs = self.selected_objects(app)
        if not objs:
            return
        for obj_id in list(self.selected_ids):
            ops.send_op(app, protocol.OP_DELETE, {"obj_id": obj_id})
        self.selected_ids = []
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
        self.selected_ids = [] if hit is None else [hit]
        if hit is None:
            return
        obj = board.objects[hit]
        editor = registry.get_editor(obj.get("kind"))
        if editor is not None:
            editor(app, canvas, copy.deepcopy(obj))

    # ---------------------------------------------------------- painting
    def _hover_cursor(self, canvas, pt):
        """Diagonal arrows over a resize handle, the move cross over the selection, else arrow."""
        app = canvas.app
        objs = self.selected_objects(app)
        cursor = Qt.ArrowCursor
        if len(objs) == 1:
            obj = objs[0]
            handle = self._handle_at(canvas, obj, pt)
            if handle is not None:
                cursor = Qt.SizeFDiagCursor if handle in (0, 2) else Qt.SizeBDiagCursor
            elif topmost_hit({obj.get("id"): obj}, pt.x(), pt.y(), HIT_TOL_PX / canvas.zoom):
                cursor = Qt.SizeAllCursor
        elif len(objs) > 1:
            rect = group_rect(objs, canvas.zoom)
            if self._group_handle_at(canvas, objs, pt):
                cursor = Qt.SizeAllCursor
            elif rect is not None and rect.contains(pt):
                # Only inside the group box is the (costly) full hit test worth doing.
                hit = topmost_hit(app.board.objects, pt.x(), pt.y(), HIT_TOL_PX / canvas.zoom)
                if hit in self.selected_ids:
                    cursor = Qt.SizeAllCursor
        canvas.setCursor(cursor)

    def _previewing(self):
        """True while a group drag or a nudge shows moved copies (offset not zero)."""
        return (self._mode == "group" or self._nudging) and self._offset != (0.0, 0.0)

    def paint_overlay(self, painter, canvas):
        """Selection boxes, handles, the group-move preview and the selection rectangle.

        Local only (never sent). The painter is in board units; pens are
        cosmetic and sizes are divided by the zoom so they look the same
        at every zoom level.
        """
        objs = self.selected_objects(canvas.app)
        if objs:
            dx, dy = self._offset if self._previewing() else (0.0, 0.0)
            if (dx, dy) != (0.0, 0.0):
                self._paint_ghosts(painter, canvas, objs, dx, dy)
            painter.save()
            painter.translate(dx, dy)            # the boxes follow the preview
            if len(objs) == 1:
                self._paint_single(painter, canvas.zoom, objs[0])
            else:
                self._paint_group(painter, canvas.zoom, objs)
            painter.restore()
        if self._mode == "marquee" and self._marquee_end is not None and self._moved:
            pen = QPen(SELECT_COLOR, 1, Qt.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QBrush(MARQUEE_FILL))
            painter.drawRect(QRectF(self._press, self._marquee_end).normalized())

    @staticmethod
    def _paint_ghosts(painter, canvas, objs, dx, dy):
        """Paint the selected objects again, semi-transparent and moved by (dx, dy): the move preview.

        The real objects stay where they are (the board has not changed
        yet), so the user sees both where the group is and where it will land.
        """
        painter.save()
        painter.setOpacity(GHOST_OPACITY)
        painter.translate(dx, dy)
        ctx = {"canvas": canvas, "zoom": canvas.zoom, "visible": canvas.visible_board_rect()}
        render.render_board(painter, objs, ctx)
        painter.restore()

    @staticmethod
    def _paint_single(painter, zoom, obj):
        """Dashed box around one selected object, plus corner handles for resizable kinds."""
        rect = tight_bounds(obj)
        if rect is None:
            return
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

    @staticmethod
    def _paint_group(painter, zoom, objs):
        """An outline around each selected object, and ONE solid group box with corner handles.

        With more than MAX_OUTLINES objects only the group box is drawn, so
        Ctrl+A on a big board stays fast.
        """
        painter.setBrush(Qt.NoBrush)
        if len(objs) <= MAX_OUTLINES:
            pad = BOX_PAD_PX / zoom
            outline = QPen(OUTLINE_COLOR, 1, Qt.DashLine)
            outline.setCosmetic(True)
            painter.setPen(outline)
            for obj in objs:
                box = tight_bounds(obj)
                if box is not None:
                    painter.drawRect(box.adjusted(-pad, -pad, pad, pad))
        rect = group_rect(objs, zoom)
        if rect is None:
            return
        solid = QPen(SELECT_COLOR, 1.5)
        solid.setCosmetic(True)
        painter.setPen(solid)
        painter.drawRect(rect)
        side = HANDLE_PX / zoom
        painter.setBrush(QBrush(QColor("#ffffff")))
        for c in corners(rect):
            painter.drawRect(QRectF(c.x() - side / 2, c.y() - side / 2, side, side))


def _select_tool_of(app):
    """The active tool when it is the select tool, else None (the keys below only act then)."""
    tool = getattr(app, "active_tool", None)
    return tool if isinstance(tool, SelectTool) else None


def delete_selection(app):
    """Delete / Backspace (register_shortcut): delete the selection when the select tool is active.

    QLineEdit widgets (chat) and the inline text editor take these keys
    themselves before a window shortcut fires, so typing never deletes objects.
    """
    tool = _select_tool_of(app)
    if tool is not None:
        tool.delete_selected(app)


def select_all(app):
    """Ctrl+A (register_shortcut): select every object on the board when the select tool is active."""
    tool = _select_tool_of(app)
    if tool is not None:
        tool.select_all(app)


registry.register_tool(SelectTool)
registry.register_shortcut("Delete", delete_selection)
registry.register_shortcut("Backspace", delete_selection)
registry.register_shortcut("Ctrl+A", select_all)
