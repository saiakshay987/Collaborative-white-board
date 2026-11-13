# OWNER: P1
"""users_panel.py: Dock panel listing the room's users (registered with register_panel).

One row per entry of app.users (filled from JOIN_OK and USER_LIST by
net_client), painted by UserRowDelegate:

    (AK)  akshay  [editor]                         [3.2 ms]
     |      |        |                                 |
  avatar  name   role pill (crown icon for the host)  RTT chip

The avatar is a circle in the user's colour with their initials (black or
white, whichever is readable on that colour). The RTT chip is green, amber
or red by the stats panel's thresholds (server STATS every 2 s). Offline
users (inside the 120 s rejoin window) are drawn faded with "offline"
instead of an RTT. The panel is only as tall as its rows (preferred_height),
which client/shell.py uses to size the Users dock above Chat.

Double-clicking another user emits app.dm_requested(name), which P4's chat
panel uses to open a direct-message conversation. Right-click on a user opens
a context menu: "Send direct message", and, only when I am the host, "Kick"
(after a confirmation it sends KICK {user}; the server checks the role
again, contract C1).

The panel only listens to app.users_changed, app.role_changed,
stats_panel.model.updated and theme.notifier.changed, all emitted on the GUI
thread; its one network action is net.send(KICK), which only queues the
message (never blocks).
"""

from PyQt5.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt5.QtWidgets import (QAbstractItemView, QLabel, QListWidget, QListWidgetItem, QMenu,
                             QMessageBox, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from common import protocol
from client import icons, stats_panel, theme
from client.registry import register_panel

ROW_HEIGHT = 40          # one user row
AVATAR = 28              # avatar circle diameter
MAX_ROWS = 6             # the panel grows to this many rows, then scrolls
OFFLINE_OPACITY = 0.45   # how faded an offline user is drawn

# Data roles of a list item (besides Qt.UserRole, which holds the name).
USER_ROLE = Qt.UserRole + 1   # the user dict from app.users

# Role -> pill colours (token prefix): host blue, editor neutral, viewer amber.
ROLE_TOKENS = {"host": ("primary_soft", "primary_text"), "editor": ("surface_alt", "text_soft"),
               "viewer": ("warning_soft", "warning_text")}


def initials(name):
    """Return one or two capital letters for an avatar: 'akshay' -> 'AK', 'Mary Ann' -> 'MA'."""
    parts = [p for p in str(name).replace("_", " ").replace("-", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    word = parts[0] if parts else "?"
    return word[:2].upper()


def text_on(color):
    """Black or white, whichever is more readable on color (for avatar initials)."""
    bg = QColor(color)
    black, white = theme.contrast_ratio("#000000", bg), theme.contrast_ratio("#FFFFFF", bg)
    return QColor("#111111") if black >= white else QColor("#FFFFFF")


def rtt_kind(ms):
    """'success', 'warning' or 'danger' for an RTT in ms by the stats thresholds; None if unknown."""
    if ms is None:
        return None
    if ms < stats_panel.GREEN_MS:
        return "success"
    if ms < stats_panel.AMBER_MS:
        return "warning"
    return "danger"


class UserRowDelegate(QStyledItemDelegate):
    """Paints one user row: avatar, name, role pill (crown for the host), RTT chip."""

    def __init__(self, panel):
        """panel: the UsersPanel (for app.me and the stats model)."""
        super().__init__(panel)
        self.panel = panel

    def sizeHint(self, option, index):
        """Every row has the same height."""
        return QSize(option.rect.width(), ROW_HEIGHT)

    @staticmethod
    def _pill(painter, rect, text, bg, fg, font):
        """Draw a rounded pill with text centred in rect."""
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setPen(QColor(fg))
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, text)

    def paint(self, painter, option, index):
        """Draw the row for the user dict stored in the item (see the module doc)."""
        user = index.data(USER_ROLE) or {}
        t = theme.tokens()
        name = str(user.get("name", "?"))
        role = str(user.get("role") or "")
        online = bool(user.get("online"))
        me = name == self.panel.app.me.username
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Background: lighter selection than the default highlight, hover tint otherwise.
        if option.state & QStyle.State_Selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t["primary_soft"]))
            painter.drawRoundedRect(rect, 8, 8)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t["hover"]))
            painter.drawRoundedRect(rect, 8, 8)
        if not online:
            painter.setOpacity(OFFLINE_OPACITY)

        base = QFont(option.font)
        small = QFont(base)
        small.setPointSizeF(theme.SMALL_PT)          # the theme's one small size (pills, 'offline')
        small.setWeight(QFont.DemiBold)

        # Avatar: a circle in the user's colour with the initials.
        x = rect.left() + 4
        circle = QRectF(x, rect.center().y() - AVATAR / 2, AVATAR, AVATAR)
        colour = QColor(user.get("color") or t["muted"])
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(circle)
        painter.setPen(text_on(colour))
        painter.setFont(small)
        painter.drawText(circle, Qt.AlignCenter, initials(name))
        x = circle.right() + 10

        # Right side first (RTT chip or "offline"), so the name knows how much room it has.
        right = rect.right() - 4
        fm_small = QFontMetrics(small)
        if online:
            ms = stats_panel.model.rtt_of(name)
            kind = rtt_kind(ms)
            text = stats_panel.fmt_rtt(ms)
            w = fm_small.horizontalAdvance(text) + 14
            chip = QRectF(right - w, rect.center().y() - 9, w, 18)
            bg, fg = (("%s_soft" % kind, "%s_text" % kind) if kind else ("surface_alt", "muted"))
            self._pill(painter, chip, text, t[bg], t[fg], small)
        else:
            text = "offline"
            w = fm_small.horizontalAdvance(text) + 4
            chip = QRectF(right - w, rect.top(), w, rect.height())
            painter.setPen(QColor(t["muted"]))
            painter.setFont(small)
            painter.drawText(chip, Qt.AlignVCenter | Qt.AlignRight, text)
        limit = chip.left() - 8

        # Role pill (with a crown for the host), placed after the name.
        role_bg, role_fg = ROLE_TOKENS.get(role, ("surface_alt", "text_soft"))
        role_w = fm_small.horizontalAdvance(role) + 14 if role else 0
        crown = 16 if role == "host" else 0
        name_font = QFont(base)
        name_font.setWeight(QFont.DemiBold if me else QFont.Normal)
        name_text = name + ("  (you)" if me else "")
        fm_name = QFontMetrics(name_font)
        room_for_name = max(20.0, limit - x - role_w - crown - 10)
        shown = fm_name.elidedText(name_text, Qt.ElideRight, int(room_for_name))
        painter.setFont(name_font)
        painter.setPen(QColor(t["text"]))
        name_rect = QRectF(x, rect.top(), fm_name.horizontalAdvance(shown) + 2, rect.height())
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, shown)
        x = name_rect.right() + 8
        if crown:
            pix = icons.pixmap("crown", 14, t["warning_text"])
            painter.drawPixmap(int(x), int(rect.center().y() - 7), pix)
            x += crown
        if role:
            pill = QRectF(x, rect.center().y() - 9, role_w, 18)
            self._pill(painter, pill, role, t[role_bg], t[role_fg], small)
        painter.restore()


class UsersPanel(QWidget):
    """List of users with avatar, name, role, host crown, RTT and online state."""

    # Emitted after the rows changed, so client/shell.py can fit the dock height.
    rows_changed = pyqtSignal()

    def __init__(self, app, parent=None):
        """Build the list and follow app.users_changed, the stats model and the theme."""
        super().__init__(parent)
        self.app = app
        self.header = QLabel()
        self.header.setProperty("muted", True)
        self.list = QListWidget()
        self.list.setObjectName("usersList")
        self.list.setItemDelegate(UserRowDelegate(self))
        self.list.setMouseTracking(True)                 # hover highlight
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setToolTip("Double-click a user to send a direct message")
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)
        layout.addWidget(self.header)
        layout.addWidget(self.list)
        app.users_changed.connect(self.refresh)
        app.role_changed.connect(self.refresh)
        # STATS arrives every 2 s: only the RTT texts change, so update the
        # rows in place instead of rebuilding them (keeps the selection).
        stats_panel.model.updated.connect(self.update_rtt)
        theme.notifier.changed.connect(lambda _mode: self.list.viewport().update())
        self.refresh()

    def _row(self, u):
        """Return (text, color or None) for one user dict from app.users.

        The text is the item's plain text (tooltips, accessibility, tests);
        the delegate paints the row itself.
        """
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
            online += bool(u.get("online"))
            text, _color = self._row(u)
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, name)
            item.setData(USER_ROLE, dict(u))
            item.setToolTip(text)
            self.list.addItem(item)
        self.header.setText("%d online, %d total" % (online, len(self.app.users)))
        self.list.setFixedHeight(self.list_height())
        # Never taller than the rows: the dock above Chat stops here and Chat gets the rest.
        self.setMaximumHeight(self.preferred_height())
        self.rows_changed.emit()

    def list_height(self):
        """Height of the list for its rows (at most MAX_ROWS; at least one row)."""
        rows = min(max(1, self.list.count()), MAX_ROWS)
        frame = 2 * self.list.frameWidth()
        return rows * ROW_HEIGHT + frame + 4

    def preferred_height(self):
        """Height the whole panel needs: header, list and margins."""
        m = self.layout().contentsMargins()
        return (self.header.sizeHint().height() + self.layout().spacing() + self.list_height()
                + m.top() + m.bottom())

    def update_rtt(self):
        """New STATS: refresh each online user's row text in place (the delegate repaints it)."""
        users = {u.get("name"): u for u in self.app.users}
        for i in range(self.list.count()):
            item = self.list.item(i)
            u = users.get(item.data(Qt.UserRole))
            if u is None or not u.get("online"):
                continue
            text, _color = self._row(u)
            item.setText(text)
            item.setToolTip(text)
        self.list.viewport().update()

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
        t = theme.tokens()
        menu = QMenu(self)
        dm = menu.addAction(icons.get_icon("chat", 16, t["text_soft"]), "Send direct message")
        kick = (menu.addAction(icons.get_icon("kick", 16, t["danger_text"]), "Kick")
                if self.app.me.role == "host" else None)
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
