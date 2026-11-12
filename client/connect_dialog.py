# OWNER: P1
"""connect_dialog.py: The dialog shown at start-up: host a new room or join one.

Two tabs:
    Host: username, password (optional). Connects to the server on this
          machine (127.0.0.1) and sends CREATE_ROOM. On success it shows the
          room code and ALL of this machine's IPv4 addresses (the likely
          Wi-Fi / hotspot one first, in large text), so the host can read
          them out to the lab, then "Open whiteboard".
    Join: server IP (or ip:port), room code, username, password. The last
          server IP and username are remembered (QSettings). "Test
          connection" only tries a TCP connect and reports the result. Sends
          JOIN_ROOM and opens the whiteboard on JOIN_OK.

Flow (nothing here blocks the GUI thread):

    click  -> net.connect_to(ip)  (background thread)
           -> NetClient.connected  -> send CREATE_ROOM / JOIN_ROOM
           -> JOIN_OK  (on_message handler below)  -> success
           -> ERROR    (on_message handler below)  -> friendly message,
                                                      connection kept for a retry

The ERROR handler also turns errors that arrive later (FORBIDDEN, ...) into
a toast when no dialog is open.
"""

import threading

from PyQt5.QtCore import QSettings, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QApplication, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from common import config, netinfo, protocol
from client import net_client
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


class ConnectDialog(QDialog):
    """Host-or-join dialog. exec_() returns Accepted once we are in a room."""

    # Worker threads report back through signals (queued onto the GUI thread):
    # widgets are only ever touched on the GUI thread.
    _addresses_ready = pyqtSignal(object)        # list of netinfo.Address
    _test_done = pyqtSignal(bool, str, float)    # ok, message, seconds

    def __init__(self, app, parent=None, notice=None):
        """Build the tabs for AppState app (app.net must be a NetClient).

        notice: optional text shown in red under the form when the dialog
        opens, e.g. why we are back here after being kicked.
        """
        super().__init__(parent)
        self.app = app
        self.net = app.net
        self._pending = None   # (mode, host, envelope type, payload) of the running attempt
        self.setWindowTitle("LAN Whiteboard: connect")
        self.setMinimumWidth(420)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_form_page())
        self.pages.addWidget(self._build_ready_page())
        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)

        self.net.connected.connect(self._on_connected)
        self.net.disconnected.connect(self._on_disconnected)
        self._addresses_ready.connect(self._show_addresses)
        self._test_done.connect(self._show_test_result)
        self._load_remembered()
        if notice:
            self._show_error(notice)
            self.tabs.setCurrentIndex(1)    # a removed user would join, not host

    # ----------------------------------------------------------------- layout

    def _build_form_page(self):
        """Page 0: the Host and Join tabs, a status line and the buttons."""
        page = QWidget()
        self.tabs = QTabWidget()

        host_tab = QWidget()
        form = QFormLayout(host_tab)
        self.host_name = QLineEdit()
        self.host_pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="optional")
        form.addRow("Username", self.host_name)
        form.addRow("Password", self.host_pw)
        form.addRow(QLabel("Start the server on this PC first: python run_server.py"))
        self.tabs.addTab(host_tab, "Host")

        join_tab = QWidget()
        form = QFormLayout(join_tab)
        self.join_ip = QLineEdit(placeholderText="e.g. 192.168.43.57")
        self.join_code = QLineEdit(placeholderText="6 characters, e.g. K7QX2M")
        self.join_code.setMaxLength(6)
        self.join_name = QLineEdit()
        self.join_pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="if the room has one")
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setToolTip("Only tries to open a TCP connection to the server; joins nothing.")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        ip_row = QHBoxLayout()
        ip_row.addWidget(self.join_ip, 1)
        ip_row.addWidget(self.test_btn)
        form.addRow("Server IP", ip_row)
        form.addRow("", self.test_result)
        form.addRow("Room code", self.join_code)
        form.addRow("Username", self.join_name)
        form.addRow("Password", self.join_pw)
        self.tabs.addTab(join_tab, "Join")

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.go_btn = QPushButton("Connect")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._start)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.go_btn)

        layout = QVBoxLayout(page)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        return page

    def _build_ready_page(self):
        """Page 1 (host only): room code and the server's IPs in large text.

        The likely Wi-Fi / hotspot IP is big; the other usable addresses of
        this PC are listed small below it, with their adapter names, in case
        the big one is the wrong network.
        """
        page = QWidget()
        big = QFont()
        big.setPointSize(32)
        big.setBold(True)
        self.code_label = QLabel()
        self.ip_label = QLabel("Looking up addresses...")
        for label in (self.code_label, self.ip_label):
            label.setFont(big)
            label.setAlignment(Qt.AlignCenter)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.other_ips_label = QLabel()
        self.other_ips_label.setAlignment(Qt.AlignCenter)
        self.other_ips_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy_btn = QPushButton("Copy code and IP")
        self.copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(
            "Room %s at %s" % (self.code_label.text(), self.ip_label.text())))
        open_btn = QPushButton("Open whiteboard")
        open_btn.setDefault(True)
        open_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Room code", alignment=Qt.AlignCenter))
        layout.addWidget(self.code_label)
        layout.addWidget(QLabel("Server IP (others type this in Join)", alignment=Qt.AlignCenter))
        layout.addWidget(self.ip_label)
        layout.addWidget(self.other_ips_label)
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(open_btn)
        layout.addLayout(buttons)
        return page

    # ------------------------------------------------------------ remembered

    def _load_remembered(self):
        """Prefill the Join tab with the last server IP and username (QSettings).

        If nothing was stored yet (or the settings store is unusable) the
        fields simply stay empty.
        """
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.join_ip.setText(str(settings.value(KEY_LAST_IP, "") or ""))
        self.join_name.setText(str(settings.value(KEY_LAST_NAME, "") or ""))

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
            self.test_result.setStyleSheet("color: #c0392b;")
            self.test_result.setText("Enter the server IP first.")
            return
        self.test_btn.setEnabled(False)
        self.test_result.setStyleSheet("")
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
        self.test_result.setStyleSheet("color: #1e8449;" if ok else "color: #c0392b;")
        self.test_result.setText("%s (%.0f ms)" % (message, seconds * 1000) if ok else message)

    # ------------------------------------------------------------------ flow

    def _show_error(self, text):
        """Show text in red and enable the form for another try."""
        self.status.setStyleSheet("color: #c0392b;")
        self.status.setText(text)
        self._set_busy(False)

    def _set_busy(self, busy):
        """Disable the form while an attempt is running."""
        self.tabs.setEnabled(not busy)
        self.go_btn.setEnabled(not busy)
        if busy:
            self.status.setStyleSheet("")
            self.status.setText("Connecting...")

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
                return self._show_error("Enter the server IP shown on the host's screen.")
            if len(code) != 6:
                return self._show_error("The room code has 6 characters.")
        if not name:
            return self._show_error("Enter a username.")
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
        """Closing the dialog (accept or reject) always detaches it from NetClient."""
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
