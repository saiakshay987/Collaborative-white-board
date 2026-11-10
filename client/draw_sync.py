# OWNER: P2
"""draw_sync.py: Batches stroke points and flushes OP_STROKE_PART messages every 40 ms (at most 50 points per part).

Contract C2 (OP_STROKE_PART) and E3 (optimistic apply).

Why batch?
----------
A mouse reports about 100-1000 moves per second. Sending one TCP frame per
move would cost a 4-byte header + a JSON envelope (~150 bytes) for a single
[x, y] pair, and the server would have to take room.lock, give out a seq and
broadcast to every client for each one. Instead StrokeStreamer collects the
points and sends them in parts:

    press    -> OP_STROKE_PART {obj_id, points:[first], done:false, style}
                (the first part creates the stroke on every client)
    drag     -> points go into a buffer; a point closer than MIN_DIST (1.5
                board units) to the last kept point is dropped
    flush    -> every FLUSH_MS (40 ms) by a QTimer, or at once when the
                buffer holds MAX_PART_POINTS (50) points:
                OP_STROKE_PART {obj_id, points:[...], done:false}
    release  -> the rest of the buffer with done:true

40 ms means at most 25 parts per second per drawing user, which still looks
live to the others (a film is 24 frames per second).

Set BATCHING = False to send one part per mouse move instead (debug/demo
mode, to compare packet counts in the stats panel or on the console).

Optimistic apply and the echo
-----------------------------
Every part goes through client/ops.send_op, which applies it to BoardModel
at once (so my own stroke appears instantly, without waiting for the network
round trip) and remembers its op_id as pending. When the server echoes the
part back (echo rule, contract C2) BoardModel sees the pending op_id and only
confirms it, so nothing is drawn twice.

Threading: everything here runs on the GUI thread. The QTimer fires in the
Qt event loop and NetClient.send only queues the frame for its socket
thread, so the GUI never waits on the network.
"""

import math

from PyQt5.QtCore import QObject, QTimer

from client import ops
from common import kinds_draw, protocol

# --- Tuning (contract C2: at most 50 points per part, flushed every 40 ms) -----

FLUSH_MS = 40            # send the buffered points this often while drawing
MAX_PART_POINTS = 50     # flush early when the buffer reaches this many points
MIN_DIST = 1.5           # board units; closer points add nothing visible

# Measurement flag. True: batch as described above. False: no flush timer;
# every kept mouse point is sent at once as its own OP_STROKE_PART, to show
# how many more packets that costs (compare "Out msg/s" in the stats panel).
BATCHING = True

# Totals for this client process, for comparing BATCHING on/off.
TOTALS = {"strokes": 0, "packets": 0, "points": 0, "dropped": 0}


def _dist(a, b):
    """Euclidean distance between two [x, y] points."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _to_xy(p):
    """Turn a QPointF (or (x, y)) into a JSON-friendly [x, y] with one decimal (contract D)."""
    if hasattr(p, "x"):
        x, y = p.x(), p.y()
    else:
        x, y = p[0], p[1]
    return [round(float(x), 1), round(float(y), 1)]


class StrokeStreamer(QObject):
    """Streams one freehand stroke at a time to the server as OP_STROKE_PART messages.

    Usage by a tool:
        streamer.begin(p)   on mouse press  (sends the first part with the style)
        streamer.add(p)     on mouse move   (buffers; flushes by timer or size)
        streamer.end(p)     on mouse release (flushes the rest with done:true)

    Counters (per stroke, reset in begin): packets, points, dropped.
    """

    def __init__(self, app, parent=None):
        """Create an idle streamer for AppState app with its 40 ms flush timer."""
        super().__init__(parent)
        self.app = app
        self.obj_id = None       # stroke being drawn, None when idle
        self._buffer = []        # points not sent yet
        self._last = None        # last point KEPT (sent or buffered), for MIN_DIST
        self._total_points = 0   # points of this stroke so far (limit: kinds_draw.MAX_POINTS)
        self._style = None
        self.packets = 0
        self.points = 0
        self.dropped = 0
        # A repeating timer in the GUI event loop: it never blocks, it just
        # calls flush() every FLUSH_MS while a stroke is active.
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_MS)
        self._timer.timeout.connect(self.flush)

    @property
    def active(self):
        """True while a stroke is being drawn (between begin and end)."""
        return self.obj_id is not None

    # ---------------------------------------------------------------- stroke
    def begin(self, p, style=None):
        """Start a new stroke at board point p and send its first part.

        The obj_id is made with protocol.new_op_id, so it is "<user>-<n>"
        and unique (contract D). style defaults to a copy of app.pen; only
        the four contract fields are sent.
        """
        if self.active:
            self.end()           # a stroke left open (e.g. tool switched mid-drag)
        pen = style if style is not None else (self.app.pen or {})
        self._style = {"tool": pen.get("tool", "pen"), "color": pen.get("color", "#000000"),
                       "width": pen.get("width", 3), "alpha": pen.get("alpha", 1.0)}
        self.packets = self.points = self.dropped = 0
        self._start(_to_xy(p))

    def _start(self, first):
        """Create the stroke object: first OP_STROKE_PART carries the style."""
        self.obj_id = protocol.new_op_id(self.app.me.username or "me")
        self._buffer = []
        self._last = first
        self._total_points = 1
        self._send([first], done=False, style=self._style)
        if BATCHING:
            self._timer.start()

    def add(self, p):
        """Add a dragged point. Drops it if closer than MIN_DIST to the last kept point.

        With BATCHING the point waits in the buffer until the timer fires or
        the buffer is full; without BATCHING it is sent at once.
        """
        if not self.active:
            return
        pt = _to_xy(p)
        if _dist(pt, self._last) < MIN_DIST:
            self.dropped += 1
            TOTALS["dropped"] += 1
            return
        if self._total_points >= kinds_draw.MAX_POINTS:
            # Contract D: at most 20000 points per stroke. Close this stroke
            # and continue seamlessly with a new one from the last point.
            last = self._last
            self.flush(done=True)
            self._start(last)
        self._buffer.append(pt)
        self._last = pt
        self._total_points += 1
        if not BATCHING or len(self._buffer) >= MAX_PART_POINTS:
            self.flush()

    def flush(self, done=False):
        """Send the buffered points as one OP_STROKE_PART (nothing to do if empty and not done).

        Called by the 40 ms timer, when the buffer is full, and by end().
        If the stroke was erased by someone else while I am still drawing
        it, it is gone from my board: stop streaming, because the server
        would answer every further part with NOT_FOUND.
        """
        if not self.active:
            return
        if not self._buffer and not done:
            return
        board = self.app.board
        if board is not None and self.obj_id not in board.objects:
            self._stop()
            return
        points, self._buffer = self._buffer, []
        self._send(points, done=done)

    def end(self, p=None):
        """Finish the stroke: add the release point, then flush the rest with done:true."""
        if not self.active:
            return
        if p is not None:
            self.add(p)
        self.flush(done=True)
        if self.active:
            print("[draw_sync] stroke %s: %d packets, %d points, %d dropped (batching %s)"
                  % (self.obj_id, self.packets, self.points, self.dropped,
                     "on" if BATCHING else "off"))
            TOTALS["strokes"] += 1
        self._stop()

    def _stop(self):
        """Go idle: stop the timer and forget the stroke."""
        self._timer.stop()
        self.obj_id = None
        self._buffer = []
        self._last = None

    # --------------------------------------------------------------- sending
    def _send(self, points, done, style=None):
        """Send one OP_STROKE_PART through ops.send_op (optimistic local apply + network send).

        Payload (contract C2): {obj_id, points, done, style?}. style only on
        the first part. The op_id of the message is made by send_op; the
        obj_id is ours and stays the same for every part of this stroke.
        """
        payload = {"obj_id": self.obj_id, "points": points, "done": bool(done)}
        if style is not None:
            payload["style"] = dict(style)
        ops.send_op(self.app, protocol.OP_STROKE_PART, payload)
        self.packets += 1
        self.points += len(points)
        TOTALS["packets"] += 1
        TOTALS["points"] += len(points)
