# OWNER: P1
"""users_panel.py: Dock panel listing the room's users (registered with register_panel).

One row per entry of app.users (filled from JOIN_OK and USER_LIST by
net_client): a dot in the user's color, the name, the role and whether the
user is online. Offline users (inside the 120 s rejoin window) are shown
greyed out. Double-clicking another user emits app.dm_requested(name),
which P4's chat panel uses to open a direct-message conversation.

Right-click on a user opens a context menu: "Send direct message", and,
only when I am the host, "Kick" (after a confirmation it sends KICK {user};
the server checks the role again, contract C1).

Online users also show their round-trip time (averaged by the server and
sent in STATS every 2 s), e.g. "alice (host) - 3.2 ms". The text is
colored green, amber or red by the stats panel's thresholds.

The panel only listens to app.users_changed and stats_panel.model.updated,
both emitted on the GUI thread; its one network action is net.send(KICK),
which only queues the message (never blocks).
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QVBoxLayout, QWidget)

from common import protocol
from client import stats_panel
from client.registry import register_panel

DOT_SIZE = 12
OFFLINE_COLOR = QColor("#9e9e9e")


def color_dot(color, online=True):
    """Return a QIcon with a filled circle in color (hollow-looking grey if offline)."""
    pix = QPixmap(DOT_SIZE, DOT_SIZE)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    fill = QColor(color or "#000000")
    if not online:
        fill.setAlpha(80)
    p.setBrush(QBrush(fill))
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, DOT_SIZE - 2, DOT_SIZE - 2)
    p.end()
    return QIcon(pix)


class UsersPanel(QWidget):
    """List of users with color, name, role and online state."""

    def __init__(self, app, parent=None):
        """Build the list and follow app.users_changed."""
        super().__init__(parent)
        self.app = app
        self.header = QLabel()
        self.list = QListWidget()
        self.list.setToolTip("Double-click a user to send a direct message")
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.header)
        layout.addWidget(self.list)
        app.users_changed.connect(self.refresh)
        app.role_changed.connect(self.refresh)
        # STATS arrives every 2 s: only the RTT texts change, so update the
        # rows in place instead of rebuilding them (keeps the selection).
        stats_panel.model.updated.connect(self.update_rtt)
        self.refresh()

    def _row(self, u):
        """Return (text, color or None) for one user dict from app.users."""
        name = u.get("name", "?")
        text = "%s  (%s)" % (name, u.get("role", "?"))
        if name == self.app.me.username:
            text += "  - you"
        if not u.get("online"):
            return text + "  - offline", None
        ms = stats_panel.model.rtt_of(name)
        if ms is None:
            return text, None
        return text + "  - " + stats_panel.fmt_rtt(ms), stats_panel.rtt_color(ms)

    def refresh(self):
        """Rebuild the rows from app.users."""
        self.list.clear()
        online = 0
        for u in self.app.users:
            name = u.get("name", "?")
            is_online = bool(u.get("online"))
            online += is_online
            text, color = self._row(u)
            item = QListWidgetItem(color_dot(u.get("color"), is_online), text)
            item.setData(Qt.UserRole, name)
            if not is_online:
                item.setForeground(OFFLINE_COLOR)
                font = QFont(item.font())
                font.setItalic(True)
                item.setFont(font)
            elif color is not None:
                item.setForeground(color)
            self.list.addItem(item)
        self.header.setText("%d online, %d total" % (online, len(self.app.users)))

    def update_rtt(self):
        """New STATS: refresh the RTT part of each online user's row in place."""
        users = {u.get("name"): u for u in self.app.users}
        for i in range(self.list.count()):
            item = self.list.item(i)
            u = users.get(item.data(Qt.UserRole))
            if u is None or not u.get("online"):
                continue
            text, color = self._row(u)
            item.setText(text)
            if color is not None:
                item.setForeground(color)

    def _on_double_click(self, item):
        """Double-click on another user: ask for a DM conversation with them."""
        name = item.data(Qt.UserRole)
        if name and name != self.app.me.username:
            self.app.dm_requested.emit(name)


    def _on_context_menu(self, pos):
        """Right-click on a row: DM, and Kick for the host (never on myself)."""
        item = self.list.itemAt(pos)
        name = item.data(Qt.UserRole) if item is not None else None
        if not name or name == self.app.me.username:
            return
        menu = QMenu(self)
        dm = menu.addAction("Send direct message")
        kick = menu.addAction("Kick") if self.app.me.role == "host" else None
        chosen = menu.exec_(self.list.viewport().mapToGlobal(pos))
        if chosen is dm:
            self.app.dm_requested.emit(name)
        elif chosen is not None and chosen is kick:
            self.kick(name)

    def kick(self, name):
        """Ask for confirmation, then send KICK {user: name} (host only).

        The server decides: it checks permissions.can(role, "kick") itself
        and answers a refused KICK with ERROR (shown as a toast).
        """
        answer = QMessageBox.question(
            self, "Kick user", "Remove %s from the room?\nThey cannot resume their seat." % name)
        if answer == QMessageBox.Yes and self.app.net is not None:
            if self.app.net.send(protocol.KICK, {"user": name}):
                self.app.toast("Removing %s..." % name)
            else:
                self.app.toast("Not connected.")


register_panel("users", UsersPanel, area="right", title="Users")
