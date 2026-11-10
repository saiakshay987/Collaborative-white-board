# OWNER: P3
"""export.py: Save the board to a JSON file and load it back (CONTRACT E7 neighbourhood, section F).

    File > Save board as JSON    every object of the board, written to a file you choose.
                                 Local only: nothing is sent to the server.
    File > Load board from JSON  HOST ONLY. Reads such a file and ADDS its objects to the board.

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
import copy
import json

from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QFileDialog, QMessageBox

from common import models, protocol
from client import ops
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


register_menu_action("File", "Save board as JSON", save_board)
register_menu_action("File", "Load board from JSON", load_board)
