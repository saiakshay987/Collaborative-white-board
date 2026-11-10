# OWNER: P1
"""cursor_overlay.py: Overlay that draws other users' remote cursors and sends our own CURSOR (max 20 per second).

Feature #7, live cursors with usernames (contract C1: CURSOR {x, y} in
board units, max 20 per second per client, not logged, no seq).

Two directions:

1. Sending (CursorSender). CanvasWidget emits cursor_moved(x, y) in board
   units on EVERY mouse move, which can be 100+ times per second on a
   gaming mouse. Sending each one would waste bandwidth on points nobody
   can see anyway, so we throttle to SEND_HZ (20/s):
     * a move at least MIN_INTERVAL_MS after the last send goes out at once,
     * a move sooner than that only overwrites `pending` (intermediate
       points are DROPPED) and arms a one-shot timer for the rest of the
       interval; when it fires, the LATEST point is sent. So after the
       mouse stops, the final resting position always arrives.
   net.send() only queues the frame for the sender thread, so this never
   blocks the GUI thread.

2. Receiving (CursorOverlay). The server relays every other user's CURSOR
   with the envelope's user field set to the sender's authenticated name.
   The @on_message handler (GUI thread) stores it as the cursor's TARGET.
   A ~60 fps animation timer moves the drawn position a fraction of the
   way toward the target each frame (exponential smoothing), so a cursor
   that is only updated 20 times a second still glides instead of jumping.
   Positions are kept in BOARD units and converted with
   canvas.board_to_screen() at paint time, so each viewer's own zoom and
   pan are always respected. A cursor disappears after STALE_S seconds
   without updates (fading out over the last FADE_S), or at once when its
   user leaves (USER_LEFT, or offline/missing in USER_LIST).

The overlay is a transparent child widget stacked on top of app.canvas,
resized with it through an event filter. It ignores the mouse
(WA_TransparentForMouseEvents), so every click, drag and wheel event still
reaches the canvas and the active tool. Whenever the canvas repaints (zoom,
pan, board change), Qt also repaints this transparent child over it, so the
cursors follow the view without the canvas knowing about us.

run_client.py calls install(app) once the main window (and so app.canvas)
exists.
"""

import math
import time

from PyQt5.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

from client.registry import on_message
from common import protocol

# Contract C1: at most 20 CURSOR frames per second per client.
SEND_HZ = 20
MIN_INTERVAL_MS = 1000 // SEND_HZ  # 50 ms between two sends

# Animation frame interval (ms): ~60 fps while a cursor is visible.
FRAME_MS = 16

# Smoothing time constant (s): each frame the drawn position covers
# 1 - exp(-dt / SMOOTH_TAU_S) of the remaining distance. 0.06 s means it is
# ~95 % of the way there after 3 tau = 180 ms, a bit less than 4 updates.
SMOOTH_TAU_S = 0.06

# Closer than this (board units) to the target counts as arrived.
SNAP_DIST = 0.3

# Hide a cursor after STALE_S seconds without a CURSOR; it fades out over
# the last FADE_S seconds so it does not vanish abruptly.
STALE_S = 3.0
FADE_S = 0.5

# Colour used when a user's colour is not known (yet).
DEFAULT_COLOR = "#555555"

# The one overlay of this client process (set by install()); the message
# handlers below hand data to it. None until the main window exists.
_overlay = None


def _arrow_path():
    """Return the pointer arrow as a QPainterPath, tip at (0, 0), in screen pixels.

    Built once; it is the same size whatever the zoom, like a real mouse
    pointer.
    """
    path = QPainterPath()
    pts = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10.5, 18.8), (7.5, 12), (13, 12)]
    path.moveTo(*pts[0])
    for p in pts[1:]:
        path.lineTo(*p)
    path.closeSubpath()
    return path


class RemoteCursor:
    """One other user's cursor: where it is drawn, where it is going, when last seen.

    pos and target are QPointF in board units; last_seen is time.time() of
    the last CURSOR received for this user.
    """

    def __init__(self, name, x, y, now):
        """Start exactly at the first reported point (no glide from (0, 0))."""
        self.name = name
        self.pos = QPointF(x, y)
        self.target = QPointF(x, y)
        self.last_seen = now

    def step(self, dt):
        """Move pos toward target for a frame of dt seconds; return True if it moved."""
        dx = self.target.x() - self.pos.x()
        dy = self.target.y() - self.pos.y()
        if abs(dx) < SNAP_DIST and abs(dy) < SNAP_DIST:
            if dx or dy:
                self.pos = QPointF(self.target)
                return True
            return False
        k = 1.0 - math.exp(-dt / SMOOTH_TAU_S)
        self.pos = QPointF(self.pos.x() + dx * k, self.pos.y() + dy * k)
        return True

    def opacity(self, now):
        """Return 1.0 while fresh, falling to 0.0 over the last FADE_S before STALE_S."""
        age = now - self.last_seen
        if age <= STALE_S - FADE_S:
            return 1.0
        return max(0.0, (STALE_S - age) / FADE_S)


class CursorSender(QObject):
    """Throttles canvas.cursor_moved to at most SEND_HZ CURSOR frames per second.

    See the module docstring, part 1. Sending is paused while we are not in
    a room (before JOIN_OK, or while reconnecting): the server would answer
    NO_ROOM, and the position would be stale by the time we are back.
    """

    def __init__(self, app, parent=None):
        """Create the sender for AppState app; nothing is sent until joined()."""
        super().__init__(parent)
        self.app = app
        self.in_room = True          # install() runs after JOIN_OK
        self.pending = None          # newest (x, y) not sent yet
        self.last_sent = None        # last (x, y) sent, to skip duplicates
        self.last_send_t = 0.0       # time.monotonic() of the last send
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        # A coarse Qt timer may fire up to 5 % early (47.5 ms instead of
        # 50 ms), which would break the 20/s limit; a precise one does not.
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.flush)

    def on_moved(self, x, y):
        """Slot for canvas.cursor_moved(x, y): send now or keep only the latest point."""
        self.pending = (round(x, 1), round(y, 1))
        if self.timer.isActive():
            return                   # a send is already scheduled; it takes the newest point
        wait_ms = MIN_INTERVAL_MS - (time.monotonic() - self.last_send_t) * 1000.0
        if wait_ms <= 0:
            self.flush()
        else:
            self.timer.start(int(math.ceil(wait_ms)))

    def flush(self):
        """Send the pending point as CURSOR {x, y} (net.send only queues it)."""
        point, self.pending = self.pending, None
        if point is None or point == self.last_sent or not self.in_room:
            return
        net = self.app.net
        if net is not None and net.send(protocol.CURSOR, {"x": point[0], "y": point[1]}):
            self.last_sent = point
            self.last_send_t = time.monotonic()

    def set_in_room(self, in_room):
        """Pause (False) or resume (True) sending; resuming forgets old state."""
        self.in_room = in_room
        self.pending = None
        self.last_sent = None
        self.timer.stop()


class CursorOverlay(QWidget):
    """Transparent, mouse-transparent layer over the canvas that paints remote cursors."""

    def __init__(self, app, canvas):
        """Create the overlay as a child of canvas and start following its size."""
        super().__init__(canvas)
        self.app = app
        self.canvas = canvas
        self.cursors = {}            # user name -> RemoteCursor
        self.colors = {}             # user name -> colour string, from app.users
        self.arrow = _arrow_path()
        self.label_font = QFont(self.font())
        self.label_font.setPointSizeF(max(8.0, self.label_font.pointSizeF() - 1))
        self.label_font.setBold(True)

        # Purely visual: never take the mouse or keyboard from the canvas.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)

        self.anim = QTimer(self)
        self.anim.setInterval(FRAME_MS)
        self.anim.timeout.connect(self._tick)
        self._last_tick = time.monotonic()

        canvas.installEventFilter(self)
        self._fit()
        self.show()
        app.users_changed.connect(self._on_users_changed)
        self._on_users_changed()

    # -- geometry ------------------------------------------------------------

    def _fit(self):
        """Cover the whole canvas and stay above its other children."""
        self.setGeometry(0, 0, self.canvas.width(), self.canvas.height())
        self.raise_()

    def eventFilter(self, obj, event):
        """Follow the canvas size; never swallow the event."""
        if obj is self.canvas and event.type() == QEvent.Resize:
            self._fit()
        return False

    # -- data from the network (GUI thread, via on_message) ------------------

    def cursor_received(self, name, x, y):
        """Another user's CURSOR arrived: set its target and start animating."""
        if not name or name == self.app.me.username:
            return
        now = time.time()
        cur = self.cursors.get(name)
        if cur is None:
            self.cursors[name] = RemoteCursor(name, x, y, now)
            self.update()
        else:
            cur.target = QPointF(x, y)
            cur.last_seen = now
        if not self.anim.isActive():
            self._last_tick = time.monotonic()
            self.anim.start()

    def remove(self, name):
        """Hide name's cursor at once (the user left)."""
        if self.cursors.pop(name, None) is not None:
            self.update()

    def clear(self):
        """Hide every cursor (new room or reconnect)."""
        self.cursors.clear()
        self.update()

    def _on_users_changed(self):
        """app.users changed: refresh colours and drop users who are gone or offline."""
        online = {}
        for u in self.app.users:
            if u.get("online", True):
                online[u.get("name")] = u.get("color") or DEFAULT_COLOR
        self.colors = online
        for name in [n for n in self.cursors if n not in online]:
            del self.cursors[name]
        self.update()

    # -- animation -----------------------------------------------------------

    def _tick(self):
        """One animation frame: glide toward targets, expire stale cursors, repaint."""
        mono = time.monotonic()
        dt, self._last_tick = mono - self._last_tick, mono
        now = time.time()
        dirty = False
        for name in list(self.cursors):
            cur = self.cursors[name]
            age = now - cur.last_seen
            if age >= STALE_S:
                del self.cursors[name]
                dirty = True
                continue
            if cur.step(dt) or age > STALE_S - FADE_S:
                dirty = True
        if dirty:
            self.update()
        if not self.cursors:
            self.anim.stop()         # idle: no timer runs while nobody moves

    # -- painting ------------------------------------------------------------

    def paintEvent(self, ev):
        """Paint each cursor: arrow in the user's colour plus a name label.

        Board -> screen conversion happens here, every paint, with the
        canvas's CURRENT zoom and offset, so cursors stay on the right spot
        while this user zooms or pans. Arrow and label keep a constant
        on-screen size.
        """
        if not self.cursors:
            return
        now = time.time()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(self.label_font)
        fm = QFontMetrics(self.label_font)
        area = QRectF(self.rect()).adjusted(-30, -30, 30, 30)
        for cur in self.cursors.values():
            p = self.canvas.board_to_screen(cur.pos)
            if not area.contains(p):
                continue             # scrolled out of view
            alpha = cur.opacity(now)
            if alpha <= 0.0:
                continue
            color = QColor(self.colors.get(cur.name, DEFAULT_COLOR))
            painter.save()
            painter.setOpacity(alpha)
            painter.translate(p)
            # Arrow: user colour, white outline so it shows on any background.
            painter.setPen(QPen(Qt.white, 1.5))
            painter.setBrush(color)
            painter.drawPath(self.arrow)
            # Name label: rounded box in the user colour below-right of the tip.
            text = cur.name
            box = QRectF(12, 18, fm.horizontalAdvance(text) + 10, fm.height() + 4)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(box, 4, 4)
            painter.setPen(_text_color(color))
            painter.drawText(box, Qt.AlignCenter, text)
            painter.restore()
        painter.end()


def _text_color(bg):
    """Return black or white, whichever reads better on background colour bg."""
    luminance = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return QColor("#000000") if luminance > 150 else QColor("#ffffff")


# --- Install -----------------------------------------------------------------

def install(app):
    """Create the overlay over app.canvas and connect the throttled sender.

    Called once by run_client.py after the main window is built. Returns
    the overlay, or None if there is no canvas.
    """
    global _overlay
    canvas = app.canvas
    if canvas is None or _overlay is not None:
        return _overlay
    _overlay = CursorOverlay(app, canvas)
    sender = CursorSender(app, _overlay)
    _overlay.sender = sender
    canvas.cursor_moved.connect(sender.on_moved)
    if app.net is not None:
        # While reconnecting, the server has not seated us yet: pause until
        # the next JOIN_OK (handled below), and drop cursors that are now stale.
        app.net.reconnecting.connect(lambda: sender.set_in_room(False))
        app.net.disconnected.connect(lambda _reason: sender.set_in_room(False))
        app.net.reconnecting.connect(_overlay.clear)
    return _overlay


# --- Message handlers (GUI thread) --------------------------------------------

@on_message(protocol.CURSOR)
def _on_cursor(app, msg):
    """CURSOR {x, y} relayed by the server; user is the (server-set) sender name."""
    if _overlay is None:
        return
    p = msg.get("payload") or {}
    x, y = p.get("x"), p.get("y")
    if isinstance(x, bool) or isinstance(y, bool) \
            or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return
    _overlay.cursor_received(msg.get("user"), float(x), float(y))


@on_message(protocol.USER_LEFT)
def _on_user_left(app, msg):
    """USER_LEFT {name, ...}: hide that user's cursor immediately."""
    if _overlay is not None:
        _overlay.remove((msg.get("payload") or {}).get("name"))


@on_message(protocol.JOIN_OK)
def _on_join_ok(app, msg):
    """JOIN_OK (first join or after a reconnect): forget old cursors, resume sending."""
    if _overlay is not None:
        _overlay.clear()
        _overlay.sender.set_in_room(True)
