# OWNER: P2
"""pen_tool.py: Pen, marker, highlighter and eraser tools.

Four tools, registered with registry.register_tool (contract F and G):

PenTool ('pen', P)
    Solid freehand ink: the width from the slider, fully opaque.
MarkerTool ('marker', M)
    A felt marker: 1.5 x the slider width, opacity 0.6.
HighlighterTool ('highlighter', H)
    A see-through chisel highlighter: opacity 0.35, at least 12 units wide,
    flat cap (render_draw paints it with a multiply blend so ink stays dark).
EraserTool ('eraser', E)
    OBJECT eraser: removes every whole object its circle touches. The hit
    circle is previewed around the mouse; its radius is the slider value.

The three drawing tools differ only in their class attributes (stroke_tool,
alpha, width_factor, min_width), so the subclasses are a few lines each.
stroke_style(pen) turns app.pen (color, width as set by the dock) into the
contract style {tool, color, width, alpha} of the first OP_STROKE_PART. The
points are streamed by draw_sync.StrokeStreamer as OP_STROKE_PART messages
(batched every 40 ms, at most 50 points each).

Per-tool memory (toolbar.py)
----------------------------
A tool class that has a `pen_defaults` dict {color, width} takes part in the
dock's per-tool memory: when it is selected the dock loads ITS last color and
width into app.pen, so switching pen -> highlighter -> pen gives back the
pen's black 3 and the highlighter's yellow 16. `uses_color = False` (eraser)
tells the dock to grey out the palette. Shape tools use the same attributes.

All tools change the board only through client/ops.send_op, so the change
is applied locally at once (optimistic, contract E3) and the server echo is
recognised by its op_id and not applied twice.

Eraser hit testing
------------------
The hit radius is r = app.pen['width'] board units (the slider while the
eraser is active), but never less than MIN_RADIUS_PX screen pixels, so it
stays usable when zoomed out. An object is hit when its shape comes within
r of the cursor:
    stroke, line, arrow     distance from the cursor to each segment
                            <= r + half the line width
    everything else         inside the bounding box (x, y, w, h) grown by r
The preview circle is drawn with exactly the same r (EraserPreview), so what
you see is what gets erased.
"""

import math

from PyQt5.QtCore import QEvent, QPointF, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from client import ops
from client.draw_sync import StrokeStreamer
from client.registry import register_tool
from common import kinds_draw, protocol

MIN_RADIUS_PX = 3            # the eraser circle is never smaller than this on screen
PREVIEW_CHECK_MS = 100       # how often the preview checks it is still wanted


def _can_draw(app):
    """True unless the server made me a viewer (the server checks again anyway).

    Checking here avoids drawing an optimistic stroke the server will
    reject with FORBIDDEN. Offline (role None) drawing is allowed.
    """
    if getattr(app.me, "role", None) == "viewer":
        app.toast("Viewers cannot draw")
        return False
    return True


def _clamp_width(value):
    """Clamp a width to the contract range 1-40 with at most one decimal (an int when whole)."""
    width = round(max(kinds_draw.WIDTH_MIN, min(kinds_draw.WIDTH_MAX, float(value))), 1)
    return int(width) if width.is_integer() else width


# ------------------------------------------------------------ drawing tools

class PenTool:
    """Solid freehand pen. Subclasses change only the style attributes below."""

    id = "pen"
    label = "Pen"
    shortcut = "P"
    group = "draw"

    # Style rules (read by stroke_style and by the dock's preview).
    stroke_tool = "pen"      # the stroke's "tool" field (contract D)
    alpha = 1.0              # opacity of the stroke
    width_factor = 1.0       # drawn width = slider width * width_factor ...
    min_width = 1            # ... but at least min_width
    flat_cap = False         # only the highlighter has a flat (chisel) tip

    # Per-tool memory: first color and width, before the user changes them.
    pen_defaults = {"color": "#000000", "width": 3}
    uses_color = True

    def __init__(self):
        """Create the tool; the streamer is made on first use (it needs app)."""
        self._streamer = None

    @classmethod
    def stroke_style(cls, pen):
        """Return the contract style {tool, color, width, alpha} for app.pen values.

        The width is the slider width scaled by width_factor, raised to
        min_width and clamped to 1-40 (the server rejects anything else).
        """
        width = float(pen.get("width", cls.pen_defaults["width"])) * cls.width_factor
        return {"tool": cls.stroke_tool,
                "color": pen.get("color", cls.pen_defaults["color"]),
                "width": _clamp_width(max(width, cls.min_width)),
                "alpha": cls.alpha}

    def _get_streamer(self, canvas):
        """Return the StrokeStreamer for canvas.app, creating it once."""
        if self._streamer is None or self._streamer.app is not canvas.app:
            self._streamer = StrokeStreamer(canvas.app)
        return self._streamer

    def on_press(self, canvas, p, ev):
        """Left button: start a stroke at p in this tool's style (sent at once as the first part)."""
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._get_streamer(canvas).begin(p, style=self.stroke_style(canvas.app.pen))

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


class MarkerTool(PenTool):
    """Felt marker: 1.5 x the slider width at 60 % opacity."""

    id = "marker"
    label = "Marker"
    shortcut = "M"
    stroke_tool = "marker"
    alpha = 0.6
    width_factor = 1.5
    pen_defaults = {"color": "#1e88e5", "width": 6}


class HighlighterTool(PenTool):
    """Highlighter: 35 % opacity, at least 12 wide, flat chisel tip."""

    id = "highlighter"
    label = "Highlighter"
    shortcut = "H"
    stroke_tool = "highlighter"
    alpha = 0.35
    min_width = 12
    flat_cap = True
    pen_defaults = {"color": "#fdd835", "width": 16}


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


def _top_down(objects):
    """Objects from the top of the paint order down (same order as render.render_board, reversed).

    Paint order is by z; objects without z yet (my optimistic adds) are on
    top in insertion order.
    """
    indexed = list(enumerate(objects.values()))
    indexed.sort(key=lambda item: (item[1].get("z") is None, item[1].get("z") or 0, item[0]))
    return [obj for _, obj in reversed(indexed)]


def topmost_hit(objects, x, y, tol):
    """Return the obj_id of the topmost object hit at (x, y), or None."""
    for obj in _top_down(objects):
        if hit_object(obj, x, y, tol):
            return obj.get("id")
    return None


def all_hits(objects, x, y, tol):
    """Return the obj_ids of every object hit at (x, y), topmost first."""
    return [obj.get("id") for obj in _top_down(objects) if hit_object(obj, x, y, tol)]


# ----------------------------------------------------------- eraser tool

def eraser_radius(canvas):
    """Hit radius of the eraser in BOARD units: the slider width, min MIN_RADIUS_PX on screen.

    The preview circle and the hit test both use this, so they always agree.
    """
    zoom = max(getattr(canvas, "zoom", 1.0), 1e-6)
    width = _num(canvas.app.pen.get("width"), EraserTool.pen_defaults["width"])
    return max(width, MIN_RADIUS_PX / zoom)


class EraserPreview(QWidget):
    """Transparent overlay on the canvas that draws the eraser's hit circle at the mouse.

    It is a child widget of the canvas covering its whole area, so it is
    painted on top of the board; WA_TransparentForMouseEvents lets every
    click go through to the canvas. There is one per canvas (found again
    with canvas.findChild). It hides itself when the mouse leaves the
    canvas or the active tool is no longer an eraser, and repaints when the
    zoom or the slider (the radius) changes while the mouse is still. The
    check runs in a QTimer on the GUI thread, so nothing ever blocks.
    """

    def __init__(self, canvas):
        """Create the hidden overlay over canvas and start watching it."""
        super().__init__(canvas)
        self.canvas = canvas
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setGeometry(canvas.rect())
        self._pos = None          # board point of the circle centre
        self._pressed = False     # tint the circle while erasing
        self._shown_key = None    # (radius, zoom, offset) last painted
        canvas.installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.setInterval(PREVIEW_CHECK_MS)
        self._timer.timeout.connect(self._check)
        self.hide()

    @classmethod
    def for_canvas(cls, canvas):
        """Return the canvas's overlay, creating it on first use."""
        preview = canvas.findChild(cls)
        return preview if preview is not None else cls(canvas)

    def show_at(self, p, pressed):
        """Centre the circle on board point p and show it."""
        self._pos = QPointF(p)
        self._pressed = pressed
        if self.geometry() != self.canvas.rect():
            self.setGeometry(self.canvas.rect())
        if not self.isVisible():
            self.show()
            self.raise_()
            self._timer.start()
        self.update()

    def _key(self):
        """What the circle depends on besides the mouse: radius, zoom and pan."""
        off = self.canvas.offset
        return (eraser_radius(self.canvas), self.canvas.zoom, off.x(), off.y())

    def _check(self):
        """Timer: hide when not wanted any more, repaint when the radius or view changed."""
        if not isinstance(getattr(self.canvas.app, "active_tool", None), EraserTool) \
                or not self.canvas.underMouse():
            self._hide()
        elif self._key() != self._shown_key:
            self.update()

    def _hide(self):
        """Hide the overlay and stop the timer."""
        self._timer.stop()
        self.hide()

    def eventFilter(self, obj, ev):
        """Follow the canvas: hide when the mouse leaves it, resize with it. Never eat events."""
        if obj is self.canvas:
            if ev.type() == QEvent.Leave:
                self._hide()
            elif ev.type() == QEvent.Resize:
                self.setGeometry(self.canvas.rect())
        return False

    def paintEvent(self, ev):
        """Draw the hit circle: white halo + dark ring (visible on any color), red tint while erasing."""
        if self._pos is None:
            return
        self._shown_key = self._key()
        center = self.canvas.board_to_screen(self._pos)
        r = eraser_radius(self.canvas) * self.canvas.zoom   # board units -> screen pixels
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            if self._pressed:
                painter.setBrush(QColor(229, 57, 53, 40))
            else:
                painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, 220), 3))
            painter.drawEllipse(center, r, r)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#333333"), 1))
            painter.drawEllipse(center, r, r)
        finally:
            painter.end()


class EraserTool:
    """Object eraser: OP_DELETE for every object its circle touches (once each)."""

    id = "eraser"
    label = "Eraser"
    shortcut = "E"
    group = "draw"

    # Per-tool memory: the slider is the hit radius; no color.
    pen_defaults = {"color": "#000000", "width": 10}
    uses_color = False

    def __init__(self):
        """Create the tool in the idle state."""
        self._down = False
        self._last = None        # last board point tested during this drag
        self._sent = set()       # obj_ids already deleted during this drag

    def on_press(self, canvas, p, ev):
        """Left button: start erasing and test the circle under the cursor."""
        EraserPreview.for_canvas(canvas).show_at(p, pressed=ev.button() == Qt.LeftButton)
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._down = True
        self._sent = set()
        self._last = (p.x(), p.y())
        self._erase_at(canvas, p.x(), p.y())

    def on_move(self, canvas, p, ev):
        """Move the preview circle; while dragging, erase along the path.

        The canvas forwards hover moves too (mouse tracking), so the circle
        follows the mouse even with no button held. A fast drag jumps many
        units between two mouse events; testing the path every radius
        units means nothing the circle swept over is missed.
        """
        dragging = self._down and bool(ev.buttons() & Qt.LeftButton)
        EraserPreview.for_canvas(canvas).show_at(p, pressed=dragging)
        if not dragging:
            return
        step = eraser_radius(canvas)
        x0, y0 = self._last
        x1, y1 = p.x(), p.y()
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for i in range(1, steps + 1):
            t = i / steps
            self._erase_at(canvas, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        self._last = (x1, y1)

    def on_release(self, canvas, p, ev):
        """Stop erasing; the preview stays, untinted."""
        self._down = False
        self._last = None
        self._sent = set()
        EraserPreview.for_canvas(canvas).show_at(p, pressed=False)

    def _erase_at(self, canvas, x, y):
        """Delete every object the circle at (x, y) touches, at most once per object per drag.

        send_op removes the object locally at once (optimistic), so the
        next test no longer finds it; _sent also guards against a delete
        being sent twice before that (e.g. if the board is not attached).
        """
        app = canvas.app
        board = app.board
        if board is None:
            return
        for obj_id in all_hits(board.objects, x, y, eraser_radius(canvas)):
            if obj_id is None or obj_id in self._sent:
                continue
            self._sent.add(obj_id)
            ops.send_op(app, protocol.OP_DELETE, {"obj_id": obj_id})

    def cursor(self):
        """Small crosshair in the middle of the preview circle."""
        return Qt.CrossCursor


register_tool(PenTool)
register_tool(MarkerTool)
register_tool(HighlighterTool)
register_tool(EraserTool)
