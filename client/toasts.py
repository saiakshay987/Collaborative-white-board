# OWNER: P1
"""toasts.py: Transient on-screen notifications behind app.toast(text), plus join/leave sounds.

Toasts
    app.toast(text) (client/app_state.py) calls show_toast(window, text).
    A toast is a small dark rounded label in the bottom-right corner of the
    main window. It fades in, stays, and fades out; its whole life is
    SHOW_MS (3 s). At most MAX_TOASTS (3) are on screen: the newest is at
    the bottom, older ones move up, and a 4th pushes the oldest out at
    once. A click dismisses a toast early.

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

from PyQt5.QtCore import (QEasingCurve, QEvent, QObject, QPropertyAnimation, QRect, Qt, QTimer,
                          QUrl)
from PyQt5.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel

try:  # QtMultimedia is part of PyQt5, but some Linux builds ship without it.
    from PyQt5.QtMultimedia import QSoundEffect
except ImportError:  # pragma: no cover - depends on the PyQt5 build
    QSoundEffect = None

from common import protocol
from client.registry import on_message, register_menu_action

# --- Toast look and timing -----------------------------------------------------

MAX_TOASTS = 3       # visible at the same time
SHOW_MS = 3000       # whole life of one toast, fades included
FADE_MS = 250        # fade-in and fade-out duration
MARGIN = 16          # distance from the window's right and bottom edges
SPACING = 8          # gap between stacked toasts
MAX_WIDTH = 340      # longer texts wrap
PADDING = 10         # inside the rounded box (also in the style sheet)

TOAST_STYLE = ("QLabel { background-color: rgba(40, 40, 40, 225); color: white;"
               " border-radius: 8px; padding: %dpx; }" % PADDING)

# USER_LEFT reason (contract C1) -> words after the name.
LEAVE_WORDS = {
    "leave": "left the room",
    "timeout": "lost the connection",
    "kicked": "was removed by the host",
}


class Toast(QLabel):
    """One notification bubble; fades in, waits, fades out, then deletes itself."""

    def __init__(self, text, parent, on_gone):
        """Create the toast as a child of parent; on_gone(toast) runs when it is removed."""
        super().__init__(text, parent)
        self._on_gone = on_gone
        self._leaving = False
        self.setStyleSheet(TOAST_STYLE)
        self.setWordWrap(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to dismiss")
        self._fit(text)

        # Opacity is animated through a graphics effect (a child widget has
        # no window opacity of its own).
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)

        # A QTimer owned by the toast dies with it, so it never fires on a
        # deleted widget (QTimer.singleShot with a lambda could).
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.fade_out)

    def _fit(self, text):
        """Size the box to the text: one line if it fits, else wrapped at MAX_WIDTH."""
        fm = self.fontMetrics()
        advance = fm.horizontalAdvance(text) if hasattr(fm, "horizontalAdvance") else fm.width(text)
        # +6: word-wrap layout needs a pixel or two more than the advance.
        width = min(MAX_WIDTH, advance + 2 * PADDING + 6)
        # QLabel.heightForWidth ignores the style-sheet padding, so measure
        # the wrapped text ourselves inside the padded width.
        inner = QRect(0, 0, width - 2 * PADDING, 10000)
        text_h = fm.boundingRect(inner, Qt.TextWordWrap, text).height()
        self.setFixedSize(width, text_h + 2 * PADDING + 2)

    def start(self):
        """Show the toast, fade it in and start its life timer."""
        self.show()
        self.raise_()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._life.start(SHOW_MS - FADE_MS)

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
        callback, self._on_gone = self._on_gone, None
        if callback is not None:
            callback(self)
            self.hide()
            self.deleteLater()

    def mousePressEvent(self, event):
        """A click dismisses the toast early."""
        self.fade_out()
        event.accept()


class ToastManager(QObject):
    """Keeps the toasts of one window stacked in its bottom-right corner."""

    def __init__(self, window):
        """Attach to window (a QMainWindow) and follow its size changes."""
        super().__init__(window)
        self.setObjectName("toastManager")
        self.window = window
        self.toasts = []            # oldest first; the last one is at the bottom
        window.installEventFilter(self)

    def show(self, text):
        """Add a toast with text; push out the oldest if MAX_TOASTS are showing."""
        while len(self.toasts) >= MAX_TOASTS:
            self.toasts[0].remove_now()      # calls _forget, which shortens the list
        toast = Toast(text, self.window, self._forget)
        self.toasts.append(toast)
        self.layout()
        toast.start()

    def _forget(self, toast):
        """A toast is gone: drop it from the list and close the gap."""
        if toast in self.toasts:
            self.toasts.remove(toast)
            self.layout()

    def layout(self):
        """Place the toasts bottom-up, newest lowest, above the status bar."""
        w = self.window
        bottom = w.height() - MARGIN
        bar = w.statusBar() if hasattr(w, "statusBar") else None
        if bar is not None and bar.isVisible():
            bottom -= bar.height()
        for toast in reversed(self.toasts):
            bottom -= toast.height()
            toast.move(w.width() - toast.width() - MARGIN, bottom)
            toast.raise_()
            bottom -= SPACING

    def eventFilter(self, obj, event):
        """Re-stack the toasts when the window is resized; never swallow the event."""
        if obj is self.window and event.type() == QEvent.Resize:
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
    if effect is None or effect.status() == QSoundEffect.Error:
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
