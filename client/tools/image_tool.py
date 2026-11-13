"""image_tool.py: Put a picture on the board: pick a file, shrink it, upload it in chunks, then add an image object (contract C3, D).

Two ways in, both ending in start_upload():

    Insert > Image...        the picture goes to the CENTRE OF THE VIEW
    the 'image' tool (I)     click the canvas, pick a file: the picture is centred on the click

The steps, in order:

    1. prepare_image(path)   read the file (EXIF rotation applied), shrink it with QImage to at most MAX_SIDE (1600)
                             pixels on the longest side, encode it as PNG or JPEG (see encode_image), compute the
                             sha256 of the encoded bytes: that is the asset_id (common/kinds_image.py)
    2. ImageUploader         IMG_BEGIN {asset_id, name, mime, size, chunks, w, h}, then IMG_CHUNK {asset_id, idx, data}
                             x n with 24 KB of raw bytes each (base64 in JSON), then IMG_END {asset_id, sha256}.
                             The chunks leave from a QTimer, CHUNKS_PER_TICK at a time, and a progress dialog shows how
                             far it is. Nothing here blocks the GUI thread: NetClient.send only queues a message.
    3. IMG_OK {asset_id}     the server verified the picture. The uploader then sends ONE OP_ADD through ops.send_op:
                             {kind:'image', x, y, w, h, asset_id}, sized to fit the view and snapped to the grid like
                             every other object. (Until then nobody can draw the picture: the object would name an
                             asset the server does not have, and the server refuses such an OP_ADD.)
       ERROR                 any ERROR about IMG_BEGIN / IMG_CHUNK / IMG_END ends the upload with the server's text.

Why in chunks, and not as one big frame that TCP could carry: see the long explanation in common/kinds_image.py
(short version: one connection carries everything, so a 6.7 MB frame would block cursors, strokes, chat and the
heartbeat of this client for as long as it takes to send; chunks interleave with them and give progress and a
way to stop). The pacing here (a few chunks per timer tick, not all 214 in one loop) is the client's half of
that: the GUI stays responsive, the progress shown is real, and the sender thread of NetClient is never handed
5 MB at once.

My own picture goes into the asset cache the moment it is encoded, so it shows at once without downloading it
back from the server.
"""
import base64
import hashlib
import os

from PyQt5.QtCore import QBuffer, QIODevice, QObject, QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QImageReader
from PyQt5.QtWidgets import QApplication, QFileDialog, QProgressDialog

from common import config, kinds_image, protocol
from client import asset_cache, ops, registry

MAX_SOURCE_BYTES = 64 * 1024 * 1024         # refuse absurd files before decoding them
MAX_SOURCE_PIXELS = 100_000_000             # a 10000 x 10000 picture is already 400 MB decoded
PNG_KEEP_BYTES = 1024 * 1024                # a PNG up to this size is kept as it is (lossless: diagrams, screenshots)
JPEG_QUALITIES = (88, 75, 60, 45)           # tried in turn for a big picture without transparency
SHRINK = 0.8                                # factor of one more shrinking step when nothing fits in 5 MB
MIN_SIDE = 32                               # never shrink below this
CHUNKS_PER_TICK = 6                         # IMG_CHUNKs sent per timer tick (about 200 KB)
TICK_MS = 15
FIT_FRACTION = 0.8                          # a new picture covers at most this part of the view
EDIT_ROLES = ("host", "editor")             # may upload (contract C4, permission 'upload')
FILE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*)"


class ImageError(Exception):
    """A picture that cannot be used; the text is shown to the user as a toast."""


# ---------------------------------------------------------------- preparing the picture

class Prepared:
    """A picture ready to upload: encoded bytes, their mime type, the pixel size, a label and the sha256 (= asset id)."""

    def __init__(self, data, mime, width, height, name, sha256):
        """Store the fields."""
        self.data, self.mime, self.w, self.h, self.name, self.sha256 = data, mime, width, height, name, sha256

    @property
    def chunks(self):
        """Number of IMG_CHUNK messages."""
        return kinds_image.chunk_count(len(self.data))


def _encode(image, fmt, quality=-1):
    """Encode a QImage to bytes in memory ('PNG' or 'JPEG'); raises ImageError if Qt cannot."""
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    if not image.save(buf, fmt, quality):
        raise ImageError("could not encode the picture as %s" % fmt)
    return bytes(buf.data())


def _shrunk(image, factor):
    """The image scaled by `factor` (smooth), or ImageError when that would go below MIN_SIDE."""
    w, h = int(image.width() * factor), int(image.height() * factor)
    if min(w, h) < MIN_SIDE:
        raise ImageError("cannot get this picture under %d MB" % (kinds_image.MAX_IMAGE_BYTES // (1024 * 1024)))
    return image.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def encode_image(image):
    """Encode a QImage as PNG or JPEG so that it fits in MAX_IMAGE_BYTES. Returns (bytes, mime, the image that was encoded).

    The rule:
      * PNG when it is small (up to PNG_KEEP_BYTES): lossless and compact for diagrams and screenshots;
      * a big picture WITH an alpha channel stays PNG (JPEG has no transparency) if it fits;
      * a big picture without transparency (a photo) becomes JPEG, quality 88 down to 45;
      * if nothing fits, the picture is shrunk by SHRINK and the rule is applied again.
    """
    while True:
        png = _encode(image, "PNG")
        if len(png) <= PNG_KEEP_BYTES:
            return png, "image/png", image
        if image.hasAlphaChannel():
            if len(png) <= kinds_image.MAX_IMAGE_BYTES:
                return png, "image/png", image
        else:
            for quality in JPEG_QUALITIES:
                jpeg = _encode(image, "JPEG", quality)
                if len(jpeg) <= kinds_image.MAX_IMAGE_BYTES:
                    return jpeg, "image/jpeg", image
        image = _shrunk(image, SHRINK)


def prepare_image(path):
    """Read a picture file and make it ready to upload (see the module docstring, step 1). Raises ImageError."""
    try:
        if os.path.getsize(path) > MAX_SOURCE_BYTES:
            raise ImageError("the file is larger than %d MB" % (MAX_SOURCE_BYTES // (1024 * 1024)))
    except OSError as exc:
        raise ImageError("cannot read the file: %s" % exc.strerror)
    reader = QImageReader(path)
    reader.setAutoTransform(True)               # honour the camera's rotation flag (EXIF)
    size = reader.size()
    if size.isValid() and size.width() * size.height() > MAX_SOURCE_PIXELS:
        raise ImageError("the picture has too many pixels (%d x %d)" % (size.width(), size.height()))
    image = reader.read()
    if image.isNull():
        raise ImageError("not a picture I can read (%s)" % (reader.errorString() or "unknown format"))
    if max(image.width(), image.height()) > kinds_image.MAX_SIDE:
        image = image.scaled(kinds_image.MAX_SIDE, kinds_image.MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    data, mime, final = encode_image(image)
    return Prepared(data, mime, final.width(), final.height(), os.path.basename(path)[:kinds_image.MAX_NAME],
                    hashlib.sha256(data).hexdigest())


# ---------------------------------------------------------------- placing the object

def _r1(value):
    """Round to one decimal (contract D), as an int when it is whole (smaller JSON)."""
    value = round(float(value), 1)
    return int(value) if value.is_integer() else value


def placement(app, width, height, anchor=None):
    """Position and size of a new image object, in board units: {x, y, w, h}.

    The centre is `anchor` (a click) or the centre of the visible part of the
    board; the picture keeps its aspect ratio and is made smaller if needed so
    that it covers at most FIT_FRACTION of the view; the top left corner is
    snapped to the grid when snapping is on (contract D: snapping is done by
    the client) and the object is kept inside the board.
    """
    canvas = getattr(app, "canvas", None)
    board = QRectF(0, 0, config.BOARD_W, config.BOARD_H)
    view = canvas.visible_board_rect().intersected(board) if canvas is not None else board
    if view.isEmpty():
        view = board
    centre = QPointF(anchor) if anchor is not None else view.center()
    scale = min(1.0, FIT_FRACTION * view.width() / width, FIT_FRACTION * view.height() / height,
                config.BOARD_W / width, config.BOARD_H / height)
    w, h = max(1.0, width * scale), max(1.0, height * scale)
    top_left = QPointF(centre.x() - w / 2, centre.y() - h / 2)
    if canvas is not None and getattr(canvas, "snap_enabled", False):
        top_left = canvas.snap(top_left)
    x = min(max(0.0, top_left.x()), max(0.0, config.BOARD_W - w))
    y = min(max(0.0, top_left.y()), max(0.0, config.BOARD_H - h))
    return {"x": _r1(x), "y": _r1(y), "w": _r1(w), "h": _r1(h)}


# ---------------------------------------------------------------- the upload

class ImageUploader(QObject):
    """Sends one prepared picture (IMG_BEGIN, IMG_CHUNK x n, IMG_END) and adds the image object when the server says IMG_OK.

    state: 'sending' -> 'waiting' (all sent, no IMG_OK yet) -> 'done', or 'failed' / 'cancelled'.
    on_progress(sent, total) is called after every group of chunks (total = chunks + 1: the last step is the
    server's answer); on_done(ok, text) once at the end.

    `send(type, payload)` defaults to app.net.send and must return False when not connected; tests pass their own.
    """

    def __init__(self, app, prepared, anchor=None, on_progress=None, on_done=None, send=None,
                 batch=CHUNKS_PER_TICK, interval_ms=TICK_MS, parent=None):
        """Remember what to send; nothing goes out until start() is called."""
        super().__init__(parent)
        self.app, self.prepared, self.anchor = app, prepared, anchor
        self.asset_id = prepared.sha256
        self.state = "new"
        self.next_chunk = 0
        self._on_progress, self._on_done = on_progress, on_done
        self._send = send or (lambda type_, payload: app.net is not None and app.net.send(type_, payload))
        self._batch = batch
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    @property
    def total(self):
        """Steps shown by the progress: every chunk plus the wait for the server's answer."""
        return self.prepared.chunks + 1

    @property
    def active(self):
        """True while the upload is running (sending or waiting for IMG_OK)."""
        return self.state in ("sending", "waiting")

    def start(self):
        """Send IMG_BEGIN and begin the paced chunk sending."""
        p = self.prepared
        if not self._send(protocol.IMG_BEGIN, {"asset_id": p.sha256, "name": p.name, "mime": p.mime, "size": len(p.data),
                                              "chunks": p.chunks, "w": p.w, "h": p.h}):
            self._finish("failed", "Not connected")
            return
        self.state = "sending"
        self._timer.start()

    def _tick(self):
        """Send the next group of chunks; after the last one send IMG_END and wait for IMG_OK."""
        if not self.active or self.state == "waiting":
            self._timer.stop()
            return
        p, raw = self.prepared, self.prepared.data
        for _ in range(self._batch):
            if self.next_chunk >= p.chunks:
                break
            i = self.next_chunk
            piece = raw[i * kinds_image.CHUNK_BYTES:(i + 1) * kinds_image.CHUNK_BYTES]
            if not self._send(protocol.IMG_CHUNK, {"asset_id": p.sha256, "idx": i,
                                                   "data": base64.b64encode(piece).decode("ascii")}):
                self._finish("failed", "Connection lost during the upload")
                return
            self.next_chunk += 1
        if self._on_progress is not None:
            self._on_progress(self.next_chunk, self.total)
        if self.next_chunk >= p.chunks:
            self._timer.stop()
            if not self._send(protocol.IMG_END, {"asset_id": p.sha256, "sha256": p.sha256}):
                self._finish("failed", "Connection lost during the upload")
                return
            self.state = "waiting"

    def on_ok(self):
        """IMG_OK arrived: add the image object to the board (ONE OP_ADD through send_op) and finish."""
        if not self.active:
            return
        place = placement(self.app, self.prepared.w, self.prepared.h, self.anchor)
        obj = {"id": protocol.new_op_id(self.app.me.username or "me"), "kind": "image", "asset_id": self.asset_id}
        obj.update(place)
        ops.send_op(self.app, protocol.OP_ADD, {"obj": obj})
        self._finish("done", "Image added")

    def on_error(self, text):
        """The server refused a message of this upload: stop and report its text."""
        if self.active:
            self._finish("failed", text)

    def cancel(self):
        """The user cancelled: stop sending. The server drops the unfinished upload when this client leaves or starts it again."""
        if self.active:
            self._finish("cancelled", "Upload cancelled")

    def _finish(self, state, text):
        """Stop the timer, set the final state and tell the caller once."""
        self.state = state
        self._timer.stop()
        if self._on_progress is not None and state == "done":
            self._on_progress(self.total, self.total)
        if self._on_done is not None:
            self._on_done(state == "done", text)


_active = None          # the upload in progress (one at a time; a QObject must be referenced or it is garbage collected)


@registry.on_message(protocol.IMG_OK)
def _on_img_ok(app, msg):
    """IMG_OK {asset_id}: the server accepted the picture of the running upload."""
    if _active is not None and _active.asset_id == (msg.get("payload") or {}).get("asset_id"):
        _active.on_ok()


@registry.on_message(protocol.ERROR)
def _on_upload_error(app, msg):
    """ERROR about IMG_BEGIN / IMG_CHUNK / IMG_END: end the running upload with the server's text."""
    payload = msg.get("payload") or {}
    if _active is not None and payload.get("ref_type") in (protocol.IMG_BEGIN, protocol.IMG_CHUNK, protocol.IMG_END):
        _active.on_error("%s: %s" % (payload.get("code"), payload.get("text")))


# ---------------------------------------------------------------- entry points

def _may_upload(app):
    """True if I may put a picture on the board now; otherwise toast why (the server checks the role again)."""
    if getattr(app.me, "role", None) not in EDIT_ROLES:
        app.toast("Viewers cannot add images")
        return False
    if app.net is None or not app.net.is_connected:
        app.toast("Not connected")
        return False
    if _active is not None and _active.active:
        app.toast("An image is still being uploaded")
        return False
    return True


def start_upload(app, path, anchor=None, show_progress=True):
    """Prepare the file and upload it; returns the ImageUploader, or None if it could not start (a toast says why).

    show_progress=False skips the progress dialog (tests).
    """
    global _active
    if not _may_upload(app):
        return None
    gui = isinstance(QApplication.instance(), QApplication)
    if gui:
        QApplication.setOverrideCursor(Qt.WaitCursor)      # decoding and encoding a big picture takes a moment
    try:
        prepared = prepare_image(path)
    except ImageError as exc:
        app.toast("Cannot use this picture: %s" % exc)
        return None
    finally:
        if gui:
            QApplication.restoreOverrideCursor()
    asset_cache.cache.app = app
    asset_cache.cache.store(prepared.sha256, prepared.data)       # my own copy is shown at once
    dialog = None
    if show_progress and gui:
        dialog = QProgressDialog("Uploading %s ..." % prepared.name, "Cancel", 0, prepared.chunks + 1, app.window)
        dialog.setWindowTitle("Insert image")
        dialog.setWindowModality(Qt.NonModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)

    def progress(sent, total):
        """Move the progress bar and the status line."""
        if dialog is not None:
            dialog.setValue(sent)
        else:
            app.toast("Uploading %s: %d / %d" % (prepared.name, min(sent, total - 1), total - 1))

    def done(ok, text):
        """Close the dialog and tell the user how it ended."""
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        app.toast(text if ok else "Image not added: %s" % text)

    _active = ImageUploader(app, prepared, anchor, on_progress=progress, on_done=done, parent=app)
    if dialog is not None:
        dialog.canceled.connect(_active.cancel)
    _active.start()
    return _active


def pick_file(app):
    """Ask for a picture file; returns its path, or None when cancelled."""
    path, _ = QFileDialog.getOpenFileName(app.window, "Insert image", "", FILE_FILTER)
    return path or None


def insert_image(app):
    """Insert > Image...: pick a file; the picture goes to the centre of the view."""
    if not _may_upload(app):
        return
    path = pick_file(app)
    if path:
        start_upload(app, path)


class ImageTool:
    """Image tool: click the canvas, pick a file, the picture is centred on the click."""

    id = "image"
    label = "Image"
    shortcut = "I"
    group = "media"

    def on_press(self, canvas, p, ev):
        """Nothing happens on press (the file dialog opens on release, so the click is complete)."""

    def on_move(self, canvas, p, ev):
        """Nothing to follow: the tool has no drag."""

    def on_release(self, canvas, p, ev):
        """Left click: pick a file and start the upload at this point, then go back to the Select tool."""
        if ev.button() != Qt.LeftButton:
            return
        app = canvas.app
        if not _may_upload(app):
            return
        path = pick_file(app)
        if path and start_upload(app, path, anchor=QPointF(p)) is not None:
            QTimer.singleShot(0, lambda: self._back_to_select(app))

    @staticmethod
    def _back_to_select(app):
        """After a click the job is done: make Select the active tool again (so the next click does not ask for a file)."""
        window = getattr(app, "window", None)
        if window is not None and hasattr(window, "select_tool") and "select" in registry.TOOLS:
            window.select_tool("select")

    def cancel(self, canvas):
        """Escape: nothing to cancel (nothing is pending between clicks)."""

    def cursor(self):
        """Pointing hand: a click opens the file picker."""
        return Qt.PointingHandCursor


registry.register_tool(ImageTool)
registry.register_menu_action("Insert", "Image...", insert_image)
