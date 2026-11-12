# OWNER: P2
"""canvas.py: CanvasWidget: zoom/pan, screen_to_board, board_to_screen, snap-to-grid and the cursor_moved signal; forwards mouse events to the active tool.

Contract section F (client):
    CanvasWidget with screen_to_board(pt), board_to_screen(pt), snap(pt),
    zoom, offset and signal cursor_moved(float, float).

Coordinate systems
------------------
Board units: the shared 4000 x 3000 page (config.BOARD_W/H), origin top-left.
Every coordinate that goes into a network message is in board units, so all
users see the same drawing whatever their own zoom and pan are.

Screen pixels: this widget's local coordinates. Each client zooms and pans
on its own (contract D), so the mapping is purely local:

    screen = board * zoom + offset          board = (screen - offset) / zoom

zoom is a float (0.1 to 8.0); offset is a QPointF, the screen position of the
board origin (0, 0).

Data flow
---------
Network data never reaches this widget directly. NetClient delivers messages
on the GUI thread through a Qt signal, BoardModel applies them and emits
`changed`, and this widget only schedules a repaint (update()) in response.
So painting never waits on the network and the GUI thread never blocks.

Mouse input goes the other way: press/move/release are converted to board
units (snapped when snapping is on) and handed to app.active_tool, which
builds the operation and sends it through client/ops.py.

The page rule
-------------
The page is the rectangle 0..BOARD_W x 0..BOARD_H and nothing may leave it:

    * objects are CLIPPED to the page when painted (setClipRect), so even an
      object that somehow lies partly outside never shows outside;
    * a press that starts outside the page is NOT forwarded to the tool, and
      the pointer is a "not allowed" cursor there for every drawing tool;
    * every point a tool receives is CLAMPED to the page edge (clamp_point),
      so a drag that leaves the page just slides along the edge;
    * clamp_point / clamp_rect / limit_to_page below are the ONE place where
      this rule is written. Tools (shapes, select) import and reuse them.

The server enforces the same rule on what it accepts (common/kinds_*.py),
because a client can never be trusted to have clamped anything.
"""

import math
import traceback

from PyQt5.QtCore import QLineF, QPointF, QRectF, QSizeF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QPainter, QPen
from PyQt5.QtWidgets import QLabel, QWidget

from client import render
from common import config

# Zoom limits and the zoom change per wheel notch (120 angle-delta units).
MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
ZOOM_STEP = 1.15

# Colors of the background outside the page, the page and its grid.
OUTSIDE_COLOR = QColor("#d6d6d6")
PAGE_COLOR = QColor("#ffffff")
PAGE_BORDER_COLOR = QColor("#616161")
GRID_COLOR = QColor("#ececec")

# Grid lines closer than this on screen (pixels) are skipped: they would only
# grey out the page and cost time when zoomed far out.
MIN_GRID_PX = 6

# Extra board units added around each object's bounding box when culling, so
# pen width, arrowheads and antialiasing at the edge are never cut off.
CULL_MARGIN = 30.0

# Margin (pixels) left around the page by fit_page().
FIT_MARGIN = 20

# Soft shadow under the page: SHADOW_STEPS translucent rectangles, each
# SHADOW_GROW pixels bigger than the previous one, shifted by SHADOW_SHIFT
# (right, down), so the edge fades out. Alpha is per rectangle and adds up.
SHADOW_STEPS = 7
SHADOW_GROW = 1.6
SHADOW_SHIFT = (2.0, 3.0)
SHADOW_ALPHA = 13

# Width (pixels) of the page border line.
BORDER_PX = 2


# ------------------------------------------------------------ the page rule
# The only place where "stay inside the page" is written down. All of these
# work in board units and never modify their argument.

def page_rect():
    """The page as a QRectF in board units: (0, 0) to (BOARD_W, BOARD_H)."""
    return QRectF(0, 0, config.BOARD_W, config.BOARD_H)


def on_page(pt):
    """True when board point pt lies on the page (the edges count as inside)."""
    return 0 <= pt.x() <= config.BOARD_W and 0 <= pt.y() <= config.BOARD_H


def clamp_point(pt):
    """Return pt (QPointF) moved to the nearest point of the page."""
    return QPointF(max(0.0, min(float(config.BOARD_W), pt.x())),
                   max(0.0, min(float(config.BOARD_H), pt.y())))


def clamp_rect(rect):
    """Return rect (QRectF, normalized) moved, and shrunk only if too big, to lie fully on the page.

    Used for anything with a size: a box that is dragged past an edge is
    pushed back so it keeps its size; one bigger than the page is cut to
    the page size.
    """
    rect = rect.normalized()
    w = min(rect.width(), float(config.BOARD_W))
    h = min(rect.height(), float(config.BOARD_H))
    x = max(0.0, min(rect.x(), config.BOARD_W - w))
    y = max(0.0, min(rect.y(), config.BOARD_H - h))
    return QRectF(x, y, w, h)


def limit_to_page(start, end):
    """Return end pulled back TOWARD start until it is on the page, keeping the direction.

    start must be on the page. Clamping x and y separately would bend a
    45 degree line or a square (Shift) when only one coordinate hits the
    edge. Instead the whole offset (dx, dy) is shortened by one common
    factor t <= 1, so the angle and the proportions stay exactly as they were.
    """
    dx, dy = end.x() - start.x(), end.y() - start.y()
    t = 1.0
    if dx > 0:
        t = min(t, (config.BOARD_W - start.x()) / dx)
    elif dx < 0:
        t = min(t, (0.0 - start.x()) / dx)
    if dy > 0:
        t = min(t, (config.BOARD_H - start.y()) / dy)
    elif dy < 0:
        t = min(t, (0.0 - start.y()) / dy)
    t = max(0.0, t)
    return clamp_point(QPointF(start.x() + dx * t, start.y() + dy * t))


def _num(value, default=0.0):
    """Return value as a float, or default when it is missing or not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def object_bounds(obj):
    """Return the bounding QRectF of a board object in board units, or None.

    Works for every kind in contract section D: strokes use their points,
    line/arrow use x1,y1,x2,y2, and rect/ellipse/text/sticky/image use
    x,y,w,h (w or h may be negative while a shape is being dragged, so the
    rect is normalized). None means "unknown geometry": the caller must
    paint the object rather than risk hiding it.
    """
    pts = obj.get("points")
    if isinstance(pts, list) and pts:
        xs = [_num(p[0]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        ys = [_num(p[1]) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        if not xs:
            return None
        rect = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    elif "x1" in obj and "x2" in obj:
        x1, y1 = _num(obj.get("x1")), _num(obj.get("y1"))
        x2, y2 = _num(obj.get("x2")), _num(obj.get("y2"))
        rect = QRectF(QPointF(x1, y1), QPointF(x2, y2)).normalized()
    elif "x" in obj and "y" in obj:
        rect = QRectF(_num(obj.get("x")), _num(obj.get("y")),
                      _num(obj.get("w")), _num(obj.get("h"))).normalized()
    else:
        return None
    pad = CULL_MARGIN + _num(obj.get("width"), 0.0)
    return rect.adjusted(-pad, -pad, pad, pad)


class CanvasWidget(QWidget):
    """The drawing surface: shows the board with local zoom/pan and feeds the active tool.

    Public API (contract F):
        zoom                    current scale, screen pixels per board unit
        offset                  QPointF, screen position of the board origin
        screen_to_board(pt)     QPointF in board units for a screen point
        board_to_screen(pt)     QPointF in screen pixels for a board point
        snap(pt)                pt snapped to the grid when snapping is on
        cursor_moved(x, y)      signal, board units, on every mouse move

    Local view options (not shared with other users):
        show_grid / set_show_grid(on)       draw the grid every grid_size units
        snap_enabled / set_snapping(on)     snap tool points to the grid
        grid_size / set_grid_size(n)        grid spacing, config.GRID (20) by default

    Tool control (used by shortcuts.py):
        is_drawing()            True while a tool got a press and awaits the release
        cancel_tool()           Escape: abort the active tool's current drawing
    """

    cursor_moved = pyqtSignal(float, float)

    def __init__(self, app, parent=None):
        """Create the canvas for AppState app and publish it as app.canvas."""
        super().__init__(parent)
        self.app = app
        app.canvas = self

        self.zoom = 1.0
        self.offset = QPointF(FIT_MARGIN, FIT_MARGIN)
        # Grid display and snapping are toggled together with G (shortcuts.py),
        # so they start in the same state: off, so freehand ink is not snapped.
        self.show_grid = False
        self.snap_enabled = False
        # Grid spacing in board units. The contract default is config.GRID;
        # the command bar ('grid n') may change it for this client only.
        # Snapping still happens here, before sending, so the server simply
        # stores whatever snapped values arrive (contract D).
        self.grid_size = float(config.GRID)
        self._snap_label = None      # "Snap on/off" label in the window's status bar

        self._pan_last = None        # last screen pos while panning, else None
        self._pan_button = None      # mouse button that started the pan
        self._space_down = False     # Space held: left-drag pans instead of drawing
        self._tool_pressed = False   # the active tool got on_press and awaits on_release
        self._cursor_tool = None     # tool whose cursor is currently shown
        self._forbidden = False      # the "not allowed" cursor is shown (pointer off the page)
        self._bounds_cache = {}      # obj_id -> QRectF (or None) for culling
        self._fitted = False         # fit_page() already ran once on first show

        # Hover moves must arrive too (for cursor_moved), not only drags.
        self.setMouseTracking(True)
        # Keyboard focus is needed to see Space for Space+drag panning.
        self.setFocusPolicy(Qt.StrongFocus)
        # We paint every pixel ourselves; skip Qt's background erase.
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMinimumSize(200, 150)

        self.attach_board(getattr(app, "board", None))

    # ------------------------------------------------------------ board model
    def attach_board(self, board):
        """Listen to board.changed so remote and local edits trigger a repaint.

        BoardModel emits `changed` on the GUI thread (it is fed by
        NetClient's Qt signal), so calling update() here is safe. update()
        only schedules a repaint: many changes in one event-loop pass are
        merged into a single paintEvent.
        """
        if board is None:
            return
        board.changed.connect(self._on_board_changed)
        self.update()

    def _on_board_changed(self, obj_id):
        """Forget cached bounds of the changed object (None: all) and repaint."""
        if obj_id is None:
            self._bounds_cache.clear()
        else:
            self._bounds_cache.pop(obj_id, None)
        self.update()

    # ------------------------------------------------------------- transforms
    def screen_to_board(self, pt):
        """Convert a screen point (QPoint/QPointF, widget pixels) to board units."""
        return QPointF((pt.x() - self.offset.x()) / self.zoom,
                       (pt.y() - self.offset.y()) / self.zoom)

    def board_to_screen(self, pt):
        """Convert a board point (QPointF or (x, y)) to widget pixels."""
        if isinstance(pt, (tuple, list)):
            pt = QPointF(pt[0], pt[1])
        return QPointF(pt.x() * self.zoom + self.offset.x(),
                       pt.y() * self.zoom + self.offset.y())

    def snap(self, pt):
        """Return pt (board units) snapped to the nearest grid point when snapping is on.

        Snapping happens on the client before sending (contract D), so the
        server stores the snapped values. With snapping off pt is returned
        unchanged.
        """
        if not self.snap_enabled:
            return QPointF(pt)
        g = self.grid_size
        return QPointF(round(pt.x() / g) * g, round(pt.y() / g) * g)

    def visible_board_rect(self):
        """Return the part of the board visible in the widget, in board units."""
        tl = self.screen_to_board(QPointF(0, 0))
        br = self.screen_to_board(QPointF(self.width(), self.height()))
        return QRectF(tl, br)

    # ---------------------------------------------------------- view control
    def set_zoom(self, zoom, anchor=None):
        """Set zoom (clamped to 0.1-8x), keeping the board point under anchor fixed.

        anchor is a screen point (default: widget centre). We remember which
        board point is under it, change zoom, then move offset so that the
        same board point is under the anchor again: this is "zoom around
        the mouse".
        """
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        if anchor is None:
            anchor = QPointF(self.width() / 2.0, self.height() / 2.0)
        anchor = QPointF(anchor)
        before = self.screen_to_board(anchor)
        self.zoom = zoom
        self.offset = QPointF(anchor.x() - before.x() * zoom,
                              anchor.y() - before.y() * zoom)
        self.update()

    def pan_by(self, dx, dy):
        """Move the view by (dx, dy) screen pixels."""
        self.offset = QPointF(self.offset.x() + dx, self.offset.y() + dy)
        self.update()

    def fit_page(self):
        """Zoom and centre so the whole page fits in the widget."""
        w = max(1, self.width() - 2 * FIT_MARGIN)
        h = max(1, self.height() - 2 * FIT_MARGIN)
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, min(w / config.BOARD_W, h / config.BOARD_H)))
        self.offset = QPointF((self.width() - config.BOARD_W * self.zoom) / 2.0,
                              (self.height() - config.BOARD_H * self.zoom) / 2.0)
        self.update()

    def set_show_grid(self, on):
        """Show or hide the grid (local view option)."""
        self.show_grid = bool(on)
        self.update()

    def set_grid_size(self, size):
        """Set the grid spacing in board units (local view option) and repaint."""
        self.grid_size = float(size)
        self.update()

    def set_snapping(self, on):
        """Turn snap-to-grid for tool points on or off (local option) and show it in the status bar."""
        self.snap_enabled = bool(on)
        self._show_snap_status()

    def _show_snap_status(self):
        """Show 'Snap on' / 'Snap off' as a permanent label in the main window's status bar.

        The label is added on first use, once app.window exists (run_client
        sets it before show(), so the first showEvent is early enough).
        """
        window = getattr(self.app, "window", None)
        if window is None or not hasattr(window, "statusBar"):
            return
        if self._snap_label is None:
            self._snap_label = QLabel()
            self._snap_label.setStyleSheet("padding: 0 6px;")
            self._snap_label.setToolTip("Grid and snap-to-grid: press G to toggle")
            window.statusBar().addPermanentWidget(self._snap_label)
        self._snap_label.setText("Snap on" if self.snap_enabled else "Snap off")

    def showEvent(self, ev):
        """On the first show, fit the whole page into the window and show the snap state."""
        super().showEvent(ev)
        if not self._fitted:
            self._fitted = True
            self.fit_page()
            self._show_snap_status()

    # --------------------------------------------------------------- painting
    def paintEvent(self, ev):
        """Paint the grey surround, the white page, the grid, then the board objects.

        The painter is given the zoom/pan transform once, so everything
        after that (grid and every renderer) works in board units. Only
        objects whose bounds touch the visible area are passed to
        render.render_board, which keeps painting fast on big boards.
        """
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), OUTSIDE_COLOR)
            self._paint_page_shadow(painter)      # in screen pixels, before the transform

            # screen = board * zoom + offset
            painter.translate(self.offset)
            painter.scale(self.zoom, self.zoom)

            page = page_rect()
            visible = self.visible_board_rect()
            painter.fillRect(page, PAGE_COLOR)
            if self.show_grid:
                self._paint_grid(painter, page.intersected(visible))

            painter.setRenderHint(QPainter.Antialiasing, True)
            board = getattr(self.app, "board", None)
            if board is not None:
                objects = self._visible_objects(board.objects, visible)
                ctx = {"canvas": self, "zoom": self.zoom, "visible": visible}
                # Clip to the page: nothing of an object may show outside it.
                painter.save()
                painter.setClipRect(page)
                render.render_board(painter, objects, ctx)
                painter.restore()

            # The border goes on top of the objects so ink at the edge never hides it.
            border = QPen(PAGE_BORDER_COLOR)
            border.setWidth(BORDER_PX)
            border.setCosmetic(True)  # BORDER_PX screen pixels whatever the zoom
            painter.setPen(border)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(page)
            self._paint_tool_overlay(painter)
        finally:
            painter.end()

    def _paint_page_shadow(self, painter):
        """Draw a soft drop shadow around the page, in screen pixels (painter not transformed yet).

        Several nearly transparent rectangles of growing size are stacked;
        where they overlap the shadow is darkest (next to the page) and it
        fades to nothing outwards. The page is painted over the middle.
        """
        top_left = self.board_to_screen(QPointF(0, 0))
        base = QRectF(top_left, QSizeF(config.BOARD_W * self.zoom, config.BOARD_H * self.zoom))
        base.translate(SHADOW_SHIFT[0], SHADOW_SHIFT[1])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, SHADOW_ALPHA))
        for i in range(1, SHADOW_STEPS + 1):
            grow = i * SHADOW_GROW
            painter.drawRect(base.adjusted(-grow, -grow, grow, grow))

    def _paint_tool_overlay(self, painter):
        """Let the active tool draw local-only feedback on top of the board.

        A tool may define paint_overlay(painter, canvas); the painter is
        still in board units. Shape tools draw their rubber-band preview and
        the select tool its dashed selection box here. Nothing drawn here
        is ever sent over the network. A failing overlay is printed and
        skipped so the board itself still paints.
        """
        tool = getattr(self.app, "active_tool", None)
        fn = getattr(tool, "paint_overlay", None) if tool is not None else None
        if fn is None:
            return
        painter.save()
        try:
            fn(painter, self)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            painter.restore()

    def _paint_grid(self, painter, area):
        """Draw grid lines every grid_size units, only inside area (board units).

        When zoomed out so far that lines would be under MIN_GRID_PX apart,
        only every n-th line is drawn so the page stays readable and fast.
        """
        if area.isEmpty():
            return
        step = self.grid_size
        while step * self.zoom < MIN_GRID_PX:
            step *= 5
        pen = QPen(GRID_COLOR)
        pen.setCosmetic(True)
        painter.setPen(pen)
        lines = []
        x = math.ceil(area.left() / step) * step
        while x <= area.right():
            lines.append(QLineF(x, area.top(), x, area.bottom()))
            x += step
        y = math.ceil(area.top() / step) * step
        while y <= area.bottom():
            lines.append(QLineF(area.left(), y, area.right(), y))
            y += step
        if lines:
            painter.drawLines(lines)

    def _visible_objects(self, objects, visible):
        """Return the objects (keeping their order) whose bounds touch visible.

        Bounds are cached per obj_id and the cache entry is dropped when
        BoardModel reports that object as changed, so a long stroke's points
        are only scanned again after it really changes.
        """
        out = []
        cache = self._bounds_cache
        for obj_id, obj in objects.items():
            if obj_id in cache:
                rect = cache[obj_id]
            else:
                rect = cache[obj_id] = object_bounds(obj)
            if rect is None or rect.intersects(visible):
                out.append(obj)
        return out

    # ----------------------------------------------------------- tool control
    def is_drawing(self):
        """True between a tool's on_press and its on_release (a drag is in progress)."""
        return self._tool_pressed

    def cancel_tool(self):
        """Abort the active tool's current drawing (Escape, see shortcuts.py).

        A tool opts in by defining cancel(canvas); it then drops its
        unfinished shape, stroke or drag. The pending release is forgotten
        too, so letting go of the mouse afterwards does nothing. A tool
        without cancel() is left alone (its own press/release pair stays
        intact). Returns True if a tool was cancelled.
        """
        tool = getattr(self.app, "active_tool", None)
        fn = getattr(tool, "cancel", None) if tool is not None else None
        if fn is None:
            return False
        self._tool_pressed = False
        try:
            fn(self)
        except Exception:  # noqa: BLE001  (same rule as _call_tool)
            traceback.print_exc()
        self.update()
        return True

    # ------------------------------------------------------------ mouse input
    def _event_point(self, ev):
        """Board point of a mouse event: converted, snapped if on, one decimal, clamped to the page.

        Clamping is the LAST step, so a tool never sees a point outside the
        page: a drag that leaves the page continues along the edge.
        """
        p = self.snap(self.screen_to_board(ev.localPos()))
        return clamp_point(QPointF(round(p.x(), 1), round(p.y(), 1)))

    def _is_drawing_tool(self):
        """True when the active tool makes ink or objects (everything except the select tool)."""
        tool = getattr(self.app, "active_tool", None)
        return tool is not None and getattr(tool, "group", None) != "select"

    def _update_page_cursor(self, ev):
        """Show the "not allowed" cursor while a drawing tool is over the grey area outside the page.

        Back on the page the tool's own cursor returns. Space-panning shows
        its hand cursor everywhere, so it is left alone here.
        """
        outside = (not on_page(self.screen_to_board(ev.localPos()))
                   and self._is_drawing_tool() and not self._space_down)
        if outside:
            self.setCursor(Qt.ForbiddenCursor)
            self._forbidden = True
        elif self._forbidden:
            self._forbidden = False
            self._update_cursor()

    def _call_tool(self, method, ev):
        """Call app.active_tool.<method>(canvas, p, ev) if there is an active tool.

        An exception in a tool is printed and swallowed: PyQt5 aborts the
        whole program on an exception escaping a Qt event handler, and one
        broken tool must not close everyone's whiteboard.
        """
        tool = getattr(self.app, "active_tool", None)
        fn = getattr(tool, method, None) if tool is not None else None
        if fn is None:
            return
        try:
            fn(self, self._event_point(ev), ev)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        self.update()

    def _update_cursor(self):
        """Show the active tool's cursor (or a hand while Space-panning)."""
        if self._pan_last is not None:
            self.setCursor(Qt.ClosedHandCursor)
            return
        if self._space_down:
            self.setCursor(Qt.OpenHandCursor)
            return
        tool = getattr(self.app, "active_tool", None)
        shape = None
        if tool is not None and hasattr(tool, "cursor"):
            try:
                shape = tool.cursor()
            except Exception:  # noqa: BLE001
                shape = None
        self._cursor_tool = tool
        if isinstance(shape, QCursor):
            self.setCursor(shape)
        else:
            self.setCursor(shape if shape is not None else Qt.CrossCursor)

    def mousePressEvent(self, ev):
        """Start a pan (middle button, or Space + left) or forward the press to the tool."""
        self.setFocus(Qt.MouseFocusReason)
        btn = ev.button()
        if btn == Qt.MiddleButton or (btn == Qt.LeftButton and self._space_down):
            self._pan_last = ev.localPos()
            self._pan_button = btn
            self._update_cursor()
            return
        if btn == Qt.LeftButton or btn == Qt.RightButton:
            # A press that starts outside the page does nothing at all: the
            # tool is not told, and since _tool_pressed stays False the
            # release is not forwarded either.
            if not on_page(self.screen_to_board(ev.localPos())):
                return
            self._tool_pressed = True
            self._call_tool("on_press", ev)

    def mouseMoveEvent(self, ev):
        """Pan the view while panning, else forward to the tool; always emit cursor_moved."""
        bp = self.screen_to_board(ev.localPos())
        # Raw (unsnapped) position: remote cursors should follow the real mouse.
        self.cursor_moved.emit(round(bp.x(), 1), round(bp.y(), 1))

        if self._pan_last is not None:
            pos = ev.localPos()
            self.pan_by(pos.x() - self._pan_last.x(), pos.y() - self._pan_last.y())
            self._pan_last = pos
            return
        if getattr(self.app, "active_tool", None) is not self._cursor_tool:
            self._update_cursor()
        self._update_page_cursor(ev)
        self._call_tool("on_move", ev)

    def mouseReleaseEvent(self, ev):
        """End a pan, or forward the release to the tool that got the press."""
        if self._pan_last is not None and ev.button() == self._pan_button:
            self._pan_last = None
            self._pan_button = None
            self._update_cursor()
            return
        if self._tool_pressed and ev.button() in (Qt.LeftButton, Qt.RightButton):
            self._tool_pressed = False
            self._call_tool("on_release", ev)

    def wheelEvent(self, ev):
        """Ctrl+wheel zooms around the mouse; plain wheel scrolls, Shift+wheel scrolls sideways."""
        delta = ev.angleDelta()
        if ev.modifiers() & Qt.ControlModifier:
            notches = delta.y() / 120.0
            if notches:
                self.set_zoom(self.zoom * (ZOOM_STEP ** notches), ev.posF())
        elif ev.modifiers() & Qt.ShiftModifier:
            self.pan_by(delta.y() / 2.0, 0)
        else:
            self.pan_by(delta.x() / 2.0, delta.y() / 2.0)
        ev.accept()

    def enterEvent(self, ev):
        """Refresh the cursor when the mouse enters (the tool may have changed)."""
        self._update_cursor()
        super().enterEvent(ev)

    # --------------------------------------------------------- keyboard input
    def keyPressEvent(self, ev):
        """Holding Space turns left-drag into panning."""
        if ev.key() == Qt.Key_Space and not ev.isAutoRepeat():
            self._space_down = True
            self._update_cursor()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        """Releasing Space ends Space-panning mode."""
        if ev.key() == Qt.Key_Space and not ev.isAutoRepeat():
            self._space_down = False
            if self._pan_button == Qt.LeftButton:
                self._pan_last = None
                self._pan_button = None
            self._update_cursor()
            ev.accept()
            return
        super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        """Forget a held Space when focus leaves, so panning mode never sticks."""
        self._space_down = False
        super().focusOutEvent(ev)
