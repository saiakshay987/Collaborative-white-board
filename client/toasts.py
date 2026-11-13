# OWNER: P1
"""toasts.py: Transient on-screen notifications behind app.toast(text), plus join/leave sounds.

Toasts
    app.toast(text) (client/app_state.py) calls show_toast(window, text).
    A toast is a rounded card with an icon in the bottom-right corner of
    the canvas. Its accent colour and icon (info, success, warning, error)
    are guessed from the wording by toast_kind(); that only changes the look,
    never the text. It fades and slides in, stays, and fades out; its whole
    life is SHOW_MS (3 s). At most MAX_TOASTS (3) are on screen: the newest
    is at the bottom, older ones move up, and a 4th pushes the oldest out at
    once. Toasts let every click through to what is underneath, so they
    never block drawing, chatting or typing.

    The toasts are child widgets of the main window (they float over the
    canvas and docks), managed by one ToastManager per window. It is found
    with window.findChild, so no other module has to store it anywhere.

Who toasts what
    USER_JOINED / USER_LEFT (handlers below), with the USER_LEFT reason;
    being kicked (net_client.kicked_out -> run_client.py); reconnecting and
    reconnected (NetClient signals, wired in run_client.py); and any
    app.toast() call from other modules (copy room code, errors, ...).

Sounds
    A short rising two-note chime on USER_JOINED and a falling one on
    USER_LEFT. The WAV data is generated in code with the standard `wave`
    module (no sound files to ship with PyInstaller) and written once to a
    temp file, because QSoundEffect only plays from a URL. If QtMultimedia
    or the audio device is missing, QApplication.beep() is used instead.
    View > Mute sounds toggles them.

Everything here runs on the GUI thread: on_message handlers are delivered
there by net_client, and app.toast() is only called from GUI code.
"""

import atexit
import math
import os
import struct
import tempfile
import wave

from PyQt5.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, Qt,
                          QTimer, QUrl)
from PyQt5.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel

try:  # QtMultimedia is part of PyQt5, but some Linux builds ship without it.
    from PyQt5.QtMultimedia import QSoundEffect
except ImportError:  # pragma: no cover - depends on the PyQt5 build
    QSoundEffect = None

from common import protocol
from client import icons, theme
from client.registry import on_message, register_menu_action

# --- Toast look and timing -----------------------------------------------------

MAX_TOASTS = 3       # visible at the same time
SHOW_MS = 3000       # whole life of one toast, fades included
FADE_MS = 250        # fade-in (and slide-in) and fade-out duration
SLIDE_PX = 16        # a new toast slides up this far while it fades in
MARGIN = 16          # distance from the canvas's right and bottom edges
SPACING = 8          # gap between stacked toasts
MAX_WIDTH = 360      # longer texts wrap
PADDING = 10         # inside the rounded card
ICON = 18            # icon size

# USER_LEFT reason (contract C1) -> words after the name.
LEAVE_WORDS = {
    "leave": "left the room",
    "timeout": "lost the connection",
    "kicked": "was removed by the host",
}

# Words that hint at the kind of a toast (lower case). Only the LOOK depends
# on it (accent colour and icon); the text is shown exactly as given.
ERROR_WORDS = ("cannot", "can't", "could not", "failed", "error", "refused", "forbidden",
               "wrong", "not allowed", "only the host", "viewers cannot", "does not allow",
               "too large", "no room", "not connected")
WARNING_WORDS = ("lost", "reconnecting", "removed", "kicked", "left the room", "timed out",
                 "offline", "not changed", "nothing to")
SUCCESS_WORDS = ("copied", "joined", "saved", "reconnected", "back in room", "restored",
                 "renamed", "deleted", "reset")

# kind -> (icon name, theme token of its colour)
KIND_LOOK = {"info": ("info", "primary"), "success": ("success", "success"),
             "warning": ("warning", "warning"), "error": ("error", "danger")}


def toast_kind(text):
    """Guess 'error', 'warning', 'success' or 'info' from the wording (a styling hint only)."""
    low = str(text).lower()
    for kind, words in (("error", ERROR_WORDS), ("warning", WARNING_WORDS),
                        ("success", SUCCESS_WORDS)):
        if any(word in low for word in words):
            return kind
    return "info"


class Toast(QFrame):
    """One notification card: icon + text; fades and slides in, waits, fades out, deletes itself.

    It ignores the mouse (WA_TransparentForMouseEvents): clicks go to the
    canvas or panel underneath, so a toast can never block drawing or typing.
    """

    def __init__(self, text, parent, on_gone):
        """Create the toast as a child of parent; on_gone(toast) runs when it is removed."""
        super().__init__(parent)
        self._on_gone = on_gone
        self._leaving = False
        self.kind = toast_kind(text)
        self.setObjectName("toast")
        self.setProperty("kind", self.kind)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        icon_name, token = KIND_LOOK[self.kind]
        self.icon = QLabel()
        self.icon.setPixmap(icons.pixmap(icon_name, ICON, theme.tokens()[token]))
        self.icon.setFixedSize(ICON, ICON)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(PADDING + 4, PADDING, PADDING + 2, PADDING)
        row.setSpacing(10)
        row.addWidget(self.icon, 0, Qt.AlignTop)
        row.addWidget(self.label, 1)
        self._fit(text)

        # Opacity is animated through a graphics effect (a child widget has
        # no window opacity of its own); the slide animates the position.
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(FADE_MS)
        self._slide.setEasingCurve(QEasingCurve.OutCubic)

        # A QTimer owned by the toast dies with it, so it never fires on a
        # deleted widget (QTimer.singleShot with a lambda could).
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.fade_out)

    def _fit(self, text):
        """Size the card to the text: one line if it fits, else wrapped within MAX_WIDTH."""
        fm = self.label.fontMetrics()
        advance = fm.horizontalAdvance(text) if hasattr(fm, "horizontalAdvance") else fm.width(text)
        chrome = ICON + 10 + 2 * PADDING + 8          # icon, gap, padding, accent border
        text_w = min(MAX_WIDTH - chrome, advance + 6)
        text_h = fm.boundingRect(QRect(0, 0, text_w, 10000), Qt.TextWordWrap, text).height()
        self.label.setFixedWidth(text_w)
        self.setFixedSize(text_w + chrome, max(text_h, ICON) + 2 * PADDING + 2)

    def start(self, target):
        """Show the toast at QPoint target: fade in while sliding up, then start its life timer."""
        self.move(target + QPoint(0, SLIDE_PX))
        self.show()
        self.raise_()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._slide.setStartValue(target + QPoint(0, SLIDE_PX))
        self._slide.setEndValue(target)
        self._slide.start()
        self._life.start(SHOW_MS - FADE_MS)

    def place(self, target):
        """Move to target (stacking changed); a running slide-in is redirected there."""
        if self._slide.state() == QPropertyAnimation.Running:
            self._slide.setEndValue(target)
        else:
            self.move(target)

    def fade_out(self):
        """Fade out, then remove the toast (only once)."""
        if self._leaving:
            return
        self._leaving = True
        self._life.stop()
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.remove_now)
        self._anim.start()

    def remove_now(self):
        """Remove at once: tell the manager, then delete the widget."""
        self._leaving = True
        self._life.stop()
        self._anim.stop()
        self._slide.stop()
        callback, self._on_gone = self._on_gone, None
        if callback is not None:
            callback(self)
            self.hide()
            self.deleteLater()


class ToastManager(QObject):
    """Keeps the toasts of one window stacked in the bottom-right corner of the canvas."""

    def __init__(self, window):
        """Attach to window (a QMainWindow) and follow its size changes."""
        super().__init__(window)
        self.setObjectName("toastManager")
        self.window = window
        self.toasts = []            # oldest first; the last one is at the bottom
        window.installEventFilter(self)
        # The canvas also changes size when panels are hidden or collapsed.
        central = window.centralWidget() if hasattr(window, "centralWidget") else None
        if central is not None:
            central.installEventFilter(self)

    def show(self, text):
        """Add a toast with text; push out the oldest if MAX_TOASTS are showing."""
        while len(self.toasts) >= MAX_TOASTS:
            self.toasts[0].remove_now()      # calls _forget, which shortens the list
        toast = Toast(text, self.window, self._forget)
        self.toasts.append(toast)
        targets = self._targets()
        for other in self.toasts[:-1]:
            other.place(targets[other])
        toast.start(targets[toast])

    def _forget(self, toast):
        """A toast is gone: drop it from the list and close the gap."""
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.layout()

    def _area(self):
        """The rectangle (window coordinates) the toasts stack in: the canvas, else the window."""
        w = self.window
        central = w.centralWidget() if hasattr(w, "centralWidget") else None
        if central is not None and central.isVisible():
            return central.geometry()
        rect = w.rect()
        bar = w.statusBar() if hasattr(w, "statusBar") else None
        if bar is not None and bar.isVisible():
            rect.setBottom(rect.bottom() - bar.height())
        return rect

    def _targets(self):
        """{toast: QPoint}: bottom-up from the area's bottom-right corner, newest lowest."""
        area = self._area()
        bottom = area.bottom() - MARGIN
        out = {}
        for toast in reversed(self.toasts):
            bottom -= toast.height()
            out[toast] = QPoint(area.right() - toast.width() - MARGIN, bottom)
            bottom -= SPACING
        return out

    def layout(self):
        """Place every toast at its stacking position, newest lowest."""
        for toast, target in self._targets().items():
            toast.place(target)
            toast.raise_()

    def eventFilter(self, obj, event):
        """Re-stack the toasts when the window or the canvas is resized; never swallow the event."""
        if event.type() == QEvent.Resize and self.toasts:
            self.layout()
        return False


def show_toast(window, text):
    """Show text as a toast on window (None: print it, e.g. before the window exists).

    The ToastManager is created on the first toast of each window.
    """
    if window is None:
        print("[toast]", text)
        return
    manager = window.findChild(ToastManager, "toastManager", Qt.FindDirectChildrenOnly)
    if manager is None:
        manager = ToastManager(window)
    manager.show(str(text))


# --- Sounds --------------------------------------------------------------------

SAMPLE_RATE = 22050
VOLUME = 0.35        # 0..1 of full scale; a notification should not startle

# (frequency Hz, duration s) notes of each sound.
JOIN_NOTES = ((660.0, 0.08), (880.0, 0.12))    # rising: E5 -> A5
LEAVE_NOTES = ((880.0, 0.08), (587.0, 0.12))   # falling: A5 -> D5

_muted = False
_effects = None      # {"join": QSoundEffect or None, "leave": ...}, built on first use
_temp_files = []


def make_wav_bytes(notes, rate=SAMPLE_RATE, volume=VOLUME):
    """Return 16-bit mono PCM samples (bytes) for a sequence of sine notes.

    Each note gets a 5 ms fade in and out, otherwise the jump from silence
    to a full sine wave is heard as a click.
    """
    frames = bytearray()
    ramp = int(rate * 0.005)
    for freq, seconds in notes:
        n = int(rate * seconds)
        for i in range(n):
            envelope = min(1.0, i / ramp, (n - 1 - i) / ramp) if ramp else 1.0
            value = volume * envelope * math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(value * 32767))
    return bytes(frames)


def write_temp_wav(prefix, notes):
    """Write notes as a WAV file in the temp folder and return its path.

    The file is deleted when the program exits.
    """
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)            # 16-bit
        w.setframerate(SAMPLE_RATE)
        w.writeframes(make_wav_bytes(notes))
    _temp_files.append(path)
    return path


@atexit.register
def _remove_temp_files():
    """Delete the generated WAV files at exit."""
    for path in _temp_files:
        try:
            os.remove(path)
        except OSError:
            pass


def _load_effects():
    """Create the two QSoundEffects once; {name: None} where that is impossible."""
    global _effects
    if _effects is not None:
        return _effects
    _effects = {"join": None, "leave": None}
    if QSoundEffect is None:
        return _effects
    for name, notes in (("join", JOIN_NOTES), ("leave", LEAVE_NOTES)):
        try:
            effect = QSoundEffect(QApplication.instance())
            effect.setSource(QUrl.fromLocalFile(write_temp_wav("whiteboard_%s_" % name, notes)))
            effect.setVolume(1.0)    # the loudness is already in the samples
            _effects[name] = effect
        except (OSError, RuntimeError):
            _effects[name] = None
    return _effects


def play(name):
    """Play the "join" or "leave" sound, unless muted; beep if it cannot play.

    QSoundEffect loads its file in the background; play() before the load
    has finished is queued by Qt and plays as soon as it is ready.
    """
    if _muted:
        return
    effect = _load_effects().get(name)
    if effect is None or effect.status() != QSoundEffect.Ready:
        QApplication.beep()
        return
    effect.play()


def preload_sounds():
    """Generate and load the sounds now, so the first join is heard without delay."""
    _load_effects()


def _toggle_mute(app):
    """View > Mute sounds: switch the join/leave sounds off or on."""
    global _muted
    _muted = not _muted
    app.toast("Sounds muted." if _muted else "Sounds on.")


register_menu_action("View", "Mute sounds", _toggle_mute)


# --- Message handlers (GUI thread) ---------------------------------------------

@on_message(protocol.USER_JOINED)
def _on_user_joined(app, msg):
    """USER_JOINED {name, color}: someone (re)joined the room."""
    name = (msg.get("payload") or {}).get("name") or "Someone"
    app.toast("%s joined" % name)
    play("join")


@on_message(protocol.USER_LEFT)
def _on_user_left(app, msg):
    """USER_LEFT {name, color, reason}: someone left, timed out or was removed.

    A USER_LEFT about me means I was kicked: net_client handles that and
    run_client.py shows the message, so nothing is done here.
    """
    p = msg.get("payload") or {}
    name = p.get("name") or "Someone"
    if name == app.me.username:
        return
    app.toast("%s %s" % (name, LEAVE_WORDS.get(p.get("reason"), "left")))
    play("leave")
