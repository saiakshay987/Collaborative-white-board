# OWNER: P1
"""users_panel.py: Dock panel listing the room's users (registered with register_panel).

One row per entry of app.users (filled from JOIN_OK and USER_LIST by
net_client): a dot in the user's color, the name, the role and whether the
user is online. Offline users (inside the 120 s rejoin window) are shown
greyed out. Double-clicking another user emits app.dm_requested(name),
which P4's chat panel uses to open a direct-message conversation.

The panel never touches the network: it only listens to app.users_changed,
which is emitted on the GUI thread.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.header)
        layout.addWidget(self.list)
        app.users_changed.connect(self.refresh)
        app.role_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        """Rebuild the rows from app.users."""
        self.list.clear()
        online = 0
        for u in self.app.users:
            name = u.get("name", "?")
            is_online = bool(u.get("online"))
            online += is_online
            text = "%s  (%s)" % (name, u.get("role", "?"))
            if name == self.app.me.username:
                text += "  - you"
            if not is_online:
                text += "  - offline"
            item = QListWidgetItem(color_dot(u.get("color"), is_online), text)
            item.setData(Qt.UserRole, name)
            if not is_online:
                item.setForeground(OFFLINE_COLOR)
                font = QFont(item.font())
                font.setItalic(True)
                item.setFont(font)
            self.list.addItem(item)
        self.header.setText("%d online, %d total" % (online, len(self.app.users)))

    def _on_double_click(self, item):
        """Double-click on another user: ask for a DM conversation with them."""
        name = item.data(Qt.UserRole)
        if name and name != self.app.me.username:
            self.app.dm_requested.emit(name)


register_panel("users", UsersPanel, area="right", title="Users")
