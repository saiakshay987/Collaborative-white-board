# OWNER: P2
"""shape_tools.py: Line, arrow, rectangle and ellipse tools.

Four tools, registered with registry.register_tool (contract F and G):

    LineTool     'line'     L    kind line    {x1, y1, x2, y2, color, width}
    ArrowTool    'arrow'    A    kind arrow   {x1, y1, x2, y2, color, width}
    RectTool     'rect'     R    kind rect    {x, y, w, h, color, width, fill}
    EllipseTool  'ellipse'  O    kind ellipse {x, y, w, h, color, width, fill}

How a shape is drawn
--------------------
press    remember the start point (board units, already snapped by the canvas
         when snapping is on).
move     remember the current point. Nothing is sent: the canvas repaints
         and calls paint_overlay(), which draws a "rubber-band" preview of
         the shape with the SAME renderer the board uses (render_draw), so
         the preview looks exactly like the final shape. Other users see
         nothing yet.
release  build the finished object and send ONE OP_ADD {obj} through
         client/ops.send_op. send_op applies it locally at once (optimistic,
         contract E3) and the server echo, recognised by its op_id, only
         confirms it. A click without a drag sends nothing.

So a whole shape costs exactly one network message, however long the drag.

Shift constrains the shape (constrain()):
    line, arrow     the angle snaps to the nearest multiple of 45 degrees
                    (horizontal, vertical or diagonal)
    rect, ellipse   width and height become equal: a square or a circle

Snap-to-grid: the canvas snaps every point to config.GRID before the tool
sees it (canvas.snap), and the Shift rules only use max/abs of grid
multiples, so a constrained shape is still on the grid. The server
therefore only ever stores snapped values (contract D).

Every coordinate is rounded to one decimal (contract D). Color and width come
from app.pen (the tool dock, with per-tool memory through pen_defaults);
rect and ellipse are sent with fill null (outline only).
"""

import math

from PyQt5.QtCore import QPointF, Qt

from client import ops, registry
from client.tools.pen_tool import _can_draw, _clamp_width
from common import kinds_draw, protocol

# A drag shorter than this (board units) in both directions is treated as a
# click and sends nothing, so a stray click never leaves an invisible shape.
MIN_DRAG = 2.0


def _r1(value):
    """Round to one decimal (contract D), as an int when it is whole (smaller JSON)."""
    value = round(float(value), 1)
    return int(value) if value.is_integer() else value


def constrain_line(start, end):
    """Return end moved so the line start->end is at 0, 45 or 90 degrees (any direction).

    The angle is rounded to the nearest multiple of 45 degrees. For a
    horizontal or vertical line the other coordinate is copied from start;
    for a diagonal both offsets get the larger of |dx| and |dy|, so with
    grid-snapped input the result is still on the grid.
    """
    dx, dy = end.x() - start.x(), end.y() - start.y()
    if dx == 0 and dy == 0:
        return QPointF(end)
    octant = round(math.atan2(dy, dx) / (math.pi / 4)) % 8   # 0..7, 45 degrees each
    if octant in (0, 4):                                     # horizontal
        return QPointF(end.x(), start.y())
    if octant in (2, 6):                                     # vertical
        return QPointF(start.x(), end.y())
    d = max(abs(dx), abs(dy))                                # diagonal
    return QPointF(start.x() + math.copysign(d, dx), start.y() + math.copysign(d, dy))


def constrain_box(start, end):
    """Return end moved so the box from start to end is a square (keeps the drag direction)."""
    dx, dy = end.x() - start.x(), end.y() - start.y()
    d = max(abs(dx), abs(dy))
    return QPointF(start.x() + math.copysign(d, dx), start.y() + math.copysign(d, dy))


class _ShapeTool:
    """Common drag logic of the four shape tools; subclasses set kind and build()."""

    group = "shapes"
    kind = None                      # contract D kind of the object this tool makes

    # Per-tool memory in the tool dock (toolbar.py): first color and width.
    pen_defaults = {"color": "#000000", "width": 3}
    uses_color = True

    def __init__(self):
        """Start idle: no drag in progress."""
        self._start = None           # board point where the drag started, None when idle
        self._end = None             # current (possibly constrained) end point

    # ------------------------------------------------------------ geometry
    def constrain(self, start, end):
        """Shift rule for this kind; overridden by the line and box bases."""
        return end

    def build(self, app, start, end):
        """Return the contract-D object for a drag from start to end (without id)."""
        raise NotImplementedError

    def _style(self, app):
        """Color and width from app.pen, in the ranges the server accepts."""
        return {"color": app.pen.get("color", self.pen_defaults["color"]),
                "width": _clamp_width(app.pen.get("width", self.pen_defaults["width"]))}

    def _end_point(self, p, ev):
        """End point of the drag: p, constrained when Shift is held."""
        if ev.modifiers() & Qt.ShiftModifier:
            return self.constrain(self._start, p)
        return QPointF(p)

    def _is_click(self):
        """True when the drag was too short to be a shape (both directions under MIN_DRAG)."""
        return (abs(self._end.x() - self._start.x()) < MIN_DRAG
                and abs(self._end.y() - self._start.y()) < MIN_DRAG)

    # --------------------------------------------------------- tool events
    def on_press(self, canvas, p, ev):
        """Left button: start a new shape at p (nothing is sent yet)."""
        if ev.button() != Qt.LeftButton or not _can_draw(canvas.app):
            return
        self._start = QPointF(p)
        self._end = QPointF(p)

    def on_move(self, canvas, p, ev):
        """While dragging, update the end point; the canvas repaints the preview."""
        if self._start is not None and ev.buttons() & Qt.LeftButton:
            self._end = self._end_point(p, ev)

    def on_release(self, canvas, p, ev):
        """Finish the shape and send it as ONE OP_ADD through send_op."""
        if self._start is None:
            return
        self._end = self._end_point(p, ev)
        start, end = self._start, self._end
        click = self._is_click()
        self._start = self._end = None          # idle again whatever happens below
        if click:
            return
        app = canvas.app
        obj = self.build(app, start, end)
        # The object id is "<user>-<n>", made like an op_id (contract D).
        obj["id"] = protocol.new_op_id(app.me.username or "me")
        ok, reason = kinds_draw.validate(obj)    # same check the server runs
        if not ok:
            app.toast("Shape not sent: %s" % reason)
            return
        ops.send_op(app, protocol.OP_ADD, {"obj": obj})

    def paint_overlay(self, painter, canvas):
        """Rubber-band preview (local only): draw the shape being dragged with its real renderer.

        Called by the canvas after the board, with the painter already in
        board units. The preview object never goes to BoardModel or the
        network.
        """
        if self._start is None or self._end is None or self._is_click():
            return
        fn = registry.get_renderer(self.kind)
        if fn is not None:
            fn(painter, self.build(canvas.app, self._start, self._end), {"canvas": canvas})

    def cursor(self):
        """Crosshair: the exact corner or end point is visible."""
        return Qt.CrossCursor


class _LineBase(_ShapeTool):
    """Shapes defined by two end points: line and arrow."""

    def constrain(self, start, end):
        """Shift: 0, 45 or 90 degree line."""
        return constrain_line(start, end)

    def build(self, app, start, end):
        """{kind, x1, y1, x2, y2, color, width}: the end points exactly as dragged."""
        obj = {"kind": self.kind,
               "x1": _r1(start.x()), "y1": _r1(start.y()),
               "x2": _r1(end.x()), "y2": _r1(end.y())}
        obj.update(self._style(app))
        return obj


class _BoxBase(_ShapeTool):
    """Shapes defined by a bounding box: rect and ellipse."""

    def constrain(self, start, end):
        """Shift: square / circle."""
        return constrain_box(start, end)

    def build(self, app, start, end):
        """{kind, x, y, w, h, color, width, fill}: normalized so w and h are >= 0.

        A drag up or to the left gives a negative size; the validator
        requires w, h >= 0, so x, y is moved to the top-left corner.
        """
        x, y = min(start.x(), end.x()), min(start.y(), end.y())
        obj = {"kind": self.kind,
               "x": _r1(x), "y": _r1(y),
               "w": _r1(abs(end.x() - start.x())), "h": _r1(abs(end.y() - start.y())),
               "fill": None}
        obj.update(self._style(app))
        return obj


class LineTool(_LineBase):
    """Straight line; Shift for 0/45/90 degrees."""

    id = "line"
    label = "Line"
    shortcut = "L"
    kind = "line"


class ArrowTool(_LineBase):
    """Line with an arrowhead at the release point; Shift for 0/45/90 degrees."""

    id = "arrow"
    label = "Arrow"
    shortcut = "A"
    kind = "arrow"


class RectTool(_BoxBase):
    """Rectangle outline; Shift for a square."""

    id = "rect"
    label = "Rectangle"
    shortcut = "R"
    kind = "rect"


class EllipseTool(_BoxBase):
    """Ellipse outline inside the dragged box; Shift for a circle."""

    id = "ellipse"
    label = "Ellipse"
    shortcut = "O"
    kind = "ellipse"


registry.register_tool(LineTool)
registry.register_tool(ArrowTool)
registry.register_tool(RectTool)
registry.register_tool(EllipseTool)
