"""icons.py: Every icon of the client, painted in code with QPainter (no image files).

Why painted: the client ships as a PyInstaller one-file executable and must
work offline, so there are no icon or font files to bundle. Each icon is a
small function that draws thin, rounded line art on a 24 x 24 design grid;
the painter is scaled to the requested size, so the same drawing serves
16, 20 and 32 px buttons.

HiDPI: a pixmap is painted at size x device pixel ratio physical pixels and
tagged with setDevicePixelRatio, so icons stay sharp at 125 %, 150 % and
200 % scaling (run_client.py turns on AA_UseHighDpiPixmaps).

Caching: painting is cheap but not free, and the same icon is asked for
again on every theme change, so finished QIcons are kept in _CACHE by
(name, size, colours, ratio). Everything here runs on the GUI thread.

Public API
    NAMES                          the icon names that exist
    get_icon(name, size, color)    one-colour QIcon (unknown name: empty QIcon)
    get_state_icon(name, size, normal, checked, disabled)
                                   QIcon whose colour follows the button state
                                   (normal / checked / disabled)
    pixmap(name, size, color)      the QPixmap itself (for labels and delegates)
    write_png(name, size, color)   path of a temporary PNG (for style-sheet url())
"""

import atexit
import os
import tempfile

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QApplication

# Design grid every icon is drawn on, and the stroke width on that grid.
GRID = 24.0
STROKE = 1.8

_CACHE = {}          # key -> QIcon or QPixmap
_TEMP_FILES = []     # PNGs written for style sheets, removed at exit


# --- drawing helpers -------------------------------------------------------------

def _poly(points, close=True):
    """Return a QPainterPath through points [(x, y), ...], closed if close."""
    path = QPainterPath(QPointF(*points[0]))
    for x, y in points[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    return path


def _line(p, x1, y1, x2, y2):
    """Draw one straight line on the design grid."""
    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _dot(p, x, y, r=1.1):
    """Draw a small filled dot (the point of an 'i' or '!')."""
    p.save()
    p.setBrush(p.pen().color())
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(x, y), r, r)
    p.restore()


# --- the icons (each draws on the 24 x 24 grid with the current pen) -------------

def _pen(p):
    """Pencil: a long body with a pointed tip at the bottom left."""
    p.drawPath(_poly([(16, 5), (19, 8), (9, 18), (3.5, 20.5), (6, 15)]))
    _line(p, 14, 7, 17, 10)


def _marker(p):
    """Marker: a wide body and a short chisel nib."""
    p.drawPath(_poly([(14, 4), (20, 10), (11.5, 18.5), (5.5, 12.5)]))
    p.drawPath(_poly([(6.5, 14.5), (9.5, 17.5), (6, 21), (3, 18)]))


def _highlighter(p):
    """Highlighter: a slanted nib above a highlighted line."""
    p.drawPath(_poly([(13, 3), (19, 9), (12, 16), (6, 10)]))
    p.drawPath(_poly([(7, 12), (10, 15), (7.5, 17.5), (4.5, 14.5)]))
    p.save()
    col = QColor(p.pen().color())
    col.setAlphaF(0.45)
    p.setPen(QPen(col, 3.2, Qt.SolidLine, Qt.RoundCap))
    _line(p, 12, 20.5, 21, 20.5)
    p.restore()


def _eraser(p):
    """Eraser: a tilted block with its rubber end marked, on a base line."""
    p.drawPath(_poly([(3.5, 15), (13, 5.5), (19.5, 12), (11, 20.5), (8, 20.5)]))
    _line(p, 8.5, 10, 14.5, 16)
    _line(p, 11, 20.5, 20.5, 20.5)


def _line_tool(p):
    """Line: a diagonal with round end points."""
    _line(p, 6, 18, 18, 6)
    p.drawEllipse(QPointF(5, 19), 1.6, 1.6)
    p.drawEllipse(QPointF(19, 5), 1.6, 1.6)


def _arrow(p):
    """Arrow: a diagonal with an open head at the top right."""
    _line(p, 5, 19, 19, 5)
    p.drawPath(_poly([(10, 5), (19, 5), (19, 14)], close=False))


def _rectangle(p):
    """Rectangle with softly rounded corners."""
    p.drawRoundedRect(QRectF(3.5, 6, 17, 12), 2, 2)


def _ellipse(p):
    """Ellipse, wider than tall."""
    p.drawEllipse(QRectF(3, 6, 18, 12))


def _select(p):
    """Select: a mouse pointer."""
    p.drawPath(_poly([(5, 4), (11.5, 20), (13.8, 13.8), (20, 11.5)]))


def _text(p):
    """Text: a capital T with small serifs."""
    _line(p, 5, 5, 19, 5)
    _line(p, 5, 5, 5, 7.5)
    _line(p, 19, 5, 19, 7.5)
    _line(p, 12, 5, 12, 19)
    _line(p, 9, 19, 15, 19)


def _sticky(p):
    """Sticky note: a square with a folded bottom-right corner."""
    p.drawPath(_poly([(4, 4), (20, 4), (20, 14), (14, 20), (4, 20)]))
    p.drawPath(_poly([(14, 20), (14, 14), (20, 14)], close=False))


def _image(p):
    """Image: a frame with a sun and a hill."""
    p.drawRoundedRect(QRectF(3, 4, 18, 16), 2.5, 2.5)
    p.drawEllipse(QPointF(8.5, 9.5), 1.8, 1.8)
    p.drawPath(_poly([(20.5, 15.5), (15.5, 10.5), (6, 20)], close=False))


def _undo(p):
    """Undo: an arrow pointing left that curls back down."""
    p.drawPath(_poly([(9, 14), (4, 9), (9, 4)], close=False))
    path = QPainterPath(QPointF(4, 9))
    path.lineTo(14.5, 9)
    path.arcTo(QRectF(9, 9, 11, 11), 90, -180)   # clockwise over the right side
    path.lineTo(11, 20)
    p.drawPath(path)


def _redo(p):
    """Redo: the undo arrow mirrored left-right."""
    p.save()
    p.translate(GRID, 0)
    p.scale(-1, 1)
    _undo(p)
    p.restore()


def _magnifier(p):
    """Magnifying glass (shared by zoom in and zoom out)."""
    p.drawEllipse(QPointF(10.5, 10.5), 6.5, 6.5)
    _line(p, 15.5, 15.5, 20.5, 20.5)
    _line(p, 8, 10.5, 13, 10.5)


def _zoom_in(p):
    """Zoom in: magnifier with a plus."""
    _magnifier(p)
    _line(p, 10.5, 8, 10.5, 13)


def _zoom_out(p):
    """Zoom out: magnifier with a minus."""
    _magnifier(p)


def _grid(p):
    """Grid: a rounded square divided into 3 x 3 cells."""
    p.drawRoundedRect(QRectF(4, 4, 16, 16), 2, 2)
    for t in (9.33, 14.67):
        _line(p, t, 4, t, 20)
        _line(p, 4, t, 20, t)


def _snap(p):
    """Snap: a horseshoe magnet with marked poles."""
    path = QPainterPath(QPointF(6, 4))
    path.lineTo(6, 12)
    path.arcTo(QRectF(6, 6, 12, 12), 180, 180)    # bottom half, left to right
    path.lineTo(18, 4)
    p.drawPath(path)
    _line(p, 4, 8, 8, 8)
    _line(p, 16, 8, 20, 8)


def _copy(p):
    """Copy: two overlapping sheets."""
    p.drawRoundedRect(QRectF(8.5, 8.5, 11.5, 11.5), 2, 2)
    path = QPainterPath(QPointF(15.5, 8.5))
    path.lineTo(15.5, 6)
    path.quadTo(15.5, 4, 13.5, 4)
    path.lineTo(6, 4)
    path.quadTo(4, 4, 4, 6)
    path.lineTo(4, 13.5)
    path.quadTo(4, 15.5, 6, 15.5)
    path.lineTo(8.5, 15.5)
    p.drawPath(path)


def _plug(p):
    """Plug: two prongs, a rounded body and a cable (connected)."""
    _line(p, 9, 3, 9, 8)
    _line(p, 15, 3, 15, 8)
    path = QPainterPath(QPointF(6, 8))
    path.lineTo(18, 8)
    path.lineTo(18, 11)
    path.cubicTo(18, 14.5, 15.5, 17, 12, 17)
    path.cubicTo(8.5, 17, 6, 14.5, 6, 11)
    path.closeSubpath()
    p.drawPath(path)
    _line(p, 12, 17, 12, 21)


def _disconnect(p):
    """Disconnect: the plug crossed out."""
    _plug(p)
    _line(p, 3, 3, 21, 21)


def _moon(p):
    """Moon: a crescent (a circle minus an offset circle)."""
    outer = QPainterPath()
    outer.addEllipse(QPointF(12, 12), 8, 8)
    bite = QPainterPath()
    bite.addEllipse(QPointF(17, 7.5), 6.5, 6.5)
    p.drawPath(outer.subtracted(bite))


def _sun(p):
    """Sun: a circle with eight short rays."""
    p.drawEllipse(QPointF(12, 12), 4, 4)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0),
                   (0.707, 0.707), (-0.707, 0.707), (0.707, -0.707), (-0.707, -0.707)):
        _line(p, 12 + dx * 7, 12 + dy * 7, 12 + dx * 9.5, 12 + dy * 9.5)


def _crown(p):
    """Crown (the host): three points over a band."""
    p.drawPath(_poly([(3, 8), (7.5, 12.5), (12, 5), (16.5, 12.5), (21, 8), (19, 18), (5, 18)]))


def _kick(p):
    """Kick: an arrow leaving through a door."""
    p.drawPath(_poly([(10, 4), (6, 4), (5, 5), (5, 19), (6, 20), (10, 20)], close=False))
    _line(p, 10, 12, 20, 12)
    p.drawPath(_poly([(16, 8), (20, 12), (16, 16)], close=False))


def _rename(p):
    """Rename: a small pencil over a text line."""
    p.drawPath(_poly([(15, 4), (19, 8), (9.5, 17.5), (4.5, 19), (6, 14)]))
    _line(p, 12.5, 20.5, 20.5, 20.5)


def _trash(p):
    """Trash can: lid, handle, tapered body with two slots."""
    _line(p, 4, 7, 20, 7)
    p.drawPath(_poly([(9, 7), (9, 4.5), (15, 4.5), (15, 7)], close=False))
    p.drawPath(_poly([(6, 7), (7, 20), (17, 20), (18, 7)], close=False))
    _line(p, 10, 11, 10, 16)
    _line(p, 14, 11, 14, 16)


def _lock(p):
    """Padlock: body and shackle (a password-protected room)."""
    p.drawRoundedRect(QRectF(5, 11, 14, 10), 2, 2)
    path = QPainterPath(QPointF(8, 11))
    path.lineTo(8, 8)
    path.arcTo(QRectF(8, 4, 8, 8), 180, -180)     # over the top, left to right
    path.lineTo(16, 11)
    p.drawPath(path)


def _users(p):
    """Users: two people."""
    p.drawEllipse(QPointF(9, 8), 3.5, 3.5)
    path = QPainterPath(QPointF(3, 20))
    path.cubicTo(3, 15, 15, 15, 15, 20)
    p.drawPath(path)
    p.drawEllipse(QPointF(16.5, 8.5), 2.5, 2.5)
    path = QPainterPath(QPointF(17.5, 14))
    path.cubicTo(19.5, 14.5, 21, 16.5, 21, 19)
    p.drawPath(path)


def _chat(p):
    """Chat: a speech bubble with a tail."""
    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(3, 4, 18, 13), 3.5, 3.5)
    tail = _poly([(7, 16), (7, 21), (12, 16)])
    p.drawPath(bubble.united(tail).simplified())


def _history(p):
    """History: a clock face."""
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.drawPath(_poly([(12, 7), (12, 12), (15.5, 14)], close=False))


def _send(p):
    """Send: a paper plane."""
    p.drawPath(_poly([(3, 11), (21, 3), (13, 21), (11, 13)]))
    _line(p, 11, 13, 21, 3)


def _check(p):
    """Check mark."""
    p.drawPath(_poly([(5, 12.5), (10, 17), (19, 7)], close=False))


def _info(p):
    """Info: an 'i' in a circle."""
    p.drawEllipse(QPointF(12, 12), 9, 9)
    _line(p, 12, 11, 12, 16.5)
    _dot(p, 12, 7.8)


def _success(p):
    """Success: a check mark in a circle."""
    p.drawEllipse(QPointF(12, 12), 9, 9)
    p.drawPath(_poly([(8, 12.3), (11, 15.2), (16.2, 9.2)], close=False))


def _warning(p):
    """Warning: an exclamation mark in a triangle."""
    p.drawPath(_poly([(12, 3.5), (21.5, 20), (2.5, 20)]))
    _line(p, 12, 9.5, 12, 14)
    _dot(p, 12, 17)


def _error(p):
    """Error: a cross in a circle."""
    p.drawEllipse(QPointF(12, 12), 9, 9)
    _line(p, 9, 9, 15, 15)
    _line(p, 15, 9, 9, 15)


def _close(p):
    """Close: a plain cross."""
    _line(p, 6, 6, 18, 18)
    _line(p, 18, 6, 6, 18)


def _activity(p):
    """Activity: a heartbeat line (latency and traffic)."""
    p.drawPath(_poly([(3, 12), (7, 12), (10, 5), (14, 19), (17, 12), (21, 12)], close=False))


def _chevron_down(p):
    """Chevron pointing down (combo boxes, expanders)."""
    p.drawPath(_poly([(7, 10), (12, 15), (17, 10)], close=False))


def _chevron_up(p):
    """Chevron pointing up."""
    p.drawPath(_poly([(7, 14), (12, 9), (17, 14)], close=False))


def _align(p, offsets):
    """Four text lines; offsets gives (left, right) of each line (alignment icons)."""
    for i, (x1, x2) in enumerate(offsets):
        y = 6 + i * 4
        _line(p, x1, y, x2, y)


def _align_left(p):
    """Align left: lines flush with the left edge."""
    _align(p, ((4, 20), (4, 14), (4, 18), (4, 12)))


def _align_center(p):
    """Center: lines centred."""
    _align(p, ((4, 20), (7, 17), (5, 19), (8, 16)))


def _align_right(p):
    """Align right: lines flush with the right edge."""
    _align(p, ((4, 20), (10, 20), (6, 20), (12, 20)))


def _save(p):
    """Save: an arrow down into a tray."""
    _line(p, 12, 4, 12, 15)
    p.drawPath(_poly([(7.5, 10.5), (12, 15), (16.5, 10.5)], close=False))
    p.drawPath(_poly([(4, 15), (4, 19), (5, 20), (19, 20), (20, 19), (20, 15)], close=False))


def _panel(p, side):
    """A window frame with one side filled (left / right / bottom panel toggles)."""
    frame = QRectF(3, 4, 18, 16)
    p.drawRoundedRect(frame, 2.5, 2.5)
    p.save()
    p.setBrush(p.pen().color())
    p.setPen(Qt.NoPen)
    if side == "left":
        p.drawRoundedRect(QRectF(4.5, 5.5, 5, 13), 1, 1)
    elif side == "right":
        p.drawRoundedRect(QRectF(14.5, 5.5, 5, 13), 1, 1)
    else:
        p.drawRoundedRect(QRectF(4.5, 14, 15, 4.5), 1, 1)
    p.restore()


def _panel_left(p):
    """Left panel toggle."""
    _panel(p, "left")


def _panel_right(p):
    """Right panel toggle."""
    _panel(p, "right")


def _panel_bottom(p):
    """Bottom panel toggle."""
    _panel(p, "bottom")


def _chevron_left(p):
    """Chevron pointing left (collapse to the left)."""
    p.drawPath(_poly([(14, 7), (9, 12), (14, 17)], close=False))


def _chevron_right(p):
    """Chevron pointing right (expand to the right)."""
    p.drawPath(_poly([(10, 7), (15, 12), (10, 17)], close=False))


def _fit(p):
    """Fit to content: four corner brackets around a small square."""
    for path in (((4, 9), (4, 4), (9, 4)), ((15, 4), (20, 4), (20, 9)),
                 ((20, 15), (20, 20), (15, 20)), ((9, 20), (4, 20), (4, 15))):
        p.drawPath(_poly(list(path), close=False))
    p.drawRoundedRect(QRectF(9, 9, 6, 6), 1, 1)


_PAINTERS = {
    "panel_left": _panel_left, "panel_right": _panel_right, "panel_bottom": _panel_bottom,
    "chevron_left": _chevron_left, "chevron_right": _chevron_right, "fit": _fit,
    "align_left": _align_left, "align_center": _align_center, "align_right": _align_right,
    "save": _save,
    "pen": _pen, "marker": _marker, "highlighter": _highlighter, "eraser": _eraser,
    "line": _line_tool, "arrow": _arrow, "rectangle": _rectangle, "ellipse": _ellipse,
    "select": _select, "text": _text, "sticky": _sticky, "image": _image,
    "undo": _undo, "redo": _redo, "zoom_in": _zoom_in, "zoom_out": _zoom_out,
    "grid": _grid, "snap": _snap, "copy": _copy, "plug": _plug, "disconnect": _disconnect,
    "moon": _moon, "sun": _sun, "crown": _crown, "kick": _kick, "rename": _rename,
    "trash": _trash, "lock": _lock, "users": _users, "chat": _chat, "history": _history,
    "send": _send, "check": _check, "info": _info, "success": _success,
    "warning": _warning, "error": _error, "close": _close, "activity": _activity,
    "chevron_down": _chevron_down, "chevron_up": _chevron_up,
}

# Every icon name that get_icon understands.
NAMES = tuple(sorted(_PAINTERS))


# --- public API --------------------------------------------------------------------

def device_ratio():
    """Device pixel ratio to paint at: the highest of all screens (1.0 without a QApplication).

    Painting for the sharpest screen means an icon dragged to a second,
    denser monitor is still crisp; on a 1x screen Qt scales it down smoothly.
    """
    app = QApplication.instance()
    if app is None:
        return 1.0
    ratios = [s.devicePixelRatio() for s in app.screens()] or [1.0]
    return max(1.0, max(ratios))


def pixmap(name, size=20, color="#1F2430", ratio=None):
    """Return a QPixmap of icon `name`, `size` logical pixels square, in `color`.

    An unknown name gives a transparent pixmap (never an exception), so a
    typo can only cost an icon, never a crash.
    """
    ratio = device_ratio() if ratio is None else float(ratio)
    key = ("pix", name, int(size), QColor(color).name(QColor.HexArgb), ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    side = max(1, int(round(size * ratio)))
    pix = QPixmap(side, side)
    pix.fill(Qt.transparent)
    draw = _PAINTERS.get(name)
    if draw is not None:
        p = QPainter(pix)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.scale(side / GRID, side / GRID)
            # Thicker lines on tiny icons keep them from looking washed out.
            width = STROKE * (1.15 if size <= 16 else 1.0)
            p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            draw(p)
        finally:
            p.end()
    pix.setDevicePixelRatio(ratio)
    _CACHE[key] = pix
    return pix


def get_icon(name, size=20, color="#1F2430"):
    """Return a one-colour QIcon (cached) of icon `name` at `size` px."""
    key = ("icon", name, int(size), QColor(color).name(QColor.HexArgb), device_ratio())
    icon = _CACHE.get(key)
    if icon is None:
        icon = QIcon(pixmap(name, size, color))
        _CACHE[key] = icon
    return icon


def get_state_icon(name, size, normal, checked=None, disabled=None, active=None):
    """Return a QIcon whose colour follows the widget state (cached).

    normal   colour of a plain button
    checked  colour while the button is checked (QIcon.On), e.g. the primary colour
    disabled colour while disabled (the viewer role greys every tool)
    active   colour under the mouse (QIcon.Active), defaults to normal
    """
    checked = checked or normal
    disabled = disabled or normal
    active = active or normal
    key = ("state", name, int(size), normal, checked, disabled, active, device_ratio())
    icon = _CACHE.get(key)
    if icon is not None:
        return icon
    icon = QIcon()
    icon.addPixmap(pixmap(name, size, normal), QIcon.Normal, QIcon.Off)
    icon.addPixmap(pixmap(name, size, active), QIcon.Active, QIcon.Off)
    icon.addPixmap(pixmap(name, size, checked), QIcon.Normal, QIcon.On)
    icon.addPixmap(pixmap(name, size, checked), QIcon.Active, QIcon.On)
    icon.addPixmap(pixmap(name, size, disabled), QIcon.Disabled, QIcon.Off)
    icon.addPixmap(pixmap(name, size, disabled), QIcon.Disabled, QIcon.On)
    _CACHE[key] = icon
    return icon


def write_png(name, size, color):
    """Write icon `name` as a PNG to the temp folder once and return its path.

    Qt style sheets can only load images from files (url(...)); this lets the
    theme use painted arrows without shipping files. A '@2x' twin is written
    next to it, which Qt picks automatically on HiDPI screens. Forward
    slashes are returned because QSS url() wants them on Windows too.
    """
    key = ("png", name, int(size), QColor(color).name(QColor.HexArgb))
    path = _CACHE.get(key)
    if path is not None and os.path.exists(path):
        return path
    fd, path = tempfile.mkstemp(prefix="wb_icon_%s_" % name, suffix=".png")
    os.close(fd)
    double = path[:-4] + "@2x.png"
    pixmap(name, size, color, ratio=1.0).save(path, "PNG")
    pixmap(name, size, color, ratio=2.0).save(double, "PNG")
    _TEMP_FILES.extend([path, double])
    path = path.replace("\\", "/")
    _CACHE[key] = path
    return path


@atexit.register
def _remove_temp_files():
    """Delete the PNGs written for style sheets when the program exits."""
    for path in _TEMP_FILES:
        try:
            os.remove(path)
        except OSError:
            pass
