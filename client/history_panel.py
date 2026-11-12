# OWNER: P3
"""history_panel.py: Bottom dock panel for the version history, undo/redo and the board checksum (contract C3).

What the panel shows and does:

    list      every version of the room: name, author, time, object count (newest first)
    Save      VERSION_SAVE {name}: asks for a name (default "Version HH:MM:SS"); host and editor
    Restore   VERSION_RESTORE {id}: host only, after a confirmation; the server first saves the
              current board as 'before restore <name>' and then sends a SNAPSHOT to everybody
    Rename    VERSION_RENAME {id, name}: host only; asks for the new name (pre-filled, 1-60 characters)
    Delete    VERSION_DELETE {id}: host only, after a confirmation; the version is gone for everybody
              (Restore, Rename and Delete are also in the right-click menu of a version; all three
              are disabled for editors and viewers, with a tooltip that says why)
    Undo/Redo UNDO / REDO {} through ops.send_op, like Ctrl+Z / Ctrl+Y (host and editor)
    footer    object count, seq and the board checksum of THIS client. When two clients (and the
              server) are at the same seq and nothing is pending, the checksums must be equal:
              looking at two screens proves that every client holds the identical board.

Ctrl+S (File > Save version) saves a version at once with the default name.

The server pushes VERSIONS {versions:[{id, name, by, ts, seq, count}]} to everybody after every
change; the panel asks for the list once after joining (VERSION_LIST), because nothing is pushed
to a client that joins later. Everything runs on the GUI thread: network data arrives only through
the on_message handlers below, which store it in `model` and emit a Qt signal. Buttons only send
messages (NetClient.send never blocks). The role rules here only grey out buttons; the server
checks the role of every request anyway (never trust the client).

The checksum is a sha1 over all objects. It is computed only when the board changed AND none of
my own operations is still waiting for its echo (while operations are pending, the board is
legitimately ahead of the server and a comparison would be meaningless), and at most once per
REFRESH_MS, so drawing on a big board is not slowed down.
"""
import time

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                             QMenu, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from common import protocol
from client import ops
from client.registry import on_message, on_session_reset, register_menu_action, register_panel

REFRESH_MS = 1000                       # footer refresh; also the longest the checksum can lag behind the board
CHECKSUM_SHOWN = 12                     # hex characters of the sha1 that are displayed
COLUMNS = ("Name", "By", "Time", "Objects")
EDIT_ROLES = ("host", "editor")         # may save versions, undo and redo (contract C4)
MAX_NAME_LEN = 60                       # longest version name the server accepts for a rename (contract C3)
HOST_ONLY_TIP = "Only the host can %s a version"   # tooltip of a disabled button / menu entry
AUTO_NAME_RESTORE = "before restore "   # the names the server gives to its automatic versions
AUTO_NAME_CLEAR = "before clear"


# ---------------------------------------------------------------- small pure helpers

def format_time(ts, now=None):
    """Text for a version's time: HH:MM:SS today, 'DD Mon HH:MM' on another day, '-' when unknown."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "-"
    now = time.time() if now is None else now
    when = time.localtime(ts)
    if time.strftime("%Y%m%d", when) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M:%S", when)
    return time.strftime("%d %b %H:%M", when)


def default_name(now=None):
    """The name Ctrl+S and the dialog suggest: 'Version HH:MM:SS'."""
    return "Version " + time.strftime("%H:%M:%S", time.localtime(time.time() if now is None else now))


def footer_text(count, seq, checksum, pending):
    """The footer line. checksum is None while it is not meaningful (operations pending)."""
    if pending:
        return "Objects: %d   |   seq %d   |   checksum: waiting for %d pending operation(s)" % (count, seq, pending)
    return "Objects: %d   |   seq %d   |   checksum: %s" % (count, seq, (checksum or "-")[:CHECKSUM_SHOWN])


def clean_versions(payload):
    """The well-formed entries of a VERSIONS payload, in the server's order (oldest first).

    The server is trusted less than it looks: an entry without an integer id
    or a string name cannot be restored or shown, so it is skipped; the other
    fields fall back to harmless defaults.
    """
    items = payload.get("versions") if isinstance(payload, dict) else None
    result = []
    for v in items if isinstance(items, list) else []:
        if isinstance(v, dict) and isinstance(v.get("id"), int) and not isinstance(v["id"], bool) \
                and isinstance(v.get("name"), str):
            result.append({"id": v["id"], "name": v["name"], "by": str(v.get("by") or "?"),
                           "ts": v.get("ts"), "seq": v.get("seq"), "count": v.get("count")})
    return result


# ---------------------------------------------------------------- the list of versions

class VersionsModel(QObject):
    """The latest VERSIONS list of the server (GUI thread only).

    versions  list of {id, name, by, ts, seq, count}, oldest first
    known     False until the first list after joining arrived (no toasts for what was already there)
    updated   signal emitted after every change
    """

    updated = pyqtSignal()

    def __init__(self, parent=None):
        """Start empty."""
        super().__init__(parent)
        self.versions = []
        self.known = False

    def reset(self):
        """Forget everything (a new JOIN_OK: possibly another room)."""
        self.versions = []
        self.known = False
        self.updated.emit()

    def apply(self, payload):
        """Store a VERSIONS payload; returns the entries that are new since the last list."""
        old_ids = {v["id"] for v in self.versions}
        self.versions = clean_versions(payload)
        new = [v for v in self.versions if v["id"] not in old_ids] if self.known else []
        self.known = True
        self.updated.emit()
        return new


# One model per client process; a plain QObject, so it may exist before the QApplication does.
model = VersionsModel()


def announce(app, new_versions):
    """Toast the new versions that matter: my own saves, a restore by anybody, a clear."""
    me = app.me.username
    for v in new_versions:
        if v["name"].startswith(AUTO_NAME_RESTORE):
            app.toast("%s restored a version; the board before it is saved as '%s'" % (v["by"], v["name"]))
        elif v["name"] == AUTO_NAME_CLEAR:
            app.toast("Board cleared by %s; saved as version '%s'" % (v["by"], v["name"]))
        elif v["by"] == me:
            app.toast("Version saved: %s" % v["name"])


@on_message(protocol.VERSIONS)
def _on_versions(app, msg):
    """VERSIONS {versions}: store the list and announce what is new (runs on the GUI thread)."""
    announce(app, model.apply(msg.get("payload") or {}))


@on_session_reset
def _on_session_reset(app):
    """The user left the room: forget the version list (the panel shows it empty)."""
    model.reset()


@on_message(protocol.JOIN_OK)
def _on_join_ok(app, msg):
    """JOIN_OK (also after a reconnect): forget the old list and ask for the room's list.

    The server pushes VERSIONS only when something changes, so a client that
    joins later has to ask once (VERSION_LIST). net_client's own JOIN_OK
    handler ran first, so the connection is ready for sending.
    """
    model.reset()
    if app.net is not None:
        app.net.send(protocol.VERSION_LIST, {})


# ---------------------------------------------------------------- actions (shared by the panel and Ctrl+S)

def can_edit(app):
    """True if my role may save versions, undo and redo (the server decides again)."""
    return app.me.role in EDIT_ROLES


def save_version(app, name=None):
    """Send VERSION_SAVE with `name` (default 'Version HH:MM:SS'). Returns True if it was sent."""
    if not can_edit(app):
        app.toast("Viewers cannot save versions")
        return False
    if app.net is None or not app.net.send(protocol.VERSION_SAVE, {"name": name or default_name()}):
        app.toast("Not connected")
        return False
    return True


def save_version_quick(app):
    """File > Save version / Ctrl+S: save at once, without asking for a name."""
    save_version(app)


def restore_version(app, version):
    """Send VERSION_RESTORE for one entry of the list (host only). Returns True if it was sent."""
    if app.me.role != "host":
        app.toast("Only the host can restore a version")
        return False
    if app.net is None or not app.net.send(protocol.VERSION_RESTORE, {"id": version["id"]}):
        app.toast("Not connected")
        return False
    return True


def rename_version(app, version, name):
    """Send VERSION_RENAME for one entry (host only). Returns True if it was sent.

    The name is trimmed here; an empty one, one longer than MAX_NAME_LEN, or one
    that did not change is not sent (the server would refuse the first two).
    """
    if app.me.role != "host":
        app.toast(HOST_ONLY_TIP % "rename")
        return False
    name = (name or "").strip()
    if not name or name == version["name"]:
        return False
    if len(name) > MAX_NAME_LEN:
        app.toast("A version name can have at most %d characters" % MAX_NAME_LEN)
        return False
    if app.net is None or not app.net.send(protocol.VERSION_RENAME, {"id": version["id"], "name": name}):
        app.toast("Not connected")
        return False
    return True


def delete_version(app, version):
    """Send VERSION_DELETE for one entry (host only). Returns True if it was sent."""
    if app.me.role != "host":
        app.toast(HOST_ONLY_TIP % "delete")
        return False
    if app.net is None or not app.net.send(protocol.VERSION_DELETE, {"id": version["id"]}):
        app.toast("Not connected")
        return False
    return True


def history_step(app, type_):
    """UNDO or REDO through ops.send_op, the same path as Ctrl+Z / Ctrl+Y (the server answers with OP_* messages)."""
    if not can_edit(app):
        app.toast("Viewers cannot undo or redo")
        return
    ops.send_op(app, type_, {})


# ---------------------------------------------------------------- the panel

class HistoryPanel(QWidget):
    """Version list, the four buttons and the footer with the object count and the board checksum."""

    def __init__(self, app, parent=None):
        """Build the widgets, connect the signals and start the footer timer."""
        super().__init__(parent)
        self.app = app
        self._dirty = True              # the board changed since the checksum was computed
        self._checksum = None

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(90)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self.save_btn = QPushButton("Save version")
        self.save_btn.setToolTip("Store a copy of the board (Ctrl+S saves with a default name)")
        self.restore_btn = QPushButton("Restore")
        self.rename_btn = QPushButton("Rename")
        self.delete_btn = QPushButton("Delete")
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setToolTip("Undo my last change (Ctrl+Z)")
        self.redo_btn = QPushButton("Redo")
        self.redo_btn.setToolTip("Redo what I undid (Ctrl+Y)")
        self.footer = QLabel()
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.footer.setFont(font)
        self.footer.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.footer.setToolTip("Same seq and no pending operations on two clients: the checksums must be equal")

        buttons = QVBoxLayout()
        for button in (self.save_btn, self.restore_btn, self.rename_btn, self.delete_btn, self.undo_btn, self.redo_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.table, 1)
        top.addLayout(buttons)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addLayout(top, 1)
        layout.addWidget(self.footer)

        self.save_btn.clicked.connect(self.on_save)
        self.restore_btn.clicked.connect(self.on_restore)
        self.rename_btn.clicked.connect(lambda: self.on_rename())
        self.delete_btn.clicked.connect(lambda: self.on_delete())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_context_menu)
        self.undo_btn.clicked.connect(lambda: history_step(app, protocol.UNDO))
        self.redo_btn.clicked.connect(lambda: history_step(app, protocol.REDO))
        self.table.itemSelectionChanged.connect(self.refresh_buttons)
        model.updated.connect(self.refresh_list)
        app.role_changed.connect(self.refresh_buttons)
        if app.board is not None:
            app.board.changed.connect(self._board_changed)
        # A QTimer runs in the GUI event loop: refreshing never blocks and needs no locking.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_footer)
        self._timer.start(REFRESH_MS)
        self.refresh_list()
        self.refresh_footer()

    # -- list ------------------------------------------------------------------

    def selected_version(self):
        """The entry of the selected row, or None."""
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        vid = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((v for v in model.versions if v["id"] == vid), None)

    def refresh_list(self):
        """Fill the table from model (newest first) and keep the selected version selected."""
        keep = self.selected_version()
        self.table.setRowCount(0)
        for v in reversed(model.versions):
            row = self.table.rowCount()
            self.table.insertRow(row)
            count = v["count"] if isinstance(v["count"], int) and not isinstance(v["count"], bool) else "?"
            for col, text in enumerate((v["name"], v["by"], format_time(v["ts"]), str(count))):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, v["id"])
                if col == 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
            if keep is not None and keep["id"] == v["id"]:
                self.table.selectRow(row)
        self.refresh_buttons()

    def refresh_buttons(self):
        """Enable the buttons the role may use (the server checks again)."""
        edit = can_edit(self.app)
        self.save_btn.setEnabled(edit)
        self.undo_btn.setEnabled(edit)
        self.redo_btn.setEnabled(edit)
        host = self.app.me.role == "host"
        chosen = self.selected_version() is not None
        for button, verb in ((self.restore_btn, "restore"), (self.rename_btn, "rename"), (self.delete_btn, "delete")):
            button.setEnabled(host and chosen)
            button.setToolTip(self.action_tip(verb, host))

    @staticmethod
    def action_tip(verb, host):
        """Tooltip of the Restore / Rename / Delete button or menu entry: what it does, or why it is disabled."""
        if not host:
            return HOST_ONLY_TIP % verb
        return {"restore": "Replace the board by the selected version. The current board is saved first.",
                "rename": "Give the selected version another name",
                "delete": "Delete the selected version for everybody"}[verb]

    # -- buttons ---------------------------------------------------------------

    def on_save(self):
        """Save version: ask for a name, then send VERSION_SAVE."""
        name, ok = QInputDialog.getText(self, "Save version", "Name of the version:", text=default_name())
        if ok and name.strip():
            save_version(self.app, name.strip())

    def on_restore(self):
        """Restore: confirm, then send VERSION_RESTORE for the selected version."""
        version = self.selected_version()
        if version is None:
            return
        answer = QMessageBox.question(
            self, "Restore version",
            "Replace the board by version '%s' (%s objects)?\n\nThe current board is saved first as '%s%s', "
            "so this can be reversed." % (version["name"], version["count"], AUTO_NAME_RESTORE, version["name"]))
        if answer == QMessageBox.Yes:
            restore_version(self.app, version)

    def on_rename(self, version=None):
        """Rename: ask for the new name (pre-filled with the old one), then send VERSION_RENAME."""
        version = version if isinstance(version, dict) else self.selected_version()
        if version is None:
            return
        name, ok = QInputDialog.getText(self, "Rename version", "New name (1-%d characters):" % MAX_NAME_LEN,
                                        text=version["name"])
        if ok:
            rename_version(self.app, version, name)

    def on_delete(self, version=None):
        """Delete: confirm, then send VERSION_DELETE for the selected version."""
        version = version if isinstance(version, dict) else self.selected_version()
        if version is None:
            return
        answer = QMessageBox.question(
            self, "Delete version",
            "Delete version '%s'?\n\nThis removes it for everybody and cannot be undone." % version["name"])
        if answer == QMessageBox.Yes:
            delete_version(self.app, version)

    def on_context_menu(self, pos):
        """Right-click on a version: select it and show Restore / Rename / Delete (disabled, with a tooltip, unless host)."""
        item = self.table.itemAt(pos)
        if item is None:
            return
        self.table.selectRow(item.row())
        version = self.selected_version()
        if version is None:
            return
        host = self.app.me.role == "host"
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for verb, slot in (("restore", self.on_restore), ("rename", self.on_rename), ("delete", self.on_delete)):
            action = menu.addAction(verb.capitalize())
            action.setEnabled(host)
            action.setToolTip(self.action_tip(verb, host))
            action.triggered.connect(lambda _checked=False, s=slot: s())
        self.context_menu = menu                # kept so a test (or a debugger) can look at it
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    # -- footer ----------------------------------------------------------------

    def _board_changed(self, _obj_id):
        """BoardModel.changed: the checksum is out of date (it is recomputed at the next footer refresh)."""
        self._dirty = True

    def refresh_footer(self):
        """Update the footer: count, seq, and the checksum (recomputed only when changed and nothing is pending)."""
        board = self.app.board
        if board is None:
            self.footer.setText("Objects: 0")
            return
        pending = len(board.pending)
        if not pending and (self._dirty or self._checksum is None):
            self._checksum = board.checksum()
            self._dirty = False
        self.footer.setText(footer_text(len(board.objects), board.last_seq, self._checksum, pending))


register_panel("history", HistoryPanel, area="bottom", title="History")
register_menu_action("File", "Save version", save_version_quick, shortcut="Ctrl+S")
