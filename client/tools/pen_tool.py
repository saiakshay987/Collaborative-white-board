# OWNER: P2
"""pen_tool.py: Pen, marker and highlighter tools.

Two tools, registered with registry.register_tool (contract F and G):

PenTool ('pen')
    Freehand drawing. The style comes from app.pen (color, width, tool =
    pen / marker / highlighter, alpha), so the same tool draws all three
    looks. The points are streamed by draw_sync.StrokeStreamer as
    OP_STROKE_PART messages (batched every 40 ms, at most 50 points each).

EraserTool ('eraser')
    OBJECT eraser: it removes whole objects, never parts of them. On press
    and while dragging it hit-tests the topmost object under the cursor and
    sends OP_DELETE {obj_id} once per object.

Both tools change the board only through client/ops.send_op, so the change
is applied locally at once (optimistic, contract E3) and the server echo is
recognised by its op_id and not applied twice.

Hit testing (eraser)
--------------------
The tolerance is 8 SCREEN pixels, converted to board units by dividing by
canvas.zoom: zoomed out (zoom 0.5) one screen pixel covers 2 board units,
so the eraser reaches 16 board units; zoomed in it gets finer. Per kind:
    stroke, line, arrow     distance from the cursor to each segment
                            <= tolerance + half the line width
    everything else         inside the bounding box (x, y, w, h)
                            grown by the tolerance
Objects are tested from the top of the paint order down (the same order
as render.render_board), so the object you see is the one erased.
"""

import math

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QCursor, QPainter, QPen, QPixmap

from client import ops
from client.draw_sync import StrokeStreamer
from client.registry import register_tool
from common import protocol

ERASER_TOLERANCE_PX = 8      # hit radius on the screen, in pixels


def _can_draw(app):
    """True unless the server made me a viewer (the server checks again anyway).

    Checking here avoids drawing an optimistic stroke the server will
    reject with FORBIDDEN. Offline (role None) drawing is allowed.
    """
    if getattr(app.me, "role", None) == "viewer":
        app.toast("Viewers cannot draw")
        return False
    return True


# ----------------------------------------------------------------- pen tool

class PenTool:
    """Freehand pen; app.pen decides color, width, alpha and pen/marker/highlighter."""

    id = "pen"
    label = "Pen"
    shortcut = "P"
    group = "draw"

    def __init__(self):
        """Create the tool; the streamer is made on first use (it needs app)."""
        self._streamer = None

    def _get_streamer(self, canvas):
        """Return the StrokeStreamer for canvas.app, creating it once."""
        if self._streamer is None or self._streamer.app is not canvas.app:
            self._streamer = StrokeStreamer(canvas.app)
        return self._streamer

    def on_press(self, canvas, p, ev):
        """Left button: start a stroke at p (sent at once as the first part)."""
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._get_streamer(canvas).begin(p)

    def on_move(self, canvas, p, ev):
        """While the left button is held, add p to the stroke (buffered by the streamer)."""
        if self._streamer is not None and self._streamer.active and ev.buttons() & Qt.LeftButton:
            self._streamer.add(p)

    def on_release(self, canvas, p, ev):
        """Finish the stroke: the last part goes out with done:true."""
        if self._streamer is not None and self._streamer.active:
            self._streamer.end(p)

    def cursor(self):
        """Crosshair: the exact point being drawn is visible."""
        return Qt.CrossCursor


# ------------------------------------------------------------ hit testing

def _num(value, default=0.0):
    """Return value as a float, or default if it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dist_to_segment(px, py, ax, ay, bx, by):
    """Shortest distance from point P to the segment AB.

    Project P onto the line AB: t = (AP . AB) / |AB|^2, clamped to [0, 1]
    so the closest point stays on the segment, then measure P to A + t*AB.
    """
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def hit_object(obj, x, y, tol):
    """True if board point (x, y) is within tol board units of obj (rules in the module docstring)."""
    half = _num(obj.get("width"), 0.0) / 2.0
    pts = obj.get("points")
    if obj.get("kind") == "stroke" and isinstance(pts, list):
        reach = tol + half
        pts = [p for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(pts) == 1:
            return math.hypot(x - pts[0][0], y - pts[0][1]) <= reach
        for a, b in zip(pts, pts[1:]):
            # Cheap box reject before the exact segment distance.
            if (min(a[0], b[0]) - reach <= x <= max(a[0], b[0]) + reach
                    and min(a[1], b[1]) - reach <= y <= max(a[1], b[1]) + reach
                    and dist_to_segment(x, y, a[0], a[1], b[0], b[1]) <= reach):
                return True
        return False
    if "x1" in obj and "x2" in obj:          # line, arrow
        return dist_to_segment(x, y, _num(obj.get("x1")), _num(obj.get("y1")),
                               _num(obj.get("x2")), _num(obj.get("y2"))) <= tol + half
    if "x" in obj and "y" in obj:            # rect, ellipse, text, sticky, image
        x0, y0 = _num(obj.get("x")), _num(obj.get("y"))
        x1, y1 = x0 + _num(obj.get("w")), y0 + _num(obj.get("h"))
        return (min(x0, x1) - tol <= x <= max(x0, x1) + tol
                and min(y0, y1) - tol <= y <= max(y0, y1) + tol)
    return False


def topmost_hit(objects, x, y, tol):
    """Return the obj_id of the topmost object hit at (x, y), or None.

    Paint order is the same as render.render_board: by z, and objects
    without z yet (my optimistic adds) on top in insertion order. We walk
    it backwards so the first hit is the one on top.
    """
    indexed = list(enumerate(objects.values()))
    indexed.sort(key=lambda item: (item[1].get("z") is None, item[1].get("z") or 0, item[0]))
    for _, obj in reversed(indexed):
        if hit_object(obj, x, y, tol):
            return obj.get("id")
    return None


# ----------------------------------------------------------- eraser tool

def _eraser_cursor():
    """A circle cursor whose radius is the eraser tolerance (8 px)."""
    r = ERASER_TOLERANCE_PX
    size = 2 * r + 3
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor("#ffffff"), 3))
    painter.drawEllipse(QPointF(size / 2.0, size / 2.0), r, r)
    painter.setPen(QPen(QColor("#333333"), 1))
    painter.drawEllipse(QPointF(size / 2.0, size / 2.0), r, r)
    painter.end()
    return QCursor(pix, size // 2, size // 2)


class EraserTool:
    """Object eraser: OP_DELETE for every object the cursor touches (once each)."""

    id = "eraser"
    label = "Eraser"
    shortcut = "E"
    group = "draw"

    def __init__(self):
        """Create the tool in the idle state."""
        self._down = False
        self._last = None        # last board point tested during this drag
        self._sent = set()       # obj_ids already deleted during this drag

    def on_press(self, canvas, p, ev):
        """Left button: start erasing and test the point under the cursor."""
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._down = True
        self._sent = set()
        self._last = (p.x(), p.y())
        self._erase_at(canvas, p.x(), p.y())

    def on_move(self, canvas, p, ev):
        """While dragging, test every point between the last and this one.

        A fast drag jumps many pixels between two mouse events; sampling
        the path every `tol` board units means a thin line crossed by the
        jump is still erased.
        """
        if not self._down or not ev.buttons() & Qt.LeftButton:
            return
        tol = ERASER_TOLERANCE_PX / max(canvas.zoom, 1e-6)
        x0, y0 = self._last
        x1, y1 = p.x(), p.y()
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / tol))
        for i in range(1, steps + 1):
            t = i / steps
            self._erase_at(canvas, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        self._last = (x1, y1)

    def on_release(self, canvas, p, ev):
        """Stop erasing."""
        self._down = False
        self._last = None
        self._sent = set()

    def _erase_at(self, canvas, x, y):
        """Delete the topmost object at (x, y), at most once per object per drag.

        The tolerance is 8 screen pixels scaled to board units by the zoom.
        send_op removes the object locally at once, so the next hit test
        no longer finds it; _sent also guards against a delete being sent
        twice before that (e.g. if the board is not attached).
        """
        app = canvas.app
        board = app.board
        if board is None:
            return
        tol = ERASER_TOLERANCE_PX / max(canvas.zoom, 1e-6)
        obj_id = topmost_hit(board.objects, x, y, tol)
        if obj_id is None or obj_id in self._sent:
            return
        self._sent.add(obj_id)
        ops.send_op(app, protocol.OP_DELETE, {"obj_id": obj_id})

    def cursor(self):
        """Circle the size of the hit tolerance."""
        return _eraser_cursor()


register_tool(PenTool)
register_tool(EraserTool)
