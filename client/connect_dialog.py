# OWNER: P1
"""connect_dialog.py: The dialog shown at start-up: host a new room or join one.

Two tabs:
    Host: username, password (optional). Connects to the server on this
          machine (127.0.0.1) and sends CREATE_ROOM. On success it shows the
          room code and this machine's LAN IP in large text, so the host
          can read them out to the lab, then "Open whiteboard".
    Join: server IP, room code, username, password. Sends JOIN_ROOM and
          opens the whiteboard on JOIN_OK.

Flow (nothing here blocks the GUI thread):

    click  -> net.connect_to(ip)  (background thread)
           -> NetClient.connected  -> send CREATE_ROOM / JOIN_ROOM
           -> JOIN_OK  (on_message handler below)  -> success
           -> ERROR    (on_message handler below)  -> friendly message,
                                                      connection kept for a retry

The ERROR handler also turns errors that arrive later (FORBIDDEN, ...) into
a toast when no dialog is open.
"""

import socket

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QApplication, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from common import config, protocol
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

# The dialog currently waiting for JOIN_OK / ERROR, if any. The on_message
# handlers below are registered once at import time and forward to it.
_active = None


def friendly_error(payload):
    """Return a user-facing sentence for an ERROR payload {code, text, ref_type}."""
    code = payload.get("code")
    return FRIENDLY_ERRORS.get(code) or payload.get("text") or "Error: %s" % code


def lan_ip():
    """Return this machine's LAN IP address (what others type in the Join tab).

    Connecting a UDP socket sends no packet; it only makes the OS pick the
    outgoing interface, whose address getsockname() reports. Falls back to
    127.0.0.1 without a network.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class ConnectDialog(QDialog):
    """Host-or-join dialog. exec_() returns Accepted once we are in a room."""

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
        self.join_ip = QLineEdit(placeholderText="e.g. 192.168.1.20")
        self.join_code = QLineEdit(placeholderText="6 characters, e.g. K7QX2M")
        self.join_code.setMaxLength(6)
        self.join_name = QLineEdit()
        self.join_pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="if the room has one")
        form.addRow("Server IP", self.join_ip)
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
        """Page 1 (host only): room code and LAN IP in large text."""
        page = QWidget()
        big = QFont()
        big.setPointSize(32)
        big.setBold(True)
        self.code_label = QLabel()
        self.ip_label = QLabel()
        for label in (self.code_label, self.ip_label):
            label.setFont(big)
            label.setAlignment(Qt.AlignCenter)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        copy_btn = QPushButton("Copy code and IP")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(
            "Room %s at %s" % (self.code_label.text(), self.ip_label.text())))
        open_btn = QPushButton("Open whiteboard")
        open_btn.setDefault(True)
        open_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Room code", alignment=Qt.AlignCenter))
        layout.addWidget(self.code_label)
        layout.addWidget(QLabel("Server IP (others type this in Join)", alignment=Qt.AlignCenter))
        layout.addWidget(self.ip_label)
        buttons = QHBoxLayout()
        buttons.addWidget(copy_btn)
        buttons.addStretch(1)
        buttons.addWidget(open_btn)
        layout.addLayout(buttons)
        return page

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
            mode, host, msg_type = "host", "127.0.0.1", protocol.CREATE_ROOM
            payload = {"username": name}
        else:
            name, pw = self.join_name.text().strip(), self.join_pw.text()
            host, code = self.join_ip.text().strip(), self.join_code.text().strip().upper()
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
        self._pending = (mode, host, msg_type, payload)
        self._set_busy(True)
        # After an ERROR the server keeps the connection open, so a retry to
        # the same server just sends the request again.
        if self.net.is_connected and self.net.host == host:
            self._send_request()
        else:
            self.net.connect_to(host, config.PORT)

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
        else:
            # Join: we never got an answer from the host (connect timed
            # out, was refused, or went silent before JOIN_OK). On a lab
            # LAN that is almost always one of these three causes.
            hint = "\n\n" + JOIN_NETWORK_HINT % (self.net.port or config.PORT)
        self._show_error(reason + hint)

    def join_ok(self, msg):
        """JOIN_OK arrived: show the room code (host) or close the dialog (join)."""
        mode = self._pending[0] if self._pending else "join"
        self._pending = None
        self._finish()
        if mode == "host":
            self.code_label.setText((msg.get("payload") or {}).get("room", ""))
            self.ip_label.setText(lan_ip())
            self.pages.setCurrentIndex(1)
        else:
            self.accept()

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
