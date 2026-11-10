# OWNER: P3
"""export.py: Export the board as JSON, PNG or PDF, and load a JSON file back (CONTRACT section F).

    File > Save board as JSON    every object of the board, written to a file you choose.
                                 Local only: nothing is sent to the server.
    File > Load board from JSON  HOST ONLY. Reads such a file and ADDS its objects to the board.
    File > Export PNG  (Ctrl+E)  a picture of the board; 1x or 2x is chosen in the save dialog.
    File > Export PDF            the board on one A4 landscape page, with a footer line.

PNG and PDF reuse render.render_board, the function that paints the canvas, on a QImage or on a
QPdfWriter, so an export looks like the screen. They cover the bounding box of all objects plus
a 40 unit margin (the whole 4000 x 3000 board when it is empty), on white. Nothing is sent to
the network and the board is not changed; the exports read app.board.objects on the GUI
thread (the work is local CPU time, a wait cursor shows while it runs). See the section
"PNG and PDF" below for the details, including how images that are not loaded are handled.

The file is {"format": "whiteboard-board", "version": 1, "objects": [...]} with
the objects in paint (z) order.

Loading never writes to the socket itself and never sends everything at once.
Every object becomes an ordinary OP_ADD through client/ops.send_op (the one
path for every board change), but in small groups: BATCH_SIZE operations,
then a pause of BATCH_INTERVAL_MS driven by a QTimer. A board of thousands of
objects therefore trickles out at about BATCH_SIZE * 1000 / BATCH_INTERVAL_MS
operations per second, the GUI stays responsive (the timer runs on the GUI
thread but does only a few sends per tick) and the server and the other
clients are not flooded.

Loaded objects get FRESH ids (protocol.new_op_id) and lose owner and z:
  * the ids in the file may already be used on this board (loading a file
    twice, or a file saved from this very board), and the server refuses an
    OP_ADD with an existing id; ids that are on the board right now are
    skipped when the new ones are made (fresh_objects(taken=...));
  * a later object made by this client could otherwise reuse an id from the file;
  * owner and z belong to the server (contract D): it stamps the loader as
    owner and gives z in the order the objects arrive, which is the file's
    paint order.

Limits worth knowing: image objects keep their asset_id, but the image bytes
are not in the file, so they only show up when the server still has that asset.
"host only" is a rule of this menu entry; the server cannot tell a loaded
OP_ADD from a drawn one, so it only enforces the normal 'draw' permission.
"""
import contextlib
import copy
import json
import math
import os
import time

from PyQt5.QtCore import QMarginsF, QObject, QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen
from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox

from common import config, models, protocol
from client import ops, registry, render
from client.canvas import CULL_MARGIN, object_bounds
from client.registry import register_menu_action

FORMAT_NAME = "whiteboard-board"
FORMAT_VERSION = 1
BATCH_SIZE = 20             # OP_ADDs sent per timer tick
BATCH_INTERVAL_MS = 40      # pause between two groups: at most 500 objects per second
MAX_FILE_BYTES = 64 * 1024 * 1024   # refuse absurd files before reading them into memory


# ---------------------------------------------------------------- the file format

def build_document(objects):
    """The JSON text of a board file for a dict or list of objects, in paint (z) order.

    Objects that have no z yet (still waiting for the server echo) go last,
    which is where the renderer paints them too.
    """
    values = list(objects.values()) if isinstance(objects, dict) else list(objects)
    values.sort(key=lambda o: (o.get("z") is None, o.get("z") or 0))
    return json.dumps({"format": FORMAT_NAME, "version": FORMAT_VERSION, "objects": values},
                      indent=1, sort_keys=True)


def parse_document(text):
    """Read a board file. Returns (objects, skipped): the valid objects and the number of invalid ones.

    Raises ValueError with a short message when the text is not a board file
    at all (not JSON, wrong format name, no object list). One bad object does
    not spoil the file: it is skipped and counted. The same check the server
    applies to an OP_ADD (models.validate_obj) is used, so what passes here is
    not refused later for its shape.
    """
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError("not a JSON file (%s)" % exc)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT_NAME:
        raise ValueError("not a whiteboard board file")
    if doc.get("version") != FORMAT_VERSION:
        raise ValueError("unsupported file version %r" % (doc.get("version"),))
    items = doc.get("objects")
    if not isinstance(items, list):
        raise ValueError("the file has no object list")
    objects = [o for o in items if models.validate_obj(o)[0]]
    return objects, len(items) - len(objects)


def fresh_objects(objects, username, taken=()):
    """Copies of the objects ready to be sent as OP_ADD: new id each, no owner, no z (see the module docstring).

    `taken` is the set of ids already on the board. new_op_id() counts per
    program run, so after a restart of the client (or of the server, with
    its autosaved board) it can hand out an id that an old object still
    uses; such ids are skipped.
    """
    used = set(taken)
    result = []
    for obj in objects:
        new = copy.deepcopy(obj)
        new.pop("owner", None)
        new.pop("z", None)
        new["id"] = protocol.new_op_id(username)
        while new["id"] in used:
            new["id"] = protocol.new_op_id(username)
        used.add(new["id"])
        result.append(new)
    return result


# ---------------------------------------------------------------- sending in small groups

class BoardLoader(QObject):
    """Sends a list of objects as OP_ADD, BATCH_SIZE per timer tick, on the GUI thread.

    `send(app, type, payload)` defaults to ops.send_op; a test passes its own.
    A tick stops the whole load when the connection is gone or the role
    is no longer host. `on_done(sent, total)` is called once when it ends, and
    `on_progress(sent, total)` after every group.
    """

    def __init__(self, app, objects, send=ops.send_op, batch=BATCH_SIZE, interval_ms=BATCH_INTERVAL_MS,
                 on_progress=None, on_done=None, parent=None):
        """Remember what to send; nothing is sent until start() is called."""
        super().__init__(parent)
        self.app = app
        self.objects = list(objects)
        self.sent = 0
        self.finished = False
        self._send = send
        self._batch = batch
        self._on_progress = on_progress
        self._on_done = on_done
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self):
        """Begin sending (the first group goes out on the first tick, not inside this call)."""
        if not self.objects:
            self._finish()
            return
        self._timer.start()

    def _tick(self):
        """Send the next group of objects; stop when everything is sent or the link is lost."""
        if self.app.net is None or not self.app.net.is_connected:
            self._finish()          # no point in queueing the rest
            return
        for _ in range(self._batch):
            if self.sent >= len(self.objects):
                break
            self._send(self.app, protocol.OP_ADD, {"obj": self.objects[self.sent]})
            self.sent += 1
        if self._on_progress is not None:
            self._on_progress(self.sent, len(self.objects))
        if self.sent >= len(self.objects):
            self._finish()

    def _finish(self):
        """Stop the timer and report the result once."""
        if self.finished:
            return
        self.finished = True
        self._timer.stop()
        if self._on_done is not None:
            self._on_done(self.sent, len(self.objects))


_loader = None      # the load in progress (a QObject must be referenced or it is garbage collected)


# ---------------------------------------------------------------- menu actions

def _parent(app):
    """The window to attach dialogs to (None without a window)."""
    return app.window


def save_board(app):
    """File > Save board as JSON: write every object of the board to a file the user picks."""
    if app.board is None or not app.board.objects:
        app.toast("The board is empty: nothing to save")
        return
    path, _ = QFileDialog.getSaveFileName(_parent(app), "Save board as JSON", "board.json",
                                          "Whiteboard files (*.json)")
    if not path:
        return                      # dialog cancelled
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_document(app.board.objects))
    except OSError as exc:
        app.toast("Could not save: %s" % exc)
        return
    app.toast("Saved %d objects to %s" % (len(app.board.objects), path))


def load_board(app):
    """File > Load board from JSON (host only): read a file and send its objects as OP_ADD in small groups."""
    global _loader
    if app.me.role != "host":
        app.toast("Only the host can load a board")
        return
    if _loader is not None and not _loader.finished:
        app.toast("A board is still loading")
        return
    if app.net is None or not app.net.is_connected:
        app.toast("Not connected")
        return
    path, _ = QFileDialog.getOpenFileName(_parent(app), "Load board from JSON", "",
                                          "Whiteboard files (*.json)")
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(MAX_FILE_BYTES + 1)
        if len(text) > MAX_FILE_BYTES:
            raise ValueError("file is larger than %d MB" % (MAX_FILE_BYTES // (1024 * 1024)))
        objects, skipped = parse_document(text)
    except (OSError, ValueError) as exc:        # UnicodeDecodeError is a ValueError too
        app.toast("Could not load: %s" % exc)
        return
    if not objects:
        app.toast("No valid objects in that file")
        return
    answer = QMessageBox.question(
        _parent(app), "Load board",
        "Add %d objects to the current board?%s" % (
            len(objects), " (%d invalid objects will be skipped)" % skipped if skipped else ""))
    if answer != QMessageBox.Yes:
        return

    def progress(sent, total):
        """Show how far the load is."""
        app.toast("Loading board: %d / %d" % (sent, total))

    def done(sent, total):
        """Tell the user how the load ended."""
        app.toast("Board loaded: %d objects added" % sent if sent == total
                  else "Load stopped after %d of %d objects (connection lost)" % (sent, total))

    taken = app.board.objects.keys() if app.board is not None else ()
    _loader = BoardLoader(app, fresh_objects(objects, app.me.username or "me", taken),
                          on_progress=progress, on_done=done, parent=app)
    _loader.start()


# ---------------------------------------------------------------- PNG and PDF
#
# Both exports do the same four things: find the rectangle to export (export_rect),
# prepare the objects (export_objects), set up a painter on the target (QImage or
# QPdfWriter) so that board units map to target pixels, and call
# render.render_board with ctx = {"export": True, ...}.
#
# Images that are not loaded. The renderer of image objects (render_image.py) belongs to
# the image feature and may be missing, may fail, or may find the picture still downloading.
# An export must never fail or show a hole for that. Every image object is therefore
# swapped for a stand-in object of the kind EXPORT_IMAGE_KIND, whose renderer (below) calls
# the registered 'image' renderer inside a try block and, when there is none or it raises,
# paints a grey placeholder box instead. A renderer that is still waiting for its download
# must do the same itself: when ctx["export"] is true it paints a placeholder and starts
# no download (draw_placeholder is public for that).

PNG_MARGIN = 40             # board units around the bounding box (also used for the PDF)
PNG_SCALES = (1, 2)         # offered in the save dialog
PNG_MAX_PIXELS = 64_000_000     # about 256 MB of RGB; the whole board at 2x (48 M pixels) fits
PDF_DPI = 300               # resolution of the PDF paint device (board units are scaled to it)
PDF_PAGE_MARGIN_MM = 10
PDF_FOOTER_MM = 7           # height reserved under the board for the footer line
FOOTER_SUFFIX = "exported from LAN Whiteboard"
EXPORT_IMAGE_KIND = "_export_image"     # client-local stand-in kind, never sent anywhere
PLACEHOLDER_FILL = QColor("#ececec")
PLACEHOLDER_LINE = QColor("#9a9a9a")


class ExportError(Exception):
    """An export that cannot be done; the text is shown to the user as a toast."""


def _number(value, default=0.0):
    """value as a finite float, or default (a damaged object must not crash an export)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def draw_placeholder(painter, obj):
    """Paint the stand-in of an image that is not available: a grey box, a cross and the word 'image'.

    Everything is in board units and uses ordinary (not cosmetic) pens, so it
    scales with the export. A box of unusable size is skipped.
    """
    box = QRectF(_number(obj.get("x")), _number(obj.get("y")),
                 _number(obj.get("w")), _number(obj.get("h"))).normalized()
    if box.width() < 1 or box.height() < 1:
        return
    pen = QPen(PLACEHOLDER_LINE)
    pen.setWidthF(2.0)
    painter.setPen(pen)
    painter.setBrush(PLACEHOLDER_FILL)
    painter.drawRect(box)
    painter.drawLine(box.topLeft(), box.bottomRight())
    painter.drawLine(box.topRight(), box.bottomLeft())
    size = min(24.0, box.height() / 4.0, box.width() / 6.0)
    if size >= 6:
        font = QFont()
        font.setPixelSize(int(size))
        painter.setFont(font)
        painter.setPen(PLACEHOLDER_LINE.darker(150))
        painter.fillRect(QRectF(box.center().x() - size * 2, box.center().y() - size, size * 4, size * 2), PLACEHOLDER_FILL)
        painter.drawText(QRectF(box.center().x() - size * 2, box.center().y() - size, size * 4, size * 2),
                         Qt.AlignCenter, "image")


def _render_export_image(painter, obj, ctx):
    """Renderer of the stand-in objects: the real image renderer if it works, else the placeholder."""
    inner = obj["inner"]
    fn = registry.get_renderer("image")
    if fn is not None:
        painter.save()
        try:
            fn(painter, inner, ctx)
            return
        except Exception:  # noqa: BLE001 - a broken or unfinished image must not stop the export
            pass
        finally:
            painter.restore()
    draw_placeholder(painter, inner)


registry.register_renderer(EXPORT_IMAGE_KIND, _render_export_image)


def export_objects(objects):
    """The objects to paint, as a list: image objects swapped for stand-ins (see above), the rest as they are."""
    items = objects.values() if hasattr(objects, "values") else objects
    result = []
    for obj in items:
        if isinstance(obj, dict) and obj.get("kind") == "image":
            result.append({"id": obj.get("id"), "kind": EXPORT_IMAGE_KIND, "z": obj.get("z"), "inner": obj})
        else:
            result.append(obj)
    return result


def export_rect(objects):
    """The QRectF (board units) to export: the box around all objects plus PNG_MARGIN, or the whole board.

    client.canvas.object_bounds pads every box by CULL_MARGIN (a safety margin
    for painting only) plus the pen width; CULL_MARGIN is taken off again, the
    pen width stays so thick lines are not cut. Objects of unknown geometry
    are ignored here (they are still painted).
    """
    items = objects.values() if hasattr(objects, "values") else objects
    union = None
    for obj in items:
        box = object_bounds(obj) if isinstance(obj, dict) else None
        if box is None or not all(math.isfinite(v) for v in (box.x(), box.y(), box.width(), box.height())):
            continue
        box = box.adjusted(CULL_MARGIN, CULL_MARGIN, -CULL_MARGIN, -CULL_MARGIN)
        union = box if union is None else union.united(box)
    if union is None:
        return QRectF(0, 0, config.BOARD_W, config.BOARD_H)
    return union.adjusted(-PNG_MARGIN, -PNG_MARGIN, PNG_MARGIN, PNG_MARGIN)


def paint_board(painter, objects, source, transform_scale):
    """Paint the objects with `source` (a board-unit rect) mapped onto the painter's current origin at `transform_scale`.

    The caller has set up the painter (origin and clip of the target area);
    this scales it and shifts the source's top left to the origin. render.render_board does
    the painting, with ctx["export"] True (see the image paragraph above).
    """
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    painter.scale(transform_scale, transform_scale)
    painter.translate(-source.topLeft())
    painter.setClipRect(source)
    ctx = {"canvas": None, "zoom": transform_scale, "visible": source, "export": True}
    render.render_board(painter, export_objects(objects), ctx)


@contextlib.contextmanager
def busy_cursor():
    """Show the wait cursor while an export runs (only when a QApplication exists)."""
    gui = isinstance(QApplication.instance(), QApplication)
    if gui:
        QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        if gui:
            QApplication.restoreOverrideCursor()


# -- PNG

def render_png(objects, scale=1):
    """Render the board to a QImage: export_rect(objects) at `scale` (1 or 2), white background.

    Raises ExportError if the picture would have more than PNG_MAX_PIXELS pixels
    (the answer is then to export at 1x) or if Qt cannot allocate it.
    """
    source = export_rect(objects)
    width, height = math.ceil(source.width() * scale), math.ceil(source.height() * scale)
    if width * height > PNG_MAX_PIXELS:
        raise ExportError("The picture would be %d x %d pixels: too large, try 1x" % (width, height))
    image = QImage(width, height, QImage.Format_RGB32)
    if image.isNull():
        raise ExportError("Not enough memory for a %d x %d picture, try 1x" % (width, height))
    image.fill(Qt.white)
    painter = QPainter(image)
    try:
        paint_board(painter, objects, source, scale)
    finally:
        painter.end()
    return image


def write_png(path, objects, scale=1):
    """Render the board and save it as a PNG file. Returns (width, height) in pixels."""
    image = render_png(objects, scale)
    if not image.save(path, "PNG"):
        raise ExportError("Could not write %s" % path)
    return image.width(), image.height()


# -- PDF

def fit_into(content, area):
    """Fit a content QSizeF into an area QSizeF keeping the aspect ratio: returns (scale, dx, dy).

    scale is the largest factor at which the content fits (it may enlarge a
    small drawing); dx and dy centre it inside the area.
    """
    if content.width() <= 0 or content.height() <= 0:
        return 1.0, 0.0, 0.0
    scale = min(area.width() / content.width(), area.height() / content.height())
    return scale, (area.width() - content.width() * scale) / 2.0, (area.height() - content.height() * scale) / 2.0


def footer_text(room, when=None):
    """The PDF footer: room code, date and time, and where it came from."""
    stamp = time.strftime("%d %b %Y %H:%M", time.localtime(time.time() if when is None else when))
    return "Room %s  |  %s  |  %s" % (room or "-", stamp, FOOTER_SUFFIX)


def paint_page(painter, page, objects, room=None, when=None, dpi=PDF_DPI):
    """Paint one export page onto `painter`: the board fitted into `page` (a QRectF), the footer line underneath.

    The lowest PDF_FOOTER_MM of the page (at `dpi` pixels per inch) hold the
    footer, the rest holds the board, centred and scaled to fit with the
    aspect ratio kept (fit_into). Separate from write_pdf so the layout can be
    painted on a QImage as well (the tests and the visual check do that).
    """
    footer_h = PDF_FOOTER_MM * dpi / 25.4
    area = QRectF(page.x(), page.y(), page.width(), page.height() - footer_h)
    source = export_rect(objects)
    scale, dx, dy = fit_into(source.size(), area.size())
    painter.save()
    painter.setClipRect(area)
    painter.translate(QPointF(area.left() + dx, area.top() + dy))
    paint_board(painter, objects, source, scale)
    painter.restore()
    font = QFont()
    font.setPointSizeF(8)
    painter.setFont(font)
    painter.setPen(QColor("#555555"))
    painter.drawText(QRectF(page.x(), area.bottom(), page.width(), footer_h), Qt.AlignLeft | Qt.AlignVCenter,
                     footer_text(room, when))


def write_pdf(path, objects, room=None, when=None):
    """Write the board as a one-page A4 landscape PDF (see paint_page for the layout).

    The page has PDF_PAGE_MARGIN_MM margins. The paint device has PDF_DPI
    pixels per inch, so every size in paint_page is in device pixels.
    """
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageOrientation(QPageLayout.Landscape)
    writer.setPageMargins(QMarginsF(PDF_PAGE_MARGIN_MM, PDF_PAGE_MARGIN_MM, PDF_PAGE_MARGIN_MM, PDF_PAGE_MARGIN_MM),
                          QPageLayout.Millimeter)
    writer.setResolution(PDF_DPI)
    writer.setTitle("Whiteboard %s" % (room or ""))
    writer.setCreator("LAN Whiteboard")
    painter = QPainter()
    if not painter.begin(writer):
        raise ExportError("Could not write %s" % path)
    try:
        # The painter's origin of a QPdfWriter is the corner of the PRINTABLE area (inside the
        # margins), so the page rectangle starts at 0, 0 and is width() x height() device pixels.
        # (pageLayout().paintRectPixels() starts at the margin and would shift everything twice.)
        paint_page(painter, QRectF(0, 0, writer.width(), writer.height()), objects, room, when, PDF_DPI)
    finally:
        painter.end()


# -- menu actions

def _objects_of(app):
    """The objects of the board of this client (an empty dict before there is a board)."""
    return app.board.objects if app.board is not None else {}


def _with_suffix(path, suffix):
    """path with `suffix` added when it has no extension of that kind (the dialog does not always add it)."""
    return path if path.lower().endswith(suffix) else path + suffix


def _default_name(app, suffix):
    """File name offered by the save dialogs: whiteboard-<room>.<suffix>."""
    return "whiteboard-%s%s" % (app.me.room or "board", suffix)


def export_png(app):
    """File > Export PNG (Ctrl+E): ask for a file and a scale (1x or 2x filter), render, toast."""
    filters = ";;".join("PNG image, %dx (*.png)" % s for s in PNG_SCALES)
    path, chosen = QFileDialog.getSaveFileName(app.window, "Export PNG", _default_name(app, ".png"), filters)
    if not path:
        return                              # dialog cancelled
    scale = max((s for s in PNG_SCALES if "%dx" % s in chosen), default=PNG_SCALES[0])
    path = _with_suffix(path, ".png")
    try:
        with busy_cursor():
            width, height = write_png(path, _objects_of(app), scale)
    except (ExportError, OSError) as exc:
        app.toast("Could not export: %s" % exc)
        return
    app.toast("Exported PNG (%d x %d px): %s" % (width, height, path))


def export_pdf(app):
    """File > Export PDF: ask for a file, write the one-page PDF, toast."""
    path, _ = QFileDialog.getSaveFileName(app.window, "Export PDF", _default_name(app, ".pdf"), "PDF document (*.pdf)")
    if not path:
        return
    path = _with_suffix(path, ".pdf")
    try:
        with busy_cursor():
            write_pdf(path, _objects_of(app), app.me.room)
    except (ExportError, OSError) as exc:
        app.toast("Could not export: %s" % exc)
        return
    app.toast("Exported PDF: %s" % path)


register_menu_action("File", "Save board as JSON", save_board)
register_menu_action("File", "Load board from JSON", load_board)
register_menu_action("File", "Export PNG", export_png, shortcut="Ctrl+E")
register_menu_action("File", "Export PDF", export_pdf)
