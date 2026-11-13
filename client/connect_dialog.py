"""connect_dialog.py: The dialog shown at start-up: host a new room or join one.

Two tabs:
    Host: username, password (optional). Connects to the server on this
          machine (127.0.0.1) and sends CREATE_ROOM. On success it shows the
          room code and ALL of this machine's IPv4 addresses (the likely
          Wi-Fi / hotspot one first, in large text), so the host can read
          them out to the lab, then "Open whiteboard".
    Join: server IP (or ip:port), room code, username, password. The last
          server IP and username are remembered (QSettings). "Test
          connection" only tries a TCP connect and reports the result.
          "Find rooms" (and an automatic refresh every 5 s while this tab is
          open) lists the rooms the server knows: code, host, online/total,
          objects, a lock for password rooms and when they last changed.
          Double-click a row to fill the room code (and ask for the password
          of a locked room). Sends JOIN_ROOM and opens the whiteboard on JOIN_OK.

Flow (nothing here blocks the GUI thread):

    click  -> net.connect_to(ip)  (background thread)
           -> NetClient.connected  -> send CREATE_ROOM / JOIN_ROOM
           -> JOIN_OK  (on_message handler below)  -> success
           -> ERROR    (on_message handler below)  -> friendly message,
                                                      connection kept for a retry

The ERROR handler also turns errors that arrive later (FORBIDDEN, ...) into
a toast when no dialog is open.
"""

import html
import threading
import time

from PyQt5.QtCore import QEvent, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFormLayout,
                             QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                             QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem,
                             QTabWidget, QToolButton, QVBoxLayout, QWidget)

from common import config, netinfo, protocol
from client import icons, net_client, theme
from client.registry import on_message

# ERROR codes (contract B) shown as plain-language messages.
FRIENDLY_ERRORS = {
    protocol.BAD_PASSWORD: "Wrong password for this room.",
    protocol.NAME_TAKEN: "Someone in this room already uses that name. Pick another one.",
    protocol.NO_ROOM: "No room with that code. Check the code with the host.",
    protocol.BAD_FORMAT: "The server did not accept that request.",
    protocol.FORBIDDEN: "Your role does not allow that.",
    protocol.TOO_LARGE: "That is too large to send.",
    protocol.NOT_FOUND: "Nothing to undo or redo, or the item no longer exists.",
}

# Shown under the error when a Join attempt cannot reach the host. ERRORs
# the server sends back (wrong password, ...) do not get it: those prove the
# network works. %d is the TCP port.
JOIN_NETWORK_HINT = ("Check: same Wi-Fi as the host? Host's firewall allows TCP %d? "
                     "Try the host's other IP.")

# QSettings location and keys for the remembered Join fields. The organisation
# and application names are the same ones main_window.py uses for the window
# geometry, so everything lands in one settings file per user.
SETTINGS_ORG = "CN Lab"
SETTINGS_APP = "LAN Whiteboard"
KEY_LAST_IP = "join/server_ip"
KEY_LAST_NAME = "join/username"

# Room list (Revision 5): how often the Join tab asks the server again, and the table columns.
ROOM_REFRESH_MS = 5000
ROOM_COLUMNS = ["Room", "Host", "Online", "Objects", "Lock", "Updated"]
LOCK_ICON = "Locked"        # Lock cell text of a password room, shown next to a painted padlock
LOCK_COLUMN = ROOM_COLUMNS.index("Lock")
# Columns sized to their contents (the rest share the width), so "10 Nov 14:05" is never cut.
FIT_COLUMNS = ("Room", "Online", "Objects", "Lock", "Updated")

# The dialog currently waiting for JOIN_OK / ERROR, if any. The on_message
# handlers below are registered once at import time and forward to it.
_active = None


def friendly_error(payload):
    """Return a user-facing sentence for an ERROR payload {code, text, ref_type}."""
    code = payload.get("code")
    return FRIENDLY_ERRORS.get(code) or payload.get("text") or "Error: %s" % code


def parse_server(text):
    """Split what the user typed in "Server IP" into (host, port).

    "192.168.43.57" -> ("192.168.43.57", 5050); "192.168.43.57:6000" uses
    port 6000 (for a server started with --port). A bad port number falls
    back to the default so the connect error, not a crash, tells the user.
    """
    text = text.strip()
    host, sep, port = text.rpartition(":")
    if sep and host and port.isdigit() and 0 < int(port) < 65536:
        return host.strip(), int(port)
    return text, config.PORT


def format_updated(ts, now=None):
    """Return "just now", "5 min ago", "3 h ago" or a date for the epoch time ts of a room's last change."""
    now = time.time() if now is None else now
    try:
        age = max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return "?"
    if age < 60:
        return "just now"
    if age < 3600:
        return "%d min ago" % (age // 60)
    if age < 86400:
        return "%d h ago" % (age // 3600)
    return time.strftime("%d %b %H:%M", time.localtime(ts))


def room_cells(row, now=None):
    """Turn one ROOM_LIST row into the six display strings of the table (see ROOM_COLUMNS).

    The server is trusted for the shape but not for the types: a missing or odd
    field shows as a dash instead of raising on the GUI thread.
    """
    def num(key):
        """The row's field as an int, or 0."""
        value = row.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    host = row.get("host")
    return [str(row.get("code", "")), host if isinstance(host, str) and host else "-",
            "%d/%d" % (num("online"), num("members")), str(num("objects")),
            LOCK_ICON if row.get("locked") else "", format_updated(row.get("updated"), now)]


class ConnectDialog(QDialog):
    """Host-or-join dialog. exec_() returns Accepted once we are in a room."""

    # Worker threads report back through signals (queued onto the GUI thread):
    # widgets are only ever touched on the GUI thread.
    _addresses_ready = pyqtSignal(object)        # list of netinfo.Address
    _test_done = pyqtSignal(bool, str, float)    # ok, message, seconds
    _rooms_ready = pyqtSignal(str, object, str)  # "host:port" asked, list of rows (or None), error text

    def __init__(self, app, parent=None, notice=None, prefill=None):
        """Build the tabs for AppState app (app.net must be a NetClient).

        notice: optional text shown under the form when the dialog opens,
        e.g. why we are back here (kicked, connection lost, left the room).
        prefill: optional dict {server, room, username} of the session we
        just left; the Join tab is filled with it and opened, so rejoining
        the same room is one click (the password is never remembered).
        """
        super().__init__(parent)
        self.app = app
        self.net = app.net
        self._pending = None   # (mode, host, envelope type, payload) of the running attempt
        self.setWindowTitle("LAN Whiteboard: connect")
        self.setObjectName("connectDialog")
        self.setMinimumWidth(580)          # the six room-list columns fit without scrolling

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_form_page())
        self.pages.addWidget(self._build_ready_page())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(self.pages)

        self.net.connected.connect(self._on_connected)
        self.net.disconnected.connect(self._on_disconnected)
        self._addresses_ready.connect(self._show_addresses)
        self._test_done.connect(self._show_test_result)
        self._rooms_ready.connect(self._show_rooms)
        self._rooms_busy = False                  # a room-list request is in flight
        self._rooms_again = None                  # request asked for while busy: "manual", "auto" or None
        self._room_timer = QTimer(self)           # auto-refresh; stopped in done()
        self._room_timer.timeout.connect(self._auto_refresh)
        self._room_timer.start(ROOM_REFRESH_MS)
        self.tabs.currentChanged.connect(lambda _i: self._auto_refresh())
        self._load_remembered()
        if prefill:
            self._apply_prefill(prefill)
        if notice:
            self._show_notice(notice)
            self.tabs.setCurrentIndex(1)    # a user who left would join again, not host
        self._focus_first_field()
        QTimer.singleShot(0, self._auto_refresh)    # list the rooms of the remembered server at once

    # ----------------------------------------------------------------- layout

    def _build_form_page(self):
        """Page 0: the landing card: app name, Host/Join switch, the two forms, message, buttons.

        Host and Join are the two pages of self.tabs (a QTabWidget whose tab bar
        is hidden); the big segmented switch above it changes the page, and
        everything that reads self.tabs.currentIndex() works as before.
        """
        page = QWidget()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("connectTabs")
        self.tabs.tabBar().hide()
        # The tab bar is hidden, so the tab widget itself must not take focus: it
        # would hand it straight back to the switch and Tab could never leave it.
        self.tabs.setFocusPolicy(Qt.NoFocus)

        host_tab = QWidget()
        form = self._form(host_tab)
        self.host_name = QLineEdit(placeholderText="e.g. Sharwan")
        self.host_pw = QLineEdit(echoMode=QLineEdit.Password,
                                 placeholderText="Optional: leave empty for an open room")
        form.addRow("Your name", self.host_name)
        form.addRow("Room password", self.host_pw)
        hint = QLabel("Start the server on this PC first: python run_server.py")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        form.addRow(hint)
        self.tabs.addTab(host_tab, "Host")

        join_tab = QWidget()
        form = self._form(join_tab)
        self.join_ip = QLineEdit(placeholderText="e.g. 192.168.43.57 (on the host's screen)")
        self.join_code = QLineEdit(placeholderText="6 letters, e.g. K7QX2M")
        self.join_code.setMaxLength(6)
        self.join_code.setObjectName("roomCodeInput")
        self.join_name = QLineEdit(placeholderText="e.g. Akshay")
        self.join_pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="Only if the room has one")
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setToolTip("Only tries to open a TCP connection to the server; joins nothing.")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        self.find_btn = QPushButton("Find rooms")
        self.find_btn.setToolTip("Ask the server which rooms it has (refreshes by itself every 5 s).")
        self.find_btn.clicked.connect(lambda: self._find_rooms(manual=True))
        # The buttons sit under the address field, so nothing is squeezed at 150 % scaling.
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(self.test_btn)
        actions.addWidget(self.find_btn)
        actions.addStretch(1)
        form.addRow("Server address", self.join_ip)
        form.addRow("", actions)
        form.addRow("", self.test_result)
        self.rooms_table = QTableWidget(0, len(ROOM_COLUMNS))
        self.rooms_table.setObjectName("roomsTable")
        self.rooms_table.setHorizontalHeaderLabels(ROOM_COLUMNS)
        self.rooms_table.verticalHeader().setVisible(False)
        self.rooms_table.verticalHeader().setDefaultSectionSize(30)
        self.rooms_table.setShowGrid(False)
        self.rooms_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rooms_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rooms_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.rooms_table.setMinimumHeight(96)
        header = self.rooms_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        for name in FIT_COLUMNS:
            header.setSectionResizeMode(ROOM_COLUMNS.index(name), QHeaderView.ResizeToContents)
        self.rooms_table.setToolTip("Double-click a room to fill in its code.")
        self.rooms_table.cellDoubleClicked.connect(self._room_chosen)
        self.rooms_status = QLabel("Enter the server address to see its rooms.")
        self.rooms_status.setWordWrap(True)
        self.rooms_status.setProperty("muted", True)
        form.addRow(self.rooms_table)
        form.addRow(self.rooms_status)
        form.addRow("Room code", self.join_code)
        form.addRow("Your name", self.join_name)
        form.addRow("Password", self.join_pw)
        self.tabs.addTab(join_tab, "Join")

        # Header: app name and tagline.
        title = QLabel("LAN Whiteboard")
        title.setObjectName("connectTitle")
        tagline = QLabel("Draw, write and chat together on your local network.")
        tagline.setProperty("muted", True)

        # The big two-way switch; it only changes the page of self.tabs.
        switch = QFrame()
        switch.setObjectName("segmented")
        row = QHBoxLayout(switch)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)
        self._segments = QButtonGroup(self)
        self._segments.setExclusive(True)
        for index, (text, icon, tip) in enumerate((
                ("Host a room", "crown", "Start a new room on this PC"),
                ("Join a room", "users", "Join a room another PC is hosting"))):
            btn = QPushButton(text)
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setAutoDefault(False)
            btn.setToolTip(tip)
            btn.setProperty("iconName", icon)
            btn.setIconSize(QSize(theme.ICON_TEXT, theme.ICON_TEXT))
            btn.clicked.connect(lambda _checked, i=index: self.tabs.setCurrentIndex(i))
            btn.installEventFilter(self)          # Left / Right switch Host and Join
            self._segments.addButton(btn, index)
            row.addWidget(btn, 1)
        self.tabs.currentChanged.connect(self._sync_switch)
        self._sync_switch(self.tabs.currentIndex())

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)
        self.status.hide()
        self.go_btn = QPushButton("Connect")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._start)
        cancel = QPushButton("Cancel")
        cancel.setProperty("flat", True)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.go_btn)

        card = QFrame()
        card.setObjectName("connectCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(tagline)
        layout.addSpacing(4)
        layout.addWidget(switch)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        return page

    @staticmethod
    def _form(parent):
        """A form layout with labels on the left, fields stretching, friendly spacing."""
        form = QFormLayout(parent)
        form.setContentsMargins(0, 4, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return form

    def _page_fields(self):
        """The text fields of the visible page, top to bottom."""
        if self.tabs.currentIndex() == 0:
            return [self.host_name, self.host_pw]
        return [self.join_ip, self.join_code, self.join_name, self.join_pw]

    def _focus_first_field(self):
        """Put the cursor in the first empty field (the password when all others are filled)."""
        fields = self._page_fields()
        empty = [f for f in fields[:-1] if not f.text().strip()]
        (empty[0] if empty else fields[-1]).setFocus()

    def eventFilter(self, obj, event):
        """Left / Right on the Host-Join switch change the page, like a segmented control.

        Tab reaches the switch once (Qt gives Tab focus only to the checked
        button of an exclusive group); the arrows then pick the other page.
        """
        if (event.type() == QEvent.KeyPress and obj in self._segments.buttons()
                and event.key() in (Qt.Key_Left, Qt.Key_Right)):
            index = 0 if event.key() == Qt.Key_Left else 1
            self.tabs.setCurrentIndex(index)
            self._segments.button(index).setFocus()
            return True
        return super().eventFilter(obj, event)

    def _sync_switch(self, index):
        """Check the switch button of the visible page and paint its icons for the theme."""
        t = theme.tokens()
        for button in self._segments.buttons():
            i = self._segments.id(button)
            button.setChecked(i == index)
            colour = t["primary_text"] if i == index else t["text_soft"]
            button.setIcon(icons.get_icon(button.property("iconName"), theme.ICON_TEXT, colour))

    def _build_ready_page(self):
        """Page 1 (host only): room code and the server's IPs in large, readable text.

        The likely Wi-Fi / hotspot IP is big; the other usable addresses of
        this PC are listed small below it, with their adapter names, in case
        the big one is the wrong network.
        """
        page = QWidget()
        self.code_label = QLabel()
        self.code_label.setObjectName("roomCode")
        self.ip_label = QLabel("Looking up addresses...")
        self.ip_label.setObjectName("serverIp")
        code_font = QFont()
        code_font.setPointSize(theme.HERO_PT)
        code_font.setBold(True)
        code_font.setLetterSpacing(QFont.AbsoluteSpacing, 4)
        self.code_label.setFont(code_font)
        ip_font = QFont()
        ip_font.setPointSize(theme.HEADLINE_PT)
        ip_font.setBold(True)
        self.ip_label.setFont(ip_font)
        for label in (self.code_label, self.ip_label):
            label.setAlignment(Qt.AlignCenter)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.other_ips_label = QLabel()
        self.other_ips_label.setAlignment(Qt.AlignCenter)
        self.other_ips_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.other_ips_label.setProperty("muted", True)
        self.other_ips_label.setWordWrap(True)
        self.copy_code_btn = QToolButton()
        self.copy_code_btn.setObjectName("copyCode")
        self.copy_code_btn.setToolTip("Copy the room code")
        self.copy_code_btn.setIconSize(QSize(20, 20))
        self.copy_code_btn.setIcon(icons.get_icon("copy", 20, theme.tokens()["text_soft"]))
        self.copy_code_btn.clicked.connect(
            lambda: self._copy(self.code_label.text(), "Room code copied"))
        self.copy_btn = QPushButton("Copy code and IP")
        self.copy_btn.clicked.connect(lambda: self._copy(
            "Room %s at %s" % (self.code_label.text(), self.ip_label.text()), "Code and IP copied"))
        self.copied = QLabel()
        self.copied.setProperty("muted", True)
        open_btn = QPushButton("Open whiteboard")
        open_btn.setDefault(True)
        open_btn.clicked.connect(self.accept)

        heading = QLabel("Your room is ready")
        heading.setObjectName("connectTitle")
        sub = QLabel("Read the code and the address out to the lab; they type them in Join.")
        sub.setProperty("muted", True)
        sub.setWordWrap(True)

        code_card = QFrame()
        code_card.setProperty("card", True)
        code_box = QVBoxLayout(code_card)
        code_box.setContentsMargins(16, 10, 16, 12)
        code_title = QLabel("Room code")
        code_title.setProperty("muted", True)
        code_title.setAlignment(Qt.AlignCenter)
        code_row = QHBoxLayout()
        code_row.addStretch(1)
        code_row.addWidget(self.code_label)
        code_row.addWidget(self.copy_code_btn, 0, Qt.AlignVCenter)
        code_row.addStretch(1)
        code_box.addWidget(code_title)
        code_box.addLayout(code_row)

        ip_card = QFrame()
        ip_card.setProperty("card", True)
        ip_box = QVBoxLayout(ip_card)
        ip_box.setContentsMargins(16, 10, 16, 12)
        ip_title = QLabel("Server address (others type this in Join)")
        ip_title.setProperty("muted", True)
        ip_title.setAlignment(Qt.AlignCenter)
        ip_box.addWidget(ip_title)
        ip_box.addWidget(self.ip_label)
        ip_box.addWidget(self.other_ips_label)

        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.copied)
        buttons.addStretch(1)
        buttons.addWidget(open_btn)

        card = QFrame()
        card.setObjectName("connectCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(sub)
        layout.addWidget(code_card)
        layout.addWidget(ip_card)
        layout.addStretch(1)                  # the page is as tall as the Join form: pack at the top
        layout.addLayout(buttons)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        return page

    def _copy(self, text, done):
        """Put text on the clipboard and say so next to the buttons for two seconds."""
        QApplication.clipboard().setText(text)
        self.copied.setText(done)
        QTimer.singleShot(2000, lambda: self.copied.setText(""))

    # ---------------------------------------------------------------- messages

    @staticmethod
    def _tone(label, kind, box=False):
        """Colour a message label: kind is 'error', 'warning', 'success' or None (plain).

        box=True draws it as a soft coloured box (the main message under the
        form); otherwise only the text colour changes (inline results).
        """
        prop = "tone" if box else "toneText"
        if box:
            # Margins, not QSS padding: a word-wrapped QLabel measures its height
            # without the style sheet's padding, which would squeeze the text.
            label.setContentsMargins(*((12, 8, 12, 8) if kind else (0, 0, 0, 0)))
        if label.property(prop) != (kind or ""):
            label.setProperty(prop, kind or "")
            theme.repolish(label)

    @staticmethod
    def _message_html(text):
        """First line of a message in bold, the rest (advice) smaller below it."""
        head, _sep, rest = str(text).strip().partition("\n")
        rest = " ".join(line.strip() for line in rest.splitlines() if line.strip())
        out = "<b>%s</b>" % html.escape(head)
        if rest:
            out += '<br><span style="font-size:9pt">%s</span>' % html.escape(rest)
        return out

    # ------------------------------------------------------------ remembered

    def _load_remembered(self):
        """Prefill the Join tab with the last server IP and username (QSettings).

        If nothing was stored yet (or the settings store is unusable) the
        fields simply stay empty.
        """
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.join_ip.setText(str(settings.value(KEY_LAST_IP, "") or ""))
        self.join_name.setText(str(settings.value(KEY_LAST_NAME, "") or ""))

    def _apply_prefill(self, prefill):
        """Fill the Join tab with the last session's server, room code and username.

        Empty values leave what _load_remembered put there, and the Join tab
        is shown because that is where the user can get back into the room.
        """
        if prefill.get("server"):
            self.join_ip.setText(prefill["server"])
        if prefill.get("room"):
            self.join_code.setText(prefill["room"])
        if prefill.get("username"):
            self.join_name.setText(prefill["username"])
        self.tabs.setCurrentIndex(1)

    def _remember(self, ip, name):
        """Store the Join fields for next time."""
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.setValue(KEY_LAST_IP, ip)
        settings.setValue(KEY_LAST_NAME, name)

    # --------------------------------------------------------- test connection

    def _test_connection(self):
        """Test-connection button: TCP connect on a worker thread, show the result."""
        host, port = parse_server(self.join_ip.text())
        if not host:
            self._tone(self.test_result, "error")
            self.test_result.setText("Enter the server address first.")
            return
        self.test_btn.setEnabled(False)
        self._tone(self.test_result, None)
        self.test_result.setText("Testing %s:%d ..." % (host, port))
        # The connect can take up to CONNECT_TIMEOUT_S seconds: never on the GUI thread.
        threading.Thread(target=self._test_worker, args=(host, port),
                         name="connect-test", daemon=True).start()

    def _test_worker(self, host, port):
        """Thread: probe the server and report through the _test_done signal."""
        ok, message, seconds = net_client.probe_connection(host, port)
        try:
            self._test_done.emit(ok, message, seconds)
        except RuntimeError:
            pass  # the dialog was closed while we were testing

    def _show_test_result(self, ok, message, seconds):
        """GUI thread: show the outcome of the connection test."""
        self.test_btn.setEnabled(True)
        self._tone(self.test_result, "success" if ok else "error")
        self.test_result.setText("%s (%.0f ms)" % (message, seconds * 1000) if ok else message)

    # ------------------------------------------------------------- room list

    def _auto_refresh(self):
        """Timer / tab-change slot: refresh the list, but only while it can be seen and nothing else is going on."""
        if (self.pages.currentIndex() == 0 and self.tabs.currentIndex() == 1
                and self._pending is None and self.join_ip.text().strip()):
            self._find_rooms(manual=False)

    def _find_rooms(self, manual):
        """Ask the server for its room list on a worker thread (never on the GUI thread).

        manual is True for the button: an empty IP is then reported to the
        user; the timer stays quiet. Only one request is in flight at a time.
        """
        host, port = parse_server(self.join_ip.text())
        if not host:
            if manual:
                self.rooms_status.setText("Enter the server address first.")
            return
        if self._rooms_busy:
            # One request at a time. A click (or a changed address) while an older
            # request runs is not lost: it runs as soon as that answer is in.
            self._rooms_again = "manual" if manual else (self._rooms_again or "auto")
            return
        self._rooms_busy = True
        if manual:
            self._tone(self.rooms_status, None)
            self.rooms_status.setText("Looking for rooms on %s:%d ..." % (host, port))
        threading.Thread(target=self._rooms_worker, args=(host, port),
                         name="room-list", daemon=True).start()

    def _rooms_worker(self, host, port):
        """Thread: fetch the list (a short-lived connection, see net_client.fetch_room_list) and report back."""
        rows, error = net_client.fetch_room_list(host, port)
        try:
            self._rooms_ready.emit("%s:%d" % (host, port), rows, error or "")
        except RuntimeError:
            pass  # the dialog was closed meanwhile

    def _show_rooms(self, asked, rows, error):
        """GUI thread: fill the table (or show why not).

        asked is "host:port" of the request. An answer for an address the user
        has since changed (host OR port) is dropped, and a request that waited
        behind this one (_rooms_again) runs now.
        """
        self._rooms_busy = False
        if self._rooms_again:
            queued, self._rooms_again = self._rooms_again, None
            self._find_rooms(manual=queued == "manual")
        host, port = parse_server(self.join_ip.text())
        if asked != "%s:%d" % (host, port):
            return
        if rows is None:
            self._tone(self.rooms_status, "error")
            self.rooms_status.setText("Cannot list rooms: %s" % error)
            return
        selected = self._selected_code()
        now = time.time()
        self.rooms_table.setUpdatesEnabled(False)
        self.rooms_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(room_cells(row, now)):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter if c >= 2 else Qt.AlignVCenter | Qt.AlignLeft)
                if c == LOCK_COLUMN and text == LOCK_ICON:
                    # A painted padlock (no emoji, no image file) next to the word.
                    item.setIcon(icons.get_icon("lock", 16, theme.tokens()["text_soft"]))
                    item.setToolTip("This room has a password")
                item.setData(Qt.UserRole, row)      # the double-click handler reads code and locked from here
                self.rooms_table.setItem(r, c, item)
            if row.get("code") == selected:
                self.rooms_table.selectRow(r)
        self.rooms_table.setUpdatesEnabled(True)
        self._tone(self.rooms_status, None)
        self.rooms_status.setText("%d room%s on this server. Double-click one to join it."
                                  % (len(rows), "" if len(rows) == 1 else "s") if rows
                                  else "No rooms on this server yet. Host one in the Host tab.")

    def _selected_code(self):
        """The room code of the selected table row, or None."""
        items = self.rooms_table.selectedItems()
        return (items[0].data(Qt.UserRole) or {}).get("code") if items else None

    def _room_chosen(self, row, _column):
        """Double-click: fill the room code; for a locked room ask for the password right away."""
        item = self.rooms_table.item(row, 0)
        info = item.data(Qt.UserRole) if item is not None else None
        if not isinstance(info, dict) or not info.get("code"):
            return
        self.join_code.setText(str(info["code"]))
        self.join_pw.clear()
        if info.get("locked"):
            text, ok = QInputDialog.getText(self, "Room password",
                                            "Room %s is locked. Password:" % info["code"],
                                            QLineEdit.Password)
            if ok:
                self.join_pw.setText(text)
        (self.go_btn if self.join_name.text().strip() else self.join_name).setFocus()

    # ------------------------------------------------------------------ flow

    def _show_error(self, text):
        """Show text in a red box under the form and enable it for another try."""
        self._tone(self.status, "error", box=True)
        self.status.setText(self._message_html(text))
        self.status.show()
        self._set_busy(False)

    def _show_notice(self, text):
        """Show why the join screen is back (amber box, not an error) and enable the form."""
        self._tone(self.status, "warning", box=True)
        self.status.setText(self._message_html(text))
        self.status.show()
        self._set_busy(False)

    def _set_busy(self, busy):
        """Disable the form while an attempt is running."""
        self.tabs.setEnabled(not busy)
        for button in self._segments.buttons():
            button.setEnabled(not busy)
        self.go_btn.setEnabled(not busy)
        if busy:
            self._tone(self.status, "info", box=True)
            self.status.setText("<b>Connecting...</b>")
            self.status.show()

    def _start(self):
        """Connect button: validate the form, then connect (or reuse the connection)."""
        if self.tabs.currentIndex() == 0:
            name, pw = self.host_name.text().strip(), self.host_pw.text()
            mode, host, port, msg_type = "host", "127.0.0.1", config.PORT, protocol.CREATE_ROOM
            payload = {"username": name}
        else:
            name, pw = self.join_name.text().strip(), self.join_pw.text()
            host, port = parse_server(self.join_ip.text())
            code = self.join_code.text().strip().upper()
            mode, msg_type = "join", protocol.JOIN_ROOM
            payload = {"room": code, "username": name}
            if not host:
                return self._show_error("Enter the server address shown on the host's screen.")
            if len(code) != 6:
                return self._show_error("The room code has 6 characters.")
        if not name:
            return self._show_error("Enter your name.")
        if pw:
            payload["password"] = pw

        global _active
        _active = self
        if mode == "join":
            self._remember(self.join_ip.text().strip(), name)
        self._pending = (mode, host, msg_type, payload)
        self._set_busy(True)
        # After an ERROR the server keeps the connection open, so a retry to
        # the same server just sends the request again.
        if self.net.is_connected and self.net.host == host and self.net.port == port:
            self._send_request()
        else:
            self.net.connect_to(host, port)

    def _send_request(self):
        """Send the pending CREATE_ROOM / JOIN_ROOM."""
        _mode, _host, msg_type, payload = self._pending
        self.net.send(msg_type, payload)

    def _on_connected(self):
        """NetClient connected: send the request we were waiting to send."""
        if self._pending is not None:
            self._send_request()

    def _on_disconnected(self, reason):
        """The connection failed or dropped before JOIN_OK."""
        if self._pending is None:
            return
        hint = ""
        if self._pending[0] == "host":
            hint = "\nIs the server running? Start it with: python run_server.py"
        elif not reason.startswith(net_client.CONNECT_MESSAGES):
            # Join: the connection went silent or dropped before JOIN_OK.
            # (A failed connect already carries its own advice, see
            # net_client.describe_connect_error, so it gets no extra hint.)
            hint = "\n\n" + JOIN_NETWORK_HINT % (self.net.port or config.PORT)
        self._show_error(reason + hint)

    def join_ok(self, msg):
        """JOIN_OK arrived: show the room code (host) or close the dialog (join)."""
        mode = self._pending[0] if self._pending else "join"
        self._pending = None
        self._finish()
        if mode == "host":
            self.code_label.setText((msg.get("payload") or {}).get("room", ""))
            self.ip_label.setText("Looking up addresses...")
            self.other_ips_label.setText("")
            self.pages.setCurrentIndex(1)
            # Listing adapters runs `ipconfig` on Windows (~100 ms): do it
            # off the GUI thread and fill the labels when it is done.
            threading.Thread(target=self._address_worker, name="lan-addresses",
                             daemon=True).start()
        else:
            self.accept()

    def _address_worker(self):
        """Thread: list this PC's IPv4 addresses and hand them to the GUI thread."""
        try:
            self._addresses_ready.emit(netinfo.local_addresses())
        except RuntimeError:
            pass  # the dialog was closed meanwhile

    def _show_addresses(self, addresses):
        """GUI thread: likely IP in large text, the other usable ones below it."""
        best = netinfo.likely_address(addresses)
        if best is None:
            self.ip_label.setText("No network")
            self.other_ips_label.setText("This PC has no usable network address.\n"
                                         "Connect it to the shared Wi-Fi / hotspot, then restart.")
            self.copy_btn.setEnabled(False)
            return
        self.ip_label.setText(best.ip)
        others = ["%s: %s" % (a.name, a.ip) for a in addresses if a.usable and not a.likely]
        text = "Port %d" % (self.net.port or config.PORT)
        if others:
            text += "\nOther addresses of this PC (if the big one does not work):\n" + "\n".join(others)
        self.other_ips_label.setText(text)

    def join_error(self, payload):
        """ERROR for our CREATE/JOIN: show it as a friendly message."""
        self._show_error(friendly_error(payload))

    def _finish(self):
        """Stop forwarding messages to this dialog and drop its signal links."""
        global _active
        if _active is self:
            _active = None
        for sig, slot in ((self.net.connected, self._on_connected),
                          (self.net.disconnected, self._on_disconnected)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    def done(self, result):
        """Closing the dialog (accept or reject) detaches it from NetClient and stops the room-list timer."""
        self._room_timer.stop()
        self._finish()
        super().done(result)


# --- message handlers (registered once; forward to the open dialog) ---------

@on_message(protocol.JOIN_OK)
def _on_join_ok(app, msg):
    """JOIN_OK: tell the waiting dialog, if one is open.

    net_client's own JOIN_OK handler (registered earlier) already filled app.me.
    """
    if _active is not None:
        _active.join_ok(msg)
    else:
        app.toast("Back in room %s" % (msg.get("payload") or {}).get("room", ""))


@on_message(protocol.ERROR)
def _on_error(app, msg):
    """ERROR: show it in the dialog for CREATE/JOIN, otherwise as a toast."""
    payload = msg.get("payload") or {}
    if _active is not None and payload.get("ref_type") in (protocol.CREATE_ROOM, protocol.JOIN_ROOM):
        _active.join_error(payload)
    else:
        app.toast(friendly_error(payload))
