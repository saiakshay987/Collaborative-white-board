# OWNER: P1
"""stats_panel.py: Dock panel showing live RTT per user and the traffic counters.

Two data sources, both read on the GUI thread only:

1. STATS from the server (contract C1, every 2 s):
       {rtt: {name: ms}, server: {clients, msgs_in, msgs_out, bytes_in, bytes_out}}
   The server measures each client's round-trip time from PING/PONG and
   averages the last 5 samples (server/presence.py). The @on_message
   handler below stores the payload in `model` (a StatsModel) and emits
   model.updated, which this panel and users_panel listen to.

2. framing.STATS of THIS client process (contract A): every frame the
   client sends or receives is counted there by framing.send_msg/recv_msg
   (on the network threads, under a lock). Once a second the panel takes a
   stats_snapshot() and divides the difference to the previous one by the
   elapsed time, which gives messages per second and KB/s in and out.

The last 60 one-second samples feed a small line graph (my RTT and
messages per second) painted with QPainter. RTT is colored green below
20 ms, amber below 100 ms and red above. "Copy stats" puts a one-line
summary on the clipboard for the report (e.g. P2's batching comparison:
draw with draw_sync.BATCHING True and False and compare "out msg/s").
"""

import html
import time
from collections import deque

from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from client import draw_sync
from client.registry import on_message, register_panel
from common import framing, protocol

# The panel samples framing.STATS this often (ms); rates are per second.
SAMPLE_MS = 1000

# The graph shows this many samples (one per SAMPLE_MS, so 60 s).
HISTORY = 60

# RTT color thresholds (ms): below GREEN_MS green, below AMBER_MS amber, else red.
GREEN_MS = 20
AMBER_MS = 100
GREEN = QColor("#2e7d32")
AMBER = QColor("#e69500")
RED = QColor("#c62828")
GREY = QColor("#9e9e9e")

# Series color for messages per second in the graph.
MSG_COLOR = QColor("#4363d8")

KB = 1024.0


def rtt_color(ms):
    """Return the QColor for an RTT in ms (grey when unknown)."""
    if ms is None:
        return GREY
    if ms < GREEN_MS:
        return GREEN
    if ms < AMBER_MS:
        return AMBER
    return RED


def fmt_rtt(ms):
    """Format an RTT for display: '3.2 ms', or '-' when not measured yet."""
    return "-" if ms is None else "%.1f ms" % ms


def _number(value):
    """Return value if it is a real number (not bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


# --- Latest STATS from the server ---------------------------------------------

class StatsModel(QObject):
    """Holds the latest STATS payload and announces changes (GUI thread only).

    rtt: {name: ms}; server: {clients, msgs_in, msgs_out, bytes_in,
    bytes_out}; received: time.monotonic() of the last STATS, or None.
    """

    updated = pyqtSignal()

    def __init__(self, parent=None):
        """Start empty: nothing measured yet."""
        super().__init__(parent)
        self.rtt = {}
        self.server = {}
        self.received = None

    def rtt_of(self, name):
        """Return the averaged RTT of user name in ms, or None."""
        return self.rtt.get(name)

    def apply(self, payload):
        """Store a STATS payload, keeping only well-formed numbers, then emit updated."""
        rtt = payload.get("rtt") if isinstance(payload.get("rtt"), dict) else {}
        server = payload.get("server") if isinstance(payload.get("server"), dict) else {}
        self.rtt = {str(k): float(v) for k, v in rtt.items() if _number(v) is not None}
        self.server = {k: v for k, v in server.items() if _number(v) is not None}
        self.received = time.monotonic()
        self.updated.emit()


# The one model of the client process. A plain QObject (not a widget), so it
# may be created at import time, before the QApplication exists.
model = StatsModel()


@on_message(protocol.STATS)
def _on_stats(app, msg):
    """STATS {rtt, server}: store it in model (runs on the GUI thread)."""
    model.apply(msg.get("payload") or {})


# --- Live graph ---------------------------------------------------------------

class LiveGraph(QWidget):
    """60-second line graph of my RTT (colored by threshold) and messages/s.

    Each series has its own vertical scale (shown as "max" labels), because
    ms and msg/s are different units. samples is a sequence of
    (rtt_ms or None, msgs_per_s) tuples, oldest first.
    """

    def __init__(self, samples, parent=None):
        """Draw from the shared samples deque (owned by StatsPanel)."""
        super().__init__(parent)
        self.samples = samples
        self.setMinimumSize(240, 90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def _x(self, plot, i):
        """x pixel of sample i; the newest sample is always at the right edge."""
        slot = HISTORY - len(self.samples) + i
        return plot.left() + plot.width() * slot / (HISTORY - 1)

    @staticmethod
    def _y(plot, value, top):
        """y pixel of value on a scale from 0 (bottom) to top (top edge)."""
        return plot.bottom() - plot.height() * min(value, top) / top

    def paintEvent(self, _event):
        """Paint the frame, grid, both series and the scale labels."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self.palette().base())
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        p.setFont(font)
        plot = QRectF(self.rect()).adjusted(6, 16, -6, -14)

        # Frame and three horizontal grid lines.
        p.setPen(QPen(GREY, 1, Qt.DotLine))
        for k in range(1, 4):
            y = plot.top() + plot.height() * k / 4
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        p.setPen(QPen(GREY, 1))
        p.drawRect(plot)

        rtts = [s[0] for s in self.samples if s[0] is not None]
        rates = [s[1] for s in self.samples]
        rtt_top = max([GREEN_MS] + rtts) * 1.2
        rate_top = max([1.0] + rates) * 1.2

        # Messages per second (in + out): one blue path.
        if len(self.samples) >= 2:
            path = QPainterPath()
            for i, (_rtt, rate) in enumerate(self.samples):
                pt = QPointF(self._x(plot, i), self._y(plot, rate, rate_top))
                if i:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
            p.setPen(QPen(MSG_COLOR, 1.5))
            p.drawPath(path)

        # My RTT: each segment colored by the threshold of its newer point,
        # so a latency spike shows up amber or red.
        prev = None
        for i, (rtt, _rate) in enumerate(self.samples):
            if rtt is None:
                prev = None
                continue
            pt = QPointF(self._x(plot, i), self._y(plot, rtt, rtt_top))
            if prev is not None:
                p.setPen(QPen(rtt_color(rtt), 2))
                p.drawLine(prev, pt)
            prev = pt

        # Labels: scales at the top, time axis at the bottom.
        p.setPen(self.palette().text().color())
        p.drawText(QRectF(plot.left(), 0, plot.width(), 15), Qt.AlignLeft | Qt.AlignVCenter,
                   "RTT (max %.0f ms)" % rtt_top)
        p.setPen(MSG_COLOR)
        p.drawText(QRectF(plot.left(), 0, plot.width(), 15), Qt.AlignRight | Qt.AlignVCenter,
                   "msg/s in+out (max %.0f)" % rate_top)
        p.setPen(GREY)
        bottom = QRectF(plot.left(), plot.bottom() + 1, plot.width(), 13)
        p.drawText(bottom, Qt.AlignLeft | Qt.AlignVCenter, "-%d s" % HISTORY)
        p.drawText(bottom, Qt.AlignRight | Qt.AlignVCenter, "now")
        p.end()


# --- The panel ----------------------------------------------------------------

class StatsPanel(QWidget):
    """Bottom dock: my RTT, everyone's RTT, client traffic rates, graph, copy button."""

    def __init__(self, app, parent=None):
        """Build the widgets and start the 1 s sampling timer."""
        super().__init__(parent)
        self.app = app
        # Rates of the last sample, per second: msgs_in, msgs_out, bytes_in, bytes_out.
        self.rates = dict.fromkeys(framing.STATS, 0.0)
        # (my_rtt_ms or None, msgs_in+out per s) per second, newest last.
        self.samples = deque(maxlen=HISTORY)
        self._last = framing.stats_snapshot()
        self._last_t = time.monotonic()

        self.my_rtt = QLabel("-")
        big = QFont(self.my_rtt.font())
        big.setPointSizeF(big.pointSizeF() * 1.6)
        big.setBold(True)
        self.my_rtt.setFont(big)
        self.traffic_in = QLabel()
        self.traffic_out = QLabel()
        self.server_line = QLabel()
        self.server_line.setWordWrap(True)
        self.users_rtt = QLabel()
        self.users_rtt.setTextFormat(Qt.RichText)
        self.users_rtt.setWordWrap(True)
        self.users_rtt.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.copy_btn = QPushButton("Copy stats")
        self.copy_btn.setToolTip("Copy a one-line summary to the clipboard (for the report)")
        self.copy_btn.clicked.connect(self.copy_stats)
        self.graph = LiveGraph(self.samples)

        numbers = QGridLayout()
        numbers.addWidget(QLabel("My RTT"), 0, 0)
        numbers.addWidget(self.my_rtt, 0, 1)
        numbers.addWidget(QLabel("In"), 1, 0)
        numbers.addWidget(self.traffic_in, 1, 1)
        numbers.addWidget(QLabel("Out"), 2, 0)
        numbers.addWidget(self.traffic_out, 2, 1)
        numbers.addWidget(self.copy_btn, 3, 0, 1, 2)
        left = QVBoxLayout()
        left.addLayout(numbers)
        left.addWidget(self.server_line)
        left.addStretch(1)

        users_box = QVBoxLayout()
        users_box.addWidget(QLabel("<b>RTT per user</b>"))
        users_box.addWidget(self.users_rtt, 1)

        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addLayout(left)
        layout.addWidget(line)
        layout.addLayout(users_box, 1)
        layout.addWidget(self.graph, 2)

        model.updated.connect(self.refresh_rtt)
        app.users_changed.connect(self.refresh_rtt)
        # A QTimer runs in the GUI event loop: sampling never blocks the GUI
        # and needs no locking beyond framing's own stats lock.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sample)
        self._timer.start(SAMPLE_MS)
        self.refresh_rtt()
        self._show_rates()

    # -- sampling ------------------------------------------------------------

    def my_rtt_ms(self):
        """My averaged RTT from the latest STATS, or None."""
        return model.rtt_of(self.app.me.username)

    def sample(self):
        """Timer slot: turn the framing.STATS delta since the last call into rates.

        Uses time.monotonic() so a change of the wall clock cannot produce
        negative or huge rates; dt is measured, not assumed to be 1 s.
        """
        now = time.monotonic()
        snap = framing.stats_snapshot()
        dt = max(now - self._last_t, 1e-3)
        self.rates = {k: max(0, snap[k] - self._last[k]) / dt for k in snap}
        self._last, self._last_t = snap, now
        self.samples.append((self.my_rtt_ms(), self.rates["msgs_in"] + self.rates["msgs_out"]))
        self._show_rates()
        self.graph.update()

    def _show_rates(self):
        """Show the current rates and the server counters."""
        r = self.rates
        self.traffic_in.setText("%.1f msg/s   %.2f KB/s" % (r["msgs_in"], r["bytes_in"] / KB))
        self.traffic_out.setText("%.1f msg/s   %.2f KB/s" % (r["msgs_out"], r["bytes_out"] / KB))
        s = model.server
        if s:
            self.server_line.setText(
                "Server: %d clients, %d msgs in / %d out, %.1f KB in / %.1f KB out"
                % (s.get("clients", 0), s.get("msgs_in", 0), s.get("msgs_out", 0),
                   s.get("bytes_in", 0) / KB, s.get("bytes_out", 0) / KB))
        else:
            self.server_line.setText("Server: waiting for STATS...")

    def refresh_rtt(self):
        """Update my RTT and the colored per-user RTT list from model."""
        mine = self.my_rtt_ms()
        self.my_rtt.setText(fmt_rtt(mine))
        self.my_rtt.setStyleSheet("color: %s;" % rtt_color(mine).name())
        rows = []
        for u in self.app.users:
            if not u.get("online"):
                continue
            name = u.get("name", "?")
            ms = model.rtt_of(name)
            rows.append('<span style="color:%s">&#9679;</span> %s: <b style="color:%s">%s</b>'
                        % (html.escape(u.get("color") or "#000000"), html.escape(name),
                           rtt_color(ms).name(), fmt_rtt(ms)))
        self.users_rtt.setText("<br>".join(rows) or "<i>nobody online</i>")
        self._show_rates()

    # -- copy ----------------------------------------------------------------

    def summary(self):
        """Return the one-line summary that 'Copy stats' puts on the clipboard."""
        r = self.rates
        n = len(self.samples) or 1
        avg_rate = sum(s[1] for s in self.samples) / n
        others = " ".join("%s=%s" % (name, fmt_rtt(ms).replace(" ", ""))
                          for name, ms in sorted(model.rtt.items()))
        s = model.server
        t = draw_sync.TOTALS
        return ("[stats] room %s user %s | my RTT %s | RTT %s | in %.1f msg/s %.2f KB/s"
                " | out %.1f msg/s %.2f KB/s | avg %.1f msg/s over %d s"
                " | batching %s, %d strokes, %d stroke parts"
                " | server %d clients, %d msgs in, %d msgs out"
                % (self.app.me.room, self.app.me.username, fmt_rtt(self.my_rtt_ms()),
                   others or "-", r["msgs_in"], r["bytes_in"] / KB,
                   r["msgs_out"], r["bytes_out"] / KB, avg_rate, len(self.samples),
                   "on" if draw_sync.BATCHING else "off", t["strokes"], t["packets"],
                   s.get("clients", 0), s.get("msgs_in", 0), s.get("msgs_out", 0)))

    def copy_stats(self):
        """Button slot: copy summary() to the clipboard and confirm with a toast."""
        QApplication.clipboard().setText(self.summary())
        self.app.toast("Stats copied to the clipboard")


register_panel("stats", StatsPanel, area="bottom", title="Latency and traffic")
